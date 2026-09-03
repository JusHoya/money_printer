"""Campaign calendar, blocked k-fold and row stripping for factory frames.

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 6.1 (anchored
walk-forward campaigns, 2-day embargo -- the headline), 6.2 (blocked 5-fold
with a 2-day purge -- diagnostic only) and PRD_STRATEGY_FACTORY FR-F1.4.

The 69 development dates are 2026-05-18..2026-07-25 (every calendar day; one
ladder file per city per day under ``data/ladders/``). Campaign windows are
declared on the CALENDAR and intersected with the frame's actual dates by
:func:`campaigns`, which asserts the 30/12, 44/12, 58/9 and 69 counts whenever
the frame carries the full development window.

Workers must never see validation or embargo rows: :func:`strip_rows` and
:func:`strip_pair` PHYSICALLY remove rows and re-densify ``market_code`` /
``target_date_code`` so the stripped frame still passes ``Frame.validate()``
(block ``i`` <-> ``market_code i``). The removed dates are recorded in
``provenance["stripped_dates"]``; the parent's date list is kept in
``provenance["parent_dates"]`` so per-date vectors can be re-aligned.

numpy-only (no pandas) so it can be imported wherever ``columns.py`` can.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from src.factory.columns import Frame

DEV_START = "2026-05-18"
DEV_END = "2026-07-25"


def date_range(start: str, end: str) -> Tuple[str, ...]:
    """Inclusive tuple of ISO calendar dates from ``start`` to ``end``."""
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    if d1 < d0:
        raise ValueError(f"date_range: end {end} before start {start}")
    return tuple((d0 + _dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1))


DEV_DATES: Tuple[str, ...] = date_range(DEV_START, DEV_END)
assert len(DEV_DATES) == 69


@dataclass(frozen=True)
class Campaign:
    """One anchored walk-forward campaign (section 6.1)."""

    name: str
    search_dates: Tuple[str, ...]
    embargo_dates: Tuple[str, ...]
    validation_dates: Tuple[str, ...]

    @property
    def worker_dates(self) -> Tuple[str, ...]:
        """Dates a worker frame may contain: the search window only."""
        return self.search_dates

    @property
    def stripped_dates(self) -> Tuple[str, ...]:
        """Dates physically absent from worker frames (embargo + validation)."""
        return tuple(sorted(set(self.embargo_dates) | set(self.validation_dates)))

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "search_dates": list(self.search_dates),
            "embargo_dates": list(self.embargo_dates),
            "validation_dates": list(self.validation_dates),
        }


#: Section 6.1, verbatim. Search windows are anchored at DEV_START.
CAMPAIGN_CALENDAR: Dict[str, Campaign] = {
    "A": Campaign(
        "A",
        date_range("2026-05-18", "2026-06-16"),
        ("2026-06-17", "2026-06-18"),
        date_range("2026-06-19", "2026-06-30"),
    ),
    "B": Campaign(
        "B",
        date_range("2026-05-18", "2026-06-30"),
        ("2026-07-01", "2026-07-02"),
        date_range("2026-07-03", "2026-07-14"),
    ),
    "C": Campaign(
        "C",
        date_range("2026-05-18", "2026-07-14"),
        ("2026-07-15", "2026-07-16"),
        date_range("2026-07-17", "2026-07-25"),
    ),
    "ALL69": Campaign("ALL69", date_range("2026-05-18", "2026-07-25"), (), ()),
}

#: (search, validation) counts on the full development window.
EXPECTED_COUNTS: Dict[str, Tuple[int, int]] = {
    "A": (30, 12),
    "B": (44, 12),
    "C": (58, 9),
    "ALL69": (69, 0),
}
for _name, (_s, _v) in EXPECTED_COUNTS.items():
    assert len(CAMPAIGN_CALENDAR[_name].search_dates) == _s, _name
    assert len(CAMPAIGN_CALENDAR[_name].validation_dates) == _v, _name


def campaigns(frame_dates: Sequence[str]) -> Dict[str, Campaign]:
    """Intersect :data:`CAMPAIGN_CALENDAR` with the frame's actual dates.

    Dates outside ``DEV_START..DEV_END`` are refused (the search frame must
    respect the cutoff; a post-cutoff date here means the frame is wrong, not
    the calendar). When the frame carries the full 69-date development window
    the 30/12, 44/12, 58/9, 69 counts are asserted.
    """
    present = sorted(set(str(d) for d in frame_dates))
    outside = [d for d in present if d < DEV_START or d > DEV_END]
    if outside:
        raise ValueError(
            f"campaigns: {len(outside)} frame date(s) outside the development "
            f"window {DEV_START}..{DEV_END}: {outside[:5]}"
        )
    pset = set(present)
    out: Dict[str, Campaign] = {}
    for name, cal in CAMPAIGN_CALENDAR.items():
        out[name] = Campaign(
            name,
            tuple(d for d in cal.search_dates if d in pset),
            tuple(d for d in cal.embargo_dates if d in pset),
            tuple(d for d in cal.validation_dates if d in pset),
        )
    if pset == set(DEV_DATES):
        for name, (n_s, n_v) in EXPECTED_COUNTS.items():
            c = out[name]
            if len(c.search_dates) != n_s or len(c.validation_dates) != n_v:
                raise AssertionError(
                    f"campaign {name}: expected {n_s}/{n_v} search/validation dates, "
                    f"got {len(c.search_dates)}/{len(c.validation_dates)}"
                )
    return out


# ---------------------------------------------------------------------------
# Blocked k-fold with calendar purge (section 6.2, diagnostic only)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Fold:
    """One blocked fold: ``held`` scored once; ``purge`` and ``held`` absent from workers."""

    index: int
    held: Tuple[str, ...]
    purge: Tuple[str, ...]
    train: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "held": list(self.held),
            "purge": list(self.purge),
            "train": list(self.train),
        }


def _within_days(a: str, b: str, days: int) -> bool:
    da = _dt.date.fromisoformat(a)
    db = _dt.date.fromisoformat(b)
    return 0 < abs((da - db).days) <= days


def blocked_kfold(dates: Sequence[str], k: int = 5, purge_days: int = 2) -> List[Fold]:
    """Contiguous blocks (13-14 dates for 69 / 5) with a calendar purge.

    ``purge`` = the dates lying within ``purge_days`` CALENDAR days before the
    block's first date or after its last date (so a purge day that is missing
    from the frame simply has nothing to remove). ``train`` = every other date.
    """
    ds = sorted(set(str(d) for d in dates))
    if k < 2 or k > len(ds):
        raise ValueError(f"blocked_kfold: k={k} invalid for {len(ds)} dates")
    blocks = [list(b) for b in np.array_split(np.array(ds, dtype=object), k)]
    folds: List[Fold] = []
    for i, held in enumerate(blocks):
        held_t = tuple(held)
        first, last = held_t[0], held_t[-1]
        purge = tuple(
            d
            for d in ds
            if d not in held_t
            and (
                (d < first and _within_days(d, first, purge_days))
                or (d > last and _within_days(d, last, purge_days))
            )
        )
        drop = set(held_t) | set(purge)
        train = tuple(d for d in ds if d not in drop)
        folds.append(Fold(i, held_t, purge, train))
    return folds


# ---------------------------------------------------------------------------
# Row membership and physical stripping
# ---------------------------------------------------------------------------
def date_mask(F: Frame, dates: Iterable[str]) -> np.ndarray:
    """Boolean row mask: rows whose target date is in ``dates`` (via ``F.dates``)."""
    wanted = np.asarray(sorted(set(str(d) for d in dates)), dtype=str)
    if wanted.size == 0:
        return np.zeros(F.n_rows, dtype=bool)
    date_in = np.isin(F.dates.astype(str), wanted)
    return date_in[F.visible["target_date_code"]]


def _densify(codes: np.ndarray, dtype: str) -> Tuple[np.ndarray, np.ndarray]:
    """Remap arbitrary non-negative codes to 0..k-1; returns (new_codes, surviving_old)."""
    surviving = np.unique(codes)
    new = np.searchsorted(surviving, codes).astype(dtype)
    return new, surviving


def strip_rows(
    F: Frame,
    keep: np.ndarray,
    *,
    stripped_dates: Optional[Sequence[str]] = None,
    twin_row_map: Optional[np.ndarray] = None,
) -> Frame:
    """Physically remove the rows where ``keep`` is False.

    The result passes ``Frame.validate()``: ``market_code`` is re-densified over
    the surviving markets (``F.markets`` sliced accordingly) and ``block_starts``
    rebuilt; ``target_date_code`` is re-densified over the surviving dates
    (``F.dates`` sliced). Row order is preserved, so the (market, ts) sort
    survives.

    ``twin_index`` handling: if ``twin_row_map`` is given (old twin row -> new
    twin row, -1 when dropped) it is applied; otherwise the sliced values still
    refer to the UNSTRIPPED twin and ``provenance["twin_index_refers_to"]`` says
    so. Use :func:`strip_pair` to strip a search frame and its gefs twin
    consistently.
    """
    keep = np.asarray(keep, dtype=bool)
    if keep.shape != (F.n_rows,):
        raise ValueError(f"strip_rows: keep has shape {keep.shape}, frame has {F.n_rows} rows")
    visible = {name: arr[keep] for name, arr in F.visible.items()}
    hidden = {name: arr[keep] for name, arr in F.hidden.items()}
    n = int(keep.sum())

    old_mc = visible["market_code"]
    new_mc, surviving_markets = _densify(old_mc, "int32")
    visible["market_code"] = new_mc
    markets = F.markets[surviving_markets] if surviving_markets.size else F.markets[:0]
    k = int(surviving_markets.size)
    counts = np.bincount(new_mc, minlength=k) if n else np.zeros(k, dtype=np.int64)
    block_starts = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)

    old_dc = visible["target_date_code"]
    new_dc, surviving_dates = _densify(old_dc, "int16")
    visible["target_date_code"] = new_dc
    dates = F.dates[surviving_dates] if surviving_dates.size else F.dates[:0]

    twin_index = None
    prov = dict(F.provenance)
    if F.twin_index is not None:
        ti = F.twin_index[keep]
        if twin_row_map is not None:
            mapped = np.full(ti.shape, -1, dtype=ti.dtype)
            ok = ti >= 0
            mapped[ok] = twin_row_map[ti[ok]]
            twin_index = mapped
            prov.pop("twin_index_refers_to", None)
        else:
            twin_index = ti
            prov["twin_index_refers_to"] = "parent_twin"

    parent_dates = [str(d) for d in F.dates]
    absent = sorted(set(parent_dates) - set(str(d) for d in dates))
    if stripped_dates is not None:
        absent = sorted(set(absent) | set(str(d) for d in stripped_dates))
    prov["parent_dates"] = prov.get("parent_dates", parent_dates)
    prov["stripped_dates"] = absent
    prov["parent_rows"] = int(F.n_rows)
    prov["parent_name"] = F.name

    out = Frame(
        name=F.name,
        visible=visible,
        hidden=hidden,
        dates=np.asarray(dates, dtype=F.dates.dtype),
        markets=np.asarray(markets, dtype=F.markets.dtype),
        block_starts=block_starts,
        provenance=prov,
        twin_index=twin_index,
    )
    out.validate()
    return out


def strip_pair(search: Frame, twin: Frame, keep_dates: Iterable[str]) -> Tuple[Frame, Frame]:
    """Strip a search frame AND its gefs twin to ``keep_dates``, remapping ``twin_index``.

    Both frames are stripped with the same date rule, so a kept search row's
    twin row (same ticker/ts/direction/mode, hence same target date) is kept
    too; any twin pointer to a dropped row becomes ``-1``.
    """
    keep_dates = tuple(sorted(set(str(d) for d in keep_dates)))
    keep_s = date_mask(search, keep_dates)
    keep_t = date_mask(twin, keep_dates)
    twin_row_map = np.full(twin.n_rows, -1, dtype=np.int64)
    twin_row_map[keep_t] = np.arange(int(keep_t.sum()), dtype=np.int64)
    dropped = sorted(set(str(d) for d in search.dates) - set(keep_dates))
    s = strip_rows(search, keep_s, stripped_dates=dropped, twin_row_map=twin_row_map)
    t = strip_rows(twin, keep_t, stripped_dates=dropped)
    return s, t


def strip_to_campaign(search: Frame, twin: Optional[Frame], campaign: Campaign):
    """Worker-side frames for a campaign: search window only (embargo + validation absent)."""
    if twin is None:
        keep = date_mask(search, campaign.worker_dates)
        s = strip_rows(search, keep, stripped_dates=campaign.stripped_dates)
        t = None
    else:
        s, t = strip_pair(search, twin, campaign.worker_dates)
    s.provenance["campaign"] = campaign.name
    if t is not None:
        t.provenance["campaign"] = campaign.name
    return s, t
