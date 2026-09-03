#!/usr/bin/env bash
# Install the F2 factory user unit ON alcyone (docs/factory/F2_RUNBOOK.md).
#   bash deploy/spark/install_factory_unit.sh
# Idempotent: re-copies the template, reloads the user manager, prints the
# start/watch commands. Nothing is enabled or started: a factory run is
# launched on purpose with `systemctl --user start mp-factory@<run_id>`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

if [ "$REPO_DIR" != "$HOME/projects/money_printer" ]; then
  echo "WARN: this checkout is $REPO_DIR but the unit runs %h/projects/money_printer"
  echo "      (WorkingDirectory + MP_REPO_DIR) -- edit the .service before starting."
fi
if ! id -nG | tr ' ' '\n' | grep -qx docker; then
  echo "WARN: $(id -un) is not in the docker group; the user unit runs as $(id -un)"
  echo "      and needs it (sudo usermod -aG docker $(id -un); re-login)."
fi
if ! docker image inspect money-printer-lab:latest >/dev/null 2>&1; then
  echo "WARN: image money-printer-lab:latest not present -- build it first:"
  echo "      docker compose -f $HERE/docker-compose.lab.yml build"
fi
for f in "$HERE/mp_factory_run.sh" "$HERE/mp_factory_notify.sh" "$REPO_DIR/scripts/factory_bench_coexist.py"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; exit 1; }
done
bash -n "$HERE/mp_factory_run.sh"
bash -n "$HERE/mp_factory_notify.sh"

# Bind sources + nested mountpoints (compose header): docker would create a
# missing bind SOURCE as root, and cannot mkdir inside the :ro /app bind.
(cd "$REPO_DIR" && mkdir -p logs data/factory data/factory/runs data/ladders_holdout data/ladders_2026-09 reports/factory)

mkdir -p "$UNIT_DIR"
install -m 0644 "$HERE/systemd/mp-factory@.service" "$UNIT_DIR/"
systemctl --user daemon-reload

if ! loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  echo "NOTE: lingering is off for $(id -un): a user unit dies with the last login session."
  echo "      For a run that must survive an SSH drop:  loginctl enable-linger $(id -un)"
fi

echo
systemctl --user cat mp-factory@.service --no-pager | sed -n '1,3p'
echo
echo "Dry run   :  MP_FACTORY_DRY_RUN=1 bash $HERE/mp_factory_run.sh run_test"
echo "Start     :  systemctl --user start mp-factory@run_\$(date -u +%F)"
echo "Watch     :  journalctl --user -u mp-factory@run_\$(date -u +%F) -f"
echo "Status    :  systemctl --user status mp-factory@run_\$(date -u +%F) --no-pager"
