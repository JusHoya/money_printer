"""
VM Watchdog — Automated error monitor and self-healing loop.

Runs ON the VM alongside the dashboard. Monitors for errors, uses Claude Code
+ Playwright MCP to analyze/fix bugs, commits/pushes, and restarts the dashboard.

Flow:
    Monitor (process/endpoint/logs) → Detect error →
    Inspect dashboard (Playwright) → Claude fix → Commit/push →
    Restart dashboard → Verify (Playwright) → Loop

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
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via CLI args)
# ---------------------------------------------------------------------------
REPO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TMUX_SESSION = "money"

DASHBOARD_CMD = (
    f"cd {REPO_PATH} && source {REPO_PATH}/venv/bin/activate && "
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

GIT_BRANCH = "refactor_v0.1"

CLAUDE_TIMEOUT_S = 300

# Inspect dashboard via Playwright every N monitoring cycles
PLAYWRIGHT_INSPECT_INTERVAL = 15

# Grace period after watchdog start — skip Playwright inspections while
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
            cwd=REPO_PATH,
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
7. Run `python3 -m pytest tests/ --ignore=tests/test_output_cooldown.txt --ignore=tests/fixtures/ -x -q` to verify your fix
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
    use_playwright: bool = True,
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

                # Periodic Playwright inspection (skip during startup grace period)
                in_grace_period = (time.time() - start_time) < STARTUP_GRACE_PERIOD_S
                if (
                    error is None
                    and not args.no_claude
                    and not args.dry_run
                    and not in_grace_period
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
