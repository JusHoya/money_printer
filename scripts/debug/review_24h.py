"""Throwaway 24h trade journal review."""

import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict

now = datetime.now(timezone.utc).replace(tzinfo=None)
cut_24h = now - timedelta(hours=24)
cut_48h = now - timedelta(hours=48)
binary_fix = datetime(2026, 4, 5, 17, 42)  # 12:42 CT = 17:42 UTC
metar_fix = datetime(2026, 4, 5, 18, 10)  # 13:10 CT = 18:10 UTC

bins = {
    "last_24h": [],
    "last_48h": [],
    "pre_binary_fix_48h": [],
    "post_binary_fix": [],
}

with open("data/trade_journal.jsonl") as f:
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
        if dt > cut_24h:
            bins["last_24h"].append(t)
        if dt > cut_48h:
            bins["last_48h"].append(t)
            if dt < binary_fix:
                bins["pre_binary_fix_48h"].append(t)
            else:
                bins["post_binary_fix"].append(t)


def stats(trades, label):
    if not trades:
        print(f"{label}: 0 trades")
        return
    pnls = [t.get("pnl") or 0 for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    expectancy = total / len(pnls)
    print(
        f"{label}: n={len(trades):4d} pnl={total:+8.2f} wr={wr:5.1f}% avg_w={avg_w:6.2f} avg_l={avg_l:7.2f} EV={expectancy:+6.2f}"
    )


print("=== TIME BUCKETS ===")
for k in ["last_24h", "last_48h", "pre_binary_fix_48h", "post_binary_fix"]:
    stats(bins[k], k.ljust(20))

print()
print("=== BY STRATEGY (24h) ===")
strats = defaultdict(list)
for t in bins["last_24h"]:
    strats[t.get("strategy_name", "?")].append(t)
for s, ts in sorted(
    strats.items(), key=lambda x: -sum((t.get("pnl") or 0) for t in x[1])
):
    stats(ts, s[:28].ljust(28))

print()
print("=== BY STRATEGY (post binary fix only) ===")
strats_post = defaultdict(list)
for t in bins["post_binary_fix"]:
    strats_post[t.get("strategy_name", "?")].append(t)
for s, ts in sorted(
    strats_post.items(), key=lambda x: -sum((t.get("pnl") or 0) for t in x[1])
):
    stats(ts, s[:28].ljust(28))

print()
print("=== CLOSE REASONS (24h) ===")
reasons = defaultdict(list)
for t in bins["last_24h"]:
    reasons[t.get("close_reason", "?")].append(t.get("pnl") or 0)
for r, ps in sorted(reasons.items(), key=lambda x: -len(x[1])):
    wr = sum(1 for p in ps if p > 0) / len(ps) * 100
    print(f"  {r:25s}: n={len(ps):4d} pnl={sum(ps):+8.2f} wr={wr:5.1f}%")

print()
print("=== CLOSE REASONS (BTC strategies, 24h) ===")
btc_reasons = defaultdict(list)
for t in bins["last_24h"]:
    sym = t.get("symbol", "")
    if "KXBTC" in sym:
        btc_reasons[t.get("close_reason", "?")].append(t.get("pnl") or 0)
for r, ps in sorted(btc_reasons.items(), key=lambda x: -len(x[1])):
    wr = sum(1 for p in ps if p > 0) / len(ps) * 100
    print(f"  {r:25s}: n={len(ps):4d} pnl={sum(ps):+8.2f} wr={wr:5.1f}%")

print()
print("=== HOURLY PNL (24h, UTC) ===")
hour_pnl = defaultdict(float)
hour_n = defaultdict(int)
for t in bins["last_24h"]:
    et = t.get("exit_time")
    if not et:
        continue
    try:
        dt = datetime.fromisoformat(et.replace("Z", ""))
    except Exception:
        continue
    key = dt.strftime("%m-%d %H:00")
    hour_pnl[key] += t.get("pnl") or 0
    hour_n[key] += 1

cum = 0.0
for k in sorted(hour_pnl.keys()):
    cum += hour_pnl[k]
    bar = "█" * min(40, int(abs(hour_pnl[k]) / 2))
    sign = "+" if hour_pnl[k] >= 0 else "-"
    print(f"  {k}  {hour_pnl[k]:+8.2f}  cum={cum:+8.2f}  n={hour_n[k]:3d}  {sign}{bar}")

print()
print("=== TOTAL JOURNAL ===")
total_lines = 0
with open("data/trade_journal.jsonl") as f:
    for _ in f:
        total_lines += 1
print(f"Total trades in journal: {total_lines}")
