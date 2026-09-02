"""Fee regime as data (FACTORY_ARCHITECTURE section 4.2 item 6; PRD FR-F1.1).

``configs/fees/fee_regime.csv`` is the time-indexed record of what Kalshi
bills per series -- one row per ``(series_prefix, effective_from_utc)``:

    series_prefix,effective_from_utc,fee_type,taker_multiplier,maker_multiplier,source_note

``fee_type`` is the API's ``series.fee_type`` (``quadratic`` = standard
schedule, maker $0; ``quadratic_with_maker_fees`` = resting liquidity is
billed). ``taker_multiplier`` / ``maker_multiplier`` are the API's
``fee_multiplier`` applied to the taker and maker quadratic respectively
(the maker one is irrelevant, and recorded as 0, under the standard
schedule). ``source_note`` names where the number came from; a value that is
not recorded anywhere is the ``fee_calculator`` default and says so.

The regime is matched by **longest series prefix** (``KXHIGH`` covers
``KXHIGHNY``/``KXHIGHCHI``/...; ``KXAAAGASM`` beats a hypothetical
``KXAAAGAS`` row) and by the latest ``effective_from_utc <= ts_utc``. A
series with no matching row, or a timestamp before its first row, raises
:class:`FeeRegimeError` -- silently pricing an unknown series at the default
is exactly the optimistic-fee failure both HALTs were built on.

``fee_per_contract`` reproduces ``ev_analysis._vector_fee`` bit-for-bit for
the KXHIGH regime: the price is rounded to 4 decimals (lossless on Kalshi's
cent grid) and the fee is computed by the *scalar*
``fee_calculator.taker_fee``/``maker_fee`` on the unique prices -- so the
ceil-to-cent rounding is the sandbox's own, not a numpy re-implementation.
"""
from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.core.fee_calculator import (
    FEE_TYPE_STANDARD,
    FEE_TYPE_WITH_MAKER_FEES,
    maker_fee,
    taker_fee,
)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
#: The tracked regime file (section 8 of the architecture).
DEFAULT_REGIME_PATH = os.path.join(REPO_ROOT, "configs", "fees", "fee_regime.csv")

REGIME_COLUMNS: Tuple[str, ...] = (
    "series_prefix",
    "effective_from_utc",
    "fee_type",
    "taker_multiplier",
    "maker_multiplier",
    "source_note",
)
_FEE_TYPES = (FEE_TYPE_STANDARD, FEE_TYPE_WITH_MAKER_FEES)


class FeeRegimeError(RuntimeError):
    """The regime file is malformed or does not cover a (series, ts) that was priced."""


def _parse_utc(value: str) -> int:
    """ISO-8601 UTC string -> epoch seconds (``Z`` or ``+00:00`` accepted)."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


@dataclass(frozen=True)
class RegimeRow:
    series_prefix: str
    effective_from_epoch: int
    fee_type: str
    taker_multiplier: float
    maker_multiplier: float
    source_note: str


@dataclass(frozen=True)
class FeeRegime:
    """The parsed regime: rows grouped by prefix, each group sorted by effective time."""

    path: str
    rows: Tuple[RegimeRow, ...]
    sha256: str

    @property
    def prefixes(self) -> Tuple[str, ...]:
        return tuple(sorted({r.series_prefix for r in self.rows}, key=len, reverse=True))

    def match_prefix(self, series: str) -> Optional[str]:
        """Longest ``series_prefix`` that ``series`` starts with, or ``None``."""
        s = (series or "").strip().upper()
        for p in self.prefixes:
            if s.startswith(p):
                return p
        return None

    def rows_for(self, prefix: str) -> List[RegimeRow]:
        return sorted(
            (r for r in self.rows if r.series_prefix == prefix),
            key=lambda r: r.effective_from_epoch,
        )

    def lookup(self, series: str, ts_epoch: int) -> RegimeRow:
        """The regime row governing ``series`` at ``ts_epoch``; raises when uncovered."""
        prefix = self.match_prefix(series)
        if prefix is None:
            raise FeeRegimeError(
                f"no fee regime row covers series {series!r} in {self.path}; add one "
                "with its /series record rather than pricing it at a default"
            )
        rows = self.rows_for(prefix)
        eff = np.asarray([r.effective_from_epoch for r in rows], dtype=np.int64)
        i = int(np.searchsorted(eff, int(ts_epoch), side="right")) - 1
        if i < 0:
            raise FeeRegimeError(
                f"series {series!r} at epoch {int(ts_epoch)} predates the first regime row "
                f"({rows[0].effective_from_epoch}) in {self.path}"
            )
        return rows[i]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_regime(path: str = DEFAULT_REGIME_PATH) -> FeeRegime:
    """Parse and validate the regime CSV."""
    if not os.path.exists(path):
        raise FeeRegimeError(f"fee regime file missing: {path}")
    rows: List[RegimeRow] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != REGIME_COLUMNS:
            raise FeeRegimeError(
                f"{path}: columns {reader.fieldnames} != {list(REGIME_COLUMNS)}"
            )
        for n, rec in enumerate(reader, start=2):
            prefix = rec["series_prefix"].strip().upper()
            fee_type = rec["fee_type"].strip()
            if not prefix:
                raise FeeRegimeError(f"{path}:{n}: empty series_prefix")
            if fee_type not in _FEE_TYPES:
                raise FeeRegimeError(f"{path}:{n}: fee_type {fee_type!r} not in {_FEE_TYPES}")
            try:
                tm = float(rec["taker_multiplier"])
                mm = float(rec["maker_multiplier"])
            except ValueError as exc:
                raise FeeRegimeError(f"{path}:{n}: bad multiplier: {exc}") from exc
            if tm < 0 or mm < 0:
                raise FeeRegimeError(f"{path}:{n}: negative multiplier")
            rows.append(
                RegimeRow(
                    series_prefix=prefix,
                    effective_from_epoch=_parse_utc(rec["effective_from_utc"]),
                    fee_type=fee_type,
                    taker_multiplier=tm,
                    maker_multiplier=mm,
                    source_note=rec["source_note"].strip(),
                )
            )
    if not rows:
        raise FeeRegimeError(f"{path}: no regime rows")
    keys = [(r.series_prefix, r.effective_from_epoch) for r in rows]
    if len(set(keys)) != len(keys):
        raise FeeRegimeError(f"{path}: duplicate (series_prefix, effective_from_utc) rows")
    return FeeRegime(path=path, rows=tuple(rows), sha256=sha256_file(path))


def regime_sha256(path: str = DEFAULT_REGIME_PATH) -> str:
    """sha256 of the regime file bytes (provenance input)."""
    return sha256_file(path)


def _scalar_fee(price: float, contracts: int, is_maker: bool, row: RegimeRow) -> float:
    """Per-contract entry fee via the scalar schedule -- ``ev_analysis.fee_per_contract``'s division."""
    if is_maker:
        total = maker_fee(price, contracts, row.fee_type, row.maker_multiplier)
    else:
        total = taker_fee(price, contracts, row.taker_multiplier)
    return total / float(contracts)


def fee_per_contract(
    price_paid: Any,
    ts_utc_epoch: Any,
    series_prefix: Any,
    contracts: int = 20,
    is_maker: Any = False,
    regime: Optional[FeeRegime] = None,
) -> np.ndarray:
    """Entry fee per contract for every row, from the regime at each row's ``ts_utc``.

    Parameters
    ----------
    price_paid : array-like float
        Price after the adverse-fill allowance; NaN rows return NaN.
    ts_utc_epoch : array-like int
        Epoch seconds UTC (broadcast if scalar).
    series_prefix : array-like str or str
        Series ticker per row (``KXHIGHNY`` ...) or a single one for all rows;
        matched to the regime by longest prefix.
    contracts : int
        Order size the cent-ceiling is applied on (evaluator: 20).
    is_maker : array-like bool or bool
        Maker path per row (broadcast if scalar).
    regime : FeeRegime, optional
        Defaults to :func:`load_regime` on ``DEFAULT_REGIME_PATH``.

    Returns
    -------
    numpy.ndarray of float64, same length as ``price_paid``.
    """
    if contracts <= 0:
        raise FeeRegimeError(f"contracts must be positive, got {contracts}")
    reg = regime if regime is not None else load_regime()
    price = np.asarray(price_paid, dtype=np.float64).ravel()
    n = price.shape[0]
    ts = np.broadcast_to(np.asarray(ts_utc_epoch, dtype=np.int64), (n,))
    maker = np.broadcast_to(np.asarray(is_maker, dtype=bool), (n,))
    ser = np.broadcast_to(np.asarray(series_prefix).astype(str), (n,))

    out = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return out
    # Evaluator convention (_vector_fee): price rounded to 4 dp before the fee.
    price4 = np.round(price, 4)
    valid = ~np.isnan(price4)

    uniq_series, ser_inv = np.unique(ser, return_inverse=True)
    for si, series in enumerate(uniq_series):
        sel = (ser_inv == si) & valid
        if not sel.any():
            continue
        prefix = reg.match_prefix(str(series))
        if prefix is None:
            raise FeeRegimeError(
                f"no fee regime row covers series {series!r} in {reg.path}; add one "
                "with its /series record rather than pricing it at a default"
            )
        rows = reg.rows_for(prefix)
        eff = np.asarray([r.effective_from_epoch for r in rows], dtype=np.int64)
        idx = np.searchsorted(eff, ts[sel], side="right") - 1
        if (idx < 0).any():
            raise FeeRegimeError(
                f"{int((idx < 0).sum())} row(s) of series {series!r} predate the first "
                f"regime row ({rows[0].effective_from_epoch}) in {reg.path}"
            )
        sel_idx = np.flatnonzero(sel)
        for ri in np.unique(idx):
            row = rows[int(ri)]
            for mk in (False, True):
                m = (idx == ri) & (maker[sel] == mk)
                if not m.any():
                    continue
                rows_here = sel_idx[m]
                uniq_p, inv = np.unique(price4[rows_here], return_inverse=True)
                fees = np.asarray(
                    [_scalar_fee(float(p), contracts, mk, row) for p in uniq_p],
                    dtype=np.float64,
                )
                out[rows_here] = fees[inv]
    return out


def describe(regime: Optional[FeeRegime] = None) -> Dict[str, Any]:
    """JSON-ready summary of the regime for provenance / the board."""
    reg = regime if regime is not None else load_regime()
    return {
        "path": os.path.relpath(reg.path, REPO_ROOT).replace(os.sep, "/"),
        "sha256": reg.sha256,
        "rows": [
            {
                "series_prefix": r.series_prefix,
                "effective_from_epoch": r.effective_from_epoch,
                "fee_type": r.fee_type,
                "taker_multiplier": r.taker_multiplier,
                "maker_multiplier": r.maker_multiplier,
            }
            for r in reg.rows
        ],
    }


__all__ = [
    "DEFAULT_REGIME_PATH",
    "FeeRegime",
    "FeeRegimeError",
    "REGIME_COLUMNS",
    "RegimeRow",
    "describe",
    "fee_per_contract",
    "load_regime",
    "regime_sha256",
]
