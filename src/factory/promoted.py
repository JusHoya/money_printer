"""Promoted-genome spec: the tracked JSON that turns a factory genome into a sandbox strategy.

``configs/factory/promoted/<genome_id>.json`` is the ONLY artefact the maia
sandbox reads to instantiate ``GenomeStrategy`` (F3 contract, "Promoted spec";
FACTORY_ARCHITECTURE section 9 step 4). It is timestamp-free so a re-promotion
of the same genome on the same inputs is byte-identical, and it carries a
``spec_hash`` over every other field so the FR-5.2 gate can prove the spec
did not move between the first paper trade and the verdict.

Schema (all keys required; ``spec_hash`` covers everything else)::

    genome_id             sha256(genome_json)[:16]           (== src.factory.ledger.genome_id)
    genome_json           genome.Genome.to_json(), canonical (sort_keys, compact separators)
    family                family name from the registry line
    config_sha256         sha256 of the family YAML (registry line)
    frame_search_sha256   sha256 of the search frame the genome was scored on
    calibration           {"dir": repo-relative dir, "sha256": sha of the calibration payload
                           files (frame provenance ``calibration_dir.files`` mapping)}
    fee                   {"type": "taker"|"maker", "fee_type": API series fee_type at the
                           frame's ts range ("quadratic"), "regime_sha256": fee_regime.csv sha}
    forecast_source       genome.source ("gfs_mex" | "gefs")
    adverse_fill          0.01
    contracts_frame       20
    availability_lag_min  240
    sigma_cap             4.0 (the search frame's pre-selection cap, R3 #1)
    mode                  "shadow" | "paper"
    registry_status       "CLOSED" | "PROPOSED" | "RATIFIED" | "OPEN" | "HALT"
    source                "seed" | "pick:<campaign>:<run_id>"
    parity                {"frame_sha12", "n_markets", "n_offline", "n_live", "n_discrepancies"}
                          (the replay-parity result the promotion was gated on)
    spec_hash             sha256 of the canonical JSON of every other field

numpy-free and pandas-free: the sandbox image imports this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple, Union

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
PROMOTED_DIR = os.path.join(REPO_ROOT, "configs", "factory", "promoted")

MODES: Tuple[str, ...] = ("shadow", "paper")
#: Registry statuses under which a ``paper`` spec may exist (F3 contract).
PAPER_ALLOWED_STATUSES: Tuple[str, ...] = ("PROPOSED", "RATIFIED")

REQUIRED_KEYS: Tuple[str, ...] = (
    "genome_id",
    "genome_json",
    "family",
    "config_sha256",
    "frame_search_sha256",
    "calibration",
    "fee",
    "forecast_source",
    "adverse_fill",
    "contracts_frame",
    "availability_lag_min",
    "sigma_cap",
    "mode",
    "registry_status",
    "source",
    "parity",
    "spec_hash",
)


class PromotedSpecError(ValueError):
    """The spec is malformed, its hash does not verify, or a policy rule is violated."""


# ---------------------------------------------------------------------------
# canonical hashing
# ---------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """The one serialisation used for ``genome_json`` and ``spec_hash``."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def genome_id_for(genome_json: str) -> str:
    """``sha256(genome_json)[:16]`` -- identical to ``src.factory.ledger.genome_id``."""
    return hashlib.sha256(genome_json.encode("utf-8")).hexdigest()[:16]


def genome_json_for(genome: Any) -> str:
    """Canonical ``genome_json`` string for a ``Genome`` (or a dict / str already in that form)."""
    if isinstance(genome, str):
        return genome
    obj = genome.to_json() if hasattr(genome, "to_json") else genome
    return canonical_json(obj)


def spec_hash_of(doc: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of every field except ``spec_hash``."""
    stripped = {k: v for k, v in doc.items() if k != "spec_hash"}
    return hashlib.sha256(canonical_json(stripped).encode("utf-8")).hexdigest()


def sha256_of_mapping(m: Mapping[str, str]) -> str:
    """``frame._sha256_of_mapping``: sorted ``key\\0sha\\n`` lines -- the calibration-dir identity."""
    h = hashlib.sha256()
    for k in sorted(m):
        h.update(str(k).encode("utf-8"))
        h.update(b"\0")
        h.update(str(m[k]).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _relpath(path: str) -> str:
    p = os.path.abspath(str(path))
    try:
        rel = os.path.relpath(p, REPO_ROOT)
        if not rel.startswith(".."):
            p = rel
    except ValueError:
        pass
    return p.replace(os.sep, "/").replace("\\", "/")


def hash_calibration_dir(path: str, suffixes: Tuple[str, ...] = (".json",)) -> Dict[str, str]:
    """``{repo-relative path: sha256}`` of every ``*.json`` in ``path`` -- ``frame._hash_dir``.

    Uses the CRLF-normalised ``fees.sha256_file`` so the hash is a property of
    the content on both Windows and the containers.
    """
    from src.factory.fees import sha256_file  # numpy-only module

    out: Dict[str, str] = {}
    if not os.path.isdir(path):
        return out
    for fn in sorted(os.listdir(path)):
        if fn.endswith(suffixes):
            p = os.path.join(path, fn)
            if os.path.isfile(p):
                out[_relpath(p)] = sha256_file(p)
    return out


def calibration_dir_sha256(path: str) -> str:
    """The calibration identity ``GenomeStrategy`` checks against ``spec.calibration.sha256``."""
    return sha256_of_mapping(hash_calibration_dir(path))


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibrationRef:
    dir: str
    sha256: str


@dataclass(frozen=True)
class FeeRef:
    type: str  # "taker" | "maker" (the genome's mode)
    fee_type: str  # API series fee_type at the frame's ts range ("quadratic")
    regime_sha256: str


@dataclass(frozen=True)
class PromotedSpec:
    genome_id: str
    genome_json: str
    family: str
    config_sha256: str
    frame_search_sha256: str
    calibration: CalibrationRef
    fee: FeeRef
    forecast_source: str
    adverse_fill: float
    contracts_frame: int
    availability_lag_min: int
    sigma_cap: float
    mode: str
    registry_status: str
    source: str
    parity: Dict[str, Any] = field(default_factory=dict)
    spec_hash: str = ""

    # -- views ---------------------------------------------------------------
    def genome(self):
        """Decode ``genome_json`` into a ``src.factory.genome.Genome`` (numpy-only import)."""
        from src.factory.genome import Genome

        return Genome.from_json(self.genome_json)

    @property
    def id8(self) -> str:
        return self.genome_id[:8]

    def to_doc(self, *, with_hash: bool = True) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "genome_id": self.genome_id,
            "genome_json": self.genome_json,
            "family": self.family,
            "config_sha256": self.config_sha256,
            "frame_search_sha256": self.frame_search_sha256,
            "calibration": {"dir": self.calibration.dir, "sha256": self.calibration.sha256},
            "fee": {
                "type": self.fee.type,
                "fee_type": self.fee.fee_type,
                "regime_sha256": self.fee.regime_sha256,
            },
            "forecast_source": self.forecast_source,
            "adverse_fill": float(self.adverse_fill),
            "contracts_frame": int(self.contracts_frame),
            "availability_lag_min": int(self.availability_lag_min),
            "sigma_cap": float(self.sigma_cap),
            "mode": self.mode,
            "registry_status": self.registry_status,
            "source": self.source,
            "parity": dict(self.parity),
        }
        if with_hash:
            doc["spec_hash"] = spec_hash_of(doc)
        return doc

    def with_hash(self) -> "PromotedSpec":
        """The same spec with ``spec_hash`` (re)computed."""
        return from_doc(self.to_doc(with_hash=True))


def _require(doc: Mapping[str, Any]) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in doc]
    if missing:
        raise PromotedSpecError(f"promoted spec lacks keys {missing}")
    extra = sorted(set(doc) - set(REQUIRED_KEYS))
    if extra:
        raise PromotedSpecError(f"promoted spec carries unknown keys {extra}")


def from_doc(doc: Mapping[str, Any], *, verify_hash: bool = True) -> PromotedSpec:
    """Build a ``PromotedSpec`` from its JSON document, verifying ``spec_hash`` and the id."""
    _require(doc)
    if verify_hash:
        want = spec_hash_of(doc)
        if doc.get("spec_hash") != want:
            raise PromotedSpecError(
                f"spec_hash {str(doc.get('spec_hash'))[:12]} does not verify (content hashes to {want[:12]})"
            )
    gj = str(doc["genome_json"])
    if genome_id_for(gj) != doc["genome_id"]:
        raise PromotedSpecError(
            f"genome_id {doc['genome_id']} != sha256(genome_json)[:16] = {genome_id_for(gj)}"
        )
    if doc["mode"] not in MODES:
        raise PromotedSpecError(f"mode {doc['mode']!r} not in {MODES}")
    if doc["mode"] == "paper" and doc["registry_status"] not in PAPER_ALLOWED_STATUSES:
        raise PromotedSpecError(
            f"mode 'paper' requires registry status in {PAPER_ALLOWED_STATUSES}, "
            f"spec says {doc['registry_status']!r}"
        )
    cal = doc["calibration"]
    fee = doc["fee"]
    if fee.get("type") not in ("taker", "maker"):
        raise PromotedSpecError(f"fee.type {fee.get('type')!r} must be 'taker' or 'maker'")
    return PromotedSpec(
        genome_id=str(doc["genome_id"]),
        genome_json=gj,
        family=str(doc["family"]),
        config_sha256=str(doc["config_sha256"]),
        frame_search_sha256=str(doc["frame_search_sha256"]),
        calibration=CalibrationRef(dir=str(cal["dir"]), sha256=str(cal["sha256"])),
        fee=FeeRef(
            type=str(fee["type"]), fee_type=str(fee["fee_type"]), regime_sha256=str(fee["regime_sha256"])
        ),
        forecast_source=str(doc["forecast_source"]),
        adverse_fill=float(doc["adverse_fill"]),
        contracts_frame=int(doc["contracts_frame"]),
        availability_lag_min=int(doc["availability_lag_min"]),
        sigma_cap=float(doc["sigma_cap"]),
        mode=str(doc["mode"]),
        registry_status=str(doc["registry_status"]),
        source=str(doc["source"]),
        parity=dict(doc.get("parity") or {}),
        spec_hash=str(doc.get("spec_hash", "")),
    )


def build_spec(
    genome: Any,
    *,
    family: str,
    config_sha256: str,
    frame_search_sha256: str,
    calibration_dir: str,
    calibration_sha256: str,
    fee_type: str,
    fee_regime_sha256: str,
    adverse_fill: float = 0.01,
    contracts_frame: int = 20,
    availability_lag_min: int = 240,
    sigma_cap: float = 4.0,
    mode: str = "shadow",
    registry_status: str = "CLOSED",
    source: str = "seed",
    parity: Optional[Mapping[str, Any]] = None,
) -> PromotedSpec:
    """Assemble a spec for ``genome`` (a ``Genome``); ``spec_hash`` is computed here."""
    gj = genome_json_for(genome)
    g = genome
    if isinstance(genome, str):
        from src.factory.genome import Genome

        g = Genome.from_json(genome)
    mode_label = "maker" if int(g.mode) == 1 else "taker"
    doc = {
        "genome_id": genome_id_for(gj),
        "genome_json": gj,
        "family": family,
        "config_sha256": config_sha256,
        "frame_search_sha256": frame_search_sha256,
        "calibration": {"dir": _relpath(calibration_dir), "sha256": calibration_sha256},
        "fee": {"type": mode_label, "fee_type": fee_type, "regime_sha256": fee_regime_sha256},
        "forecast_source": str(getattr(g, "source", "gfs_mex")),
        "adverse_fill": float(adverse_fill),
        "contracts_frame": int(contracts_frame),
        "availability_lag_min": int(availability_lag_min),
        "sigma_cap": float(sigma_cap),
        "mode": mode,
        "registry_status": registry_status,
        "source": source,
        "parity": dict(parity or {}),
    }
    doc["spec_hash"] = spec_hash_of(doc)
    return from_doc(doc)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def spec_path(genome_id: str, directory: str = PROMOTED_DIR) -> str:
    return os.path.join(directory, f"{genome_id}.json")


def resolve_path(id_or_path: str, directory: str = PROMOTED_DIR) -> str:
    """A path (exists as given) or a genome id resolved under ``directory``."""
    if os.path.isfile(id_or_path):
        return id_or_path
    p = spec_path(id_or_path, directory)
    if os.path.isfile(p):
        return p
    raise PromotedSpecError(f"no promoted spec at {id_or_path!r} nor {p}")


def load_promoted(id_or_path: str, directory: str = PROMOTED_DIR) -> PromotedSpec:
    """Load and verify a promoted spec by genome id or file path (raises ``PromotedSpecError``)."""
    path = resolve_path(id_or_path, directory)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PromotedSpecError(f"{path}: cannot read promoted spec ({exc})") from exc
    if not isinstance(doc, dict):
        raise PromotedSpecError(f"{path}: promoted spec is not a JSON object")
    try:
        return from_doc(doc)
    except PromotedSpecError as exc:
        raise PromotedSpecError(f"{path}: {exc}") from exc


def write_promoted(spec: PromotedSpec, path: Union[str, os.PathLike, None] = None) -> str:
    """Write ``spec`` (sort_keys, indent=2, trailing newline, LF) via ``report.write_json``."""
    from pathlib import Path

    from src.factory.report import write_json

    target = Path(path) if path is not None else Path(spec_path(spec.genome_id))
    doc = spec.to_doc(with_hash=True)
    from_doc(doc)  # never write a document that would not load
    write_json(target, doc)
    return str(target)


__all__ = [
    "MODES",
    "PAPER_ALLOWED_STATUSES",
    "PROMOTED_DIR",
    "REQUIRED_KEYS",
    "CalibrationRef",
    "FeeRef",
    "PromotedSpec",
    "PromotedSpecError",
    "build_spec",
    "calibration_dir_sha256",
    "canonical_json",
    "from_doc",
    "genome_id_for",
    "genome_json_for",
    "hash_calibration_dir",
    "load_promoted",
    "resolve_path",
    "sha256_of_mapping",
    "spec_hash_of",
    "spec_path",
    "write_promoted",
]
