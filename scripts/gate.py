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

That merge is NOT free, and since 2026-09-05 the gate says so instead of leaving
it implicit (F3 review round 2, should-fix 1). Two brackets on ONE city-day
ladder are mutually EXCLUSIVE, which is the structure the within-unit null is
built on and attains. Two CITIES' brackets are not mutually exclusive at all --
both can settle YES -- so on a cross-city date the premise the null leans on
does not hold, and four cities' fills on one day are scored as one trial rather
than four. The verdict therefore reports ``units.cross_city_units``,
``units.n_units_if_grouped_by_city_day`` and
``units.units_lost_to_cross_city_merging`` (the power cost, in trials), raises a
``cross_city_units_merged`` warning, and prints the loss on the ``--quiet``
line. It does NOT redefine the unit: ``target_date`` is pre-registered by FR-5.2
and PRD_STRATEGY_FACTORY FR-F3.4, and changing a pre-registered unit mid-run is
the owner's call, not the gate's. This is an open governance question, reported
so the call can be made on numbers.

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

THE NULL -- least favourable over the unknown dependence (2026-09-05, F3 review defect 1)
-----------------------------------------------------------------------------------------
Under the null each fill ``i`` wins with its OWN breakeven probability
``q*_i = p_i + f_i``. That fixes the MARGINALS. It does not fix the JOINT law,
and the joint law is exactly what the independence unit exists to absorb: the
fills inside one ``target_date`` are brackets on a city-day ladder (disjoint
brackets on ONE ladder cannot both settle YES -- they are mutually EXCLUSIVE,
the opposite of independent) or brackets on several cities' ladders for the same
day (dependent through synoptic weather, with no copula anyone can defend).

Until 2026-09-05 this function multiplied the marginals as if the fills were
independent Bernoullis. That is not conservative, it is anti-conservative in the
case that actually occurs: two mutually exclusive brackets at ``q* = 0.417``
give a true unit win probability of ``q*_1 + q*_2 = 0.834`` (exactly one wins,
and one win pays for one loss), where the independent model reported
``1 - (1 - q*)^2 = 0.660``. Modelling ``w_u`` too LOW makes ``P[K >= k]`` too
small, so the gate passed far too easily: on a record of 50 mutually exclusive
pairs the measured TRUE-null PASS rate was 88.5% against a nominal 5%.

The gate therefore models each unit at the LEAST FAVOURABLE joint law consistent
with the marginals -- the largest ``w_u`` any dependence structure could
produce. Writing ``S(A) = sum_{i in A} qty_i`` and ``T = sum_i (p_i + f_i) qty_i``,
the unit's PnL is ``S(A) - T`` when exactly the fills in ``A`` win, so

    unit wins  <=>  S(A) > T

which is a monotone (upward-closed) event. Let ``A_1..A_r`` be its MINIMAL
winning sets. Then, for ANY joint law with the given marginals,

    w_u = P[union_j {every fill in A_j wins}]
        <= sum_j P[every fill in A_j wins]      (union bound)
        <= sum_j min_{i in A_j} q*_i            (Frechet-Hoeffding)

and the gate uses ``w_u = min(1, sum_j min_{i in A_j} q*_i)``. Properties:

* one fill per unit -> the single minimal set ``{i}`` -> ``w_u = q*_i`` exactly,
  and the test below reduces to the plain binomial. Nothing changes for the
  one-fill-per-date records the pre-registration was written for.
* two disjoint brackets on one ladder, either of which pays for the other ->
  minimal sets ``{1}, {2}`` -> ``w_u = q*_1 + q*_2``, which mutual exclusivity
  ATTAINS. The bound is tight on the structure the unit exists for.
* extreme price pairs where only a both-win date is profitable -> the single
  minimal set ``{1, 2}`` -> ``w_u = min(q*_1, q*_2)``, just above the
  independent product and still an upper bound.
* when the minimal sets are numerous enough that the sum reaches 1 (e.g. three
  brackets at ``q* = 0.417``: ``3 q* = 1.25``), ``w_u`` SATURATES at 1. A unit
  that always wins under the null is not evidence, and the gate says so:
  ``units_with_saturated_null`` in the verdict. Such a record cannot reach a
  small p, which is the honest outcome -- a date carrying several brackets is
  not one clean trial, and the fix is to trade one bracket per date, not to
  assume a copula.
* saturation is the EXTREME case and fires far too late to be the safeguard
  (F3 review round 2, should-fix 3): a unit is evidence-free well before
  ``w_u`` reaches 1. Two brackets at ``q* = 0.47``, or three cities at
  ``q* = 0.30``, already win under the null nine times in ten. The verdict
  therefore also carries ``units_with_degenerate_null`` -- units at or above
  ``NULL_DEGENERATE_W`` (0.90) -- with their dates, a warning, and a
  ``--quiet`` line entry. It is reported, not gating: the Poisson-binomial
  already handles such units correctly (they simply carry no information), so
  the honest response is to make the operator SEE the regime, not to invent a
  second threshold on top of ``alpha``.

Raising ``w_u`` can only raise ``P[K >= k]``, so a bound that is loose costs
power and never validity. ``unit_null_win_probability_independent`` keeps the
old independent enumeration per unit as a NON-gating diagnostic so a reader can
see how much the dependence allowance is worth on this record.

Two assumptions survive and are stated rather than hidden: units (settlement
days) are still treated as independent of one another -- that is the
pre-registered ``grouping_unit`` and it is not relitigated here -- and the
marginals ``q*_i`` are taken at the fill's own entry price. The pooled ``q_bar``
binomial is kept as ``p_pooled_qbar_secondary``, labelled an approximation and
NON-gating.

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
is how a 72-hour streak turned into a promotion in the crypto era. The same
``refused`` flag carries the other "this record cannot be gated as it stands"
findings below.

SETTLEMENT RECONCILIATION (2026-09-05, F3 review defect 2)
----------------------------------------------------------
Every admitted fill's booked money is recomputed from the row's OWN settlement
fields and must agree, because the number under audit may not vouch for itself:

    payoff        = 1 if settlement_outcome names the side held else 0
                    (contract_side YES pays on "yes", NO pays on "no")
    exit_price    == payoff
    pnl           == (payoff - entry_price) * quantity - exit_fee   (buy)
                     (entry_price - payoff) * quantity - exit_fee   (sell)
    direction     settlement_outcome == bracket_payoff.settles_yes(spec, high)

Any disagreement REFUSES the whole record (exit 3) -- it is corruption, and
which fills it touched is not knowable from inside. This is deliberately not
limited to the pre-724d93c NO-side formula: a mis-repair by
``scripts/repair_no_settlement_pnl.py`` writes the ``repaired_no_side_settlement``
marker too, and the marker is no longer taken as proof of anything. A row whose
outcome or strike spec is missing cannot be reconciled either way; it is
EXCLUDED and counted against the exclusion budget below, never assumed good.

EXCLUSION BUDGET (2026-09-05, F3 review defect 3)
-------------------------------------------------
``net_pnl``, the unit table and ``k_wins`` are computed over ADMITTED fills only,
so anything that quietly drops a fill improves every gating number at once.
Degrading the losing fills is therefore a free PASS unless the drops are
themselves gated. The gate splits the exclusions in two:

    scope       other_strategy, outside_market_family, non_weather_symbol,
                not_settled, settlement_unresolved, duplicate_journal_row
                -- reported, not counted: these are filters, not damage
    quality     no_exit_price, exit_price_off_grid, no_target_date,
                missing_numeric_field, non_positive_quantity,
                no_side_outcome_unverifiable, settlement_outcome_missing,
                strike_spec_unverifiable -- in-scope settled fills the gate
                could not read

``excluded_rate = quality / (admitted + quality)`` is a gating condition and,
above ``MAX_EXCLUDED_RATE`` (2%), a refusal. Every dropped row is listed in the
verdict under ``counts.excluded_rows`` so the holes are auditable rather than a
bare counter.

The budget was still HALF-CLOSED until 2026-09-05 (F3 review round 2, BLOCKING
1). Charging only the quality reasons left every SCOPE filter as a free
deletion, and each of them fires on ONE field of ONE row. Writing
``settlement_error`` onto one losing leg of each two-fill unit -- a free-text
field, on rows that stayed fully readable -- took a record from FAIL
(p=0.22795, net 59.60) to PASS (p=0.02904, net 143.00) with ``excluded_rate``
still 0.0000, ``quality_excluded`` 0, ``refused`` false and ``n_units`` intact
at 50. Worse, the least-favourable null made the reward BIGGER than the
superseded independent model did: stripping a fill from a two-fill unit drops
``w_u`` from 0.834 to 0.417, against 0.660 to 0.417 before.

So every scope filter that fires on a row of THIS strategy is now cross-checked
before it is honoured. ``settlement_payoff_check`` is asked whether the row is a
settled fill after all; returning ``None`` means exit_price IS the settled
payoff, pnl matches it, and the strike spec re-derives the recorded outcome. A
row that reconciles exactly while claiming to be out of scope contradicts
itself, and which claim is false is not knowable from inside, so it REFUSES the
record (``unresolved_row_reconciles``, ``unsettled_row_reconciles``,
``out_of_family_row_reconciles``, ``non_weather_row_reconciles``) exactly as a
contradicted payoff does. A genuinely unresolved row does not reconcile -- its
exit price is a mark, not a settled payoff -- so the honest filter still costs
nothing. ``other_strategy`` is the one filter with no cross-check (nothing on a
fill names the strategy but the field itself, and another strategy's fill
reconciles perfectly well); it is defended instead by the join key, which
CONTAINS strategy_name, so a name rewritten in one file leaves the other file's
row under its original key where the twin rule below still admits it.

``thresholds.max_excluded_rate`` may only TIGHTEN the budget. It used to be
overridable upward with nothing gating the override, and a registration that
can dial away its own guard is not a guard (should-fix 4): a looser value is
rejected, ``MAX_EXCLUDED_RATE`` stands, and the verdict records the attempt
under ``conditions.excluded_rate_within_bound.registration_override_rejected``.

PREFER THE READABLE TWIN (2026-09-05, F3 review round 2, should-fix 2)
----------------------------------------------------------------------
The journal and ``closed_trades`` hold the same fill under one join key, and the
``seen`` key set that stopped one fill being charged to the budget twice also
DELETED a fill whose journal row was unreadable and whose state twin was
complete. A key whose journal row was excluded is therefore retried against its
twin: when the twin admits and reconciles the journal's charge is RETRACTED
(source ``closed_trades_over_unreadable_journal``), and when it does not, the
twin's own charge is rolled back so the fill is still counted exactly once.

TARGET DATE (2026-09-05, F3 review defect 4)
---------------------------------------------
``target_date`` is the unit key: a row whose label is wrong splits one city-day
ladder into several "independent" units and inflates the ``n_units >= n_min``
count that gates everything. The gate no longer trusts the string. It parses it
as a calendar date, normalises it to ``YYYY-MM-DD`` (so ``2026-06-01`` and
``2026-06-01T00:00:00`` cannot become two units) and cross-checks it against
``settlement_date_for(symbol)``, the ticker's own event-date label. An
unparseable label or a label that disagrees with the ticker REFUSES the record.

REGISTRATION TIME (2026-09-05, F3 review defect 6)
---------------------------------------------------
``registration_commit_utc`` is typed by hand into the registration JSON, so on
its own it proves nothing about when the registration was committed. The gate
reconciles it against ``git log --diff-filter=A --format=%cI -- <registration>``
(the newest ADD of that path, which is the latest instant the file could have
started existing) and REFUSES when the two disagree by more than a second. When
git cannot answer -- no repo, untracked file, no git binary -- the value stays
UNVERIFIED and the condition FAILS unless ``--allow-unverified-registration``
downgrades it to a reported, non-gating line for dry runs.

REALISTIC FILLS (2026-09-05, F3 review defect 5)
-------------------------------------------------
``requires_realistic_fills`` was a no-op: the exchange state has never
serialised a ``realistic_fills`` key, so the condition resolved to None, dropped
out of the gating list into ``not_applicable``, and the verdict still said PASS.
It now REFUSES: a registration that requires realistic fills and a record that
cannot evidence them is a record the gate will not score. ``--realistic-fills
true|false`` is the operator's explicit, recorded assertion; silence is not one.

USAGE
-----
    python scripts/gate.py --journal data/trade_journal.jsonl \
        --state data/exchange_state.json \
        --registration configs/factory/gate_registration.json \
        --out reports/factory/gate_<genome_id>.json

Exit codes: 0 PASS, 1 FAIL, 2 usage / input error, 3 refused. The gate refuses
rather than scoring when the record cannot be gated as it stands:

    n_units < n_min                     underpowered (verdict, ``refused: true``)
    excluded_rate > max_excluded_rate   too much of the sample is unreadable
    requires_realistic_fills unevidenced
    registration_commit_utc contradicted by git
    stale NO-side settlement rows       (GateRefusal, before any p is computed)
    rows whose booked money or unit key contradicts their settlement fields
    rows filtered out of SCOPE that nonetheless reconcile as settled fills

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
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date as _date
from datetime import datetime, timezone
from fractions import Fraction
from itertools import product
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.bracket_payoff import BracketSpecError, parse_bracket_spec, settles_yes  # noqa: E402
from src.core.fee_calculator import compute_fee, fee_type_for_symbol  # noqa: E402
from src.core.weather_settlement import (  # noqa: E402
    city_key_for_station,
    settlement_date_for,
    settlement_station_for,
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
#: booked pnl must match the payoff recomputed from the row's settlement fields.
_PNL_TOL = 1e-6
#: a unit with more fills than this is refused (2^m enumeration), never approximated.
MAX_FILLS_PER_UNIT = 16
#: above this many fills the NON-gating independent diagnostic is skipped (2^m in Fractions).
MAX_FILLS_FOR_INDEPENDENT_DIAGNOSTIC = 10
#: the journal/state marker scripts/repair_no_settlement_pnl.py writes.
REPAIRED_MARKER = "repaired_no_side_settlement"
#: default ceiling on quality_excluded / (admitted + quality_excluded).
#: 2% is one dropped fill in fifty: a gate whose sample can be trimmed further
#: than that by data damage is not measuring the strategy any more.
MAX_EXCLUDED_RATE = 0.02

#: in-scope settled fills the gate could not READ. These count against the
#: exclusion budget: dropping them improves every gating number at once.
QUALITY_EXCLUSION_REASONS = (
    "no_exit_price",
    "exit_price_off_grid",
    "no_target_date",
    "missing_numeric_field",
    "non_positive_quantity",
    "no_side_outcome_unverifiable",
    "settlement_outcome_missing",
    "strike_spec_unverifiable",
)

#: SCOPE filters that fire on a row ALREADY matched to the registered strategy.
#: Each is one per-row field an operator or a corrupted writer can flip, and a
#: scope drop is deliberately NOT charged to the exclusion budget -- so each was
#: a free deletion of an inconvenient fill (F3 review round 2, BLOCKING 1). Each
#: is now cross-checked against ``settlement_payoff_check`` before it is
#: honoured, and the value is the refusal code a reconciling row earns instead.
CROSS_CHECKED_SCOPE_REASONS = {
    "settlement_unresolved": "unresolved_row_reconciles",
    "not_settled": "unsettled_row_reconciles",
    "outside_market_family": "out_of_family_row_reconciles",
    "non_weather_symbol": "non_weather_row_reconciles",
}

#: a unit whose least-favourable null win probability reaches this carries next
#: to no evidence: under the null it wins almost always, so winning it is not a
#: result. Saturation (w_u = 1) is the extreme case and fires far too late --
#: two brackets at q* = 0.47, or three cities at q* = 0.30, are already here.
NULL_DEGENERATE_W = 0.90

FORMULAS: Dict[str, str] = {
    "breakeven_per_fill": (
        "q* = entry_price + entry_fee_per_contract; from "
        "q*(1 - p - f) - (1 - q*)(p + f) = 0 for a binary bought at p with entry fee f "
        "per contract, held to settlement (payout 1, no settlement fee)"
    ),
    "unit_null_win_probability": (
        "w_u = min(1, sum over the unit's MINIMAL winning fill sets A of min_{i in A} q*_i): "
        "the largest unit win probability any joint law with marginals q*_i could produce "
        "(union bound over the monotone win event, Frechet-Hoeffding on each minimal set). "
        "One fill per unit gives exactly q*_i; two mutually exclusive brackets give q*_1 + q*_2, "
        "which the ladder structure attains. Independence is NOT assumed -- brackets on one "
        "city-day ladder are mutually exclusive, and modelling them as independent understated "
        "w_u and made the test anti-conservative (F3 review defect 1)"
    ),
    "unit_null_independent_diagnostic": (
        "NON-GATING: the superseded independent model, P[sum_i (won_i - p_i - f_i) qty_i > 0] "
        "with won_i ~ Bernoulli(q*_i) independent, by exact enumeration of the unit's 2^m "
        "fill outcomes; reported so the dependence allowance is visible"
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
    "payoff_reconciliation": (
        "payoff = 1 if settlement_outcome names the held side else 0; exit_price == payoff and "
        "pnl == (payoff - entry_price) * quantity - exit_fee (sign-flipped on a sell); the "
        "direction is re-derived from the strike spec and the settled high. Any disagreement "
        "refuses the record -- the booked number never vouches for itself"
    ),
    "exclusion_rate": (
        "quality_excluded / (admitted + quality_excluded) over IN-SCOPE settled fills the gate "
        "could not read; scope filters (other strategy, other family, unsettled) are not counted, "
        "but every scope filter that fires on a row of THIS strategy is first cross-checked "
        "against settlement_payoff_check, and a row that reconciles while claiming to be out "
        "of scope refuses the record. The ceiling is MAX_EXCLUDED_RATE; a registration may "
        "tighten it and may never loosen it"
    ),
    "cross_city_units": (
        "FR-5.2 fixes the independence unit at target_date ALONE, so fills on several cities' "
        "ladders on one date collapse into ONE unit. Reported, never silently redefined: "
        "n_units_if_grouped_by_city_day counts the (city, target_date) pairs the record actually "
        "holds, and units_lost_to_cross_city_merging is the power the pre-registered unit costs"
    ),
    "target_date": (
        "parsed as a calendar date, normalised to YYYY-MM-DD, and cross-checked against "
        "weather_settlement.settlement_date_for(symbol); a disagreement refuses the record"
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


def _unit_terms(
    fills: Sequence[Mapping[str, Any]],
) -> Tuple[List[Fraction], List[Fraction], Fraction]:
    """``(q*_i clipped, qty_i, T)`` in exact rationals.

    ``T = sum_i (p_i + f_i) qty_i`` is the unit's total cost including entry
    fees, so the unit is profitable exactly when the winning fills' quantity
    exceeds it: PnL(A) = ``sum_{i in A} qty_i - T``. The probability uses the
    clipped ``q*``; the PnL uses the raw ``p + f`` (a fill priced above 1 is a
    guaranteed loser, not a probability).
    """
    if len(fills) > MAX_FILLS_PER_UNIT:
        raise GateRefusal(
            f"a unit holds {len(fills)} fills > MAX_FILLS_PER_UNIT={MAX_FILLS_PER_UNIT}; "
            "the exact 2^m enumeration is refused rather than approximated"
        )
    qs: List[Fraction] = []
    qty: List[Fraction] = []
    total_cost = Fraction(0)
    for f in fills:
        p = Fraction(float(f["entry_price"]))
        fee = Fraction(float(f["fee_per_contract"]))
        q = Fraction(float(f["quantity"]))
        qs.append(_clip01(p + fee))
        qty.append(q)
        total_cost += (p + fee) * q
    return qs, qty, total_cost


def unit_minimal_winning_sets(fills: Sequence[Mapping[str, Any]]) -> List[Tuple[int, ...]]:
    """The MINIMAL sets of fill indices whose joint win makes the unit profitable.

    ``PnL(A) = sum_{i in A} qty_i - T``, so the win event is upward closed and
    is generated by its minimal elements. Enumerated exactly over the unit's
    ``2^m`` subsets in integer arithmetic (every ``qty_i`` and ``T`` is scaled
    onto their common denominator, which is exact -- they are binary rationals).
    """
    m = len(fills)
    if m == 0:
        return []
    qs, qty, total_cost = _unit_terms(fills)
    den = 1
    for x in (*qty, total_cost):
        den = math.lcm(den, x.denominator)
    qty_i = [int(x * den) for x in qty]
    cost_i = int(total_cost * den)
    sums = [0] * (1 << m)
    minimal: List[Tuple[int, ...]] = []
    for mask in range(1, 1 << m):
        low = mask & -mask
        s = sums[mask ^ low] + qty_i[low.bit_length() - 1]
        sums[mask] = s
        if s <= cost_i:
            continue
        rest, is_min = mask, True
        while rest:
            bit = rest & -rest
            if s - qty_i[bit.bit_length() - 1] > cost_i:
                is_min = False
                break
            rest ^= bit
        if is_min:
            members, rest = [], mask
            while rest:
                bit = rest & -rest
                members.append(bit.bit_length() - 1)
                rest ^= bit
            minimal.append(tuple(members))
    return minimal


def unit_null_win_probability(
    fills: Sequence[Mapping[str, Any]],
    minimal_sets: Optional[Sequence[Tuple[int, ...]]] = None,
) -> Fraction:
    """``w_u``: the LEAST FAVOURABLE unit win probability under the null.

    The marginals are fixed (fill ``i`` wins with ``q*_i = p_i + f_i``); the
    joint law is not, and assuming independence is the defect this replaces --
    disjoint brackets on one city-day ladder are mutually EXCLUSIVE, which makes
    a split date a win and pushes the true ``w_u`` well ABOVE the independent
    value. The gate therefore takes the largest ``w_u`` any dependence structure
    could produce (see the module docstring):

        w_u = min(1, sum over minimal winning sets A of min_{i in A} q*_i)

    Exact rationals throughout, so a unit whose PnL can only be exactly 0 is a
    loss, never a rounding win. Raising ``w_u`` can only raise ``P[K >= k]``, so
    any slack here costs power and never validity.

    ``minimal_sets`` lets a caller that already enumerated them (``group_units``,
    which reports their count) hand them back instead of paying 2^m twice.
    """
    if not fills:
        return Fraction(0)
    qs, _, _ = _unit_terms(fills)
    if minimal_sets is None:
        minimal_sets = unit_minimal_winning_sets(fills)
    total = Fraction(0)
    for members in minimal_sets:
        total += min(qs[i] for i in members)
        if total >= 1:
            return Fraction(1)
    return total


def unit_null_win_probability_independent(
    fills: Sequence[Mapping[str, Any]],
) -> Fraction:
    """The SUPERSEDED independent model, kept as a NON-GATING diagnostic.

    ``P[sum_i (won_i - p_i - f_i) qty_i > 0]`` with ``won_i ~ Bernoulli(q*_i)``
    independent, by exact enumeration of the ``2^m`` fill outcomes. Reported per
    unit so a reader can see how much the dependence allowance is worth on this
    record. It is never used to compute the gating p (F3 review defect 1).
    """
    m = len(fills)
    if m == 0:
        return Fraction(0)
    qs, qty, _ = _unit_terms(fills)
    # PnL uses the RAW p + f, not the clipped q*: a fill priced above 1 is a
    # guaranteed loser, not a probability.
    raw = [
        Fraction(float(f["entry_price"])) + Fraction(float(f["fee_per_contract"]))
        for f in fills
    ]
    win_pnl = [(1 - raw[i]) * qty[i] for i in range(m)]
    loss_pnl = [-raw[i] * qty[i] for i in range(m)]
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


def git_added_commit_utc(path: str) -> Tuple[Optional[str], str]:
    """``(ISO committer date of the commit that ADDED path, source note)``.

    ``registration_commit_utc`` is typed into the registration JSON by hand, so
    on its own it is an assertion about the past made by the party the gate
    exists to check (F3 review defect 6). This asks git instead:

        git log --diff-filter=A --format=%cI -- <registration>

    and takes the NEWEST add. A file deleted and re-added started existing, for
    the purposes of "was this registered before the first trade", at the LATER
    instant, so the newest add is the conservative reading. ``(None, why)`` when
    git cannot answer -- no binary, no repo, untracked path -- which the caller
    reports as UNVERIFIED rather than quietly trusting the typed value.
    """
    if shutil.which("git") is None:
        return None, "git binary not found; registration_commit_utc is unverified"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        return None, f"{directory} is not a directory; registration_commit_utc is unverified"
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                directory,
                "log",
                "--diff-filter=A",
                "--format=%cI",
                "--",
                os.path.basename(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git log failed ({exc}); registration_commit_utc is unverified"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return None, (
            "git log failed ("
            + (detail[0] if detail else f"exit {proc.returncode}")
            + "); registration_commit_utc is unverified"
        )
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None, (
            "git records no commit ADDING this registration (untracked, or added "
            "outside this history); registration_commit_utc is unverified"
        )
    return lines[0], "git log --diff-filter=A --format=%cI (newest add)"


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


def _first_present(rows: Sequence[Mapping[str, Any]], key: str) -> Any:
    """The first non-``None`` ``key`` across the row's sources (journal, state)."""
    for row in rows:
        if row is None:
            continue
        value = row.get(key)
        if value is not None:
            return value
    return None


def _bracket_spec_fields(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The settled bracket semantics recorded on the row (FR-1.1), or ``None``."""
    spec = _first_present(rows, "settlement_spec")
    if isinstance(spec, Mapping) and spec.get("strike_type"):
        return spec
    strike_type = _first_present(rows, "strike_type")
    if not strike_type:
        return None
    return {
        "strike_type": strike_type,
        "floor_strike": _first_present(rows, "floor_strike"),
        "cap_strike": _first_present(rows, "cap_strike"),
    }


def settlement_payoff_check(
    trade: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> Optional[Tuple[str, str]]:
    """``None`` when the booked money reconciles with the row's settlement fields.

    Otherwise ``(code, detail)``. The gate recomputes rather than inspecting:
    the settled payoff per contract is 1 when ``settlement_outcome`` names the
    side actually held (``contract_side`` YES pays on ``"yes"``, NO on
    ``"no"``) and 0 otherwise, ``exit_price`` must BE that payoff, and ``pnl``
    must be ``(payoff - entry_price) * quantity - exit_fee`` (sign-flipped on a
    sell). The direction is then re-derived from the strike spec and the settled
    daily high, so ``settlement_outcome`` does not vouch for itself either.

    Codes ending ``_missing`` / ``_unverifiable`` mean the row cannot be
    reconciled either way and must be EXCLUDED (and counted against the
    exclusion budget). Every other code is corruption and refuses the record --
    including a row carrying the ``repaired_no_side_settlement`` marker, which
    is written by a script that can itself mis-repair (F3 review defect 2).
    """
    outcome = str(_first_present(rows, "settlement_outcome") or "").strip().lower()
    if outcome not in _YES_PAYOFF:
        return (
            "settlement_outcome_missing",
            f"settlement_outcome={_first_present(rows, 'settlement_outcome')!r} is not 'yes'/'no'",
        )
    side_held = str(trade.get("contract_side") or "YES").strip().upper()
    if side_held not in ("YES", "NO"):
        return ("settlement_outcome_missing", f"contract_side={side_held!r} is neither YES nor NO")
    payoff = 1.0 if outcome == side_held.lower() else 0.0

    try:
        exit_price = float(trade["exit_price"])
        entry_price = float(trade["entry_price"])
        quantity = float(trade["quantity"])
        pnl = float(trade["pnl"])
    except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - _admit filters these
        return ("pnl_not_reconcilable", f"unreadable numeric field ({exc})")
    if abs(exit_price - payoff) > _SETTLEMENT_TOL:
        return (
            "exit_price_contradicts_settlement",
            f"{side_held} contract settled {outcome!r} pays {payoff:.1f}/contract, "
            f"but exit_price={exit_price!r}",
        )

    try:
        exit_fee = float(_first_present(rows, "exit_fee") or 0.0)
    except (TypeError, ValueError):
        return ("pnl_not_reconcilable", f"exit_fee={_first_present(rows, 'exit_fee')!r} is not numeric")
    direction = -1.0 if str(_first_present(rows, "side") or "buy").strip().lower() == "sell" else 1.0
    expected = direction * (payoff - entry_price) * quantity - exit_fee
    if abs(pnl - expected) > _PNL_TOL:
        return (
            "pnl_not_reconcilable",
            f"booked pnl={pnl!r} but the row's own settlement fields give "
            f"{expected!r} (payoff {payoff:.1f}, entry {entry_price!r}, qty {quantity!r}, "
            f"exit_fee {exit_fee!r})",
        )

    spec_fields = _bracket_spec_fields(rows)
    high = _first_present(rows, "settlement_high")
    if spec_fields is None or high is None:
        return (
            "strike_spec_unverifiable",
            "no settled daily high and/or bracket spec on the row; the settlement "
            "direction cannot be re-derived",
        )
    try:
        spec = parse_bracket_spec(str(trade.get("symbol") or ""), spec_fields)
        derived = "yes" if settles_yes(spec, float(high)) else "no"
    except (BracketSpecError, TypeError, ValueError) as exc:
        return ("strike_spec_unverifiable", f"bracket spec unusable ({exc})")
    if derived != outcome:
        return (
            "settlement_outcome_contradicts_strike_spec",
            f"settled high {high!r} against {spec.describe()} settles {derived!r}, "
            f"but the row records settlement_outcome={outcome!r}",
        )
    return None


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


def _parse_iso_day(value: Any) -> Optional[_date]:
    """``value`` as a calendar date, or ``None``. Never a guess."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return _date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _target_date(row: Mapping[str, Any]) -> Optional[str]:
    """The row's normalised ``YYYY-MM-DD`` unit key, or ``None``.

    The plain-string form of :func:`_target_date_checked`, kept because it is
    this module's published shape: ``scripts/factory_paper_reconcile.py`` loads
    gate.py as a module and feeds the result straight to ``date.fromisoformat``
    (F3 review round 2, BLOCKING 2). A row whose label cannot be trusted reads
    as "no date" here; only the gate itself acts on WHY, through the checked form.
    """
    return _target_date_checked(row)[0]


def _target_date_checked(row: Mapping[str, Any]) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
    """``(normalised YYYY-MM-DD, problem)`` for the row's independence-unit key.

    ``target_date`` is what ``group_units`` buckets on, so a wrong label splits
    one city-day ladder into several "independent" units and inflates the
    ``n_units >= n_min`` count that gates everything (F3 review defect 4). The
    recorded string is therefore parsed as a calendar date, normalised (so
    ``2026-06-01`` and ``2026-06-01T00:00:00`` cannot become two units) and
    cross-checked against ``settlement_date_for(symbol)`` -- the ticker's own
    event-date label, which is derived from the identifier and not from
    anything the run wrote. A disagreement is a refusal, not an exclusion:
    which of the two is wrong is not knowable from inside.
    """
    symbol = str(row.get("symbol") or "")
    label = settlement_date_for(symbol)
    recorded = row.get("target_date")
    if isinstance(recorded, str) and recorded.strip():
        parsed = _parse_iso_day(recorded)
        if parsed is None:
            return None, ("target_date_unparseable", f"target_date={recorded!r} is not a calendar date")
        if label is not None and parsed != label:
            return None, (
                "target_date_mismatch",
                f"row target_date={parsed.isoformat()} but the ticker's event-date label "
                f"({symbol}) settles {label.isoformat()}",
            )
        return parsed.isoformat(), None
    derived = _parse_iso_day(target_date_for_position(dict(row)))
    if derived is not None:
        if label is not None and derived != label:
            return None, (
                "target_date_mismatch",
                f"expiration_time implies target_date={derived.isoformat()} but the ticker's "
                f"event-date label ({symbol}) settles {label.isoformat()}",
            )
        return derived.isoformat(), None
    return (label.isoformat(), None) if label is not None else (None, None)


def _city_of(symbol: str) -> str:
    """The settlement city a weather ticker belongs to (``KXHIGHNY-...`` -> ``NY``).

    Taken from the settlement-station registry rather than the ticker text, so
    it names the station whose daily high actually settles the bracket. Falls
    back to the series prefix for anything unregistered -- this feeds a REPORT,
    never a gating number.
    """
    station = settlement_station_for(symbol)
    if station:
        return city_key_for_station(station) or station
    return (str(symbol).split("-", 1)[0] or "?").upper()


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
    reconcile_settlement: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Merge journal + closed_trades into one settled-fill list for ``strategy_name``.

    The journal is the durable record (append-only); ``closed_trades`` carries
    ``entry_fee`` and is cleared on a cycle reset. Rows are joined on
    ``(symbol, entry_time, strategy_name)`` -- ``entry_time`` is the exact
    ``open_time.isoformat()`` string in both files.

    Fee trust (taker registration): ``fee = max(booked entry_fee / qty,
    recomputed taker fee at the row's price and the JOURNAL quantity)``.
    ``counts["stale_no_side_rows"]`` lists rows still carrying the pre-724d93c
    sign-flipped numbers and ``counts["corrupt_rows"]`` rows whose booked money
    or unit key contradicts their own settlement fields; the caller refuses when
    either is non-empty. ``counts["excluded_rows"]`` lists every dropped row
    with its reason, and ``counts["excluded_rate"]`` is the share of in-scope
    settled fills the gate could not read (F3 review defects 2-4).

    ``reconcile_settlement`` defaults to True and the gate always passes True:
    a fill whose booked money the gate cannot reproduce from the row's own
    settlement fields is dropped and charged, never assumed good. It is a
    keyword because this function is also the fill collector for NON-gating
    tools (``scripts/factory_paper_reconcile.py`` loads gate.py as a module),
    and a repricing report is not a promotion decision -- a caller that is not
    scoring a genome may pass False to keep pre-FR-1.2 rows, which carry no
    ``settlement_spec`` / ``settlement_high``, rather than silently report zero
    fills. The scope cross-check stays on either way: it only ever fires on a
    row that reconciles EXACTLY, so it is never a false alarm.
    """
    excluded: Counter = Counter()
    excluded_rows: List[Optional[Dict[str, Any]]] = []
    by_key: Dict[Tuple[str, Optional[str], str], Dict[str, Any]] = {}
    stale_rows: List[Dict[str, Any]] = []
    corrupt_rows: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    taker_reg = str(fee_type).lower() == "taker"

    state_by_key: Dict[Tuple[str, Optional[str], str], Mapping[str, Any]] = {}
    for t in closed_trades:
        state_by_key.setdefault(_join_key(t), t)

    def _identify(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": str(row.get("symbol") or ""),
            "entry_time": _iso(row.get("entry_time")) or _iso(row.get("open_time")),
        }

    def _drop(
        reason: str,
        row: Mapping[str, Any],
        source: str,
        detail: Optional[str] = None,
        *,
        listed: bool = True,
    ) -> Tuple[str, Optional[int]]:
        """Charge one exclusion and return a handle the twin pass can RETRACT."""
        excluded[reason] += 1
        idx: Optional[int] = None
        if listed:
            entry = {**_identify(row), "reason": reason, "source": source}
            if detail:
                entry["detail"] = detail
            excluded_rows.append(entry)
            idx = len(excluded_rows) - 1
        return reason, idx

    def _exclude(
        reason: str, row: Mapping[str, Any], source: str, detail: Optional[str] = None
    ) -> Tuple[str, Optional[int]]:
        """Drop a row, counted AND named: a bare counter hides which fills went."""
        return _drop(reason, row, source, detail, listed=True)

    def _retract(record: Tuple[str, Optional[int]]) -> None:
        """Un-charge an exclusion a readable twin of the same fill has replaced."""
        reason, idx = record
        excluded[reason] -= 1
        if excluded[reason] <= 0:
            del excluded[reason]
        if idx is not None:
            excluded_rows[idx] = None

    def _snapshot() -> Tuple[Counter, int]:
        return Counter(excluded), len(excluded_rows)

    def _restore(snap: Tuple[Counter, int]) -> None:
        """Undo a failed twin attempt's exclusions so one fill is charged once."""
        before, n_rows = snap
        excluded.clear()
        excluded.update(before)
        del excluded_rows[n_rows:]

    def _corrupt(reason: str, row: Mapping[str, Any], source: str, detail: str) -> None:
        corrupt_rows.append({**_identify(row), "reason": reason, "source": source, "detail": detail})

    def _scope_drop(
        reason: str,
        row: Mapping[str, Any],
        source: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> Optional[Tuple[str, Optional[int]]]:
        """Honour a per-row scope filter only when the row is NOT a readable fill.

        Scope drops are deliberately not charged to the exclusion budget -- they
        are filters, not damage. But every filter here fires on ONE field of ONE
        row, so an operator or a corrupted writer could delete an inconvenient
        fill from the sample for free, and the gate's numbers all improve when a
        losing fill vanishes. The reviewer demonstrated it: ``settlement_error``
        written onto one losing leg of each two-fill unit took a record from
        FAIL (p=0.22795, net 59.60) to PASS (p=0.02904, net 143.00) with
        ``excluded_rate`` still 0.0000.

        So before a scope filter is honoured the row's OWN settlement fields are
        asked whether it is a settled fill after all. ``settlement_payoff_check``
        returning ``None`` means exit_price IS the settled payoff, pnl matches
        it, and the strike spec re-derives the recorded outcome -- a fully
        readable, fully reconcilable settled fill. A row that reconciles while
        claiming to be out of scope contradicts itself, and which of the two
        claims is false is not knowable from inside, so it REFUSES the record
        exactly as a contradicted payoff does (F3 review round 2, BLOCKING 1).
        """
        code = CROSS_CHECKED_SCOPE_REASONS.get(reason)
        if code is not None and settlement_payoff_check(row, rows) is None:
            _corrupt(
                code,
                row,
                source,
                f"filtered out of scope as {reason!r}, yet the row's own settlement "
                "fields reconcile exactly (exit_price is the settled payoff, pnl "
                "matches it, and the strike spec re-derives the recorded outcome): "
                "a readable settled fill cannot be deleted from the sample by one "
                "field, so the record is refused rather than scored without it",
            )
            return None
        return _drop(reason, row, source, listed=False)

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
            _exclude("no_side_outcome_unverifiable", trade, source)
            return False
        return True

    def _admit(
        row: Mapping[str, Any], source: str, twin: Optional[Mapping[str, Any]] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, Optional[int]]]]:
        """``(trade, retractable exclusion handle)``; the handle is ``None`` when
        nothing was charged (admitted, or refused as corrupt)."""
        rows = tuple(r for r in (row, twin) if r is not None)
        symbol = str(row.get("symbol") or "")
        if str(row.get("strategy_name") or "") != strategy_name:
            # The ONE scope filter the gate cannot cross-check: nothing on a fill
            # names the strategy except the field itself, and a row of ANOTHER
            # strategy reconciles perfectly well. It is defended instead by the
            # join key -- strategy_name is part of it, so a name rewritten in one
            # file leaves the other file's row under the original key, where the
            # twin rule below still admits it.
            excluded["other_strategy"] += 1
            return None, None
        if not symbol.upper().startswith(market_family.upper()):
            return None, _scope_drop("outside_market_family", row, source, rows)
        if settlement_timezone_for(symbol) is None:
            return None, _scope_drop("non_weather_symbol", row, source, rows)
        ok, why = _is_settled(row)
        if not ok:
            reason = why.split(":", 1)[0]
            if reason in QUALITY_EXCLUSION_REASONS:
                return None, _exclude(reason, row, source, why)
            return None, _scope_drop(reason, row, source, rows)
        td, td_problem = _target_date_checked(row)
        if td_problem is not None:
            _corrupt(td_problem[0], row, source, td_problem[1])
            return None, None
        if td is None:
            return None, _exclude(
                "no_target_date", row, source,
                "no target_date, expiration stamp or event-date label",
            )
        try:
            entry_price = float(row["entry_price"])
            quantity = float(row["quantity"])
            pnl = float(row["pnl"])
        except (KeyError, TypeError, ValueError):
            return None, _exclude(
                "missing_numeric_field", row, source,
                "entry_price / quantity / pnl unreadable",
            )
        if quantity <= 0:
            return None, _exclude("non_positive_quantity", row, source, f"quantity={quantity!r}")
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
        }, None

    def _reconcile(
        trade: Dict[str, Any], rows: Sequence[Mapping[str, Any]], source: str
    ) -> bool:
        """False when the row's booked money does not survive recomputation."""
        if not reconcile_settlement:
            return True
        problem = settlement_payoff_check(trade, rows)
        if problem is None:
            return True
        code, detail = problem
        if code in QUALITY_EXCLUSION_REASONS:
            _exclude(code, trade, source, detail)
        else:
            _corrupt(code, trade, source, detail)
        return False

    # Every journal key the loop below touches, admitted or not, together with
    # the exclusion it was charged. The closed_trades pass exists for fills the
    # journal LOST: without ``seen`` an excluded journal row's state twin is
    # excluded a second time and the budget double-counts one fill, but ``seen``
    # alone also DELETED a fill whose journal row was unreadable and whose state
    # twin was complete. So a key with a retractable charge is retried against
    # its twin, and the journal's charge is retracted when the twin admits --
    # the readable twin is preferred, and the fill is still charged at most once
    # (F3 review round 2, should-fix 2).
    seen: set = set()
    retractable: Dict[Tuple[str, Optional[str], str], List[Tuple[str, Optional[int]]]] = {}
    journal_by_key: Dict[Tuple[str, Optional[str], str], Mapping[str, Any]] = {}

    for row in journal_rows:
        key = _join_key(row)
        if key in by_key:
            excluded["duplicate_journal_row"] += 1
            continue
        seen.add(key)
        journal_by_key.setdefault(key, row)
        st = state_by_key.get(key)
        trade, drop = _admit(row, "journal", twin=st)
        if trade is None:
            if drop is not None:
                retractable.setdefault(key, []).append(drop)
            continue
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
            if not _reconcile(trade, (row, st), "journal+closed_trades"):
                continue
        else:
            if not _check_no_side(trade, row, "journal"):
                continue
            trade["entry_fee"], trade["fee_source"] = _fee_for(trade, None)
            if not _reconcile(trade, (row,), "journal"):
                continue
        for record in retractable.pop(key, []):
            _retract(record)  # an earlier, unreadable duplicate of this same fill
        by_key[key] = trade

    for t in closed_trades:
        key = _join_key(t)
        if key in by_key:
            continue
        replaced = key in seen
        if replaced and key not in retractable:
            # the journal row was read and then dropped downstream with this very
            # state row already consulted (stale NO leg, unreconcilable payoff):
            # retrying it here would only charge the same fill twice
            continue
        snapshot = _snapshot() if replaced else None
        source = "closed_trades_over_unreadable_journal" if replaced else "closed_trades_only"
        twin = journal_by_key.get(key)
        trade, _unused = _admit(t, source, twin=twin)
        ok = trade is not None
        if ok and not _check_no_side(trade, t, "closed_trades"):
            ok = False
        if ok:
            booked = float(t["entry_fee"]) if t.get("entry_fee") is not None else None
            trade["entry_fee"], trade["fee_source"] = _fee_for(trade, booked)
            rows = tuple(r for r in (t, twin) if r is not None)
            if not _reconcile(trade, rows, source):
                ok = False
        if not ok:
            if snapshot is not None:
                # the twin is no better than the journal row: keep the journal's
                # single charge rather than charging one fill twice
                _restore(snapshot)
            continue
        for record in retractable.pop(key, []):
            _retract(record)
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

    listed_rows = [r for r in excluded_rows if r is not None]
    quality_excluded = sum(excluded[r] for r in QUALITY_EXCLUSION_REASONS)
    denominator = len(trades) + quality_excluded
    if not trades and quality_excluded:
        # The gate refuses on excluded_rate long before this, but this function
        # is also the fill collector for non-gating tools, and "0 fills" printed
        # without a reason reads as "the strategy did not trade" rather than
        # "every fill it made was dropped". Say which it is, in counts, where
        # every caller already carries it into its report.
        warnings.append(
            {
                "warning": "every_in_scope_settled_fill_was_excluded",
                "quality_excluded": quality_excluded,
                "by_reason": {
                    r: int(excluded[r]) for r in QUALITY_EXCLUSION_REASONS if excluded[r]
                },
                "note": (
                    "no settled fill survived; a report built on this list is empty for a "
                    "reason, not because the strategy did not trade. strike_spec_unverifiable "
                    "on every row means the record predates the FR-1.2 settlement provenance "
                    "(settlement_spec / settlement_high) the payoff reconciliation needs"
                ),
            }
        )
    counts = {
        "journal_rows": len(journal_rows),
        "closed_trades": len(closed_trades),
        "settled_fills": len(trades),
        "fills_by_source": dict(Counter(t["source"] for t in trades)),
        "fills_by_fee_source": dict(Counter(t["fee_source"] for t in trades)),
        "excluded": dict(sorted(excluded.items())),
        "excluded_rows": listed_rows,
        "quality_excluded": quality_excluded,
        "quality_exclusion_reasons": list(QUALITY_EXCLUSION_REASONS),
        "excluded_rate": (quality_excluded / denominator) if denominator else 0.0,
        "excluded_rate_formula": FORMULAS["exclusion_rate"],
        "stale_no_side_rows": stale_rows,
        "corrupt_rows": corrupt_rows,
        "warnings": warnings,
        "maker_booked_fills": [
            {"symbol": t["symbol"], "entry_time": t["entry_time"]} for t in trades if t["maker_booked"]
        ],
    }
    return trades, counts


def group_units(trades: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One unit per ``target_date``: summed net PnL, contract-weighted breakeven.

    The key is ``target_date`` ALONE, which is the pre-registered
    ``grouping_unit`` (FR-5.2 / FR-F3.4) and is NOT redefined here. Each unit
    therefore records the cities its fills span, so ``evaluate`` can report what
    that costs: four cities' ladders on one date are one unit, and unlike two
    brackets on ONE ladder they are not mutually exclusive.
    """
    buckets: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for t in trades:
        buckets[t["target_date"]].append(t)
    units: List[Dict[str, Any]] = []
    for td in sorted(buckets):
        fills = buckets[td]
        qty = sum(f["quantity"] for f in fills)
        net = math.fsum(f["net_pnl"] for f in fills)
        q_star = math.fsum(f["q_star"] * f["quantity"] for f in fills) / qty
        minimal = unit_minimal_winning_sets(fills)
        w_u = unit_null_win_probability(fills, minimal)
        independent = (
            float(unit_null_win_probability_independent(fills))
            if len(fills) <= MAX_FILLS_FOR_INDEPENDENT_DIAGNOSTIC
            else None
        )
        cities = sorted({_city_of(f["symbol"]) for f in fills})
        units.append(
            {
                "target_date": td,
                "n_fills": len(fills),
                "symbols": sorted(f["symbol"] for f in fills),
                "cities": cities,
                "n_cities": len(cities),
                "cross_city": len(cities) > 1,
                "quantity": qty,
                "net_pnl": net,
                "q_star": q_star,
                "null_win_probability": float(w_u),
                "null_win_probability_exact": _fraction_str(w_u),
                "null_win_probability_independent": independent,
                "n_minimal_winning_sets": len(minimal),
                "min_fills_to_win": min((len(a) for a in minimal), default=None),
                "null_saturated": w_u >= 1,
                "null_degenerate": float(w_u) >= NULL_DEGENERATE_W,
                "won": net > 0.0,
                "_w_u": w_u,
            }
        )
    return units


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def _as_utc(value: Any) -> Optional[datetime]:
    """ISO-8601 (``Z`` accepted) as a tz-aware UTC instant; naive is read as UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # sandbox clock is UTC (deploy/pi)
    return dt.astimezone(timezone.utc)


def _first_trade_after(
    trades: Sequence[Mapping[str, Any]],
    cutoff_iso: Optional[str],
    git_result: Optional[Tuple[Optional[str], str]] = None,
):
    """``(ok, note, detail)`` for the registered_before_first_trade condition.

    The declared ``registration_commit_utc`` is reconciled against git before it
    is used (F3 review defect 6): a hand-typed timestamp cannot establish that
    the registration preceded the run it is meant to bind. ``ok`` is

    * ``False`` when the value is missing (the gate cannot prove anything),
    * ``False`` when git could not verify it (``--allow-unverified-registration``
      downgrades that to a reported, non-gating line for dry runs),
    * ``False`` with ``detail["disagreement"]`` set when git DOES have an add
      commit and it names another instant -- the caller turns that into a
      refusal, because one of the two is false and the gate cannot tell which,
    * otherwise ``min(entry_time) >= cutoff``, with git's instant as the cutoff.
    """
    git_iso, git_note = git_result if git_result is not None else (None, "git not consulted")
    detail: Dict[str, Any] = {
        "declared": cutoff_iso,
        "git_added_commit_utc": git_iso,
        "git_source": git_note,
        "verified": False,
    }
    if not cutoff_iso:
        detail["note_for_operator"] = git_iso or (
            "fill it from `git log --diff-filter=A --format=%cI -- "
            "configs/factory/gate_registration.json`"
        )
        return False, (
            "registration commit time not recorded (registration_commit_utc is null); "
            + (
                f"git says the registration was added at {git_iso}"
                if git_iso
                else "fill it from `git log --diff-filter=A --format=%cI -- "
                "configs/factory/gate_registration.json`"
            )
        ), detail

    declared = _as_utc(cutoff_iso)
    if declared is None:
        return False, f"registration_commit_utc {cutoff_iso!r} is not ISO-8601", detail

    cutoff = declared
    if git_iso is None:
        note_prefix = f"UNVERIFIED ({git_note}); "
    else:
        from_git = _as_utc(git_iso)
        if from_git is None:  # pragma: no cover - git emits ISO-8601
            return False, f"git returned a non-ISO add time {git_iso!r}", detail
        if abs((from_git - declared).total_seconds()) > 1.0:
            detail["disagreement"] = (
                f"registration_commit_utc says {declared.isoformat()} but git records the "
                f"registration as ADDED at {from_git.isoformat()}"
            )
            return False, detail["disagreement"], detail
        detail["verified"] = True
        cutoff = from_git
        note_prefix = "verified against git; "

    earliest: Optional[datetime] = None
    for t in trades:
        dt = _as_utc(t.get("entry_time"))
        if dt is None:
            continue
        if earliest is None or dt < earliest:
            earliest = dt
    detail["first_settled_fill_utc"] = earliest.isoformat() if earliest else None
    if earliest is None:
        return bool(detail["verified"]), note_prefix + "no settled fills carry an entry_time", detail
    ok = bool(detail["verified"]) and earliest >= cutoff
    return ok, (
        note_prefix
        + f"first settled fill {earliest.isoformat()} vs registration {cutoff.isoformat()}"
    ), detail


def evaluate(
    trades: Sequence[Mapping[str, Any]],
    registration: Mapping[str, Any],
    *,
    observed_spec_hash: Optional[str],
    spec_hash_source: str,
    allow_unverified_registration: bool = False,
    realistic_fills: Optional[bool] = None,
    counts: Optional[Mapping[str, Any]] = None,
    registration_commit_git: Optional[Tuple[Optional[str], str]] = None,
) -> Dict[str, Any]:
    """The verdict dict (timestamp-free). Pure: no I/O."""
    thresholds = registration["thresholds"]
    n_min = int(thresholds["n_min"])
    alpha = float(thresholds["alpha"])
    net_gt = float(thresholds.get("net_pnl_gt", 0.0))
    # The exclusion budget is a GUARD, so the registration may only TIGHTEN it.
    # It used to be overridable upward with nothing gating the override, which
    # let a registration dial away the one condition standing between a damaged
    # sample and a PASS (F3 review round 2, should-fix 4).
    raw_max_excluded = thresholds.get("max_excluded_rate")
    reg_max_excluded = None if raw_max_excluded is None else float(raw_max_excluded)
    max_excluded = (
        MAX_EXCLUDED_RATE if reg_max_excluded is None
        else min(reg_max_excluded, MAX_EXCLUDED_RATE)
    )
    override_rejected = reg_max_excluded is not None and reg_max_excluded > MAX_EXCLUDED_RATE
    counts = counts or {}
    extra_warnings: List[Dict[str, Any]] = []
    if override_rejected:
        extra_warnings.append(
            {
                "warning": "registration_max_excluded_rate_rejected",
                "registration_max_excluded_rate": reg_max_excluded,
                "applied": max_excluded,
                "note": (
                    "the registration asked to raise the exclusion budget above the "
                    "hard ceiling; a guard a registration can dial away is not a guard, "
                    "so the ceiling stands"
                ),
            }
        )

    units = group_units(trades)
    n_units = len(units)
    k_units = sum(1 for u in units if u["won"])
    refusals: List[str] = []
    underpowered = n_units < n_min
    if underpowered:
        refusals.append(
            f"n_units={n_units} < n_min={n_min}: the binomial test is not run on an "
            "underpowered sample"
        )

    net_pnl = math.fsum(t["net_pnl"] for t in trades)
    gross = math.fsum(t["pnl"] for t in trades)
    fees = math.fsum(t["entry_fee"] for t in trades)

    multi = sum(1 for u in units if u["n_fills"] > 1)
    saturated = sum(1 for u in units if u["null_saturated"])
    degenerate = sum(1 for u in units if u["null_degenerate"])
    cross_city = sum(1 for u in units if u["cross_city"])
    city_day_units = len({(c, u["target_date"]) for u in units for c in u["cities"]})
    max_cities = max((u["n_cities"] for u in units), default=0)
    if cross_city:
        extra_warnings.append(
            {
                "warning": "cross_city_units_merged",
                "cross_city_units": cross_city,
                "n_units": n_units,
                "n_units_if_grouped_by_city_day": city_day_units,
                "units_lost_to_cross_city_merging": city_day_units - n_units,
                "max_cities_in_one_unit": max_cities,
                "note": (
                    "the pre-registered unit is target_date ALONE (FR-5.2), so fills on "
                    "several cities' ladders on one date are scored as ONE trial. That is "
                    "a power loss of "
                    f"{city_day_units - n_units} trial(s) here, and the mutual-exclusivity "
                    "premise the within-unit null leans on does not hold ACROSS cities -- "
                    "two cities' brackets can both settle YES. Redefining the unit is the "
                    "owner's call, not the gate's; this is reported so the call is informed"
                ),
            }
        )
    if degenerate:
        extra_warnings.append(
            {
                "warning": "units_with_degenerate_null",
                "units": degenerate,
                "of": n_units,
                "threshold": NULL_DEGENERATE_W,
                "dates": [u["target_date"] for u in units if u["null_degenerate"]][:20],
                "note": (
                    "these units win under the null almost always, so winning them is not "
                    "evidence. Saturation (w_u = 1) fires far too late to catch this; a "
                    "date carrying several brackets, or several cities, is already here"
                ),
            }
        )
    w_us = [u.pop("_w_u") for u in units]  # Fractions: not for the JSON
    p_exact: Optional[Fraction] = None
    if n_units:
        q_bar_units = math.fsum(u["q_star"] for u in units) / n_units
        if not underpowered:
            p_exact = poisson_binomial_upper_tail(w_us, k_units)
        p_units = None if p_exact is None else float(p_exact)
        p_pooled = None if underpowered else binomial_upper_tail(n_units, k_units, q_bar_units)
    else:
        q_bar_units = None
        p_units = None
        p_pooled = None

    n_fills = len(trades)
    k_fills = sum(1 for t in trades if t["won"])
    q_bar_fills = math.fsum(t["q_star"] for t in trades) / n_fills if n_fills else None
    p_fills = (
        binomial_upper_tail(n_fills, k_fills, q_bar_fills)
        if (n_fills and not underpowered)
        else None
    )

    registered_hash = str(registration["spec_hash"])
    hash_ok = observed_spec_hash is not None and observed_spec_hash == registered_hash
    rbft_ok, rbft_note, rbft_detail = _first_trade_after(
        trades, registration.get("registration_commit_utc"), registration_commit_git
    )
    rbft_gating = True
    if rbft_detail.get("disagreement"):
        # git and the typed value name different instants: one of them is false
        # and the gate cannot tell which. Refuse rather than pick (defect 6).
        refusals.append(
            "registration_commit_utc does not reconcile with git: " + rbft_detail["disagreement"]
        )
        rbft_ok = False
    elif allow_unverified_registration and not rbft_detail.get("verified"):
        rbft_gating = False
        rbft_ok = None
        rbft_note = "UNVERIFIED (--allow-unverified-registration): " + rbft_note

    reg_fee_type = str(registration.get("fee_type") or "taker").lower()
    maker_fills = list(counts.get("maker_booked_fills") or [])
    fee_type_ok = not (reg_fee_type == "taker" and maker_fills)

    quality_excluded = int(counts.get("quality_excluded", 0) or 0)
    excluded_rate = float(counts.get("excluded_rate", 0.0) or 0.0)
    excl_ok = excluded_rate <= max_excluded
    if not excl_ok:
        refusals.append(
            f"excluded_rate={excluded_rate:.4f} > max_excluded_rate={max_excluded}: "
            f"{quality_excluded} in-scope settled fill(s) could not be read, and every "
            "gating number is computed over the survivors only -- see counts.excluded_rows"
        )

    requires_realistic = bool(registration.get("requires_realistic_fills", True))
    if realistic_fills is None and not requires_realistic:
        rf_ok: Optional[bool] = None
        rf_note = (
            "not required by the registration and not recorded by the exchange state; "
            "reported, non-gating"
        )
    elif realistic_fills is None:
        # requires_realistic and nothing evidences it: the no-op that let
        # requires_realistic_fills=True sit next to a PASS (defect 5).
        rf_ok = False
        rf_note = (
            "REFUSED: the registration requires realistic fills and nothing evidences "
            "them -- SimulatedExchange._save_state does not serialise a realistic_fills "
            "key, so pass --realistic-fills true|false as an explicit, recorded assertion"
        )
        refusals.append(
            "requires_realistic_fills=True but the run cannot evidence realistic fills; "
            "pass --realistic-fills true|false"
        )
    else:
        rf_ok = bool(realistic_fills) or not requires_realistic
        rf_note = f"realistic_fills={realistic_fills}; required={requires_realistic}"

    refused = bool(refusals)

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
            "note": (
                "not computed (underpowered)"
                if underpowered
                else "exact Poisson-binomial upper tail over units, least-favourable "
                "within-unit dependence"
            ),
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
            **rbft_detail,
        },
        "excluded_rate_within_bound": {
            "ok": excl_ok,
            "observed": excluded_rate,
            "required_le": max_excluded,
            "quality_excluded": quality_excluded,
            "admitted": n_fills,
            "excluded_by_reason": {
                r: int(counts.get("excluded", {}).get(r, 0))
                for r in QUALITY_EXCLUSION_REASONS
                if counts.get("excluded", {}).get(r)
            },
            "hard_ceiling": MAX_EXCLUDED_RATE,
            "registration_max_excluded_rate": reg_max_excluded,
            "registration_override_rejected": override_rejected,
            "source": FORMULAS["exclusion_rate"],
            "note": (
                "every gating number is computed over ADMITTED fills only, so a dropped "
                "losing fill improves all of them at once; the drops are themselves gated"
            ),
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
        conditions["excluded_rate_within_bound"]["ok"],
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
        "refusal": "; ".join(refusals) if refusals else None,
        "refusals": list(refusals),
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
            "null_independent_diagnostic": FORMULAS["unit_null_independent_diagnostic"],
            "test": FORMULAS["p_value"],
            "rule": FORMULAS["unit_win"],
            "units_with_multiple_fills": multi,
            "units_with_saturated_null": saturated,
            "units_with_degenerate_null": degenerate,
            "degenerate_null_threshold": NULL_DEGENERATE_W,
            "degenerate_note": (
                "a unit whose least-favourable null win probability reaches "
                f"{NULL_DEGENERATE_W} carries next to no evidence: under the null it wins "
                "almost always. Saturation at 1 is the extreme case and fires far too "
                "late -- two brackets at q* = 0.47, or three cities at q* = 0.30, are "
                "already here"
            ),
            "cross_city_units": cross_city,
            "n_units_if_grouped_by_city_day": city_day_units,
            "units_lost_to_cross_city_merging": city_day_units - n_units,
            "max_cities_in_one_unit": max_cities,
            "cross_city_note": (
                "GOVERNANCE, not a gate: FR-5.2 pre-registers the unit as target_date "
                "ALONE, so fills on several cities' ladders on one date collapse into one "
                "trial. Two brackets on ONE city-day ladder are mutually exclusive, which "
                "is what the within-unit null is built on; two cities' brackets are NOT "
                "mutually exclusive, only correlated through synoptic weather. The gate "
                "reports the merging and its power cost and refuses to redefine the "
                "pre-registered unit on its own authority"
            ),
            "cross_city_formula": FORMULAS["cross_city_units"],
            "saturated_note": (
                "a unit whose least-favourable null win probability reaches 1 carries NO "
                "evidence: some dependence structure makes it win every time. Several "
                "brackets on one settlement day do that; one bracket per day does not"
            ),
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
        "warnings": list(counts.get("warnings") or []) + extra_warnings,
        "formulas": dict(FORMULAS),
        "unit_table": units,
        "fills": [dict(t) for t in trades],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _state_realistic_fills(state_path: Optional[str]) -> Optional[bool]:
    """``realistic_fills`` from the exchange state, or ``None``.

    ``SimulatedExchange._save_state`` does not serialise this key (and
    ``matching_engine.py`` is a protected file the F3 sprint may not touch), so
    on today's state files this always returns ``None``. That used to make
    ``requires_realistic_fills`` a structural no-op -- the condition fell out of
    the gating list into ``not_applicable`` and the verdict still said PASS.
    ``evaluate`` now REFUSES on that ``None`` instead (F3 review defect 5); the
    read stays so the gate picks the flag up for free if the engine ever writes it.
    """
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
    commit_time_lookup: Optional[Callable[[str], Tuple[Optional[str], str]]] = None,
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
        reconcile_settlement=True,  # explicit: the gate never scores money it cannot reproduce
    )
    if counts["stale_no_side_rows"]:
        rows = counts["stale_no_side_rows"]
        raise GateRefusal(
            f"stale NO-side settlement rows present ({len(rows)}: "
            + ", ".join(f"{r['symbol']}@{r['entry_time']} [{r['source']}]" for r in rows[:5])
            + ("..." if len(rows) > 5 else "")
            + "); run scripts/repair_no_settlement_pnl.py --state ... --journal ... --apply first"
        )
    if counts["corrupt_rows"]:
        rows = counts["corrupt_rows"]
        by_reason = Counter(r["reason"] for r in rows)
        raise GateRefusal(
            f"{len(rows)} row(s) contradict their own settlement fields "
            f"({dict(sorted(by_reason.items()))}): "
            + "; ".join(
                f"{r['symbol']}@{r['entry_time']} [{r['source']}] {r['detail']}" for r in rows[:3]
            )
            + ("..." if len(rows) > 3 else "")
            + ". The gate recomputes every payoff from the row's own settlement fields and "
            "will not score a record whose booked money or unit key it cannot reproduce"
        )
    spec_path = promoted_override or registration.get("promoted_spec_path")
    observed_hash, hash_source = resolve_spec_hash(spec_path)
    if realistic_fills is None:
        realistic_fills = _state_realistic_fills(state_path)
    lookup = commit_time_lookup or git_added_commit_utc
    commit_git = lookup(registration_path)
    verdict = evaluate(
        trades,
        registration,
        observed_spec_hash=observed_hash,
        spec_hash_source=hash_source,
        allow_unverified_registration=allow_unverified_registration,
        realistic_fills=realistic_fills,
        counts=counts,
        registration_commit_git=commit_git,
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
        "registration_commit_utc_from_git": commit_git[0],
        "registration_commit_git_source": commit_git[1],
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
        help=(
            "dry runs only: a registration_commit_utc that is null, or that git cannot "
            "confirm, is reported rather than gating. A value git CONTRADICTS still refuses"
        ),
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
            f"net_pnl={verdict['pnl']['net']:+.2f} "
            f"excluded_rate={verdict['counts']['excluded_rate']:.4f} "
            f"saturated_units={u['units_with_saturated_null']} "
            f"degenerate_units={u['units_with_degenerate_null']} "
            f"cross_city_units={u['cross_city_units']}"
            + (
                f" [{u['units_lost_to_cross_city_merging']} trial(s) lost to cross-city "
                "unit merging -- see units.cross_city_note]"
                if u["cross_city_units"]
                else ""
            )
        )
        for w in verdict["warnings"]:
            print(f"  WARNING: {w.get('warning')}: {w.get('note') or ''}")
        for reason in verdict["refusals"]:
            print(f"  REFUSED: {reason}")
    else:
        printable = {k: v for k, v in verdict.items() if k != "fills"}
        print(json.dumps(printable, sort_keys=True, indent=2, default=str))
    if verdict["refused"]:
        return EXIT_REFUSED
    return EXIT_PASS if verdict["verdict"] == "PASS" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
