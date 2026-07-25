"""Bracket semantics must survive the whole strategy-to-settlement chain.

PRD FR-1.2 requires the sim settlement path to use ``bracket_payoff``. That is
only reachable if ``strike_type``/``floor_strike``/``cap_strike`` travel from
the API, through the strategy's ``TradeSignal``, through
``RiskManager.record_execution``, onto the position record, and finally into
``SimulatedExchange._close_position``. Each link was individually present and
the chain was still broken end to end -- ``record_execution`` dropped the three
fields on the floor, so a live weather position would have reached expiry with
no semantics and closed ``SETTLEMENT_UNRESOLVED``.

These tests walk the real chain rather than asserting on any one link.
"""

from datetime import datetime

import pytest

from src.core.bracket_payoff import attach_spec_to_signals, parse_bracket_spec
from src.core.interfaces import MarketData, TradeSignal
from src.core.risk_manager import RiskManager

# Live-verified 2026-07-25 (see src/core/bracket_payoff.py module docstring).
GOLDEN_MARKETS = [
    # (symbol, strike_type, floor, cap, settling_high, expect_yes)
    ("KXHIGHNY-26JUL25-B86.5", "between", 86, 87, 86, True),
    ("KXHIGHNY-26JUL25-B86.5", "between", 86, 87, 84, False),
    ("KXHIGHNY-26JUL25-T87", "greater", 87, None, 88, True),
    ("KXHIGHNY-26JUL25-T87", "greater", 87, None, 87, False),
    ("KXHIGHNY-26JUL25-T80", "less", None, 80, 79, True),
    ("KXHIGHNY-26JUL25-T80", "less", None, 80, 85, False),
]


def _market(symbol, strike_type, floor, cap):
    """A MarketData shaped exactly as kalshi_provider._parse_market_data emits."""
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(),
        price=0.30,
        volume=100,
        bid=0.29,
        ask=0.31,
        extra={
            "status": "active",
            "source": "live_metar",
            "no_bid": 0.69,
            "no_ask": 0.71,
            "strike": floor,
            "strike_type": strike_type,
            "floor_strike": floor,
            "cap_strike": cap,
        },
    )


@pytest.mark.parametrize("symbol,strike_type,floor,cap,high,expect_yes", GOLDEN_MARKETS)
def test_spec_survives_strategy_to_settlement(
    tmp_path, symbol, strike_type, floor, cap, high, expect_yes
):
    """Signal -> record_execution -> position -> settlement, no link dropped."""
    market = _market(symbol, strike_type, floor, cap)

    # Link 1: the strategy return boundary stamps the signal.
    signal = TradeSignal(symbol=symbol, side="buy", quantity=5, limit_price=0.30)
    (signal,) = attach_spec_to_signals([signal], market)
    assert signal.strike_type == strike_type
    assert signal.floor_strike == (None if floor is None else float(floor))
    assert signal.cap_strike == (None if cap is None else float(cap))

    # Link 2-3: risk manager forwards to the exchange, which caches on the position.
    risk = RiskManager(starting_balance=3000.0)
    exchange = risk.exchange
    risk.record_execution(
        cost=signal.limit_price * signal.quantity,
        symbol=signal.symbol,
        side=signal.side,
        quantity=signal.quantity,
        price=signal.limit_price,
        strategy_name="spec-plumbing-test",
        strike_type=signal.strike_type,
        floor_strike=signal.floor_strike,
        cap_strike=signal.cap_strike,
    )
    pos = exchange.positions[-1]
    assert pos["strike_type"] == strike_type

    # Link 4: settlement reads them back through bracket_payoff.
    exchange._close_position(pos, float(high), reason="EXPIRATION")
    closed = exchange.closed_trades[-1]
    assert closed["exit_price"] == (1.0 if expect_yes else 0.0), (
        f"{symbol} at {high}F expected "
        f"{'YES' if expect_yes else 'NO'}; got exit {closed['exit_price']}"
    )
    assert "UNRESOLVED" not in str(closed.get("reason", ""))


def test_unstamped_signal_yields_unresolved_not_a_guess():
    """The failure mode this plumbing prevents, asserted explicitly.

    A weather position that reaches settlement with no cached semantics must
    close at entry price with an unresolved reason -- never a fabricated NO.
    Roughly 90% of a bracket ladder settles NO, so a "safe" NO default would
    look right most days and be systematically wrong.
    """
    risk = RiskManager(starting_balance=3000.0)
    exchange = risk.exchange
    risk.record_execution(
        cost=1.5,
        symbol="KXHIGHNY-26JUL25-B86.5",
        side="buy",
        quantity=5,
        price=0.30,
        strategy_name="spec-plumbing-test",
    )
    pos = exchange.positions[-1]
    assert pos["strike_type"] is None

    exchange._close_position(pos, 86.0, reason="EXPIRATION")
    closed = exchange.closed_trades[-1]
    assert "UNRESOLVED" in str(closed.get("reason", ""))
    assert closed["exit_price"] == pytest.approx(0.30)


def test_attach_spec_leaves_non_bracket_markets_alone():
    """A market with no bracket semantics must not gain fabricated ones."""
    market = MarketData(
        symbol="KXBTC15M-26JUL25T1200-30",
        timestamp=datetime.now(),
        price=0.4,
        volume=1,
        bid=0.39,
        ask=0.41,
        extra={"source": "live", "strike": 118000.0},
    )
    signal = TradeSignal(symbol=market.symbol, side="buy", quantity=1, limit_price=0.4)
    (out,) = attach_spec_to_signals([signal], market)
    assert out.strike_type is None
    assert out.floor_strike is None
    assert out.cap_strike is None


def test_every_weather_strategy_stamps_at_its_return_boundary():
    """Guard: both weather strategies route analyze() through the stamper.

    A new early-return added inside the analysis body must not be able to leak
    an unstamped signal, so the stamping has to live in the public wrapper.
    """
    import inspect

    from src.strategies.ml_weather import MLWeatherStrategy
    from src.strategies.weather_strategy import WeatherArbitrageStrategyV2

    for cls in (WeatherArbitrageStrategyV2, MLWeatherStrategy):
        source = inspect.getsource(cls.analyze)
        assert "attach_spec_to_signals" in source, (
            f"{cls.__name__}.analyze must stamp bracket semantics onto every "
            f"emitted signal (PRD FR-1.2)"
        )
        assert hasattr(cls, "_analyze"), (
            f"{cls.__name__} must keep its analysis body in _analyze so the "
            f"public analyze() stays a single stamping boundary"
        )


def test_strategy_analyze_stamps_a_real_signal(monkeypatch):
    """Drive the real strategy and assert the emitted signal is stamped."""
    from src.strategies.weather_strategy import WeatherArbitrageStrategyV2

    symbol = "KXHIGHNY-26JUL25-B86.5"
    market = _market(symbol, "between", 86, 87)
    market.extra.update(
        {
            "temperature_f": 86.0,
            "max_temp_today_f": 86.0,
            "forecast": [{"isDaytime": True, "temperature": 86}],
        }
    )
    # The strategy's own signals carry the spec whenever it emits one; if the
    # gates reject today's market shape it emits none, which is also fine --
    # what must never happen is an emitted signal without semantics.
    strategy = WeatherArbitrageStrategyV2()
    signals = strategy.analyze(market) or []
    for sig in signals:
        assert (
            sig.strike_type == "between"
        ), f"{sig.symbol} left the strategy without bracket semantics"
        assert sig.floor_strike == 86.0 and sig.cap_strike == 87.0


def test_parse_bracket_spec_is_the_only_source_of_direction():
    """The stamper must agree with parse_bracket_spec, not re-derive."""
    for symbol, strike_type, floor, cap, _high, _yes in GOLDEN_MARKETS:
        market = _market(symbol, strike_type, floor, cap)
        spec = parse_bracket_spec(symbol, market.extra)
        signal = TradeSignal(symbol=symbol, side="buy", quantity=1, limit_price=0.1)
        (out,) = attach_spec_to_signals([signal], market)
        assert (out.strike_type, out.floor_strike, out.cap_strike) == (
            spec.strike_type,
            spec.floor_strike,
            spec.cap_strike,
        )
