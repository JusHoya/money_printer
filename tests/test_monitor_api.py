"""Tests for the remote-monitoring API routes added for the split
alcyone/maia deployment (src/web/server.py): /api/journal, /api/training,
/api/win_rates, /api/stats/rolling, /api/logs/tail.

These routes are what the Hermes agent's mp_* tools fall back to when the
sandbox data files are not on the agent's own filesystem.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import src.web.server as server_mod
from src.web.server import create_app


class _StubStateManager:
    def snapshot(self):
        return {"portfolio": {}, "positions": []}


class _StubOrchestrator:
    bots = []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    app = create_app(_StubStateManager(), _StubOrchestrator())
    with TestClient(app) as c:
        yield c, tmp_path


def _write_journal(tmp_path, trades):
    lines = "\n".join(json.dumps(t) for t in trades)
    (tmp_path / "data" / "trade_journal.jsonl").write_text(lines, encoding="utf-8")


def test_journal_returns_recent_trades(client):
    c, tmp_path = client
    _write_journal(
        tmp_path,
        [
            {"symbol": "A", "strategy_name": "s1", "pnl": 1.0},
            {"symbol": "B", "strategy_name": "s2", "pnl": -0.5},
            {"symbol": "C", "strategy_name": "s1", "pnl": 2.0},
        ],
    )
    body = c.get("/api/journal?last_n=2").json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert [t["symbol"] for t in body["trades"]] == ["B", "C"]


def test_journal_strategy_filter(client):
    c, tmp_path = client
    _write_journal(
        tmp_path,
        [
            {"symbol": "A", "strategy_name": "s1", "pnl": 1.0},
            {"symbol": "B", "strategy_name": "s2", "pnl": -0.5},
        ],
    )
    body = c.get("/api/journal?strategy=s2").json()
    assert body["count"] == 1
    assert body["trades"][0]["symbol"] == "B"


def test_journal_missing_file_is_404(client):
    c, _ = client
    r = c.get("/api/journal")
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_training_state_roundtrip(client):
    c, tmp_path = client
    payload = {"cycle_count": 7, "history": []}
    (tmp_path / "data" / "training_state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    body = c.get("/api/training").json()
    assert body["ok"] is True
    assert body["data"] == payload


def test_training_missing_is_404(client):
    c, _ = client
    assert c.get("/api/training").status_code == 404


def test_win_rates_raw_passthrough(client):
    c, tmp_path = client
    raw = {"StratA": {"window": [1, 0, 1], "updated": "2026-08-30T00:00:00"}}
    (tmp_path / "data" / "strategy_win_rates.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )
    body = c.get("/api/win_rates").json()
    assert body["ok"] is True
    assert body["data"] == raw


def test_rolling_stats_windows_and_breakdown(client):
    c, tmp_path = client
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=50)).isoformat()
    _write_journal(
        tmp_path,
        [
            {"strategy_name": "s1", "pnl": 2.0, "exit_time": recent},
            {"strategy_name": "s1", "pnl": -1.0, "exit_time": recent},
            {"strategy_name": "s2", "pnl": 5.0, "exit_time": old},
        ],
    )
    body = c.get("/api/stats/rolling?hours=24").json()
    assert body["ok"] is True
    assert body["trades"] == 2
    assert body["pnl"] == 1.0
    assert body["wins"] == 1 and body["losses"] == 1
    assert body["by_strategy"]["s1"]["trades"] == 2
    assert "s2" not in body["by_strategy"]


def test_rolling_stats_empty_journal(client):
    c, _ = client
    body = c.get("/api/stats/rolling").json()
    assert body["ok"] is True
    assert body["trades"] == 0
    assert body["ev_per_trade"] == 0


def test_log_tail_filters_secrets(client):
    c, tmp_path = client
    (tmp_path / "logs" / "run.log").write_text(
        "line one\nAPI_KEY=abc123 leaked\nline three\n", encoding="utf-8"
    )
    body = c.get("/api/logs/tail?pattern=*.log&lines=10").json()
    assert body["ok"] is True
    assert body["file"] == "run.log"
    assert "API_KEY" not in body["content"]
    assert "line one" in body["content"] and "line three" in body["content"]


def test_log_tail_rejects_traversal(client):
    c, _ = client
    assert c.get("/api/logs/tail?pattern=../secrets.txt").status_code == 400


def test_log_tail_no_match_is_404(client):
    c, _ = client
    assert c.get("/api/logs/tail?pattern=nope-*.log").status_code == 404
