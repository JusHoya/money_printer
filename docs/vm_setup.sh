#!/bin/bash
# Money Printer VM Setup Script
# Paste this into the Cloud Console SSH terminal after connecting.
# -------------------------------------------------------------------

set -e  # Stop on first error

echo "=== Step 1: System packages ==="
sudo apt update && sudo apt upgrade -y
sudo apt install -y software-properties-common build-essential libffi-dev libssl-dev git tmux

echo "=== Step 2: Python 3.14 ==="
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.14 python3.14-venv python3.14-dev || {
    echo "Python 3.14 not available in deadsnakes. Using system Python 3."
    echo "This is fine — all dependencies work on Python 3.12+."
    PYTHON=python3
}
PYTHON=${PYTHON:-python3.14}
$PYTHON --version

echo "=== Step 3: Clone repo ==="
cd ~
git clone https://github.com/JusHoya/money_printer.git
cd money_printer

echo "=== Step 4: Virtual environment + dependencies ==="
$PYTHON -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Step 5: Create directories ==="
mkdir -p data/models data/historical logs

echo ""
echo "============================================"
echo " Base setup complete!"
echo " Python: $($PYTHON --version)"
echo " Venv:   ~/money_printer/venv"
echo "============================================"
echo ""
echo " NEXT STEPS (do these manually):"
echo "   1. Upload .env and Kalshi private key (use the gear icon > Upload file in SSH window)"
echo "   2. Move them into place:"
echo "      mv ~/.env ~/money_printer/.env"
echo "      mv ~/Kalshi_priv_key_1_readOnly.key ~/money_printer/Kalshi_priv_key_1_readOnly.key"
echo "      chmod 600 ~/money_printer/Kalshi_priv_key_1_readOnly.key"
echo "   3. Test run:"
echo "      cd ~/money_printer && source venv/bin/activate"
echo "      python scripts/run_web_dashboard.py --auto-cycle --sim-balance 3000 --host 0.0.0.0 --port 8050 --no-browser"
echo "   4. Check it from your laptop: http://VM_EXTERNAL_IP:8050"
echo ""
