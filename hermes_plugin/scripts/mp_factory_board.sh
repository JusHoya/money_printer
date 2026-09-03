#!/usr/bin/env bash
# Silent factory-board monitor for `hermes cron --no-agent` (mp_watch.sh style):
# prints reports/factory/<run>/board.md ONLY when its sha256 differs from the
# hash recorded on the previous run. Empty stdout = no Discord message.
#
# Why the hash lives here: `hermes cron --monitor-script` is the built-in
# byte-hash mode, but it is incompatible with `--no-agent` (it exists to gate an
# AGENT run), so a no-agent job must dedupe itself. board.md is timestamp-free
# (src/factory/report.py) so the hash only changes when the numbers change.
#
# Install ON alcyone (Hermes only runs scripts under ~/.hermes/scripts/):
#   cp hermes_plugin/scripts/mp_factory_board.sh ~/.hermes/scripts/
#   ~/.local/bin/hermes cron create 60m --name mp-factory-board --no-agent \
#       --script mp_factory_board.sh --deliver discord:1491982736989093961 \
#       --provider custom --model ykarout/Qwen3.5-9B-NVFP4
#   (flags verified 2026-09-02 against `hermes cron add --help` on alcyone: the
#    schedule is the positional argument, `add` is an alias of `create`; the
#    provider/model pin is a no-op for a no-agent job but every mp-* cron carries
#    it so a later edit that adds a prompt cannot drift_skip.)
#   ~/.local/bin/hermes cron list      # confirm Mode: no-agent, Script: mp_factory_board.sh
#
# Env for the gateway (~/.hermes/.env on alcyone): MONEY_PRINTER_FACTORY_DIR=
# /home/jushoya/projects/money_printer/reports/factory. The default below is
# that alcyone path; the plugin's ~/money_printer default does not exist there.
#
# Discord hard-caps a message at 2000 chars; the board is ~1.2k for five lanes.
# If it ever exceeds the cap, Hermes chunks the delivery.
set -u
DIR="${MONEY_PRINTER_FACTORY_DIR:-$HOME/projects/money_printer/reports/factory}"
STATE="${MP_FACTORY_STATE:-${HERMES_HOME:-$HOME/.hermes}/state/mp_factory_board.sha}"
LATEST="$DIR/latest.json"

[ -f "$LATEST" ] || exit 0   # no run yet: stay silent

# latest.json -> "board": "<run_id>/board.md" (relative to DIR). python3 is on
# alcyone; the grep/sed fallback covers a host without it.
if command -v python3 >/dev/null 2>&1; then
  rel=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("board",""))' "$LATEST" 2>/dev/null)
else
  rel=$(grep -o '"board": *"[^"]*"' "$LATEST" | head -1 | sed 's/.*: *"//; s/"$//')
fi
[ -n "$rel" ] || exit 0
BOARD="$DIR/$rel"
[ -f "$BOARD" ] || exit 0

hash=$(sha256sum "$BOARD" | cut -d' ' -f1)
last=$(cat "$STATE" 2>/dev/null || true)
[ "$hash" = "$last" ] && exit 0

mkdir -p "$(dirname "$STATE")" 2>/dev/null || STATE="/tmp/mp_factory_board.sha"
cat "$BOARD"
printf '%s\n' "$hash" > "$STATE"
