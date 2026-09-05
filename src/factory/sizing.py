"""The runtime's cold-start sizing law, as one shared offline predicate.

Why this module exists
----------------------
``RiskManager.calculate_kelly_size`` deliberately refuses to size from raw model
confidence (Bertsimas & Mundru 2024). Until a strategy has
``MIN_WIN_SAMPLES`` closed trades it blends a **neutral 0.50 prior** with the
caller's confidence::

    historical_wr = 0.50                        # risk_manager.py, cold start
    p             = 0.6 * historical_wr + 0.4 * confidence
    f             = p - (1 - p) / b,   b = net_win / net_loss

so ``p <= 0.70`` for any confidence in ``[0, 1]`` and a buy above roughly 70c is
sized to **zero contracts**, not to a small size.  Nothing on the factory side
knew that.  The F3 factory promoted genome ``0c4b20502f2daf65``, whose offline
trade set buys far-bracket NO at a median price of 0.84; 117 of its 130 offline
trades size to 0 in the sandbox that executes it.  The full reconstruction,
the four options that were rejected and the two that were adopted are in
``reports/factory/sizing_cold_start_2026-09-05.md``.

This module is the reusable piece the fix needs:

* :func:`is_sizable` / :func:`sizable_mask` -- would the runtime give this
  ``(price, confidence)`` a non-zero size at cold start?
* :func:`audit_rows` / :func:`audit_genome` -- how much of an offline trade set
  survives that law, in trades and in ``target_date`` gate units.
* :func:`assert_promotable` -- the promotion guard ``scripts/factory.py``
  calls before it writes a promoted spec.

Fidelity
--------
:func:`kelly_edge` follows ``calculate_kelly_size``'s arithmetic **step for
step** (fee -> ``net_win``/``net_loss`` -> ``b`` -> ``p`` -> ``f``) rather than
the algebraic shortcut, and takes the fee from the project's own
``src.core.fee_calculator`` with the same ``is_maker=True`` /
``fee_type_for_symbol`` threading the risk manager uses -- so a change to the
fee schedule moves this predicate too.  The shortcut ``f > 0  <=>  p > price +
2 * fee_per`` is exact (see :func:`price_ceiling`) and is what makes the law a
**price** cut rather than a confidence cut, but it is derived, not assumed:
``tests/test_factory_sizing.py`` drives all 130 of the deployed genome's offline
trades through this module *and* through a real ``RiskManager`` with an empty
win record and asserts they agree row for row.  That test, not this docstring,
is what keeps the mirrored constants below honest.

Scope
-----
Offline / lab only.  This module imports ``src.core`` and must **never** be
reachable from the maia sandbox image's import graph -- ``src/factory/__init__``
names ``columns``/``features``/``genome``/``promoted`` as the runtime-safe slice
and ``tests/test_factory_isolation.py`` enforces it.  It changes no runtime
behaviour: it only predicts what the unmodified risk manager will do.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Sequence

import numpy as np

from src.core.fee_calculator import compute_fee, fee_type_for_symbol
from src.core.risk_manager import MIN_WIN_SAMPLES

# ---------------------------------------------------------------------------
# The law, mirrored from src/core/risk_manager.py (protected; never edited here)
# ---------------------------------------------------------------------------
#: ``historical_wr`` before the win-rate window holds ``MIN_WIN_SAMPLES``.
NEUTRAL_PRIOR_WIN_RATE = 0.50
#: Weight on the (cold-start: prior) realized win rate in the sizing blend.
HISTORICAL_WEIGHT = 0.6
#: Weight on the caller's confidence in the sizing blend.
CONFIDENCE_WEIGHT = 0.4
#: Both legs are charged when sizing (entry + a hypothetical traded-out exit).
FEE_LEGS = 2.0

#: Fraction of a genome's offline trades that must be sizable for `promote`.
#:
#: A policy choice, and it is stated as one.  It is deliberately NOT calibrated
#: to a curve, because there is no curve: across the six specs in
#: ``configs/factory/promoted/`` the sizable fraction is bimodal -- five sit at
#: 7.1-33.3 % and one at 98.8 %, with nothing in between -- so every threshold
#: in ``(0.34, 0.98)`` returns the same six verdicts.  0.75 sits in that empty
#: middle, high rather than low, because FR-5.2 condition 4 checks the *spec
#: hash* and not the executed shape: a promoted genome whose executed form
#: silently drops a quarter of its fills is no longer the shape the gate
#: pre-registered.  It may be raised per-run (``--min-sizable-fraction``); it
#: may not be lowered, because loosening it is how this defect shipped.
MIN_SIZABLE_TRADE_FRACTION = 0.75

#: The memo every refusal points the operator at.
FINDING_REPORT = "reports/factory/sizing_cold_start_2026-09-05.md"


class UnsizableGenomeError(ValueError):
    """A genome's offline trade set is mostly unsizable by the runtime that would execute it."""


# ---------------------------------------------------------------------------
# scalar predicate
# ---------------------------------------------------------------------------
def blended_p(confidence: float, historical_wr: float = NEUTRAL_PRIOR_WIN_RATE) -> float:
    """``0.6 * historical_wr + 0.4 * confidence`` -- the risk manager's sizing probability."""
    return HISTORICAL_WEIGHT * float(historical_wr) + CONFIDENCE_WEIGHT * float(confidence)


def fee_per_contract(price: float, symbol: str = "") -> float:
    """The per-contract fee ``calculate_kelly_size`` deducts from the odds at ``price``.

    Same call the risk manager makes: the **maker** leg of the project's own
    ``fee_calculator``, with the series' ``fee_type`` derived from the ticker so
    a non-standard series (``KXAAAGASM``) is not priced as if resting liquidity
    were free.  Every ``KXHIGH*`` weather series is standard, so this is 0.0
    there -- but it is read, not assumed.
    """
    return float(
        compute_fee(
            float(price), 1, is_maker=True, series_fee_type=fee_type_for_symbol(symbol)
        ).per_contract
    )


def kelly_edge(
    price: float,
    confidence: float,
    *,
    symbol: str = "",
    historical_wr: float = NEUTRAL_PRIOR_WIN_RATE,
) -> float:
    """``f = p - q/b`` exactly as ``calculate_kelly_size`` computes it.

    Returns ``-inf`` for the two inputs the risk manager rejects before it ever
    reaches the Kelly arithmetic (a price outside ``(0, 1)``, and fees that
    exceed the payoff), and for NaN.  ``f > 0`` is the whole admission test:
    the stage's ``kelly_frac`` is strictly positive, so it scales ``f`` without
    changing its sign, and the final ``max(1, min(quantity, 75))`` guarantees
    at least one contract once ``f`` clears zero.  Admission is therefore
    **balance-independent** -- confirmed against the real risk manager at five
    bankroll stages in the memo's section 2.3.
    """
    p_ = float(price)
    if not np.isfinite(p_) or p_ <= 0.0 or p_ >= 1.0:
        return float("-inf")
    c_ = float(confidence)
    if not np.isfinite(c_):
        return float("-inf")
    fee = fee_per_contract(p_, symbol)
    net_win = (1.0 - p_) - FEE_LEGS * fee
    net_loss = p_ + FEE_LEGS * fee
    if net_win <= 0:
        return float("-inf")
    b = net_win / net_loss
    p = blended_p(c_, historical_wr)
    q = 1.0 - p
    return p - (q / b)


def is_sizable(
    price: float,
    confidence: float,
    *,
    symbol: str = "",
    historical_wr: float = NEUTRAL_PRIOR_WIN_RATE,
) -> bool:
    """Would ``calculate_kelly_size`` return a non-zero size for this row at cold start?

    ``historical_wr`` defaults to the neutral prior, which is what the runtime
    uses until the strategy's win-rate window holds ``MIN_WIN_SAMPLES`` closed
    trades -- i.e. for every trade a freshly promoted genome makes until then.
    """
    return kelly_edge(price, confidence, symbol=symbol, historical_wr=historical_wr) > 0.0


def price_ceiling(
    confidence: float,
    *,
    symbol: str = "",
    historical_wr: float = NEUTRAL_PRIOR_WIN_RATE,
    grid: float = 0.01,
) -> float:
    """Highest price on the cent grid this ``confidence`` can still be sized at.

    ``f > 0`` reduces exactly to ``p > price + 2 * fee_per(price)`` (multiply
    ``p > (1-p) * net_loss / net_win`` out; the ``p * price`` terms cancel), so
    with the standard schedule's zero maker fee the ceiling is simply ``p``.
    Because the fee is itself a function of price on a non-standard series, the
    general answer is found by scanning the grid the exchange actually quotes
    on rather than by inverting a formula.  Returns ``0.0`` when no grid price
    is sizable.
    """
    best = 0.0
    n = int(round(1.0 / grid))
    for i in range(1, n):
        px = i * grid
        if is_sizable(px, confidence, symbol=symbol, historical_wr=historical_wr):
            best = px
    return best


# ---------------------------------------------------------------------------
# vectorised predicate (frames)
# ---------------------------------------------------------------------------
def sizable_mask(
    price: Sequence[float],
    confidence: Sequence[float],
    *,
    symbols: Any = "",
    historical_wr: float = NEUTRAL_PRIOR_WIN_RATE,
) -> np.ndarray:
    """:func:`is_sizable` over arrays -- ``True`` where the runtime would size the row.

    ``symbols`` is a per-row ticker array or one ticker for all rows; it only
    reaches the fee, so rows are grouped by ``(fee_type, price)`` and the
    **scalar** ``fee_calculator`` prices each group.  NaN prices (the frame's
    convention for "that side of the book was empty", never executable) are
    ``False``.
    """
    px = np.asarray(price, dtype=np.float64).ravel()
    conf = np.asarray(confidence, dtype=np.float64).ravel()
    n = px.shape[0]
    if conf.shape[0] != n:
        conf = np.broadcast_to(conf, (n,))
    out = np.zeros(n, dtype=bool)
    if n == 0:
        return out

    valid = np.isfinite(px) & np.isfinite(conf) & (px > 0.0) & (px < 1.0)
    if not valid.any():
        return out

    sym = np.broadcast_to(np.asarray(symbols).astype(str), (n,))
    ftypes = np.asarray([fee_type_for_symbol(str(s)) for s in np.unique(sym)])
    ft_by_sym = {str(s): str(t) for s, t in zip(np.unique(sym), ftypes)}
    row_ft = np.asarray([ft_by_sym[str(s)] for s in sym])

    fee = np.zeros(n, dtype=np.float64)
    for ft in np.unique(row_ft[valid]):
        sel = valid & (row_ft == ft)
        uniq, inv = np.unique(px[sel], return_inverse=True)
        fees = np.asarray(
            [compute_fee(float(u), 1, is_maker=True, series_fee_type=str(ft)).per_contract for u in uniq],
            dtype=np.float64,
        )
        fee[sel] = fees[inv]

    net_win = (1.0 - px) - FEE_LEGS * fee
    net_loss = px + FEE_LEGS * fee
    ok = valid & (net_win > 0)
    if not ok.any():
        return out
    b = np.divide(net_win, net_loss, out=np.ones(n, dtype=np.float64), where=ok)
    p = HISTORICAL_WEIGHT * float(historical_wr) + CONFIDENCE_WEIGHT * conf
    f = p - (1.0 - p) / b
    out[ok] = f[ok] > 0.0
    return out


# ---------------------------------------------------------------------------
# audit over a trade set
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SizingAudit:
    """What the cold-start sizing law does to one offline trade set."""

    label: str
    n_trades: int
    n_sizable: int
    n_dates: int
    n_dates_sizable: int
    sizable_fraction: float
    median_price: float
    median_p_win: float
    #: ``0.6*historical_wr + 0.4*median(p_win)`` -- the price a median-confidence
    #: row must stay strictly below.  Exact wherever the maker fee is zero (every
    #: standard-schedule series, which is every series the factory searches); on a
    #: maker-fee series the true bound is this minus ``2*fee(price)``, so use
    #: :func:`price_ceiling` for the quotable answer there.
    median_ceiling: float
    min_sizable_fraction: float
    historical_wr: float

    @property
    def ok(self) -> bool:
        return self.n_trades > 0 and self.sizable_fraction >= self.min_sizable_fraction

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def summary(self) -> str:
        """One operator-facing line."""
        return (
            f"{self.n_sizable}/{self.n_trades} offline trades sizable "
            f"({self.sizable_fraction:.1%}), {self.n_dates_sizable}/{self.n_dates} target_date units; "
            f"median price_paid {self.median_price:.3f} vs median cold-start ceiling "
            f"{self.median_ceiling:.3f}"
        )

    def refusal(self) -> str:
        """The full refusal text, with the numbers and where to read the finding."""
        return (
            f"{self.label} is mostly unsizable by the runtime that would execute it: "
            f"{self.summary()}. "
            f"RiskManager.calculate_kelly_size blends a neutral {NEUTRAL_PRIOR_WIN_RATE:.2f} prior "
            f"until the strategy has {MIN_WIN_SAMPLES} closed trades, so it sizes a buy only when "
            f"{HISTORICAL_WEIGHT:g}*{NEUTRAL_PRIOR_WIN_RATE:.2f} + {CONFIDENCE_WEIGHT:g}*p_win > "
            f"price_paid + {FEE_LEGS:g}*fee -- everything above that is 0 contracts, not a small size. "
            f"Required: at least {self.min_sizable_fraction:.1%} of the trade set. "
            f"Read {FINDING_REPORT}; reproduce with "
            f"`python scripts/factory_sizing_audit.py --frames <DIR> --genome <ID>`. "
            f"--min-sizable-fraction may raise this bar, never lower it."
        )


def audit_rows(
    price: Sequence[float],
    p_win: Sequence[float],
    *,
    symbols: Any = "",
    date_codes: Optional[Sequence[Any]] = None,
    label: str = "trade set",
    min_fraction: float = MIN_SIZABLE_TRADE_FRACTION,
    historical_wr: float = NEUTRAL_PRIOR_WIN_RATE,
) -> SizingAudit:
    """Audit a trade set given as parallel ``price_paid`` / ``p_win`` arrays."""
    px = np.asarray(price, dtype=np.float64).ravel()
    pw = np.asarray(p_win, dtype=np.float64).ravel()
    n = int(px.shape[0])
    keep = sizable_mask(px, pw, symbols=symbols, historical_wr=historical_wr)
    if date_codes is None:
        n_dates = n_dates_keep = 0
    else:
        dc = np.asarray(date_codes).ravel()
        n_dates = int(np.unique(dc).shape[0])
        n_dates_keep = int(np.unique(dc[keep]).shape[0]) if keep.any() else 0
    med_pw = float(np.median(pw)) if n else float("nan")
    return SizingAudit(
        label=label,
        n_trades=n,
        n_sizable=int(keep.sum()),
        n_dates=n_dates,
        n_dates_sizable=n_dates_keep,
        sizable_fraction=(float(keep.sum()) / n) if n else 0.0,
        median_price=float(np.median(px)) if n else float("nan"),
        median_p_win=med_pw,
        median_ceiling=(blended_p(med_pw, historical_wr) if n and np.isfinite(med_pw) else float("nan")),
        min_sizable_fraction=float(min_fraction),
        historical_wr=float(historical_wr),
    )


def genome_trade_rows(F: Any, genome: Any) -> np.ndarray:
    """The frame rows a genome would actually trade -- ``fitness.score(...).trade_rows``.

    Reproduces the scorer's selection (``mask & executable``, then the first
    surviving row of each market block) without paying for the bootstrap, so
    the promotion guard is cheap.  ``tests/test_factory_sizing.py`` pins it to
    ``fitness.score``.
    """
    from src.factory import genome as G

    mask = np.logical_and(np.asarray(G.to_mask(genome, F), dtype=bool), F.visible["executable"])
    return G.first_true_per_block(mask, F.block_starts)


def audit_genome(
    F: Any,
    genome: Any,
    *,
    label: str = "",
    min_fraction: float = MIN_SIZABLE_TRADE_FRACTION,
    historical_wr: float = NEUTRAL_PRIOR_WIN_RATE,
) -> SizingAudit:
    """Audit the offline trade set a genome produces on a frozen frame (read-only)."""
    rows = genome_trade_rows(F, genome)
    vis = F.visible
    tickers = np.asarray(F.markets)[vis["market_code"][rows]] if rows.shape[0] else np.asarray([], dtype=str)
    return audit_rows(
        vis["price_paid"][rows],
        vis["p_win"][rows],
        symbols=tickers if rows.shape[0] else "",
        date_codes=vis["target_date_code"][rows],
        label=label or str(getattr(genome, "name", "genome")),
        min_fraction=min_fraction,
        historical_wr=historical_wr,
    )


def assert_promotable(
    F: Any,
    genome: Any,
    *,
    label: str = "",
    min_fraction: float = MIN_SIZABLE_TRADE_FRACTION,
) -> SizingAudit:
    """Raise :class:`UnsizableGenomeError` unless enough of the trade set is sizable.

    The guard the F3 review asked for: it is the check that would have refused
    ``0c4b20502f2daf65`` (10.0 % sizable) before it was ever deployed.  Returns
    the audit on success so the caller can print it.
    """
    audit = audit_genome(F, genome, label=label, min_fraction=min_fraction)
    if audit.n_trades == 0:
        raise UnsizableGenomeError(
            f"{audit.label} has no offline trades on {getattr(F, 'name', 'frame')}; "
            "there is nothing for the runtime to size"
        )
    if not audit.ok:
        raise UnsizableGenomeError(audit.refusal())
    return audit


__all__ = [
    "CONFIDENCE_WEIGHT",
    "FEE_LEGS",
    "FINDING_REPORT",
    "HISTORICAL_WEIGHT",
    "MIN_SIZABLE_TRADE_FRACTION",
    "MIN_WIN_SAMPLES",
    "NEUTRAL_PRIOR_WIN_RATE",
    "SizingAudit",
    "UnsizableGenomeError",
    "assert_promotable",
    "audit_genome",
    "audit_rows",
    "blended_p",
    "fee_per_contract",
    "genome_trade_rows",
    "is_sizable",
    "kelly_edge",
    "price_ceiling",
    "sizable_mask",
]
