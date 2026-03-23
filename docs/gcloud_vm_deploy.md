# Money Printer — Google Cloud VM Deployment Guide

Deploy the auto-cycle training loop + web dashboard on a Compute Engine VM.

---

## Step 1: Create the VM

In the Google Cloud Console, go to **Compute Engine > VM instances > Create Instance**.

| Setting | Value |
|---------|-------|
| **Name** | `money-printer` (or whatever you like) |
| **Region** | `us-east1` or `us-central1` (low latency to Kalshi/Coinbase APIs) |
| **Machine type** | `e2-standard-2` (2 vCPU, 8 GB RAM) |
| **Boot disk** | Click **Change** → Ubuntu 24.04 LTS (x86/64), 30 GB standard persistent disk |
| **Firewall** | Default (SSH only — dashboard access uses SSH tunnel, no extra ports needed) |

> **Budget option:** Under **Availability policies**, set **VM provisioning model** to
> **Spot**. Same hardware at ~60-70% discount. Risk: Google can reclaim it (rare for e2).
> If you do this, also check **Automatic restart: On** so it reboots if preempted.

Click **Create**. Wait ~30 seconds for the VM to spin up.

---

## Step 2: Dashboard Access via SSH Tunnel (Secure)

**No firewall rule needed.** Instead of exposing port 8050 to the internet, we
access the dashboard through an encrypted SSH tunnel. This is more secure (no
open ports, no IP allowlisting that breaks when your ISP rotates your IP) and
authentication is handled by gcloud IAM.

From your laptop, run:

```bash
# Windows (PowerShell or CMD)
gcloud compute ssh VM_NAME --zone=YOUR_ZONE --ssh-flag="-L 8050:localhost:8050" --ssh-flag="-N"

# macOS / Linux
gcloud compute ssh VM_NAME --zone=YOUR_ZONE -- -L 8050:localhost:8050 -N
```

Then open **http://localhost:8050** in your browser.

The `-N` flag keeps the tunnel open without starting a remote shell. Press
`Ctrl+C` to close the tunnel when you're done.

---

## Step 3: SSH into the VM

In the VM instances list, click the **SSH** button next to your VM. This opens a browser-based terminal. Alternatively, from your local machine:

```bash
gcloud compute ssh money-printer --zone=YOUR_ZONE
```

---

## Step 4: System Setup

Run these commands on the VM:

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Install Python 3.14 + essentials
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.14 python3.14-venv python3.14-dev

# Verify
python3.14 --version

# Install system dependencies for compiled packages (numpy, scipy, etc.)
sudo apt install -y build-essential libffi-dev libssl-dev git
```

> **If `deadsnakes` doesn't have 3.14 yet**, Python 3.12 (Ubuntu's default) works
> fine for all dependencies. Just substitute `python3` for `python3.14` everywhere
> below.

---

## Step 5: Clone the Repo & Set Up Virtual Environment

```bash
# Clone
cd ~
git clone https://github.com/JusHoya/money_printer.git
cd money_printer

# Create virtual environment
python3.14 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

The `keyboard` package will be skipped automatically (it's Windows-only in requirements.txt).
PyTorch will install the CPU-only build, which is exactly what you want.

This step takes 5-10 minutes (PyTorch is ~800MB to download).

---

## Step 6: Configure Secrets

You need two things that aren't in the repo: your `.env` file and your Kalshi private key.

### Option A: Copy from your laptop (recommended)

From **your local machine** (not the VM), open a terminal:

```bash
# Upload .env
gcloud compute scp .env money-printer:~/money_printer/.env --zone=YOUR_ZONE

# Upload your Kalshi private key
gcloud compute scp Kalshi_priv_key_1_readOnly.key money-printer:~/money_printer/Kalshi_priv_key_1_readOnly.key --zone=YOUR_ZONE
```

### Option B: Create them manually on the VM

```bash
# On the VM, inside ~/money_printer/
nano .env
```

Paste in your credentials:

```
KALSHI_API_URL=https://api.elections.kalshi.com/trade-api/v2
KALSHI_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=Kalshi_priv_key_1_readOnly.key

NWS_USER_AGENT=(MoneyPrinter_Bot, your_email@example.com)
NWS_STATION_ID=KJFK
```

Then create the key file:

```bash
nano Kalshi_priv_key_1_readOnly.key
# Paste your RSA private key content, save
chmod 600 Kalshi_priv_key_1_readOnly.key
```

---

## Step 7: Transfer Training Data (Optional)

Your Parquet files in `data/historical/` are gitignored. If you want the VM to
start with your existing training data instead of harvesting from scratch:

```bash
# From your local machine — upload the whole data/historical directory
gcloud compute scp --recurse data/historical money-printer:~/money_printer/data/historical --zone=YOUR_ZONE

# Also upload trained models if you want to skip initial training
gcloud compute scp --recurse data/models money-printer:~/money_printer/data/models --zone=YOUR_ZONE
```

If you skip this, the auto-cycle loop will harvest fresh data over time. It just
means the first few cycles won't have much training data.

---

## Step 8: Test Run

```bash
# On the VM
cd ~/money_printer
source venv/bin/activate

# Quick smoke test — make sure imports work
PYTHONPATH=. python -c "from scripts.run_web_dashboard import main; print('OK')"

# Run the dashboard (foreground first to verify it works)
python scripts/run_web_dashboard.py \
    --auto-cycle \
    --sim-balance 3000 \
    --host 0.0.0.0 \
    --port 8050 \
    --no-browser
```

You should see:
```
  Money Printer Web Dashboard -> http://0.0.0.0:8050
  Press Ctrl+C to stop.
```

In a **separate terminal** on your laptop, open the SSH tunnel (see Step 2),
then open **http://localhost:8050** in your browser.

If it works, Ctrl+C the dashboard — next step makes it persistent.

---

## Step 9: Keep It Running with tmux

`tmux` lets the process survive after you disconnect SSH.

```bash
# Install tmux (usually pre-installed on Ubuntu)
sudo apt install -y tmux

# Start a named session
tmux new -s money

# Inside tmux, activate venv and run
cd ~/money_printer
source venv/bin/activate
python scripts/run_web_dashboard.py \
    --auto-cycle \
    --sim-balance 3000 \
    --host 0.0.0.0 \
    --port 8050 \
    --no-browser

# Detach from tmux: press Ctrl+B, then D
# The process keeps running in the background.

# To reattach later:
tmux attach -t money
```

---

## Step 10 (Optional): Auto-Start on Boot with systemd

If you're using a Spot VM (or want crash recovery), set up a systemd service so
the dashboard starts automatically on boot.

```bash
sudo tee /etc/systemd/system/money-printer.service > /dev/null << 'EOF'
[Unit]
Description=Money Printer Trading Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/money_printer
Environment=PYTHONPATH=/home/YOUR_USERNAME/money_printer
ExecStart=/home/YOUR_USERNAME/money_printer/venv/bin/python scripts/run_web_dashboard.py --auto-cycle --sim-balance 3000 --host 0.0.0.0 --port 8050 --no-browser
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Replace `YOUR_USERNAME` with your actual username (run `whoami` to check).

```bash
sudo systemctl daemon-reload
sudo systemctl enable money-printer
sudo systemctl start money-printer

# Check status
sudo systemctl status money-printer

# View live logs
sudo journalctl -u money-printer -f
```

---

## Accessing the Dashboard

Open an SSH tunnel from your laptop (keep this terminal open):

```bash
# Windows
gcloud compute ssh VM_NAME --zone=YOUR_ZONE --ssh-flag="-L 8050:localhost:8050" --ssh-flag="-N"

# macOS / Linux
gcloud compute ssh VM_NAME --zone=YOUR_ZONE -- -L 8050:localhost:8050 -N
```

Then open **http://localhost:8050** in your browser. The dashboard streams live
data over WebSocket — PnL, positions, strategy signals, cycle history, all in
real time. Close the tunnel with `Ctrl+C` when done.

---

## Day-to-Day Operations

### View the dashboard

```bash
# Open SSH tunnel (keep this terminal open)
# Windows:
gcloud compute ssh VM_NAME --zone=YOUR_ZONE --ssh-flag="-L 8050:localhost:8050" --ssh-flag="-N"
# macOS / Linux:
gcloud compute ssh VM_NAME --zone=YOUR_ZONE -- -L 8050:localhost:8050 -N
```

Then open **http://localhost:8050**. `Ctrl+C` in this terminal **only closes
the tunnel** — the simulation on the VM keeps running.

### Stop the simulation

```bash
# Option 1: Reattach to tmux and Ctrl+C the process
gcloud compute ssh VM_NAME --zone=YOUR_ZONE
tmux attach -t money
# Press Ctrl+C to stop the dashboard, then Ctrl+D or 'exit' to close tmux

# Option 2: Send Ctrl+C remotely (without reattaching)
gcloud compute ssh VM_NAME --zone=YOUR_ZONE --command="tmux send-keys -t money C-c"
```

### Inspect logs & retrain

```bash
# SSH into the VM
gcloud compute ssh VM_NAME --zone=YOUR_ZONE

# Reattach to tmux (if using tmux)
tmux attach -t money

# Check logs
cat ~/money_printer/logs/money_printer_*.log

# View cycle history
ls ~/money_printer/logs/_archive/

# Manually retrain
cd ~/money_printer && source venv/bin/activate
PYTHONPATH=. python scripts/train_models.py

# Pull latest code changes
cd ~/money_printer && git pull origin main

# Restart the service (if using systemd)
sudo systemctl restart money-printer
```

---

## Cost Management

| Config | Estimated Monthly Cost |
|--------|----------------------|
| `e2-standard-2` (on-demand) | ~$50-60 |
| `e2-standard-2` (spot) | ~$15-25 |
| `e2-medium` (1 vCPU, 4 GB — tight but works) | ~$25-30 on-demand, ~$8-12 spot |
| 30 GB standard disk | ~$1.20 |
| Network egress | Negligible (small API calls + dashboard WebSocket) |

To stop costs when not using it: **Stop** (not delete) the VM in the console.
The disk persists (~$1.20/month) but compute charges stop entirely.
