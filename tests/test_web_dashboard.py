"""
Comprehensive tests for the web dashboard: StateManager, server endpoints,
uptime timer, and formatting helpers.
"""

import time
import pytest
from unittest.mock import MagicMock, PropertyMock
from collections import deque
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bot(name):
    bot = MagicMock()
    type(bot).name = PropertyMock(return_value=name)
    bot.strategies = {}
    return bot


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    bot1 = _make_bot("btc_15m")
    bot2 = _make_bot("weather")
    orch.bots = [bot1, bot2]
    orch.active_bots = {"btc_15m", "weather"}
    orch.risk_manager = MagicMock()
    orch.risk_manager.balance = 100.0
    orch.risk_manager.daily_pnl = 5.0
    orch.risk_manager.unrealized_pnl = -2.0
    orch.risk_manager.get_current_exposure.return_value = 30.0
    orch.risk_manager.exchange.positions = []
    orch.dashboard = MagicMock()
    orch.dashboard.start_time = datetime.now()
    orch.dashboard.latest_prices = {}
    orch.dashboard.alerts = deque(["Test alert"])
    orch.dashboard.logs = deque(["System started"])
    orch.dashboard.strategy_stats = {}
    orch.dashboard.mascot = MagicMock()
    orch.dashboard.mascot.state = "IDLE"
    orch.uptime_seconds = 125.0
    return orch


# ---------------------------------------------------------------------------
# StateManager snapshot tests
# ---------------------------------------------------------------------------


class TestStateManagerSnapshot:
    def test_snapshot_has_all_keys(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        expected_keys = {
            "mode",
            "uptime",
            "portfolio",
            "market_data",
            "alerts",
            "logs",
            "strategy_stats",
            "positions",
            "pnl_history",
            "bots",
            "mascot_state",
            "data_log",
            "cycle_history",
            "training_diagnostics",
            "training_history",
        }
        assert expected_keys == set(snap.keys())

    def test_portfolio_with_risk_manager(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        pf = snap["portfolio"]
        # equity = balance + exposure + unrealized_pnl = 100 + 30 + (-2) = 128
        assert pf["equity"] == 128.0
        assert pf["cash"] == 100.0
        assert pf["exposure"] == 30.0
        assert pf["realized_pnl"] == 5.0
        assert pf["unrealized_pnl"] == -2.0

    def test_portfolio_without_risk_manager(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.risk_manager = None
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        pf = snap["portfolio"]
        assert pf["equity"] == 0.0
        assert pf["cash"] == 0.0
        assert pf["exposure"] == 0.0

    def test_market_data_includes_fresh(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.dashboard.latest_prices = {
            "BTC-YES": {"price": 0.55, "ts": time.time(), "extra": {}},
        }
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert len(snap["market_data"]) == 1
        assert snap["market_data"][0]["symbol"] == "BTC-YES"

    def test_market_data_excludes_stale(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.dashboard.latest_prices = {
            "OLD-SYM": {"price": 0.30, "ts": time.time() - 400, "extra": {}},
        }
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert len(snap["market_data"]) == 0

    def test_market_data_sorted_by_symbol(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        now = time.time()
        mock_orchestrator.dashboard.latest_prices = {
            "ZZZ": {"price": 0.10, "ts": now, "extra": {}},
            "AAA": {"price": 0.90, "ts": now, "extra": {}},
        }
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        symbols = [m["symbol"] for m in snap["market_data"]]
        assert symbols == ["AAA", "ZZZ"]

    def test_alerts_and_logs(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert snap["alerts"] == ["Test alert"]
        assert snap["logs"] == ["System started"]

    def test_strategy_stats_rounding(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.dashboard.strategy_stats = {
            "V3_Momentum": {
                "signals": 10,
                "wins": 6,
                "losses": 4,
                "pnl": 1.23456,
                "active": 2,
            },
        }
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        stats = snap["strategy_stats"]["V3_Momentum"]
        assert stats["pnl"] == 1.2346  # rounded to 4 places

    def test_positions_with_age(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.risk_manager.exchange.positions = [
            {
                "id": "pos1",
                "symbol": "BTC-YES",
                "side": "buy",
                "contract_side": "YES",
                "entry_price": 0.45,
                "current_price": 0.50,
                "quantity": 10,
                "pnl": 0.50,
                "strategy_name": "V3",
                "open_time": datetime.now() - timedelta(seconds=60),
            }
        ]
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert len(snap["positions"]) == 1
        pos = snap["positions"][0]
        assert pos["id"] == "pos1"
        assert 58 <= pos["age"] <= 62  # approximately 60 seconds

    def test_bots_active_status(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.active_bots = {"btc_15m"}  # only btc active
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        bots_map = {b["name"]: b["active"] for b in snap["bots"]}
        assert bots_map["btc_15m"] is True
        assert bots_map["weather"] is False

    def test_mascot_state_from_dashboard(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.dashboard.mascot.state = "PANIC"
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert snap["mascot_state"] == "PANIC"

    def test_mascot_state_no_dashboard(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.dashboard = None
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert snap["mascot_state"] == "IDLE"

    def test_pnl_history_accumulates(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        sm.snapshot()
        sm.snapshot()
        sm.snapshot()
        snap = sm.snapshot()
        assert len(snap["pnl_history"]) == 4  # 4 calls total

    def test_pnl_history_maxlen(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        for _ in range(510):
            sm.snapshot()
        snap = sm.snapshot()
        assert len(snap["pnl_history"]) == 500

    def test_uptime_format(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        mock_orchestrator.uptime_seconds = 3661.0
        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        assert snap["uptime"] == "01:01:01"


# ---------------------------------------------------------------------------
# Uptime timer tests
# ---------------------------------------------------------------------------


class TestUptimeTimer:
    """Test uptime pause/resume logic on OrchestratorEngine."""

    def _make_engine_stub(self):
        from scripts.run_dashboard import OrchestratorEngine

        engine = object.__new__(OrchestratorEngine)
        engine.bots = [_make_bot("bot_a"), _make_bot("bot_b")]
        engine.active_bots = {"bot_a", "bot_b"}
        engine._uptime_accumulated = 0.0
        engine._uptime_resumed_at = time.time()
        return engine

    def test_uptime_increases_when_bots_active(self):
        engine = self._make_engine_stub()
        t1 = engine.uptime_seconds
        time.sleep(0.05)
        t2 = engine.uptime_seconds
        assert t2 > t1

    def test_uptime_pauses_when_all_stopped(self):
        engine = self._make_engine_stub()
        time.sleep(0.05)
        engine.stop_bot("bot_a")
        engine.stop_bot("bot_b")
        t1 = engine.uptime_seconds
        time.sleep(0.05)
        t2 = engine.uptime_seconds
        assert abs(t2 - t1) < 0.01

    def test_uptime_resumes_after_restart(self):
        engine = self._make_engine_stub()
        time.sleep(0.05)
        engine.stop_bot("bot_a")
        engine.stop_bot("bot_b")
        frozen = engine.uptime_seconds
        time.sleep(0.05)
        engine.start_bot("bot_a")
        time.sleep(0.05)
        resumed = engine.uptime_seconds
        assert resumed > frozen

    def test_uptime_accumulates_across_cycles(self):
        engine = self._make_engine_stub()
        time.sleep(0.05)
        engine.stop_bot("bot_a")
        engine.stop_bot("bot_b")
        first_period = engine.uptime_seconds
        assert first_period > 0
        engine.start_bot("bot_a")
        time.sleep(0.05)
        engine.stop_bot("bot_a")
        total = engine.uptime_seconds
        assert total > first_period

    def test_start_unknown_bot_raises(self):
        engine = self._make_engine_stub()
        with pytest.raises(ValueError):
            engine.start_bot("nonexistent")

    def test_stop_unknown_bot_raises(self):
        engine = self._make_engine_stub()
        with pytest.raises(ValueError):
            engine.stop_bot("nonexistent")


# ---------------------------------------------------------------------------
# Server endpoint tests
# ---------------------------------------------------------------------------


class TestServerEndpoints:
    @pytest.fixture
    def client(self, mock_orchestrator):
        from src.web.state_manager import StateManager
        from src.web.server import create_app
        from fastapi.testclient import TestClient

        sm = StateManager(mock_orchestrator)
        app = create_app(sm, mock_orchestrator)
        return TestClient(app)

    def test_index_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "html" in resp.headers.get("content-type", "").lower()

    def test_get_bots(self, client):
        resp = client.get("/api/bots")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        names = [b["name"] for b in data]
        assert "btc_15m" in names
        assert "weather" in names

    def test_get_bots_active_status(self, client, mock_orchestrator):
        mock_orchestrator.active_bots = {"btc_15m"}
        resp = client.get("/api/bots")
        data = resp.json()
        bots_map = {b["name"]: b["active"] for b in data}
        assert bots_map["btc_15m"] is True
        assert bots_map["weather"] is False

    def test_start_bot_success(self, client, mock_orchestrator):
        mock_orchestrator.start_bot = MagicMock()
        resp = client.post("/api/bots/btc_15m/start")
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "started"
        mock_orchestrator.start_bot.assert_called_once_with("btc_15m")

    def test_start_bot_not_found(self, client, mock_orchestrator):
        mock_orchestrator.start_bot = MagicMock(side_effect=ValueError("not found"))
        resp = client.post("/api/bots/nonexistent/start")
        assert resp.status_code == 404

    def test_stop_bot_success(self, client, mock_orchestrator):
        mock_orchestrator.stop_bot = MagicMock()
        resp = client.post("/api/bots/weather/stop")
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "stopped"
        mock_orchestrator.stop_bot.assert_called_once_with("weather")

    def test_stop_bot_not_found(self, client, mock_orchestrator):
        mock_orchestrator.stop_bot = MagicMock(side_effect=ValueError("not found"))
        resp = client.post("/api/bots/nonexistent/stop")
        assert resp.status_code == 404

    def test_websocket_connects(self, client):
        """Verify the WebSocket endpoint accepts connections."""
        with client.websocket_connect("/ws") as ws:
            # Connection accepted — the broadcast loop sends snapshots
            # every 1s. We just verify the connection works.
            assert ws is not None


# ---------------------------------------------------------------------------
# Bot control flow tests
# ---------------------------------------------------------------------------


class TestBotControlFlow:
    def test_start_adds_to_active(self):
        from scripts.run_dashboard import OrchestratorEngine

        engine = object.__new__(OrchestratorEngine)
        engine.bots = [_make_bot("bot_a")]
        engine.active_bots = set()
        engine._uptime_accumulated = 0.0
        engine._uptime_resumed_at = time.time()
        engine.start_bot("bot_a")
        assert "bot_a" in engine.active_bots

    def test_stop_removes_from_active(self):
        from scripts.run_dashboard import OrchestratorEngine

        engine = object.__new__(OrchestratorEngine)
        engine.bots = [_make_bot("bot_a")]
        engine.active_bots = {"bot_a"}
        engine._uptime_accumulated = 0.0
        engine._uptime_resumed_at = time.time()
        engine.stop_bot("bot_a")
        assert "bot_a" not in engine.active_bots

    def test_double_start_is_idempotent(self):
        from scripts.run_dashboard import OrchestratorEngine

        engine = object.__new__(OrchestratorEngine)
        engine.bots = [_make_bot("bot_a")]
        engine.active_bots = {"bot_a"}
        engine._uptime_accumulated = 0.0
        engine._uptime_resumed_at = time.time()
        engine.start_bot("bot_a")
        assert "bot_a" in engine.active_bots

    def test_double_stop_is_idempotent(self):
        from scripts.run_dashboard import OrchestratorEngine

        engine = object.__new__(OrchestratorEngine)
        engine.bots = [_make_bot("bot_a")]
        engine.active_bots = set()
        engine._uptime_accumulated = 5.0
        engine._uptime_resumed_at = time.time()
        engine.stop_bot("bot_a")  # already not in active_bots
        assert engine._uptime_accumulated == 5.0  # should not change


# ---------------------------------------------------------------------------
# Formatting helpers tests
# ---------------------------------------------------------------------------


class TestFormattingHelpers:
    def test_fmt_uptime_zero(self):
        from src.web.state_manager import StateManager

        assert StateManager._fmt_uptime_seconds(0) == "00:00:00"

    def test_fmt_uptime_seconds_only(self):
        from src.web.state_manager import StateManager

        assert StateManager._fmt_uptime_seconds(45) == "00:00:45"

    def test_fmt_uptime_minutes(self):
        from src.web.state_manager import StateManager

        assert StateManager._fmt_uptime_seconds(125) == "00:02:05"

    def test_fmt_uptime_hours(self):
        from src.web.state_manager import StateManager

        assert StateManager._fmt_uptime_seconds(3661) == "01:01:01"

    def test_fmt_uptime_large(self):
        from src.web.state_manager import StateManager

        assert StateManager._fmt_uptime_seconds(86400) == "24:00:00"


# ---------------------------------------------------------------------------
# Market Data Flow tests
# ---------------------------------------------------------------------------


class TestMarketDataFlow:
    """Test price updates flow through Dashboard into StateManager snapshots."""

    @pytest.fixture
    def dashboard_and_sm(self, mock_orchestrator, tmp_path):
        from src.visualization.dashboard import Dashboard
        from src.web.state_manager import StateManager

        dash = Dashboard.__new__(Dashboard)
        dash.latest_prices = {}
        dash.alerts = deque()
        dash.logs = deque()
        dash.strategy_stats = {}
        dash.mascot = MagicMock()
        dash.mascot.state = "IDLE"
        dash.data_log_path = str(tmp_path / "data.csv")
        dash.portfolio_log_path = str(tmp_path / "portfolio.csv")
        dash.session_log_path = str(tmp_path / "session.log")
        dash.start_time = datetime.now()
        dash.log_dir = str(tmp_path)
        # Init CSV headers
        import csv

        with open(dash.data_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
        with open(dash.portfolio_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [
                    "Timestamp",
                    "Equity",
                    "Cash",
                    "Exposure",
                    "Realized_PnL",
                    "Unrealized_PnL",
                ]
            )

        mock_orchestrator.dashboard = dash
        sm = StateManager(mock_orchestrator)
        return dash, sm

    def test_update_price_appears_in_snapshot(self, dashboard_and_sm):
        dash, sm = dashboard_and_sm
        dash.update_price("BTC-USD", 71000.0)
        snap = sm.snapshot()
        md = snap["market_data"]
        assert len(md) == 1
        assert md[0]["symbol"] == "BTC-USD"
        assert md[0]["price"] == 71000.0

    def test_stale_data_filtered(self, dashboard_and_sm):
        dash, sm = dashboard_and_sm
        dash.latest_prices["STALE-SYM"] = {
            "price": 42.0,
            "ts": time.time() - 600,  # 10 min ago
            "extra": {},
        }
        snap = sm.snapshot()
        assert len(snap["market_data"]) == 0

    def test_market_data_with_bid_ask_volume(self, dashboard_and_sm):
        dash, sm = dashboard_and_sm
        dash.update_price("ETH-USD", 3500.0, bid=3499.0, ask=3501.0, volume=1234.5)
        snap = sm.snapshot()
        md = snap["market_data"][0]
        assert md["bid"] == 3499.0
        assert md["ask"] == 3501.0
        assert md["volume"] == 1234.5


# ---------------------------------------------------------------------------
# Portfolio Logging tests
# ---------------------------------------------------------------------------


class TestPortfolioLogging:
    """Test that portfolio CSV logging works correctly."""

    @pytest.fixture
    def dashboard_with_tmp(self, tmp_path):
        from src.visualization.dashboard import Dashboard
        import csv

        dash = Dashboard.__new__(Dashboard)
        dash.latest_prices = {}
        dash.alerts = deque()
        dash.logs = deque()
        dash.strategy_stats = {}
        dash.mascot = MagicMock()
        dash.mascot.state = "IDLE"
        dash.start_time = datetime.now()
        dash.log_dir = str(tmp_path)
        dash.data_log_path = str(tmp_path / "data.csv")
        dash.portfolio_log_path = str(tmp_path / "portfolio.csv")
        dash.session_log_path = str(tmp_path / "session.log")
        with open(dash.data_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
        with open(dash.portfolio_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [
                    "Timestamp",
                    "Equity",
                    "Cash",
                    "Exposure",
                    "Realized_PnL",
                    "Unrealized_PnL",
                ]
            )
        return dash

    def _make_risk_manager(self):
        rm = MagicMock()
        rm.balance = 100.0
        rm.daily_pnl = 5.0
        rm.unrealized_pnl = -2.0
        rm.get_current_exposure.return_value = 30.0
        rm.exchange.positions = []
        return rm

    def test_log_portfolio_writes_csv_row(self, dashboard_with_tmp):
        import csv

        dash = dashboard_with_tmp
        rm = self._make_risk_manager()
        dash.log_portfolio(rm)

        with open(dash.portfolio_log_path, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert len(rows) == 2  # header + 1 data row
        # equity = 100 + 30 + (-2) = 128
        assert float(rows[1][1]) == 128.0
        assert float(rows[1][2]) == 100.0  # cash
        assert float(rows[1][3]) == 30.0  # exposure

    def test_snapshot_triggers_portfolio_logging(
        self, mock_orchestrator, dashboard_with_tmp
    ):
        import csv
        from src.web.state_manager import StateManager

        dash = dashboard_with_tmp
        mock_orchestrator.dashboard = dash
        sm = StateManager(mock_orchestrator)
        sm.snapshot()

        with open(dash.portfolio_log_path, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        # header + 1 row from snapshot calling log_portfolio
        assert len(rows) == 2

    def test_csv_format_correctness(self, dashboard_with_tmp):
        import csv

        dash = dashboard_with_tmp
        rm = self._make_risk_manager()
        dash.log_portfolio(rm)

        with open(dash.portfolio_log_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        row = rows[0]
        assert set(row.keys()) == {
            "Timestamp",
            "Equity",
            "Cash",
            "Exposure",
            "Realized_PnL",
            "Unrealized_PnL",
        }


# ---------------------------------------------------------------------------
# Extended Bot Control Flow tests
# ---------------------------------------------------------------------------


class TestBotControlFlowExtended:
    """Additional bot control flow tests via HTTP endpoints."""

    @pytest.fixture
    def client_and_orch(self, mock_orchestrator):
        from src.web.state_manager import StateManager
        from src.web.server import create_app
        from fastapi.testclient import TestClient

        mock_orchestrator.start_bot = MagicMock()
        mock_orchestrator.stop_bot = MagicMock()
        sm = StateManager(mock_orchestrator)
        app = create_app(sm, mock_orchestrator)
        return TestClient(app), mock_orchestrator

    def test_start_changes_active_bots(self, client_and_orch):
        client, orch = client_and_orch
        orch.active_bots = {"weather"}

        def fake_start(name):
            orch.active_bots.add(name)

        orch.start_bot.side_effect = fake_start

        resp = client.post("/api/bots/btc_15m/start")
        assert resp.status_code == 200
        assert "btc_15m" in orch.active_bots

    def test_stop_changes_active_bots(self, client_and_orch):
        client, orch = client_and_orch
        orch.active_bots = {"btc_15m", "weather"}

        def fake_stop(name):
            orch.active_bots.discard(name)

        orch.stop_bot.side_effect = fake_stop

        resp = client.post("/api/bots/weather/stop")
        assert resp.status_code == 200
        assert "weather" not in orch.active_bots

    def test_stopped_bot_not_active_in_snapshot(self, client_and_orch):
        client, orch = client_and_orch
        orch.active_bots = {"btc_15m"}  # weather stopped
        resp = client.get("/api/bots")
        bots_map = {b["name"]: b["active"] for b in resp.json()}
        assert bots_map["btc_15m"] is True
        assert bots_map["weather"] is False


# ---------------------------------------------------------------------------
# Data Log in Snapshot tests
# ---------------------------------------------------------------------------


class TestDataLogSnapshot:
    """Test that data_log key appears in snapshots with correct entries."""

    @pytest.fixture
    def dashboard_sm(self, mock_orchestrator, tmp_path):
        from src.visualization.dashboard import Dashboard
        from src.web.state_manager import StateManager
        import csv

        dash = Dashboard.__new__(Dashboard)
        dash.latest_prices = {}
        dash.alerts = deque()
        dash.logs = deque()
        dash.strategy_stats = {}
        dash.mascot = MagicMock()
        dash.mascot.state = "IDLE"
        dash.start_time = datetime.now()
        dash.log_dir = str(tmp_path)
        dash.data_log_path = str(tmp_path / "data.csv")
        dash.portfolio_log_path = str(tmp_path / "portfolio.csv")
        dash.session_log_path = str(tmp_path / "session.log")
        with open(dash.data_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
        with open(dash.portfolio_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                [
                    "Timestamp",
                    "Equity",
                    "Cash",
                    "Exposure",
                    "Realized_PnL",
                    "Unrealized_PnL",
                ]
            )

        mock_orchestrator.dashboard = dash
        sm = StateManager(mock_orchestrator)
        return dash, sm

    def test_data_log_key_exists(self, dashboard_sm):
        _, sm = dashboard_sm
        snap = sm.snapshot()
        assert "data_log" in snap

    def test_update_price_creates_data_log_entries(self, dashboard_sm):
        dash, sm = dashboard_sm
        dash.update_price("BTC-USD", 71000.0)
        dash.update_price("ETH-USD", 3500.0)
        snap = sm.snapshot()
        assert len(snap["data_log"]) == 2
        symbols = [row["Symbol"] for row in snap["data_log"]]
        assert "BTC-USD" in symbols
        assert "ETH-USD" in symbols

    def test_data_log_api_endpoint(self, mock_orchestrator, tmp_path):
        from src.web.state_manager import StateManager
        from src.web.server import create_app
        from fastapi.testclient import TestClient
        import csv

        dash = MagicMock()
        dash.data_log_path = str(tmp_path / "data.csv")
        with open(dash.data_log_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
            writer.writerow(
                ["2026-03-15T10:00:00", "BTC-USD", "71000.0", "MARKET_DATA", "REAL"]
            )

        mock_orchestrator.dashboard = dash
        sm = StateManager(mock_orchestrator)
        app = create_app(sm, mock_orchestrator)
        client = TestClient(app)

        resp = client.get("/api/logs/data")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["Symbol"] == "BTC-USD"


# ---------------------------------------------------------------------------
# Log API Endpoints tests
# ---------------------------------------------------------------------------


class TestLogEndpoints:
    """Test the /api/logs/data and /api/logs/session endpoints."""

    @pytest.fixture
    def client_with_logs(self, mock_orchestrator, tmp_path):
        from src.web.state_manager import StateManager
        from src.web.server import create_app
        from fastapi.testclient import TestClient

        dash = MagicMock()
        dash.data_log_path = str(tmp_path / "data.csv")
        dash.session_log_path = str(tmp_path / "session.log")
        mock_orchestrator.dashboard = dash
        sm = StateManager(mock_orchestrator)
        app = create_app(sm, mock_orchestrator)
        return TestClient(app), tmp_path, dash

    def test_data_log_returns_json_array(self, client_with_logs):
        import csv

        client, tmp_path, dash = client_with_logs
        with open(dash.data_log_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
            writer.writerow(
                ["2026-03-15T10:00:00", "BTC-USD", "71000", "MARKET_DATA", "REAL"]
            )
        resp = client.get("/api/logs/data")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_session_log_returns_json_array(self, client_with_logs):
        client, tmp_path, dash = client_with_logs
        with open(dash.session_log_path, "w", encoding="utf-8") as f:
            f.write("Line 1\nLine 2\n")
        resp = client.get("/api/logs/session")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0] == "Line 1"

    def test_empty_data_log_returns_empty_array(self, client_with_logs):
        import csv

        client, tmp_path, dash = client_with_logs
        # Write only header
        with open(dash.data_log_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Timestamp", "Symbol", "Price", "Type", "Status"])
        resp = client.get("/api/logs/data")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_empty_session_log_returns_empty_array(self, client_with_logs):
        client, tmp_path, dash = client_with_logs
        with open(dash.session_log_path, "w", encoding="utf-8"):
            pass  # empty file
        resp = client.get("/api/logs/session")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_missing_log_files_returns_empty_array(self, mock_orchestrator):
        from src.web.state_manager import StateManager
        from src.web.server import create_app
        from fastapi.testclient import TestClient

        dash = MagicMock()
        dash.data_log_path = "/nonexistent/data.csv"
        dash.session_log_path = "/nonexistent/session.log"
        mock_orchestrator.dashboard = dash
        sm = StateManager(mock_orchestrator)
        app = create_app(sm, mock_orchestrator)
        client = TestClient(app)

        assert client.get("/api/logs/data").json() == []
        assert client.get("/api/logs/session").json() == []


# ---------------------------------------------------------------------------
# Snapshot Completeness tests
# ---------------------------------------------------------------------------


class TestSnapshotCompleteness:
    """Verify snapshot contains all expected keys and is JSON-serializable."""

    def test_snapshot_contains_all_expected_keys(self, mock_orchestrator):
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        expected = {
            "mode",
            "uptime",
            "portfolio",
            "market_data",
            "alerts",
            "logs",
            "strategy_stats",
            "positions",
            "pnl_history",
            "bots",
            "mascot_state",
            "data_log",
            "cycle_history",
            "training_diagnostics",
            "training_history",
        }
        assert expected == set(snap.keys())

    def test_snapshot_is_json_serializable(self, mock_orchestrator):
        import json
        from src.web.state_manager import StateManager

        sm = StateManager(mock_orchestrator)
        snap = sm.snapshot()
        # Should not raise
        serialized = json.dumps(snap, default=str)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict)


# =========================================================================
# Live Kalshi API Market Data Tests
# =========================================================================


class TestKalshiMarketDataLive:
    """Integration tests that hit the real Kalshi public API to verify
    we can discover and read market data (YES and NO prices) for all
    three market types: BTC 15m, BTC Hourly, and Weather.

    These tests validate that:
    1. The API field names we use (_dollars suffix) match the actual API
    2. _parse_price handles both V2 (dollars strings) and V1 (cents ints)
    3. _parse_market_data returns non-None MarketData with correct fields
    4. Discovery (search_markets) finds markets for each series
    5. Weather markets (active) return real YES and NO prices
    """

    @pytest.fixture
    def provider(self):
        """Unauthenticated provider pointed at public production API."""
        from src.data.kalshi_provider import KalshiProvider

        return KalshiProvider(
            key_id=None,
            private_key_path=None,
            api_url="https://api.elections.kalshi.com/trade-api/v2",
            read_only=True,
        )

    # ------------------------------------------------------------------
    # _parse_price unit tests (no network)
    # ------------------------------------------------------------------

    def test_parse_price_v2_dollars_field(self):
        """V2 API returns prices as dollar strings like '0.0700'."""
        from src.data.kalshi_provider import KalshiProvider

        data = {"yes_bid_dollars": "0.0700", "yes_bid": 7}
        result = KalshiProvider._parse_price(data, "yes_bid_dollars", "yes_bid")
        assert abs(result - 0.07) < 1e-9

    def test_parse_price_v1_cents_fallback(self):
        """V1 API may only have cents integer; _parse_price falls back."""
        from src.data.kalshi_provider import KalshiProvider

        data = {"yes_bid": 55}  # no _dollars key
        result = KalshiProvider._parse_price(data, "yes_bid_dollars", "yes_bid")
        assert abs(result - 0.55) < 1e-9

    def test_parse_price_none_values(self):
        """If both fields are None, returns 0.0."""
        from src.data.kalshi_provider import KalshiProvider

        data = {"yes_bid_dollars": None, "yes_bid": None}
        result = KalshiProvider._parse_price(data, "yes_bid_dollars", "yes_bid")
        assert result == 0.0

    def test_parse_price_missing_keys(self):
        """If keys are absent entirely, returns 0.0."""
        from src.data.kalshi_provider import KalshiProvider

        result = KalshiProvider._parse_price({}, "yes_bid_dollars", "yes_bid")
        assert result == 0.0

    def test_parse_market_data_v2_response(self):
        """_parse_market_data correctly reads a V2 API-shaped dict."""
        from src.data.kalshi_provider import KalshiProvider

        provider = KalshiProvider(key_id=None, private_key_path=None, read_only=True)
        data = {
            "yes_bid_dollars": "0.4500",
            "yes_ask_dollars": "0.5500",
            "no_bid_dollars": "0.4500",
            "no_ask_dollars": "0.5500",
            "last_price_dollars": "0.5000",
            "volume_fp": "1234.00",
            "status": "active",
            "close_time": "2026-03-16T00:00:00Z",
        }
        md = provider._parse_market_data("TEST-TICKER", data, "test")
        assert md.bid == 0.45
        assert md.ask == 0.55
        assert md.price == 0.50
        assert md.extra["no_bid"] == 0.45
        assert md.extra["no_ask"] == 0.55
        assert md.volume == 1234
        assert md.extra["status"] == "active"

    # ------------------------------------------------------------------
    # Live API: Weather markets (most reliably active with prices)
    # ------------------------------------------------------------------

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requests not installed",
    )
    def test_weather_market_has_yes_and_no_prices(self, provider):
        """Fetch a real KXHIGHNY weather market and verify YES/NO prices."""
        import requests

        # Discover an active weather market
        url = f"{provider.api_url}/markets"
        resp = requests.get(
            url,
            params={
                "series_ticker": "KXHIGHNY",
                "limit": 5,
            },
            timeout=10,
        )
        assert resp.status_code == 200, f"Market search failed: {resp.status_code}"

        markets = resp.json().get("markets", [])
        active = [m for m in markets if m.get("status") == "active"]
        assert len(active) > 0, (
            f"No active KXHIGHNY markets found. Statuses: "
            f"{[m.get('status') for m in markets]}"
        )

        # Fetch via our provider
        ticker = active[0].get("ticker")
        md = provider.fetch_latest(ticker)
        assert md is not None, f"fetch_latest({ticker}) returned None"

        # YES side
        assert md.bid >= 0, f"yes_bid should be >= 0, got {md.bid}"
        assert md.ask >= 0, f"yes_ask should be >= 0, got {md.ask}"
        # NO side
        assert (
            md.extra["no_bid"] >= 0
        ), f"no_bid should be >= 0, got {md.extra['no_bid']}"
        assert (
            md.extra["no_ask"] >= 0
        ), f"no_ask should be >= 0, got {md.extra['no_ask']}"

        # At least ONE side should have a non-zero price (market has liquidity)
        has_price = (
            md.bid > 0 or md.ask > 0 or md.extra["no_bid"] > 0 or md.extra["no_ask"] > 0
        )
        assert has_price, (
            f"All prices zero for {ticker}: "
            f"yes_bid={md.bid} yes_ask={md.ask} "
            f"no_bid={md.extra['no_bid']} no_ask={md.extra['no_ask']}"
        )

        # Sanity: YES + NO should roughly sum to ~1.0
        if md.bid > 0 and md.extra["no_ask"] > 0:
            complement = md.bid + md.extra["no_ask"]
            assert (
                0.9 <= complement <= 1.1
            ), f"yes_bid + no_ask = {complement}, expected ~1.0"

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requests not installed",
    )
    def test_btc_15m_market_discovery(self, provider):
        """Verify we can discover KXBTC15M markets via the API.
        These may be 'initialized' (pre-open) rather than 'active'."""
        import requests

        url = f"{provider.api_url}/markets"
        resp = requests.get(
            url,
            params={
                "series_ticker": "KXBTC15M",
                "limit": 5,
            },
            timeout=10,
        )
        assert resp.status_code == 200

        markets = resp.json().get("markets", [])
        assert len(markets) > 0, "No KXBTC15M markets found at all"

        # Verify we can fetch at least one via our provider
        ticker = markets[0].get("ticker")
        md = provider.fetch_latest(ticker)
        assert md is not None, f"fetch_latest({ticker}) returned None"
        assert md.symbol == ticker

        # Initialized markets may have 0 prices — that's OK.
        # But the MarketData object must be valid.
        assert md.bid >= 0
        assert md.ask >= 0
        assert md.extra["no_bid"] >= 0
        assert md.extra["no_ask"] >= 0

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requests not installed",
    )
    def test_btc_hourly_v1_discovery(self, provider):
        """Verify V1 BTC Hourly event discovery returns markets."""
        markets = provider.fetch_btc_hourly_markets()

        # V1 discovery probes 12 hours; at least some should exist
        assert len(markets) > 0, "V1 BTC Hourly discovery returned no markets"

        md = markets[0]
        assert md.symbol is not None
        assert "KXBTCD" in md.symbol
        assert md.extra.get("close_time") is not None

        # V1 markets should have parsed prices (may be 0 for finalized ones)
        assert md.bid >= 0
        assert md.ask >= 0

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requests not installed",
    )
    def test_fetch_latest_returns_all_price_fields(self, provider):
        """fetch_latest must populate bid, ask, price AND no_bid, no_ask
        in the extra dict for any successfully fetched market."""
        import requests

        # Use a weather market (reliably active with liquidity)
        url = f"{provider.api_url}/markets"
        resp = requests.get(
            url,
            params={
                "series_ticker": "KXHIGHNY",
                "limit": 3,
            },
            timeout=10,
        )
        markets = [
            m for m in resp.json().get("markets", []) if m.get("status") == "active"
        ]
        if not markets:
            pytest.skip("No active weather markets available")

        ticker = markets[0]["ticker"]
        md = provider.fetch_latest(ticker)
        assert md is not None

        # Verify all expected fields exist and are floats
        assert isinstance(md.bid, float), f"bid type: {type(md.bid)}"
        assert isinstance(md.ask, float), f"ask type: {type(md.ask)}"
        assert isinstance(md.price, float), f"price type: {type(md.price)}"
        assert isinstance(
            md.extra["no_bid"], float
        ), f"no_bid type: {type(md.extra['no_bid'])}"
        assert isinstance(
            md.extra["no_ask"], float
        ), f"no_ask type: {type(md.extra['no_ask'])}"

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requests not installed",
    )
    def test_weather_multiple_cities(self, provider):
        """Verify market discovery works for all 4 weather cities."""
        import requests

        series_tickers = ["KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI", "KXHIGHMIA"]
        for series in series_tickers:
            url = f"{provider.api_url}/markets"
            resp = requests.get(
                url,
                params={
                    "series_ticker": series,
                    "limit": 3,
                },
                timeout=10,
            )
            assert resp.status_code == 200, f"{series}: status {resp.status_code}"
            markets = resp.json().get("markets", [])
            assert len(markets) > 0, f"No markets found for {series}"
