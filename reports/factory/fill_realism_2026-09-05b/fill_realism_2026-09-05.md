# Fill-realism study 2026-09-05 -- KXHIGH on the maia tape

**p90 adverse drift = 0.0000 (basis: next poll; 20 s window p90=None n=0, next-poll p90=0.0 n=60) does not exceed 1c: adverse_fill=0.01 stands**

- tape rows 15950, KXHIGH market rows 2918, markets 48, first 2026-09-05T02:21:12.504930+00:00, last 2026-09-05T03:10:03.715138+00:00
- UTC hours with a decision point: 1 (03:00)
- poll gap (s): {'n': 2870, 'p50': 39.805029, 'p90': 80.145419, 'p95': 123.050405, 'max': 339.626903, 'mean': 48.293503779790946, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- decision lag after :00 (s): {'n': 36, 'p50': 9.617981, 'p90': 14.356369, 'p95': 14.365305, 'max': 14.370157, 'mean': 8.085756833333333, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- counts: {'decision_polls': 36, 'gap_no_decision_poll': 12, 'gap_no_followup_20s_no': 36, 'gap_no_followup_20s_yes': 36, 'gap_no_followup_60s_no': 6, 'gap_no_followup_60s_yes': 6, 'gap_no_next_poll_60s_no': 6, 'gap_no_next_poll_60s_yes': 6, 'market_hours': 48}

## Adverse drift of the traded-side ask (max(0, max ask_t - ask_0))

| window | side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|---|
| 20s | yes_ask | 0 | None | None | None | None | None | None |
| 20s | no_ask | 0 | None | None | None | None | None | None |
| 20s | both_sides | 0 | None | None | None | None | None | None |
| 60s | yes_ask | 30 | 0.0 | 0.0 | 0.0 | 0.010000000000000009 | 0.03333333333333333 | 0.0 |
| 60s | no_ask | 30 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 60s | both_sides | 60 | 0.0 | 0.0 | 0.0 | 0.010000000000000009 | 0.016666666666666666 | 0.0 |

## Next-poll adverse drift (max(0, ask_next - ask_0); upper bound on the 20 s drift at this cadence)

| side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|
| yes_ask | 30 | 0.0 | 0.0 | 0.0 | 0.010000000000000009 | 0.03333333333333333 | 0.0 |
| no_ask | 30 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| both_sides | 60 | 0.0 | 0.0 | 0.0 | 0.010000000000000009 | 0.016666666666666666 | 0.0 |

- gap from the decision poll to the next poll (s): {'n': 60, 'p50': 30.819692, 'p90': 57.111817, 'p95': 57.118991, 'max': 57.123673, 'mean': 33.850921, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- signed next-poll drift, YES ask: {'n': 30, 'p50': 0.0, 'p90': 0.0, 'p95': 0.0, 'max': 0.010000000000000009, 'mean': 0.00033333333333333365, 'share_gt_0': 0.03333333333333333, 'share_gt_1c': 0.0}
- signed next-poll drift, NO ask: {'n': 30, 'p50': 0.0, 'p90': 0.0, 'p95': 0.0, 'max': 0.0, 'mean': -0.00033333333333333365, 'share_gt_0': 0.0, 'share_gt_1c': 0.0}

## Recommendation

- p90 at the primary 20.0 s window (both sides): None
- p90 next poll (both sides): 0.0
- p90 used: 0.0 (basis: next poll)
- p90 exceeds 1c: False
- recommended adverse_fill = max(0.01, ceil_to_cent(p90)) = 0.01

Per city: {"KXHIGHCHI": {"decision_polls": 12, "samples_60s": 24}, "KXHIGHLAX": {"decision_polls": 12, "samples_60s": 12}, "KXHIGHNY": {"decision_polls": 12, "samples_60s": 24}}
