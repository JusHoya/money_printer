#!/usr/bin/env bash
# Install + enable the weekly lab-vs-paper reconcile timer ON alcyone (FR-F4.2).
#   bash deploy/spark/install_factory_reconcile.sh
# NEEDS ROOT (sudo for install/daemon-reload/enable) and STARTS THE TIMER (`enable --now`):
# the first run fires the next Monday 14:30Z, or immediately if one was missed
# (Persistent=true). Before running it:
#   * the promoted spec's frozen search frame must be on this box --
#     data/factory/frames/*_<frame_search_sha256[:12]> (for 0c4b20502f2daf65 that is
#     the bfcf94654a3a frame, which lives on the dev box, NOT the 0fdf39ea506b frame
#     alcyone has); the wrapper exits 5 FRAME_MISSING otherwise, no fallback;
#   * the host must resolve maia's name (getent/avahi); the wrapper passes the IP into
#     the container with --add-host because mDNS does not resolve inside it.
# Idempotent: re-copies the units, reloads systemd, (re)enables the timer.
# Mirrors install_ladder_capture.sh; the Hermes watch is a separate, optional step (below).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system
REPO_DIR="$(cd "$HERE/../.." && pwd)"

if [ "$REPO_DIR" != "$HOME/projects/money_printer" ]; then
  echo "WARN: this checkout is $REPO_DIR but the unit hard-codes"
  echo "      /home/jushoya/projects/money_printer -- edit the .service before enabling."
fi
if ! id -nG | tr ' ' '\n' | grep -qx docker; then
  echo "WARN: $(id -un) is not in the docker group; the service runs as that user"
  echo "      and needs it (sudo usermod -aG docker $(id -un); re-login)."
fi
for spec in "$REPO_DIR"/configs/factory/promoted/*.json; do
  [ -f "$spec" ] || continue
  sha12=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["frame_search_sha256"][:12])' "$spec" 2>/dev/null || true)
  [ -n "$sha12" ] || continue
  if ! ls -d "$REPO_DIR"/data/factory/frames/*_"$sha12" >/dev/null 2>&1; then
    echo "WARN: $(basename "$spec" .json) was scored on search frame $sha12..., which is NOT under"
    echo "      data/factory/frames on this box; the weekly run for that genome exits 5 FRAME_MISSING"
    echo "      (no fallback). Copy the frame dir here first (docs/FACTORY.md section 4.8)."
  fi
done
SANDBOX_HOST=$(printf '%s' "${MONEY_PRINTER_URL:-http://maia.local:8050}" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
if ! getent hosts "$SANDBOX_HOST" >/dev/null 2>&1 && ! avahi-resolve -4 -n "$SANDBOX_HOST" >/dev/null 2>&1; then
  echo "WARN: the HOST cannot resolve $SANDBOX_HOST (getent/avahi); the wrapper resolves it here and"
  echo "      injects the IP into the container (--add-host), so until this resolves the job exits 4"
  echo "      HOST_UNRESOLVED. Set MP_SANDBOX_IP=<ip> in the unit's Environment= as a workaround."
fi
if ! curl -fsS --max-time 10 "${MONEY_PRINTER_URL:-http://maia.local:8050}/healthz" >/dev/null 2>&1; then
  echo "WARN: ${MONEY_PRINTER_URL:-http://maia.local:8050}/healthz did not answer from here;"
  echo "      the job reads maia over HTTP and will exit 2 until it does."
fi

sudo install -m 0644 \
  "$HERE/systemd/mp-factory-reconcile.service" \
  "$HERE/systemd/mp-factory-reconcile.timer" \
  "$UNIT_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable --now mp-factory-reconcile.timer

echo
systemctl list-timers mp-factory-reconcile.timer --no-pager
echo
echo "Dry run now :  MP_RECONCILE_DRY_RUN=1 bash $HERE/factory_reconcile.sh"
echo "Real run now:  sudo systemctl start mp-factory-reconcile.service"
echo "Logs        :  journalctl -u mp-factory-reconcile.service -n 80 --no-pager"
echo
echo "Optional Hermes anomaly line (mp_watch.sh style, silent unless something is wrong):"
echo "  cp $REPO_DIR/hermes_plugin/scripts/mp_factory_reconcile.sh ~/.hermes/scripts/"
echo "  ~/.local/bin/hermes cron create 6h --name mp-factory-reconcile --no-agent \\"
echo "      --script mp_factory_reconcile.sh --deliver discord:1491982736989093961 \\"
echo "      --provider custom --model ykarout/Qwen3.5-9B-NVFP4"
