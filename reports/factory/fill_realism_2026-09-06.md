# Fill-realism study 2026-09-06 -- KXHIGH on the maia tape

**p90 adverse drift = 0.0000 (basis: next poll; 20 s window p90=None n=0, next-poll p90=0.0 n=156) does not exceed 1c: adverse_fill=0.01 stands**

Note: Daytime collector owed by the F3 registered deviation on FR-5.2: covers the 14:00Z, 15:00Z and 16:00Z decision boundaries -- the hours the deployed genome 0c4b20502f2daf65 actually trades (15Z 34.6%, 16Z 16.2%), against the 02Z/03Z of the 2026-09-05 runs where it has none. Polled every 3s from 13:10Z to 16:38Z on 2026-09-06 (819 polls, 0 errors). Analysed at HEAD with the zero-ask rule (a 0.00 ask is an empty book, not a price).

- tape rows 67434, KXHIGH market rows 12657, markets 48, first 2026-09-06T13:10:11.963276+00:00, last 2026-09-06T16:38:05.633729+00:00
- UTC hours with a decision point: 3 (14:00, 15:00, 16:00)
- poll gap (s): {'n': 12609, 'p50': 34.947842, 'p90': 80.316302, 'p95': 110.51408, 'max': 371.007705, 'mean': 47.01495719716076, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- decision lag after :00 (s): {'n': 132, 'p50': 17.063177, 'p90': 52.783129, 'p95': 55.302413, 'max': 55.303095, 'mean': 23.022771492424244, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- counts: {'decision_polls': 132, 'gap_no_decision_poll': 12, 'gap_no_followup_20s_no': 132, 'gap_no_followup_20s_yes': 120, 'gap_no_followup_60s_no': 48, 'gap_no_followup_60s_yes': 48, 'gap_no_next_poll_60s_no': 48, 'gap_no_next_poll_60s_yes': 48, 'market_hours': 144, 'missing_quote_yes': 12}

## Adverse drift of the traded-side ask (max(0, max ask_t - ask_0))

| window | side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|---|
| 20s | yes_ask | 0 | None | None | None | None | None | None |
| 20s | no_ask | 0 | None | None | None | None | None | None |
| 20s | both_sides | 0 | None | None | None | None | None | None |
| 60s | yes_ask | 72 | 0.0 | 0.0 | 0.010000000000000009 | 0.020000000000000018 | 0.05555555555555555 | 0.027777777777777776 |
| 60s | no_ask | 84 | 0.0 | 0.0 | 0.0 | 0.020000000000000018 | 0.047619047619047616 | 0.011904761904761904 |
| 60s | both_sides | 156 | 0.0 | 0.0 | 0.010000000000000009 | 0.020000000000000018 | 0.05128205128205128 | 0.019230769230769232 |

## Next-poll adverse drift (max(0, ask_next - ask_0); upper bound on the 20 s drift at this cadence)

| side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|
| yes_ask | 72 | 0.0 | 0.0 | 0.010000000000000009 | 0.020000000000000018 | 0.05555555555555555 | 0.027777777777777776 |
| no_ask | 84 | 0.0 | 0.0 | 0.0 | 0.020000000000000018 | 0.047619047619047616 | 0.011904761904761904 |
| both_sides | 156 | 0.0 | 0.0 | 0.010000000000000009 | 0.020000000000000018 | 0.05128205128205128 | 0.019230769230769232 |

- gap from the decision poll to the next poll (s): {'n': 156, 'p50': 37.418748, 'p90': 55.707715, 'p95': 55.707767, 'max': 55.70778, 'mean': 40.219264358974364, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- signed next-poll drift, YES ask: {'n': 72, 'p50': 0.0, 'p90': 0.0, 'p95': 0.010000000000000009, 'max': 0.020000000000000018, 'mean': -0.0019444444444444435, 'share_gt_0': 0.05555555555555555, 'share_gt_1c': 0.027777777777777776}
- signed next-poll drift, NO ask: {'n': 84, 'p50': 0.0, 'p90': 0.0, 'p95': 0.0, 'max': 0.020000000000000018, 'mean': -0.019285714285714288, 'share_gt_0': 0.047619047619047616, 'share_gt_1c': 0.011904761904761904}

## Recommendation

- p90 at the primary 20.0 s window (both sides): None
- p90 next poll (both sides): 0.0
- p90 used: 0.0 (basis: next poll)
- p90 exceeds 1c: False
- recommended adverse_fill = max(0.01, ceil_to_cent(p90)) = 0.01

Per city: {"KXHIGHCHI": {"decision_polls": 36, "samples_60s": 42}, "KXHIGHLAX": {"decision_polls": 36, "samples_60s": 42}, "KXHIGHMIA": {"decision_polls": 24}, "KXHIGHNY": {"decision_polls": 36, "samples_60s": 72}}

## Examples of drift > 1c

- KXHIGHNY-26SEP06-B75.5 2026-09-06T16:00:00+00:00 yes 60s: 0.36 -> 0.38 (+0.02)
- KXHIGHNY-26SEP06-B73.5 2026-09-06T16:00:00+00:00 no 60s: 0.38 -> 0.4 (+0.02)
- KXHIGHCHI-26SEP06-B77.5 2026-09-06T14:00:00+00:00 yes 60s: 0.37 -> 0.39 (+0.02)
