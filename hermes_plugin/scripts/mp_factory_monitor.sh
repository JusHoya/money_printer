#!/usr/bin/env bash
# Silent factory-run progress monitor for `hermes cron --no-agent`
# (mp_factory_board.sh style; PRD_STRATEGY_FACTORY Phase F2 exit criterion
# "Discord receives >=3 monitor posts during the run and one completion message").
#
# Follows reports/factory/latest.json -> "status": "<run_id>/status.json"
# (EVOLVE writes that pointer plus "active_run" at run start; the gen-0 report
# also carries a "status" pointer, so the script works on a fresh checkout).
# Prints ONE compact line
#   factory <run_id> <state> <phase> <campaign> gen <g>/<n> best_fit <x> phenotypes <n> evals <n>
# ONLY when the sha256 of status.json (+ completion.txt) differs from the hash
# recorded on the previous invocation; when reports/factory/<run_id>/completion.txt
# exists and has not been posted yet, its content follows the line (it is the
# completion message's fallback path, see deploy/spark/mp_factory_notify.sh).
# Empty stdout = no Discord message. status.json is timestamp-free, so the
# hash changes only when the numbers do (a 60-gen campaign posts ~every gen
# that lands inside a 10-min window, never twice for the same state).
#
# Install ON alcyone (Hermes only runs scripts under ~/.hermes/scripts/):
#   cp hermes_plugin/scripts/mp_factory_monitor.sh ~/.hermes/scripts/
#   ~/.local/bin/hermes cron create 10m --name mp-factory-monitor --no-agent \
#       --script mp_factory_monitor.sh --deliver discord:1491982736989093961 \
#       --provider custom --model ykarout/Qwen3.5-9B-NVFP4
#   (form verified on alcyone 2026-09-03: schedule positional; `--no-agent` +
#    `--script`; `--monitor-script` is the agent-gating byte-hash mode and is
#    incompatible with `--no-agent`, hence the self-hash below; the provider/model
#    pin is a no-op for a no-agent job but every mp-* cron carries it.)
#   ~/.local/bin/hermes cron list      # confirm Mode: no-agent, Script: mp_factory_monitor.sh
#
# Env for the gateway (~/.hermes/.env on alcyone): MONEY_PRINTER_FACTORY_DIR=
# /home/jushoya/projects/money_printer/reports/factory (default below).
# State: ${MP_FACTORY_MONITOR_STATE:-~/.hermes/state/mp_factory_monitor.sha}
# (+ ".completion" beside it for the once-only completion post).
set -u
DIR="${MONEY_PRINTER_FACTORY_DIR:-$HOME/projects/money_printer/reports/factory}"
STATE="${MP_FACTORY_MONITOR_STATE:-${HERMES_HOME:-$HOME/.hermes}/state/mp_factory_monitor.sha}"
LATEST="$DIR/latest.json"

[ -f "$LATEST" ] || exit 0   # no run yet: stay silent

# python3 on alcyone; `python` on the dev box; the Windows Store `python3` stub
# fails `-c pass`, so probe before trusting the name. grep/sed is the last resort.
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c pass >/dev/null 2>&1; then PY="$c"; break; fi
done
jget() {  # jget FILE KEY -> scalar value or ""
  if [ -n "$PY" ]; then
    "$PY" -c 'import json,sys
try:
    v = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], "")
except Exception:
    v = ""
print("" if v is None or isinstance(v, (dict, list)) else v)' "$1" "$2" 2>/dev/null
  else
    grep -o "\"$2\": *\"[^\"]*\"" "$1" | head -1 | sed 's/.*: *"//; s/"$//'
  fi
}

rel=$(jget "$LATEST" status)
[ -n "$rel" ] || exit 0
STATUS="$DIR/$rel"
[ -f "$STATUS" ] || exit 0
RUN_DIR=$(dirname "$STATUS")
COMPLETION="$RUN_DIR/completion.txt"

hash=$( { cat "$STATUS"; [ -f "$COMPLETION" ] && cat "$COMPLETION"; } | sha256sum | cut -d' ' -f1)
last=$(cat "$STATE" 2>/dev/null || true)
[ "$hash" = "$last" ] && exit 0

# --- the one line ------------------------------------------------------------
if [ -n "$PY" ]; then
  line=$("$PY" - "$STATUS" "$(basename "$RUN_DIR")" <<'PYEOF' 2>/dev/null
import json, sys
try:
    s = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    s = {}
def g(k, d="-"):
    v = s.get(k)
    return d if v is None or v == "" else v
bf = s.get("best_fit")
try:
    bf = f"{float(bf):+.4f}"
except (TypeError, ValueError):
    bf = "-"
print(f"factory {g('run_id', sys.argv[2])} {g('state')} {g('phase')} {g('campaign')} "
      f"gen {g('gen')}/{g('n_gens')} best_fit {bf} phenotypes {g('n_phenotypes')} evals {g('evaluations')}")
PYEOF
)
else
  line="factory $(jget "$STATUS" run_id) $(jget "$STATUS" state) $(jget "$STATUS" phase) $(jget "$STATUS" campaign) gen $(jget "$STATUS" gen)/$(jget "$STATUS" n_gens) best_fit $(jget "$STATUS" best_fit) phenotypes $(jget "$STATUS" n_phenotypes) evals $(jget "$STATUS" evaluations)"
fi
[ -n "$line" ] || line="factory $(basename "$RUN_DIR") (status.json unreadable)"
echo "$line"

# --- completion message, once ---------------------------------------------------
if [ -f "$COMPLETION" ]; then
  chash=$(sha256sum "$COMPLETION" | cut -d' ' -f1)
  clast=$(cat "$STATE.completion" 2>/dev/null || true)
  if [ "$chash" != "$clast" ]; then
    cat "$COMPLETION"
    mkdir -p "$(dirname "$STATE")" 2>/dev/null && printf '%s\n' "$chash" > "$STATE.completion"
  fi
fi

mkdir -p "$(dirname "$STATE")" 2>/dev/null || STATE="/tmp/mp_factory_monitor.sha"
printf '%s\n' "$hash" > "$STATE"
