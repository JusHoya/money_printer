"""Sprint 8 – Task 8.5: SimulatedExchange disk persistence tests.

All writes are redirected to pytest's tmp_path so the real data/ dir is
never touched.  The state_file init arg is used for path injection.
"""

import json
from pathlib import Path
from datetime import datetime

import pytest

from src.core.matching_engine import SimulatedExchange, _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_exchange(tmp_path: Path, **kwargs) -> SimulatedExchange:
    """Create a SimulatedExchange whose state file lives in tmp_path."""
    state_file = tmp_path / "exchange_state.json"
    return SimulatedExchange(state_file=state_file, **kwargs)


def open_dummy_position(
    exc: SimulatedExchange, symbol: str = "KXHIGH-NY-50", qty: int = 5
):
    """Open a minimal position that avoids real fee-calculator network calls."""
    exc.open_position(
        symbol=symbol,
        side="buy",
        entry_price=0.50,
        quantity=qty,
        strategy_name="test_strategy",
    )


def close_all(exc: SimulatedExchange):
    """Force-close all open positions via _close_position."""
    for pos in list(exc.positions):
        exc._close_position(pos, final_spot_price=0.60, reason="TEST_CLOSE")


# ---------------------------------------------------------------------------
# Test 1: state file is written after a close
# ---------------------------------------------------------------------------


def test_exchange_serializes_on_position_close(tmp_path):
    exc = make_exchange(tmp_path)
    state_file = exc._state_file

    assert not state_file.exists(), "No state file should exist before any activity"

    open_dummy_position(exc)
    assert (
        not state_file.exists()
    ), "State file should NOT be written on open (only on close)"

    close_all(exc)

    assert state_file.exists(), "State file must be created after closing a position"

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["schema_version"] == _SCHEMA_VERSION, "schema_version must be 1"
    assert "saved_at" in data, "saved_at timestamp must be present"

    # The closed trade should be recorded in closed_trades
    assert (
        len(data["closed_trades"]) >= 1
    ), "closed_trades must contain the closed position"

    # _next_id equivalent: the position id counter should be > 0
    # (positions uses len(positions)+len(closed_trades)+1 for new IDs)
    closed = data["closed_trades"][0]
    assert closed["id"] >= 1, "position id must be positive"


# ---------------------------------------------------------------------------
# Test 2: state survives across restart (open positions reload)
# ---------------------------------------------------------------------------


def test_exchange_state_persists_across_restart(tmp_path):
    exc1 = make_exchange(tmp_path)
    state_file = exc1._state_file

    # Open two positions, do NOT close — we want them to survive as open
    open_dummy_position(exc1, symbol="KXHIGH-NY-55", qty=3)
    open_dummy_position(exc1, symbol="KXHIGH-NY-60", qty=7)

    # Manually save state (simulates a graceful close or restart checkpoint)
    exc1._save_state()

    assert state_file.exists()

    # Simulate restart: new instance pointing at same state file
    exc2 = SimulatedExchange(state_file=state_file)

    assert (
        len(exc2.positions) == len(exc1.positions)
    ), f"Reloaded positions count {len(exc2.positions)} != original {len(exc1.positions)}"

    # Verify position IDs round-trip correctly
    orig_ids = sorted(p["id"] for p in exc1.positions)
    restored_ids = sorted(p["id"] for p in exc2.positions)
    assert orig_ids == restored_ids, "Position IDs must survive restart"

    # Verify scalar counters persisted
    assert exc2.realized_pnl == pytest.approx(exc1.realized_pnl, abs=1e-6)
    assert exc2.total_fees_paid == pytest.approx(exc1.total_fees_paid, abs=1e-6)
    assert exc2.maker_fills == exc1.maker_fills
    assert exc2.taker_fills == exc1.taker_fills


# ---------------------------------------------------------------------------
# Test 3: corrupt state file does not crash; gets renamed
# ---------------------------------------------------------------------------


def test_corrupt_state_file_does_not_crash(tmp_path):
    state_file = tmp_path / "exchange_state.json"
    state_file.write_text("this is {NOT valid json!!!", encoding="utf-8")

    # Must not raise
    exc = SimulatedExchange(state_file=state_file)

    # Fresh state
    assert exc.positions == []
    assert exc.realized_pnl == 0.0

    # Original file should have been renamed
    assert not state_file.exists(), "Corrupt file must be removed/renamed"

    corrupt_files = list(tmp_path.glob("*.corrupt-*"))
    assert (
        len(corrupt_files) == 1
    ), f"Expected exactly one *.corrupt-* backup, found: {[f.name for f in corrupt_files]}"


def test_wrong_schema_version_does_not_crash(tmp_path):
    """A state file with schema_version != 1 is treated as corrupt."""
    state_file = tmp_path / "exchange_state.json"
    payload = {
        "schema_version": 99,
        "saved_at": datetime.now().isoformat(),
        "positions": [],
        "closed_trades": [],
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_fees_paid": 0.0,
        "maker_fills": 0,
        "taker_fills": 0,
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    exc = SimulatedExchange(state_file=state_file)
    assert exc.positions == []

    corrupt_files = list(tmp_path.glob("*.corrupt-*"))
    assert len(corrupt_files) == 1


# ---------------------------------------------------------------------------
# Test 4: atomic write leaves no *.tmp files after a close
# ---------------------------------------------------------------------------


def test_atomic_write_never_leaves_partial_file(tmp_path):
    exc = make_exchange(tmp_path)

    open_dummy_position(exc)
    close_all(exc)

    # No leftover .tmp files in the data dir
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert (
        tmp_files == []
    ), f"Atomic write left behind temp files: {[f.name for f in tmp_files]}"

    # State file must exist and be valid JSON
    state_file = exc._state_file
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["schema_version"] == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Test 5: datetime round-trip in position fields
# ---------------------------------------------------------------------------


def test_datetime_fields_survive_serialization(tmp_path):
    """open_time and expiration_time must round-trip through JSON as datetimes."""
    exc1 = make_exchange(tmp_path)
    open_dummy_position(exc1, symbol="KXHIGH-NY-55", qty=2)
    exc1._save_state()

    exc2 = SimulatedExchange(state_file=exc1._state_file)
    pos = exc2.positions[0]

    assert isinstance(
        pos["open_time"], datetime
    ), f"open_time should be datetime after restore, got {type(pos['open_time'])}"
    # expiration_time is None for our dummy — just verify it doesn't blow up
    assert pos["expiration_time"] is None or isinstance(
        pos["expiration_time"], datetime
    )
