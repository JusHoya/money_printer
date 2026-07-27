# EC-1 evidence: GEFS ensemble members per city per cycle

Generated 2026-07-27T03:49:27Z by `scripts/fetch_ensemble.py`. Every number below was measured by that script; none is hand-entered.

**Criterion.** PRD Phase 2 exit criterion 1 (first half): the ensemble provider returns >=20 members for each of the 4 cities on >=5 consecutive days

**Verdict: MET** -- 20/20 city-cycle runs cleared the 20-member floor across 5 consecutive cycles; the lowest member count on any successful run was 31.

## Configuration

| Setting | Value |
| --- | --- |
| Source | `gefs_aws_pgrb2sp25` (NOAA NODD, anonymous HTTPS) |
| Field | `TMAX:2 m above ground` (max over interval) |
| Members requested | 31 (`gec00` + `gep01`..`gep30`) |
| Member floor | 20 |
| Cycles | 5 consecutive 00Z runs from 2026-07-20 |
| Target | init date + 1 day(s), local calendar day |
| Cities | NY, CHI, LAX, MIA |

## Grid nodes actually used

| City | Station | Station lat/lon | Nearest 0.25 deg node (j, i) | Node lat/lon | Distance |
| --- | --- | --- | --- | --- | --- |
| NY | KNYC | 40.78333, -73.96667 | (197, 1144) | 40.75, -74.00 | 4.6 km |
| CHI | KMDW | 41.78417, -87.75528 | (193, 1089) | 41.75, -87.75 | 3.8 km |
| LAX | KLAX | 33.93806, -118.38889 | (224, 966) | 34.00, -118.50 | 12.3 km |
| MIA | KMIA | 25.79056, -80.31639 | (257, 1119) | 25.75, -80.25 | 8.0 km |

## Per city-cycle results

| Cycle (UTC) | Target (local) | City | Members | Lead h | Mean F | Sigma F | Min F | Max F | S3 objects | Fetched |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-07-20T00:00:00Z | 2026-07-21 | NY | 31 | 28 | 83.22 | 2.93 | 79.10 | 90.95 | 155 | 2026-07-27T03:34:15Z |
| 2026-07-20T00:00:00Z | 2026-07-21 | CHI | 31 | 29 | 84.60 | 2.16 | 79.11 | 88.25 | 155 | 2026-07-27T03:43:04Z |
| 2026-07-20T00:00:00Z | 2026-07-21 | LAX | 31 | 31 | 81.45 | 1.39 | 78.28 | 83.75 | 155 | 2026-07-27T03:34:48Z |
| 2026-07-20T00:00:00Z | 2026-07-21 | MIA | 31 | 28 | 88.76 | 0.77 | 86.54 | 89.94 | 155 | 2026-07-27T03:35:05Z |
| 2026-07-21T00:00:00Z | 2026-07-22 | NY | 31 | 28 | 87.67 | 2.57 | 82.23 | 91.67 | 155 | 2026-07-27T03:35:26Z |
| 2026-07-21T00:00:00Z | 2026-07-22 | CHI | 31 | 29 | 77.18 | 2.31 | 71.80 | 80.45 | 155 | 2026-07-27T03:35:43Z |
| 2026-07-21T00:00:00Z | 2026-07-22 | LAX | 31 | 31 | 81.86 | 1.70 | 78.98 | 85.52 | 155 | 2026-07-27T03:36:00Z |
| 2026-07-21T00:00:00Z | 2026-07-22 | MIA | 31 | 28 | 88.92 | 0.82 | 87.10 | 90.71 | 155 | 2026-07-27T03:36:17Z |
| 2026-07-22T00:00:00Z | 2026-07-23 | NY | 31 | 28 | 84.94 | 2.20 | 80.08 | 88.97 | 155 | 2026-07-27T03:36:37Z |
| 2026-07-22T00:00:00Z | 2026-07-23 | CHI | 31 | 29 | 79.09 | 1.98 | 74.39 | 82.67 | 155 | 2026-07-27T03:36:56Z |
| 2026-07-22T00:00:00Z | 2026-07-23 | LAX | 31 | 31 | 86.84 | 1.62 | 83.90 | 90.72 | 155 | 2026-07-27T03:37:13Z |
| 2026-07-22T00:00:00Z | 2026-07-23 | MIA | 31 | 28 | 89.46 | 0.53 | 88.51 | 90.72 | 155 | 2026-07-27T03:37:30Z |
| 2026-07-23T00:00:00Z | 2026-07-24 | NY | 31 | 28 | 85.02 | 1.36 | 82.02 | 88.25 | 155 | 2026-07-27T03:31:42Z |
| 2026-07-23T00:00:00Z | 2026-07-24 | CHI | 31 | 29 | 78.72 | 2.14 | 74.57 | 82.10 | 155 | 2026-07-27T03:37:50Z |
| 2026-07-23T00:00:00Z | 2026-07-24 | LAX | 31 | 31 | 87.71 | 2.00 | 83.93 | 92.57 | 155 | 2026-07-27T03:38:07Z |
| 2026-07-23T00:00:00Z | 2026-07-24 | MIA | 31 | 28 | 89.23 | 0.96 | 87.53 | 91.31 | 155 | 2026-07-27T03:38:24Z |
| 2026-07-24T00:00:00Z | 2026-07-25 | NY | 31 | 28 | 82.77 | 2.40 | 75.12 | 87.18 | 155 | 2026-07-27T03:32:01Z |
| 2026-07-24T00:00:00Z | 2026-07-25 | CHI | 31 | 29 | 82.48 | 2.16 | 76.66 | 86.10 | 155 | 2026-07-27T03:38:44Z |
| 2026-07-24T00:00:00Z | 2026-07-25 | LAX | 31 | 31 | 86.06 | 1.58 | 83.22 | 89.18 | 155 | 2026-07-27T03:39:03Z |
| 2026-07-24T00:00:00Z | 2026-07-25 | MIA | 31 | 28 | 89.83 | 1.16 | 86.90 | 91.57 | 155 | 2026-07-27T03:39:21Z |

## Coverage windows (over-coverage is reported, not hidden)

| Cycle | City | Local day (UTC) | Requested leads | Covered leads | Spill before/after |
| --- | --- | --- | --- | --- | --- |
| 2026-07-20T00:00:00Z | NY | 2026-07-21T04:00:00Z .. 2026-07-22T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-20T00:00:00Z | CHI | 2026-07-21T05:00:00Z .. 2026-07-22T05:00:00Z | 29-53 | 24-54 | 5 h / 1 h |
| 2026-07-20T00:00:00Z | LAX | 2026-07-21T07:00:00Z .. 2026-07-22T07:00:00Z | 31-55 | 30-57 | 1 h / 2 h |
| 2026-07-20T00:00:00Z | MIA | 2026-07-21T04:00:00Z .. 2026-07-22T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-21T00:00:00Z | NY | 2026-07-22T04:00:00Z .. 2026-07-23T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-21T00:00:00Z | CHI | 2026-07-22T05:00:00Z .. 2026-07-23T05:00:00Z | 29-53 | 24-54 | 5 h / 1 h |
| 2026-07-21T00:00:00Z | LAX | 2026-07-22T07:00:00Z .. 2026-07-23T07:00:00Z | 31-55 | 30-57 | 1 h / 2 h |
| 2026-07-21T00:00:00Z | MIA | 2026-07-22T04:00:00Z .. 2026-07-23T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-22T00:00:00Z | NY | 2026-07-23T04:00:00Z .. 2026-07-24T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-22T00:00:00Z | CHI | 2026-07-23T05:00:00Z .. 2026-07-24T05:00:00Z | 29-53 | 24-54 | 5 h / 1 h |
| 2026-07-22T00:00:00Z | LAX | 2026-07-23T07:00:00Z .. 2026-07-24T07:00:00Z | 31-55 | 30-57 | 1 h / 2 h |
| 2026-07-22T00:00:00Z | MIA | 2026-07-23T04:00:00Z .. 2026-07-24T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-23T00:00:00Z | NY | 2026-07-24T04:00:00Z .. 2026-07-25T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-23T00:00:00Z | CHI | 2026-07-24T05:00:00Z .. 2026-07-25T05:00:00Z | 29-53 | 24-54 | 5 h / 1 h |
| 2026-07-23T00:00:00Z | LAX | 2026-07-24T07:00:00Z .. 2026-07-25T07:00:00Z | 31-55 | 30-57 | 1 h / 2 h |
| 2026-07-23T00:00:00Z | MIA | 2026-07-24T04:00:00Z .. 2026-07-25T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-24T00:00:00Z | NY | 2026-07-25T04:00:00Z .. 2026-07-26T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |
| 2026-07-24T00:00:00Z | CHI | 2026-07-25T05:00:00Z .. 2026-07-26T05:00:00Z | 29-53 | 24-54 | 5 h / 1 h |
| 2026-07-24T00:00:00Z | LAX | 2026-07-25T07:00:00Z .. 2026-07-26T07:00:00Z | 31-55 | 30-57 | 1 h / 2 h |
| 2026-07-24T00:00:00Z | MIA | 2026-07-25T04:00:00Z .. 2026-07-26T04:00:00Z | 28-52 | 24-54 | 4 h / 2 h |

## Sanity check against CLI settlement truth

Paired against CLI settlement truth on a 5-day sample. This is an order-of-magnitude sanity check on the decoder and the windowing, NOT the FR-2.2 calibration, which requires >=60 paired days.

| City | Paired days | Bias (nearest node) F | Bias (bilinear diagnostic) F | MAE F |
| --- | --- | --- | --- | --- |
| NY | 5 | +2.92 | +2.74 | 2.92 |
| CHI | 5 | +1.61 | +1.61 | 2.28 |
| LAX | 5 | +5.78 | +5.61 | 5.78 |
| MIA | 5 | -3.16 | -2.08 | 3.16 |

Cross-check against NCEP's own `geavg` product on 20 city-cycles: member-mean minus geavg daily high ranges -0.048 F to +0.316 F (mean +0.096 F). `max` and `mean` do not commute, so an exact match is not expected. This agreement rules out a fault in **member selection, the TMAX interval algebra or the local-day windowing** -- any of those would move the member mean away from `geavg` by degrees.

**It is not decoder-independent, and must not be read as such.** `geavg` is a GRIB2 record from the same bucket decoded by the *same* in-house decoder, so a global decode fault -- a Kelvin offset, a binary/decimal scale exponent, a sign, a hemisphere or scan-mode error -- shifts both sides identically and cancels exactly here. Independence is evidenced separately, against a different GRIB2 implementation, in `reports/phase2/ws_g_decoder_independence.md`.

## Instantaneous `TMP` versus interval `TMAX`

Daily high built from 3-hourly instantaneous TMP versus the interval TMAX this provider uses. A negative delta is the under-estimation the instantaneous field would have introduced.

| City | Members | TMP daily high (mean F) | TMAX daily high (mean F) | TMP - TMAX |
| --- | --- | --- | --- | --- |
| NY | 31 | 81.96 | 82.77 | -0.81 |
| CHI | 31 | 82.05 | 82.48 | -0.42 |
| LAX | 31 | 85.15 | 86.06 | -0.91 |
| MIA | 31 | 89.20 | 89.83 | -0.63 |

## Reproduce

```bash
PYTHONPATH=. python scripts/fetch_ensemble.py --start-init 2026-07-20 --days 5 --validate --tmp-comparison
```

Full machine-readable evidence, including every S3 key, byte range and per-member value: `reports/phase2/ec1_ensemble_members.json`.
