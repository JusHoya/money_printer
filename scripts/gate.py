#!/usr/bin/env python3
"""gate.py -- the FR-5.2 promotion gate for a paper-traded factory genome.

PRD.md FR-5.2 (pre-registered, immutable during the run): a promoted strategy
passes the gate only when ALL of

    1. n_units  >= n_min          settled trades, grouped by the independence unit
    2. p        <  alpha          exact one-sided binomial p of the unit win count
                                  against the fee-adjusted breakeven at actual entry
    3. net PnL  >  0              settlement-true, from closed_trades / the journal,
                                  never from equity or balance
    4. spec hash unchanged        the promoted spec at gate time is the one registered
                                  in gate_registration.json before the first trade

hold. PRD_STRATEGY_FACTORY.md FR-F3.4 fixes the unit as ``target_date`` and the
test as the exact binomial with ``math.comb`` (no scipy). FACTORY_ARCHITECTURE
section 9 items 7-8 and FACTORY_ROADMAP section F3 item 6 are the design record.

INDEPENDENCE UNIT -- one trial per ``target_date``
--------------------------------------------------
Every bracket on a city-day ladder settles against the same CLI daily high, so
two fills that share a settlement day are one bet on one number, not two
trials. The gate therefore aggregates fills per ``target_date`` (the settlement
station's LOCAL calendar day, ``TradeOutcome.target_date`` /
``weather_settlement.settlement_date_for``) and scores each date as ONE unit:

    unit net PnL  = sum over the date's settled fills of (pnl - entry_fee)
    unit is a WIN = unit net PnL > 0            (exactly 0 is a loss)

Four cities sharing a date collapse into one unit as well: their highs are not
independent either (synoptic weather), and the frame the genome was searched on
grouped by target_date for the same reason (registry ``grouping_unit``).

BREAKEVEN -- derived, per fill
------------------------------
A binary bought at price ``p`` with entry fee ``f`` per contract and held to
settlement (payout 1, Kalshi charges no settlement fee) pays

    win:   +1 - p - f          loss:   -(p + f)

so the win-rate ``q*`` at which the expected PnL is zero solves

    q* (1 - p - f) - (1 - q*) (p + f) = 0   ->   q* = p + f

``f`` is ``entry_fee / quantity`` from ``closed_trades`` -- the taker fee the
sandbox actually booked (``fee_calculator.taker_fee`` at actual quantity,
ceil-to-cent on the order total, so ``f`` depends on size). For a journal row
the state file no longer holds (``closed_trades`` is cleared on a cycle reset;
the journal is append-only) the taker fee is recomputed from
``(entry_price, quantity)`` with the same function and the row is marked
``fee_source = "recomputed_taker"``.

A unit's breakeven is the contract-weighted mean of its fills' ``q*``; the
pooled null win-rate is the plain mean of the unit breakevens:

    q_bar = mean over units of q*_unit

With one fill per unit this null is exact. With several fills per unit the
event "unit net PnL > 0" is not exactly Bernoulli(q*_unit) -- it is the event
that the fills' summed PnL is positive -- so ``q_bar`` is an approximation
there, stated as such in the verdict, and the per-fill binomial is printed as a
secondary, NON-gating line for comparison.

THE TEST -- exact binomial upper tail
-------------------------------------
    p = P[X >= k | n, q_bar] = sum_{i=k}^{n} C(n, i) q_bar^i (1 - q_bar)^(n - i)

with ``math.comb`` and ``math.fsum``; no scipy, no normal approximation. ``k`` is
the number of winning units, ``n`` the number of settled units.

REFUSAL
-------
Below ``n_min`` units the gate does not compute a p at all (exit 3, verdict
FAIL, ``refused: true``): an underpowered p printed next to a PASS/FAIL banner
is how a 72-hour streak turned into a promotion in the crypto era.

USAGE
-----
    python scripts/gate.py --journal data/trade_journal.jsonl \
        --state data/exchange_state.json \
        --registration configs/factory/gate_registration.json \
        --out reports/factory/gate_<genome_id>.json

Exit codes: 0 PASS, 1 FAIL, 2 usage / input error, 3 refused (n_units < n_min).
The verdict JSON is written with ``sort_keys=True, indent=2`` and carries no
timestamps (the report is a function of its inputs; the commit dates it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.fee_calculator import compute_fee, fee_type_for_symbol  # noqa: E402
from src.core.weather_settlement import (  # noqa: E402
    settlement_date_for,
    settlement_timezone_for,
)
from src.factory.report import write_json  # noqa: E402
from src.ml.trade_journal import target_date_for_position  # noqa: E402

SCHEMA_VERSION = 1
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

#: exit_price must sit on the settlement grid {0, 1} within this.
_SETTLEMENT_TOL = 1e-9

FORMULAS: Dict[str, str] = {
    "breakeven_per_fill": (
        "q* = entry_price + entry_fee_per_contract; from "
        "q*(1 - p - f) - (1 - q*)(p + f) = 0 for a binary bought at p with entry fee f "
        "per contract, held to settlement (payout 1, no settlement fee)"
    ),
    "unit_breakeven": "contract-weighted mean of the unit's fills' q*",
    "pooled_null": "q_bar = mean over settled units of the unit breakeven",
    "unit_win": "unit net PnL = sum(pnl - entry_fee) over the unit's fills; win iff > 0",
    "p_value": (
        "exact binomial upper tail P[X >= k | n, q_bar] = "
        "sum_{i=k}^{n} C(n, i) q_bar^i (1 - q_bar)^(n - i), math.comb + math.fsum"
    ),
    "net_pnl": (
        "sum over settled fills of (pnl - entry_fee); pnl is closed_trades' pnl "
        "(net of the exit fee, which is 0 at settlement) and entry_fee the booked "
        "taker fee; never equity or balance"
    ),
}


class GateError(RuntimeError):
    """Malformed inputs. The gate exits 2 rather than guessing."""


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------
def binomial_upper_tail(n: int, k: int, q: float) -> float:
    """``P[X >= k]`` for ``X ~ Binomial(n, q)`` -- exact, ``math.comb`` + ``math.fsum``."""
    if n < 0 or k < 0:
        raise GateError(f"binomial_upper_tail: n={n}, k={k} must be non-negative")
    if k > n:
        return 0.0
    if k == 0:
        return 1.0
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    terms = [
        math.comb(n, i) * (q ** i) * ((1.0 - q) ** (n - i)) for i in range(k, n + 1)
    ]
    return min(1.0, max(0.0, math.fsum(terms)))


def breakeven_win_rate(entry_price: float, fee_per_contract: float) -> float:
    """``q* = p + f`` (see the module docstring for the derivation)."""
    return float(entry_price) + float(fee_per_contract)


def nearest_cent_taker_fee(symbol: str, entry_price: float, quantity: float) -> float:
    """The taker entry fee the sandbox books for this fill (order total, ceil to cent)."""
    return compute_fee(
        float(entry_price),
        int(round(quantity)),
        is_maker=False,
        series_fee_type=fee_type_for_symbol(symbol),
    ).fee


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def _repo_relative(path: str) -> str:
    """Repo-relative POSIX path when possible; the given path otherwise (other drive)."""
    if not os.path.isabs(path):
        return path
    try:
        return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
    except ValueError:  # Windows: different drive letters
        return path.replace(os.sep, "/")


def sha256_file(path: str) -> str:
    """sha256 of a file with CRLF normalised to LF (same rule as src.factory.fees)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def load_journal(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        raise GateError(f"journal not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GateError(f"{path}:{n}: malformed JSON ({exc})") from exc
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def load_closed_trades(path: Optional[str]) -> List[Dict[str, Any]]:
    if path is None:
        return []
    if not os.path.exists(path):
        raise GateError(f"exchange state not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    trades = state.get("closed_trades") if isinstance(state, dict) else None
    return [t for t in (trades or []) if isinstance(t, dict)]


def load_registration(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise GateError(f"registration not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        reg = json.load(fh)
    if not isinstance(reg, dict):
        raise GateError(f"{path}: registration must be a JSON object")
    if int(reg.get("schema_version", -1)) != SCHEMA_VERSION:
        raise GateError(
            f"{path}: schema_version {reg.get('schema_version')!r} != {SCHEMA_VERSION}"
        )
    for key in ("strategy_name", "spec_hash", "thresholds", "grouping_unit"):
        if key not in reg:
            raise GateError(f"{path}: registration lacks {key!r}")
    if reg["grouping_unit"] != "target_date":
        raise GateError(
            f"{path}: grouping_unit {reg['grouping_unit']!r} is not 'target_date'; "
            "this gate implements exactly the pre-registered unit"
        )
    for key in ("n_min", "alpha"):
        if key not in reg["thresholds"]:
            raise GateError(f"{path}: thresholds lacks {key!r}")
    if "REPLACE_ME" in json.dumps(reg):
        raise GateError(f"{path}: registration still carries REPLACE_ME placeholders")
    return reg


def resolve_spec_hash(spec_path: Optional[str]) -> Tuple[Optional[str], str]:
    """``(observed spec_hash, source)`` for the promoted spec at gate time.

    Prefers ``src.factory.promoted.load_promoted`` (which verifies the spec's
    own hash) when that module exists; else the file's ``spec_hash`` field;
    else the CRLF-normalised sha256 of the file. ``(None, reason)`` when the
    spec cannot be read -- the condition then fails, it is never skipped.
    """
    if not spec_path:
        return None, "no promoted_spec_path in the registration"
    path = spec_path if os.path.isabs(spec_path) else os.path.join(REPO_ROOT, spec_path)
    if not os.path.exists(path):
        return None, f"promoted spec not found: {spec_path}"
    try:
        from src.factory.promoted import load_promoted  # type: ignore

        spec = load_promoted(path)
        observed = getattr(spec, "spec_hash", None)
        if observed is None and isinstance(spec, Mapping):
            observed = spec.get("spec_hash")
        if observed:
            return str(observed), "src.factory.promoted.load_promoted"
    except ImportError:
        pass
    except Exception as exc:  # PromotedSpecError or a corrupt file
        return None, f"load_promoted rejected the spec: {exc}"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and raw.get("spec_hash"):
            return str(raw["spec_hash"]), "spec_hash field of the promoted spec file"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"promoted spec unreadable: {exc}"
    return sha256_file(path), "sha256 of the promoted spec file (CRLF-normalised)"


# ---------------------------------------------------------------------------
# Trade assembly
# ---------------------------------------------------------------------------
def _is_settled(row: Mapping[str, Any]) -> Tuple[bool, str]:
    reason = str(row.get("close_reason") or row.get("reason") or "").upper()
    if "UNRESOLVED" in reason or row.get("settlement_error"):
        return False, "settlement_unresolved"
    if "EXPIRATION" not in reason and "SETTLEMENT" not in reason:
        return False, f"not_settled:{reason or 'NO_REASON'}"
    try:
        exit_price = float(row.get("exit_price"))
    except (TypeError, ValueError):
        return False, "no_exit_price"
    on_grid = abs(exit_price) < _SETTLEMENT_TOL or abs(exit_price - 1.0) < _SETTLEMENT_TOL
    if not on_grid:
        return False, f"exit_price_off_grid:{exit_price}"
    return True, ""


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _join_key(row: Mapping[str, Any]) -> Tuple[str, Optional[str], str]:
    entry = _iso(row.get("entry_time")) or _iso(row.get("open_time"))
    return (
        str(row.get("symbol") or ""),
        entry,
        str(row.get("strategy_name") or ""),
    )


def _target_date(row: Mapping[str, Any]) -> Optional[str]:
    td = row.get("target_date")
    if isinstance(td, str) and td:
        return td
    derived = target_date_for_position(dict(row))
    if derived:
        return derived
    day = settlement_date_for(str(row.get("symbol") or ""))
    return day.isoformat() if day else None


def collect_settled_trades(
    journal_rows: Sequence[Mapping[str, Any]],
    closed_trades: Sequence[Mapping[str, Any]],
    *,
    strategy_name: str,
    market_family: str = "KXHIGH",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Merge journal + closed_trades into one settled-fill list for ``strategy_name``.

    The journal is the durable record (append-only); ``closed_trades`` carries
    ``entry_fee`` and is cleared on a cycle reset. Rows are joined on
    ``(symbol, entry_time, strategy_name)`` -- ``entry_time`` is the exact
    ``open_time.isoformat()`` string in both files.
    """
    excluded: Counter = Counter()
    by_key: Dict[Tuple[str, Optional[str], str], Dict[str, Any]] = {}

    state_by_key: Dict[Tuple[str, Optional[str], str], Mapping[str, Any]] = {}
    for t in closed_trades:
        state_by_key.setdefault(_join_key(t), t)

    def _admit(row: Mapping[str, Any], source: str) -> Optional[Dict[str, Any]]:
        symbol = str(row.get("symbol") or "")
        if str(row.get("strategy_name") or "") != strategy_name:
            excluded["other_strategy"] += 1
            return None
        if not symbol.upper().startswith(market_family.upper()):
            excluded["outside_market_family"] += 1
            return None
        if settlement_timezone_for(symbol) is None:
            excluded["non_weather_symbol"] += 1
            return None
        ok, why = _is_settled(row)
        if not ok:
            excluded[why.split(":", 1)[0]] += 1
            return None
        td = _target_date(row)
        if td is None:
            excluded["no_target_date"] += 1
            return None
        try:
            entry_price = float(row["entry_price"])
            quantity = float(row["quantity"])
            pnl = float(row["pnl"])
        except (KeyError, TypeError, ValueError):
            excluded["missing_numeric_field"] += 1
            return None
        if quantity <= 0:
            excluded["non_positive_quantity"] += 1
            return None
        return {
            "symbol": symbol,
            "strategy_name": strategy_name,
            "target_date": td,
            "entry_time": _iso(row.get("entry_time")) or _iso(row.get("open_time")),
            "exit_time": _iso(row.get("exit_time")) or _iso(row.get("close_time")),
            "contract_side": str(row.get("contract_side") or "YES"),
            "entry_price": entry_price,
            "quantity": quantity,
            "exit_price": float(row.get("exit_price")),
            "pnl": pnl,
            "source": source,
        }

    for row in journal_rows:
        key = _join_key(row)
        if key in by_key:
            excluded["duplicate_journal_row"] += 1
            continue
        trade = _admit(row, "journal")
        if trade is None:
            continue
        st = state_by_key.get(key)
        if st is not None and st.get("entry_fee") is not None:
            trade["entry_fee"] = float(st["entry_fee"])
            trade["fee_source"] = "closed_trades.entry_fee"
            trade["source"] = "journal+closed_trades"
            if abs(float(st.get("pnl", trade["pnl"])) - trade["pnl"]) > 1e-6:
                trade["pnl_journal"] = trade["pnl"]
                trade["pnl"] = float(st["pnl"])
                trade["pnl_source"] = "closed_trades (journal disagreed)"
        else:
            trade["entry_fee"] = nearest_cent_taker_fee(
                trade["symbol"], trade["entry_price"], trade["quantity"]
            )
            trade["fee_source"] = "recomputed_taker"
        by_key[key] = trade

    for t in closed_trades:
        key = _join_key(t)
        if key in by_key:
            continue
        trade = _admit(t, "closed_trades_only")
        if trade is None:
            continue
        if t.get("entry_fee") is not None:
            trade["entry_fee"] = float(t["entry_fee"])
            trade["fee_source"] = "closed_trades.entry_fee"
        else:
            trade["entry_fee"] = nearest_cent_taker_fee(
                trade["symbol"], trade["entry_price"], trade["quantity"]
            )
            trade["fee_source"] = "recomputed_taker"
        by_key[key] = trade

    trades: List[Dict[str, Any]] = []
    for trade in by_key.values():
        fee_pc = trade["entry_fee"] / trade["quantity"]
        trade["fee_per_contract"] = fee_pc
        trade["net_pnl"] = trade["pnl"] - trade["entry_fee"]
        trade["won"] = trade["net_pnl"] > 0.0
        trade["q_star"] = breakeven_win_rate(trade["entry_price"], fee_pc)
        trades.append(trade)
    trades.sort(key=lambda t: (t["target_date"], t["entry_time"] or "", t["symbol"]))

    counts = {
        "journal_rows": len(journal_rows),
        "closed_trades": len(closed_trades),
        "settled_fills": len(trades),
        "fills_by_source": dict(Counter(t["source"] for t in trades)),
        "fills_by_fee_source": dict(Counter(t["fee_source"] for t in trades)),
        "excluded": dict(sorted(excluded.items())),
    }
    return trades, counts


def group_units(trades: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One unit per ``target_date``: summed net PnL, contract-weighted breakeven."""
    buckets: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for t in trades:
        buckets[t["target_date"]].append(t)
    units: List[Dict[str, Any]] = []
    for td in sorted(buckets):
        fills = buckets[td]
        qty = sum(f["quantity"] for f in fills)
        net = math.fsum(f["net_pnl"] for f in fills)
        q_star = math.fsum(f["q_star"] * f["quantity"] for f in fills) / qty
        units.append(
            {
                "target_date": td,
                "n_fills": len(fills),
                "symbols": sorted(f["symbol"] for f in fills),
                "quantity": qty,
                "net_pnl": net,
                "q_star": q_star,
                "won": net > 0.0,
            }
        )
    return units


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def _first_trade_after(trades: Sequence[Mapping[str, Any]], cutoff_iso: Optional[str]):
    """``(ok, note)`` for the optional registration_commit_utc condition."""
    if not cutoff_iso:
        return None, (
            "UNVERIFIED: registration_commit_utc is null; verify by hand that the "
            "commit adding gate_registration.json predates the first journal row"
        )
    try:
        cutoff = datetime.fromisoformat(str(cutoff_iso).replace("Z", "+00:00"))
    except ValueError:
        return False, f"registration_commit_utc {cutoff_iso!r} is not ISO-8601"
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    earliest: Optional[datetime] = None
    for t in trades:
        et = t.get("entry_time")
        if not et:
            continue
        try:
            dt = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # sandbox clock is UTC (deploy/pi)
        if earliest is None or dt < earliest:
            earliest = dt
    if earliest is None:
        return True, "no settled fills carry an entry_time"
    ok = earliest >= cutoff
    return ok, f"first settled fill {earliest.isoformat()} vs registration {cutoff.isoformat()}"


def evaluate(
    trades: Sequence[Mapping[str, Any]],
    registration: Mapping[str, Any],
    *,
    observed_spec_hash: Optional[str],
    spec_hash_source: str,
) -> Dict[str, Any]:
    """The verdict dict (timestamp-free). Pure: no I/O."""
    thresholds = registration["thresholds"]
    n_min = int(thresholds["n_min"])
    alpha = float(thresholds["alpha"])
    net_gt = float(thresholds.get("net_pnl_gt", 0.0))

    units = group_units(trades)
    n_units = len(units)
    k_units = sum(1 for u in units if u["won"])
    refused = n_units < n_min

    net_pnl = math.fsum(t["net_pnl"] for t in trades)
    gross = math.fsum(t["pnl"] for t in trades)
    fees = math.fsum(t["entry_fee"] for t in trades)

    multi = sum(1 for u in units if u["n_fills"] > 1)
    if n_units:
        q_bar_units = math.fsum(u["q_star"] for u in units) / n_units
        p_units = None if refused else binomial_upper_tail(n_units, k_units, q_bar_units)
    else:
        q_bar_units = None
        p_units = None

    n_fills = len(trades)
    k_fills = sum(1 for t in trades if t["won"])
    q_bar_fills = math.fsum(t["q_star"] for t in trades) / n_fills if n_fills else None
    p_fills = (
        binomial_upper_tail(n_fills, k_fills, q_bar_fills)
        if (n_fills and not refused)
        else None
    )

    registered_hash = str(registration["spec_hash"])
    hash_ok = observed_spec_hash is not None and observed_spec_hash == registered_hash
    rbft_ok, rbft_note = _first_trade_after(
        trades, registration.get("registration_commit_utc")
    )

    conditions: Dict[str, Dict[str, Any]] = {
        "n_units_ge_n_min": {
            "ok": n_units >= n_min,
            "observed": n_units,
            "required": n_min,
            "unit": "target_date",
        },
        "p_lt_alpha": {
            "ok": (p_units is not None and p_units < alpha),
            "observed": p_units,
            "required_lt": alpha,
            "note": "not computed (refused)" if refused else "exact binomial upper tail",
        },
        "net_pnl_gt_0": {
            "ok": net_pnl > net_gt,
            "observed": net_pnl,
            "required_gt": net_gt,
            "source": FORMULAS["net_pnl"],
        },
        "spec_hash_unchanged": {
            "ok": hash_ok,
            "registered": registered_hash,
            "observed": observed_spec_hash,
            "source": spec_hash_source,
        },
        "registered_before_first_trade": {
            "ok": rbft_ok,
            "gating": rbft_ok is not None,
            "note": rbft_note,
        },
    }
    gating = [
        conditions["n_units_ge_n_min"]["ok"],
        conditions["p_lt_alpha"]["ok"],
        conditions["net_pnl_gt_0"]["ok"],
        conditions["spec_hash_unchanged"]["ok"],
    ]
    if rbft_ok is not None:
        gating.append(bool(rbft_ok))
    verdict = "PASS" if (not refused and all(gating)) else "FAIL"

    return {
        "verdict": verdict,
        "refused": refused,
        "refusal": (
            f"n_units={n_units} < n_min={n_min}: the binomial test is not run on an "
            "underpowered sample"
            if refused
            else None
        ),
        "conditions": conditions,
        "units": {
            "n": n_units,
            "k_wins": k_units,
            "null_win_rate_q_bar": q_bar_units,
            "p_upper_tail": p_units,
            "rule": FORMULAS["unit_win"],
            "units_with_multiple_fills": multi,
            "null_note": (
                "exact: one fill per unit"
                if multi == 0
                else (
                    f"{multi} unit(s) hold several fills; q_bar is the mean of the "
                    "contract-weighted unit breakevens, an approximation of the null "
                    "for 'unit net PnL > 0' -- see per_fill_secondary"
                )
            ),
        },
        "per_fill_secondary": {
            "gating": False,
            "n": n_fills,
            "k_wins": k_fills,
            "null_win_rate_q_bar": q_bar_fills,
            "p_upper_tail": p_fills,
            "note": "fills treated as independent trials; reported for comparison only",
        },
        "pnl": {
            "net": net_pnl,
            "pnl_after_exit_fee": gross,
            "entry_fees": fees,
            "source": FORMULAS["net_pnl"],
        },
        "formulas": dict(FORMULAS),
        "unit_table": units,
        "fills": [dict(t) for t in trades],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_gate(
    *,
    journal_path: str,
    state_path: Optional[str],
    registration_path: str,
    out_path: Optional[str],
    strategy_override: Optional[str] = None,
    promoted_override: Optional[str] = None,
) -> Dict[str, Any]:
    registration = load_registration(registration_path)
    strategy_name = strategy_override or str(registration["strategy_name"])
    journal_rows = load_journal(journal_path)
    closed = load_closed_trades(state_path)
    trades, counts = collect_settled_trades(
        journal_rows,
        closed,
        strategy_name=strategy_name,
        market_family=str(registration.get("market_family", "KXHIGH")),
    )
    spec_path = promoted_override or registration.get("promoted_spec_path")
    observed_hash, hash_source = resolve_spec_hash(spec_path)
    verdict = evaluate(
        trades,
        registration,
        observed_spec_hash=observed_hash,
        spec_hash_source=hash_source,
    )
    verdict["registration"] = {
        "path": _repo_relative(registration_path),
        "genome_id": registration.get("genome_id"),
        "strategy_name": strategy_name,
        "grouping_unit": registration["grouping_unit"],
        "thresholds": dict(registration["thresholds"]),
        "spec_hash": registration["spec_hash"],
        "adverse_fill": registration.get("adverse_fill"),
        "fee_type": registration.get("fee_type"),
    }
    verdict["inputs"] = {
        "journal": journal_path,
        "journal_sha256": sha256_file(journal_path),
        "state": state_path,
        "state_sha256": sha256_file(state_path) if state_path else None,
        "promoted_spec_path": spec_path,
    }
    verdict["counts"] = counts
    if out_path:
        write_json(Path(out_path), verdict)
    return verdict


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--journal", default="data/trade_journal.jsonl")
    ap.add_argument("--state", default=None, help="exchange_state.json (closed_trades)")
    ap.add_argument("--registration", required=True, help="gate_registration.json")
    ap.add_argument("--out", default=None, help="verdict JSON path")
    ap.add_argument("--strategy", default=None, help="override registration.strategy_name")
    ap.add_argument(
        "--promoted", default=None, help="override registration.promoted_spec_path"
    )
    ap.add_argument("--quiet", action="store_true", help="print only the verdict line")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        verdict = run_gate(
            journal_path=args.journal,
            state_path=args.state,
            registration_path=args.registration,
            out_path=args.out,
            strategy_override=args.strategy,
            promoted_override=args.promoted,
        )
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.quiet:
        u = verdict["units"]
        print(
            f"{verdict['verdict']} n_units={u['n']} k={u['k_wins']} "
            f"q_bar={u['null_win_rate_q_bar']} p={u['p_upper_tail']} "
            f"net_pnl={verdict['pnl']['net']:+.2f}"
        )
    else:
        printable = {k: v for k, v in verdict.items() if k != "fills"}
        print(json.dumps(printable, sort_keys=True, indent=2, default=str))
    if verdict["refused"]:
        return EXIT_REFUSED
    return EXIT_PASS if verdict["verdict"] == "PASS" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
