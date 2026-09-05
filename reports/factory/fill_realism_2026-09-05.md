# Fill-realism study 2026-09-05 -- KXHIGH on the maia tape

**p90 adverse drift = 0.0000 (basis: next poll; 20 s window p90=None n=0, next-poll p90=0.0 n=72) does not exceed 1c: adverse_fill=0.01 stands**

Note: Tape collected live from http://maia.local:8050/api/logs/data (last-100-rows route, no history parameters -- src/web/server.py get_data_log) by polling every 3 s from 2026-09-05 01:21:10Z; this run covers the 02:00Z decision boundary. Per-market poll gap p50 ~37 s / p90 ~68 s (the bot rotates cities), so the 20 s window is sparse and the next-poll drift is the conservative upper bound the recommendation uses.

- tape rows 14088, KXHIGH market rows 2690, markets 48, first 2026-09-05T01:21:10.557105+00:00, last 2026-09-05T02:02:26.041362+00:00
- UTC hours with a decision point: 1 (02:00)
- poll gap (s): {'n': 2642, 'p50': 34.9862, 'p90': 74.262644, 'p95': 90.86059, 'max': 227.020522, 'mean': 43.385372440575324, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- decision lag after :00 (s): {'n': 48, 'p50': 5.244867, 'p90': 49.025829, 'p95': 49.026053, 'max': 49.026278, 'mean': 23.18466325, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- counts: {'decision_polls': 48, 'gap_no_followup_20s_no': 48, 'gap_no_followup_20s_yes': 48, 'gap_no_followup_60s_no': 12, 'gap_no_followup_60s_yes': 12, 'gap_no_next_poll_60s_no': 12, 'gap_no_next_poll_60s_yes': 12, 'market_hours': 48}

## Adverse drift of the traded-side ask (max(0, max ask_t - ask_0))

| window | side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|---|
| 20s | yes_ask | 0 | None | None | None | None | None | None |
| 20s | no_ask | 0 | None | None | None | None | None | None |
| 20s | both_sides | 0 | None | None | None | None | None | None |
| 60s | yes_ask | 36 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 60s | no_ask | 36 | 0.0 | 0.0 | 0.0 | 0.010000000000000009 | 0.027777777777777776 | 0.0 |
| 60s | both_sides | 72 | 0.0 | 0.0 | 0.0 | 0.010000000000000009 | 0.013888888888888888 | 0.0 |

## Next-poll adverse drift (max(0, ask_next - ask_0); upper bound on the 20 s drift at this cadence)

| side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|
| yes_ask | 36 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| no_ask | 36 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| both_sides | 72 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |

- gap from the decision poll to the next poll (s): {'n': 72, 'p50': 29.255956, 'p90': 30.453351, 'p95': 30.453458, 'max': 30.453462, 'mean': 27.22696708333333, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- signed next-poll drift, YES ask: {'n': 36, 'p50': 0.0, 'p90': 0.0, 'p95': 0.0, 'max': 0.0, 'mean': 0.0, 'share_gt_0': 0.0, 'share_gt_1c': 0.0}
- signed next-poll drift, NO ask: {'n': 36, 'p50': 0.0, 'p90': 0.0, 'p95': 0.0, 'max': 0.0, 'mean': 0.0, 'share_gt_0': 0.0, 'share_gt_1c': 0.0}

## Recommendation

- p90 at the primary 20.0 s window (both sides): None
- p90 next poll (both sides): 0.0
- p90 used: 0.0 (basis: next poll)
- p90 exceeds 1c: False
- recommended adverse_fill = max(0.01, ceil_to_cent(p90)) = 0.01

Per city: {"KXHIGHCHI": {"decision_polls": 12, "samples_60s": 24}, "KXHIGHLAX": {"decision_polls": 12, "samples_60s": 24}, "KXHIGHMIA": {"decision_polls": 12}, "KXHIGHNY": {"decision_polls": 12, "samples_60s": 24}}
