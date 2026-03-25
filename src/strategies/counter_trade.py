"""Counter-trade analyzer for hedging confirmed losing positions.

Sprint 5 — evaluates open losing positions and generates conditional
hedge signals when the original trade direction appears wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from src.core.interfaces import MarketData, TradeSignal

logger = logging.getLogger(__name__)


class CounterTradeAnalyzer:
    """Evaluates open losing positions for counter-trade hedging opportunities.

    This is NOT automatic reversal --- it's a conditional hedge. Only fires when:
    1. Position has moved significantly against us (>= min_adverse_move)
    2. Model re-evaluation confirms original direction was wrong
    3. Enough time remains before expiry (>= min_tte_remaining seconds)
    4. No prior counter-trade for this contract

    Starts in LOG-ONLY mode by default. Set live=True to enable execution.
    """

    def __init__(
        self,
        min_adverse_move: float = 0.06,
        min_tte_remaining: int = 300,
        counter_size_ratio: float = 0.5,
        live: bool = False,
    ):
        self._min_adverse = min_adverse_move
        self._min_tte = min_tte_remaining  # 5 minutes
        self._size_ratio = counter_size_ratio  # 50% of original
        self._live = live
        self._countered_symbols: set[str] = set()  # track already-countered
        self._log_count = 0
        self._fire_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_counter(
        self,
        position: dict,
        current_price: float,
        predictor=None,
        market_data: Optional[MarketData] = None,
    ) -> Optional[TradeSignal]:
        """Evaluate a losing position for counter-trade.

        Args:
            position: Position dict from SimulatedExchange with keys:
                symbol, side, entry_price, pnl, contract_side, strike,
                quantity, expiration_time, open_time
            current_price: Current market price of the contract
            predictor: ModelPredictor for re-evaluation (optional)
            market_data: Current MarketData for re-evaluation (optional)

        Returns:
            TradeSignal for the counter-trade, or None if conditions not met.
        """
        symbol = position.get("symbol")
        if not symbol:
            return None

        # 1. Skip if symbol already countered this cycle
        if symbol in self._countered_symbols:
            return None

        # 2. Check adverse move threshold
        pnl = position.get("pnl", 0.0)
        quantity = position.get("quantity", 0)
        if quantity <= 0:
            return None
        if abs(pnl) < self._min_adverse * quantity:
            return None
        # Must actually be a *loss* (negative pnl)
        if pnl >= 0:
            return None

        # 3. Check time remaining before expiry
        expiration_time = position.get("expiration_time")
        if expiration_time is not None:
            now = datetime.now()
            # Handle both datetime objects and timestamp floats
            if isinstance(expiration_time, (int, float)):
                remaining_s = expiration_time - now.timestamp()
            elif isinstance(expiration_time, datetime):
                remaining_s = (expiration_time - now).total_seconds()
            else:
                remaining_s = 0.0

            if remaining_s < self._min_tte:
                logger.debug(
                    "[CounterTrade] %s: only %.0fs remaining (need %d), skipping",
                    symbol,
                    remaining_s,
                    self._min_tte,
                )
                return None

        # 4. Model re-evaluation (optional)
        if predictor is not None and market_data is not None:
            strike = position.get("strike")
            if strike is not None:
                tte = 0.0
                if expiration_time is not None:
                    now = datetime.now()
                    if isinstance(expiration_time, datetime):
                        tte = max(0.0, (expiration_time - now).total_seconds())
                    elif isinstance(expiration_time, (int, float)):
                        tte = max(0.0, expiration_time - now.timestamp())

                try:
                    pred = predictor.predict_btc(market_data, strike, tte)
                    prob = pred.get("probability", 0.5)
                except Exception as exc:
                    logger.debug("[CounterTrade] %s: predictor failed: %s", symbol, exc)
                    prob = None

                if prob is not None:
                    contract_side = position.get("contract_side", "YES")
                    side = position.get("side", "buy")

                    # Original trade was bullish on this contract_side
                    was_bullish = side == "buy"

                    if contract_side == "YES":
                        # Bought YES expecting prob > 0.5; counter if prob < 0.40
                        # Sold YES expecting prob < 0.5; counter if prob > 0.60
                        if was_bullish and prob >= 0.40:
                            logger.debug(
                                "[CounterTrade] %s: model prob=%.3f still supports "
                                "original BUY YES, skipping",
                                symbol,
                                prob,
                            )
                            return None
                        if not was_bullish and prob <= 0.60:
                            logger.debug(
                                "[CounterTrade] %s: model prob=%.3f still supports "
                                "original SELL YES, skipping",
                                symbol,
                                prob,
                            )
                            return None
                    else:  # NO contract
                        # Bought NO expecting prob < 0.5; counter if prob > 0.60
                        # Sold NO expecting prob > 0.5; counter if prob < 0.40
                        if was_bullish and prob <= 0.60:
                            logger.debug(
                                "[CounterTrade] %s: model prob=%.3f still supports "
                                "original BUY NO, skipping",
                                symbol,
                                prob,
                            )
                            return None
                        if not was_bullish and prob >= 0.40:
                            logger.debug(
                                "[CounterTrade] %s: model prob=%.3f still supports "
                                "original SELL NO, skipping",
                                symbol,
                                prob,
                            )
                            return None

        # 5. Generate counter signal
        original_contract_side = position.get("contract_side", "YES")
        counter_side = "NO" if original_contract_side == "YES" else "YES"
        counter_qty = max(1, int(quantity * self._size_ratio))

        signal = TradeSignal(
            symbol=symbol,
            side="buy",
            quantity=counter_qty,
            limit_price=current_price,
            confidence=0.6,
            contract_side=counter_side,
        )

        # 6. Mark symbol as countered
        self._countered_symbols.add(symbol)
        self._log_count += 1

        # 7. Log the decision
        mode = "LIVE" if self._live else "LOG-ONLY"
        logger.info(
            "[CounterTrade][%s] %s: pnl=%.4f, entry=%.4f, current=%.4f -> "
            "counter BUY %s x%d @ %.4f",
            mode,
            symbol,
            pnl,
            position.get("entry_price", 0.0),
            current_price,
            counter_side,
            counter_qty,
            current_price,
        )

        if self._live:
            self._fire_count += 1
            return signal

        # Log-only mode: return None so caller does not execute
        return None

    def reset_cycle(self) -> None:
        """Reset countered symbols set on new cycle."""
        self._countered_symbols.clear()

    def get_stats(self) -> dict:
        """Return counter-trade statistics."""
        return {
            "log_count": self._log_count,
            "fire_count": self._fire_count,
            "live": self._live,
            "pending_countered": len(self._countered_symbols),
            "min_adverse_move": self._min_adverse,
            "min_tte_remaining": self._min_tte,
            "counter_size_ratio": self._size_ratio,
        }
