"""Sealed ladder roots: the search frame must never see them.

``PRD_STRATEGY_FACTORY.md`` §4 A3 declares the 69 development dates
(2026-05-18..07-25, ``data/ladders``) *already searched*; the first true
out-of-sample data is sealed holdout-B (``data/ladders_holdout``,
2026-07-26..08-31) followed by the Sept-Oct R3 reserve
(``data/ladders_2026-09``, the M0 capture). FR-F0.5 requires that the
search-frame loader **refuses** both roots, and §4 A7 keeps them unmounted
from the ``factory`` service altogether. This module is the single source of
truth for *which* roots are sealed and *how* a refusal is decided:

* by **path identity** -- the candidate root is resolved (symlinks, ``..``)
  and case-normalised before comparison, and a root *inside* a sealed root is
  refused too. A string-prefix check would refuse ``data/ladders_holdout_x``
  for the wrong reason and wave ``data/ladders/../ladders_holdout`` through;
  neither happens here.
* by a **marker file** -- a directory (or any ancestor) holding a file named
  ``SEALED`` is refused regardless of its path, so a sealed root copied or
  renamed elsewhere stays protected as long as the marker travels with it.

The only sanctioned readers of a sealed root are F4's ``holdout.py`` (once,
under ``--unseal RATIFIED-<date>``, logged to
``reports/factory/unseal_log.jsonl``) and descriptive coverage statistics
(``backfill_ladders.py --stats``); both call the loader's explicit unchecked
entry point (``kalshi_history._load_ladders_unchecked``) rather than this gate.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

#: PRD clause every refusal cites.
PRD_CLAUSE = "PRD_STRATEGY_FACTORY.md §4 A3 / FR-F0.5"

#: Name of the marker file that seals a directory wherever it lives.
SEALED_MARKER = "SEALED"

#: The sealed ladder roots (absolute). Order is documentary only.
SEALED_LADDER_ROOTS = (
    # Holdout-B: target dates 2026-07-26..2026-08-31, opened once in F4.
    _PROJECT_ROOT / "data" / "ladders_holdout",
    # M0 daily capture (kill date 2026-09-15): the Sept-Oct R3 reserve.
    _PROJECT_ROOT / "data" / "ladders_2026-09",
)


class SealedDataError(PermissionError):
    """Raised when a search-frame path tries to read a sealed ladder root."""


def _canonical(path) -> Path:
    """Resolve symlinks and ``..`` and normalise case (Windows) for identity."""
    return Path(os.path.normcase(str(Path(path).expanduser().resolve())))


def _display(path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(_PROJECT_ROOT))
    except (ValueError, OSError):
        return str(p)


def sealed_reason(
    root, sealed_roots: Optional[Iterable[Path]] = None
) -> Optional[str]:
    """Why ``root`` is sealed, or ``None`` when it may be searched.

    ``sealed_roots`` defaults to :data:`SEALED_LADDER_ROOTS` *at call time*
    so tests can monkeypatch the module attribute.
    """
    roots = SEALED_LADDER_ROOTS if sealed_roots is None else tuple(sealed_roots)
    candidate = _canonical(root)
    for sealed in roots:
        canon = _canonical(sealed)
        if candidate == canon:
            return f"{_display(root)} is the sealed root {_display(sealed)}"
        if canon in candidate.parents:
            return f"{_display(root)} lies inside the sealed root {_display(sealed)}"
    for ancestor in (candidate, *candidate.parents):
        marker = ancestor / SEALED_MARKER
        try:
            if marker.is_file():
                return f"{_display(root)} carries a {SEALED_MARKER} marker at {marker}"
        except OSError:
            continue
    return None


def is_sealed(root, sealed_roots: Optional[Iterable[Path]] = None) -> bool:
    return sealed_reason(root, sealed_roots) is not None


def assert_not_sealed(
    root,
    sealed_roots: Optional[Iterable[Path]] = None,
    purpose: str = "the search frame",
) -> Path:
    """Return ``Path(root)`` or raise :class:`SealedDataError`.

    The message names the offending root, the sealed root it matched (or the
    marker), and the PRD clause, so a refusal in a log is self-explanatory.
    """
    reason = sealed_reason(root, sealed_roots)
    if reason is not None:
        raise SealedDataError(
            f"refusing to load {purpose} from a sealed ladder root: {reason}. "
            f"Sealed roots are out-of-sample by construction ({PRD_CLAUSE}); "
            "they are opened once, under an unseal record, by the F4 holdout "
            "path -- never by the frame loader."
        )
    return Path(root)


def assert_frame_not_sealed(frame, purpose: str = "the search frame") -> None:
    """Refuse a ladder DataFrame that records a sealed origin in ``attrs``.

    :func:`src.data.kalshi_history.load_ladders` stamps
    ``df.attrs["ladder_root"]`` with the resolved root it read. The frame
    builder checks that stamp so a frame that reached it through some other
    reader is refused at the last gate as well. Frames without the stamp
    (hand-built fixtures) pass.
    """
    attrs = getattr(frame, "attrs", None) or {}
    origin = attrs.get("ladder_root")
    if origin:
        assert_not_sealed(origin, purpose=purpose)
