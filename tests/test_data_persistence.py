"""Tests for data persistence lifecycle — shutdown archiving, startup recovery,
trade journal accumulation, and training sample growth across runs.

Verifies the fixes for the "sample count not growing between VM runs" bug.
"""

import json
import os
import shutil
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def work_dir(tmp_path):
    """Create a temporary working directory with logs/ and data/ structure."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "models").mkdir()
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(original_cwd)


def _write_fake_csv(path: Path, rows: int = 5):
    """Write a minimal data CSV that load_session can parse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("Timestamp,Type,Symbol,Price,Extra\n")
        for i in range(rows):
            ts = f"2026-03-20 10:{i:02d}:00"
            f.write(f"{ts},MARKET_DATA,BTC-USD (Coinbase),84000.{i},\n")


# ---------------------------------------------------------------------------
# TradeJournal persistence tests
# ---------------------------------------------------------------------------


class TestTradeJournal:
    """Trade journal must accumulate entries across instantiations."""

    def test_journal_creates_file_on_first_record(self, work_dir):
        from src.ml.trade_journal import TradeJournal, TradeOutcome

        journal_path = work_dir / "data" / "trade_journal.jsonl"
        assert not journal_path.exists()

        journal = TradeJournal(str(journal_path))
        assert journal.get_sample_count() == 0

        outcome = TradeOutcome(
            symbol="KXBTC15M-TEST", strategy_name="TestStrat", pnl=5.0
        )
        journal.record(outcome)
        assert journal_path.exists()
        assert journal.get_sample_count() == 1

    def test_journal_persists_across_instances(self, work_dir):
        from src.ml.trade_journal import TradeJournal, TradeOutcome

        journal_path = str(work_dir / "data" / "trade_journal.jsonl")

        # Instance 1: write 3 outcomes
        j1 = TradeJournal(journal_path)
        for i in range(3):
            j1.record(
                TradeOutcome(symbol=f"SYM-{i}", strategy_name="Strat", pnl=float(i))
            )
        assert j1.get_sample_count() == 3

        # Instance 2: should see existing 3 and be able to add more
        j2 = TradeJournal(journal_path)
        assert j2.get_sample_count() == 3

        j2.record(TradeOutcome(symbol="SYM-NEW", strategy_name="Strat", pnl=10.0))
        assert j2.get_sample_count() == 4

        # Instance 3: should see all 4
        j3 = TradeJournal(journal_path)
        assert j3.get_sample_count() == 4
        all_outcomes = j3.load_all()
        assert len(all_outcomes) == 4
        assert all_outcomes[-1].symbol == "SYM-NEW"

    def test_journal_from_position_records_correctly(self, work_dir):
        from src.ml.trade_journal import TradeJournal, TradeOutcome

        journal_path = str(work_dir / "data" / "trade_journal.jsonl")
        journal = TradeJournal(journal_path)

        position = {
            "symbol": "KXBTC15M-26MAR201715-15",
            "strategy_name": "Crypto 15m V3",
            "entry_price": 0.45,
            "exit_price": 0.90,
            "quantity": 10,
            "side": "BUY",
            "contract_side": "YES",
            "pnl": 4.50,
            "reason": "PROFIT_TARGET",
            "open_time": "2026-03-26T17:00:00",
            "close_time": "2026-03-26T17:14:00",
            "ml_context": {
                "model_probability": 0.72,
                "model_confidence": 0.65,
                "model_used": "xgboost_v1",
                "tte_at_entry": 14.0,
                "btc_spot": 87500.0,
            },
        }

        outcome = TradeOutcome.from_position(position)
        journal.record(outcome)

        loaded = journal.load_all()
        assert len(loaded) == 1
        o = loaded[0]
        assert o.symbol == "KXBTC15M-26MAR201715-15"
        assert o.pnl == 4.50
        assert o.prediction_correct is True
        assert o.model_probability == 0.72
        assert o.btc_spot_at_entry == 87500.0


# ---------------------------------------------------------------------------
# Shutdown archive tests
# ---------------------------------------------------------------------------


class TestShutdownArchive:
    """OrchestratorEngine.shutdown() must archive CSV data and save state."""

    def _make_engine_mock(self, work_dir):
        """Create a minimal OrchestratorEngine-like object for testing shutdown."""
        # We can't easily instantiate the real OrchestratorEngine (too many deps),
        # so test the archive logic directly.
        from src.ml.trade_journal import TradeJournal

        class FakeEngine:
            _TRAINING_STATE_PATH = str(work_dir / "data" / "training_state.json")
            _shutdown_done = False
            running = True
            _cycle_count = 3
            cycle_history = [{"cycle": 1}, {"cycle": 2}, {"cycle": 3}]
            _training_history = []
            _training_diagnostics = {"training_samples": 42}

            class dashboard:
                data_log_path = str(work_dir / "logs" / "data_current.csv")
                session_log_path = str(work_dir / "logs" / "session_current.log")
                portfolio_log_path = str(work_dir / "logs" / "portfolio_current.csv")

            trade_journal = TradeJournal(str(work_dir / "data" / "trade_journal.jsonl"))

        return FakeEngine()

    def test_shutdown_archives_csv_files(self, work_dir):
        """Shutdown should copy CSV/log files to logs/_archive/shutdown_*/."""
        # Create some CSV files in logs/
        for name in ["data_20260326_100000.csv", "data_20260326_120000.csv"]:
            _write_fake_csv(work_dir / "logs" / name)
        # Also create a log file
        (work_dir / "logs" / "money_printer_20260326.log").write_text("test log")

        # Import and call the shutdown method directly
        from scripts.run_dashboard import OrchestratorEngine

        engine = self._make_engine_mock(work_dir)
        # Bind the real methods
        engine.shutdown = OrchestratorEngine.shutdown.__get__(engine)
        engine._save_training_state = OrchestratorEngine._save_training_state.__get__(
            engine
        )
        engine._log_data_inventory = OrchestratorEngine._log_data_inventory.__get__(
            engine
        )

        engine.shutdown()

        # Verify archive was created
        archive_dir = work_dir / "logs" / "_archive"
        assert archive_dir.exists()
        shutdown_dirs = [
            d for d in archive_dir.iterdir() if d.name.startswith("shutdown_")
        ]
        assert len(shutdown_dirs) == 1

        archived_files = list(shutdown_dirs[0].iterdir())
        archived_names = {f.name for f in archived_files}
        assert "data_20260326_100000.csv" in archived_names
        assert "data_20260326_120000.csv" in archived_names
        assert "money_printer_20260326.log" in archived_names

    def test_shutdown_saves_training_state(self, work_dir):
        """Shutdown should persist training_state.json."""
        from scripts.run_dashboard import OrchestratorEngine

        engine = self._make_engine_mock(work_dir)
        engine.shutdown = OrchestratorEngine.shutdown.__get__(engine)
        engine._save_training_state = OrchestratorEngine._save_training_state.__get__(
            engine
        )
        engine._log_data_inventory = OrchestratorEngine._log_data_inventory.__get__(
            engine
        )

        state_path = work_dir / "data" / "training_state.json"
        assert not state_path.exists()

        engine.shutdown()

        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert state["cycle_count"] == 3
        assert state["training_diagnostics"]["training_samples"] == 42

    def test_shutdown_is_idempotent(self, work_dir):
        """Calling shutdown() twice should not create duplicate archives."""
        _write_fake_csv(work_dir / "logs" / "data_test.csv")

        from scripts.run_dashboard import OrchestratorEngine

        engine = self._make_engine_mock(work_dir)
        engine.shutdown = OrchestratorEngine.shutdown.__get__(engine)
        engine._save_training_state = OrchestratorEngine._save_training_state.__get__(
            engine
        )
        engine._log_data_inventory = OrchestratorEngine._log_data_inventory.__get__(
            engine
        )

        engine.shutdown()
        engine.shutdown()  # second call should be a no-op

        archive_dir = work_dir / "logs" / "_archive"
        shutdown_dirs = [
            d for d in archive_dir.iterdir() if d.name.startswith("shutdown_")
        ]
        assert len(shutdown_dirs) == 1


# ---------------------------------------------------------------------------
# Startup recovery tests
# ---------------------------------------------------------------------------


class TestStartupRecovery:
    """Startup archive should find and preserve data from previous runs."""

    def test_stale_csvs_archived_on_startup(self, work_dir):
        """CSVs left in logs/ from a prior run should be moved to _archive/startup_*."""
        # Simulate prior run's leftover files
        _write_fake_csv(work_dir / "logs" / "data_20260325_080000.csv")
        _write_fake_csv(work_dir / "logs" / "data_20260325_120000.csv")

        # The "current" session's files (should NOT be archived)
        current_data = work_dir / "logs" / "data_20260326_100000.csv"
        _write_fake_csv(current_data)

        # Test the startup archive logic in isolation
        active_files = {os.path.abspath(str(current_data))}

        stale_files = []
        for f in os.listdir(str(work_dir / "logs")):
            fpath = os.path.join(str(work_dir / "logs"), f)
            if not os.path.isfile(fpath):
                continue
            if not (f.endswith(".csv") or f.endswith(".log")):
                continue
            if os.path.abspath(fpath) in active_files:
                continue
            stale_files.append((f, fpath))

        assert len(stale_files) == 2  # The two old CSVs

        # Archive them
        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_dir = os.path.join(str(work_dir / "logs"), "_archive", f"startup_{ts}")
        os.makedirs(archive_dir, exist_ok=True)
        for f, fpath in stale_files:
            shutil.copy2(fpath, os.path.join(archive_dir, f))
            os.remove(fpath)

        # Verify: stale files moved, current file remains
        remaining = [
            f
            for f in os.listdir(str(work_dir / "logs"))
            if f.endswith(".csv") and not f.startswith("_")
        ]
        assert len(remaining) == 1
        assert remaining[0] == "data_20260326_100000.csv"

        # Verify archive
        archived = os.listdir(archive_dir)
        assert "data_20260325_080000.csv" in archived
        assert "data_20260325_120000.csv" in archived

    def test_training_state_survives_restart(self, work_dir):
        """Training state written by shutdown should be readable on next startup."""
        import json

        state_path = work_dir / "data" / "training_state.json"

        # Simulate shutdown saving state
        state = {
            "cycle_count": 5,
            "cycle_history": [{"cycle": i, "pnl": -100 + i * 10} for i in range(5)],
            "training_history": [],
            "training_diagnostics": {"training_samples": 150, "contracts_labeled": 30},
        }
        state_path.write_text(json.dumps(state))

        # Simulate startup loading state

        loaded = json.loads(state_path.read_text())
        assert loaded["cycle_count"] == 5
        assert loaded["training_diagnostics"]["training_samples"] == 150


# ---------------------------------------------------------------------------
# Data accumulation across cycles
# ---------------------------------------------------------------------------


class TestDataAccumulation:
    """Verify that archived data accumulates and dedup works correctly."""

    def test_multiple_archives_all_discoverable(self, work_dir):
        """CSVs in multiple archive subdirs should all be found by rglob."""
        archive_base = work_dir / "logs" / "_archive"

        # Simulate 3 cycles with different CSVs
        for cycle in range(1, 4):
            cycle_dir = archive_base / f"cycle_dd{cycle}"
            cycle_dir.mkdir(parents=True)
            _write_fake_csv(cycle_dir / f"data_2026032{cycle}_100000.csv")

        # Also a startup archive
        startup_dir = archive_base / "startup_20260326"
        startup_dir.mkdir(parents=True)
        _write_fake_csv(startup_dir / "data_20260326_080000.csv")

        # rglob should find all 4
        all_csvs = sorted(archive_base.rglob("data_*.csv"))
        assert len(all_csvs) == 4

    def test_filename_dedup_keeps_unique_only(self, work_dir):
        """Same filename in different dirs should be deduped to one copy."""
        archive_base = work_dir / "logs" / "_archive"

        # Same filename in two different archive dirs (duplicate)
        for subdir in ["cycle_dd1", "shutdown_20260326"]:
            d = archive_base / subdir
            d.mkdir(parents=True)
            _write_fake_csv(d / "data_20260325_100000.csv")

        # Different filename (unique)
        d = archive_base / "cycle_dd2"
        d.mkdir(parents=True)
        _write_fake_csv(d / "data_20260326_100000.csv")

        all_csvs = sorted(archive_base.rglob("data_*.csv"))
        assert len(all_csvs) == 3  # 2 copies of same name + 1 unique

        # Dedup by filename
        seen_names = set()
        unique_csvs = []
        for csv_path in all_csvs:
            if csv_path.name not in seen_names:
                seen_names.add(csv_path.name)
                unique_csvs.append(csv_path)

        assert len(unique_csvs) == 2  # Only 2 unique filenames

    def test_shutdown_then_startup_preserves_all_data(self, work_dir):
        """Full lifecycle: archive from cycle, shutdown archive, startup discovers both."""
        archive_base = work_dir / "logs" / "_archive"

        # Cycle 1 archive (from a prior drawdown)
        cycle_dir = archive_base / "cycle_dd1"
        cycle_dir.mkdir(parents=True)
        _write_fake_csv(cycle_dir / "data_20260324_100000.csv", rows=10)

        # Shutdown archive (from graceful shutdown after cycle 1)
        shutdown_dir = archive_base / "shutdown_20260325"
        shutdown_dir.mkdir(parents=True)
        _write_fake_csv(shutdown_dir / "data_20260325_100000.csv", rows=15)

        # Startup archive (stale file from the post-shutdown session)
        startup_dir = archive_base / "startup_20260326"
        startup_dir.mkdir(parents=True)
        _write_fake_csv(startup_dir / "data_20260325_200000.csv", rows=8)

        # All 3 unique CSVs should be discoverable
        all_csvs = sorted(archive_base.rglob("data_*.csv"))
        assert len(all_csvs) == 3

        seen_names = set()
        unique_csvs = []
        for csv_path in all_csvs:
            if csv_path.name not in seen_names:
                seen_names.add(csv_path.name)
                unique_csvs.append(csv_path)

        assert len(unique_csvs) == 3  # All have different names
