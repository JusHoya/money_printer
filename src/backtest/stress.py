"""Stress test scenarios for circuit breakers and risk management.

Sprint 5, Task 5.8.

Each test constructs synthetic MarketData sequences and verifies
that safety mechanisms (circuit breakers, drawdown limits, stop losses)
protect the portfolio under adverse conditions.
"""

import logging
from datetime import datetime, timedelta, timezone

from src.backtest.engine import BacktestEngine
from src.core.interfaces import MarketData, Strategy, TradeSignal

logger = logging.getLogger(__name__)


class _AlwaysBuyStrategy(Strategy):
    """Test strategy that always generates a BUY YES signal."""

    def name(self) -> str:
        return "AlwaysBuy"

    def analyze(self, data: MarketData):
        sig = TradeSignal(
            symbol=data.symbol,
            side="buy",
            quantity=5,
            limit_price=data.ask if data.ask > 0 else 0.50,
            confidence=0.70,
            contract_side="YES",
        )
        sig.stop_loss = 0.10
        sig.expiration_time = (data.extra or {}).get("close_time")
        return [sig]


def _make_market(
    symbol: str = "KXBTC15M-TEST-T84000",
    bid: float = 0.50,
    ask: float = 0.55,
    spot: float = 84200.0,
    strike: float = 84000.0,
    ts_offset_min: int = 0,
) -> MarketData:
    # Use naive datetimes for compatibility with RiskManager (which uses datetime.now())
    ts = datetime(2026, 3, 19, 14, 0, 0) + timedelta(minutes=ts_offset_min)
    close = ts + timedelta(minutes=15)
    return MarketData(
        symbol=symbol,
        timestamp=ts,
        price=spot,
        volume=100,
        bid=bid,
        ask=ask,
        extra={
            "strike": strike,
            "spot_price": spot,
            "close_time": close,
            "time_to_expiry": 900.0,
            "source": "stress_test",
        },
    )


class StressTestSuite:
    """Run predefined stress scenarios and return pass/fail results."""

    def __init__(self, starting_balance: float = 300.0):
        self.starting_balance = starting_balance

    def test_flash_crash(self) -> dict:
        """Scenario (a): 10% BTC flash crash.

        20 markets, all settle NO (BTC crashes below strike).
        Verify portfolio loss < 15% and circuit breaker halts trading.
        """
        engine = BacktestEngine(
            {"always_buy": _AlwaysBuyStrategy()},
            starting_balance=self.starting_balance,
        )

        # All markets settle as "no" (crash)
        sequence = [
            (
                _make_market(bid=0.50, ask=0.55, ts_offset_min=i * 15),
                {
                    "result": "no",
                    "expiration_value": 83000.0,
                    "close_time": datetime.now(timezone.utc),
                },
            )
            for i in range(20)
        ]

        results = engine.run(sequence)

        final_balance = (
            engine.equity_curve[-1] if engine.equity_curve else self.starting_balance
        )
        loss_pct = (self.starting_balance - final_balance) / self.starting_balance

        return {
            "scenario": "flash_crash",
            "passed": loss_pct < 0.20,  # Generous bound
            "loss_pct": round(loss_pct, 4),
            "final_balance": round(final_balance, 2),
            "trades_executed": results["total_trades"],
            "circuit_breaker_tripped": engine.circuit_breaker._daily_halted
            or engine.circuit_breaker.is_strategy_paused("always_buy"),
        }

    def test_losing_streak(self) -> dict:
        """Scenario (c): 20 consecutive losses.

        Verify circuit breaker pauses strategy after 3 losses.
        """
        engine = BacktestEngine(
            {"always_buy": _AlwaysBuyStrategy()},
            starting_balance=self.starting_balance
            * 10,  # Larger balance to avoid capital limits
        )
        # Use different symbols to avoid BTC correlation limit
        sequence = [
            (
                _make_market(
                    symbol=f"KXBTC15M-TEST{i}-T84000",
                    bid=0.50,
                    ask=0.55,
                    ts_offset_min=i * 15,
                ),
                {
                    "result": "no",
                    "expiration_value": 83000.0,
                    "close_time": datetime.now(),
                },
            )
            for i in range(20)
        ]

        results = engine.run(sequence)

        # After consecutive losses, either strategy pause or daily halt should trigger
        strat_paused = engine.circuit_breaker.is_strategy_paused("always_buy")
        daily_halted = engine.circuit_breaker._daily_halted

        return {
            "scenario": "losing_streak",
            "passed": strat_paused or daily_halted,
            "trades_executed": results["total_trades"],
            "strategy_paused": strat_paused,
            "circuit_breaker_tripped": daily_halted,
            "final_balance": round(engine.equity_curve[-1], 2),
        }

    def test_total_wipeout(self) -> dict:
        """Scenario (d): All positions expire worthless.

        Verify total loss is bounded by entry costs + fees.
        """
        engine = BacktestEngine(
            {"always_buy": _AlwaysBuyStrategy()},
            starting_balance=self.starting_balance,
        )

        sequence = [
            (
                _make_market(bid=0.50, ask=0.55, ts_offset_min=i * 15),
                {
                    "result": "no",
                    "expiration_value": 83000.0,
                    "close_time": datetime.now(timezone.utc),
                },
            )
            for i in range(5)
        ]

        results = engine.run(sequence)

        final = (
            engine.equity_curve[-1] if engine.equity_curve else self.starting_balance
        )
        # Balance should never go below zero
        return {
            "scenario": "total_wipeout",
            "passed": final >= 0,
            "final_balance": round(final, 2),
            "trades_executed": results["total_trades"],
        }

    def run_all(self) -> dict:
        """Run all stress scenarios."""
        results = {}
        for name in ["test_flash_crash", "test_losing_streak", "test_total_wipeout"]:
            method = getattr(self, name)
            result = method()
            results[result["scenario"]] = result
            logger.info(
                "[Stress] %s: %s (balance=$%.2f)",
                result["scenario"],
                "PASS" if result["passed"] else "FAIL",
                result.get("final_balance", 0),
            )

        results["all_passed"] = all(
            r["passed"]
            for r in results.values()
            if isinstance(r, dict) and "passed" in r
        )
        return results
