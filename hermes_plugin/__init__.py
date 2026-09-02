"""Money Printer trading system integration plugin for Hermes Agent.

Provides 15 tools (prefixed mp_*) that let the agent monitor and control
the trading system via its HTTP API (localhost:8050) and data files, plus the
two strategy-factory readers (mp_factory_status / mp_factory_board) over
$MONEY_PRINTER_FACTORY_DIR (PRD_STRATEGY_FACTORY FR-F1.6).

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
    # Control-plane auth: when the sandbox sets MP_CONTROL_TOKEN, its POST
    # routes require a matching X-MP-Token header (401 otherwise). GET routes
    # never require it. Read per-call so a token set after plugin import works.
    headers = {}
    token = os.getenv("MP_CONTROL_TOKEN", "")
    if token:
        headers["X-MP-Token"] = token
    try:
        r = requests.post(f"{DASHBOARD_URL}{path}", headers=headers, timeout=TIMEOUT)
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


def _api_get_json(path):
    """GET a dashboard API path and return the parsed body, or None.

    Remote-deployment fallback: when the sandbox runs on another host
    (MONEY_PRINTER_URL=http://maia.local:8050), its data files are not on
    this filesystem, but the dashboard serves them — see src/web/server.py.
    """
    try:
        r = requests.get(f"{DASHBOARD_URL}{path}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


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
    if not os.path.exists(TRAINING_STATE_PATH):
        body = _api_get_json("/api/training")
        if body is not None:
            return json.dumps(body)
    return _read_json_file(TRAINING_STATE_PATH)


def get_win_rates(params):
    """Per-strategy historical win rates.

    Since the Phase 0 pivot (PRD FR-0.6) strategy_win_rates.json stores a
    recency WINDOW per strategy:

        {"Strategy": {"window": [1, 0, ...], "updated": iso}}

    Old files may still contain legacy ``[wins, total]`` cumulative entries;
    the RiskManager ignores those on load, so they are surfaced here with
    format="legacy" for visibility only. Each strategy is normalized to:

        {"win_rate": float|None, "wins": int, "n": int,
         "format": "window"|"legacy"|"unknown", "updated": iso|None}
    """
    try:
        with open(WIN_RATES_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        body = _api_get_json("/api/win_rates")
        if body is not None and body.get("ok") and isinstance(body.get("data"), dict):
            raw = body["data"]
        else:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"File not found: {os.path.basename(WIN_RATES_PATH)}",
                }
            )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

    if not isinstance(raw, dict):
        return json.dumps({"ok": False, "error": "win-rates file is not a JSON object"})

    out = {}
    for name, value in raw.items():
        try:
            if isinstance(value, dict) and isinstance(value.get("window"), list):
                window = [1 if x else 0 for x in value["window"]]
                n = len(window)
                wins = sum(window)
                out[name] = {
                    "win_rate": round(wins / n, 4) if n else None,
                    "wins": wins,
                    "n": n,
                    "format": "window",
                    "updated": value.get("updated"),
                }
            elif (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and all(isinstance(x, (int, float)) for x in value)
            ):
                wins, total = int(value[0]), int(value[1])
                out[name] = {
                    "win_rate": round(wins / total, 4) if total else None,
                    "wins": wins,
                    "n": total,
                    # Ignored by the RiskManager since FR-0.6 (pivot reset).
                    "format": "legacy",
                    "updated": None,
                }
            else:
                out[name] = {
                    "win_rate": None,
                    "wins": 0,
                    "n": 0,
                    "format": "unknown",
                    "updated": None,
                }
        except Exception:
            out[name] = {
                "win_rate": None,
                "wins": 0,
                "n": 0,
                "format": "unknown",
                "updated": None,
            }
    return json.dumps({"ok": True, "data": out})


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
        query = f"/api/journal?last_n={last_n}"
        if strategy:
            query += f"&strategy={strategy}"
        body = _api_get_json(query)
        if body is not None:
            return json.dumps(body)
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
            body = _api_get_json(f"/api/stats/rolling?hours={hours}")
            if body is not None:
                return json.dumps(body)
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
    """Check if the trading system process is alive and API responding.

    Prefers GET /healthz — the zero-side-effect probe (no snapshot, no CSV
    writes) that returns {"status": "ok", "uptime_s": <float>}. A 404 means an
    older sandbox without the route, so it falls back to /api/status.
    """
    try:
        r = requests.get(f"{DASHBOARD_URL}/healthz", timeout=5)
        endpoint = "/healthz"
        uptime_s = None
        if r.status_code == 404:
            r = requests.get(f"{DASHBOARD_URL}/api/status", timeout=5)
            endpoint = "/api/status"
        else:
            try:
                uptime_s = r.json().get("uptime_s")
            except ValueError:
                pass
        body = {
            "ok": True,
            "api_reachable": True,
            "endpoint": endpoint,
            "status_code": r.status_code,
            "response_time_ms": int(r.elapsed.total_seconds() * 1000),
        }
        if uptime_s is not None:
            body["uptime_s"] = uptime_s
        return json.dumps(body)
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
            body = _api_get_json(f"/api/logs/tail?pattern={pattern}&lines={n_lines}")
            if body is not None:
                return json.dumps(body)
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


def get_claude_usage(params):
    """Compute Claude API token usage from Hermes session files."""
    try:
        hours = params.get("hours", 24)
        sessions_dir = os.path.expanduser("~/.hermes/sessions")

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=hours
        )

        session_files = sorted(
            glob.glob(os.path.join(sessions_dir, "session_*.json")),
            key=os.path.getmtime,
            reverse=True,
        )

        by_model = defaultdict(
            lambda: {
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            }
        )
        by_platform = defaultdict(lambda: {"sessions": 0, "total_tokens": 0})
        total_sessions = 0
        total_messages = 0

        for sf in session_files:
            try:
                with open(sf) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            start = data.get("session_start", data.get("created_at", ""))
            if not start:
                continue
            try:
                dt = datetime.fromisoformat(start.replace("Z", ""))
            except ValueError:
                continue
            if dt < cutoff:
                continue

            model = data.get("model", "unknown")
            platform = data.get("platform", "unknown")
            msg_count = data.get("message_count", 0)
            if isinstance(msg_count, int):
                total_messages += msg_count

            msgs = data.get("messages", [])
            session_input = 0
            session_output = 0
            session_cache_read = 0
            session_cache_write = 0

            for msg in msgs:
                usage = msg.get("usage", {})
                if usage:
                    session_input += usage.get("input_tokens", 0)
                    session_output += usage.get("output_tokens", 0)
                    session_cache_read += usage.get(
                        "cache_read_input_tokens", usage.get("cache_read_tokens", 0)
                    )
                    session_cache_write += usage.get(
                        "cache_creation_input_tokens",
                        usage.get("cache_write_tokens", 0),
                    )

            session_total = (
                session_input
                + session_output
                + session_cache_read
                + session_cache_write
            )

            by_model[model]["sessions"] += 1
            by_model[model]["input_tokens"] += session_input
            by_model[model]["output_tokens"] += session_output
            by_model[model]["cache_read_tokens"] += session_cache_read
            by_model[model]["cache_write_tokens"] += session_cache_write
            by_model[model]["total_tokens"] += session_total

            by_platform[platform]["sessions"] += 1
            by_platform[platform]["total_tokens"] += session_total

            total_sessions += 1

        grand_total = sum(m["total_tokens"] for m in by_model.values())

        return json.dumps(
            {
                "ok": True,
                "hours": hours,
                "sessions": total_sessions,
                "messages": total_messages,
                "total_tokens": grand_total,
                "by_model": dict(by_model),
                "by_platform": dict(by_platform),
                "billing_mode": "subscription (Claude Max)"
                if not os.getenv("ANTHROPIC_API_KEY")
                else "api_key",
            }
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


# ── Strategy factory (F1) ─────────────────────────────────────────


def _factory_dir():
    """reports/factory root. Read at CALL time so the gateway env wins.

    On alcyone the checkout is /home/jushoya/projects/money_printer, so
    MONEY_PRINTER_FACTORY_DIR must be set explicitly there (the ~/money_printer
    default of _PROJECT does not exist on that host).
    """
    return os.getenv(
        "MONEY_PRINTER_FACTORY_DIR",
        os.path.join(
            os.getenv("MONEY_PRINTER_DIR", _PROJECT), "reports", "factory"
        ),
    )


def _factory_load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _factory_latest():
    """(latest_dict, error_string). Never raises."""
    root = _factory_dir()
    path = os.path.join(root, "latest.json")
    if not os.path.isdir(root):
        return None, f"factory dir not found: {root} (set MONEY_PRINTER_FACTORY_DIR)"
    if not os.path.exists(path):
        return None, f"no factory run yet: {path} missing"
    try:
        return _factory_load_json(path), None
    except Exception as e:  # malformed pointer
        return None, f"latest.json unreadable: {e}"


def get_factory_status(params):
    """Headline numbers of the latest factory run (latest.json -> summary.json)."""
    latest, err = _factory_latest()
    if err:
        return json.dumps({"ok": False, "error": err})
    root = _factory_dir()
    rel = latest.get("summary")
    if not rel:
        return json.dumps({"ok": False, "error": "latest.json has no 'summary' pointer"})
    try:
        summary = _factory_load_json(os.path.join(root, rel))
    except Exception as e:
        return json.dumps({"ok": False, "error": f"summary unreadable ({rel}): {e}"})

    seeds = summary.get("seeds") or {}

    def _row(r):
        r = r or {}
        return {
            k: r.get(k)
            for k in ("trades", "dates", "realized", "boot_lo", "boot_hi", "fit", "constraint_reason")
        }

    def _seed(name):
        s = seeds.get(name) or {}
        camps = s.get("campaigns") or {}
        return {
            "parity_full": _row(s.get("parity_full")),
            "search_full": _row(s.get("search_full")),
            "validation": {c: _row((camps.get(c) or {}).get("validation")) for c in ("A", "B", "C") if c in camps},
            "phenotype_hash": s.get("phenotype_hash"),
            "notes": s.get("notes"),
        }

    fr = (seeds.get("fr31a_taker") or {}).get("parity_full") or {}
    ref = (seeds.get("fr31a_taker") or {}).get("reference") or {}
    parity_line = {
        "expected": {"trades": 181, "dates": 65, "realized": 0.0636},
        "kernel": {k: fr.get(k) for k in ("trades", "dates", "realized", "boot_lo", "boot_hi")},
        "matches_1e9": ref.get("matches_1e9"),
        "fields_differing": ref.get("fields_differing", []),
    }
    body = {
        "ok": True,
        "factory_dir": root,
        "run_id": summary.get("run_id", latest.get("run_id")),
        "kind": summary.get("kind", latest.get("kind")),
        "family": summary.get("family", latest.get("family")),
        "registry_status": (summary.get("registry_line") or {}).get("status"),
        "git_rev": summary.get("git_rev"),
        "parity_fr31a": parity_line,
        "seeds": {n: _seed(n) for n in seeds},
        "brier_skill_vs_market": summary.get("brier_skill_vs_market"),
        "throughput": summary.get("throughput"),
        "summary_path": rel,
        "phase_note": "F1 gen-0: seeds only; evolution/RC/Holm/controls arrive in F2",
    }
    return json.dumps(body)


def get_factory_board(params):
    """board.md of the latest factory run plus coverage.json."""
    latest, err = _factory_latest()
    if err:
        return json.dumps({"ok": False, "error": err})
    root = _factory_dir()
    rel = latest.get("board")
    if not rel:
        return json.dumps({"ok": False, "error": "latest.json has no 'board' pointer"})
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            board = f.read()
    except Exception as e:
        return json.dumps({"ok": False, "error": f"board unreadable ({rel}): {e}"})
    coverage = None
    cov_path = os.path.join(root, "coverage.json")
    if os.path.exists(cov_path):
        try:
            coverage = _factory_load_json(cov_path)
        except Exception as e:
            coverage = {"error": f"coverage.json unreadable: {e}"}
    return json.dumps(
        {
            "ok": True,
            "run_id": latest.get("run_id"),
            "board_path": rel,
            "board_md": board,
            "coverage": coverage,
        }
    )


# ── Tool schemas ──────────────────────────────────────────────────

TOOLS = {
    "mp_factory_status": {
        "schema": {
            "name": "mp_factory_status",
            "description": "Headline numbers of the latest strategy-factory run (reports/factory/latest.json -> summary.json): run id, kind, family, registry status, per-seed parity/search/validation rows (fr31a parity line vs 181/65/+0.0636, nofilter_no, mlweather_fallback), frame-level Brier skill vs market, throughput. Offline lab artefact on alcyone; never a trading signal.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_factory_status,
    },
    "mp_factory_board": {
        "schema": {
            "name": "mp_factory_board",
            "description": "The strategy-factory board (board.md of the latest run: one row per lane — status, family, pick, pooled OOS, p_RC, Holm p, vs no-filter, vs fr31a, N phenotypes, controls, coverage — plus the PAPER row) and reports/factory/coverage.json. Post the markdown as-is.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": get_factory_board,
    },
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
            "description": "Activate a bot so it participates in the market loop. Confirm with the user before executing. Available: weather (paper-trades in the sandbox sim), gas, mention, crypto_annual, tweets (feed-only harvesters). Sends X-MP-Token from MP_CONTROL_TOKEN when the sandbox requires it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_name": {
                        "type": "string",
                        "description": "Bot to activate",
                        "enum": ["weather", "gas", "mention", "crypto_annual", "tweets"],
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
            "description": "Deactivate a bot (it stops ticking but stays registered). Confirm with the user before executing. Available: weather, gas, mention, crypto_annual, tweets. Sends X-MP-Token from MP_CONTROL_TOKEN when the sandbox requires it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bot_name": {
                        "type": "string",
                        "description": "Bot to deactivate",
                        "enum": ["weather", "gas", "mention", "crypto_annual", "tweets"],
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
            "description": "Get per-strategy historical win rates from the recency window (last 50 closed trades per strategy: win_rate, wins, sample count n, last-updated). Legacy cumulative entries appear as format=legacy and are ignored by the risk manager.",
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
            "description": "Check if the trading system API is reachable and responding. Probes the zero-side-effect /healthz route (uptime_s included when available; falls back to /api/status on older sandboxes). Returns status, response time, and error details if down.",
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
    "mp_claude_usage": {
        "schema": {
            "name": "mp_claude_usage",
            "description": "Get Claude API token usage from Hermes session history. Shows total tokens, per-model breakdown (Opus vs Sonnet), per-platform breakdown (cron vs discord vs cli), and billing mode (subscription vs API key).",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Lookback window in hours (default 24)",
                    }
                },
                "required": [],
            },
        },
        "handler": get_claude_usage,
    },
}


# ── Plugin registration ───────────────────────────────────────────

TOOLSET = "money-printer"


def register(ctx):
    """Register all Money Printer tools with Hermes."""
    for name, tool in TOOLS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=tool["schema"],
            handler=lambda params, _h=tool["handler"], **kw: _h(params),
            description=tool["schema"].get("description", ""),
        )
