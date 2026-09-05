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

THE NULL -- exact, per unit (2026-09-05, red team B defect 1)
-------------------------------------------------------------
Under the null every fill ``i`` of a unit wins independently with its OWN
breakeven probability ``q*_i = p_i + f_i``. The unit's win probability is then

    w_u = P[ sum_i (won_i - p_i - f_i) * qty_i > 0 ]

computed EXACTLY by enumerating the 2^m fill outcomes of the unit (``m`` fills;
the gate refuses a unit with more than ``MAX_FILLS_PER_UNIT`` fills rather than
approximate). With one fill per unit ``w_u = q*_i`` and the test below reduces to
the plain binomial. The earlier pooled ``q_bar`` (mean of contract-weighted unit
breakevens) is kept as ``p_pooled_qbar_secondary`` -- labelled an approximation,
NON-gating -- because with several fills per date it is neither exact nor
reliably conservative (identical fills: a split date is a WIN, so the true
``w_u = 1 - (1 - q*)^2`` is far above ``q*``; extreme price pairs: only both-win
wins, so ``w_u = q*_1 q*_2`` is far below).

THE TEST -- exact Poisson-binomial upper tail
---------------------------------------------
    p = P[K >= k],  K = sum over units of Bernoulli(w_u)

by dynamic programming over units in exact rational arithmetic
(``fractions.Fraction``; the float is derived from it and ``p_exact_str`` is
kept in the verdict). No scipy, no normal approximation. ``k`` is the number of
winning units, ``n`` the number of settled units.

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

Exit codes: 0 PASS, 1 FAIL, 2 usage / input error, 3 refused (n_units < n_min,
or stale NO-side settlement rows present -- see below).

STALE NO-SIDE ROWS (engine fix 724d93c, 2026-09-04)
---------------------------------------------------
Before that commit the sandbox booked the YES-leg payoff against NO entries, so
every settled NO paper trade carried a sign-flipped ``pnl``. The gate refuses
(exit 3) when any admitted NO settlement -- from the journal or from
``closed_trades`` -- still carries the buggy numbers (``exit_price`` equals the
YES payoff of its recorded ``settlement_outcome`` and ``pnl`` equals the old
formula) and does not carry the ``repaired_no_side_settlement`` marker written
by ``scripts/repair_no_settlement_pnl.py``. A NO settlement without a recorded
outcome cannot be verified either way and is excluded (``no_side_outcome_unverifiable``).

FEE TRUST (red team B defect 4)
-------------------------------
Under a ``fee_type: taker`` registration the fee charged to a fill is
``max(booked entry_fee / qty, taker fee recomputed at the fill's price and the
JOURNAL quantity)``: a maker-booked or zero fee can never lower the breakeven.
Quantity comes from the same row as the price; a journal/state quantity
mismatch is reported under ``warnings``. A fill booked as a maker under a taker
registration fails the gating condition ``fee_type_matches``.

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
from fractions import Fraction
from itertools import product
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
#: a unit with more fills than this is refused (2^m enumeration), never approximated.
MAX_FILLS_PER_UNIT = 16
#: the journal/state marker scripts/repair_no_settlement_pnl.py writes.
REPAIRED_MARKER = "repaired_no_side_settlement"

FORMULAS: Dict[str, str] = {
    "breakeven_per_fill": (
        "q* = entry_price + entry_fee_per_contract; from "
        "q*(1 - p - f) - (1 - q*)(p + f) = 0 for a binary bought at p with entry fee f "
        "per contract, held to settlement (payout 1, no settlement fee)"
    ),
    "unit_null_win_probability": (
        "w_u = P[sum_i (won_i - p_i - f_i) qty_i > 0] with won_i ~ Bernoulli(q*_i) "
        "independent, by exact enumeration of the unit's 2^m fill outcomes"
    ),
    "unit_breakeven": "contract-weighted mean of the unit's fills' q* (diagnostic only)",
    "pooled_null_secondary": "q_bar = mean over settled units of the unit breakeven (approximation, non-gating)",
    "unit_win": "unit net PnL = sum(pnl - entry_fee) over the unit's fills; win iff > 0",
    "p_value": (
        "exact Poisson-binomial upper tail P[K >= k], K = sum over units of Bernoulli(w_u), "
        "by dynamic programming in fractions.Fraction; float derived from the rational"
    ),
    "fee_trust": (
        "taker registration: fee = max(booked entry_fee/qty, taker fee recomputed at the "
        "fill's price and journal quantity)"
    ),
    "net_pnl": (
        "sum over settled fills of (pnl - entry_fee); pnl is closed_trades' pnl "
        "(net of the exit fee, which is 0 at settlement) and entry_fee the booked "
        "taker fee; never equity or balance"
    ),
}


class GateError(RuntimeError):
    """Malformed inputs. The gate exits 2 rather than guessing."""


class GateRefusal(RuntimeError):
    """The record cannot be gated as it stands (stale NO-side rows, oversized unit). Exit 3."""


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


def _clip01(q: Fraction) -> Fraction:
    return Fraction(0) if q < 0 else (Fraction(1) if q > 1 else q)


def unit_null_win_probability(fills: Sequence[Mapping[str, Any]]) -> Fraction:
    """``w_u = P[unit net PnL > 0]`` under the null, by exact enumeration.

    Each fill ``i`` (``entry_price`` p, ``fee_per_contract`` f, ``quantity`` q)
    wins independently with ``q*_i = p + f``; its PnL is ``(1 - p - f) q`` on a
    win and ``-(p + f) q`` on a loss (settlement fee 0). Exact rationals
    throughout so a unit whose PnL is exactly 0 is a loss, never a rounding win.
    """
    m = len(fills)
    if m == 0:
        return Fraction(0)
    if m > MAX_FILLS_PER_UNIT:
        raise GateRefusal(
            f"a unit holds {m} fills > MAX_FILLS_PER_UNIT={MAX_FILLS_PER_UNIT}; the exact "
            "2^m enumeration is refused rather than approximated"
        )
    qs: List[Fraction] = []
    win_pnl: List[Fraction] = []
    loss_pnl: List[Fraction] = []
    for f in fills:
        p = Fraction(float(f["entry_price"]))
        fee = Fraction(float(f["fee_per_contract"]))
        qty = Fraction(float(f["quantity"]))
        qs.append(_clip01(p + fee))
        win_pnl.append((1 - p - fee) * qty)
        loss_pnl.append(-(p + fee) * qty)
    total = Fraction(0)
    for outcome in product((False, True), repeat=m):
        prob = Fraction(1)
        pnl = Fraction(0)
        for i, won in enumerate(outcome):
            prob *= qs[i] if won else (1 - qs[i])
            pnl += win_pnl[i] if won else loss_pnl[i]
        if pnl > 0:
            total += prob
    return total


def poisson_binomial_upper_tail(win_probs: Sequence[Fraction], k: int) -> Fraction:
    """``P[K >= k]`` for ``K = sum_u Bernoulli(w_u)`` -- exact DP over units."""
    n = len(win_probs)
    if k <= 0:
        return Fraction(1)
    if k > n:
        return Fraction(0)
    dist: List[Fraction] = [Fraction(1)]
    for w in win_probs:
        w = Fraction(w)
        nxt = [Fraction(0)] * (len(dist) + 1)
        for j, pj in enumerate(dist):
            if pj == 0:
                continue
            nxt[j] += pj * (1 - w)
            nxt[j + 1] += pj * w
        dist = nxt
    return sum(dist[k:], Fraction(0))


def _fraction_str(x: Optional[Fraction]) -> Optional[str]:
    return None if x is None else f"{x.numerator}/{x.denominator}"


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
        from src.factory.promoted import PromotedSpecError, load_promoted  # type: ignore
    except ImportError:
        PromotedSpecError = None  # type: ignore[assignment]
        load_promoted = None  # type: ignore[assignment]
    if load_promoted is not None:
        try:
            spec = load_promoted(path)
            observed = getattr(spec, "spec_hash", None)
            if observed is None and isinstance(spec, Mapping):
                observed = spec.get("spec_hash")
            if observed:
                return str(observed), "src.factory.promoted.load_promoted (content hash verified)"
        except PromotedSpecError as exc:  # type: ignore[misc]
            msg = str(exc)
            # A spec whose content no longer hashes to its own spec_hash (or a
            # genome_id that does not match its genome) is a CHANGED spec: the
            # condition fails outright, never falls back to the raw file.
            if "does not verify" in msg or "genome_id" in msg:
                return None, f"load_promoted rejected the spec: {msg}"
            # Otherwise the file is not a full promoted spec (missing/unknown
            # keys) -- fall through to the raw-file fallback and say so.
            fallback_note = f"not a full promoted spec ({msg}); "
        except Exception as exc:  # a corrupt file
            return None, f"load_promoted rejected the spec: {exc}"
        else:
            fallback_note = ""
    else:
        fallback_note = "src.factory.promoted unavailable; "
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and raw.get("spec_hash"):
            return str(raw["spec_hash"]), fallback_note + "spec_hash field of the promoted spec file (unverified)"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"promoted spec unreadable: {exc}"
    return sha256_file(path), fallback_note + "sha256 of the promoted spec file (CRLF-normalised)"


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


_YES_PAYOFF = {"yes": 1.0, "no": 0.0}


def stale_no_side_settlement(row: Mapping[str, Any]) -> Optional[str]:
    """``None`` when the row's NO-side settlement numbers are trustworthy, else why not.

    ``"stale"``: exit_price is the YES payoff of the recorded outcome and pnl is the
    pre-724d93c formula -> the row needs scripts/repair_no_settlement_pnl.py.
    ``"unverifiable"``: a NO settlement with no recorded outcome (the same numbers
    read as a repaired winner or a buggy loser).
    """
    if str(row.get("contract_side") or "YES").upper() != "NO":
        return None
    if row.get(REPAIRED_MARKER):
        return None
    reason = str(row.get("close_reason") or row.get("reason") or "").upper()
    if "EXPIRATION" not in reason and "SETTLEMENT" not in reason:
        return None
    if "UNRESOLVED" in reason:
        return None
    try:
        exit_price = float(row.get("exit_price"))
    except (TypeError, ValueError):
        return None
    on_grid = abs(exit_price) < _SETTLEMENT_TOL or abs(exit_price - 1.0) < _SETTLEMENT_TOL
    if not on_grid:
        return None
    outcome = str(row.get("settlement_outcome") or "").lower()
    if outcome not in _YES_PAYOFF:
        return "unverifiable"
    if abs(exit_price - _YES_PAYOFF[outcome]) > _SETTLEMENT_TOL:
        return None  # already priced on the NO leg
    try:
        entry = float(row["entry_price"])
        qty = float(row["quantity"])
        pnl = float(row["pnl"])
    except (KeyError, TypeError, ValueError):
        return None
    exit_fee = float(row.get("exit_fee") or 0.0)
    old = (exit_price - entry) * qty - exit_fee
    if str(row.get("side") or "buy") == "sell":
        old = (entry - exit_price) * qty - exit_fee
    return "stale" if abs(pnl - old) < 1e-6 else None


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


def _is_maker_booked(row: Mapping[str, Any]) -> bool:
    if row.get("is_maker") is True:
        return True
    return str(row.get("fill_type") or "").lower() == "maker"


def collect_settled_trades(
    journal_rows: Sequence[Mapping[str, Any]],
    closed_trades: Sequence[Mapping[str, Any]],
    *,
    strategy_name: str,
    market_family: str = "KXHIGH",
    fee_type: str = "taker",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Merge journal + closed_trades into one settled-fill list for ``strategy_name``.

    The journal is the durable record (append-only); ``closed_trades`` carries
    ``entry_fee`` and is cleared on a cycle reset. Rows are joined on
    ``(symbol, entry_time, strategy_name)`` -- ``entry_time`` is the exact
    ``open_time.isoformat()`` string in both files.

    Fee trust (taker registration): ``fee = max(booked entry_fee / qty,
    recomputed taker fee at the row's price and the JOURNAL quantity)``.
    ``counts["stale_no_side_rows"]`` lists rows still carrying the pre-724d93c
    sign-flipped numbers; the caller refuses when it is non-empty.
    """
    excluded: Counter = Counter()
    by_key: Dict[Tuple[str, Optional[str], str], Dict[str, Any]] = {}
    stale_rows: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    taker_reg = str(fee_type).lower() == "taker"

    state_by_key: Dict[Tuple[str, Optional[str], str], Mapping[str, Any]] = {}
    for t in closed_trades:
        state_by_key.setdefault(_join_key(t), t)

    def _fee_for(trade: Dict[str, Any], booked: Optional[float]) -> Tuple[float, str]:
        recomputed = nearest_cent_taker_fee(trade["symbol"], trade["entry_price"], trade["quantity"])
        if booked is None:
            return recomputed, "recomputed_taker"
        if taker_reg and recomputed > float(booked) + 1e-9:
            return recomputed, "recomputed_taker (booked fee below taker; max rule)"
        return float(booked), "closed_trades.entry_fee"

    def _check_no_side(trade: Dict[str, Any], row: Mapping[str, Any], source: str) -> bool:
        """False when the row must not be admitted (stale -> collected; unverifiable -> excluded)."""
        why = stale_no_side_settlement(row)
        if why == "stale":
            stale_rows.append({"symbol": trade["symbol"], "entry_time": trade["entry_time"], "source": source})
            return False
        if why == "unverifiable":
            excluded["no_side_outcome_unverifiable"] += 1
            return False
        return True

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
            "maker_booked": _is_maker_booked(row),
            "repaired": bool(row.get(REPAIRED_MARKER)),
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
        if st is not None:
            # the state row is the fee/pnl source when present: it must be clean too
            if not _check_no_side(trade, st, "closed_trades"):
                continue
            trade["source"] = "journal+closed_trades"
            trade["maker_booked"] = trade["maker_booked"] or _is_maker_booked(st)
            trade["repaired"] = trade["repaired"] or bool(st.get(REPAIRED_MARKER))
            try:
                st_qty = float(st.get("quantity"))
            except (TypeError, ValueError):
                st_qty = None
            if st_qty is not None and abs(st_qty - trade["quantity"]) > 1e-9:
                warnings.append(
                    {
                        "warning": "quantity_mismatch_journal_vs_state",
                        "symbol": trade["symbol"],
                        "entry_time": trade["entry_time"],
                        "journal_quantity": trade["quantity"],
                        "state_quantity": st_qty,
                        "used": "journal quantity (same row as the price)",
                    }
                )
            booked = None
            if st.get("entry_fee") is not None:
                booked = float(st["entry_fee"])
                if st_qty is not None and st_qty > 0 and abs(st_qty - trade["quantity"]) > 1e-9:
                    # a fee booked at another size is rescaled per contract onto the journal qty
                    booked = booked / st_qty * trade["quantity"]
            trade["entry_fee"], trade["fee_source"] = _fee_for(trade, booked)
            if abs(float(st.get("pnl", trade["pnl"])) - trade["pnl"]) > 1e-6:
                trade["pnl_journal"] = trade["pnl"]
                trade["pnl"] = float(st["pnl"])
                trade["pnl_source"] = "closed_trades (journal disagreed)"
        else:
            if not _check_no_side(trade, row, "journal"):
                continue
            trade["entry_fee"], trade["fee_source"] = _fee_for(trade, None)
        by_key[key] = trade

    for t in closed_trades:
        key = _join_key(t)
        if key in by_key:
            continue
        trade = _admit(t, "closed_trades_only")
        if trade is None:
            continue
        if not _check_no_side(trade, t, "closed_trades"):
            continue
        booked = float(t["entry_fee"]) if t.get("entry_fee") is not None else None
        trade["entry_fee"], trade["fee_source"] = _fee_for(trade, booked)
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
        "stale_no_side_rows": stale_rows,
        "warnings": warnings,
        "maker_booked_fills": [
            {"symbol": t["symbol"], "entry_time": t["entry_time"]} for t in trades if t["maker_booked"]
        ],
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
        w_u = unit_null_win_probability(fills)
        units.append(
            {
                "target_date": td,
                "n_fills": len(fills),
                "symbols": sorted(f["symbol"] for f in fills),
                "quantity": qty,
                "net_pnl": net,
                "q_star": q_star,
                "null_win_probability": float(w_u),
                "null_win_probability_exact": _fraction_str(w_u),
                "won": net > 0.0,
                "_w_u": w_u,
            }
        )
    return units


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def _first_trade_after(trades: Sequence[Mapping[str, Any]], cutoff_iso: Optional[str]):
    """``(ok, note)`` for the registered_before_first_trade condition.

    ``ok`` is ``False`` when ``registration_commit_utc`` is missing: the gate
    cannot prove the registration preceded the first trade, so it fails by
    default (``--allow-unverified-registration`` downgrades it to non-gating).
    """
    if not cutoff_iso:
        return False, (
            "registration commit time not recorded (registration_commit_utc is null); "
            "fill it from `git log --diff-filter=A --format=%cI -- "
            "configs/factory/gate_registration.json`"
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
    allow_unverified_registration: bool = False,
    realistic_fills: Optional[bool] = None,
    counts: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The verdict dict (timestamp-free). Pure: no I/O."""
    thresholds = registration["thresholds"]
    n_min = int(thresholds["n_min"])
    alpha = float(thresholds["alpha"])
    net_gt = float(thresholds.get("net_pnl_gt", 0.0))
    counts = counts or {}

    units = group_units(trades)
    n_units = len(units)
    k_units = sum(1 for u in units if u["won"])
    refused = n_units < n_min

    net_pnl = math.fsum(t["net_pnl"] for t in trades)
    gross = math.fsum(t["pnl"] for t in trades)
    fees = math.fsum(t["entry_fee"] for t in trades)

    multi = sum(1 for u in units if u["n_fills"] > 1)
    w_us = [u.pop("_w_u") for u in units]  # Fractions: not for the JSON
    p_exact: Optional[Fraction] = None
    if n_units:
        q_bar_units = math.fsum(u["q_star"] for u in units) / n_units
        if not refused:
            p_exact = poisson_binomial_upper_tail(w_us, k_units)
        p_units = None if p_exact is None else float(p_exact)
        p_pooled = None if refused else binomial_upper_tail(n_units, k_units, q_bar_units)
    else:
        q_bar_units = None
        p_units = None
        p_pooled = None

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
    rbft_gating = True
    if allow_unverified_registration and not registration.get("registration_commit_utc"):
        rbft_gating = False
        rbft_ok = None
        rbft_note = "UNVERIFIED (--allow-unverified-registration): " + rbft_note

    reg_fee_type = str(registration.get("fee_type") or "taker").lower()
    maker_fills = list(counts.get("maker_booked_fills") or [])
    fee_type_ok = not (reg_fee_type == "taker" and maker_fills)

    requires_realistic = bool(registration.get("requires_realistic_fills", True))
    if realistic_fills is None:
        rf_ok: Optional[bool] = None
        rf_note = "UNVERIFIED: the exchange state does not record realistic_fills; pass --realistic-fills"
    else:
        rf_ok = bool(realistic_fills) or not requires_realistic
        rf_note = f"realistic_fills={realistic_fills}; required={requires_realistic}"

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
            "observed_exact": _fraction_str(p_exact),
            "required_lt": alpha,
            "note": "not computed (refused)" if refused else "exact Poisson-binomial upper tail over units",
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
            "gating": rbft_gating,
            "note": rbft_note,
        },
        "fee_type_matches": {
            "ok": fee_type_ok,
            "registered_fee_type": reg_fee_type,
            "maker_booked_fills": maker_fills,
            "note": "a maker-booked fill under a taker registration is not the registered fee leg",
        },
        "realistic_fills_enabled": {
            "ok": rf_ok,
            "gating": rf_ok is not None,
            "observed": realistic_fills,
            "required": requires_realistic,
            "note": rf_note,
        },
    }
    gating = [
        conditions["n_units_ge_n_min"]["ok"],
        conditions["p_lt_alpha"]["ok"],
        conditions["net_pnl_gt_0"]["ok"],
        conditions["spec_hash_unchanged"]["ok"],
        conditions["fee_type_matches"]["ok"],
    ]
    if rbft_gating:
        gating.append(bool(rbft_ok))
    if rf_ok is not None:
        gating.append(bool(rf_ok))
    not_applicable = [name for name, c in conditions.items() if c.get("ok") is None]
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
        "failing": [name for name, c in conditions.items() if c.get("ok") is False and c.get("gating", True)],
        "not_applicable": not_applicable,
        "allow_unverified_registration": bool(allow_unverified_registration),
        "units": {
            "n": n_units,
            "k_wins": k_units,
            "p_upper_tail": p_units,
            "p_exact_str": _fraction_str(p_exact),
            "null": FORMULAS["unit_null_win_probability"],
            "test": FORMULAS["p_value"],
            "rule": FORMULAS["unit_win"],
            "units_with_multiple_fills": multi,
            "max_fills_per_unit": max((u["n_fills"] for u in units), default=0),
            "null_win_rate_q_bar": q_bar_units,
            "p_pooled_qbar_secondary": p_pooled,
            "pooled_note": (
                "p_pooled_qbar_secondary = binomial with q_bar = mean unit breakeven; an "
                "APPROXIMATION kept for comparison, non-gating (exact only with one fill per unit)"
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
        "warnings": list(counts.get("warnings") or []),
        "formulas": dict(FORMULAS),
        "unit_table": units,
        "fills": [dict(t) for t in trades],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _state_realistic_fills(state_path: Optional[str]) -> Optional[bool]:
    """``realistic_fills`` if the exchange state ever serialises it (it does not today)."""
    if not state_path or not os.path.exists(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    v = state.get("realistic_fills") if isinstance(state, dict) else None
    return bool(v) if isinstance(v, bool) else None


def run_gate(
    *,
    journal_path: str,
    state_path: Optional[str],
    registration_path: str,
    out_path: Optional[str],
    strategy_override: Optional[str] = None,
    promoted_override: Optional[str] = None,
    allow_unverified_registration: bool = False,
    realistic_fills: Optional[bool] = None,
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
        fee_type=str(registration.get("fee_type") or "taker"),
    )
    if counts["stale_no_side_rows"]:
        rows = counts["stale_no_side_rows"]
        raise GateRefusal(
            f"stale NO-side settlement rows present ({len(rows)}: "
            + ", ".join(f"{r['symbol']}@{r['entry_time']} [{r['source']}]" for r in rows[:5])
            + ("..." if len(rows) > 5 else "")
            + "); run scripts/repair_no_settlement_pnl.py --state ... --journal ... --apply first"
        )
    spec_path = promoted_override or registration.get("promoted_spec_path")
    observed_hash, hash_source = resolve_spec_hash(spec_path)
    if realistic_fills is None:
        realistic_fills = _state_realistic_fills(state_path)
    verdict = evaluate(
        trades,
        registration,
        observed_spec_hash=observed_hash,
        spec_hash_source=hash_source,
        allow_unverified_registration=allow_unverified_registration,
        realistic_fills=realistic_fills,
        counts=counts,
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
        "registration_commit_utc": registration.get("registration_commit_utc"),
        "requires_realistic_fills": registration.get("requires_realistic_fills", True),
    }
    verdict["inputs"] = {
        "journal": journal_path,
        "journal_sha256": sha256_file(journal_path),
        "state": state_path,
        "state_sha256": sha256_file(state_path) if state_path else None,
        "promoted_spec_path": spec_path,
        "realistic_fills": realistic_fills,
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
    ap.add_argument(
        "--allow-unverified-registration",
        action="store_true",
        help="dry runs only: a null registration_commit_utc is reported, not gating",
    )
    ap.add_argument(
        "--realistic-fills",
        choices=("true", "false", "unknown"),
        default="unknown",
        help="whether the sandbox ran with realistic_fills (the state file does not record it)",
    )
    ap.add_argument("--quiet", action="store_true", help="print only the verdict line")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    rf = {"true": True, "false": False, "unknown": None}[args.realistic_fills]
    try:
        verdict = run_gate(
            journal_path=args.journal,
            state_path=args.state,
            registration_path=args.registration,
            out_path=args.out,
            strategy_override=args.strategy,
            promoted_override=args.promoted,
            allow_unverified_registration=args.allow_unverified_registration,
            realistic_fills=rf,
        )
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GateRefusal as exc:
        print(f"gate: REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if args.quiet:
        u = verdict["units"]
        print(
            f"{verdict['verdict']} n_units={u['n']} k={u['k_wins']} "
            f"p={u['p_upper_tail']} (pooled q_bar={u['null_win_rate_q_bar']} "
            f"p_secondary={u['p_pooled_qbar_secondary']}) "
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
