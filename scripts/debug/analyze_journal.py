"""Analyze trade journal for strategy biases and loss patterns.

Usage:
    python scripts/debug/analyze_journal.py
    python scripts/debug/analyze_journal.py --since 2026-03-28  # post-fix only
"""

import argparse
import json
from collections import defaultdict


def load_journal(path="data/trade_journal.jsonl", since=None):
    outcomes = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if since and (o.get("exit_time") or "") < since:
                continue
            outcomes.append(o)
    return outcomes


def analyze(outcomes):
    print(f"Total trades: {len(outcomes)}")
    if not outcomes:
        return
    print(
        f"Date range: {outcomes[0].get('entry_time', '?')[:10]} "
        f"to {outcomes[-1].get('exit_time', '?')[:10]}"
    )

    # ── Per-strategy stats ──────────────────────────────────────────────
    strats = defaultdict(
        lambda: {
            "wins": 0,
            "losses": 0,
            "flat": 0,
            "total_pnl": 0.0,
            "win_pnl": 0.0,
            "loss_pnl": 0.0,
            "reasons": defaultdict(lambda: {"count": 0, "pnl": 0.0}),
            "sides": defaultdict(int),
            "contract_sides": defaultdict(int),
            "entry_prices": [],
            "confidences": [],
            "win_sizes": [],
            "loss_sizes": [],
        }
    )

    for o in outcomes:
        s = strats[o["strategy_name"]]
        pnl = o["pnl"]
        s["total_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
            s["win_pnl"] += pnl
            s["win_sizes"].append(pnl)
        elif pnl < 0:
            s["losses"] += 1
            s["loss_pnl"] += pnl
            s["loss_sizes"].append(pnl)
        else:
            s["flat"] += 1

        reason = o.get("close_reason", "?")
        s["reasons"][reason]["count"] += 1
        s["reasons"][reason]["pnl"] += pnl

        s["sides"][o.get("side", "?")] += 1
        s["contract_sides"][o.get("contract_side", "?")] += 1
        if o.get("entry_price"):
            s["entry_prices"].append(o["entry_price"])
        if o.get("model_confidence"):
            s["confidences"].append(o["model_confidence"])

    # ── Summary table ───────────────────────────────────────────────────
    print()
    header = (
        f"{'Strategy':25s} {'Trades':>6s} {'W':>4s} {'L':>4s} "
        f"{'WR%':>6s} {'TotalPnL':>10s} {'AvgWin':>8s} {'AvgLoss':>8s} "
        f"{'W/L':>5s} {'AvgEntry':>9s}"
    )
    print("=" * len(header))
    print(header)
    print("=" * len(header))

    for name in sorted(strats, key=lambda k: strats[k]["total_pnl"]):
        s = strats[name]
        total = s["wins"] + s["losses"] + s["flat"]
        wr = s["wins"] / max(1, s["wins"] + s["losses"]) * 100
        avg_win = s["win_pnl"] / max(1, s["wins"])
        avg_loss = s["loss_pnl"] / max(1, s["losses"])
        wl = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        avg_e = (
            sum(s["entry_prices"]) / len(s["entry_prices"]) if s["entry_prices"] else 0
        )
        print(
            f"{name:25s} {total:6d} {s['wins']:4d} {s['losses']:4d} "
            f"{wr:5.1f}% ${s['total_pnl']:+9.2f} ${avg_win:+7.2f} "
            f"${avg_loss:+7.2f} {wl:5.2f} {avg_e:8.3f}"
        )

    # ── Close reason breakdown ──────────────────────────────────────────
    print()
    print("=" * 90)
    print("CLOSE REASONS (by PnL impact)")
    print("=" * 90)
    for name in sorted(strats, key=lambda k: strats[k]["total_pnl"]):
        s = strats[name]
        print(f"\n  {name}:")
        for reason, data in sorted(s["reasons"].items(), key=lambda x: x[1]["pnl"]):
            avg = data["pnl"] / data["count"] if data["count"] else 0
            print(
                f"    {reason:40s} {data['count']:4d} trades  "
                f"PnL=${data['pnl']:+9.2f}  avg=${avg:+7.2f}"
            )

    # ── Side / contract side ────────────────────────────────────────────
    print()
    print("=" * 90)
    print("SIDE / CONTRACT_SIDE")
    print("=" * 90)
    for name in sorted(strats, key=lambda k: strats[k]["total_pnl"]):
        s = strats[name]
        print(
            f"  {name:25s}  sides={dict(s['sides'])}  "
            f"contract_sides={dict(s['contract_sides'])}"
        )

    # ── Win/loss size distribution ──────────────────────────────────────
    print()
    print("=" * 90)
    print("WIN/LOSS SIZE DISTRIBUTION (P10 / P25 / median / P75 / P90)")
    print("=" * 90)

    for name in sorted(strats, key=lambda k: strats[k]["total_pnl"]):
        s = strats[name]
        if s["win_sizes"]:
            ws = sorted(s["win_sizes"])
            n = len(ws)
            w_pcts = [
                ws[int(n * 0.1)],
                ws[int(n * 0.25)],
                ws[int(n * 0.5)],
                ws[int(n * 0.75)],
                ws[min(int(n * 0.9), n - 1)],
            ]
        else:
            w_pcts = [0] * 5
        if s["loss_sizes"]:
            ls = sorted(s["loss_sizes"])
            n = len(ls)
            l_pcts = [
                ls[int(n * 0.1)],
                ls[int(n * 0.25)],
                ls[int(n * 0.5)],
                ls[int(n * 0.75)],
                ls[min(int(n * 0.9), n - 1)],
            ]
        else:
            l_pcts = [0] * 5
        print(f"\n  {name}:")
        print(
            f"    Wins  ({s['wins']:4d}): " + " / ".join(f"${x:+.2f}" for x in w_pcts)
        )
        print(
            f"    Losses({s['losses']:4d}): " + " / ".join(f"${x:+.2f}" for x in l_pcts)
        )

    # ── Confidence calibration ──────────────────────────────────────────
    print()
    print("=" * 90)
    print("CONFIDENCE vs OUTCOME (is high confidence = more wins?)")
    print("=" * 90)
    buckets = {"0.0-0.3": [], "0.3-0.5": [], "0.5-0.7": [], "0.7-1.0": []}
    for o in outcomes:
        c = o.get("model_confidence")
        if c is None:
            continue
        if c < 0.3:
            buckets["0.0-0.3"].append(o)
        elif c < 0.5:
            buckets["0.3-0.5"].append(o)
        elif c < 0.7:
            buckets["0.5-0.7"].append(o)
        else:
            buckets["0.7-1.0"].append(o)

    for bucket, trades in sorted(buckets.items()):
        if not trades:
            continue
        wins = sum(1 for t in trades if t["pnl"] > 0)
        total = len(trades)
        wr = wins / total * 100 if total else 0
        total_pnl = sum(t["pnl"] for t in trades)
        avg_pnl = total_pnl / total if total else 0
        print(
            f"  Confidence {bucket}: {total:4d} trades, "
            f"WR={wr:5.1f}%, PnL=${total_pnl:+9.2f}, avg=${avg_pnl:+6.2f}"
        )

    # ── Time-of-day analysis ────────────────────────────────────────────
    print()
    print("=" * 90)
    print("TIME-OF-DAY PnL (UTC hours)")
    print("=" * 90)
    hours = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for o in outcomes:
        et = o.get("entry_time", "")
        if len(et) >= 13:
            try:
                h = int(et[11:13])
                hours[h]["trades"] += 1
                hours[h]["pnl"] += o["pnl"]
                if o["pnl"] > 0:
                    hours[h]["wins"] += 1
            except ValueError:
                pass
    for h in sorted(hours):
        d = hours[h]
        wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        bar = "#" * int(abs(d["pnl"]) / 50)
        sign = "+" if d["pnl"] >= 0 else "-"
        print(
            f"  {h:02d}:00 UTC  {d['trades']:4d} trades  "
            f"WR={wr:5.1f}%  PnL=${d['pnl']:+9.2f}  {sign}{bar}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since", help="Only include trades after this date (YYYY-MM-DD)"
    )
    parser.add_argument("--journal", default="data/trade_journal.jsonl")
    args = parser.parse_args()

    outcomes = load_journal(args.journal, args.since)
    analyze(outcomes)
