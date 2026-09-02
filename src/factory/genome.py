"""GENE_SPEC v1 genome: encoding, operators, seeds, and ``to_mask`` (FR-F1.2).

A genome is a fixed-length ``np.int16`` vector decoded into a *conjunction of
column predicates* over the VISIBLE columns of a factory frame
(``src/factory/columns.py``) plus a frozen entry policy (one entry per
market). Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 3;
governing assumptions PRD_STRATEGY_FACTORY.md section 4 A2/A6.

Rules
-----
* numpy-only. This module is imported by the maia sandbox image, so it must
  not import pandas, pyarrow or anything under ``src.backtest``.
* Runs on Python 3.11 / numpy 1.25 AND Python 3.12 / numpy 2.x: no
  ``np.float_``, ``np.NaN``, ``np.in1d``, ``np.product``.
* A predicate may name VISIBLE columns only. Naming a hidden column raises
  ``columns.HiddenColumnError`` when the ``Predicate``/``Genome`` is
  *constructed*, never at evaluation time.
* ``to_mask(g, F)`` uses ONE code path for a whole ``Frame`` (returns a bool
  ndarray) and for a single row mapping (returns a numpy bool scalar). That is
  the lab/sandbox parity contract: ``to_mask(g, F)[i] == to_mask(g,
  row_view(F, i))`` for every row.

Encoding
--------
``genes[k]`` for gene ``k`` of ``GENE_SPEC``:

* categorical: index into ``domain`` (``direction``: 0 buy_yes / 1 buy_no;
  ``mode``: 0 taker / 1 maker -- matching ``columns.DIRECTION_LABELS`` and
  ``columns.MODE_LABELS``).
* subset: a bitmask over ``domain`` labels (bit ``i`` <-> label ``i``); legal
  values are ``1 .. 2**n - 1`` (never empty).
* ordinal: index into ``domain`` where index 0 is ``OFF`` (``None``) for every
  gene that allows OFF. ``sigma_cap`` allows OFF for ENCODING (parity/seeds
  only); the search never draws it and ``repair`` removes it.
* frozen (``entries_per_market``): always index 0 (value 1).

The ``far_margin`` predicate is written in exactly the evaluator's arithmetic
form so that boundary rows compare identically to ``ev_analysis.fr31a_mask``:
NO side ``yes_ask < 1.0 and p_yes <= yes_ask - v``; YES side (mirror)
``yes_bid > 0.0 and p_yes >= yes_bid + v``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from src.factory import columns as C
from src.factory.columns import Frame, HiddenColumnError, VisibleOnly, assert_visible

GENE_SPEC_VERSION = 1

OFF = None  # decoded value of an ordinal gene that is switched off

KIND_CATEGORICAL = "categorical"
KIND_SUBSET = "subset"
KIND_ORDINAL = "ordinal"

# ---------------------------------------------------------------------------
# Gene specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneSpec:
    """One gene of GENE_SPEC v1.

    ``domain``: categorical -> labels; subset -> labels (encoded as bitmask);
    ordinal -> values with ``None`` (OFF) at index 0 when ``off_allowed``.
    ``off_in_search``: whether OFF may appear in a searchable genome
    (False for ``sigma_cap``: OFF is encoding-only).
    ``searchable``: encoded values the search may use (None = all legal).
    """

    name: str
    kind: str
    domain: Tuple[Any, ...]
    off_allowed: bool = False
    frozen: bool = False
    off_in_search: bool = True
    searchable: Optional[Tuple[int, ...]] = None

    @property
    def n_bits(self) -> int:
        return len(self.domain) if self.kind == KIND_SUBSET else 0

    @property
    def full_mask(self) -> int:
        return (1 << len(self.domain)) - 1 if self.kind == KIND_SUBSET else 0

    @property
    def n_values(self) -> int:
        if self.kind == KIND_SUBSET:
            return self.full_mask  # 1 .. 2**n - 1
        return len(self.domain)

    def is_valid_code(self, code: int) -> bool:
        code = int(code)
        if self.kind == KIND_SUBSET:
            return 1 <= code <= self.full_mask
        return 0 <= code < len(self.domain)

    def is_off(self, code: int) -> bool:
        return self.kind == KIND_ORDINAL and self.off_allowed and int(code) == 0

    def search_codes(self) -> Tuple[int, ...]:
        """Encoded values the search may draw."""
        if self.searchable is not None:
            return self.searchable
        if self.kind == KIND_SUBSET:
            return tuple(range(1, self.full_mask + 1))
        if self.kind == KIND_ORDINAL and self.off_allowed and not self.off_in_search:
            return tuple(range(1, len(self.domain)))
        return tuple(range(len(self.domain)))


def _grid(start: float, stop: float, step: float) -> Tuple[float, ...]:
    n = int(round((stop - start) / step)) + 1
    return tuple(round(start + i * step, 10) for i in range(n))


P_WIN_LO_VALUES: Tuple[float, ...] = _grid(0.50, 0.95, 0.05)  # 10 values
P_WIN_HI_VALUES: Tuple[float, ...] = _grid(0.60, 1.00, 0.05)  # 9 values
FAR_MARGIN_VALUES: Tuple[float, ...] = _grid(0.00, 0.20, 0.02)  # 11 values
#: {0.02, 0.05, 0.10, 0.15, ..., 0.50}: 11 values (+OFF = 12, <= 15 per spec)
QUOTE_LO_VALUES: Tuple[float, ...] = (0.02, 0.05) + _grid(0.10, 0.50, 0.05)
#: {0.10, 0.20, ..., 0.80, 0.85, 0.90, 0.95, 0.98}: 12 values (+OFF = 13)
QUOTE_HI_VALUES: Tuple[float, ...] = _grid(0.10, 0.80, 0.10) + (0.85, 0.90, 0.95, 0.98)
SIGMA_CAP_VALUES: Tuple[float, ...] = (2.0, 2.5, 3.0, 3.5, 4.0)
EDGE_DISTANCE_VALUES: Tuple[int, ...] = (1, 2, 3, 4, 5, 6)

GENE_SPEC: Tuple[GeneSpec, ...] = (
    GeneSpec("direction", KIND_CATEGORICAL, C.DIRECTION_LABELS),
    GeneSpec("mode", KIND_CATEGORICAL, C.MODE_LABELS, searchable=(0,)),  # taker only
    GeneSpec("windows", KIND_SUBSET, C.WINDOW_LABELS),
    GeneSpec("bands", KIND_SUBSET, C.BAND_LABELS),
    GeneSpec("p_win_lo", KIND_ORDINAL, (OFF,) + P_WIN_LO_VALUES, off_allowed=True),
    GeneSpec("p_win_hi", KIND_ORDINAL, (OFF,) + P_WIN_HI_VALUES, off_allowed=True),
    GeneSpec("far_margin", KIND_ORDINAL, (OFF,) + FAR_MARGIN_VALUES, off_allowed=True),
    GeneSpec("quote_lo", KIND_ORDINAL, (OFF,) + QUOTE_LO_VALUES, off_allowed=True),
    GeneSpec("quote_hi", KIND_ORDINAL, (OFF,) + QUOTE_HI_VALUES, off_allowed=True),
    GeneSpec(
        "sigma_cap",
        KIND_ORDINAL,
        (OFF,) + SIGMA_CAP_VALUES,
        off_allowed=True,
        off_in_search=False,
    ),
    GeneSpec("lead_buckets", KIND_SUBSET, C.LEAD_BUCKET_LABELS),
    GeneSpec("edge_distance_lo", KIND_ORDINAL, (OFF,) + EDGE_DISTANCE_VALUES, off_allowed=True),
    GeneSpec("entries_per_market", KIND_ORDINAL, (1,), frozen=True),
)
GENE_NAMES: Tuple[str, ...] = tuple(s.name for s in GENE_SPEC)
GENE_INDEX: Dict[str, int] = {s.name: i for i, s in enumerate(GENE_SPEC)}
N_GENES = len(GENE_SPEC)
assert N_GENES == 13
MUTABLE_GENES: Tuple[int, ...] = tuple(i for i, s in enumerate(GENE_SPEC) if not s.frozen)
GENE_DTYPE = np.int16

# search-time legality constraints (pairs of ordinals: lo <= hi when both on)
ORDERED_PAIRS: Tuple[Tuple[str, str], ...] = (("p_win_lo", "p_win_hi"), ("quote_lo", "quote_hi"))


def spec(name: str) -> GeneSpec:
    return GENE_SPEC[GENE_INDEX[name]]


# ---------------------------------------------------------------------------
# encode / decode
# ---------------------------------------------------------------------------


def _subset_to_mask(s: GeneSpec, labels: Sequence[str]) -> int:
    if isinstance(labels, (int, np.integer)):
        return int(labels)
    m = 0
    for lab in labels:
        m |= 1 << s.domain.index(lab)
    return m


def _mask_to_subset(s: GeneSpec, m: int) -> Tuple[str, ...]:
    return tuple(lab for i, lab in enumerate(s.domain) if (int(m) >> i) & 1)


def _ordinal_index(s: GeneSpec, value: Any) -> int:
    if value is OFF or (isinstance(value, str) and value.upper() == "OFF"):
        if not s.off_allowed:
            raise ValueError(f"gene {s.name}: OFF not allowed")
        return 0
    v = float(value)
    for i, d in enumerate(s.domain):
        if d is not OFF and abs(float(d) - v) < 1e-9:
            return i
    raise ValueError(f"gene {s.name}: value {value!r} not in domain {s.domain}")


def encode(values: Mapping[str, Any]) -> np.ndarray:
    """Decoded gene values (labels / label tuples / floats / OFF) -> int16 vector.

    Missing subset genes default to the FULL set; missing ordinals to OFF (or
    the first domain value when OFF is not allowed); missing categoricals to
    index 0 (buy_yes / taker).
    """
    genes = np.zeros(N_GENES, dtype=GENE_DTYPE)
    unknown = set(values) - set(GENE_NAMES)
    if unknown:
        raise KeyError(f"unknown gene(s): {sorted(unknown)}")
    for i, s in enumerate(GENE_SPEC):
        if s.frozen:
            genes[i] = 0
            continue
        if s.name not in values:
            genes[i] = s.full_mask if s.kind == KIND_SUBSET else 0
            continue
        v = values[s.name]
        if s.kind == KIND_CATEGORICAL:
            genes[i] = s.domain.index(v) if isinstance(v, str) else int(v)
        elif s.kind == KIND_SUBSET:
            genes[i] = _subset_to_mask(s, v)
        else:
            genes[i] = _ordinal_index(s, v)
    return genes


def decode(genes: np.ndarray) -> Dict[str, Any]:
    """int16 vector -> {gene name: decoded value}."""
    genes = np.asarray(genes)
    out: Dict[str, Any] = {}
    for i, s in enumerate(GENE_SPEC):
        code = int(genes[i])
        if s.kind == KIND_CATEGORICAL:
            out[s.name] = s.domain[code]
        elif s.kind == KIND_SUBSET:
            out[s.name] = _mask_to_subset(s, code)
        else:
            out[s.name] = s.domain[code]
    return out


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

Getter = Callable[[str], Any]


@dataclass(frozen=True)
class Predicate:
    """``column <op> value`` over visible columns; validated at construction.

    ops: ``eq`` (col == value), ``in`` (bit ``col`` of value, a bitmask over
    ``n_labels``), ``ge``/``le``/``gt``/``lt`` (col vs value),
    ``le_diff`` (col <= other - value), ``ge_sum`` (col >= other + value).
    """

    column: str
    op: str
    value: Any
    other: Optional[str] = None
    n_labels: int = 0
    _lut: Optional[np.ndarray] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        names = (self.column,) if self.other is None else (self.column, self.other)
        assert_visible(names)  # HiddenColumnError / KeyError here, never later
        if self.op not in _OPS:
            raise ValueError(f"unknown predicate op {self.op!r}")
        if self.op == "in":
            if self.n_labels <= 0:
                raise ValueError("'in' predicate needs n_labels")
            lut = np.zeros(self.n_labels + 1, dtype=bool)  # index -1 -> False
            for i in range(self.n_labels):
                lut[i] = bool((int(self.value) >> i) & 1)
            object.__setattr__(self, "_lut", lut)
        if self.op in ("le_diff", "ge_sum") and self.other is None:
            raise ValueError(f"{self.op} needs 'other'")

    def __call__(self, get: Getter) -> Any:
        return _OPS[self.op](self, get)

    def describe(self) -> str:
        if self.op == "in":
            return f"{self.column} in bits({int(self.value):#b})"
        if self.op == "le_diff":
            return f"{self.column} <= {self.other} - {self.value}"
        if self.op == "ge_sum":
            return f"{self.column} >= {self.other} + {self.value}"
        sym = {"eq": "==", "ge": ">=", "le": "<=", "gt": ">", "lt": "<"}[self.op]
        return f"{self.column} {sym} {self.value}"


def _op_eq(p: Predicate, get: Getter) -> Any:
    return np.equal(get(p.column), p.value)


def _op_in(p: Predicate, get: Getter) -> Any:
    return p._lut[get(p.column)]


def _op_ge(p: Predicate, get: Getter) -> Any:
    return np.greater_equal(get(p.column), p.value)


def _op_le(p: Predicate, get: Getter) -> Any:
    return np.less_equal(get(p.column), p.value)


def _op_gt(p: Predicate, get: Getter) -> Any:
    return np.greater(get(p.column), p.value)


def _op_lt(p: Predicate, get: Getter) -> Any:
    return np.less(get(p.column), p.value)


def _op_le_diff(p: Predicate, get: Getter) -> Any:
    # evaluator form: p_yes <= yes_ask - margin  (ev_analysis.fr31a_mask)
    return np.less_equal(get(p.column), get(p.other) - p.value)


def _op_ge_sum(p: Predicate, get: Getter) -> Any:
    return np.greater_equal(get(p.column), get(p.other) + p.value)


_OPS: Dict[str, Callable[[Predicate, Getter], Any]] = {
    "eq": _op_eq,
    "in": _op_in,
    "ge": _op_ge,
    "le": _op_le,
    "gt": _op_gt,
    "lt": _op_lt,
    "le_diff": _op_le_diff,
    "ge_sum": _op_ge_sum,
}


def compile_predicates(genes: np.ndarray) -> Tuple[Predicate, ...]:
    """The conjunction a gene vector stands for (visible columns only).

    Full subsets compile to no predicate (they cannot exclude a row whose code
    is in-domain); OFF ordinals compile to no predicate.
    """
    v = decode(genes)
    preds: List[Predicate] = [
        Predicate("direction_code", "eq", C.code_for(C.DIRECTION_LABELS, v["direction"])),
        Predicate("mode_code", "eq", C.code_for(C.MODE_LABELS, v["mode"])),
    ]
    for gname, col in (
        ("windows", "window_code"),
        ("bands", "band_code"),
        ("lead_buckets", "lead_bucket_code"),
    ):
        s = spec(gname)
        code = int(genes[GENE_INDEX[gname]])
        if code != s.full_mask:
            preds.append(Predicate(col, "in", code, n_labels=s.n_bits))
    if v["p_win_lo"] is not OFF:
        preds.append(Predicate("p_win", "ge", float(v["p_win_lo"])))
    if v["p_win_hi"] is not OFF:
        preds.append(Predicate("p_win", "le", float(v["p_win_hi"])))
    if v["far_margin"] is not OFF:
        m = float(v["far_margin"])
        if v["direction"] == "buy_no":
            preds.append(Predicate("yes_ask", "lt", 1.0))
            preds.append(Predicate("p_yes", "le_diff", m, other="yes_ask"))
        else:
            preds.append(Predicate("yes_bid", "gt", 0.0))
            preds.append(Predicate("p_yes", "ge_sum", m, other="yes_bid"))
    if v["quote_lo"] is not OFF:
        preds.append(Predicate("quote", "ge", float(v["quote_lo"])))
    if v["quote_hi"] is not OFF:
        preds.append(Predicate("quote", "le", float(v["quote_hi"])))
    if v["sigma_cap"] is not OFF:
        preds.append(Predicate("sigma_f", "le", float(v["sigma_cap"])))
    if v["edge_distance_lo"] is not OFF:
        preds.append(Predicate("edge_distance_f", "ge", float(v["edge_distance_lo"])))
    return tuple(preds)


# ---------------------------------------------------------------------------
# Genome
# ---------------------------------------------------------------------------


def _check_genes(genes: np.ndarray) -> np.ndarray:
    g = np.asarray(genes)
    if g.shape != (N_GENES,):
        raise ValueError(f"genes must have shape ({N_GENES},), got {g.shape}")
    g = g.astype(GENE_DTYPE, copy=True)
    for i, s in enumerate(GENE_SPEC):
        if not s.is_valid_code(int(g[i])):
            raise ValueError(f"gene {s.name}: code {int(g[i])} outside its domain")
    g.setflags(write=False)
    return g


@dataclass(frozen=True, eq=False)
class Genome:
    """An immutable GENE_SPEC v1 genome (canonical int16 encoding + metadata).

    Equality/hash are over ``genes`` and ``source`` (the phenotype-determining
    parts); ``name``/``notes`` are labels.
    """

    genes: np.ndarray
    name: str = ""
    notes: str = ""
    source: str = "gfs_mex"
    predicates: Tuple[Predicate, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        g = _check_genes(self.genes)
        object.__setattr__(self, "genes", g)
        object.__setattr__(self, "predicates", compile_predicates(g))

    # -- construction helpers -------------------------------------------
    @classmethod
    def from_values(cls, name: str = "", notes: str = "", source: str = "gfs_mex", **values: Any) -> "Genome":
        return cls(encode(values), name=name, notes=notes, source=source)

    def with_meta(self, name: Optional[str] = None, notes: Optional[str] = None, source: Optional[str] = None) -> "Genome":
        return Genome(
            self.genes,
            name=self.name if name is None else name,
            notes=self.notes if notes is None else notes,
            source=self.source if source is None else source,
        )

    def replace(self, **values: Any) -> "Genome":
        d = self.values()
        d.update(values)
        return Genome(encode(d), name=self.name, notes=self.notes, source=self.source)

    # -- views -----------------------------------------------------------
    def values(self) -> Dict[str, Any]:
        return decode(self.genes)

    def value(self, name: str) -> Any:
        return decode(self.genes)[name]

    @property
    def direction(self) -> int:
        return int(self.genes[GENE_INDEX["direction"]])

    @property
    def mode(self) -> int:
        return int(self.genes[GENE_INDEX["mode"]])

    def describe(self) -> str:
        return " & ".join(p.describe() for p in self.predicates)

    # -- equality --------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Genome):
            return NotImplemented
        return bool(np.array_equal(self.genes, other.genes)) and self.source == other.source

    def __hash__(self) -> int:
        return hash((self.genes.tobytes(), self.source))

    def __repr__(self) -> str:
        return f"Genome(name={self.name!r}, source={self.source!r}, {self.values()})"

    # -- legality --------------------------------------------------------
    def is_legal(self) -> bool:
        return is_legal(self)

    def is_searchable(self) -> bool:
        return is_searchable(self)

    # -- JSON ------------------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        vals = self.values()
        genes_json: Dict[str, Any] = {}
        for s in GENE_SPEC:
            v = vals[s.name]
            if s.kind == KIND_SUBSET:
                genes_json[s.name] = list(v)
            elif v is OFF:
                genes_json[s.name] = "OFF"
            else:
                genes_json[s.name] = v
        return {
            "gene_spec_version": GENE_SPEC_VERSION,
            "name": self.name,
            "notes": self.notes,
            "source": self.source,
            "genes": genes_json,
            "encoding": [int(x) for x in self.genes],
            "n_active_clauses": n_active_clauses(self),
        }

    @classmethod
    def from_json(cls, obj: Union[str, Mapping[str, Any]]) -> "Genome":
        d = json.loads(obj) if isinstance(obj, str) else dict(obj)
        ver = int(d.get("gene_spec_version", -1))
        if ver != GENE_SPEC_VERSION:
            raise ValueError(f"gene_spec_version {ver} != {GENE_SPEC_VERSION}")
        genes = encode(d["genes"])
        g = cls(
            genes,
            name=str(d.get("name", "")),
            notes=str(d.get("notes", "")),
            source=str(d.get("source", "gfs_mex")),
        )
        enc = d.get("encoding")
        if enc is not None and not np.array_equal(np.asarray(enc, dtype=GENE_DTYPE), g.genes):
            raise ValueError("JSON 'encoding' disagrees with 'genes'")
        return g

    def to_json_str(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True)

    # -- random ----------------------------------------------------------
    @staticmethod
    def random(rng: np.random.Generator) -> "Genome":
        """A uniformly-random searchable genome (mode taker, sigma_cap on).

        Ordinal genes that allow OFF are OFF with p=0.5, otherwise uniform
        over their values; subsets uniform over non-empty bitmasks.
        """
        genes = np.zeros(N_GENES, dtype=GENE_DTYPE)
        for i, s in enumerate(GENE_SPEC):
            if s.frozen:
                continue
            if s.kind == KIND_CATEGORICAL:
                codes = s.search_codes()
                genes[i] = codes[int(rng.integers(0, len(codes)))]
            elif s.kind == KIND_SUBSET:
                genes[i] = int(rng.integers(1, s.full_mask + 1))
            else:
                if s.off_allowed and s.off_in_search and rng.random() < 0.5:
                    genes[i] = 0
                else:
                    genes[i] = int(rng.integers(1, len(s.domain)))
        return repair(Genome(genes), rng)


def is_legal(g: Genome) -> bool:
    """Domain-valid codes, non-empty subsets, ``lo <= hi`` for ordered pairs."""
    genes = g.genes
    for i, s in enumerate(GENE_SPEC):
        if not s.is_valid_code(int(genes[i])):
            return False
    v = decode(genes)
    for lo, hi in ORDERED_PAIRS:
        if v[lo] is not OFF and v[hi] is not OFF and float(v[lo]) > float(v[hi]):
            return False
    return True


def is_searchable(g: Genome) -> bool:
    """Legal AND inside the search domain: mode taker, ``sigma_cap != OFF``."""
    if not is_legal(g):
        return False
    for i, s in enumerate(GENE_SPEC):
        if int(g.genes[i]) not in s.search_codes():
            return False
    return True


def n_active_clauses(g: Genome) -> int:
    """Optional clauses that restrict beyond the structural direction/mode.

    Counts each non-full subset gene and each non-OFF ordinal gene once
    (``far_margin`` is one clause although it compiles to two predicates).
    """
    n = 0
    genes = g.genes
    for i, s in enumerate(GENE_SPEC):
        if s.frozen or s.kind == KIND_CATEGORICAL:
            continue
        code = int(genes[i])
        if s.kind == KIND_SUBSET:
            n += int(code != s.full_mask)
        elif not s.is_off(code):
            n += 1
    return n


# ---------------------------------------------------------------------------
# Operators (architecture section 3)
# ---------------------------------------------------------------------------

P_DIRECTION_FLIP = 0.02
P_OFF_JUMP = 0.10


def _mutate_subset(s: GeneSpec, code: int, rng: np.random.Generator) -> int:
    """Flip one random bit; never produce the empty set."""
    bit = int(rng.integers(0, s.n_bits))
    new = code ^ (1 << bit)
    if new == 0:  # only bit set -> set a different bit instead of clearing it
        others = [b for b in range(s.n_bits) if b != bit]
        new = code | (1 << others[int(rng.integers(0, len(others)))])
    return new


def _mutate_ordinal(s: GeneSpec, code: int, rng: np.random.Generator) -> int:
    n = len(s.domain)
    if s.off_allowed and s.off_in_search:
        if rng.random() < P_OFF_JUMP:
            # jump to/from OFF
            return 0 if code != 0 else int(rng.integers(1, n))
        if code == 0:
            return 0  # OFF leaves OFF only through the jump
        lo = 1
    else:
        lo = 1 if s.off_allowed else 0  # sigma_cap: OFF is never a search value
        if code < lo:
            return int(rng.integers(lo, n))
    step = -1 if rng.random() < 0.5 else 1
    new = code + step
    if new < lo or new >= n:
        new = code  # boundary: stay
    return new


def mutate(g: Genome, rng: np.random.Generator) -> Genome:
    """Per-gene mutation at rate 1/L (L = mutable genes); direction flips with
    p=0.02; ``mode`` is never mutated (taker-only search); result repaired."""
    genes = np.array(g.genes, dtype=GENE_DTYPE)
    L = len(MUTABLE_GENES)
    rate = 1.0 / L
    for i in MUTABLE_GENES:
        s = GENE_SPEC[i]
        if s.name == "direction":
            if rng.random() < P_DIRECTION_FLIP:
                genes[i] = 1 - genes[i]
            continue
        if s.name == "mode":
            continue
        if rng.random() >= rate:
            continue
        code = int(genes[i])
        if s.kind == KIND_SUBSET:
            genes[i] = _mutate_subset(s, code, rng)
        elif s.kind == KIND_ORDINAL:
            genes[i] = _mutate_ordinal(s, code, rng)
    return repair(Genome(genes, name="", notes="", source=g.source), rng)


def crossover(a: Genome, b: Genome, rng: np.random.Generator) -> Genome:
    """Uniform crossover, p=0.5 per gene, then repair."""
    take_b = rng.random(N_GENES) < 0.5
    genes = np.where(take_b, b.genes, a.genes).astype(GENE_DTYPE)
    return repair(Genome(genes, source=a.source), rng)


def repair(g: Genome, rng: np.random.Generator) -> Genome:
    """Make ``g`` searchable by resampling offending genes.

    Non-empty subsets; ``lo <= hi`` (the *hi* gene is resampled among the
    values >= lo, or OFF); ``sigma_cap != OFF``; ``mode = taker``.
    """
    genes = np.array(g.genes, dtype=GENE_DTYPE)
    changed = False
    for i, s in enumerate(GENE_SPEC):
        code = int(genes[i])
        if s.frozen:
            if code != 0:
                genes[i] = 0
                changed = True
            continue
        if s.kind == KIND_SUBSET and code == 0:
            genes[i] = int(rng.integers(1, s.full_mask + 1))
            changed = True
        elif s.kind == KIND_ORDINAL and s.off_allowed and not s.off_in_search and code == 0:
            genes[i] = int(rng.integers(1, len(s.domain)))
            changed = True
        elif s.kind == KIND_CATEGORICAL and s.searchable is not None and code not in s.searchable:
            genes[i] = s.searchable[int(rng.integers(0, len(s.searchable)))]
            changed = True
    for lo_name, hi_name in ORDERED_PAIRS:
        lo_s, hi_s = spec(lo_name), spec(hi_name)
        lo_c, hi_c = int(genes[GENE_INDEX[lo_name]]), int(genes[GENE_INDEX[hi_name]])
        if lo_c == 0 or hi_c == 0:
            continue
        lo_v, hi_v = float(lo_s.domain[lo_c]), float(hi_s.domain[hi_c])
        if lo_v > hi_v:
            ok = [0] + [k for k in range(1, len(hi_s.domain)) if float(hi_s.domain[k]) >= lo_v]
            genes[GENE_INDEX[hi_name]] = ok[int(rng.integers(0, len(ok)))]
            changed = True
    if not changed:
        return g
    return Genome(genes, name=g.name, notes=g.notes, source=g.source)


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------


def _getter(F: Union[Frame, Mapping[str, Any]]) -> Getter:
    if isinstance(F, Frame):
        return F.col  # raises HiddenColumnError on a hidden name
    if isinstance(F, VisibleOnly):
        return F.__getitem__
    if isinstance(F, Mapping):
        return VisibleOnly(F).__getitem__
    raise TypeError(f"to_mask needs a Frame or a row mapping, got {type(F).__name__}")


def to_mask(g: Genome, F: Union[Frame, Mapping[str, Any]]) -> Any:
    """Conjunction of ``g``'s predicates over ``F``.

    ``F`` a ``Frame`` -> bool ndarray of ``n_rows``; ``F`` a row mapping
    (``columns.row_view``) -> numpy bool scalar. Same code path either way.
    """
    get = _getter(F)
    preds = g.predicates
    acc = preds[0](get)
    if isinstance(acc, np.ndarray):
        acc = np.array(acc, dtype=bool, copy=True)
        for p in preds[1:]:
            np.logical_and(acc, p(get), out=acc)
        return acc
    out = np.bool_(acc)
    for p in preds[1:]:
        out = np.logical_and(out, p(get))
    return np.bool_(out)


def first_true_per_block(M: np.ndarray, block_starts: np.ndarray) -> np.ndarray:
    """Row index of the FIRST True in each market block (blocks with none skipped).

    Rows are sorted by ``(market_code, ts_utc)`` so this is the earliest
    masked snapshot per market -- ``groupby('market_ticker').head(1)`` on the
    mergesort-sorted evaluator frame.
    """
    idx = np.flatnonzero(M)
    if idx.size == 0:
        return idx.astype(np.int64)
    block = np.searchsorted(block_starts, idx, side="right") - 1
    keep = np.empty(idx.size, dtype=bool)
    keep[0] = True
    np.not_equal(block[1:], block[:-1], out=keep[1:])
    return idx[keep].astype(np.int64)


def phenotype_hash_from_codes(market_codes: np.ndarray) -> str:
    """sha1 of the sorted, comma-joined decimal market codes."""
    codes = np.unique(np.asarray(market_codes, dtype=np.int64))
    payload = ",".join(str(int(c)) for c in codes).encode("ascii")
    return hashlib.sha1(payload).hexdigest()


def phenotype_hash(g: Genome, F: Frame, date_mask: Optional[np.ndarray] = None) -> str:
    """sha1 of the sorted set of ``market_code`` the genome trades on ``F``.

    "Trades" = the first masked EXECUTABLE row per market block (the fitness
    kernel's trade set); ``date_mask`` restricts the rows considered.
    """
    M = to_mask(g, F)
    np.logical_and(M, F.visible["executable"], out=M)
    if date_mask is not None:
        np.logical_and(M, date_mask, out=M)
    rows = first_true_per_block(M, F.block_starts)
    return phenotype_hash_from_codes(F.visible["market_code"][rows])


# ---------------------------------------------------------------------------
# Generation-0 seeds (PRD FR-F1.5; orchestrator resolution 2026-09-02)
# ---------------------------------------------------------------------------

ALL_WINDOWS = C.WINDOW_LABELS
ALL_BANDS = C.BAND_LABELS
ALL_LEADS = C.LEAD_BUCKET_LABELS
TRADEABLE_WINDOWS = (">=24h", "12-24h")  # ev_analysis.TRADEABLE_WINDOWS

seed_notes: Dict[str, str] = {
    "fr31a_taker": (
        "PRD FR-3.1(a) far-bracket NO, taker, >=12h to close: == ev_analysis.fr31a_mask & "
        "mode==taker row-for-row (margin 0.08 in the evaluator's own arithmetic "
        "p_yes <= yes_ask - 0.08 with yes_ask < 1.0; edge_distance_f >= 4)."
    ),
    "fr31b": (
        "PRD FR-3.1(b) lock-in: buy YES, taker, <12h to close, p_win >= 0.95 "
        "(== ev_analysis.fr31b_mask & mode==taker)."
    ),
    "nofilter_no": (
        "The Phase-2 BASELINE shape ('BASELINE far-bracket NO, no 8pt filter, taker, >=12h'): "
        "buy NO, taker, windows {>=24h,12-24h}, bands {4-5F,5F+}, no p/margin filter. "
        "Pinned to 664 trades / +0.0209 on the parity frame; supersedes the architecture's "
        "'all windows, all bands' wording."
    ),
    "far_yes_taker": (
        "Diagnostic: the Phase-2 'far-bracket YES (buy the tail), taker, >=12h' shape "
        "(813 trades, -0.062) so all four Phase-2 taker shapes are covered."
    ),
    "salvage_5f": (
        "DIAGNOSTIC ONLY: buy NO, MAKER, bands {5F+}, all windows. mode=maker is legal for "
        "encoding but not searchable (Genome.is_searchable() is False; random() never draws it)."
    ),
    "mlweather_fallback": (
        "APPROXIMATION of what maia's MLWeatherStrategy analytical fallback traded "
        "(src/strategies/ml_weather.py + src/ml/predictor.py:583-604): buy NO, taker, when the "
        "predictor's Gaussian P(in bracket)=max(0.05, exp(-0.5 z^2)), z=(forecast-mid)/(width/2) "
        "with width = cap-floor = 1 for 'between' brackets, gives no_edge = bid - P >= 0.08. "
        "For a forecast >= 1.22F from the bracket midpoint P floors at 0.05 so the rule is "
        "'bid >= 0.13'. GENE_SPEC v1 encoding: bands {1-2F..5F+} (distance_f = |midpoint - mu| "
        ">= 1F is the nearest legal value to 1.22F, and mu_f is the calibrated median, not the "
        "raw NWS high the sandbox used); quote_hi 0.85 (quote = 1 - bid <= 0.87 -> nearest grid "
        "value); windows {>=24h,12-24h,6-12h} (the 10:00-13:59 ET decision window maps to "
        "12-24h/6-12h for same-day and >=24h for next-day markets on the parity tape); p_win OFF "
        "(the fallback never used the calibration). NOT encoded: the one-slot-per-city "
        "'highest YES bid first' selection (cross-row), the METAR/NWS source gate, the winner "
        "guard, the Yogi-Berra branch, and the fallback's YES branch on open-ended tails "
        "(P clipped to 0.95 on a 50F virtual bracket -> BUY YES when ask <= 0.87)."
    ),
    "fr31a_gefs": (
        "Same genes as fr31a_taker, scored on the gefs twin frame (source='gefs'); on the "
        "gfs_mex parity frame it reproduces fr31a_taker."
    ),
}

SEEDS: Dict[str, Genome] = {
    "fr31a_taker": Genome.from_values(
        name="fr31a_taker",
        notes=seed_notes["fr31a_taker"],
        direction="buy_no",
        mode="taker",
        windows=TRADEABLE_WINDOWS,
        bands=ALL_BANDS,
        p_win_lo=OFF,
        p_win_hi=OFF,
        far_margin=0.08,
        quote_lo=OFF,
        quote_hi=OFF,
        sigma_cap=OFF,
        lead_buckets=ALL_LEADS,
        edge_distance_lo=4,
    ),
    "fr31b": Genome.from_values(
        name="fr31b",
        notes=seed_notes["fr31b"],
        direction="buy_yes",
        mode="taker",
        windows=("6-12h", "3-6h", "1-3h", "<1h"),
        bands=ALL_BANDS,
        p_win_lo=0.95,
        p_win_hi=OFF,
        far_margin=OFF,
        quote_lo=OFF,
        quote_hi=OFF,
        sigma_cap=OFF,
        lead_buckets=ALL_LEADS,
        edge_distance_lo=OFF,
    ),
    "nofilter_no": Genome.from_values(
        name="nofilter_no",
        notes=seed_notes["nofilter_no"],
        direction="buy_no",
        mode="taker",
        windows=TRADEABLE_WINDOWS,
        bands=("4-5F", "5F+"),
        p_win_lo=OFF,
        p_win_hi=OFF,
        far_margin=OFF,
        quote_lo=OFF,
        quote_hi=OFF,
        sigma_cap=OFF,
        lead_buckets=ALL_LEADS,
        edge_distance_lo=OFF,
    ),
    "far_yes_taker": Genome.from_values(
        name="far_yes_taker",
        notes=seed_notes["far_yes_taker"],
        direction="buy_yes",
        mode="taker",
        windows=TRADEABLE_WINDOWS,
        bands=("4-5F", "5F+"),
        p_win_lo=OFF,
        p_win_hi=OFF,
        far_margin=OFF,
        quote_lo=OFF,
        quote_hi=OFF,
        sigma_cap=OFF,
        lead_buckets=ALL_LEADS,
        edge_distance_lo=OFF,
    ),
    "salvage_5f": Genome.from_values(
        name="salvage_5f",
        notes=seed_notes["salvage_5f"],
        direction="buy_no",
        mode="maker",
        windows=ALL_WINDOWS,
        bands=("5F+",),
        p_win_lo=OFF,
        p_win_hi=OFF,
        far_margin=OFF,
        quote_lo=OFF,
        quote_hi=OFF,
        sigma_cap=OFF,
        lead_buckets=ALL_LEADS,
        edge_distance_lo=OFF,
    ),
    "mlweather_fallback": Genome.from_values(
        name="mlweather_fallback",
        notes=seed_notes["mlweather_fallback"],
        direction="buy_no",
        mode="taker",
        windows=(">=24h", "12-24h", "6-12h"),
        bands=("1-2F", "2-3F", "3-4F", "4-5F", "5F+"),
        p_win_lo=OFF,
        p_win_hi=OFF,
        far_margin=OFF,
        quote_lo=OFF,
        quote_hi=0.85,
        sigma_cap=OFF,
        lead_buckets=ALL_LEADS,
        edge_distance_lo=OFF,
    ),
}
SEEDS["fr31a_gefs"] = SEEDS["fr31a_taker"].with_meta(
    name="fr31a_gefs", notes=seed_notes["fr31a_gefs"], source="gefs"
)
#: Seeds that are also Phase-2 shapes in reports/phase2/ws_e_go_no_go_data_2026-07-26.json
PHASE2_SHAPE_LABELS: Dict[str, str] = {
    "fr31a_taker": "FR-3.1(a) far-bracket NO, taker, >=12h to close",
    "fr31b": "FR-3.1(b) lock-in P>=0.95, taker, <12h to close",
    "far_yes_taker": "far-bracket YES (buy the tail), taker, >=12h",
    "nofilter_no": "BASELINE far-bracket NO, no 8pt filter, taker, >=12h",
}

__all__ = [
    "GENE_SPEC_VERSION",
    "GENE_SPEC",
    "GENE_NAMES",
    "GENE_INDEX",
    "N_GENES",
    "OFF",
    "GeneSpec",
    "Genome",
    "Predicate",
    "HiddenColumnError",
    "encode",
    "decode",
    "compile_predicates",
    "is_legal",
    "is_searchable",
    "n_active_clauses",
    "mutate",
    "crossover",
    "repair",
    "to_mask",
    "first_true_per_block",
    "phenotype_hash",
    "phenotype_hash_from_codes",
    "SEEDS",
    "seed_notes",
    "PHASE2_SHAPE_LABELS",
]
