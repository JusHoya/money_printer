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


def _journal(tmp_path):
    rows = [
        # stale NO win (journal carries close_reason, not reason)
        {"symbol": "KXHIGHNY-26JUL20-B79.5", "strategy_name": "Meteorologist V2", "contract_side": "NO", "side": "buy",
         "entry_price": 0.33, "quantity": 50.0, "pnl": -16.5, "exit_price": 0.0, "close_reason": "EXPIRATION",
         "settlement_outcome": "no", "entry_time": "2026-07-19T15:00:00"},
        # YES row: untouched
        {"symbol": "KXHIGHCHI-26JUL20-B84.5", "strategy_name": "Meteorologist V2", "contract_side": "YES", "side": "buy",
         "entry_price": 0.40, "quantity": 10.0, "pnl": 6.0, "exit_price": 1.0, "close_reason": "EXPIRATION",
         "settlement_outcome": "yes"},
        # stale NO loss booked as a fake profit
        {"symbol": "KXHIGHNY-26JUL21-B86.5", "strategy_name": "Meteorologist V2", "contract_side": "NO", "side": "buy",
         "entry_price": 0.60, "quantity": 10.0, "pnl": 4.0, "exit_price": 1.0, "close_reason": "EARLY_SETTLEMENT",
         "settlement_outcome": "yes"},
        # NO without an outcome: ambiguous, left alone
        {"symbol": "KXHIGHMIA-26JUL21-B90.5", "strategy_name": "Meteorologist V2", "contract_side": "NO", "side": "buy",
         "entry_price": 0.30, "quantity": 10.0, "pnl": -3.0, "exit_price": 0.0, "close_reason": "EXPIRATION"},
    ]
    journal = tmp_path / "trade_journal.jsonl"
    # a blank line and a non-JSON line must be copied through byte-for-byte
    body = json.dumps(rows[0]) + "\n" + json.dumps(rows[1]) + "\n\n" + json.dumps(rows[2]) + "\n# comment\n" + json.dumps(rows[3]) + "\n"
    journal.write_bytes(body.encode("utf-8"))
    return journal, body


def test_journal_repair_dry_run_then_apply_then_idempotent(tmp_path, capsys):
    path, doc = _state(tmp_path)
    journal, before = _journal(tmp_path)
    # dry run: both files listed, nothing written, exit 1 (repairs pending)
    assert rep.main(["--state", str(path), "--journal", str(journal), "--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["journal"]["n_corrected"] == 2 and summary["journal"]["applied"] is False
    assert [c["line"] for c in summary["journal"]["corrections"]] == [1, 4]
    assert summary["journal"]["n_unmatched_skipped"] == 1  # the outcome-less NO
    assert journal.read_bytes().decode("utf-8") == before

    assert rep.main(["--state", str(path), "--journal", str(journal), "--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["applied"] is True and summary["journal"]["applied"] is True
    backup = summary["journal"]["backup"]
    assert os.path.exists(backup) and open(backup, encoding="utf-8", newline="").read() == before
    lines = journal.read_bytes().decode("utf-8").split("\n")
    r0, r2, r4 = json.loads(lines[0]), json.loads(lines[3]), json.loads(lines[5])
    assert r0["exit_price"] == 1.0 and r0["pnl"] == pytest.approx(33.5) and r0["repaired_no_side_settlement"] is True
    assert r2["exit_price"] == 0.0 and r2["pnl"] == pytest.approx(-6.0) and r2["repaired_no_side_settlement"] is True
    assert "repaired_no_side_settlement" not in r4 and r4["pnl"] == -3.0
    assert lines[1] == json.dumps({"symbol": "KXHIGHCHI-26JUL20-B84.5", "strategy_name": "Meteorologist V2",
                                   "contract_side": "YES", "side": "buy", "entry_price": 0.40, "quantity": 10.0,
                                   "pnl": 6.0, "exit_price": 1.0, "close_reason": "EXPIRATION", "settlement_outcome": "yes"})
    assert lines[2] == "" and lines[4] == "# comment"

    # idempotent: a second pass changes nothing and exits 0
    after = journal.read_bytes().decode("utf-8")
    assert rep.main(["--state", str(path), "--journal", str(journal), "--apply"]) == 0
    assert journal.read_bytes().decode("utf-8") == after
    assert not os.path.exists(str(journal) + ".bak-2")


# ---------------------------------------------------------------------------
# The two consumers of the inverted sign that the PnL-only repair left behind
# (F3 review follow-up, 2026-09-05). Both were live on maia: the four repaired
# rows contradicted their own repaired PnL, and the Kelly windows still counted
# three money-losing trades as wins.
# ---------------------------------------------------------------------------


def _maia_row(symbol, strategy, side, entry, qty, pnl, exit_price, outcome, correct, repaired=True):
    """A journal row shaped like the ones on maia: PnL already repaired, the
    ``prediction_correct`` flag still derived from the pre-repair sign."""
    row = {
        "symbol": symbol, "strategy_name": strategy, "contract_side": side, "side": "buy",
        "entry_price": entry, "quantity": qty, "pnl": pnl, "exit_price": exit_price,
        "close_reason": "EXPIRATION", "settlement_outcome": outcome,
        "prediction_correct": correct,
    }
    if repaired:
        row["repaired_no_side_settlement"] = True
    return row


def _maia_journal(tmp_path):
    rows = [
        # three ML Weather NO trades that LOST money but book prediction_correct=True
        _maia_row("KXHIGHNY-26SEP01-T83", "ML Weather", "NO", 0.17, 50.0, -8.5, 0.0, "yes", True),
        _maia_row("KXHIGHLAX-26SEP01-B76.5", "ML Weather", "NO", 0.37, 50.0, -18.5, 0.0, "yes", True),
        _maia_row("KXHIGHMIA-26SEP01-B89.5", "ML Weather", "NO", 0.34, 50.0, -17.0, 0.0, "yes", True),
        # a Meteorologist V2 NO trade that WON but books prediction_correct=False
        _maia_row("KXHIGHNY-26SEP03-T83", "Meteorologist V2", "NO", 0.52, 42.0, 20.16, 1.0, "no", False),
        # already consistent: untouched
        _maia_row("KXHIGHCHI-26SEP03-B92.5", "Meteorologist V2", "YES", 0.39, 50.0, 30.5, 1.0, "yes", True, repaired=False),
        _maia_row("KXHIGHLAX-26SEP04-B77.5", "Meteorologist V2", "NO", 0.40, 50.0, -20.0, 0.0, "yes", False, repaired=False),
    ]
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_bytes(("".join(json.dumps(r) + "\n" for r in rows)).encode("utf-8"))
    return journal, rows


def _win_rates(tmp_path, data):
    path = tmp_path / "strategy_win_rates.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_prediction_correct_is_settlement_truth_not_pnl_sign():
    """``TradeOutcome`` must derive the flag from the settlement, not the money.

    Deriving it from ``pnl`` made it a second copy of the PnL, so the NO-side
    sign inversion inverted the flag with it -- and
    ``settlement_reconcile.sim_recorded_result`` reads this field IN PREFERENCE
    to ``pnl``, turning every such row into a daily false settlement breach.
    """
    from src.ml.trade_journal import TradeOutcome, prediction_correct_for

    # a NO holder whose bracket settled NO won, even when the exit fee ate the edge
    assert prediction_correct_for({"contract_side": "NO", "settlement_outcome": "no"}, -0.5) is True
    # the pre-repair shape: NO holder, bracket settled YES -> lost, whatever the sign said
    assert prediction_correct_for({"contract_side": "NO", "settlement_outcome": "yes"}, 4.0) is False
    assert prediction_correct_for({"contract_side": "YES", "settlement_outcome": "yes"}, 0.0) is True
    assert prediction_correct_for({"contract_side": "YES", "settlement_outcome": "no"}, 0.0) is False
    # no recorded outcome (stop-loss, SETTLEMENT_UNRESOLVED, pre-FR-1.2): PnL sign
    assert prediction_correct_for({"contract_side": "NO"}, -1.0) is False
    assert prediction_correct_for({"contract_side": "NO", "settlement_outcome": None}, 0.0) is None

    # and it is what the journal actually writes
    out = TradeOutcome.from_position({
        "symbol": "KXHIGHNY-26SEP01-T83", "strategy_name": "ML Weather", "contract_side": "NO",
        "entry_price": 0.17, "quantity": 50.0, "exit_price": 1.0, "pnl": -0.5,
        "reason": "EXPIRATION", "settlement_outcome": "no",
    })
    assert out.prediction_correct is True


def test_journal_repair_corrects_prediction_correct_contradicting_the_repaired_pnl(tmp_path, capsys):
    path, _ = _state(tmp_path)
    journal, rows = _maia_journal(tmp_path)
    before = journal.read_bytes()

    assert rep.main(["--state", str(path), "--journal", str(journal), "--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    flags = summary["journal"]["flag_corrections"]
    assert [f["line"] for f in flags] == [1, 2, 3, 4]
    assert [(f["old"], f["new"]) for f in flags] == [
        (True, False), (True, False), (True, False), (False, True)
    ]
    # the PnL itself is already repaired on these rows -> nothing to re-price
    assert summary["journal"]["n_corrected"] == 0
    assert journal.read_bytes() == before  # dry run writes nothing

    assert rep.main(["--state", str(path), "--journal", str(journal), "--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["journal"]["applied"] is True
    assert open(summary["journal"]["backup"], "rb").read() == before
    got = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]
    assert [r["prediction_correct"] for r in got] == [False, False, False, True, True, False]
    # every other field survives the rewrite
    assert got[3]["pnl"] == pytest.approx(20.16) and got[3]["settlement_outcome"] == "no"

    # idempotent: a second pass changes nothing and leaves no second backup
    after = journal.read_bytes()
    assert rep.main(["--state", str(path), "--journal", str(journal), "--apply"]) == 0
    assert journal.read_bytes() == after
    assert not os.path.exists(str(journal) + ".bak-2")


def test_row_without_the_flag_is_never_given_one(tmp_path):
    """A row that never carried ``prediction_correct`` is silent, not wrong."""
    journal, _before = _journal(tmp_path)  # none of these rows carry the flag
    result = rep.repair_journal(str(journal), apply=True)
    assert result["flag_corrections"] == []
    for line in journal.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("{"):
            assert "prediction_correct" not in line


def test_win_rates_are_replayed_from_the_repaired_journal(tmp_path, capsys):
    """The FR-0.6 Kelly windows were written from the buggy PnL at close time."""
    path, _ = _state(tmp_path)
    journal, _ = _maia_journal(tmp_path)
    wr = _win_rates(tmp_path, {
        "ML Weather": {"window": [1, 1, 1], "updated": "2026-09-02T22:48:38.464338+00:00"},
        "Meteorologist V2": {"window": [0, 1, 0], "updated": "2026-09-05T07:00:00.465817+00:00"},
        # a strategy the journal cannot speak to must survive untouched
        "Late Sniper": {"window": [1, 0], "updated": "2026-08-01T00:00:00+00:00"},
    })
    args = ["--state", str(path), "--journal", str(journal), "--win-rates", str(wr)]

    assert rep.main(args + ["--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["win_rates"]["changes"] == [
        {"strategy": "ML Weather", "old": [1, 1, 1], "new": [0, 0, 0]},
        {"strategy": "Meteorologist V2", "old": [0, 1, 0], "new": [1, 1, 0]},
    ]
    assert summary["win_rates"]["warnings"] == []
    assert json.loads(wr.read_text(encoding="utf-8"))["ML Weather"]["window"] == [1, 1, 1]

    assert rep.main(args + ["--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["win_rates"]["applied"] is True
    written = json.loads(wr.read_text(encoding="utf-8"))
    assert written["ML Weather"]["window"] == [0, 0, 0]
    assert written["Meteorologist V2"]["window"] == [1, 1, 0]
    assert written["Late Sniper"] == {"window": [1, 0], "updated": "2026-08-01T00:00:00+00:00"}

    # idempotent: identical windows are not rewritten, so `updated` does not churn
    stamp = written["ML Weather"]["updated"]
    assert rep.main(args + ["--apply"]) == 0
    assert json.loads(wr.read_text(encoding="utf-8"))["ML Weather"]["updated"] == stamp
    assert not os.path.exists(str(wr) + ".bak-2")


def test_win_rate_replay_counts_every_close_at_the_risk_manager_threshold():
    """``_on_trade_close`` appends for EVERY close and is fees-tolerant."""
    from src.core.risk_manager import FEE_TOLERANCE

    rows = [
        {"strategy_name": "S", "pnl": -FEE_TOLERANCE / 2, "close_reason": "EXPIRATION"},
        {"strategy_name": "S", "pnl": -FEE_TOLERANCE * 2, "close_reason": "EXPIRATION"},
        # a price-based exit: no settlement, but the RiskManager still counted it
        {"strategy_name": "S", "pnl": 3.0, "close_reason": "STOP_LOSS"},
    ]
    result = rep.rebuild_win_rates(rows, {})
    assert result["win_rates"]["S"]["window"] == [1, 0, 1]


def test_shorter_rebuilt_window_is_refused_unless_forced():
    """The journal cannot account for the whole window -> do not destroy it."""
    rows = [{"strategy_name": "ML Weather", "pnl": -8.5, "close_reason": "EXPIRATION"}]
    existing = {"ML Weather": {"window": [1, 1, 1], "updated": "x"}}
    guarded = rep.rebuild_win_rates(rows, existing)
    assert guarded["changes"] == []
    assert "shorter than the stored one" in guarded["warnings"][0]
    assert guarded["win_rates"]["ML Weather"]["window"] == [1, 1, 1]

    forced = rep.rebuild_win_rates(rows, existing, allow_shrink=True)
    assert forced["warnings"] == []
    assert forced["win_rates"]["ML Weather"]["window"] == [0]


# ---------------------------------------------------------------------------
# Concurrency: the sandbox settles on a timer, so TradeJournal.record can append
# while this runs. A snapshot swapped in over a grown file destroys those rows.
# ---------------------------------------------------------------------------


def _append_row(journal, symbol="KXHIGHDEN-26SEP05-B70.5"):
    row = {"symbol": symbol, "strategy_name": "Meteorologist V2", "contract_side": "YES",
           "side": "buy", "entry_price": 0.5, "quantity": 10.0, "pnl": 5.0, "exit_price": 1.0,
           "close_reason": "EXPIRATION", "settlement_outcome": "yes", "prediction_correct": True}
    with open(journal, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def test_row_appended_mid_repair_aborts_the_swap_and_survives(tmp_path, monkeypatch):
    journal, _ = _maia_journal(tmp_path)
    before = journal.read_bytes()

    # Hook a per-row call so the append lands after the snapshot was read and
    # before the swap -- exactly where TradeJournal.record hits it on maia.
    real = rep.classify
    fired = []

    def racing(trade):
        if not fired:
            fired.append(_append_row(journal))
        return real(trade)

    monkeypatch.setattr(rep, "classify", racing)
    with pytest.raises(rep.ConcurrentWriteError):
        rep.repair_journal(str(journal), apply=True)

    now = journal.read_bytes()
    assert now.startswith(before)                       # nothing was rewritten
    assert b"KXHIGHDEN-26SEP05-B70.5" in now            # the appended row survived
    assert not os.path.exists(str(journal) + ".bak-1")  # no stale backup left
    assert not os.path.exists(str(journal) + ".repair-lock")


def test_state_saved_mid_repair_aborts_the_swap(tmp_path, monkeypatch):
    path, _doc = _state(tmp_path)
    before = path.read_bytes()
    real = rep.repair

    def racing(state):
        out = real(state)
        with open(path, "a", encoding="utf-8", newline="") as fh:  # the exchange persists
            fh.write("\n")
        return out

    monkeypatch.setattr(rep, "repair", racing)
    with pytest.raises(rep.ConcurrentWriteError):
        rep.main(["--state", str(path), "--apply"])
    assert path.read_bytes() == before + b"\n"
    assert not os.path.exists(str(path) + ".bak-1")


def test_second_repair_is_locked_out(tmp_path):
    journal, _ = _maia_journal(tmp_path)
    lock = str(journal) + ".repair-lock"
    open(lock, "w").close()
    try:
        with pytest.raises(rep.RepairLockedError):
            rep.repair_journal(str(journal), apply=True)
    finally:
        os.unlink(lock)
    # the lock is released again on a clean run
    rep.repair_journal(str(journal), apply=True)
    assert not os.path.exists(lock)


def test_backup_is_a_byte_exact_copy_of_the_file_it_replaces(tmp_path, capsys):
    """The backup is the undo, so it must be the bytes it replaced -- not a
    re-emission of the snapshot through a text-mode writer (which rewrites line
    endings, and before the stat guard could also miss appended rows)."""
    path, doc = _state(tmp_path)
    path.write_bytes(json.dumps(doc, indent=2).encode("utf-8"))  # LF, not CRLF
    before = path.read_bytes()
    assert rep.main(["--state", str(path), "--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert open(summary["backup"], "rb").read() == before
