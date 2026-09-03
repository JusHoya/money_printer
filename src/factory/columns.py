"""Column contract shared by every factory module (PRD_STRATEGY_FACTORY FR-F1.1/F1.2).

This is the ONE place that names the genome-visible and the scorer-only
(hidden) columns of a factory frame, the integer codes for the categorical
columns, and the slim in-memory ``Frame`` container that ``frame.py`` builds,
``genome.to_mask`` reads (visible only) and ``fitness.score`` consumes.

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 4.2
(visible/hidden split), section 5 (fitness), section 3 (GENE_SPEC v1).

Rules
-----
* numpy-only: this module (like ``features.py`` and ``genome.py``) is imported
  by the maia sandbox image, which has no pandas guarantee and no pyarrow.
* Every visible float is float64 and every hidden float is float64. The
  architecture sketch says float32/int16 for the visible block; the 1e-9
  parity requirement against ``ev_analysis.evaluate_shape`` (which is float64
  end to end) makes float32 an unacceptable rounding risk on boundary rows
  (``p_yes <= yes_ask - margin``). ~60 MB for the search frame is fine.
* Categorical codes are ``np.int16`` and are the ONLY way a genome sees a
  category. The label tuples below are ordered; the code is the tuple index.
* ``to_mask`` may reference ``VISIBLE_COLUMNS`` only. Referencing any name in
  ``HIDDEN_COLUMNS`` must fail when the genome/predicate is constructed, not
  when it is evaluated (FR-F1 exit criterion).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Categorical codes (index in the tuple == the int16 code)
# ---------------------------------------------------------------------------
CITY_LABELS: Tuple[str, ...] = ("NY", "CHI", "LAX", "MIA")
DIRECTION_LABELS: Tuple[str, ...] = ("buy_yes", "buy_no")  # ev_analysis.DIRECTIONS order
MODE_LABELS: Tuple[str, ...] = ("taker", "maker")  # NOTE: taker is code 0 (search default)
#: ev_analysis.TIME_WINDOWS order (far from close first); "post_close" never enters a frame
WINDOW_LABELS: Tuple[str, ...] = (">=24h", "12-24h", "6-12h", "3-6h", "1-3h", "<1h")
#: ev_analysis.BAND_LABELS order; binned on distance_f = |midpoint_f - mu_f|
BAND_LABELS: Tuple[str, ...] = ("0-1F", "1-2F", "2-3F", "3-4F", "4-5F", "5F+")
#: Genome-facing coarsening of forecast_calibration.LEAD_BUCKETS (9 buckets):
#:   short  = lead_hours <  12      (calibration bucket "day_of")
#:   medium = 12 <= lead_hours < 60 (buckets lead_12_36, lead_36_60)
#:   long   = lead_hours >= 60      (every longer bucket)
LEAD_BUCKET_LABELS: Tuple[str, ...] = ("short", "medium", "long")
LEAD_BUCKET_EDGES_H: Tuple[int, int] = (12, 60)
STRIKE_TYPE_LABELS: Tuple[str, ...] = ("between", "less", "greater")
#: Hidden ``result_code``: 0 = "no", 1 = "yes", -1 = anything else (unsettled/void)
RESULT_LABELS: Tuple[str, ...] = ("no", "yes")


def code_for(labels: Sequence[str], value: str) -> int:
    """Index of ``value`` in ``labels``; ``-1`` for an unknown label."""
    try:
        return labels.index(value)
    except ValueError:
        return -1


def lead_bucket_code(lead_hours: Any) -> int:
    """short/medium/long code from the vintage lead in hours (LEAD_BUCKET_EDGES_H)."""
    lh = float(lead_hours)
    if lh < LEAD_BUCKET_EDGES_H[0]:
        return 0
    if lh < LEAD_BUCKET_EDGES_H[1]:
        return 1
    return 2


# ---------------------------------------------------------------------------
# Visible (genome-facing) and hidden (scorer-only) columns
# ---------------------------------------------------------------------------
#: name -> numpy dtype. Order is the canonical column order for on-disk frames.
VISIBLE_DTYPES: Dict[str, str] = {
    "city_code": "int16",
    "target_date_code": "int16",  # index into Frame.dates (sorted ISO dates)
    "market_code": "int32",  # index into Frame.markets (sorted tickers)
    "ts_utc": "int64",  # epoch seconds, UTC
    "minutes_to_close": "float64",
    "window_code": "int16",
    "direction_code": "int16",
    "mode_code": "int16",
    "band_code": "int16",
    "lead_bucket_code": "int16",
    "lead_hours": "float64",
    "p_yes": "float64",
    "p_win": "float64",
    "mu_f": "float64",
    "sigma_f": "float64",
    "midpoint_f": "float64",
    "distance_f": "float64",
    "edge_distance_f": "float64",
    "yes_bid": "float64",
    "yes_ask": "float64",
    "no_bid": "float64",
    "no_ask": "float64",
    "last": "float64",
    "price_mean": "float64",
    "volume": "float64",
    "open_interest": "float64",
    "quote": "float64",  # NaN when that side of the book was empty
    "price_paid": "float64",  # quote + adverse_fill; NaN if > 0.99
    "fee_per_contract": "float64",  # fee regime at ts_utc (== evaluator fee on parity)
    "executable": "bool",  # parity: evaluator's; search: evaluator's & sandbox_admissible
    "sandbox_admissible": "bool",  # trade_is_profitable(p_win, price_paid, 1, is_maker=False)
    "floor_strike": "float64",
    "cap_strike": "float64",
    "strike_type_code": "int16",
}
HIDDEN_DTYPES: Dict[str, str] = {
    "won": "bool",
    "realized_per_contract": "float64",  # NaN when not executable (evaluator convention)
    "result_code": "int16",  # RESULT_LABELS; -1 = unsettled/void
    "settles_yes": "bool",
    "expiration_value": "float64",
    "cli_high": "float64",
    "truth_agrees": "int16",  # 1 True, 0 False, -1 None
    "payoff_matches_kalshi": "int16",  # 1 True, 0 False, -1 None
    "maker_yes_fill": "bool",
    "maker_no_fill": "bool",
    "fwd_min_ask": "float64",
    "fwd_max_bid": "float64",
    "yes_bid_low": "float64",
    "yes_ask_high": "float64",
    "ev_per_contract": "float64",  # diagnostics only; NEVER a fitness input
}
VISIBLE_COLUMNS: Tuple[str, ...] = tuple(VISIBLE_DTYPES)
HIDDEN_COLUMNS: Tuple[str, ...] = tuple(HIDDEN_DTYPES)
assert not set(VISIBLE_COLUMNS) & set(HIDDEN_COLUMNS)


class HiddenColumnError(ValueError):
    """A genome/predicate tried to name a scorer-only column."""


def assert_visible(names: Sequence[str]) -> None:
    """Raise HiddenColumnError (hidden) or KeyError (unknown) -- at construction time."""
    for n in names:
        if n in HIDDEN_DTYPES:
            raise HiddenColumnError(f"column {n!r} is hidden from genomes")
        if n not in VISIBLE_DTYPES:
            raise KeyError(f"unknown frame column {n!r}")


# ---------------------------------------------------------------------------
# Frame container
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    """A slim, contiguous, row-aligned numpy frame.

    Rows are sorted by ``(market_code, ts_utc)`` (stable). ``block_starts`` has
    one entry per market block (start row) plus a final sentinel ``n_rows`` so
    block ``i`` is ``rows[block_starts[i]:block_starts[i+1]]`` and every row of
    block ``i`` has ``market_code == i`` (market_code is dense 0..n_markets-1).

    ``twin_index`` (search frame only): for row ``r``, the row in the gefs twin
    frame with the same ``(market_ticker, ts_utc, direction, mode)`` or ``-1``.
    """

    name: str  # "parity" | "search" | "gefs_twin" | test names
    visible: Dict[str, np.ndarray]
    hidden: Dict[str, np.ndarray]
    dates: np.ndarray  # dtype=str (ISO), sorted; target_date_code indexes it
    markets: np.ndarray  # dtype=str, sorted; market_code indexes it
    block_starts: np.ndarray  # int64, len n_markets + 1
    provenance: Dict[str, Any] = field(default_factory=dict)
    twin_index: Optional[np.ndarray] = None

    @property
    def n_rows(self) -> int:
        return int(self.visible["market_code"].shape[0])

    @property
    def n_markets(self) -> int:
        return int(self.markets.shape[0])

    @property
    def n_dates(self) -> int:
        return int(self.dates.shape[0])

    def col(self, name: str) -> np.ndarray:
        """Visible column access for genomes; hidden names raise HiddenColumnError."""
        assert_visible((name,))
        return self.visible[name]

    def validate(self) -> None:
        n = self.n_rows
        for name, dt in VISIBLE_DTYPES.items():
            a = self.visible[name]
            if a.shape != (n,) or a.dtype != np.dtype(dt):
                raise ValueError(
                    f"visible {name}: shape {a.shape} dtype {a.dtype}, want ({n},) {dt}"
                )
        for name, dt in HIDDEN_DTYPES.items():
            a = self.hidden[name]
            if a.shape != (n,) or a.dtype != np.dtype(dt):
                raise ValueError(
                    f"hidden {name}: shape {a.shape} dtype {a.dtype}, want ({n},) {dt}"
                )
        mc = self.visible["market_code"]
        if n and np.any(np.diff(mc) < 0):
            raise ValueError("rows not sorted by market_code")
        ts = self.visible["ts_utc"]
        if n:
            same = np.diff(mc) == 0
            if np.any(np.diff(ts)[same] < 0):
                raise ValueError("rows not sorted by ts_utc within market blocks")
        if self.block_starts.shape != (self.n_markets + 1,) or (
            n and self.block_starts[-1] != n
        ):
            raise ValueError("block_starts malformed")
        if self.twin_index is not None and self.twin_index.shape != (n,):
            raise ValueError("twin_index malformed")


def row_view(frame: Frame, i: int) -> Dict[str, Any]:
    """The visible columns of one row as scalars -- the shape a live sandbox row has.

    ``genome.to_mask`` must produce the same answer on ``row_view(F, i)`` as
    ``to_mask(genome, F)[i]``; that is the lab/sandbox parity contract.
    """
    return {name: frame.visible[name][i] for name in VISIBLE_COLUMNS}


class VisibleOnly(Mapping):
    """Read-only mapping exposing ONLY the visible columns of a frame or row dict."""

    def __init__(self, source: Mapping):
        self._src = source

    def __getitem__(self, key: str) -> Any:
        assert_visible((key,))
        return self._src[key]

    def __iter__(self):
        return iter(VISIBLE_COLUMNS)

    def __len__(self) -> int:
        return len(VISIBLE_COLUMNS)
