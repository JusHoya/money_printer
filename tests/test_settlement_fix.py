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
    **bracket,
):
    """Open a position then settle it via the binary path; return the pos.

    ``**bracket`` forwards the PRD FR-1.1 API fields (``strike_type``,
    ``floor_strike``, ``cap_strike``) that weather settlement now requires.
    """
    ex = _fresh_exchange()
    ex.open_position(
        symbol,
        side,
        entry_price,
        quantity,
        strategy_name="test",
        strike=strike,
        **bracket,
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
# 8-9. Weather now settles through the FR-1.2 payoff module.
#
# REWRITTEN 2026-07-25 (PRD FR-1.1/FR-1.2). These two tests previously asserted
# the suffix-letter semantics: "T => above-bucket, YES iff temp >= 86" and
# "B => below-bucket, YES iff temp <= 86.5". The 2026-07-24 review probed the
# live API and found both are wrong -- KXHIGHNY-26JUL25-B86.5 is
# "86 to 87" (a two-sided `between` bracket, not a below-bucket) and T-tickers
# are `greater` OR `less` depending on the API's strike_type. The behaviour
# these asserted was the defect, so the assertions move to the verified
# semantics; the full golden table lives in tests/test_bracket_payoff.py and
# tests/test_weather_settlement_semantics.py.
# ---------------------------------------------------------------------------
def test_weather_greater_settles_from_api_strike_type():
    """KXHIGHNY-...-T86, strike_type='greater', floor=86 => YES iff temp >= 87."""
    pos_yes = _open_and_settle(
        "KXHIGHNY-26JUN04-T86",
        strike=None,
        final_spot_price=87.0,
        strike_type="greater",
        floor_strike=86,
    )
    assert pos_yes["exit_price"] == 1.00

    pos_no = _open_and_settle(
        "KXHIGHNY-26JUN04-T86",
        strike=None,
        final_spot_price=85.0,
        strike_type="greater",
        floor_strike=86,
    )
    assert pos_no["exit_price"] == 0.00

    # The off-by-one that the suffix parser got wrong: `greater` floor=86 is
    # "87 or above", so 86 itself settles NO.
    pos_boundary = _open_and_settle(
        "KXHIGHNY-26JUN04-T86",
        strike=None,
        final_spot_price=86.0,
        strike_type="greater",
        floor_strike=86,
    )
    assert pos_boundary["exit_price"] == 0.00


def test_weather_between_bracket_settles_on_both_bounds():
    """KXHIGHNY-...-B86.5 is `between` floor=86 cap=87 -- "86 to 87", not "<= 86.5"."""
    pos_inside = _open_and_settle(
        "KXHIGHNY-26JUN04-B86.5",
        strike=None,
        final_spot_price=86.0,
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
    )
    assert pos_inside["exit_price"] == 1.00

    # 84F is BELOW the band. The old below-bucket reading paid YES here.
    pos_below = _open_and_settle(
        "KXHIGHNY-26JUN04-B86.5",
        strike=None,
        final_spot_price=84.0,
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
    )
    assert pos_below["exit_price"] == 0.00

    pos_above = _open_and_settle(
        "KXHIGHNY-26JUN04-B86.5",
        strike=None,
        final_spot_price=88.0,
        strike_type="between",
        floor_strike=86,
        cap_strike=87,
    )
    assert pos_above["exit_price"] == 0.00


def test_weather_less_bracket_is_not_inverted():
    """KXHIGHNY-...-T80 is `less` cap=80 -- "79 or below", the case the suffix
    parser evaluated exactly backwards."""
    pos_cold = _open_and_settle(
        "KXHIGHNY-26JUN04-T80",
        strike=None,
        final_spot_price=75.0,
        strike_type="less",
        cap_strike=80,
    )
    assert pos_cold["exit_price"] == 1.00

    pos_hot = _open_and_settle(
        "KXHIGHNY-26JUN04-T80",
        strike=None,
        final_spot_price=85.0,
        strike_type="less",
        cap_strike=80,
    )
    assert pos_hot["exit_price"] == 0.00


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
# 12. Weather with no bracket spec => SETTLEMENT_UNRESOLVED, no crash.
#
# REWRITTEN 2026-07-25 (PRD FR-1.2). This test previously asserted that an
# unparseable weather ticker fail-safed to NO (exit 0.00). Under FR-1.2 that is
# no longer the safe default: ~90% of a weather ladder settles NO, so a
# fabricated NO looks plausible and silently books a full loss on the one
# bracket that actually paid. Missing semantics now closes FLAT at the entry
# price with reason SETTLEMENT_UNRESOLVED (abort-on-missing-critical-input).
# The crypto fail-safe-to-NO behaviour above is unchanged.
# ---------------------------------------------------------------------------
def test_weather_without_bracket_spec_closes_unresolved():
    """A weather ticker with no cached strike_type must not invent an outcome."""
    pos = _open_and_settle("KXHIGHNY-26JUN04-XYZ", strike=None, final_spot_price=85.0)
    assert pos["exit_price"] == 0.50  # entry price -- a flat, zero-PnL close
    assert pos["reason"] == "SETTLEMENT_UNRESOLVED"
    assert "settlement_outcome" not in pos


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
