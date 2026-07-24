"""Phase 0 workstream D — state hygiene + risk observability (FR-0.6, FR-0.4).

Covers:
  1. Final partial close removes the emptied position (and the persisted
     state file never contains a qty<=0 "open" shell — the id-1582 bug).
  2. The expiration sweep drops qty<=0 shells without booking phantom
     settlements.
  3. Recency-windowed win-rate records: append/evict at 50, legacy-format
     migration (ignored, never crashes), persistence round-trip.
  4. INFO rejection logging with stable reason codes (the KELLY_ZERO silent
     kill-switch is now observable).
  5. scripts/purge_stale_state.py against copies of the REAL VM fixtures
     (review_2026_07_24/vm_data/data/), which contain the actual id-1582
     shell and the poisoned "ML BTC 15m": [30, 1048] entry.
"""

import importlib.util
import json
import logging
import os
import shutil
import sys
from collections import deque
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.core.risk_manager as rm_mod
from src.core.matching_engine import SimulatedExchange
from src.core.risk_manager import (
    MIN_WIN_SAMPLES,
    WIN_RATE_WINDOW,
    RejectReason,
    RiskManager,
)
from src.utils.logger import logger as mp_logger

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
VM_DATA = os.path.join(_REPO_ROOT, "review_2026_07_24", "vm_data", "data")


def _load_script(name: str):
    path = os.path.join(_SCRIPTS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


purge_mod = _load_script("purge_stale_state")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mp_caplog(caplog):
    """caplog wired to the project logger (it has propagate=False)."""
    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    yield caplog
    mp_logger.removeHandler(caplog.handler)


@pytest.fixture
def isolated_win_rates(tmp_path, monkeypatch):
    """Point WIN_RATES_PATH at a tmp file so tests never touch data/."""
    path = tmp_path / "strategy_win_rates.json"
    monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", str(path))
    return path


@pytest.fixture
def vm_fixtures(tmp_path):
    """Copies of the REAL VM state files (id-1582 shell + poisoned rates)."""
    state = tmp_path / "exchange_state.json"
    rates = tmp_path / "strategy_win_rates.json"
    shutil.copy2(os.path.join(VM_DATA, "exchange_state.json"), state)
    shutil.copy2(os.path.join(VM_DATA, "strategy_win_rates.json"), rates)
    return state, rates


def _make_rm(**kwargs):
    return RiskManager(starting_balance=100.0, persist_state=False, **kwargs)


# ---------------------------------------------------------------------------
# 1. FR-0.6: final partial close removes the emptied position
# ---------------------------------------------------------------------------


class TestFinalPartialCloseRemoval:
    def test_qty1_final_partial_close_removes_position(self, tmp_path):
        """The final partial close IS the close: position gone, on_close
        fired exactly once, journal (closed_trades) row written."""
        state_file = tmp_path / "state.json"
        closes = []
        ex = SimulatedExchange(on_close=closes.append, state_file=state_file)
        ex.open_position("KXHIGHNY-TEST-T75", "buy", 0.40, 1)
        pos = ex.positions[0]

        # +0.16 move hits the first target; exit_qty=max(1,0)=1 empties it.
        result = ex._check_profit_targets(pos, 0.56)

        assert result is True
        assert ex.positions == [], "emptied position must leave the open list"
        assert len(closes) == 1, "on_close must fire exactly once"
        assert len(ex.closed_trades) == 1, "journal row must be written"
        row = ex.closed_trades[0]
        assert row["reason"].startswith("PROFIT_TARGET")
        assert row["quantity"] == 1
        assert closes[0] is row

    def test_two_step_ladder_final_close_removes_position(self, tmp_path):
        state_file = tmp_path / "state.json"
        closes = []
        ex = SimulatedExchange(on_close=closes.append, state_file=state_file)
        ex.open_position("KXHIGHNY-TEST-T75", "buy", 0.40, 10)
        pos = ex.positions[0]

        # First target (+0.15, 50%): partial close, position stays.
        assert ex._check_profit_targets(pos, 0.56) is False
        assert len(ex.positions) == 1 and pos["quantity"] == 5
        assert len(closes) == 1

        # Second target (+0.30, 100%): final partial close empties it.
        assert ex._check_profit_targets(pos, 0.71) is True
        assert ex.positions == []
        assert len(closes) == 2
        assert len(ex.closed_trades) == 2
        assert sum(t["quantity"] for t in ex.closed_trades) == 10

    def test_state_file_never_persists_qty0_shell(self, tmp_path):
        """Regression for id-1582: the legacy order saved state BEFORE
        removing the emptied position, so the file kept a qty-0 'open'
        shell that a restart resurrected."""
        state_file = tmp_path / "state.json"
        ex = SimulatedExchange(on_close=lambda p: None, state_file=state_file)
        ex.open_position("KXHIGHNY-TEST-T75", "buy", 0.40, 1)
        ex._check_profit_targets(ex.positions[0], 0.56)

        with open(state_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert (
            data["positions"] == []
        ), "persisted state must not contain the emptied position"
        assert len(data["closed_trades"]) == 1

        # A restart from this file must come up clean.
        ex2 = SimulatedExchange(state_file=state_file)
        assert ex2.positions == []
        assert len(ex2.closed_trades) == 1


# ---------------------------------------------------------------------------
# 2. FR-0.6: expiration sweep drops qty<=0 shells
# ---------------------------------------------------------------------------


def _make_shell(expiration):
    return {
        "id": 1582,
        "symbol": "KXBTC15M-26JUL060945-45",
        "side": "buy",
        "entry_price": 0.44,
        "current_price": 0.5,
        "quantity": 0,
        "original_quantity": 50,
        "open_time": datetime.now() - timedelta(days=3),
        "pnl": 0.0,
        "stop_loss": 0.0,
        "trailing_rules": None,
        "trailing_activated": False,
        "expiration_time": expiration,
        "strategy_name": "Latency Arb",
        "last_market_price": 0.5,
        "contract_side": "YES",
        "strike": None,
        "profit_targets": [
            {"move": 0.15, "exit_pct": 0.50, "hit": True},
            {"move": 0.30, "exit_pct": 1.00, "hit": True},
        ],
        "entry_fee": 0.0,
        "is_maker": True,
    }


class TestExpirationSweepDropsShells:
    @pytest.mark.parametrize(
        "expiration",
        [None, datetime.now() - timedelta(days=17)],
        ids=["no-expiry", "expired"],
    )
    def test_sweep_drops_shell_without_phantom_settlement(self, tmp_path, expiration):
        state_file = tmp_path / "state.json"
        closes = []
        ex = SimulatedExchange(on_close=closes.append, state_file=state_file)
        ex.positions.append(_make_shell(expiration))

        ex.update_market("BTC", 118000.0)

        assert ex.positions == [], "sweep must drop the qty<=0 shell"
        assert closes == [], "dropping a shell is not an economic close"
        assert ex.closed_trades == [], "no phantom settlement row"

        with open(state_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["positions"] == [], "removal must be persisted"

    def test_sweep_keeps_live_positions(self, tmp_path):
        ex = SimulatedExchange(state_file=tmp_path / "state.json")
        ex.positions.append(_make_shell(None))
        ex.open_position("KXBTC15M-26JUL241200-00", "buy", 0.50, 5, strike=118000.0)

        ex.update_market("BTC", 118000.0)

        assert len(ex.positions) == 1
        assert ex.positions[0]["quantity"] == 5

    def test_sweep_drops_real_id_1582_shell_from_vm_state(self, vm_fixtures):
        """Load the actual VM exchange_state.json: the sweep must drop the
        real id-1582 shell and persist the cleaned state."""
        state_file, _ = vm_fixtures
        closes = []
        ex = SimulatedExchange(on_close=closes.append, state_file=state_file)
        assert len(ex.positions) == 1
        assert ex.positions[0]["id"] == 1582
        assert ex.positions[0]["quantity"] == 0
        n_closed = len(ex.closed_trades)

        ex.update_market("BTC", 118000.0)

        assert ex.positions == []
        assert closes == []
        assert len(ex.closed_trades) == n_closed, "closed history untouched"

        with open(state_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["positions"] == []
        assert len(data["closed_trades"]) == n_closed


# ---------------------------------------------------------------------------
# 3. FR-0.6: recency-windowed win-rate records
# ---------------------------------------------------------------------------


class TestWinRateWindow:
    def test_append_and_evict_at_window_size(self, isolated_win_rates):
        rm = _make_rm()
        for i in range(60):
            rm._on_trade_close(
                {
                    "symbol": f"T{i}",
                    "pnl": 5.0 if i % 2 == 0 else -5.0,
                    "strategy_name": "S",
                }
            )
        record = rm.strategy_win_records["S"]
        assert len(record) == WIN_RATE_WINDOW == 50
        expected = [1 if i % 2 == 0 else 0 for i in range(10, 60)]
        assert list(record) == expected, "oldest 10 outcomes must be evicted"

    def test_persist_roundtrip_new_schema(self, isolated_win_rates):
        rm = _make_rm()
        rm._persist_state = True
        rm._on_trade_close({"symbol": "A", "pnl": 5.0, "strategy_name": "S"})
        rm._on_trade_close({"symbol": "B", "pnl": -5.0, "strategy_name": "S"})

        with open(isolated_win_rates, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["S"]["window"] == [1, 0]
        assert isinstance(data["S"]["updated"], str)

        rm2 = _make_rm()
        assert list(rm2.strategy_win_records["S"]) == [1, 0]

    def test_legacy_format_ignored_never_crashes(self, isolated_win_rates):
        """The poisoned crypto-era file loads as EMPTY history: the pivot
        deliberately resets, so 'ML BTC 15m' sizes from the 0.5 prior
        instead of the 2.9% deadlock."""
        isolated_win_rates.write_text(
            json.dumps({"ML BTC 15m": [30, 1048], "Latency Arb": [9, 9]}),
            encoding="utf-8",
        )
        rm = _make_rm()
        assert rm.strategy_win_records == {}

        poisoned = rm.calculate_kelly_size(0.8, 0.5, "ML BTC 15m")
        unknown = rm.calculate_kelly_size(0.8, 0.5, "NeverSeenStrategy")
        assert poisoned == unknown, "legacy entry must act exactly like absent"
        assert poisoned > 0, "the Kelly deadlock must be broken by the reset"

    def test_real_poisoned_vm_file_loads_clean(
        self, tmp_path, monkeypatch, vm_fixtures
    ):
        _, rates = vm_fixtures
        monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", str(rates))
        rm = _make_rm()
        assert rm.strategy_win_records == {}
        assert rm.calculate_kelly_size(0.8, 0.5, "ML BTC 15m") > 0

    def test_mixed_format_keeps_only_new_entries(self, isolated_win_rates):
        isolated_win_rates.write_text(
            json.dumps(
                {
                    "Old Strat": [10, 20],
                    "New Strat": {"window": [1, 0, 1], "updated": "2026-07-24"},
                }
            ),
            encoding="utf-8",
        )
        rm = _make_rm()
        assert set(rm.strategy_win_records) == {"New Strat"}
        assert list(rm.strategy_win_records["New Strat"]) == [1, 0, 1]

    def test_corrupt_file_never_crashes(self, isolated_win_rates):
        isolated_win_rates.write_text("not-json{{{", encoding="utf-8")
        rm = _make_rm()
        assert rm.strategy_win_records == {}

    def test_sample_size_gate_uses_window_length(self, isolated_win_rates):
        rm = _make_rm()
        # 19 losses: below MIN_WIN_SAMPLES -> neutral prior, sizes like unknown.
        rm.strategy_win_records["Cold"] = deque(
            [0] * (MIN_WIN_SAMPLES - 1), maxlen=WIN_RATE_WINDOW
        )
        assert rm.calculate_kelly_size(0.8, 0.5, "Cold") == (
            rm.calculate_kelly_size(0.8, 0.5, "Unknown")
        )
        # 20 losses: gate opens, 0% WR zeroes the size.
        rm.strategy_win_records["Cold"] = deque(
            [0] * MIN_WIN_SAMPLES, maxlen=WIN_RATE_WINDOW
        )
        assert rm.calculate_kelly_size(0.8, 0.5, "Cold") == 0


# ---------------------------------------------------------------------------
# 4. FR-0.4: INFO rejection logging with stable reason codes
# ---------------------------------------------------------------------------


class TestRejectionLogging:
    def _reject_lines(self, caplog):
        return [
            r.getMessage() for r in caplog.records if "[Risk] REJECT" in r.getMessage()
        ]

    def test_kelly_zero_is_observable_at_info(self, isolated_win_rates, mp_caplog):
        """The silent kill-switch: a zero-sized signal must produce an INFO
        line with reason=KELLY_ZERO."""
        rm = _make_rm()
        rm.strategy_win_records["Dead"] = deque(
            [0] * MIN_WIN_SAMPLES, maxlen=WIN_RATE_WINDOW
        )
        qty = rm.calculate_kelly_size(0.8, 0.5, "Dead", symbol="KXHIGHNY-X")
        assert qty == 0
        lines = self._reject_lines(mp_caplog)
        assert len(lines) == 1, "exactly one rejection line per decision"
        line = lines[0]
        assert "reason=KELLY_ZERO" in line
        assert "strategy=Dead" in line
        assert "symbol=KXHIGHNY-X" in line
        assert "p=" in line and "price=" in line
        rec = [r for r in mp_caplog.records if "[Risk] REJECT" in r.getMessage()]
        assert rec[0].levelno == logging.INFO

    def test_kelly_zero_price_out_of_range(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        assert rm.calculate_kelly_size(0.8, 0.0, "S") == 0
        assert "reason=KELLY_ZERO" in mp_caplog.text
        assert "PRICE_OUT_OF_RANGE" in mp_caplog.text

    def test_insufficient_balance(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        assert rm.check_order(500.0, strategy_name="S", symbol="SYM-1") is False
        assert "reason=INSUFFICIENT_BALANCE" in mp_caplog.text

    def test_trade_interval(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        rm.last_trade_time = datetime.now()
        assert rm.check_order(1.0, strategy_name="S", symbol="SYM-1") is False
        assert "reason=TRADE_INTERVAL" in mp_caplog.text

    def test_cooldown_symbol_exact_and_series(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        rm.loss_cooldown["SYM-1"] = datetime.now() + timedelta(seconds=300)
        assert rm.check_order(1.0, strategy_name="S", symbol="SYM-1") is False
        assert "reason=COOLDOWN_SYMBOL" in mp_caplog.text
        assert "scope=exact" in mp_caplog.text

        mp_caplog.clear()
        rm2 = _make_rm()
        rm2.loss_cooldown["KXHIGHNY"] = datetime.now() + timedelta(seconds=300)
        assert (
            rm2.check_order(1.0, strategy_name="S", symbol="KXHIGHNY-25JUL-B86.5")
            is False
        )
        assert "reason=COOLDOWN_SYMBOL" in mp_caplog.text
        assert "scope=series" in mp_caplog.text

    def test_cooldown_strategy(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        rm.strategy_cooldown["S"] = datetime.now() + timedelta(seconds=60)
        assert rm.check_order(1.0, strategy_name="S", symbol="SYM-1") is False
        assert "reason=COOLDOWN_STRATEGY" in mp_caplog.text

    def test_daily_drawdown(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        rm.daily_pnl = -60.0  # limit is 50% of $100
        assert rm.check_order(1.0, strategy_name="S", symbol="SYM-1") is False
        assert "reason=DAILY_DRAWDOWN" in mp_caplog.text
        assert rm.drawdown_kill_triggered is True

    def test_strategy_drawdown(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        rm.strategy_peak_pnl["Whale"] = 300.0
        rm.strategy_pnl["Whale"] = 250.0  # $50 dd > 10% of $400 base
        assert rm.check_order(4.0, strategy_name="Whale", symbol="SYM-1") is False
        assert "reason=STRATEGY_DRAWDOWN" in mp_caplog.text

    def test_max_exposure(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        rm.exchange.open_position("TESTSYM-1", "buy", 0.50, 90)  # $45 exposure
        assert rm.check_order(6.0, strategy_name="S", symbol="SYM-1") is False
        assert "reason=MAX_EXPOSURE" in mp_caplog.text

    def test_accepted_order_logs_no_reject(self, isolated_win_rates, mp_caplog):
        rm = _make_rm()
        assert rm.check_order(1.0, strategy_name="S", symbol="SYM-1") is True
        assert self._reject_lines(mp_caplog) == []

    def test_reason_codes_are_complete(self):
        """The FR-0.4 minimum vocabulary must exist as stable constants."""
        required = [
            "KELLY_ZERO",
            "EV_GATE",
            "MAX_EXPOSURE",
            "DAILY_DRAWDOWN",
            "STRATEGY_DRAWDOWN",
            "COOLDOWN_SYMBOL",
            "COOLDOWN_STRATEGY",
            "TRADE_INTERVAL",
            "INSUFFICIENT_BALANCE",
        ]
        for code in required:
            assert getattr(RejectReason, code) == code


# ---------------------------------------------------------------------------
# 5. scripts/purge_stale_state.py against the REAL VM fixtures
# ---------------------------------------------------------------------------


class TestPurgeStaleState:
    def _run(self, state, rates):
        return purge_mod.main(["--state-file", str(state), "--win-rates", str(rates)])

    def test_purge_real_vm_fixtures(self, vm_fixtures, capsys):
        state, rates = vm_fixtures
        with open(state, "r", encoding="utf-8") as fh:
            before = json.load(fh)
        n_closed = len(before["closed_trades"])

        rc = self._run(state, rates)
        out = capsys.readouterr().out

        assert rc == 0
        assert "1582" in out, "the removed shell must be reported"
        assert "ML BTC 15m" in out, "dropped legacy entries must be reported"

        with open(state, "r", encoding="utf-8") as fh:
            after = json.load(fh)
        assert after["positions"] == [], "id-1582 shell must be gone"
        assert len(after["closed_trades"]) == n_closed
        assert after["schema_version"] == before["schema_version"]
        assert after["realized_pnl"] == before["realized_pnl"]

        with open(rates, "r", encoding="utf-8") as fh:
            new_rates = json.load(fh)
        assert new_rates == {}, "poisoned file resets to the new empty format"

        archive = rates.parent / "strategy_win_rates.legacy.json"
        assert archive.exists(), "legacy file must be archived before reset"
        with open(archive, "r", encoding="utf-8") as fh:
            archived = json.load(fh)
        assert archived["ML BTC 15m"] == [30, 1048]

    def test_purge_is_idempotent(self, vm_fixtures, capsys):
        state, rates = vm_fixtures
        assert self._run(state, rates) == 0
        capsys.readouterr()

        rc = self._run(state, rates)
        out = capsys.readouterr().out
        assert rc == 0
        assert "already clean" in out
        archives = list(rates.parent.glob("*.legacy*"))
        assert len(archives) == 1, "second run must not create a new archive"

    def test_purge_missing_files_is_safe(self, tmp_path, capsys):
        state = tmp_path / "nope" / "exchange_state.json"
        rates = tmp_path / "nope" / "strategy_win_rates.json"
        rc = self._run(state, rates)
        out = capsys.readouterr().out
        assert rc == 0
        assert "not found" in out
        with open(rates, "r", encoding="utf-8") as fh:
            assert json.load(fh) == {}

    def test_purged_state_loads_clean_in_engine(self, vm_fixtures):
        """End-to-end: after the purge, the engine restarts with zero open
        positions and the full closed history intact (exit criterion 7)."""
        state, rates = vm_fixtures
        assert self._run(state, rates) == 0
        ex = SimulatedExchange(state_file=state)
        assert ex.positions == []
        assert len(ex.closed_trades) == 1583


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
