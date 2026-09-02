"""Write-then-evaluate generation ledger (parquet via pyarrow).

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 6.3 ("Ledger")
and 7.3 (atomic ``gen_<N>.parquet``); PRD_STRATEGY_FACTORY FR-F1.4.

Semantics
---------
* ``append_unscored(gen, genomes)`` writes ``<run_dir>/ledger/<campaign>/gen_NNN.parquet``
  atomically (tmp file + ``os.replace``) with every row ``UNSCORED`` BEFORE any
  evaluation happens. A crash between that write and ``mark_scored`` leaves
  UNSCORED rows on disk -- never a missing row. Every genome handed to a worker
  is therefore in the ledger; that is what makes the RC/SPA multiplicity count
  honest (a killed or duplicated genome was still a test).
* ``mark_scored(gen, results)`` rewrites the same file atomically with status
  ``SCORED`` (finite fitness) or ``KILLED`` (``fit == -inf`` / ``None``), the
  fitness fields and the per-date PnL vectors.
* A generation file is written once. Re-writing it is allowed ONLY while every
  existing row is still UNSCORED (a crashed generation being redone on resume).

Schema (``SCHEMA``)
-------------------
======================  ==============  ==========================================
column                  arrow type      meaning
======================  ==============  ==========================================
row_id                  string          ``<campaign>/g<gen:03d>/<idx:05d>``
campaign                string          A / B / C / ALL69 / fold names
gen                     int32           generation number
idx                     int32           position within the generation
genome_id               string          sha256(genome_json)[:16]
genome_json             string          ``genome.to_json`` (sorted keys)
status                  string          UNSCORED | SCORED | KILLED
fitness                 double          ``FitnessResult.fit`` (NaN unscored; -inf killed)
reason                  string          constraint reason code ("" when none)
trades                  int32           masked executable trades
dates                   int32           distinct target dates traded
cities                  int16           distinct cities traded
realized                double          mean realized PnL per contract
realized_se             double          date-clustered SE
t_stat                  double
boot_lo / boot_hi       double          4000-draw date bootstrap CI
worst_date_pnl          double
bss_trades              double          Brier skill on the traded rows
phenotype_hash          string          ``genome.phenotype_hash`` on the scoring frame
per_date_pnl            list<double>    per-date PnL (position = ``per_date_codes``)
per_date_codes          list<int16>     target_date_code of each entry
======================  ==============  ==========================================
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Union

import pyarrow as pa
import pyarrow.parquet as pq

STATUS_UNSCORED = "UNSCORED"
STATUS_SCORED = "SCORED"
STATUS_KILLED = "KILLED"

SCHEMA = pa.schema(
    [
        ("row_id", pa.string()),
        ("campaign", pa.string()),
        ("gen", pa.int32()),
        ("idx", pa.int32()),
        ("genome_id", pa.string()),
        ("genome_json", pa.string()),
        ("status", pa.string()),
        ("fitness", pa.float64()),
        ("reason", pa.string()),
        ("trades", pa.int32()),
        ("dates", pa.int32()),
        ("cities", pa.int16()),
        ("realized", pa.float64()),
        ("realized_se", pa.float64()),
        ("t_stat", pa.float64()),
        ("boot_lo", pa.float64()),
        ("boot_hi", pa.float64()),
        ("worst_date_pnl", pa.float64()),
        ("bss_trades", pa.float64()),
        ("phenotype_hash", pa.string()),
        ("per_date_pnl", pa.list_(pa.float64())),
        ("per_date_codes", pa.list_(pa.int16())),
    ]
)

_GEN_RE = re.compile(r"^gen_(\d{3,})\.parquet$")


class LedgerError(RuntimeError):
    """Ledger invariant violated (e.g. rewriting a scored generation)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def genome_json(g: Any) -> str:
    """Canonical JSON for a genome: ``g.to_json()`` if present, else ``genome.to_json(g)``."""
    if isinstance(g, str):
        return g
    if hasattr(g, "to_json"):
        out = g.to_json()
    else:
        from src.factory import genome as _genome  # lazy: numpy-only module

        out = _genome.to_json(g)
    if isinstance(out, (dict, list)):
        out = json.dumps(out, sort_keys=True, separators=(",", ":"))
    return str(out)


def genome_id(gj: str) -> str:
    return hashlib.sha256(gj.encode("utf-8")).hexdigest()[:16]


def _atomic_write_table(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _fnum(v: Any, default: float = math.nan) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _inum(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _get(r: Any, name: str, default: Any = None) -> Any:
    if r is None:
        return default
    if isinstance(r, dict):
        return r.get(name, default)
    return getattr(r, name, default)


def _empty_row(campaign: str, gen: int, idx: int, gj: str) -> Dict[str, Any]:
    return {
        "row_id": f"{campaign}/g{gen:03d}/{idx:05d}",
        "campaign": campaign,
        "gen": gen,
        "idx": idx,
        "genome_id": genome_id(gj),
        "genome_json": gj,
        "status": STATUS_UNSCORED,
        "fitness": math.nan,
        "reason": "",
        "trades": 0,
        "dates": 0,
        "cities": 0,
        "realized": math.nan,
        "realized_se": math.nan,
        "t_stat": math.nan,
        "boot_lo": math.nan,
        "boot_hi": math.nan,
        "worst_date_pnl": math.nan,
        "bss_trades": math.nan,
        "phenotype_hash": "",
        "per_date_pnl": [],
        "per_date_codes": [],
    }


def _scored_row(base: Dict[str, Any], r: Any) -> Dict[str, Any]:
    row = dict(base)
    if r is None:
        row["status"] = STATUS_KILLED
        row["fitness"] = -math.inf
        row["reason"] = "NO_RESULT"
        return row
    fit = _fnum(_get(r, "fit"), -math.inf)
    reason = _get(r, "constraint_reason") or _get(r, "reason") or ""
    killed = (not math.isfinite(fit)) or bool(reason)
    row["status"] = STATUS_KILLED if killed else STATUS_SCORED
    row["fitness"] = -math.inf if killed and not math.isfinite(fit) else fit
    row["reason"] = str(reason or "")
    row["trades"] = _inum(_get(r, "trades"))
    row["dates"] = _inum(_get(r, "dates"))
    cities = _get(r, "cities")
    row["cities"] = len(cities) if isinstance(cities, (list, tuple, set)) else _inum(cities)
    for f in ("realized", "realized_se", "t_stat", "boot_lo", "boot_hi", "worst_date_pnl", "bss_trades"):
        row[f] = _fnum(_get(r, f))
    row["phenotype_hash"] = str(_get(r, "phenotype_hash") or "")
    pdp = _get(r, "per_date_pnl")
    pdc = _get(r, "per_date_codes")
    row["per_date_pnl"] = [float(x) for x in (pdp if pdp is not None else [])]
    row["per_date_codes"] = [int(x) for x in (pdc if pdc is not None else [])]
    if len(row["per_date_pnl"]) != len(row["per_date_codes"]):
        raise LedgerError(
            f"{row['row_id']}: per_date_pnl ({len(row['per_date_pnl'])}) and "
            f"per_date_codes ({len(row['per_date_codes'])}) differ in length"
        )
    return row


def _table_from_rows(rows: Sequence[Dict[str, Any]]) -> pa.Table:
    cols = {name: [r[name] for r in rows] for name in SCHEMA.names}
    return pa.table(cols, schema=SCHEMA)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
class Ledger:
    """Per-campaign generation ledger under ``<run_dir>/ledger/<campaign>/``."""

    def __init__(self, run_dir: Union[str, Path], campaign: str):
        self.run_dir = Path(run_dir)
        self.campaign = str(campaign)
        self.dir = self.run_dir / "ledger" / self.campaign
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def gen_path(self, gen: int) -> Path:
        return self.dir / f"gen_{int(gen):03d}.parquet"

    def generations(self) -> List[int]:
        out = []
        for p in self.dir.iterdir():
            m = _GEN_RE.match(p.name)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def read_gen(self, gen: int) -> pa.Table:
        return pq.read_table(self.gen_path(gen))

    # -- write-then-evaluate ----------------------------------------------
    def append_unscored(self, gen: int, genomes: Iterable[Any]) -> List[str]:
        """Write every genome of ``gen`` as UNSCORED; returns the row ids."""
        gen = int(gen)
        path = self.gen_path(gen)
        if path.exists():
            existing = pq.read_table(path, columns=["status"])
            statuses = set(existing.column("status").to_pylist())
            if statuses - {STATUS_UNSCORED}:
                raise LedgerError(
                    f"{path.name} already holds scored rows ({sorted(statuses)}); "
                    "a generation is written once"
                )
        rows = [
            _empty_row(self.campaign, gen, i, genome_json(g)) for i, g in enumerate(genomes)
        ]
        _atomic_write_table(_table_from_rows(rows), path)
        return [r["row_id"] for r in rows]

    def mark_scored(self, gen: int, results: Sequence[Any]) -> None:
        """Rewrite ``gen`` atomically with the results (aligned with ``append_unscored``)."""
        gen = int(gen)
        path = self.gen_path(gen)
        if not path.exists():
            raise LedgerError(f"{path.name} does not exist; call append_unscored first")
        table = pq.read_table(path)
        base_rows = table.to_pylist()
        if len(results) != len(base_rows):
            raise LedgerError(
                f"{path.name}: {len(results)} results for {len(base_rows)} ledger rows"
            )
        rows = [_scored_row(b, r) for b, r in zip(base_rows, results)]
        _atomic_write_table(_table_from_rows(rows), path)

    # -- reads -------------------------------------------------------------
    def read_all(self, as_pandas: bool = False):
        """Every generation concatenated (arrow Table, or a DataFrame with ``as_pandas``)."""
        gens = self.generations()
        if gens:
            table = pa.concat_tables([self.read_gen(g) for g in gens])
        else:
            table = SCHEMA.empty_table()
        return table.to_pandas() if as_pandas else table

    def phenotypes(self) -> Set[str]:
        """Distinct non-empty phenotype hashes across every generation."""
        table = self.read_all()
        return {h for h in table.column("phenotype_hash").to_pylist() if h}

    def unscored(self) -> List[str]:
        table = self.read_all()
        st = table.column("status").to_pylist()
        ids = table.column("row_id").to_pylist()
        return [i for i, s in zip(ids, st) if s == STATUS_UNSCORED]

    def summary(self) -> Dict[str, Any]:
        table = self.read_all()
        st = table.column("status").to_pylist()
        fits = table.column("fitness").to_pylist()
        finite = [f for f in fits if f is not None and math.isfinite(f)]
        return {
            "campaign": self.campaign,
            "generations": self.generations(),
            "n_rows": table.num_rows,
            "n_unscored": sum(1 for s in st if s == STATUS_UNSCORED),
            "n_scored": sum(1 for s in st if s == STATUS_SCORED),
            "n_killed": sum(1 for s in st if s == STATUS_KILLED),
            "n_phenotypes": len(self.phenotypes()),
            "n_genome_ids": len(set(table.column("genome_id").to_pylist())),
            "best_fitness": max(finite) if finite else None,
        }
