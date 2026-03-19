"""Tests for Sprint 5 — Backtesting, sandbox, stress tests, and metrics.

Covers: data loader reconstruction, engine replay, settlement logic,
metrics computation, report generation, stress scenarios, and sandbox
scorecard.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.core.interfaces import MarketData, Strategy, TradeSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sequence(n=20, yes_pct=0.6, seed=42):
    """Build a synthetic replay sequence of (MarketData, outcome) tuples."""
    rng = np.random.RandomState(seed)
    seq = []
    for i in range(n):
        result = "yes" if rng.rand() < yes_pct else "no"
        strike = 84000.0
        spot = strike + rng.randn() * 500
        # Use naive datetimes for compatibility with RiskManager
        ts = datetime(2026, 3, 19, 10, 0, 0) + timedelta(minutes=i * 15)
        md = MarketData(
            symbol=f"KXBTC15M-TEST-T{int(strike)}",
            timestamp=ts,
            price=spot,
            volume=100,
            bid=0.45,
            ask=0.55,
            extra={
                "strike": strike,
                "spot_price": spot,
                "close_time": ts + timedelta(minutes=15),
                "time_to_expiry": 900.0,
                "source": "test",
            },
        )
        outcome = {
            "result": result,
            "expiration_value": spot,
            "close_time": ts + timedelta(minutes=15),
        }
        seq.append((md, outcome))
    return seq


class _FixedConfidenceStrategy(Strategy):
    """Test strategy that always buys YES at a fixed confidence."""

    def __init__(self, confidence=0.70):
        self._confidence = confidence

    def name(self):
        return "FixedConfidence"

    def analyze(self, data: MarketData):
        if data.ask <= 0 or data.ask >= 1.0:
            return []
        sig = TradeSignal(
            symbol=data.symbol,
            side="buy",
            quantity=5,
            limit_price=data.ask,
            confidence=self._confidence,
            contract_side="YES",
        )
        sig.stop_loss = 0.10
        sig.expiration_time = (data.extra or {}).get("close_time")
        return [sig]


# ===========================================================================
# Metrics
# ===========================================================================


class TestMetrics:
    def test_win_rate_all_wins(self):
        from src.backtest.metrics import compute_win_rate

        trades = [{"pnl": 1.0}, {"pnl": 0.5}, {"pnl": 2.0}]
        assert compute_win_rate(trades) == 1.0

    def test_win_rate_mixed(self):
        from src.backtest.metrics import compute_win_rate

        trades = [{"pnl": 1.0}, {"pnl": -0.5}, {"pnl": 2.0}, {"pnl": -1.0}]
        assert compute_win_rate(trades) == 0.5

    def test_win_rate_empty(self):
        from src.backtest.metrics import compute_win_rate

        assert compute_win_rate([]) == 0.0

    def test_max_drawdown_known(self):
        from src.backtest.metrics import compute_max_drawdown

        # 100 → 120 → 90 → 110: max DD = (120-90)/120 = 25%
        dd, peak_i, trough_i = compute_max_drawdown([100, 120, 90, 110])
        assert abs(dd - 0.25) < 0.01
        assert peak_i == 1
        assert trough_i == 2

    def test_max_drawdown_monotonic_up(self):
        from src.backtest.metrics import compute_max_drawdown

        dd, _, _ = compute_max_drawdown([100, 110, 120, 130])
        assert dd == 0.0

    def test_sharpe_ratio_positive(self):
        from src.backtest.metrics import compute_sharpe_ratio

        returns = [0.01, 0.02, 0.01, 0.03, 0.02]
        assert compute_sharpe_ratio(returns) > 0

    def test_sharpe_ratio_zero_returns(self):
        from src.backtest.metrics import compute_sharpe_ratio

        assert compute_sharpe_ratio([0, 0, 0, 0]) == 0.0

    def test_brier_score_perfect(self):
        from src.backtest.metrics import compute_brier_score

        # Perfect predictions: predicted 1.0 when outcome=1, 0.0 when outcome=0
        preds = [(1.0, 1), (0.0, 0), (1.0, 1)]
        assert compute_brier_score(preds) == 0.0

    def test_brier_score_worst(self):
        from src.backtest.metrics import compute_brier_score

        preds = [(1.0, 0), (0.0, 1)]
        assert compute_brier_score(preds) == 1.0

    def test_profit_factor(self):
        from src.backtest.metrics import compute_profit_factor

        trades = [{"pnl": 10}, {"pnl": -5}, {"pnl": 8}]
        assert compute_profit_factor(trades) == 18.0 / 5.0

    def test_validate_criteria(self):
        from src.backtest.metrics import validate_criteria

        good = {
            "win_rate": 0.90,
            "per_strategy": {"s1": {"win_rate": 0.80}},
            "net_pnl": 50.0,
            "max_drawdown_pct": 0.05,
            "brier_score": 0.15,
        }
        result = validate_criteria(good)
        assert all(result.values())


# ===========================================================================
# Data Loader
# ===========================================================================


class TestBacktestDataLoader:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        import src.data.harvester as hmod

        # Write fake Parquet
        rng = np.random.RandomState(42)
        n = 50
        strikes = rng.choice([83000, 84000, 85000], n)
        spots = strikes + rng.randn(n) * 500
        times = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")

        df = pd.DataFrame(
            {
                "ticker": [f"KXBTC15M-26JAN01-T{s}" for s in strikes],
                "floor_strike": strikes.astype(float),
                "expiration_value": spots,
                "result": np.where(spots >= strikes, "yes", "no"),
                "previous_yes_bid_dollars": rng.uniform(0.3, 0.7, n)
                .round(4)
                .astype(str),
                "previous_yes_ask_dollars": rng.uniform(0.3, 0.7, n)
                .round(4)
                .astype(str),
                "last_price_dollars": rng.uniform(0.1, 0.9, n).round(4).astype(str),
                "volume_fp": rng.randint(10, 500, n).astype(str),
                "open_interest_fp": rng.randint(0, 200, n).astype(str),
                "open_time": times.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "close_time": (times + pd.Timedelta(minutes=15)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "status": "settled",
            }
        )

        btc_dir = tmp_path / "btc_15m"
        btc_dir.mkdir()
        df.to_parquet(btc_dir / "2026-03-19.parquet")

        monkeypatch.setattr(hmod, "_HISTORICAL_DIR", tmp_path)
        self.data_dir = tmp_path

    def test_load_raw_returns_dataframe(self):
        from src.backtest.data_loader import BacktestDataLoader

        loader = BacktestDataLoader(data_dir=str(self.data_dir))
        df = loader.load_raw("btc_15m")
        assert not df.empty
        assert "ticker" in df.columns

    def test_reconstruct_market_data(self):
        from src.backtest.data_loader import BacktestDataLoader

        loader = BacktestDataLoader(data_dir=str(self.data_dir))
        df = loader.load_raw("btc_15m")
        md, outcome = loader.reconstruct_market_data(df.iloc[0])

        assert isinstance(md, MarketData)
        assert md.bid > 0
        assert md.ask > 0
        assert md.extra["strike"] is not None
        assert outcome["result"] in ("yes", "no")

    def test_build_replay_sequence(self):
        from src.backtest.data_loader import BacktestDataLoader

        loader = BacktestDataLoader(data_dir=str(self.data_dir))
        seq = loader.build_replay_sequence("btc_15m")
        assert len(seq) > 0
        # Check chronological order
        times = [md.timestamp for md, _ in seq]
        assert times == sorted(times)

    def test_market_data_has_required_extra_fields(self):
        from src.backtest.data_loader import BacktestDataLoader

        loader = BacktestDataLoader(data_dir=str(self.data_dir))
        seq = loader.build_replay_sequence("btc_15m")
        md, _ = seq[0]
        assert "strike" in md.extra
        assert "spot_price" in md.extra
        assert "close_time" in md.extra
        assert "time_to_expiry" in md.extra

    def test_empty_market_type_returns_empty(self):
        from src.backtest.data_loader import BacktestDataLoader

        loader = BacktestDataLoader(data_dir=str(self.data_dir))
        seq = loader.build_replay_sequence("nonexistent")
        assert seq == []


# ===========================================================================
# Backtest Engine
# ===========================================================================


class TestBacktestEngine:
    def test_run_produces_results(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(20, yes_pct=0.6)
        results = engine.run(seq)

        assert "total_trades" in results
        assert "win_rate" in results
        assert "equity_curve" in results
        assert "criteria" in results
        assert results["total_trades"] > 0

    def test_settlement_yes_wins_pnl_positive(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        # All markets settle YES
        seq = _make_sequence(5, yes_pct=1.0)
        results = engine.run(seq)

        # Buying YES at 0.55 and it settles to 1.00 → profit per trade
        assert results["total_pnl"] > 0
        assert results["win_rate"] == 1.0

    def test_settlement_all_losses(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(5, yes_pct=0.0)  # All settle NO
        results = engine.run(seq)

        assert results["total_pnl"] < 0

    def test_fees_deducted(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(10, yes_pct=0.5)
        results = engine.run(seq)

        assert results["total_fees"] > 0

    def test_equity_curve_starts_at_balance(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=500.0,
        )
        seq = _make_sequence(5)
        engine.run(seq)

        assert engine.equity_curve[0] == 500.0

    def test_brier_score_computed(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(10)
        results = engine.run(seq)

        assert 0 <= results["brier_score"] <= 1

    def test_per_strategy_tracked(self):
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(10)
        results = engine.run(seq)

        assert "test" in results["per_strategy"]
        assert results["per_strategy"]["test"]["trades"] > 0

    def test_signals_rejected_counted(self):
        """Low confidence → EV gate rejects → signals_rejected > 0."""
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.51)},  # Barely above 50%
            starting_balance=300.0,
        )
        seq = _make_sequence(10)
        results = engine.run(seq)

        # At 0.51 confidence, 0.55 price: EV = 0.51 - 0.55 - fee < 0 → rejected
        assert results["signals_rejected"] > 0

    def test_walk_forward(self, tmp_path, monkeypatch):
        from src.backtest.data_loader import BacktestDataLoader
        from src.backtest.engine import BacktestEngine
        import src.data.harvester as hmod

        # Create fake data
        rng = np.random.RandomState(42)
        n = 100
        strikes = np.full(n, 84000.0)
        spots = strikes + rng.randn(n) * 500
        times = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")

        df = pd.DataFrame(
            {
                "ticker": [f"KXBTC15M-T{int(strikes[0])}" for _ in range(n)],
                "floor_strike": strikes,
                "expiration_value": spots,
                "result": np.where(spots >= strikes, "yes", "no"),
                "previous_yes_bid_dollars": "0.4500",
                "previous_yes_ask_dollars": "0.5500",
                "last_price_dollars": "0.5000",
                "volume_fp": "100",
                "open_interest_fp": "50",
                "open_time": times.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "close_time": (times + pd.Timedelta(minutes=15)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "status": "settled",
            }
        )

        d = tmp_path / "btc_15m"
        d.mkdir()
        df.to_parquet(d / "2026-03-19.parquet")
        monkeypatch.setattr(hmod, "_HISTORICAL_DIR", tmp_path)

        loader = BacktestDataLoader(data_dir=str(tmp_path))
        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        results = engine.run_walk_forward(loader, "btc_15m", n_folds=2)

        assert results["n_folds"] >= 1
        assert results["total_trades"] >= 0


# ===========================================================================
# Report Generator
# ===========================================================================


class TestBacktestReport:
    def test_html_generated(self, tmp_path):
        from src.backtest.engine import BacktestEngine
        from src.backtest.report import BacktestReportGenerator

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(15, yes_pct=0.7)
        results = engine.run(seq)

        gen = BacktestReportGenerator(output_dir=str(tmp_path))
        path = gen.generate(results, filename="test_report.html")

        assert path.exists()
        content = path.read_text()
        assert "Money Printer" in content
        assert "Win Rate" in content
        assert "Validation Criteria" in content

    def test_report_contains_criteria(self, tmp_path):
        from src.backtest.engine import BacktestEngine
        from src.backtest.report import BacktestReportGenerator

        engine = BacktestEngine(
            {"test": _FixedConfidenceStrategy(0.70)},
            starting_balance=300.0,
        )
        seq = _make_sequence(10)
        results = engine.run(seq)

        gen = BacktestReportGenerator(output_dir=str(tmp_path))
        path = gen.generate(results)
        content = path.read_text()

        assert "PASS" in content or "FAIL" in content


# ===========================================================================
# Stress Tests
# ===========================================================================


class TestStressSuite:
    def test_flash_crash(self):
        from src.backtest.stress import StressTestSuite

        suite = StressTestSuite(starting_balance=300.0)
        result = suite.test_flash_crash()
        assert "passed" in result
        assert result["final_balance"] >= 0

    def test_losing_streak(self):
        from src.backtest.stress import StressTestSuite

        suite = StressTestSuite(starting_balance=300.0)
        result = suite.test_losing_streak()
        assert "passed" in result
        # Either strategy is paused (3 consecutive losses) or daily halt triggers
        assert result["strategy_paused"] or result.get("circuit_breaker_tripped", False)

    def test_total_wipeout(self):
        from src.backtest.stress import StressTestSuite

        suite = StressTestSuite(starting_balance=300.0)
        result = suite.test_total_wipeout()
        assert result["passed"] is True
        assert result["final_balance"] >= 0

    def test_run_all(self):
        from src.backtest.stress import StressTestSuite

        suite = StressTestSuite(starting_balance=300.0)
        results = suite.run_all()
        assert "all_passed" in results


# ===========================================================================
# Sandbox Scorecard
# ===========================================================================


class TestSandboxScorecard:
    def test_initial_scorecard(self):
        from src.backtest.sandbox import SandboxRunner

        runner = SandboxRunner(starting_balance=300.0, min_trades_for_validation=10)
        sc = runner.get_scorecard()
        assert sc["trades"] == 0
        assert sc["ready_for_live"] is False
        assert sc["enough_trades"] is False

    def test_settlement_tracking(self):
        from src.backtest.sandbox import SandboxRunner

        runner = SandboxRunner(starting_balance=300.0, min_trades_for_validation=2)

        # Simulate two winning settlements
        for i in range(2):
            runner._on_settlement(
                {
                    "symbol": f"TEST-{i}",
                    "strategy_name": "TestStrat",
                    "pnl": 5.0,
                    "entry_price": 0.50,
                    "exit_price": 1.00,
                    "contract_side": "YES",
                    "reason": "EXPIRATION",
                }
            )

        sc = runner.get_scorecard()
        assert sc["trades"] == 2
        assert sc["wins"] == 2
        assert sc["win_rate"] == 1.0
        assert sc["enough_trades"] is True

    def test_scorecard_not_ready_on_losses(self):
        from src.backtest.sandbox import SandboxRunner

        runner = SandboxRunner(starting_balance=300.0, min_trades_for_validation=3)

        # 2 wins, 1 loss
        runner._on_settlement({"symbol": "T1", "strategy_name": "S", "pnl": 5.0})
        runner._on_settlement({"symbol": "T2", "strategy_name": "S", "pnl": -3.0})
        runner._on_settlement({"symbol": "T3", "strategy_name": "S", "pnl": 5.0})

        sc = runner.get_scorecard()
        assert sc["win_rate"] < 0.85  # 66% < 85%
        assert sc["ready_for_live"] is False
