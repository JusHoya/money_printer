"""``scripts/genome_dry_run.py`` on one REAL archived city-day (F3 INFRA).

PRD_STRATEGY_FACTORY.md Phase F3 exit criterion: every emitted weather signal
carries a tz-aware ``expiration_time`` equal to the settlement-day close, and
the 24-h dev-box dry run settles its positions through
``SimulatedExchange._settle_weather_position``.

The pinned day, NY 2026-07-20, is one on which the V2 waterfall actually
opens a position (BUY NO on KXHIGHNY-26JUL20-B79.5 at the 16:00Z candle), so
the expiration/settlement assertions are exercised on a real position and not
only on the empty path. A second candidate is tried if the first yields no
position, so a re-archived ladder does not turn this into a vacuous test.

The whole run is well under a second; the 60-s budget in the sprint contract
is enforced explicitly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import genome_dry_run as gdr  # noqa: E402

CANDIDATE_DAYS = (("NY", "2026-07-20"), ("CHI", "2026-07-20"), ("NY", "2026-07-22"))

LIFECYCLE_ASSERTIONS = (
    "every_signal_has_settlement_close_expiration",
    "every_position_settled_via_settle_weather_position",
    "no_position_remains_open",
    "held_open_until_truth_published",
    "fr04_every_emit_has_one_outcome",
)

# Known engine defect surfaced by this dry run (protected file, F3 may not
# patch it): ``_close_position`` books a YES-side exit price against a NO-side
# entry price at settlement. Tracked as an xfail so the day the engine is
# fixed this test starts asserting it for real.
KNOWN_PNL_SIGN_DEFECT = (
    "matching_engine._close_position prices NO-side settlements with the YES "
    "exit price (BUY NO that settles 'no' is booked as a loss); protected file"
)


def _has_archive() -> bool:
    return os.path.isdir(os.path.join(ROOT, "data", "ladders", "KXHIGHNY")) and os.path.exists(
        os.path.join(ROOT, "data", "forecast_archive", "forecast_series_gfs_mex.csv")
    )


pytestmark = pytest.mark.skipif(not _has_archive(), reason="ladder/forecast archive not on disk")


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory):
    """Run the dry run once on the first candidate day that opens a position."""
    out_dir = tmp_path_factory.mktemp("dry_run")
    last = None
    for city, date in CANDIDATE_DAYS:
        args = gdr.build_parser().parse_args(
            ["--city", city, "--date", date, "--quiet", "--out", str(out_dir / f"{city}_{date}.json")]
        )
        t0 = time.monotonic()
        report, code = gdr.run_dry_run(args)
        elapsed = time.monotonic() - t0
        assert elapsed < 60.0, f"dry run took {elapsed:.1f}s (> 60 s budget)"
        gdr.write_report(out_dir / f"{city}_{date}.json", report)
        last = (report, code, out_dir / f"{city}_{date}.json")
        if report["positions"]["opened"] > 0:
            return last
    pytest.fail(
        "none of the candidate city-days opened a position; the settlement "
        f"assertion would be vacuous. Last report: {json.dumps(last[0]['signals'])}"
    )


def test_a_real_position_was_opened_and_settled(dry_run):
    report, _code, _path = dry_run
    assert report["positions"]["opened"] >= 1
    assert report["positions"]["settled"] == report["positions"]["opened"]
    assert report["positions"]["open_at_end"] == 0
    settled = report["assertions"]["every_position_settled_via_settle_weather_position"]
    for pos in settled["detail"]:
        assert pos["reason"] == "EXPIRATION"
        assert pos["via_settle_weather_position"] is True
        assert pos["settlement_high"] == report["truth"]["high"]
        assert pos["settlement_outcome"] in ("yes", "no")


def test_lifecycle_assertions_hold(dry_run):
    report, _code, _path = dry_run
    failed = {k: v for k, v in report["assertions"].items() if k in LIFECYCLE_ASSERTIONS and not v["ok"]}
    assert not failed, json.dumps(failed, indent=1)[:2000]


def test_every_signal_expiration_is_tz_aware_settlement_close(dry_run):
    report, _code, _path = dry_run
    from src.core.weather_settlement import settlement_close_for

    detail = report["assertions"]["every_signal_has_settlement_close_expiration"]["detail"]
    assert detail, "the pinned day must emit at least one signal"
    for row in detail:
        exp = datetime.fromisoformat(row["expiration_time"])
        assert exp.tzinfo is not None and exp.utcoffset() is not None
        assert exp == settlement_close_for(row["symbol"])
        # Settlement-day close is local midnight after the event date (NY: 04:00Z in July).
        assert exp.astimezone(ZoneInfo("America/New_York")).hour == 0


def test_truth_was_published_offline_and_after_the_close(dry_run):
    report, _code, _path = dry_run
    # The IEM stub was consulted (the PENDING branch ran on the close candle)
    # and nothing reached the network.
    assert report["truth"]["iem_network_calls"] >= 1
    assert report["truth"]["agree"] is True
    assert report["assertions"]["held_open_until_truth_published"]["ok"]


def test_settlement_pnl_matches_contract_side(dry_run):
    report, _code, _path = dry_run
    a = report["assertions"]["settlement_pnl_matches_contract_side"]
    if not a["ok"]:
        pytest.xfail(KNOWN_PNL_SIGN_DEFECT + f" -- {json.dumps(a['detail'])[:400]}")
    assert a["ok"]


def test_exit_code_reflects_assertions(dry_run):
    report, code, _path = dry_run
    assert code == (gdr.EXIT_OK if report["ok"] else gdr.EXIT_ASSERTION)


def test_report_is_timestamp_free_and_reproducible(dry_run, tmp_path):
    report, _code, path = dry_run
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    loaded = json.loads(text)
    assert loaded["city"] == report["city"] and loaded["assertions"].keys() == report["assertions"].keys()
    for key in ("generated_at", "timestamp", "run_at", "wall_clock"):
        assert key not in loaded
    assert "\\" not in loaded["ladder_root"]  # host-independent path form
    # A second run of the same day is byte-identical.
    args = gdr.build_parser().parse_args(
        ["--city", report["city"], "--date", report["date"], "--quiet", "--out", str(tmp_path / "again.json")]
    )
    again, _ = gdr.run_dry_run(args)
    gdr.write_report(tmp_path / "again.json", again)
    assert (tmp_path / "again.json").read_text(encoding="utf-8") == text


def test_clock_patches_are_restored_after_the_run(dry_run):
    import src.bots.weather_bot as wb
    import src.core.matching_engine as me
    import src.core.risk_manager as rm
    import src.core.weather_settlement as ws
    from datetime import date as real_date
    import time as real_time

    assert wb.datetime is datetime and me.datetime is datetime and rm.datetime is datetime
    assert ws.datetime is datetime
    assert rm.date is real_date
    assert wb.time is real_time
    assert ws._provider is None  # reset_caches ran; no stub left behind
    assert "settlement_cache.json" in ws.SETTLEMENT_CACHE_PATH
    assert "dry_run_" not in ws.SETTLEMENT_CACHE_PATH


def test_fake_datetime_is_isinstance_compatible():
    clock = gdr.DryRunClock(datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc))
    fake = gdr._make_fake_datetime(clock)
    assert isinstance(datetime(2020, 1, 1), fake)
    assert fake.now(timezone.utc) == clock.now_utc
    assert fake.now(ZoneInfo("America/New_York")).hour == 12
    naive = fake.now()
    assert naive.tzinfo is None
    assert naive.astimezone().astimezone(timezone.utc) == clock.now_utc
    clock.advance(gdr.timedelta(hours=1))
    assert fake.now(timezone.utc).hour == 17
    assert gdr._make_fake_date(clock).today() == clock.now_utc.astimezone().date()


def test_cli_unavailable_day_exits_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "genome_dry_run.py"),
         "--city", "NY", "--date", "1999-01-01", "--out", str(tmp_path / "x.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == gdr.EXIT_UNAVAILABLE, proc.stderr[-800:]
    assert "UNAVAILABLE" in proc.stderr


def test_genome_spec_flag_reports_unavailable_when_module_missing(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    args = gdr.build_parser().parse_args(
        ["--city", "NY", "--date", "2026-07-20", "--quiet", "--genome-spec", str(spec),
         "--out", str(tmp_path / "g.json")]
    )
    try:
        import src.strategies.genome_strategy  # noqa: F401
        import src.factory.promoted  # noqa: F401
    except ImportError:
        with pytest.raises(gdr.DryRunError):
            gdr.run_dry_run(args)
        return
    # Module present (STRATEGY landed): an empty spec must be refused loudly, never run silently.
    with pytest.raises(Exception):
        gdr.run_dry_run(args)
