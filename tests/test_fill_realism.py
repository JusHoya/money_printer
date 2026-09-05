"""Tests for the configurable probabilistic fill model (Task E, 2026-06-03).

The exchange has an opt-in ``realistic_fills`` flag (DEFAULT OFF). When off,
every order fills exactly as before (byte-identical legacy behaviour). When on,
penny-floor orders ($0.01-$0.05) fill with probability < 1, scaled by a
queue-position / adverse-selection penalty so the cheapest contracts fill
least often.
"""

import pytest

from src.core.matching_engine import SimulatedExchange


# ----------------------------------------------------------------------
# Default OFF must reproduce current behaviour exactly
# ----------------------------------------------------------------------


def test_default_is_off():
    ex = SimulatedExchange()
    assert ex.realistic_fills is False


def test_off_always_fills_penny_floor():
    """With the model off, even a $0.01 order always opens a position."""
    ex = SimulatedExchange()
    for i in range(200):
        ex.open_position(
            f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="ML BTC 15m"
        )
    assert len(ex.positions) == 200
    assert ex.penny_floor_requested == 0
    assert ex.penny_floor_skipped == 0


def test_off_fill_probability_is_one_everywhere():
    ex = SimulatedExchange()  # off
    for price in (0.01, 0.02, 0.05, 0.06, 0.50, 0.99):
        assert ex.penny_floor_fill_probability(price, "buy") == 1.0


def test_off_path_byte_identical_to_legacy_open():
    """Position dict + scalar counters identical whether flag is unset or False."""
    ex_default = SimulatedExchange()
    ex_explicit = SimulatedExchange(realistic_fills=False)
    for ex in (ex_default, ex_explicit):
        ex.open_position(
            "KXBTC15M-26JAN010000-45", "buy", 0.01, 10, strategy_name="ML BTC 15m"
        )
    p1 = ex_default.positions[0]
    p2 = ex_explicit.positions[0]
    # Compare the load-bearing fields (ids/timestamps differ by construction).
    for key in ("entry_price", "quantity", "entry_fee", "is_maker", "contract_side"):
        assert p1[key] == p2[key]
    assert ex_default.realized_pnl == ex_explicit.realized_pnl
    assert ex_default.cumulative_entry_fees == ex_explicit.cumulative_entry_fees


# ----------------------------------------------------------------------
# Realistic ON reduces penny-floor fill rate
# ----------------------------------------------------------------------


def test_on_reduces_penny_floor_fill_rate():
    """With the model on, far fewer $0.01 orders fill than requested."""
    n = 2000
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.5, fill_rng_seed=7)
    for i in range(n):
        ex.open_position(
            f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="ML BTC 15m"
        )
    filled = len(ex.positions)
    # p_fill at $0.01 = 0.5 (base) * 0.5 (max adverse-selection penalty) = 0.25
    assert filled < n  # strictly fewer than requested
    assert ex.penny_floor_requested == n
    assert ex.penny_floor_skipped == n - filled
    # Observed fill rate near the modelled 0.25 (loose band for RNG noise).
    assert 0.18 < (filled / n) < 0.32


def test_on_off_diverge_for_penny_floor():
    """Same workload: ON fills strictly fewer than OFF."""
    n = 1000
    off = SimulatedExchange()
    on = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.5, fill_rng_seed=1)
    for i in range(n):
        sym = f"KXBTC15M-X-{i}-45"
        off.open_position(sym, "buy", 0.02, 10, strategy_name="S")
        on.open_position(sym, "buy", 0.02, 10, strategy_name="S")
    assert len(off.positions) == n
    assert len(on.positions) < n


def test_on_does_not_touch_non_penny_orders():
    """Orders priced above the penny band always fill, even with the model on."""
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.25, fill_rng_seed=3)
    for i in range(300):
        ex.open_position(f"KXBTC15M-X-{i}-45", "buy", 0.40, 10, strategy_name="S")
    assert len(ex.positions) == 300  # none skipped
    assert ex.penny_floor_requested == 0
    assert ex.penny_floor_skipped == 0


def test_adverse_selection_cheaper_fills_less():
    """The penalty makes $0.01 fill less often than $0.05."""
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.6)
    p_low = ex.penny_floor_fill_probability(0.01, "buy")
    p_high = ex.penny_floor_fill_probability(0.05, "buy")
    assert p_low < p_high
    assert p_low == pytest.approx(0.6 * 0.5, abs=1e-9)  # bottom of band: half base
    assert p_high == pytest.approx(0.6 * 1.0, abs=1e-9)  # top of band: full base


def test_fill_probability_clamped_to_band():
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.5)
    assert ex.penny_floor_fill_probability(0.009, "buy") == 1.0  # below band
    assert ex.penny_floor_fill_probability(0.051, "buy") == 1.0  # above band
    assert ex.penny_floor_fill_probability(0.50, "buy") == 1.0


def test_reproducible_with_seed():
    """Same seed -> same number of fills."""

    def run():
        ex = SimulatedExchange(
            realistic_fills=True, penny_fill_prob=0.5, fill_rng_seed=99
        )
        for i in range(500):
            ex.open_position(f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="S")
        return len(ex.positions)

    assert run() == run()


def test_p_fill_zero_skips_all_penny_floor():
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.0, fill_rng_seed=5)
    for i in range(100):
        ex.open_position(f"KXBTC15M-X-{i}-45", "buy", 0.01, 10, strategy_name="S")
    # 0.0 base * any penalty = 0.0 -> nothing fills.
    assert len(ex.positions) == 0
    assert ex.penny_floor_skipped == 100


def test_skipped_order_charges_no_fee():
    """A no-fill order must not deduct a fee or bump the cumulative ledger."""
    ex = SimulatedExchange(realistic_fills=True, penny_fill_prob=0.0, fill_rng_seed=5)
    ex.open_position("KXBTC15M-X-0-45", "buy", 0.01, 10, strategy_name="S")
    assert len(ex.positions) == 0
    assert ex.realized_pnl == 0.0
    assert ex.total_fees_paid == 0.0
    assert ex.cumulative_entry_fees == 0.0
    assert ex.cumulative_fees_paid == 0.0


# ======================================================================
# scripts/measure_fill_realism.py -- intra-cadence ask drift on the maia
# tape (F3 fill-realism study, PRD_STRATEGY_FACTORY FR-F3.4)
# ======================================================================
import csv  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "mp_fill_realism", _REPO_ROOT / "scripts" / "measure_fill_realism.py"
)
mfr = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mfr)


def _row(ts, symbol, ask, no_ask, typ="MARKET_DATA"):
    return {
        "Timestamp": ts, "Symbol": symbol, "Price": "", "Type": typ, "Status": "REAL",
        "Bid": "", "Ask": ask, "NoBid": "", "NoAsk": no_ask, "Last": "", "Volume": "",
        "Depth": "", "StrikeType": "between", "FloorStrike": "84", "CapStrike": "85",
    }


A = "KXHIGHNY-26SEP05-B84.5 (Market)"
B = "KXHIGHCHI-26SEP05-B80.5 (Market)"
C = "KXHIGHLAX-26SEP05-B92.5 (Market)"


def _synthetic_tape():
    """Three weather markets across the 02:00Z decision point (14-s cadence).

    A: decision poll 02:00:04 (YES ask 0.40, NO ask 0.61); follow-ups at +14 s
       (0.42 / 0.61), +28 s (0.41 / 0.61), +42 s (0.40 / 0.63).
       -> 20 s window: YES drift 0.02, NO drift 0.00; 60 s: YES 0.02, NO 0.02
    B: decision poll 02:00:05 with a BLANK YES ask (missing quote), NO ask 0.50;
       follow-up +14 s NO ask 0.49 (price improvement -> clipped to 0).
    C: first poll after 02:00 is at 02:01:30 (lag 90 s > 60) -> tape gap, no decision.
    Also: a temperature row and a mention market, both ignored.
    """
    rows = [
        _row("2026-09-05T01:59:50.1", A, "0.39", "0.62"),
        _row("2026-09-05T02:00:04.0", A, "0.40", "0.61"),
        _row("2026-09-05T02:00:18.0", A, "0.42", "0.61"),
        _row("2026-09-05T02:00:32.0", A, "0.41", "0.61"),
        _row("2026-09-05T02:00:46.0", A, "0.40", "0.63"),
        _row("2026-09-05T01:59:51.0", B, "0.30", "0.71"),
        _row("2026-09-05T02:00:05.0", B, "", "0.50"),
        _row("2026-09-05T02:00:19.0", B, "0.31", "0.49"),
        _row("2026-09-05T01:59:52.0", C, "0.10", "0.91"),
        _row("2026-09-05T02:01:30.0", C, "0.10", "0.91"),
        _row("2026-09-05T02:01:44.0", C, "0.15", "0.91"),
        _row("2026-09-05T02:00:03.0", "KXHIGHLAX (F)", "", ""),
        _row("2026-09-05T02:00:03.0", "KXTRUMPMENTION-26SEP10-FAKE (Market)", "0.93", "0.07"),
        _row("2026-09-05T02:00:04.0", A, "0.40", "0.61", typ="DEPTH"),
    ]
    return rows


def test_market_ticker_strips_the_market_suffix_only():
    assert mfr.market_ticker("KXHIGHNY-26SEP05-B84.5 (Market)") == "KXHIGHNY-26SEP05-B84.5"
    assert mfr.market_ticker("KXHIGHLAX (F)") is None
    assert mfr.market_ticker("") is None


def test_percentile_nearest_rank_and_ceil_to_cent():
    vals = [0.0, 0.0, 0.0, 0.01, 0.02, 0.02, 0.03, 0.05, 0.10, 0.12]
    assert mfr.percentile_nearest_rank(vals, 0.5) == 0.02  # ceil(5)-1 = index 4
    assert mfr.percentile_nearest_rank(vals, 0.9) == 0.10  # ceil(9)-1 = index 8
    assert mfr.percentile_nearest_rank(vals, 0.95) == 0.12  # ceil(9.5)-1 = index 9
    assert mfr.percentile_nearest_rank([], 0.9) is None
    assert mfr.ceil_to_cent(0.0101) == 0.02
    assert mfr.ceil_to_cent(0.02) == 0.02
    assert mfr.ceil_to_cent(0.02 + 1e-12) == 0.02
    assert mfr.ceil_to_cent(0.0) == 0.0


def test_analyse_synthetic_tape_drift_gaps_and_missing_quotes():
    rep = mfr.analyse(_synthetic_tape(), windows=(20, 60), max_decision_lag=60)
    assert rep["tape"]["markets"] == 3
    assert rep["tape"]["market_rows_in_series"] == 11
    assert rep["tape"]["hours_utc"] == ["2026-09-05T02:00:00+00:00"]
    c = rep["counts"]
    assert c["market_hours"] == 3
    assert c["decision_polls"] == 2  # A and B; C is a gap
    assert c["gap_no_decision_poll"] == 1
    assert c["missing_quote_yes"] == 1  # B
    assert c.get("gap_no_followup_60s_no", 0) == 0
    d20 = rep["adverse_drift"]["20s"]
    assert d20["yes_ask"]["n"] == 1 and d20["yes_ask"]["max"] == pytest.approx(0.02)
    assert d20["no_ask"]["n"] == 2 and d20["no_ask"]["max"] == pytest.approx(0.0)
    assert sorted(round(x, 4) for x in [d20["both_sides"]["p50"], d20["both_sides"]["max"]]) == [0.0, 0.02]
    d60 = rep["adverse_drift"]["60s"]
    assert d60["yes_ask"]["max"] == pytest.approx(0.02)
    assert d60["no_ask"]["max"] == pytest.approx(0.02)  # A's NO ask reached 0.63 at +42 s
    # nearest-rank p90 of the 20 s both-sides sample [0.0, 0.0, 0.02] = index ceil(2.7)-1 = 2 -> 0.02
    assert rep["p90_primary_window"] == pytest.approx(0.02)
    # next poll: A YES 0.42-0.40 = 0.02, A NO 0.0, B NO clipped 0.0 -> p90 0.02; gaps 14 s
    npd = rep["next_poll_adverse_drift"]
    assert npd["both_sides"]["n"] == 3 and npd["both_sides"]["p90"] == pytest.approx(0.02)
    assert npd["gap_s"]["max"] == pytest.approx(14.0)
    assert rep["p90_next_poll"] == pytest.approx(0.02)
    assert rep["p90_primary"] == pytest.approx(0.02)
    assert rep["p90_basis"] in ("20s window", "next poll")
    assert rep["p90_exceeds_1c"] is True
    assert rep["recommended_adverse_fill"] == pytest.approx(0.02)
    assert "EXCEEDS 1c" in rep["statement"] and "re-scored" in rep["statement"]
    assert rep["decision_lag_s"]["max"] == pytest.approx(5.0)
    assert rep["signed_next_poll"]["no_ask"]["n"] == 2  # A (0.0) and B (-0.01, price improvement)
    assert rep["examples_gt_1c"][0]["market"] == "KXHIGHNY-26SEP05-B84.5"
    assert rep["per_city"]["KXHIGHNY"]["decision_polls"] == 1


def test_next_poll_is_the_conservative_basis_when_the_window_is_empty():
    """At a ~40 s per-market cadence the 20 s window has no follow-up; the next poll still does."""
    rows = [
        _row("2026-09-05T01:59:40.0", A, "0.40", "0.61"),
        _row("2026-09-05T02:00:04.0", A, "0.40", "0.61"),
        _row("2026-09-05T02:00:41.0", A, "0.43", "0.61"),  # +37 s: outside 20 s, inside 60 s
    ]
    rep = mfr.analyse(rows, windows=(20, 60))
    assert rep["counts"]["gap_no_followup_20s_yes"] == 1
    assert rep["adverse_drift"]["20s"]["both_sides"]["n"] == 0
    assert rep["adverse_drift"]["60s"]["yes_ask"]["max"] == pytest.approx(0.03)
    assert rep["next_poll_adverse_drift"]["yes_ask"]["max"] == pytest.approx(0.03)
    assert rep["next_poll_adverse_drift"]["gap_s"]["max"] == pytest.approx(37.0)
    assert rep["p90_primary_window"] is None and rep["p90_next_poll"] == pytest.approx(0.03)
    assert rep["p90_basis"] == "next poll" and rep["recommended_adverse_fill"] == pytest.approx(0.03)


def test_analyse_quiet_tape_recommends_the_1c_floor():
    rows = [
        _row("2026-09-05T01:59:48.0", A, "0.40", "0.61"),  # the boundary must be covered
        _row("2026-09-05T02:00:02.0", A, "0.40", "0.61"),
        _row("2026-09-05T02:00:16.0", A, "0.40", "0.61"),
        _row("2026-09-05T02:00:30.0", A, "0.39", "0.60"),
    ]
    rep = mfr.analyse(rows, windows=(20, 60))
    assert rep["p90_primary"] == 0.0
    assert rep["p90_exceeds_1c"] is False
    assert rep["recommended_adverse_fill"] == 0.01
    assert "does not exceed 1c" in rep["statement"]


def test_analyse_empty_or_boundary_free_tape_has_no_recommendation():
    rep = mfr.analyse([])
    assert rep["recommended_adverse_fill"] is None and rep["p90_exceeds_1c"] is None
    rows = [_row("2026-09-05T02:10:02.0", A, "0.40", "0.61"), _row("2026-09-05T02:10:16.0", A, "0.40", "0.61")]
    rep = mfr.analyse(rows)
    assert rep["counts"] == {} and rep["recommended_adverse_fill"] is None


def test_collect_dedupes_the_100_row_window(tmp_path):
    """/api/logs/data returns the last 100 rows; repeated polls must not duplicate."""
    batches = [
        [_row("2026-09-05T02:00:04.0", A, "0.40", "0.61"), _row("2026-09-05T02:00:04.0", B, "0.30", "0.71")],
        [_row("2026-09-05T02:00:04.0", B, "0.30", "0.71"), _row("2026-09-05T02:00:18.0", A, "0.42", "0.61")],
    ]
    calls = {"n": 0}

    def fetch(url):
        i = min(calls["n"], len(batches) - 1)
        calls["n"] += 1
        return batches[i]

    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(s):
        clock["t"] += s

    cache = tmp_path / "tape.csv"
    stats = mfr.collect("http://x/api/logs/data", str(cache), seconds=3.0, poll_interval=3.0,
                        _fetch=fetch, _sleep=sleep, _now=now)
    assert stats["rows_added"] == 3 and stats["errors"] == 0 and stats["polls"] == 2
    with open(cache, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3 and rows[0]["Symbol"] == A
    # a second collection run appends only unseen rows
    stats = mfr.collect("http://x/api/logs/data", str(cache), seconds=0.0, _fetch=lambda u: batches[1],
                        _sleep=sleep, _now=now)
    assert stats["rows_added"] == 0
    with open(cache, "r", encoding="utf-8", newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 3


def test_cli_writes_dated_report(tmp_path):
    tape = tmp_path / "tape.csv"
    with open(tape, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=mfr.DATA_CSV_HEADER)
        w.writeheader()
        for r in _synthetic_tape():
            w.writerow(r)
    out = tmp_path / "reports"
    rc = mfr.main(["--csv", str(tape), "--out-dir", str(out), "--date", "2026-09-05"])
    assert rc == 0
    rep = json.loads((out / "fill_realism_2026-09-05.json").read_text("utf-8"))
    assert rep["recommended_adverse_fill"] == 0.02 and rep["p90_exceeds_1c"] is True
    md = (out / "fill_realism_2026-09-05.md").read_text("utf-8")
    assert "EXCEEDS 1c" in md and "| 20s | both_sides |" in md
