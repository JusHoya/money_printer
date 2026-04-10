"""Discord webhook notifications for cycle status reports."""

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

JOURNAL_PATH = "data/trade_journal.jsonl"


def _read_recent_trades(hours: int = 24) -> list[dict]:
    """Read trades from journal that exited within the last N hours."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    trades = []
    try:
        with open(JOURNAL_PATH) as f:
            for line in f:
                try:
                    t = json.loads(line)
                except Exception:
                    continue
                et = t.get("exit_time") or t.get("entry_time")
                if not et:
                    continue
                try:
                    dt = datetime.fromisoformat(et.replace("Z", ""))
                except Exception:
                    continue
                if dt > cutoff:
                    trades.append(t)
    except FileNotFoundError:
        pass
    return trades


def _compute_stats(trades: list[dict]) -> dict:
    """Compute PnL, WR, EV from a list of trade dicts."""
    if not trades:
        return {"n": 0, "pnl": 0, "wr": 0, "ev": 0, "avg_w": 0, "avg_l": 0}
    pnls = [t.get("pnl") or 0 for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    return {
        "n": len(trades),
        "pnl": total,
        "wr": len(wins) / len(pnls) * 100,
        "ev": total / len(pnls),
        "avg_w": sum(wins) / len(wins) if wins else 0,
        "avg_l": sum(losses) / len(losses) if losses else 0,
    }


def build_status_report(cycle_record: dict, journal_count: int) -> dict:
    """Build a comprehensive status report for Discord.

    Returns a dict with all the data needed to format the embed.
    """
    trades_24h = _read_recent_trades(hours=24)
    overall = _compute_stats(trades_24h)

    # Per-strategy breakdown
    by_strategy = defaultdict(list)
    for t in trades_24h:
        by_strategy[t.get("strategy_name", "?")].append(t)
    strategy_stats = {
        name: _compute_stats(ts)
        for name, ts in sorted(
            by_strategy.items(),
            key=lambda x: -sum((t.get("pnl") or 0) for t in x[1]),
        )
    }

    # Close-reason breakdown
    by_reason = defaultdict(list)
    for t in trades_24h:
        by_reason[t.get("close_reason", "?")].append(t.get("pnl") or 0)
    reason_stats = {}
    for reason, pnls in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0
        reason_stats[reason] = {"n": len(pnls), "pnl": sum(pnls), "wr": wr}

    return {
        "cycle": cycle_record,
        "rolling_24h": overall,
        "strategies": strategy_stats,
        "close_reasons": reason_stats,
        "journal_count": journal_count,
    }


def format_discord_embed(report: dict) -> dict:
    """Format the status report as a Discord embed."""
    cr = report["cycle"]
    r24 = report["rolling_24h"]

    pnl_color = 0x2ECC71 if r24["pnl"] >= 0 else 0xE74C3C  # green / red

    # Cycle summary
    cycle_line = (
        f"**Duration:** {cr['duration_min']:.0f} min | "
        f"**Trades:** {cr['trades']} ({cr['wins']}W/{cr['losses']}L) | "
        f"**WR:** {cr['win_rate']:.0f}%"
    )

    # 24h rolling
    rolling_line = (
        f"**Trades:** {r24['n']} | "
        f"**PnL:** ${r24['pnl']:+.2f} | "
        f"**WR:** {r24['wr']:.1f}% | "
        f"**EV:** ${r24['ev']:+.2f}"
    )

    # Strategy breakdown
    strat_lines = []
    for name, st in report["strategies"].items():
        short = name[:20]
        strat_lines.append(
            f"`{short:20s}` {st['n']:2d} trades  ${st['pnl']:+8.2f}  WR {st['wr']:5.1f}%"
        )
    strat_block = "\n".join(strat_lines) if strat_lines else "_No trades_"

    # Close reasons
    reason_lines = []
    for reason, rs in report["close_reasons"].items():
        reason_lines.append(f"`{reason:25s}` {rs['n']:3d}x  ${rs['pnl']:+8.2f}")
    reason_block = "\n".join(reason_lines) if reason_lines else "_None_"

    # Model info
    auc = cr.get("train_val_auc", 0)
    samples = cr.get("train_samples", 0)
    model_line = f"**AUC:** {auc:.4f} | **Samples:** {samples:,}"

    description = (
        f"**Cycle Summary**\n{cycle_line}\n\n"
        f"**24h Rolling Performance**\n{rolling_line}\n\n"
        f"**Strategy Breakdown (24h)**\n{strat_block}\n\n"
        f"**Close Reasons (24h)**\n{reason_block}\n\n"
        f"**Model**\n{model_line}\n\n"
        f"**Journal:** {report['journal_count']:,} total outcomes"
    )

    return {
        "embeds": [
            {
                "title": f"Cycle #{cr['cycle']} Complete",
                "description": description,
                "color": pnl_color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


def send_discord_notification(
    webhook_url: str,
    cycle_record: dict,
    journal_count: int,
) -> None:
    """Build and send a status report to Discord. Fire-and-forget in a thread."""

    def _send():
        try:
            report = build_status_report(cycle_record, journal_count)
            payload = format_discord_embed(report)
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code >= 400:
                logger.warning(
                    "[Discord] Webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
            else:
                logger.info(
                    "[Discord] Cycle #%d notification sent",
                    cycle_record.get("cycle", "?"),
                )
        except Exception as exc:
            logger.warning("[Discord] Notification failed: %s", exc)

    threading.Thread(target=_send, daemon=True).start()


def send_test_message(webhook_url: str) -> bool:
    """Send a test message to verify webhook connectivity. Returns True on success."""
    payload = {
        "embeds": [
            {
                "title": "Money Printer Connected",
                "description": "Discord notifications are now active. You will receive a status update after each cycle completion (~every 4 hours).",
                "color": 0x3498DB,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        return resp.status_code < 400
    except Exception as exc:
        logger.error("[Discord] Test message failed: %s", exc)
        return False
