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


def _maia_row(symbol, strategy, side, entry, qty, pnl, exit_price, outcome, correct,
              repaired=True, exit_fee=0.0):
    """A journal row shaped like the ones on maia: PnL already repaired, the
    ``prediction_correct`` flag still derived from the pre-repair sign."""
    row = {
        "symbol": symbol, "strategy_name": strategy, "contract_side": side, "side": "buy",
        "entry_price": entry, "quantity": qty, "pnl": pnl, "exit_price": exit_price,
        "exit_fee": exit_fee, "close_reason": "EXPIRATION", "settlement_outcome": outcome,
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
        # ------------------------------------------------------------------
        # The two rows that make this fixture DISCRIMINATING. Everything above
        # has a PnL sign that agrees with its settlement, so the whole file
        # reads identically under the settlement-truth rule and under the old
        # ``pnl > 0`` one -- reverting prediction_correct_for's body would leave
        # the end-to-end test green. These two do not:
        #   * a NO holder whose bracket settled NO (a CORRECT call) whose 0.03
        #     of edge the 0.60 exit fee ate: settlement truth says True,
        #     ``pnl > 0`` says False;
        #   * a YES holder who won at a 1.00 entry, so the PnL is exactly 0.00:
        #     settlement truth says True, ``pnl > 0`` says None ("unknown").
        # They are their own strategy so the ML Weather / Meteorologist V2
        # windows stay exactly the ones the operator runbook quotes.
        _maia_row("KXHIGHDEN-26SEP04-B70.5", "Bracket Sniper", "NO", 0.97, 10.0, -0.30, 1.0,
                  "no", False, repaired=False, exit_fee=0.60),
        _maia_row("KXHIGHPHX-26SEP04-B105.5", "Bracket Sniper", "YES", 1.00, 10.0, 0.0, 1.0,
                  "yes", None, repaired=False),
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
    assert [f["line"] for f in flags] == [1, 2, 3, 4, 7, 8]
    assert [(f["old"], f["new"]) for f in flags] == [
        (True, False), (True, False), (True, False), (False, True),
        # line 7: a correct call the exit fee turned into a loss -- ``pnl > 0``
        # would leave this row alone, settlement truth corrects it
        (False, True),
        # line 8: pnl exactly 0.00 -- ``pnl > 0`` records None ("unknown") for a
        # settlement whose outcome is in fact known
        (None, True),
    ]
    # the PnL itself is already repaired on these rows -> nothing to re-price,
    # and every row's money agrees with its own bracket
    assert summary["journal"]["n_corrected"] == 0
    assert summary["journal"]["n_pnl_settlement_disagreements"] == 0
    assert journal.read_bytes() == before  # dry run writes nothing

    assert rep.main(["--state", str(path), "--journal", str(journal), "--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["journal"]["applied"] is True
    assert open(summary["journal"]["backup"], "rb").read() == before
    got = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]
    assert [r["prediction_correct"] for r in got] == [
        False, False, False, True, True, False, True, True
    ]
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
        # a strategy the file has never seen is added, not silently ignored
        {"strategy": "Bracket Sniper", "old": None, "new": [0, 1]},
    ]
    assert summary["win_rates"]["warnings"] == []
    assert summary["win_rates"]["drops"] == []
    assert json.loads(wr.read_text(encoding="utf-8"))["ML Weather"]["window"] == [1, 1, 1]

    assert rep.main(args + ["--apply", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["win_rates"]["applied"] is True
    written = json.loads(wr.read_text(encoding="utf-8"))
    assert written["ML Weather"]["window"] == [0, 0, 0]
    assert written["Meteorologist V2"]["window"] == [1, 1, 0]
    assert written["Bracket Sniper"]["window"] == [0, 1]
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


# ---------------------------------------------------------------------------
# F3 review remediation (2026-09-05). The runbook's own acceptance criterion was
# unreachable, the concurrency guard was blind on the one artifact that matters,
# and an abort could claim "nothing written" after something was.
# ---------------------------------------------------------------------------


def test_the_runbook_stops_the_sandbox_and_never_restarts_it():
    """``docker restart`` reverts the win-rate half of this repair.

    ``run_web_dashboard.py`` registers ``atexit.register(engine.shutdown)`` AND a
    SIGTERM handler that calls ``engine.shutdown()``; ``OrchestratorEngine
    .shutdown`` step "2b. Save win rates" calls ``RiskManager._save_win_rates()``,
    which serialises the process's PRE-repair in-memory windows over the file we
    just wrote -- and the restarted process loads them straight back. The only
    correct order is stop -> apply -> start, and the runbook has to say WHY or
    the next operator reaches for ``restart`` again.
    """
    doc = rep.__doc__
    assert "docker restart mp-sandbox" not in doc
    assert "docker stop mp-sandbox" in doc
    assert "docker start mp-sandbox" in doc
    # the mechanism, not just the order
    for mechanism in ("_save_win_rates", "atexit", "SIGTERM", "shutdown"):
        assert mechanism in doc, mechanism
    # and the order is stated as an order
    assert doc.index("Step 1 -- STOP") < doc.index("Step 4 -- APPLY") < doc.index("Step 6 -- START")
    # the pre-flight the operator can actually run
    assert "--preflight" in doc
    assert "{{.State.Running}}" in doc
    # rollback stops first too
    rollback = doc[doc.index("Rollback --"):]
    assert rollback.index("docker stop mp-sandbox") < rollback.index("docker start mp-sandbox")


def test_a_live_risk_manager_really_would_overwrite_the_repaired_windows(tmp_path, monkeypatch):
    """Pins the hazard the runbook is written around, against the REAL RiskManager.

    Not a regression test for this script -- a proof that the ordering rule is
    load-bearing. If this ever stops holding, the runbook can be simplified.
    """
    from collections import deque

    import src.core.risk_manager as rm

    wr = tmp_path / "strategy_win_rates.json"
    monkeypatch.setattr(rm, "WIN_RATES_PATH", str(wr))
    manager = rm.RiskManager.__new__(rm.RiskManager)  # no I/O, no full construction
    manager.strategy_win_records = {
        "ML Weather": deque([1, 1, 1], maxlen=rm.WIN_RATE_WINDOW),
        "Meteorologist V2": deque([0, 1, 0], maxlen=rm.WIN_RATE_WINDOW),
    }
    manager._win_rate_updated = {}
    # the file as this repair leaves it
    wr.write_text(json.dumps({
        "ML Weather": {"window": [0, 0, 0], "updated": "repaired"},
        "Meteorologist V2": {"window": [1, 1, 0], "updated": "repaired"},
    }, indent=2), encoding="utf-8")

    manager._save_win_rates()  # what shutdown step 2b and every close do

    got = json.loads(wr.read_text(encoding="utf-8"))
    assert got["ML Weather"]["window"] == [1, 1, 1]
    assert got["Meteorologist V2"]["window"] == [0, 1, 0]


# --- the pre-flight ---------------------------------------------------------


def _fake_proc(tmp_path, processes, name="proc"):
    """A ``/proc``-shaped tree: ``{pid: [argv, ...]}``."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "notapid").mkdir(exist_ok=True)
    for pid, argv in processes.items():
        d = root / str(pid)
        d.mkdir(exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode("utf-8") for a in argv) + b"\0")
    return str(root)


SANDBOX_ARGV = ["python", "scripts/run_web_dashboard.py", "--auto-cycle",
                "--sim-balance", "3000", "--no-browser", "--host", "0.0.0.0"]


def test_apply_refuses_while_an_orchestrator_process_is_running(tmp_path, capsys):
    path, _ = _state(tmp_path)
    journal, _ = _maia_journal(tmp_path)
    wr = _win_rates(tmp_path, {"ML Weather": {"window": [1, 1, 1], "updated": "u"}})
    proc = _fake_proc(tmp_path, {1234: SANDBOX_ARGV, 7: ["/bin/sh"]})
    args = ["--state", str(path), "--journal", str(journal), "--win-rates", str(wr),
            "--proc-root", proc, "--apply"]
    before = (path.read_bytes(), journal.read_bytes(), wr.read_bytes())

    with pytest.raises(rep.LiveWriterError):
        rep.main(args)
    assert (path.read_bytes(), journal.read_bytes(), wr.read_bytes()) == before

    assert rep.cli(args) == 3
    err = capsys.readouterr().err
    assert "REFUSED" in err and "1234" in err and "docker stop mp-sandbox" in err
    assert (path.read_bytes(), journal.read_bytes(), wr.read_bytes()) == before

    # the operator who knows better can still force it
    assert rep.cli(args + ["--allow-live-writer"]) == 0
    assert path.read_bytes() != before[0]


def test_preflight_refuses_writes_nothing_and_clears_when_the_writer_is_gone(tmp_path, capsys):
    path, _ = _state(tmp_path)
    journal, _ = _maia_journal(tmp_path)
    before = (path.read_bytes(), journal.read_bytes())
    busy = _fake_proc(tmp_path, {1234: SANDBOX_ARGV}, name="proc-busy")
    quiet = _fake_proc(tmp_path, {7: ["/bin/sh"]}, name="proc-quiet")

    args = ["--preflight", "--state", str(path), "--journal", str(journal), "--watch-seconds", "0"]
    assert rep.cli(args + ["--proc-root", busy]) == 3
    out = capsys.readouterr().out
    assert "REFUSE" in out and "1234" in out
    assert (path.read_bytes(), journal.read_bytes()) == before  # --preflight never writes

    assert rep.cli(args + ["--proc-root", quiet]) == 0
    out = capsys.readouterr().out
    assert "CLEAR" in out
    # a clear verdict must not overstate itself
    assert "not proof" in out
    assert (path.read_bytes(), journal.read_bytes()) == before


def test_preflight_write_probe_catches_a_writer_the_process_scan_cannot_see(tmp_path, monkeypatch):
    """A repair in a throwaway container has its own pid namespace, so the scan
    is blind. The probe shares the bind mount, so it is not."""
    path, _ = _state(tmp_path)
    journal, _ = _maia_journal(tmp_path)
    quiet = _fake_proc(tmp_path, {7: ["/bin/sh"]}, name="proc-quiet")

    monkeypatch.setattr(rep, "_sleep", lambda _s: _append_row(journal))
    result = rep.preflight([str(path), str(journal)], proc_root=quiet, watch_seconds=5)
    assert result["clear"] is False
    assert result["writers"] == []
    assert os.path.abspath(str(journal)) in result["changed"]


def test_preflight_that_could_check_nothing_does_not_report_clear(tmp_path, capsys):
    """A green light nobody earned is worse than no light: with no /proc AND no
    write probe the pre-flight checked nothing, so it refuses rather than
    telling the operator to go ahead."""
    path, _ = _state(tmp_path)
    assert rep.cli(["--preflight", "--state", str(path), "--watch-seconds", "0",
                    "--proc-root", str(tmp_path / "no-such-proc")]) == 3
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out and "CLEAR" not in out


def test_missing_proc_warns_rather_than_pretending_the_check_ran(tmp_path, capsys):
    path, _ = _state(tmp_path)
    assert rep.main(["--state", str(path), "--apply", "--proc-root",
                     str(tmp_path / "no-such-proc")]) == 0
    err = capsys.readouterr().err
    assert "WARN preflight" in err and "State.Running" in err


# --- the concurrency guard on the artifact that matters ---------------------


def _rows_from(journal):
    return rep.repair_journal(str(journal), apply=False)["rows"]


def test_a_same_size_win_rate_rewrite_aborts_the_swap(tmp_path, monkeypatch):
    """``_save_win_rates`` rewrites this file IN PLACE, and a window flipping
    ``[1,1,1]`` -> ``[0,0,0]`` is byte-identical in LENGTH. A ``(mtime, size)``
    guard is blind on exactly this file and exactly this mutation, so the guard
    has to compare CONTENT."""
    journal, _ = _maia_journal(tmp_path)
    wr = _win_rates(tmp_path, {
        "ML Weather": {"window": [1, 1, 1], "updated": "2026-09-02T22:48:38+00:00"},
    })
    rows = _rows_from(journal)
    stat_before = os.stat(wr)
    payload_before = wr.read_bytes()

    real = rep.rebuild_win_rates
    fired = []

    def racing(rows_, existing, allow_shrink=False):
        out = real(rows_, existing, allow_shrink)
        if not fired:
            fired.append(True)
            # the live RiskManager flips the window and keeps the byte length,
            # and (pathologically but legally) the mtime lands identical too
            doc = json.loads(wr.read_text(encoding="utf-8"))
            doc["ML Weather"]["window"] = [0, 0, 0]
            wr.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            assert len(wr.read_bytes()) == len(payload_before)
            os.utime(wr, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        return out

    monkeypatch.setattr(rep, "rebuild_win_rates", racing)
    with pytest.raises(rep.ConcurrentWriteError):
        rep.repair_win_rates(str(wr), rows, apply=True)

    # the racing write survived; ours was never swapped in; no stale backup
    assert json.loads(wr.read_text(encoding="utf-8"))["ML Weather"]["window"] == [0, 0, 0]
    assert not os.path.exists(str(wr) + ".bak-1")
    assert not os.path.exists(str(wr) + ".repair-lock")


# --- "nothing written" must be true -----------------------------------------


def test_an_abort_after_the_state_landed_says_what_was_written(tmp_path, monkeypatch, capsys):
    """``main`` swaps the state first and the journal second, so a journal abort
    can leave the state already rewritten. Saying "nothing written" there is a
    lie the operator acts on."""
    path, _ = _state(tmp_path)          # two real corrections -> the state IS written
    journal, _ = _maia_journal(tmp_path)

    real_lines = rep._repair_journal_lines

    def racing(raw_lines):
        # lands after read_lines_snapshot, before swap_in -- exactly where
        # TradeJournal.record hits it on maia
        _append_row(journal)
        return real_lines(raw_lines)

    monkeypatch.setattr(rep, "_repair_journal_lines", racing)
    with pytest.raises(rep.ConcurrentWriteError) as excinfo:
        rep.main(["--state", str(path), "--journal", str(journal), "--apply"])

    assert [w[0] for w in excinfo.value.written] == [str(path)]
    text = rep.format_abort(excinfo.value)
    assert "PARTIALLY APPLIED" in text
    assert str(path) in text
    assert "Nothing was written" not in text
    # the state really did land
    assert json.loads(path.read_text(encoding="utf-8"))["closed_trades"][0]["exit_price"] == 1.0
    # and the journal really did not
    assert b"repaired_no_side_settlement" in journal.read_bytes()  # fixture rows carry it
    assert json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["prediction_correct"] is True


def test_an_abort_with_nothing_written_says_exactly_that(tmp_path, monkeypatch, capsys):
    journal, _ = _maia_journal(tmp_path)
    real_lines = rep._repair_journal_lines

    def racing(raw_lines):
        _append_row(journal)
        return real_lines(raw_lines)

    monkeypatch.setattr(rep, "_repair_journal_lines", racing)
    with pytest.raises(rep.ConcurrentWriteError) as excinfo:
        rep.repair_journal(str(journal), apply=True)
    assert rep.format_abort(excinfo.value).endswith("Nothing was written.")

    # and the CLI reports it on stderr with exit code 2
    path, _ = _state(tmp_path)
    assert rep.cli(["--state", str(path), "--journal", str(journal), "--apply"]) == 2
    err = capsys.readouterr().err
    assert "ABORTED" in err and "PARTIALLY APPLIED" in err  # the state landed this time


# --- the lock the docstring promises ----------------------------------------


def test_the_state_apply_takes_the_repair_lock_too(tmp_path):
    """The docstring promised "every --apply holds an exclusive
    <path>.repair-lock"; only the journal did."""
    path, _ = _state(tmp_path)
    before = path.read_bytes()
    lock = str(path) + ".repair-lock"
    open(lock, "w").close()
    try:
        with pytest.raises(rep.RepairLockedError):
            rep.main(["--state", str(path), "--apply"])
    finally:
        os.unlink(lock)
    assert path.read_bytes() == before
    # the lock is taken and released on a clean run
    assert rep.main(["--state", str(path), "--apply"]) == 0
    assert not os.path.exists(lock)


def test_the_win_rate_apply_takes_the_repair_lock_too(tmp_path):
    path, _ = _state(tmp_path)
    journal, _ = _maia_journal(tmp_path)
    wr = _win_rates(tmp_path, {"ML Weather": {"window": [1, 1, 1], "updated": "u"}})
    before = wr.read_bytes()
    lock = str(wr) + ".repair-lock"
    open(lock, "w").close()
    try:
        with pytest.raises(rep.RepairLockedError) as excinfo:
            rep.main(["--state", str(path), "--journal", str(journal),
                      "--win-rates", str(wr), "--apply"])
    finally:
        os.unlink(lock)
    assert wr.read_bytes() == before
    # the earlier passes DID land, and the abort message says so
    assert [w[0] for w in excinfo.value.written] == [str(path), str(journal)]
    assert "PARTIALLY APPLIED" in rep.format_abort(excinfo.value)


# --- win-rate rebuild: legacy entries and window growth ---------------------


def test_legacy_win_rate_entries_are_dropped_not_resurrected():
    """FR-0.6 ``_load_win_rates`` IGNORES legacy ``[wins, total]`` entries and
    ``_save_win_rates`` drops them at the next close, so copying them into the
    rebuilt file would put back, in the artifact, a record the runtime has
    already discarded."""
    rows = [{"strategy_name": "S", "pnl": 1.0, "close_reason": "EXPIRATION"}]
    existing = {
        "S": {"window": [1], "updated": "u"},
        "Old Crypto Fader": [3, 5],        # legacy cumulative counter
        "Weird": "not a window",
        "Late Sniper": {"window": [1, 0], "updated": "keep me"},
    }
    out = rep.rebuild_win_rates(rows, existing)
    assert "Old Crypto Fader" not in out["win_rates"]
    assert "Weird" not in out["win_rates"]
    assert out["win_rates"]["Late Sniper"] == {"window": [1, 0], "updated": "keep me"}
    assert sorted(d["strategy"] for d in out["drops"]) == ["Old Crypto Fader", "Weird"]
    assert any("Old Crypto Fader" in w for w in out["warnings"])


def test_a_legacy_entry_alone_is_enough_to_rewrite_the_file(tmp_path, capsys):
    """Nothing else changing must not leave the legacy entry sitting there."""
    path, _ = _state(tmp_path)
    journal, _ = _maia_journal(tmp_path)
    wr = _win_rates(tmp_path, {
        # already exactly what the journal replays -> zero window changes
        "ML Weather": {"window": [0, 0, 0], "updated": "a"},
        "Meteorologist V2": {"window": [1, 1, 0], "updated": "b"},
        "Bracket Sniper": {"window": [0, 1], "updated": "c"},
        "Old Crypto Fader": [3, 5],
    })
    args = ["--state", str(path), "--journal", str(journal), "--win-rates", str(wr)]

    assert rep.main(args + ["--json"]) == 1  # pending: the drop
    summary = json.loads(capsys.readouterr().out)
    assert summary["win_rates"]["changes"] == []
    assert summary["win_rates"]["drops"] == [{"strategy": "Old Crypto Fader", "value": [3, 5]}]
    assert "Old Crypto Fader" in json.loads(wr.read_text(encoding="utf-8"))  # dry run

    assert rep.main(args + ["--apply"]) == 0
    written = json.loads(wr.read_text(encoding="utf-8"))
    assert "Old Crypto Fader" not in written
    assert written["ML Weather"] == {"window": [0, 0, 0], "updated": "a"}  # untouched
    assert "DROPPED" in capsys.readouterr().out


def test_a_longer_rebuilt_window_warns_and_is_written():
    """Growing n changes Kelly sizing just as shrinking it does; the guard was
    one-sided, so a grow landed silently."""
    rows = [{"strategy_name": "S", "pnl": 1.0, "close_reason": "EXPIRATION"},
            {"strategy_name": "S", "pnl": -5.0, "close_reason": "EXPIRATION"}]
    existing = {"S": {"window": [1], "updated": "u"}}
    out = rep.rebuild_win_rates(rows, existing)
    assert out["win_rates"]["S"]["window"] == [1, 0]
    assert out["changes"] == [{"strategy": "S", "old": [1], "new": [1, 0]}]
    assert any("LONGER" in w for w in out["warnings"]), out["warnings"]
    # an unchanged-length rebuild stays quiet
    quiet = rep.rebuild_win_rates(rows, {"S": {"window": [0, 0], "updated": "u"}})
    assert quiet["warnings"] == []


# --- the independent detector the reconcile can no longer be -----------------


def test_expected_settlement_pnl_reads_only_the_settlement_inputs():
    from src.ml.trade_journal import expected_settlement_pnl, settlement_pnl_disagreement

    won_no = {"contract_side": "NO", "settlement_outcome": "no", "close_reason": "EXPIRATION",
              "entry_price": 0.33, "quantity": 50.0}
    assert expected_settlement_pnl(won_no) == pytest.approx(33.5)
    lost_no = dict(won_no, settlement_outcome="yes")
    assert expected_settlement_pnl(lost_no) == pytest.approx(-16.5)
    # fees come off the winner
    assert expected_settlement_pnl(dict(won_no, exit_fee=0.6)) == pytest.approx(32.9)
    # not derivable: no outcome, or a mid-book exit
    assert expected_settlement_pnl(dict(won_no, settlement_outcome=None)) is None
    assert expected_settlement_pnl(dict(won_no, close_reason="STOP_LOSS")) is None
    assert expected_settlement_pnl({"settlement_outcome": "no", "close_reason": "EXPIRATION"}) is None

    # the F3 shape: the money says one thing, the bracket another
    assert settlement_pnl_disagreement(dict(won_no, pnl=-16.5)) == pytest.approx(-50.0)
    assert settlement_pnl_disagreement(dict(won_no, pnl=33.5)) is None
    # a correct call the exit fee ate is NOT a disagreement (a pnl-sign check
    # would have called it one)
    assert settlement_pnl_disagreement(
        {"contract_side": "NO", "settlement_outcome": "no", "close_reason": "EXPIRATION",
         "entry_price": 0.97, "quantity": 10.0, "exit_fee": 0.60, "pnl": -0.30}
    ) is None


def test_a_pnl_that_contradicts_its_own_bracket_is_reported_not_repaired(tmp_path, capsys):
    """The row ``classify`` cannot prove: priced on the correct NO leg, so it is
    left alone, but carrying the pre-repair money. Nothing else in the pipeline
    looks at this any more -- ``settlement_reconcile`` now reads
    ``prediction_correct``, which is itself derived from the settlement."""
    path, _ = _state(tmp_path)
    rows = [
        # exit_price is already the NO-leg payoff -> classify() says "ok"
        {"symbol": "KXHIGHNY-26SEP05-B80.5", "strategy_name": "Meteorologist V2",
         "contract_side": "NO", "side": "buy", "entry_price": 0.33, "quantity": 50.0,
         "exit_price": 1.0, "pnl": -16.5, "close_reason": "EXPIRATION",
         "settlement_outcome": "no", "prediction_correct": True},
    ]
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_bytes(("".join(json.dumps(r) + "\n" for r in rows)).encode("utf-8"))
    before = journal.read_bytes()

    assert rep.main(["--state", str(path), "--journal", str(journal), "--json"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["journal"]["n_corrected"] == 0
    assert summary["journal"]["n_flags_corrected"] == 0  # the flag already agrees
    disagreements = summary["journal"]["pnl_settlement_disagreements"]
    assert len(disagreements) == 1
    assert disagreements[0]["line"] == 1
    assert disagreements[0]["delta"] == pytest.approx(-50.0)
    assert disagreements[0]["expected_pnl"] == pytest.approx(33.5)

    # reported, never silently "fixed"
    rep.main(["--state", str(path), "--journal", str(journal), "--apply"])
    assert journal.read_bytes() == before
    out = capsys.readouterr().out
    assert "PNL/SETTLEMENT DISAGREEMENT" in out
    assert "NOT repaired, investigate" in out
