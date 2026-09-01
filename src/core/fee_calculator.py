"""Kalshi fee computation.

Maker and taker fee formulas per the Kalshi fee schedule. Used by the order
router, risk manager, backtest engine, and EV calculations.

Sprint 4, Task 4.2 of Money Printer V2. Corrected in PRD Phase 2 against the
published schedule effective 2026-07-07 and live ``/series`` metadata; see
``reports/phase2/ws_c_fee_verification.md`` for the provenance and the 21-row
table check.

Fee formulas
------------
- Taker: ``ceil(0.07 * C * P * (1-P))`` per order, all series.
- Maker: ``ceil(M * C * P * (1-P))`` per order, where the multiplier *M*
  defaults to **zero**. Only the series listed in the schedule's
  "Non-Standard Fees" table are billed for resting liquidity, and the live API
  marks them with ``series.fee_type == "quadratic_with_maker_fees"``.
- Settlement: **no fee**. A contract held to expiry pays its entry fee only.

where *C* = number of contracts, *P* = contract price (0–1). The ``ceil``
rounds UP to the nearest cent ($0.01).

Three corrections landed here, each of which had been inflating modelled cost:

1. ``ceil(raw * 100)`` on a binary float adds a spurious cent whenever the
   exact fee lands on a whole cent (``0.07*100*0.10*0.90 == 0.6300000000000002``).
   That reproduced only 14 of the 21 published table rows; rounding to 9 places
   before the ceil reproduces 21 of 21.
2. ``maker_fee`` charged 1.75% unconditionally. The weather series this project
   now trades (``KXHIGH*``) are absent from the non-standard table and report
   ``fee_type == "quadratic"``, so their true maker fee is **$0.00**. Charging
   1.75% on a 10¢ contract books a full cent of cost the exchange never levies
   — a tenth of the premium, on the maker-first path PRD FR-3.3 makes the
   flagship.
3. ``ev_after_fees`` hard-coded a round trip. Weather positions are held to
   settlement (PRD FR-1.5) and settlement is free, so they pay one leg, not two.

Series threading
----------------
The maker multiplier is a property of the *series*, so every maker-path caller
has to say which series it is pricing. Runtime call sites hold a full market
ticker rather than a series ticker, so they derive the fee type with
:func:`fee_type_for_symbol`; the threaded callers today are
``RiskManager.calculate_kelly_size``, ``SimulatedExchange.open_position`` and
its two exit-fee paths, ``BacktestEngine``, and ``OrderRouter.route``. A caller
that supplies no symbol falls to the schedule's documented standard default (no
maker fee), which is correct for every series in the Phase 1-3 weather scope and
would be wrong for ``KXAAAGASM`` (PRD Phase 4) — so pass the symbol.

The taker formula is uniform across series and needs no such threading, which
is one reason the live pre-trade EV gate prices the taker path.
"""

import math
from typing import NamedTuple

MAKER_RATE = 0.0175
TAKER_RATE = 0.07

#: ``series.fee_type`` value marking a series that bills resting liquidity.
FEE_TYPE_WITH_MAKER_FEES = "quadratic_with_maker_fees"
#: ``series.fee_type`` value for the standard schedule (maker multiplier M=0).
FEE_TYPE_STANDARD = "quadratic"

#: Series verified to charge a maker fee. Every entry here has been confirmed
#: BOTH in the schedule's "Non-Standard Fees" table and against live
#: ``/series`` metadata (``fee_type == "quadratic_with_maker_fees"``).
#:
#: Verified 2026-07-27: ``KXAAAGASM`` reports ``quadratic_with_maker_fees`` and
#: appears in the table. ``KXAAAGASW`` was briefly listed here on the
#: assumption that the weekly gas series shared the monthly one's treatment; it
#: does not — it reports ``quadratic`` and appears zero times in the schedule.
#: Do not re-add a series on the strength of its name resembling another's.
#:
#: A series absent from this set is treated as standard (no maker fee), which
#: is the schedule's documented default. Verify a new series against the live
#: API before trading it rather than relying on that default.
KNOWN_MAKER_FEE_SERIES = frozenset({"KXAAAGASM"})

#: Per-series fee multiplier (the API's ``series.fee_multiplier``), applied to
#: BOTH the maker and taker quadratic before the ceil-to-cent. The schedule's
#: documented default is 1.0, and any series absent from this map is billed at
#: 1.0.
#:
#: Live-API findings (verified 2026-09-01):
#:
#: * The Mentions category (95 series, ``KX*MENTION``) reports
#:   ``fee_type == "quadratic"`` with ``fee_multiplier == 1`` — the standard
#:   schedule, so no entry is needed here (maker fee is $0 under the standard
#:   schedule anyway).
#: * ``KXBTCY`` / ``KXETHY`` (annual BTC/ETH range ladders) report
#:   ``fee_multiplier == 0`` in the live API, which would make them literally
#:   fee-free. That is UNVERIFIED by an actual trade, and a fee model that
#:   understates cost is exactly the optimistic-EV failure mode behind both
#:   HALT verdicts — so their entries stay at the conservative 1.0 until a
#:   demo-API trade confirms the fill really books $0.00. Change an entry only
#:   with that receipt.
SERIES_FEE_MULTIPLIER = {
    "KXBTCY": 1.0,  # API reports 0; unverified by a trade — kept conservative
    "KXETHY": 1.0,  # API reports 0; unverified by a trade — kept conservative
}

#: Exit models for :func:`ev_after_fees`.
EXIT_TRADE_OUT = "trade_out"
EXIT_SETTLEMENT = "settlement"


class FeeResult(NamedTuple):
    """Breakdown of a fee calculation."""

    fee: float  # Total fee in dollars
    per_contract: float  # Fee per contract in dollars
    rate_used: str  # "maker" or "taker"


def fee_type_for_series(series_ticker: str) -> str:
    """Return the ``fee_type`` to bill a series under.

    Verified series are looked up; anything else falls to the schedule's
    documented default of no maker fee. This default is correct for every
    series this project currently trades, but it is a *default*: confirm a new
    series against live ``/series`` metadata before relying on it.
    """
    ticker = (series_ticker or "").strip().upper()
    return (
        FEE_TYPE_WITH_MAKER_FEES
        if ticker in KNOWN_MAKER_FEE_SERIES
        else FEE_TYPE_STANDARD
    )


def series_ticker_from_symbol(symbol: str) -> str:
    """Extract the series ticker from a full Kalshi market ticker.

    Kalshi market tickers are ``SERIES-EVENT-STRIKE``
    (``KXHIGHNY-26JUL27-B82.5``, ``KXAAAGASM-26AUG-B3.25``), so the series is
    everything before the first hyphen. Returns ``""`` for an empty or
    hyphen-less input, which :func:`fee_type_for_series` maps to the standard
    schedule.
    """
    return (symbol or "").strip().upper().split("-", 1)[0]


def fee_type_for_symbol(symbol: str) -> str:
    """Return the ``fee_type`` to bill a full market ticker under.

    The convenience wrapper the runtime uses: call sites hold a market symbol,
    not a series ticker, and the maker multiplier is a per-series property.
    Threading this is what keeps a ``KXAAAGASM`` order (PRD Phase 4) from being
    priced with the weather series' zero maker multiplier.
    """
    return fee_type_for_series(series_ticker_from_symbol(symbol))


def fee_multiplier_for_series(series_ticker: str) -> float:
    """Return the ``fee_multiplier`` to bill a series at (default 1.0).

    Looked up in :data:`SERIES_FEE_MULTIPLIER`; anything absent — including an
    empty ticker from a symbol-less caller — bills at the schedule's documented
    default of 1.0. See the map's docstring for why a live-API multiplier of 0
    is not, on its own, grounds for an entry below 1.0.
    """
    ticker = (series_ticker or "").strip().upper()
    return SERIES_FEE_MULTIPLIER.get(ticker, 1.0)


def fee_multiplier_for_symbol(symbol: str) -> float:
    """Return the ``fee_multiplier`` for a full market ticker (default 1.0)."""
    return fee_multiplier_for_series(series_ticker_from_symbol(symbol))


def _ceil_cents(raw_dollars: float) -> float:
    """Round a raw fee UP to the next cent, without the float-ULP overcharge.

    ``round(.., 9)`` collapses the representation error that would otherwise
    push an exact whole-cent fee one ULP above the cent boundary and make
    ``ceil`` add a full cent.
    """
    return math.ceil(round(raw_dollars * 100, 9)) / 100.0


def maker_fee(
    price: float,
    contracts: int = 1,
    series_fee_type: str = FEE_TYPE_STANDARD,
    fee_multiplier: float = 1.0,
) -> float:
    """Compute maker fee in dollars (rounded up to nearest cent).

    Returns ``0.0`` for standard-schedule series, which is every series in the
    Phase 1–3 weather scope. Pass ``FEE_TYPE_WITH_MAKER_FEES`` (or the result of
    :func:`fee_type_for_series`) for a series that bills resting liquidity.
    ``fee_multiplier`` (the API's per-series ``fee_multiplier``, via
    :func:`fee_multiplier_for_symbol`) scales the quadratic before the ceil;
    the default 1.0 leaves every existing call site's fee unchanged.
    """
    if series_fee_type != FEE_TYPE_WITH_MAKER_FEES:
        return 0.0
    if price <= 0 or price >= 1.0 or contracts <= 0:
        return 0.0
    return _ceil_cents(fee_multiplier * MAKER_RATE * contracts * price * (1.0 - price))


def taker_fee(
    price: float, contracts: int = 1, fee_multiplier: float = 1.0
) -> float:
    """Compute taker fee in dollars (rounded up to nearest cent).

    ``fee_multiplier`` scales the quadratic before the ceil (default 1.0, the
    schedule's documented default — see :data:`SERIES_FEE_MULTIPLIER`).
    """
    if price <= 0 or price >= 1.0 or contracts <= 0:
        return 0.0
    return _ceil_cents(fee_multiplier * TAKER_RATE * contracts * price * (1.0 - price))


def compute_fee(
    price: float,
    contracts: int = 1,
    is_maker: bool = True,
    series_fee_type: str = FEE_TYPE_STANDARD,
    fee_multiplier: float = 1.0,
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
    series_fee_type : str
        The series' ``fee_type``. Only affects the maker path; the taker
        formula is uniform across series.
    fee_multiplier : float
        The series' ``fee_multiplier`` (via :func:`fee_multiplier_for_symbol`),
        scaling both rate formulas. Defaults to the schedule's documented 1.0,
        which every entry in :data:`SERIES_FEE_MULTIPLIER` currently also is.

    Returns
    -------
    FeeResult
        Named tuple with ``fee``, ``per_contract``, ``rate_used``.
    """
    if is_maker:
        total = maker_fee(price, contracts, series_fee_type, fee_multiplier)
        per = maker_fee(price, 1, series_fee_type, fee_multiplier)
        label = "maker"
    else:
        total = taker_fee(price, contracts, fee_multiplier)
        per = taker_fee(price, 1, fee_multiplier)
        label = "taker"
    return FeeResult(fee=total, per_contract=per, rate_used=label)


def ev_after_fees(
    probability: float,
    price: float,
    contracts: int = 1,
    is_maker: bool = True,
    series_fee_type: str = FEE_TYPE_STANDARD,
    exit_mode: str = EXIT_TRADE_OUT,
) -> float:
    """Expected value per contract after fees.

    EV = P(win) * (1 - price) - (1 - P(win)) * price - legs * fee_per_contract
       = probability - price - legs * fee_per_contract

    ``exit_mode``
        ``"trade_out"`` (default) charges both legs, correct for a position
        closed by trading back out. ``"settlement"`` charges the entry leg only:
        Kalshi levies no settlement fee, and PRD FR-1.5 holds every weather
        position to expiry.
    """
    if exit_mode not in (EXIT_TRADE_OUT, EXIT_SETTLEMENT):
        raise ValueError(
            f"exit_mode must be {EXIT_TRADE_OUT!r} or {EXIT_SETTLEMENT!r}, got {exit_mode!r}"
        )
    fee_per = compute_fee(price, 1, is_maker, series_fee_type).per_contract
    legs = 1 if exit_mode == EXIT_SETTLEMENT else 2
    return probability - price - legs * fee_per


def trade_is_profitable(
    probability: float,
    price: float,
    contracts: int = 1,
    is_maker: bool = True,
    series_fee_type: str = FEE_TYPE_STANDARD,
    exit_mode: str = EXIT_TRADE_OUT,
) -> bool:
    """Return True if the trade has positive EV after fees."""
    return (
        ev_after_fees(
            probability, price, contracts, is_maker, series_fee_type, exit_mode
        )
        > 0
    )
