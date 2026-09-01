"""Tests for the remote-monitoring API routes added for the split
alcyone/maia deployment (src/web/server.py): /api/journal, /api/training,
/api/win_rates, /api/stats/rolling, /api/logs/tail — plus the side-effect
free /healthz probe and /api/portfolio_history.

These routes are what the Hermes agent's mp_* tools fall back to when the
sandbox data files are not on the agent's own filesystem.
"""

import csv
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import src.web.server as server_mod
from src.web.server import create_app


class _StubStateManager:
    def __init__(self):
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return {"portfolio": {}, "positions": []}


class _StubOrchestrator:
    bots = []
    uptime_seconds = 42.5


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    sm = _StubStateManager()
    orch = _StubOrchestrator()
    app = create_app(sm, orch)
    with TestClient(app) as c:
        c.sm = sm  # exposed for side-effect assertions
        c.orch = orch  # exposed so tests can stamp market-loop liveness
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


def test_rolling_stats_handles_tz_aware_timestamps(client):
    """Journal timestamps arrive as naive, '+00:00'-offset AND 'Z'-suffixed
    strings; all three must be parsed (naive treated as UTC), none dropped."""
    c, tmp_path = client
    now = datetime.now(timezone.utc)
    recent_naive = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    recent_offset = (now - timedelta(hours=2)).isoformat()  # ends '+00:00'
    recent_z = (now - timedelta(hours=3)).replace(tzinfo=None).isoformat() + "Z"
    old_offset = (now - timedelta(hours=50)).isoformat()
    _write_journal(
        tmp_path,
        [
            {"strategy_name": "s1", "pnl": 1.0, "exit_time": recent_naive},
            {"strategy_name": "s1", "pnl": 2.0, "exit_time": recent_offset},
            {"strategy_name": "s1", "pnl": 3.0, "exit_time": recent_z},
            {"strategy_name": "s2", "pnl": 5.0, "exit_time": old_offset},
        ],
    )
    body = c.get("/api/stats/rolling?hours=24").json()
    assert body["ok"] is True
    assert body["trades"] == 3
    assert body["pnl"] == 6.0
    assert "s2" not in body["by_strategy"]


# ---------------------------------------------------------------------------
# /healthz — side-effect-free liveness probe
# ---------------------------------------------------------------------------


def test_healthz_contract(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_s"], float)
    assert body["uptime_s"] == 42.5


def test_healthz_never_calls_snapshot(client):
    c, _ = client
    for _ in range(3):
        c.get("/healthz")
    assert c.sm.snapshot_calls == 0


def test_healthz_fresh_loop_stamp_is_ok(client):
    c, _ = client
    c.orch._last_loop_pass_monotonic = time.monotonic()
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_stale_loop_stamp_is_503(client):
    c, _ = client
    c.orch._last_loop_pass_monotonic = time.monotonic() - 1000.0
    r = c.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "stale"
    assert body["loop_age_s"] > 900
    assert body["uptime_s"] == 42.5


def test_healthz_without_loop_stamp_is_ok(client):
    # Test stubs and TUI-less contexts never stamp the loop: stay 200.
    c, _ = client
    assert not hasattr(c.orch, "_last_loop_pass_monotonic")
    assert c.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# /api/portfolio_history
# ---------------------------------------------------------------------------

_PORTFOLIO_HEADER = [
    "Timestamp",
    "Equity",
    "Cash",
    "Exposure",
    "Realized_PnL",
    "Unrealized_PnL",
]


def _write_portfolio_csv(tmp_path, name, rows, subdir="logs"):
    path = tmp_path / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_PORTFOLIO_HEADER)
        for r in rows:
            w.writerow(r)


def test_portfolio_history_merges_sessions_and_sorts(client):
    c, tmp_path = client
    now = datetime.now()
    t0 = (now - timedelta(minutes=30)).isoformat()
    t1 = (now - timedelta(minutes=20)).isoformat()
    t2 = (now - timedelta(minutes=10)).isoformat()
    # Rows deliberately spread across two session files, newest row in the
    # OLDER file so a correct merge must sort across files.
    _write_portfolio_csv(
        tmp_path,
        "portfolio_20260901_000000.csv",
        [[t0, "100.0", "70.0", "30.0", "0", "0"], [t2, "102.0", "72.0", "30.0", "0", "0"]],
    )
    _write_portfolio_csv(
        tmp_path,
        "portfolio_20260901_010000.csv",
        [[t1, "101.0", "71.0", "30.0", "0", "0"]],
    )
    body = c.get("/api/portfolio_history?hours=24").json()
    assert body["count"] == 3
    assert len(body["history"]) == 3
    assert [p["equity"] for p in body["history"]] == [100.0, 101.0, 102.0]
    first = body["history"][0]
    # ts is epoch seconds (float) — naive CSV stamps read as local wall clock
    assert first["ts"] == datetime.fromisoformat(t0).timestamp()
    assert first["cash"] == 70.0
    assert first["exposure"] == 30.0


def test_portfolio_history_window_filter(client):
    c, tmp_path = client
    now = datetime.now()
    recent = (now - timedelta(minutes=30)).isoformat()
    old = (now - timedelta(hours=5)).isoformat()
    _write_portfolio_csv(
        tmp_path,
        "portfolio_20260901_000000.csv",
        [[old, "90.0", "90.0", "0.0", "0", "0"], [recent, "100.0", "100.0", "0.0", "0", "0"]],
    )
    body = c.get("/api/portfolio_history?hours=1").json()
    assert body["count"] == 1
    assert body["history"][0]["equity"] == 100.0


def test_portfolio_history_hours_clamped(client):
    c, tmp_path = client
    old = (datetime.now() - timedelta(hours=2)).isoformat()
    _write_portfolio_csv(
        tmp_path,
        "portfolio_20260901_000000.csv",
        [[old, "90.0", "90.0", "0.0", "0", "0"]],
    )
    # hours=0 clamps to 1 → the 2h-old row stays outside the window
    assert c.get("/api/portfolio_history?hours=0").json()["count"] == 0
    # absurdly large values clamp to 2160 and still succeed
    assert c.get("/api/portfolio_history?hours=999999").json()["count"] == 1


def test_portfolio_history_downsamples_to_1000_points(client):
    c, tmp_path = client
    base = datetime.now() - timedelta(hours=1)
    rows = [
        [(base + timedelta(seconds=i)).isoformat(), f"{100 + i * 0.01:.2f}", "100.0", "0.0", "0", "0"]
        for i in range(2050)
    ]
    _write_portfolio_csv(tmp_path, "portfolio_20260901_000000.csv", rows)
    body = c.get("/api/portfolio_history?hours=24").json()
    assert body["count"] <= 1000
    assert body["count"] == len(body["history"])
    # Still ascending after downsampling
    ts_list = [p["ts"] for p in body["history"]]
    assert ts_list == sorted(ts_list)


def test_portfolio_history_includes_archived_sessions(client):
    """Rows swept into logs/_archive/<...>/ by the startup/cycle/shutdown
    sweeps still feed the endpoint, and a file present in both logs/ and
    the archive (or in several archive subdirs) is counted exactly once."""
    c, tmp_path = client
    now = datetime.now()
    t0 = (now - timedelta(minutes=40)).isoformat()
    t1 = (now - timedelta(minutes=20)).isoformat()
    t2 = (now - timedelta(minutes=10)).isoformat()
    # A previous session that only survives in the archive.
    _write_portfolio_csv(
        tmp_path,
        "portfolio_20260831_000000.csv",
        [[t0, "95.0", "95.0", "0.0", "0", "0"]],
        subdir="logs/_archive/startup_20260901_000000",
    )
    # Current session: live in logs/ AND copied into two archive subdirs.
    live_rows = [
        [t1, "100.0", "70.0", "30.0", "0", "0"],
        [t2, "101.0", "71.0", "30.0", "0", "0"],
    ]
    _write_portfolio_csv(tmp_path, "portfolio_20260901_010000.csv", live_rows)
    for sub in (
        "logs/_archive/shutdown_20260901_020000",
        "logs/_archive/cycle_20260901_030000",
    ):
        _write_portfolio_csv(
            tmp_path, "portfolio_20260901_010000.csv", live_rows, subdir=sub
        )
    body = c.get("/api/portfolio_history?hours=24").json()
    assert body["count"] == 3
    assert [p["equity"] for p in body["history"]] == [95.0, 100.0, 101.0]


def test_portfolio_history_downsample_keeps_newest_row(client):
    c, tmp_path = client
    base = datetime.now() - timedelta(hours=1)
    stamps = [base + timedelta(seconds=i) for i in range(2048)]
    rows = [
        [t.isoformat(), f"{100 + i * 0.01:.2f}", "100.0", "0.0", "0", "0"]
        for i, t in enumerate(stamps)
    ]
    _write_portfolio_csv(tmp_path, "portfolio_20260901_000000.csv", rows)
    body = c.get("/api/portfolio_history?hours=24").json()
    # 2048 rows, step=3: [::3] ends at index 2046 — the newest row must be
    # re-appended, not dropped.
    assert body["history"][-1]["ts"] == stamps[-1].timestamp()
    ts_list = [p["ts"] for p in body["history"]]
    assert ts_list == sorted(ts_list)
    assert body["count"] == len(body["history"])


def test_portfolio_history_skips_malformed_rows(client):
    c, tmp_path = client
    recent = (datetime.now() - timedelta(minutes=5)).isoformat()
    _write_portfolio_csv(
        tmp_path,
        "portfolio_20260901_000000.csv",
        [
            ["not-a-timestamp", "100.0", "100.0", "0.0", "0", "0"],
            [recent, "not-a-float", "100.0", "0.0", "0", "0"],
            [recent, "101.0", "101.0", "0.0", "0", "0"],
        ],
    )
    body = c.get("/api/portfolio_history").json()
    assert body["count"] == 1
    assert body["history"][0]["equity"] == 101.0


def test_portfolio_history_empty_when_no_files(client):
    c, _ = client
    body = c.get("/api/portfolio_history").json()
    assert body == {"history": [], "count": 0}
