"""Genome-visible feature derivations -- the ONE implementation shared by the lab and the sandbox.

``frame.py`` (lab) and ``GenomeStrategy`` (maia, F3) must derive every
genome-facing column the same way, or the lab/sandbox parity contract
(``columns.row_view``) is broken silently. This module is that single source
of truth (``docs/factory/FACTORY_ARCHITECTURE.md`` section 1.1, section 4.2).

Rules
-----
* **numpy-only.** No pandas, no scipy, and no ``src.backtest.ev_analysis``
  (which imports pandas). The evaluator constants this module needs
  (``BAND_EDGES``, ``TIME_WINDOWS``, ``MAX_ORDERABLE_PRICE``,
  ``ADVERSE_FILL_DOLLARS``) are *copied* here and pinned by
  ``tests/test_factory_frame.py`` against the evaluator's own values.
  ``src.core.fee_calculator`` is pure ``math`` and is imported directly.
* **Vectorised and scalar-safe.** Every function accepts numpy arrays *or* a
  Python/numpy scalar (the shape a live row from ``columns.row_view`` has)
  and returns the same kind: an array for array input, a scalar for scalar
  input. ``genome.to_mask`` relies on this to run the same code on a frame
  and on a single row.
* **Bit-exact to the evaluator.** ``window_code``/``band_code`` are the tuple
  index of ``ev_analysis.time_window_label``/``band_label``; ``quote`` and
  ``price_paid`` are ``build_opportunity_frame``'s rules including the
  empty-book sentinels; ``sandbox_admissible`` is
  ``fee_calculator.trade_is_profitable(p, price, 1, is_maker=False)`` -- the
  maia EV gate -- evaluated through the scalar fee function on the unique
  prices so the ceil-to-cent fee is identical to the last ULP.
"""
from __future__ import annotations

import math
from typing import Any, Tuple

import numpy as np

from src.core.fee_calculator import taker_fee
from src.factory.columns import (
    BAND_LABELS,
    LEAD_BUCKET_EDGES_H,
    LEAD_BUCKET_LABELS,
    WINDOW_LABELS,
)

# ---------------------------------------------------------------------------
# Evaluator constants (copied; pinned by tests against ev_analysis)
# ---------------------------------------------------------------------------
#: ``ev_analysis.BAND_EDGES`` -- bracket-distance band edges in degF; final band open.
BAND_EDGES: Tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
#: ``ev_analysis.TIME_WINDOWS`` (name, lo_min, hi_min), far-from-close first.
TIME_WINDOWS: Tuple[Tuple[str, float, float], ...] = (
    (">=24h", 24 * 60, math.inf),
    ("12-24h", 12 * 60, 24 * 60),
    ("6-12h", 6 * 60, 12 * 60),
    ("3-6h", 3 * 60, 6 * 60),
    ("1-3h", 60, 3 * 60),
    ("<1h", 0.0, 60),
)
#: ``ev_analysis.MAX_ORDERABLE_PRICE`` -- Kalshi's grid is whole cents in [0.01, 0.99].
MAX_ORDERABLE_PRICE: float = 0.99
#: ``ev_analysis.ADVERSE_FILL_DOLLARS`` -- EC-5's 1c adverse-fill allowance.
ADVERSE_FILL_DOLLARS: float = 0.01
#: Sandbox EV gate legs (mixins.py:291-318 prices two taker legs at C=1) --
#: ``fee_calculator.ev_after_fees(..., exit_mode="trade_out")``.
SANDBOX_GATE_LEGS: int = 2
SANDBOX_GATE_CONTRACTS: int = 1

assert tuple(n for n, _, _ in TIME_WINDOWS) == WINDOW_LABELS
assert len(BAND_EDGES) == len(BAND_LABELS)

# Ascending lower edges of the time windows, in WINDOW_LABELS reverse order:
# [0, 60, 180, 360, 720, 1440]. searchsorted(side="right") - 1 gives the
# ascending slot; the code is (n_windows - 1 - slot).
_WINDOW_LO_ASC = np.asarray([lo for _, lo, _ in reversed(TIME_WINDOWS)], dtype=np.float64)
_BAND_INNER_EDGES = np.asarray(BAND_EDGES[1:], dtype=np.float64)
_LEAD_EDGES = np.asarray(LEAD_BUCKET_EDGES_H, dtype=np.float64)
assert len(LEAD_BUCKET_LABELS) == len(LEAD_BUCKET_EDGES_H) + 1


# ---------------------------------------------------------------------------
# scalar / array plumbing
# ---------------------------------------------------------------------------
def _as_f64(x: Any) -> Tuple[np.ndarray, bool]:
    """``(array, was_scalar)``; ``None`` becomes NaN so scalars from a live row work."""
    if x is None:
        return np.asarray(np.nan, dtype=np.float64), True
    a = np.asarray(x)
    scalar = a.ndim == 0
    if a.dtype == object:
        a = np.asarray([np.nan if v is None else v for v in np.atleast_1d(a)], dtype=np.float64)
        if scalar:
            a = a[0]
    return np.asarray(a, dtype=np.float64), scalar


def _ret(out: np.ndarray, scalar: bool):
    """Return a numpy scalar for scalar input, the array otherwise."""
    return out[()] if scalar else out


# ---------------------------------------------------------------------------
# Categorical codes
# ---------------------------------------------------------------------------
def window_code(minutes_to_close: Any):
    """Index in ``WINDOW_LABELS`` of ``ev_analysis.time_window_label``; ``-1`` = post_close.

    ``[lo, hi)`` half-open bins; negative or NaN minutes are "post_close" and
    never enter a frame (the evaluator drops ``minutes_to_close <= 0``, but 0
    itself is ``<1h`` per the label function and is coded 5 here for parity).
    """
    m, scalar = _as_f64(minutes_to_close)
    slot = np.searchsorted(_WINDOW_LO_ASC, m, side="right") - 1
    code = (len(TIME_WINDOWS) - 1 - slot).astype(np.int16)
    bad = np.isnan(m) | (m < 0.0)
    code = np.where(bad, np.int16(-1), code).astype(np.int16)
    return _ret(code, scalar)


def band_code(distance_f: Any):
    """Index in ``BAND_LABELS`` of ``ev_analysis.band_label(distance_f)``; NaN -> ``-1``."""
    d, scalar = _as_f64(distance_f)
    code = np.searchsorted(_BAND_INNER_EDGES, d, side="right").astype(np.int16)
    code = np.where(np.isnan(d), np.int16(-1), code).astype(np.int16)
    return _ret(code, scalar)


def lead_bucket_code(lead_hours: Any):
    """``columns.lead_bucket_code`` vectorised: short (<12h) / medium (<60h) / long."""
    lh, scalar = _as_f64(lead_hours)
    code = np.searchsorted(_LEAD_EDGES, lh, side="right").astype(np.int16)
    return _ret(code, scalar)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def quote(yes_bid: Any, yes_ask: Any, direction_code: Any, mode_code: Any):
    """The price a trade shape must hit -- ``build_opportunity_frame``'s rule, NaN if absent.

    ``direction_code``: 0 = buy_yes, 1 = buy_no; ``mode_code``: 0 = taker,
    1 = maker (``columns.DIRECTION_LABELS`` / ``MODE_LABELS``).

    * taker YES pays ``yes_ask`` (needs ``yes_ask < 1``)
    * taker NO  pays ``1 - yes_bid`` (needs ``yes_bid > 0``)
    * maker YES pays ``yes_bid`` (needs ``yes_bid > 0``)
    * maker NO  pays ``1 - yes_ask`` (needs ``yes_ask < 1``)

    ``yes_bid == 0`` / ``yes_ask == 1`` are Kalshi's empty-book sentinels and
    yield NaN, exactly as the evaluator's ``.where`` masks do.
    """
    bid, s1 = _as_f64(yes_bid)
    ask, s2 = _as_f64(yes_ask)
    dc = np.asarray(direction_code)
    mc = np.asarray(mode_code)
    scalar = s1 and s2 and dc.ndim == 0 and mc.ndim == 0
    bid, ask, dc, mc = np.broadcast_arrays(bid, ask, dc, mc)
    has_ask = ask < 1.0  # NaN compares False
    has_bid = bid > 0.0
    is_no = dc == 1
    is_maker = mc == 1
    use_ask = is_no == is_maker  # (yes,taker) or (no,maker) read the ask
    px_from_ask = np.where(is_no, 1.0 - ask, ask)
    px_from_bid = np.where(is_no, 1.0 - bid, bid)
    out = np.where(
        use_ask,
        np.where(has_ask, px_from_ask, np.nan),
        np.where(has_bid, px_from_bid, np.nan),
    ).astype(np.float64)
    return _ret(out, scalar)


def price_paid(quote_: Any, adverse_fill: float = ADVERSE_FILL_DOLLARS):
    """``round(quote + adverse_fill, 10)``; NaN when that leaves the orderable grid.

    Mirrors ``ev_analysis.adverse_fill_price`` / the frame's vectorised form:
    ``> MAX_ORDERABLE_PRICE + 1e-12`` -> NaN (the frame does not apply the
    ``<= 0`` branch because a quote is never negative; neither does this).
    """
    q, scalar = _as_f64(quote_)
    p = np.round(q + float(adverse_fill), 10)
    p = np.where(p > MAX_ORDERABLE_PRICE + 1e-12, np.nan, p).astype(np.float64)
    return _ret(p, scalar)


def far_margin_value(p_yes: Any, yes_bid: Any, yes_ask: Any, direction_code: Any):
    """The fr31a "far margin": how far the *market* is beyond the model on the traded side.

    * buy NO : ``yes_ask - p_yes`` (requires ``yes_ask < 1``, else NaN)
    * buy YES: ``p_yes - yes_bid`` (requires ``yes_bid > 0``, else NaN)

    ``fr31a_mask`` is ``p_yes <= yes_ask - margin`` on the NO side, i.e.
    ``far_margin_value >= margin``. The YES form is the mirror image.
    """
    p, s1 = _as_f64(p_yes)
    bid, s2 = _as_f64(yes_bid)
    ask, s3 = _as_f64(yes_ask)
    dc = np.asarray(direction_code)
    scalar = s1 and s2 and s3 and dc.ndim == 0
    p, bid, ask, dc = np.broadcast_arrays(p, bid, ask, dc)
    is_no = dc == 1
    no_val = np.where(ask < 1.0, ask - p, np.nan)
    yes_val = np.where(bid > 0.0, p - bid, np.nan)
    out = np.where(is_no, no_val, yes_val).astype(np.float64)
    return _ret(out, scalar)


# ---------------------------------------------------------------------------
# Sandbox admissibility (the maia EV gate)
# ---------------------------------------------------------------------------
def _taker_fee_per_price(price: np.ndarray, contracts: int) -> np.ndarray:
    """``fee_calculator.taker_fee`` over an array, via the scalar function on unique prices.

    The ceil-to-cent fee is computed by the *same* scalar code path the
    sandbox uses, so there is no numpy-vs-Python rounding drift; NaN prices
    map to NaN.
    """
    flat = np.asarray(price, dtype=np.float64).ravel()
    out = np.full(flat.shape, np.nan, dtype=np.float64)
    ok = ~np.isnan(flat)
    if ok.any():
        uniq, inv = np.unique(flat[ok], return_inverse=True)
        fees = np.asarray([taker_fee(float(u), contracts) for u in uniq], dtype=np.float64)
        out[ok] = fees[inv]
    return out.reshape(np.shape(price))


def sandbox_admissible(p_win: Any, price_paid_: Any):
    """``trade_is_profitable(p_win, price_paid, contracts=1, is_maker=False)`` vectorised.

    The maia EV gate (``mixins.py``): two taker legs at C=1,
    ``p_win - price - 2 * taker_fee(price, 1) > 0``. Same float operations in
    the same order as ``fee_calculator.ev_after_fees`` so the answer is
    bit-identical to the scalar function; NaN price -> False.
    """
    p, s1 = _as_f64(p_win)
    px, s2 = _as_f64(price_paid_)
    scalar = s1 and s2
    p, px = np.broadcast_arrays(p, px)
    fee1 = _taker_fee_per_price(px, SANDBOX_GATE_CONTRACTS)
    with np.errstate(invalid="ignore"):
        ev = (p - px) - SANDBOX_GATE_LEGS * fee1
        out = np.asarray(ev > 0, dtype=bool)
    return _ret(out, scalar)


__all__ = [
    "ADVERSE_FILL_DOLLARS",
    "BAND_EDGES",
    "MAX_ORDERABLE_PRICE",
    "SANDBOX_GATE_CONTRACTS",
    "SANDBOX_GATE_LEGS",
    "TIME_WINDOWS",
    "band_code",
    "far_margin_value",
    "lead_bucket_code",
    "price_paid",
    "quote",
    "sandbox_admissible",
    "window_code",
]
