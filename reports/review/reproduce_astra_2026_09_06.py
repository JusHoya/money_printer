"""Read-only review arithmetic. Reads named reports and DEVELOPMENT fixtures only.

Run from the repository root:
    python reports/review/reproduce_astra_2026_09_06.py
No exchange connection, application initialization, or sealed-root access.
"""
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median, stdev

ROOT = Path(__file__).resolve().parents[2]


def read(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def fee(p, n):
    # Repo whole-contract, whole-cent-price single-order convention.
    return math.ceil(round(0.07 * n * p * (1 - p) * 100, 9)) / 100


summary_path = "reports/factory/run_2026-09-03b/summary.json"
fixture_path = "tests/fixtures/factory/genome_0c4b20502f2daf65_offline_trades.json"
fill_path = "reports/factory/fill_realism_2026-09-06.json"
s = read(summary_path)
f = read(fixture_path)
fill = read(fill_path)
rows = [dict(zip(f["columns"], row)) for row in f["rows"]]
admitted = [r for r in rows if 0.3 + 0.4 * r["p_win"] > r["price_paid"]]
assert len(rows) == 130 and len(admitted) == 13
pooled = s["pooled_oos"]
daily = pooled["per_date"]
pooled_sd = stdev(d["pnl"] for d in daily)

out = {
    "source_sha256": {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in (summary_path, fixture_path, fill_path)
    },
    "reported_search": {
        "evaluations": s["evaluations"],
        "n_phenotypes": s["n_phenotypes"],
        "pooled_oos": {k: v for k, v in pooled.items() if k != "per_date"},
        "paired_vs_nofilter": s["paired_vs_nofilter"]["pooled"],
        "sd_active_date_means": pooled_sd,
        "equal_contract_pnl_reconstructed_from_daily_means": sum(d["pnl"] * d["trades"] for d in daily),
        "dates_for_4c_80pct_power_using_active_date_sd": math.ceil(((1.96 + 0.841621) * pooled_sd / 0.04) ** 2),
        "p_rc_feasible": s["multiplicity"]["ALL69"]["p_rc"],
        "p_rc_all": s["multiplicity"]["ALL69"]["p_rc_all"],
        "holm_p_adj": s["holm"]["this_family"]["p_adj"],
    },
    "development_fixture": {
        "trades": len(rows),
        "cold_start_admitted": len(admitted),
        "cold_start_rejected": len(rows) - len(admitted),
        "median_price": median(r["price_paid"] for r in rows),
        "median_admitted_price": median(r["price_paid"] for r in admitted),
        "active_dates": len({r["target_date"] for r in rows}),
        "admitted_dates": len({r["target_date"] for r in admitted}),
        "full_per_trade_mean": mean(r["realized_per_contract"] for r in rows),
        "admitted_per_trade_mean": mean(r["realized_per_contract"] for r in admitted),
    },
    "reported_fill_study": {
        k: fill[k] for k in (
            "primary_window_s", "p90_primary_window", "p90_next_poll",
            "next_poll_adverse_drift", "recommended_adverse_fill",
        )
    },
    "capital_scenarios_NOT_forecasts": [
        {
            "net_monthly_return": r,
            "capital_for_2000_mean_profit": 2000 / r,
            "months_from_300_no_withdrawals": math.log((2000 / r) / 300) / math.log(1 + r),
            "months_with_300_monthly_contribution": math.log(
                ((2000 / r) + 300 / r) / (300 + 300 / r)
            ) / math.log(1 + r),
        }
        for r in (0.01, 0.02, 0.05, 0.10)
    ],
    "income_arithmetic": {
        "monthly_profit_over_starting_300": 2000 / 300,
        "revival_100_monthly_over_350": 100 / 350,
        "revival_300_monthly_over_350": 300 / 350,
        "net_4c_contracts_per_month": 2000 / 0.04,
        "net_4c_contracts_per_30_day_day": 2000 / 0.04 / 30,
        "net_3c_contracts_per_month": 2000 / 0.03,
        "four_city_one_entry_per_day_50_contracts_net_4c_month": 4 * 50 * 30 * 0.04,
        "historical_130_in_69_days_50_contracts_net_4c_month": 130 / 69 * 50 * 30 * 0.04,
        "seed_300_max_allocation_contracts_at_84c": math.floor(300 * 0.10 / 0.84),
        "seed_350_max_allocation_contracts_at_84c": math.floor(350 * 0.10 / 0.84),
        "four_city_seed_300_net_4c_month": 4 * math.floor(300 * 0.10 / 0.84) * 30 * 0.04,
        "four_city_seed_350_net_4c_month": 4 * math.floor(350 * 0.10 / 0.84) * 30 * 0.04,
    },
    "fee_examples_repo_convention": [
        {"price": p, "contracts": n, "total_fee": fee(p, n), "fee_per_contract": fee(p, n) / n,
         "hold_to_settlement_breakeven_probability": p + fee(p, n) / n}
        for p in (0.5, 0.84) for n in (1, 20, 50)
    ],
    "power_sensitivity_NOT_empirical_variance": [
        {
            "assumed_sd_of_daily_statistic": sd,
            "target_mean": effect,
            "dates_normal_approx_80pct_power_two_sided_5pct": math.ceil(((1.96 + 0.841621) * sd / effect) ** 2),
        }
        for sd in (0.19, 0.24, 0.32) for effect in (0.03, 0.04, 0.05)
    ],
    "illustrative_objective_counterexample": {
        "daily_contract_counts": [1, 100],
        "daily_per_contract_pnl": [0.10, -0.02],
        "mean_of_daily_means": mean([0.10, -0.02]),
        "total_dollar_pnl_one_contract_per_opportunity": 0.10 - 100 * 0.02,
    },
    "illustrative_sampling_counterexample": {
        "ask_at_0s": 0.80,
        "unobserved_ask_at_10s": 0.90,
        "ask_at_40s": 0.80,
        "next_poll_adverse_drift": 0.0,
        "actual_20s_max_adverse_drift": 0.10,
    },
}
print(json.dumps(out, indent=2, sort_keys=True))
