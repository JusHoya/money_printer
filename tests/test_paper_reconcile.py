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

STRATEGY = "Genome 7d857b00"
GENOME_ID = "7d857b00d373260c"

LOG_LINES = [
    "2026-09-09 15:00:03 | INFO    | [Signal] EMIT strategy=Genome 7d857b00 symbol=KXHIGHCHI-26SEP09-B84.5 side=buy contract=YES price=0.41 qty=20 confidence=0.610",
    "2026-09-09 15:00:03 | INFO    | [Risk] REJECT strategy=Genome 7d857b00 symbol=KXHIGHCHI-26SEP09-B84.5 reason=KELLY_ZERO kelly=0 p=0.61 price=0.41",
    "2026-09-10 16:00:02 | INFO    | [Risk] REJECT strategy=Genome 7d857b00 symbol=KXHIGHLAX-26SEP10-B92.5 reason=WEATHER_SLOT_FULL city=LAX",
    "2026-09-10 16:00:02 | INFO    | [Risk] REJECT strategy=Meteorologist V2 symbol=KXHIGHLAX-26SEP10-B92.5 reason=EV_NEGATIVE ev=-0.01",
    "2026-09-21 16:00:02 | INFO    | [Risk] REJECT strategy=Genome 7d857b00 symbol=KXHIGHLAX-26SEP21-B92.5 reason=GENOME_SHADOW",
    "2026-09-10 16:00:05 | INFO    | [Tweets] KXELONTWEETS: no active markets returned",
]


def _fill(symbol, side, price, qty, won, entry_time, strategy=STRATEGY):
    exit_price = 1.0 if won else 0.0
    pnl = (exit_price - price) * qty
    j = {
        "symbol": symbol, "strategy_name": strategy, "entry_time": entry_time,
        "exit_time": entry_time[:10] + "T23:59:59", "entry_price": price, "exit_price": exit_price,
        "quantity": float(qty), "side": "buy", "contract_side": side, "pnl": pnl,
        "close_reason": "EXPIRATION", "settlement_outcome": "yes" if (won == (side == "YES")) else "no",
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
    spec.write_text(json.dumps({"genome_id": GENOME_ID, "genome_json": {"gene_spec_version": 1, "genes": {}},
                                "adverse_fill": 0.01, "contracts_frame": 20,
                                "fee": {"type": "taker"}, "mode": "shadow"}), encoding="utf-8")
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
    assert b["won"] is False
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


def test_lab_only_markets_get_reject_codes(record, monkeypatch):
    """A fake lab set: two markets the sandbox skipped, one it took, one it took that the lab did not."""
    fake = {
        "coverage": "full", "reason": None, "frame_dates": ["2026-09-08", "2026-09-14"],
        "trades": [
            {"market_ticker": "KXHIGHNY-26SEP09-B84.5", "target_date": "2026-09-09", "ts_utc": 0,
             "direction": "buy_yes", "quote": 0.40, "price_paid": 0.41, "fee_per_contract": 0.017,
             "realized_per_contract": 0.573},
            {"market_ticker": "KXHIGHCHI-26SEP09-B84.5", "target_date": "2026-09-09", "ts_utc": 0,
             "direction": "buy_yes", "quote": 0.40, "price_paid": 0.41, "fee_per_contract": 0.017,
             "realized_per_contract": -0.427},
            {"market_ticker": "KXHIGHLAX-26SEP10-B92.5", "target_date": "2026-09-10", "ts_utc": 0,
             "direction": "buy_no", "quote": 0.30, "price_paid": 0.31, "fee_per_contract": 0.015,
             "realized_per_contract": 0.675},
            {"market_ticker": "KXHIGHMIA-26SEP11-B90.5", "target_date": "2026-09-11", "ts_utc": 0,
             "direction": "buy_no", "quote": 0.30, "price_paid": 0.31, "fee_per_contract": 0.015,
             "realized_per_contract": 0.675},
        ],
    }
    monkeypatch.setattr(pr, "lab_trade_set", lambda *a, **k: fake)
    rep = _run(record, frames_dir="whatever")
    lab_only = {t["market_ticker"]: t for t in rep["lab_only"]}
    assert set(lab_only) == {"KXHIGHCHI-26SEP09-B84.5", "KXHIGHLAX-26SEP10-B92.5", "KXHIGHMIA-26SEP11-B90.5"}
    assert lab_only["KXHIGHCHI-26SEP09-B84.5"]["reject_codes"] == [("KELLY_ZERO", 1)]
    assert lab_only["KXHIGHLAX-26SEP10-B92.5"]["reject_codes"] == [("WEATHER_SLOT_FULL", 1)]
    assert lab_only["KXHIGHMIA-26SEP11-B90.5"]["reject_codes"] == [] and not lab_only["KXHIGHMIA-26SEP11-B90.5"]["explained"]
    assert [x["symbol"] for x in rep["sandbox_only"]] == ["KXHIGHMIA-26SEP10-B90.5", "KXHIGHCHI-26SEP12-B80.5"]
    assert rep["summary"]["sandbox_subset_of_lab"] is False
    assert rep["summary"]["n_lab_only_explained_by_reject"] == 2
    md = pr.render_markdown(rep)
    assert "sandbox \\ lab (must be empty)" in md and "KELLY_ZEROx1" in md


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
