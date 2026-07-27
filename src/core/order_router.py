"""Maker-first order routing.

All orders default to limit (maker) to capture the maker-taker fee advantage.
Only routes as market (taker) when an urgency flag is set (e.g., latency
arbitrage with fast-decaying edge).

Sprint 4, Task 4.1 of Money Printer V2. Revised in PRD Phase 2: the maker
multiplier is a per-series property, zero on the standard schedule and
non-zero only for the series in the fee schedule's "Non-Standard Fees" table.
The router therefore takes the market ``symbol`` and derives the series fee
type from it, instead of pricing every maker order as free — which made maker
beat taker on EV in every equal-price comparison regardless of series. On a
standard series the advantage is the whole 7% taker fee, not the 5.25-point
maker/taker spread this module was written against.

This module has no production caller today. It is kept correct so that the
first caller inherits a threaded fee model rather than a silent assumption.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from src.core.fee_calculator import compute_fee, ev_after_fees, fee_type_for_symbol

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """Result of the order router's analysis."""

    order_type: str  # "limit" or "market"
    limit_price: Optional[float]  # Price for limit orders
    is_maker: bool
    fee_estimate: float  # Estimated fee in dollars
    ev_per_contract: float  # EV per contract after fees
    reason: str  # Human-readable explanation


@dataclass
class OrderRouterStats:
    """Tracks maker/taker ratio."""

    maker_count: int = 0
    taker_count: int = 0

    @property
    def total(self) -> int:
        return self.maker_count + self.taker_count

    @property
    def maker_ratio(self) -> float:
        if self.total == 0:
            return 1.0
        return self.maker_count / self.total


class OrderRouter:
    """Maker-first order router for Kalshi.

    Parameters
    ----------
    spread_buffer : float
        How far inside the spread to place limit orders (default 0.02 = 2c).
    urgency_threshold : float
        If edge exceeds this, allow taker orders (default 0.10 = 10c).
    """

    def __init__(
        self,
        spread_buffer: float = 0.02,
        urgency_threshold: float = 0.10,
    ):
        self.spread_buffer = spread_buffer
        self.urgency_threshold = urgency_threshold
        self.stats = OrderRouterStats()

    def route(
        self,
        side: str,
        probability: float,
        bid: float,
        ask: float,
        contracts: int = 1,
        urgent: bool = False,
        recommended_price: Optional[float] = None,
        symbol: str = "",
    ) -> RoutingDecision:
        """Decide order type and price.

        Parameters
        ----------
        side : str
            ``"buy"`` or ``"sell"``.
        probability : float
            Calibrated probability of winning (P(YES) for YES, P(NO) for NO).
        bid : float
            Current best bid.
        ask : float
            Current best ask.
        contracts : int
            Number of contracts.
        urgent : bool
            Force taker routing (e.g., latency arb).
        recommended_price : float, optional
            ML-recommended limit price (from time-optimizer).
        symbol : str
            Full market ticker. Supplies the series identity the maker fee
            depends on. Omitting it prices the maker leg on the standard
            schedule (no maker fee), which is correct for the weather series
            and wrong for KXAAAGASM.
        """
        series_fee_type = fee_type_for_symbol(symbol)
        # Default: maker (limit order)
        if side == "buy":
            # Place limit at bid + buffer (penny the bid)
            if recommended_price is not None:
                limit_price = min(recommended_price, ask)
            else:
                limit_price = bid + self.spread_buffer
            limit_price = max(0.01, min(limit_price, ask))
        else:
            # Selling: place at ask - buffer
            if recommended_price is not None:
                limit_price = max(recommended_price, bid)
            else:
                limit_price = ask - self.spread_buffer
            limit_price = max(bid, min(0.99, limit_price))

        # Check if taker is needed
        maker_ev = ev_after_fees(
            probability,
            limit_price,
            contracts,
            is_maker=True,
            series_fee_type=series_fee_type,
        )
        taker_ev = ev_after_fees(
            probability, ask if side == "buy" else bid, contracts, is_maker=False
        )

        use_taker = False
        reason = "default maker"

        if urgent and taker_ev > 0:
            use_taker = True
            reason = "urgent flag set"
        elif maker_ev <= 0 and taker_ev > 0 and taker_ev > self.urgency_threshold:
            use_taker = True
            reason = "maker EV negative but taker has strong edge"

        if use_taker:
            fill_price = ask if side == "buy" else bid
            fee_est = compute_fee(fill_price, contracts, is_maker=False).fee
            ev = taker_ev
            self.stats.taker_count += 1
            return RoutingDecision(
                order_type="market",
                limit_price=None,
                is_maker=False,
                fee_estimate=fee_est,
                ev_per_contract=ev,
                reason=reason,
            )

        # Maker path
        fee_est = compute_fee(
            limit_price, contracts, is_maker=True, series_fee_type=series_fee_type
        ).fee
        self.stats.maker_count += 1
        return RoutingDecision(
            order_type="limit",
            limit_price=round(limit_price, 2),
            is_maker=True,
            fee_estimate=fee_est,
            ev_per_contract=maker_ev,
            reason=reason,
        )

    def get_maker_ratio(self) -> float:
        """Return the current maker/total order ratio."""
        return self.stats.maker_ratio
