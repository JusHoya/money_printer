# Fill-realism study 2026-09-05 -- KXHIGH on the maia tape

**no drift samples (no covered :00 boundary with a follow-up poll); no recommendation can be made**

Note: Tape collected 2026-09-05 01:21:10Z..01:43:13Z by polling http://maia.local:8050/api/logs/data every 3 s (the route returns only the LAST 100 rows, ~15 s of tape, with no history parameters -- src/web/server.py get_data_log -- so no earlier polls were fetchable). The collected span contains NO :00 UTC boundary with a poll on both sides, so there are zero decision points and no p90; the study must be re-run on a tape spanning at least one :00 UTC boundary (collector: --url ... --collect-seconds 7200 --cache ...). Per-market poll gap on this tape: p50 37 s, p90 68 s, max 210 s (the bot rotates cities; the 14-s figure is the whole-tape cadence, not the per-market one).

- tape rows 7810, KXHIGH market rows 1436, markets 48, first 2026-09-05T01:21:10.557105+00:00, last 2026-09-05T01:43:13.502683+00:00
- UTC hours with a decision point: 0 ()
- poll gap (s): {'n': 1388, 'p50': 37.323907, 'p90': 67.874991, 'p95': 98.241991, 'max': 209.972212, 'mean': 42.3597533832853, 'share_gt_0': 1.0, 'share_gt_1c': 1.0}
- decision lag after :00 (s): {'n': 0, 'p50': None, 'p90': None, 'p95': None, 'max': None, 'mean': None, 'share_gt_0': None, 'share_gt_1c': None}
- counts: {}

## Adverse drift of the traded-side ask (max(0, max ask_t - ask_0))

| window | side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|---|
| 20s | yes_ask | 0 | None | None | None | None | None | None |
| 20s | no_ask | 0 | None | None | None | None | None | None |
| 20s | both_sides | 0 | None | None | None | None | None | None |
| 60s | yes_ask | 0 | None | None | None | None | None | None |
| 60s | no_ask | 0 | None | None | None | None | None | None |
| 60s | both_sides | 0 | None | None | None | None | None | None |

## Next-poll adverse drift (max(0, ask_next - ask_0); upper bound on the 20 s drift at this cadence)

| side | n | p50 | p90 | p95 | max | share > 0 | share > 1c |
|---|---|---|---|---|---|---|---|
| yes_ask | 0 | None | None | None | None | None | None |
| no_ask | 0 | None | None | None | None | None | None |
| both_sides | 0 | None | None | None | None | None | None |

- gap from the decision poll to the next poll (s): None
- signed next-poll drift, YES ask: {'n': 0, 'p50': None, 'p90': None, 'p95': None, 'max': None, 'mean': None, 'share_gt_0': None, 'share_gt_1c': None}
- signed next-poll drift, NO ask: {'n': 0, 'p50': None, 'p90': None, 'p95': None, 'max': None, 'mean': None, 'share_gt_0': None, 'share_gt_1c': None}

## Recommendation

- p90 at the primary 20.0 s window (both sides): None
- p90 next poll (both sides): None
- p90 used: None (basis: None)
- p90 exceeds 1c: None
- recommended adverse_fill = max(0.01, ceil_to_cent(p90)) = None

Per city: {}
