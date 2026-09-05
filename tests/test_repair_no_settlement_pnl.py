"""``scripts/repair_no_settlement_pnl.py`` on a synthetic exchange state (F3).

Before commit 724d93c every settled NO paper trade was booked at the YES-leg
payoff; the repair script re-prices exactly those trades, is idempotent, keeps
the cumulative ledger consistent with ``closed_trades`` and never touches YES
trades, traded-out exits or unresolved closes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "repair_no_settlement_pnl", os.path.join(ROOT, "scripts", "repair_no_settlement_pnl.py")
)
rep = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(rep)


def _trade(i, symbol, side_contract, entry, qty, reason, exit_price, pnl, entry_fee=0.34, exit_fee=0.0, outcome=None):
    return {
        "id": i,
        "symbol": symbol,
        "side": "buy",
        "contract_side": side_contract,
        "entry_price": entry,
        "quantity": qty,
        "reason": reason,
        "exit_price": exit_price,
        "pnl": pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "settlement_outcome": outcome,
        "strategy_name": "Meteorologist V2",
    }


def _state(tmp_path):
    trades = [
        # NO that WON (bracket settled no) booked with the YES payoff 0.00 -> stale
        _trade(1, "KXHIGHNY-26JUL20-B79.5", "NO", 0.33, 50, "EXPIRATION", 0.00, (0.00 - 0.33) * 50, outcome="no"),
        # NO that LOST (bracket settled yes) booked with the YES payoff 1.00 -> stale (a fake profit)
        _trade(2, "KXHIGHNY-26JUL21-B86.5", "NO", 0.60, 10, "EXPIRATION", 1.00, (1.00 - 0.60) * 10, outcome="yes"),
        # YES trade: untouched
        _trade(3, "KXHIGHCHI-26JUL20-B84.5", "YES", 0.40, 10, "EXPIRATION", 1.00, 6.0, outcome="yes"),
        # NO traded out early at a market price: untouched
        _trade(4, "KXHIGHLAX-26JUL20-B92.5", "NO", 0.55, 10, "STOP_LOSS", 0.45, -1.0),
        # NO unresolved flat close: untouched
        _trade(5, "KXHIGHMIA-26JUL20-B90.5", "NO", 0.30, 10, "SETTLEMENT_UNRESOLVED", 0.30, 0.0),
        # NO already repaired (pnl matches the corrected formula): untouched, idempotent
        _trade(6, "KXHIGHNY-26JUL22-B80.5", "NO", 0.25, 20, "EXPIRATION", 1.00, (1.00 - 0.25) * 20, outcome="no"),
    ]
    realized = sum(t["pnl"] for t in trades)
    doc = {
        "schema_version": 3,
        "saved_at": "2026-09-04T00:00:00",
        "realized_pnl": realized,
        "unrealized_pnl": 0.0,
        "total_fees_paid": 6 * 0.34,
        "cumulative_realized_pnl": realized,
        "cumulative_fees_paid": 6 * 0.34,
        "cumulative_entry_fees": 6 * 0.34,
        "positions": [],
        "closed_trades": trades,
    }
    path = tmp_path / "exchange_state.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path, doc


def test_classify_and_repair(tmp_path):
    path, doc = _state(tmp_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    result = rep.repair(state)
    ids = [c["id"] for c in result["corrections"]]
    assert ids == [1, 2]
    c1, c2 = result["corrections"]
    assert c1["exit_price_new"] == 1.0 and c1["pnl_new"] == pytest.approx(33.5)
    assert c1["pnl_old"] == pytest.approx(-16.5) and c1["delta"] == pytest.approx(50.0)
    assert c2["exit_price_new"] == 0.0 and c2["pnl_new"] == pytest.approx(-6.0)
    assert c2["delta"] == pytest.approx(-10.0)
    assert result["delta"] == pytest.approx(40.0)
    assert result["skipped_unmatched"] == []
    trades = {t["id"]: t for t in state["closed_trades"]}
    assert trades[1]["repaired_no_side_settlement"] is True
    for untouched in (3, 4, 5, 6):
        assert "repaired_no_side_settlement" not in trades[untouched]
        assert trades[untouched]["pnl"] == doc["closed_trades"][untouched - 1]["pnl"]
    assert state["realized_pnl"] == pytest.approx(doc["realized_pnl"] + 40.0)
    assert state["cumulative_realized_pnl"] == pytest.approx(sum(t["pnl"] for t in state["closed_trades"]))
    assert state["cumulative_entry_fees"] == pytest.approx(6 * 0.34)
    assert state["cumulative_fees_paid"] == pytest.approx(6 * 0.34)


def test_cli_dry_run_then_apply_then_idempotent(tmp_path, capsys):
    path, doc = _state(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert rep.main(["--state", str(path)]) == 1  # repairs pending, nothing written
    assert path.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "2 NO-side settlement(s) to repair, total delta +40.00" in out
    assert "dry run" in out

    assert rep.main(["--state", str(path), "--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["applied"] is True and summary["n_corrected"] == 2
    backup = summary["backup"]
    assert os.path.exists(backup) and open(backup, encoding="utf-8").read() == before
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["realized_pnl"] == pytest.approx(doc["realized_pnl"] + 40.0)
    assert repaired["closed_trades"][0]["exit_price"] == 1.0
    assert repaired["closed_trades"][0]["pnl"] == pytest.approx(33.5)
    assert repaired["schema_version"] == 3 and repaired["positions"] == []

    # second pass: nothing left to repair, exit 0, file untouched
    after = path.read_text(encoding="utf-8")
    assert rep.main(["--state", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == after
    assert "0 NO-side settlement(s) to repair" in capsys.readouterr().out


def test_unmatched_pnl_is_skipped_not_guessed(tmp_path):
    path, doc = _state(tmp_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["closed_trades"][0]["pnl"] = 12.34  # matches neither formula
    result = rep.repair(state)
    assert [c["id"] for c in result["corrections"]] == [2]
    assert result["skipped_unmatched"][0]["id"] == 1
    assert state["closed_trades"][0]["pnl"] == 12.34


def test_journal_rows_listed(tmp_path):
    path, doc = _state(tmp_path)
    journal = tmp_path / "trade_journal.jsonl"
    rows = [
        {"symbol": "KXHIGHNY-26JUL20-B79.5", "contract_side": "NO", "entry_price": 0.33, "pnl": -16.5, "exit_price": 0.0},
        {"symbol": "KXHIGHCHI-26JUL20-B84.5", "contract_side": "YES", "entry_price": 0.40, "pnl": 6.0, "exit_price": 1.0},
    ]
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    state = json.loads(path.read_text(encoding="utf-8"))
    result = rep.repair(state)
    affected = rep.journal_rows_affected(str(journal), result["corrections"])
    assert [a["symbol"] for a in affected] == ["KXHIGHNY-26JUL20-B79.5"]
