# Factory board

## VERDICT: CLOSED -- `weather/gfs_mex/taker/v1` run `run_2026-09-03b`
failing: pooled_boot_lo_gt0, holm_p_lt_alpha, p_rc_all69_lt_threshold, beats_every_control, paired_vs_nofilter_lo_gt0, point_estimate_ge_4c
pooled OOS +0.0308 [-0.0900, +0.1417] t +0.51 n 29d/49t | p_RC A/B/C/ALL69 0.41/0.77/0.72/0.89 (all-phen 0.99/1.00/1.00/1.00) | Holm p 0.289 | DSR 0.00 | N_phen 42619
RESIDUAL-NULL paired rank 21/20 (p95 delta +0.8048; raw rank 21/20 p95 +0.8725) | snapshot boot_lo>0 0/20 KS p 0.00 rank 4 | planted capture 1.09 PASS (rule 1.07, 2/48 flipped)

| lane | status | family | pick | pooled OOS lo..hi | dates | trades | p_RC | Holm p | vs no-filter | N_phen | controls | units / ETA |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| weather | READY | weather/gfs_mex/taker/v1 | CLOSED `4b5acfa1` | -0.0900..+0.1417 | 29 | 49 | 0.89 | 0.289 | -0.0528 lo -0.1910 | 42619 | snap 0/20 res#21 plant 1.09 | 69 target_date / floor 40; ETA 2026-10-03 |
| gas | NOT_PROMOTABLE(14) | — | — | — | — | — | — | — | — | — | — | 14 settlement_date / floor 40; ETA — |
| mention | NOT_READY | — | — | — | — | — | — | — | — | — | — | 0 event_ticker / floor 40; ETA — |
| tweets | NOT_READY | — | — | — | — | — | — | — | — | — | — | 0 event_ticker / floor 40; ETA — |
| crypto_annual | NOT_READY | — | — | — | — | — | — | — | — | — | — | 0 event_ticker / floor 40; ETA 2027-01-01 |
