"""Kalshi fee computation.

Accurate maker and taker fee formulas per the Kalshi fee schedule.
Used by the order router, risk manager, and EV calculations.

Sprint 4, Task 4.2 of Money Printer V2.

Fee formulas
------------
- Maker: ``ceil(0.0175 * C * P * (1-P))`` per lot
- Taker: ``ceil(0.07  * C * P * (1-P))`` per lot

where *C* = number of contracts, *P* = contract price (0–1).

The ``ceil`` rounds UP to the nearest cent ($0.01).
"""

import math
from typing import NamedTuple

MAKER_RATE = 0.0175
TAKER_RATE = 0.07


class FeeResult(NamedTuple):
    """Breakdown of a fee calculation."""

    fee: float  # Total fee in dollars
    per_contract: float  # Fee per contract in dollars
    rate_used: str  # "maker" or "taker"


def maker_fee(price: float, contracts: int = 1) -> float:
    """Compute maker fee in dollars (rounded up to nearest cent)."""
    if price <= 0 or price >= 1.0 or contracts <= 0:
        return 0.0
    raw = MAKER_RATE * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


def taker_fee(price: float, contracts: int = 1) -> float:
    """Compute taker fee in dollars (rounded up to nearest cent)."""
    if price <= 0 or price >= 1.0 or contracts <= 0:
        return 0.0
    raw = TAKER_RATE * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


def compute_fee(
    price: float,
    contracts: int = 1,
    is_maker: bool = True,
) -> FeeResult:
    """Compute fee with full breakdown.

    Parameters
    ----------
    price : float
        Contract price in the 0–1 range.
    contracts : int
        Number of contracts.
    is_maker : bool
        ``True`` for limit (maker) orders, ``False`` for market (taker).

    Returns
    -------
    FeeResult
        Named tuple with ``fee``, ``per_contract``, ``rate_used``.
    """
    if is_maker:
        total = maker_fee(price, contracts)
        per = maker_fee(price, 1)
        label = "maker"
    else:
        total = taker_fee(price, contracts)
        per = taker_fee(price, 1)
        label = "taker"
    return FeeResult(fee=total, per_contract=per, rate_used=label)


def ev_after_fees(
    probability: float,
    price: float,
    contracts: int = 1,
    is_maker: bool = True,
) -> float:
    """Expected value per contract after fees.

    Binary contracts pay fees on both entry and exit (round-trip), so
    fee_per_contract is doubled:

    EV = P(win) * (1 - price) - (1 - P(win)) * price - 2 * fee_per_contract
       = probability - price - 2 * fee_per_contract
    """
    fee_per = compute_fee(price, 1, is_maker).per_contract
    return probability - price - 2 * fee_per


def trade_is_profitable(
    probability: float,
    price: float,
    contracts: int = 1,
    is_maker: bool = True,
) -> bool:
    """Return True if the trade has positive EV after fees."""
    return ev_after_fees(probability, price, contracts, is_maker) > 0
