"""Phase 0 workstream B — logging & observability (FR-0.3, FR-0.4, FR-0.5).

Covers:
1. Archive sweeps (startup + cycle) never remove the process's own active
   money_printer_*.log, the current dashboard session files, or watchdog
   state/log files (FR-0.3).
2. configure_root_logging() routes module-level loggers into the shared log
   file with no duplicate lines for the "MoneyPrinter" logger (FR-0.3).
3. _process_signals: every emitted signal logs at INFO; every skip/rejection
   produces exactly ONE INFO rejection line with a stable reason code, with
   no double-logging of reasons the RiskManager logs itself (FR-0.4).
4. Cycle rollover creates the new session log before the old ones are
   archived, and the cycle summary lists every bot with
   TRADING / FEED-ONLY / DISABLED status (FR-0.4, FR-0.5).
"""

import logging
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_dashboard as rd
import src.core.risk_manager as rm_mod
from scripts.run_dashboard import OrchestratorEngine
from src.bots.mixins import (
    REASON_MISSING_LIMIT_PRICE,
    REASON_WEATHER_SLOT_FULL,
    SignalProcessorMixin,
)
from src.core.interfaces import TradeSignal
from src.core.risk_manager import RiskManager
from src.utils.logger import (
    configure_root_logging,
    get_active_log_path,
    logger as mp_logger,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mp_records():
    """Capture every record emitted through the shared MoneyPrinter logger.

    caplog cannot see these records (the logger has propagate=False), so a
    collector handler is attached directly.
    """
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    mp_logger.addHandler(handler)
    yield records
    mp_logger.removeHandler(handler)


def _reject_lines(records):
    return [r for r in records if "[Risk] REJECT" in r.getMessage()]


def _info_lines(records, needle):
    return [
        r for r in records if r.levelno == logging.INFO and needle in r.getMessage()
    ]


class _DashboardStub:
    """Stands in for the terminal/web dashboard in signal-processing tests."""

    def __init__(self):
        self.logged = []
        self.signals = []
        self.alerts = []
        self.strategy_stats = {}

    def log(self, msg):
        self.logged.append(msg)

    def alert(self, msg):
        self.alerts.append(msg)

    def record_signal(self, sig, status="", strategy_name=""):
        self.signals.append((sig.symbol, status, strategy_name))


class _RiskStub:
    """Minimal risk manager for mixins tests. It logs nothing itself, which
    lets the tests prove the mixins layer never emits rejection lines for
    reasons the RiskManager owns."""

    def __init__(self, kelly_qty=5, safe=True, positions=None):
        self.kelly_qty = kelly_qty
        self.safe = safe
        self.last_trade_time = None
        self.loss_cooldown = {}
        self.exchange = SimpleNamespace(positions=positions or [])
        self.executions = []
        self.check_order_calls = []

    def calculate_kelly_size(self, confidence, price, strategy_name="", symbol=""):
        return self.kelly_qty

    def check_order(self, cost, **kwargs):
        self.check_order_calls.append((cost, kwargs))
        return self.safe

    def record_execution(self, *args, **kwargs):
        self.executions.append((args, kwargs))


class _Host(SignalProcessorMixin):
    pass


def _signal(symbol="KXGASMAXUS-26AUG", side="buy", qty=1, price=0.40, conf=0.90):
    return TradeSignal(
        symbol=symbol,
        side=side,
        quantity=qty,
        limit_price=price,
        confidence=conf,
    )


# ---------------------------------------------------------------------------
# 1. FR-0.3 — preserved patterns + startup sweep
# ---------------------------------------------------------------------------


class TestPreservedLogPatterns:
    @pytest.mark.parametrize(
        "filename",
        [
            "host_watchdog.log",
            "host_watchdog.ts",
            "host_watchdog_missing_since.ts",
            "watchdog_cron_endpoint_fail.ts",
            "watchdog_last_alert.ts",
            ".sim_balance",
            "watchdog_alerts.log",
            "watchdog_20260724.log",
            "watchdog.log",
        ],
    )
    def test_watchdog_state_is_preserved(self, filename):
        assert OrchestratorEngine._is_preserved_log(filename) is True

    @pytest.mark.parametrize(
        "filename",
        [
            "money_printer_20260724_120000.log",
            "session_20260724_120000.log",
            "data_20260724_120000.csv",
            "portfolio_20260724_120000.csv",
        ],
    )
    def test_session_files_are_not_preserved_by_pattern(self, filename):
        assert OrchestratorEngine._is_preserved_log(filename) is False


def _touch(path: Path, content="x"):
    path.write_text(content, encoding="utf-8")
    return path


WATCHDOG_FILES = [
    "host_watchdog.log",
    "host_watchdog.ts",
    "watchdog_cron_endpoint_fail.ts",
    "watchdog_last_alert.ts",
    ".sim_balance",
    "watchdog_alerts.log",
]


@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
    """A temp cwd with a populated logs/ dir and a stub engine whose
    dashboard session files and active process log live inside it."""
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()

    active_log = _touch(logs / "money_printer_ACTIVE.log", "active process log")
    monkeypatch.setattr(rd, "get_active_log_path", lambda: str(active_log))

    dash = SimpleNamespace(
        session_log_path=str(_touch(logs / "session_NEW.log")),
        data_log_path=str(_touch(logs / "data_NEW.csv")),
        portfolio_log_path=str(_touch(logs / "portfolio_NEW.csv")),
    )

    stale = [
        _touch(logs / "money_printer_20260101_000000.log"),
        _touch(logs / "session_OLD.log"),
        _touch(logs / "data_OLD.csv"),
    ]
    watchdog = [_touch(logs / name) for name in WATCHDOG_FILES]

    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.dashboard = dash
    engine._training_diagnostics = {}

    return SimpleNamespace(
        logs=logs,
        engine=engine,
        active_log=active_log,
        dash=dash,
        stale=stale,
        watchdog=watchdog,
    )


class TestStartupArchiveSweep:
    def test_active_and_watchdog_files_survive_stale_files_archived(self, sweep_env):
        sweep_env.engine._startup_archive()

        # The process's own log and the current session files survive.
        assert sweep_env.active_log.exists()
        assert Path(sweep_env.dash.session_log_path).exists()
        assert Path(sweep_env.dash.data_log_path).exists()
        assert Path(sweep_env.dash.portfolio_log_path).exists()

        # Watchdog state files survive.
        for f in sweep_env.watchdog:
            assert f.exists(), f"watchdog file removed by startup sweep: {f.name}"

        # Stale files are gone from logs/ but copied into the archive.
        archive_dirs = list((sweep_env.logs / "_archive").glob("startup_*"))
        assert len(archive_dirs) == 1
        for f in sweep_env.stale:
            assert not f.exists(), f"stale file not removed: {f.name}"
            assert (archive_dirs[0] / f.name).exists()


class TestCycleSweep:
    def test_cycle_clean_protects_active_log_and_watchdog_state(self, sweep_env):
        archive_dir = sweep_env.logs / "_archive" / "cycle_test_dd1"
        archive_dir.mkdir(parents=True)

        sweep_env.engine._cycle_archive_and_clean(str(archive_dir))

        assert sweep_env.active_log.exists()
        assert Path(sweep_env.dash.session_log_path).exists()
        for f in sweep_env.watchdog:
            assert f.exists(), f"watchdog file removed by cycle sweep: {f.name}"

        # Non-protected session files were archived and removed.
        for f in sweep_env.stale:
            assert not f.exists()
            assert (archive_dir / f.name).exists()


# ---------------------------------------------------------------------------
# 2. FR-0.3 — root logger configuration
# ---------------------------------------------------------------------------


class TestRootLoggerConfig:
    def _read_active_log(self):
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        for handler in mp_logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
        path = get_active_log_path()
        assert path is not None, "MoneyPrinter logger lost its file handler"
        return Path(path).read_text(encoding="utf-8")

    def test_module_logger_reaches_shared_file(self):
        configure_root_logging()
        marker = f"phase0-module-marker-{uuid.uuid4().hex}"
        logging.getLogger("src.strategies.some_module").info(marker)
        assert self._read_active_log().count(marker) == 1

    def test_moneyprinter_logger_lines_are_not_duplicated(self):
        configure_root_logging()
        marker = f"phase0-mp-marker-{uuid.uuid4().hex}"
        mp_logger.info(marker)
        assert self._read_active_log().count(marker) == 1

    def test_idempotent_no_duplicate_handlers_or_lines(self):
        configure_root_logging()
        configure_root_logging()
        root = logging.getLogger()
        # The MoneyPrinter file handler must appear on root EXACTLY once
        # (foreign FileHandlers from the test harness may also be present).
        shared = [h for h in mp_logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(shared) == 1
        # The instance is SHARED with the MoneyPrinter logger, not a copy —
        # two handles on the same file would interleave/duplicate writes.
        assert root.handlers.count(shared[0]) == 1

        marker = f"phase0-idem-marker-{uuid.uuid4().hex}"
        logging.getLogger("src.data.some_provider").info(marker)
        assert self._read_active_log().count(marker) == 1

    def test_root_level_is_info(self):
        configure_root_logging()
        assert logging.getLogger().getEffectiveLevel() <= logging.INFO


# ---------------------------------------------------------------------------
# 3. FR-0.4 — signal emission / single-rejection-line logging in mixins
# ---------------------------------------------------------------------------


class TestSignalProcessingLogging:
    def test_executed_signal_logs_emit_and_executed_no_reject(self, mp_records):
        host, dash = _Host(), _DashboardStub()
        risk = _RiskStub(kelly_qty=5, safe=True)
        mp_records.clear()

        traded = host._process_signals([_signal()], "TestStrat", risk, dash)

        assert traded is True
        assert len(_info_lines(mp_records, "[Signal] EMIT")) == 1
        assert len(_info_lines(mp_records, "[Signal] EXECUTED")) == 1
        assert _reject_lines(mp_records) == []
        assert len(risk.executions) == 1
        # EMIT line carries strategy, symbol, side, price, qty, confidence
        emit = _info_lines(mp_records, "[Signal] EMIT")[0].getMessage()
        for part in (
            "strategy=TestStrat",
            "symbol=KXGASMAXUS-26AUG",
            "side=buy",
            "price=0.4",
            "qty=",
            "confidence=0.900",
        ):
            assert part in emit, f"missing {part!r} in EMIT line: {emit}"

    def test_ev_gate_rejection_logs_exactly_one_line(self, mp_records):
        host, dash = _Host(), _DashboardStub()
        risk = _RiskStub(kelly_qty=5, safe=True)
        sig = _signal(conf=0.30, price=0.50)  # EV clearly negative
        mp_records.clear()

        traded = host._process_signals([sig], "TestStrat", risk, dash)

        assert traded is False
        rejects = _reject_lines(mp_records)
        assert len(rejects) == 1
        msg = rejects[0].getMessage()
        assert "reason=EV_GATE" in msg
        assert "strategy=TestStrat" in msg
        assert "symbol=KXGASMAXUS-26AUG" in msg
        assert _info_lines(mp_records, "[Signal] EXECUTED") == []
        assert risk.executions == []

    def test_weather_slot_full_logs_exactly_one_line(self, mp_records):
        host, dash = _Host(), _DashboardStub()
        risk = _RiskStub(
            positions=[{"symbol": "KXHIGHNY-26JUL24-B85.5"}],
        )
        sig = _signal(symbol="KXHIGHNY-26JUL25-B87.5")
        mp_records.clear()

        traded = host._process_signals([sig], "Meteorologist V2", risk, dash)

        assert traded is False
        rejects = _reject_lines(mp_records)
        assert len(rejects) == 1
        assert f"reason={REASON_WEATHER_SLOT_FULL}" in rejects[0].getMessage()

    def test_missing_limit_price_logs_exactly_one_line(self, mp_records):
        host, dash = _Host(), _DashboardStub()
        risk = _RiskStub()
        sig = TradeSignal(
            symbol="KXGASMAXUS-26AUG", side="buy", quantity=3, limit_price=None
        )
        mp_records.clear()

        traded = host._process_signals([sig], "TestStrat", risk, dash)

        assert traded is False
        rejects = _reject_lines(mp_records)
        assert len(rejects) == 1
        assert f"reason={REASON_MISSING_LIMIT_PRICE}" in rejects[0].getMessage()

    def test_kelly_zero_is_logged_once_by_risk_manager_not_mixins(
        self, mp_records, tmp_path, monkeypatch
    ):
        """Real RiskManager: KELLY_ZERO logs inside calculate_kelly_size and
        the mixins qty<1 skip must NOT add a second rejection line."""
        monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", str(tmp_path / "win_rates.json"))
        risk = RiskManager(starting_balance=1000.0, persist_state=False)
        host, dash = _Host(), _DashboardStub()
        sig = _signal(price=0.99, conf=0.90)  # fees/odds force zero sizing
        mp_records.clear()

        traded = host._process_signals([sig], "TestStrat", risk, dash)

        assert traded is False
        rejects = _reject_lines(mp_records)
        assert len(rejects) == 1, [r.getMessage() for r in rejects]
        assert "reason=KELLY_ZERO" in rejects[0].getMessage()

    def test_risk_check_rejection_logged_once_by_risk_manager(
        self, mp_records, tmp_path, monkeypatch
    ):
        """Exit criterion 4 end-to-end: a synthetic signal hitting a real
        RiskManager rejection produces exactly ONE INFO rejection line, and
        the mixins layer adds none of its own."""
        monkeypatch.setattr(rm_mod, "WIN_RATES_PATH", str(tmp_path / "win_rates.json"))
        risk = RiskManager(starting_balance=1000.0, persist_state=False)
        risk.daily_trade_count = risk.MAX_DAILY_TRADES  # force DAILY_TRADE_CAP
        host, dash = _Host(), _DashboardStub()
        mp_records.clear()

        traded = host._process_signals([_signal()], "TestStrat", risk, dash)

        assert traded is False
        rejects = _reject_lines(mp_records)
        assert len(rejects) == 1, [r.getMessage() for r in rejects]
        msg = rejects[0].getMessage()
        assert "reason=DAILY_TRADE_CAP" in msg
        # check_order now receives the signal context for its log line
        assert "side=buy" in msg
        assert "quantity=" in msg
        # Mixins-side bookkeeping still happened
        assert any("HARVEST" in m for m in dash.logged)

    def test_stub_risk_rejection_mixins_stays_silent(self, mp_records):
        """With a risk manager that rejects but logs nothing (stub), the
        mixins layer must not invent a rejection line — proving the reason
        codes for risk checks are owned by the RiskManager alone."""
        host, dash = _Host(), _DashboardStub()
        risk = _RiskStub(kelly_qty=5, safe=False)
        mp_records.clear()

        traded = host._process_signals([_signal()], "TestStrat", risk, dash)

        assert traded is False
        assert _reject_lines(mp_records) == []
        assert len(_info_lines(mp_records, "[Signal] EMIT")) == 1


# ---------------------------------------------------------------------------
# 4. FR-0.4 / FR-0.5 — cycle summary bot status + rollover session handshake
# ---------------------------------------------------------------------------


class _StubBot:
    def __init__(self, name, trading_enabled=None):
        self.name = name
        if trading_enabled is not None:
            self.trading_enabled = trading_enabled


def _status_engine(bots, active):
    engine = OrchestratorEngine.__new__(OrchestratorEngine)
    engine.bots = bots
    engine.active_bots = set(active)
    return engine


class TestBotStatusSummary:
    def test_trading_feed_only_disabled(self):
        bots = [
            _StubBot("alpha", trading_enabled=True),
            _StubBot("beta", trading_enabled=False),
            _StubBot("gamma", trading_enabled=True),
        ]
        engine = _status_engine(bots, active={"alpha", "beta"})
        assert engine._bot_status(bots[0]) == "TRADING"
        assert engine._bot_status(bots[1]) == "FEED-ONLY"
        assert engine._bot_status(bots[2]) == "DISABLED"
        assert (
            engine._bot_status_summary()
            == "alpha=TRADING, beta=FEED-ONLY, gamma=DISABLED"
        )

    def test_weather_bot_module_constant_maps_to_feed_only(self):
        """The real weather bot exposes no attribute — its module-level
        WEATHER_TRADING_ENABLED=False must map to FEED-ONLY."""
        from src.bots.weather_bot import WeatherBot

        bot = WeatherBot.__new__(WeatherBot)  # skip heavy __init__
        bot.name = "Weather"
        engine = _status_engine([bot], active={"Weather"})
        assert engine._bot_status(bot) == "FEED-ONLY"
        assert engine._bot_status_summary() == "Weather=FEED-ONLY"

    def test_every_registered_bot_appears_in_summary(self):
        bots = [_StubBot(f"bot{i}", trading_enabled=True) for i in range(3)]
        engine = _status_engine(bots, active={b.name for b in bots})
        summary = engine._bot_status_summary()
        for b in bots:
            assert f"{b.name}=" in summary


class _OldDashboardStub:
    """The pre-rollover dashboard: real files on disk, per-write semantics."""

    def __init__(self, logs_dir):
        self.session_log_path = str(_touch(logs_dir / "session_19990101_000000.log"))
        self.data_log_path = str(_touch(logs_dir / "data_19990101_000000.csv"))
        self.portfolio_log_path = str(
            _touch(logs_dir / "portfolio_19990101_000000.csv")
        )
        self.alerts = ["carried-alert"]
        self.strategy_stats = {"S": {"wins": 1, "losses": 2}}

    def log(self, msg):
        with open(self.session_log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    def alert(self, msg):
        self.alerts.append(msg)


class _CycleRiskStub:
    def __init__(self):
        self.drawdown_kill_triggered = True
        self.daily_pnl = -12.5
        self.strategy_pnl = {}
        self.loss_cooldown = {}
        self.last_trade_time = None
        self.active_positions = 0
        # PRD FR-1.5 (2026-07-25): the rollover now calls real exchange methods
        # (_close_position / _bump_next_id / _save_state) to hold weather
        # positions across the boundary, so the hand-rolled SimpleNamespace
        # stub is replaced by a real, non-persisting SimulatedExchange. No
        # assertion in this file changes — the cycle tests keep an empty book.
        from src.core.matching_engine import SimulatedExchange

        self.exchange = SimulatedExchange(state_file=None)
        self.balance_updates = []

    def _save_win_rates(self):
        pass

    def update_balance(self, bal):
        self.balance_updates.append(bal)


class TestCycleRollover:
    @pytest.fixture
    def cycle_engine(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        logs = tmp_path / "logs"
        logs.mkdir()

        active_log = _touch(logs / "money_printer_ACTIVE.log", "process log")
        monkeypatch.setattr(rd, "get_active_log_path", lambda: str(active_log))
        watchdog = [_touch(logs / name) for name in WATCHDOG_FILES]

        from src.bots.weather_bot import WeatherBot

        weather = WeatherBot.__new__(WeatherBot)
        weather.name = "Weather"

        engine = OrchestratorEngine.__new__(OrchestratorEngine)
        engine.dashboard = _OldDashboardStub(logs)
        engine.risk_manager = _CycleRiskStub()
        engine.bots = [weather]
        engine.active_bots = {"Weather"}
        engine.sim_balance = 0.0
        engine._cycle_count = 0
        engine._cycle_start_time = time.time() - 300
        engine._profitable_since = None
        engine.cycle_history = []
        engine._training_history = []
        engine._training_diagnostics = {}
        engine.trade_journal = SimpleNamespace(
            get_sample_count=lambda: 0,
            analyze_losses=lambda n=50: {"summary": ""},
        )
        engine.settlement_resolver = SimpleNamespace(get_pending_count=lambda: 0)
        engine.counter_analyzer = SimpleNamespace(reset_cycle=lambda: None)

        return SimpleNamespace(
            engine=engine,
            logs=logs,
            active_log=active_log,
            watchdog=watchdog,
            old_session=Path(engine.dashboard.session_log_path),
        )

    def test_rollover_new_session_log_exists_and_old_archived(self, cycle_engine):
        env = cycle_engine
        old_name = env.old_session.name

        env.engine._run_drawdown_cycle()

        # FR-0.5: a session log exists immediately after rollover (the new
        # Dashboard was created before the old files were archived) — the
        # watchdog never sees a zero-session-log window.
        session_logs = list(env.logs.glob("session_*.log"))
        assert len(session_logs) == 1
        assert session_logs[0].name != old_name
        assert "SESSION STARTED" in session_logs[0].read_text(encoding="utf-8")

        # The old session file was archived, then removed.
        archive_dirs = list((env.logs / "_archive").glob("cycle_*_dd1"))
        assert len(archive_dirs) == 1
        assert (archive_dirs[0] / old_name).exists()
        assert not env.old_session.exists()

        # FR-0.3: process log + watchdog state survive the cycle sweep.
        assert env.active_log.exists()
        for f in env.watchdog:
            assert f.exists(), f"watchdog file removed at rollover: {f.name}"

        # Alerts carried over to the new dashboard.
        assert "carried-alert" in env.engine.dashboard.alerts

    def test_rollover_summary_lists_bot_status(self, cycle_engine):
        env = cycle_engine
        env.engine._run_drawdown_cycle()

        record = env.engine.cycle_history[-1]
        assert record["bot_status"] == "Weather=FEED-ONLY"
        assert any("BOTS | Weather=FEED-ONLY" in a for a in env.engine.dashboard.alerts)
        # Risk manager was reset to the default sim balance.
        assert env.engine.risk_manager.balance_updates == [3000.0]
