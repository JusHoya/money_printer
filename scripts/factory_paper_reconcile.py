#!/usr/bin/env python3
"""factory_paper_reconcile.py -- weekly lab-vs-paper reconciliation of a promoted genome.

FACTORY_ARCHITECTURE section 9 item 7 / FACTORY_ROADMAP section F3 item 7 /
PRD_STRATEGY_FACTORY FR-F3.4: once a promoted genome paper-trades in the maia
sandbox, every week the sandbox record is re-read against the lab that produced
the genome. Two questions, answered separately and never merged:

1. **Re-pricing.** Is each sandbox fill worth what the lab formula says it is?
   Every fill is re-priced as the lab priced its frame rows::

       quote      = the FRAME's own ``quote`` for that (market, decision hour,
                    direction) when the lab set covers it -- a number the
                    sandbox did not produce. Only when the frame has no row for
                    that market-hour does the script fall back to the algebraic
                    reconstruction ``price_booked - adverse_fill_at_entry``,
                    and it then SAYS SO per fill (``quote_source``): that
                    fallback is not independent of the number being audited.
       price_lab  = quote + adverse_fill_lab                      (--adverse-fill;
                    defaults to the spec's own allowance)
       fee_lab    = src.factory.fees.fee_per_contract(price_lab, ts, series,
                    contracts=C, is_maker=False)                  (the frame's own
                    fee function: taker, ceil-to-cent on a C=20 order, per contract)
       payoff     = 1.0 iff the contract SIDE WE HELD settled in the money,
                    derived from the row's own settlement truth -- the recorded
                    ``settlement_outcome`` cross-checked against
                    ``settlement_spec`` + ``settlement_high`` through
                    ``bracket_payoff.settles_yes``. The payoff is NEVER read off
                    ``won``/``net_pnl``: those are the numbers under audit, and
                    a settlement sign error read off itself reconciles to
                    delta 0.000000 (red team F3, defect 1).
       realized_lab per contract = payoff - price_lab - fee_lab   (held to settlement)

   and set against the sandbox's own per-contract realized
   ``(pnl - entry_fee) / quantity`` at ACTUAL quantity (Kelly sizing differs
   from the 20-contract frame assumption, CONTRA 10, so the fee per contract
   differs by the cent-ceiling; the report shows both). The same settlement
   truth also RE-DERIVES what the sandbox's own realized should have been
   (``payoff - price_booked - sandbox_fee``); a disagreement is reported as
   ``settlement_sign_mismatch`` and is blocking.

2. **Trade set.** Is the sandbox trade set a subset of the lab trade set on
   the same dates? The lab set is the genome's ``fitness.score(F, to_mask(g,
   F)).trade_rows`` on the frozen search frame restricted to the date range.
   Matching is a BIJECTION on ``(market_ticker, decision hour UTC, direction)``:
   two lab trades in one market-hour are not "explained" by one sandbox fill,
   and the count/quantity discrepancy is reported (``multiplicity_mismatch``,
   ``quantity_mismatch``) rather than collapsed away by set membership
   (red team F3, defect 3).
   ``lab \\ sandbox`` are lab-admissible trades the sandbox skipped: each is
   annotated with the REJECT codes the runtime logged for that market
   (``[Risk] REJECT strategy=... symbol=... reason=CODE`` lines from
   ``risk_manager.log_rejection`` -- KELLY_ZERO, WEATHER_SLOT_FULL, cooldowns,
   allocation, GENOME_* ...). An UNEXPLAINED ``lab_only`` row is the most
   informative output this tool produces and is blocking. ``sandbox \\ lab``
   should be EMPTY; anything there is a live-path/offline discrepancy.

   The frozen frame ends at its cutoff (2026-07-25 for
   ``weather_2026-07-25_bfcf94654a3a``). When the requested dates are not in
   the frame the report SAYS SO (``lab_trade_set.coverage``) and falls back to
   re-pricing only plus the REJECT-code profile from the log; it does not
   pretend the frame covers dates it never saw.

PnL is read from ``closed_trades`` / the journal, never from equity (the
UTC-midnight reset double-subtracts).

VACUOUS RUNS ARE NOT PASSES
---------------------------
A run answers a question only if it had the inputs for it: re-pricing needs at
least one settled fill, the trade set needs frame coverage. A shadow-mode week
on dates outside the frozen frame answers NEITHER -- it is 0 fills against 0
lab trades. That report is ``verdict: REFUSED`` and exit 3, never a pass, and
the markdown says so in its first line (red team F3, defect 6).

READING THE SANDBOX OVER HTTP
-----------------------------
``--url http://maia.local:8050`` pulls the journal from ``/api/journal`` and
the REJECT lines from ``/api/logs/tail``. The exchange state is NOT exposed
over HTTP: ``closed_trades`` and open positions are then unobtainable, and the
report says exactly that under ``inputs.not_obtained`` with the consequence for
each conclusion. Both endpoints are capped at 500 rows/lines server-side; the
report records whether the cap was hit. A run with incomplete inputs can never
be ``verdict: OK`` -- at best ``PARTIAL``.

USAGE
-----
    python scripts/factory_paper_reconcile.py \\
        --promoted configs/factory/promoted/<id>.json \\
        --journal data/trade_journal.jsonl --state data/exchange_state.json \\
        --log logs/money_printer_*.log --from 2026-09-08 --to 2026-09-14

    python scripts/factory_paper_reconcile.py \\
        --promoted configs/factory/promoted/<id>.json \\
        --url http://maia.local:8050 --from 2026-09-08 --to 2026-09-14

Writes ``reports/factory/paper_reconcile_<from>_<to>.json`` and ``.md``.
Exit codes: 0 OK/PARTIAL (nothing blocking), 1 DISCREPANCY, 2 usage error,
3 REFUSED (nothing was comparable, or the inputs are not trustworthy).
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.fee_calculator import series_ticker_from_symbol  # noqa: E402
from src.factory import fees as fees_mod  # noqa: E402
from src.factory.report import write_json, write_text  # noqa: E402


def _load_gate_module():
    """``scripts/gate.py`` as a module (shared journal/state assembly)."""
    spec = importlib.util.spec_from_file_location(
        "mp_gate", os.path.join(_THIS_DIR, "gate.py")
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate_module()

DEFAULT_FRAMES_DIR = os.path.join(
    REPO_ROOT, "data", "factory", "frames", "weather_2026-07-25_bfcf94654a3a"
)
MAX_ORDERABLE_PRICE = 0.99
DEFAULT_URL = "http://maia.local:8050"
#: Server-side caps in ``src/web/server.py`` (/api/journal, /api/logs/tail).
HTTP_JOURNAL_CAP = 500
HTTP_LOG_CAP = 500
#: A quote reconstructed from a cent-grid price matches the frame exactly; any
#: difference above this is a real disagreement, not float noise.
QUOTE_TOL = 1e-6
#: Per-contract realized tolerance for the settlement cross-check. Settlement
#: exit fees are zero in the sandbox; half a cent absorbs any rounding while
#: staying two orders of magnitude below a sign error (which is ~1.00).
REALIZED_TOL = 5e-3

#: ``2026-09-05 01:24:01 | INFO    | [Risk] REJECT strategy=Genome 7d857b00 symbol=KXHIGHNY-26SEP04-B84.5 reason=KELLY_ZERO k=v``
_REJECT_RE = re.compile(
    r"\[Risk\] REJECT strategy=(?P<strategy>.*?) symbol=(?P<symbol>\S+) "
    r"reason=(?P<reason>\S+)(?P<rest>.*)$"
)
_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

#: Settlement-truth fields carried by a journal row / closed-trade row. They are
#: dropped by ``gate.collect_settled_trades`` (which returns only the numeric
#: audit surface), so the raw rows are re-joined on the gate's own join key.
_TRUTH_FIELDS = (
    "contract_side",
    "settlement_outcome",
    "settlement_high",
    "settlement_value",
    "settlement_rule",
    "settlement_spec",
    "settlement_error",
    "strike_type",
    "floor_strike",
    "cap_strike",
)

#: ``collect_settled_trades`` exclusion buckets that mean a row could not be
#: verified (as opposed to a row that simply is not ours).
REFUSING_EXCLUSIONS = (
    "no_side_outcome_unverifiable",
    "no_target_date",
    "missing_numeric_field",
    "non_positive_quantity",
)


class ReconcileError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Promoted spec
# ---------------------------------------------------------------------------
def load_spec(path: str) -> Dict[str, Any]:
    """The promoted spec as a plain dict (``load_promoted`` when available)."""
    full = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
    if not os.path.exists(full):
        raise ReconcileError(f"promoted spec not found: {path}")
    try:
        from src.factory.promoted import PromotedSpecError, load_promoted  # type: ignore
    except ImportError:
        PromotedSpecError = None  # type: ignore[assignment]
        load_promoted = None  # type: ignore[assignment]
    if load_promoted is not None:
        try:
            spec = load_promoted(full)
            if isinstance(spec, Mapping):
                return dict(spec)
            out = {}
            for key in (
                "genome_id", "genome_json", "family", "adverse_fill", "contracts_frame",
                "fee", "mode", "spec_hash", "frame_search_sha256",
            ):
                if hasattr(spec, key):
                    val = getattr(spec, key)
                    out[key] = dict(val.__dict__) if hasattr(val, "__dict__") else val
            if out.get("genome_json") is not None:
                out["spec_source"] = "src.factory.promoted.load_promoted (content hash verified)"
                return out
        except PromotedSpecError as exc:  # type: ignore[misc]
            msg = str(exc)
            if "does not verify" in msg or "genome_id" in msg:
                raise ReconcileError(f"{path}: promoted spec changed since it was written: {msg}")
            # not a full promoted spec: fall back to the raw document below
    with open(full, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict) or "genome_json" not in raw:
        raise ReconcileError(f"{path}: not a promoted spec (no genome_json)")
    raw["spec_source"] = "raw promoted spec file (not a full spec; hash unverified)"
    return raw


def strategy_name_for(spec: Mapping[str, Any]) -> str:
    gid = str(spec.get("genome_id") or "")
    return f"Genome {gid[:8]}"


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
def parse_reject_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Every ``log_rejection`` line -> ``{ts, strategy, symbol, reason, context}``."""
    out: List[Dict[str, Any]] = []
    for line in lines:
        m = _REJECT_RE.search(line)
        if not m:
            continue
        ctx: Dict[str, str] = {}
        for tok in m.group("rest").split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                ctx[k] = v
        tm = _LINE_TS_RE.match(line)
        out.append(
            {
                "ts": tm.group(1).replace(" ", "T") if tm else None,
                "strategy": m.group("strategy").strip(),
                "symbol": m.group("symbol"),
                "reason": m.group("reason"),
                "context": ctx,
            }
        )
    return out


def load_reject_lines(paths: Sequence[str]) -> List[Dict[str, Any]]:
    files: List[str] = []
    for p in paths:
        files.extend(sorted(glob.glob(p)) or ([p] if os.path.exists(p) else []))
    rejects: List[Dict[str, Any]] = []
    for f in files:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            rejects.extend(parse_reject_lines(fh))
    return rejects


# ---------------------------------------------------------------------------
# Sandbox over HTTP (the owner has no shell on maia)
# ---------------------------------------------------------------------------
def http_get_json(url: str, timeout: float = 15.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (LAN GET)
        return json.loads(resp.read())


def fetch_journal_http(
    base_url: str, *, last_n: int = HTTP_JOURNAL_CAP, timeout: float = 15.0
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """``/api/journal`` -> (rows, provenance). Raises ``ReconcileError`` on failure."""
    q = urllib.parse.urlencode({"last_n": min(max(int(last_n), 1), HTTP_JOURNAL_CAP)})
    url = f"{base_url.rstrip('/')}/api/journal?{q}"
    try:
        payload = http_get_json(url, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ReconcileError(f"{url}: {exc}") from exc
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        raise ReconcileError(f"{url}: {payload!r}")
    rows = [r for r in (payload.get("trades") or []) if isinstance(r, dict)]
    entries = sorted(str(r.get("entry_time") or "") for r in rows if r.get("entry_time"))
    prov = {
        "url": url,
        "rows": len(rows),
        "cap": HTTP_JOURNAL_CAP,
        "cap_hit": len(rows) >= HTTP_JOURNAL_CAP,
        "earliest_entry_time": entries[0] if entries else None,
    }
    return rows, prov


def fetch_reject_lines_http(
    base_url: str,
    *,
    pattern: str = "money_printer_*.log",
    lines: int = HTTP_LOG_CAP,
    timeout: float = 15.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """``/api/logs/tail`` -> (rejects, provenance). Raises ``ReconcileError``."""
    q = urllib.parse.urlencode(
        {"pattern": pattern, "lines": min(max(int(lines), 1), HTTP_LOG_CAP)}
    )
    url = f"{base_url.rstrip('/')}/api/logs/tail?{q}"
    try:
        payload = http_get_json(url, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ReconcileError(f"{url}: {exc}") from exc
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        raise ReconcileError(f"{url}: {payload!r}")
    text = str(payload.get("content") or "")
    raw_lines = text.splitlines()
    stamps = [m.group(1) for m in (_LINE_TS_RE.match(x) for x in raw_lines) if m]
    prov = {
        "url": url,
        "lines": len(raw_lines),
        "cap": HTTP_LOG_CAP,
        "cap_hit": len(raw_lines) >= HTTP_LOG_CAP,
        "window": (
            f"{stamps[0].replace(' ', 'T')} .. {stamps[-1].replace(' ', 'T')}"
            if stamps
            else None
        ),
    }
    return parse_reject_lines(raw_lines), prov


def _input_row(name: str, source: str, detail: str) -> Dict[str, str]:
    return {"input": name, "source": source, "detail": detail}


def _missing_row(name: str, why: str, consequence: str) -> Dict[str, str]:
    return {"input": name, "why": why, "consequence": consequence}


def gather_inputs_http(base_url: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    """Everything the dashboard exposes, plus a plain list of what it does not."""
    journal_rows, jprov = fetch_journal_http(base_url, timeout=timeout)
    rejects, lprov = fetch_reject_lines_http(base_url, timeout=timeout)
    obtained = [
        _input_row(
            "journal",
            jprov["url"],
            f"{jprov['rows']} rows"
            + (
                f"; SERVER CAP {jprov['cap']} HIT -- rows before "
                f"{jprov['earliest_entry_time']} are not visible to this run"
                if jprov["cap_hit"]
                else ""
            ),
        ),
        _input_row(
            "reject_lines",
            lprov["url"],
            f"{lprov['lines']} log lines"
            + (f", window {lprov['window']}" if lprov["window"] else "")
            + (
                f"; SERVER CAP {lprov['cap']} HIT -- this is an 8-16 minute window, "
                "so the absence of a REJECT code is NOT evidence the runtime never "
                "logged one"
                if lprov["cap_hit"]
                else ""
            ),
        ),
    ]
    not_obtained = [
        _missing_row(
            "exchange_state (closed_trades)",
            "the sandbox dashboard exposes no endpoint for data/exchange_state.json "
            "and the owner has HTTP-only access to maia (no ssh)",
            "fees fall back to a recomputed taker fee instead of the booked "
            "entry_fee; a settled fill whose journal row is missing is invisible; "
            "the journal-vs-state quantity cross-check cannot run. The fill set is "
            "therefore a LOWER BOUND, so 'sandbox subset of lab' is not proven.",
        ),
        _missing_row(
            "exchange_state (open positions)",
            "same endpoint gap",
            "pending (unsettled) positions cannot be listed; n_sandbox_pending is 0 "
            "because it is unknown, not because the book is flat, and an open "
            "position cannot contribute its market-hour to the trade-set match.",
        ),
    ]
    return {
        "journal_rows": journal_rows,
        "closed_trades": [],
        "open_positions": [],
        "rejects": rejects,
        "inputs": {
            "mode": "http",
            "base_url": base_url,
            "obtained": obtained,
            "not_obtained": not_obtained,
            "complete": False,
            "journal_provenance": jprov,
            "log_provenance": lprov,
        },
    }


def gather_inputs_files(
    journal: str, state: Optional[str], logs: Sequence[str]
) -> Dict[str, Any]:
    journal_rows = _GATE.load_journal(journal)
    closed = _GATE.load_closed_trades(state)
    open_positions: List[Dict[str, Any]] = []
    if state:
        with open(state, "r", encoding="utf-8") as fh:
            open_positions = [
                p for p in (json.load(fh).get("positions") or []) if isinstance(p, dict)
            ]
    rejects = load_reject_lines(logs)
    obtained = [_input_row("journal", journal, f"{len(journal_rows)} rows")]
    not_obtained: List[Dict[str, str]] = []
    if state:
        obtained.append(
            _input_row(
                "exchange_state",
                state,
                f"{len(closed)} closed_trades, {len(open_positions)} open positions",
            )
        )
    else:
        not_obtained.append(
            _missing_row(
                "exchange_state",
                "--state was not given",
                "booked entry_fee and open positions unavailable; fees are "
                "recomputed as taker and the fill set is a lower bound.",
            )
        )
    if logs:
        obtained.append(_input_row("reject_lines", ", ".join(logs), f"{len(rejects)} REJECT lines"))
    else:
        not_obtained.append(
            _missing_row(
                "reject_lines",
                "--log was not given",
                "no lab_only row can be explained by a runtime REJECT code, so every "
                "one of them is reported unexplained.",
            )
        )
    return {
        "journal_rows": journal_rows,
        "closed_trades": closed,
        "open_positions": open_positions,
        "rejects": rejects,
        "inputs": {
            "mode": "filesystem",
            "base_url": None,
            "obtained": obtained,
            "not_obtained": not_obtained,
            "complete": not not_obtained,
        },
    }


# ---------------------------------------------------------------------------
# Settlement truth (independent of pnl / won)
# ---------------------------------------------------------------------------
def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def truth_index(
    journal_rows: Sequence[Mapping[str, Any]],
    closed_trades: Sequence[Mapping[str, Any]],
) -> Dict[Any, Dict[str, Any]]:
    """Settlement fields of the RAW rows, keyed by ``gate._join_key``.

    ``collect_settled_trades`` strips them; they are the only ground truth in
    the record that does not descend from ``pnl``.
    """
    idx: Dict[Any, Dict[str, Any]] = {}
    for row in list(closed_trades) + list(journal_rows):  # journal wins on conflict
        merged = idx.setdefault(_GATE._join_key(row), {})
        for field in _TRUTH_FIELDS:
            value = row.get(field)
            if value is not None:
                merged[field] = value
    return idx


def outcome_from_strike_spec(symbol: str, row: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(outcome, note)`` recomputed from the strike spec and the settled high."""
    high = _num(row.get("settlement_high"))
    if high is None:
        if _num(row.get("settlement_value")) is not None:
            return None, "settlement_value is a gas truth (USD/gal); not a temperature bracket"
        return None, "no settlement_high recorded"
    spec = row.get("settlement_spec")
    fields = spec if isinstance(spec, Mapping) else row
    strike_type = fields.get("strike_type")
    if not strike_type:
        return None, "no strike_type recorded (settlement_spec absent)"
    try:
        from src.core.bracket_payoff import BracketSpec, BracketSpecError, settles_yes

        bracket = BracketSpec(
            ticker=symbol,
            strike_type=str(strike_type),
            floor_strike=_num(fields.get("floor_strike")),
            cap_strike=_num(fields.get("cap_strike")),
        )
        return ("yes" if settles_yes(bracket, high) else "no"), None
    except BracketSpecError as exc:
        return None, f"strike spec unusable: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"strike spec recompute failed: {exc}"


def settlement_truth(symbol: str, contract_side: Any, row: Mapping[str, Any]) -> Dict[str, Any]:
    """The payoff of the side WE held, from settlement truth only.

    Never touches ``pnl``, ``net_pnl`` or ``won``: those are the numbers under
    audit. The recorded ``settlement_outcome`` is cross-checked against the
    strike spec, and the strike spec wins a disagreement (it is the deeper
    truth) with the disagreement reported.
    """
    side = str(contract_side or row.get("contract_side") or "YES").upper()
    recorded = str(row.get("settlement_outcome") or "").strip().lower() or None
    if recorded not in ("yes", "no"):
        recorded = None
    derived, derive_note = outcome_from_strike_spec(symbol, row)
    disagreement = recorded is not None and derived is not None and recorded != derived
    if derived is not None:
        used, source = derived, "strike_spec (settlement_spec + settlement_high)"
    elif recorded is not None:
        used, source = recorded, "settlement_outcome (recorded)"
    else:
        used, source = None, None
    out: Dict[str, Any] = {
        "contract_side": side,
        "outcome_recorded": recorded,
        "outcome_from_strike_spec": derived,
        "outcome_used": used,
        "outcome_source": source,
        "outcome_disagreement": disagreement,
        "payoff_per_contract": None,
        "note": derive_note,
    }
    if disagreement:
        out["note"] = (
            f"settlement_outcome={recorded!r} contradicts the strike spec "
            f"({row.get('settlement_rule') or row.get('settlement_spec')!r} at "
            f"settlement_high={row.get('settlement_high')!r} -> {derived!r}); "
            f"the strike spec is used"
        )
    if used is None:
        out["note"] = out["note"] or "no settlement truth on the row (outcome and strike spec both absent)"
        return out
    if side not in ("YES", "NO"):
        out["note"] = f"unknown contract_side {side!r}; payoff undecidable"
        return out
    out["payoff_per_contract"] = 1.0 if ((side == "YES") == (used == "yes")) else 0.0
    return out


# ---------------------------------------------------------------------------
# Re-pricing
# ---------------------------------------------------------------------------
def _epoch(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # sandbox clock is UTC (deploy/pi TZ=UTC)
    return int(dt.timestamp())


def reprice_fill(
    fill: Mapping[str, Any],
    *,
    adverse_fill_at_entry: float,
    adverse_fill_lab: float,
    contracts: int,
    regime: fees_mod.FeeRegime,
    truth: Optional[Mapping[str, Any]] = None,
    frame_row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One sandbox fill through the lab formula (see module docstring).

    ``truth`` is ``settlement_truth(...)`` for the fill's own row; ``frame_row``
    is the lab trade this fill was matched to, whose ``quote`` is the only
    price in the whole comparison the sandbox did not produce.
    """
    price_booked = float(fill["entry_price"])
    quote_reconstructed = round(price_booked - adverse_fill_at_entry, 4)
    quote_frame = _num((frame_row or {}).get("quote"))
    price_paid_frame = _num((frame_row or {}).get("price_paid"))
    if quote_frame is not None:
        quote, quote_source = quote_frame, "frame.quote (independent of the booked price)"
    else:
        quote = quote_reconstructed
        quote_source = (
            "reconstructed price_booked - adverse_fill_at_entry "
            "(NO frame row for this market-hour; NOT independent of the booked price)"
        )
    price_lab = round(quote + adverse_fill_lab, 10)
    ts = _epoch(fill.get("entry_time"))
    series = series_ticker_from_symbol(fill["symbol"])
    truth = dict(truth or {})
    payoff = truth.get("payoff_per_contract")
    sandbox_fee_pc = float(fill["entry_fee"]) / float(fill["quantity"])
    sandbox_realized_pc = float(fill["net_pnl"]) / float(fill["quantity"])
    quote_delta = None if quote_frame is None else quote_frame - quote_reconstructed
    out: Dict[str, Any] = {
        "symbol": fill["symbol"],
        "target_date": fill.get("target_date"),
        "entry_time": fill.get("entry_time"),
        "contract_side": fill.get("contract_side"),
        "quantity": fill["quantity"],
        "price_booked": price_booked,
        "quote_reconstructed": quote_reconstructed,
        "quote_frame": quote_frame,
        "quote_used": quote,
        "quote_source": quote_source,
        "quote_delta_frame_minus_reconstructed": quote_delta,
        "price_paid_frame": price_paid_frame,
        "price_disagreement": bool(quote_delta is not None and abs(quote_delta) > QUOTE_TOL),
        "adverse_fill_at_entry": adverse_fill_at_entry,
        "adverse_fill_lab": adverse_fill_lab,
        "price_lab": None,
        "fee_lab_per_contract": None,
        "realized_lab_per_contract": None,
        "sandbox_fee_per_contract": sandbox_fee_pc,
        "sandbox_realized_per_contract": sandbox_realized_pc,
        "settlement": truth,
        "payoff_per_contract": payoff,
        "payoff_source": truth.get("outcome_source"),
        "won_from_settlement": None if payoff is None else payoff > 0.5,
        "won_booked": bool(fill["won"]),
        "sandbox_realized_expected_per_contract": None,
        "sandbox_realized_error_per_contract": None,
        "settlement_sign_mismatch": False,
        "settled": True,
        "note": None,
    }
    # ``quote_recovered`` was the old field name; kept so an existing report
    # diff and the runbook's column stay readable.
    out["quote_recovered"] = quote_reconstructed
    if payoff is None:
        out["note"] = (
            "settlement truth unavailable for this row; the lab payoff cannot be "
            "derived without reading the number under audit -- "
            f"{truth.get('note') or 'no settlement fields on the journal/state row'}"
        )
        return out
    expected = payoff - price_booked - sandbox_fee_pc
    out["sandbox_realized_expected_per_contract"] = expected
    out["sandbox_realized_error_per_contract"] = sandbox_realized_pc - expected
    out["settlement_sign_mismatch"] = abs(sandbox_realized_pc - expected) > REALIZED_TOL
    if quote <= 0.0 or price_lab > MAX_ORDERABLE_PRICE + 1e-12:
        out["note"] = "price_lab off the orderable grid (ev_analysis.adverse_fill_price -> None)"
        return out
    if ts is None:
        out["note"] = "entry_time unparseable; fee regime lookup impossible"
        return out
    fee = float(
        fees_mod.fee_per_contract(
            [price_lab], [ts], series, contracts=contracts, is_maker=False, regime=regime
        )[0]
    )
    out["price_lab"] = price_lab
    out["fee_lab_per_contract"] = fee
    out["realized_lab_per_contract"] = payoff - price_lab - fee
    out["fee_delta_per_contract_sandbox_minus_lab"] = sandbox_fee_pc - fee
    out["realized_delta_per_contract_sandbox_minus_lab"] = (
        sandbox_realized_pc - out["realized_lab_per_contract"]
    )
    return out


# ---------------------------------------------------------------------------
# Lab trade set
# ---------------------------------------------------------------------------
def lab_trade_set(
    frames_dir: str,
    genome_json: Any,
    date_from: date,
    date_to: date,
) -> Dict[str, Any]:
    """The genome's offline trade set on the frozen search frame, restricted to the dates.

    Imports the factory modules lazily (numpy/pyarrow); returns a ``coverage``
    of ``"none"`` with a reason when the frame is absent or ends before the
    range -- the caller then falls back to re-pricing only.
    """
    if not os.path.isdir(frames_dir):
        return {
            "coverage": "none",
            "reason": f"frame dir not found: {frames_dir}",
            "trades": [],
        }
    from src.factory import fitness, gen0
    from src.factory import genome as G

    fs = gen0.load_frameset(Path(frames_dir))
    F = fs.search
    frame_first, frame_last = str(F.dates[0]), str(F.dates[-1])
    if date.fromisoformat(frame_last) < date_from or date.fromisoformat(frame_first) > date_to:
        return {
            "coverage": "none",
            "reason": (
                f"frame {os.path.basename(frames_dir)} covers {frame_first}..{frame_last}; "
                f"requested {date_from.isoformat()}..{date_to.isoformat()} lies outside it"
            ),
            "frame_dates": [frame_first, frame_last],
            "trades": [],
        }
    import numpy as np

    g = G.Genome.from_json(genome_json if not isinstance(genome_json, str) else genome_json)
    in_range = np.asarray(
        [date_from <= date.fromisoformat(str(d)) <= date_to for d in F.dates], dtype=bool
    )
    date_mask = in_range[F.visible["target_date_code"]]
    mask = G.to_mask(g, F)
    res = fitness.score(F, mask, date_mask=date_mask, n_boot=100, constraints=False)
    from src.factory.columns import DIRECTION_LABELS

    trades = []
    for r in res.trade_rows.tolist():
        trades.append(
            {
                "market_ticker": str(F.markets[int(F.visible["market_code"][r])]),
                "target_date": str(F.dates[int(F.visible["target_date_code"][r])]),
                "ts_utc": int(F.visible["ts_utc"][r]),
                "direction": DIRECTION_LABELS[int(F.visible["direction_code"][r])],
                "quote": float(F.visible["quote"][r]),
                "price_paid": float(F.visible["price_paid"][r]),
                "fee_per_contract": float(F.visible["fee_per_contract"][r]),
                "realized_per_contract": float(F.hidden["realized_per_contract"][r]),
            }
        )
    covered_dates = [str(d) for d, ok in zip(F.dates, in_range) if ok]
    partial = (
        date.fromisoformat(frame_first) > date_from or date.fromisoformat(frame_last) < date_to
    )
    return {
        "coverage": "partial" if partial else "full",
        "reason": None
        if not partial
        else f"frame covers {frame_first}..{frame_last}; dates outside it are not scored",
        "frame_dates": [frame_first, frame_last],
        "frame_search_sha256": F.provenance.get("frame_sha256"),
        "dates_scored": covered_dates,
        "n_trades": int(res.trades),
        "realized_mean_per_contract": None if math.isnan(res.realized) else float(res.realized),
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
def _hour_key(iso: Optional[str]) -> Optional[str]:
    """``YYYY-MM-DDTHH`` (UTC) of an ISO timestamp; naive = UTC (the sandbox clock)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H")


def _hour_key_epoch(ts: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _in_range(td: Optional[str], d0: date, d1: date) -> bool:
    if not td:
        return False
    try:
        d = date.fromisoformat(td)
    except ValueError:
        return False
    return d0 <= d <= d1


def _side_dir(contract_side: Any) -> Optional[str]:
    cs = str(contract_side or "").upper()
    return {"YES": "buy_yes", "NO": "buy_no"}.get(cs)


def _key_dict(key: Tuple[Any, ...]) -> Dict[str, Any]:
    return {"market_ticker": key[0], "hour_utc": key[1], "direction": key[2]}


def reconcile(
    *,
    spec: Mapping[str, Any],
    journal_rows: Sequence[Mapping[str, Any]],
    closed_trades: Sequence[Mapping[str, Any]],
    open_positions: Sequence[Mapping[str, Any]],
    rejects: Sequence[Mapping[str, Any]],
    date_from: date,
    date_to: date,
    frames_dir: Optional[str],
    adverse_fill_lab: Optional[float] = None,
    contracts: Optional[int] = None,
    strategy_name: Optional[str] = None,
    regime: Optional[fees_mod.FeeRegime] = None,
    inputs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Pure reconciliation (no file writes); the tests drive this."""
    strategy = strategy_name or strategy_name_for(spec)
    adverse_at_entry = float(spec.get("adverse_fill", 0.01))
    adverse_lab = float(adverse_fill_lab if adverse_fill_lab is not None else adverse_at_entry)
    C = int(contracts if contracts is not None else spec.get("contracts_frame", 20))
    regime = regime or fees_mod.load_regime()
    inputs = dict(inputs or {"mode": "in-process", "obtained": [], "not_obtained": [], "complete": True})

    # ``reconcile_settlement=False`` on purpose: the gate drops a fill whose booked
    # money it cannot reproduce from the row's own settlement fields, because it is
    # deciding a promotion. This tool is doing the opposite job -- it exists to SURFACE
    # those rows, and it runs its own, deeper cross-check below (the strike spec beats
    # a recorded ``settlement_outcome``). Collecting strictly here would delegate the
    # verdict to the gate and report zero fills where the answer is "here is what is
    # wrong with them". Stale NO-side rows still refuse the run; they are counted, not
    # hidden.
    fills, counts = _GATE.collect_settled_trades(
        journal_rows, closed_trades, strategy_name=strategy, reconcile_settlement=False
    )
    fills = [f for f in fills if _in_range(f["target_date"], date_from, date_to)]
    counts["settled_fills_all_dates"] = counts.pop("settled_fills")
    counts["settled_fills"] = len(fills)
    counts["fills_by_source"] = dict(Counter(f["source"] for f in fills))
    counts["fills_by_fee_source"] = dict(Counter(f["fee_source"] for f in fills))

    # The lab set is built FIRST: its quote for a market-hour is the only price
    # in the comparison that the sandbox did not produce (red team F3, defect 2).
    lab = (
        lab_trade_set(frames_dir, spec.get("genome_json"), date_from, date_to)
        if frames_dir
        else {"coverage": "none", "reason": "no --frames given", "trades": []}
    )
    lab_by_key: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    for t in lab["trades"]:
        lab_by_key[(t["market_ticker"], _hour_key_epoch(t.get("ts_utc")), t["direction"])].append(t)

    truths = truth_index(journal_rows, closed_trades)
    sandbox_by_key: Dict[Tuple[Any, ...], List[Mapping[str, Any]]] = defaultdict(list)
    for f in fills:
        sandbox_by_key[
            (f["symbol"], _hour_key(f["entry_time"]), _side_dir(f.get("contract_side")))
        ].append(f)

    repriced = []
    seen: Counter = Counter()
    for f in fills:
        key = (f["symbol"], _hour_key(f["entry_time"]), _side_dir(f.get("contract_side")))
        candidates = lab_by_key.get(key, [])
        frame_row = candidates[seen[key]] if seen[key] < len(candidates) else None
        seen[key] += 1
        row = truths.get(_GATE._join_key(f), {})
        repriced.append(
            reprice_fill(
                f,
                adverse_fill_at_entry=adverse_at_entry,
                adverse_fill_lab=adverse_lab,
                contracts=C,
                regime=regime,
                truth=settlement_truth(f["symbol"], f.get("contract_side"), row),
                frame_row=frame_row,
            )
        )

    pending = []
    #: market-hours the sandbox holds an UNSETTLED position in whose side was
    #: not recorded. A lab trade there was not "skipped" -- it just has not
    #: settled yet, so it must not be reported as an unexplained miss.
    pending_hours: set = set()
    for pos in open_positions:
        if str(pos.get("strategy_name") or "") != strategy:
            continue
        td = _GATE._target_date(pos)
        if not _in_range(td, date_from, date_to):
            continue
        pending.append(
            {
                "symbol": pos.get("symbol"),
                "target_date": td,
                "entry_time": _GATE._iso(pos.get("open_time")),
                "contract_side": pos.get("contract_side"),
                "price_booked": pos.get("entry_price"),
                "quantity": pos.get("quantity"),
                "settled": False,
            }
        )
        hk = _hour_key(pending[-1]["entry_time"])
        d = _side_dir(pos.get("contract_side"))
        if d is not None:
            sandbox_by_key[(pos.get("symbol"), hk, d)].append(pending[-1])
        else:
            pending_hours.add((pos.get("symbol"), hk))

    # Trade-set matching is a BIJECTION on (ticker, decision hour UTC, direction)
    # -- a REJECT at another hour, or a fill on the other side of the same
    # bracket, never "explains" or "matches" a lab trade (red team B, F3), and
    # one sandbox fill never absorbs two lab trades (red team F3, defect 3).
    # A surplus row is a DIRECTION mismatch only when the other book traded that
    # market-hour on the OTHER side and not on this one; when this very
    # direction was traded too, the surplus is a count discrepancy and belongs
    # in lab_only / sandbox_only where the REJECT codes and the flag can speak.
    sandbox_dirs: Dict[Tuple[Any, Any], set] = defaultdict(set)
    for k, v in sandbox_by_key.items():
        if v:
            sandbox_dirs[(k[0], k[1])].add(k[2])
    lab_dirs: Dict[Tuple[Any, Any], set] = defaultdict(set)
    for k, v in lab_by_key.items():
        if v:
            lab_dirs[(k[0], k[1])].add(k[2])

    strat_rejects = [
        r for r in rejects if r["strategy"] == strategy and _in_range(
            _GATE._target_date({"symbol": r["symbol"]}), date_from, date_to
        )
    ]
    rejects_by_symbol_hour: Dict[Tuple[str, Optional[str]], List[str]] = defaultdict(list)
    for r in strat_rejects:
        rejects_by_symbol_hour[(r["symbol"], _hour_key(r.get("ts")))].append(r["reason"])

    lab_only: List[Dict[str, Any]] = []
    sandbox_only: List[Dict[str, Any]] = []
    direction_mismatch: List[Dict[str, Any]] = []
    multiplicity_mismatch: List[Dict[str, Any]] = []
    quantity_mismatch: List[Dict[str, Any]] = []
    n_matched_pairs = 0
    compare_trade_sets = lab["coverage"] != "none"

    if compare_trade_sets:
        for key in sorted(set(lab_by_key) | set(sandbox_by_key), key=lambda k: tuple(str(x) for x in k)):
            lab_rows = lab_by_key.get(key, [])
            box_rows = sandbox_by_key.get(key, [])
            matched = min(len(lab_rows), len(box_rows))
            n_matched_pairs += matched
            # A count discrepancy is only interesting when BOTH sides traded the
            # market-hour: that is precisely what set membership used to hide.
            # A zero on one side is already reported as lab_only / sandbox_only.
            if lab_rows and box_rows and len(lab_rows) != len(box_rows):
                multiplicity_mismatch.append(
                    {
                        **_key_dict(key),
                        "n_lab_trades": len(lab_rows),
                        "n_sandbox_fills": len(box_rows),
                        "surplus": "lab" if len(lab_rows) > len(box_rows) else "sandbox",
                    }
                )
            for lab_row, box_row in zip(lab_rows[:matched], box_rows[:matched]):
                qty = _num(box_row.get("quantity"))
                if qty is not None and abs(qty - C) > 1e-9:
                    quantity_mismatch.append(
                        {
                            **_key_dict(key),
                            "sandbox_quantity": qty,
                            "lab_contracts_assumed": C,
                            "note": "Kelly sizing differs from the frame's fixed C "
                                    "(expected; the per-contract fee differs by the cent-ceiling)",
                            "lab_price_paid": lab_row.get("price_paid"),
                        }
                    )
            hour = (key[0], key[1])
            for lab_row in lab_rows[matched:]:
                if not box_rows and (sandbox_dirs.get(hour, set()) - {key[2]}):
                    direction_mismatch.append(
                        {**lab_row, "flag": "DIRECTION_MISMATCH (sandbox traded the other side at this hour)"}
                    )
                    continue
                if not box_rows and hour in pending_hours:
                    lab_only.append(
                        {
                            **lab_row,
                            "reject_codes": [],
                            "explained": True,
                            "flag": "OPEN_POSITION_UNSETTLED (the sandbox holds a position in "
                                    "this market-hour; its side was not recorded)",
                        }
                    )
                    continue
                codes = rejects_by_symbol_hour.get(hour, [])
                lab_only.append(
                    {
                        **lab_row,
                        "reject_codes": sorted(Counter(codes).items()),
                        "explained": bool(codes),
                        "flag": (
                            "SURPLUS_LAB_TRADE (more lab trades than sandbox fills in this market-hour)"
                            if box_rows
                            else "NOT_TRADED_BY_THE_SANDBOX"
                        ),
                    }
                )
            for box_row in box_rows[matched:]:
                if not lab_rows and (lab_dirs.get(hour, set()) - {key[2]}):
                    flag = "DIRECTION_MISMATCH (lab traded the other side at this hour)"
                elif lab_rows:
                    flag = "SURPLUS_FILL (more sandbox fills than lab trades in this market-hour)"
                else:
                    flag = "NOT_IN_LAB_TRADE_SET (live-path/offline discrepancy)"
                sandbox_only.append(
                    {
                        "symbol": box_row.get("symbol"),
                        "target_date": box_row.get("target_date"),
                        "entry_time": box_row.get("entry_time"),
                        "contract_side": box_row.get("contract_side"),
                        "price_booked": box_row.get("entry_price", box_row.get("price_booked")),
                        "flag": flag,
                    }
                )

    settled_lab = [r for r in repriced if r["realized_lab_per_contract"] is not None]
    sign_mismatch = [r for r in repriced if r["settlement_sign_mismatch"]]
    outcome_disagreement = [r for r in repriced if r["settlement"].get("outcome_disagreement")]
    no_truth = [r for r in repriced if r["payoff_per_contract"] is None]
    price_disagreement = [r for r in repriced if r["price_disagreement"]]
    reconstructed_quotes = [r for r in repriced if r["quote_frame"] is None]
    lab_only_unexplained = [x for x in lab_only if not x["explained"]]

    answered = {
        "repricing": bool(repriced),
        "trade_set": compare_trade_sets,
    }
    blocking: List[str] = []
    if sandbox_only:
        blocking.append(f"{len(sandbox_only)} sandbox fill(s) not in the lab trade set (must be empty)")
    if lab_only_unexplained:
        blocking.append(
            f"{len(lab_only_unexplained)} lab-admissible trade(s) the sandbox skipped with NO "
            "REJECT code logged for that market-hour"
        )
    if multiplicity_mismatch:
        blocking.append(
            f"{len(multiplicity_mismatch)} market-hour(s) where the lab and sandbox trade COUNTS differ"
        )
    if direction_mismatch:
        blocking.append(f"{len(direction_mismatch)} market-hour(s) traded on opposite sides")
    if sign_mismatch:
        blocking.append(
            f"{len(sign_mismatch)} fill(s) whose booked realized contradicts the settlement truth "
            "(payoff - price - fee); a settlement sign error"
        )
    if outcome_disagreement:
        blocking.append(
            f"{len(outcome_disagreement)} fill(s) whose recorded settlement_outcome contradicts "
            "the strike spec at the settled high"
        )
    if price_disagreement:
        blocking.append(
            f"{len(price_disagreement)} fill(s) whose booked price does not reconstruct the frame's quote"
        )
    if counts.get("warnings"):
        blocking.append(f"{len(counts['warnings'])} collector warning(s) (see counts.warnings)")

    refused: List[str] = []
    if counts.get("stale_no_side_rows"):
        refused.append(
            f"{len(counts['stale_no_side_rows'])} stale NO-side settlement row(s) in range: the "
            "booked pnl is the pre-724d93c sign-flipped formula. Run "
            "scripts/repair_no_settlement_pnl.py --apply first; until then no number here is trustworthy."
        )
    for bucket in REFUSING_EXCLUSIONS:
        n = int((counts.get("excluded") or {}).get(bucket, 0))
        if n:
            refused.append(f"{n} row(s) excluded as {bucket}: they could not be verified either way")
    if not answered["repricing"] and not answered["trade_set"]:
        refused.append(
            "VACUOUS: 0 sandbox fills and no lab trade set for these dates, so neither question "
            "was asked. An empty report is not parity evidence"
            + (f" ({lab.get('reason')})" if lab.get("reason") else "")
        )

    if blocking:
        verdict, exit_code = "DISCREPANCY", 1
    elif refused:
        verdict, exit_code = "REFUSED", 3
    elif all(answered.values()) and inputs.get("complete", True):
        verdict, exit_code = "OK", 0
    else:
        verdict, exit_code = "PARTIAL", 0

    summary = {
        "verdict": verdict,
        "exit_code": exit_code,
        "blocking": blocking,
        "refused_reasons": refused,
        "questions_answered": answered,
        "vacuous": not answered["repricing"] and not answered["trade_set"],
        "inputs_complete": bool(inputs.get("complete", True)),
        "n_sandbox_fills_settled": len(fills),
        "n_sandbox_pending": len(pending),
        "sandbox_net_pnl": math.fsum(f["net_pnl"] for f in fills),
        "sandbox_realized_per_contract_mean": (
            math.fsum(r["sandbox_realized_per_contract"] for r in repriced) / len(repriced)
            if repriced
            else None
        ),
        "lab_repriced_realized_per_contract_mean": (
            math.fsum(r["realized_lab_per_contract"] for r in settled_lab) / len(settled_lab)
            if settled_lab
            else None
        ),
        "n_payoff_from_strike_spec": sum(
            1 for r in repriced if r["payoff_source"] and r["payoff_source"].startswith("strike_spec")
        ),
        "n_payoff_from_recorded_outcome": sum(
            1 for r in repriced if r["payoff_source"] and r["payoff_source"].startswith("settlement_outcome")
        ),
        "n_payoff_unavailable": len(no_truth),
        "n_settlement_sign_mismatch": len(sign_mismatch),
        "n_settlement_outcome_disagreement": len(outcome_disagreement),
        "n_quote_from_frame": len(repriced) - len(reconstructed_quotes),
        "n_quote_reconstructed": len(reconstructed_quotes),
        "n_price_disagreements": len(price_disagreement),
        "n_lab_trades": len(lab["trades"]),
        "n_lab_only": len(lab_only),
        "n_lab_only_explained_by_reject": sum(1 for x in lab_only if x["explained"]),
        "n_lab_only_unexplained": len(lab_only_unexplained),
        "n_sandbox_only": len(sandbox_only),
        "n_direction_mismatch": len(direction_mismatch),
        "n_multiplicity_mismatch": len(multiplicity_mismatch),
        "n_quantity_mismatch": len(quantity_mismatch),
        "n_matched_pairs": n_matched_pairs,
        "match_key": "(market_ticker, decision hour UTC, direction), matched as a bijection",
        "sandbox_subset_of_lab": (
            None if not compare_trade_sets else len(sandbox_only) == 0
        ),
        "reject_profile": dict(sorted(Counter(r["reason"] for r in strat_rejects).items())),
        "n_reject_lines_strategy": len(strat_rejects),
        "n_reject_lines_total": len(rejects),
    }
    return {
        "genome_id": spec.get("genome_id"),
        "strategy_name": strategy,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "formula": {
            "quote": "the frame's own quote for the market-hour; only when the frame has no "
                     "such row does it fall back to price_booked - adverse_fill_at_entry "
                     "(flagged per fill in quote_source, and NOT independent)",
            "price_lab": "quote + adverse_fill_lab",
            "payoff": "1.0 iff the held side settled in the money, from settlement_spec + "
                      "settlement_high (cross-checked against settlement_outcome); never from won/net_pnl",
            "fee_lab": (
                f"src.factory.fees.fee_per_contract(price_lab, entry_ts, series, "
                f"contracts={C}, is_maker=False)"
            ),
            "realized_lab_per_contract": "payoff - price_lab - fee_lab (held to settlement)",
            "sandbox_realized_per_contract": "(pnl - entry_fee) / quantity at actual quantity",
            "sandbox_realized_expected_per_contract": "payoff - price_booked - sandbox_fee "
                                                      "(the same settlement truth, re-deriving the booked number)",
            "pnl_source": "closed_trades / journal, never equity",
        },
        "parameters": {
            "adverse_fill_at_entry": adverse_at_entry,
            "adverse_fill_lab": adverse_lab,
            "contracts_frame": C,
            "fee_regime_sha256": regime.sha256,
            "frames_dir": frames_dir,
            "quote_tolerance": QUOTE_TOL,
            "realized_tolerance_per_contract": REALIZED_TOL,
        },
        "inputs": dict(inputs),
        "summary": summary,
        "counts": counts,
        "repriced_fills": repriced,
        "pending_positions": pending,
        "lab_trade_set": {k: v for k, v in lab.items() if k != "trades"},
        "lab_only": lab_only,
        "sandbox_only": sandbox_only,
        "direction_mismatch": direction_mismatch,
        "multiplicity_mismatch": multiplicity_mismatch,
        "quantity_mismatch": quantity_mismatch,
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
_VERDICT_BANNER = {
    "OK": "**OK** -- both questions were asked and nothing disagreed.",
    "PARTIAL": "**PARTIAL** -- nothing disagreed, but at least one question could not be asked, or "
               "an input could not be obtained. Read '## Verdict' and '## Inputs' "
               "before treating it as parity evidence.",
    "DISCREPANCY": "**DISCREPANCY** -- the sandbox record and the lab disagree. See '## Verdict'.",
    "REFUSED": "**REFUSED** -- this run reached no conclusion. It is NOT a pass. See '## Verdict'.",
}


def _fmt(value: Any, spec: str = "") -> str:
    if value is None:
        return "n/a"
    if spec and isinstance(value, (int, float)):
        return format(value, spec)
    return str(value)


def render_markdown(rep: Mapping[str, Any]) -> str:
    s = rep["summary"]
    lab = rep["lab_trade_set"]
    counts = rep.get("counts") or {}
    inputs = rep.get("inputs") or {}
    lines = [
        f"# Paper reconcile {rep['date_from']} .. {rep['date_to']} -- {rep['strategy_name']}",
        "",
        _VERDICT_BANNER.get(s["verdict"], s["verdict"]),
        "",
        f"Genome `{rep['genome_id']}`; adverse_fill at entry {rep['parameters']['adverse_fill_at_entry']}, "
        f"lab {rep['parameters']['adverse_fill_lab']}; C={rep['parameters']['contracts_frame']} taker; "
        f"fee regime `{rep['parameters']['fee_regime_sha256'][:12]}`.",
        "",
        "## Verdict",
        "",
        f"- verdict: **{s['verdict']}** (exit {s['exit_code']})",
        f"- questions answered: re-pricing {s['questions_answered']['repricing']}, "
        f"trade set {s['questions_answered']['trade_set']}",
    ]
    if s["blocking"]:
        lines.append("- blocking:")
        lines += [f"  - {b}" for b in s["blocking"]]
    else:
        lines.append("- blocking: (none)")
    if s["refused_reasons"]:
        lines.append("- refused because:")
        lines += [f"  - {r}" for r in s["refused_reasons"]]
    lines += ["", "## Inputs", ""]
    lines.append(f"- mode: {inputs.get('mode', 'in-process')}"
                 + (f" ({inputs.get('base_url')})" if inputs.get("base_url") else ""))
    for row in inputs.get("obtained") or []:
        lines.append(f"- obtained **{row['input']}** from `{row['source']}` -- {row['detail']}")
    if inputs.get("not_obtained"):
        lines += ["", "### What this run could not obtain", ""]
        for row in inputs["not_obtained"]:
            lines.append(f"- **{row['input']}** -- {row['why']}")
            lines.append(f"  - consequence: {row['consequence']}")
    lines += ["", "## Input integrity (collector counts)", ""]
    lines += [
        f"- journal rows {counts.get('journal_rows')}, closed_trades {counts.get('closed_trades')}",
        f"- settled fills: {counts.get('settled_fills')} in range "
        f"(of {counts.get('settled_fills_all_dates')} on all dates)",
        f"- by source: {counts.get('fills_by_source')}",
        f"- by fee source: {counts.get('fills_by_fee_source')}",
    ]
    excluded = counts.get("excluded") or {}
    lines.append(f"- excluded rows: {excluded if excluded else '(none)'}")
    stale = counts.get("stale_no_side_rows") or []
    lines.append(f"- stale NO-side settlement rows: {len(stale)}")
    for row in stale:
        lines.append(f"  - {row.get('symbol')} {row.get('entry_time')} ({row.get('source')})")
    warns = counts.get("warnings") or []
    lines.append(f"- collector warnings: {len(warns)}")
    for w in warns:
        lines.append(f"  - {w}")
    maker = counts.get("maker_booked_fills") or []
    lines.append(f"- maker-booked fills: {len(maker)}")
    for m in maker:
        lines.append(f"  - {m.get('symbol')} {m.get('entry_time')}")

    lines += [
        "",
        "## Re-pricing (sandbox fills through the lab formula)",
        "",
        f"- settled fills: {s['n_sandbox_fills_settled']}; pending (open) positions: {s['n_sandbox_pending']}",
        f"- sandbox net PnL (closed_trades pnl - entry_fee): {s['sandbox_net_pnl']:+.2f}",
        f"- mean realized/contract: sandbox {s['sandbox_realized_per_contract_mean']}, "
        f"lab re-priced {s['lab_repriced_realized_per_contract_mean']}",
        f"- payoff derived from: strike spec {s['n_payoff_from_strike_spec']}, "
        f"recorded settlement_outcome {s['n_payoff_from_recorded_outcome']}, "
        f"unavailable {s['n_payoff_unavailable']}",
        f"- settlement sign mismatches: {s['n_settlement_sign_mismatch']}; "
        f"outcome-vs-strike-spec disagreements: {s['n_settlement_outcome_disagreement']}",
        f"- quote source: frame {s['n_quote_from_frame']}, reconstructed "
        f"{s['n_quote_reconstructed']} (a reconstructed quote cannot disagree with the booked "
        f"price -- it is derived from it); price disagreements {s['n_price_disagreements']}",
        "",
        "| symbol | target_date | side | qty | booked | quote | quote src | price_lab | "
        "payoff | fee sandbox | fee lab | realized sandbox | expected | realized lab |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rep["repriced_fills"]:
        lines.append(
            f"| {r['symbol']} | {r['target_date']} | {r['contract_side']} | {r['quantity']:.0f} | "
            f"{r['price_booked']:.2f} | {_fmt(r['quote_used'], '.2f')} | "
            f"{'frame' if r['quote_frame'] is not None else 'reconstructed'} | "
            f"{_fmt(r['price_lab'], '.4f')} | {_fmt(r['payoff_per_contract'], '.2f')} | "
            f"{r['sandbox_fee_per_contract']:.4f} | {_fmt(r['fee_lab_per_contract'], '.4f')} | "
            f"{r['sandbox_realized_per_contract']:+.4f} | "
            f"{_fmt(r['sandbox_realized_expected_per_contract'], '+.4f')} | "
            f"{_fmt(r['realized_lab_per_contract'], '+.4f')} |"
        )
    bad = [r for r in rep["repriced_fills"] if r["settlement_sign_mismatch"]
           or r["settlement"].get("outcome_disagreement") or r["payoff_per_contract"] is None]
    if bad:
        lines += ["", "**Settlement-truth problems:**", ""]
        for r in bad:
            note = r["settlement"].get("note") or r["note"] or ""
            lines.append(
                f"- {r['symbol']} {r['target_date']} {r['contract_side']}: booked "
                f"{r['sandbox_realized_per_contract']:+.4f}/contract, settlement truth says "
                f"{_fmt(r['sandbox_realized_expected_per_contract'], '+.4f')} "
                f"(outcome {r['settlement'].get('outcome_used')} via "
                f"{r['settlement'].get('outcome_source')}) -- {note}"
            )

    lines += ["", "## Lab trade set", ""]
    if lab["coverage"] == "none":
        lines.append(f"**Frame does not cover the dates**: {lab['reason']}. Re-pricing only; "
                     "the REJECT profile below is still taken from the runtime log. "
                     "The trade-set question was NOT answered.")
    else:
        lines.append(
            f"coverage {lab['coverage']} (frame {lab['frame_dates'][0]}..{lab['frame_dates'][1]}); "
            f"lab trades {s['n_lab_trades']}; matched pairs {s['n_matched_pairs']}; lab-only {s['n_lab_only']} "
            f"({s['n_lab_only_explained_by_reject']} explained by a REJECT line, "
            f"{s['n_lab_only_unexplained']} UNEXPLAINED); "
            f"sandbox-only {s['n_sandbox_only']}; direction mismatches {s.get('n_direction_mismatch', 0)}; "
            f"multiplicity mismatches {s['n_multiplicity_mismatch']}; "
            f"quantity mismatches {s['n_quantity_mismatch']}; "
            f"sandbox subset of lab: {s['sandbox_subset_of_lab']} (match key {s.get('match_key')})"
        )
        if rep["lab_only"]:
            lines += ["", "| lab market | target_date | direction | price_paid | REJECT codes |", "|---|---|---|---|---|"]
            for t in rep["lab_only"]:
                codes = ", ".join(f"{c}x{n}" for c, n in t["reject_codes"]) or "(none logged)"
                lines.append(
                    f"| {t['market_ticker']} | {t['target_date']} | {t['direction']} | "
                    f"{t['price_paid']:.2f} | {codes} |"
                )
        if rep["sandbox_only"]:
            lines += ["", "**sandbox \\ lab (must be empty):**", ""]
            for x in rep["sandbox_only"]:
                lines.append(f"- {x['symbol']} {x['target_date']} booked {x['price_booked']} -- {x['flag']}")
        if rep["multiplicity_mismatch"]:
            lines += ["", "**multiplicity (a market-hour is a bijection, not set membership):**", ""]
            for m in rep["multiplicity_mismatch"]:
                lines.append(
                    f"- {m['market_ticker']} {m['hour_utc']} {m['direction']}: "
                    f"lab {m['n_lab_trades']} vs sandbox {m['n_sandbox_fills']} "
                    f"(surplus on the {m['surplus']} side)"
                )
        if rep["quantity_mismatch"]:
            lines += ["", "**quantity (sandbox Kelly size vs the frame's fixed C):**", ""]
            for q in rep["quantity_mismatch"]:
                lines.append(
                    f"- {q['market_ticker']} {q['hour_utc']} {q['direction']}: "
                    f"sandbox {q['sandbox_quantity']:.0f} vs lab {q['lab_contracts_assumed']}"
                )
    lines += [
        "",
        "## REJECT profile for the strategy in range",
        "",
        f"{s['n_reject_lines_strategy']} lines for `{rep['strategy_name']}` "
        f"(of {s['n_reject_lines_total']} REJECT lines parsed):",
        "",
    ]
    for code, n in s["reject_profile"].items():
        lines.append(f"- {code}: {n}")
    if not s["reject_profile"]:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--promoted", required=True, help="promoted spec JSON")
    ap.add_argument("--journal", default="data/trade_journal.jsonl")
    ap.add_argument("--state", default=None, help="exchange_state.json")
    ap.add_argument("--log", nargs="*", default=[], help="runtime log path(s)/globs")
    ap.add_argument(
        "--url",
        nargs="?",
        const=DEFAULT_URL,
        default=None,
        help=(
            "read the sandbox over HTTP instead of the filesystem "
            f"(default {DEFAULT_URL}). The exchange state has no endpoint; the "
            "report lists what could not be obtained and what that costs."
        ),
    )
    ap.add_argument("--http-timeout", type=float, default=15.0)
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD target_date")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD target_date")
    ap.add_argument("--frames", default=DEFAULT_FRAMES_DIR)
    ap.add_argument("--no-frame", action="store_true", help="skip the lab trade set")
    ap.add_argument("--adverse-fill", type=float, default=None, help="lab allowance (default: spec)")
    ap.add_argument("--contracts", type=int, default=None, help="lab order size (default: spec/20)")
    ap.add_argument("--strategy", default=None, help="override 'Genome <id8>'")
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "reports", "factory"))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        spec = load_spec(args.promoted)
        d0 = date.fromisoformat(args.date_from)
        d1 = date.fromisoformat(args.date_to)
        if d1 < d0:
            raise ReconcileError("--to precedes --from")
        if args.url:
            got = gather_inputs_http(args.url, timeout=args.http_timeout)
        else:
            got = gather_inputs_files(args.journal, args.state, args.log)
    except (_GATE.GateError, ReconcileError, ValueError) as exc:
        print(f"factory_paper_reconcile: {exc}", file=sys.stderr)
        return 2
    rep = reconcile(
        spec=spec,
        journal_rows=got["journal_rows"],
        closed_trades=got["closed_trades"],
        open_positions=got["open_positions"],
        rejects=got["rejects"],
        date_from=d0,
        date_to=d1,
        frames_dir=None if args.no_frame else args.frames,
        adverse_fill_lab=args.adverse_fill,
        contracts=args.contracts,
        strategy_name=args.strategy,
        inputs=got["inputs"],
    )
    out_dir = Path(args.out_dir)
    stem = f"paper_reconcile_{d0.isoformat()}_{d1.isoformat()}"
    write_json(out_dir / f"{stem}.json", rep)
    write_text(out_dir / f"{stem}.md", render_markdown(rep))
    s = rep["summary"]
    print(
        f"{stem}: {s['verdict']} fills={s['n_sandbox_fills_settled']} "
        f"pending={s['n_sandbox_pending']} lab={s['n_lab_trades']} "
        f"lab_only={s['n_lab_only']} (unexplained {s['n_lab_only_unexplained']}) "
        f"sandbox_only={s['n_sandbox_only']} multiplicity={s['n_multiplicity_mismatch']} "
        f"sign_mismatch={s['n_settlement_sign_mismatch']} "
        f"coverage={rep['lab_trade_set']['coverage']} -> {out_dir / (stem + '.json')}"
    )
    for line in s["blocking"] + s["refused_reasons"]:
        print(f"  ! {line}", file=sys.stderr)
    return int(s["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
