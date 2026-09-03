"""Append-only family registry (``reports/factory/registry.jsonl``).

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 1.1 / 6.3 and
PRD_STRATEGY_FACTORY FR-F1.4: a family line (lane, source, mode, gene-spec
version, config hash, budget, picker, thresholds) is written BEFORE any result
exists, so the thresholds a run is judged against are pre-committed. Status
transitions (PROPOSED / RATIFIED / CLOSED / HALT) are further appended lines;
nothing is ever rewritten.

Lines carry a wall-clock ``ts`` (this file is NOT byte-hash monitored; the
timestamp-free artifacts are ``status.json`` / ``board.md``) and the ``git_rev``.

Line shapes::

    {"event": "family", "family": "weather/gfs_mex/taker/v1", "status": "OPEN",
     "lane": ..., "source": ..., "mode": ..., "gene_spec_version": 1,
     "config_sha256": ..., "budget": {...}, "picker": ..., "thresholds": {...},
     "cutoff": "2026-07-25", "grouping_unit": "target_date", "family_cap": 6,
     "notes": "", "ts": iso, "git_rev": sha}
    {"event": "transition", "family": ..., "status": "PROPOSED"|"RATIFIED"|"CLOSED"|"HALT",
     "genome_id": ..., "evidence": {...}, "ts": iso, "git_rev": sha}
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

FAMILY_F1 = "weather/gfs_mex/taker/v1"
DEFAULT_REGISTRY = Path("reports") / "factory" / "registry.jsonl"
TRANSITIONS = ("PROPOSED", "RATIFIED", "CLOSED", "HALT")
TERMINAL = ("CLOSED", "HALT")


class RegistryError(RuntimeError):
    """Registry invariant violated (duplicate open family, missing line, cap)."""


def git_rev(repo_root: Optional[Union[str, Path]] = None) -> str:
    """The checkout's HEAD sha (``MP_GIT_REV`` env wins); "" when unavailable.

    Callers that need a NON-EMPTY rev (run.json, registry lines) must abort on
    "" -- PRD_STRATEGY_FACTORY section 6 "Failure is loud".
    """
    env = os.getenv("MP_GIT_REV", "").strip()
    if env:
        return env
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    rev = out.stdout.strip() if out.returncode == 0 else ""
    return rev + git_dirty_suffix(root) if rev else ""


def git_dirty_suffix(repo_root: Union[str, Path]) -> str:
    """``"+dirty"`` when tracked files differ from HEAD, else ``""``.

    A rev alone does not identify the code that produced an artifact when the
    tree was dirty (red-team finding 2026-09-02: the first gen-0 report named
    a commit that did not yet contain ``gen0.py``). Untracked files are
    ignored on purpose (ignored data caches, logs). ``""`` when git is
    unavailable -- the rev is then unverifiable and says so by omission only,
    which is why the container image ships git.
    """
    # The `factory` service masks the sealed roots with empty tmpfs (compose
    # section 7.1), so inside the container git sees their tracked CSVs as
    # deleted. That is the sandbox, not drift: exclude them by pathspec.
    pathspec = ["--", ".", ":!data/ladders_holdout", ":!data/ladders_2026-09"]
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no", *pathspec],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return "+dirty" if out.stdout.strip() else ""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class Registry:
    def __init__(self, path: Union[str, Path] = DEFAULT_REGISTRY, repo_root: Optional[Path] = None):
        self.path = Path(path)
        self.repo_root = repo_root

    # -- reading -----------------------------------------------------------
    def lines(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if raw:
                    out.append(json.loads(raw))
        return out

    def family_line(self, family: str) -> Optional[Dict[str, Any]]:
        """The (first) family line for ``family`` or None."""
        for ln in self.lines():
            if ln.get("event") == "family" and ln.get("family") == family:
                return ln
        return None

    def status(self, family: str) -> Optional[str]:
        """Current status: last transition, else OPEN, else None (unregistered)."""
        st: Optional[str] = None
        for ln in self.lines():
            if ln.get("family") != family:
                continue
            if ln.get("event") == "family" and st is None:
                st = "OPEN"
            elif ln.get("event") == "transition":
                st = ln.get("status")
        return st

    def families(self) -> List[str]:
        seen: List[str] = []
        for ln in self.lines():
            if ln.get("event") == "family" and ln["family"] not in seen:
                seen.append(ln["family"])
        return seen

    def assert_registered(self, family: str) -> Dict[str, Any]:
        ln = self.family_line(family)
        if ln is None:
            raise RegistryError(
                f"family {family!r} has no registry line in {self.path}; "
                "write_family_line() must run BEFORE any result exists"
            )
        return ln

    # -- writing -----------------------------------------------------------
    def _append(self, line: Dict[str, Any]) -> Dict[str, Any]:
        rev = git_rev(self.repo_root)
        if not rev:
            raise RegistryError("git rev is empty; refusing to write a registry line")
        line = dict(line, ts=_now_iso(), git_rev=rev)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        return line

    def write_family_line(
        self,
        family: str,
        *,
        lane: str,
        source: str,
        mode: str,
        gene_spec_version: int,
        config_sha256: str,
        budget: Dict[str, Any],
        picker: str,
        thresholds: Dict[str, Any],
        cutoff: str,
        grouping_unit: str = "target_date",
        family_cap: int = 6,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Append the pre-run family line; refuses a second OPEN line per family."""
        current = self.status(family)
        if current is not None and current not in TERMINAL:
            raise RegistryError(
                f"family {family!r} already has an open registry line (status {current}); "
                "a rerun is a NEW family name, never a second line"
            )
        others = [f for f in self.families() if f != family]
        if len(others) + 1 > int(family_cap):
            raise RegistryError(
                f"family cap {family_cap} reached ({len(others)} registered); "
                "section 6.3 caps registered families before 2027"
            )
        line = {
            "event": "family",
            "family": family,
            "status": "OPEN",
            "lane": lane,
            "source": source,
            "mode": mode,
            "gene_spec_version": int(gene_spec_version),
            "config_sha256": config_sha256,
            "budget": dict(budget),
            "picker": picker,
            "thresholds": dict(thresholds),
            "cutoff": cutoff,
            "grouping_unit": grouping_unit,
            "family_cap": int(family_cap),
            "notes": notes,
        }
        return self._append(line)

    def transition(
        self,
        family: str,
        status: str,
        genome_id: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if status not in TRANSITIONS:
            raise RegistryError(f"unknown transition {status!r}; want one of {TRANSITIONS}")
        self.assert_registered(family)
        current = self.status(family)
        if current in TERMINAL:
            raise RegistryError(f"family {family!r} is {current}; no further transitions")
        return self._append(
            {
                "event": "transition",
                "family": family,
                "status": status,
                "genome_id": genome_id,
                "evidence": dict(evidence or {}),
            }
        )
