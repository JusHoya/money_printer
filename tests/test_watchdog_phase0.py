"""Phase 0 watchdog tests — FR-0.5 / PRD section 8 exit criterion 5.

Targets: <=1 false alert/day in normal operation AND a genuinely dead
process alerts within 10 minutes.

Covers three layers:

1. The pure-python decision core in scripts/vm_watchdog.py
   (evaluate_host_health): 15-min staleness margin, 10-min rollover grace
   for missing session logs, immediate process-death detection, and
   per-condition alert backoff with reset-on-recovery.
2. Bash integration for scripts/host_watchdog.sh via a temp-dir fixture with
   PATH-stubbed pgrep/curl: fresh logs -> no alert; missing logs once -> no
   alert; missing logs persisting >10 min -> alert; stale log -> alert; dead
   process -> alert on the first check; repeated condition -> 60-min
   backoff; retired retrain marker ignored; --check-only is read-only.
3. Bash integration for scripts/watchdog_cron.sh: endpoint failure with a
   live process needs two consecutive failing runs before a restart; a dead
   process restarts immediately; the restart outcome message is no longer
   suppressed by its own initial alert (per-incident cooldown).

Run:
    $env:PYTHONPATH="."; python -m pytest tests/test_watchdog_phase0.py
"""

import importlib.util
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOST_SH = REPO / "scripts" / "host_watchdog.sh"
CRON_SH = REPO / "scripts" / "watchdog_cron.sh"

# Import the decision core without the module's file-logging side effect.
os.environ.setdefault("VM_WATCHDOG_NO_FILELOG", "1")
_spec = importlib.util.spec_from_file_location(
    "vm_watchdog", REPO / "scripts" / "vm_watchdog.py"
)
vm_watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vm_watchdog)

evaluate_host_health = vm_watchdog.evaluate_host_health
HostHealthState = vm_watchdog.HostHealthState
PROCESS_DEAD = vm_watchdog.COND_PROCESS_DEAD
LOG_STALE = vm_watchdog.COND_LOG_STALE
LOGS_MISSING = vm_watchdog.COND_LOGS_MISSING
STALE_MAX_AGE = vm_watchdog.STALE_MAX_AGE_S
MISSING_GRACE = vm_watchdog.MISSING_LOG_GRACE_S
REALERT = vm_watchdog.REALERT_INTERVAL_S

NOW = 1_800_000_000.0


def _find_bash():
    """Locate a POSIX bash (Git bash on Windows, never the WSL shim)."""
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ]
    for c in candidates:
        if c and os.path.exists(c) and "system32" not in c.lower():
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(
    BASH is None, reason="POSIX bash not available on this machine"
)


# ===========================================================================
# 1. Decision core (pure python, always runs)
# ===========================================================================
class TestDecisionCore:
    def test_healthy_no_alert(self):
        d = evaluate_host_health(True, NOW - 70, NOW)
        assert d.failing == []
        assert d.alerts_due == []
        assert d.pending == []

    def test_cycle_transition_quiet_window_is_not_stale(self):
        # A no-retrain cycle transition quiets the log for a few minutes;
        # 10 min old is still comfortably inside the 15-min margin.
        d = evaluate_host_health(True, NOW - 600, NOW)
        assert d.failing == []
        assert d.alerts_due == []

    def test_stale_log_alerts(self):
        d = evaluate_host_health(True, NOW - (STALE_MAX_AGE + 100), NOW)
        assert d.failing == [LOG_STALE]
        assert d.alerts_due == [LOG_STALE]

    def test_missing_logs_first_check_is_pending_not_failing(self):
        d = evaluate_host_health(True, None, NOW)
        assert d.failing == []
        assert d.alerts_due == []
        assert d.pending == [LOGS_MISSING]
        assert d.state.missing_logs_since == NOW

    def test_missing_logs_within_grace_stays_pending(self):
        st = HostHealthState(missing_logs_since=NOW - (MISSING_GRACE - 100))
        d = evaluate_host_health(True, None, NOW, st)
        assert d.failing == []
        assert d.pending == [LOGS_MISSING]

    def test_missing_logs_past_grace_alerts(self):
        st = HostHealthState(missing_logs_since=NOW - (MISSING_GRACE + 100))
        d = evaluate_host_health(True, None, NOW, st)
        assert d.failing == [LOGS_MISSING]
        assert d.alerts_due == [LOGS_MISSING]

    def test_missing_logs_recovery_clears_state(self):
        st = HostHealthState(
            missing_logs_since=NOW - 700,
            last_alert_ts={LOGS_MISSING: NOW - 60},
        )
        d = evaluate_host_health(True, NOW - 30, NOW, st)
        assert d.failing == []
        assert d.state.missing_logs_since is None
        assert LOGS_MISSING not in d.state.last_alert_ts

    def test_rollover_sequence_never_alerts(self):
        # t0: fresh log; t0+300: logs archived, none yet; t0+480: new log.
        st = HostHealthState()
        d = evaluate_host_health(True, NOW - 70, NOW, st)
        assert d.alerts_due == []
        d = evaluate_host_health(True, None, NOW + 300, d.state)
        assert d.alerts_due == []
        d = evaluate_host_health(True, (NOW + 480) - 10, NOW + 480, d.state)
        assert d.alerts_due == []
        assert d.failing == []

    def test_dead_process_alerts_immediately(self):
        # No grace, no margin: satisfies the 10-min bound at 5-min cron
        # cadence even with fresh logs.
        d = evaluate_host_health(False, NOW - 70, NOW)
        assert d.failing == [PROCESS_DEAD]
        assert d.alerts_due == [PROCESS_DEAD]

    def test_new_condition_not_suppressed_by_recent_other_alert(self):
        # log_stale alerted 1 min ago; process now dies -> must still alert.
        st = HostHealthState(last_alert_ts={LOG_STALE: NOW - 60})
        d = evaluate_host_health(False, NOW - (STALE_MAX_AGE + 100), NOW, st)
        assert PROCESS_DEAD in d.alerts_due
        # The delivered message covers both, so both timestamps refresh.
        assert d.state.last_alert_ts[LOG_STALE] == NOW
        assert d.state.last_alert_ts[PROCESS_DEAD] == NOW

    def test_backoff_suppresses_repeat_within_interval(self):
        st = HostHealthState(last_alert_ts={PROCESS_DEAD: NOW - (REALERT - 100)})
        d = evaluate_host_health(False, NOW - 70, NOW, st)
        assert d.failing == [PROCESS_DEAD]
        assert d.alerts_due == []

    def test_realert_after_interval(self):
        st = HostHealthState(last_alert_ts={PROCESS_DEAD: NOW - (REALERT + 1)})
        d = evaluate_host_health(False, NOW - 70, NOW, st)
        assert d.alerts_due == [PROCESS_DEAD]

    def test_recovery_resets_backoff(self):
        st = HostHealthState(last_alert_ts={PROCESS_DEAD: NOW - 120})
        d = evaluate_host_health(True, NOW - 70, NOW, st)
        assert PROCESS_DEAD not in d.state.last_alert_ts
        # Flap: dies again shortly after -> alerts immediately again.
        d2 = evaluate_host_health(False, NOW - 70, NOW + 300, d.state)
        assert d2.alerts_due == [PROCESS_DEAD]

    def test_ten_minute_detection_bound(self):
        # Process dies at t; the next 5-min cron check must raise the alert.
        death_t = NOW
        first_check_after = death_t + 300
        d = evaluate_host_health(False, death_t - 10, first_check_after)
        assert PROCESS_DEAD in d.alerts_due
        assert first_check_after - death_t <= 600


# ===========================================================================
# 2/3. Bash integration harness
# ===========================================================================
PGREP_STUB = """#!/bin/sh
echo "pgrep $@" >> "$STUB_CALLS"
if [ -f "$PGREP_ALIVE_FLAG" ]; then echo 1234; exit 0; fi
exit 1
"""

CURL_STUB = """#!/bin/sh
echo "curl $@" >> "$STUB_CALLS"
payload=""
prev=""
url=""
for a in "$@"; do
  if [ "$prev" = "-d" ]; then payload="$a"; fi
  prev="$a"
  url="$a"
done
case "$url" in
  *discord*)
    printf '%s\\n' "$payload" >> "$DISCORD_OUT"
    printf '204'
    ;;
  *)
    code=""
    if [ -f "$HEALTH_CODES" ]; then
      code=$(head -n1 "$HEALTH_CODES")
      tail -n +2 "$HEALTH_CODES" > "$HEALTH_CODES.tmp" && mv "$HEALTH_CODES.tmp" "$HEALTH_CODES"
    fi
    [ -n "$code" ] || code=000
    printf '%s' "$code"
    ;;
esac
exit 0
"""

PKILL_STUB = """#!/bin/sh
echo "pkill $@" >> "$STUB_CALLS"
exit 1
"""

TMUX_STUB = """#!/bin/sh
echo "tmux $@" >> "$STUB_CALLS"
case "$1" in
  has-session) exit 1 ;;
  *) exit 0 ;;
esac
"""


class WatchdogHarness:
    """Temp-dir project fixture with PATH-stubbed pgrep/curl/pkill/tmux."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.proj = tmp / "proj"
        self.logs = self.proj / "logs"
        self.logs.mkdir(parents=True)
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.discord_out = tmp / "discord_payloads.txt"
        self.alive_flag = tmp / "proc_alive"
        self.health_codes = tmp / "health_codes.txt"
        self.calls = tmp / "stub_calls.txt"
        for name, body in [
            ("pgrep", PGREP_STUB),
            ("curl", CURL_STUB),
            ("pkill", PKILL_STUB),
            ("tmux", TMUX_STUB),
        ]:
            self._write_script(self.bin / name, body)

    @staticmethod
    def _write_script(path: Path, content: str):
        with open(path, "w", newline="\n") as f:
            f.write(content)
        path.chmod(0o755)

    def env(self, **extra):
        e = dict(os.environ)
        e.update(
            {
                "HOME": str(self.home),
                "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                "HOST_WATCHDOG_PROJECT_DIR": self.proj.as_posix(),
                "WATCHDOG_PROJECT_DIR": self.proj.as_posix(),
                "DISCORD_WEBHOOK_URL": "https://discord.test/api/webhooks/wd-test",
                "DISCORD_OUT": self.discord_out.as_posix(),
                "PGREP_ALIVE_FLAG": self.alive_flag.as_posix(),
                "HEALTH_CODES": self.health_codes.as_posix(),
                "STUB_CALLS": self.calls.as_posix(),
                "WATCHDOG_RESTART_STARTUP_WAIT": "0",
            }
        )
        e.update(extra)
        return e

    # Git-for-Windows bash prepends its own /mingw64/bin and /usr/bin ahead of
    # the inherited PATH at startup, which would shadow the curl stub with the
    # real curl. Re-prepend the stub dir INSIDE bash (cygpath-converted) so
    # the stubs always win, then exec the script under test.
    _WRAPPER = (
        'if command -v cygpath >/dev/null 2>&1; then SB=$(cygpath -u "$STUB_BIN"); '
        'else SB="$STUB_BIN"; fi; PATH="$SB:$PATH"; exec "$0" "$@"'
    )

    def _run_script(self, script: Path, *args, **envextra):
        env = self.env(**envextra)
        env["STUB_BIN"] = self.bin.as_posix()
        return subprocess.run(
            [BASH, "-c", self._WRAPPER, script.as_posix(), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_host(self, *args, **envextra):
        return self._run_script(HOST_SH, *args, **envextra)

    def run_cron(self, *args, **envextra):
        return self._run_script(CRON_SH, *args, **envextra)

    # --- scenario helpers ---------------------------------------------------
    def set_process(self, alive: bool):
        if alive:
            self.alive_flag.write_text("1")
        elif self.alive_flag.exists():
            self.alive_flag.unlink()

    def write_session_log(self, age_s=0, name="session_20260724.log"):
        p = self.logs / name
        p.write_text("heartbeat\n")
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def write_ts_file(self, path: Path, age_s: int):
        # No trailing newline: the bash side reads with head -n1 and a strict
        # numeric regex, so a CRLF would corrupt it.
        with open(path, "w", newline="\n") as f:
            f.write(str(int(time.time() - age_s)))

    def set_health(self, *codes):
        with open(self.health_codes, "w", newline="\n") as f:
            f.write("\n".join(codes) + "\n")

    def cond_file(self, cond: str) -> Path:
        return self.logs / f"host_watchdog_cond_{cond}.ts"

    @property
    def missing_since(self) -> Path:
        return self.logs / "host_watchdog_missing_since.ts"

    @property
    def cron_fail_marker(self) -> Path:
        return self.logs / "watchdog_cron_endpoint_fail.ts"

    def payloads(self):
        if not self.discord_out.exists():
            return []
        return [
            line
            for line in self.discord_out.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def calls_text(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""


@pytest.fixture
def wd(tmp_path):
    return WatchdogHarness(tmp_path)


# ===========================================================================
# 2. host_watchdog.sh (alert-only external watchdog)
# ===========================================================================
@needs_bash
class TestHostWatchdogBash:
    def test_healthy_fresh_log_no_alert(self, wd):
        wd.set_process(True)
        wd.write_session_log(age_s=60)
        r = wd.run_host()
        assert r.returncode == 0, r.stdout + r.stderr
        assert wd.payloads() == []
        assert not wd.missing_since.exists()

    def test_cycle_transition_log_age_no_alert(self, wd):
        # 10-min-old log: legitimate cycle transition, inside 15-min margin.
        wd.set_process(True)
        wd.write_session_log(age_s=600)
        r = wd.run_host()
        assert r.returncode == 0, r.stdout + r.stderr
        assert wd.payloads() == []

    def test_missing_logs_first_check_arms_grace_no_alert(self, wd):
        wd.set_process(True)
        r = wd.run_host()
        assert r.returncode == 0, r.stdout + r.stderr
        assert wd.payloads() == []
        assert wd.missing_since.exists()

    def test_missing_logs_persisting_past_grace_alerts(self, wd):
        wd.set_process(True)
        wd.write_ts_file(wd.missing_since, age_s=700)  # first seen 11:40 ago
        r = wd.run_host()
        assert r.returncode == 1, r.stdout + r.stderr
        assert len(wd.payloads()) == 1
        assert "no session" in wd.payloads()[0]

    def test_missing_logs_recovery_clears_state(self, wd):
        wd.set_process(True)
        wd.write_ts_file(wd.missing_since, age_s=700)
        wd.write_ts_file(wd.cond_file("logs_missing"), age_s=60)
        wd.write_session_log(age_s=30)  # new session log appears
        r = wd.run_host()
        assert r.returncode == 0, r.stdout + r.stderr
        assert not wd.missing_since.exists()
        assert not wd.cond_file("logs_missing").exists()

    def test_stale_log_alerts(self, wd):
        wd.set_process(True)
        wd.write_session_log(age_s=1200)  # 20 min > 15 min margin
        r = wd.run_host()
        assert r.returncode == 1, r.stdout + r.stderr
        assert len(wd.payloads()) == 1
        assert "stale" in wd.payloads()[0]

    def test_dead_process_alerts_on_first_check(self, wd):
        # The 10-minute real-failure bound: dead process, logs still fresh.
        wd.set_process(False)
        wd.write_session_log(age_s=60)
        r = wd.run_host()
        assert r.returncode == 1, r.stdout + r.stderr
        assert len(wd.payloads()) == 1
        assert "not running" in wd.payloads()[0]
        assert wd.cond_file("process_dead").exists()

    def test_repeat_condition_backs_off(self, wd):
        wd.set_process(False)
        wd.write_session_log(age_s=60)
        r1 = wd.run_host()
        r2 = wd.run_host()  # same ongoing incident 1 cron cycle later
        assert r1.returncode == 1 and r2.returncode == 1
        assert len(wd.payloads()) == 1  # one incident != alert storm

    def test_realert_after_backoff_interval(self, wd):
        wd.set_process(False)
        wd.write_session_log(age_s=60)
        wd.run_host()
        assert len(wd.payloads()) == 1
        # Pretend the last alert was >60 min ago.
        wd.write_ts_file(wd.cond_file("process_dead"), age_s=4000)
        wd.run_host()
        assert len(wd.payloads()) == 2

    def test_new_condition_not_suppressed_by_recent_alert(self, wd):
        # Stale-log alert fires; minutes later the process also dies. The
        # process-death alert must NOT be swallowed by a shared cooldown.
        wd.set_process(True)
        wd.write_session_log(age_s=1200)
        wd.run_host()
        assert len(wd.payloads()) == 1
        wd.set_process(False)
        r = wd.run_host()
        assert r.returncode == 1
        assert len(wd.payloads()) == 2
        assert "not running" in wd.payloads()[1]

    def test_retired_retrain_marker_is_ignored(self, wd):
        # Phase 0 removed the runtime retrain; a leftover "retraining" marker
        # must not widen the staleness margin (old behavior: 3000s).
        wd.set_process(True)
        marker = wd.logs / ".orchestrator_state"
        with open(marker, "w", newline="\n") as f:
            f.write(f"retraining {int(time.time())}")
        wd.write_session_log(age_s=1200)  # stale under the 900s margin
        r = wd.run_host()
        assert r.returncode == 1, r.stdout + r.stderr
        assert len(wd.payloads()) == 1

    def test_check_only_is_read_only_and_never_alerts(self, wd):
        wd.set_process(True)
        # Missing logs: within grace -> OK, and no state file may be armed.
        r = wd.run_host("--check-only")
        assert r.returncode == 0, r.stdout + r.stderr
        assert not wd.missing_since.exists()
        # Stale log: unhealthy exit code, but still no alert, no state.
        wd.write_session_log(age_s=1200)
        r = wd.run_host("--check-only")
        assert r.returncode == 1
        assert wd.payloads() == []
        assert not wd.cond_file("log_stale").exists()

    def test_disable_flag_silences_everything(self, wd):
        wd.set_process(False)
        (wd.proj / ".host_watchdog_disabled").write_text("maintenance")
        r = wd.run_host()
        assert r.returncode == 0, r.stdout + r.stderr
        assert wd.payloads() == []


# ===========================================================================
# 3. watchdog_cron.sh (restart watchdog)
# ===========================================================================
@needs_bash
class TestWatchdogCronBash:
    def test_endpoint_failure_with_live_process_needs_confirmation(self, wd):
        wd.set_process(True)
        wd.set_health("000")
        r = wd.run_cron()
        assert r.returncode == 1, r.stdout + r.stderr
        assert "tmux new-session" not in wd.calls_text()
        assert wd.cron_fail_marker.exists()
        assert wd.payloads() == []  # no alert on an unconfirmed blip

    def test_endpoint_failure_confirmed_restarts_and_reports_outcome(self, wd):
        wd.set_process(True)
        wd.write_ts_file(wd.cron_fail_marker, age_s=300)  # previous run failed
        wd.set_health("000", "000")  # initial check + post-restart check
        r = wd.run_cron()
        assert r.returncode == 2, r.stdout + r.stderr
        assert "new-session" in wd.calls_text()
        msgs = wd.payloads()
        # Per-incident reporting: the outcome message is no longer suppressed
        # by the cooldown timestamp its own initial alert just wrote.
        assert len(msgs) == 2
        assert "attempting restart" in msgs[0]
        assert "FAILED" in msgs[1]

    def test_dead_process_restarts_immediately(self, wd):
        wd.set_process(False)
        wd.set_health("000", "200")  # dead now, healthy after restart
        r = wd.run_cron()
        assert r.returncode == 0, r.stdout + r.stderr
        assert "new-session" in wd.calls_text()
        msgs = wd.payloads()
        assert len(msgs) == 2
        assert "attempting restart" in msgs[0]
        assert "succeeded" in msgs[1]

    def test_healthy_run_clears_confirmation_marker(self, wd):
        wd.set_process(True)
        wd.write_ts_file(wd.cron_fail_marker, age_s=300)
        wd.set_health("200")
        r = wd.run_cron()
        assert r.returncode == 0, r.stdout + r.stderr
        assert not wd.cron_fail_marker.exists()
        assert wd.payloads() == []
