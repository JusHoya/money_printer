"""Tests for the immutable cumulative PnL ledger (Task F, 2026-06-03).

The exchange keeps three monotonic accumulators —
``cumulative_realized_pnl``, ``cumulative_fees_paid``,
``cumulative_entry_fees`` — that are incremented on every fill/close but are
NEVER zeroed by ``reset_stats()``. ``get_cumulative_net_pnl()`` exposes the
true lifetime net (net of ALL fees), and the accumulator must reconcile with
``sum(closed_trades.pnl)``. ``closed_trades`` is the source of truth.
"""

import json
from pathlib import Path

import pytest

from src.core.matching_engine import SimulatedExchange


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _open_and_settle(ex, symbol, entry, qty, win):
    """Open a binary YES position and settle it via EXPIRATION.

    ``win`` True -> strike below spot (YES settles 1.00); False -> 0.00.
    Symbol format ``KXBTC15M-<period>-<strike>`` with a strike of 45 (parsed).
    """
    ex.open_position(
        symbol,
        "buy",
        entry,
        qty,
        strategy_name="ML BTC 15m",
        expiration_time="2020-01-01T00:00:00+00:00",  # already expired
    )
    spot = 100.0 if win else 0.0  # vs parsed strike of 45
    ex.update_market("BTC", spot)


# ----------------------------------------------------------------------
# Core invariants
# ----------------------------------------------------------------------


def test_cumulative_fields_exist_and_start_zero():
    ex = SimulatedExchange()
    assert ex.cumulative_realized_pnl == 0.0
    assert ex.cumulative_fees_paid == 0.0
    assert ex.cumulative_entry_fees == 0.0
    assert ex.get_cumulative_net_pnl() == 0.0


def test_cumulative_survives_reset_stats():
    """reset_stats() must zero realized_pnl but NEVER the cumulative ledger."""
    ex = SimulatedExchange()
    _open_and_settle(ex, "KXBTC15M-26JAN010000-45", 0.40, 10, win=True)

    assert len(ex.closed_trades) == 1
    net_before = ex.get_cumulative_net_pnl()
    cum_realized_before = ex.cumulative_realized_pnl
    cum_fees_before = ex.cumulative_fees_paid
    cum_entry_before = ex.cumulative_entry_fees
    assert cum_realized_before != 0.0  # sanity: something happened

    # Simulate a balance sync.
    ex.realized_pnl = 123.45
    ex.reset_stats()

    assert ex.realized_pnl == 0.0  # fragment zeroed
    assert ex.cumulative_realized_pnl == cum_realized_before
    assert ex.cumulative_fees_paid == cum_fees_before
    assert ex.cumulative_entry_fees == cum_entry_before
    assert ex.get_cumulative_net_pnl() == net_before


def test_cumulative_reconciles_with_closed_trades():
    """cumulative_realized_pnl == sum(closed_trades.pnl) after many trades."""
    ex = SimulatedExchange()
    for i in range(10):
        win = i % 2 == 0
        _open_and_settle(ex, f"KXBTC15M-26JAN01{i:04d}-45", 0.30, 10, win=win)

    assert len(ex.closed_trades) == 10
    sum_pnl = sum(t["pnl"] for t in ex.closed_trades)
    assert ex.cumulative_realized_pnl == pytest.approx(sum_pnl, abs=1e-9)

    # get_cumulative_net_pnl is net of entry fees too.
    sum_entry = sum(t.get("entry_fee", 0.0) for t in ex.closed_trades)
    assert ex.get_cumulative_net_pnl() == pytest.approx(sum_pnl - sum_entry, abs=1e-9)


def test_reset_stats_then_more_trades_keeps_reconciliation():
    """A mid-life reset_stats must not break cumulative reconciliation."""
    ex = SimulatedExchange()
    for i in range(5):
        _open_and_settle(ex, f"KXBTC15M-26FEB01{i:04d}-45", 0.30, 10, win=(i % 2 == 0))

    ex.realized_pnl = 999.0
    ex.reset_stats()  # fragment wiped, cumulative untouched

    for i in range(5):
        _open_and_settle(ex, f"KXBTC15M-26FEB02{i:04d}-45", 0.30, 10, win=(i % 2 == 1))

    sum_pnl = sum(t["pnl"] for t in ex.closed_trades)
    assert ex.cumulative_realized_pnl == pytest.approx(sum_pnl, abs=1e-9)


def test_entry_fee_counted_once_not_per_partial_close():
    """cumulative_entry_fees increments once at open, not per partial close.

    Opened as a taker: PRD Phase 2 corrected the standard-series maker
    multiplier to zero, so a maker open books $0.00 and there would be no fee
    to count once. The invariant under test is unchanged.
    """
    ex = SimulatedExchange()
    # Open WITHOUT disabling profit targets so partial closes can occur.
    ex.open_position(
        "KXHIGHNY-26JAN01-T45",
        "buy",
        0.40,
        10,
        strategy_name="X",
        is_maker=False,
    )
    entry_after_open = ex.cumulative_entry_fees
    assert entry_after_open > 0.0

    # Drive the price up to trigger the profit-target ladder (partial closes).
    # Spot well above the parsed strike of 45 pushes the estimate toward 1.00.
    for _ in range(3):
        ex.update_market("NY", 80.0)

    # Even after partial/full closes, entry fees counted exactly once.
    assert ex.cumulative_entry_fees == pytest.approx(entry_after_open, abs=1e-9)


# ----------------------------------------------------------------------
# Persistence + reconciliation on load
# ----------------------------------------------------------------------


def test_cumulative_persists_and_reloads(tmp_path):
    state_file = tmp_path / "exchange_state.json"
    ex = SimulatedExchange(state_file=state_file)
    for i in range(6):
        _open_and_settle(ex, f"KXBTC15M-26MAR01{i:04d}-45", 0.30, 10, win=(i % 2 == 0))

    net_before = ex.get_cumulative_net_pnl()
    cum_before = ex.cumulative_realized_pnl
    assert state_file.exists()

    # Reload from disk into a fresh exchange.
    ex2 = SimulatedExchange(state_file=state_file)
    assert ex2.cumulative_realized_pnl == pytest.approx(cum_before, abs=1e-9)
    assert ex2.get_cumulative_net_pnl() == pytest.approx(net_before, abs=1e-9)
    # Reconciles with the restored closed_trades.
    sum_pnl = sum(t["pnl"] for t in ex2.closed_trades)
    assert ex2.cumulative_realized_pnl == pytest.approx(sum_pnl, abs=1e-9)


def test_backfill_from_legacy_state_without_cumulative_keys(tmp_path):
    """A pre-Task-F state file (no cumulative_* keys) is backfilled on load."""
    state_file = tmp_path / "exchange_state.json"
    # First create a normal state with trades.
    ex = SimulatedExchange(state_file=state_file)
    for i in range(8):
        _open_and_settle(ex, f"KXBTC15M-26APR01{i:04d}-45", 0.30, 10, win=(i % 3 == 0))
    expected_net = ex.get_cumulative_net_pnl()

    # Strip the cumulative_* keys to simulate a legacy file.
    data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    for k in (
        "cumulative_realized_pnl",
        "cumulative_fees_paid",
        "cumulative_entry_fees",
    ):
        data.pop(k, None)
    Path(state_file).write_text(json.dumps(data), encoding="utf-8")

    # Reload — backfill should reconstruct the ledger from closed_trades.
    ex2 = SimulatedExchange(state_file=state_file)
    sum_pnl = sum(t["pnl"] for t in ex2.closed_trades)
    assert ex2.cumulative_realized_pnl == pytest.approx(sum_pnl, abs=1e-9)
    assert ex2.get_cumulative_net_pnl() == pytest.approx(expected_net, abs=1e-9)


def test_reconciliation_warns_on_divergence(tmp_path):
    """If the scalar drifts from closed_trades, load logs a warning."""
    state_file = tmp_path / "exchange_state.json"
    ex = SimulatedExchange(state_file=state_file)
    for i in range(4):
        _open_and_settle(ex, f"KXBTC15M-26MAY01{i:04d}-45", 0.30, 10, win=True)

    # Corrupt only the scalar in the persisted file (closed_trades stays truth).
    data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    data["cumulative_realized_pnl"] = data["cumulative_realized_pnl"] + 50.0
    Path(state_file).write_text(json.dumps(data), encoding="utf-8")

    # The project logger has propagate=False, so attach a capture handler
    # directly to it for the duration of the load.
    import logging

    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    proj_logger = logging.getLogger("MoneyPrinter")
    handler = _Capture(level=logging.WARNING)
    proj_logger.addHandler(handler)
    try:
        SimulatedExchange(state_file=state_file)
    finally:
        proj_logger.removeHandler(handler)

    assert any("divergence" in msg.lower() for msg in captured)


def test_reconciles_against_review_ledger():
    """Sanity-check against the real 2026-06-03 review ledger if present."""
    candidates = [
        Path(__file__).resolve().parent.parent / "data" / "exchange_state.json",
        Path(__file__).resolve().parent.parent
        / "review_2026_06_03"
        / "exchange_state.json",
    ]
    state = next((p for p in candidates if p.exists()), None)
    if state is None:
        pytest.skip("no exchange_state.json available")

    ex = SimulatedExchange(state_file=state)
    sum_pnl = sum(float(t.get("pnl", 0.0)) for t in ex.closed_trades)
    assert ex.cumulative_realized_pnl == pytest.approx(sum_pnl, abs=0.01)
    sum_entry = sum(float(t.get("entry_fee", 0.0)) for t in ex.closed_trades)
    assert ex.get_cumulative_net_pnl() == pytest.approx(sum_pnl - sum_entry, abs=0.01)
