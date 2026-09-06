#!/usr/bin/env bash
# Silent weekly-reconcile watch for `hermes cron --no-agent` (mp_watch.sh style):
# prints ONLY when something is wrong. Empty stdout = no Discord message.
#
# Two sources, checked in this order:
#   1. The wrapper's host-side status file, written on EVERY exit of
#      deploy/spark/factory_reconcile.sh (default
#      ~/.local/state/money_printer/factory_reconcile/last_run.json):
#      a non-zero exit -- HOST_UNRESOLVED (4), FRAME_MISSING (5), DOCKER_FAILED (6),
#      DISCREPANCY (1), REFUSED (3), usage (2) -- is reported the SAME DAY the timer
#      ran, once per run (deduped on finished_utc). This is what catches "the lab
#      container could not reach maia" on Monday afternoon rather than as an
#      "overdue report" a week later (red team 2026-09-06, item 3).
#   2. The newest reports/factory/paper_reconcile_<from>_<to>.json: a blocking
#      (DISCREPANCY) or REFUSED verdict is reported once (deduped on sha256), and a
#      report older than MP_RECONCILE_MAX_AGE_DAYS (default 8: the timer is weekly,
#      Monday 14:30Z) is reported once a day as OVERDUE.
#
# Install ON alcyone (Hermes only runs scripts under ~/.hermes/scripts/):
#   cp hermes_plugin/scripts/mp_factory_reconcile.sh ~/.hermes/scripts/
#   ~/.local/bin/hermes cron create 6h --name mp-factory-reconcile --no-agent \
#       --script mp_factory_reconcile.sh --deliver discord:1491982736989093961 \
#       --provider custom --model ykarout/Qwen3.5-9B-NVFP4
#   (same flag form mp_factory_board.sh documents; verified 2026-09-02 on alcyone.)
set -u
DIR="${MONEY_PRINTER_FACTORY_DIR:-$HOME/projects/money_printer/reports/factory}"
RUN_STATUS="${MP_RECONCILE_STATE_DIR:-$HOME/.local/state/money_printer/factory_reconcile}/last_run.json"
STATE_DIR="${HERMES_HOME:-$HOME/.hermes}/state"
STATE="${MP_FACTORY_RECONCILE_STATE:-$STATE_DIR/mp_factory_reconcile.sha}"
RUN_STATE="${MP_FACTORY_RECONCILE_RUN_STATE:-$STATE_DIR/mp_factory_reconcile.run}"
OVERDUE_STATE="${MP_FACTORY_RECONCILE_OVERDUE_STATE:-$STATE_DIR/mp_factory_reconcile.overdue}"
MAX_AGE_DAYS="${MP_RECONCILE_MAX_AGE_DAYS:-8}"

mkdir -p "$STATE_DIR" 2>/dev/null || { STATE="/tmp/mp_factory_reconcile.sha"; RUN_STATE="/tmp/mp_factory_reconcile.run"; OVERDUE_STATE="/tmp/mp_factory_reconcile.overdue"; }

# --- 1. the wrapper's own last run (same-day failure report) ----------------------
if [ -f "$RUN_STATUS" ]; then
  if command -v python3 >/dev/null 2>&1; then
    run_line=$(python3 - "$RUN_STATUS" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"UNREADABLE|0|{exc}")
    raise SystemExit(0)
print(f"{d.get('finished_utc','?')}|{d.get('exit','?')}|{d.get('reason','?')} genome={d.get('genome_id','?')} window={d.get('window','?')} frame={d.get('frame') or '-'} sandbox_ip={d.get('sandbox_ip') or '-'} report={d.get('report') or '-'}")
PY
)
  else
    run_line="$(grep -o '"finished_utc":"[^"]*"' "$RUN_STATUS" | sed 's/.*://; s/"//g')|$(grep -o '"exit":[0-9]*' "$RUN_STATUS" | sed 's/.*://')|$(grep -o '"reason":"[^"]*"' "$RUN_STATUS" | sed 's/.*://; s/"//g')"
  fi
  finished=${run_line%%|*}
  rest=${run_line#*|}
  rc=${rest%%|*}
  detail=${rest#*|}
  last_seen=$(cat "$RUN_STATE" 2>/dev/null || true)
  if [ "$rc" != "0" ] && [ "$finished" != "$last_seen" ]; then
    printf '%s\n' "$finished" > "$RUN_STATE"
    case "$rc" in
      4) echo "⚠ factory weekly reconcile FAILED $finished: HOST_UNRESOLVED — the host could not resolve maia's name, so the lab container was never started ($detail)" ;;
      5) echo "⚠ factory weekly reconcile FAILED $finished: FRAME_MISSING — the promoted spec's search frame is not on alcyone; copy it before the next run ($detail)" ;;
      6) echo "⚠ factory weekly reconcile FAILED $finished: DOCKER_FAILED — compose could not run the lab container ($detail)" ;;
      1) echo "⚠ factory weekly reconcile $finished: DISCREPANCY (blocking rows) ($detail)" ;;
      3) echo "ℹ factory weekly reconcile $finished: REFUSED — nothing comparable this week (normal for a shadow week outside the frame) ($detail)" ;;
      *) echo "⚠ factory weekly reconcile FAILED $finished: exit $rc ($detail) — journalctl -u mp-factory-reconcile.service" ;;
    esac
  fi
fi

# --- 2. the newest report: overdue, or a bad verdict --------------------------------
[ -d "$DIR" ] || exit 0
newest=$(ls -t "$DIR"/paper_reconcile_*.json 2>/dev/null | head -1 || true)
now=$(date -u +%s)
if [ -z "$newest" ]; then
  age_days=999
else
  mtime=$(stat -c %Y "$newest" 2>/dev/null || stat -f %m "$newest" 2>/dev/null || echo 0)
  age_days=$(( (now - mtime) / 86400 ))
fi
if [ "$age_days" -gt "$MAX_AGE_DAYS" ]; then
  today=$(date -u +%F)
  last=$(cat "$OVERDUE_STATE" 2>/dev/null || true)
  if [ "$last" != "$today" ]; then
    if [ -z "$newest" ]; then
      echo "⚠ factory weekly reconcile: NO paper_reconcile_*.json under $DIR (timer never produced one) — journalctl -u mp-factory-reconcile.service"
    else
      echo "⚠ factory weekly reconcile OVERDUE: newest report $(basename "$newest") is ${age_days} d old (> ${MAX_AGE_DAYS}) — journalctl -u mp-factory-reconcile.service"
    fi
    printf '%s\n' "$today" > "$OVERDUE_STATE"
  fi
fi
[ -n "$newest" ] || exit 0

if command -v python3 >/dev/null 2>&1; then
  line=$(python3 - "$newest" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"UNREADABLE {exc}")
    raise SystemExit(0)
s = d.get("summary") or {}
v = str(s.get("verdict") or "?")
bits = [f"fills={s.get('n_sandbox_fills_settled')}", f"lab={s.get('n_lab_trades')}",
        f"lab_only_unexplained={s.get('n_lab_only_unexplained')}", f"sandbox_only={s.get('n_sandbox_only')}",
        f"sign_mismatch={s.get('n_settlement_sign_mismatch')}"]
reasons = list(s.get("blocking") or []) + list(s.get("refused_reasons") or [])
print(v + " " + " ".join(bits) + ((" | " + "; ".join(str(r) for r in reasons[:3])) if reasons else ""))
PY
)
else
  line=$(grep -o '"verdict": *"[^"]*"' "$newest" | head -1 | sed 's/.*: *"//; s/"$//')
fi
verdict=${line%% *}
case "$verdict" in
  OK|PARTIAL) exit 0 ;;
esac
hash=$(sha256sum "$newest" | cut -d' ' -f1)
last=$(cat "$STATE" 2>/dev/null || true)
[ "$hash" = "$last" ] && exit 0
printf '%s\n' "$hash" > "$STATE"
echo "⚠ factory weekly reconcile $(basename "$newest" .json): $line"
