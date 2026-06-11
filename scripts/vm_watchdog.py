"""
VM Watchdog — Automated error monitor and self-healing loop.

Runs ON the VM alongside the dashboard. Monitors for errors, uses Claude Code
to analyze/fix bugs, commits/pushes, and restarts the dashboard. Periodically
inspects dashboard health via the /api/status HTTP endpoint.

Flow:
    Monitor (process/endpoint/logs) → Detect error →
    Inspect dashboard (HTTP API) → Claude fix → Commit/push →
    Restart dashboard → Verify (HTTP API) → Loop

Usage (run in a separate tmux session on the VM):
    tmux new-session -d -s watchdog 'cd ~/money_printer && source venv/bin/activate && PYTHONPATH=. python3 scripts/vm_watchdog.py'

    python3 scripts/vm_watchdog.py                   # full autonomous mode
    python3 scripts/vm_watchdog.py --dry-run          # detect only, no fixes
    python3 scripts/vm_watchdog.py --no-claude         # restart-only on error
    python3 scripts/vm_watchdog.py --check-interval 120
"""

import argparse
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via CLI args)
# ---------------------------------------------------------------------------
REPO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TMUX_SESSION = "money"


def _resolve_sim_balance() -> str:
    """2026-06-10 fix (b): never hardcode the sim balance on auto-restart — that
    would silently override a future Phase-2 $500 run. Reuse the balance the
    dashboard launched with, written by run_dashboard.py to logs/.sim_balance.
    Precedence: $MP_SIM_BALANCE env var > marker file > safe default 3000."""
    bal = os.environ.get("MP_SIM_BALANCE", "").strip()
    if not bal:
        marker = os.path.join(REPO_PATH, "logs", ".sim_balance")
        try:
            with open(marker, encoding="utf-8") as f:
                bal = f.readline().strip()
        except OSError:
            bal = ""
    try:
        if float(bal) > 0:
            return bal
    except (TypeError, ValueError):
        pass
    return "3000"


DASHBOARD_CMD = (
    f"cd {REPO_PATH} && source {REPO_PATH}/venv/bin/activate && "
    "PYTHONPATH=. python3 scripts/run_web_dashboard.py "
    f"--auto-cycle --sim-balance {_resolve_sim_balance()} "
    "--host 0.0.0.0 --port 8050 --no-browser"
)

CHECK_INTERVAL_S = 60
LOG_TAIL_LINES = 200
HEALTH_CHECK_PORT = 8050
HEALTH_CHECK_TIMEOUT_S = 10

MAX_FIX_ATTEMPTS_PER_ERROR = 3
MAX_TOTAL_FIXES_PER_SESSION = 10
COOLDOWN_AFTER_FIX_S = 120
COOLDOWN_AFTER_FAILURE_S = 300

GIT_BRANCH = "refactor_v0.1"

CLAUDE_TIMEOUT_S = 300

# Inspect dashboard via HTTP /api/status every N monitoring cycles
HTTP_INSPECT_INTERVAL = 15

# Grace period after watchdog/dashboard start — skip deep inspections while
# the dashboard does its startup retrain (build_features takes ~10-12 min
# with 14K+ samples).  The watchdog killed a healthy dashboard during
# retrain on 2026-03-30 because it saw "No bots active" during this phase.
STARTUP_GRACE_PERIOD_S = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_watchdog_logger() -> logging.Logger:
    log_dir = os.path.join(REPO_PATH, "logs")
    os.makedirs(log_dir, exist_ok=True)

    wdlog = logging.getLogger("vm_watchdog")
    wdlog.setLevel(logging.DEBUG)
    if wdlog.handlers:
        return wdlog

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = logging.FileHandler(
        os.path.join(log_dir, f"watchdog_{ts}.log"), encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | [Watchdog] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    wdlog.addHandler(fh)
    wdlog.addHandler(ch)
    return wdlog


log = setup_watchdog_logger()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class DetectedError:
    category: str  # "crash", "code_bug", "transient", "hung", "ui_error"
    summary: str
    traceback: str
    log_context: str
    fingerprint: str
    timestamp: str
    actionable: bool  # whether Claude should try to fix it


class ErrorTracker:
    """Dedup and retry tracking to prevent infinite fix loops."""

    def __init__(self, max_per_error: int, max_total: int):
        self.max_per_error = max_per_error
        self.max_total = max_total
        self.attempts: dict[str, int] = {}
        self.total_fixes: int = 0
        self.history: list[dict] = []

    def can_attempt(self, fingerprint: str) -> bool:
        if self.total_fixes >= self.max_total:
            return False
        return self.attempts.get(fingerprint, 0) < self.max_per_error

    def record_attempt(self, fingerprint: str, success: bool, summary: str):
        self.attempts[fingerprint] = self.attempts.get(fingerprint, 0) + 1
        self.total_fixes += 1
        self.history.append(
            {
                "fingerprint": fingerprint,
                "attempt": self.attempts[fingerprint],
                "success": success,
                "summary": summary,
                "timestamp": datetime.now().isoformat(),
            }
        )

    def get_attempt_count(self, fingerprint: str) -> int:
        return self.attempts.get(fingerprint, 0)


# ---------------------------------------------------------------------------
# Error classification patterns
# ---------------------------------------------------------------------------
TRANSIENT_PATTERNS = [
    r"502 Server Error",
    r"503 Service Unavailable",
    r"Connection Error",
    r"ConnectionResetError",
    r"ConnectionRefusedError",
    r"TimeoutError",
    r"WebSocket connection timed out",
    r"\[Risk\] \[REJECT\]",
    r"\[Risk\] \[WAIT\]",
    r"\[Risk\] \[KILL\]",
    r"Rate Limit",
    r"Temporary failure in name resolution",
]

CODE_BUG_PATTERNS = [
    r"Traceback \(most recent call last\)",
    r"IndexError:",
    r"KeyError:",
    r"AttributeError:",
    r"TypeError:",
    r"NameError:",
    r"ImportError:",
    r"ModuleNotFoundError:",
    r"SyntaxError:",
    r"ZeroDivisionError:",
    r"FileNotFoundError:",
    r"ValueError:.*unexpected",
    r"UnboundLocalError:",
    r"RecursionError:",
    r"StopIteration:",
    r"RuntimeError:",
    r"AssertionError:",
]


# ---------------------------------------------------------------------------
# Local command execution
# ---------------------------------------------------------------------------
def run_cmd(
    command: str, timeout: int = 30, cwd: Optional[str] = None
) -> tuple[int, str, str]:
    """Run a shell command locally on the VM."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or REPO_PATH,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        log.warning(f"Command timed out ({timeout}s): {command[:80]}")
        return -1, "", "timeout"
    except Exception as e:
        log.error(f"Command error: {e}")
        return -1, "", str(e)


# ---------------------------------------------------------------------------
# Health detection
# ---------------------------------------------------------------------------
def check_process_alive() -> bool:
    rc, stdout, _ = run_cmd("pgrep -f 'run_web_dashboard.py' | head -1", timeout=5)
    return rc == 0 and stdout.strip() != ""


def check_health_endpoint() -> bool:
    """Probe /api/status locally."""
    rc, stdout, _ = run_cmd(
        f"curl -s -o /dev/null -w '%{{http_code}}' "
        f"--max-time {HEALTH_CHECK_TIMEOUT_S} "
        f"http://localhost:{HEALTH_CHECK_PORT}/api/status",
        timeout=HEALTH_CHECK_TIMEOUT_S + 5,
    )
    return rc == 0 and stdout.strip() == "200"


def get_latest_log_tail(lines: int = LOG_TAIL_LINES) -> str:
    """Read the tail of the most recent log file directly."""
    log_dir = os.path.join(REPO_PATH, "logs")
    log_files = sorted(
        glob.glob(os.path.join(log_dir, "money_printer_*.log")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not log_files:
        return ""
    try:
        with open(log_files[0], encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except OSError:
        return ""


def capture_crash_output() -> str:
    """Capture tmux pane scrollback for crash tracebacks."""
    rc, stdout, _ = run_cmd(
        f"tmux capture-pane -t {TMUX_SESSION} -p -S -100 2>/dev/null",
        timeout=5,
    )
    return stdout if rc == 0 else ""


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------
def _compute_fingerprint(error_text: str) -> str:
    """Hash error text with timestamps/values stripped for stable dedup."""
    stripped = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "", error_text)
    stripped = re.sub(r"0x[0-9a-f]+", "0xADDR", stripped)
    stripped = re.sub(r"\$[\d.]+", "$N", stripped)
    stripped = re.sub(r"\d{3,}", "N", stripped)
    return hashlib.sha256(stripped.encode()).hexdigest()[:16]


def classify_error(log_tail: str) -> Optional[DetectedError]:
    """Parse log output, return a DetectedError if actionable, else None."""
    if not log_tail.strip():
        return None

    lines = log_tail.strip().split("\n")

    # Find ERROR-level lines
    error_lines = [(i, line) for i, line in enumerate(lines) if "| ERROR   |" in line]

    if not error_lines:
        return None

    # Most recent error
    error_idx, error_line = error_lines[-1]

    # Check if transient
    for pattern in TRANSIENT_PATTERNS:
        if re.search(pattern, error_line):
            return DetectedError(
                category="transient",
                summary=error_line.split("|")[-1].strip()[:200],
                traceback="",
                log_context=error_line,
                fingerprint=_compute_fingerprint(error_line),
                timestamp=datetime.now().isoformat(),
                actionable=False,
            )

    # Extract traceback: lines after ERROR that don't match log format
    traceback_lines = []
    for line in lines[error_idx + 1 :]:
        if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \|", line):
            break
        traceback_lines.append(line)

    # Context window: 20 lines before + error + traceback
    context_start = max(0, error_idx - 20)
    context_end = error_idx + len(traceback_lines) + 1
    context = "\n".join(lines[context_start:context_end])

    full_error = error_line + "\n" + "\n".join(traceback_lines)

    is_code_bug = any(re.search(p, full_error) for p in CODE_BUG_PATTERNS)

    return DetectedError(
        category="code_bug" if is_code_bug else "runtime_error",
        summary=error_line.split("|")[-1].strip()[:200],
        traceback="\n".join(traceback_lines),
        log_context=context,
        fingerprint=_compute_fingerprint(full_error),
        timestamp=datetime.now().isoformat(),
        actionable=is_code_bug,
    )


# ---------------------------------------------------------------------------
# Dashboard inspection via HTTP API (replaces Playwright MCP)
# ---------------------------------------------------------------------------
def _fetch_api_status(timeout: int = HEALTH_CHECK_TIMEOUT_S) -> Optional[dict]:
    """Fetch /api/status JSON from the local dashboard."""
    url = f"http://localhost:{HEALTH_CHECK_PORT}/api/status"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.debug("API status fetch failed: %s", e)
        return None


def inspect_dashboard() -> dict:
    """Inspect dashboard health via /api/status HTTP endpoint.

    Checks portfolio, active bots, error alerts, and position health.
    Returns {"healthy": bool, "errors": [...], "summary": "..."}.
    """
    log.info("Inspecting dashboard via HTTP API...")
    status = _fetch_api_status()

    if status is None:
        log.warning("Dashboard /api/status unreachable")
        return {
            "healthy": None,
            "errors": ["api_unreachable"],
            "summary": "inspection_failed",
        }

    errors = []

    # Check bots are active
    bots = status.get("bots", [])
    active_bots = [b for b in bots if b.get("active")]
    if not active_bots:
        errors.append("no_active_bots")

    # Check portfolio exists and has reasonable values
    portfolio = status.get("portfolio", {})
    equity = portfolio.get("equity", 0)
    if equity <= 0:
        errors.append(f"zero_equity: {equity}")

    # Check for error alerts (skip startup retrain alerts)
    alerts = status.get("alerts", [])
    error_alerts = [
        a for a in alerts if "ERROR" in a.upper() and "RETRAIN" not in a.upper()
    ]
    if error_alerts:
        errors.append(f"error_alerts: {len(error_alerts)}")

    # Check positions for anomalies (e.g., massive unrealized losses)
    unrealized = portfolio.get("unrealized_pnl", 0)
    if equity > 0 and unrealized < -(equity * 0.5):
        errors.append(f"extreme_unrealized_loss: {unrealized:.2f}")

    healthy = len(errors) == 0

    summary_parts = [
        f"equity=${equity:.2f}",
        f"bots={len(active_bots)}/{len(bots)}",
        f"realized_pnl=${portfolio.get('realized_pnl', 0):.2f}",
        f"unrealized=${unrealized:.2f}",
        f"exposure={portfolio.get('exposure_pct', 0):.1f}%",
    ]
    summary = " | ".join(summary_parts)

    if healthy:
        log.info("Dashboard healthy: %s", summary)
    else:
        log.warning("Dashboard issues: %s | errors=%s", summary, errors)

    return {"healthy": healthy, "errors": errors, "summary": summary}


def verify_dashboard_health() -> bool:
    """Verify dashboard is healthy after a fix via HTTP API."""
    log.info("Verifying dashboard health via HTTP API...")
    status = _fetch_api_status()
    if status is None:
        log.warning("Dashboard unreachable during verification")
        return False

    bots = status.get("bots", [])
    active_bots = [b for b in bots if b.get("active")]
    portfolio = status.get("portfolio", {})
    equity = portfolio.get("equity", 0)

    healthy = len(active_bots) > 0 and equity > 0
    if healthy:
        log.info(
            "Verification passed: %d active bots, equity=$%.2f",
            len(active_bots),
            equity,
        )
    else:
        log.warning(
            "Verification failed: %d active bots, equity=$%.2f",
            len(active_bots),
            equity,
        )
    return healthy


# ---------------------------------------------------------------------------
# Claude Code integration (CLI with API fallback)
# ---------------------------------------------------------------------------
_claude_cli_available: Optional[bool] = None  # cached after first check


def _check_claude_cli() -> bool:
    """Check if Claude Code CLI is installed and authenticated."""
    global _claude_cli_available
    if _claude_cli_available is not None:
        return _claude_cli_available

    try:
        result = subprocess.run(
            ["claude", "-p", "Reply with just: OK", "--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_PATH,
        )
        _claude_cli_available = (
            result.returncode == 0 and "login" not in result.stdout.lower()
        )
        if not _claude_cli_available:
            log.warning(
                "Claude CLI not authenticated. Bug fixing will use API fallback "
                "(diagnostic-only, no auto-fix). Run 'claude /login' on the VM "
                "to enable full auto-fix mode."
            )
        else:
            log.info("Claude CLI authenticated — full auto-fix mode enabled")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _claude_cli_available = False
        log.warning("Claude CLI not available. Bug fixing disabled.")

    return _claude_cli_available


def _invoke_claude_api(prompt: str) -> tuple[bool, str]:
    """Fallback: call Anthropic Messages API directly via urllib (no SDK needed).

    Returns diagnostic analysis only — cannot edit files.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Try loading from .env
        env_path = os.path.join(REPO_PATH, ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip("\"'")
                        break

    if not api_key:
        return False, "ANTHROPIC_API_KEY not found in environment or .env"

    body = json.dumps(
        {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=CLAUDE_TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode())
            text = result.get("content", [{}])[0].get("text", "")
            return True, text
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        log.error("Anthropic API error %d: %s", e.code, error_body[:200])
        return False, f"API error {e.code}: {error_body[:200]}"
    except Exception as e:
        log.error("Anthropic API call failed: %s", e)
        return False, str(e)


def invoke_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT_S) -> tuple[bool, str]:
    """Invoke Claude — tries CLI first (can edit files), falls back to API (diagnostic only)."""
    if _check_claude_cli():
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--dangerously-skip-permissions"],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=REPO_PATH,
            )
            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr
            return result.returncode == 0, output
        except subprocess.TimeoutExpired:
            log.warning("Claude CLI timed out (%ds)", timeout)
            return False, "timeout"
        except Exception as e:
            log.error("Claude CLI error: %s", e)
            # Fall through to API

    # API fallback — diagnostic analysis only
    log.info("Using Anthropic API fallback (diagnostic-only, cannot edit files)")
    return _invoke_claude_api(prompt)


def build_fix_prompt(error: DetectedError, dashboard_state: dict) -> str:
    """Construct the prompt for Claude Code to fix a bug."""
    dashboard_info = ""

    if dashboard_state and dashboard_state.get("summary"):
        dashboard_info = f"""
## Dashboard State (from /api/status)
{json.dumps(dashboard_state, indent=2)}
"""

    return f"""\
You are fixing a bug in the Money Printer algorithmic trading bot.
The bot runs on a GCE VM and has encountered a runtime error.

## Error Summary
{error.summary}

## Category
{error.category}

## Full Traceback
```
{error.traceback}
```

## Log Context (surrounding lines)
```
{error.log_context}
```
{dashboard_info}
## Instructions
1. Read the error carefully and identify the root cause in the source code
2. Find and read the relevant source file(s)
3. Make the MINIMAL fix needed to resolve this error
4. Do NOT refactor unrelated code or add unnecessary error handling
5. Do NOT suppress the error — fix the underlying bug
6. Run `python3 -m pytest tests/ --ignore=tests/test_output_cooldown.txt --ignore=tests/fixtures/ -x -q` to verify your fix
7. If you cannot determine the root cause, do NOT make speculative changes

## Repository Layout
- src/bots/ — Bot implementations (btc_15m.py, btc_hourly.py, weather.py)
- src/core/ — Risk manager, matching engine, circuit breaker, interfaces
- src/data/ — Data providers (kalshi_provider.py, coinbase_provider.py, nws_provider.py)
- src/strategies/ — Trading strategies (crypto, weather, bracket)
- src/ml/ — Machine learning (trade journal, online updater)
- src/web/ — Web dashboard (server.py, state_manager.py)
- scripts/ — Entry points (run_web_dashboard.py, run_dashboard.py)
"""


# ---------------------------------------------------------------------------
# Git operations (all local on the VM)
# ---------------------------------------------------------------------------
def git_has_changes() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=REPO_PATH,
    )
    return bool(result.stdout.strip())


def git_commit_and_push(error_summary: str, branch: str) -> bool:
    """Stage modified tracked files, commit, and push."""
    # Stage tracked modifications
    subprocess.run(["git", "add", "-u"], cwd=REPO_PATH, check=True)
    # Also pick up new files Claude may have created in src/ or tests/
    for pattern in ["src/", "scripts/", "tests/"]:
        subprocess.run(["git", "add", pattern], cwd=REPO_PATH, capture_output=True)

    # Truncate summary for commit message
    short_summary = re.sub(r"[\[\]]", "", error_summary)[:68]
    commit_msg = f"fix(watchdog): {short_summary}"

    try:
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=REPO_PATH,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Git commit failed: {e.stderr}")
        return False

    result = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error(f"Git push failed: {result.stderr}")
        return False

    log.info("Fix committed and pushed to %s", branch)
    return True


# ---------------------------------------------------------------------------
# Dashboard restart (local tmux management)
# ---------------------------------------------------------------------------
def stop_dashboard() -> None:
    """Stop the dashboard process in the tmux session."""
    log.info("Stopping dashboard...")
    run_cmd(f"tmux send-keys -t {TMUX_SESSION} C-c C-c", timeout=5)
    time.sleep(5)

    # Force kill if still running
    if check_process_alive():
        run_cmd("pkill -f 'run_web_dashboard.py'", timeout=5)
        time.sleep(3)

    # Kill the whole session to start fresh
    if check_process_alive():
        run_cmd(f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null", timeout=5)
        time.sleep(2)


def start_dashboard() -> bool:
    """Start the dashboard in a tmux session and verify it's healthy."""
    # Ensure tmux session exists
    run_cmd(
        f"tmux new-session -d -s {TMUX_SESSION} 2>/dev/null || true",
        timeout=5,
    )

    # Send launch command
    log.info("Starting dashboard in tmux session '%s'...", TMUX_SESSION)
    rc, _, stderr = run_cmd(
        f"tmux send-keys -t {TMUX_SESSION} '{DASHBOARD_CMD}' Enter",
        timeout=10,
    )
    if rc != 0:
        log.error(f"Failed to send start command: {stderr}")
        return False

    # Wait for startup, then health check
    log.info("Waiting 30s for dashboard startup...")
    time.sleep(30)

    if check_health_endpoint():
        log.info("Dashboard started and healthy")
        return True

    # Retry after additional wait
    log.info("First health check failed, retrying in 15s...")
    time.sleep(15)
    alive = check_health_endpoint()
    if alive:
        log.info("Dashboard healthy on second attempt")
    else:
        log.warning("Dashboard may not be fully healthy yet")
    return alive


def restart_dashboard(branch: str) -> bool:
    """Stop dashboard, git pull latest, restart."""
    stop_dashboard()

    # Git pull latest (in case fix was committed)
    log.info("Pulling latest code...")
    rc, stdout, stderr = run_cmd(f"git pull origin {branch}", timeout=30, cwd=REPO_PATH)
    if rc != 0:
        log.error(f"Git pull failed: {stderr}")
        # Continue anyway — the fix might already be local
    else:
        log.info("Git pull: %s", stdout.strip().split("\\n")[-1])

    return start_dashboard()


# ---------------------------------------------------------------------------
# Post-fix verification
# ---------------------------------------------------------------------------
def verify_fix(
    fingerprint: str,
    wait_time: int = 120,
) -> bool:
    """Wait, then check that the same error doesn't reappear."""
    log.info(f"Verifying fix for {wait_time}s...")
    time.sleep(wait_time)

    # Check logs for recurrence
    log_tail = get_latest_log_tail(lines=100)
    new_error = classify_error(log_tail)

    if new_error and new_error.fingerprint == fingerprint:
        log.warning("Same error recurred — fix did NOT work")
        return False

    # HTTP health verification
    if not verify_dashboard_health():
        log.warning("HTTP health check reports dashboard unhealthy")
        return False

    log.info("Fix verified — no recurrence detected")
    return True


# ---------------------------------------------------------------------------
# Main monitoring loop
# ---------------------------------------------------------------------------
def main_loop(args):
    tracker = ErrorTracker(
        max_per_error=args.max_retries,
        max_total=args.max_fixes,
    )
    branch = args.branch
    cycle_count = 0
    start_time = time.time()

    log.info(
        "Watchdog started. repo=%s, interval=%ds, dry_run=%s, no_claude=%s",
        REPO_PATH,
        args.check_interval,
        args.dry_run,
        args.no_claude,
    )

    # Pre-check Claude CLI auth at startup so we warn once, not every cycle
    if not args.no_claude and not args.dry_run:
        _check_claude_cli()

    while True:
        try:
            cycle_count += 1

            # === PHASE 1: HEALTH CHECK ===
            process_alive = check_process_alive()
            endpoint_ok = check_health_endpoint() if process_alive else False

            log.debug(
                "Cycle %d: process=%s, endpoint=%s",
                cycle_count,
                process_alive,
                endpoint_ok,
            )

            error: Optional[DetectedError] = None

            if process_alive and endpoint_ok:
                # === PHASE 2: LOG SCAN ===
                log_tail = get_latest_log_tail()
                error = classify_error(log_tail)

                if error and not error.actionable:
                    log.debug(
                        "Transient error detected, ignoring: %s", error.summary[:100]
                    )
                    error = None

                # Periodic HTTP inspection (skip during startup grace period)
                in_grace_period = (time.time() - start_time) < STARTUP_GRACE_PERIOD_S
                if (
                    error is None
                    and not in_grace_period
                    and cycle_count % HTTP_INSPECT_INTERVAL == 0
                ):
                    state = inspect_dashboard()
                    if state.get("healthy") is False:
                        errors = state.get("errors", [])
                        error = DetectedError(
                            category="ui_error",
                            summary=f"HTTP inspection detected issues: {errors}",
                            traceback="",
                            log_context=str(state),
                            fingerprint=_compute_fingerprint(str(errors)),
                            timestamp=datetime.now().isoformat(),
                            actionable=True,
                        )
                        log.warning("HTTP inspection detected issues: %s", errors)

                if error is None:
                    time.sleep(args.check_interval)
                    continue

            elif not process_alive:
                # Process crashed
                crash_output = capture_crash_output()
                log_tail = get_latest_log_tail()
                combined = crash_output + "\n" + log_tail

                error = DetectedError(
                    category="crash",
                    summary="Dashboard process died",
                    traceback=crash_output,
                    log_context=log_tail[-2000:] if log_tail else "",
                    fingerprint=_compute_fingerprint(combined),
                    timestamp=datetime.now().isoformat(),
                    actionable=True,
                )
                log.warning("CRASH detected — dashboard process is dead")

            else:
                # Alive but endpoint unresponsive (hung)
                error = DetectedError(
                    category="hung",
                    summary="Dashboard alive but /api/status unresponsive",
                    traceback="",
                    log_context=get_latest_log_tail()[-1000:],
                    fingerprint="hung_process",
                    timestamp=datetime.now().isoformat(),
                    actionable=False,
                )
                log.warning("HUNG detected — process alive but endpoint down")

            # === PHASE 3: REPORT (dry run stops here) ===
            log.warning(
                "Error [%s]: %s (fingerprint=%s, actionable=%s)",
                error.category,
                error.summary[:100],
                error.fingerprint,
                error.actionable,
            )

            if args.dry_run:
                log.info("[DRY RUN] Would attempt fix. Sleeping...")
                time.sleep(args.check_interval)
                continue

            # === PHASE 4: NON-ACTIONABLE → JUST RESTART ===
            if not error.actionable:
                log.info("Non-actionable error — restarting dashboard...")
                restart_dashboard(branch)
                start_time = time.time()  # reset grace period for startup retrain
                time.sleep(args.cooldown)
                continue

            # === PHASE 5: CHECK RETRY BUDGET ===
            if not tracker.can_attempt(error.fingerprint):
                log.error(
                    "Max fix attempts reached for %s. Manual intervention required.",
                    error.fingerprint,
                )
                # Still restart the dashboard if it's down
                if not process_alive:
                    restart_dashboard(branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            attempt = tracker.get_attempt_count(error.fingerprint) + 1

            # === PHASE 6: STOP DASHBOARD ===
            log.info("Stopping dashboard for fix (attempt %d)...", attempt)
            stop_dashboard()

            if args.no_claude:
                log.info("[NO-CLAUDE] Restarting without fix...")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                restart_dashboard(branch)
                time.sleep(args.cooldown)
                continue

            # === PHASE 7: INSPECT DASHBOARD + INVOKE CLAUDE ===
            dashboard_state = inspect_dashboard()

            log.info(
                "Invoking Claude Code (attempt %d/%d)...", attempt, args.max_retries
            )
            prompt = build_fix_prompt(error, dashboard_state)
            claude_ok, claude_output = invoke_claude(prompt)

            if not claude_ok:
                log.warning("Claude invocation failed: %s", claude_output[:200])
                tracker.record_attempt(error.fingerprint, False, error.summary)
                restart_dashboard(branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            if not git_has_changes():
                log.warning("Claude did not produce any code changes")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                restart_dashboard(branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            # === PHASE 8: COMMIT AND PUSH ===
            log.info("Committing and pushing fix...")
            if not git_commit_and_push(error.summary, branch):
                log.error("Git commit/push failed")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                restart_dashboard(branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            # === PHASE 9: RESTART DASHBOARD ===
            if not start_dashboard():
                log.error("Dashboard restart failed after fix")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue
            start_time = time.time()  # reset grace period for startup retrain

            # === PHASE 10: VERIFY FIX ===
            fix_worked = verify_fix(
                error.fingerprint,
                wait_time=args.cooldown,
            )
            tracker.record_attempt(error.fingerprint, fix_worked, error.summary)

            if fix_worked:
                log.info("Fix VERIFIED for: %s", error.summary[:100])
            else:
                log.warning(
                    "Fix FAILED for: %s (will retry if budget remains)",
                    error.summary[:100],
                )

            time.sleep(args.cooldown)

        except KeyboardInterrupt:
            log.info("Watchdog stopped by user (Ctrl+C)")
            break
        except Exception as e:
            log.exception(f"Watchdog loop error: {e}")
            time.sleep(args.check_interval)

    # Write session summary
    if tracker.history:
        log.info("=== Session Summary ===")
        for entry in tracker.history:
            status = "OK" if entry["success"] else "FAIL"
            log.info(
                "  [%s] %s (attempt %d, %s)",
                status,
                entry["summary"][:80],
                entry["attempt"],
                entry["timestamp"],
            )
    log.info("Watchdog exited. Total fix attempts: %d", tracker.total_fixes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VM Watchdog: monitors Money Printer dashboard and auto-fixes errors"
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=CHECK_INTERVAL_S,
        help=f"Seconds between health checks (default: {CHECK_INTERVAL_S})",
    )
    parser.add_argument(
        "--max-fixes",
        type=int,
        default=MAX_TOTAL_FIXES_PER_SESSION,
        help=f"Max total fix attempts per session (default: {MAX_TOTAL_FIXES_PER_SESSION})",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_FIX_ATTEMPTS_PER_ERROR,
        help=f"Max fix attempts per unique error (default: {MAX_FIX_ATTEMPTS_PER_ERROR})",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=COOLDOWN_AFTER_FIX_S,
        help=f"Seconds to wait after a fix (default: {COOLDOWN_AFTER_FIX_S})",
    )
    parser.add_argument(
        "--branch",
        default=GIT_BRANCH,
        help=f"Git branch (default: {GIT_BRANCH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect errors but don't fix or restart",
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Skip Claude — just restart on error",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main_loop(args)
