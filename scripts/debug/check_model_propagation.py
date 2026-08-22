"""Verify model_used propagation fix for ML BTC Hourly."""

import json
from datetime import datetime, timedelta
from collections import Counter

fix_deploy = datetime(2026, 4, 8, 0, 20)

pre_hourly = []
post_hourly = []
with open("data/trade_journal.jsonl") as f:
    for line in f:
        try:
            t = json.loads(line)
        except Exception:
            continue
        if t.get("strategy_name") != "ML BTC Hourly":
            continue
        et = t.get("entry_time") or t.get("exit_time")
        if not et:
            continue
        try:
            dt = datetime.fromisoformat(et.replace("Z", ""))
        except Exception:
            continue
        if dt > fix_deploy:
            post_hourly.append(t)
        elif dt > fix_deploy - timedelta(hours=48):
            pre_hourly.append(t)

print("=== ML BTC Hourly model_used propagation ===")
print(
    f'PRE-fix (48h before): n={len(pre_hourly):3d}  with model_used: {sum(1 for t in pre_hourly if t.get("model_used")):3d}'
)
print(
    f'POST-fix:             n={len(post_hourly):3d}  with model_used: {sum(1 for t in post_hourly if t.get("model_used")):3d}'
)
print()
post_models = Counter(t.get("model_used", "(null)") for t in post_hourly)
print("POST-fix model distribution:")
for k, v in post_models.most_common():
    print(f"  {k}: {v}")
print()

print("Sample post-fix ML BTC Hourly entries:")
for t in post_hourly[:3]:
    print(
        f'  {t.get("exit_time", "")[:19]} model_used={t.get("model_used")} prob={t.get("model_probability")} btc_spot={t.get("btc_spot_at_entry")} tte={t.get("tte_at_entry")}'
    )

# Weather
print()
weather = []
with open("data/trade_journal.jsonl") as f:
    for line in f:
        try:
            t = json.loads(line)
        except Exception:
            continue
        name = t.get("strategy_name") or ""
        if not any(s in name for s in ["Weather", "Meteorologist"]):
            continue
        et = t.get("exit_time")
        if not et:
            continue
        try:
            dt = datetime.fromisoformat(et.replace("Z", ""))
        except Exception:
            continue
        if dt > fix_deploy - timedelta(hours=48):
            weather.append(t)

print(f"Weather trades in last 48h: n={len(weather)}")
for t in weather:
    print(
        f'  {t.get("exit_time", "")[:19]} {t.get("strategy_name"):22s} pnl={t.get("pnl"):+6.2f} reason={t.get("close_reason")}'
    )
