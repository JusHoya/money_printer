"""Money Printer trading system integration plugin for Hermes Agent.

Provides 12 tools (prefixed mp_*) that let the agent monitor and control
the trading system via its HTTP API (localhost:8050) and data files.

Deploy:  ln -sf ~/money_printer/hermes_plugin ~/.hermes/plugins/money-printer
"""

import json
import os
import glob
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ── Configuration ─────────────────────────────────────────────────

DASHBOARD_URL = os.getenv("MONEY_PRINTER_URL", "http://localhost:8050")

# Paths — relative on local dev, absolute on VM via expanduser
_PROJECT = os.getenv(
    "MONEY_PRINTER_DIR",
    os.path.expanduser("~/money_printer"),
)
TRADE_JOURNAL_PATH = os.path.join(_PROJECT, "data", "trade_journal.jsonl")
TRAINING_STATE_PATH = os.path.join(_PROJECT, "data", "training_state.json")
WIN_RATES_PATH = os.path.join(_PROJECT, "data", "strategy_win_rates.json")
LOGS_DIR = os.path.join(_PROJECT, "logs")

TIMEOUT = 15
SECRET_KEYWORDS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE")


# ── HTTP helpers ──────────────────────────────────────────────────


def _api_get(path):
    try:
        r = requests.get(f"{DASHBOARD_URL}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return json.dumps({"ok": True, "data": r.json()})
    except requests.ConnectionError:
        return json.dumps(
            {"ok": False, "error": "Trading system unreachable (connection refused)"}
        )
    except requests.Timeout:
        return json.dumps(
            {"ok": False, "error": f"Trading system timed out ({TIMEOUT}s)"}
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def _api_post(path):
    try:
        r = requests.post(f"{DASHBOARD_URL}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return json.dumps({"ok": True, "data": r.json()})
    except requests.ConnectionError:
        return json.dumps(
            {"ok": False, "error": "Trading system unreachable (connection refused)"}
        )
    except requests.Timeout:
        return json.dumps(
            {"ok": False, "error": f"Trading system timed out ({TIMEOUT}s)"}
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def _read_json_file(path):
    try:
        with open(path) as f:
            return json.dumps({"ok": True, "data": json.load(f)})
    except FileNotFoundError:
        return json.dumps(
            {"ok": False, "error": f"File not found: {os.path.basename(path)}"}
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


# ── Tool implementations ─────────────────────────────────────────


def get_status(params):
    """Full dashboard snapshot: portfolio, positions, market data, alerts,
    strategy stats, cycle history, training diagnostics."""
    return _api_get("/api/status")


def get_bots(params):
    """List all bots with active/inactive status."""
    return _api_get("/api/bots")


def start_bot(params):
    """Activate a trading bot."""
    name = params.get("bot_name", "")
    return _api_post(f"/api/bots/{name}/start")


def stop_bot(params):
    """Deactivate a trading bot."""
    name = params.get("bot_name", "")
    return _api_post(f"/api/bots/{name}/stop")


def get_session_log(params):
    """Last 50 lines of the session log."""
    return _api_get("/api/logs/session")


def get_data_log(params):
    """Last 100 rows of market data CSV."""
    return _api_get("/api/logs/data")


def get_training_state(params):
    """ML training state: cycle count, cycle history, diagnostics."""
    return _read_json_file(TRAINING_STATE_PATH)


def get_win_rates(params):
    """Per-strategy historical win rates."""
    return _read_json_file(WIN_RATES_PATH)


def read_trade_journal(params):
    """Read recent trades with optional filtering."""
    try:
        last_n = min(params.get("last_n", 50), 500)
        strategy = params.get("strategy_filter", "")

        trades = []
        with open(TRADE_JOURNAL_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if strategy:
            trades = [t for t in trades if t.get("strategy_name", "") == strategy]

        trades = trades[-last_n:]
        return json.dumps({"ok": True, "count": len(trades), "trades": trades})
    except FileNotFoundError:
        return json.dumps({"ok": False, "error": "trade_journal.jsonl not found"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def compute_rolling_stats(params):
    """Compute rolling PnL, win rate, EV, and per-strategy breakdown."""
    try:
        hours = params.get("hours", 24)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=hours
        )

        trades = []
        try:
            with open(TRADE_JOURNAL_PATH) as f:
                for line in f:
                    try:
                        t = json.loads(line.strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    et = t.get("exit_time") or t.get("entry_time", "")
                    if not et:
                        continue
                    try:
                        dt = datetime.fromisoformat(et.replace("Z", ""))
                    except ValueError:
                        continue
                    if dt > cutoff:
                        trades.append(t)
        except FileNotFoundError:
            return json.dumps(
                {
                    "ok": True,
                    "hours": hours,
                    "trades": 0,
                    "pnl": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0,
                    "ev_per_trade": 0,
                    "by_strategy": {},
                }
            )

        # Overall stats
        pnls = [t.get("pnl", 0) or 0 for t in trades]
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        n = len(trades)
        wr = (wins / n * 100) if n else 0
        ev = (total_pnl / n) if n else 0

        # Per-strategy breakdown
        by_strat = defaultdict(list)
        for t in trades:
            by_strat[t.get("strategy_name", "?")].append(t.get("pnl", 0) or 0)

        strat_stats = {}
        for name, strat_pnls in sorted(by_strat.items(), key=lambda x: -sum(x[1])):
            sn = len(strat_pnls)
            sw = sum(1 for p in strat_pnls if p > 0)
            strat_stats[name] = {
                "trades": sn,
                "pnl": round(sum(strat_pnls), 2),
                "win_rate": round(sw / sn * 100, 1) if sn else 0,
            }

        return json.dumps(
            {
                "ok": True,
                "hours": hours,
                "trades": n,
                "pnl": round(total_pnl, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wr, 1),
                "ev_per_trade": round(ev, 4),
                "by_strategy": strat_stats,
            }
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def check_health(params):
    """Check if the trading system process is alive and API responding."""
    try:
        r = requests.get(f"{DASHBOARD_URL}/api/status", timeout=5)
        return json.dumps(
            {
                "ok": True,
                "api_reachable": True,
                "status_code": r.status_code,
                "response_time_ms": int(r.elapsed.total_seconds() * 1000),
            }
        )
    except requests.ConnectionError:
        return json.dumps(
            {
                "ok": True,
                "api_reachable": False,
                "error": "Connection refused — trading system may be down",
            }
        )
    except requests.Timeout:
        return json.dumps(
            {
                "ok": True,
                "api_reachable": False,
                "error": "API timed out (5s) — trading system may be hung",
            }
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def read_log_tail(params):
    """Read tail of the most recent log file matching a glob pattern."""
    try:
        pattern = params.get("pattern", "*.log")
        n_lines = min(params.get("lines", 100), 500)

        matches = sorted(
            glob.glob(os.path.join(LOGS_DIR, pattern)),
            key=os.path.getmtime,
            reverse=True,
        )
        if not matches:
            return json.dumps(
                {"ok": False, "error": f"No files matching '{pattern}' in logs/"}
            )

        path = matches[0]
        with open(path) as f:
            lines = f.readlines()

        # Sanitize: strip lines containing secrets
        filtered = [
            line
            for line in lines[-n_lines:]
            if not any(kw in line.upper() for kw in SECRET_KEYWORDS)
        ]

        return json.dumps(
            {
                "ok": True,
                "file": os.path.basename(path),
                "lines": len(filtered),
                "content": "".join(filtered),
            }
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


# ── Tool schemas ──────────────────────────────────────────────────

TOOLS = {
    "mp_status": {
        "schema": {
            "name": "mp_status",
            "description": "Get full Money Printer trading system snapshot: portfolio equity, positions, market data, strategy stats, cycle history, training diagnostics.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_status,
    },
    "mp_bots": {
        "schema": {
            "name": "mp_bots",
            "description": "List all trading bots and their active/inactive status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_bots,
    },
    "mp_start_bot": {
        "schema": {
            "name": "mp_start_bot",
            "description": "Activate a trading bot so it participates in the market loop. Confirm with the user before executing. Available: btc_15m, btc_hourly, weather.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_name": {
                        "type": "string",
                        "description": "Bot to activate",
                        "enum": ["btc_15m", "btc_hourly", "weather"],
                    }
                },
                "required": ["bot_name"],
            },
        },
        "handler": start_bot,
    },
    "mp_stop_bot": {
        "schema": {
            "name": "mp_stop_bot",
            "description": "Deactivate a trading bot (it stops ticking but stays registered). Confirm with the user before executing. Available: btc_15m, btc_hourly, weather.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_name": {
                        "type": "string",
                        "description": "Bot to deactivate",
                        "enum": ["btc_15m", "btc_hourly", "weather"],
                    }
                },
                "required": ["bot_name"],
            },
        },
        "handler": stop_bot,
    },
    "mp_journal": {
        "schema": {
            "name": "mp_journal",
            "description": "Read trade outcomes from the persistent journal. Each entry has: symbol, strategy_name, entry/exit times and prices, pnl, close_reason, model_probability, edge_at_entry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "last_n": {
                        "type": "integer",
                        "description": "Number of most recent trades to return (default 50, max 500)",
                    },
                    "strategy_filter": {
                        "type": "string",
                        "description": "Only return trades from this strategy",
                    },
                },
                "required": [],
            },
        },
        "handler": read_trade_journal,
    },
    "mp_rolling_stats": {
        "schema": {
            "name": "mp_rolling_stats",
            "description": "Compute rolling performance statistics: total PnL, win rate, EV per trade, and per-strategy breakdown over a time window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Rolling window in hours (default 24)",
                    }
                },
                "required": [],
            },
        },
        "handler": compute_rolling_stats,
    },
    "mp_training": {
        "schema": {
            "name": "mp_training",
            "description": "Get ML training state: cycle count, cycle history (last 20), training diagnostics (AUC, sample count, feature names).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_training_state,
    },
    "mp_win_rates": {
        "schema": {
            "name": "mp_win_rates",
            "description": "Get per-strategy historical win rates (wins/total counts, persisted across cycle resets).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_win_rates,
    },
    "mp_session_log": {
        "schema": {
            "name": "mp_session_log",
            "description": "Get last 50 lines of the current session log. Shows system events, alerts, trade executions, heartbeats.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_session_log,
    },
    "mp_data_log": {
        "schema": {
            "name": "mp_data_log",
            "description": "Get last 100 rows of market data CSV. Each row has timestamp, symbol, price, bid, ask, volume, indicator values.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_data_log,
    },
    "mp_health": {
        "schema": {
            "name": "mp_health",
            "description": "Check if the trading system API is reachable and responding. Returns status, response time, and error details if down.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": check_health,
    },
    "mp_log_tail": {
        "schema": {
            "name": "mp_log_tail",
            "description": "Read the tail of a log file from the trading system logs/ directory. Matches the most recent file by glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern for the log file (e.g. '*.log', 'money_printer_*.log', 'watchdog_*.log')",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of lines from end to return (default 100, max 500)",
                    },
                },
                "required": ["pattern"],
            },
        },
        "handler": read_log_tail,
    },
}


# ── Plugin registration ───────────────────────────────────────────


def register(ctx):
    """Register all Money Printer tools with Hermes."""
    for name, tool in TOOLS.items():
        ctx.register_tool(name, tool["schema"], tool["handler"])
