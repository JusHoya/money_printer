# Decoder independence: in-house GRIB2 decode vs Open-Meteo `gfs025`

Generated 2026-07-27T05:09:45Z by `scripts/verify_decoder_independence.py`. Every number below was measured by that script on the run recorded here; none is hand-entered or carried over from another report.

## Why the existing cross-check was not enough

`reports/phase2/ec1_ensemble_members.md` compares the 31 decoded members against NCEP's own `geavg` product. That is a real check -- it catches a member-selection, windowing or interval fault -- but **it is not decoder-independent**: `geavg` is a GRIB2 record from the same bucket, decoded by the same `src/data/ensemble_provider.py`. A global fault in that decoder (Kelvin offset, binary/decimal scale exponent, sign, hemisphere, scan mode) moves both sides identically and cancels. The check would pass with every published temperature wrong by tens of degrees.

This report supplies the missing independence by comparing against **Open-Meteo's `gfs025` ensemble API** -- the same NCEP GEFS product, decoded by an entirely separate GRIB2 implementation (Open-Meteo's Swift stack), operated by someone else.

## Configuration

| Setting | Value |
| --- | --- |
| Model cycle | `2026-07-27T00:00:00Z` |
| Field | `TMP:2 m above ground` (instantaneous) |
| Forecast hours | f006, f012, f018 |
| Members (in-house) | 31 (`gec00` + `gep01`..`gep30`) |
| Cities | NY, CHI, LAX, MIA |
| Reference | Open-Meteo `gfs025` ensemble, `temperature_2m`, `cell_selection=nearest` |
| City-hours compared | 12 |

## Grid nodes: both sides asked for the same cell

Open-Meteo is queried at the **GEFS node's own coordinate**, not the station's, so the comparison is not confounded by two different nearest-cell rules. The coordinate the API served back is recorded, not assumed.

| City | Station | GEFS node (j, i) | Node lat/lon | Station-to-node | Open-Meteo served lat/lon | Its elevation |
| --- | --- | --- | --- | --- | --- | --- |
| NY | KNYC | (197, 1144) | 40.75, -74.00 | 4.6 km | 40.75, -74.0 | 32.0 m |
| CHI | KMDW | (193, 1089) | 41.75, -87.75 | 3.8 km | 41.75, -87.75 | 189.0 m |
| LAX | KLAX | (224, 966) | 34.00, -118.50 | 12.3 km | 34.0, -118.5 | 0.0 m |
| MIA | KMIA | (257, 1119) | 25.75, -80.25 | 8.0 km | 25.75, -80.25 | 12.0 m |

## Result

**Verdict: DECODER INDEPENDENTLY CORROBORATED.**

- Overall mean bias (in-house minus Open-Meteo), pooled over 12 city-hours: **+0.06 degF**.
- Per city-hour mean bias spans **-1.36 to +3.18 degF**.
- Sorted member distributions agree to a mean absolute **1.04 degF** per order statistic, worst single rank **7.16 degF**.

Where the residual sits, measured rather than asserted: mean absolute bias by forecast hour is f006 0.77, f012 0.52, f018 1.69 degF, i.e. it concentrates at the afternoon hour where both ensembles are widest. The largest single bias is **CHI 2026-07-27T18:00:00Z** at +3.18 degF, and the largest single order-statistic gap is **CHI 2026-07-27T18:00:00Z** at 7.16 degF -- the same city-hour whose ensemble sigma disagrees most (2.10 here vs 4.22 there). Sigma ratios across all city-hours span 0.50..1.22. Two ensembles drawn from the same distribution but different cycles disagree most in exactly that place -- in the tails, on the convectively active hour -- so this shape is consistent with the member-identity caveat below and is not the signature of a scale or offset fault, which would be uniform across every hour and every rank.

A Kelvin-to-Fahrenheit slip is worth ~460 degF, a Kelvin-left-as-Celsius slip ~273 degF, a decimal-scale exponent error a factor of ten, a sign error a reflection about zero, and a hemisphere or scan-mode error puts the sample on the wrong continent -- typically tens of degrees. Nothing of that magnitude is present: the largest discrepancy anywhere in the table is under 10 degF, and the residual that remains is the size expected from Open-Meteo's own elevation correction to 2 m temperature (which its API does not let a caller disable) plus a possible cycle difference.

## Per city-hour

| City | Valid (UTC) | f | n | Mean in-house | Mean Open-Meteo | Bias | Sigma in-house | Sigma Open-Meteo | Sigma ratio | Sorted-rank delta (min..max) | Mean abs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NY | 2026-07-27T06:00:00Z | f006 | 31 | 72.91 | 73.73 | **-0.81** | 0.47 | 0.57 | 0.82 | -1.32..-0.46 | 0.81 |
| NY | 2026-07-27T12:00:00Z | f012 | 31 | 73.62 | 74.11 | **-0.49** | 0.46 | 0.62 | 0.74 | -0.85..-0.01 | 0.49 |
| NY | 2026-07-27T18:00:00Z | f018 | 31 | 85.37 | 83.74 | **+1.63** | 1.18 | 1.26 | 0.93 | +1.24..+1.98 | 1.63 |
| CHI | 2026-07-27T06:00:00Z | f006 | 31 | 76.76 | 76.43 | **+0.33** | 0.49 | 0.89 | 0.54 | -0.42..+1.15 | 0.49 |
| CHI | 2026-07-27T12:00:00Z | f012 | 31 | 75.46 | 75.19 | **+0.27** | 0.80 | 1.18 | 0.68 | -0.61..+1.46 | 0.42 |
| CHI | 2026-07-27T18:00:00Z | f018 | 31 | 91.61 | 88.43 | **+3.18** | 2.10 | 4.22 | 0.50 | -0.35..+7.16 | 3.23 |
| LAX | 2026-07-27T06:00:00Z | f006 | 31 | 74.88 | 76.25 | **-1.36** | 0.48 | 0.72 | 0.68 | -1.73..-0.92 | 1.36 |
| LAX | 2026-07-27T12:00:00Z | f012 | 31 | 72.94 | 73.97 | **-1.03** | 0.49 | 0.52 | 0.93 | -1.42..-0.83 | 1.03 |
| LAX | 2026-07-27T18:00:00Z | f018 | 31 | 80.71 | 81.74 | **-1.03** | 0.69 | 0.66 | 1.05 | -1.38..-0.26 | 1.03 |
| MIA | 2026-07-27T06:00:00Z | f006 | 31 | 83.06 | 83.66 | **-0.60** | 0.81 | 0.67 | 1.22 | -1.22..-0.31 | 0.60 |
| MIA | 2026-07-27T12:00:00Z | f012 | 31 | 82.84 | 83.12 | **-0.29** | 0.38 | 0.45 | 0.85 | -0.83..-0.16 | 0.29 |
| MIA | 2026-07-27T18:00:00Z | f018 | 31 | 87.96 | 87.05 | **+0.91** | 2.14 | 2.86 | 0.75 | -1.14..+2.94 | 1.10 |

## Open caveat: per-member identity is NOT established

The comparison above is between *distributions*. It does not show that this repo's `gepNN` is Open-Meteo's `temperature_2m_memberNN`.

| Quantity | Value |
| --- | --- |
| Mean per-member correlation, identity labelling | 0.0082 |
| Largest \|r\| under identity labelling | 0.2705 |
| Largest \|r\| under **any** cyclic relabelling | 0.4425 |

Per-member correlation is near zero and **no cyclic relabelling recovers it**, while the sorted distributions match closely. That signature is what a *different model cycle* looks like: two draws from the same forecast distribution, member for member unrelated. The Open-Meteo ensemble API publishes no initialisation time, so this cannot be confirmed from the response, and no alternative explanation has been excluded either.

**Recorded as an open item, not as a finding.** It does not weaken the conclusion this report is for -- a global decode fault would move the *distribution*, and the distributions agree -- but any future work that depends on member identity (per-member bias correction, member tracking across cycles) must establish it separately.

## Reproduce

```bash
$env:PYTHONPATH = "."
python scripts/verify_decoder_independence.py --init 2026-07-27T00 \
  --fhours 6,12,18 \
  --json-out reports/phase2/ws_g_decoder_independence.json \
  --md-out reports/phase2/ws_g_decoder_independence.md
```

Both sides are live services. GEFS records are immutable once published and re-run identically for as long as NODD retains the cycle; Open-Meteo serves whatever cycle is current, so its numbers will move. Full machine-readable evidence, including every member value on both sides: `reports/phase2/ws_g_decoder_independence.json`.
