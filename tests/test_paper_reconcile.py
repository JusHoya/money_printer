"""Tests for ``scripts/factory_paper_reconcile.py`` (FR-F3.4, ARCHITECTURE section 9 item 7).

Synthetic sandbox record for ``Genome 7d857b00`` over 2026-09-08..2026-09-14:

    fill A  KXHIGHNY-26SEP09  YES  booked 0.41 x 30, won
        quote = 0.41 - 0.01 = 0.40; price_lab = 0.40 + 0.01 = 0.41
        sandbox fee: taker 30 x 0.41 -> 0.07*30*0.41*0.59 = 0.50799 -> 0.51 -> 0.017/contract
        lab fee (C=20): 0.07*20*0.41*0.59 = 0.33866 -> 0.34 -> 0.017/contract
        realized/contract: sandbox ((1-0.41)*30 - 0.51)/30 = 0.573; lab 1 - 0.41 - 0.017 = 0.573
        with --adverse-fill 0.02: price_lab 0.42, fee 0.07*20*0.42*0.58 = 0.34104 -> 0.35 -> 0.0175,
        realized_lab = 1 - 0.42 - 0.0175 = 0.5625
    fill B  KXHIGHMIA-26SEP10  NO   booked 0.31 x 20, lost
        lab fee: 0.07*20*0.31*0.69 = 0.29946 -> 0.30 -> 0.015; realized_lab = 0 - 0.31 - 0.015 = -0.325
    fill C  KXHIGHCHI-26SEP12  YES  booked 0.55 x 10, won  (journal only: state was reset)
    fill D  KXHIGHNY-26SEP20  out of range; fill E Meteorologist V2 -- both ignored
    open position KXHIGHLAX-26SEP13 -> pending
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "mp_paper_reconcile", REPO_ROOT / "scripts" / "factory_paper_reconcile.py"
)
pr = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(pr)

from src.factory import genome as G  # noqa: E402
from src.factory.promoted import build_spec, write_promoted  # noqa: E402

SPEC = build_spec(
    G.SEEDS["fr31a_taker"],
    family="weather/gfs_mex/taker/v1",
    config_sha256="c" * 64,
    frame_search_sha256="f" * 64,
    calibration_dir="data/calibration",
    calibration_sha256="d" * 64,
    fee_type="quadratic",
    fee_regime_sha256="e" * 64,
)
GENOME_ID = SPEC.genome_id
STRATEGY = f"Genome {SPEC.id8}"

LOG_LINES = [
    f"2026-09-09 15:00:03 | INFO    | [Signal] EMIT strategy={STRATEGY} symbol=KXHIGHCHI-26SEP09-B84.5 side=buy contract=YES price=0.41 qty=20 confidence=0.610",
    f"2026-09-09 15:00:03 | INFO    | [Risk] REJECT strategy={STRATEGY} symbol=KXHIGHCHI-26SEP09-B84.5 reason=KELLY_ZERO kelly=0 p=0.61 price=0.41",
    f"2026-09-10 16:00:02 | INFO    | [Risk] REJECT strategy={STRATEGY} symbol=KXHIGHLAX-26SEP10-B92.5 reason=WEATHER_SLOT_FULL city=LAX",
    "2026-09-10 16:00:02 | INFO    | [Risk] REJECT strategy=Meteorologist V2 symbol=KXHIGHLAX-26SEP10-B92.5 reason=EV_NEGATIVE ev=-0.01",
    f"2026-09-21 16:00:02 | INFO    | [Risk] REJECT strategy={STRATEGY} symbol=KXHIGHLAX-26SEP21-B92.5 reason=GENOME_SHADOW",
    "2026-09-10 16:00:05 | INFO    | [Tweets] KXELONTWEETS: no active markets returned",
]


def _settlement_provenance(symbol, outcome):
    """The FR-1.2 settlement fields ``SimulatedExchange`` stamps on every settled position.

    ``scripts/gate.py`` reconciles each fill's booked money against these rather than
    trusting ``pnl``, so a fixture without them is excluded before it is ever scored.
    Verified against the live maia journal (2026-09-05): every one of its six closed
    trades carries ``settlement_spec``, the three flat strike fields and
    ``settlement_high``, written by matching_engine.py and persisted by trade_journal.py.
    """
    bracket = symbol.rsplit("-", 1)[1]
    if bracket.startswith("B"):            # "B84.5" -> between 84 and 85
        floor_strike = float(int(float(bracket[1:])))
        cap_strike = floor_strike + 1.0
        spec = {"strike_type": "between", "floor_strike": floor_strike, "cap_strike": cap_strike}
        high = floor_strike if outcome == "yes" else floor_strike + 5.0
    else:                                  # "T83" -> less than 83, i.e. "82 or below"
        cap_strike = float(int(float(bracket[1:])))
        spec = {"strike_type": "less", "floor_strike": None, "cap_strike": cap_strike}
        high = cap_strike - 1.0 if outcome == "yes" else cap_strike
    return {**spec, "settlement_spec": spec, "settlement_high": high}


def _fill(symbol, side, price, qty, won, entry_time, strategy=STRATEGY):
    exit_price = 1.0 if won else 0.0
    pnl = (exit_price - price) * qty
    outcome = "yes" if (won == (side == "YES")) else "no"
    j = {
        "symbol": symbol, "strategy_name": strategy, "entry_time": entry_time,
        "exit_time": entry_time[:10] + "T23:59:59", "entry_price": price, "exit_price": exit_price,
        "quantity": float(qty), "side": "buy", "contract_side": side, "pnl": pnl,
        "close_reason": "EXPIRATION", "settlement_outcome": outcome,
        **_settlement_provenance(symbol, outcome),
    }
    from src.core.fee_calculator import taker_fee

    s = {**j, "open_time": entry_time, "close_time": j["exit_time"], "reason": "EXPIRATION",
         "entry_fee": taker_fee(price, qty), "exit_fee": 0.0, "is_maker": False}
    del s["entry_time"], s["exit_time"], s["close_reason"]
    return j, s


@pytest.fixture
def record(tmp_path):
    a = _fill("KXHIGHNY-26SEP09-B84.5", "YES", 0.41, 30, True, "2026-09-09T15:00:03")
    b = _fill("KXHIGHMIA-26SEP10-B90.5", "NO", 0.31, 20, False, "2026-09-10T16:00:02")
    c = _fill("KXHIGHCHI-26SEP12-B80.5", "YES", 0.55, 10, True, "2026-09-12T14:00:01")
    d = _fill("KXHIGHNY-26SEP20-B84.5", "YES", 0.41, 30, True, "2026-09-20T15:00:03")
    e = _fill("KXHIGHNY-26SEP09-T83", "NO", 0.52, 42, False, "2026-09-09T16:52:23", "Meteorologist V2")
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text("".join(json.dumps(x[0]) + "\n" for x in (a, b, c, d, e)), encoding="utf-8")
    state = tmp_path / "exchange_state.json"
    state.write_text(json.dumps({
        "closed_trades": [a[1], b[1], d[1], e[1]],  # c: journal only (state reset)
        "positions": [{"symbol": "KXHIGHLAX-26SEP13-B92.5", "strategy_name": STRATEGY,
                       "open_time": "2026-09-13T17:00:02", "entry_price": 0.36, "quantity": 20.0}],
    }), encoding="utf-8")
    log = tmp_path / "money_printer_20260908.log"
    log.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    spec = tmp_path / "promoted.json"
    write_promoted(SPEC, spec)
    return {"journal": journal, "state": state, "log": log, "spec": spec, "tmp": tmp_path}


def test_parse_reject_lines_handles_strategy_names_with_spaces():
    rejects = pr.parse_reject_lines(LOG_LINES)
    assert [r["reason"] for r in rejects] == ["KELLY_ZERO", "WEATHER_SLOT_FULL", "EV_NEGATIVE", "GENOME_SHADOW"]
    assert rejects[0]["strategy"] == STRATEGY
    assert rejects[0]["symbol"] == "KXHIGHCHI-26SEP09-B84.5"
    assert rejects[0]["context"] == {"kelly": "0", "p": "0.61", "price": "0.41"}
    assert rejects[0]["ts"] == "2026-09-09T15:00:03"
    assert rejects[2]["strategy"] == "Meteorologist V2"


def test_reprice_matches_hand_computation(record):
    rep = _run(record)
    assert rep["strategy_name"] == STRATEGY
    by = {r["symbol"]: r for r in rep["repriced_fills"]}
    assert set(by) == {"KXHIGHNY-26SEP09-B84.5", "KXHIGHMIA-26SEP10-B90.5", "KXHIGHCHI-26SEP12-B80.5"}
    a = by["KXHIGHNY-26SEP09-B84.5"]
    assert a["quote_recovered"] == pytest.approx(0.40)
    assert a["price_lab"] == pytest.approx(0.41)
    assert a["sandbox_fee_per_contract"] == pytest.approx(0.017)
    assert a["fee_lab_per_contract"] == pytest.approx(0.017)
    assert a["sandbox_realized_per_contract"] == pytest.approx(0.573)
    assert a["realized_lab_per_contract"] == pytest.approx(0.573)
    b = by["KXHIGHMIA-26SEP10-B90.5"]
    # ``won`` is now split: what the booked pnl says vs what settlement truth says.
    assert b["won_booked"] is False and b["won_from_settlement"] is False
    assert b["settlement_sign_mismatch"] is False
    assert b["payoff_per_contract"] == 0.0
    assert b["fee_lab_per_contract"] == pytest.approx(0.015)
    assert b["realized_lab_per_contract"] == pytest.approx(-0.325)
    c = by["KXHIGHCHI-26SEP12-B80.5"]
    assert c["sandbox_fee_per_contract"] == pytest.approx(0.07 * 10 * 0.55 * 0.45 / 10, abs=0.002)
    assert rep["counts"]["fills_by_fee_source"] == {"closed_trades.entry_fee": 2, "recomputed_taker": 1}
    assert rep["summary"]["n_sandbox_fills_settled"] == 3
    assert [p["symbol"] for p in rep["pending_positions"]] == ["KXHIGHLAX-26SEP13-B92.5"]
    assert rep["summary"]["sandbox_net_pnl"] == pytest.approx(
        ((1 - 0.41) * 30 - 0.51) + ((0 - 0.31) * 20 - 0.30) + ((1 - 0.55) * 10 - 0.18)
    )


def test_raised_adverse_fill_reprices_against_the_sandbox(record):
    rep = _run(record, adverse_fill_lab=0.02)
    a = {r["symbol"]: r for r in rep["repriced_fills"]}["KXHIGHNY-26SEP09-B84.5"]
    assert a["price_lab"] == pytest.approx(0.42)
    assert a["fee_lab_per_contract"] == pytest.approx(0.0175)
    assert a["realized_lab_per_contract"] == pytest.approx(0.5625)
    assert a["realized_delta_per_contract_sandbox_minus_lab"] == pytest.approx(0.573 - 0.5625)
    assert rep["parameters"]["adverse_fill_lab"] == 0.02


def test_frame_absent_says_so_and_falls_back_to_repricing(record):
    rep = _run(record, frames_dir=str(record["tmp"] / "no_such_frame"))
    lab = rep["lab_trade_set"]
    assert lab["coverage"] == "none" and "not found" in lab["reason"]
    assert rep["lab_only"] == [] and rep["sandbox_only"] == []
    assert rep["summary"]["sandbox_subset_of_lab"] is None
    # REJECT profile: strategy's lines in range only (V2 and the 09-21 line excluded)
    assert rep["summary"]["reject_profile"] == {"KELLY_ZERO": 1, "WEATHER_SLOT_FULL": 1}
    assert rep["summary"]["n_reject_lines_total"] == 4
    md = pr.render_markdown(rep)
    assert "Frame does not cover" in md and "KELLY_ZERO: 1" in md


def _ts(iso: str) -> int:
    from datetime import datetime, timezone

    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def test_lab_only_markets_get_reject_codes(record, monkeypatch):
    """A fake lab set keyed on (ticker, decision hour UTC, direction).

    NY 09-09 15Z buy_yes  -> matches fill A (YES at 15:00:03)
    CHI 09-09 15Z buy_yes -> lab-only, explained by the KELLY_ZERO reject at 15:00:03
    LAX 09-10 16Z buy_no  -> lab-only, explained by WEATHER_SLOT_FULL at 16:00:02
    MIA 09-11 16Z buy_no  -> lab-only, unexplained
    MIA 09-10 16Z buy_yes -> DIRECTION_MISMATCH: fill B is a NO at that hour
    NY  09-09 18Z buy_yes -> lab-only (a REJECT three hours earlier does not explain it)
    """
    fake = {
        "coverage": "full", "reason": None, "frame_dates": ["2026-09-08", "2026-09-14"],
        "trades": [
            {"market_ticker": "KXHIGHNY-26SEP09-B84.5", "target_date": "2026-09-09", "ts_utc": _ts("2026-09-09T15:00:00"),
             "direction": "buy_yes", "quote": 0.40, "price_paid": 0.41, "fee_per_contract": 0.017,
             "realized_per_contract": 0.573},
            {"market_ticker": "KXHIGHCHI-26SEP09-B84.5", "target_date": "2026-09-09", "ts_utc": _ts("2026-09-09T15:00:00"),
             "direction": "buy_yes", "quote": 0.40, "price_paid": 0.41, "fee_per_contract": 0.017,
             "realized_per_contract": -0.427},
            {"market_ticker": "KXHIGHLAX-26SEP10-B92.5", "target_date": "2026-09-10", "ts_utc": _ts("2026-09-10T16:00:00"),
             "direction": "buy_no", "quote": 0.30, "price_paid": 0.31, "fee_per_contract": 0.015,
             "realized_per_contract": 0.675},
            {"market_ticker": "KXHIGHMIA-26SEP11-B90.5", "target_date": "2026-09-11", "ts_utc": _ts("2026-09-11T16:00:00"),
             "direction": "buy_no", "quote": 0.30, "price_paid": 0.31, "fee_per_contract": 0.015,
             "realized_per_contract": 0.675},
            {"market_ticker": "KXHIGHMIA-26SEP10-B90.5", "target_date": "2026-09-10", "ts_utc": _ts("2026-09-10T16:00:00"),
             "direction": "buy_yes", "quote": 0.68, "price_paid": 0.69, "fee_per_contract": 0.015,
             "realized_per_contract": 0.295},
            {"market_ticker": "KXHIGHNY-26SEP09-B84.5", "target_date": "2026-09-09", "ts_utc": _ts("2026-09-09T18:00:00"),
             "direction": "buy_yes", "quote": 0.45, "price_paid": 0.46, "fee_per_contract": 0.017,
             "realized_per_contract": 0.523},
        ],
    }
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: fake)
    rep = _run(record, frames_dir="whatever")
    lab_only = {(t["market_ticker"], t["ts_utc"]): t for t in rep["lab_only"]}
    assert set(lab_only) == {
        ("KXHIGHCHI-26SEP09-B84.5", _ts("2026-09-09T15:00:00")),
        ("KXHIGHLAX-26SEP10-B92.5", _ts("2026-09-10T16:00:00")),
        ("KXHIGHMIA-26SEP11-B90.5", _ts("2026-09-11T16:00:00")),
        ("KXHIGHNY-26SEP09-B84.5", _ts("2026-09-09T18:00:00")),
    }
    assert lab_only[("KXHIGHCHI-26SEP09-B84.5", _ts("2026-09-09T15:00:00"))]["reject_codes"] == [("KELLY_ZERO", 1)]
    assert lab_only[("KXHIGHLAX-26SEP10-B92.5", _ts("2026-09-10T16:00:00"))]["reject_codes"] == [("WEATHER_SLOT_FULL", 1)]
    unexplained = lab_only[("KXHIGHMIA-26SEP11-B90.5", _ts("2026-09-11T16:00:00"))]
    assert unexplained["reject_codes"] == [] and not unexplained["explained"]
    assert not lab_only[("KXHIGHNY-26SEP09-B84.5", _ts("2026-09-09T18:00:00"))]["explained"]
    assert [t["market_ticker"] for t in rep["direction_mismatch"]] == ["KXHIGHMIA-26SEP10-B90.5"]
    sandbox_only = {x["symbol"]: x for x in rep["sandbox_only"]}
    assert set(sandbox_only) == {"KXHIGHMIA-26SEP10-B90.5", "KXHIGHCHI-26SEP12-B80.5"}
    assert sandbox_only["KXHIGHMIA-26SEP10-B90.5"]["flag"].startswith("DIRECTION_MISMATCH")
    assert sandbox_only["KXHIGHCHI-26SEP12-B80.5"]["flag"].startswith("NOT_IN_LAB_TRADE_SET")
    assert rep["summary"]["sandbox_subset_of_lab"] is False
    assert rep["summary"]["n_lab_only_explained_by_reject"] == 2
    assert rep["summary"]["n_direction_mismatch"] == 1
    assert rep["summary"]["match_key"].startswith("(market_ticker, decision hour UTC, direction)")
    md = pr.render_markdown(rep)
    assert "sandbox \\ lab (must be empty)" in md and "KELLY_ZEROx1" in md and "direction mismatches 1" in md


def test_cli_writes_json_and_md(record):
    out_dir = record["tmp"] / "reports"
    rc = pr.main([
        "--promoted", str(record["spec"]), "--journal", str(record["journal"]),
        "--state", str(record["state"]), "--log", str(record["log"]),
        "--from", "2026-09-08", "--to", "2026-09-14", "--no-frame", "--out-dir", str(out_dir),
    ])
    assert rc == 0
    js = json.loads((out_dir / "paper_reconcile_2026-09-08_2026-09-14.json").read_text("utf-8"))
    assert js["summary"]["n_sandbox_fills_settled"] == 3
    assert js["lab_trade_set"]["coverage"] == "none"
    assert (out_dir / "paper_reconcile_2026-09-08_2026-09-14.md").exists()


_MAIN_FRAME = Path("W:/Hoya_Space/Projects/money_printer/data/factory/frames/weather_2026-07-25_bfcf94654a3a")


@pytest.mark.skipif(not _MAIN_FRAME.is_dir(), reason="frozen frame not on this box")
def test_lab_trade_set_on_the_frozen_frame_with_a_seed(record):
    """Real frame, real seed genome, dates inside the frame: the lab set is computed."""
    from src.factory.genome import SEEDS

    lab = pr.lab_trade_set(str(_MAIN_FRAME), SEEDS["fr31a_taker"].to_json(), date(2026, 7, 10), date(2026, 7, 20))
    assert lab["coverage"] == "full"
    assert all("2026-07-10" <= t["target_date"] <= "2026-07-20" for t in lab["trades"])
    assert lab["n_trades"] == len(lab["trades"])
    outside = pr.lab_trade_set(str(_MAIN_FRAME), SEEDS["fr31a_taker"].to_json(), date(2026, 9, 8), date(2026, 9, 14))
    assert outside["coverage"] == "none" and "2026-07-25" in outside["reason"]


def _run(record, *, frames_dir=None, adverse_fill_lab=None):
    from src.factory import fees as fees_mod

    gate = pr._GATE
    with open(record["state"], "r", encoding="utf-8") as fh:
        st = json.load(fh)
    return pr.reconcile(
        spec=json.loads(record["spec"].read_text("utf-8")),
        journal_rows=gate.load_journal(str(record["journal"])),
        closed_trades=st["closed_trades"],
        open_positions=st["positions"],
        rejects=pr.load_reject_lines([str(record["log"])]),
        date_from=date(2026, 9, 8),
        date_to=date(2026, 9, 14),
        frames_dir=frames_dir,
        adverse_fill_lab=adverse_fill_lab,
        regime=fees_mod.load_regime(),
    )


# ---------------------------------------------------------------------------
# Red team F3 follow-up: the three defects that made this an identity, plus the
# loudness / HTTP / vacuous defects. Every test below fails on the pre-fix
# script.
# ---------------------------------------------------------------------------
BRACKET_70_71 = {"strike_type": "between", "floor_strike": 70.0, "cap_strike": 71.0}


def _row(symbol, side, price, qty, *, exit_price, settlement_outcome, entry_time,
         settlement_high=None, settlement_spec=None, strategy=STRATEGY, exit_fee=0.0):
    """A journal row and its ``closed_trades`` twin, with the booked numbers and
    the settlement truth supplied INDEPENDENTLY so a test can make them disagree."""
    from src.core.fee_calculator import taker_fee

    pnl = (exit_price - price) * qty - exit_fee
    j = {
        "symbol": symbol, "strategy_name": strategy, "entry_time": entry_time,
        "exit_time": entry_time[:10] + "T23:59:59", "entry_price": price,
        "exit_price": float(exit_price), "quantity": float(qty), "side": "buy",
        "contract_side": side, "pnl": pnl, "close_reason": "EXPIRATION",
        "settlement_outcome": settlement_outcome,
    }
    if settlement_high is not None:
        j["settlement_high"] = settlement_high
    if settlement_spec is not None:
        j["settlement_spec"] = dict(settlement_spec)
    s = {**j, "open_time": entry_time, "close_time": j["exit_time"], "reason": "EXPIRATION",
         "entry_fee": taker_fee(price, qty), "exit_fee": exit_fee, "is_maker": False}
    del s["entry_time"], s["exit_time"], s["close_reason"]
    return j, s


def _write_record(tmp_path, pairs, *, positions=(), log_lines=LOG_LINES, in_state=None):
    """Write a journal / exchange_state / log / promoted-spec quartet."""
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text("".join(json.dumps(p[0]) + "\n" for p in pairs), encoding="utf-8")
    keep = pairs if in_state is None else in_state
    state = tmp_path / "exchange_state.json"
    state.write_text(json.dumps({
        "closed_trades": [p[1] for p in keep], "positions": list(positions),
    }), encoding="utf-8")
    log = tmp_path / "money_printer_20260908.log"
    log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    spec = tmp_path / "promoted.json"
    write_promoted(SPEC, spec)
    return {"journal": journal, "state": state, "log": log, "spec": spec, "tmp": tmp_path}


def _cli(record, *extra):
    out_dir = record["tmp"] / "reports"
    rc = pr.main([
        "--promoted", str(record["spec"]), "--journal", str(record["journal"]),
        "--state", str(record["state"]), "--log", str(record["log"]),
        "--from", "2026-09-08", "--to", "2026-09-14", "--out-dir", str(out_dir), *extra,
    ])
    js = json.loads((out_dir / "paper_reconcile_2026-09-08_2026-09-14.json").read_text("utf-8"))
    md = (out_dir / "paper_reconcile_2026-09-08_2026-09-14.md").read_text("utf-8")
    return rc, js, md


# --- defect 1: the payoff was read off the number being audited --------------
def test_settlement_sign_error_does_not_reconcile_to_zero(tmp_path):
    """A YES fill the sandbox booked as a winner that the strike spec says LOST.

    Pre-fix the payoff came from ``fill["won"] = net_pnl > 0``, so the lab
    formula agreed with the booked number by construction and the delta was
    0.000000. The payoff now comes from ``settlement_spec`` + ``settlement_high``.
    """
    bad = _row("KXHIGHCHI-26SEP11-B70.5", "YES", 0.42, 20, exit_price=1.0,
               settlement_outcome="no", entry_time="2026-09-11T15:00:03",
               settlement_high=90.0, settlement_spec=BRACKET_70_71)
    rep = _run(_write_record(tmp_path, [bad]))
    r = rep["repriced_fills"][0]
    # THE defect: an audited-off-itself payoff makes this identically zero.
    assert r["realized_delta_per_contract_sandbox_minus_lab"] == pytest.approx(1.0, abs=1e-9)
    assert r["payoff_per_contract"] == 0.0
    assert r["payoff_source"].startswith("strike_spec")
    assert r["won_booked"] is True and r["won_from_settlement"] is False
    assert r["settlement_sign_mismatch"] is True
    assert r["sandbox_realized_expected_per_contract"] == pytest.approx(-0.4375)
    assert r["realized_lab_per_contract"] == pytest.approx(-0.4375)
    s = rep["summary"]
    assert s["n_settlement_sign_mismatch"] == 1
    assert s["n_payoff_from_strike_spec"] == 1
    assert s["verdict"] == "DISCREPANCY" and s["exit_code"] == 1
    assert any("settlement sign error" in b for b in s["blocking"])
    assert "Settlement-truth problems" in pr.render_markdown(rep)


def test_recorded_outcome_contradicting_the_strike_spec_is_flagged(tmp_path):
    """``settlement_outcome`` is cross-checked, not trusted; the spec wins."""
    row = _row("KXHIGHCHI-26SEP11-B70.5", "YES", 0.42, 20, exit_price=0.0,
               settlement_outcome="yes", entry_time="2026-09-11T15:00:03",
               settlement_high=90.0, settlement_spec=BRACKET_70_71)  # 90F is not 70..71
    rep = _run(_write_record(tmp_path, [row]))
    r = rep["repriced_fills"][0]
    assert r["settlement"]["outcome_recorded"] == "yes"
    assert r["settlement"]["outcome_from_strike_spec"] == "no"
    assert r["settlement"]["outcome_disagreement"] is True
    assert r["settlement"]["outcome_used"] == "no"  # the spec is the deeper truth
    assert r["payoff_per_contract"] == 0.0
    assert r["settlement_sign_mismatch"] is False  # the BOOKED numbers are fine
    s = rep["summary"]
    assert s["n_settlement_outcome_disagreement"] == 1
    assert s["verdict"] == "DISCREPANCY" and s["exit_code"] == 1


def test_payoff_without_settlement_truth_is_refused_not_guessed(tmp_path):
    """No outcome and no strike spec: the payoff is None, never ``won``."""
    row = _row("KXHIGHCHI-26SEP11-B70.5", "YES", 0.42, 20, exit_price=1.0,
               settlement_outcome=None, entry_time="2026-09-11T15:00:03")
    rep = _run(_write_record(tmp_path, [row]))
    r = rep["repriced_fills"][0]
    assert r["payoff_per_contract"] is None
    assert r["realized_lab_per_contract"] is None
    assert "settlement truth unavailable" in r["note"]
    assert rep["summary"]["n_payoff_unavailable"] == 1


# --- defect 2: the price leg was an algebraic identity -----------------------
def _fake_lab(trades, coverage="full"):
    return {"coverage": coverage, "reason": None,
            "frame_dates": ["2026-09-08", "2026-09-14"], "trades": list(trades)}


def _lab_trade(ticker, td, iso, direction, quote, price_paid):
    return {"market_ticker": ticker, "target_date": td, "ts_utc": _ts(iso),
            "direction": direction, "quote": quote, "price_paid": price_paid,
            "fee_per_contract": 0.017, "realized_per_contract": 0.0}


def test_price_lab_uses_the_frames_own_quote_not_the_booked_price(record, monkeypatch):
    """Pre-fix ``price_lab`` was ``(price_booked - af) + af == price_booked``.

    The frame's quote for the market-hour is a number the sandbox never
    produced, so the price leg can now actually disagree.
    """
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([
        # fill A booked 0.41 -> reconstructs to 0.40; the frame says 0.35.
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.35, 0.36),
        _lab_trade("KXHIGHMIA-26SEP10-B90.5", "2026-09-10", "2026-09-10T16:00:00",
                   "buy_no", 0.30, 0.31),
        _lab_trade("KXHIGHCHI-26SEP12-B80.5", "2026-09-12", "2026-09-12T14:00:00",
                   "buy_yes", 0.54, 0.55),
    ]))
    rep = _run(record, frames_dir="whatever")
    a = {r["symbol"]: r for r in rep["repriced_fills"]}["KXHIGHNY-26SEP09-B84.5"]
    # THE defect: pre-fix this was identically price_booked (0.41).
    assert a["price_lab"] == pytest.approx(0.36)
    assert a["quote_frame"] == pytest.approx(0.35)
    assert a["quote_used"] == pytest.approx(0.35)
    assert a["quote_reconstructed"] == pytest.approx(0.40)
    assert a["quote_delta_frame_minus_reconstructed"] == pytest.approx(-0.05)
    assert a["price_disagreement"] is True
    assert a["quote_source"].startswith("frame.quote")
    ok = {r["symbol"]: r for r in rep["repriced_fills"]}["KXHIGHMIA-26SEP10-B90.5"]
    assert ok["price_disagreement"] is False and ok["quote_frame"] == pytest.approx(0.30)
    s = rep["summary"]
    assert s["n_price_disagreements"] == 1
    assert s["n_quote_from_frame"] == 3 and s["n_quote_reconstructed"] == 0
    assert s["verdict"] == "DISCREPANCY" and s["exit_code"] == 1


def test_reconstructed_quote_says_it_is_not_independent(record, monkeypatch):
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([], coverage="full"))
    rep = _run(record, frames_dir="whatever")
    for r in rep["repriced_fills"]:
        assert r["quote_frame"] is None
        assert "NOT independent of the booked price" in r["quote_source"]
        assert r["price_disagreement"] is False
    assert rep["summary"]["n_quote_reconstructed"] == 3
    assert "reconstructed 3" in pr.render_markdown(rep)


# --- defect 3: matching was set membership, so multiplicity was invisible ----
def test_two_lab_trades_in_one_market_hour_are_not_absorbed_by_one_fill(record, monkeypatch):
    """Pre-fix ``(ticker, hour, direction)`` was a SET key: 2 lab vs 1 sandbox
    matched with no residue at all."""
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.40, 0.41),
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.40, 0.41),
        _lab_trade("KXHIGHMIA-26SEP10-B90.5", "2026-09-10", "2026-09-10T16:00:00",
                   "buy_no", 0.30, 0.31),
        _lab_trade("KXHIGHCHI-26SEP12-B80.5", "2026-09-12", "2026-09-12T14:00:00",
                   "buy_yes", 0.54, 0.55),
    ]))
    rep = _run(record, frames_dir="whatever")
    # THE defect: pre-fix the surplus lab trade vanished into set membership.
    assert len(rep["lab_only"]) == 1
    assert rep["lab_only"][0]["market_ticker"] == "KXHIGHNY-26SEP09-B84.5"
    mm = rep["multiplicity_mismatch"]
    assert [(m["market_ticker"], m["n_lab_trades"], m["n_sandbox_fills"], m["surplus"])
            for m in mm] == [("KXHIGHNY-26SEP09-B84.5", 2, 1, "lab")]
    s = rep["summary"]
    assert s["n_multiplicity_mismatch"] == 1 and s["n_matched_pairs"] == 3
    assert s["verdict"] == "DISCREPANCY" and s["exit_code"] == 1
    assert "multiplicity (a market-hour is a bijection" in pr.render_markdown(rep)


def test_two_sandbox_fills_in_one_market_hour_leave_a_surplus(tmp_path, monkeypatch):
    """The other direction: one lab trade cannot cover two sandbox fills."""
    common = dict(exit_price=1.0, settlement_outcome="yes", settlement_high=85.0,
                  settlement_spec={"strike_type": "greater", "floor_strike": 84.5})
    pairs = [
        _row("KXHIGHNY-26SEP09-B84.5", "YES", 0.41, 20, entry_time="2026-09-09T15:00:03", **common),
        _row("KXHIGHNY-26SEP09-B84.5", "YES", 0.41, 20, entry_time="2026-09-09T15:31:12", **common),
    ]
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.40, 0.41),
    ]))
    rep = _run(_write_record(tmp_path, pairs), frames_dir="whatever")
    assert [x["flag"] for x in rep["sandbox_only"]] == [
        "SURPLUS_FILL (more sandbox fills than lab trades in this market-hour)"
    ]
    assert rep["summary"]["n_multiplicity_mismatch"] == 1
    assert rep["summary"]["sandbox_subset_of_lab"] is False
    assert rep["summary"]["exit_code"] == 1


def test_matched_pairs_report_the_quantity_discrepancy(record, monkeypatch):
    """Kelly sizing vs the frame's fixed C is reported, not silently dropped."""
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.40, 0.41),
        _lab_trade("KXHIGHMIA-26SEP10-B90.5", "2026-09-10", "2026-09-10T16:00:00",
                   "buy_no", 0.30, 0.31),
        _lab_trade("KXHIGHCHI-26SEP12-B80.5", "2026-09-12", "2026-09-12T14:00:00",
                   "buy_yes", 0.54, 0.55),
    ]))
    rep = _run(record, frames_dir="whatever")
    qm = {q["market_ticker"]: q for q in rep["quantity_mismatch"]}
    assert set(qm) == {"KXHIGHNY-26SEP09-B84.5", "KXHIGHCHI-26SEP12-B80.5"}  # 30 and 10 vs C=20
    assert qm["KXHIGHNY-26SEP09-B84.5"]["sandbox_quantity"] == 30.0
    assert qm["KXHIGHNY-26SEP09-B84.5"]["lab_contracts_assumed"] == 20
    assert rep["summary"]["n_quantity_mismatch"] == 2
    # sizing is expected to differ: it is reported but does not fail the run
    assert rep["summary"]["verdict"] in ("OK", "PARTIAL") and rep["summary"]["exit_code"] == 0


# --- defect 4: nothing was loud ----------------------------------------------
def test_stale_no_side_rows_refuse_the_run_and_are_printed(tmp_path):
    """``counts.stale_no_side_rows`` was copied into the JSON and never read."""
    good = _row("KXHIGHNY-26SEP09-B84.5", "YES", 0.41, 30, exit_price=1.0,
                settlement_outcome="yes", entry_time="2026-09-09T15:00:03")
    stale = _row("KXHIGHMIA-26SEP10-B90.5", "NO", 0.31, 20, exit_price=0.0,
                 settlement_outcome="no", entry_time="2026-09-10T16:00:02")
    rc, js, md = _cli(_write_record(tmp_path, [good, stale]), "--no-frame")
    assert js["counts"]["stale_no_side_rows"], "the collector must have flagged the row"
    # THE defect: pre-fix this exited 0 and the markdown never mentioned it.
    assert rc == 3
    assert js["summary"]["verdict"] == "REFUSED"
    assert any("stale NO-side" in r for r in js["summary"]["refused_reasons"])
    assert "repair_no_settlement_pnl.py" in md
    n_stale = len(js["counts"]["stale_no_side_rows"])
    assert f"stale NO-side settlement rows: {n_stale}" in md
    assert "KXHIGHMIA-26SEP10-B90.5" in md
    # the clean YES fill still reconciles; only the stale row is withheld
    assert js["summary"]["n_sandbox_fills_settled"] == 1


def test_unexplained_lab_only_row_fails_the_run(record, monkeypatch):
    """A lab-admissible trade the sandbox skipped for no logged reason is the
    most informative output this tool produces; pre-fix it exited 0."""
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.40, 0.41),
        _lab_trade("KXHIGHMIA-26SEP10-B90.5", "2026-09-10", "2026-09-10T16:00:00",
                   "buy_no", 0.30, 0.31),
        _lab_trade("KXHIGHCHI-26SEP12-B80.5", "2026-09-12", "2026-09-12T14:00:00",
                   "buy_yes", 0.54, 0.55),
        # never traded, never rejected: unexplained
        _lab_trade("KXHIGHLAX-26SEP13-B92.5", "2026-09-13", "2026-09-13T15:00:00",
                   "buy_yes", 0.20, 0.21),
    ]))
    rep = _run(record, frames_dir="whatever")
    s = rep["summary"]
    assert s["n_lab_only"] == 1 and s["n_lab_only_unexplained"] == 1
    assert s["n_sandbox_only"] == 0
    assert s["verdict"] == "DISCREPANCY" and s["exit_code"] == 1
    assert any("REJECT code logged" in b for b in s["blocking"])


def test_markdown_always_prints_the_collector_counts(record):
    rc, js, md = _cli(record, "--no-frame")
    assert "## Input integrity (collector counts)" in md
    assert "excluded rows:" in md
    assert "other_strategy" in md          # the Meteorologist V2 row
    assert "collector warnings: 0" in md
    assert "maker-booked fills: 0" in md
    assert "## Verdict" in md and "## Inputs" in md
    assert js["summary"]["verdict"] == "PARTIAL"  # trade-set question unanswered
    assert rc == 0


# --- defect 5: it could not be run against maia at all -----------------------
def test_url_mode_reads_the_journal_and_names_what_it_cannot_see(record, monkeypatch, tmp_path):
    def fake_get(url, timeout=15.0):
        if "/api/journal" in url:
            rows = [json.loads(x) for x in
                    record["journal"].read_text("utf-8").splitlines() if x.strip()]
            return {"ok": True, "count": len(rows), "trades": rows}
        if "/api/logs/tail" in url:
            return {"ok": True, "content": "\n".join(LOG_LINES)}
        raise AssertionError(url)

    monkeypatch.setattr(pr, "http_get_json", fake_get)
    out_dir = tmp_path / "http_reports"
    rc = pr.main([
        "--promoted", str(record["spec"]), "--url", "http://maia.local:8050",
        "--from", "2026-09-08", "--to", "2026-09-14", "--no-frame",
        "--out-dir", str(out_dir),
    ])
    js = json.loads((out_dir / "paper_reconcile_2026-09-08_2026-09-14.json").read_text("utf-8"))
    md = (out_dir / "paper_reconcile_2026-09-08_2026-09-14.md").read_text("utf-8")
    assert rc == 0
    assert js["inputs"]["mode"] == "http"
    assert js["inputs"]["base_url"] == "http://maia.local:8050"
    assert js["inputs"]["complete"] is False
    missing = {row["input"] for row in js["inputs"]["not_obtained"]}
    assert missing == {"exchange_state (closed_trades)", "exchange_state (open positions)"}
    for row in js["inputs"]["not_obtained"]:
        assert "no ssh" in row["why"] or "endpoint gap" in row["why"]
        assert row["consequence"]
    # the journal alone still reprices the fills, at a recomputed taker fee
    assert js["summary"]["n_sandbox_fills_settled"] == 3
    assert js["counts"]["fills_by_fee_source"] == {"recomputed_taker": 3}
    assert js["summary"]["verdict"] == "PARTIAL"
    assert "### What this run could not obtain" in md
    assert "exchange_state" in md and "LOWER BOUND" in md
    assert "/api/journal?last_n=500" in md


def test_url_mode_reports_the_server_side_caps(record, monkeypatch):
    rows = [json.loads(x) for x in record["journal"].read_text("utf-8").splitlines() if x.strip()]

    def fake_get(url, timeout=15.0):
        if "/api/journal" in url:
            return {"ok": True, "trades": rows * 100}          # 500 rows -> cap hit
        return {"ok": True, "content": "\n".join(LOG_LINES * 100)}  # 600 -> cap hit

    monkeypatch.setattr(pr, "http_get_json", fake_get)
    got = pr.gather_inputs_http("http://maia.local:8050")
    assert got["inputs"]["journal_provenance"]["cap_hit"] is True
    assert got["inputs"]["log_provenance"]["cap_hit"] is True
    detail = " ".join(r["detail"] for r in got["inputs"]["obtained"])
    assert "SERVER CAP 500 HIT" in detail
    assert "8-16 minute window" in detail
    assert "NOT evidence" in detail


def test_url_mode_failure_is_a_usage_error_not_a_silent_empty_run(record, monkeypatch, tmp_path):
    def boom(url, timeout=15.0):
        raise OSError("connection refused")

    monkeypatch.setattr(pr, "http_get_json", boom)
    rc = pr.main([
        "--promoted", str(record["spec"]), "--url", "http://maia.local:8050",
        "--from", "2026-09-08", "--to", "2026-09-14", "--no-frame",
        "--out-dir", str(tmp_path / "r"),
    ])
    assert rc == 2
    assert not (tmp_path / "r").exists()


# --- defect 6: the vacuous case read as a pass -------------------------------
def test_vacuous_run_is_refused_not_a_pass(tmp_path):
    """Shadow mode (no fills) on dates outside the frozen frame: 0 vs 0.

    The runbook designates exactly this report as F3 parity evidence; pre-fix
    it exited 0 with ``sandbox_subset_of_lab: null`` and read as a pass.
    """
    rc, js, md = _cli(_write_record(tmp_path, []), "--no-frame")
    s = js["summary"]
    # THE defect: pre-fix this returned 0.
    assert rc == 3
    assert s["verdict"] == "REFUSED"
    assert s["vacuous"] is True
    assert s["questions_answered"] == {"repricing": False, "trade_set": False}
    assert s["n_sandbox_fills_settled"] == 0 and s["n_lab_trades"] == 0
    assert s["sandbox_subset_of_lab"] is None
    assert any(r.startswith("VACUOUS") for r in s["refused_reasons"])
    assert "An empty report is not parity evidence" in " ".join(s["refused_reasons"])
    assert "**REFUSED**" in md and "It is NOT a pass" in md


def test_fills_present_but_no_frame_is_partial_not_ok(record):
    """One of the two questions was asked: not a refusal, but not an OK either."""
    rc, js, md = _cli(record, "--no-frame")
    assert rc == 0
    assert js["summary"]["verdict"] == "PARTIAL"
    assert js["summary"]["vacuous"] is False
    assert "The trade-set question was NOT answered." in md
    assert "PARTIAL" in md and "before treating it as parity evidence" in md


def test_open_position_market_hour_is_not_an_unexplained_miss(record, monkeypatch):
    """The fixture holds an unsettled KXHIGHLAX-26SEP13 position at 17:00:02
    whose contract_side the state never recorded. A lab trade in that very
    market-hour was not skipped -- it has not settled -- so it must not count
    as an unexplained lab-only row and must not fail the run."""
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: _fake_lab([
        _lab_trade("KXHIGHNY-26SEP09-B84.5", "2026-09-09", "2026-09-09T15:00:00",
                   "buy_yes", 0.40, 0.41),
        _lab_trade("KXHIGHMIA-26SEP10-B90.5", "2026-09-10", "2026-09-10T16:00:00",
                   "buy_no", 0.30, 0.31),
        _lab_trade("KXHIGHCHI-26SEP12-B80.5", "2026-09-12", "2026-09-12T14:00:00",
                   "buy_yes", 0.54, 0.55),
        _lab_trade("KXHIGHLAX-26SEP13-B92.5", "2026-09-13", "2026-09-13T17:00:00",
                   "buy_yes", 0.35, 0.36),
    ]))
    rep = _run(record, frames_dir="whatever")
    assert [t["flag"] for t in rep["lab_only"]] == [
        "OPEN_POSITION_UNSETTLED (the sandbox holds a position in this market-hour; "
        "its side was not recorded)"
    ]
    assert rep["summary"]["n_lab_only_unexplained"] == 0
    assert rep["summary"]["exit_code"] == 0
