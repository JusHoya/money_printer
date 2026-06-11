#!/usr/bin/env python3
"""settlement_reconcile.py — Daily settlement reconciliation / alarm job.

2026-06-10 fix — permanent guard for the settlement bug.

WHY THIS EXISTS
---------------
A 2026-06-10 review found the simulator was booking FICTIONAL profit: the
binary-settlement path parsed the 15m-crypto ticker's minute-label suffix
(00/15/30/45) as a price strike, forcing near-100% auto-YES settlements. The
sim "won" trades that, in reality, settled NO. The matching-engine fix
(_close_position) stops generating new fiction, but nothing was *checking* the
sim's recorded outcomes against ground truth. This script is that check.

WHAT IT DOES
------------
For every settled/closed trade in the ledger (data/exchange_state.json
closed_trades and/or data/trade_journal.jsonl), it:
  1. Derives the outcome the SIM recorded (yes/no the contract settled).
  2. Fetches the contract's ACTUAL result from the public Kalshi V2 market
     endpoint  GET .../markets/{ticker}  (the `result` field is 'yes'/'no').
  3. Compares them, accumulating mismatch count, signed PnL delta, and a
     per-strategy breakdown.
  4. Caches every fetched result to data/settlement_cache.json so subsequent
     runs (and offline runs) don't refetch — same {ticker: {result,status}}
     shape the matching engine consumes.
  5. Prints a mismatch report and, if mismatches exceed --threshold, exits
     non-zero and (when DISCORD_WEBHOOK_URL is set) posts a Discord alert.

It degrades gracefully offline: anything already in settlement_cache.json is
reconciled even with no network; un-cached tickers are skipped (counted as
"unresolved") rather than crashing.

USAGE
-----
    python scripts/settlement_reconcile.py                 # reconcile, alert on breach
    python scripts/settlement_reconcile.py --threshold 5   # tolerate <=5 mismatches
    python scripts/settlement_reconcile.py --offline       # cache only, never hit network
    python scripts/settlement_reconcile.py --max-fetch 200 # cap network fetches this run
    python scripts/settlement_reconcile.py --json          # machine-readable summary

CRON (mirrors scripts/host_watchdog.sh — runs from the host crontab):
    # Once a day at 06:15 UTC; alert + non-zero exit on a settlement-truth breach.
    15 6 * * * cd /home/USER/money_printer && /usr/bin/python3 scripts/settlement_reconcile.py \
        >> /home/USER/money_printer/logs/settlement_reconcile.log 2>&1

Exit codes:
    0  reconciled, mismatches within threshold
    1  mismatches exceeded threshold (settlement-truth breach)
    2  usage / fatal error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

# requests is already a hard dependency of the project (used in discord.py and
# the Kalshi provider). Import lazily-tolerant so --offline still works if the
# environment is broken.
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
# Resolve repo root from this file so the script runs from anywhere (cron uses
# an absolute cwd, but a stray invocation shouldn't write the cache elsewhere).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_THIS_DIR)
DATA_DIR = os.path.join(REPO_ROOT, "data")

EXCHANGE_STATE_PATH = os.path.join(DATA_DIR, "exchange_state.json")
TRADE_JOURNAL_PATH = os.path.join(DATA_DIR, "trade_journal.jsonl")
SETTLEMENT_CACHE_PATH = os.path.join(DATA_DIR, "settlement_cache.json")

# The ONLY valid production endpoint (see CLAUDE.md "Kalshi API"). Public,
# no auth required for the markets read.
KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Terminal statuses where Kalshi has published a `result`.
_SETTLED_STATUSES = {"finalized", "settled", "closed"}

DEFAULT_THRESHOLD = 0  # any mismatch is a breach by default
DEFAULT_MAX_FETCH = 500  # politeness cap on network calls per run
FETCH_TIMEOUT = 10  # seconds per request
FETCH_PAUSE = 0.05  # gentle throttle between live fetches


# ---------------------------------------------------------------------------
# Logging (stdout, timestamped — matches host_watchdog.sh log() format)
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Ledger loading
# ---------------------------------------------------------------------------
def load_closed_trades(state_path: str | None = None) -> list[dict]:
    """Return the closed_trades list from exchange_state.json (or [])."""
    # Resolve the path at CALL time (not via a default arg) so tests can
    # monkeypatch the module-level EXCHANGE_STATE_PATH and have it take effect.
    if state_path is None:
        state_path = EXCHANGE_STATE_PATH
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log(f"WARN: could not parse {state_path}: {exc}")
        return []
    trades = state.get("closed_trades")
    return trades if isinstance(trades, list) else []


def load_journal_trades(journal_path: str | None = None) -> list[dict]:
    """Return all trade dicts from the JSONL journal (or [])."""
    # Resolve at CALL time so monkeypatching the module constant works (see
    # load_closed_trades).
    if journal_path is None:
        journal_path = TRADE_JOURNAL_PATH
    trades: list[dict] = []
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    return trades


# ---------------------------------------------------------------------------
# Sim outcome derivation
# ---------------------------------------------------------------------------
def sim_recorded_result(trade: dict) -> str | None:
    """Derive the YES/NO outcome the SIM recorded for a settled binary trade.

    Returns 'yes', 'no', or None (not a clean binary settlement we can judge).

    A binary settlement pays the contract holder 1.00 if their side won. We
    therefore reconstruct the *contract's* outcome from (contract_side, won)
    where `won` is taken from prediction_correct / pnl:

        contract_side YES, won  -> contract settled YES
        contract_side YES, lost -> contract settled NO
        contract_side NO,  won  -> contract settled NO   (NO holders win on NO)
        contract_side NO,  lost -> contract settled YES

    Trades closed for a non-settlement reason (stop-loss, profit-target, time
    limit) exited at a mid-book price, NOT at terminal 0/1, so they carry no
    settlement truth — we return None and skip them. We detect a true binary
    settlement primarily via close_reason, falling back to a terminal
    exit_price (~0.0 / ~1.0).
    """
    contract_side = (trade.get("contract_side") or "YES").upper()

    reason = (trade.get("close_reason") or trade.get("reason") or "").upper()
    is_settlement = ("EXPIRATION" in reason) or ("SETTLEMENT" in reason)

    exit_price = trade.get("exit_price")
    if not is_settlement and exit_price is not None:
        try:
            ep = float(exit_price)
            if ep <= 0.01 or ep >= 0.99:
                is_settlement = True
        except (TypeError, ValueError):
            pass

    if not is_settlement:
        return None

    # Determine whether the holder's side won.
    won = trade.get("prediction_correct")
    if won is None:
        pnl = trade.get("pnl")
        if pnl is None:
            return None
        try:
            won = float(pnl) > 0
        except (TypeError, ValueError):
            return None

    if contract_side == "NO":
        # NO holder wins  -> contract settled NO; loses -> settled YES.
        return "no" if won else "yes"
    # YES (default) holder wins -> settled YES; loses -> settled NO.
    return "yes" if won else "no"


# ---------------------------------------------------------------------------
# Settlement cache (shared shape with matching engine: {ticker: {result,status}})
# ---------------------------------------------------------------------------
def load_cache(path: str | None = None) -> dict:
    if path is None:
        path = SETTLEMENT_CACHE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"WARN: could not parse cache {path}: {exc}; starting empty")
        return {}


def save_cache(cache: dict, path: str | None = None) -> None:
    if path is None:
        path = SETTLEMENT_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Atomic-ish write so a crash mid-write can't corrupt the cache.
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        log(f"WARN: could not save cache {path}: {exc}")


# ---------------------------------------------------------------------------
# Actual-result lookup (network, with cache)
# ---------------------------------------------------------------------------
def fetch_actual_result(ticker: str) -> dict | None:
    """Fetch a single market's settled result from the public Kalshi V2 API.

    Returns {"result": "yes"/"no", "status": "<status>"} or None on any
    failure / non-terminal market. Network access is isolated here so tests can
    monkeypatch this one function (no real HTTP in tests).
    """
    if requests is None:
        return None
    url = f"{KALSHI_PROD_URL}/markets/{ticker}"
    try:
        resp = requests.get(
            url, headers={"Content-Type": "application/json"}, timeout=FETCH_TIMEOUT
        )
        if resp.status_code != 200:
            return None
        market = resp.json().get("market", {}) or {}
    except Exception:
        return None

    result = (market.get("result") or "").lower()
    status = market.get("status") or ""
    if result not in ("yes", "no"):
        # Not yet resolved (or a void/unknown result) — don't cache a guess.
        return None
    return {"result": result, "status": status}


def resolve_result(
    ticker: str,
    cache: dict,
    *,
    offline: bool,
    fetch_budget: list[int],
    fetcher=None,
) -> dict | None:
    """Return the actual {result,status} for a ticker, using cache then network.

    `fetch_budget` is a 1-element mutable list acting as a countdown of allowed
    live fetches this run; `fetcher` is injectable for tests. When not supplied
    it resolves to the module-level fetch_actual_result at CALL time, so tests
    that monkeypatch the module attribute take effect.
    """
    if fetcher is None:
        fetcher = fetch_actual_result
    cached = cache.get(ticker)
    if isinstance(cached, dict) and cached.get("result") in ("yes", "no"):
        return cached

    if offline or fetch_budget[0] <= 0:
        return None

    fetch_budget[0] -= 1
    actual = fetcher(ticker)
    if FETCH_PAUSE:
        time.sleep(FETCH_PAUSE)
    if actual is not None:
        cache[ticker] = actual
    return actual


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def reconcile(
    trades: list[dict],
    cache: dict,
    *,
    offline: bool = False,
    max_fetch: int = DEFAULT_MAX_FETCH,
    fetcher=None,
) -> dict:
    """Reconcile sim-recorded outcomes against actual results.

    Mutates `cache` in place with any newly fetched results. Returns a summary
    dict with totals, mismatch list, and per-strategy breakdown. `fetcher`
    defaults (at call time) to the module-level fetch_actual_result so tests can
    monkeypatch it.
    """
    if fetcher is None:
        fetcher = fetch_actual_result
    fetch_budget = [max_fetch]

    checked = 0
    unresolved = 0  # no actual result available (un-cached + offline/over budget)
    skipped = 0  # not a clean binary settlement we can judge
    mismatches: list[dict] = []
    pnl_delta = 0.0
    by_strategy: dict[str, dict] = defaultdict(
        lambda: {"checked": 0, "mismatches": 0, "pnl_delta": 0.0}
    )

    for trade in trades:
        ticker = trade.get("symbol") or trade.get("ticker")
        if not ticker:
            skipped += 1
            continue

        sim = sim_recorded_result(trade)
        if sim is None:
            skipped += 1
            continue

        actual = resolve_result(
            ticker, cache, offline=offline, fetch_budget=fetch_budget, fetcher=fetcher
        )
        if actual is None:
            unresolved += 1
            continue

        actual_result = actual.get("result")
        checked += 1
        strategy = trade.get("strategy_name", "Unknown")
        by_strategy[strategy]["checked"] += 1

        if sim != actual_result:
            try:
                pnl = float(trade.get("pnl") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            # The sim believed it earned `pnl` on a settlement that did not
            # actually happen that way. The fictional component is the recorded
            # pnl; surface it as the delta the ledger overstated.
            pnl_delta += pnl
            by_strategy[strategy]["mismatches"] += 1
            by_strategy[strategy]["pnl_delta"] += pnl
            mismatches.append(
                {
                    "ticker": ticker,
                    "strategy": strategy,
                    "sim_result": sim,
                    "actual_result": actual_result,
                    "recorded_pnl": pnl,
                    "contract_side": (trade.get("contract_side") or "YES"),
                    "exit_time": trade.get("exit_time") or trade.get("close_time"),
                }
            )

    return {
        "total_trades": len(trades),
        "checked": checked,
        "unresolved": unresolved,
        "skipped": skipped,
        "mismatch_count": len(mismatches),
        "pnl_delta": round(pnl_delta, 2),
        "mismatches": mismatches,
        "by_strategy": {k: dict(v) for k, v in by_strategy.items()},
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_report(summary: dict, *, threshold: int) -> str:
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("SETTLEMENT RECONCILIATION REPORT")
    lines.append("=" * 64)
    lines.append(f"  Trades scanned    : {summary['total_trades']}")
    lines.append(f"  Reconciled        : {summary['checked']}")
    lines.append(f"  Unresolved (no truth) : {summary['unresolved']}")
    lines.append(f"  Skipped (non-settle)  : {summary['skipped']}")
    lines.append(
        f"  MISMATCHES        : {summary['mismatch_count']}  (threshold {threshold})"
    )
    lines.append(f"  Overstated PnL    : ${summary['pnl_delta']:+.2f}")

    by_strat = summary.get("by_strategy", {})
    strat_with_mm = {k: v for k, v in by_strat.items() if v.get("mismatches", 0) > 0}
    if strat_with_mm:
        lines.append("")
        lines.append("  Per-strategy mismatches:")
        for name, st in sorted(
            strat_with_mm.items(), key=lambda kv: kv[1]["pnl_delta"]
        ):
            lines.append(
                f"    {name[:28]:28s} "
                f"{st['mismatches']:4d}/{st['checked']:<4d}  "
                f"${st['pnl_delta']:+9.2f}"
            )

    mm = summary.get("mismatches", [])
    if mm:
        lines.append("")
        lines.append("  Sample mismatches (up to 15):")
        for m in mm[:15]:
            lines.append(
                f"    {m['ticker']:34s} sim={m['sim_result']:3s} "
                f"actual={m['actual_result']:3s} "
                f"pnl=${m['recorded_pnl']:+8.2f}  [{m['strategy'][:18]}]"
            )
        if len(mm) > 15:
            lines.append(f"    ... and {len(mm) - 15} more")

    lines.append("=" * 64)
    return "\n".join(lines)


def post_discord_alert(summary: dict, threshold: int) -> None:
    """Post a settlement-breach alert to Discord if DISCORD_WEBHOOK_URL is set.

    Reuses the project's discord module for the webhook POST; degrades silently
    if the env var is unset or the module/network is unavailable.
    """
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        log("DISCORD_WEBHOOK_URL not set — skipping Discord alert")
        return

    host = ""
    try:
        import socket

        host = socket.gethostname()
    except Exception:
        host = "unknown-host"

    description = (
        f"**{summary['mismatch_count']} settlement mismatches** "
        f"(threshold {threshold})\n"
        f"Overstated PnL: **${summary['pnl_delta']:+.2f}**\n"
        f"Reconciled {summary['checked']} settled trades.\n"
        f"_This is the settlement-truth guard. Investigate the matching engine "
        f"settlement path and run scripts/reset_contaminated_state.py if the "
        f"ledger is contaminated._"
    )
    payload = {
        "embeds": [
            {
                "title": f"🚨 Settlement Reconciliation Breach [{host}]",
                "description": description,
                "color": 0xE74C3C,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    try:
        if requests is None:
            log("requests unavailable — cannot post Discord alert")
            return
        resp = requests.post(webhook, json=payload, timeout=10)
        if resp.status_code >= 400:
            log(f"Discord alert failed (HTTP {resp.status_code})")
        else:
            log("Discord alert sent")
    except Exception as exc:
        log(f"Discord alert failed: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reconcile sim-recorded settlements against actual Kalshi results."
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help="Max mismatches tolerated before exit-1 + Discord alert (default 0).",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Never hit the network; reconcile only what is already cached.",
    )
    p.add_argument(
        "--max-fetch",
        type=int,
        default=DEFAULT_MAX_FETCH,
        help=f"Cap live result fetches this run (default {DEFAULT_MAX_FETCH}).",
    )
    p.add_argument(
        "--source",
        choices=["state", "journal", "both"],
        default="both",
        help="Which ledger to reconcile (default both, deduped by ticker+time).",
    )
    p.add_argument(
        "--no-discord",
        action="store_true",
        help="Suppress the Discord alert even on a breach.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON summary instead of the text report.",
    )
    return p


def _gather_trades(source: str) -> list[dict]:
    """Load trades from the requested ledger(s), deduped by (ticker, exit_time)."""
    trades: list[dict] = []
    if source in ("state", "both"):
        trades.extend(load_closed_trades())
    if source in ("journal", "both"):
        trades.extend(load_journal_trades())

    if source != "both":
        return trades

    seen: set = set()
    deduped: list[dict] = []
    for t in trades:
        key = (
            t.get("symbol") or t.get("ticker"),
            t.get("exit_time") or t.get("close_time"),
            t.get("strategy_name"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(t)
    return deduped


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # Load .env (best-effort) so DISCORD_WEBHOOK_URL is available under cron.
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(override=False)
    except Exception:
        pass

    trades = _gather_trades(args.source)
    if not trades:
        log("No trades found in ledger(s) — nothing to reconcile.")
        # Empty ledger is not a breach.
        if args.json:
            print(json.dumps({"mismatch_count": 0, "checked": 0}, indent=2))
        return 0

    cache = load_cache()
    cache_size_before = len(cache)

    summary = reconcile(
        trades,
        cache,
        offline=args.offline,
        max_fetch=args.max_fetch,
    )

    # Persist any newly fetched results (best-effort; offline runs no-op).
    if len(cache) != cache_size_before:
        save_cache(cache)
        log(f"Cache updated: {cache_size_before} -> {len(cache)} tickers")

    if args.json:
        # Drop the verbose mismatch list from the top-level to keep it scannable;
        # callers wanting detail can read settlement_cache.json + the text report.
        compact = dict(summary)
        compact["mismatches"] = summary["mismatches"][:50]
        print(json.dumps(compact, indent=2))
    else:
        print(format_report(summary, threshold=args.threshold))

    breach = summary["mismatch_count"] > args.threshold
    if breach:
        log(
            f"BREACH: {summary['mismatch_count']} mismatches > threshold "
            f"{args.threshold} (overstated ${summary['pnl_delta']:+.2f})"
        )
        if not args.no_discord:
            post_discord_alert(summary, args.threshold)
        return 1

    log(
        f"OK: {summary['mismatch_count']} mismatches within threshold "
        f"{args.threshold} ({summary['checked']} reconciled, "
        f"{summary['unresolved']} unresolved)"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - top-level safety net
        log(f"FATAL: {exc}")
        sys.exit(2)
