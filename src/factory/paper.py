"""PAPER board row: the sandbox's settled record beside the factory's prediction (F4, FR-F4.2).

PRD_STRATEGY_FACTORY Phase F4 exit criterion 4: *"the PAPER board row shows
settled ``target_date``s and sandbox c/contract beside the prediction"*; criterion
5: *"if HALT, the board shows the family KILLED"*. This module builds the
``paper`` dict ``src.factory.report.render_board`` renders.

What the row carries, and where each number comes from
------------------------------------------------------
* **mode** -- ``shadow`` | ``paper``: the deployed genome's execution mode
  (``/api/genome`` ``modes.genome`` over HTTP, or ``--mode`` for a file run).
* **settled target_dates** ``k`` -- the FR-5.2 grouping unit: distinct
  ``target_date`` among the genome strategy's *settled* fills (``close_reason
  EXPIRATION``, ``exit_price in {0, 1}``, ``settlement_outcome`` recorded --
  the same admission ``scripts/gate.py`` applies).
* **sandbox c/contract** -- ``sum(pnl - entry_fee) / sum(quantity)`` over those
  fills. ``pnl`` and ``entry_fee`` come from ``closed_trades`` (the exchange's
  ledger) joined on ``(symbol, entry_time, strategy_name)``; a journal row the
  ledger no longer holds gets the taker fee recomputed at its price and
  quantity, exactly as the gate does. **Never equity**: the UTC-midnight reset
  double-subtracts, and ``portfolio.realized_pnl`` is a per-cycle fragment.
* **prediction** -- the factory's number for the same genome: a family pick's
  pooled-OOS mean (``summary.json`` ``pooled_oos``), or a gen-0 seed's
  date-clustered search-frame realized (``search_full.realized``), labelled by
  source so the two are never confused.
* **n_min progress** ``k/50`` -- ``thresholds.n_min`` from
  ``gate_registration.json`` when it exists, else the template's.
* **KILLED** -- when the registry's current status for the family is ``HALT``,
  or a gate verdict file for the genome reads ``FAIL`` (not refused).

A shadow run books nothing, so its row reads ``shadow 0/50`` with the note
*"shadow run: 0 units (instrumentation, not gate evidence)"* -- the registered
deviation in PRD_STRATEGY_FACTORY Phase F4 says exactly this, and the board must
not describe it as accruing gate evidence.

Inputs: ``--paper-url http://maia.local:8050`` (``/api/genome``, ``/api/journal``,
``/api/closed_trades``; each capped at 500 rows server-side -- the row records
whether a cap was hit) or ``--paper-state exchange_state.json`` +
``--paper-journal trade_journal.jsonl``. Lab-side module: it imports
``src.core.fee_calculator`` (stdlib+numpy) and nothing that reaches an exchange.
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

_THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _THIS_DIR.parents[1]
REPORTS_ROOT = REPO_ROOT / "reports" / "factory"
DEFAULT_URL = "http://maia.local:8050"
HTTP_CAP = 500
DEFAULT_N_MIN = 50
SHADOW_NOTE = "shadow run: 0 units (instrumentation, not gate evidence)"
SETTLED_OUTCOMES = ("yes", "no")


class PaperInputError(RuntimeError):
    """The sandbox record could not be read (HTTP failure, missing file, bad JSON)."""


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def _get_json(url: str, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (LAN GET)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise PaperInputError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PaperInputError(f"{url}: {exc}") from exc


def load_sandbox_http(base_url: str = DEFAULT_URL, *, timeout: float = 15.0) -> Dict[str, Any]:
    """Journal, closed trades, open positions and the genome block over the read-only API."""
    base = base_url.rstrip("/")
    not_obtained: List[str] = []
    genome = _get_json(f"{base}/api/genome", timeout) or {}
    q = urllib.parse.urlencode({"last_n": HTTP_CAP})
    journal = _get_json(f"{base}/api/journal?{q}", timeout)
    journal_rows: List[Dict[str, Any]] = []
    journal_capped = False
    if isinstance(journal, dict) and journal.get("ok"):
        journal_rows = [r for r in journal.get("trades") or [] if isinstance(r, dict)]
        journal_capped = int(journal.get("count") or 0) >= HTTP_CAP
    else:
        not_obtained.append("journal (/api/journal answered no rows)")
    closed = _get_json(f"{base}/api/closed_trades?{q}", timeout)
    closed_trades: List[Dict[str, Any]] = []
    positions: List[Dict[str, Any]] = []
    closed_capped = False
    if isinstance(closed, dict) and closed.get("ok"):
        closed_trades = [r for r in closed.get("closed_trades") or [] if isinstance(r, dict)]
        positions = [r for r in closed.get("positions") or [] if isinstance(r, dict)]
        closed_capped = bool(closed.get("capped"))
    else:
        not_obtained.append(
            "closed_trades (/api/closed_trades absent on this sandbox image: entry fees are "
            "recomputed at the taker rate from the journal)"
        )
    gblock = genome.get("genome") if isinstance(genome, dict) else None
    modes = genome.get("modes") if isinstance(genome, dict) else None
    return {
        "source": {"mode": "http", "base_url": base, "journal_capped": journal_capped, "closed_capped": closed_capped},
        "journal_rows": journal_rows,
        "closed_trades": closed_trades,
        "positions": positions,
        "genome": gblock if isinstance(gblock, dict) else {},
        "genome_mode": (modes or {}).get("genome") if isinstance(modes, dict) else None,
        "not_obtained": not_obtained,
    }


def load_sandbox_files(state_path: Optional[str], journal_path: Optional[str]) -> Dict[str, Any]:
    journal_rows: List[Dict[str, Any]] = []
    not_obtained: List[str] = []
    if journal_path:
        p = Path(journal_path)
        if not p.exists():
            raise PaperInputError(f"journal not found: {p}")
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    journal_rows.append(row)
    else:
        not_obtained.append("journal (no --paper-journal)")
    closed_trades: List[Dict[str, Any]] = []
    positions: List[Dict[str, Any]] = []
    if state_path:
        p = Path(state_path)
        if not p.exists():
            raise PaperInputError(f"exchange state not found: {p}")
        try:
            with open(p, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except ValueError as exc:
            raise PaperInputError(f"{p}: not valid JSON ({exc})") from exc
        if isinstance(state, dict):
            closed_trades = [r for r in state.get("closed_trades") or [] if isinstance(r, dict)]
            positions = [r for r in state.get("positions") or [] if isinstance(r, dict)]
    else:
        not_obtained.append("closed_trades (no --paper-state: entry fees recomputed at the taker rate)")
    return {
        "source": {"mode": "files", "state": state_path, "journal": journal_path},
        "journal_rows": journal_rows,
        "closed_trades": closed_trades,
        "positions": positions,
        "genome": {},
        "genome_mode": None,
        "not_obtained": not_obtained,
    }


# ---------------------------------------------------------------------------
# settled fills for one strategy
# ---------------------------------------------------------------------------
def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    s = str(value)
    return s or None


def _join_key(row: Mapping[str, Any]) -> Tuple[str, Optional[str], str]:
    return (
        str(row.get("symbol") or ""),
        _iso(row.get("entry_time")) or _iso(row.get("open_time")),
        str(row.get("strategy_name") or row.get("strategy") or ""),
    )


def is_settled(row: Mapping[str, Any]) -> bool:
    """The gate's admission: closed by settlement with a recorded binary outcome."""
    if str(row.get("close_reason") or "").upper() != "EXPIRATION":
        return False
    try:
        exit_price = float(row.get("exit_price"))
    except (TypeError, ValueError):
        return False
    if exit_price not in (0.0, 1.0):
        return False
    return str(row.get("settlement_outcome") or "").lower() in SETTLED_OUTCOMES


def taker_fee_for(symbol: str, entry_price: float, quantity: float) -> float:
    """Fee the sandbox books for a taker fill at this price/qty (``src.core.fee_calculator``)."""
    from src.core.fee_calculator import compute_fee, fee_type_for_symbol

    return float(
        compute_fee(float(entry_price), int(round(quantity)), is_maker=False,
                    series_fee_type=fee_type_for_symbol(symbol)).fee
    )


def settled_fills(
    journal_rows: Iterable[Mapping[str, Any]],
    closed_trades: Iterable[Mapping[str, Any]],
    *,
    strategy_name: str,
    market_family: str = "KXHIGH",
) -> List[Dict[str, Any]]:
    """Settled fills of ``strategy_name`` with ``entry_fee`` from the ledger (else recomputed)."""
    ledger: Dict[Tuple[str, Optional[str], str], Mapping[str, Any]] = {}
    for t in closed_trades:
        ledger[_join_key(t)] = t
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in journal_rows:
        if str(row.get("strategy_name") or "") != strategy_name:
            continue
        symbol = str(row.get("symbol") or "")
        if market_family and not symbol.startswith(market_family):
            continue
        if not is_settled(row):
            continue
        key = _join_key(row)
        if key in seen:
            continue
        seen.add(key)
        try:
            entry_price = float(row.get("entry_price"))
            quantity = float(row.get("quantity"))
            pnl = float(row.get("pnl"))
        except (TypeError, ValueError):
            continue
        led = ledger.get(key)
        fee_source = "closed_trades"
        entry_fee: Optional[float] = None
        if led is not None and led.get("entry_fee") is not None:
            try:
                entry_fee = float(led.get("entry_fee"))
            except (TypeError, ValueError):
                entry_fee = None
        if entry_fee is None:
            entry_fee = taker_fee_for(symbol, entry_price, quantity)
            fee_source = "recomputed_taker"
        target_date = row.get("target_date")
        out.append({
            "symbol": symbol,
            "target_date": str(target_date) if target_date else None,
            "entry_time": key[1],
            "exit_time": _iso(row.get("exit_time")) or _iso(row.get("close_time")),
            "entry_price": entry_price,
            "quantity": quantity,
            "pnl": pnl,
            "entry_fee": entry_fee,
            "fee_source": fee_source,
            "net_pnl": pnl - entry_fee,
        })
    return out


def sandbox_summary(fills: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    dates = sorted({str(f["target_date"]) for f in fills if f.get("target_date")})
    undated = sum(1 for f in fills if not f.get("target_date"))
    contracts = math.fsum(float(f["quantity"]) for f in fills)
    net = math.fsum(float(f["net_pnl"]) for f in fills)
    return {
        "settled_trades": len(fills),
        "settled_target_dates": len(dates),
        "target_dates": dates,
        "fills_without_target_date": undated,
        "contracts": contracts,
        "net_pnl": net,
        "c_per_contract": (net / contracts) if contracts > 0 else None,
        "fee_sources": sorted({str(f["fee_source"]) for f in fills}),
    }


# ---------------------------------------------------------------------------
# the factory's prediction for the genome
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _seed_genome_id(seed: Mapping[str, Any]) -> Optional[str]:
    from src.factory.promoted import canonical_json, genome_id_for

    g = seed.get("genome")
    if isinstance(g, str):
        return genome_id_for(g)
    if isinstance(g, dict):
        return genome_id_for(canonical_json(g))
    return None


def prediction_for(
    genome_id: str,
    *,
    family_summary: Optional[Mapping[str, Any]] = None,
    gen0_summary: Optional[Mapping[str, Any]] = None,
    spec_doc: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``{c_per_contract, lo, hi, dates, trades, source}`` for ``genome_id``; source ``None`` when unknown."""
    empty = {"c_per_contract": None, "lo": None, "hi": None, "dates": None, "trades": None, "source": None}
    picks = (family_summary or {}).get("picks") or {}
    for camp, pick in picks.items():
        if isinstance(pick, dict) and str(pick.get("genome_id") or "") == genome_id:
            pooled = (family_summary or {}).get("pooled_oos") or {}
            return {
                "c_per_contract": pooled.get("mean"),
                "lo": pooled.get("boot_lo"),
                "hi": pooled.get("boot_hi"),
                "dates": pooled.get("n_dates"),
                "trades": pooled.get("trades"),
                "source": f"pooled OOS, pick {camp} of {(family_summary or {}).get('run_id', '?')}",
            }
    seeds = (gen0_summary or {}).get("seeds") or {}
    for name, seed in seeds.items():
        if not isinstance(seed, dict):
            continue
        sid = _seed_genome_id(seed)
        if sid is None:
            sid = _seed_genome_id_by_name(name)
        if sid == genome_id:
            row = seed.get("search_full") or {}
            return {
                "c_per_contract": row.get("realized"),
                "lo": row.get("boot_lo"),
                "hi": row.get("boot_hi"),
                "dates": row.get("dates"),
                "trades": row.get("trades"),
                "source": f"search frame (date-clustered), seed {name} of {(gen0_summary or {}).get('run_id', '?')}",
            }
    return empty


def _seed_genome_id_by_name(name: str) -> Optional[str]:
    """``genome_id`` of ``src.factory.genome.SEEDS[name]`` (numpy-only import); ``None`` if unknown."""
    try:
        from src.factory import genome as G
        from src.factory.promoted import genome_id_for, genome_json_for
    except Exception:  # pragma: no cover - numpy absent
        return None
    seed = G.SEEDS.get(name)
    return genome_id_for(genome_json_for(seed)) if seed is not None else None


# ---------------------------------------------------------------------------
# KILLED marker
# ---------------------------------------------------------------------------
def gate_verdict_path(genome_id: str, reports_root: Path = REPORTS_ROOT) -> Path:
    return Path(reports_root) / f"gate_{genome_id}.json"


def killed_reason(
    registry_status: Optional[str],
    gate_verdict: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """``"HALT"`` when the registry says so, ``"GATE_FAIL"`` for a scored-and-failed gate, else ``None``."""
    if str(registry_status or "").upper() == "HALT":
        return "HALT"
    if isinstance(gate_verdict, Mapping):
        if str(gate_verdict.get("verdict") or "").upper() == "FAIL" and not gate_verdict.get("refused"):
            return "GATE_FAIL"
    return None


# ---------------------------------------------------------------------------
# the row
# ---------------------------------------------------------------------------
def n_min_from_registration(reg_path: Optional[Path], template_path: Optional[Path]) -> int:
    for p in (reg_path, template_path):
        if p is None:
            continue
        doc = _load_json(Path(p))
        if doc and isinstance(doc.get("thresholds"), dict) and doc["thresholds"].get("n_min") is not None:
            try:
                return int(doc["thresholds"]["n_min"])
            except (TypeError, ValueError):
                continue
    return DEFAULT_N_MIN


def build_paper_row(
    *,
    genome_id: str,
    family: Optional[str],
    mode: Optional[str],
    sandbox: Mapping[str, Any],
    prediction: Mapping[str, Any],
    registry_status: Optional[str],
    n_min: int = DEFAULT_N_MIN,
    gate_verdict: Optional[Mapping[str, Any]] = None,
    strategy_name: Optional[str] = None,
    not_obtained: Sequence[str] = (),
) -> Dict[str, Any]:
    """The ``paper`` dict ``report.render_board`` renders (all numbers already computed)."""
    k = int(sandbox.get("settled_target_dates") or 0)
    n = int(sandbox.get("settled_trades") or 0)
    mode_s = str(mode or "?").lower()
    killed = killed_reason(registry_status, gate_verdict)
    notes: List[str] = []
    if mode_s == "shadow" and n == 0:
        notes.append(SHADOW_NOTE)
    elif n == 0:
        notes.append(f"{mode_s} run: 0 settled fills yet")
    if sandbox.get("fills_without_target_date"):
        notes.append(f"{sandbox['fills_without_target_date']} settled fill(s) carry no target_date and are not units")
    if sandbox.get("fee_sources") and "recomputed_taker" in sandbox["fee_sources"]:
        notes.append("entry fees recomputed at the taker rate for fills the ledger no longer holds")
    for item in not_obtained:
        notes.append(f"not obtained: {item}")
    if killed:
        notes.insert(0, f"family KILLED ({killed}); nothing under src/factory/ may reference a live-capital flag")
    return {
        "status": f"KILLED:{killed}" if killed else f"{mode_s} {k}/{n_min}",
        "killed": killed,
        "mode": mode_s,
        "family": family,
        "genome_id": genome_id,
        "strategy_name": strategy_name or f"Genome {genome_id[:8]}",
        "n_min": int(n_min),
        "settled_target_dates": k,
        "settled_trades": n,
        "contracts": sandbox.get("contracts"),
        "net_pnl": sandbox.get("net_pnl"),
        "sandbox_c_per_contract": sandbox.get("c_per_contract"),
        "prediction_c_per_contract": prediction.get("c_per_contract"),
        "prediction_lo": prediction.get("lo"),
        "prediction_hi": prediction.get("hi"),
        "prediction_source": prediction.get("source"),
        "note": "; ".join(notes) if notes else "sandbox closed_trades vs factory prediction",
    }


def paper_row_from_inputs(
    inputs: Mapping[str, Any],
    *,
    genome_id: str,
    family: Optional[str],
    mode: Optional[str],
    registry_status: Optional[str],
    family_summary: Optional[Mapping[str, Any]] = None,
    gen0_summary: Optional[Mapping[str, Any]] = None,
    spec_doc: Optional[Mapping[str, Any]] = None,
    n_min: int = DEFAULT_N_MIN,
    gate_verdict: Optional[Mapping[str, Any]] = None,
    strategy_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Glue: settled fills -> summary -> prediction -> row."""
    name = strategy_name or f"Genome {genome_id[:8]}"
    fills = settled_fills(inputs.get("journal_rows") or [], inputs.get("closed_trades") or [], strategy_name=name)
    summary = sandbox_summary(fills)
    pred = prediction_for(genome_id, family_summary=family_summary, gen0_summary=gen0_summary, spec_doc=spec_doc)
    row = build_paper_row(
        genome_id=genome_id, family=family, mode=mode, sandbox=summary, prediction=pred,
        registry_status=registry_status, n_min=n_min, gate_verdict=gate_verdict, strategy_name=name,
        not_obtained=list(inputs.get("not_obtained") or []),
    )
    row["fills"] = fills
    return row


__all__ = [
    "DEFAULT_N_MIN",
    "DEFAULT_URL",
    "SHADOW_NOTE",
    "PaperInputError",
    "build_paper_row",
    "gate_verdict_path",
    "is_settled",
    "killed_reason",
    "load_sandbox_files",
    "load_sandbox_http",
    "n_min_from_registration",
    "paper_row_from_inputs",
    "prediction_for",
    "sandbox_summary",
    "settled_fills",
    "taker_fee_for",
]
