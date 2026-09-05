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

    EXACT NULL (red team B, 2026-09-05): each fill wins independently with its
    own q*; a unit wins when its summed PnL is positive.
        single-fill unit:  w = q* = 0.417
        two-fill unit:     both-win or split both win -> w = 1 - (1 - 0.417)^2
                           = 1 - 0.583^2 = 1 - 0.339889 = 0.660111
    p = P[K >= k], K = 10 x Bernoulli(0.660111) + 40 x Bernoulli(0.417)
    (Poisson-binomial, exact rationals; _pb_tail below is the test's own DP).

    PASS scenario  (8 both-win, 1 split, 1 both-lose; 23 of 40 singles win)
        unit wins k = 8 + 1 + 23 = 32 of n = 50
        fill wins    = 16 + 1 + 23 = 40 of 60
        net PnL      = 40 * 11.66 - 20 * 8.34 = 466.40 - 166.80 = +299.60
        p = P[K >= 32] = 0.008728294823503926   -> < 0.05, net > 0, hash ok => PASS
        (the old pooled q_bar binomial would say P[X >= 32 | 50, 0.417] = 0.00122 --
         7x too small, because it models a split two-fill date at 0.417 not 0.660)

    FAIL scenario  (4 both-win, 2 split, 4 both-lose; 16 of 40 singles win)
        unit wins k = 4 + 2 + 16 = 22 of 50
        fill wins    = 8 + 2 + 16 = 26 of 60
        net PnL      = 26 * 11.66 - 34 * 8.34 = 303.16 - 283.56 = +19.60  (> 0!)
        p = P[K >= 22] = 0.6955838881502316   -> p >= 0.05 is the ONLY failing condition
        (pooled q_bar secondary: P[X >= 22 | 50, 0.417] = 0.4231476244946547)

    EXTREME-PRICE scenario (red team B): the 10 two-fill dates pair a 0.08 fill
    with a 0.92 fill (20 contracts each; taker fee 0.07*20*0.08*0.92 = 0.10304 ->
    0.11 -> f = 0.0055 for both):
        q*_a = 0.0855, q*_b = 0.9255
        win@0.08 = (1-0.08)*20-0.11 = +18.29; loss@0.08 = -1.60-0.11 = -1.71
        win@0.92 = +1.60-0.11 = +1.49;        loss@0.92 = -18.40-0.11 = -18.51
        a split date is a LOSS either way (18.29-18.51 < 0; 1.49-1.71 < 0), so
        w_pair = q*_a * q*_b = 0.07913025  (vs the pooled q_bar reading 0.5055)
        5 both-win, 3 split, 2 both-lose; 24 of 40 singles win -> k = 29
        p exact = 0.00039157949076542084; pooled q_bar = (10*0.5055+40*0.417)/50
        = 0.4347, P[X >= 29 | 50, 0.4347] = 0.027310211981160168 (70x too large)
"""
from __future__ import annotations

import importlib.util
import json
import os
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
W_TWO = 1 - (1 - Q_STAR) ** 2  # 660111/1000000
REG_COMMIT = "2026-05-01T00:00:00+00:00"  # before every synthetic entry_time

P_PASS_UNITS = 0.008728294823503926  # P[K >= 32], exact Poisson-binomial
P_FAIL_UNITS = 0.6955838881502316  # P[K >= 22]
P_PASS_POOLED = 0.0012171990345424294  # P[X >= 32 | 50, 0.417] (secondary)
P_FAIL_POOLED = 0.4231476244946547  # P[X >= 22 | 50, 0.417] (secondary)
P_PASS_FILLS = 8.310514656378994e-05  # P[X >= 40 | 60, 0.417]
P_FAIL_FILLS = 0.4472224935802638  # P[X >= 26 | 60, 0.417]
P_EXTREME_UNITS = 0.00039157949076542084
P_EXTREME_POOLED = 0.027310211981160168


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


def test_hand_constants_are_the_exact_rational_tails():
    layout_ws = [W_TWO] * 10 + [Q_STAR] * 40
    assert abs(float(_pb_tail(layout_ws, 32)) - P_PASS_UNITS) < 1e-15
    assert abs(float(_pb_tail(layout_ws, 22)) - P_FAIL_UNITS) < 1e-15
    assert abs(float(_exact_tail(50, 32, Q_STAR)) - P_PASS_POOLED) < 1e-15
    assert abs(float(_exact_tail(50, 22, Q_STAR)) - P_FAIL_POOLED) < 1e-15
    assert abs(float(_exact_tail(60, 40, Q_STAR)) - P_PASS_FILLS) < 1e-15
    assert abs(float(_exact_tail(60, 26, Q_STAR)) - P_FAIL_FILLS) < 1e-15
    assert float(W_TWO) == pytest.approx(0.660111, abs=1e-15)


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
                  commit_utc=REG_COMMIT, fills=None, state_rows_override=None):
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
                "thresholds": {"n_min": 50, "alpha": 0.05, "net_pnl_gt": 0.0},
                "fee_type": "taker",
                "adverse_fill": 0.01,
                "requires_realistic_fills": True,
                "registration_commit_utc": commit_utc,
            }
        ),
        encoding="utf-8",
    )
    return journal, state, registration


def _run(tmp_path: Path, journal, state, registration, **kw):
    out = tmp_path / "verdict.json"
    verdict = gate.run_gate(
        journal_path=str(journal),
        state_path=str(state),
        registration_path=str(registration),
        out_path=str(out),
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


def test_unit_null_win_probability_enumerates_fill_outcomes():
    one = {"entry_price": P_ENTRY, "fee_per_contract": 0.017, "quantity": float(QTY)}
    assert abs(float(gate.unit_null_win_probability([one, one])) - float(W_TWO)) < 1e-15
    a = {"entry_price": 0.08, "fee_per_contract": 0.0055, "quantity": 20.0}
    b = {"entry_price": 0.92, "fee_per_contract": 0.0055, "quantity": 20.0}
    # only both-win wins: w = q*_a * q*_b
    assert abs(float(gate.unit_null_win_probability([a, b])) - 0.0855 * 0.9255) < 1e-15
    # a unit whose PnL can only be exactly 0 is a loss (exact rationals, no rounding win)
    zero = {"entry_price": 0.5, "fee_per_contract": 0.0, "quantity": 2.0}
    assert gate.unit_null_win_probability([zero, zero]) == Fraction(1, 4)  # both win only
    with pytest.raises(gate.GateRefusal):
        gate.unit_null_win_probability([one] * (gate.MAX_FILLS_PER_UNIT + 1))


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

    assert v["refused"] is False
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
    assert c["fee_type_matches"]["ok"] is True
    assert c["realistic_fills_enabled"]["ok"] is None  # unknown -> UNVERIFIED, non-gating
    assert v["not_applicable"] == ["realistic_fills_enabled"]
    assert v["verdict"] == "PASS"
    assert v["units"]["units_with_multiple_fills"] == 10
    assert v["counts"]["excluded"] == {"other_strategy": 1, "settlement_unresolved": 1}
    assert v["counts"]["fills_by_fee_source"] == {"closed_trades.entry_fee": 60}
    assert v["warnings"] == []
    # unit-level null: two-fill dates at 0.660111, singles at 0.417
    two = [u for u in v["unit_table"] if u["n_fills"] == 2]
    one = [u for u in v["unit_table"] if u["n_fills"] == 1]
    assert len(two) == 10 and len(one) == 40
    assert all(abs(u["null_win_probability"] - float(W_TWO)) < 1e-12 for u in two)
    assert all(abs(u["null_win_probability"] - 0.417) < 1e-12 for u in one)
    assert all(abs(u["q_star"] - 0.417) < 1e-12 for u in v["unit_table"])


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


def test_extreme_price_pairs_use_the_exact_unit_null(tmp_path):
    """0.08/0.92 pairs: a split date loses, so w_pair = q*_a q*_b, far below the pooled q_bar."""
    layout = _layout(both_win=5, split=3, both_lose=2, single_wins=24, pair_prices=(0.08, 0.92))
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)
    assert v["units"]["n"] == 50 and v["units"]["k_wins"] == 29
    f = Fraction(11, 2000)  # 0.11 / 20
    w_pair = (Fraction(8, 100) + f) * (Fraction(92, 100) + f)
    hand = _pb_tail([w_pair] * 10 + [Q_STAR] * 40, 29)
    assert abs(float(hand) - P_EXTREME_UNITS) < 1e-15
    assert abs(v["units"]["p_upper_tail"] - float(hand)) < 1e-12
    assert abs(v["units"]["p_pooled_qbar_secondary"] - P_EXTREME_POOLED) < 1e-9
    two = [u for u in v["unit_table"] if u["n_fills"] == 2]
    assert all(abs(u["null_win_probability"] - float(w_pair)) < 1e-12 for u in two)
    assert v["verdict"] == "PASS"  # exact p 0.00039 < 0.05


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


def test_registration_commit_time_is_gating_by_default(tmp_path):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout, commit_utc=None)
    v = _run(tmp_path, journal, state, registration)
    c = v["conditions"]["registered_before_first_trade"]
    assert c["ok"] is False and c["gating"] is True
    assert "not recorded" in c["note"]
    assert v["verdict"] == "FAIL" and v["failing"] == ["registered_before_first_trade"]
    # dry-run downgrade: reported, not gating, recorded in the verdict
    v2 = _run(tmp_path, journal, state, registration, allow_unverified_registration=True)
    c2 = v2["conditions"]["registered_before_first_trade"]
    assert c2["ok"] is None and c2["gating"] is False and "UNVERIFIED" in c2["note"]
    assert v2["allow_unverified_registration"] is True
    assert "registered_before_first_trade" in v2["not_applicable"]
    assert v2["verdict"] == "PASS"
    # a registration dated AFTER the first fill fails even when filled in
    journal, state, registration = _write_record(tmp_path, layout, commit_utc="2026-06-15T00:00:00+00:00")
    v3 = _run(tmp_path, journal, state, registration)
    assert v3["conditions"]["registered_before_first_trade"]["ok"] is False
    assert v3["verdict"] == "FAIL"


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
         "--realistic-fills", "false", "--quiet"]
    )
    assert rc == gate.EXIT_FAIL


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
    assert len(v["warnings"]) == 1
    w = v["warnings"][0]
    assert w["warning"] == "quantity_mismatch_journal_vs_state"
    assert w["journal_quantity"] == 20.0 and w["state_quantity"] == 5.0
    fill = [f for f in v["fills"] if f["symbol"] == w["symbol"] and f["entry_time"] == w["entry_time"]][0]
    assert fill["quantity"] == 20.0
    # 0.09 at size 5 rescales to 0.36 at size 20 > the 0.34 taker fee -> the booked (rescaled) fee stands
    assert fill["entry_fee"] == pytest.approx(0.36) and fill["fee_source"] == "closed_trades.entry_fee"
    assert v["verdict"] == "PASS"


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
    rows[1].pop("settlement_outcome", None)  # a YES row: unaffected
    rows[0].pop("settlement_outcome", None)
    rows[0].pop("repaired_no_side_settlement", None)
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["excluded"].get("no_side_outcome_unverifiable") == 1
    assert v["counts"]["stale_no_side_rows"] == []


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
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration), "--quiet"]
    )
    assert rc == gate.EXIT_REFUSED


def test_cli_exit_codes(tmp_path, capsys):
    layout = _layout(both_win=8, split=1, both_lose=1, single_wins=23)
    journal, state, registration = _write_record(tmp_path, layout)
    rc = gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration),
         "--out", str(tmp_path / "v.json")]
    )
    assert rc == gate.EXIT_PASS
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "PASS" and "fills" not in printed
    layout = _layout(both_win=4, split=2, both_lose=4, single_wins=16)
    journal, state, registration = _write_record(tmp_path, layout)
    assert gate.main(
        ["--journal", str(journal), "--state", str(state), "--registration", str(registration), "--quiet"]
    ) == gate.EXIT_FAIL


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
