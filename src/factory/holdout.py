"""Sealed-root evaluation (F4): holdout-B finalists, the Sept-Oct R3 score, the R5 re-check.

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 1.1 (the
``holdout.py`` row), section 5.8 (promotion-time gates), section 6.3 (Holm
across all registry entries), section 9 steps 2-3; ``PRD_STRATEGY_FACTORY.md``
FR-F4.1; ``docs/REVIVAL_2026_09.md`` section 5 (draft R3 criteria 1-6).
Red-team round 1 (2026-09-06, on 48b8ae6) hardened the "once" and
"ratified" guarantees from file-content conventions into mechanisms; the
items are marked ``[RT1-n]`` below.

The commands
------------
``factory.py holdout --finalists FILE --unseal RATIFIED-<date>``
    scores <= 3 finalists of ONE family on sealed holdout-B
    (``data/ladders_holdout``, manifest date range 2026-07-26..2026-08-31)
    with Holm.
``factory.py score --genome ID --ladders data/ladders_2026-09 --unseal RATIFIED-<date> [--as-of D]``
    scores ONE PROPOSED genome on the Sept-Oct R3 root, prints the result
    sha256 BEFORE any number, appends the R3 checks to the registry.
``factory.py score --genome ID --r5-check --unseal ... [--ladders ROOT]``
    re-evaluates ONLY REVIVAL criterion 6 (cold-season month) on the dates
    after the recorded ``as_of`` of the genome's R3 score; never recomputes
    1-5; once only; RATIFIED confirmed or HALT.

Unseal protocol (each step pinned by ``tests/test_factory_holdout.py``)
------------------------------------------------------------------------
1. ``--unseal RATIFIED-<date>`` is mandatory and ``<date>`` must be
   ratified in ``docs/REVIVAL_2026_09.md``. [RT1-2, RT2-2] The ONLY form
   recognised is a whole line reading exactly ``RATIFIED YYYY-MM-DD`` --
   column 0, nothing after the date (trailing prose negates) -- outside
   ``` / ~~~ fences, ``<pre>`` blocks and ``<!-- -->`` comment spans; and
   it is read from the COMMITTED content (``git show HEAD:``): a working
   copy that differs from HEAD, or an uncommitted/untracked doc, is
   refused. ``--revival-doc`` exists for tests: with ``MP_FACTORY_TEST_DOC=1``
   the disk file is read as-is (a doc outside the repo is refused without
   it); the unseal line records the doc's repo-relative path and sha256.
2. The root must carry ``SEALED`` + ``SHA256SUMS`` + ``manifest.json``;
   every ``<date>.csv`` stem must postdate the development set. [RT1-1iv]
   Root PURPOSE is enforced from manifest metadata (never labels):
   ``holdout`` refuses a root whose ``date_range`` is not holdout-B's,
   ``score`` refuses one that is.
3. Finalists: <= 3 per family, each PROPOSED (on a PROPOSED line of a live
   family, not yet RATIFIED). Family #1 is CLOSED -> refused; a rerun is a
   new family (``.../v2``).
4. Budget: <= 3 unseals per calendar quarter (UTC). [RT1-1, RT2-1] The
   once-lock is PER PURPOSE, not per root digest: ``holdout`` is once per
   FAMILY (holdout-B is one purpose; any copy, re-pull or edit of the root
   is the same look), ``score`` once per GENOME (the R3 purpose), and
   ``--r5-check`` once per genome. ``root_digest`` (sha256 over the PARSED
   ``SHA256SUMS`` entries + manifest ``date_range`` + series) is RECORDED
   as evidence of what was looked at; it is not the lock's identity. The
   lock consults BOTH ``unseal_log.jsonl`` AND every registry line's
   evidence (``holdout`` / ``r3`` / ``r5`` blocks), and the quarter quota
   counts registry evidence as well as log lines, so deleting the
   uncommitted log resets nothing. Both files are checked for append-only
   integrity at the start of every command: the content committed at
   ``HEAD`` must be a byte prefix of the on-disk file (a missing on-disk
   file while HEAD has one is a refusal too). Plainly: a COMMITTED rewrite
   of ``registry.jsonl`` / ``unseal_log.jsonl`` is a git-history event this
   tool cannot detect -- integrity beyond the HEAD prefix is delegated to
   review of git history -- and the unseal log gains HEAD-prefix protection
   only once it is first committed.
5. The unseal line is written BEFORE any price row is read. Anything that
   fails after it has spent the look: it is raised as ``HoldoutAbort``
   naming the spent line, and the CLI exits with ``EXIT_ABORT``. [RT1-3]

The sealed-root bypass is narrow
--------------------------------
``kalshi_history.load_ladders`` / ``ev_analysis.load_search_ladders`` /
``sealed_roots.assert_frame_not_sealed`` keep refusing the roots. This module
alone calls ``kalshi_history._load_ladders_unchecked`` for scoring and sets
``sealed_roots.SEALED_EVALUATION_ATTR`` on the tape, in exactly one function
(``open_sealed_root``) that refuses to run without an appended record. The
origin stamp moves to ``attrs["unsealed_root"]``.

Frame semantics are the search frame's
--------------------------------------
Same evaluator chain (``lanes.weather.build_opportunities_from_ladders``),
same hardening (``frame.from_opportunity_frame`` with the promoted spec's
availability lag, sigma cap, adverse fill, contracts; ``truth_filter=True``;
``fold_sandbox_admissible=True``; the family config's embargo), the gefs twin
for the ex-ante disqualifier. ``cutoff=None`` (every date postdates the dev
set, asserted) and markets with ``payoff_matches_kalshi == False`` are DROPPED
and counted instead of aborting the frame.

Thresholds come from the registry [RT1-4]
-----------------------------------------
The hard constraints (``min_trades``, ``min_cities``, ``min_dates_frac``,
``worst_date_pnl_min``, ``max_clauses``, ``gefs_twin_min``, ``bss_trades_min``,
``pooled_boot_lo_gt``) and ``holm_alpha`` are read from the family's
pre-committed ``thresholds`` block on its registry line; ``fitness.py``'s
constants are the documented fallback and the report says which was used
(``thresholds_source``). Section 5.8's ``BSS_trades >= 0`` is a separately
named promotion-time gate (``bss_trades_ge0``) beside the registry's
``bss_trades_min`` constraint; both gate, both are labelled. ``p_RC(ALL69)``
and ``beats_every_control`` are search-window quantities carried from the
F2 report; they are NOT recomputed here and the report says so.

Verdicts and the registry [RT1-6]
---------------------------------
``registry.py`` has family-level status only, so:

* ``holdout`` PASS always writes a PROPOSED transition naming the genome with
  ``evidence.holdout`` (never RATIFIED -- that is R3's verdict). A failing
  finalist gets a status-neutral ``evidence`` event. The family is HALTed
  only when NO finalist passes AND NO genome in it is RATIFIED. Because
  ``Registry.status()`` is last-transition-wins (and the OPS flow stamps it
  into gate registrations), a holdout PASS in a family that already holds a
  RATIFIED genome is followed by a RATIFIED line re-asserting that genome
  (``evidence.reasserted``), so the family is not demoted; per-genome truth
  is always ``genome_status`` / ``ratified_genomes`` / ``r3_line``.
* ``score`` PASS -> RATIFIED. A failing R3 -> HALT #3, unless a sibling is
  RATIFIED, in which case the failure is an ``evidence`` event. ``score``
  requires a PROPOSED genome with a PASS holdout line (section 9 order).
* ``--r5-check`` PASS -> a RATIFIED line (confirmed) with ``evidence.r5``;
  FAIL -> HALT #3 under the same sibling rule.

Holm [RT1-5]: strict ``p_adj < alpha`` (PRD: "< 0.05"), computed over the
finalists' one-sided p-values plus one entry per other registry family (its
recorded p on this root if any, else its pooled-validation p, else 1.0,
which cannot reject and only makes the finalists' adjustment more
conservative). ``holm_m`` and the p vector go into the registry evidence.

R3 #2 when the gefs twin is EMPTY or CANNOT BE BUILT [RT2-4]: the gefs
calibration's ``sigma_f`` can exceed the sigma cap on every row of a root
(measured 4.54..5.97 on 2026-07-26/27), and the gefs archive in
``data/forecast_archive`` ends 2026-07-27, so on a Sept-dated root the twin
build raises ``EVAnalysisError: no forecast vintage could be matched``.
``default_frame_builder`` catches ANY failure of the twin build
(``provenance["gefs_twin_unavailable"]``) instead of aborting a spent look,
and the ``gefs_twin_ge0`` gate FAILS with that reason. Consequence, stated
plainly: **R3 #2 on Sept-Oct is a HALT-by-construction** unless gefs
Sept-Oct is backfilled into the chosen archive dir, or the owner ratifies
the sigma-cap / no-gefs ex-ante disqualifier. That is an owner ruling, not
this module's. The forecast archive dir is chosen BEFORE the unseal
(``select_forecast_archive_dir``): ``--forecast-archive-dir`` if given, else
the ``data/forecast_archive*`` dir whose gfs_mex series covers the root's
dates; none covering -> refused, nothing written; the choice is recorded in
the report.

R3 #4 (tail ratio in [0.8, 1.25] at |z| >= 2.5) is evaluated literally as
pre-registered; on ~148 city-days the ratio is coarse (0 -> 0.00, 1 ->
0.54, 2 -> 1.09) and the report carries the counts.

R5 (criterion 6) definition, NOT ratified: a "cold-season month of recorded
ladders" is >= ``COLD_SEASON_MIN_DATES`` (28) distinct target dates after
the recorded ``as_of`` whose month is in ``COLD_SEASON_MONTHS`` (Nov-Mar).
The owner may amend both constants at ratification; they are recorded in
the evidence.

The label-leak caveat (HANDOFF 2026-09-05 correction) is copied into every
unseal line: the root's ``manifest.json`` exposes ``days[].market_detail[].result``
and ``RECONCILE.md`` exposes settled highs, so "unsealed once" protects the
prices, not the outcomes.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import subprocess
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from src.factory import fitness
from src.factory import genome as G
from src.factory import multiplicity as MP
from src.factory import promoted as P
from src.factory.procedure import pooled_stats
from src.factory.registry import TERMINAL, Registry, RegistryError, git_rev

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = Path(os.path.dirname(os.path.dirname(_THIS_DIR)))

# ``src.data.kalshi_history`` and ``src.backtest`` are imported LAZILY, inside the functions that need them:
# ``kalshi_history`` pulls in the live exchange client module (the one ``tests/test_factory_no_live_capital.py`` names), which the factory package must
# never load at import time (OPS red team, 2026-09-06; pinned by
# tests/test_factory_holdout.py::test_importing_holdout_does_not_load_the_kalshi_client). The sealed reader is
# resolved through this module attribute so tests can spy on it; ``None`` means "import on first use".
_load_ladders_unchecked = None

DEFAULT_UNSEAL_LOG = REPO_ROOT / "reports" / "factory" / "unseal_log.jsonl"
DEFAULT_REVIVAL_DOC = REPO_ROOT / "docs" / "REVIVAL_2026_09.md"
DEFAULT_REGISTRY = REPO_ROOT / "reports" / "factory" / "registry.jsonl"
DEFAULT_PROMOTED_DIR = REPO_ROOT / "configs" / "factory" / "promoted"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports" / "factory"
DEFAULT_FAMILY_CONFIG = REPO_ROOT / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml"
DEFAULT_HOLDOUT_ROOT = REPO_ROOT / "data" / "ladders_holdout"
DEFAULT_R3_ROOT = REPO_ROOT / "data" / "ladders_2026-09"

MAX_FINALISTS = 3
MAX_UNSEALS_PER_QUARTER = 3
TRUTH_FILTER_MAX_DROP = 0.10
POINT_ESTIMATE_MIN = 0.04
TAIL_RATIO_RANGE = (0.8, 1.25)
TAIL_Z = 2.5
PRICE_SWEEP = (0.02, 0.03)
SENSITIVITY_EMBARGO_DAYS = 2
UNKNOWN_FAMILY_P = 1.0
FROZEN_SOURCE = "gfs_mex"  # REVIVAL section 4 (R1)
ELIGIBLE_STATUSES = ("PROPOSED", "RATIFIED")
SHA256SUMS = "SHA256SUMS"
MANIFEST = "manifest.json"
RESULT_LABELS = ("yes", "no")
#: PRD_STRATEGY_FACTORY section 4 A3: holdout-B's target dates. The manifest's date_range must equal it.
HOLDOUT_B_RANGE = ("2026-07-26", "2026-08-31")
#: R5 (REVIVAL section 5 #6) -- NOT ratified; see the module docstring.
COLD_SEASON_MONTHS = (11, 12, 1, 2, 3)
COLD_SEASON_MIN_DATES = 28
TEST_DOC_ENV = "MP_FACTORY_TEST_DOC"

UNSEAL_TAG_RE = re.compile(r"^RATIFIED-(\d{4}-\d{2}-\d{2})$")
#: The owner's ratification line: column 0, the word, one space, the date, then whitespace or end of line.
RATIFICATION_LINE_RE = re.compile(r"^RATIFIED (\d{4}-\d{2}-\d{2})\s*$")

#: Registry thresholds read from the family line (registry key -> fitness fallback constant).
THRESHOLD_FALLBACKS: Dict[str, float] = {
    "min_trades": fitness.MIN_TRADES,
    "min_cities": fitness.MIN_CITIES,
    "min_dates_frac": fitness.MIN_DATE_FRACTION,
    "worst_date_pnl_min": fitness.WORST_DATE_MIN,
    "max_clauses": fitness.MAX_CLAUSES,
    "gefs_twin_min": 0.0,
    "bss_trades_min": fitness.BSS_TRADES_MIN,
    "pooled_boot_lo_gt": 0.0,
    "holm_alpha": 0.05,
}
SEARCH_WINDOW_ONLY = ("p_rc_all69_lt", "beats_every_control")

LABEL_LEAK_CAVEAT = (
    "HANDOFF 2026-09-05 correction: this root's manifest.json exposes days[].market_detail[].result "
    "(every market's outcome label with its bracket bounds) and RECONCILE.md exposes settled highs; "
    "the seal protected the prices, not the outcomes. Any search designed after reading those files "
    "is not cleanly out-of-sample on this root."
)

EXIT_PASS, EXIT_HALT, EXIT_REFUSED, EXIT_ABORT = 0, 1, 2, 3


class UnsealRefused(RuntimeError):
    """The unseal protocol refused; nothing was read, nothing was written."""


class HoldoutAbort(RuntimeError):
    """The evaluation aborted AFTER the unseal line was written (the look is spent)."""

    def __init__(self, msg: str, record: Optional["UnsealRecord"] = None):
        super().__init__(msg)
        self.record = record


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HoldoutPaths:
    """Every file the protocol touches, so tests can point all of them at tmp."""

    repo_root: Path = REPO_ROOT
    unseal_log: Path = DEFAULT_UNSEAL_LOG
    revival_doc: Path = DEFAULT_REVIVAL_DOC
    registry: Path = DEFAULT_REGISTRY
    promoted_dir: Path = DEFAULT_PROMOTED_DIR
    reports_dir: Path = DEFAULT_REPORTS_DIR
    family_config: Path = DEFAULT_FAMILY_CONFIG


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def quarter_of(ts: Union[str, _dt.datetime]) -> str:
    """``"2026Q3"`` for a UTC timestamp (ISO string or datetime)."""
    if isinstance(ts, str):
        ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"


def _relpath(path: Union[str, Path], repo_root: Path = REPO_ROOT) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def _inside_repo(path: Union[str, Path], repo_root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(repo_root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe(obj: Any) -> Any:
    """JSON-safe copy: numpy scalars/arrays -> Python, NaN/inf -> None, Paths -> str."""
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_safe(v) for v in obj.tolist()]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, Path):
        return obj.as_posix()
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_safe(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def result_sha256(result: Dict[str, Any]) -> str:
    """sha256 of the canonical JSON of the numbers -- printed before them."""
    return sha256_text(canonical_json(result))


# ---------------------------------------------------------------------------
# 1. the tag and the ratification line  [RT1-2]
# ---------------------------------------------------------------------------
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def ratified_dates_in(text: str) -> List[str]:
    """Dates on lines that are EXACTLY ``RATIFIED YYYY-MM-DD`` (nothing after the date), outside
    ``` / ~~~ fences, ``<pre>``...``</pre>`` blocks and ``<!--``...``-->`` comment spans."""
    out: List[str] = []
    fence: Optional[str] = None
    in_pre = False
    in_comment = False
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        m = FENCE_RE.match(line)
        if m and not in_pre and not in_comment:
            tok = m.group(1)
            if fence is None:
                fence = tok
            elif fence == tok:
                fence = None
            continue
        if fence is not None:
            continue
        low = line.lower()
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if in_pre:
            if "</pre>" in low:
                in_pre = False
            continue
        if "<!--" in line:
            if "-->" not in line.split("<!--", 1)[1]:
                in_comment = True
            continue
        if "<pre" in low:
            if "</pre>" not in low:
                in_pre = True
            continue
        m2 = RATIFICATION_LINE_RE.match(line)
        if m2:
            out.append(m2.group(1))
    return out


def read_ratification_text(doc_path: Union[str, Path], repo_root: Path = REPO_ROOT) -> str:
    """The COMMITTED content of the ratification document (``git show HEAD:``); tests read the disk file.

    Refuses a doc outside the repo (unless ``MP_FACTORY_TEST_DOC=1``), one not
    committed at HEAD, or one whose working copy differs from HEAD.
    """
    p = Path(doc_path)
    if os.getenv(TEST_DOC_ENV, "").strip() == "1":
        if not p.is_file():
            raise UnsealRefused(f"ratification document {p} does not exist")
        return p.read_text(encoding="utf-8", errors="replace")
    if not _inside_repo(p, repo_root):
        raise UnsealRefused(
            f"ratification document {p} lies outside the repository; the owner ratifies in the tracked "
            f"docs/REVIVAL_2026_09.md (set {TEST_DOC_ENV}=1 only in tests)"
        )
    rel = _relpath(p, repo_root)
    head = git_show_head(repo_root, rel)
    if head is None:
        raise UnsealRefused(f"ratification document {rel} is not committed at HEAD; an uncommitted or untracked "
                            "doc ratifies nothing")
    if not p.is_file():
        raise UnsealRefused(f"ratification document {rel} is committed at HEAD but missing from the working copy")
    if p.read_bytes().replace(b"\r", b"") != head.replace(b"\r", b""):
        raise UnsealRefused(f"ratification document {rel} differs from its committed content at HEAD; commit the "
                            "ratification (or restore the file) before opening a sealed root")
    return head.decode("utf-8", errors="replace")


def ratified_dates(doc_path: Union[str, Path], repo_root: Path = REPO_ROOT) -> List[str]:
    """Ratified dates in the committed doc (``[]`` when the doc does not exist)."""
    p = Path(doc_path)
    if os.getenv(TEST_DOC_ENV, "").strip() == "1" and not p.exists():
        return []
    return ratified_dates_in(read_ratification_text(p, repo_root))


def parse_unseal_tag(tag: Optional[str]) -> str:
    if not tag:
        raise UnsealRefused(
            "--unseal RATIFIED-<date> is required: a sealed root is opened only under a ratification "
            "recorded in docs/REVIVAL_2026_09.md (FACTORY_ARCHITECTURE section 9 step 2)"
        )
    m = UNSEAL_TAG_RE.match(str(tag).strip())
    if not m:
        raise UnsealRefused(f"unseal tag {tag!r} is not of the form RATIFIED-YYYY-MM-DD")
    return m.group(1)


def resolve_revival_doc(doc_path: Union[str, Path], repo_root: Path = REPO_ROOT) -> Tuple[str, str]:
    """(repo-relative path, sha256 of the text the check read) of the ratification document."""
    p = Path(doc_path)
    text = read_ratification_text(p, repo_root)
    return _relpath(p, repo_root), sha256_text(text.replace("\r", ""))


def assert_tag_ratified(tag: Optional[str], doc_path: Union[str, Path], repo_root: Path = REPO_ROOT) -> str:
    """Return the ratification date the tag names, or refuse."""
    date = parse_unseal_tag(tag)
    dates = ratified_dates(doc_path, repo_root)
    if date not in dates:
        have = ", ".join(dates) if dates else "none -- the document says nothing is ratified yet"
        raise UnsealRefused(
            f"unseal tag {tag} does not match a 'RATIFIED <date>' line in {_relpath(doc_path, repo_root)} "
            f"(ratified dates present: {have}); the owner ratifies by committing a whole line reading exactly "
            "'RATIFIED YYYY-MM-DD' (column 0, nothing after the date, outside fences/pre/comments), not by passing a tag"
        )
    return date


# ---------------------------------------------------------------------------
# 2. the root's seal and identity  [RT1-1i, RT1-1iv]
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RootSeal:
    root: Path
    relpath: str
    marker_text: str
    root_digest: str  # over the PARSED SHA256SUMS entries + manifest date_range + series
    sha256sums_digest: str  # raw file bytes, informational
    n_entries: int
    csv_stems: Tuple[str, ...]
    date_range: Tuple[str, str]
    series: Tuple[str, ...]

    @property
    def is_holdout_b(self) -> bool:
        return tuple(self.date_range) == HOLDOUT_B_RANGE


def parse_sha256sums(path: Path) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].strip().lstrip("*")
        out.append((digest, name.replace("\\", "/")))
    return out


def root_digest_of(entries: Sequence[Tuple[str, str]], date_range: Sequence[str], series: Sequence[str]) -> str:
    """Identity of a root: sorted (hash, path) pairs + date_range + series. Comments/whitespace cannot move it."""
    h = hashlib.sha256()
    for digest, name in sorted(entries, key=lambda e: (e[1], e[0])):
        h.update(f"{digest}  {name}\n".encode("utf-8"))
    h.update(f"date_range={date_range[0]}..{date_range[1]}\n".encode("utf-8"))
    h.update(("series=" + ",".join(sorted(series)) + "\n").encode("utf-8"))
    return h.hexdigest()


def manifest_metadata(root: Path) -> Tuple[Tuple[str, str], Tuple[str, ...]]:
    """(date_range, series) from ``manifest.json`` -- metadata keys only, never labels."""
    m = root / MANIFEST
    if not m.is_file():
        raise UnsealRefused(f"{root} has no {MANIFEST}; a root without its manifest cannot be identified")
    try:
        doc = json.loads(m.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise UnsealRefused(f"{m}: not JSON ({exc})") from exc
    dr = doc.get("date_range") or {}
    start, end = str(dr.get("start") or ""), str(dr.get("end") or "")
    if not (re.match(r"^\d{4}-\d{2}-\d{2}$", start) and re.match(r"^\d{4}-\d{2}-\d{2}$", end)):
        raise UnsealRefused(f"{m}: date_range.start/end missing or malformed ({dr!r})")
    series = sorted(set(map(str, (doc.get("series_metadata") or {}).keys()))
                    | {str(c.get("series")) for c in (doc.get("cities") or []) if c.get("series")})
    if not series:
        raise UnsealRefused(f"{m}: no series listed (series_metadata / cities)")
    return (start, end), tuple(series)


def inspect_root_seal(root: Union[str, Path], repo_root: Path = REPO_ROOT) -> RootSeal:
    """Refuse a root without ``SEALED`` + ``SHA256SUMS`` + manifest or with dev-set-dated CSVs. Reads no rows."""
    from src.backtest.sealed_roots import DEV_SET_LAST_DATE, SEALED_MARKER

    r = Path(root)
    if not r.is_dir():
        raise UnsealRefused(f"ladder root {r} does not exist")
    marker = r / SEALED_MARKER
    if not marker.is_file():
        raise UnsealRefused(
            f"{_relpath(r, repo_root)} carries no {SEALED_MARKER} marker: a root that was never sealed is not a "
            "holdout, and this command exists only to spend a seal (PRD_STRATEGY_FACTORY section 4 A3)"
        )
    sums = r / SHA256SUMS
    if not sums.is_file():
        raise UnsealRefused(f"{_relpath(r, repo_root)} has no {SHA256SUMS}; the root's content cannot be identified")
    entries = parse_sha256sums(sums)
    if not entries:
        raise UnsealRefused(f"{sums} lists no files")
    stems = tuple(sorted({Path(p).stem for p in r.glob("*/*.csv")}))
    early = [s for s in stems if s <= DEV_SET_LAST_DATE]
    if early:
        raise UnsealRefused(
            f"{_relpath(r, repo_root)} holds {len(early)} day file(s) dated <= {DEV_SET_LAST_DATE} "
            f"(e.g. {early[:3]}); those dates were searched, so this is not a sealed out-of-sample root"
        )
    date_range, series = manifest_metadata(r)
    return RootSeal(
        root=r,
        relpath=_relpath(r, repo_root),
        marker_text=marker.read_text(encoding="utf-8", errors="replace").strip(),
        root_digest=root_digest_of(entries, date_range, series),
        sha256sums_digest=sha256_bytes_of(sums),
        n_entries=len(entries),
        csv_stems=stems,
        date_range=date_range,
        series=series,
    )


def assert_root_purpose(command: str, seal: RootSeal) -> None:
    """[RT1-1iv] ``holdout`` only on holdout-B; ``score``/``score-r5`` never on it. Manifest metadata only."""
    dr = f"{seal.date_range[0]}..{seal.date_range[1]}"
    if command == "holdout" and not seal.is_holdout_b:
        raise UnsealRefused(
            f"holdout refuses {seal.relpath}: its manifest date_range is {dr}, not holdout-B's "
            f"{HOLDOUT_B_RANGE[0]}..{HOLDOUT_B_RANGE[1]} (PRD_STRATEGY_FACTORY section 4 A3)"
        )
    if command != "holdout" and seal.is_holdout_b:
        raise UnsealRefused(
            f"{command} refuses {seal.relpath}: its manifest date_range {dr} is holdout-B's; the R3/R5 root "
            "is the Sept-Oct capture (data/ladders_2026-09), and holdout-B is spent only by `holdout`"
        )


def verify_sha256sums(root: Path) -> Dict[str, Any]:
    """``sha256sum -c`` equivalent over the listed files (called AFTER the unseal line)."""
    ok, failed, missing = 0, [], []
    for digest, name in parse_sha256sums(root / SHA256SUMS):
        p = root / name
        if not p.is_file():
            missing.append(name)
            continue
        if sha256_bytes_of(p) == digest:
            ok += 1
        else:
            failed.append(name)
    return {"ok": ok, "failed": failed, "missing": missing}


# ---------------------------------------------------------------------------
# 3. finalists and their registry status
# ---------------------------------------------------------------------------
def load_finalists(path: Union[str, Path]) -> Tuple[str, List[str]]:
    """``{"family": ..., "finalists": [genome_id, ...]}`` -> (family, ids); <= 3, unique, non-empty."""
    p = Path(path)
    if not p.is_file():
        raise UnsealRefused(f"finalists file {p} does not exist")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise UnsealRefused(f"finalists file {p} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("finalists"), list) or not doc.get("family"):
        raise UnsealRefused(
            f"finalists file {p} must be an object {{\"family\": <registry family>, \"finalists\": [genome_id, ...]}}"
        )
    ids = [str(x) for x in doc["finalists"]]
    if not ids:
        raise UnsealRefused(f"finalists file {p} names no genome")
    if len(ids) != len(set(ids)):
        raise UnsealRefused(f"finalists file {p} repeats a genome id")
    if len(ids) > MAX_FINALISTS:
        raise UnsealRefused(
            f"finalists file {p} names {len(ids)} genomes; at most {MAX_FINALISTS} finalists per family may "
            "spend the holdout (FACTORY_ARCHITECTURE section 1.1 holdout.py row / section 9 step 2)"
        )
    return str(doc["family"]), ids


def _transition_lines(reg: Registry, family: str) -> List[Dict[str, Any]]:
    return [ln for ln in reg.lines() if ln.get("event") == "transition" and ln.get("family") == family]


def genome_status(reg: Registry, family: str, genome_id: str) -> Optional[str]:
    """Per-genome status derived from the family's lines (the registry itself is family-level).

    ``None`` when the family is unregistered or the genome is on no
    PROPOSED/RATIFIED transition line; the family's terminal status
    (CLOSED/HALT) when it has one; else ``"RATIFIED"`` if a RATIFIED line
    names the genome, else ``"PROPOSED"``.
    """
    fam = reg.status(family)
    if fam is None:
        return None
    if fam in TERMINAL:
        return fam
    named = [
        str(ln.get("status")) for ln in _transition_lines(reg, family)
        if str(ln.get("genome_id")) == genome_id and ln.get("status") in ELIGIBLE_STATUSES
    ]
    if not named:
        return None
    return "RATIFIED" if "RATIFIED" in named else "PROPOSED"


def ratified_genomes(reg: Registry, family: str) -> List[str]:
    """Genomes named on RATIFIED lines (re-asserts included), MOST RECENT FIRST, unique."""
    out: List[str] = []
    for ln in reversed(_transition_lines(reg, family)):
        gid = str(ln.get("genome_id"))
        if ln.get("status") == "RATIFIED" and ln.get("genome_id") and gid not in out:
            out.append(gid)
    return out


def assert_genome_eligible(reg: Registry, family: str, genome_id: str,
                           want: Sequence[str] = ("PROPOSED",), command: str = "holdout") -> str:
    """The family is live, ``genome_id`` sits on a PROPOSED/RATIFIED line, and its status is one of ``want``."""
    fam = reg.status(family)
    if fam is None:
        raise UnsealRefused(f"family {family!r} has no registry line in {_relpath(reg.path)}")
    if fam in TERMINAL:
        raise UnsealRefused(
            f"family {family!r} is {fam} in the registry, not one of {ELIGIBLE_STATUSES}; genome {genome_id} "
            "cannot be a finalist. CLOSED/HALT are terminal (registry.py) -- a rerun is a NEW family (.../v2), "
            "never a second look for a closed one"
        )
    gs = genome_status(reg, family, genome_id)
    if gs is None:
        proposed = sorted({
            str(ln.get("genome_id")) for ln in _transition_lines(reg, family)
            if ln.get("status") in ELIGIBLE_STATUSES and ln.get("genome_id")
        })
        raise UnsealRefused(
            f"genome {genome_id} is not on any PROPOSED/RATIFIED transition line of family {family!r} "
            f"(have {proposed or 'none'}); a human writes PROPOSED <genome_id> first (section 9 step 1)"
        )
    if gs not in want:
        raise UnsealRefused(
            f"genome {genome_id} is already {gs} in family {family!r}; {command} needs one of {tuple(want)} "
            "(a RATIFIED genome is not scored again; its R3 verdict is on the record)"
        )
    return gs


def _evidence_blocks(ln: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    ev = ln.get("evidence") or {}
    return [(k, ev[k]) for k in ("holdout", "r3", "r5") if isinstance(ev.get(k), dict)]


def holdout_pass_lines(reg: Registry, family: str, genome_id: str) -> List[Dict[str, Any]]:
    out = []
    for ln in _transition_lines(reg, family):
        h = (ln.get("evidence") or {}).get("holdout") or {}
        if str(ln.get("genome_id")) == genome_id and h.get("verdict") == "PASS":
            out.append(ln)
    return out


def r3_line(reg: Registry, family: str, genome_id: str) -> Optional[Dict[str, Any]]:
    """The RATIFIED line carrying ``evidence.r3`` for the genome (its R3 score), or None."""
    for ln in reversed(_transition_lines(reg, family)):
        if ln.get("status") == "RATIFIED" and str(ln.get("genome_id")) == genome_id and \
                isinstance((ln.get("evidence") or {}).get("r3"), dict):
            return ln
    return None


def family_thresholds(fam_line: Dict[str, Any]) -> Tuple[Dict[str, float], str]:
    """[RT1-4] The pre-committed thresholds from the family line; fallback constants documented per key."""
    block = fam_line.get("thresholds") or {}
    thr: Dict[str, float] = {}
    missing: List[str] = []
    for key, default in THRESHOLD_FALLBACKS.items():
        if key in block and block[key] is not None:
            thr[key] = float(block[key])
        else:
            thr[key] = float(default)
            missing.append(key)
    if not missing:
        source = "registry"
    elif len(missing) == len(THRESHOLD_FALLBACKS):
        source = "default"
    else:
        source = "registry+default(" + ",".join(missing) + ")"
    return thr, source


def load_spec(genome_id: str, promoted_dir: Path, family: str, config_sha256: Optional[str]) -> P.PromotedSpec:
    try:
        spec = P.load_promoted(genome_id, directory=str(promoted_dir))
    except P.PromotedSpecError as exc:
        raise UnsealRefused(f"genome {genome_id}: {exc}") from exc
    if spec.genome_id != genome_id:
        raise UnsealRefused(f"{genome_id}: promoted spec carries genome_id {spec.genome_id}")
    if spec.family != family:
        raise UnsealRefused(f"{genome_id}: promoted spec belongs to family {spec.family!r}, not {family!r}")
    if config_sha256 and spec.config_sha256 != config_sha256:
        raise UnsealRefused(
            f"{genome_id}: promoted spec config_sha256 {spec.config_sha256[:12]} != registry family line "
            f"{config_sha256[:12]}; the thresholds it is judged against must be the pre-committed ones"
        )
    if int(spec.genome().mode) == 1:
        raise UnsealRefused(f"{genome_id} is a MAKER genome; maker executability is not scorable honestly (F3)")
    return spec


# ---------------------------------------------------------------------------
# 4. the unseal log, the once-lock and append-only integrity  [RT1-1ii, RT1-1iii]
# ---------------------------------------------------------------------------
@dataclass
class UnsealRecord:
    command: str  # "holdout" | "score" | "score-r5"
    tag: str
    ratified_date: str
    doc_path: str  # repo-relative
    doc_sha256: str
    family: str
    genome_ids: List[str]
    root: str  # repo-relative
    root_digest: str  # parsed-entries digest (identity)
    sha256sums_digest: str  # raw file bytes (informational)
    sha256sums_entries: int
    manifest_date_range: List[str]
    series: List[str]
    as_of: Optional[str] = None
    ts: str = ""
    git_rev: str = ""
    quarter: str = ""
    caveat: str = LABEL_LEAK_CAVEAT
    line_no: Optional[int] = None  # set once appended

    def line(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("line_no", None)
        return d


def read_unseal_log(path: Union[str, Path]) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                out.append(json.loads(raw))
    return out


def git_show_head(repo_root: Path, relpath: str) -> Optional[bytes]:
    """Bytes of ``relpath`` at ``HEAD``; ``None`` when HEAD has no such file (or git is unavailable)."""
    try:
        out = subprocess.run(["git", "-C", str(repo_root), "show", f"HEAD:{relpath}"],
                             capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def assert_append_only(path: Union[str, Path], repo_root: Path = REPO_ROOT) -> Dict[str, Any]:
    """[RT1-1iii] The content committed at HEAD must be a byte prefix of the on-disk file (CR-normalised)."""
    p = Path(path)
    if not _inside_repo(p, repo_root):
        return {"path": p.as_posix(), "checked": False, "reason": "outside the repository"}
    rel = _relpath(p, repo_root)
    head = git_show_head(repo_root, rel)
    if head is None:
        return {"path": rel, "checked": False, "reason": "not in HEAD"}
    committed = head.replace(b"\r", b"")
    if not p.exists():
        raise UnsealRefused(
            f"{rel} is committed at HEAD ({len(committed)} bytes) but missing on disk: an append-only record "
            "has been deleted; restore it from git before any sealed root is opened"
        )
    current = p.read_bytes().replace(b"\r", b"")
    if not current.startswith(committed):
        raise UnsealRefused(
            f"{rel} has been rewritten: the {len(committed)} bytes committed at HEAD are not a prefix of the "
            f"{len(current)} bytes on disk; append-only records are never edited (restore from git)"
        )
    return {"path": rel, "checked": True, "head_bytes": len(committed), "disk_bytes": len(current)}


_KIND_TO_COMMAND = {"holdout": "holdout", "r3": "score", "r5": "score-r5"}


def _quota_key(command: str, family: str, genome_id: Optional[str]) -> Tuple[str, str]:
    return (command, family) if command == "holdout" else (command, str(genome_id))


def assert_unseal_allowed(
    log_lines: Sequence[Dict[str, Any]],
    registry_lines: Sequence[Dict[str, Any]] = (),
    *,
    command: str,
    family: str,
    genome_ids: Sequence[str],
    root: str,
    root_digest: str,
    now: Optional[_dt.datetime] = None,
) -> None:
    """[RT2-1] The once-lock is per PURPOSE: ``holdout`` once per family, ``score``/``score-r5`` once per
    genome -- over the log AND the registry, whatever the root's digest or path. Refuses quoting the prior
    line. The quarter quota counts log lines and registry evidence blocks alike."""
    gids = set(map(str, genome_ids))
    for ln in log_lines:
        prior = json.dumps(ln, sort_keys=True)
        overlap = sorted(set(map(str, ln.get("genome_ids") or [])) & gids)
        if command == "holdout" and ln.get("command") == "holdout" and ln.get("family") == family:
            raise UnsealRefused(
                f"family {family!r} already spent its holdout-B look ({ln.get('root')}, digest "
                f"{str(ln.get('root_digest'))[:12]}); once per family per PURPOSE, whatever the root's copy or "
                f"digest (FR-F4.1) -- this root is {root} digest {root_digest[:12]}. Prior unseal line: {prior}"
            )
        if command in ("score", "score-r5") and ln.get("command") == command and overlap:
            raise UnsealRefused(
                f"genome(s) {overlap} already spent their {command} look ({ln.get('root')}, digest "
                f"{str(ln.get('root_digest'))[:12]}); once per genome per PURPOSE (FR-F4.1) -- no second scoring "
                f"may exist. Prior unseal line: {prior}"
            )
    for ln in registry_lines:
        for kind, block in _evidence_blocks(ln):
            prior = json.dumps(ln, sort_keys=True)
            if command == "holdout" and kind == "holdout" and ln.get("family") == family:
                raise UnsealRefused(
                    f"the registry already carries holdout evidence for family {family!r} (root digest "
                    f"{str(block.get('root_digest'))[:12]}); the unseal log is not the only record. "
                    f"Prior registry line: {prior}"
                )
            if command == "score" and kind == "r3" and str(ln.get("genome_id")) in gids:
                raise UnsealRefused(
                    f"the registry already carries r3 evidence for genome {ln.get('genome_id')} (root digest "
                    f"{str(block.get('root_digest'))[:12]}); the unseal log is not the only record. "
                    f"Prior registry line: {prior}"
                )
            if command == "score-r5" and kind == "r5" and str(ln.get("genome_id")) in gids:
                raise UnsealRefused(f"R5 re-check already recorded in the registry. Prior registry line: {prior}")
    # the quota last: a repeat look is refused as a repeat, not as a budget overrun
    now = now or _now()
    q = quarter_of(now)
    keys = set()
    for ln in log_lines:
        if ln.get("quarter") == q or (ln.get("ts") and quarter_of(str(ln.get("ts"))) == q):
            for g in (ln.get("genome_ids") or [None]):
                keys.add(_quota_key(str(ln.get("command")), str(ln.get("family")), g))
    for ln in registry_lines:
        if not ln.get("ts") or quarter_of(str(ln.get("ts"))) != q:
            continue
        for kind, _block in _evidence_blocks(ln):
            keys.add(_quota_key(_KIND_TO_COMMAND[kind], str(ln.get("family")), ln.get("genome_id")))
    if len(keys) >= MAX_UNSEALS_PER_QUARTER:
        raise UnsealRefused(
            f"{len(keys)} unseals already recorded in {q} across the unseal log and the registry "
            f"(cap {MAX_UNSEALS_PER_QUARTER} per quarter): {sorted(keys)}"
        )


def append_unseal_line(path: Union[str, Path], record: UnsealRecord, repo_root: Path = REPO_ROOT) -> UnsealRecord:
    """Append the record (ts, non-empty git_rev or abort). Append-only: never truncates."""
    rev = git_rev(repo_root)
    if not rev:
        raise UnsealRefused("git rev is empty; refusing to unseal without a code identity (set MP_GIT_REV)")
    record.git_rev = rev
    record.ts = _now().isoformat()
    record.quarter = quarter_of(record.ts)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n_before = len(read_unseal_log(p))
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record.line(), sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    record.line_no = n_before + 1
    return record


# ---------------------------------------------------------------------------
# 5. the single-purpose loader (THE bypass; nothing else may do this)
# ---------------------------------------------------------------------------
def open_sealed_root(root: Path, record: UnsealRecord):
    """Read the sealed tape under an APPENDED unseal record; stamps the sanctioned-evaluation attr.

    Refuses when ``record.line_no`` is unset, i.e. the log line is not on disk
    yet. Moves ``attrs["ladder_root"]`` to ``attrs["unsealed_root"]`` (so the
    origin gate in ``assert_frame_not_sealed`` stays meaningful elsewhere) and
    sets ``attrs[SEALED_EVALUATION_ATTR]`` to the record's identity, which the
    content gate honours and ``pd.concat`` drops (by design).
    """
    from src.backtest.sealed_roots import SEALED_EVALUATION_ATTR

    if record.line_no is None:
        raise HoldoutAbort("open_sealed_root called before the unseal line was appended; refusing", record)
    loader = _load_ladders_unchecked
    if loader is None:
        from src.data.kalshi_history import _load_ladders_unchecked as loader  # the ONLY sanctioned reader
    df = loader(root)
    origin = df.attrs.pop("ladder_root", str(Path(root).resolve()))
    df.attrs["unsealed_root"] = origin
    df.attrs[SEALED_EVALUATION_ATTR] = {
        "command": record.command,
        "tag": record.tag,
        "line_no": record.line_no,
        "root_digest": record.root_digest,
    }
    return df


# ---------------------------------------------------------------------------
# 6. truth filter accounting (rows) and the manifest-only audit (no rows)
# ---------------------------------------------------------------------------
def _tri(series) -> np.ndarray:
    """True -> 1, False -> 0, missing -> -1."""
    vals = series.to_numpy(dtype=object)
    out = np.full(len(vals), -1, dtype=np.int8)
    for i, v in enumerate(vals):
        if v is True or v == 1:
            out[i] = 1
        elif v is False or v == 0:
            out[i] = 0
        elif isinstance(v, str):
            s = v.strip().lower()
            if s == "true":
                out[i] = 1
            elif s == "false":
                out[i] = 0
    return out


def truth_filter_stats(ladders) -> Dict[str, Any]:
    """Market-level truth filter (search-frame rule) counted per market and per city-day. Prints no label."""
    if ladders.empty:
        return {"markets": 0, "city_days": 0, "markets_dropped": 0, "city_days_affected": 0,
                "city_days_fully_dropped": 0, "fraction_city_days_affected": None,
                "criterion_lt_10pct": None}
    first = ladders.groupby("market_ticker", sort=True).first().reset_index()
    result_ok = first["result"].astype(str).str.strip().str.lower().isin(RESULT_LABELS).to_numpy()
    payoff_bad = _tri(first["payoff_matches_kalshi"]) == 0
    truth_bad = _tri(first["truth_agrees"]) == 0
    dropped = (~result_ok) | payoff_bad | truth_bad
    cd_key = first["city"].astype(str) + "|" + first["target_date"].astype(str)
    keys, inv = np.unique(cd_key.to_numpy(), return_inverse=True)
    n_cd = int(keys.shape[0])
    any_drop = np.zeros(n_cd, dtype=bool)
    all_drop = np.ones(n_cd, dtype=bool)
    np.logical_or.at(any_drop, inv, dropped)
    np.logical_and.at(all_drop, inv, dropped)
    frac = float(any_drop.sum() / n_cd) if n_cd else None
    return {
        "markets": int(len(first)),
        "city_days": n_cd,
        "markets_dropped": int(dropped.sum()),
        "markets_dropped_result_unsettled": int((~result_ok).sum()),
        "markets_dropped_payoff_mismatch": int(payoff_bad.sum()),
        "markets_dropped_truth_disagree": int(truth_bad.sum()),
        "city_days_affected": int(any_drop.sum()),
        "city_days_fully_dropped": int(all_drop.sum()),
        "fraction_city_days_affected": frac,
        "criterion_lt_10pct": (frac < TRUTH_FILTER_MAX_DROP) if frac is not None else None,
        "rule": "drop a market when result not in {yes,no} OR payoff_matches_kalshi == False OR truth_agrees == False",
        "payoff_mismatch_tickers_dropped": sorted(first.loc[payoff_bad, "market_ticker"].astype(str).tolist()),
    }


def _count(v: Any) -> int:
    """A manifest tally: an int, or a list of records (counted, never inspected)."""
    if isinstance(v, (list, tuple, dict)):
        return len(v)
    return int(v or 0)


def audit_root(root: Union[str, Path], repo_root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Truth-filter drop fraction from ``manifest.json`` COUNTS only. No CSV is opened, nothing is written."""
    from src.backtest.sealed_roots import SEALED_MARKER

    r = Path(root)
    manifest = r / MANIFEST
    if not manifest.is_file():
        raise UnsealRefused(f"{_relpath(r, repo_root)} has no {MANIFEST}; the audit reads manifest metadata only")
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    days = doc.get("days") or []
    n_days = n_markets = n_settled = n_payoff_checked = n_payoff_matched = 0
    n_truth_checked = n_truth_disagree = days_affected = days_fully_dropped = 0
    for d in days:
        if d.get("empty") or not int(d.get("rows") or 0):
            continue
        n_days += 1
        details = d.get("market_detail") or []
        m = _count(d.get("markets")) or len(details)
        settled = sum(1 for md in details if str(md.get("result") or "").strip().lower() in RESULT_LABELS)
        pc, pm = _count(d.get("payoff_checked")), _count(d.get("payoff_matched"))
        tc, td = _count(d.get("truth_checked")), _count(d.get("truth_disagreements"))
        n_markets += m
        n_settled += settled
        n_payoff_checked += pc
        n_payoff_matched += pm
        n_truth_checked += tc
        n_truth_disagree += td
        unsettled = max(0, m - settled)
        mismatched = max(0, pc - pm)
        if unsettled or mismatched or td:
            days_affected += 1
        if (unsettled >= m and m) or td:
            days_fully_dropped += 1
    frac = (days_affected / n_days) if n_days else None
    sums = r / SHA256SUMS
    digest = None
    if sums.is_file() and (r / MANIFEST).is_file():
        try:
            dr, series = manifest_metadata(r)
            digest = root_digest_of(parse_sha256sums(sums), dr, series)
        except UnsealRefused:
            digest = None
    return {
        "root": _relpath(r, repo_root),
        "sealed_marker_present": (r / SEALED_MARKER).is_file(),
        "sha256sums_present": sums.is_file(),
        "sha256sums_digest": sha256_bytes_of(sums) if sums.is_file() else None,
        "root_digest": digest,
        "manifest_generated_at_utc": doc.get("generated_at_utc"),
        "date_range": doc.get("date_range"),
        "city_days_with_rows": n_days,
        "markets": n_markets,
        "markets_result_settled": n_settled,
        "markets_result_unsettled": max(0, n_markets - n_settled),
        "payoff_checked": n_payoff_checked,
        "payoff_matched": n_payoff_matched,
        "payoff_mismatched": max(0, n_payoff_checked - n_payoff_matched),
        "truth_checked": n_truth_checked,
        "truth_disagreements": n_truth_disagree,
        "city_days_affected": days_affected,
        "city_days_fully_dropped": days_fully_dropped,
        "fraction_city_days_affected": frac,
        "criterion_lt_10pct": (frac < TRUTH_FILTER_MAX_DROP) if frac is not None else None,
        "reads": "manifest.json counts only; no CSV opened; no unseal line written",
        "caveat": LABEL_LEAK_CAVEAT,
    }


def render_audit(a: Dict[str, Any]) -> str:
    frac = a.get("fraction_city_days_affected")
    verdict = a.get("criterion_lt_10pct")
    return "\n".join([
        f"holdout audit (manifest metadata only) -- {a['root']}",
        f"  SEALED marker: {a['sealed_marker_present']}   SHA256SUMS: {a['sha256sums_present']} "
        f"file {str(a.get('sha256sums_digest') or '')[:16]}   root_digest {str(a.get('root_digest') or '')[:16]}",
        f"  manifest generated_at_utc: {a.get('manifest_generated_at_utc')}   date_range: {a.get('date_range')}",
        f"  city-days with rows: {a['city_days_with_rows']}   markets: {a['markets']} "
        f"(result settled {a['markets_result_settled']}, unsettled {a['markets_result_unsettled']})",
        f"  payoff checked/matched/mismatched: {a['payoff_checked']}/{a['payoff_matched']}/{a['payoff_mismatched']}",
        f"  truth checked/disagreements: {a['truth_checked']}/{a['truth_disagreements']}",
        f"  truth filter would touch {a['city_days_affected']} city-days "
        f"({'n/a' if frac is None else f'{100 * frac:.2f} %'}; fully dropped {a['city_days_fully_dropped']}) "
        f"-- criterion < 10 %: {'n/a' if verdict is None else ('PASS' if verdict else 'FAIL')}",
        f"  {a['reads']}",
        f"  caveat: {a['caveat']}",
    ])


# ---------------------------------------------------------------------------
# 7. frame building on the unsealed tape (search-frame semantics)
# ---------------------------------------------------------------------------
FrameBuilder = Callable[..., Tuple[Any, Any]]


def _frame_kwargs_from_spec(spec: P.PromotedSpec, embargo_days: int) -> Dict[str, Any]:
    return {
        "availability_lag_min": int(spec.availability_lag_min),
        "sigma_cap": float(spec.sigma_cap),
        "adverse_fill": float(spec.adverse_fill),
        "contracts": int(spec.contracts_frame),
        "embargo_days": int(embargo_days),
        "source": str(spec.forecast_source),
    }


def forecast_archive_candidates(repo_root: Path = REPO_ROOT) -> List[Path]:
    """``data/forecast_archive`` first, then every other ``data/forecast_archive*`` directory, sorted."""
    base = Path(repo_root) / "data"
    main = base / "forecast_archive"
    others = sorted(p for p in base.glob("forecast_archive*") if p.is_dir() and p != main)
    return ([main] if main.is_dir() else []) + others


def gfs_mex_coverage(archive_dir: Path) -> Optional[Tuple[str, str]]:
    """(min, max) target_date of ``forecast_series_gfs_mex.csv`` under ``archive_dir``; None when absent/empty."""
    import pandas as pd

    p = Path(archive_dir) / "forecast_series_gfs_mex.csv"
    if not p.is_file():
        return None
    col = pd.read_csv(p, usecols=["target_date"])["target_date"].astype(str)
    if col.empty:
        return None
    return str(col.min())[:10], str(col.max())[:10]


def select_forecast_archive_dir(date_range: Sequence[str], explicit: Optional[Union[str, Path]] = None,
                                repo_root: Path = REPO_ROOT) -> Dict[str, Any]:
    """[RT2-4b] The archive dir whose gfs_mex series covers ``date_range`` (explicit dir validated the same way).

    Refuses when none covers it -- called BEFORE the unseal so nothing is spent.
    """
    start, end = str(date_range[0]), str(date_range[1])
    cands = [Path(explicit)] if explicit else forecast_archive_candidates(repo_root)
    tried: List[Dict[str, Any]] = []
    for c in cands:
        cov = gfs_mex_coverage(c) if c.is_dir() else None
        covers = bool(cov and cov[0] <= start and cov[1] >= end)
        tried.append({"dir": _relpath(c, repo_root), "gfs_mex_coverage": cov, "covers": covers})
        if covers:
            return {"dir": str(c), "relpath": _relpath(c, repo_root), "gfs_mex_coverage": list(cov),
                    "root_dates": [start, end], "explicit": bool(explicit), "tried": tried}
    raise UnsealRefused(
        f"no forecast archive covers the root's dates {start}..{end} with a gfs_mex series "
        f"({'explicit ' + str(explicit) if explicit else 'candidates data/forecast_archive*'}: {tried}); "
        "backfill the archive (scripts/backfill_forecasts.py) before spending a sealed look"
    )


def default_frame_builder(ladders, *, spec: P.PromotedSpec, embargo_days: int, root: Path,
                          fee_regime_path: Optional[str] = None,
                          forecast_archive_dir: Optional[str] = None) -> Tuple[Any, Any]:
    """(frame, gefs_twin) on the unsealed tape with the search frame's hardening.

    ``forecast_archive_dir`` is the dir ``select_forecast_archive_dir`` chose
    (or the operator passed); ``None`` means the evaluator's default
    (``data/forecast_archive``). The gefs twin is best-effort: ANY failure to
    build it (empty after hardening, no gefs vintage for these dates, ...)
    leaves ``twin=None`` with the reason in ``provenance["gefs_twin_unavailable"]``.
    """
    from src.factory import fees as fees_mod
    from src.factory import frame as fr
    from src.factory.lanes import weather as W

    kw = _frame_kwargs_from_spec(spec, embargo_days)
    regime = fees_mod.load_regime(fee_regime_path or fees_mod.DEFAULT_REGIME_PATH)
    if spec.fee.regime_sha256 and regime.sha256 != spec.fee.regime_sha256:
        raise HoldoutAbort(
            f"fee regime {regime.sha256[:12]} differs from the promoted spec's {spec.fee.regime_sha256[:12]}; "
            "the fee schedule is part of the pre-registration"
        )
    common = dict(root=str(root), embargo_days=kw["embargo_days"], contracts=kw["contracts"],
                  adverse_fill=kw["adverse_fill"], availability_lag_min=kw["availability_lag_min"],
                  forecast_archive_dir=(str(forecast_archive_dir) if forecast_archive_dir else None))
    hardening = dict(
        availability_lag_min=kw["availability_lag_min"], truth_filter=True, sigma_cap=kw["sigma_cap"],
        fold_sandbox_admissible=True, cutoff=None, fee_regime=regime,
        adverse_fill=kw["adverse_fill"], contracts=kw["contracts"],
    )
    opp = W.build_opportunities_from_ladders(ladders, kw["source"], **common)
    frame = fr.from_opportunity_frame(opp, name=f"holdout_e{kw['embargo_days']}", **hardening)
    frame.provenance["forecast_archive_dir"] = common["forecast_archive_dir"]
    del opp
    try:
        # [RT2-4a] any failure to BUILD the twin (no gefs vintage for these dates, every row above the sigma
        # cap, ...) is recorded, never raised: a spent look is not aborted for the ex-ante twin.
        opp_g = W.build_opportunities_from_ladders(ladders, "gefs", **common)
        twin = fr.build_gefs_twin(frame, opp_g, **hardening)
        del opp_g
    except Exception as exc:  # noqa: BLE001
        twin = None
        frame.provenance["gefs_twin_unavailable"] = f"{type(exc).__name__}: {exc}"
    return frame, twin


def drop_payoff_mismatch_markets(ladders) -> Tuple[Any, List[str]]:
    """Remove markets whose recorded result does not reproduce the payoff (the search frame aborts on them)."""
    bad = _tri(ladders["payoff_matches_kalshi"]) == 0
    if not bad.any():
        return ladders, []
    tickers = sorted(set(ladders.loc[bad, "market_ticker"].astype(str)))
    keep = ~ladders["market_ticker"].astype(str).isin(tickers).to_numpy()
    out = ladders.loc[keep].reset_index(drop=True)
    out.attrs.update(ladders.attrs)
    return out, tickers


# ---------------------------------------------------------------------------
# 8. evaluation of one genome on a frame: kernel + registry constraints + section 5.8 gates + R3 map
# ---------------------------------------------------------------------------
def _gate(passed: Optional[bool], value: Any = None, threshold: Any = None, note: str = "") -> Dict[str, Any]:
    return {"pass": (None if passed is None else bool(passed)), "value": _safe(value), "threshold": threshold,
            "note": note}


def check_constraints(res: fitness.FitnessResult, thr: Dict[str, float]) -> Optional[str]:
    """``fitness.check_constraints`` with the family's pre-committed thresholds (same order, same codes)."""
    if res.trades <= 0:
        return fitness.REASON_NO_TRADES
    if res.trades < thr["min_trades"]:
        return fitness.REASON_MIN_TRADES
    if res.dates < thr["min_dates_frac"] * res.n_dates_in_mask:
        return fitness.REASON_MIN_DATES
    if res.cities < thr["min_cities"]:
        return fitness.REASON_MIN_CITIES
    if not (res.worst_date_pnl >= thr["worst_date_pnl_min"]):
        return fitness.REASON_WORST_DATE
    if res.n_active_clauses is not None and res.n_active_clauses > thr["max_clauses"]:
        return fitness.REASON_MAX_CLAUSES
    if res.gefs_twin_realized == res.gefs_twin_realized and res.gefs_twin_realized < thr["gefs_twin_min"]:
        return fitness.REASON_GEFS_TWIN
    if res.bss_trades == res.bss_trades and res.bss_trades < thr["bss_trades_min"]:
        return fitness.REASON_BSS
    return None


def _per_date_mean(F, rows: np.ndarray, values: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    tdc = F.visible["target_date_code"][rows]
    return fitness.group_mean_kahan(tdc, values, F.n_dates)


def price_sweep(F, res: fitness.FitnessResult, deltas: Sequence[float] = PRICE_SWEEP) -> Dict[str, Any]:
    """Date-clustered mean with ``price_paid = quote + delta`` (fee held at the frame's value; rows > 0.99 dropped)."""
    out: Dict[str, Any] = {}
    rows = res.trade_rows
    if rows.size == 0:
        return {f"+{d:.2f}": {"mean": None, "n_dates": 0, "sign_positive": False} for d in deltas}
    quote = F.visible["quote"][rows]
    fee = F.visible["fee_per_contract"][rows]
    won = F.hidden["won"][rows].astype(np.float64)
    for d in deltas:
        price = quote + float(d)
        ok = np.isfinite(price) & (price <= 0.99) & np.isfinite(fee)
        if not ok.any():
            out[f"+{d:.2f}"] = {"mean": None, "n_dates": 0, "n_trades": 0, "sign_positive": False}
            continue
        realized = won[ok] - price[ok] - fee[ok]
        _codes, vals = _per_date_mean(F, rows[ok], realized)
        m = float(vals.mean())
        out[f"+{d:.2f}"] = {"mean": m, "n_dates": int(vals.shape[0]), "n_trades": int(ok.sum()),
                            "sign_positive": bool(m > 0)}
    return out


def paired_vs_nofilter(F, res: fitness.FitnessResult, *, n_boot: int, seed: int) -> Dict[str, Any]:
    """Per-date (g_k - B_k) against the no-filter NO baseline on the dates g traded (B = 0 where absent)."""
    base = G.SEEDS["nofilter_no"]
    r_b = fitness.score(F, G.to_mask(base, F), constraints=False, n_boot=n_boot, seed=seed, label="nofilter_no")
    b_map = {int(k): float(v) for k, v in zip(r_b.per_date_codes, r_b.per_date_pnl)} if r_b.trades else {}
    d = np.asarray([float(v) - b_map.get(int(k), 0.0) for k, v in zip(res.per_date_codes, res.per_date_pnl)],
                   dtype=np.float64)
    st = pooled_stats(d, n_boot=n_boot, seed=seed)
    return {"baseline": "nofilter_no", "baseline_trades": int(r_b.trades),
            "baseline_realized": (float(r_b.realized) if r_b.trades else None),
            "rule": "per-date (g_k - B_k) on the dates g traded; B = 0 where it did not trade", **st}


def tail_ratio(F, dates: Sequence[str]) -> Dict[str, Any]:
    from src.factory import report as report_mod

    try:
        tr = report_mod._tail_ratio(types.SimpleNamespace(search=F), list(dates))
    except Exception as exc:  # a frame without the city-day table (fixtures) -> not computable
        return {"n_city_days": 0, "ratio": None, "error": f"{type(exc).__name__}: {exc}"}
    tr = dict(tr)
    tr.pop("gate_applicable", None)
    tr.pop("note", None)
    return tr


def evaluate_genome(
    F,
    twin,
    genome: G.Genome,
    *,
    thresholds: Optional[Dict[str, float]] = None,
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
    rebuild_embargo2: Optional[Callable[[], Tuple[Any, Any]]] = None,
) -> Dict[str, Any]:
    """Kernel result + one-sided p + registry constraints + every section 5.8 gate + the REVIVAL map."""
    thr = thresholds or {k: float(v) for k, v in THRESHOLD_FALLBACKS.items()}
    res = fitness.score(F, G.to_mask(genome, F), twin=twin, genome=genome, n_boot=n_boot, seed=seed,
                        constraints=False, label=genome.name)
    reason = check_constraints(res, thr)
    res.constraint_reason = reason
    if reason is not None:
        res.fit = fitness.NEG_INF
    p = MP.one_sided_p(res.per_date_pnl, n_boot=n_boot, seed=seed) if res.trades else math.nan
    gates: Dict[str, Dict[str, Any]] = {}
    gates["constraints"] = _gate(reason is None, reason, {k: thr[k] for k in (
        "min_trades", "min_dates_frac", "min_cities", "worst_date_pnl_min", "max_clauses", "gefs_twin_min",
        "bss_trades_min")}, "registry-line hard constraints (section 5.6)")
    gates["boot_lo_gt0"] = _gate(res.trades > 0 and res.boot_lo > thr["pooled_boot_lo_gt"], res.boot_lo,
                                 f"> {thr['pooled_boot_lo_gt']}", "date-clustered 95 % bootstrap lower bound")
    if res.trades:
        paired = paired_vs_nofilter(F, res, n_boot=n_boot, seed=seed)
        gates["paired_vs_nofilter_lo_gt0"] = _gate(paired["boot_lo"] is not None and paired["boot_lo"] > 0,
                                                   paired["boot_lo"], "> 0", "HANDOFF rule 2")
        sweep = price_sweep(F, res)
        for k, v in sweep.items():
            gates[f"price{k}_sign"] = _gate(v["sign_positive"], v["mean"], "> 0", f"price_paid = quote {k}")
        tr = tail_ratio(F, [str(F.dates[int(c)]) for c in res.per_date_codes])
        ratio = tr.get("ratio")
        in_range = ratio is not None and ratio == ratio and TAIL_RATIO_RANGE[0] <= ratio <= TAIL_RATIO_RANGE[1]
        gates["tail_ratio_in_range"] = _gate(in_range, ratio, list(TAIL_RATIO_RANGE),
                                             f"|z| >= {TAIL_Z}: {tr.get('n_abs_z_ge_2p5')} of {tr.get('n_city_days')} "
                                             f"city-days, expected {tr.get('expected_count')}")
        bss = res.bss_trades
        gates["bss_trades_ge0"] = _gate(bss == bss and bss >= 0, bss, ">= 0",
                                        "section 5.8 promotion-time gate (R3 Brier beats market); distinct from the "
                                        f"registry constraint bss_trades_min = {thr['bss_trades_min']}")
        gates["point_estimate_ge_4c"] = _gate(res.realized >= POINT_ESTIMATE_MIN, res.realized, f">= {POINT_ESTIMATE_MIN}",
                                              "R3 #5")
        gates["cities_ge3"] = _gate(res.cities >= thr["min_cities"], res.cities, f">= {thr['min_cities']}")
        gtr = res.gefs_twin_realized
        twin_note = "R3 #2 ex-ante disqualifier (gefs realized on the same trade keys)"
        unavailable = (F.provenance or {}).get("gefs_twin_unavailable") if twin is None else None
        if unavailable:
            twin_note = (f"gefs twin UNAVAILABLE ({unavailable}); no gefs trade set exists on these keys -- whether "
                         "the sigma cap counts as the ex-ante disqualifier is an owner ruling, not assumed here")
        gates["gefs_twin_ge0"] = _gate(gtr == gtr and gtr >= thr["gefs_twin_min"], gtr, f">= {thr['gefs_twin_min']}",
                                       twin_note)
    else:
        paired, sweep, tr = None, None, None
        for k in ("paired_vs_nofilter_lo_gt0", "price+0.02_sign", "price+0.03_sign", "tail_ratio_in_range",
                  "bss_trades_ge0", "point_estimate_ge_4c", "cities_ge3", "gefs_twin_ge0"):
            gates[k] = _gate(False, None, None, "no trades")
    if rebuild_embargo2 is not None:
        try:
            F2, twin2 = rebuild_embargo2()
            r2 = fitness.score(F2, G.to_mask(genome, F2), twin=twin2, genome=genome, n_boot=n_boot, seed=seed,
                               constraints=False, label=f"{genome.name}@embargo{SENSITIVITY_EMBARGO_DAYS}")
            gates["embargo_2_sign"] = _gate(r2.trades > 0 and r2.realized > 0, r2.realized, "> 0",
                                            f"frame rebuilt with embargo_days = {SENSITIVITY_EMBARGO_DAYS}; "
                                            f"trades {r2.trades}, dates {r2.dates}")
        except Exception as exc:
            gates["embargo_2_sign"] = _gate(False, None, "> 0", f"rebuild failed: {type(exc).__name__}: {exc}")
    else:
        gates["embargo_2_sign"] = _gate(False, None, "> 0", "no embargo-2 rebuild available; not computed = not passed")
    frozen = str(getattr(genome, "source", FROZEN_SOURCE)) == FROZEN_SOURCE
    r3 = {
        "1_frozen_source_ci_lo_gt0": _gate(frozen and gates["boot_lo_gt0"]["pass"], res.boot_lo, "> 0",
                                           f"source {getattr(genome, 'source', '?')} (frozen {FROZEN_SOURCE}); "
                                           "sigma cap applied pre-selection; one entry per market"),
        "2_gefs_realized_ge0": gates["gefs_twin_ge0"],
        "3_beats_nofilter_baseline": gates["paired_vs_nofilter_lo_gt0"],
        "4_tail_ratio_in_range": gates["tail_ratio_in_range"],
        "5_point_estimate_ge_4c": gates["point_estimate_ge_4c"],
        "6_cold_season_month": _gate(None, None, f">= {COLD_SEASON_MIN_DATES} target dates in months {COLD_SEASON_MONTHS}",
                                     "PENDING -- R5 accrues after Nov 7; re-evaluated once by `score --r5-check`"),
    }
    return {
        "genome_id": P.genome_id_for(P.genome_json_for(genome)),
        "name": genome.name,
        "source": getattr(genome, "source", None),
        "n_active_clauses": G.n_active_clauses(genome),
        "kernel": {
            "trades": int(res.trades), "markets": int(res.markets), "city_days": int(res.city_days),
            "dates": int(res.dates), "n_dates_in_frame": int(F.n_dates),
            "realized": res.realized, "realized_se": res.realized_se, "t_stat": res.t_stat,
            "boot_lo": res.boot_lo, "boot_hi": res.boot_hi, "win_rate": res.win_rate,
            "mean_price_paid": res.mean_price_paid, "mean_fee": res.mean_fee,
            "losing_dates": int(res.losing_dates), "worst_date_pnl": res.worst_date_pnl,
            "cities": int(res.cities), "bss_trades": res.bss_trades, "gefs_twin_realized": res.gefs_twin_realized,
            "modelled_ev_diagnostic_only": res.modelled_ev, "constraint_reason": res.constraint_reason,
            "phenotype_hash": res.phenotype_hash, "fill_opportunity_rate": res.fill_opportunity_rate,
        },
        "one_sided_p": p,
        "per_date_pnl": {str(F.dates[int(c)]): float(v) for c, v in zip(res.per_date_codes, res.per_date_pnl)},
        "paired_vs_nofilter": paired,
        "price_sweep": sweep,
        "tail_ratio": tr,
        "gates": gates,
        "r3": r3,
        "thresholds": thr,
    }


# ---------------------------------------------------------------------------
# 9. Holm across finalists and all registry families  [RT1-5]
# ---------------------------------------------------------------------------
def _family_prior_p(reg: Registry, family: str, root_digest: str) -> Tuple[float, str]:
    """(p, provenance) for a non-finalist family: this-root p > pooled-validation p > unknown (1.0)."""
    lines = [ln for ln in reg.lines() if ln.get("family") == family]
    for ln in reversed(lines):
        for _kind, block in _evidence_blocks(ln):
            if block.get("root_digest") == root_digest and block.get("one_sided_p") is not None:
                return float(block["one_sided_p"]), "recorded p on this root"
    for ln in reversed(lines):
        ev = ln.get("evidence") or {}
        if ev.get("pooled_one_sided_p") is not None:
            return float(ev["pooled_one_sided_p"]), "pooled-validation one-sided p"
    return UNKNOWN_FAMILY_P, "no recorded p; counted as a test at p = 1.0"


def holm_across_registry(reg: Registry, family: str, finalist_ps: Dict[str, float], root_digest: str,
                         alpha: float = 0.05) -> Dict[str, Any]:
    pvals: Dict[str, float] = {}
    prov: Dict[str, str] = {}
    for gid, p in finalist_ps.items():
        key = f"{family}:{gid}"
        pvals[key] = float(p) if p == p else 1.0
        prov[key] = "finalist one-sided p on this root" + ("" if p == p else " (no trades -> 1.0)")
    for other in reg.families():
        if other == family:
            continue
        p_o, why = _family_prior_p(reg, other, root_digest)
        pvals[other] = p_o
        prov[other] = why
    adj = MP.holm(pvals, alpha=alpha)
    entries = {}
    for k in pvals:
        e = dict(adj[k], provenance=prov[k])
        e["reject"] = bool(e["p_adj"] == e["p_adj"] and e["p_adj"] < alpha)  # strict: PRD says "< 0.05"
        entries[k] = e
    return {"alpha": float(alpha), "m": len(pvals), "p": pvals, "entries": entries,
            "rule": "Holm step-down across the finalists and every other registry family (section 6.3); reject iff p_adj < alpha"}


# ---------------------------------------------------------------------------
# 10. the shared evaluation on a root
# ---------------------------------------------------------------------------
@dataclass
class Outcome:
    exit_code: int
    verdicts: Dict[str, str] = field(default_factory=dict)
    result_sha256: Optional[str] = None
    report_path: Optional[Path] = None
    record: Optional[UnsealRecord] = None
    doc: Dict[str, Any] = field(default_factory=dict)


def _load_family_config(path: Path) -> Dict[str, Any]:
    import yaml

    from src.factory.fees import sha256_file as _norm_sha

    if not path.is_file():
        raise UnsealRefused(f"family config {path} does not exist")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["_config_sha256"] = _norm_sha(str(path))
    return cfg


def _fmt(v: Any, nd: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, (float, np.floating)):
        return "-" if v != v else f"{float(v):+.{nd}f}"
    return str(v)


def _print_numbers(out: Callable[[str], None], evals: Dict[str, Dict[str, Any]], holm: Dict[str, Any],
                   truth: Dict[str, Any], verdicts: Dict[str, str], thresholds_source: str) -> None:
    tf = truth.get("fraction_city_days_affected")
    out(f"thresholds: {thresholds_source} (search-window quantities {SEARCH_WINDOW_ONLY} are carried from the F2 "
        "report, not recomputed here)")
    out(f"truth filter: dropped {truth.get('markets_dropped')} of {truth.get('markets')} markets; "
        f"touched {truth.get('city_days_affected')} of {truth.get('city_days')} city-days "
        f"({'n/a' if tf is None else f'{100 * tf:.2f} %'}) -- criterion < 10 %: "
        f"{'n/a' if truth.get('criterion_lt_10pct') is None else ('PASS' if truth['criterion_lt_10pct'] else 'FAIL')}")
    for gid, e in evals.items():
        k = e["kernel"]
        h = holm["entries"].get(f"{e['family']}:{gid}", {})
        out(f"{gid} {e['name']}: trades {k['trades']} dates {k['dates']} cities {k['cities']} "
            f"pooled OOS mean {_fmt(k['realized'])} boot [{_fmt(k['boot_lo'])}, {_fmt(k['boot_hi'])}] "
            f"one-sided p {_fmt(e['one_sided_p'], 4)} Holm p_adj {_fmt(h.get('p_adj'), 4)} "
            f"(m={holm['m']}, alpha={holm['alpha']}) -> {verdicts[gid]}")
        for name, g in e["gates"].items():
            out(f"    gate {name:<28} {'PASS' if g['pass'] else ('n/a' if g['pass'] is None else 'FAIL'):<4} "
                f"value {_fmt(g['value'])} threshold {g['threshold']}")
        for name, g in e["r3"].items():
            st = "PENDING" if g["pass"] is None else ("PASS" if g["pass"] else "FAIL")
            out(f"    R3 {name:<30} {st:<7} {g.get('note', '')}")


def _verdict_for(e: Dict[str, Any], holm_entry: Dict[str, Any]) -> Tuple[str, List[str]]:
    failing = [k for k, g in e["gates"].items() if g["pass"] is False]
    if not holm_entry.get("reject"):
        failing.append("holm_p_adj_lt_alpha")
    return ("PASS" if not failing else "HALT"), failing


def _preflight(command: str, root: Union[str, Path], unseal_tag: Optional[str], family: str,
               genome_ids: Sequence[str], paths: HoldoutPaths, as_of: Optional[str], now: Optional[_dt.datetime],
               select_archive: bool = False, forecast_archive_dir: Optional[str] = None):
    """Every refusal, in order; returns what the evaluation needs. Nothing is written here."""
    ratified = assert_tag_ratified(unseal_tag, paths.revival_doc, paths.repo_root)
    doc_rel, doc_sha = resolve_revival_doc(paths.revival_doc, paths.repo_root)
    integrity = {"unseal_log": assert_append_only(paths.unseal_log, paths.repo_root),
                 "registry": assert_append_only(paths.registry, paths.repo_root)}
    reg = Registry(paths.registry, repo_root=paths.repo_root)
    want = ("RATIFIED",) if command == "score-r5" else ("PROPOSED",)
    for gid in genome_ids:
        assert_genome_eligible(reg, family, gid, want=want, command=command)
    fam_line = reg.family_line(family) or {}
    cfg = _load_family_config(paths.family_config)
    if str(cfg.get("family")) != family:
        raise UnsealRefused(f"family config {paths.family_config.name} is for {cfg.get('family')!r}, not {family!r}")
    if fam_line.get("config_sha256") and cfg["_config_sha256"] != fam_line["config_sha256"]:
        raise UnsealRefused(
            f"{paths.family_config.name} hashes to {cfg['_config_sha256'][:12]} but the registry family line "
            f"says {str(fam_line['config_sha256'])[:12]}; thresholds are pre-committed, edit = new family"
        )
    specs = {gid: load_spec(gid, paths.promoted_dir, family, fam_line.get("config_sha256")) for gid in genome_ids}
    if command == "score":
        for gid in genome_ids:
            if not holdout_pass_lines(reg, family, gid):
                raise UnsealRefused(
                    f"genome {gid} has no PASS holdout evidence line in the registry; section 9 orders holdout-B "
                    "(step 2) before the R3 score (step 3), and a virgin root is not spent before that"
                )
    if as_of is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(as_of)):
        raise UnsealRefused(f"--as-of {as_of!r} is not YYYY-MM-DD")
    seal = inspect_root_seal(root, paths.repo_root)
    assert_root_purpose(command, seal)
    if as_of is not None and not any(s <= as_of for s in seal.csv_stems):
        raise UnsealRefused(f"--as-of {as_of}: no day file in {seal.relpath} is dated on or before it")
    log_lines = read_unseal_log(paths.unseal_log)
    assert_unseal_allowed(log_lines, reg.lines(), command=command, family=family, genome_ids=genome_ids,
                          root=seal.relpath, root_digest=seal.root_digest, now=now)
    thr, thr_source = family_thresholds(fam_line)
    archive = None
    if select_archive:
        stems = [d for d in seal.csv_stems if as_of is None or d <= as_of]
        archive = select_forecast_archive_dir((min(stems), max(stems)), forecast_archive_dir, paths.repo_root)
    return types.SimpleNamespace(ratified=ratified, doc_rel=doc_rel, doc_sha=doc_sha, integrity=integrity, reg=reg,
                                 fam_line=fam_line, cfg=cfg, specs=specs, seal=seal, thr=thr, thr_source=thr_source,
                                 archive=archive)


def _record_for(command: str, pf, unseal_tag: str, family: str, genome_ids: Sequence[str], as_of: Optional[str]) -> UnsealRecord:
    s = pf.seal
    return UnsealRecord(
        command=command, tag=str(unseal_tag), ratified_date=pf.ratified, doc_path=pf.doc_rel, doc_sha256=pf.doc_sha,
        family=family, genome_ids=list(genome_ids), root=s.relpath, root_digest=s.root_digest,
        sha256sums_digest=s.sha256sums_digest, sha256sums_entries=s.n_entries,
        manifest_date_range=list(s.date_range), series=list(s.series), as_of=as_of,
    )


def evaluate_on_root(
    *,
    command: str,
    root: Union[str, Path],
    unseal_tag: Optional[str],
    family: str,
    genome_ids: Sequence[str],
    paths: HoldoutPaths,
    as_of: Optional[str] = None,
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
    frame_builder: Optional[FrameBuilder] = None,
    out: Callable[[str], None] = print,
    now: Optional[_dt.datetime] = None,
    forecast_archive_dir: Optional[str] = None,
) -> Outcome:
    """The protocol, in the order the docstring states. Refusals raise before any write."""
    pf = _preflight(command, root, unseal_tag, family, genome_ids, paths, as_of, now,
                    select_archive=frame_builder is None, forecast_archive_dir=forecast_archive_dir)
    seal, reg = pf.seal, pf.reg
    embargo_days = int(((pf.cfg.get("frame") or {}).get("embargo_days")) or 1)
    report_path = paths.reports_dir / (
        f"holdout_{family.replace('/', '_')}_{seal.root.name}.json" if command == "holdout"
        else f"score_{genome_ids[0]}_{seal.root.name}.json"
    )
    if report_path.exists():
        raise UnsealRefused(f"{_relpath(report_path, paths.repo_root)} already exists; a second scoring may not exist")

    # -- the unseal line, BEFORE any row is read --------------------------------
    record = append_unseal_line(paths.unseal_log, _record_for(command, pf, unseal_tag, family, genome_ids, as_of),
                                repo_root=paths.repo_root)
    out(f"unseal: line {record.line_no} appended to {_relpath(paths.unseal_log, paths.repo_root)} "
        f"({command}, {family}, root {seal.relpath} digest {seal.root_digest[:12]}, tag {unseal_tag})")

    # -- everything below has spent the look ------------------------------------
    try:
        return _evaluate_after_unseal(command, pf, record, genome_ids, embargo_days, report_path, paths, n_boot, seed,
                                      frame_builder, out)
    except (HoldoutAbort, UnsealRefused):
        raise
    except Exception as exc:  # [RT1-3] any failure after the line is a spent look, named as such
        raise HoldoutAbort(f"{type(exc).__name__}: {exc}", record) from exc
    except BaseException as exc:  # [RT2-3] KeyboardInterrupt / SystemExit: say it is spent, then re-raise
        _print_spent(record, exc)
        raise


def _print_spent(record: UnsealRecord, exc: BaseException) -> None:
    import sys as _sys

    print(f"factory: ABORT -- unseal line {record.line_no} ({record.command}, {record.family}, root {record.root}) "
          f"is already on disk; the look is SPENT and will not be re-granted: interrupted by {type(exc).__name__}",
          file=_sys.stderr, flush=True)


def _evaluate_after_unseal(command, pf, record, genome_ids, embargo_days, report_path, paths, n_boot, seed,
                           frame_builder, out) -> Outcome:
    seal, reg, family = pf.seal, pf.reg, record.family
    sums = verify_sha256sums(seal.root)
    if sums["failed"] or sums["missing"]:
        raise HoldoutAbort(f"SHA256SUMS check failed for {seal.relpath}: failed {sums['failed']} missing {sums['missing']}",
                           record)
    ladders = open_sealed_root(seal.root, record)
    as_of = record.as_of
    if as_of is not None:
        keep = ladders["target_date"].astype(str).str.slice(0, 10) <= as_of
        ladders = ladders.loc[keep.to_numpy()].reset_index(drop=True)
    if ladders.empty:
        raise HoldoutAbort(f"{seal.relpath} yielded no rows" + (f" on or before {as_of}" if as_of else ""), record)
    truth = truth_filter_stats(ladders)
    ladders, payoff_dropped = drop_payoff_mismatch_markets(ladders)
    if frame_builder is None:
        import functools

        builder = functools.partial(default_frame_builder,
                                    forecast_archive_dir=(pf.archive or {}).get("dir"))
    else:
        builder = frame_builder
    cache: Dict[int, Tuple[Any, Any]] = {}

    def _frames(emb: int, spec: P.PromotedSpec) -> Tuple[Any, Any]:
        if emb not in cache:
            cache[emb] = builder(ladders, spec=spec, embargo_days=emb, root=seal.root)
        return cache[emb]

    evals: Dict[str, Dict[str, Any]] = {}
    for gid in genome_ids:
        spec = pf.specs[gid]
        F, twin = _frames(embargo_days, spec)
        e = evaluate_genome(F, twin, spec.genome(), thresholds=pf.thr, n_boot=n_boot, seed=seed,
                            rebuild_embargo2=lambda s=spec: _frames(SENSITIVITY_EMBARGO_DAYS, s))
        e["family"] = family
        e["frame"] = {"name": getattr(F, "name", None), "sha256": (F.provenance or {}).get("frame_sha256"),
                      "n_rows": int(F.n_rows), "n_dates": int(F.n_dates), "n_markets": int(F.n_markets),
                      "dates": [str(F.dates[0]), str(F.dates[-1])] if F.n_dates else [],
                      "ladder_files_sha256": (F.provenance or {}).get("ladder_files_sha256"),
                      "hardening": {k: v for k, v in (F.provenance or {}).items()
                                    if k in ("availability_lag_min", "truth_filter", "sigma_cap",
                                             "fold_sandbox_admissible", "cutoff", "adverse_fill", "contracts",
                                             "dropped_result_unsettled", "dropped_payoff_mismatch",
                                             "dropped_truth_disagree", "dropped_sigma_markets", "rows_kept",
                                             "executable_rows")}}
        evals[gid] = e
    holm = holm_across_registry(reg, family, {gid: e["one_sided_p"] for gid, e in evals.items()},
                                seal.root_digest, alpha=pf.thr["holm_alpha"])
    verdicts: Dict[str, str] = {}
    failing: Dict[str, List[str]] = {}
    for gid, e in evals.items():
        v, f = _verdict_for(e, holm["entries"][f"{family}:{gid}"])
        e["holm"] = holm["entries"][f"{family}:{gid}"]
        verdicts[gid], failing[gid] = v, f
        e["verdict"], e["failing"] = v, f

    result = {
        "command": record.command, "family": family, "root": seal.relpath, "root_digest": seal.root_digest,
        "sha256sums_digest": seal.sha256sums_digest, "sha256sums": sums, "manifest_date_range": list(seal.date_range),
        "series": list(seal.series), "as_of": as_of, "embargo_days": embargo_days, "n_boot": int(n_boot),
        "seed": int(seed), "thresholds": pf.thr, "thresholds_source": pf.thr_source,
        "forecast_archive": ({k: v for k, v in pf.archive.items() if k != "dir"} if pf.archive else
                             {"relpath": None, "note": "frame builder injected; no archive selection"}),
        "search_window_quantities_not_recomputed": list(SEARCH_WINDOW_ONLY),
        "truth_filter": truth, "payoff_mismatch_markets_dropped": payoff_dropped,
        "genomes": evals, "holm": holm, "verdicts": verdicts,
    }
    sha = result_sha256(result)
    out(f"result_sha256 {sha}")
    _print_numbers(out, evals, holm, truth, verdicts, pf.thr_source)
    doc = {
        "result": result, "result_sha256": sha,
        "meta": {"ts": record.ts, "git_rev": record.git_rev, "unseal": record.line(), "unseal_line_no": record.line_no,
                 "append_only_checks": pf.integrity, "caveat": LABEL_LEAK_CAVEAT, "ratified_date": pf.ratified},
    }
    from src.factory.report import write_json

    write_json(report_path, doc)
    out(f"report: {_relpath(report_path, paths.repo_root)}")
    _write_transitions(reg, record.command, family, evals, verdicts, failing, seal, sha, holm, pf.thr_source, out,
                       as_of=as_of)
    any_pass = any(v == "PASS" for v in verdicts.values())
    return Outcome(exit_code=EXIT_PASS if any_pass else EXIT_HALT, verdicts=verdicts, result_sha256=sha,
                   report_path=report_path, record=record, doc=doc)


def _evidence(command: str, e: Dict[str, Any], seal: RootSeal, sha: str, verdicts: Dict[str, str],
              failing: Dict[str, List[str]], holm: Dict[str, Any], thr_source: str) -> Dict[str, Any]:
    k = e["kernel"]
    block = {
        "root_relpath": seal.relpath, "root_digest": seal.root_digest, "sha256sums_digest": seal.sha256sums_digest,
        "as_of": e.get("as_of"), "verdict": e["verdict"], "failing": e["failing"],
        "one_sided_p": e["one_sided_p"], "holm_p_adj": e["holm"].get("p_adj"), "holm_m": holm["m"],
        "holm_alpha": holm["alpha"], "holm_p": holm["p"],
        "pooled_mean": k["realized"], "boot_lo": k["boot_lo"], "boot_hi": k["boot_hi"], "n_dates": k["dates"],
        "trades": k["trades"], "cities": k["cities"], "bss_trades": k["bss_trades"],
        "gefs_twin_realized": k["gefs_twin_realized"], "gates": {n: g["pass"] for n, g in e["gates"].items()},
        "thresholds_source": thr_source, "search_window_quantities_not_recomputed": list(SEARCH_WINDOW_ONLY),
        "finalists_failed": {g: failing[g] for g, v in verdicts.items() if v != "PASS" and g != e["genome_id"]},
    }
    ev = {"result_sha256": sha}
    if command == "holdout":
        ev["holdout"] = block
    else:
        ev["r3"] = dict(block, checks={n: g["pass"] for n, g in e["r3"].items()})
    return _safe(ev)


def _write_transitions(reg: Registry, command: str, family: str, evals, verdicts, failing, seal, sha, holm,
                       thr_source, out: Callable[[str], None], as_of: Optional[str] = None) -> None:
    """[RT1-6] holdout PASS -> PROPOSED (+evidence); score PASS -> RATIFIED; failures -> evidence events,
    HALT only when nothing passes and the family holds no RATIFIED genome."""
    passing = [g for g, v in verdicts.items() if v == "PASS"]
    ratified = ratified_genomes(reg, family)
    try:
        for gid, e in evals.items():
            e["as_of"] = as_of
        for gid in passing:
            new_status = "PROPOSED" if command == "holdout" else "RATIFIED"
            reg.transition(family, new_status, genome_id=gid,
                           evidence=_evidence(command, evals[gid], seal, sha, verdicts, failing, holm, thr_source))
            out(f"registry: {family} {gid} -> {new_status} (evidence.{'holdout' if command == 'holdout' else 'r3'} attached)")
        if command == "holdout" and passing and ratified:
            # Registry.status() is last-transition-wins, and the OPS flow stamps it into gate registrations
            # (paper_spec_hash covers registry_status): re-assert the family's RATIFIED status for the genome
            # that earned it, so a sibling's PROPOSED holdout line cannot demote the family. No new status.
            reg.transition(family, "RATIFIED", genome_id=ratified[0], evidence={
                "reasserted": True, "ratified_genomes": ratified,
                "reason": f"holdout PASS of {passing} wrote PROPOSED line(s); family status re-asserted (no R3 here)"},
                extra={"reasserted": True})  # [RT2-6] top-level flag: never a second ratification
            out(f"registry: {family} RATIFIED re-asserted for {ratified[0]} (status is last-transition-wins)")
        failed = [g for g in evals if g not in passing]
        halt_family = not passing and not ratified
        for gid in failed:
            ev = _evidence(command, evals[gid], seal, sha, verdicts, failing, holm, thr_source)
            if halt_family and gid == failed[-1]:
                ev["all_finalists"] = {g: verdicts[g] for g in evals}
                reg.transition(family, "HALT", genome_id=gid, evidence=ev)
                out(f"registry: {family} -> HALT ({'no finalist passed' if command == 'holdout' else 'R3 #3 failed'}, "
                    f"no RATIFIED genome in the family: {failing[gid]})")
            else:
                reg.evidence(family, genome_id=gid, evidence=ev)
                out(f"registry: {family} {gid} failed ({failing[gid]}); recorded as an evidence event -- the family "
                    f"keeps its status ({'a sibling passed' if passing else 'RATIFIED genome(s) ' + ','.join(ratified)})")
    except RegistryError as exc:
        raise HoldoutAbort(f"registry transition refused: {exc}") from exc


# ---------------------------------------------------------------------------
# 11. R5 re-check (REVIVAL section 5 #6) -- criterion 6 only, once  [RT1-7]
# ---------------------------------------------------------------------------
def run_r5_check(*, genome_id: str, unseal_tag: Optional[str], root: Union[str, Path] = DEFAULT_R3_ROOT,
                 paths: HoldoutPaths = HoldoutPaths(), out: Callable[[str], None] = print,
                 now: Optional[_dt.datetime] = None) -> Outcome:
    """Count cold-season target dates AFTER the recorded ``as_of``; never touches criteria 1-5."""
    family = _family_of_genome(genome_id, paths)
    pf = _preflight("score-r5", root, unseal_tag, family, [genome_id], paths, None, now)
    prior = r3_line(pf.reg, family, genome_id)
    if prior is None:
        raise UnsealRefused(f"genome {genome_id} has no RATIFIED line with evidence.r3; nothing to re-check")
    as_of = (prior.get("evidence") or {}).get("r3", {}).get("as_of")
    if not as_of:
        raise UnsealRefused(f"the R3 score of {genome_id} recorded no --as-of; there is no 'after' to re-evaluate")
    report_path = paths.reports_dir / f"score_r5_{genome_id}_{pf.seal.root.name}.json"
    if report_path.exists():
        raise UnsealRefused(f"{_relpath(report_path, paths.repo_root)} already exists; the R5 re-check is once only")
    record = append_unseal_line(paths.unseal_log, _record_for("score-r5", pf, unseal_tag, family, [genome_id], as_of),
                                repo_root=paths.repo_root)
    out(f"unseal: line {record.line_no} appended ({record.command}, {family}, root {pf.seal.relpath}, after {as_of})")
    try:
        sums = verify_sha256sums(pf.seal.root)  # [RT2-5]
        if sums["failed"] or sums["missing"]:
            raise HoldoutAbort(f"SHA256SUMS check failed for {pf.seal.relpath}: failed {sums['failed']} "
                               f"missing {sums['missing']}", record)
        ladders = open_sealed_root(pf.seal.root, record)
        dates = sorted({str(d)[:10] for d in ladders["target_date"].astype(str) if str(d)[:10] > as_of})
        del ladders
        cold = [d for d in dates if int(d[5:7]) in COLD_SEASON_MONTHS]
        passed = len(cold) >= COLD_SEASON_MIN_DATES
        result = {"command": "score-r5", "family": family, "genome_id": genome_id, "root": pf.seal.relpath,
                  "root_digest": pf.seal.root_digest, "as_of": as_of, "dates_after_as_of": len(dates),
                  "cold_season_dates": len(cold), "cold_season_months": list(COLD_SEASON_MONTHS),
                  "min_dates": COLD_SEASON_MIN_DATES, "criterion_6_pass": passed, "sha256sums": sums,
                  "note": "criteria 1-5 are NOT recomputed; definition of a cold-season month is unratified (module docstring)"}
        sha = result_sha256(result)
        out(f"result_sha256 {sha}")
        out(f"R5: {len(cold)} cold-season target dates after {as_of} (of {len(dates)} dates; need >= "
            f"{COLD_SEASON_MIN_DATES}) -> {'PASS' if passed else 'HALT'}")
        from src.factory.report import write_json

        write_json(report_path, {"result": result, "result_sha256": sha,
                                 "meta": {"ts": record.ts, "git_rev": record.git_rev, "unseal": record.line(),
                                          "unseal_line_no": record.line_no, "caveat": LABEL_LEAK_CAVEAT}})
        ev = {"result_sha256": sha, "r5": dict(result, root_relpath=pf.seal.relpath, verdict="PASS" if passed else "HALT")}
        others = [g for g in ratified_genomes(pf.reg, family) if g != genome_id]
        result["sha256sums"] = sums
        if passed:
            pf.reg.transition(family, "RATIFIED", genome_id=genome_id, evidence=ev)
            out(f"registry: {family} {genome_id} -> RATIFIED confirmed (evidence.r5)")
        elif others:
            pf.reg.evidence(family, genome_id=genome_id, evidence=ev)
            out(f"registry: {family} {genome_id} R5 failed; evidence event only (RATIFIED sibling(s) {others})")
        else:
            pf.reg.transition(family, "HALT", genome_id=genome_id, evidence=ev)
            out(f"registry: {family} -> HALT (R5 cold-season criterion failed)")
        return Outcome(exit_code=EXIT_PASS if passed else EXIT_HALT, verdicts={genome_id: "PASS" if passed else "HALT"},
                       result_sha256=sha, report_path=report_path, record=record, doc={"result": result})
    except (HoldoutAbort, UnsealRefused):
        raise
    except Exception as exc:
        raise HoldoutAbort(f"{type(exc).__name__}: {exc}", record) from exc
    except BaseException as exc:  # [RT2-3]
        _print_spent(record, exc)
        raise


# ---------------------------------------------------------------------------
# 12. entry points the CLI calls
# ---------------------------------------------------------------------------
def run_holdout(*, finalists_path: Union[str, Path], unseal_tag: Optional[str], root: Union[str, Path] = DEFAULT_HOLDOUT_ROOT,
                paths: HoldoutPaths = HoldoutPaths(), n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED,
                frame_builder: Optional[FrameBuilder] = None, out: Callable[[str], None] = print,
                now: Optional[_dt.datetime] = None, forecast_archive_dir: Optional[str] = None) -> Outcome:
    family, ids = load_finalists(finalists_path)
    return evaluate_on_root(command="holdout", root=root, unseal_tag=unseal_tag, family=family, genome_ids=ids,
                            paths=paths, n_boot=n_boot, seed=seed, frame_builder=frame_builder, out=out, now=now,
                            forecast_archive_dir=forecast_archive_dir)


def run_score(*, genome_id: str, unseal_tag: Optional[str], root: Union[str, Path] = DEFAULT_R3_ROOT,
              as_of: Optional[str] = None, paths: HoldoutPaths = HoldoutPaths(), n_boot: int = fitness.DEFAULT_N_BOOT,
              seed: int = fitness.DEFAULT_SEED, frame_builder: Optional[FrameBuilder] = None,
              out: Callable[[str], None] = print, now: Optional[_dt.datetime] = None,
              forecast_archive_dir: Optional[str] = None) -> Outcome:
    family = _family_of_genome(genome_id, paths)
    return evaluate_on_root(command="score", root=root, unseal_tag=unseal_tag, family=family, genome_ids=[genome_id],
                            paths=paths, as_of=as_of, n_boot=n_boot, seed=seed, frame_builder=frame_builder, out=out,
                            now=now, forecast_archive_dir=forecast_archive_dir)


def _family_of_genome(genome_id: str, paths: HoldoutPaths) -> str:
    """The family from the promoted spec (the registry is checked afterwards, against it)."""
    try:
        spec = P.load_promoted(genome_id, directory=str(paths.promoted_dir))
    except P.PromotedSpecError as exc:
        raise UnsealRefused(f"genome {genome_id}: {exc}") from exc
    return str(spec.family)


__all__ = [
    "COLD_SEASON_MIN_DATES", "COLD_SEASON_MONTHS", "EXIT_ABORT", "EXIT_HALT", "EXIT_PASS", "EXIT_REFUSED",
    "HOLDOUT_B_RANGE", "HoldoutAbort", "HoldoutPaths", "LABEL_LEAK_CAVEAT", "MAX_FINALISTS", "MAX_UNSEALS_PER_QUARTER",
    "Outcome", "RATIFICATION_LINE_RE", "THRESHOLD_FALLBACKS", "UnsealRecord", "UnsealRefused", "append_unseal_line",
    "assert_append_only", "assert_genome_eligible", "assert_root_purpose", "assert_tag_ratified",
    "assert_unseal_allowed", "audit_root", "canonical_json", "check_constraints", "evaluate_genome",
    "evaluate_on_root", "family_thresholds", "genome_status", "git_show_head", "holm_across_registry",
    "inspect_root_seal", "load_finalists", "manifest_metadata", "open_sealed_root", "parse_unseal_tag",
    "quarter_of", "r3_line", "ratified_dates", "ratified_dates_in", "ratified_genomes", "read_ratification_text",
    "read_unseal_log", "render_audit", "resolve_revival_doc", "result_sha256", "root_digest_of", "run_holdout",
    "run_r5_check", "run_score", "select_forecast_archive_dir",
    "truth_filter_stats", "verify_sha256sums",
]
