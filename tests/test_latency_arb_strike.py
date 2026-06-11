"""Tests for LatencyArbStrategy strike propagation (2026-06-10 fix).

A latency-arb signal must carry the REAL numeric strike on ``sig.strike`` so the
settlement path (matching_engine._close_position) can positively settle
ETH/SOL/DOGE/XRP positions instead of mis-settling.

The trap (and why the first implementation was wrong): for real production 15m
crypto tickers — ``KX{ASSET}15M-{YYMONDD}{HHMM}-{MM}`` — the symbol's LAST
hyphen-segment is the expiry MINUTE LABEL (00/15/30/45), NOT a strike. So
``_extract_strike("KXETH15M-26JUN101200-00")`` returns ``0.0``. Propagating that
to ``sig.strike`` makes ``pos['strike'] == 0`` and (because spot >= 0 is always
true) re-introduces the exact fictional auto-YES settlement bug this whole change
set out to kill. The real strike is the API floor_strike, delivered on
``market_data.extra['strike']`` (populated by crypto_15m_bot.py:116). The strategy
must read THAT, falling back to the symbol parse only for daily ``KX{ASSET}D``
markets whose suffix genuinely is the strike.
"""

from datetime import datetime, timedelta

from src.core.interfaces import MarketData
from src.strategies.latency_arb import LatencyArbStrategy


def _feed(strat, symbol, spot, ts, *, bid=0.4, ask=0.5, strike=None):
    """Push one tick through analyze() and return any signals produced."""
    extra = {"spot_price": spot}
    if strike is not None:
        extra["strike"] = strike
    md = MarketData(
        symbol=symbol,
        timestamp=ts,
        price=ask,
        volume=1000.0,
        bid=bid,
        ask=ask,
        extra=extra,
    )
    return strat.analyze(md)


def _build_buffer_and_trade(
    strat, symbol, base_spot, final_spot, *, bid=0.4, ask=0.5, strike=None
):
    """Seed the ring buffer with a move; return signals on the final tick."""
    t0 = datetime(2026, 6, 10, 12, 0, 0)
    for i in range(3):
        _feed(
            strat,
            symbol,
            base_spot,
            t0 + timedelta(seconds=i),
            bid=bid,
            ask=ask,
            strike=strike,
        )
    return _feed(
        strat,
        symbol,
        final_spot,
        t0 + timedelta(seconds=5),
        bid=bid,
        ask=ask,
        strike=strike,
    )


# --- The regression guard: the production ticker format that broke before ------


def test_extract_strike_returns_minute_label_for_15m_ticker():
    """Documents the trap: symbol-parse yields the MINUTE LABEL, not a strike."""
    # Real production format; last segment "00" is the expiry minute, not a strike.
    assert LatencyArbStrategy._extract_strike("KXETH15M-26JUN101200-00") == 0.0
    assert LatencyArbStrategy._extract_strike("KXSOL15M-26JUN101215-15") == 15.0


def test_yes_signal_uses_real_extra_strike_not_minute_label():
    """15m ETH up-move → BUY YES; sig.strike is the REAL strike (3000), NOT 0."""
    symbol = "KXETH15M-26JUN101200-00"  # minute-label suffix "00"
    assert LatencyArbStrategy._extract_strike(symbol) == 0.0  # the trap value
    strat = LatencyArbStrategy(move_threshold=0.003, min_edge_cents=0.04)

    # base 3000 → final 3300 (+10%); latest_spot(3300) > real strike(3000).
    signals = _build_buffer_and_trade(strat, symbol, 3000.0, 3300.0, strike=3000.0)

    assert len(signals) == 1, f"expected one YES signal, got {len(signals)}"
    sig = signals[0]
    assert sig.contract_side == "YES"
    assert isinstance(sig.strike, (int, float))
    assert (
        sig.strike == 3000.0
    ), "must carry the real extra['strike'], not the minute label"
    assert sig.strike > 0, "a 0 strike would re-introduce the auto-YES settlement bug"


def test_no_signal_uses_real_extra_strike():
    """15m ETH down-move → BUY NO; sig.strike is the REAL strike (3000).

    Also proves the NO branch can fire at all — impossible under the old bug,
    where strike==0 made ``latest_spot < strike`` always false.
    """
    symbol = "KXETH15M-26JUN101200-00"
    strat = LatencyArbStrategy(move_threshold=0.003, min_edge_cents=0.04)

    # base 3000 → final 2700 (-10%); latest_spot(2700) < real strike(3000).
    signals = _build_buffer_and_trade(strat, symbol, 3000.0, 2700.0, strike=3000.0)

    assert len(signals) == 1, f"expected one NO signal, got {len(signals)}"
    sig = signals[0]
    assert sig.contract_side == "NO"
    assert sig.strike == 3000.0


def test_low_strike_asset_xrp_via_extra():
    """Sub-1000 strike (XRP ~2) propagates from extra — proves no magnitude gate."""
    symbol = "KXXRP15M-26JUN101200-15"  # minute label "15"
    strat = LatencyArbStrategy(move_threshold=0.003, min_edge_cents=0.04)

    signals = _build_buffer_and_trade(
        strat, symbol, 2.0, 2.5, bid=0.35, ask=0.40, strike=2.0
    )

    assert len(signals) == 1, f"expected one signal, got {len(signals)}"
    sig = signals[0]
    assert sig.strike == 2.0


def test_daily_ticker_falls_back_to_symbol_parse():
    """Daily KX{ASSET}D markets (no extra strike) still parse the real strike from
    the suffix — preserves the original, correct behavior for that format."""
    symbol = "KXETHD-26JUN10H1200-T3000"  # suffix "T3000" genuinely IS the strike
    assert LatencyArbStrategy._extract_strike(symbol) == 3000.0
    strat = LatencyArbStrategy(move_threshold=0.003, min_edge_cents=0.04)

    # No 'strike' in extra → strategy must fall back to the symbol parse.
    signals = _build_buffer_and_trade(strat, symbol, 3000.0, 3300.0, strike=None)

    assert len(signals) == 1, f"expected one signal, got {len(signals)}"
    sig = signals[0]
    assert sig.strike == 3000.0
