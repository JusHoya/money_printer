# Holdout-B reconcile record: `data/ladders_holdout/` (2026-07-26 .. 2026-08-31)

PRD_STRATEGY_FACTORY.md FR-F0.5 / section 4 A3. Produced 2026-09-02 (workstream D, Phase F0).
This root is **sealed**: the search-frame loader must refuse it (see `SEALED`); it is only
unsealed by `scripts/factory.py holdout --unseal RATIFIED-<date>` (F4).

## What is here

| item | value |
|---|---|
| ladder pull | `python scripts/backfill_ladders.py --start 2026-07-26 --end 2026-08-31 --out data/ladders_holdout` |
| generated (UTC) | 2026-09-02T21:31:14Z (1040 anonymous HTTP requests, min_request_interval_s 0.12, wall 4 min 19 s) |
| city-days requested / with rows / empty | 148 / 148 / 0 |
| city-days with `result` in {yes,no} | **148 / 148** (KXHIGHNY 37, KXHIGHCHI 37, KXHIGHLAX 37, KXHIGHMIA 37); exactly one YES market per city-day |
| markets / hourly rows / two-sided quoted rows | 888 / 33741 / 20238 |
| `bracket_payoff` vs Kalshi `result` (from `expiration_value`) | 888/888 |
| `bracket_payoff` vs Kalshi `result` (from NWS CLI truth) | 886/888 |
| Kalshi `expiration_value` == NWS CLI high | 882/888 markets (147/148 city-days) |
| HTTP failures / empty days / spec errors / blank `expiration_value` | 0 / 0 / 0 / 0 |
| settlement source reported by `/series` on 2026-09-02 | **The Weather Company** (`https://weather.com/kalshi`) for all four series; the development set's manifest (2026-07-27) recorded *NWS Climatological Report* |

## Truth (step 1)

`python scripts/backfill_weather_truth.py --days 250 --end-date 2026-09-01 --min-days 60`
(6 s; merge-safe: 0 rows removed, 41 rows added per station incl. 2025-12-26/27 at the front).
All four stations have a CLI daily high for **every** date 2026-07-26 .. 2026-08-31 (37/37
each, no null highs). The only gap in the whole record is KMIA 2026-04-11 (outside the
holdout). Provenance: `iem_cli` for every row.

## Forecast vintages (step 2)

`python scripts/backfill_forecasts.py --start 2026-07-24 --end 2026-09-01` (12 s; merged into
`data/forecast_archive/forecast_series_gfs_mex.csv`: 80 runs, 0 missing, 0 failed, +2340 rows,
0 removed). `lead_hours` = whole hours from `init_time_utc` to the start of the target local
day (`mos_guidance_provider.lead_hours_for`; `ev_analysis.load_forecast_archive` reads that
column as-is and joins vintages with `init_ts <= ts_utc`). Recomputed independently: 0
mismatches. **37/37 holdout dates have a gfs_mex vintage with lead 4-20 h in all four cities**
(leads present: 4, 5, 7 h = same-day 00Z; 16, 17, 19 h = D-1 12Z). Exit criterion (>= 30) met.

## GEFS (step 3)

Originals copied to `data/forecast_archive_2026-09_pre/` first (the script overwrites).
`python scripts/backfill_ensemble_history.py --start 2026-07-25 --end 2026-08-31 --max-workers 4 --out data/forecast_archive_2026-09`
(`--out` added to the script for this pull; 102 s; source: `noaa-gefs-pds` S3 bucket, every
cycle present): cycles requested/fetched/missing = 38/38/0; 304 rows total,
296 in the holdout window covering 37 dates x 4 cities, leads [4, 5, 7, 28, 29, 31]; no null
`forecast_high_f` / `spread_f`. `data/forecast_archive/forecast_series_gefs.csv` is unchanged.

## Re-reconcile >= 2026-08-14 (Weather Company switch): step 5

`python scripts/reconcile_weather.py --date 2026-08-31 --days 18 --json --no-discord`
(24 s; live Kalshi `/events/<ticker>?with_nested_markets=true` + IEM CLI; sim leg: 0 records
on this box). Coverage floors met (markets 432 >= 288, verified 426 >= 72).
Totals: 72 city-days, 432 markets checked, **426 MATCH, 6 TRUTH_MISMATCH** (all six markets of
one city-day), no other category, exit 1 (breach of threshold 0, by design).

Per-date agreement (cells: `CLI high / Kalshi expiration_value  category`; "agree" counts
city-days where all six markets MATCH):

| date | agree | KNYC | KMDW | KLAX | KMIA |
|---|---|---|---|---|---|
| 2026-08-14 | 4/4 | 84/84 MATCH | 80/80 MATCH | 78/78 MATCH | 96/96 MATCH |
| 2026-08-15 | 4/4 | 83/83 MATCH | 81/81 MATCH | 77/77 MATCH | 93/93 MATCH |
| 2026-08-16 | 4/4 | 80/80 MATCH | 86/86 MATCH | 77/77 MATCH | 94/94 MATCH |
| 2026-08-17 | 4/4 | 81/81 MATCH | 78/78 MATCH | 78/78 MATCH | 95/95 MATCH |
| 2026-08-18 | 4/4 | 86/86 MATCH | 81/81 MATCH | 82/82 MATCH | 100/100 MATCH |
| 2026-08-19 | 4/4 | 85/85 MATCH | 80/80 MATCH | 82/82 MATCH | 96/96 MATCH |
| 2026-08-20 | 4/4 | 84/84 MATCH | 78/78 MATCH | 77/77 MATCH | 93/93 MATCH |
| 2026-08-21 | 4/4 | 79/79 MATCH | 80/80 MATCH | 83/83 MATCH | 94/94 MATCH |
| 2026-08-22 | 4/4 | 77/77 MATCH | 82/82 MATCH | 82/82 MATCH | 94/94 MATCH |
| 2026-08-23 | 4/4 | 80/80 MATCH | 76/76 MATCH | 85/85 MATCH | 93/93 MATCH |
| 2026-08-24 | 4/4 | 79/79 MATCH | 79/79 MATCH | 84/84 MATCH | 91/91 MATCH |
| 2026-08-25 | 4/4 | 78/78 MATCH | 81/81 MATCH | 85/85 MATCH | 92/92 MATCH |
| 2026-08-26 | 4/4 | 81/81 MATCH | 85/85 MATCH | 87/87 MATCH | 92/92 MATCH |
| 2026-08-27 | 4/4 | 77/77 MATCH | 81/81 MATCH | 90/90 MATCH | 93/93 MATCH |
| 2026-08-28 | 4/4 | 84/84 MATCH | 84/84 MATCH | 86/86 MATCH | 93/93 MATCH |
| 2026-08-29 | 3/4 | 76/76 MATCH | 82/82 MATCH | 87/87 MATCH | 85/90 TRUTH_MISMATCHx6 |
| 2026-08-30 | 4/4 | 79/79 MATCH | 81/81 MATCH | 84/84 MATCH | 91/91 MATCH |
| 2026-08-31 | 4/4 | 78/78 MATCH | 91/91 MATCH | 79/79 MATCH | 91/91 MATCH |

**The one divergence: KXHIGHMIA 2026-08-29.** Kalshi settled every market on
`expiration_value = 90.00` (B90.5 = YES) while the NWS CLI for KMIA reports a daily high of
85 F (product `202608300822-KMFL-CDUS42-CLIMIA`). `bracket_payoff` recomputed from Kalshi's
own 90 reproduces Kalshi's result on all 6 markets (`payoff_matches_kalshi = true`), so the
bracket logic is not in question: the *input* differs by 5 F. This is the first observed
instance of the settlement-source change (NWS -> The Weather Company, effective 2026-08-14)
producing a settlement value different from the NWS CLI. In the ladder CSVs that city-day
carries `truth_agrees = false` and `recomputed_yes_cli != recomputed_yes_expval` on 2 of 6
markets.

Ladder-frame cross-check over the full holdout (independent of the reconcile script, from the
CSV columns): city-days where `expiration_value == cli_high`: **147/148**
(before 08-14: 76/76; on/after 08-14: 71/72). Truth-filter drop for the
F4 criterion ("< 10% of city-days"): 1/148 = 0.7%.

**Implication for the factory (not acted on here):** `result` / `expiration_value` are the
settlement labels; `cli_high` is a proxy that agreed 100% before 08-14 and 71/72 after.
Fitness must score against `result` (A2 already says so); after 2026-08-14 `cli_high` is a
diagnostic only, and the daily reconcile will keep flagging Weather-Company days as
TRUTH_MISMATCH until the truth source is revisited.

## Files and integrity (step 6)

`SHA256SUMS` covers every `KXHIGH*/*.csv`, `manifest.json`, `RECONCILE.md` and `SEALED`,
paths relative to this directory, LF line endings (`.gitattributes` pins
`data/ladders_holdout/** text eol=lf`). Verify: `cd data/ladders_holdout && sha256sum -c SHA256SUMS`.
