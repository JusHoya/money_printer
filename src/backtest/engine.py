"""Backtesting engine — replays historical data through the trading pipeline.

Sprint 5, Task 5.1.

Each settled market is treated as a single decision point:
1. Present reconstructed MarketData to strategies
2. Collect any TradeSignal emitted
3. Process through risk management (Kelly sizing, EV gate, checks)
4. If executed, immediately settle using the known outcome
5. Record prediction, outcome, PnL, and fees
"""

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from src.backtest import metrics as bmetrics
from src.backtest.data_loader import BacktestDataLoader
from src.core.circuit_breaker import CircuitBreaker
from src.core.fee_calculator import compute_fee
from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.core.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class BacktestEngine:
    """Replay historical settlement data through the strategy pipeline.

    Parameters
    ----------
    strategies : dict[str, Strategy]
        Named strategies to evaluate (waterfall order).
    starting_balance : float
        Initial capital.
    fee_mode : str
        ``"maker"`` (default) or ``"taker"``.
    """

    def __init__(
        self,
        strategies: Dict[str, Strategy],
        starting_balance: float = 300.0,
        fee_mode: str = "maker",
    ):
        self.strategies = strategies
        self.starting_balance = starting_balance
        self.fee_mode = fee_mode

        # Initialised per run
        self.risk_manager: Optional[RiskManager] = None
        self.circuit_breaker: Optional[CircuitBreaker] = None

        # Results
        self.trades: List[dict] = []
        self.equity_curve: List[float] = []
        self.predictions: List[Tuple[float, int]] = []
        self.signals_generated = 0
        self.signals_rejected = 0
        self._per_strategy: Dict[str, list] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        replay_sequence: List[Tuple[MarketData, dict]],
        strategy_order: List[str] = None,
    ) -> dict:
        """Replay a sequence and return results.

        Parameters
        ----------
        replay_sequence : list of (MarketData, outcome_dict)
            Chronologically sorted settlement events.
        strategy_order : list of str, optional
            Waterfall order of strategy names (keys in self.strategies).
            Defaults to all strategies in insertion order.
        """
        # Fresh state
        self.risk_manager = RiskManager(starting_balance=self.starting_balance)
        self.circuit_breaker = CircuitBreaker()
        self.risk_manager.circuit_breaker = self.circuit_breaker
        self.risk_manager.MIN_TRADE_INTERVAL_SEC = (
            0  # Disable rate limiting for backtest
        )
        self.trades = []
        self.equity_curve = [self.starting_balance]
        self.predictions = []
        self.signals_generated = 0
        self.signals_rejected = 0
        self._per_strategy = defaultdict(list)

        if strategy_order is None:
            strategy_order = list(self.strategies.keys())

        for md, outcome in replay_sequence:
            # Ensure rate limiting doesn't block (set last_trade to distant past)
            from datetime import datetime as _dt

            self.risk_manager.last_trade_time = _dt.min

            traded = False
            for strat_name in strategy_order:
                strat = self.strategies.get(strat_name)
                if strat is None:
                    continue

                signals = strat.analyze(md)
                if not signals:
                    continue
                if not isinstance(signals, list):
                    signals = [signals]

                for sig in signals:
                    if sig is None:
                        continue
                    self.signals_generated += 1
                    result = self._process_and_settle(sig, strat_name, outcome)
                    if result is not None:
                        traded = True
                        break  # Waterfall: first successful trade wins

                if traded:
                    break

            self.equity_curve.append(self.risk_manager.balance)

        return self.get_results()

    def run_walk_forward(
        self,
        loader: BacktestDataLoader,
        market_type: str,
        n_folds: int = 3,
        train_pct: float = 0.70,
    ) -> dict:
        """Walk-forward backtesting across temporal folds.

        Splits the data chronologically and runs the backtest on each
        test fold, aggregating results.
        """
        sequence = loader.build_replay_sequence(market_type)
        if not sequence:
            return {"error": "No data", "folds": []}

        n = len(sequence)
        fold_size = n // (n_folds + 1)
        fold_results = []

        for fold_idx in range(n_folds):
            # Expanding window: train on [0, train_end), test on [test_start, test_end)
            train_end = int(n * train_pct) + fold_idx * fold_size
            test_start = train_end
            test_end = min(test_start + fold_size, n)

            if test_start >= n or test_end <= test_start:
                break

            test_sequence = sequence[test_start:test_end]
            fold_result = self.run(test_sequence)
            fold_result["fold"] = fold_idx
            fold_result["test_start"] = test_start
            fold_result["test_end"] = test_end
            fold_results.append(fold_result)

        # Aggregate
        all_trades = []
        for fr in fold_results:
            all_trades.extend(fr.get("trades_detail", []))

        return {
            "folds": fold_results,
            "n_folds": len(fold_results),
            "total_trades": len(all_trades),
            "aggregate_win_rate": bmetrics.compute_win_rate(all_trades),
            "aggregate_pnl": bmetrics.compute_total_pnl(all_trades),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_and_settle(
        self, signal: TradeSignal, strategy_name: str, outcome: dict
    ) -> Optional[dict]:
        """Process a signal through risk checks, then settle immediately.

        Returns a trade result dict, or None if rejected.
        """
        # Kelly sizing
        if signal.limit_price and signal.limit_price > 0:
            conf = getattr(signal, "confidence", 0.55)
            if conf <= 0:
                conf = 0.55
            qty = self.risk_manager.calculate_kelly_size(
                conf, signal.limit_price, strategy_name
            )
            signal.quantity = qty

        if signal.quantity < 1:
            self.signals_rejected += 1
            return None

        # EV gate (same as mixins._ml_ev_gate)
        from src.core.fee_calculator import trade_is_profitable

        if not trade_is_profitable(
            getattr(signal, "confidence", 0.5),
            signal.limit_price,
            is_maker=(self.fee_mode == "maker"),
        ):
            self.signals_rejected += 1
            return None

        # Cost calculation
        contract_side = getattr(signal, "contract_side", "YES")
        if signal.side == "sell" and contract_side == "YES":
            cost = (1.0 - signal.limit_price) * signal.quantity
        else:
            cost = signal.limit_price * signal.quantity

        # Risk check
        category = (
            "crypto"
            if "BTC" in signal.symbol.upper() or "kxbtcd" in signal.symbol
            else "weather"
        )
        expiry = getattr(signal, "expiration_time", None)

        if not self.risk_manager.check_order(
            cost,
            category=category,
            strategy_name=strategy_name,
            expiration_time=expiry,
        ):
            self.signals_rejected += 1
            return None

        # Execute and settle
        is_maker = self.fee_mode == "maker"
        fee = compute_fee(signal.limit_price, signal.quantity, is_maker=is_maker).fee

        # Determine payout
        result_str = outcome["result"]
        if contract_side == "YES":
            payout_per = 1.0 if result_str == "yes" else 0.0
        else:  # NO
            payout_per = 1.0 if result_str == "no" else 0.0

        pnl = (payout_per - signal.limit_price) * signal.quantity - fee

        # Record in risk manager (balance tracking)
        self.risk_manager.record_execution(
            cost,
            signal.symbol,
            signal.side,
            signal.quantity,
            signal.limit_price,
            strategy_name=strategy_name,
            contract_side=contract_side,
            # PRD FR-1.2: replayed weather signals settle through
            # bracket_payoff, same as live ones.
            strike_type=getattr(signal, "strike_type", None),
            floor_strike=getattr(signal, "floor_strike", None),
            cap_strike=getattr(signal, "cap_strike", None),
        )
        # Immediately close for settlement
        if self.risk_manager.exchange.positions:
            pos = self.risk_manager.exchange.positions[-1]
            spot = outcome["expiration_value"]
            self.risk_manager.exchange._close_position(pos, spot, reason="EXPIRATION")

        # Record prediction for Brier
        actual = 1 if result_str == "yes" else 0
        pred_prob = getattr(signal, "confidence", 0.5)
        if contract_side == "NO":
            pred_prob = 1.0 - pred_prob
        self.predictions.append((pred_prob, actual))

        # Track circuit breaker
        won = pnl > 0
        self.circuit_breaker.record_trade_result(strategy_name, won)

        trade = {
            "symbol": signal.symbol,
            "strategy": strategy_name,
            "side": signal.side,
            "contract_side": contract_side,
            "entry_price": signal.limit_price,
            "quantity": signal.quantity,
            "cost": cost,
            "fee": fee,
            "result": result_str,
            "payout": payout_per * signal.quantity,
            "pnl": pnl,
            "confidence": getattr(signal, "confidence", 0.0),
            "timestamp": str(getattr(signal, "timestamp", "")),
        }
        self.trades.append(trade)
        self._per_strategy[strategy_name].append(trade)
        self.risk_manager.last_trade_time = (
            getattr(signal, "timestamp", None) or self.risk_manager.last_trade_time
        )

        return trade

    def get_results(self) -> dict:
        """Compile all metrics into a results dict."""
        returns = [t["pnl"] for t in self.trades]
        per_strategy = {}
        for name, strades in self._per_strategy.items():
            per_strategy[name] = {
                "trades": len(strades),
                "win_rate": bmetrics.compute_win_rate(strades),
                "pnl": bmetrics.compute_total_pnl(strades),
                "fees": bmetrics.compute_total_fees(strades),
            }

        dd_pct, dd_peak, dd_trough = bmetrics.compute_max_drawdown(self.equity_curve)

        return {
            "total_trades": len(self.trades),
            "wins": sum(1 for t in self.trades if t["pnl"] > 0),
            "losses": sum(1 for t in self.trades if t["pnl"] <= 0),
            "win_rate": bmetrics.compute_win_rate(self.trades),
            "total_pnl": bmetrics.compute_total_pnl(self.trades),
            "total_fees": bmetrics.compute_total_fees(self.trades),
            "net_pnl": bmetrics.compute_total_pnl(self.trades),
            "sharpe_ratio": bmetrics.compute_sharpe_ratio(returns),
            "max_drawdown_pct": dd_pct,
            "profit_factor": bmetrics.compute_profit_factor(self.trades),
            "brier_score": bmetrics.compute_brier_score(self.predictions),
            "equity_curve": self.equity_curve,
            "signals_generated": self.signals_generated,
            "signals_rejected": self.signals_rejected,
            "per_strategy": per_strategy,
            "trades_detail": self.trades,
            "criteria": bmetrics.validate_criteria(
                {
                    "win_rate": bmetrics.compute_win_rate(self.trades),
                    "per_strategy": per_strategy,
                    "net_pnl": bmetrics.compute_total_pnl(self.trades),
                    "max_drawdown_pct": dd_pct,
                    "brier_score": bmetrics.compute_brier_score(self.predictions),
                }
            ),
        }
