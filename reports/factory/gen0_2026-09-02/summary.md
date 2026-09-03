# Factory gen-0 report -- `gen0_2026-09-03`

**Family** `weather/gfs_mex/taker/v1` -- registry status **OPEN** (picker `max_boot_lo_ties_fewer_clauses`, config sha `e679631add8e`, cutoff 2026-07-25)

**Parity check** fr31a_taker on the parity frame: expected 181 / 65 / +0.0636; kernel 181 / 65 / +0.0636 boot [+0.0122, +0.1086] -- matches within 1e-9: **yes**

Frame: parity `db6fe0ff5aaa` (251728 rows) / search `0fdf39ea506b` (216636 rows) / gefs twin `16aeb10a3608`

## Seeds (realized c/contract, date-bootstrap 95% CI, trades, dates)

| seed | parity (69d) | search (69d) | A validation | B validation | C validation |
|---|---|---|---|---|---|
| `fr31a_taker` | KILLED:WORST_DATE | KILLED:WORST_DATE | +0.1743 [+0.0539, +0.2984] n=18 d=9 | +0.1244 [+0.0539, +0.1812] n=24 d=11 | +0.0089 [-0.1674, +0.1360] n=24 d=9 |
| `fr31b` | KILLED:MIN_TRADES | KILLED:MIN_TRADES | KILLED:NO_TRADES | +0.0560 [+0.0560, +0.0560] n=1 d=1 | KILLED:NO_TRADES |
| `nofilter_no` | KILLED:BSS | +0.0602 [+0.0217, +0.0950] n=241 d=67 | +0.1260 [+0.0358, +0.2128] n=27 d=11 | +0.0960 [+0.0217, +0.1556] n=45 d=12 | -0.0179 [-0.1418, +0.0854] n=44 d=9 |
| `salvage_5f` | +0.0377 [+0.0171, +0.0560] n=320 d=69 | KILLED:WORST_DATE | +0.2019 [+0.1135, +0.3167] n=15 d=9 | +0.0945 [+0.0470, +0.1330] n=18 d=10 | +0.0220 [-0.0852, +0.0950] n=20 d=9 |
| `mlweather_fallback` | KILLED:BSS | KILLED:BSS | -0.0417 [-0.0720, -0.0138] n=107 d=12 | -0.0013 [-0.0530, +0.0418] n=109 d=12 | -0.0015 [-0.0473, +0.0437] n=88 d=9 |
| `fr31a_gefs` | KILLED:WORST_DATE | KILLED:WORST_DATE | -0.0370 [-0.5196, +0.2130] n=7 d=4 | -0.0616 [-0.2805, +0.1088] n=18 d=10 | +0.1079 [-0.0484, +0.2690] n=8 d=4 |
| `far_yes_taker` | KILLED:BSS | KILLED:GEFS_TWIN | -0.0171 [-0.0503, +0.0409] n=49 d=12 | -0.0300 [-0.0544, +0.0074] n=53 d=12 | -0.0148 [-0.0652, +0.0641] n=26 d=9 |

`mlweather_fallback` (what maia trades today): APPROXIMATION of what maia's MLWeatherStrategy analytical fallback traded (src/strategies/ml_weather.py + src/ml/predictor.py:583-604): buy NO, taker, when the predictor's Gaussian P(in bracket)=max(0.05, exp(-0.5 z^2)), z=(forecast-mid)/(width/2) with width = cap-floor = 1 for 'between' brackets, gives no_edge = bid - P >= 0.08. For a forecast >= 1.22F from the bracket midpoint P floors at 0.05 so the rule is 'bid >= 0.13'. GENE_SPEC v1 encoding: bands {1-2F..5F+} (distance_f = |midpoint - mu| >= 1F is the nearest legal value to 1.22F, and mu_f is the calibrated median, not the raw NWS high the sandbox used); quote_hi 0.85 (quote = 1 - bid <= 0.87 -> nearest grid value); windows {>=24h,12-24h,6-12h} (the 10:00-13:59 ET decision window maps to 12-24h/6-12h for same-day and >=24h for next-day markets on the parity tape); p_win OFF (the fallback never used the calibration). NOT encoded: the one-slot-per-city 'highest YES bid first' selection (cross-row), the METAR/NWS source gate, the winner guard, the Yogi-Berra branch, and the fallback's YES branch on open-ended tails (P clipped to 0.95 on a 50F virtual bracket -> BUY YES when ask <= 0.87).

**Frame-level Brier skill vs market mid** (parity frame, all two-sided rows): BSS -0.2503 CI [-0.3149, -0.1912] over 38200 rows / 69 dates (date-clustered)
**Frame-level Brier skill vs market mid** (search frame, all two-sided rows): BSS -0.2300 CI [-0.2990, -0.1697] over 31448 rows / 69 dates (date-clustered)

**Throughput** 6588.6 evals/s on 16 workers, peak RSS 324 MB, host `3d7365915c40`

Evolution, RC/SPA, Holm, controls: F2 -- not part of gen-0. No genome is proposed by this report.
