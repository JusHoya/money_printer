"""Tests for the 2026-06-10 settlement reconciliation + reset guards.

Covers:
  * scripts/settlement_reconcile.py — sim-outcome derivation, mismatch
    detection, cache use, offline degradation, threshold/exit behavior.
  * scripts/reset_contaminated_state.py — dry-run NEVER writes; --apply backs
    up then repairs; recompute-from-actual semantics.

NO real HTTP: the network result lookup is monkeypatched everywhere.
"""

import importlib.util
import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Load the two standalone scripts as modules (they live in scripts/, not a
# package). Mirror how the orchestrator imports sibling scripts.
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _load(name: str):
    path = os.path.join(_SCRIPTS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sr = _load("settlement_reconcile")
reset_mod = _load("reset_contaminated_state")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _trade(
    symbol,
    side,
    pnl,
    *,
    reason="EXPIRATION",
    exit_price=None,
    strat="ML BTC 15m",
    qty=10,
    entry=0.01,
    correct=None,
):
    """Build a closed-trade / journal-row dict in the project's shape."""
    if exit_price is None:
        exit_price = 1.0 if pnl > 0 else 0.0
    return {
        "symbol": symbol,
        "strategy_name": strat,
        "contract_side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": qty,
        "pnl": pnl,
        "close_reason": reason,
        "reason": reason,
        "exit_time": "2026-06-09T00:00:00",
        "close_time": "2026-06-09T00:00:00",
        "prediction_correct": (pnl > 0) if correct is None else correct,
        "entry_fee": 0.0,
        "exit_fee": 0.0,
    }


# ---------------------------------------------------------------------------
# sim_recorded_result
# ---------------------------------------------------------------------------
def test_sim_result_yes_holder_win_is_yes():
    t = _trade("KXBTC15M-26JUN040030-30", "YES", pnl=5.0)
    assert sr.sim_recorded_result(t) == "yes"


def test_sim_result_yes_holder_loss_is_no():
    t = _trade("KXBTC15M-26JUN040030-30", "YES", pnl=-1.0)
    assert sr.sim_recorded_result(t) == "no"


def test_sim_result_no_holder_win_is_no():
    t = _trade("KXBTC15M-26JUN040030-30", "NO", pnl=5.0)
    assert sr.sim_recorded_result(t) == "no"


def test_sim_result_no_holder_loss_is_yes():
    t = _trade("KXBTC15M-26JUN040030-30", "NO", pnl=-1.0)
    assert sr.sim_recorded_result(t) == "yes"


def test_sim_result_non_settlement_close_is_skipped():
    # Stop-loss exit at a mid-book price is NOT a settlement -> None.
    t = _trade(
        "KXBTC15M-26JUN040030-30",
        "YES",
        pnl=-2.0,
        reason="STOP_LOSS_PRICE (0.71)",
        exit_price=0.15,
    )
    assert sr.sim_recorded_result(t) is None


# ---------------------------------------------------------------------------
# reconcile — mismatch detection with a MOCKED fetcher
# ---------------------------------------------------------------------------
def test_reconcile_detects_mismatch():
    # Sim recorded YES (it "won" a YES contract) but actual settled NO ->
    # exactly the fictional auto-YES the settlement bug produced.
    trades = [
        _trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9),  # sim=yes
        _trade("KXBTC15M-26JUN040200-00", "YES", pnl=8.0),  # sim=yes
        _trade("KXHIGHNY-26JUN04-B86.5", "NO", pnl=4.0),  # sim=no, matches
    ]

    actual_map = {
        "KXBTC15M-26JUN040030-30": {"result": "no", "status": "finalized"},
        "KXBTC15M-26JUN040200-00": {"result": "no", "status": "finalized"},
        "KXHIGHNY-26JUN04-B86.5": {"result": "no", "status": "finalized"},
    }

    def fake_fetch(ticker):
        return actual_map.get(ticker)

    cache = {}
    summary = sr.reconcile(trades, cache, fetcher=fake_fetch)

    assert summary["checked"] == 3
    assert summary["mismatch_count"] == 2
    assert summary["pnl_delta"] == pytest.approx(17.9)  # 9.9 + 8.0 overstated
    # Per-strategy breakdown attributes both to ML BTC 15m.
    assert summary["by_strategy"]["ML BTC 15m"]["mismatches"] == 2
    # The matching weather trade is not a mismatch.
    tickers = {m["ticker"] for m in summary["mismatches"]}
    assert "KXHIGHNY-26JUN04-B86.5" not in tickers


def test_reconcile_no_mismatch_when_sim_matches_actual():
    trades = [_trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9)]  # sim=yes
    actual_map = {"KXBTC15M-26JUN040030-30": {"result": "yes", "status": "finalized"}}
    summary = sr.reconcile(trades, {}, fetcher=lambda t: actual_map.get(t))
    assert summary["mismatch_count"] == 0
    assert summary["checked"] == 1


def test_reconcile_uses_cache_and_populates_it():
    trades = [_trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9)]
    calls = []

    def fake_fetch(ticker):
        calls.append(ticker)
        return {"result": "no", "status": "finalized"}

    cache = {}
    sr.reconcile(trades, cache, fetcher=fake_fetch)
    # The fetched result is now cached under the project's {ticker:{result,status}} shape.
    assert cache["KXBTC15M-26JUN040030-30"]["result"] == "no"
    assert len(calls) == 1

    # Second run with a pre-populated cache must NOT fetch again.
    calls.clear()
    sr.reconcile(trades, cache, fetcher=fake_fetch)
    assert calls == []


def test_reconcile_offline_degrades_gracefully():
    trades = [
        _trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9),  # in cache
        _trade("KXBTC15M-26JUN040200-00", "YES", pnl=8.0),  # NOT in cache
    ]
    cache = {"KXBTC15M-26JUN040030-30": {"result": "no", "status": "finalized"}}

    def boom(ticker):  # must never be called offline
        raise AssertionError("network called in offline mode")

    summary = sr.reconcile(trades, cache, offline=True, fetcher=boom)
    assert summary["checked"] == 1  # only the cached one
    assert summary["unresolved"] == 1  # the un-cached one
    assert summary["mismatch_count"] == 1  # cached one is a mismatch


def test_reconcile_respects_max_fetch_budget():
    trades = [
        _trade("KXBTC15M-A", "YES", pnl=1.0),
        _trade("KXBTC15M-B", "YES", pnl=1.0),
        _trade("KXBTC15M-C", "YES", pnl=1.0),
    ]
    calls = []

    def fake_fetch(ticker):
        calls.append(ticker)
        return {"result": "no", "status": "finalized"}

    summary = sr.reconcile(trades, {}, max_fetch=1, fetcher=fake_fetch)
    assert len(calls) == 1  # budget capped at 1 live fetch
    assert summary["unresolved"] == 2


# ---------------------------------------------------------------------------
# main() exit codes via threshold (mock the module-level fetcher)
# ---------------------------------------------------------------------------
def test_main_breach_exits_nonzero(tmp_path, monkeypatch):
    state = {"closed_trades": [_trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9)]}
    state_path = tmp_path / "exchange_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    cache_path = tmp_path / "settlement_cache.json"

    monkeypatch.setattr(sr, "EXCHANGE_STATE_PATH", str(state_path))
    monkeypatch.setattr(sr, "TRADE_JOURNAL_PATH", str(tmp_path / "nope.jsonl"))
    monkeypatch.setattr(sr, "SETTLEMENT_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(
        sr,
        "fetch_actual_result",
        lambda t: {"result": "no", "status": "finalized"},
    )
    # Never actually POST to Discord.
    monkeypatch.setattr(sr, "post_discord_alert", lambda *a, **k: None)

    rc = sr.main(["--source", "state", "--threshold", "0", "--no-discord"])
    assert rc == 1  # one mismatch > threshold 0
    # Cache was written.
    written = json.loads(cache_path.read_text(encoding="utf-8"))
    assert written["KXBTC15M-26JUN040030-30"]["result"] == "no"


def test_main_within_threshold_exits_zero(tmp_path, monkeypatch):
    state = {"closed_trades": [_trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9)]}
    state_path = tmp_path / "exchange_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(sr, "EXCHANGE_STATE_PATH", str(state_path))
    monkeypatch.setattr(sr, "TRADE_JOURNAL_PATH", str(tmp_path / "nope.jsonl"))
    monkeypatch.setattr(sr, "SETTLEMENT_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(
        sr,
        "fetch_actual_result",
        lambda t: {"result": "no", "status": "finalized"},
    )

    rc = sr.main(["--source", "state", "--threshold", "5", "--no-discord"])
    assert rc == 0  # one mismatch <= threshold 5


# ---------------------------------------------------------------------------
# reset_contaminated_state — DRY RUN must never write
# ---------------------------------------------------------------------------
def _seed_data_dir(tmp_path, monkeypatch):
    """Write contaminated fixtures and repoint both modules at tmp_path."""
    wr_path = tmp_path / "strategy_win_rates.json"
    state_path = tmp_path / "exchange_state.json"
    journal_path = tmp_path / "trade_journal.jsonl"
    cache_path = tmp_path / "settlement_cache.json"
    backup_dir = tmp_path / "backups"

    # Contaminated: sim said YES-win (+9.9), actual settled NO.
    contaminated = _trade("KXBTC15M-26JUN040030-30", "YES", pnl=9.9)
    wr_path.write_text(json.dumps({"ML BTC 15m": [120, 200]}), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {"closed_trades": [dict(contaminated)], "cumulative_realized_pnl": 9.9}
        ),
        encoding="utf-8",
    )
    journal_path.write_text(json.dumps(contaminated) + "\n", encoding="utf-8")
    cache_path.write_text(
        json.dumps(
            {"KXBTC15M-26JUN040030-30": {"result": "no", "status": "finalized"}}
        ),
        encoding="utf-8",
    )

    # Repoint sr (loaders + cache) and reset_mod (paths + backup dir).
    monkeypatch.setattr(sr, "EXCHANGE_STATE_PATH", str(state_path))
    monkeypatch.setattr(sr, "TRADE_JOURNAL_PATH", str(journal_path))
    monkeypatch.setattr(sr, "SETTLEMENT_CACHE_PATH", str(cache_path))
    monkeypatch.setattr(reset_mod, "WIN_RATES_PATH", str(wr_path))
    monkeypatch.setattr(reset_mod, "EXCHANGE_STATE_PATH", str(state_path))
    monkeypatch.setattr(reset_mod, "TRADE_JOURNAL_PATH", str(journal_path))
    monkeypatch.setattr(reset_mod, "BACKUP_DIR", str(backup_dir))

    return {
        "wr": wr_path,
        "state": state_path,
        "journal": journal_path,
        "cache": cache_path,
        "backups": backup_dir,
    }


def test_reset_dry_run_never_writes(tmp_path, monkeypatch):
    paths = _seed_data_dir(tmp_path, monkeypatch)

    before = {
        "wr": paths["wr"].read_text(encoding="utf-8"),
        "state": paths["state"].read_text(encoding="utf-8"),
        "journal": paths["journal"].read_text(encoding="utf-8"),
    }

    rc = reset_mod.main([])  # default is dry-run
    assert rc == 0

    # NOTHING changed on disk and NO backups were created.
    assert paths["wr"].read_text(encoding="utf-8") == before["wr"]
    assert paths["state"].read_text(encoding="utf-8") == before["state"]
    assert paths["journal"].read_text(encoding="utf-8") == before["journal"]
    assert not paths["backups"].exists()


def test_reset_apply_backs_up_and_repairs(tmp_path, monkeypatch):
    paths = _seed_data_dir(tmp_path, monkeypatch)

    rc = reset_mod.main(["--apply"])
    assert rc == 0

    # Backups were created for each touched file.
    assert paths["backups"].exists()
    backups = list(paths["backups"].iterdir())
    assert any("strategy_win_rates" in b.name for b in backups)
    assert any("exchange_state" in b.name for b in backups)
    assert any("trade_journal" in b.name for b in backups)

    # Win rates rebuilt from truth: the single YES trade actually settled NO,
    # so the YES holder LOST -> 0 wins / 1 total for ML BTC 15m.
    wr = json.loads(paths["wr"].read_text(encoding="utf-8"))
    assert wr["ML BTC 15m"] == [0, 1]

    # Exchange state re-scored: the +9.9 fiction is gone; pnl now reflects the
    # real NO settlement loss (negative), and cumulative updated to match.
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    repaired = state["closed_trades"][0]
    assert repaired["pnl"] < 0
    assert repaired["exit_price"] == 0.0
    assert "settlement_repaired" in repaired
    assert state["cumulative_realized_pnl"] == pytest.approx(repaired["pnl"], abs=1e-6)

    # Journal prediction_correct flipped True -> False.
    line = paths["journal"].read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["prediction_correct"] is False


def test_reset_apply_only_target(tmp_path, monkeypatch):
    paths = _seed_data_dir(tmp_path, monkeypatch)
    journal_before = paths["journal"].read_text(encoding="utf-8")

    rc = reset_mod.main(["--apply", "--only", "win_rates"])
    assert rc == 0

    # Only win_rates touched; journal untouched.
    assert paths["journal"].read_text(encoding="utf-8") == journal_before
    backups = [b.name for b in paths["backups"].iterdir()]
    assert any("strategy_win_rates" in b for b in backups)
    assert not any("trade_journal" in b for b in backups)
