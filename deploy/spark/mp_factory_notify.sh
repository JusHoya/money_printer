#!/usr/bin/env bash
# Completion notification for one factory run -- Phase F2 exit criterion
# "Discord receives ... one completion message" (PRD_STRATEGY_FACTORY.md).
#
#   bash deploy/spark/mp_factory_notify.sh <run_id> DONE|FAILED
#
# ALWAYS writes reports/factory/<run_id>/completion.txt (timestamp-free body:
# run_id, state, verdict from summary.json if present, the pooled OOS line,
# wall seconds, used-GiB before/after/peak) -- the mp-factory-monitor cron
# (hermes_plugin/scripts/mp_factory_monitor.sh) posts that file once when it
# appears, so the message reaches Discord even if delivery below fails.
# Then tries direct delivery with the Hermes CLI (form verified on alcyone
# 2026-09-03; exit 0 ok / 1 delivery error / 2 usage; no gateway needed):
#   ~/.local/bin/hermes send --to discord:<chan> --subject "[factory] <run_id> DONE" --file completion.txt
# with the positional-message form as a fallback. Delivery failure is never
# fatal (exit 0 either way) -- the run's exit code is the wrapper's business.
#
# Env
#   MP_REPO_DIR            checkout (default: this script's repo)
#   HERMES_BIN             hermes binary (default ~/.local/bin/hermes; skipped if absent)
#   MP_FACTORY_DISCORD     delivery target (default discord:1491982736989093961)
#   MP_FACTORY_WALL_S      wall seconds (from the wrapper)
#   MP_FACTORY_USED_GIB    "before/after/peak" used GiB (from the wrapper)
#   MP_FACTORY_NO_SEND=1   write completion.txt only (tests)
set -euo pipefail

RUN_ID="${1:-}"; STATE="${2:-}"
[[ -n "$RUN_ID" && ( "$STATE" == "DONE" || "$STATE" == "FAILED" ) ]] || { echo "usage: $0 <run_id> DONE|FAILED" >&2; exit 2; }
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "bad run_id '$RUN_ID'" >&2; exit 2; }

REPO_DIR="${MP_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
REPORT_DIR="$REPO_DIR/reports/factory/$RUN_ID"
RUN_DIR="$REPO_DIR/data/factory/runs/$RUN_ID"
OUT="$REPORT_DIR/completion.txt"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
TARGET="${MP_FACTORY_DISCORD:-discord:1491982736989093961}"
mkdir -p "$REPORT_DIR"

# python3 (alcyone) or python (dev box) for JSON; the Windows Store stub fails `-c pass`.
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c pass >/dev/null 2>&1; then PY="$c"; break; fi
done

# Verdict + pooled OOS from summary.json (STATS report.py) or oos/pooled.json;
# every key is optional -- absence prints "n/a", never an error.
fam="n/a"; verdict="n/a"; pooled="n/a"
if [[ -n "$PY" ]]; then
  extracted="$("$PY" - "$REPORT_DIR/summary.json" "$RUN_DIR/oos/pooled.json" <<'PYEOF' || true
import json, sys
def load(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None
s = load(sys.argv[1]) or {}
fam = s.get("family") or "n/a"
v = s.get("verdict")
if v is None and isinstance(s.get("promotion"), dict):
    v = s["promotion"].get("verdict")
pooled = s.get("pooled") if isinstance(s.get("pooled"), dict) else None
if pooled is None and isinstance(s.get("oos"), dict) and isinstance(s["oos"].get("pooled"), dict):
    pooled = s["oos"]["pooled"]
if pooled is None:
    pooled = load(sys.argv[2])
def f(x, nd=4):
    try:
        return f"{float(x):+.{nd}f}"
    except Exception:
        return "n/a"
if isinstance(pooled, dict) and pooled:
    line = (f"mean={f(pooled.get('mean'))} se={f(pooled.get('se'))} t={f(pooled.get('t_stat'), 2)} "
            f"boot=[{f(pooled.get('boot_lo'))},{f(pooled.get('boot_hi'))}] n_dates={pooled.get('n_dates', 'n/a')}")
else:
    line = "n/a"
print(str(fam).replace(" ", "_"))
print(str(v or "n/a").replace(" ", "_"))
print(line)
PYEOF
)"
  if [[ -n "$extracted" ]]; then
    fam="$(printf '%s\n' "$extracted" | sed -n 1p)"
    verdict="$(printf '%s\n' "$extracted" | sed -n 2p)"
    pooled="$(printf '%s\n' "$extracted" | sed -n 3p)"
  fi
fi
wall="${MP_FACTORY_WALL_S:-n/a}"
used="${MP_FACTORY_USED_GIB:-n/a}"

{
  echo "factory run $RUN_ID: $STATE"
  echo "family: $fam"
  echo "verdict: $verdict"
  echo "pooled OOS (33 validation dates): $pooled"
  echo "wall_s: $wall"
  echo "used_gib before/after/peak: $used"
  echo "report: reports/factory/$RUN_ID/summary.md"
} > "$OUT"
echo "wrote $OUT"

[[ "${MP_FACTORY_NO_SEND:-0}" == "1" ]] && exit 0
if [[ ! -x "$HERMES_BIN" ]]; then
  echo "hermes not found at $HERMES_BIN; completion.txt left for the monitor cron" >&2
  exit 0
fi
subject="[factory] $RUN_ID $STATE"
if "$HERMES_BIN" send --to "$TARGET" --subject "$subject" --file "$OUT"; then
  echo "delivered via hermes send --file"
elif "$HERMES_BIN" send --to "$TARGET" --subject "$subject" "$(cat "$OUT")"; then
  echo "delivered via hermes send (positional message)"
else
  echo "hermes send failed (rc=$?); completion.txt left for the monitor cron" >&2
fi
exit 0
