"""Tests for ``scripts/gate.py`` (PRD FR-5.2, PRD_STRATEGY_FACTORY FR-F3.4).

Synthetic paper record: 60 settled fills over 50 ``target_date`` units, every
fill bought at 0.40 for 20 contracts as a taker (unless a test says otherwise).

Hand computation (written out so the test does not trust the script):

    entry fee, 20 contracts at p = 0.40 (fee_calculator.taker_fee):
        0.07 * 20 * 0.40 * 0.60 = 0.336  -> ceil to the cent -> 0.34 per order
        f = 0.34 / 20 = 0.017 per contract
    breakeven per fill:            q* = p + f = 0.40 + 0.017 = 0.417
    fill PnL, held to settlement:  win  = (1 - 0.40) * 20 - 0.34 = +11.66
                                   loss = (0 - 0.40) * 20 - 0.34 =  -8.34
    unit layout: dates 1..10 carry TWO fills (different cities), dates 11..50 ONE
        two-fill date both win   -> +23.32 (win)
        two-fill date split      ->  +3.32 (win: 11.66 - 8.34 > 0)
        two-fill date both lose  -> -16.68 (loss)

    THE NULL (F3 review defect 1, 2026-09-05). The marginals are fixed -- fill
    i wins with q*_i -- but the JOINT law is not, and the fills inside one unit
    are brackets on a city-day ladder: disjoint brackets are mutually EXCLUSIVE,
    not independent. The gate takes the LEAST FAVOURABLE joint law, i.e. the
    largest w_u any dependence structure could produce:

        w_u = min(1, sum over MINIMAL winning fill sets A of min_{i in A} q*_i)

        single-fill unit:  the one minimal set {i}   -> w = q* = 0.417
        two-fill unit:     one win pays for one loss, so the minimal sets are
                           {1} and {2}               -> w = 2 * 0.417 = 0.834
                           (mutual exclusivity ATTAINS this: exactly one wins)
    p = P[K >= k], K = 10 x Bernoulli(0.834) + 40 x Bernoulli(0.417)
    (Poisson-binomial, exact rationals; _pb_tail below is the test's own DP).

    The superseded independent model said 1 - (1 - 0.417)^2 = 0.660111 for a
    two-fill unit -- BELOW the truth, which makes P[K >= k] too small and the
    test anti-conservative. ``test_true_null_pass_rate_is_at_or_below_nominal``
    measures both: on 50 mutually exclusive pairs the old model passed a TRUE
    null 88.5% of the time against a nominal 5%; the corrected null passes 2.4%.

    PASS scenario  (8 both-win, 1 split, 1 both-lose; 23 of 40 singles win)
        unit wins k = 8 + 1 + 23 = 32 of n = 50
        fill wins    = 16 + 1 + 23 = 40 of 60
        net PnL      = 40 * 11.66 - 20 * 8.34 = 466.40 - 166.80 = +299.60
        p = P[K >= 32] = 0.025896938572745172  -> < 0.05, net > 0, hash ok => PASS
        (k = 32 is the smallest winning count that clears alpha here: P[K >= 31]
         is 0.05008, just the wrong side of 0.05)
        (the pooled q_bar binomial would say P[X >= 32 | 50, 0.417] = 0.00122 --
         21x too small, because it models a split two-fill date at 0.417 not 0.834)

    FAIL scenario  (4 both-win, 2 split, 4 both-lose; 16 of 40 singles win)
        unit wins k = 4 + 2 + 16 = 22 of 50
        fill wins    = 8 + 2 + 16 = 26 of 60
        net PnL      = 26 * 11.66 - 34 * 8.34 = 303.16 - 283.56 = +19.60  (> 0!)
        p = P[K >= 22] = 0.8548809806476853   -> p >= 0.05 is the ONLY failing condition
        (pooled q_bar secondary: P[X >= 22 | 50, 0.417] = 0.4231476244946547)

    EXTREME-PRICE scenario: the 10 two-fill dates pair a 0.08 fill with a 0.92
    fill (20 contracts each; taker fee 0.07*20*0.08*0.92 = 0.10304 -> 0.11 ->
    f = 0.0055 for both):
        q*_a = 0.0855, q*_b = 0.9255
        win@0.08 = (1-0.08)*20-0.11 = +18.29; loss@0.08 = -1.60-0.11 = -1.71
        win@0.92 = +1.60-0.11 = +1.49;        loss@0.92 = -18.40-0.11 = -18.51
        a split date is a LOSS either way (18.29-18.51 < 0; 1.49-1.71 < 0), so
        the ONLY minimal winning set is {a, b} and w_pair = min(q*_a, q*_b)
        = 0.0855  (the independent model said q*_a q*_b = 0.0791; the pooled
        q_bar read 0.5055)
        5 both-win, 3 split, 2 both-lose; 24 of 40 singles win -> k = 29
        p exact = 0.0004348703636931676; pooled q_bar = (10*0.5055+40*0.417)/50
        = 0.4347, P[X >= 29 | 50, 0.4347] = 0.02731021198116019 (63x too large)
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
from datetime import date, timedelta
from fractions import Fraction
from math import comb
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("mp_gate", REPO_ROOT / "scripts" / "gate.py")
gate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(gate)

from src.core.weather_settlement import settlement_close_for  # noqa: E402
from src.ml.trade_journal import TradeJournal, TradeOutcome  # noqa: E402

from src.factory import genome as G  # noqa: E402
from src.factory.promoted import build_spec, write_promoted  # noqa: E402

# A REAL promoted spec (built from a gen-0 seed, dummy provenance hashes) so the
# gate's spec-hash condition goes through ``load_promoted``'s content-hash check.
SPEC = build_spec(
    G.SEEDS["fr31a_taker"],
    family="weather/gfs_mex/taker/v1",
    config_sha256="c" * 64,
    frame_search_sha256="f" * 64,
    calibration_dir="data/calibration",
    calibration_sha256="d" * 64,
    fee_type="quadratic",
    fee_regime_sha256="e" * 64,
    mode="shadow",
    registry_status="CLOSED",
    source="seed",
)
STRATEGY = f"Genome {SPEC.id8}"
SPEC_HASH = SPEC.spec_hash
CITIES = ("NY", "CHI", "MIA", "LAX")
P_ENTRY = 0.40
QTY = 20
ENTRY_FEE = 0.34  # 0.07 * 20 * 0.4 * 0.6 = 0.336 -> ceil -> 0.34
Q_STAR = Fraction(417, 1000)
W_TWO = min(Fraction(1), 2 * Q_STAR)  # least-favourable pair null: 417/500
REG_COMMIT = "2026-05-01T00:00:00+00:00"  # before every synthetic entry_time
ALPHA = 0.05
WIN_PNL = 11.66  # (1 - 0.40) * 20 - 0.34
LOSS_PNL = -8.34  # (0 - 0.40) * 20 - 0.34

P_PASS_UNITS = 0.025896938572745172  # P[K >= 32], exact Poisson-binomial
P_FAIL_UNITS = 0.8548809806476853  # P[K >= 22]
P_PASS_POOLED = 0.0012171990345424294  # P[X >= 32 | 50, 0.417] (secondary)
P_FAIL_POOLED = 0.4231476244946547  # P[X >= 22 | 50, 0.417] (secondary)
P_PASS_FILLS = 8.310514656378994e-05  # P[X >= 40 | 60, 0.417]
P_FAIL_FILLS = 0.4472224935802638  # P[X >= 26 | 60, 0.417]
P_EXTREME_UNITS = 0.0004348703636931676
P_EXTREME_POOLED = 0.02731021198116019


def _exact_tail(n: int, k: int, q: Fraction) -> Fraction:
    """Exact rational binomial upper tail, independent of the script."""
    return sum(Fraction(comb(n, i)) * q ** i * (1 - q) ** (n - i) for i in range(k, n + 1))


def _pb_tail(ws, k: int) -> Fraction:
    """The test's own Poisson-binomial DP (exact rationals)."""
    dist = [Fraction(1)]
    for w in ws:
        nxt = [Fraction(0)] * (len(dist) + 1)
        for j, pj in enumerate(dist):
            nxt[j] += pj * (1 - w)
            nxt[j + 1] += pj * w
        dist = nxt
    return sum(dist[k:], Fraction(0))


def _fake_git(iso):
    """Stand in for ``git log --diff-filter=A`` (tmp_path is not a checkout)."""

    def _lookup(_path):
        if iso is None:
            return None, "test double: git records no ADD of this path"
        return iso, "test double: git log --diff-filter=A"

    return _lookup


def test_hand_constants_are_the_exact_rational_tails():
    layout_ws = [W_TWO] * 10 + [Q_STAR] * 40
    assert abs(float(_pb_tail(layout_ws, 32)) - P_PASS_UNITS) < 1e-15
    assert abs(float(_pb_tail(layout_ws, 22)) - P_FAIL_UNITS) < 1e-15
    assert abs(float(_exact_tail(50, 32, Q_STAR)) - P_PASS_POOLED) < 1e-15
    assert abs(float(_exact_tail(50, 22, Q_STAR)) - P_FAIL_POOLED) < 1e-15
    assert abs(float(_exact_tail(60, 40, Q_STAR)) - P_PASS_FILLS) < 1e-15
    assert abs(float(_exact_tail(60, 26, Q_STAR)) - P_FAIL_FILLS) < 1e-15
    assert W_TWO == Fraction(417, 500) and float(W_TWO) == 0.834
    # 32 really is the smallest winning count that clears alpha on this layout
    assert float(_pb_tail(layout_ws, 31)) >= ALPHA > float(_pb_tail(layout_ws, 32))


# ---------------------------------------------------------------------------
# Synthetic record
# ---------------------------------------------------------------------------
def _ticker(city: str, day: date, strike: float = 84.5) -> str:
    return f"KXHIGH{city}-{day:%y%b%d}-B{strike}".upper().replace("B84.5", "B84.5")


def _layout(both_win: int, split: int, both_lose: int, single_wins: int, pair_prices=None):
    """(target_date, city, won, price) for 10 two-fill dates + 40 single-fill dates."""
    assert both_win + split + both_lose == 10
    pa, pb = pair_prices or (P_ENTRY, P_ENTRY)
    d0 = date(2026, 6, 1)
    out = []
    for i in range(10):
        day = d0 + timedelta(days=i)
        if i < both_win:
            wins = (True, True)
        elif i < both_win + split:
            wins = (True, False)
        else:
            wins = (False, False)
        out.append((day, CITIES[0], wins[0], pa))
        out.append((day, CITIES[1], wins[1], pb))
    for j in range(40):
        day = d0 + timedelta(days=10 + j)
        out.append((day, CITIES[(j + 2) % 4], j < single_wins, P_ENTRY))
    return out


def _fill(day: date, city: str, won: bool, idx: int, price: float = P_ENTRY, *,
          contract_side: str = "YES", stale_no: bool = False, repaired: bool = False):
    symbol = _ticker(city, day)
    entry_time = f"{day - timedelta(days=1)}T15:{idx % 60:02d}:00"
    exp = settlement_close_for(symbol)
    fee = gate.nearest_cent_taker_fee(symbol, price, QTY)
    if contract_side == "YES":
        outcome = "yes" if won else "no"
        exit_price = 1.0 if won else 0.0
    else:
        outcome = "no" if won else "yes"  # a NO wins when the bracket settles no
        exit_price = 1.0 if won else 0.0  # NO-leg payoff (post-724d93c)
        if stale_no:  # pre-724d93c numbers: the YES payoff booked against the NO entry
            exit_price = 1.0 if outcome == "yes" else 0.0
    pnl = (exit_price - price) * QTY  # settlement close: exit fee 0
    journal = {
        "symbol": symbol,
        "strategy_name": STRATEGY,
        "entry_time": entry_time,
        "exit_time": exp.isoformat(),
        "entry_price": price,
        "exit_price": exit_price,
        "quantity": float(QTY),
        "side": "buy",
        "contract_side": contract_side,
        "pnl": pnl,
        "close_reason": "EXPIRATION",
        "settlement_high": 85.0 if outcome == "yes" else 80.0,
        "settlement_outcome": outcome,
        "settlement_spec": {"strike_type": "between", "floor_strike": 84.0, "cap_strike": 85.0},
        "strike_type": "between",
        "floor_strike": 84.0,
        "cap_strike": 85.0,
        # deliberately NO target_date on half the rows: the gate must derive it
        **({"target_date": day.isoformat()} if idx % 2 == 0 else {}),
        **({"repaired_no_side_settlement": True} if repaired else {}),
    }
    state = {
        "id": idx + 1,
        "symbol": symbol,
        "side": "buy",
        "entry_price": price,
        "quantity": float(QTY),
        "open_time": entry_time,
        "close_time": exp.isoformat(),
        "expiration_time": exp.isoformat(),
        "strategy_name": STRATEGY,
        "contract_side": contract_side,
        "entry_fee": fee,
        "exit_fee": 0.0,
        "is_maker": False,
        "fill_type": "taker",
        "exit_price": exit_price,
        "pnl": pnl,
        "reason": "EXPIRATION",
        "settlement_outcome": outcome,
        **({"repaired_no_side_settlement": True} if repaired else {}),
    }
    return journal, state


def _write_record(tmp_path: Path, layout, *, drop_from_state: int = 0, spec_hash=SPEC_HASH,
                  commit_utc=REG_COMMIT, fills=None, state_rows_override=None,
                  journal_rows_override=None, thresholds=None):
    journal_rows, state_rows = [], []
    if fills is None:
        for idx, (day, city, won, price) in enumerate(layout):
            j, s = _fill(day, city, won, idx, price)
            journal_rows.append(j)
            state_rows.append(s)
    else:
        for j, s in fills:
            journal_rows.append(j)
            state_rows.append(s)
    if state_rows_override is not None:
        state_rows = state_rows_override(state_rows)
    if journal_rows_override is not None:
        journal_rows = journal_rows_override(journal_rows)
    # a stray V2 row and an unresolved row must be ignored, never counted
    journal_rows.append({**journal_rows[0], "strategy_name": "Meteorologist V2"})
    journal_rows.append(
        {
            **journal_rows[1],
            "entry_time": "2026-05-01T10:00:00",
            "close_reason": "SETTLEMENT_UNRESOLVED",
            "settlement_error": "no spec",
            "exit_price": P_ENTRY,
            "pnl": 0.0,
        }
    )
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text("".join(json.dumps(r) + "\n" for r in journal_rows), encoding="utf-8")
    state = tmp_path / "exchange_state.json"
    state.write_text(
        json.dumps({"closed_trades": state_rows[drop_from_state:], "positions": []}),
        encoding="utf-8",
    )
    promoted = tmp_path / "promoted.json"
    write_promoted(SPEC, promoted)  # the spec on disk always carries its real hash
    registration = tmp_path / "gate_registration.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "genome_id": SPEC.genome_id,
                "strategy_name": STRATEGY,
                "promoted_spec_path": str(promoted),
                "spec_hash": spec_hash,  # the REGISTERED hash (differs from the spec in the change test)
                "market_family": "KXHIGH",
                "grouping_unit": "target_date",
                "unit_win_rule": "date_pnl_gt_0",
                "thresholds": thresholds or {"n_min": 50, "alpha": 0.05, "net_pnl_gt": 0.0},
                "fee_type": "taker",
                "adverse_fill": 0.01,
                "requires_realistic_fills": True,
                "registration_commit_utc": commit_utc,
            }
        ),
        encoding="utf-8",
    )
    return journal, state, registration


def _run(tmp_path: Path, journal, state, registration, *, git=REG_COMMIT, **kw):
    """Run the gate with the two operator assertions supplied by default.

    ``realistic_fills=True`` and a git double that CONFIRMS the registration
    time: without them every record now refuses (F3 review defects 5 and 6), so
    the scenario tests below would all read the same and prove nothing. The
    tests that own those two conditions override them.
    """
    kw.setdefault("realistic_fills", True)
    out = tmp_path / "verdict.json"
    verdict = gate.run_gate(
        journal_path=str(journal),
        state_path=str(state),
        registration_path=str(registration),
        out_path=str(out),
        commit_time_lookup=_fake_git(git),
        **kw,
    )
    on_disk = json.loads(out.read_text(encoding="utf-8"))
    assert on_disk["verdict"] == verdict["verdict"]
    assert "generated_at" not in on_disk and "timestamp" not in on_disk
    return verdict


# ---------------------------------------------------------------------------
# Binomial + breakeven primitives
# ---------------------------------------------------------------------------
def test_binomial_upper_tail_matches_exact_rational():
    for n, k, q in ((50, 29, 0.417), (50, 22, 0.417), (60, 35, 0.417), (10, 0, 0.3), (10, 10, 0.3)):
        want = float(_exact_tail(n, k, Fraction(q).limit_denominator(10**6)))
        assert abs(gate.binomial_upper_tail(n, k, q) - want) < 1e-12
    assert gate.binomial_upper_tail(5, 6, 0.5) == 0.0
    assert gate.binomial_upper_tail(5, 0, 0.5) == 1.0
    assert gate.binomial_upper_tail(5, 3, 0.0) == 0.0
    assert gate.binomial_upper_tail(5, 3, 1.0) == 1.0


def test_poisson_binomial_reduces_to_the_binomial_with_one_fill_per_unit():
    one = {"entry_price": P_ENTRY, "fee_per_contract": 0.017, "quantity": float(QTY)}
    w = gate.unit_null_win_probability([one])
    assert abs(float(w) - 0.417) < 1e-15
    for k in (0, 1, 22, 29, 32, 50, 51):
        exact = float(_exact_tail(50, k, Fraction(0.40) + Fraction(0.017))) if k <= 50 else 0.0
        assert abs(float(gate.poisson_binomial_upper_tail([w] * 50, k)) - exact) < 1e-15
        assert abs(float(gate.poisson_binomial_upper_tail([w] * 50, k)) - gate.binomial_upper_tail(50, k, 0.417)) < 1e-12


def test_minimal_winning_sets_generate_the_unit_win_event():
    """The win event is ``sum_{i in A} qty_i > sum_i (p_i + f_i) qty_i``, upward closed."""
    one = {"entry_price": P_ENTRY, "fee_per_contract": 0.017, "quantity": float(QTY)}
    # one win pays for one loss -> either fill alone is a minimal winning set
    assert gate.unit_minimal_winning_sets([one, one]) == [(0,), (1,)]
    # one win does NOT pay for two losses -> any two of the three
    assert gate.unit_minimal_winning_sets([one] * 3) == [(0, 1), (0, 2), (1, 2)]
    # extreme pair: only both-win is profitable
    a = {"entry_price": 0.08, "fee_per_contract": 0.0055, "quantity": 20.0}
    b = {"entry_price": 0.92, "fee_per_contract": 0.0055, "quantity": 20.0}
    assert gate.unit_minimal_winning_sets([a, b]) == [(0, 1)]
    # brute-force cross-check of the generated up-set against direct enumeration
    fills = [
        {"entry_price": 0.30, "fee_per_contract": 0.01, "quantity": 5.0},
        {"entry_price": 0.55, "fee_per_contract": 0.02, "quantity": 11.0},
        {"entry_price": 0.61, "fee_per_contract": 0.02, "quantity": 3.0},
    ]
    minimal = gate.unit_minimal_winning_sets(fills)
    for mask in range(8):
        members = {i for i in range(3) if mask & (1 << i)}
        pnl = sum(
            (1 - f["entry_price"] - f["fee_per_contract"]) * f["quantity"]
            if i in members
            else -(f["entry_price"] + f["fee_per_contract"]) * f["quantity"]
            for i, f in enumerate(fills)
        )
        assert (pnl > 1e-12) == any(set(A) <= members for A in minimal), members


def test_unit_null_is_the_least_favourable_dependence_not_independence():
    """F3 review defect 1: fills in a unit are ladder brackets, not independent draws."""
    one = {"entry_price": P_ENTRY, "fee_per_contract": 0.017, "quantity": float(QTY)}
    # TWO mutually exclusive brackets: exactly one wins with probability q1 + q2,
    # and one win pays for one loss, so the unit wins with 0.834 -- NOT the
    # 0.660111 the independent model reported.
    w2 = gate.unit_null_win_probability([one, one])
    assert abs(float(w2) - 0.834) < 1e-15
    assert float(gate.unit_null_win_probability_independent([one, one])) == pytest.approx(0.660111)
    assert w2 > gate.unit_null_win_probability_independent([one, one])
    # one fill per unit is untouched: exactly q*
    assert abs(float(gate.unit_null_win_probability([one])) - 0.417) < 1e-15
    # extreme pair: only both-win wins -> min(q*) by Frechet, just above the product
    a = {"entry_price": 0.08, "fee_per_contract": 0.0055, "quantity": 20.0}
    b = {"entry_price": 0.92, "fee_per_contract": 0.0055, "quantity": 20.0}
    assert float(gate.unit_null_win_probability([a, b])) == pytest.approx(0.0855, abs=1e-15)
    assert float(gate.unit_null_win_probability_independent([a, b])) == pytest.approx(0.0855 * 0.9255)
    # a unit whose PnL can only be exactly 0 is a loss (exact rationals, no rounding win)
    zero = {"entry_price": 0.5, "fee_per_contract": 0.0, "quantity": 2.0}
    assert gate.unit_null_win_probability([zero, zero]) == Fraction(1, 2)  # both-win, Frechet
    assert gate.unit_null_win_probability_independent([zero, zero]) == Fraction(1, 4)
    # THREE brackets at 0.417: 3 q* > 1, so no dependence structure is excluded
    # and the unit carries no evidence at all. The gate says so rather than
    # inventing a copula.
    assert gate.unit_null_win_probability([one] * 3) == Fraction(1)
    with pytest.raises(gate.GateRefusal):
        gate.unit_null_win_probability([one] * (gate.MAX_FILLS_PER_UNIT + 1))


def test_the_null_upper_bounds_the_independent_model_on_random_units():
    """The independent law is ONE admissible coupling, so the bound must dominate it."""
    rng = random.Random(20260905)
    for _ in range(200):
        fills = [
            {
                "entry_price": round(rng.uniform(0.02, 0.95), 2),
                "fee_per_contract": round(rng.uniform(0.0, 0.03), 4),
                "quantity": float(rng.randint(1, 30)),
            }
            for _ in range(rng.choice([1, 2, 3, 4]))
        ]
        bound = gate.unit_null_win_probability(fills)
        assert bound >= gate.unit_null_win_probability_independent(fills)
        assert Fraction(0) <= bound <= Fraction(1)


def test_true_null_pass_rate_is_at_or_below_nominal():
    """The gate must not pass a zero-EV strategy more often than alpha.

    The DGP is the dependence the independence unit exists to absorb: every
    settlement day carries TWO disjoint brackets on ONE city-day ladder, so
    under a zero-EV null exactly one of them settles YES with probability
    q*_1 + q*_2 = 0.834 and neither does with 0.166. One win pays for one loss,
    so the unit is profitable exactly when one of them lands.

    Both the exact PASS probability (the whole sampling distribution, not a
    sample of it) and a Monte-Carlo run must sit at or below alpha. With the
    superseded independent null the same DGP passed 88.5% of the time.
    """
    trades = _sim_trades([True] * 50)
    modelled = [u["_w_u"] for u in gate.group_units(trades)]
    assert len(modelled) == 50

    # The gate's own p, tabulated over every attainable winning count. p depends
    # on the outcomes only through k, so this IS the gate's decision rule.
    p_of_k = {k: float(gate.poisson_binomial_upper_tail(modelled, k)) for k in range(51)}
    k_crit = min(k for k in range(51) if p_of_k[k] < ALPHA)

    # EXACT true-null PASS probability: K ~ 50 x Bernoulli(0.834) under the DGP.
    # This is the whole sampling distribution, not a sample of it.
    truth = [Fraction(834, 1000)] * 50
    exact_rate = float(gate.poisson_binomial_upper_tail(truth, k_crit))
    assert exact_rate <= ALPHA, (
        f"true-null PASS rate {exact_rate:.4f} exceeds alpha {ALPHA} "
        f"(the gate passes at k >= {k_crit}, and a zero-EV strategy reaches that "
        f"{exact_rate:.1%} of the time)"
    )

    # ... and measured, so the claim is not only algebraic
    rng = random.Random(20260905)
    reps, passes = 4000, 0
    for _ in range(reps):
        k = sum(1 for _ in range(50) if rng.random() < 0.834)
        net = WIN_PNL * k + 2 * LOSS_PNL * (50 - k) + LOSS_PNL * k
        if p_of_k[k] < ALPHA and net > 0.0:
            passes += 1
    measured = passes / reps
    assert measured <= ALPHA, f"measured true-null PASS rate {measured} exceeds alpha {ALPHA}"

    # the hand numbers behind those rates
    assert k_crit == 47
    assert all(float(w) == pytest.approx(0.834, abs=1e-15) for w in modelled)
    assert exact_rate == pytest.approx(0.02448, abs=1e-4)

    # the gate's own end-to-end verdict agrees with the p(k) memo at the boundary
    for k in (k_crit - 1, k_crit):
        v = _evaluate_sim(k)
        assert abs(v["units"]["p_upper_tail"] - p_of_k[k]) < 1e-15
        assert (v["verdict"] == "PASS") == (k >= k_crit), k
    assert _evaluate_sim(50)["verdict"] == "PASS"  # the test is not vacuous

    # the model this replaced, scored on the same true null, on the same layout
    one = {"entry_price": P_ENTRY, "fee_per_contract": ENTRY_FEE / QTY, "quantity": float(QTY)}
    superseded = [gate.unit_null_win_probability_independent([one, one])] * 50
    k_crit_old = min(
        k for k in range(51) if float(gate.poisson_binomial_upper_tail(superseded, k)) < ALPHA
    )
    old_rate = float(gate.poisson_binomial_upper_tail(truth, k_crit_old))
    assert k_crit_old == 39 and old_rate > 0.85  # 0.8853: an 18x over-run of alpha


def _sim_trades(unit_wins, *, price: float = P_ENTRY):
    """One settled unit per entry: two mutually exclusive brackets on one ladder.

    Built in the shape ``collect_settled_trades`` emits, so ``group_units`` and
    ``evaluate`` see exactly what a real record would give them.
    """
    d0 = date(2026, 6, 1)
    fee_pc = ENTRY_FEE / QTY
    out = []
    for i, won in enumerate(unit_wins):
        day = d0 + timedelta(days=i)
        for leg, fill_won in enumerate((bool(won), False)):  # one win pays for one loss
            symbol = f"KXHIGH{CITIES[0]}-{day:%y%b%d}-B{84.5 + leg}".upper()
            pnl = (1.0 - price) * QTY if fill_won else (0.0 - price) * QTY
            out.append(
                {
                    "symbol": symbol,
                    "strategy_name": STRATEGY,
                    "target_date": day.isoformat(),
                    "entry_time": f"{day - timedelta(days=1)}T15:00:0{leg}",
                    "exit_time": f"{day + timedelta(days=1)}T04:00:00+00:00",
                    "contract_side": "YES",
                    "entry_price": price,
                    "quantity": float(QTY),
                    "exit_price": 1.0 if fill_won else 0.0,
                    "pnl": pnl,
                    "source": "simulation",
                    "maker_booked": False,
                    "repaired": False,
                    "entry_fee": ENTRY_FEE,
                    "fee_source": "closed_trades.entry_fee",
                    "fee_per_contract": fee_pc,
                    "net_pnl": pnl - ENTRY_FEE,
                    "won": (pnl - ENTRY_FEE) > 0.0,
                    "q_star": price + fee_pc,
                }
            )
    return out


def _evaluate_sim(k_wins: int):
    """``evaluate`` on a simulated record with exactly ``k_wins`` winning units."""
    trades = _sim_trades([True] * k_wins + [False] * (50 - k_wins))
    return gate.evaluate(
        trades,
        {
            "spec_hash": SPEC_HASH,
            "strategy_name": STRATEGY,
            "grouping_unit": "target_date",
            "thresholds": {"n_min": 50, "alpha": ALPHA, "net_pnl_gt": 0.0},
            "fee_type": "taker",
            "requires_realistic_fills": True,
            "registration_commit_utc": REG_COMMIT,
        },
        observed_spec_hash=SPEC_HASH,
        spec_hash_source="test",
        realistic_fills=True,
        counts={},
        registration_commit_git=(REG_COMMIT, "test double"),
    )


def test_breakeven_is_price_plus_fee_per_contract():
    assert gate.breakeven_win_rate(0.40, 0.017) == pytest.approx(0.417)
    fee = gate.nearest_cent_taker_fee("KXHIGHNY-26JUN01-B84.5", 0.40, 20)
    assert fee == pytest.approx(0.34)
    assert gate.nearest_cent_taker_fee("KXHIGHNY-26JUN01-B84.5", 0.08, 20) == pytest.approx(0.11)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def test_pass_scenario_reproduces_hand_computed_p(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)

    assert v["refused"] is False and v["refusals"] == []
    assert v["units"]["n"] == 50
    assert v["units"]["k_wins"] == 32
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["units"]["p_exact_str"] and "/" in v["units"]["p_exact_str"]
    assert abs(v["units"]["p_pooled_qbar_secondary"] - P_PASS_POOLED) < 1e-12
    assert v["units"]["null_win_rate_q_bar"] == pytest.approx(0.417, abs=1e-12)
    assert v["per_fill_secondary"]["n"] == 60
    assert v["per_fill_secondary"]["k_wins"] == 40
    assert abs(v["per_fill_secondary"]["p_upper_tail"] - P_PASS_FILLS) < 1e-12
    assert v["per_fill_secondary"]["gating"] is False
    assert v["pnl"]["net"] == pytest.approx(299.60, abs=1e-9)
    assert v["pnl"]["entry_fees"] == pytest.approx(60 * 0.34, abs=1e-9)
    c = v["conditions"]
    assert c["n_units_ge_n_min"]["ok"] and c["p_lt_alpha"]["ok"]
    assert c["net_pnl_gt_0"]["ok"] and c["spec_hash_unchanged"]["ok"]
    assert c["registered_before_first_trade"]["ok"] is True and c["registered_before_first_trade"]["gating"] is True
    assert c["registered_before_first_trade"]["verified"] is True
    assert c["fee_type_matches"]["ok"] is True
    assert c["realistic_fills_enabled"]["ok"] is True
    assert c["excluded_rate_within_bound"]["ok"] is True
    assert c["excluded_rate_within_bound"]["observed"] == 0.0
    assert v["not_applicable"] == []
    assert v["verdict"] == "PASS"
    assert v["units"]["units_with_multiple_fills"] == 10
    assert v["units"]["units_with_saturated_null"] == 0
    assert v["counts"]["excluded"] == {"other_strategy": 1, "settlement_unresolved": 1}
    assert v["counts"]["excluded_rows"] == []  # scope filters are not "dropped rows"
    assert v["counts"]["quality_excluded"] == 0
    assert v["counts"]["corrupt_rows"] == []
    assert v["counts"]["fills_by_fee_source"] == {"closed_trades.entry_fee": 60}
    # the only warning is the cross-city governance report: this record's ten
    # two-fill dates are two CITIES each, which the pre-registered unit merges
    assert [w["warning"] for w in v["warnings"]] == ["cross_city_units_merged"]
    # unit-level null: two-fill dates at 0.834 (least favourable), singles at 0.417
    two = [u for u in v["unit_table"] if u["n_fills"] == 2]
    one = [u for u in v["unit_table"] if u["n_fills"] == 1]
    assert len(two) == 10 and len(one) == 40
    assert all(abs(u["null_win_probability"] - 0.834) < 1e-12 for u in two)
    assert all(u["n_minimal_winning_sets"] == 2 and u["min_fills_to_win"] == 1 for u in two)
    assert all(abs(u["null_win_probability"] - 0.417) < 1e-12 for u in one)
    assert all(abs(u["q_star"] - 0.417) < 1e-12 for u in v["unit_table"])
    # the superseded independent model is reported beside it, non-gating
    assert all(u["null_win_probability_independent"] == pytest.approx(0.660111) for u in two)
    assert all(u["null_saturated"] is False for u in v["unit_table"])


def test_fail_scenario_p_is_the_only_failing_condition(tmp_path):
    layout = _layout(both_win=4, split=2, both_lose=4, single_wins=16)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)

    assert v["units"]["n"] == 50 and v["units"]["k_wins"] == 22
    assert abs(v["units"]["p_upper_tail"] - P_FAIL_UNITS) < 1e-12
    assert abs(v["units"]["p_pooled_qbar_secondary"] - P_FAIL_POOLED) < 1e-12
    assert abs(v["per_fill_secondary"]["p_upper_tail"] - P_FAIL_FILLS) < 1e-12
    assert v["pnl"]["net"] == pytest.approx(19.60, abs=1e-9)
    c = v["conditions"]
    assert c["p_lt_alpha"]["ok"] is False
    assert c["n_units_ge_n_min"]["ok"] and c["net_pnl_gt_0"]["ok"] and c["spec_hash_unchanged"]["ok"]
    assert v["failing"] == ["p_lt_alpha"]
    assert v["verdict"] == "FAIL"


def test_extreme_price_pairs_use_the_least_favourable_unit_null(tmp_path):
    """0.08/0.92 pairs: only a both-win date is profitable, so w_pair = min(q*)."""
    layout = _layout(both_win=5, split=3, both_lose=2, single_wins=24, pair_prices=(0.08, 0.92))
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)
    assert v["units"]["n"] == 50 and v["units"]["k_wins"] == 29
    f = Fraction(11, 2000)  # 0.11 / 20
    w_pair = min(Fraction(8, 100) + f, Fraction(92, 100) + f)
    hand = _pb_tail([w_pair] * 10 + [Q_STAR] * 40, 29)
    assert abs(float(hand) - P_EXTREME_UNITS) < 1e-15
    assert abs(v["units"]["p_upper_tail"] - float(hand)) < 1e-12
    assert abs(v["units"]["p_pooled_qbar_secondary"] - P_EXTREME_POOLED) < 1e-9
    two = [u for u in v["unit_table"] if u["n_fills"] == 2]
    assert all(abs(u["null_win_probability"] - float(w_pair)) < 1e-12 for u in two)
    assert all(u["n_minimal_winning_sets"] == 1 and u["min_fills_to_win"] == 2 for u in two)
    assert v["verdict"] == "PASS"  # exact p 0.00043 < 0.05


def test_spec_hash_change_fails_an_otherwise_passing_record(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout, spec_hash="b" * 64)
    v = _run(tmp_path, journal, state, registration)
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["conditions"]["spec_hash_unchanged"]["ok"] is False
    # observed = the spec's verified content hash; registered = the stale "b"*64
    assert v["conditions"]["spec_hash_unchanged"]["observed"] == SPEC_HASH
    assert v["conditions"]["spec_hash_unchanged"]["registered"] == "b" * 64
    assert "content hash verified" in v["conditions"]["spec_hash_unchanged"]["source"]
    assert v["verdict"] == "FAIL"


def test_tampered_spec_file_fails_even_when_registration_matches(tmp_path):
    """Editing the promoted JSON after registration breaks its own hash -> FAIL, no fallback."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    promoted = tmp_path / "promoted.json"
    doc = json.loads(promoted.read_text(encoding="utf-8"))
    doc["adverse_fill"] = 0.02
    promoted.write_text(json.dumps(doc), encoding="utf-8")
    v = _run(tmp_path, journal, state, registration)
    cond = v["conditions"]["spec_hash_unchanged"]
    assert cond["ok"] is False and cond["observed"] is None
    assert "does not verify" in cond["source"]
    assert v["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# Registration time (F3 review defect 6)
# ---------------------------------------------------------------------------
def test_registration_commit_time_is_gating_by_default(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout, commit_utc=None)
    v = _run(tmp_path, journal, state, registration, git=None)
    c = v["conditions"]["registered_before_first_trade"]
    assert c["ok"] is False and c["gating"] is True and c["verified"] is False
    assert "not recorded" in c["note"]
    assert v["verdict"] == "FAIL" and v["failing"] == ["registered_before_first_trade"]
    # dry-run downgrade: reported, not gating, recorded in the verdict
    v2 = _run(tmp_path, journal, state, registration, git=None, allow_unverified_registration=True)
    c2 = v2["conditions"]["registered_before_first_trade"]
    assert c2["ok"] is None and c2["gating"] is False and "UNVERIFIED" in c2["note"]
    assert v2["allow_unverified_registration"] is True
    assert "registered_before_first_trade" in v2["not_applicable"]
    assert v2["verdict"] == "PASS"
    # a registration dated AFTER the first fill fails even when filled in
    late = "2026-06-15T00:00:00+00:00"
    journal, state, registration = _write_record(tmp_path, layout, commit_utc=late)
    v3 = _run(tmp_path, journal, state, registration, git=late)
    assert v3["conditions"]["registered_before_first_trade"]["ok"] is False
    assert v3["conditions"]["registered_before_first_trade"]["verified"] is True
    assert v3["verdict"] == "FAIL"


def test_registration_commit_utc_is_reconciled_against_git(tmp_path):
    """A hand-typed timestamp does not establish when the file was committed."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    # git says the registration was added AFTER the run started; the typed value
    # claims it predates it. The gate refuses rather than picking a side.
    v = _run(tmp_path, journal, state, registration, git="2026-06-20T09:00:00+00:00")
    c = v["conditions"]["registered_before_first_trade"]
    assert c["ok"] is False and c["verified"] is False
    assert "2026-06-20" in c["disagreement"] and "ADDED" in c["disagreement"]
    assert v["refused"] is True and v["verdict"] == "FAIL"
    assert any("does not reconcile with git" in r for r in v["refusals"])
    # --allow-unverified-registration does NOT paper over a contradiction
    v2 = _run(
        tmp_path, journal, state, registration,
        git="2026-06-20T09:00:00+00:00", allow_unverified_registration=True,
    )
    assert v2["refused"] is True and v2["verdict"] == "FAIL"
    # git cannot answer -> UNVERIFIED, and the condition still GATES by default:
    # the typed value alone was the whole defect.
    v3 = _run(tmp_path, journal, state, registration, git=None)
    c3 = v3["conditions"]["registered_before_first_trade"]
    assert c3["ok"] is False and c3["verified"] is False and c3["gating"] is True
    assert c3["note"].startswith("UNVERIFIED") and "git records no ADD" in c3["git_source"]
    assert v3["verdict"] == "FAIL" and v3["failing"] == ["registered_before_first_trade"]
    assert v3["refused"] is False  # unverifiable is a FAIL, a contradiction is a refusal
    # the same instant expressed in another offset still reconciles
    v4 = _run(tmp_path, journal, state, registration, git="2026-04-30T20:00:00-04:00")
    assert v4["conditions"]["registered_before_first_trade"]["verified"] is True
    assert v4["verdict"] == "PASS"
    assert v4["registration"]["registration_commit_utc_from_git"] == "2026-04-30T20:00:00-04:00"


def test_git_added_commit_utc_reads_this_repository():
    """The real lookup, against a file that is genuinely tracked here."""
    tracked = REPO_ROOT / "configs" / "factory" / "gate_registration.template.json"
    iso, source = gate.git_added_commit_utc(str(tracked))
    if iso is None:  # shallow clone / exported tarball / no git
        pytest.skip(f"git could not answer for a tracked file: {source}")
    assert "git log" in source
    assert gate._as_utc(iso) is not None
    # a path git has never seen returns None with a reason, never a guess
    missing, why = gate.git_added_commit_utc(str(REPO_ROOT / "configs" / "factory" / "no_such_file.json"))
    assert missing is None and "unverified" in why


# ---------------------------------------------------------------------------
# Realistic fills (F3 review defect 5)
# ---------------------------------------------------------------------------
def test_realistic_fills_condition(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration, realistic_fills=False)
    assert v["conditions"]["realistic_fills_enabled"]["ok"] is False
    assert v["verdict"] == "FAIL" and v["failing"] == ["realistic_fills_enabled"]
    v = _run(tmp_path, journal, state, registration, realistic_fills=True)
    assert v["conditions"]["realistic_fills_enabled"]["ok"] is True
    assert v["not_applicable"] == [] and v["verdict"] == "PASS"
    rc = gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
         "--realistic-fills", "false", "--allow-unverified-registration",
         "--fill-config", "", "--quiet"]
    )
    assert rc == gate.EXIT_FAIL


def test_unknown_realistic_fills_is_refused_not_downgraded(tmp_path):
    """``requires_realistic_fills`` was a no-op: the state never records the flag."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    # the exchange state genuinely carries no realistic_fills key
    assert gate._state_realistic_fills(str(state)) is None
    v = _run(tmp_path, journal, state, registration, realistic_fills=None)
    c = v["conditions"]["realistic_fills_enabled"]
    assert c["ok"] is False and c["gating"] is True and "REFUSED" in c["note"]
    assert v["refused"] is True and v["verdict"] == "FAIL"
    assert any("requires_realistic_fills" in r for r in v["refusals"])
    assert "realistic_fills_enabled" not in v["not_applicable"]
    rc = gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
         "--allow-unverified-registration", "--fill-config", "", "--quiet"]
    )
    assert rc == gate.EXIT_REFUSED


# ---------------------------------------------------------------------------
# Realistic fills, part 2: the run's own record (F4 blocker 2, 2026-09-05)
# ---------------------------------------------------------------------------
# Refusing on "unknown" fixed the no-op but left FR-5.2 unsatisfiable: nothing in
# the runtime could turn realistic fills ON, and nothing could record that it
# had, so the terminal gate could never PASS either. scripts/run_dashboard.py now
# writes an append-only fill-configuration log (default-OFF switch,
# MP_REALISTIC_FILLS) and the gate joins it to the paper record BY TIME. The
# synthetic record's entry_times run 2026-05-31T15:00 .. 2026-07-19T15:xx.
FC_RUN_START = "2026-05-30T00:00:00+00:00"
FC_RUN_STOP = "2026-07-21T00:00:00+00:00"


def _fill_config_log(tmp_path, *, realistic=True, start=FC_RUN_START, stop=FC_RUN_STOP,
                     run_id="sandbox-run", name="fill_config.jsonl"):
    """A log shaped exactly like the orchestrator's, bracketing the record."""
    rows = [
        {
            "record": "fill_config",
            "schema_version": 1,
            "run_id": run_id,
            "event": event,
            "run_started_utc": start,
            "observed_utc": when,
            "heartbeat_sec": 300.0,
            "realistic_fills": realistic,
            "writer": "scripts/run_dashboard.py:OrchestratorEngine",
            "host": "maia",
        }
        for event, when in (("start", start), ("stop", stop))
    ]
    path = tmp_path / name
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def test_a_recorded_realistic_fills_run_evidences_the_gate_on_its_own(tmp_path):
    """The terminal FR-5.2 gate can now PASS -- on a RUN THAT RECORDED ITSELF.

    No ``--realistic-fills``: the only thing saying the fills were realistic is
    the log the orchestrator wrote while it owned the exchange, and every
    admitted settled fill falls inside that run's window.
    """
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    log = _fill_config_log(tmp_path)
    v = _run(tmp_path, journal, state, registration, realistic_fills=None, fill_config_path=log)
    c = v["conditions"]["realistic_fills_enabled"]
    assert c["ok"] is True and c["gating"] is True
    assert c["source"] == "fill_config_log"
    assert c["fill_config"]["fills_covered"] == 60
    assert c["fill_config"]["fills_uncovered"] == 0
    assert v["refused"] is False and v["refusals"] == []
    assert v["verdict"] == "PASS"


def test_the_verdict_is_bound_to_the_exact_log_it_was_scored_against(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    log = _fill_config_log(tmp_path)
    v = _run(tmp_path, journal, state, registration, realistic_fills=None, fill_config_path=log)
    assert v["inputs"]["fill_config_log"] == log
    assert v["inputs"]["fill_config_log_sha256"] == gate.sha256_file(log)
    assert v["inputs"]["realistic_fills_source"] == "fill_config_log"
    assert v["inputs"]["realistic_fills"] is True


def test_a_log_that_stops_before_the_trades_evidences_nothing(tmp_path):
    """Staleness must refuse, not default to yes: it describes another run."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    log = _fill_config_log(tmp_path, stop="2026-06-05T00:00:00+00:00")
    v = _run(tmp_path, journal, state, registration, realistic_fills=None, fill_config_path=log)
    c = v["conditions"]["realistic_fills_enabled"]
    assert c["ok"] is False and "REFUSED" in c["note"]
    assert c["fill_config"]["fills_uncovered"] > 0
    assert v["refused"] is True and v["verdict"] == "FAIL"


def test_a_log_recording_an_off_run_fails_the_condition(tmp_path):
    """A definite answer, so a FAIL rather than a refusal."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    log = _fill_config_log(tmp_path, realistic=False)
    v = _run(tmp_path, journal, state, registration, realistic_fills=None, fill_config_path=log)
    assert v["conditions"]["realistic_fills_enabled"]["ok"] is False
    assert v["refused"] is False
    assert v["verdict"] == "FAIL" and v["failing"] == ["realistic_fills_enabled"]


def test_an_operator_assertion_cannot_overrule_the_recorded_run(tmp_path):
    """``--realistic-fills true`` over a log that says false is a REFUSAL."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    log = _fill_config_log(tmp_path, realistic=False)
    v = _run(tmp_path, journal, state, registration, realistic_fills=True, fill_config_path=log)
    assert v["refused"] is True and v["verdict"] == "FAIL"
    assert any("CONTRADICT" in r for r in v["refusals"])
    assert v["conditions"]["realistic_fills_enabled"]["source"] is None


def _forged_log(tmp_path, **over):
    """One hand-written line, shaped like the orchestrator's but fabricated."""
    row = {
        "record": "fill_config",
        "schema_version": 1,
        "run_id": "FORGED",
        "event": "stop",
        "run_started_utc": "1970-01-01T00:00:00+00:00",
        "observed_utc": "2030-01-01T00:00:00+00:00",
        "heartbeat_sec": 300.0,
        "realistic_fills": True,
    }
    row.update(over)
    path = tmp_path / "forged.jsonl"
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def test_one_forged_line_cannot_evidence_the_whole_record(tmp_path):
    """The edge the F4 review actually broke: the WINDOW, not the grace.

    ``FILL_CONFIG_MAX_GRACE_S`` bounded only the FORWARD extension past a run's
    last stamp. ``window_start`` was taken verbatim from the record's own
    ``run_started_utc`` and, for an ``event="stop"`` record, ``window_end`` was
    taken verbatim from its ``observed_utc`` -- both attacker-supplied, both
    unbounded. So THIS SINGLE LINE, with no operator assertion, produced a
    1970 -> 2030 window, "all 60 admitted fill(s) fall inside a run window that
    recorded realistic_fills=true", and verdict PASS.

    Three independent bounds now stop it; each is asserted alone below.
    """
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration, realistic_fills=None,
             fill_config_path=_forged_log(tmp_path))
    fc = v["conditions"]["realistic_fills_enabled"]["fill_config"]
    assert fc["value"] is None and fc["fills_covered"] == 0
    assert fc["runs"] == [] and fc["future_dated_records"] == 1
    assert v["conditions"]["realistic_fills_enabled"]["ok"] is False
    assert v["refused"] is True and v["verdict"] == "FAIL"


def test_each_forged_edge_is_bounded_on_its_own(tmp_path):
    """Peel the defences apart so none of them is load-bearing by accident."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)

    def _fc(**over):
        v = _run(tmp_path, journal, state, registration, realistic_fills=None,
                 fill_config_path=_forged_log(tmp_path, **over))
        assert v["refused"] is True and v["verdict"] == "FAIL"
        return v["conditions"]["realistic_fills_enabled"]["fill_config"]

    # 1. a future ``observed_utc`` is not a stamp -- the heartbeat variant of the
    #    same forgery is dropped exactly like the stop variant.
    assert _fc(event="heartbeat")["future_dated_records"] == 1

    # 2. one line, however honest its timestamps, describes an INSTANT: a run
    #    must stamp a start AND a strictly later record before it covers a fill.
    fc = _fc(event="start", observed_utc="2026-07-25T00:00:00+00:00")
    assert fc["runs_evidencing"] == 0 and len(fc["runs"]) == 1
    assert fc["runs"][0]["evidencing"] is False
    assert "strictly later record" in fc["runs"][0]["not_evidencing"]

    # 3. and an ancient CLAIMED start cannot reach back over the record: it is
    #    clamped to the run's own first stamp minus the capped grace.
    path = tmp_path / "clamped.jsonl"
    row = {"record": "fill_config", "schema_version": 1, "run_id": "FORGED",
           "run_started_utc": "1970-01-01T00:00:00+00:00", "heartbeat_sec": 300.0,
           "realistic_fills": True}
    path.write_text(
        json.dumps({**row, "event": "start",
                    "observed_utc": "2026-07-25T00:00:00+00:00"}, sort_keys=True) + "\n"
        + json.dumps({**row, "event": "stop",
                      "observed_utc": "2026-07-25T00:05:00+00:00"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    v = _run(tmp_path, journal, state, registration, realistic_fills=None,
             fill_config_path=str(path))
    run = v["conditions"]["realistic_fills_enabled"]["fill_config"]["runs"][0]
    assert run["evidencing"] is True and run["start_clamped"] is True
    assert run["claimed_run_started_utc"] == "1970-01-01T00:00:00+00:00"
    # first stamp minus this run's own capped grace (300 s), not 1970
    assert run["window_start"] == "2026-07-24T23:55:00+00:00"
    assert v["conditions"]["realistic_fills_enabled"]["fill_config"]["fills_covered"] == 0
    assert v["refused"] is True and v["verdict"] == "FAIL"

    # the grace a record may claim for ITSELF is still capped (the edge the
    # superseded test guarded); it is now one bound of three, not the only one.
    fc = _fc(event="start", observed_utc="2026-07-25T00:00:00+00:00", heartbeat_sec=10**9)
    assert fc["runs"][0]["grace_sec"] == gate.FILL_CONFIG_MAX_GRACE_S


def test_cli_reads_the_fill_config_log(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    log = _fill_config_log(tmp_path)
    argv = ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
            "--allow-unverified-registration", "--quiet", "--fill-config"]
    assert gate.main(argv + [log]) == gate.EXIT_PASS
    assert gate.main(argv + [str(tmp_path / "absent.jsonl")]) == gate.EXIT_REFUSED
    assert gate.main(argv + [""]) == gate.EXIT_REFUSED


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
def test_maker_booked_fill_under_taker_registration_fails(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _maker(rows):
        rows[3] = {**rows[3], "is_maker": True, "fill_type": "maker", "entry_fee": 0.0}
        return rows

    journal, state, registration = _write_record(tmp_path, layout, state_rows_override=_maker)
    v = _run(tmp_path, journal, state, registration)
    c = v["conditions"]["fee_type_matches"]
    assert c["ok"] is False and len(c["maker_booked_fills"]) == 1
    assert v["verdict"] == "FAIL" and v["failing"] == ["fee_type_matches"]
    # the zero booked fee did not lower the breakeven: the taker fee was recomputed (max rule)
    fill = [f for f in v["fills"] if f["maker_booked"]][0]
    assert fill["entry_fee"] == pytest.approx(0.34) and "max rule" in fill["fee_source"]
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12


def test_zero_or_undersized_booked_fee_cannot_lower_the_breakeven(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _cheap(rows):
        for i in range(10):
            rows[i] = {**rows[i], "entry_fee": 0.0}  # taker-flagged, fee missing/zero
        return rows

    journal, state, registration = _write_record(tmp_path, layout, state_rows_override=_cheap)
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["fills_by_fee_source"] == {
        "closed_trades.entry_fee": 50,
        "recomputed_taker (booked fee below taker; max rule)": 10,
    }
    assert all(abs(u["q_star"] - 0.417) < 1e-12 for u in v["unit_table"])
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["verdict"] == "PASS"


def test_quantity_mismatch_uses_the_journal_row_and_warns(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _qty(rows):
        rows[5] = {**rows[5], "quantity": 5.0, "entry_fee": 0.09}  # fee booked at size 5
        return rows

    journal, state, registration = _write_record(tmp_path, layout, state_rows_override=_qty)
    v = _run(tmp_path, journal, state, registration)
    qty_warnings = [w for w in v["warnings"] if w["warning"] == "quantity_mismatch_journal_vs_state"]
    assert len(qty_warnings) == 1
    w = qty_warnings[0]
    assert w["journal_quantity"] == 20.0 and w["state_quantity"] == 5.0
    fill = [f for f in v["fills"] if f["symbol"] == w["symbol"] and f["entry_time"] == w["entry_time"]][0]
    assert fill["quantity"] == 20.0
    # 0.09 at size 5 rescales to 0.36 at size 20 > the 0.34 taker fee -> the booked (rescaled) fee stands
    assert fill["entry_fee"] == pytest.approx(0.36) and fill["fee_source"] == "closed_trades.entry_fee"
    assert v["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# NO-side settlement
# ---------------------------------------------------------------------------
def test_no_side_fills_repaired_state_and_cleared_closed_trades_still_pass(tmp_path):
    """NO fills post-724d93c (or repaired) count normally, even after a cycle reset clears the state."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    fills = []
    for idx, (day, city, won, price) in enumerate(layout):
        no_side = idx % 3 == 0
        fills.append(_fill(day, city, won, idx, price, contract_side="NO" if no_side else "YES",
                           repaired=no_side))
    journal, state, registration = _write_record(tmp_path, layout, fills=fills)
    v = _run(tmp_path, journal, state, registration)
    assert v["verdict"] == "PASS" and v["units"]["k_wins"] == 32
    assert v["counts"]["stale_no_side_rows"] == []
    assert v["counts"]["corrupt_rows"] == []
    # cycle reset: closed_trades cleared, journal rows carry the repaired marker
    (tmp_path / "exchange_state.json").write_text(json.dumps({"closed_trades": [], "positions": []}), "utf-8")
    v2 = _run(tmp_path, journal, state, registration)
    assert v2["verdict"] == "PASS" and v2["units"]["k_wins"] == 32
    assert v2["counts"]["fills_by_fee_source"] == {"recomputed_taker": 60}
    assert abs(v2["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12


def test_unrepaired_stale_no_rows_are_refused(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    fills = []
    for idx, (day, city, won, price) in enumerate(layout):
        stale = idx in (0, 7, 30)
        fills.append(_fill(day, city, won, idx, price, contract_side="NO" if stale else "YES", stale_no=stale))
    journal, state, registration = _write_record(tmp_path, layout, fills=fills)
    # stale rows in closed_trades -> refused
    with pytest.raises(gate.GateRefusal, match="repair_no_settlement_pnl"):
        _run(tmp_path, journal, state, registration)
    rc = gate.main(["--journal", str(journal), "--state", str(state), "--registration", str(registration), "--quiet"])
    assert rc == gate.EXIT_REFUSED
    # journal-only stale rows (state cleared) -> still refused, never silently used
    (tmp_path / "exchange_state.json").write_text(json.dumps({"closed_trades": [], "positions": []}), "utf-8")
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "3:" in str(exc.value) and "[journal]" in str(exc.value)
    # a NO settlement without a recorded outcome is excluded, not guessed
    rows = [json.loads(l) for l in journal.read_text("utf-8").splitlines()]
    for r in rows:
        if r.get("contract_side") == "NO":
            r["repaired_no_side_settlement"] = True
            r["exit_price"] = 1.0 - r["exit_price"]
            r["pnl"] = (r["exit_price"] - r["entry_price"]) * r["quantity"]
    rows[0].pop("settlement_outcome", None)
    rows[0].pop("repaired_no_side_settlement", None)
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["excluded"].get("no_side_outcome_unverifiable") == 1
    assert v["counts"]["stale_no_side_rows"] == []
    assert v["counts"]["quality_excluded"] == 1
    assert v["counts"]["excluded_rate"] == pytest.approx(1 / 60)


# ---------------------------------------------------------------------------
# Settlement reconciliation (F3 review defect 2)
# ---------------------------------------------------------------------------
def test_booked_pnl_that_does_not_reconcile_is_refused(tmp_path):
    """Nothing checked that pnl followed from exit_price, entry_price and quantity."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _flatter(rows):
        rows[20] = {**rows[20], "pnl": rows[20]["pnl"] + 50.0}  # a loser booked as a winner
        return rows

    journal, state, registration = _write_record(tmp_path, layout, state_rows_override=_flatter)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "pnl_not_reconcilable" in str(exc.value)
    assert "contradict their own settlement fields" in str(exc.value)
    rc = gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration), "--quiet"]
    )
    assert rc == gate.EXIT_REFUSED


def test_a_journal_of_losses_booked_as_wins_cannot_reach_a_verdict(tmp_path):
    """The reviewer's scenario: 50 fills that all lost, booked at a PASS-worthy pnl."""
    d0 = date(2026, 6, 1)
    fills = []
    for i in range(50):
        j, s = _fill(d0 + timedelta(days=i), CITIES[i % 4], False, i)
        j = {**j, "pnl": 11.66}  # settled to 0 but booked as if it had paid out
        s = {**s, "pnl": 11.66}
        fills.append((j, s))
    journal, state, registration = _write_record(tmp_path, [], fills=fills)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "pnl_not_reconcilable" in str(exc.value)


def test_mis_repaired_no_side_row_is_refused_despite_the_marker(tmp_path):
    """``repaired_no_side_settlement`` is written by a script that can mis-repair."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    fills = []
    for idx, (day, city, won, price) in enumerate(layout):
        j, s = _fill(day, city, won, idx, price,
                     contract_side="NO" if idx == 12 else "YES",
                     repaired=idx == 12)
        if idx == 12:  # "repaired" onto the wrong leg: the marker says trust me
            j = {**j, "exit_price": 1.0 - j["exit_price"]}
            s = {**s, "exit_price": 1.0 - s["exit_price"]}
        fills.append((j, s))
    journal, state, registration = _write_record(tmp_path, layout, fills=fills)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "exit_price_contradicts_settlement" in str(exc.value)
    assert gate.stale_no_side_settlement(fills[12][0]) is None  # the old check waves it through


def test_settlement_outcome_contradicting_the_strike_spec_is_refused(tmp_path):
    """The direction is re-derived from the settled high, not read back off the row."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _bad_high(rows):
        rows[8] = {**rows[8], "settlement_high": 70.0}  # 70F cannot settle an 84-85 bracket YES
        return rows

    journal, state, registration = _write_record(tmp_path, layout, journal_rows_override=_bad_high)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "settlement_outcome_contradicts_strike_spec" in str(exc.value)


def test_rows_that_cannot_be_reconciled_are_excluded_not_trusted(tmp_path):
    """No outcome / no strike spec: unreadable, so dropped and charged to the budget."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _strip(rows):
        rows[14] = {k: v for k, v in rows[14].items() if k != "settlement_outcome"}
        return rows

    journal, state, registration = _write_record(
        tmp_path, layout, journal_rows_override=_strip,
        state_rows_override=lambda rows: [
            {k: v for k, v in r.items() if k != "settlement_outcome"} if i == 14 else r
            for i, r in enumerate(rows)
        ],
    )
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["excluded"]["settlement_outcome_missing"] == 1
    dropped = [r for r in v["counts"]["excluded_rows"] if r["reason"] == "settlement_outcome_missing"]
    assert len(dropped) == 1 and dropped[0]["symbol"] and dropped[0]["entry_time"]
    assert v["counts"]["settled_fills"] == 59
    assert v["counts"]["excluded_rate"] == pytest.approx(1 / 60)
    assert v["conditions"]["excluded_rate_within_bound"]["ok"] is True


def test_state_only_rows_without_bracket_semantics_are_excluded(tmp_path):
    """closed_trades carries no strike spec, so a state-only fill cannot be re-derived."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(
        tmp_path, layout,
        journal_rows_override=lambda rows: rows[1:],  # row 0 survives only in closed_trades
    )
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["excluded"]["strike_spec_unverifiable"] == 1
    assert [r["reason"] for r in v["counts"]["excluded_rows"]] == ["strike_spec_unverifiable"]
    assert "spec" in v["counts"]["excluded_rows"][0]["detail"]


# ---------------------------------------------------------------------------
# Exclusion budget (F3 review defect 3)
# ---------------------------------------------------------------------------
def test_degrading_losing_fills_cannot_buy_a_pass(tmp_path):
    """The gate used to improve as its data got worse, silently and without bound."""
    layout = _layout(both_win=4, split=2, both_lose=4, single_wins=16)  # the FAIL scenario
    losers = [i for i, (_, _, won, _) in enumerate(layout) if not won]
    assert len(losers) >= 10

    def _corrupt_losers(rows):
        for i in losers[:12]:  # make the losing fills unreadable, not wrong
            rows[i] = {k: v for k, v in rows[i].items() if k != "quantity"}
        return rows

    journal, state, registration = _write_record(
        tmp_path, layout,
        journal_rows_override=_corrupt_losers,
        state_rows_override=lambda rows: [r for i, r in enumerate(rows) if i not in losers[:12]],
    )
    v = _run(tmp_path, journal, state, registration)
    c = v["conditions"]["excluded_rate_within_bound"]
    assert c["ok"] is False
    assert c["quality_excluded"] == 12 and c["observed"] == pytest.approx(12 / 60)
    assert c["excluded_by_reason"] == {"missing_numeric_field": 12}
    assert v["refused"] is True and v["verdict"] == "FAIL"
    assert any("excluded_rate" in r for r in v["refusals"])
    # every hole is named, not just counted
    assert len(v["counts"]["excluded_rows"]) == 12
    assert {r["reason"] for r in v["counts"]["excluded_rows"]} == {"missing_numeric_field"}
    assert "excluded_rate_within_bound" in v["failing"]


def test_settlement_error_marker_cannot_delete_a_reconcilable_fill(tmp_path):
    """F3 review round 2, BLOCKING 1: an in-band free-text field bought a PASS.

    ``settlement_error`` routed a row into the ``settlement_unresolved`` SCOPE
    branch, which the exclusion budget deliberately does not charge. So one
    field written onto one losing leg of each two-fill unit deleted ten losing
    fills for free: net PnL +83.40, and each stripped pair's least-favourable
    null fell from 0.834 to 0.417, which is a BIGGER reward than the superseded
    independent model gave. FAIL (p=0.22795) became PASS (p=0.02904).
    """
    layout = _layout(both_win=0, split=5, both_lose=5, single_wins=23)
    base_dir = tmp_path / "clean"
    base_dir.mkdir()
    j0, s0, r0 = _write_record(base_dir, layout)
    v0 = _run(base_dir, j0, s0, r0)
    assert v0["verdict"] == "FAIL"
    assert v0["units"]["p_upper_tail"] == pytest.approx(0.2279518598313848, abs=1e-12)
    assert v0["pnl"]["net"] == pytest.approx(59.60, abs=1e-9)
    assert v0["counts"]["settled_fills"] == 60

    # exactly one losing leg of each two-fill date, in BOTH files, marked
    # "unresolved" while every settlement field on it still reconciles.
    losing_leg = [i for i in range(20) if not layout[i][2] and i % 2 == 1]
    assert len(losing_leg) == 10

    def _mark(rows):
        for i in losing_leg:
            rows[i] = {**rows[i], "settlement_error": "transient upstream hiccup"}
        return rows

    attack_dir = tmp_path / "attack"
    attack_dir.mkdir()
    j1, s1, r1 = _write_record(
        attack_dir, layout, journal_rows_override=_mark,
        state_rows_override=lambda rows: _mark(list(rows)),
    )
    with pytest.raises(gate.GateRefusal) as exc:
        _run(attack_dir, j1, s1, r1)
    assert "reconcile" in str(exc.value)
    assert "unresolved" in str(exc.value)


def test_a_genuinely_unresolved_row_is_still_a_free_scope_filter(tmp_path):
    """The fix must not refuse the honest unresolved row every record carries."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)
    # _write_record always appends one SETTLEMENT_UNRESOLVED row whose exit_price
    # is the entry mark, so it does NOT reconcile and stays out of scope.
    assert v["counts"]["excluded"]["settlement_unresolved"] == 1
    assert v["counts"]["quality_excluded"] == 0
    assert v["refused"] is False and v["verdict"] == "PASS"


def test_close_reason_rewrite_cannot_delete_a_reconcilable_fill(tmp_path):
    """The same shape through the OTHER status scope filter (``not_settled``)."""
    layout = _layout(both_win=0, split=5, both_lose=5, single_wins=23)
    losing_leg = [i for i in range(20) if not layout[i][2] and i % 2 == 1]

    def _mark_journal(rows):
        for i in losing_leg:
            rows[i] = {**rows[i], "close_reason": "MANUAL_CLOSE"}
        return rows

    def _mark_state(rows):
        keep = set(losing_leg)
        return [
            {**r, "reason": "MANUAL_CLOSE"} if i in keep else r
            for i, r in enumerate(rows)
        ]

    journal, state, registration = _write_record(
        tmp_path, layout, journal_rows_override=_mark_journal,
        state_rows_override=_mark_state,
    )
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "reconcile" in str(exc.value)


def test_an_unreadable_journal_row_prefers_its_complete_state_twin(tmp_path):
    """The de-duplication key must not delete a fill the state file can still read."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _gut(rows):
        # the journal row can no longer be read; its state twin still can
        rows[14] = {k: v for k, v in rows[14].items() if k != "quantity"}
        return rows

    journal, state, registration = _write_record(
        tmp_path, layout, journal_rows_override=_gut
    )
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["settled_fills"] == 60          # nothing lost
    assert v["counts"]["quality_excluded"] == 0
    assert v["counts"]["excluded_rows"] == []
    assert v["verdict"] == "PASS"


def test_cross_city_unit_merging_is_reported_and_quantified(tmp_path):
    """FR-5.2 fixes the unit at target_date; the gate must SAY what that costs."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)
    u = v["units"]
    assert u["cross_city_units"] == 10               # every two-fill date is two cities
    assert u["n_units_if_grouped_by_city_day"] == 60
    assert u["units_lost_to_cross_city_merging"] == 10
    assert "NOT mutually exclusive" in u["cross_city_note"]
    assert any(w["warning"] == "cross_city_units_merged" for w in v["warnings"])
    two = [x for x in v["unit_table"] if x["n_fills"] == 2]
    assert all(x["n_cities"] == 2 and x["cross_city"] is True for x in two)


def test_degenerate_null_fires_below_saturation(tmp_path):
    """w_u = 1 is far too late: a unit is evidence-free well before it."""
    high = 0.46  # q* just under 0.47 -> a pair's least-favourable null is ~0.94
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23,
                     pair_prices=(high, high))
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)
    u = v["units"]
    assert u["units_with_saturated_null"] == 0        # nothing reaches 1
    assert u["units_with_degenerate_null"] == 10      # but ten carry ~no evidence
    assert u["degenerate_null_threshold"] == gate.NULL_DEGENERATE_W
    assert any(w["warning"] == "units_with_degenerate_null" for w in v["warnings"])
    assert all(x["null_degenerate"] is True for x in v["unit_table"] if x["n_fills"] == 2)


def test_registration_cannot_loosen_the_exclusion_budget(tmp_path):
    """A guard a registration can dial away is not a guard."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _strip(rows):
        for i in (14, 16):
            rows[i] = {k: v for k, v in rows[i].items() if k != "settlement_outcome"}
        return rows

    def _strip_state(rows):
        return [
            {k: v for k, v in r.items() if k != "settlement_outcome"} if i in (14, 16) else r
            for i, r in enumerate(rows)
        ]

    journal, state, registration = _write_record(
        tmp_path, layout, journal_rows_override=_strip, state_rows_override=_strip_state,
        thresholds={"n_min": 50, "alpha": 0.05, "net_pnl_gt": 0.0, "max_excluded_rate": 0.05},
    )
    v = _run(tmp_path, journal, state, registration)
    c = v["conditions"]["excluded_rate_within_bound"]
    assert c["required_le"] == gate.MAX_EXCLUDED_RATE       # the 0.05 did NOT take
    assert c["registration_max_excluded_rate"] == 0.05
    assert c["registration_override_rejected"] is True
    assert v["counts"]["excluded_rate"] == pytest.approx(2 / 60)
    assert v["refused"] is True and v["verdict"] == "FAIL"


def test_registration_may_tighten_the_exclusion_budget(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _strip(rows):
        rows[14] = {k: v for k, v in rows[14].items() if k != "settlement_outcome"}
        return rows

    def _strip_state(rows):
        return [
            {k: v for k, v in r.items() if k != "settlement_outcome"} if i == 14 else r
            for i, r in enumerate(rows)
        ]

    journal, state, registration = _write_record(
        tmp_path, layout, journal_rows_override=_strip, state_rows_override=_strip_state,
        thresholds={"n_min": 50, "alpha": 0.05, "net_pnl_gt": 0.0, "max_excluded_rate": 0.0},
    )
    v = _run(tmp_path, journal, state, registration)
    c = v["conditions"]["excluded_rate_within_bound"]
    assert c["required_le"] == 0.0
    assert c["registration_override_rejected"] is False
    assert v["refused"] is True  # 1/60 > 0


def test_target_date_wrapper_still_returns_a_plain_string(tmp_path):
    """scripts/factory_paper_reconcile.py calls _target_date and feeds date.fromisoformat."""
    row = {"symbol": _ticker("NY", date(2026, 6, 1)), "target_date": "2026-06-01"}
    assert gate._target_date(row) == "2026-06-01"
    assert gate._target_date({"symbol": "KXHIGHNY-26JUN01-B84.5"}) == "2026-06-01"
    assert gate._target_date({"symbol": "", "target_date": "not-a-date"}) is None
    # the checked form is the one that reports the problem
    td, problem = gate._target_date_checked({"symbol": "", "target_date": "not-a-date"})
    assert td is None and problem[0] == "target_date_unparseable"


def test_the_default_exclusion_budget_refuses_a_trimmed_sample(tmp_path):
    """Two unreadable fills in sixty is 3.3%: over the 2% ceiling, so refused.

    The registration used to be able to raise that ceiling; it now cannot --
    see test_registration_cannot_loosen_the_exclusion_budget.
    """
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _strip(rows):
        for i in (14, 16):
            rows[i] = {k: v for k, v in rows[i].items() if k != "settlement_outcome"}
        return rows

    strip_state = lambda rows: [  # noqa: E731
        {k: v for k, v in r.items() if k != "settlement_outcome"} if i in (14, 16) else r
        for i, r in enumerate(rows)
    ]
    journal, state, registration = _write_record(
        tmp_path, layout, journal_rows_override=_strip, state_rows_override=strip_state
    )
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["excluded_rate"] == pytest.approx(2 / 60)  # 3.3% > the 2% default
    assert v["refused"] is True
    assert gate.MAX_EXCLUDED_RATE == 0.02


# ---------------------------------------------------------------------------
# target_date (F3 review defect 4)
# ---------------------------------------------------------------------------
def test_target_date_is_cross_checked_against_the_ticker_label(tmp_path):
    """A wrong label splits one city-day ladder into several 'independent' units."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _shift(rows):
        rows[0] = {**rows[0], "target_date": "2026-07-04"}  # the ticker says 2026-06-01
        return rows

    journal, state, registration = _write_record(tmp_path, layout, journal_rows_override=_shift)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "target_date_mismatch" in str(exc.value) and "2026-07-04" in str(exc.value)

    def _garbage(rows):
        rows[2] = {**rows[2], "target_date": "sometime in June"}
        return rows

    journal, state, registration = _write_record(tmp_path, layout, journal_rows_override=_garbage)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "target_date_unparseable" in str(exc.value)


def test_target_date_string_variants_stay_one_unit(tmp_path):
    """Three fills on ONE ladder must not become three units through formatting."""
    d = date(2026, 6, 1)
    fills = []
    for idx, label in enumerate(("2026-06-01", "2026-06-01T00:00:00", "2026-06-01T00:00:00+00:00")):
        j, s = _fill(d, CITIES[idx], idx == 0, idx)
        fills.append(({**j, "target_date": label}, s))
    for i in range(49):  # 49 clean single-fill dates so n_units clears n_min
        day = d + timedelta(days=1 + i)
        fills.append(_fill(day, CITIES[i % 4], i < 24, 100 + i))
    journal, state, registration = _write_record(tmp_path, [], fills=fills)
    v = _run(tmp_path, journal, state, registration)
    assert v["units"]["n"] == 50, "the three labels named one settlement day"
    first = [u for u in v["unit_table"] if u["target_date"] == "2026-06-01"][0]
    assert first["n_fills"] == 3
    # and three brackets on one day carry no evidence under the least-favourable null
    assert first["null_saturated"] is True and first["null_win_probability"] == 1.0
    assert v["units"]["units_with_saturated_null"] == 1
    assert gate._parse_iso_day("2026-06-01T00:00:00+00:00") == date(2026, 6, 1)
    assert gate._parse_iso_day("not a date") is None


def test_target_date_derived_from_a_wrong_expiration_stamp_is_refused(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)

    def _bad_stamp(rows):
        row = dict(rows[1])  # an odd index: no explicit target_date, so it is derived
        assert "target_date" not in row
        row["expiration_time"] = "2026-09-09T04:00:00+00:00"
        return [row if i == 1 else r for i, r in enumerate(rows)]

    journal, state, registration = _write_record(tmp_path, layout, journal_rows_override=_bad_stamp)
    with pytest.raises(gate.GateRefusal) as exc:
        _run(tmp_path, journal, state, registration)
    assert "target_date_mismatch" in str(exc.value) and "expiration_time" in str(exc.value)


# ---------------------------------------------------------------------------
# Power / CLI / registration schema
# ---------------------------------------------------------------------------
def test_fewer_than_n_min_units_is_refused(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)[:40]  # 10x2 + 20 = 30 units
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)
    assert v["units"]["n"] == 30
    assert v["refused"] is True
    assert v["verdict"] == "FAIL"
    assert v["units"]["p_upper_tail"] is None
    assert v["conditions"]["n_units_ge_n_min"]["ok"] is False
    assert "underpowered" in v["refusal"]
    rc = gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
         "--realistic-fills", "true", "--allow-unverified-registration",
         "--fill-config", "", "--quiet"]
    )
    assert rc == gate.EXIT_REFUSED


def test_cli_exit_codes(tmp_path, capsys):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout, commit_utc=None)
    rc = gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
         "--realistic-fills", "true", "--allow-unverified-registration",
         "--fill-config", "", "--out", str(tmp_path / "v.json")]
    )
    assert rc == gate.EXIT_PASS
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "PASS" and "fills" not in printed
    layout = _layout(both_win=4, split=2, both_lose=4, single_wins=16)
    journal, state, registration = _write_record(tmp_path, layout, commit_utc=None)
    assert gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
         "--realistic-fills", "true", "--allow-unverified-registration",
         "--fill-config", "", "--quiet"]
    ) == gate.EXIT_FAIL
    out = capsys.readouterr().out
    assert "excluded_rate=" in out and "saturated_units=" in out


def test_journal_rows_missing_from_state_get_recomputed_taker_fee(tmp_path):
    """closed_trades is cleared on a cycle reset; the journal is append-only."""
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout, drop_from_state=15)
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["fills_by_fee_source"] == {
        "closed_trades.entry_fee": 45,
        "recomputed_taker": 15,
    }
    # same fee either way (same function, same size), so the verdict is unchanged
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["pnl"]["net"] == pytest.approx(299.60, abs=1e-9)
    assert v["verdict"] == "PASS"


def test_registration_placeholders_and_wrong_unit_are_refused(tmp_path):
    reg = tmp_path / "r.json"
    reg.write_text(
        json.dumps({"schema_version": 1, "strategy_name": "x", "spec_hash": "REPLACE_ME",
                    "thresholds": {"n_min": 50, "alpha": 0.05}, "grouping_unit": "target_date"}),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateError):
        gate.load_registration(str(reg))
    reg.write_text(
        json.dumps({"schema_version": 1, "strategy_name": "x", "spec_hash": "h",
                    "thresholds": {"n_min": 50, "alpha": 0.05}, "grouping_unit": "fill"}),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateError):
        gate.load_registration(str(reg))


def test_template_is_valid_apart_from_placeholders():
    tpl = json.loads(
        (REPO_ROOT / "configs" / "factory" / "gate_registration.template.json").read_text("utf-8")
    )
    assert tpl["schema_version"] == gate.SCHEMA_VERSION
    assert tpl["grouping_unit"] == "target_date"
    assert tpl["thresholds"] == {"n_min": 50, "alpha": 0.05, "net_pnl_gt": 0.0}
    assert tpl["fee_type"] == "taker" and tpl["adverse_fill"] == 0.01
    assert tpl["requires_realistic_fills"] is True
    assert tpl["registration_commit_utc"] is None
    for key in ("spec_hash", "adverse_fill", "fee_type", "registered_before_first_trade",
                "registration_commit_utc", "requires_realistic_fills",
                "grouping_unit", "thresholds.n_min", "thresholds.alpha", "breakeven", "test"):
        assert key in tpl["_doc"], key
    assert "Poisson-binomial" in tpl["_doc"]["test"]
    assert "every fill" in tpl["_doc"]["fee_type"]
    # F4 blocker 2: a reader must be able to tell from the template alone what
    # requires_realistic_fills now means and how a run evidences it.
    rf_doc = tpl["_doc"]["requires_realistic_fills"]
    for phrase in ("MP_REALISTIC_FILLS", "--fill-config", gate.FILL_CONFIG_LOG_DEFAULT,
                   "REFUSED", "entry_time", "evidence, not proof"):
        assert phrase in rf_doc, phrase
    assert "taker" in rf_doc and "NOT a reason to register false" in rf_doc
    # ...and what the bounds are, and exactly where they stop. A reader must not
    # be able to take "hash-pinned" for tamper-proof (F4 remediation, 2026-09-05).
    for phrase in ("PINNED TO AN INSTANT THE RUN ACTUALLY STAMPED",
                   "clamped", "dated in the future is dropped",
                   "a start record AND a strictly later one",
                   "NOT tamper-proof", "ANY SINGLE forged line",
                   "two coherent past-dated lines"):
        assert phrase in rf_doc, phrase
    assert gate.FILL_CONFIG_LOG_DEFAULT in tpl["_doc"]["_fill_config_log"]


# ---------------------------------------------------------------------------
# TradeOutcome.target_date
# ---------------------------------------------------------------------------
def test_trade_outcome_target_date_from_expiration_and_fallbacks(tmp_path):
    symbol = "KXHIGHLAX-26SEP03-B92.5"
    close = settlement_close_for(symbol)  # 2026-09-04 00:00 America/Los_Angeles
    assert close.tzinfo is not None
    assert TradeOutcome.from_position({"symbol": symbol, "expiration_time": close}).target_date == "2026-09-03"
    assert TradeOutcome.from_position({"symbol": symbol, "expiration_time": close.isoformat()}).target_date == "2026-09-03"
    assert TradeOutcome.from_position({"symbol": symbol}).target_date == "2026-09-03"
    assert TradeOutcome.from_position({"symbol": "KXBTC15M-X-1"}).target_date is None
    # the stamp as seen in UTC (07:00Z on the 4th) still names the 3rd
    assert TradeOutcome.from_position(
        {"symbol": symbol, "expiration_time": "2026-09-04T07:00:00+00:00"}
    ).target_date == "2026-09-03"


def test_journal_backwards_compatible_with_rows_lacking_target_date(tmp_path):
    path = tmp_path / "j.jsonl"
    old_row = {"symbol": "KXHIGHNY-26SEP03-T83", "strategy_name": "Meteorologist V2",
               "entry_price": 0.52, "exit_price": 0.0, "quantity": 42.0, "pnl": -21.84,
               "close_reason": "EXPIRATION", "unknown_future_key": 1}
    path.write_text(json.dumps(old_row) + "\n", encoding="utf-8")
    journal = TradeJournal(str(path))
    loaded = journal.load_all()
    assert len(loaded) == 1 and loaded[0].target_date is None
    journal.record(TradeOutcome.from_position({"symbol": "KXHIGHNY-26SEP04-T83", "pnl": 1.0}))
    rows = [json.loads(l) for l in path.read_text("utf-8").splitlines()]
    assert "target_date" not in rows[0] and rows[1]["target_date"] == "2026-09-04"
    assert [o.target_date for o in journal.load_all()] == [None, "2026-09-04"]
