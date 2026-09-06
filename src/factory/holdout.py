"""Sealed-root evaluation (F4): holdout-B finalists and the Sept-Oct R3 score.

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 1.1 (the
``holdout.py`` row), section 5.8 (promotion-time gates), section 6.3 (Holm
across all registry entries), section 9 steps 2-3; ``PRD_STRATEGY_FACTORY.md``
FR-F4.1; ``docs/REVIVAL_2026_09.md`` section 5 (draft R3 criteria 1-6).

The two commands
----------------
``factory.py holdout --finalists FILE --unseal RATIFIED-<date>``
    scores <= 3 finalists of ONE family on the sealed holdout-B root
    (``data/ladders_holdout``, target dates 2026-07-26..08-31) with Holm.
``factory.py score --genome ID --ladders data/ladders_2026-09 --unseal RATIFIED-<date> [--as-of]``
    scores ONE PROPOSED genome on the Sept-Oct R3 root, prints the result
    sha256 BEFORE any number, appends the R3 checks to the registry.

Unseal protocol (every step is pinned by ``tests/test_factory_holdout.py``)
---------------------------------------------------------------------------
1. ``--unseal RATIFIED-<date>`` is mandatory and ``<date>`` must appear on a
   ``RATIFIED <date>`` line of ``docs/REVIVAL_2026_09.md``. Today that file
   says "Nothing here is ratified yet", so the real command refuses.
2. The root must carry the ``SEALED`` marker and a ``SHA256SUMS`` file; a
   root that was never sealed is not a holdout. Every ``<date>.csv`` stem
   must postdate the development set (``sealed_roots.DEV_SET_LAST_DATE``).
3. Finalists: <= 3 per family, each a genome that appears on a PROPOSED (or
   RATIFIED) transition line of a family whose current status is PROPOSED or
   RATIFIED. Family #1 is CLOSED (terminal, ``registry.py``), so its seed
   genomes are refused -- a rerun is a new family (``.../v2``).
4. Budget: <= 3 unseals per calendar quarter (UTC); once per family per root
   for ``holdout``; once per genome per root for either command. A second
   look is refused with the prior ``unseal_log.jsonl`` line quoted verbatim.
5. The unseal line (``reports/factory/unseal_log.jsonl``, append-only: ts,
   non-empty git_rev or abort, the root's SHA256SUMS digest, finalists, tag,
   and the recorded label-leak caveat below) is written BEFORE any price row
   is read. An evaluation that aborts after the line still consumed the look:
   that is the point of write-before-read.

The sealed-root bypass is narrow
--------------------------------
``kalshi_history.load_ladders`` / ``ev_analysis.load_search_ladders`` /
``sealed_roots.assert_frame_not_sealed`` keep refusing the roots. This module
is the only one that (a) calls ``kalshi_history._load_ladders_unchecked``
for scoring and (b) sets ``sealed_roots.SEALED_EVALUATION_ATTR`` on the
tape, and it does so in exactly one function, ``open_sealed_root``, which
refuses to run without an unseal record that has already been appended to
the log. The origin stamp is moved to ``attrs["unsealed_root"]`` so the frame
builder's origin gate is not defeated by a copy of the tape elsewhere.

Frame semantics are the search frame's
--------------------------------------
Same evaluator chain (``lanes.weather.build_opportunities_from_ladders``),
same hardening (``frame.from_opportunity_frame`` with the promoted spec's
availability lag, sigma cap, adverse fill, contracts; ``truth_filter=True``;
``fold_sandbox_admissible=True``; the family config's embargo), the gefs twin
for the ex-ante disqualifier. Two differences, both narrowing: ``cutoff=None``
(irrelevant on a sealed root -- every date postdates the dev set, asserted)
and markets with ``payoff_matches_kalshi == False`` are DROPPED and counted
instead of aborting the frame (the search frame aborts, i.e. never scored
them either). The truth filter therefore keeps ``result in {yes, no}`` AND
``payoff_matches_kalshi != False`` AND ``truth_agrees != False`` (the search
frame's rule); the drop count and the fraction of city-days touched are
printed against the exit criterion (< 10 %).

Verdicts and the registry
-------------------------
``registry.py`` makes HALT a FAMILY-level terminal status and has no
per-genome status, so the least-surprising mapping is:

* ``holdout`` PASS: a transition line re-asserting the family's CURRENT
  status (PROPOSED, or RATIFIED if it already is) with
  ``evidence.holdout`` attached -- no new status is invented; the genome
  stays PROPOSED until ``score`` rules. A finalist that fails while a
  sibling passes is recorded in that evidence (``finalists_failed``) and can
  never be re-scored on that root (once-per-genome-per-root). If NO finalist
  passes the family is HALTed.
* ``score`` PASS -> RATIFIED; any failed R3 check -> HALT #3. ``score``
  requires the genome to be PROPOSED and to carry a PASS holdout evidence
  line (section 9 orders holdout-B before R3; a virgin root is not spent on
  a genome that has not cleared the first one).

Holm (section 6.3: "across all registry entries") is computed over the
finalists' one-sided holdout p-values PLUS one entry per other registry
family: its recorded p on this root if it has one, else its last
pooled-validation ``pooled_one_sided_p``, else ``UNKNOWN_FAMILY_P`` (1.0,
which cannot reject and can only make the finalists' adjustment more
conservative). Alpha 0.05.

R3 #4 (tail ratio in [0.8, 1.25] at |z| >= 2.5) is evaluated literally as
pre-registered. Note for the record: with the Gaussian expected share of
0.0124 per city-day, 148 city-days expect ~1.8 exceedances, so the literal
ratio is coarse (0 -> 0.00, 1 -> 0.54, 2 -> 1.09); the report carries the
counts so the owner can see what the number is made of.

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
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from src.backtest.sealed_roots import (
    DEV_SET_LAST_DATE,
    SEALED_EVALUATION_ATTR,
    SEALED_MARKER,
)
from src.data.kalshi_history import _load_ladders_unchecked
from src.factory import fitness
from src.factory import genome as G
from src.factory import multiplicity as MP
from src.factory import promoted as P
from src.factory.procedure import pooled_stats
from src.factory.registry import TERMINAL, Registry, RegistryError, git_rev

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = Path(os.path.dirname(os.path.dirname(_THIS_DIR)))

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
HOLM_ALPHA = 0.05
UNKNOWN_FAMILY_P = 1.0
FROZEN_SOURCE = "gfs_mex"  # REVIVAL section 4 (R1) -- the source criterion 1 is stated for
ELIGIBLE_STATUSES = ("PROPOSED", "RATIFIED")
SHA256SUMS = "SHA256SUMS"
MANIFEST = "manifest.json"
RESULT_LABELS = ("yes", "no")
UNSEAL_TAG_RE = re.compile(r"^RATIFIED-(\d{4}-\d{2}-\d{2})$")
RATIFIED_LINE_RE = re.compile(r"\bRATIFIED\s+(\d{4}-\d{2}-\d{2})\b")

LABEL_LEAK_CAVEAT = (
    "HANDOFF 2026-09-05 correction: this root's manifest.json exposes days[].market_detail[].result "
    "(every market's outcome label with its bracket bounds) and RECONCILE.md exposes settled highs; "
    "the seal protected the prices, not the outcomes. Any search designed after reading those files "
    "is not cleanly out-of-sample on this root."
)

EXIT_PASS, EXIT_HALT, EXIT_REFUSED = 0, 1, 2


class UnsealRefused(RuntimeError):
    """The unseal protocol refused; nothing was read, nothing was written."""


class HoldoutAbort(RuntimeError):
    """The evaluation aborted AFTER the unseal line was written (the look is spent)."""


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
# 1. the tag and the ratification line
# ---------------------------------------------------------------------------
def ratified_dates(doc_path: Union[str, Path]) -> List[str]:
    """Every ``RATIFIED <date>`` on the revival doc, in file order ([] today)."""
    p = Path(doc_path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    return [m.group(1) for m in RATIFIED_LINE_RE.finditer(text)]


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


def assert_tag_ratified(tag: Optional[str], doc_path: Union[str, Path]) -> str:
    """Return the ratification date the tag names, or refuse."""
    date = parse_unseal_tag(tag)
    dates = ratified_dates(doc_path)
    if date not in dates:
        have = ", ".join(dates) if dates else "none -- the document says nothing is ratified yet"
        raise UnsealRefused(
            f"unseal tag {tag} does not match a 'RATIFIED <date>' line in {_relpath(doc_path)} "
            f"(ratified dates present: {have}); the owner ratifies by amending that document, "
            "not by passing a tag"
        )
    return date


# ---------------------------------------------------------------------------
# 2. the root's seal
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RootSeal:
    root: Path
    relpath: str
    marker_text: str
    sha256sums_digest: str
    n_entries: int
    csv_stems: Tuple[str, ...]


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


def inspect_root_seal(root: Union[str, Path], repo_root: Path = REPO_ROOT) -> RootSeal:
    """Refuse a root without ``SEALED`` + ``SHA256SUMS`` or with dev-set-dated CSVs. Reads no rows."""
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
    return RootSeal(
        root=r,
        relpath=_relpath(r, repo_root),
        marker_text=marker.read_text(encoding="utf-8", errors="replace").strip(),
        sha256sums_digest=sha256_bytes_of(sums),
        n_entries=len(entries),
        csv_stems=stems,
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
    PROPOSED/RATIFIED line; the family's terminal status (CLOSED/HALT) when it
    has one; else ``"RATIFIED"`` if a RATIFIED line names the genome, else
    ``"PROPOSED"``.
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


def assert_genome_eligible(reg: Registry, family: str, genome_id: str,
                           want: Sequence[str] = ELIGIBLE_STATUSES, command: str = "holdout") -> str:
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


def holdout_pass_lines(reg: Registry, family: str, genome_id: str) -> List[Dict[str, Any]]:
    out = []
    for ln in _transition_lines(reg, family):
        ev = ln.get("evidence") or {}
        h = ev.get("holdout") or {}
        if str(ln.get("genome_id")) == genome_id and h.get("verdict") == "PASS":
            out.append(ln)
    return out


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
# 4. the unseal log
# ---------------------------------------------------------------------------
@dataclass
class UnsealRecord:
    command: str  # "holdout" | "score"
    tag: str
    ratified_date: str
    family: str
    genome_ids: List[str]
    root: str  # repo-relative
    root_sha256: str  # SHA256SUMS digest
    sha256sums_entries: int
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


def assert_unseal_allowed(
    log_lines: Sequence[Dict[str, Any]],
    *,
    command: str,
    family: str,
    genome_ids: Sequence[str],
    root: str,
    root_sha256: str,
    now: Optional[_dt.datetime] = None,
) -> None:
    """Quota and once-per-root rules; refuses quoting the prior line."""
    now = now or _now()
    q = quarter_of(now)
    this_quarter = [ln for ln in log_lines if ln.get("quarter") == q or quarter_of(str(ln.get("ts"))) == q]
    if len(this_quarter) >= MAX_UNSEALS_PER_QUARTER:
        raise UnsealRefused(
            f"{len(this_quarter)} unseals already recorded in {q} (cap {MAX_UNSEALS_PER_QUARTER} per quarter); "
            f"prior lines: {[json.dumps(ln, sort_keys=True) for ln in this_quarter]}"
        )
    for ln in log_lines:
        same_root = ln.get("root") == root or (root_sha256 and ln.get("root_sha256") == root_sha256)
        if not same_root:
            continue
        prior = json.dumps(ln, sort_keys=True)
        if command == "holdout" and ln.get("command") == "holdout" and ln.get("family") == family:
            raise UnsealRefused(
                f"family {family!r} already spent its holdout on root {root} -- once per family per root "
                f"(FR-F4.1). Prior unseal line: {prior}"
            )
        overlap = sorted(set(map(str, ln.get("genome_ids") or [])) & set(map(str, genome_ids)))
        if overlap:
            raise UnsealRefused(
                f"genome(s) {overlap} were already scored on root {root} by '{ln.get('command')}' -- no second "
                f"scoring of the same genome on the same root may exist (FR-F4.1). Prior unseal line: {prior}"
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
    if record.line_no is None:
        raise HoldoutAbort("open_sealed_root called before the unseal line was appended; refusing")
    df = _load_ladders_unchecked(root)
    origin = df.attrs.pop("ladder_root", str(Path(root).resolve()))
    df.attrs["unsealed_root"] = origin
    df.attrs[SEALED_EVALUATION_ATTR] = {
        "command": record.command,
        "tag": record.tag,
        "line_no": record.line_no,
        "root_sha256": record.root_sha256,
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
    import pandas as pd  # noqa: F401  (lab-only)

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
    """Truth-filter drop fraction from ``manifest.json`` COUNTS only. No CSV is opened, nothing is written.

    Uses the manifest's own per-day tallies (``rows``, ``markets``,
    ``payoff_checked``, ``payoff_matched``, ``truth_checked``,
    ``truth_disagreements``) and the COUNT of ``market_detail`` entries whose
    ``result`` is a settled label. The labels themselves are never returned.
    """
    r = Path(root)
    manifest = r / MANIFEST
    if not manifest.is_file():
        raise UnsealRefused(f"{_relpath(r, repo_root)} has no {MANIFEST}; the audit reads manifest metadata only")
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    days = doc.get("days") or []
    n_days = 0
    n_markets = 0
    n_settled = 0
    n_payoff_checked = 0
    n_payoff_matched = 0
    n_truth_checked = 0
    n_truth_disagree = 0
    days_affected = 0
    days_fully_dropped = 0
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
            # a truth disagreement settles the whole ladder against one high (rule 3)
            days_fully_dropped += 1
    frac = (days_affected / n_days) if n_days else None
    return {
        "root": _relpath(r, repo_root),
        "sealed_marker_present": (r / SEALED_MARKER).is_file(),
        "sha256sums_present": (r / SHA256SUMS).is_file(),
        "sha256sums_digest": sha256_bytes_of(r / SHA256SUMS) if (r / SHA256SUMS).is_file() else None,
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
    lines = [
        f"holdout audit (manifest metadata only) -- {a['root']}",
        f"  SEALED marker: {a['sealed_marker_present']}   SHA256SUMS: {a['sha256sums_present']} "
        f"digest {str(a.get('sha256sums_digest') or '')[:16]}",
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
    ]
    return "\n".join(lines)


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


def default_frame_builder(ladders, *, spec: P.PromotedSpec, embargo_days: int, root: Path,
                          fee_regime_path: Optional[str] = None) -> Tuple[Any, Any]:
    """(frame, gefs_twin) on the unsealed tape with the search frame's hardening."""
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
                  adverse_fill=kw["adverse_fill"], availability_lag_min=kw["availability_lag_min"])
    hardening = dict(
        availability_lag_min=kw["availability_lag_min"], truth_filter=True, sigma_cap=kw["sigma_cap"],
        fold_sandbox_admissible=True, cutoff=None, fee_regime=regime,
        adverse_fill=kw["adverse_fill"], contracts=kw["contracts"],
    )
    opp = W.build_opportunities_from_ladders(ladders, kw["source"], **common)
    frame = fr.from_opportunity_frame(opp, name=f"holdout_e{kw['embargo_days']}", **hardening)
    del opp
    opp_g = W.build_opportunities_from_ladders(ladders, "gefs", **common)
    twin = fr.build_gefs_twin(frame, opp_g, **hardening)
    del opp_g
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
# 8. evaluation of one genome on a frame: kernel + section 5.8 gates + R3 map
# ---------------------------------------------------------------------------
def _gate(passed: Optional[bool], value: Any = None, threshold: Any = None, note: str = "") -> Dict[str, Any]:
    return {"pass": (None if passed is None else bool(passed)), "value": _safe(value), "threshold": threshold,
            "note": note}


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
    n_boot: int = fitness.DEFAULT_N_BOOT,
    seed: int = fitness.DEFAULT_SEED,
    rebuild_embargo2: Optional[Callable[[], Tuple[Any, Any]]] = None,
) -> Dict[str, Any]:
    """Kernel result + one-sided p + every section 5.8 gate + the REVIVAL section 5 mapping (Holm added later)."""
    res = fitness.score(F, G.to_mask(genome, F), twin=twin, genome=genome, n_boot=n_boot, seed=seed,
                        constraints=True, label=genome.name)
    p = MP.one_sided_p(res.per_date_pnl, n_boot=n_boot, seed=seed) if res.trades else math.nan
    gates: Dict[str, Dict[str, Any]] = {}
    gates["constraints"] = _gate(res.constraint_reason is None, res.constraint_reason, "section 5.6 hard constraints")
    gates["boot_lo_gt0"] = _gate(res.trades > 0 and res.boot_lo > 0, res.boot_lo, "> 0",
                                 "date-clustered 95 % bootstrap lower bound")
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
        gates["bss_trades_ge0"] = _gate(bss == bss and bss >= 0, bss, ">= 0", "R3 Brier beats market")
        gates["point_estimate_ge_4c"] = _gate(res.realized >= POINT_ESTIMATE_MIN, res.realized, f">= {POINT_ESTIMATE_MIN}",
                                              "R3 #5")
        gates["cities_ge3"] = _gate(res.cities >= fitness.MIN_CITIES, res.cities, f">= {fitness.MIN_CITIES}")
        gtr = res.gefs_twin_realized
        gates["gefs_twin_ge0"] = _gate(gtr == gtr and gtr >= 0, gtr, ">= 0",
                                       "R3 #2 ex-ante disqualifier (gefs realized on the same trade keys)")
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
        "6_cold_season_month": _gate(None, None, ">= 1 cold-season month of recorded ladders",
                                     "PENDING -- R5 accrues after Nov 7; the verdict is provisional until then"),
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
    }


# ---------------------------------------------------------------------------
# 9. Holm across finalists and all registry families
# ---------------------------------------------------------------------------
def _family_prior_p(reg: Registry, family: str, root_sha256: str) -> Tuple[float, str]:
    """(p, provenance) for a non-finalist family: this-root holdout p > pooled-validation p > unknown (1.0)."""
    lines = _transition_lines(reg, family)
    for ln in reversed(lines):
        ev = ln.get("evidence") or {}
        h = ev.get("holdout") or ev.get("r3") or {}
        if h.get("root_sha256") == root_sha256 and h.get("one_sided_p") is not None:
            return float(h["one_sided_p"]), "recorded p on this root"
    for ln in reversed(lines):
        ev = ln.get("evidence") or {}
        if ev.get("pooled_one_sided_p") is not None:
            return float(ev["pooled_one_sided_p"]), "pooled-validation one-sided p"
    return UNKNOWN_FAMILY_P, "no recorded p; counted as a test at p = 1.0"


def holm_across_registry(reg: Registry, family: str, finalist_ps: Dict[str, float], root_sha256: str,
                         alpha: float = HOLM_ALPHA) -> Dict[str, Any]:
    pvals: Dict[str, float] = {}
    prov: Dict[str, str] = {}
    for gid, p in finalist_ps.items():
        key = f"{family}:{gid}"
        pvals[key] = float(p) if p == p else 1.0
        prov[key] = "finalist one-sided p on this root" + ("" if p == p else " (no trades -> 1.0)")
    for other in reg.families():
        if other == family:
            continue
        p_o, why = _family_prior_p(reg, other, root_sha256)
        pvals[other] = p_o
        prov[other] = why
    adj = MP.holm(pvals, alpha=alpha)
    return {"alpha": alpha, "m": len(pvals), "entries": {k: dict(adj[k], provenance=prov[k]) for k in pvals},
            "rule": "Holm step-down across the finalists and every other registry family (section 6.3)"}


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


def _print_numbers(out: Callable[[str], None], evals: Dict[str, Dict[str, Any]], holm: Dict[str, Any],
                   truth: Dict[str, Any], verdicts: Dict[str, str]) -> None:
    tf = truth.get("fraction_city_days_affected")
    out(f"truth filter: dropped {truth.get('markets_dropped')} of {truth.get('markets')} markets; "
        f"touched {truth.get('city_days_affected')} of {truth.get('city_days')} city-days "
        f"({'n/a' if tf is None else f'{100 * tf:.2f} %'}) -- criterion < 10 %: "
        f"{'n/a' if truth.get('criterion_lt_10pct') is None else ('PASS' if truth['criterion_lt_10pct'] else 'FAIL')}")
    for gid, e in evals.items():
        k = e["kernel"]
        key = f"{e['family']}:{gid}"
        h = holm["entries"].get(key, {})
        out(f"{gid} {e['name']}: trades {k['trades']} dates {k['dates']} cities {k['cities']} "
            f"pooled OOS mean {_fmt(k['realized'])} boot [{_fmt(k['boot_lo'])}, {_fmt(k['boot_hi'])}] "
            f"one-sided p {_fmt(e['one_sided_p'], 4)} Holm p_adj {_fmt(h.get('p_adj'), 4)} "
            f"(m={holm['m']}) -> {verdicts[gid]}")
        for name, g in e["gates"].items():
            out(f"    gate {name:<28} {'PASS' if g['pass'] else ('n/a' if g['pass'] is None else 'FAIL'):<4} "
                f"value {_fmt(g['value'])} threshold {g['threshold']}")
        for name, g in e["r3"].items():
            st = "PENDING" if g["pass"] is None else ("PASS" if g["pass"] else "FAIL")
            out(f"    R3 {name:<30} {st:<7} {g.get('note', '')}")


def _fmt(v: Any, nd: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, (float, np.floating)):
        return "-" if v != v else f"{float(v):+.{nd}f}"
    return str(v)


def _verdict_for(e: Dict[str, Any], holm_entry: Dict[str, Any]) -> Tuple[str, List[str]]:
    failing = [k for k, g in e["gates"].items() if g["pass"] is False]
    if not holm_entry.get("reject"):
        failing.append("holm_p_adj_lt_alpha")
    return ("PASS" if not failing else "HALT"), failing


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
) -> Outcome:
    """The protocol, in the order the docstring states. Refusals raise before any write."""
    # -- refusals (nothing written) -------------------------------------------
    ratified = assert_tag_ratified(unseal_tag, paths.revival_doc)
    reg = Registry(paths.registry, repo_root=paths.repo_root)
    want = ("PROPOSED",) if command == "score" else ELIGIBLE_STATUSES
    for gid in genome_ids:
        assert_genome_eligible(reg, family, gid, want=want, command=command)
    status = reg.status(family)
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
    if as_of is not None and not any(s <= as_of for s in seal.csv_stems):
        raise UnsealRefused(f"--as-of {as_of}: no day file in {seal.relpath} is dated on or before it")
    log_lines = read_unseal_log(paths.unseal_log)
    assert_unseal_allowed(log_lines, command=command, family=family, genome_ids=genome_ids, root=seal.relpath,
                          root_sha256=seal.sha256sums_digest, now=now)
    embargo_days = int(((cfg.get("frame") or {}).get("embargo_days")) or 1)
    report_path = paths.reports_dir / (
        f"holdout_{family.replace('/', '_')}_{seal.root.name}.json" if command == "holdout"
        else f"score_{genome_ids[0]}_{seal.root.name}.json"
    )
    if report_path.exists():
        raise UnsealRefused(f"{_relpath(report_path, paths.repo_root)} already exists; a second scoring may not exist")

    # -- the unseal line, BEFORE any row is read --------------------------------
    record = UnsealRecord(
        command=command, tag=str(unseal_tag), ratified_date=ratified, family=family, genome_ids=list(genome_ids),
        root=seal.relpath, root_sha256=seal.sha256sums_digest, sha256sums_entries=seal.n_entries, as_of=as_of,
    )
    append_unseal_line(paths.unseal_log, record, repo_root=paths.repo_root)
    out(f"unseal: line {record.line_no} appended to {_relpath(paths.unseal_log, paths.repo_root)} "
        f"({command}, {family}, root {seal.relpath} sha {seal.sha256sums_digest[:12]}, tag {unseal_tag})")

    # -- everything below has spent the look ------------------------------------
    sums = verify_sha256sums(seal.root)
    if sums["failed"] or sums["missing"]:
        raise HoldoutAbort(f"SHA256SUMS check failed for {seal.relpath}: failed {sums['failed']} missing {sums['missing']}")
    ladders = open_sealed_root(seal.root, record)
    if as_of is not None:
        keep = ladders["target_date"].astype(str).str.slice(0, 10) <= as_of
        ladders = ladders.loc[keep.to_numpy()].reset_index(drop=True)
    if ladders.empty:
        raise HoldoutAbort(f"{seal.relpath} yielded no rows" + (f" on or before {as_of}" if as_of else ""))
    truth = truth_filter_stats(ladders)
    ladders, payoff_dropped = drop_payoff_mismatch_markets(ladders)
    builder = frame_builder or default_frame_builder
    cache: Dict[int, Tuple[Any, Any]] = {}

    def _frames(emb: int, spec: P.PromotedSpec) -> Tuple[Any, Any]:
        if emb not in cache:
            cache[emb] = builder(ladders, spec=spec, embargo_days=emb, root=seal.root)
        return cache[emb]

    evals: Dict[str, Dict[str, Any]] = {}
    for gid in genome_ids:
        spec = specs[gid]
        F, twin = _frames(embargo_days, spec)
        e = evaluate_genome(F, twin, spec.genome(), n_boot=n_boot, seed=seed,
                            rebuild_embargo2=lambda s=spec: _frames(SENSITIVITY_EMBARGO_DAYS, s))
        e["family"] = family
        e["frame"] = {"name": getattr(F, "name", None), "sha256": (F.provenance or {}).get("frame_sha256"),
                      "n_rows": int(F.n_rows), "n_dates": int(F.n_dates), "n_markets": int(F.n_markets),
                      "dates": [str(F.dates[0]), str(F.dates[-1])] if F.n_dates else [],
                      "hardening": {k: v for k, v in (F.provenance or {}).items()
                                    if k in ("availability_lag_min", "truth_filter", "sigma_cap",
                                             "fold_sandbox_admissible", "cutoff", "adverse_fill", "contracts",
                                             "dropped_result_unsettled", "dropped_payoff_mismatch",
                                             "dropped_truth_disagree", "dropped_sigma_markets", "rows_kept",
                                             "executable_rows")}}
        evals[gid] = e
    holm = holm_across_registry(reg, family, {gid: e["one_sided_p"] for gid, e in evals.items()},
                                seal.sha256sums_digest)
    verdicts: Dict[str, str] = {}
    failing: Dict[str, List[str]] = {}
    for gid, e in evals.items():
        v, f = _verdict_for(e, holm["entries"][f"{family}:{gid}"])
        e["holm"] = holm["entries"][f"{family}:{gid}"]
        verdicts[gid], failing[gid] = v, f
        e["verdict"], e["failing"] = v, f

    result = {
        "command": command, "family": family, "root": seal.relpath, "root_sha256": seal.sha256sums_digest,
        "sha256sums": sums, "as_of": as_of, "embargo_days": embargo_days, "n_boot": int(n_boot), "seed": int(seed),
        "truth_filter": truth, "payoff_mismatch_markets_dropped": payoff_dropped,
        "genomes": evals, "holm": holm, "verdicts": verdicts,
    }
    sha = result_sha256(result)
    out(f"result_sha256 {sha}")
    _print_numbers(out, evals, holm, truth, verdicts)
    doc = {
        "result": result, "result_sha256": sha,
        "meta": {"ts": record.ts, "git_rev": record.git_rev, "unseal": record.line(), "unseal_line_no": record.line_no,
                 "caveat": LABEL_LEAK_CAVEAT, "ratified_date": ratified},
    }
    from src.factory.report import write_json

    write_json(report_path, doc)
    out(f"report: {_relpath(report_path, paths.repo_root)}")

    # -- registry --------------------------------------------------------------
    _write_transitions(reg, command, family, status or "PROPOSED", evals, verdicts, failing, seal, sha, out)
    any_pass = any(v == "PASS" for v in verdicts.values())
    return Outcome(exit_code=EXIT_PASS if any_pass else EXIT_HALT, verdicts=verdicts, result_sha256=sha,
                   report_path=report_path, record=record, doc=doc)


def _evidence(command: str, e: Dict[str, Any], seal: RootSeal, sha: str, verdicts: Dict[str, str],
              failing: Dict[str, List[str]]) -> Dict[str, Any]:
    k = e["kernel"]
    block = {
        "root": seal.relpath, "root_sha256": seal.sha256sums_digest, "verdict": e["verdict"], "failing": e["failing"],
        "one_sided_p": e["one_sided_p"], "holm_p_adj": e["holm"].get("p_adj"), "holm_m": None,
        "pooled_mean": k["realized"], "boot_lo": k["boot_lo"], "boot_hi": k["boot_hi"], "n_dates": k["dates"],
        "trades": k["trades"], "cities": k["cities"], "bss_trades": k["bss_trades"],
        "gefs_twin_realized": k["gefs_twin_realized"], "gates": {n: g["pass"] for n, g in e["gates"].items()},
        "finalists_failed": {g: failing[g] for g, v in verdicts.items() if v != "PASS" and g != e["genome_id"]},
    }
    ev = {"result_sha256": sha}
    if command == "holdout":
        ev["holdout"] = block
    else:
        ev["r3"] = dict(block, checks={n: g["pass"] for n, g in e["r3"].items()})
    return _safe(ev)


def _write_transitions(reg: Registry, command: str, family: str, status: str, evals, verdicts, failing, seal, sha,
                       out: Callable[[str], None]) -> None:
    passing = [g for g, v in verdicts.items() if v == "PASS"]
    try:
        if not passing:
            worst = max(evals, key=lambda g: (evals[g]["kernel"]["boot_lo"] if evals[g]["kernel"]["boot_lo"] == evals[g]["kernel"]["boot_lo"] else -1e9))
            ev = _evidence(command, evals[worst], seal, sha, verdicts, failing)
            ev["all_finalists"] = {g: verdicts[g] for g in evals}
            reg.transition(family, "HALT", genome_id=worst, evidence=ev)
            out(f"registry: {family} -> HALT ({'no finalist passed' if command == 'holdout' else 'R3 #3 failed'}: "
                f"{ {g: failing[g] for g in evals} })")
            return
        for gid in passing:
            new_status = status if command == "holdout" else "RATIFIED"
            reg.transition(family, new_status, genome_id=gid, evidence=_evidence(command, evals[gid], seal, sha, verdicts, failing))
            out(f"registry: {family} {gid} -> {new_status} (evidence.{'holdout' if command == 'holdout' else 'r3'} attached)")
    except RegistryError as exc:
        raise HoldoutAbort(f"registry transition refused: {exc}") from exc


# ---------------------------------------------------------------------------
# 11. entry points the CLI calls
# ---------------------------------------------------------------------------
def run_holdout(*, finalists_path: Union[str, Path], unseal_tag: Optional[str], root: Union[str, Path] = DEFAULT_HOLDOUT_ROOT,
                paths: HoldoutPaths = HoldoutPaths(), n_boot: int = fitness.DEFAULT_N_BOOT, seed: int = fitness.DEFAULT_SEED,
                frame_builder: Optional[FrameBuilder] = None, out: Callable[[str], None] = print,
                now: Optional[_dt.datetime] = None) -> Outcome:
    family, ids = load_finalists(finalists_path)
    return evaluate_on_root(command="holdout", root=root, unseal_tag=unseal_tag, family=family, genome_ids=ids,
                            paths=paths, n_boot=n_boot, seed=seed, frame_builder=frame_builder, out=out, now=now)


def run_score(*, genome_id: str, unseal_tag: Optional[str], root: Union[str, Path] = DEFAULT_R3_ROOT,
              as_of: Optional[str] = None, paths: HoldoutPaths = HoldoutPaths(), n_boot: int = fitness.DEFAULT_N_BOOT,
              seed: int = fitness.DEFAULT_SEED, frame_builder: Optional[FrameBuilder] = None,
              out: Callable[[str], None] = print, now: Optional[_dt.datetime] = None) -> Outcome:
    family = _family_of_genome(genome_id, paths)
    return evaluate_on_root(command="score", root=root, unseal_tag=unseal_tag, family=family, genome_ids=[genome_id],
                            paths=paths, as_of=as_of, n_boot=n_boot, seed=seed, frame_builder=frame_builder, out=out,
                            now=now)


def _family_of_genome(genome_id: str, paths: HoldoutPaths) -> str:
    """The family from the promoted spec (the registry is checked afterwards, against it)."""
    try:
        spec = P.load_promoted(genome_id, directory=str(paths.promoted_dir))
    except P.PromotedSpecError as exc:
        raise UnsealRefused(f"genome {genome_id}: {exc}") from exc
    return str(spec.family)


__all__ = [
    "EXIT_HALT", "EXIT_PASS", "EXIT_REFUSED", "HoldoutAbort", "HoldoutPaths", "LABEL_LEAK_CAVEAT", "MAX_FINALISTS",
    "MAX_UNSEALS_PER_QUARTER", "Outcome", "UnsealRecord", "UnsealRefused", "append_unseal_line", "assert_genome_eligible",
    "assert_tag_ratified", "assert_unseal_allowed", "audit_root", "canonical_json", "evaluate_genome", "evaluate_on_root",
    "genome_status", "holm_across_registry", "inspect_root_seal", "load_finalists", "open_sealed_root", "parse_unseal_tag", "quarter_of",
    "ratified_dates", "read_unseal_log", "render_audit", "result_sha256", "run_holdout", "run_score",
    "truth_filter_stats", "verify_sha256sums",
]
