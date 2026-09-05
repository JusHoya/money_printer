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

# Engine defect surfaced by this dry run on 2026-09-04 and FIXED in commit
# 724d93c: ``_close_position`` booked the YES-leg payoff against a NO-side entry
# at settlement. ``test_settlement_pnl_matches_contract_side`` now asserts the
# corrected sign for real (the former xfail).
FIXED_PNL_SIGN_DEFECT_COMMIT = "724d93c"


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
    """Hard assertion since commit 724d93c: a NO position that settles 'no' books +(1-entry)*qty."""
    report, _code, _path = dry_run
    a = report["assertions"]["settlement_pnl_matches_contract_side"]
    assert a["ok"], json.dumps(a["detail"])[:600]
    for pos in a["detail"]:
        if pos["contract_side"] == "NO":
            won = pos["settlement_outcome"] == "no"
            assert pos["booked_exit_price"] == (1.0 if won else 0.0)
            assert (pos["booked_pnl"] > 0) == won


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


# ---------------------------------------------------------------------------
# GenomeStrategy through the same harness (FR-F3.3 / F3 exit criteria)
# ---------------------------------------------------------------------------
GENOME_SPEC = os.path.join(ROOT, "configs", "factory", "promoted", "0c4b20502f2daf65.json")  # fr31a_taker, shadow


def _has_genome_spec() -> bool:
    return os.path.exists(GENOME_SPEC) and os.path.isdir(os.path.join(ROOT, "data", "calibration"))


def _run_genome(tmp_dir, mode: str):
    args = gdr.build_parser().parse_args(
        ["--city", "NY", "--date", "2026-07-20", "--quiet", "--genome-spec", GENOME_SPEC,
         "--genome-mode", mode, "--out", str(tmp_dir / f"genome_{mode}.json"),
         "--log-out", str(tmp_dir / f"genome_{mode}.log")]
    )
    t0 = time.monotonic()
    report, code = gdr.run_dry_run(args)
    assert time.monotonic() - t0 < 60.0
    gdr.write_report(tmp_dir / f"genome_{mode}.json", report)
    return report, code, tmp_dir / f"genome_{mode}.log"


@pytest.fixture(scope="module")
def genome_shadow(tmp_path_factory):
    if not _has_genome_spec():
        pytest.skip("promoted spec / calibration dir not on disk")
    return _run_genome(tmp_path_factory.mktemp("genome_shadow"), "shadow")


@pytest.fixture(scope="module")
def genome_paper(tmp_path_factory):
    if not _has_genome_spec():
        pytest.skip("promoted spec / calibration dir not on disk")
    return _run_genome(tmp_path_factory.mktemp("genome_paper"), "paper")


def _genome_lines(log_path):
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if "strategy=Genome " in l]
    emits = [l for l in lines if "[Signal] EMIT" in l]
    shadow = [l for l in lines if "reason=GENOME_SHADOW" in l]
    return lines, emits, shadow


def _symbol(line: str) -> str:
    return line.split("symbol=")[1].split()[0]


def test_genome_is_constructed_the_way_the_bot_does(genome_shadow):
    report, code, _ = genome_shadow
    g = report["genome"]
    assert g["requested"] and g["genome_id"] == "0c4b20502f2daf65"
    assert g["constructed_via"].startswith("GenomeStrategy(")
    assert g["strategy_name"] == "Genome 0c4b2050"
    assert g["bot_genome_shadow"] is True and g["paper_override_for_dry_run"] is False
    assert report["signals"]["by_strategy"].get("genome", 0) >= 1
    assert code == 0, json.dumps(report["assertions"])[:800]


def test_genome_shadow_emits_at_top_of_hour_with_one_reject_each(genome_shadow):
    report, _code, log_path = genome_shadow
    lines, emits, shadow = _genome_lines(log_path)
    assert emits, "the genome never emitted on NY 2026-07-20"
    assert len(shadow) == len(emits) == report["signals"]["by_strategy"]["genome"]
    for l in emits:
        ts = l.split(" | ")[0]
        assert ts.endswith(":00:00"), f"EMIT off the :00 UTC grid: {l}"
        assert " qty=20 " in l and " contract=NO " in l and " side=buy " in l
    assert sorted(_symbol(l) for l in emits) == sorted(_symbol(l) for l in shadow)
    assert not any("[Signal] EXECUTED strategy=Genome" in l for l in lines)
    assert report["signals"]["rejected_by_code"]["GENOME_SHADOW"] == len(emits)


def test_genome_shadow_never_reaches_process_signals(tmp_path, monkeypatch):
    if not _has_genome_spec():
        pytest.skip("promoted spec not on disk")
    from src.bots import weather_bot as wb

    original = wb.WeatherBot._process_signals

    def guarded(self, signals, strategy_name, risk_manager, dashboard):
        assert not str(strategy_name).startswith("Genome"), f"shadow signal reached _process_signals: {strategy_name}"
        return original(self, signals, strategy_name, risk_manager, dashboard)

    monkeypatch.setattr(wb.WeatherBot, "_process_signals", guarded)
    report, code, _ = _run_genome(tmp_path, "shadow")
    assert code == 0 and report["signals"]["by_strategy"].get("genome", 0) >= 1


def test_genome_limit_price_is_quote_plus_one_cent(genome_shadow):
    """EMIT price == ladder NO ask at that candle + adverse_fill (0.01), read from the archive, not the log."""
    report, _code, log_path = genome_shadow
    _lines, emits, _ = _genome_lines(log_path)
    ladders, _vintages = gdr.load_city_day(
        "NY", "2026-07-20", os.path.join(ROOT, "data", "ladders"), "gfs_mex", 240
    )
    from src.factory import features as feat

    checked = 0
    import pandas as pd

    ts_col = pd.to_datetime(ladders["ts_utc"], utc=True)
    for l in emits:
        symbol = _symbol(l)
        ts = pd.Timestamp(l.split(" | ")[0], tz="UTC")
        price = float(l.split("price=")[1].split()[0])
        rows = ladders[(ladders["market_ticker"] == symbol) & (ts_col == ts)]
        assert len(rows) == 1, (symbol, str(ts), len(rows))
        r = rows.iloc[0]
        quote = float(feat.quote(r["yes_bid"], r["yes_ask"], 1, 0))  # direction NO, taker
        assert price == pytest.approx(quote + 0.01, abs=1e-9), (symbol, ts, price, quote)
        checked += 1
    assert checked == len(emits) >= 1


def test_genome_paper_mode_flows_through_the_gauntlet(genome_paper):
    report, code, log_path = genome_paper
    assert report["genome"]["paper_override_for_dry_run"] is True
    assert report["genome"]["bot_genome_shadow"] is False
    lines, emits, shadow = _genome_lines(log_path)
    assert emits and not shadow
    outcomes = [
        l for l in lines
        if "[Signal] EXECUTED" in l or ("[Risk] REJECT" in l and "reason=GENOME_" not in l)
    ]
    assert sorted(_symbol(l) for l in emits) == sorted(_symbol(l) for l in outcomes)
    assert report["assertions"]["fr04_every_emit_has_one_outcome"]["ok"]
    assert report["assertions"]["settlement_pnl_matches_contract_side"]["ok"]
    assert code == 0


def test_cadence_checker_passes_on_the_shadow_log(genome_shadow):
    _report, _code, log_path = genome_shadow
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "check_maia_emit_cadence.py"),
         "--file", str(log_path), "--no-data-log", "--strategy", "Genome", "--json"],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-800:]
    verdict = json.loads(proc.stdout)
    assert verdict["verdict"] == "PASS"
    assert verdict["emit_multiple_outcomes"] == [] and verdict["emit_without_outcome"] == []
    assert verdict["strategy_skip_codes"].get("GENOME_ALREADY_TRADED", 0) > 0
