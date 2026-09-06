"""FR-5.2 gate registration (``configs/factory/gate_registration.json``) -- build, check, stamp.

PRD_STRATEGY_FACTORY FR-F4.2: the promotion commit carries the promoted spec,
``GENOME_STRATEGY_ID`` and ``gate_registration.json`` **committed before the
first paper trade**. ``scripts/gate.py`` scores only the pre-registered window
(``registered_before_first_trade``), so a paper spec that exists without a
committed, time-stamped registration has no gate to be scored by. This module
is what ``scripts/factory.py register-gate`` and ``promote --mode paper`` call:

* :func:`build_registration` -- the template (``gate_registration.template.json``)
  filled from a promoted spec. The ``spec_hash`` it records is the hash the
  **paper** spec will carry (``mode="paper"``, ``registry_status`` = the family's
  current status), derived from the shadow spec on disk, so the registration can
  be committed *before* the paper promotion is written and ``promote --mode
  paper`` can refuse when the spec it is about to write does not match.
* :func:`check_registration` -- every reason a registration does not license a
  paper promotion, as a list (empty = OK). Pure.
* :func:`fill_commit_time` -- stamps ``registration_commit_utc`` from
  ``git log --diff-filter=A --format=%cI`` and REFUSES while the file is
  untracked or carries uncommitted edits: the stamp must be the commit that
  added the file, not a typed date.

stdlib-only (json, subprocess); imports ``src.factory.promoted`` lazily for the
hash. Lab-side: nothing in the sandbox image imports this.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

_THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _THIS_DIR.parents[1]
CONFIG_DIR = REPO_ROOT / "configs" / "factory"
TEMPLATE_PATH = CONFIG_DIR / "gate_registration.template.json"
#: Registrations are PER GENOME: ``configs/factory/gate_registration_<genome_id>.json``.
#: One file per genome means each is ADDED to git exactly once, so ``git log
#: --diff-filter=A`` names one instant; a shared ``gate_registration.json`` re-issued
#: for a second genome would be a *modification* in git's eyes and inherit the first
#: genome's add-date (red team 2026-09-06, BROKEN-B).
REGISTRATION_FMT = "gate_registration_{genome_id}.json"
#: The pre-F4 shared path, kept only so callers can name it in messages.
LEGACY_REGISTRATION_PATH = CONFIG_DIR / "gate_registration.json"
SCHEMA_VERSION = 1
#: ``Strategy.name`` the sandbox writes into the journal for a promoted genome
#: (``GenomeStrategy.name`` = ``f"Genome {genome_id[:8]}"``; maia shows "Genome 0c4b2050").
STRATEGY_NAME_FMT = "Genome {id8}"
GIT_ADDED_COMMAND = "git log --diff-filter=A --format=%cI -- {path}"


class RegistrationError(RuntimeError):
    """The registration is missing, malformed, stale, or not yet committed."""


def strategy_name_for(genome_id: str) -> str:
    return STRATEGY_NAME_FMT.format(id8=str(genome_id)[:8])


def registration_path(genome_id: str, directory: Union[str, os.PathLike] = CONFIG_DIR) -> Path:
    """``configs/factory/gate_registration_<genome_id>.json``."""
    return Path(directory) / REGISTRATION_FMT.format(genome_id=str(genome_id))


def registration_relpath(genome_id: str) -> str:
    return f"configs/factory/{REGISTRATION_FMT.format(genome_id=str(genome_id))}"


def _relpath_str(path: Union[str, os.PathLike]) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _read_json(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise RegistrationError(f"{p}: cannot read ({exc})") from exc
    except ValueError as exc:
        raise RegistrationError(f"{p}: not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise RegistrationError(f"{p}: not a JSON object")
    return doc


def write_registration(doc: Mapping[str, Any], path: Union[str, os.PathLike]) -> Path:
    """indent=2, key order preserved (``_doc`` stays first), LF, trailing newline."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(doc), indent=2, ensure_ascii=False) + "\n"
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


def load_registration(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise RegistrationError(
            f"{p} does not exist -- run `python scripts/factory.py register-gate <genome_id>` "
            "and commit it before any paper promotion"
        )
    return _read_json(p)


# ---------------------------------------------------------------------------
# the paper spec hash a registration must name
# ---------------------------------------------------------------------------
def paper_spec_hash(spec_doc: Mapping[str, Any], *, registry_status: str) -> str:
    """``spec_hash`` of ``spec_doc`` re-stamped as ``mode="paper"`` under ``registry_status``.

    ``spec_hash`` covers ``mode`` and ``registry_status`` (``promoted.spec_hash_of``),
    so the shadow spec on disk and the paper spec ``promote --mode paper`` will
    write differ in hash by exactly those two fields. Every other field is a
    deterministic function of the same inputs (parity numbers, calibration,
    fee regime), which is what lets the registration be issued first.
    """
    from src.factory.promoted import spec_hash_of

    doc = {k: v for k, v in spec_doc.items() if k != "spec_hash"}
    doc["mode"] = "paper"
    doc["registry_status"] = str(registry_status)
    return spec_hash_of(doc)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build_registration(
    spec_doc: Mapping[str, Any],
    *,
    registry_status: str,
    template: Optional[Mapping[str, Any]] = None,
    template_path: Union[str, os.PathLike] = TEMPLATE_PATH,
    promoted_spec_path: Optional[str] = None,
) -> Dict[str, Any]:
    """The template with every REPLACE_ME filled from ``spec_doc``; ``registration_commit_utc`` stays null."""
    tpl = dict(template) if template is not None else _read_json(template_path)
    tpl_version = tpl.get("schema_version")
    if tpl_version != SCHEMA_VERSION:
        # A template whose meaning moved must not be silently rewritten to the version
        # this code knows: its fields may not mean what gate.py reads.
        raise RegistrationError(
            f"template schema_version {tpl_version!r} != {SCHEMA_VERSION}; refusing to build a "
            "registration from a template this code does not understand"
        )
    gid = str(spec_doc["genome_id"])
    fee = spec_doc.get("fee") or {}
    # ``_doc`` is the template's field documentation, and its ``_about`` sentence says
    # "fill every REPLACE_ME" -- verbatim, that token tripped gate.py's placeholder
    # check on every generated file (red team 2026-09-06, BROKEN-A). The generated
    # registration carries no ``_doc`` block at all, only a pointer to the template's.
    out: Dict[str, Any] = {"_doc_ref": f"field documentation: {_relpath_str(template_path)} (_doc block)"}
    out.update({k: v for k, v in tpl.items() if not str(k).startswith("_")})
    out["schema_version"] = SCHEMA_VERSION
    out["genome_id"] = gid
    out["strategy_name"] = strategy_name_for(gid)
    out["promoted_spec_path"] = promoted_spec_path or f"configs/factory/promoted/{gid}.json"
    out["spec_hash"] = paper_spec_hash(spec_doc, registry_status=registry_status)
    out["adverse_fill"] = float(spec_doc["adverse_fill"])
    out["contracts_frame"] = int(spec_doc["contracts_frame"])
    out["fee_type"] = str(fee.get("type") or out.get("fee_type") or "taker")
    out["registration_commit_utc"] = None
    out["registered_before_first_trade"] = (
        f"This file must be committed before the first paper trade of {out['strategy_name']!r}; verify "
        f"with `git log --diff-filter=A -- {registration_relpath(gid)}` against the first journal row."
    )
    leftovers = [k for k, v in out.items() if isinstance(v, str) and "REPLACE_ME" in v]
    if leftovers:
        raise RegistrationError(f"template fields still unfilled: {leftovers}")
    return out


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def check_registration(
    reg: Mapping[str, Any],
    spec_doc: Mapping[str, Any],
    *,
    registry_status: str,
    require_commit_time: bool = True,
    expected_spec_hash: Optional[str] = None,
) -> List[str]:
    """Every reason ``reg`` does not license a paper promotion of ``spec_doc``; ``[]`` = OK.

    ``expected_spec_hash`` overrides the derived paper hash when the caller
    already holds the spec it is about to write (``promote --mode paper``).
    """
    problems: List[str] = []
    gid = str(spec_doc.get("genome_id") or "")
    if int(reg.get("schema_version", -1)) != SCHEMA_VERSION:
        problems.append(f"schema_version {reg.get('schema_version')!r} != {SCHEMA_VERSION}")
    if str(reg.get("genome_id") or "") != gid:
        problems.append(f"genome_id {reg.get('genome_id')!r} != spec {gid!r}")
    want_name = strategy_name_for(gid)
    if str(reg.get("strategy_name") or "") != want_name:
        problems.append(f"strategy_name {reg.get('strategy_name')!r} != {want_name!r}")
    try:
        if abs(float(reg.get("adverse_fill")) - float(spec_doc.get("adverse_fill"))) > 1e-12:
            problems.append(f"adverse_fill {reg.get('adverse_fill')!r} != spec {spec_doc.get('adverse_fill')!r}")
    except (TypeError, ValueError):
        problems.append(f"adverse_fill {reg.get('adverse_fill')!r} is not a number")
    want_hash = expected_spec_hash or paper_spec_hash(spec_doc, registry_status=registry_status)
    if str(reg.get("spec_hash") or "") != want_hash:
        problems.append(
            f"spec_hash {str(reg.get('spec_hash'))[:12]} != the paper spec's {want_hash[:12]} "
            "(registration is stale: re-run register-gate, commit, and fill the commit time again)"
        )
    want_path = f"configs/factory/promoted/{gid}.json"
    if str(reg.get("promoted_spec_path") or "").replace("\\", "/") != want_path:
        problems.append(f"promoted_spec_path {reg.get('promoted_spec_path')!r} != {want_path!r}")
    fee_type = str((spec_doc.get("fee") or {}).get("type") or "")
    if fee_type and str(reg.get("fee_type") or "") != fee_type:
        problems.append(f"fee_type {reg.get('fee_type')!r} != spec fee.type {fee_type!r}")
    if require_commit_time and not reg.get("registration_commit_utc"):
        problems.append(
            f"registration_commit_utc is null: commit {registration_relpath(gid)}, then run "
            f"`python scripts/factory.py register-gate --fill-commit-time {gid}` and commit again"
        )
    return problems


def reconcile_commit_time(reg: Mapping[str, Any], path: Union[str, os.PathLike]) -> List[str]:
    """Problems with ``registration_commit_utc`` against git, the way ``gate.py`` reconciles it.

    A typed value cannot establish that the registration preceded the run it
    binds (gate.py ``_first_trade_after``): the value must EQUAL the committer
    date of the commit that added the file. ``[]`` = verified. Git being unable
    to answer (untracked, no repo) is a problem here -- ``promote --mode paper``
    is not a dry run.
    """
    declared = reg.get("registration_commit_utc")
    if not declared:
        return ["registration_commit_utc is null"]
    stamp, note = git_added_commit_utc(path)
    if stamp is None:
        return [f"registration_commit_utc {declared!r} cannot be verified against git: {note}"]
    if str(declared) != stamp:
        return [
            f"registration_commit_utc {declared!r} does not reconcile with git, which says the file was "
            f"added at {stamp!r} ({note})"
        ]
    if git_file_state(path) == "modified":
        return ["the registration carries uncommitted edits; commit them (the stamp describes the file as committed)"]
    return []


# ---------------------------------------------------------------------------
# git stamp
# ---------------------------------------------------------------------------
def _git(args: List[str], cwd: Union[str, os.PathLike]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


def git_added_commit_utc(path: Union[str, os.PathLike]) -> Tuple[Optional[str], str]:
    """``(ISO committer date of the newest commit that ADDED path, note)``; ``(None, why)`` when git cannot say."""
    p = Path(path).resolve()
    if shutil.which("git") is None:
        return None, "git binary not found"
    if not p.parent.is_dir():
        return None, f"{p.parent} is not a directory"
    proc = _git(["log", "--diff-filter=A", "--format=%cI", "--", p.name], cwd=p.parent)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, "git log failed (" + (detail[0] if detail else f"exit {proc.returncode}") + ")"
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None, "git records no commit adding this file (untracked, or never committed)"
    return lines[0], "git log --diff-filter=A --format=%cI (newest add)"


def git_file_state(path: Union[str, os.PathLike]) -> str:
    """``"committed"`` | ``"modified"`` | ``"untracked"`` | ``"not-a-repo"``."""
    p = Path(path).resolve()
    if shutil.which("git") is None:
        return "not-a-repo"
    inside = _git(["rev-parse", "--is-inside-work-tree"], cwd=p.parent)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "not-a-repo"
    tracked = _git(["ls-files", "--error-unmatch", "--", p.name], cwd=p.parent)
    if tracked.returncode != 0:
        return "untracked"
    status = _git(["status", "--porcelain", "--", p.name], cwd=p.parent)
    return "modified" if status.stdout.strip() else "committed"


def fill_commit_time(path: Union[str, os.PathLike]) -> str:
    """Stamp ``registration_commit_utc`` from git and write the file back; refuse if not committed as-is.

    Refuses (``RegistrationError``) when the file is untracked, carries edits
    not yet committed, or git records no add for it. Returns the stamp.
    """
    p = Path(path)
    reg = load_registration(p)
    state = git_file_state(p)
    if state == "not-a-repo":
        raise RegistrationError(f"{p}: not inside a git checkout; the stamp must come from the commit that added it")
    if state == "untracked":
        raise RegistrationError(
            f"{p} is not committed yet -- `git add {p.as_posix()}` and commit it first; the "
            "registration time IS that commit"
        )
    if state == "modified":
        raise RegistrationError(
            f"{p} has uncommitted edits -- commit them first; the stamp must describe the file as committed"
        )
    stamp, note = git_added_commit_utc(p)
    if not stamp:
        raise RegistrationError(f"{p}: {note}")
    current = reg.get("registration_commit_utc")
    if current and str(current) != stamp:
        raise RegistrationError(
            f"{p}: registration_commit_utc already reads {current!r} but git says the file was added at "
            f"{stamp!r}; a typed value is not accepted -- reset it to null and re-run"
        )
    reg["registration_commit_utc"] = stamp
    write_registration(reg, p)
    return stamp


def reissue_problems(path: Union[str, os.PathLike]) -> List[str]:
    """Why a registration may NOT be written at ``path`` right now; ``[]`` = free to write.

    A registration is added to git exactly once. If the file already exists, or
    is still tracked in HEAD, writing it again is a *modification* and ``git log
    --diff-filter=A`` keeps the OLD add-date -- the gate would then vouch for the
    wrong instant. The only re-issue path is: ``git rm`` the old file, commit that
    removal, then register again, which is a fresh add commit.
    """
    p = Path(path)
    rel = _relpath_str(p)
    if p.exists():
        return [
            f"{rel} exists; a registration is re-issued, never edited in place. To re-issue: "
            f"`git rm {rel} && git commit -m 'gate: withdraw registration'`, then run register-gate again"
        ]
    if p.parent.is_dir() and git_file_state(p) in ("committed", "modified"):
        return [
            f"{rel} is deleted in the working tree but still tracked in HEAD; commit the removal first "
            f"(`git rm {rel} && git commit -m 'gate: withdraw registration'`) so the next write is a NEW add"
        ]
    return []


__all__ = [
    "CONFIG_DIR",
    "GIT_ADDED_COMMAND",
    "LEGACY_REGISTRATION_PATH",
    "REGISTRATION_FMT",
    "SCHEMA_VERSION",
    "TEMPLATE_PATH",
    "RegistrationError",
    "build_registration",
    "check_registration",
    "fill_commit_time",
    "git_added_commit_utc",
    "git_file_state",
    "load_registration",
    "paper_spec_hash",
    "reconcile_commit_time",
    "registration_path",
    "registration_relpath",
    "reissue_problems",
    "strategy_name_for",
    "write_registration",
]
