"""
VM Watchdog — Automated error monitor and self-healing loop.

Monitors the Money Printer web dashboard running on a GCE VM for errors,
uses Claude Code + Playwright MCP to analyze/fix bugs, then deploys fixes
automatically.

Flow:
    SSH tunnel → Monitor (process/endpoint/logs) → Detect error →
    Inspect dashboard (Playwright) → Claude fix → Commit/push →
    VM git pull + restart → Verify (Playwright) → Loop

Usage:
    python scripts/vm_watchdog.py                   # full autonomous mode
    python scripts/vm_watchdog.py --dry-run          # detect only, no fixes
    python scripts/vm_watchdog.py --no-claude         # restart-only on error
    python scripts/vm_watchdog.py --check-interval 120
"""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via CLI args)
# ---------------------------------------------------------------------------
VM_NAME = "money-printer-preschool-20260322"
VM_ZONE = "us-central1-c"
VM_REPO_PATH = "/home/hoyer/money_printer"
VM_VENV_ACTIVATE = "source /home/hoyer/money_printer/venv/bin/activate"
TMUX_SESSION = "money"

DASHBOARD_CMD = (
    f"cd {VM_REPO_PATH} && {VM_VENV_ACTIVATE} && "
    "PYTHONPATH=. python3 scripts/run_web_dashboard.py "
    "--auto-cycle --sim-balance 3000 --host 0.0.0.0 --port 8050 --no-browser"
)

CHECK_INTERVAL_S = 60
LOG_TAIL_LINES = 200
HEALTH_CHECK_PORT = 8050
HEALTH_CHECK_TIMEOUT_S = 10

MAX_FIX_ATTEMPTS_PER_ERROR = 3
MAX_TOTAL_FIXES_PER_SESSION = 10
COOLDOWN_AFTER_FIX_S = 120
COOLDOWN_AFTER_FAILURE_S = 300

LOCAL_REPO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GIT_BRANCH = "refactor_v0.1"

CLAUDE_TIMEOUT_S = 300

# Inspect dashboard via Playwright every N monitoring cycles
PLAYWRIGHT_INSPECT_INTERVAL = 10


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_watchdog_logger() -> logging.Logger:
    log_dir = os.path.join(LOCAL_REPO_PATH, "logs")
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
# SSH layer
# ---------------------------------------------------------------------------
_tunnel_proc: Optional[subprocess.Popen] = None


def start_ssh_tunnel(vm_name: str, vm_zone: str) -> subprocess.Popen:
    """Start a persistent background SSH tunnel: localhost:8050 → VM:8050."""
    global _tunnel_proc
    cmd = [
        "gcloud",
        "compute",
        "ssh",
        vm_name,
        f"--zone={vm_zone}",
        "--ssh-flag=-L 8050:localhost:8050",
        "--ssh-flag=-N",
        "--ssh-flag=-o ServerAliveInterval=30",
        "--ssh-flag=-o ServerAliveCountMax=3",
        "--ssh-flag=-o ExitOnForwardFailure=yes",
    ]
    log.info("Starting SSH tunnel: localhost:8050 → VM:8050")
    _tunnel_proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    # Give it a moment to establish
    time.sleep(5)
    if _tunnel_proc.poll() is not None:
        stderr = _tunnel_proc.stderr.read().decode() if _tunnel_proc.stderr else ""
        log.error(f"SSH tunnel failed to start: {stderr}")
        return _tunnel_proc
    log.info("SSH tunnel established (PID %d)", _tunnel_proc.pid)
    return _tunnel_proc


def check_tunnel_alive() -> bool:
    global _tunnel_proc
    if _tunnel_proc is None:
        return False
    return _tunnel_proc.poll() is None


def ensure_tunnel(vm_name: str, vm_zone: str):
    """Restart tunnel if it died."""
    if not check_tunnel_alive():
        log.warning("SSH tunnel is down. Restarting...")
        start_ssh_tunnel(vm_name, vm_zone)


def ssh_exec(
    command: str,
    vm_name: str,
    vm_zone: str,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Execute a command on the VM via gcloud SSH."""
    full_cmd = [
        "gcloud",
        "compute",
        "ssh",
        vm_name,
        f"--zone={vm_zone}",
        f"--command={command}",
    ]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        log.warning(f"SSH command timed out ({timeout}s): {command[:80]}")
        return -1, "", "timeout"
    except Exception as e:
        log.error(f"SSH exec error: {e}")
        return -1, "", str(e)


# ---------------------------------------------------------------------------
# Health detection
# ---------------------------------------------------------------------------
def check_process_alive(vm_name: str, vm_zone: str) -> bool:
    rc, stdout, _ = ssh_exec(
        "pgrep -f 'run_web_dashboard.py' | head -1",
        vm_name,
        vm_zone,
        timeout=15,
    )
    return rc == 0 and stdout.strip() != ""


def check_health_endpoint(vm_name: str, vm_zone: str) -> bool:
    """Probe /api/status from within the VM (avoids tunnel dependency)."""
    rc, stdout, _ = ssh_exec(
        f"curl -s -o /dev/null -w '%{{http_code}}' "
        f"--max-time {HEALTH_CHECK_TIMEOUT_S} "
        f"http://localhost:{HEALTH_CHECK_PORT}/api/status",
        vm_name,
        vm_zone,
        timeout=HEALTH_CHECK_TIMEOUT_S + 10,
    )
    return rc == 0 and stdout.strip() == "200"


def get_latest_log_tail(vm_name: str, vm_zone: str, lines: int = LOG_TAIL_LINES) -> str:
    rc, stdout, _ = ssh_exec(
        f"ls -t {VM_REPO_PATH}/logs/money_printer_*.log 2>/dev/null | head -1 | "
        f"xargs tail -n {lines} 2>/dev/null",
        vm_name,
        vm_zone,
        timeout=15,
    )
    return stdout if rc == 0 else ""


def capture_crash_output(vm_name: str, vm_zone: str) -> str:
    """Capture tmux pane scrollback for crash tracebacks."""
    rc, stdout, _ = ssh_exec(
        f"tmux capture-pane -t {TMUX_SESSION} -p -S -100 2>/dev/null",
        vm_name,
        vm_zone,
        timeout=10,
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
# Claude Code + Playwright MCP integration
# ---------------------------------------------------------------------------
INSPECT_PROMPT = """\
Use the Playwright MCP browser tools to inspect the Money Printer dashboard:
1. Navigate to http://localhost:8050
2. Take a snapshot of the page content
3. Report:
   - Is the dashboard rendering correctly?
   - What is the current portfolio status (PnL, equity, exposure)?
   - Are there any error alerts visible in the alerts section?
   - Are all bots shown as active?
   - Is there a "DISCONNECTED" overlay?
4. Return ONLY a JSON object (no markdown fences): {"healthy": true/false, "errors": [...], "summary": "..."}
"""

VERIFY_PROMPT = """\
Use Playwright MCP to verify the Money Printer dashboard is healthy after a fix:
1. Navigate to http://localhost:8050
2. Wait 5 seconds for data to load via WebSocket
3. Check that:
   - The page renders (not blank, no error page)
   - Portfolio section shows numeric data
   - No error alerts are visible
   - At least one bot is listed
4. Return ONLY a JSON object (no markdown fences): {"healthy": true/false, "details": "..."}
"""


def invoke_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT_S) -> tuple[bool, str]:
    """Invoke Claude Code CLI in one-shot mode."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--dangerously-skip-permissions"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=LOCAL_REPO_PATH,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        log.warning(f"Claude invocation timed out ({timeout}s)")
        return False, "timeout"
    except FileNotFoundError:
        log.error("'claude' CLI not found in PATH")
        return False, "claude not found"
    except Exception as e:
        log.error(f"Claude invocation error: {e}")
        return False, str(e)


def inspect_dashboard() -> dict:
    """Use Claude + Playwright MCP to visually inspect the dashboard.
    Returns parsed JSON result or a fallback dict.
    """
    log.info("Inspecting dashboard via Playwright MCP...")
    ok, output = invoke_claude(INSPECT_PROMPT, timeout=120)
    if not ok:
        log.warning(f"Playwright inspection failed: {output[:200]}")
        return {"healthy": None, "errors": [], "summary": "inspection_failed"}

    # Try to parse JSON from Claude's output
    try:
        # Claude may wrap in markdown fences; strip them
        cleaned = re.sub(r"```json?\s*", "", output)
        cleaned = re.sub(r"```\s*", "", cleaned)
        # Find the JSON object
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
    except (json.JSONDecodeError, AttributeError):
        pass

    log.warning("Could not parse Playwright inspection output")
    return {"healthy": None, "errors": [], "summary": output[:500]}


def verify_dashboard_health() -> bool:
    """Use Claude + Playwright MCP to verify dashboard health post-deploy."""
    log.info("Verifying dashboard health via Playwright MCP...")
    ok, output = invoke_claude(VERIFY_PROMPT, timeout=120)
    if not ok:
        log.warning(f"Playwright verification failed: {output[:200]}")
        return False

    try:
        cleaned = re.sub(r"```json?\s*", "", output)
        cleaned = re.sub(r"```\s*", "", cleaned)
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return result.get("healthy", False)
    except (json.JSONDecodeError, AttributeError):
        pass

    # If we can't parse, assume unhealthy
    return False


def build_fix_prompt(error: DetectedError, dashboard_state: dict) -> str:
    """Construct the prompt for Claude Code to fix a bug."""
    dashboard_info = ""

    if dashboard_state and dashboard_state.get("summary"):
        dashboard_info = f"""
## Dashboard State (from Playwright inspection)
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
1. Use Playwright MCP to navigate to http://localhost:8050 and inspect the current dashboard state for additional context
2. Read the error carefully and identify the root cause in the source code
3. Find and read the relevant source file(s)
4. Make the MINIMAL fix needed to resolve this error
5. Do NOT refactor unrelated code or add unnecessary error handling
6. Do NOT suppress the error — fix the underlying bug
7. Run `python -m pytest tests/ --ignore=tests/test_output_cooldown.txt --ignore=tests/fixtures/ -x -q` to verify your fix
8. If you cannot determine the root cause, do NOT make speculative changes

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
# Git operations
# ---------------------------------------------------------------------------
def git_has_changes() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=LOCAL_REPO_PATH,
    )
    return bool(result.stdout.strip())


def git_commit_and_push(error_summary: str, branch: str) -> bool:
    """Stage modified tracked files, commit, and push."""
    # Stage tracked modifications
    subprocess.run(
        ["git", "add", "-u"],
        cwd=LOCAL_REPO_PATH,
        check=True,
    )
    # Also pick up new files Claude may have created in src/ or tests/
    for pattern in ["src/", "scripts/", "tests/"]:
        subprocess.run(
            ["git", "add", pattern],
            cwd=LOCAL_REPO_PATH,
            capture_output=True,
        )

    # Truncate summary for commit message
    short_summary = re.sub(r"[\[\]]", "", error_summary)[:68]
    commit_msg = f"fix(watchdog): {short_summary}"

    try:
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=LOCAL_REPO_PATH,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error(f"Git commit failed: {e.stderr}")
        return False

    result = subprocess.run(
        ["git", "push", "origin", branch],
        cwd=LOCAL_REPO_PATH,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error(f"Git push failed: {result.stderr}")
        return False

    log.info("Fix committed and pushed to %s", branch)
    return True


# ---------------------------------------------------------------------------
# VM restart
# ---------------------------------------------------------------------------
def vm_pull_and_restart(vm_name: str, vm_zone: str, branch: str) -> bool:
    """SSH into VM: kill dashboard, git pull, restart in tmux."""
    # 1. Stop existing process
    log.info("Stopping dashboard on VM...")
    ssh_exec(f"tmux send-keys -t {TMUX_SESSION} C-c C-c", vm_name, vm_zone, timeout=5)
    time.sleep(5)

    # Force kill if still running
    if check_process_alive(vm_name, vm_zone):
        ssh_exec("pkill -f 'run_web_dashboard.py'", vm_name, vm_zone, timeout=5)
        time.sleep(3)

    # 2. Git pull
    log.info("Pulling latest code on VM...")
    rc, stdout, stderr = ssh_exec(
        f"cd {VM_REPO_PATH} && git pull origin {branch}",
        vm_name,
        vm_zone,
        timeout=30,
    )
    if rc != 0:
        log.error(f"Git pull failed on VM: {stderr}")
        return False
    log.info("Git pull: %s", stdout.strip().split("\n")[-1])

    # 3. Ensure tmux session exists
    ssh_exec(
        f"tmux new-session -d -s {TMUX_SESSION} 2>/dev/null || true",
        vm_name,
        vm_zone,
        timeout=5,
    )

    # 4. Send launch command to tmux
    log.info("Restarting dashboard in tmux...")
    rc, _, stderr = ssh_exec(
        f"tmux send-keys -t {TMUX_SESSION} '{DASHBOARD_CMD}' Enter",
        vm_name,
        vm_zone,
        timeout=10,
    )
    if rc != 0:
        log.error(f"Failed to send restart command: {stderr}")
        return False

    # 5. Wait for startup, then health check
    log.info("Waiting 30s for dashboard startup...")
    time.sleep(30)

    if check_health_endpoint(vm_name, vm_zone):
        log.info("Dashboard restarted and healthy")
        return True

    # Retry after additional wait
    log.info("First health check failed, retrying in 15s...")
    time.sleep(15)
    alive = check_health_endpoint(vm_name, vm_zone)
    if alive:
        log.info("Dashboard healthy on second attempt")
    else:
        log.warning("Dashboard may not be fully healthy yet")
    return alive


# ---------------------------------------------------------------------------
# Post-fix verification
# ---------------------------------------------------------------------------
def verify_fix(
    fingerprint: str,
    vm_name: str,
    vm_zone: str,
    use_playwright: bool = True,
    wait_time: int = 120,
) -> bool:
    """Wait, then check that the same error doesn't reappear."""
    log.info(f"Verifying fix for {wait_time}s...")
    time.sleep(wait_time)

    # Check logs for recurrence
    log_tail = get_latest_log_tail(vm_name, vm_zone, lines=100)
    new_error = classify_error(log_tail)

    if new_error and new_error.fingerprint == fingerprint:
        log.warning("Same error recurred — fix did NOT work")
        return False

    # Playwright visual verification
    if use_playwright:
        if not verify_dashboard_health():
            log.warning("Playwright reports dashboard unhealthy")
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
    vm_name = args.vm_name
    vm_zone = args.vm_zone
    branch = args.branch
    cycle_count = 0

    # Start SSH tunnel for Playwright access
    if not args.dry_run:
        start_ssh_tunnel(vm_name, vm_zone)
        time.sleep(2)

    log.info(
        "Watchdog started. VM=%s, zone=%s, interval=%ds, dry_run=%s, no_claude=%s",
        vm_name,
        vm_zone,
        args.check_interval,
        args.dry_run,
        args.no_claude,
    )

    while True:
        try:
            cycle_count += 1

            # Ensure SSH tunnel is alive (for Playwright)
            if not args.dry_run:
                ensure_tunnel(vm_name, vm_zone)

            # === PHASE 1: HEALTH CHECK ===
            process_alive = check_process_alive(vm_name, vm_zone)
            endpoint_ok = (
                check_health_endpoint(vm_name, vm_zone) if process_alive else False
            )

            log.debug(
                "Cycle %d: process=%s, endpoint=%s",
                cycle_count,
                process_alive,
                endpoint_ok,
            )

            error: Optional[DetectedError] = None

            if process_alive and endpoint_ok:
                # === PHASE 2: LOG SCAN ===
                log_tail = get_latest_log_tail(vm_name, vm_zone)
                error = classify_error(log_tail)

                if error and not error.actionable:
                    log.debug(
                        "Transient error detected, ignoring: %s", error.summary[:100]
                    )
                    error = None

                # Periodic Playwright inspection
                if (
                    error is None
                    and not args.no_claude
                    and not args.dry_run
                    and cycle_count % PLAYWRIGHT_INSPECT_INTERVAL == 0
                ):
                    state = inspect_dashboard()
                    if state.get("healthy") is False:
                        errors = state.get("errors", [])
                        error = DetectedError(
                            category="ui_error",
                            summary=f"Playwright detected UI errors: {errors}",
                            traceback="",
                            log_context=str(state),
                            fingerprint=_compute_fingerprint(str(errors)),
                            timestamp=datetime.now().isoformat(),
                            actionable=True,
                        )
                        log.warning("Playwright detected UI error: %s", errors)

                if error is None:
                    time.sleep(args.check_interval)
                    continue

            elif not process_alive:
                # Process crashed
                crash_output = capture_crash_output(vm_name, vm_zone)
                log_tail = get_latest_log_tail(vm_name, vm_zone)
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
                    log_context=get_latest_log_tail(vm_name, vm_zone)[-1000:],
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
                vm_pull_and_restart(vm_name, vm_zone, branch)
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
                    vm_pull_and_restart(vm_name, vm_zone, branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            attempt = tracker.get_attempt_count(error.fingerprint) + 1

            # === PHASE 6: STOP DASHBOARD ===
            log.info("Stopping dashboard for fix (attempt %d)...", attempt)
            ssh_exec(
                f"tmux send-keys -t {TMUX_SESSION} C-c C-c",
                vm_name,
                vm_zone,
                timeout=5,
            )
            time.sleep(5)

            if args.no_claude:
                log.info("[NO-CLAUDE] Restarting without fix...")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                vm_pull_and_restart(vm_name, vm_zone, branch)
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
                vm_pull_and_restart(vm_name, vm_zone, branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            if not git_has_changes():
                log.warning("Claude did not produce any code changes")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                vm_pull_and_restart(vm_name, vm_zone, branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            # === PHASE 8: COMMIT AND PUSH ===
            log.info("Committing and pushing fix...")
            if not git_commit_and_push(error.summary, branch):
                log.error("Git commit/push failed")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                vm_pull_and_restart(vm_name, vm_zone, branch)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            # === PHASE 9: DEPLOY AND RESTART ===
            if not vm_pull_and_restart(vm_name, vm_zone, branch):
                log.error("VM restart failed after deploying fix")
                tracker.record_attempt(error.fingerprint, False, error.summary)
                time.sleep(COOLDOWN_AFTER_FAILURE_S)
                continue

            # === PHASE 10: VERIFY FIX ===
            fix_worked = verify_fix(
                error.fingerprint,
                vm_name,
                vm_zone,
                use_playwright=True,
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

    # Cleanup
    if _tunnel_proc and _tunnel_proc.poll() is None:
        log.info("Closing SSH tunnel...")
        _tunnel_proc.terminate()

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
        "--vm-name",
        default=VM_NAME,
        help=f"GCE VM name (default: {VM_NAME})",
    )
    parser.add_argument(
        "--vm-zone",
        default=VM_ZONE,
        help=f"GCE zone (default: {VM_ZONE})",
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
