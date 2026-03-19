"""Cross-spread arbitrage detector.

Monitors for the condition ``YES_ask + NO_ask < 1.00`` within the
same Kalshi market.  When found, simultaneously buying YES and NO
locks in a risk-free profit minus fees.

Sprint 3, Task 3.8 of Money Printer V2.
"""

import logging
import math
from datetime import datetime, timedelta
from typing import List

from src.core.interfaces import MarketData, Strategy, TradeSignal

logger = logging.getLogger(__name__)

# Kalshi fee helpers (simplified — full calculator is Sprint 4)
_MAKER_FEE_RATE = 0.0175
_TAKER_FEE_RATE = 0.07


def _estimate_fee(price: float, contracts: int, maker: bool = True) -> float:
    """Estimate Kalshi fee per side (cents, rounded up).

    fee = ceil(rate * contracts * P * (1-P))
    """
    rate = _MAKER_FEE_RATE if maker else _TAKER_FEE_RATE
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0  # Round up to nearest cent


class CrossSpreadArbStrategy(Strategy):
    """Risk-free cross-spread arbitrage.

    When ``YES_ask + NO_ask < 1.00`` (accounting for fees), buy both
    sides for a guaranteed profit at settlement.  One side settles
    at 1.00, the other at 0.00 — total payout is 1.00 per pair.

    Parameters
    ----------
    min_profit_after_fees : float
        Minimum profit per pair of contracts (in dollars) after
        estimated maker fees (default $0.005 = half a cent).
    cooldown_seconds : int
        Cooldown per symbol after an arb is taken.
    """

    def __init__(
        self,
        min_profit_after_fees: float = 0.005,
        cooldown_seconds: int = 120,
    ):
        self.min_profit_after_fees = min_profit_after_fees
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until: dict = {}

    def name(self) -> str:
        return "Cross-Spread Arb"

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        now = datetime.now()

        # Cooldown per symbol
        sym = market_data.symbol
        if now < self._cooldown_until.get(sym, datetime.min):
            return signals

        yes_ask = market_data.ask
        no_ask = extra.get("no_ask", 0.0)

        # Derive NO ask from YES bid if not explicit
        if no_ask <= 0 and market_data.bid > 0:
            no_ask = 1.0 - market_data.bid

        # Both sides must be quotable
        if yes_ask <= 0 or no_ask <= 0:
            return signals
        if yes_ask >= 1.0 or no_ask >= 1.0:
            return signals

        total_cost = yes_ask + no_ask

        # Check for arb: cost < 1.00
        if total_cost >= 1.0:
            return signals

        # Gross profit
        gross = 1.0 - total_cost

        # Estimate fees (both sides, maker)
        contracts = 10
        fee_yes = _estimate_fee(yes_ask, contracts, maker=True)
        fee_no = _estimate_fee(no_ask, contracts, maker=True)
        total_fees = fee_yes + fee_no

        # Net profit per pair
        net_per_pair = gross - total_fees / contracts
        if net_per_pair < self.min_profit_after_fees:
            return signals

        close_time = extra.get("close_time")

        logger.info(
            "[CrossArb] ARB FOUND %s | YES_ask=%.3f + NO_ask=%.3f = %.3f < 1.00 | "
            "gross=%.4f fees=%.4f net/pair=%.4f",
            sym,
            yes_ask,
            no_ask,
            total_cost,
            gross,
            total_fees,
            net_per_pair,
        )

        # Signal 1: Buy YES
        sig_yes = TradeSignal(
            symbol=sym,
            side="buy",
            quantity=contracts,
            limit_price=yes_ask,
            confidence=0.99,
            contract_side="YES",
        )
        sig_yes.stop_loss = 0.0  # Hold to settlement (guaranteed profit)
        sig_yes.disable_profit_targets = True
        if close_time:
            sig_yes.expiration_time = close_time
        signals.append(sig_yes)

        # Signal 2: Buy NO
        sig_no = TradeSignal(
            symbol=sym,
            side="buy",
            quantity=contracts,
            limit_price=no_ask,
            confidence=0.99,
            contract_side="NO",
        )
        sig_no.stop_loss = 0.0
        sig_no.disable_profit_targets = True
        if close_time:
            sig_no.expiration_time = close_time
        signals.append(sig_no)

        self._cooldown_until[sym] = now + timedelta(seconds=self.cooldown_seconds)
        return signals
