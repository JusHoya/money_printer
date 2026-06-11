"""Tests for the 2026-06-10 settlement-strike fix.

Covers SimulatedExchange._close_position's BINARY SETTLEMENT branch
(EXPIRATION / EARLY_SETTLEMENT) in src/core/matching_engine.py.

Truth anchor (real Kalshi finalized results, review_2026_06_09/
settlement_cache.json): 114 finalized 15m/weather markets = 105 'no' /
9 'yes' (7.9% YES). The pre-fix code parsed the ticker's last dash-segment
as the strike; for 15m crypto tickers that segment is the EXPIRY MINUTE
LABEL (00/15/30/45), not a strike, so essentially every ATM contract
auto-settled YES (the fictional-profit bug). The fix prefers the cached
pos["strike"] (real API floor_strike) and fails safe to NO when a 15m
crypto position is missing its strike, while preserving weather (T/B
prefix), hourly (KXBTCD), and PRECIP behavior exactly.

These tests construct a SimulatedExchange (state_file=None => persistence
disabled, fully isolated), call open_position, then drive _close_position
with a binary reason and assert exit_price / pnl sign.
"""

import pytest

from src.core.matching_engine import SimulatedExchange


def _fresh_exchange():
    """SimulatedExchange with persistence disabled (no disk reads/writes)."""
    return SimulatedExchange()  # state_file defaults to None


def _open_and_settle(
    symbol,
    strike,
    final_spot_price,
    side="buy",
    entry_price=0.50,
    quantity=10,
    reason="EXPIRATION",
):
    """Open a position then settle it via the binary path; return the pos."""
    ex = _fresh_exchange()
    ex.open_position(
        symbol,
        side,
        entry_price,
        quantity,
        strategy_name="test",
        strike=strike,
    )
    pos = ex.positions[0]
    ex._close_position(pos, final_spot_price, reason=reason)
    return pos


# ---------------------------------------------------------------------------
# 1. Headline regression: minute-label must NOT auto-win.
# ---------------------------------------------------------------------------
def test_btc_15m_minute_label_no_longer_auto_wins():
    """KXBTC15M-...-30 with a real cached strike of 77500: spot 77000 < strike
    => NO (0.00), NOT the legacy auto-YES off minute label "30".

    Verified real case: KXBTC15M-26JUN032130-30 actual result = 'no'.
    """
    pos = _open_and_settle(
        "KXBTC15M-26JUN032130-30", strike=77500.0, final_spot_price=77000.0
    )
    assert pos["exit_price"] == 0.00
    assert pos["pnl"] < 0  # bought YES at 0.50, settled 0.00 -> loss


def test_btc_15m_real_yes():
    """Genuine YES: spot 77000 >= real cached strike 76500 => 1.00."""
    pos = _open_and_settle(
        "KXBTC15M-26JUN032130-30", strike=76500.0, final_spot_price=77000.0
    )
    assert pos["exit_price"] == 1.00
    assert pos["pnl"] > 0  # bought YES at 0.50, settled 1.00 -> win


# ---------------------------------------------------------------------------
# 2-6. Sub-1000 real strikes settle correctly when cached (no >1000 heuristic).
# ---------------------------------------------------------------------------
def test_sol_strike_below_1000():
    """SOL real strike ~150: must use cached strike, no magnitude rejection."""
    pos_yes = _open_and_settle(
        "KXSOL15M-26JUN032130-15", strike=150.0, final_spot_price=151.0
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXSOL15M-26JUN032130-15", strike=150.0, final_spot_price=149.0
    )
    assert pos_no["exit_price"] == 0.00


def test_doge_strike_far_below_1000():
    """DOGE real strike ~0.40: must settle correctly from cached strike."""
    pos_yes = _open_and_settle(
        "KXDOGE15M-26JUN032130-45", strike=0.40, final_spot_price=0.45
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXDOGE15M-26JUN032130-45", strike=0.40, final_spot_price=0.35
    )
    assert pos_no["exit_price"] == 0.00


def test_xrp_strike_below_1000():
    """XRP real strike ~2.00: cached strike preferred over minute label."""
    pos_yes = _open_and_settle(
        "KXXRP15M-26JUN032130-00", strike=2.00, final_spot_price=2.10
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXXRP15M-26JUN032130-00", strike=2.00, final_spot_price=1.90
    )
    assert pos_no["exit_price"] == 0.00


def test_eth_strike():
    """ETH real strike ~3000: cached strike preferred over minute label."""
    pos_yes = _open_and_settle(
        "KXETH15M-26JUN032130-00", strike=3000.0, final_spot_price=3010.0
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXETH15M-26JUN032130-00", strike=3000.0, final_spot_price=2990.0
    )
    assert pos_no["exit_price"] == 0.00


# ---------------------------------------------------------------------------
# 7. Missing strike on a 15m crypto family => fail-safe NO + loud error log.
# ---------------------------------------------------------------------------
def test_missing_strike_15m_crypto_failsafe_no():
    """latency_arb reality: ETH/SOL/DOGE/XRP arrive with strike=None.

    The minute label "00" must NOT auto-settle YES. Fail-safe to NO and emit
    a loud logger.error tagged "2026-06-10 settlement fix". This is the exact
    fictional-profit case from prod (was pnl=+$29.50).
    """
    import logging

    # The project logger ("MoneyPrinter") has propagate=False (src/utils/
    # logger.py), so the stdlib caplog/root capture does not see it. Attach a
    # capturing handler directly to the named logger instead.
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    named_logger = logging.getLogger("MoneyPrinter")
    handler = _Capture()
    handler.setLevel(logging.ERROR)
    named_logger.addHandler(handler)
    try:
        pos = _open_and_settle(
            "KXETH15M-26APR261800-00", strike=None, final_spot_price=3000.0
        )
    finally:
        named_logger.removeHandler(handler)

    assert pos["exit_price"] == 0.00  # fail-safe NO, NOT 1.00 off minute label
    assert pos["pnl"] < 0
    assert any("2026-06-10 settlement fix" in msg for msg in captured)


# ---------------------------------------------------------------------------
# 8-9. Weather T/B prefix preserved (cached strike None, suffix IS the strike).
# ---------------------------------------------------------------------------
def test_weather_above_T_prefix_preserved():
    """KXHIGHNY-...-T86 (above-bucket): YES iff temp >= 86."""
    pos_yes = _open_and_settle(
        "KXHIGHNY-26JUN04-T86", strike=None, final_spot_price=87.0
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXHIGHNY-26JUN04-T86", strike=None, final_spot_price=85.0
    )
    assert pos_no["exit_price"] == 0.00


def test_weather_below_B_prefix_preserved():
    """KXHIGHNY-...-B86.5 (below-bucket): YES iff temp <= 86.5."""
    pos_yes = _open_and_settle(
        "KXHIGHNY-26JUN04-B86.5", strike=None, final_spot_price=84.0
    )
    assert pos_yes["exit_price"] == 1.00  # below bucket, temp <= 86.5

    pos_no = _open_and_settle(
        "KXHIGHNY-26JUN04-B86.5", strike=None, final_spot_price=88.0
    )
    assert pos_no["exit_price"] == 0.00


# ---------------------------------------------------------------------------
# 10. Hourly KXBTCD: suffix is the real strike, not a 15m family => preserved.
# ---------------------------------------------------------------------------
def test_hourly_kxbtcd_suffix_strike_preserved():
    """KXBTCD-...-T78499.99 (hourly): YES iff spot >= 78499.99."""
    pos_yes = _open_and_settle(
        "KXBTCD-26MAY0117-T78499.99", strike=None, final_spot_price=78600.0
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXBTCD-26MAY0117-T78499.99", strike=None, final_spot_price=78000.0
    )
    assert pos_no["exit_price"] == 0.00


# ---------------------------------------------------------------------------
# 11. PRECIP path unchanged (>0.50 => YES).
# ---------------------------------------------------------------------------
def test_precip_unchanged():
    """PRECIP markets settle off probability: > 0.50 => YES."""
    pos_yes = _open_and_settle(
        "KXPRECIPNY-26JUN04-1", strike=None, final_spot_price=0.60
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXPRECIPNY-26JUN04-1", strike=None, final_spot_price=0.40
    )
    assert pos_no["exit_price"] == 0.00


# ---------------------------------------------------------------------------
# 12. Unparseable suffix with no cached strike => fail-safe NO, no crash.
# ---------------------------------------------------------------------------
def test_unparseable_suffix_failsafe():
    """A weather-like ticker whose suffix has no digits and no cached strike
    must fail safe to NO without raising."""
    pos = _open_and_settle("KXHIGHNY-26JUN04-XYZ", strike=None, final_spot_price=85.0)
    assert pos["exit_price"] == 0.00


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
