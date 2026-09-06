#!/usr/bin/env bash
# Silent weekly-reconcile watch for `hermes cron --no-agent` (mp_watch.sh style):
# prints ONLY when the newest reports/factory/paper_reconcile_<from>_<to>.json is
# blocking (DISCREPANCY) or REFUSED, or when no report has appeared for longer
# than MP_RECONCILE_MAX_AGE_DAYS (default 8: the timer is weekly, Monday 14:30Z).
# Empty stdout = no Discord message.
#
# Install ON alcyone (Hermes only runs scripts under ~/.hermes/scripts/):
#   cp hermes_plugin/scripts/mp_factory_reconcile.sh ~/.hermes/scripts/
#   ~/.local/bin/hermes cron create 6h --name mp-factory-reconcile --no-agent \
#       --script mp_factory_reconcile.sh --deliver discord:1491982736989093961 \
#       --provider custom --model ykarout/Qwen3.5-9B-NVFP4
#   (same flag form mp_factory_board.sh documents; verified 2026-09-02 on alcyone.)
#
# Dedupe: the sha256 of the newest report is remembered under ~/.hermes/state so
# one bad week posts once, not every 6 h; the "overdue" line re-posts daily.
set -u
DIR="${MONEY_PRINTER_FACTORY_DIR:-$HOME/projects/money_printer/reports/factory}"
STATE_DIR="${HERMES_HOME:-$HOME/.hermes}/state"
STATE="${MP_FACTORY_RECONCILE_STATE:-$STATE_DIR/mp_factory_reconcile.sha}"
OVERDUE_STATE="${MP_FACTORY_RECONCILE_OVERDUE_STATE:-$STATE_DIR/mp_factory_reconcile.overdue}"
MAX_AGE_DAYS="${MP_RECONCILE_MAX_AGE_DAYS:-8}"

[ -d "$DIR" ] || exit 0
newest=$(ls -t "$DIR"/paper_reconcile_*.json 2>/dev/null | head -1 || true)

mkdir -p "$STATE_DIR" 2>/dev/null || { STATE="/tmp/mp_factory_reconcile.sha"; OVERDUE_STATE="/tmp/mp_factory_reconcile.overdue"; }

# --- overdue: no report at all, or the newest is older than the weekly cadence allows
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
      echo "⚠ factory weekly reconcile: NO paper_reconcile_*.json under $DIR (timer never ran?) — journalctl -u mp-factory-reconcile.service"
    else
      echo "⚠ factory weekly reconcile OVERDUE: newest report $(basename "$newest") is ${age_days} d old (> ${MAX_AGE_DAYS}) — journalctl -u mp-factory-reconcile.service"
    fi
    printf '%s\n' "$today" > "$OVERDUE_STATE"
  fi
fi
[ -n "$newest" ] || exit 0

# --- verdict of the newest report (python3 on alcyone; grep fallback)
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
