# Workstream G -- Phase 2 remediation: reproducibility and publication defects

Closes four defects an independent red team raised against Phase 2 after
verifying exit criteria 1-3 as MET. None of them was a wrong number; all four
were about the *documented path*, the *publication clause*, or the *strength of
a claim*. No measured value in any Phase 2 artifact moved.

**Branch:** `phase-2-forecast-calibration`. Generated 2026-07-27.

---

## E1 -- the documented rebuild command emitted a different artifact

### The defect

`data/calibration/<CITY>_gefs_v1.json` carries a top-level `statistic` block.
That block is not decoration: it names which statistic `forecast_high_f`
actually is, and warns that the backfill statistic `max_t(geavg)` is **not** the
live-path statistic `mean_m(max_t member)` that `EnsembleProvider.fetch()`
returns. Nothing else in the artifact distinguishes the two.

The block was stamped by `scripts/backfill_ensemble_history.py` *after* it
called `forecast_calibration.build_all()`. The documented generic rebuild --

```bash
python scripts/build_calibration.py --source gefs --out-dir <tmp>
```

-- called the same `build_all()` and stopped there. It produced the same
numbers with the block **stripped**: a **10 402-byte** file where the committed
artifact is **11 932 bytes**, a different `content_hash`, and no statistic
warning at all. Running the documented command would have silently deleted the
guard and left behind a file that still looked authoritative.

### The fix

The stamping moved into the shared build path, behind a registry rather than a
branch:

* `src/calibration/forecast_calibration.py` gained `SOURCE_ANNOTATORS`, mapping
  a source label to a `"module:function"` hook, resolved **lazily** so that
  building `gfs_mex` never imports the ensemble/GRIB2 stack. `build_all()`
  applies the hook through `annotate()` before `finalize()` seals the hash.
* `annotate()` is **additive only**: an annotator may add top-level keys and can
  never overwrite one the calibrator computed, or any of the artifact's identity
  keys. A collision raises.
* An annotator that cannot be imported **raises** rather than emitting a
  stripped file. Degrading silently is the defect itself.
* `src/calibration/gefs_series.py` gained `statistic_block()` /
  `calibration_annotation()`, which own the block's content. It is a pure
  function of the city -- no clock, host, path or run id -- so determinism is
  unaffected.
* `scripts/backfill_ensemble_history.stamp_statistic()` now delegates to the
  same function and is **idempotent**; the backfill additionally refuses to
  write if `build_all()` did not apply the block.

### Verification

`scripts/build_calibration.py --source gefs --out-dir <tmp> --check-deterministic`,
byte-compared against the committed files:

| Artifact | Result |
| --- | --- |
| `NY_gefs_v1.json` | **byte-identical** (11 900 B) |
| `CHI_gefs_v1.json` | **byte-identical** (11 932 B) |
| `LAX_gefs_v1.json` | **byte-identical** (11 926 B) |
| `MIA_gefs_v1.json` | **byte-identical** (11 949 B) |

The four `*_gfs_mex_v1.json` files were re-derived the same way and are
**byte-identical to what was on disk before this remediation** -- the annotator
registry has no entry for `gfs_mex`, and a test pins that it gains nothing.

---

## E2 -- EC-3's publication clause was satisfied for only one source

### The defect

PRD §8 Phase 2 exit criterion 3 requires the day-of sigma to be *published in
the calibration report*. Two problems:

1. `gfs_mex` sigma were published; `gefs` sigma -- the FR-2.1 **primary**
   source -- appeared only in `reports/phase2/ws_f_report.md`, a workstream
   report, not a calibration report.
2. Both sources wrote the **same filename**, `calibration_report_<date>.md`, so
   building `gefs` would have overwritten `gfs_mex`'s report. One file cannot
   publish two sources' sigma; the survivor was whichever ran last.

### The fix

* `report_filename(source, date)` -> `calibration_report_<source>_<date>.md`.
* The report renderer's source-specific narrative (header line, day-of
  provenance, margin discussion, caveats, reproduction commands) moved into
  per-source blocks keyed by source, instead of `gfs_mex` prose hard-coded in
  the renderer body.
* `reports/phase2/calibration_report_gefs_2026-07-26.md` now publishes the
  `gefs` EC-3 sigma table with the same structure and the same honesty.

### The published `gefs` verdict

| City | Station | n | bias degF | sigma degF | verdict |
| --- | --- | --- | --- | --- | --- |
| LAX | KLAX | 209 | -0.33 | **3.77** | PASS |
| CHI | KMDW | 209 | -1.10 | **3.76** | PASS |
| MIA | KMIA | 208 | -2.50 | **2.42** | PASS |
| NY | KNYC | 209 | +0.68 | **4.11** | **FAIL** |

**3 of 4 -> EC-3 MET.** NY is stated as **excluded per the criterion's own
rule**, not adjusted, re-bucketed or re-fitted, and its number is published in
full rather than omitted.

The knife-edge discussion is preserved and, for this source, strengthened --
because the sensitivity is now *computed by the renderer* rather than typed in.
`day_of_sensitivity()` splits the sample chronologically and recomputes
leave-one-out sigma. Its output reproduces workstream B's hand-written `gfs_mex`
CHI numbers exactly (first half 4.59, second half 2.78, LOO 3.59..3.99), which
is what validates it. Applied to `gefs`:

| City | pooled | first half (104-105 d) | second half | leave-one-out |
| --- | --- | --- | --- | --- |
| LAX | 3.77 | **4.05** | 3.34 | 3.72..3.78 |
| CHI | 3.76 | **4.10** | 3.31 | 3.65..3.77 |
| MIA | 2.42 | 3.00 | 1.33 | 2.23..2.42 |
| NY | 4.11 | **4.38** | 2.90 | 4.00..4.12 |

On the cold half the 4 degF bound holds at **MIA only**. Read literally, `gefs`
would **not** meet EC-3's "at least 3 of 4" on the first half of its own window;
it meets it on an annual sample that is roughly 60% warm-season. Leave-one-out
ranges are tight everywhere, so no single extraordinary day is holding any
verdict up or down. That statement is published in the report itself, not only
here.

### Effect on the `gfs_mex` report

Regenerated under the new name. Its content is **unchanged apart from the E4
section below**: the diff against the pre-remediation file is 10 added lines and
**0 removed or altered lines**. No number, table, caveat or verdict moved.

---

## E3 -- decoder-independence evidence, measured rather than asserted

### The defect

`reports/phase2/ec1_ensemble_members.md` presented a cross-check against NCEP's
`geavg` as external validation of the in-house GRIB2 decoder. It is not
decoder-independent: `geavg` is a GRIB2 record from the same bucket decoded by
the **same** `src/data/ensemble_provider.py`. A global fault -- Kelvin offset,
binary/decimal scale exponent, sign, hemisphere, scan mode -- moves both sides
identically and cancels exactly. The check would pass with every published
temperature wrong by tens of degrees.

### What was added

`scripts/verify_decoder_independence.py` decodes live GEFS `TMP:2 m above
ground` with the in-house decoder and compares against **Open-Meteo's `gfs025`
ensemble API**, an entirely separate GRIB2 implementation (Swift stack) run by a
different operator. Open-Meteo is queried at the **GEFS node's own coordinate**
with `cell_selection=nearest`, and the coordinate it serves back is recorded --
all four nodes matched exactly (40.75/-74.00, 41.75/-87.75, 34.00/-118.50,
25.75/-80.25), so the comparison is not confounded by two different nearest-cell
rules.

Evidence: `reports/phase2/ws_g_decoder_independence.md` (+ `.json`).

### Measured here, on this run

GEFS **2026-07-27 00Z**, 31 members x 3 valid times (06Z/12Z/18Z) x 4 cities =
12 city-hours, 372 member values per side.

| Quantity | Measured |
| --- | --- |
| Overall mean bias (in-house minus Open-Meteo) | **+0.06 degF** |
| Per city-hour mean bias | **-1.36 to +3.18 degF** |
| Ensemble sigma ratio (ours / theirs) | 0.50 .. 1.22 |
| Sorted order statistics, mean abs delta | **1.04 degF** |
| Sorted order statistics, worst single rank | **7.16 degF** |

**Verdict: decoder independently corroborated.** A Kelvin-to-Fahrenheit slip is
worth ~460 degF, a Kelvin-left-as-Celsius slip ~273 degF, a decimal-scale
exponent error a factor of ten, a sign error a reflection about zero, a
hemisphere or scan-mode error puts the sample on the wrong continent. Nothing of
that magnitude is present anywhere in the table.

The residual is not uniform, which is itself informative: mean absolute bias by
forecast hour is f006 0.77, f012 0.52, **f018 1.69** degF. Both the largest bias
(+3.18) and the largest order-statistic gap (7.16) are the same city-hour, CHI
18Z, whose ensemble sigma also disagrees most (2.10 here vs 4.22 there) -- the
convectively active afternoon, in the tails. A scale or offset fault would be
uniform across every hour and every rank; this is not.

> These are **this workstream's own measurements**, re-run rather than carried
> over. They are not identical to the red team's reported figures (+0.38 degF
> overall, -0.67 to +3.11 per city-hour) because Open-Meteo serves whatever
> cycle is current and its numbers move between runs. The conclusion is the
> same; the numbers above are the ones that were measured here.

### Open caveat, recorded and not resolved

Per-member identity is **not** established. Mean per-member correlation under
identity labelling is **0.008** (largest |r| 0.27), and **no cyclic relabelling
recovers it** (best |r| across all 31 shifts: 0.44), while the sorted
distributions match closely. That signature is what a *different model cycle*
looks like -- two draws from the same forecast distribution, member for member
unrelated. Open-Meteo's ensemble API publishes no initialisation time, so this
cannot be confirmed from the response, and no alternative explanation has been
excluded. It does not weaken the conclusion the check is for (a global decode
fault would move the distribution, and the distributions agree), but any future
work depending on member identity must establish it separately.

### Wording corrected

`reports/phase2/ec1_ensemble_members.md` no longer presents the `geavg`
comparison as decoder-independent. It now states what that comparison *does*
establish (member selection, TMAX interval algebra, local-day windowing), states
plainly that it is not decoder-independent and why, and points at
`ws_g_decoder_independence.md`. The same correction was made to the string in
`scripts/fetch_ensemble.py` that generates it, so a regeneration cannot revert
it; the corrected paragraphs were verified to match the generator's output
exactly.

---

## E4 -- "day-of" semantics, so Phase 3 cannot misuse the sigma

`day_of` is the lead bucket `[-24, 12)` h to the **start of the target local
day**. In both committed sources the leads actually observed are **4-8 h**, all
from the 00Z cycle issued the *evening before* -- roughly 10-16 h ahead of a
typical afternoon maximum. The bucket's lower edge would admit an intraday run
(negative lead); **no row in either sample has one**, and every bucket's
published `lead_hours_observed` is what proves it.

FR-3.1(b)'s lock-in strategy needs a **midday re-forecast**. These sigma were
not measured on one. The direction is known -- a shorter-lead forecast is
normally more accurate, so an evening-before sigma is an upper bound, and
reusing it widens the predictive distribution and shrinks the apparent edge --
but the size of the gap is unmeasured, and **conservative is not correct**: any
rule whose EV improves under a wider distribution (selling tails, pricing wide
brackets, sizing on a fat sigma) is flattered rather than penalised by the
substitution.

Stated in three places, all of which a reader of a number will pass through:

* `reports/phase2/calibration_report_gfs_mex_2026-07-26.md` -- new section
  immediately below the EC-3 sigma table.
* `reports/phase2/calibration_report_gefs_2026-07-26.md` -- same section.
* `src/calibration/forecast_calibration.py` module docstring.

Pinned by `test_the_report_states_what_day_of_may_not_be_used_for`.

---

## Verification summary

| Check | Result |
| --- | --- |
| `build_calibration.py --source gefs` vs committed | **4/4 byte-identical** |
| `build_calibration.py --source gfs_mex` vs pre-remediation | **4/4 byte-identical** |
| `gfs_mex` report vs pre-remediation | 10 lines added (E4), **0 removed or altered** |
| `go_no_go.py --as-of 2026-07-26` report | 68 967 B, **1 line differs** -- see below |
| `tests/test_forecast_calibration.py` | **37 passed** (12 new) |
| `tests/test_gefs_backfill.py` | **48 passed** |
| `tests/test_ev_analysis.py` + `tests/test_probability_engine.py` | **92 passed** |

### The one go/no-go line, in full

`reports/phase2/phase2_go_no_go_2026-07-26.md` regenerates at the same 68 967
bytes with exactly one line changed:

```
-* uncommitted paths at generation time: 40 ...
+* uncommitted paths at generation time: 37 ...
```

That is `len(git_state()["dirty_paths"])`, a `git status --porcelain` count
capped at 40 -- a snapshot of the working tree, not a measured result. It moved
because files were added to an uncommitted tree (this workstream added
`scripts/verify_decoder_independence.py`; a concurrent workstream's edits to
`src/core/`, `src/backtest/` and `src/bots/` appeared in the same window). The
counter is now saturated at its cap.

A structural diff of the machine-readable companion,
`ws_e_go_no_go_data_2026-07-26.json`, confirms the scope: the **only** differing
node is `/git/dirty_paths` (length 37 -> 40). Every EV number, table, sensitivity
run and verdict compares equal. No calibration-derived value moved, which is the
property that mattered -- `go_no_go.py` reads `data/calibration/*.json`, and
those files are byte-identical.

## Open item for the orchestrator

`reports/phase2/ws_b_report.md` line 369 lists
`reports/phase2/calibration_report_2026-07-26.md` as a deliverable. That file is
now `calibration_report_gfs_mex_2026-07-26.md`. The pointer was left alone
rather than edited, because `ws_b_report.md` is another workstream's evidence
and is not this workstream's to modify.
