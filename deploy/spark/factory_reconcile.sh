#!/usr/bin/env bash
# Weekly lab-vs-paper reconcile of the promoted genome -- PRD_STRATEGY_FACTORY FR-F4.2,
# docs/factory/F3_RUNBOOK.md section 5, docs/FACTORY.md section 4.8. Runs ON alcyone
# from the checkout, normally via mp-factory-reconcile.timer (deploy/spark/systemd/).
# Safe to run by hand: bash deploy/spark/factory_reconcile.sh
#
# What it does
#   1. Finds the genome to reconcile: MP_GENOME_ID, else the id maia reports on
#      GET /api/genome (the deployed one). Refuses if no promoted spec exists for it.
#   2. Picks the reconcile window: the previous Monday..Sunday in UTC by default
#      (MP_RECONCILE_FROM / MP_RECONCILE_TO override, YYYY-MM-DD target_dates).
#   3. Resolves the sandbox host ON THE HOST (getent / avahi-resolve): `maia.local` is
#      an mDNS name and does NOT resolve inside the lab container (docker's 127.0.0.11
#      stub does not forward mDNS -- `getent hosts maia.local` is rc=2 in-container
#      while the host answers). The container is handed the IP in the URL itself
#      (`--url http://<ip>:8050`): `docker compose run` has no `--add-host` flag in
#      any Compose version (it is a `docker run` flag), and the first fire on
#      2026-09-07 died on exactly that -- `unknown flag: --add-host`, Compose v5.0.2 --
#      while the wrapper reported it as a "discrepancy". No resolution -> exit 4
#      HOST_UNRESOLVED.
#   4. Resolves the FROZEN SEARCH FRAME the spec was scored on:
#      data/factory/frames/*_<frame_search_sha256[:12]>. That directory is gitignored
#      and lives only in the lab -- which is why this job is on alcyone, not maia.
#      If the exact frame is absent the job exits 5 FRAME_MISSING <sha>: a report
#      against the wrong frame is worse than no report, so there is NO fallback.
#   5. Runs scripts/factory_paper_reconcile.py inside the `lab` compose service
#      (pandas/pyarrow live in the lab image; `lab` keeps its network -- the `factory`
#      service is network_mode: none by design):
#        --promoted configs/factory/promoted/<id>.json --url http://maia.local:8050
#        --from <mon> --to <sun> --frames <frame dir>
#      which writes reports/factory/paper_reconcile_<from>_<to>.{json,md}.
#   6. Commits exactly the two report files it produced (explicit paths, never
#      `git add -A`, never a push) unless MP_RECONCILE_NO_COMMIT=1.
#   7. ALWAYS writes a one-line JSON status to $MP_RECONCILE_STATE_DIR/last_run.json
#      (default ~/.local/state/money_printer/factory_reconcile/, host-side, outside
#      the checkout) so the Hermes watch can report a failed run the SAME DAY instead
#      of noticing an "overdue" report a week later.
#
# Exit codes
#   0 OK/PARTIAL, 1 DISCREPANCY (blocking rows), 2 usage / no spec / no python,
#   3 REFUSED (nothing comparable -- the normal answer for a SHADOW week outside the
#     frozen frame: 0 fills against 0 lab trades is not a pass, the report says so),
#   4 HOST_UNRESOLVED (maia's name did not resolve on the host),
#   5 FRAME_MISSING (the spec's search frame is not on this box),
#   6 DOCKER_FAILED (compose could not run the container at all).
#
# Env overrides
#   MP_REPO_DIR              checkout to operate on (default: this script's repo)
#   MONEY_PRINTER_URL        sandbox base URL (default http://maia.local:8050)
#   MP_SANDBOX_IP            skip host-side resolution and use this IP
#   MP_GENOME_ID             genome id (default: maia's /api/genome)
#   MP_RECONCILE_FROM/TO     window (default previous Mon..Sun UTC)
#   MP_RECONCILE_FRAMES      frame dir (default: resolved from the spec's frame_search_sha256; still checked)
#   MP_RECONCILE_STATE_DIR   where last_run.json goes (default ~/.local/state/money_printer/factory_reconcile)
#   MP_RECONCILE_DRY_RUN=1   print the plan and stop before docker
#   MP_RECONCILE_NO_COMMIT=1 leave the report files uncommitted
set -uo pipefail

REPO_DIR="${MP_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
URL="${MONEY_PRINTER_URL:-http://maia.local:8050}"
COMPOSE="$REPO_DIR/deploy/spark/docker-compose.lab.yml"
STATE_DIR="${MP_RECONCILE_STATE_DIR:-$HOME/.local/state/money_printer/factory_reconcile}"
STATUS_FILE="$STATE_DIR/last_run.json"
cd "$REPO_DIR" || exit 2

EXIT_USAGE=2; EXIT_REFUSED=3; EXIT_HOST_UNRESOLVED=4; EXIT_FRAME_MISSING=5; EXIT_DOCKER_FAILED=6
GID=""; FROM=""; TO=""; FRAMES=""; SANDBOX_IP=""; STEM=""

# --- status file: written on EVERY exit path (trap), read by the Hermes watch ------
write_status() {
  local rc="$1" reason="$2"
  mkdir -p "$STATE_DIR" 2>/dev/null || return 0
  printf '{"finished_utc":"%s","exit":%s,"reason":"%s","genome_id":"%s","window":"%s..%s","frame":"%s","sandbox_ip":"%s","report":"%s","host":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" "$reason" "$GID" "$FROM" "$TO" "$FRAMES" "$SANDBOX_IP" \
    "${STEM:+reports/factory/$STEM.json}" "$(hostname)" > "$STATUS_FILE" 2>/dev/null || true
}
REASON="unknown"
trap 'rc=$?; write_status "$rc" "$REASON"; exit $rc' EXIT

die() { local code="$1"; shift; REASON="$*"; echo "factory_reconcile: $*" >&2; exit "$code"; }

# --- python for JSON (python3 on alcyone; `python` on a dev box) -------------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c pass >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die $EXIT_USAGE "no python interpreter on PATH"

# --- 3. resolve the sandbox host ON THE HOST ----------------------------------
HOSTNAME_PART=$(printf '%s' "$URL" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
resolve_host() {
  local name="$1" ip=""
  if [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo "$name"; return 0; fi
  ip=$(getent hosts "$name" 2>/dev/null | awk '{print $1; exit}')
  if [ -z "$ip" ] && command -v avahi-resolve >/dev/null 2>&1; then
    ip=$(avahi-resolve -4 -n "$name" 2>/dev/null | awk '{print $2; exit}')
  fi
  if [ -z "$ip" ] && command -v getent >/dev/null 2>&1; then
    ip=$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1; exit}')
  fi
  [ -n "$ip" ] && echo "$ip"
}
SANDBOX_IP="${MP_SANDBOX_IP:-$(resolve_host "$HOSTNAME_PART")}"
[ -n "$SANDBOX_IP" ] || die $EXIT_HOST_UNRESOLVED "HOST_UNRESOLVED $HOSTNAME_PART: the host could not resolve the sandbox name (mDNS/avahi down?); the lab container cannot resolve it at all, so nothing is run. Set MP_SANDBOX_IP=<ip> to override"
[[ "$SANDBOX_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die $EXIT_HOST_UNRESOLVED "HOST_UNRESOLVED $HOSTNAME_PART: resolved to '$SANDBOX_IP', not an IPv4 address"

# --- 1. which genome ---------------------------------------------------------
GID="${MP_GENOME_ID:-}"
if [ -z "$GID" ]; then
  GID=$(curl -sS --max-time 15 --resolve "$HOSTNAME_PART:8050:$SANDBOX_IP" "$URL/api/genome" 2>/dev/null \
        | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print((d.get("genome") or {}).get("genome_id") or "")' 2>/dev/null || true)
fi
[ -n "$GID" ] || die $EXIT_USAGE "no genome id: set MP_GENOME_ID or make $URL/api/genome answer (host $SANDBOX_IP)"
[[ "$GID" =~ ^[0-9a-f]{16}$ ]] || die $EXIT_USAGE "genome id '$GID' is not a 16-hex genome_id"
SPEC="configs/factory/promoted/$GID.json"
[ -f "$SPEC" ] || die $EXIT_USAGE "no promoted spec at $SPEC (git pull --ff-only first?)"

# --- 2. window -----------------------------------------------------------------
if [ -n "${MP_RECONCILE_FROM:-}" ] || [ -n "${MP_RECONCILE_TO:-}" ]; then
  FROM="${MP_RECONCILE_FROM:-}"; TO="${MP_RECONCILE_TO:-}"
  [ -n "$FROM" ] && [ -n "$TO" ] || die $EXIT_USAGE "MP_RECONCILE_FROM and MP_RECONCILE_TO go together"
else
  dow=$(date -u +%u)                      # 1 = Monday .. 7 = Sunday
  TO=$(date -u -d "-$dow days" +%F)       # last Sunday
  FROM=$(date -u -d "$TO -6 days" +%F)
fi
[[ "$FROM" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$TO" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die $EXIT_USAGE "bad window $FROM..$TO"
STEM="paper_reconcile_${FROM}_${TO}"

# --- 4. the frame the spec was scored on -- exact match or FRAME_MISSING -------
SHA12=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["frame_search_sha256"][:12])' "$SPEC")
FRAMES="${MP_RECONCILE_FRAMES:-}"
if [ -z "$FRAMES" ]; then
  FRAMES=$(ls -d data/factory/frames/*_"$SHA12" 2>/dev/null | head -1 || true)
fi
[ -n "$FRAMES" ] && [ -d "$FRAMES/search" ] || die $EXIT_FRAME_MISSING "FRAME_MISSING $SHA12: no data/factory/frames/*_$SHA12/search on this box for spec $GID (frame_search_sha256 $SHA12...). A report against another frame would not be the promoted spec's lab trade set, so there is no fallback -- copy the frame dir here first (docs/FACTORY.md section 4.8)"
if [ -f "$FRAMES/frame.sha256" ] && ! grep -q "^$SHA12" "$FRAMES/frame.sha256" 2>/dev/null; then
  if ! grep -q "$SHA12" "$FRAMES/frame.sha256" 2>/dev/null; then
    die $EXIT_FRAME_MISSING "FRAME_MISSING $SHA12: $FRAMES/frame.sha256 does not list the spec's search sha; refusing to score against it"
  fi
fi

echo "factory_reconcile: genome $GID  window $FROM..$TO  frame $FRAMES  sandbox $URL ($HOSTNAME_PART -> $SANDBOX_IP)"
if [ "${MP_RECONCILE_DRY_RUN:-0}" = "1" ]; then
  REASON="dry_run"
  echo "factory_reconcile: dry run -- stopping before docker"
  exit 0
fi

# --- 5. run inside the lab image (network ON; the sandbox is addressed by IP, because
#        the name does not resolve in-container and `compose run` cannot inject one) --
export LAB_UID="$(id -u)" LAB_GID="$(id -g)"
RUN_URL="${URL//$HOSTNAME_PART/$SANDBOX_IP}"
docker compose -f "$COMPOSE" run --rm -T lab \
  python scripts/factory_paper_reconcile.py \
    --promoted "$SPEC" --url "$RUN_URL" --from "$FROM" --to "$TO" --frames "$FRAMES"
rc=$?
# The reconcile script's own exit 1 means "discrepancy" and ALWAYS writes the report;
# `docker compose` also exits 1 when it never ran the script at all (bad flag, missing
# image, compose file error). Tell them apart by whether the report exists, so an
# invocation failure is never posted as a trading finding.
REPORT="reports/factory/$STEM.json"
case "$rc" in
  0) REASON="ok" ;;
  1) if [ -f "$REPORT" ]; then REASON="discrepancy"; else REASON="invocation_failed"; rc=$EXIT_DOCKER_FAILED; fi ;;
  2) REASON="usage" ;;
  3) REASON="refused" ;;
  125|126|127) REASON="docker_failed"; rc=$EXIT_DOCKER_FAILED ;;
  *) REASON="reconcile_exit_$rc" ;;
esac
echo "factory_reconcile: reconcile exit $rc ($REASON) -> reports/factory/$STEM.{json,md}  (sandbox $URL as $RUN_URL)"

# --- 6. commit exactly what was produced --------------------------------------
if [ "${MP_RECONCILE_NO_COMMIT:-0}" != "1" ] && [ -f "reports/factory/$STEM.json" ]; then
  git add -- "reports/factory/$STEM.json" "reports/factory/$STEM.md" 2>/dev/null || true
  if ! git diff --cached --quiet -- "reports/factory/$STEM.json" "reports/factory/$STEM.md"; then
    git commit -q -m "data(paper-reconcile): $GID week $FROM..$TO (exit $rc)" \
      -- "reports/factory/$STEM.json" "reports/factory/$STEM.md"
    echo "factory_reconcile: committed reports/factory/$STEM.{json,md} (not pushed)"
  fi
fi
exit "$rc"
