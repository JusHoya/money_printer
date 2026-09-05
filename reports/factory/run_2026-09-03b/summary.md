# Factory family report -- `run_2026-09-03b`

## VERDICT: **CLOSED**
failing conditions: pooled_boot_lo_gt0, holm_p_lt_alpha, p_rc_all69_lt_threshold, beats_every_control, paired_vs_nofilter_lo_gt0, point_estimate_ge_4c

**Family** `weather/gfs_mex/taker/v1` -- registry status **CLOSED** (config sha `e679631add8e`, git `fb91898288c4`, master seed 20260902, evaluations 96000)

## Picks (pre-registered picker: max search-window boot_lo among constraint-satisfying elites; ties -> fewer clauses)

| campaign | genome | phenotype | gen | in-sample realized [boot CI] n/d | validation realized [boot CI] n/d | BSS_trades (val) | N phenotypes | p_RC | p_SPA |
|---|---|---|---|---|---|---|---|---|---|
| A | `7d857b00d373260c` direction_code == 1 & mode_code == 0 & window_code in bits(0b1001) & band_code in bits(0b10011) & p_win >= 0.75 & p_win <= 0.95 & quote >= 0.5 & sigma_f <= 3.5 | `880d0b59dfd8` | 59 | +0.2492 [+0.1919, +0.2981] n=40 d=22 | -0.0365 [-0.3153, +0.2271] n=13 d=10 | -0.034 | 10932 | 0.406 | 0.152 |
| B | `7f1bc92348300906` direction_code == 1 & mode_code == 0 & window_code in bits(0b111) & band_code in bits(0b11111) & p_win >= 0.85 & p_win <= 0.95 & quote >= 0.45 & quote <= 0.8 & sigma_f <= 3.0 | `c76a185bb262` | 59 | +0.1694 [+0.1101, +0.2213] n=52 d=32 | +0.0354 [-0.1467, +0.1954] n=23 d=11 | 0.069 | 10831 | 0.767 | 0.325 |
| C | `03a5966189d0a789` direction_code == 1 & mode_code == 0 & window_code in bits(0b11011) & band_code in bits(0b101111) & p_win >= 0.95 & yes_ask < 1.0 & p_yes <= yes_ask - 0.08 & quote <= 0.95 & sigma_f <= 4.0 & edge_distance_f >= 3.0 | `532ae4d2a2d2` | 59 | +0.1281 [+0.0827, +0.1772] n=61 d=35 | +0.1087 [+0.0742, +0.1478] n=13 d=8 | 0.972 | 8745 | 0.721 | 0.244 |
| ALL69 | `4b5acfa1055e4250` direction_code == 0 & mode_code == 0 & window_code in bits(0b1) & band_code in bits(0b110010) & lead_bucket_code in bits(0b1) & quote >= 0.1 & quote <= 0.95 & sigma_f <= 4.0 | `6d315b316307` | 59 | +0.2021 [+0.0869, +0.3178] n=71 d=44 | none (deployment genome) | — | 12111 | 0.886 | 0.664 |

p_RC / p_SPA above: feasible competition set (dates >= ceil(0.6 D), trades >= 40; section 6.3 amendment). All-phenotype p_RC A/B/C/ALL69: 0.992 / 0.997 / 0.999 / 1.000; L_feasible/L_all: 9187/10932 / 9588/10831 / 7326/8745 / 10538/12111

## Pooled OOS (29 validation dates, 49 trades, 4 cities)

mean +0.0308 se +0.0604 t +0.51 boot 95% [-0.0900, +0.1417] one-sided p 0.2895 (matches procedure: True)
Holm across 1 registry entry (alpha 0.05): p_adj 0.2895 reject False
Clustered DSR: SR 0.095 DSR 0.000 PSR(0) 0.683 E[max SR] 1.711 (N_trials 30439 phenotypes, skew -1.08 kurt 3.17, MAD-robust V[SR] 0.1720 over 27151 distinct SR trials, 15 clipped at |SR| > 50.0); raw-variance companion: V[SR] 0.6189 -> E[max SR] 3.245 DSR 0.000

## Gates (section 5.8)

- paired vs `nofilter_no`: mean -0.0528 se +0.0687 t -0.77 boot lo -0.1910 (n 29)
- sign at +2c: + (+0.0208); +3c: + (+0.0108); embargo 2: + -- the same picks scored on the validation dates of a frame REBUILT with calibration embargo_days = 2 (section 5.8 sensitivity); the run's frame uses embargo_days = 1
- BSS_trades on pooled validation trades: 0.065 (A -0.034 / B 0.069 / C 0.972)
- tail ratio |z|>=2.5 (informational): 2.12 (3 of 114 city-days; expected 1.4)
- phenotype Jaccard: A/B 0.21, A/C 0.03, A/ALL69 0.01, B/C 0.03, B/ALL69 0.03, C/ALL69 0.01

## Controls

- snapshot-efficient null: 20/20 replicates, pooled boot_lo > 0 in 0 (<= 1 required: True); KS of the picks' p_RC vs U(0,1): D 0.245 p 0.001 (> 0.05 required: False); real pooled mean rank 4 of 20
- residual-shuffle null (section 6.4a, PAIRED vs nofilter_no under the same truth): 20/20 replicates, p95 of paired deltas +0.8048; real paired delta -0.0528 rank 21 of 20 (exceeds p95: False). Raw pooled means (diagnostic, inflated by late-day market information): p95 +0.8725, real +0.0308 rank 21 of 20
- planted edge (+0.05): recovered pick pooled on planted +0.1476 vs original +0.0933; captured +0.0543 ratio 1.09 (>= 0.8: True); the rule's own validation delta +0.0533 (rule ratio 1.07); 2 of the picks' 48 validation trades were flipped rows, 0.83 of them inside the planted region (one-trade granularity, see controls.planted.note)

## Blocked 5-fold diagnostic (in-sample blocks postdate the held block)

pooled +0.0169 [-0.0696, +0.0968] n 56; folds: F1, F2, F3, F4, F5

## Verdict conditions

- headline_picks_present: PASS
- pooled_boot_lo_gt0: FAIL
- holm_p_lt_alpha: FAIL
- p_rc_all69_lt_threshold: FAIL
- beats_every_control: FAIL
- paired_vs_nofilter_lo_gt0: FAIL
- sign_survives_2c: PASS
- sign_survives_3c: PASS
- sign_survives_embargo2: PASS
- bss_trades_ge0: PASS
- point_estimate_ge_4c: FAIL
- cities_ge3: PASS

**CLOSED** -- PROPOSED iff every headline campaign (A/B/C/ALL69) has a pick, pooled boot_lo > 0, Holm p < alpha, p_RC(ALL69) < threshold on the feasible competition set, beats every snapshot replicate's pooled validation and the residual null's paired-delta p95 (section 6.4a), and every section 5.8 gate; embargo-2 is not applicable unless a rebuilt embargo-2 frame was supplied; otherwise CLOSED (section 6.3)
