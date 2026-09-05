"""Tests for ``scripts/gate.py`` (PRD FR-5.2, PRD_STRATEGY_FACTORY FR-F3.4).

Synthetic paper record: 60 settled fills over 50 ``target_date`` units, every
fill bought at 0.40 for 20 contracts as a taker.

Hand computation (written out so the test does not trust the script):

    entry fee, 20 contracts at p = 0.40 (fee_calculator.taker_fee):
        0.07 * 20 * 0.40 * 0.60 = 0.336  -> ceil to the cent -> 0.34 per order
        f = 0.34 / 20 = 0.017 per contract
    breakeven per fill:            q* = p + f = 0.40 + 0.017 = 0.417
    every fill identical           -> every unit breakeven = 0.417, q_bar = 0.417
    fill PnL, held to settlement:  win  = (1 - 0.40) * 20 - 0.34 = +11.66
                                   loss = (0 - 0.40) * 20 - 0.34 =  -8.34
    unit layout: dates 1..10 carry TWO fills (different cities), dates 11..50 ONE
        two-fill date both win   -> +23.32 (win)
        two-fill date split      ->  +3.32 (win: 11.66 - 8.34 > 0)
        two-fill date both lose  -> -16.68 (loss)

    PASS scenario  (6 both-win, 2 split, 2 both-lose; 21 of 40 singles win)
        unit wins k = 6 + 2 + 21 = 29 of n = 50
        fill wins    = 12 + 2 + 21 = 35 of 60
        net PnL      = 35 * 11.66 - 25 * 8.34 = 408.10 - 208.50 = +199.60
        p = P[X >= 29 | 50, 0.417] = sum_{i=29}^{50} C(50,i) 0.417^i 0.583^(50-i)
          = 0.014696224040724546        (exact rational sum, see _exact_tail)
        -> 0.0147 < 0.05, net > 0, hash matches               => PASS

    FAIL scenario  (4 both-win, 2 split, 4 both-lose; 16 of 40 singles win)
        unit wins k = 4 + 2 + 16 = 22 of 50
        fill wins    = 8 + 2 + 16 = 26 of 60
        net PnL      = 26 * 11.66 - 34 * 8.34 = 303.16 - 283.56 = +19.60  (> 0!)
        p = P[X >= 22 | 50, 0.417] = 0.4231476244946547
        -> p >= 0.05 is the ONLY failing condition             => FAIL

    secondary per-fill line (not gating):
        PASS: P[X >= 35 | 60, 0.417] = 0.0068901801913623445
        FAIL: P[X >= 26 | 60, 0.417] = 0.4472224935802638
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

STRATEGY = "Genome 7d857b00"
SPEC_HASH = "a" * 64
CITIES = ("NY", "CHI", "MIA", "LAX")
P_ENTRY = 0.40
QTY = 20
ENTRY_FEE = 0.34  # 0.07 * 20 * 0.4 * 0.6 = 0.336 -> ceil -> 0.34
Q_STAR = Fraction(417, 1000)

P_PASS_UNITS = 0.014696224040724546  # P[X >= 29 | 50, 0.417]
P_FAIL_UNITS = 0.4231476244946547  # P[X >= 22 | 50, 0.417]
P_PASS_FILLS = 0.0068901801913623445  # P[X >= 35 | 60, 0.417]
P_FAIL_FILLS = 0.4472224935802638  # P[X >= 26 | 60, 0.417]


def _exact_tail(n: int, k: int, q: Fraction) -> Fraction:
    """Exact rational upper tail, independent of the script."""
    return sum(Fraction(comb(n, i)) * q ** i * (1 - q) ** (n - i) for i in range(k, n + 1))


def test_hand_constants_are_the_exact_rational_tails():
    assert abs(float(_exact_tail(50, 29, Q_STAR)) - P_PASS_UNITS) < 1e-15
    assert abs(float(_exact_tail(50, 22, Q_STAR)) - P_FAIL_UNITS) < 1e-15
    assert abs(float(_exact_tail(60, 35, Q_STAR)) - P_PASS_FILLS) < 1e-15
    assert abs(float(_exact_tail(60, 26, Q_STAR)) - P_FAIL_FILLS) < 1e-15


# ---------------------------------------------------------------------------
# Synthetic record
# ---------------------------------------------------------------------------
def _ticker(city: str, day: date, strike: float = 84.5) -> str:
    return f"KXHIGH{city}-{day:%y%b%d}-B{strike}".upper().replace("B84.5", "B84.5")


def _layout(both_win: int, split: int, both_lose: int, single_wins: int):
    """(target_date, city, won) for 10 two-fill dates + 40 single-fill dates."""
    assert both_win + split + both_lose == 10
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
        out.append((day, CITIES[0], wins[0]))
        out.append((day, CITIES[1], wins[1]))
    for j in range(40):
        day = d0 + timedelta(days=10 + j)
        out.append((day, CITIES[(j + 2) % 4], j < single_wins))
    return out


def _fill(day: date, city: str, won: bool, idx: int):
    symbol = _ticker(city, day)
    entry_time = f"{day - timedelta(days=1)}T15:{idx % 60:02d}:00"
    exp = settlement_close_for(symbol)
    exit_price = 1.0 if won else 0.0
    pnl = (exit_price - P_ENTRY) * QTY  # settlement close: exit fee 0
    journal = {
        "symbol": symbol,
        "strategy_name": STRATEGY,
        "entry_time": entry_time,
        "exit_time": exp.isoformat(),
        "entry_price": P_ENTRY,
        "exit_price": exit_price,
        "quantity": float(QTY),
        "side": "buy",
        "contract_side": "YES",
        "pnl": pnl,
        "close_reason": "EXPIRATION",
        "settlement_high": 85.0 if won else 80.0,
        "settlement_outcome": "yes" if won else "no",
        "settlement_spec": {"strike_type": "between", "floor_strike": 84.0, "cap_strike": 85.0},
        "strike_type": "between",
        "floor_strike": 84.0,
        "cap_strike": 85.0,
        # deliberately NO target_date on half the rows: the gate must derive it
        **({"target_date": day.isoformat()} if idx % 2 == 0 else {}),
    }
    state = {
        "id": idx + 1,
        "symbol": symbol,
        "side": "buy",
        "entry_price": P_ENTRY,
        "quantity": float(QTY),
        "open_time": entry_time,
        "close_time": exp.isoformat(),
        "expiration_time": exp.isoformat(),
        "strategy_name": STRATEGY,
        "contract_side": "YES",
        "entry_fee": ENTRY_FEE,
        "exit_fee": 0.0,
        "is_maker": False,
        "fill_type": "taker",
        "exit_price": exit_price,
        "pnl": pnl,
        "reason": "EXPIRATION",
        "settlement_outcome": "yes" if won else "no",
    }
    return journal, state


def _write_record(tmp_path: Path, layout, *, drop_from_state: int = 0, spec_hash=SPEC_HASH):
    journal_rows, state_rows = [], []
    for idx, (day, city, won) in enumerate(layout):
        j, s = _fill(day, city, won, idx)
        journal_rows.append(j)
        state_rows.append(s)
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
    promoted.write_text(
        json.dumps({"genome_id": "7d857b00d373260c", "spec_hash": spec_hash}), encoding="utf-8"
    )
    registration = tmp_path / "gate_registration.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "genome_id": "7d857b00d373260c",
                "strategy_name": STRATEGY,
                "promoted_spec_path": str(promoted),
                "spec_hash": SPEC_HASH,
                "market_family": "KXHIGH",
                "grouping_unit": "target_date",
                "unit_win_rule": "date_pnl_gt_0",
                "thresholds": {"n_min": 50, "alpha": 0.05, "net_pnl_gt": 0.0},
                "fee_type": "taker",
                "adverse_fill": 0.01,
                "registration_commit_utc": None,
            }
        ),
        encoding="utf-8",
    )
    return journal, state, registration


def _run(tmp_path: Path, journal, state, registration):
    out = tmp_path / "verdict.json"
    verdict = gate.run_gate(
        journal_path=str(journal),
        state_path=str(state),
        registration_path=str(registration),
        out_path=str(out),
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


def test_breakeven_is_price_plus_fee_per_contract():
    assert gate.breakeven_win_rate(0.40, 0.017) == pytest.approx(0.417)
    fee = gate.nearest_cent_taker_fee("KXHIGHNY-26JUN01-B84.5", 0.40, 20)
    assert fee == pytest.approx(0.34)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def test_pass_scenario_reproduces_hand_computed_p(tmp_path):
    layout = _layout(both_win=6, split=2, both_lose=2, single_wins=21)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)

    assert v["refused"] is False
    assert v["units"]["n"] == 50
    assert v["units"]["k_wins"] == 29
    assert v["units"]["null_win_rate_q_bar"] == pytest.approx(0.417, abs=1e-12)
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["per_fill_secondary"]["n"] == 60
    assert v["per_fill_secondary"]["k_wins"] == 35
    assert abs(v["per_fill_secondary"]["p_upper_tail"] - P_PASS_FILLS) < 1e-12
    assert v["per_fill_secondary"]["gating"] is False
    assert v["pnl"]["net"] == pytest.approx(199.60, abs=1e-9)
    assert v["pnl"]["entry_fees"] == pytest.approx(60 * 0.34, abs=1e-9)
    c = v["conditions"]
    assert c["n_units_ge_n_min"]["ok"] and c["p_lt_alpha"]["ok"]
    assert c["net_pnl_gt_0"]["ok"] and c["spec_hash_unchanged"]["ok"]
    assert c["registered_before_first_trade"]["gating"] is False  # null commit -> UNVERIFIED
    assert v["verdict"] == "PASS"
    assert v["units"]["units_with_multiple_fills"] == 10
    assert v["counts"]["excluded"] == {"other_strategy": 1, "settlement_unresolved": 1}
    assert v["counts"]["fills_by_fee_source"] == {"closed_trades.entry_fee": 60}
    # every unit breakeven is exactly p + f
    assert all(abs(u["q_star"] - 0.417) < 1e-12 for u in v["unit_table"])


def test_fail_scenario_p_is_the_only_failing_condition(tmp_path):
    layout = _layout(both_win=4, split=2, both_lose=4, single_wins=16)
    journal, state, registration = _write_record(tmp_path, layout)
    v = _run(tmp_path, journal, state, registration)

    assert v["units"]["n"] == 50 and v["units"]["k_wins"] == 22
    assert abs(v["units"]["p_upper_tail"] - P_FAIL_UNITS) < 1e-12
    assert abs(v["per_fill_secondary"]["p_upper_tail"] - P_FAIL_FILLS) < 1e-12
    assert v["pnl"]["net"] == pytest.approx(19.60, abs=1e-9)
    c = v["conditions"]
    assert c["p_lt_alpha"]["ok"] is False
    assert c["n_units_ge_n_min"]["ok"] and c["net_pnl_gt_0"]["ok"] and c["spec_hash_unchanged"]["ok"]
    assert v["verdict"] == "FAIL"


def test_spec_hash_change_fails_an_otherwise_passing_record(tmp_path):
    layout = _layout(both_win=6, split=2, both_lose=2, single_wins=21)
    journal, state, registration = _write_record(tmp_path, layout, spec_hash="b" * 64)
    v = _run(tmp_path, journal, state, registration)
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["conditions"]["spec_hash_unchanged"]["ok"] is False
    assert v["conditions"]["spec_hash_unchanged"]["observed"] == "b" * 64
    assert v["verdict"] == "FAIL"


def test_fewer_than_n_min_units_is_refused(tmp_path):
    layout = _layout(both_win=6, split=2, both_lose=2, single_wins=21)[:40]  # 10x2 + 20 = 30 units
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
    layout = _layout(both_win=6, split=2, both_lose=2, single_wins=21)
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
    layout = _layout(both_win=6, split=2, both_lose=2, single_wins=21)
    journal, state, registration = _write_record(tmp_path, layout, drop_from_state=15)
    v = _run(tmp_path, journal, state, registration)
    assert v["counts"]["fills_by_fee_source"] == {
        "closed_trades.entry_fee": 45,
        "recomputed_taker": 15,
    }
    # same fee either way (same function, same size), so the verdict is unchanged
    assert abs(v["units"]["p_upper_tail"] - P_PASS_UNITS) < 1e-12
    assert v["pnl"]["net"] == pytest.approx(199.60, abs=1e-9)
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
    for key in ("spec_hash", "adverse_fill", "fee_type", "registered_before_first_trade",
                "grouping_unit", "thresholds.n_min", "thresholds.alpha", "breakeven", "test"):
        assert key in tpl["_doc"], key


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
