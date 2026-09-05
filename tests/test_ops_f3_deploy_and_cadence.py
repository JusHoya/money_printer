"""OPS tooling for the F3 shadow deploy: the deploy script and the cadence checker.

Regression coverage for the F3 review findings of 2026-09-05, all of which were
defects in the *verification* rather than in the runtime:

deploy/pi/deploy_f3_shadow.sh
  1. the "loaded" gate grepped ``compose logs`` -- container stdout, which
     src/utils/logger.py never writes to, so the gate could only ever ``die``;
  3. it asserted the presence of any ``GENOME_`` line instead of the MODE VALUE,
     and wrote ``GENOME_STRATEGY_MODE`` into the losing env-file layer;
  4. an off-hour deploy silently forfeited a market-day via the missed-hour rule.

scripts/check_maia_emit_cadence.py
  2. the command the deploy printed 404'd with an unhandled traceback;
  5. the verdict ignored ``emit_executed`` -- the one event shadow mode prevents;
  6. the quote fallback could take a REJECT line, so file order decided the
     limit-price clause;
  7. ``NO_EMIT`` -- the NORMAL verdict -- was uninterpretable.

The deploy script is exercised through its side-effect-free ``--plan`` pre-flight
with the clock pinned by ``MP_DEPLOY_NOW_EPOCH``; the parts that need docker/sudo
are asserted structurally against the script text.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CHECKER = os.path.join(ROOT, "scripts", "check_maia_emit_cadence.py")
DEPLOY = os.path.join(ROOT, "deploy", "pi", "deploy_f3_shadow.sh")
DEPLOY_TEXT = open(DEPLOY, "r", encoding="utf-8").read()

TOP_OF_HOUR = 1757041200          # 2026-09-05T00:00:00Z -- exactly on a boundary
LATE_IN_HOUR = TOP_OF_HOUR + 2968  # +49m28s, the real 2026-09-05T03:49:28Z offset

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not on PATH")


def _check(*args: str):
    """Run the cadence checker; return (returncode, parsed stdout or None, stderr)."""
    proc = subprocess.run([sys.executable, CHECKER, *args],
                          capture_output=True, text=True, cwd=ROOT, timeout=120)
    try:
        verdict = json.loads(proc.stdout)
    except ValueError:
        verdict = None
    return proc.returncode, verdict, proc.stderr


def _plan(*args: str, epoch: int = TOP_OF_HOUR, **env):
    """Run the deploy script's side-effect-free pre-flight."""
    environ = {**os.environ, "MP_DEPLOY_NOW_EPOCH": str(epoch), **env}
    return subprocess.run([BASH, DEPLOY, "0c4b20502f2daf65", "--plan", *args],
                          capture_output=True, text=True, cwd=ROOT, timeout=120, env=environ)


@pytest.fixture()
def stub_dashboard():
    """A dashboard stub: /api/logs/tail serves one shadow EMIT, everything else 404s.

    Stands in for maia so the HTTP paths are exercised without the LAN.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = json.dumps({"ok": True, "file": "money_printer_20260905_034934.log",
                       "lines": 2, "content": _EMIT + _SHADOW}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] == "/api/logs/tail":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "Not Found")

        def log_message(self, *a):  # silence the stub
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Finding 2 -- an HTTP failure must be a readable message, never a traceback.
# ---------------------------------------------------------------------------

def test_endpoint_passed_as_base_url_is_a_readable_error_not_a_traceback(stub_dashboard):
    # The exact command the deploy script used to print. --url is a BASE url, so the
    # client appended the path a second time, the server 404'd, and urlopen raised
    # HTTPError straight through main() with no handler at all.
    rc, verdict, err = _check("--url", f"{stub_dashboard}/api/logs/tail", "--timeout", "5")
    assert rc == 2, (rc, err)
    assert verdict is None
    assert "Traceback" not in err, err
    assert "cannot run the check" in err and "HTTP 404" in err
    # and it names the actual mistake
    assert "--url is the BASE url" in err and f"--url {stub_dashboard}" in err


def test_the_base_url_the_deploy_prints_actually_works(stub_dashboard):
    """The same invocation with the base url reaches the endpoint and evaluates."""
    rc, verdict, err = _check("--url", stub_dashboard, "--no-data-log", "--timeout", "5",
                              "--strategy", "Genome")
    assert rc == 0, err
    assert verdict["verdict"] == "PASS" and verdict["n_emit"] == 1
    assert verdict["outcome_codes"] == {"GENOME_SHADOW": 1}


def test_unreachable_host_is_a_readable_error_not_a_traceback():
    rc, _, err = _check("--url", "http://nosuchhost.invalid:8050", "--timeout", "3")
    assert rc == 2, err
    assert "Traceback" not in err and "unreachable" in err


def test_deploy_prints_a_base_url_the_checker_accepts():
    import scripts.check_maia_emit_cadence as chk

    printed = [ln for ln in DEPLOY_TEXT.splitlines() if "check_maia_emit_cadence.py" in ln]
    assert printed, "the deploy script no longer tells the operator how to verify"
    urls = [tok for ln in printed for tok in ln.split()
            if tok.startswith("http://") or tok.startswith("https://")]
    assert urls, printed
    for url in urls:
        # _base_url_hint fires exactly when a path was passed where a base url belongs
        assert chk._base_url_hint(url) == "", f"the deploy prints an endpoint, not a base url: {url}"


# ---------------------------------------------------------------------------
# Finding 5 -- an EXECUTED outcome is the one event shadow mode must prevent.
# ---------------------------------------------------------------------------

_EMIT = ("2026-09-05 15:00:23 | INFO    | [Signal] EMIT strategy=Genome 0c4b2050 "
         "symbol=KXHIGHNY-26SEP05-B79.5 side=buy contract=NO price=0.86 qty=20 "
         "confidence=0.870 quote=0.85 limit=0.86\n")


def _log(tmp_path, *lines: str):
    path = tmp_path / "sandbox.log"
    path.write_text("".join(lines), encoding="utf-8")
    return str(path)


def test_an_executed_emit_fails_in_shadow_mode(tmp_path):
    executed = ("2026-09-05 15:00:23 | INFO    | [Signal] EXECUTED strategy=Genome 0c4b2050 "
                "symbol=KXHIGHNY-26SEP05-B79.5 side=buy contract=NO price=0.86 quantity=20\n")
    log = _log(tmp_path, _EMIT, executed)
    rc, verdict, err = _check("--file", log, "--no-data-log", "--strategy", "Genome")
    # exactly one outcome line, on the hour, limit == quote + 0.01 -- the old verdict
    # expression called this a PASS even though something reached the exchange.
    assert verdict["emit_multiple_outcomes"] == [] and verdict["emit_without_outcome"] == []
    assert verdict["emit_off_hour"] == [] and verdict["limit_price"]["verified_ok"] == 1
    assert verdict["verdict"] == "FAIL", verdict
    assert rc == 1
    assert verdict["emit_executed"] == ["15:00:23 KXHIGHNY-26SEP05-B79.5"]
    assert "nothing may" in err and "reach the exchange" in err
    # F4 paper mode, where EXECUTED is the expected outcome, opts out explicitly
    rc, verdict, _ = _check("--file", log, "--no-data-log", "--strategy", "Genome",
                            "--allow-executed")
    assert rc == 0 and verdict["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Finding 6 -- a REJECT line is never a quote source, so file order cannot decide.
# ---------------------------------------------------------------------------

_EMIT_NO_QUOTE = ("2026-09-05 15:00:23 | INFO    | [Signal] EMIT strategy=Genome 0c4b2050 "
                  "symbol=KXHIGHNY-26SEP05-B79.5 side=buy contract=NO price=0.86 qty=20 "
                  "confidence=0.870\n")
_SHADOW = ("2026-09-05 15:00:23 | INFO    | [Risk] REJECT strategy=Genome 0c4b2050 "
           "symbol=KXHIGHNY-26SEP05-B79.5 reason=GENOME_SHADOW side=buy contract=NO "
           "price=0.86 quantity=20 quote=0.85 limit=0.86\n")
# a same-second strategy skip for the SAME symbol carrying its own, unrelated quote --
# maia logs exactly these (reason=GENOME_MASK_FALSE ... quote=0.76)
_MASK_FALSE = ("2026-09-05 15:00:23 | INFO    | [Risk] REJECT strategy=Genome 0c4b2050 "
               "symbol=KXHIGHNY-26SEP05-B79.5 reason=GENOME_MASK_FALSE p_win=0.3691 "
               "quote=0.76 window=0 band=0\n")
_DECIDE = ("2026-09-05 15:00:23 | INFO    | [Genome] DECIDE strategy=Genome 0c4b2050 "
           "symbol=KXHIGHNY-26SEP05-B79.5 contract=NO quote=0.8500 limit=0.8600 "
           "p_win=0.8700 target_date=2026-09-05\n")


@pytest.mark.parametrize("order", ["reject_first", "reject_last"])
def test_the_verdict_does_not_depend_on_the_order_of_same_second_reject_lines(tmp_path, order):
    lines = ([_EMIT_NO_QUOTE, _MASK_FALSE, _SHADOW] if order == "reject_first"
             else [_EMIT_NO_QUOTE, _SHADOW, _MASK_FALSE])
    rc, verdict, _ = _check("--file", _log(tmp_path, *lines), "--no-data-log", "--strategy", "Genome")
    # Before the fix the fallback took whichever quote-carrying line came first in the
    # file: quote=0.76 -> false FAIL, quote=0.85 -> PASS. Now no reject is a source, so
    # both orderings agree, and with no DECIDE line the EMIT is honestly UNVERIFIED.
    assert verdict["verdict"] == "UNVERIFIED", verdict
    assert rc == 3
    assert verdict["limit_price"]["verified_bad"] == 0
    assert verdict["limit_price"]["unverified"] == ["15:00:23 KXHIGHNY-26SEP05-B79.5"]


@pytest.mark.parametrize("order", ["decide_first", "decide_last"])
def test_the_decide_line_wins_over_a_same_second_reject(tmp_path, order):
    lines = ([_EMIT_NO_QUOTE, _DECIDE, _MASK_FALSE, _SHADOW] if order == "decide_first"
             else [_EMIT_NO_QUOTE, _MASK_FALSE, _SHADOW, _DECIDE])
    rc, verdict, _ = _check("--file", _log(tmp_path, *lines), "--no-data-log", "--strategy", "Genome")
    assert rc == 0 and verdict["verdict"] == "PASS", verdict
    assert verdict["limit_price"]["verified_ok"] == 1
    assert verdict["limit_price"]["verified_bad"] == 0


# ---------------------------------------------------------------------------
# Finding 7 -- NO_EMIT is the NORMAL verdict and must say which case it is.
# ---------------------------------------------------------------------------

_TICK = "2026-09-05 18:{m:02d}:{s:02d} | INFO    | [Weather] Using METAR data for station KNYC\n"


def test_no_emit_says_no_boundary_was_sampled(tmp_path):
    # 18:37 -> 18:52, the window a real 500-line tail covers: it never spans a :00.
    lines = [_TICK.format(m=m, s=11) for m in range(37, 53)]
    rc, verdict, err = _check("--file", _log(tmp_path, *lines), "--no-data-log", "--strategy", "Genome")
    assert rc == 3 and verdict["verdict"] == "NO_EMIT"
    assert verdict["no_emit_reason"] == "NO_BOUNDARY_IN_WINDOW", verdict
    assert verdict["boundaries_sampled"] == []
    assert "NO_BOUNDARY_IN_WINDOW" in err and "next :00 UTC" in err


def test_no_emit_says_the_genome_is_alive_and_legitimately_silent(tmp_path):
    already = ("2026-09-05 19:00:05 | INFO    | [Risk] REJECT strategy=Genome 0c4b2050 "
               "symbol=KXHIGHNY-26SEP05-B79.5 reason=GENOME_ALREADY_TRADED target_date=2026-09-05\n")
    log = _log(tmp_path, _TICK.format(m=59, s=50).replace("18:", "18:"), already)
    rc, verdict, err = _check("--file", log, "--no-data-log", "--strategy", "Genome")
    assert rc == 3 and verdict["verdict"] == "NO_EMIT"
    assert verdict["no_emit_reason"] == "GENOME_ALIVE_NO_EMIT", verdict
    assert verdict["strategy_skip_codes"] == {"GENOME_ALREADY_TRADED": 1}
    assert verdict["boundaries_sampled"] == ["2026-09-05T19:00"]
    assert "GENOME_ALIVE_NO_EMIT" in err and "Nothing to do" in err


def test_no_emit_says_the_genome_logged_nothing_at_all(tmp_path):
    # a boundary WAS sampled and no strategy line exists: the genome is not loaded
    log = _log(tmp_path, "2026-09-05 19:00:05 | INFO    | [Weather] Using METAR data for station KNYC\n")
    rc, verdict, err = _check("--file", log, "--no-data-log", "--strategy", "Genome")
    assert rc == 3 and verdict["verdict"] == "NO_EMIT"
    assert verdict["no_emit_reason"] == "NO_GENOME_LINES", verdict
    assert verdict["boundaries_sampled"] == ["2026-09-05T19:00"] and verdict["n_strategy_lines"] == 0
    assert "NO_GENOME_LINES" in err and "REFUSED" in err


# ---------------------------------------------------------------------------
# Finding 1 -- the loaded gate must read the FILE log, not container stdout.
# ---------------------------------------------------------------------------

def test_the_loaded_gate_does_not_grep_container_stdout():
    # src/utils/logger.py attaches only a FileHandler and pins any console
    # StreamHandler to WARNING+, so no bot log line ever reaches container stdout and
    # `compose logs | grep 'GenomeStrategy'` is empty on a GOOD deploy too.
    gate_lines = [ln for ln in DEPLOY_TEXT.splitlines()
                  if "GenomeStrategy" in ln and "logs --tail" in ln]
    assert gate_lines == [], f"the loaded/REFUSED gate still reads container stdout: {gate_lines}"


def test_the_loaded_gate_reads_the_container_file_log_and_separates_refused():
    assert "/app/logs/money_printer_" in DEPLOY_TEXT, "the gate does not read the file log"
    assert "GenomeStrategy REFUSED" in DEPLOY_TEXT, "the gate cannot distinguish REFUSED from loaded"
    # both branches must exist: a REFUSED genome and a missing line are different failures
    assert "the bot is running V2 only" in DEPLOY_TEXT
    assert "neither 'loaded' nor 'REFUSED'" in DEPLOY_TEXT


def test_logger_really_writes_nothing_to_stdout():
    """The premise of finding 1, asserted against the real logger rather than assumed.

    Not a regression test for a fix -- it guards the *reason* the compose-logs gate
    could never work, so that a future logging change invalidating it is noticed here.
    """
    import logging

    from src.utils.logger import logger

    # propagate=False, so its records never reach a root console handler either
    assert logger.propagate is False
    assert any(isinstance(h, logging.FileHandler) for h in logger.handlers), logger.handlers
    # No handler writes to the real stdout/stderr -- the same predicate
    # src/utils/logger.py uses, so pytest's own capture handlers (LogCaptureHandler on a
    # StringIO, the null _FileHandler) do not count and do not make this flaky.
    real_streams = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    console = [h for h in logger.handlers
               if isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler)
               and getattr(h, "stream", None) in real_streams]
    assert console == [], f"a console handler would put bot logs on container stdout: {console}"


# ---------------------------------------------------------------------------
# Finding 3 -- assert the MODE VALUE from the container; stop writing dead config.
# ---------------------------------------------------------------------------

def test_the_deploy_asserts_the_mode_value_from_the_container():
    assert 'printenv GENOME_STRATEGY_MODE' in DEPLOY_TEXT
    assert '"$ACTUAL_MODE" == "shadow"' in DEPLOY_TEXT, "the mode value is not asserted"
    assert '"$ACTUAL_ID" == "$GENOME_ID"' in DEPLOY_TEXT, "the genome id value is not asserted"
    # the old check: any GENOME_ line in `docker exec env` satisfied it, so a lone
    # GENOME_STRATEGY_ID -- or GENOME_STRATEGY_MODE=paper -- passed
    assert "env | grep -E 'GENOME_" not in DEPLOY_TEXT


def test_the_deploy_no_longer_writes_the_losing_env_file_layer():
    # docker-compose.yml declares GENOME_STRATEGY_MODE under environment:, which compose
    # resolves ABOVE env_file:, so a line in /srv/money_printer/.env is dead config.
    compose = open(os.path.join(ROOT, "deploy", "pi", "docker-compose.yml"),
                   encoding="utf-8").read()
    assert "GENOME_STRATEGY_MODE:" in compose, "premise changed: compose no longer pins the mode"
    assert "upsert GENOME_STRATEGY_MODE" not in DEPLOY_TEXT, "still writing the dead layer"
    assert "DEAD CONFIG" in DEPLOY_TEXT, "the dead env-file line is not neutralised"


@needs_bash
def test_the_deploy_refuses_a_paper_mode_shell():
    # The invoking shell IS the layer that beats compose's default, so this is the one
    # way a "shadow deploy" could silently not be shadow.
    proc = _plan(GENOME_STRATEGY_MODE="paper")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "would NOT be shadow" in proc.stderr, proc.stderr
    proc = _plan(GENOME_STRATEGY_MODE="shadow")
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Finding 4 -- an off-hour deploy forfeits a market-day; the pre-flight says so.
# ---------------------------------------------------------------------------

@needs_bash
def test_preflight_reports_the_forfeited_market_day_off_hour():
    proc = _plan(epoch=LATE_IN_HOUR)   # +49m28s, the real 2026-09-05T03:49:28Z offset
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "boundary verdict    = LATE" in proc.stdout, proc.stdout
    assert "COST of deploying now" in proc.stdout
    assert "GENOME_MISSED_HOUR" in proc.stdout
    # and it offers both ways out the review asked for: wait, or acknowledge
    assert "will WAIT" in proc.stdout and "--any-time" in proc.stdout


@needs_bash
def test_preflight_is_silent_about_cost_inside_the_aligned_window():
    proc = _plan(epoch=TOP_OF_HOUR)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "boundary verdict    = IN_WINDOW" in proc.stdout, proc.stdout
    assert "no city-day is forfeited" in proc.stdout
    assert "COST of deploying now" not in proc.stdout


@needs_bash
def test_any_time_acknowledges_the_cost_instead_of_waiting():
    proc = _plan("--any-time", epoch=LATE_IN_HOUR)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ACCEPTING the forfeited market-day" in proc.stdout, proc.stdout
    assert "will WAIT" not in proc.stdout


@needs_bash
def test_plan_has_no_side_effects():
    """--plan must exit before git/sudo/docker, so it is safe to run anywhere."""
    proc = _plan(epoch=TOP_OF_HOUR)
    assert proc.returncode == 0
    assert "nothing was changed" in proc.stdout
    for forbidden in ("git pull", "docker compose build", "forecast cache bind"):
        assert forbidden not in proc.stdout, f"--plan ran '{forbidden}'"


def test_the_boundary_tolerance_matches_the_strategy_constant():
    """The pre-flight's arithmetic is only true while it mirrors the runtime constant."""
    from src.strategies.genome_strategy import DEFAULT_TOP_OF_HOUR_TOLERANCE_S

    assert f"MP_TOP_OF_HOUR_TOLERANCE_S:-{DEFAULT_TOP_OF_HOUR_TOLERANCE_S}" in DEPLOY_TEXT


# ---------------------------------------------------------------------------
# Finding 8 -- the runbook stated arithmetic and remedies that do not hold.
# ---------------------------------------------------------------------------

RUNBOOK = os.path.join(ROOT, "docs", "factory", "F3_RUNBOOK.md")
RUNBOOK_TEXT = open(RUNBOOK, "r", encoding="utf-8").read()


def test_runbook_does_not_claim_a_500_line_tail_is_hours():
    # Measured six times on 2026-09-05: 8.1-16.3 min, three consecutive samples 14.0/14.2/14.3.
    assert "roughly one to two hours" not in RUNBOOK_TEXT
    assert "8-16 minutes" in RUNBOOK_TEXT or "8–16 minutes" in RUNBOOK_TEXT, \
        "the runbook does not state the measured tail window"


def test_runbook_does_not_offer_the_two_inoperative_no_emit_remedies():
    # --lines is clamped at 500 by BOTH ends, and maia has no ssh, so neither old
    # remedy could ever be carried out.
    assert "raise `--lines`" not in RUNBOOK_TEXT
    assert "pull a longer data-log window from the bind mount" not in RUNBOOK_TEXT
    assert "cannot widen it" in RUNBOOK_TEXT and "no ssh to maia" in RUNBOOK_TEXT


def test_the_lines_clamp_the_runbook_describes_is_real():
    """Both ends really do clamp at 500 -- the reason --lines is not a remedy."""
    import inspect

    import scripts.check_maia_emit_cadence as chk

    assert "min(max(lines, 1), 500)" in inspect.getsource(chk.fetch_log_tail)
    server = open(os.path.join(ROOT, "src", "web", "server.py"), encoding="utf-8").read()
    assert "all_lines[-min(max(lines, 1), 500):]" in server


def test_runbook_drops_the_stale_xfail_parenthetical():
    from src.strategies import genome_strategy  # noqa: F401  (the module STRATEGY landed)

    assert "until STRATEGY lands" not in RUNBOOK_TEXT
    assert "No xfails remain" in RUNBOOK_TEXT


def test_runbook_does_not_claim_the_protected_diff_is_empty():
    """§1.1 documents ONE allowed hunk in matching_engine.py, so the diff is never empty."""
    proc = subprocess.run(
        ["git", "diff", "--stat", "38d5fdd", "--", "src/core/risk_manager.py",
         "src/bots/mixins.py", "src/core/matching_engine.py"],
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert "matching_engine.py" in proc.stdout, "premise changed: the allowed hunk is gone"
    assert "empty on the merge commit" not in RUNBOOK_TEXT, \
        "the checklist still asserts an empty diff, which is false and contradicts §1.1"
    assert "must stay empty" not in RUNBOOK_TEXT, \
        "the intro still asserts an empty diff, which is false and contradicts §1.1"
    assert "is **not** empty and is not supposed to be" in RUNBOOK_TEXT


def test_runbook_records_the_verified_cadence_pass():
    assert '`outcome_codes == {"GENOME_SHADOW": 4}`' in RUNBOOK_TEXT
    assert "verified_ok 4" in RUNBOOK_TEXT


def test_runbook_explains_all_three_no_emit_reasons():
    import scripts.check_maia_emit_cadence as chk

    for reason in chk.NO_EMIT_EXPLANATION:
        assert reason in RUNBOOK_TEXT, f"the runbook does not explain no_emit_reason={reason}"


def test_runbook_warns_that_an_off_hour_deploy_forfeits_the_day():
    assert "3.1" in RUNBOOK_TEXT and "GENOME_MISSED_HOUR" in RUNBOOK_TEXT
    assert "03:49:28Z" in RUNBOOK_TEXT, "the runbook does not record the deploy that cost a day"


def test_runbook_records_that_bot_logs_never_reach_container_stdout():
    assert "NOT on container stdout" in RUNBOOK_TEXT or "not on container stdout" in RUNBOOK_TEXT
    assert "/app/logs/money_printer_" in RUNBOOK_TEXT
