"""Build the per-city forecast calibration files and the calibration report (FR-2.2).

Usage::

    $env:PYTHONPATH = "."
    python scripts/build_calibration.py
    python scripts/build_calibration.py --source gfs_mex --version 1

Reads
  * ``data/forecast_archive/forecast_series_<SOURCE>.csv`` (normalized, source-agnostic)
  * ``data/weather_truth/cli_daily_high_<STATION>.csv``     (read-only settlement truth)

Writes
  * ``data/calibration/<CITY>_<SOURCE>_v<N>.json``          -- the artifact; deterministic
  * ``data/calibration/<CITY>_<SOURCE>_v<N>.runlog.json``   -- run metadata; NOT deterministic
  * ``reports/phase2/calibration_report_<SOURCE>_<YYYY-MM-DD>.md`` -- the EC-3 sigma table

The artifact contains no timestamp, so re-running this script over unchanged
inputs rewrites byte-identical JSON. ``--check-deterministic`` proves it in
process: it builds twice and byte-compares.

TWO DEFECTS THIS FILE ONCE HAD, AND WHERE THEY WENT
---------------------------------------------------
1. **The report name did not carry the source.** Every source wrote
   ``calibration_report_<date>.md``, so building ``gefs`` overwrote ``gfs_mex``'s
   report -- and PRD Phase 2 exit criterion 3 requires the day-of sigma to be
   *published in the calibration report*, which only one source could be at a
   time. The name is now ``calibration_report_<source>_<date>.md``.
2. **``--source gefs`` emitted a different artifact from the one committed.**
   The ``statistic`` block (which carries the warning separating the backfill
   statistic ``max_t(geavg)`` from the live ``mean_m(max_t member)``) was stamped
   by ``scripts/backfill_ensemble_history.py`` *after* ``build_all()``, so this
   script silently produced a shorter file with a different ``content_hash`` and
   no warning. The stamping now lives in
   :data:`~src.calibration.forecast_calibration.SOURCE_ANNOTATORS`, inside
   ``build_all()``, so both producers emit the same bytes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.calibration.forecast_calibration import (  # noqa: E402
    CALIBRATION_DIR,
    DAY_OF_BUCKET,
    FORECAST_ARCHIVE_DIR,
    LEAD_BUCKETS,
    MIN_BUCKET_N,
    SIGMA_SANITY_BOUND_F,
    CityResult,
    build_all,
    canonical_bytes,
    day_of_sensitivity,
    day_of_sigma_table,
    write_calibration,
    write_runlog,
)
from src.data.iem_cli_provider import STATIONS  # noqa: E402
from src.data.mos_guidance_provider import SOURCE_GFS_MEX  # noqa: E402

logger = logging.getLogger("build_calibration")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(REPO_ROOT, "reports", "phase2")

#: PRD Phase 2 exit criterion 2: "each built from >=60 paired forecast-vs-CLI days".
EC2_MIN_PAIRED_DAYS = 60
#: PRD Phase 2 exit criterion 3: sigma bound must hold for at least this many cities.
EC3_MIN_CITIES_PASSING = 3

#: The FR-2.1 primary source's label. Held as a literal rather than imported
#: from ``src.calibration.gefs_series``, because that module pulls the whole
#: ensemble/GRIB2 stack in and building ``gfs_mex`` has no business paying for
#: it. ``tests/test_forecast_calibration.py`` pins this against the real
#: constant, so the two cannot drift apart silently.
SOURCE_GEFS = "gefs"


def report_filename(source: str, report_date: str) -> str:
    """The EC-3 report's name, **keyed by source**.

    It used to be ``calibration_report_<date>.md`` for every source, so building
    a second source silently overwrote the first source's report -- and EC-3
    requires the day-of sigma to be *published in the calibration report*. One
    file cannot publish two sources' sigma, and the one that survived was
    whichever ran last.
    """
    return f"calibration_report_{source}_{report_date}.md"


def _fmt(x, nd: int = 2) -> str:
    if x is None:
        return "--"
    if isinstance(x, int):
        return str(x)
    return f"{x:.{nd}f}"


def _bucket_row(city: str, name: str, block: Dict) -> str:
    if not block.get("sufficient"):
        return (
            f"| {city} | {name} | {block.get('n', 0)} | -- | -- | -- | -- | "
            f"insufficient (n < {MIN_BUCKET_N}) |"
        )
    lo = block.get("lead_hours_observed", {})
    return (
        f"| {city} | {name} | {block['n']} | {_fmt(block.get('bias_f'))} | "
        f"{_fmt(block.get('sigma_f'))} | {_fmt(block.get('mae_f'))} | "
        f"{lo.get('min')}..{lo.get('max')} | ok |"
    )


# ---------------------------------------------------------------------------
# Per-source narrative
# ---------------------------------------------------------------------------
# The skeleton of the report -- the EC-2 coverage table, the EC-3 sigma table,
# the day-of semantics, the lead/month/season tables -- is the same for every
# source and is rendered once. What differs is what the source *is*, what its
# published sigma is fragile to, and how to rebuild it. Those live here, keyed
# by source, rather than as `if source ==` branches threaded through the
# renderer. A source with no entry gets the generic blocks and says so.


def _source_header_line(source: str) -> str:
    if source == SOURCE_GFS_MEX:
        return (
            f"- **Source:** `{source}` (GFS extended MOS `N/X`, IEM `/api/1/mos.json`)"
        )
    if source == SOURCE_GEFS:
        return (
            f"- **Source:** `{source}` (GEFS ensemble derived products `geavg`/`gespr`, "
            "`TMAX:2 m above ground`, NOAA NODD `pgrb2sp25` 0.25 deg)"
        )
    return f"- **Source:** `{source}`"


def _day_of_provenance_line(source: str) -> str:
    bucket = f"`[{LEAD_BUCKETS[0][1]}, {LEAD_BUCKETS[0][2]})`"
    if source == SOURCE_GFS_MEX:
        return (
            f"`day_of` is the lead bucket {bucket} "
            "hours from model runtime to the start of the target **local** day. For this "
            "source every day-of row comes from the 00Z GFS run, whose guidance is on the "
            "wire before that day's maximum occurs."
        )
    if source == SOURCE_GEFS:
        return (
            f"`day_of` is the lead bucket {bucket} "
            "hours from model runtime to the start of the target **local** day. For this "
            "source every day-of row comes from the 00Z GEFS cycle -- the backfill "
            "requests no other cycle hour -- so the guidance is on the wire before that "
            "day's maximum occurs."
        )
    return (
        f"`day_of` is the lead bucket {bucket} hours from model runtime to the "
        "start of the target **local** day."
    )


def _day_of_semantics_lines(results: Sequence[CityResult]) -> List[str]:
    """The E4 block: what "day-of" is, and what Phase 3 may not do with it.

    The lead range is read out of the artifacts rather than asserted, because
    the whole point of the section is that the number describes an
    evening-before forecast and nothing else.
    """
    observed = [
        r.payload["by_lead"][DAY_OF_BUCKET].get("lead_hours_observed") or {}
        for r in results
        if r.payload["by_lead"][DAY_OF_BUCKET].get("sufficient")
    ]
    mins = [o["min"] for o in observed if o.get("min") is not None]
    maxs = [o["max"] for o in observed if o.get("max") is not None]
    span = f"**{min(mins)}..{max(maxs)} h**" if mins and maxs else "not measurable here"
    lines = [
        '### What "day-of" means -- and what these sigma may not be applied to',
        "",
        "Read this before reusing a sigma above anywhere in Phase 3.",
        "",
        f"Across the cities above, the leads actually observed inside the "
        f"`day_of` bucket are {span} to the **start of the target local day** "
        "-- every one of them from the 00Z cycle issued the *evening before* "
        "that day, roughly 10-16 h ahead of a typical afternoon maximum. The "
        "bucket's lower edge would admit a genuinely intraday run (a negative "
        "lead is a cycle initialised *during* the target day), but **no row in "
        "this sample has one**, which is why every bucket publishes its "
        "observed lead range.",
        "",
        "So `day_of` bias and sigma describe an **evening-before forecast, not "
        "an intraday update**. PRD FR-3.1(b)'s lock-in strategy re-forecasts at "
        "midday and trades on the update; these numbers were not measured on a "
        "midday re-forecast and must not be applied to one as though they were.",
        "",
        "The direction of the substitution error is known: a shorter-lead "
        "forecast is normally more accurate, so an evening-before sigma is an "
        "**upper bound** on a midday one. Reusing it therefore widens the "
        "predictive distribution and shrinks the apparent edge -- conservative, "
        "for a rule that buys a narrow bracket. But the *size* of the gap is "
        "unmeasured, and conservative is not correct: any rule whose EV "
        "improves under a wider distribution -- selling tails, pricing wide "
        "brackets, sizing on a fat sigma -- is flattered rather than penalised "
        "by it. Phase 3 must calibrate the lead it actually trades before "
        "pricing on it.",
        "",
    ]
    return lines


def _sensitivity_table(results: Sequence[CityResult]) -> List[str]:
    """Split-half and leave-one-out sigma per city, measured on the same sample."""
    lines = [
        "| City | n | pooled sigma | first half (dates, n) | sigma | second half (dates, n) | "
        "sigma | leave-one-out sigma range |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = day_of_sensitivity(r)
        if not s:
            lines.append(f"| {r.city} | -- | -- | -- | -- | -- | -- | insufficient |")
            continue
        a, b, loo = s["first_half"], s["second_half"], s["leave_one_out_sigma_f"]
        lines.append(
            f"| {r.city} | {s['n']} | **{_fmt(s['sigma_f'])}** | "
            f"{a['first_target_date']}..{a['last_target_date']} ({a['n']}) | "
            f"**{_fmt(a['sigma_f'])}** | "
            f"{b['first_target_date']}..{b['last_target_date']} ({b['n']}) | "
            f"**{_fmt(b['sigma_f'])}** | {_fmt(loo['min'])}..{_fmt(loo['max'])} |"
        )
    return lines


def _margin_lines(results: Sequence[CityResult], source: str) -> List[str]:
    """How fragile this source's EC-3 verdict is. Source-specific narrative."""
    if source == SOURCE_GFS_MEX:
        # Workstream B's published wording, preserved verbatim. Its numbers are
        # reproduced exactly by `day_of_sensitivity()` above -- see
        # `reports/phase2/ws_g_report.md`.
        return [
            "### CHI passes on a knife edge -- do not read it as a comfortable pass",
            "",
            "KMDW's day-of sigma is **3.98 degF against a 4.00 degF bound**, a margin "
            "of 0.02 degF on n=209. Measured sensitivity, all on the same sample:",
            "",
            "- First half of the window (2025-12-28..2026-04-10, 104 days): sigma "
            "**4.59** -- would **FAIL** the bound outright.",
            "- Second half (2026-04-11..2026-07-24, 105 days): sigma **2.78** -- "
            "passes comfortably.",
            "- Leave-one-out sigma across all 209 days ranges 3.59..3.99, so no single "
            "outlier is holding it under the bound; the cold season is.",
            "",
            "The honest reading is that CHI meets the bound *on an annual sample that "
            "is 60% warm-season*, and does not meet it in winter and early spring. "
            "Nothing was trimmed, excluded, or re-bucketed to produce the passing "
            "number -- the seasonal tables below are published precisely so this is "
            "visible rather than averaged away.",
            "",
        ]
    if source == SOURCE_GEFS:
        sens = {r.city: day_of_sensitivity(r) for r in results}
        table = day_of_sigma_table(results)
        by_city = {t["city"]: t for t in table}
        failing = [t["city"] for t in table if t["verdict"] == "FAIL"]
        cold_fail = sorted(
            c
            for c, s in sens.items()
            if s
            and s["first_half"]["sigma_f"] is not None
            and s["first_half"]["sigma_f"] > SIGMA_SANITY_BOUND_F
        )
        cold_pass = sorted(set(sens) - set(cold_fail))
        lines = [
            "### The margin is thin everywhere, and the cold half fails",
            "",
            "This source clears EC-3 by the letter of the rule and by very little "
            "else. Every city's margin to the bound, measured on the sample above "
            "and none of it reached by re-bucketing or trimming:",
            "",
        ]
        for city in ("NY", "CHI", "LAX", "MIA"):
            t = by_city.get(city)
            if not t or t.get("sigma_f") is None:
                continue
            margin = SIGMA_SANITY_BOUND_F - float(t["sigma_f"])
            lines.append(
                f"- **{city}** ({t['station']}): sigma **{_fmt(t['sigma_f'])}** "
                f"against the {SIGMA_SANITY_BOUND_F:.2f} degF bound -- "
                f"{'over by' if margin < 0 else 'under by'} "
                f"**{abs(margin):.2f} degF** on n={t['n']}. -> {t['verdict']}"
            )
        lines += [
            "",
            f"**{', '.join(failing) if failing else 'No city'} "
            f"{'fails' if len(failing) == 1 else 'fail'} the bound.** Per the exit "
            "criterion's own rule -- *\"a city failing this is excluded, not "
            "fudged\"* -- that city is **excluded from this source, not adjusted, "
            "re-bucketed, or re-fitted to fit**. No day was dropped, no error was "
            "trimmed or winsorised, and the failing city's number is published above "
            "in full rather than omitted. EC-3 is met on the remaining cities and "
            "on nothing else.",
            "",
            "Sensitivity, split chronologically and leave-one-out, on the same sample:",
            "",
        ]
        lines += _sensitivity_table(results)
        lines += [
            "",
            f"**On the cold half of the window the bound holds at "
            f"{', '.join(cold_pass) if cold_pass else 'no city'} and fails at "
            f"{', '.join(cold_fail) if cold_fail else 'no city'}.** Read literally, "
            f"this source would **not** meet EC-3's \"at least 3 of 4\" on its "
            "first half; it meets it on an annual sample that is roughly 60% "
            "warm-season. The leave-one-out ranges are tight at every city, so no "
            "single extraordinary day is holding any verdict up or down -- the "
            "season is.",
            "",
            "That is the honest statement of what this calibration supports: a "
            "warm-season sigma that Phase 3 may price on, and a cold-season sigma "
            "that must be re-measured (or traded smaller) once autumn and winter "
            "truth exists. The month and season tables below are published so the "
            "split is visible rather than averaged away.",
            "",
        ]
        return lines
    return []


#: Workstream B's published caveats, preserved verbatim. Moved out of the
#: renderer body unchanged when the report was parameterized by source; the
#: rendered `gfs_mex` report is byte-identical apart from the day-of semantics
#: section this remediation added.
_GFS_MEX_CAVEATS: List[str] = [
    "1. **This is raw GFS extended MOS, not the NWS forecast and not an "
    "ensemble.** The ~2.5 degF figure the exit criterion cites as a sanity "
    "anchor is the accuracy of the *official human/NDFD* forecast, which "
    "routinely beats raw model output statistics. The sigmas here should "
    "therefore be read as an **upper bound** on what a calibrated ensemble "
    "(FR-2.1) can achieve, not as the best available forecast.",
    "2. **A large part of the error is a sampling-window artifact, and it is "
    "measured, not assumed.** The MOS `N/X` daytime maximum covers roughly "
    "0700-1900 local standard time; the CLI daily maximum is local "
    "midnight-to-midnight. Splitting the day-of sample on the CLI's own "
    "`high_time`:",
    "",
    "   | City | max inside 07-19 LST | | | max outside 07-19 LST | | |",
    "   |---|---|---|---|---|---|---|",
    "   | | n | bias | sigma | n | bias | sigma |",
    "   | NY | 172 | +0.48 | 2.88 | 37 | -1.89 | 4.64 |",
    "   | CHI | 171 | +0.71 | 2.93 | 38 | -2.45 | 6.43 |",
    "   | LAX | 206 | -0.36 | 2.14 | 2 | -2.50 | 2.12 |",
    "   | MIA | 196 | -0.21 | 1.65 | 2 | +1.50 | 4.95 |",
    "",
    "   About 18% of NY and CHI days set their maximum outside the MOS "
    "window, and those days carry roughly twice the sigma and a cold bias. "
    "LAX and MIA almost never do (2 days each) -- which is most of why their "
    "sigmas are so much lower. The worst single day in the sample is "
    "KMDW 2026-03-22: CLI high **71 degF set at 1:57 AM** ahead of a cold "
    "front, against a day-of MOS daytime max of 46 degF -- a -25 degF error "
    "that is not a forecast bust at all.",
    "",
    "   **This is a live trading risk, not just a calibration nuisance.** "
    "Kalshi settles on the CLI value, so a day whose maximum lands at 2 AM "
    "settles far outside any bracket a daytime-max model would price. FR-2.3 "
    "and the FR-2.4 go/no-go must either model the overnight-max regime "
    "explicitly or exclude days with that synoptic setup, not average over "
    "them.",
    "3. **No spread is available from this source.** `spread_f` is blank in "
    "every row. NBM's guidance text does carry a max/min standard deviation "
    "(`XND`) but the IEM AFOS route serves only the latest bulletin, so it "
    "is not retrievable historically. A blank is recorded as a blank; it is "
    "never written as 0.0.",
    "4. **Seasonal coverage is incomplete.** The paired window is "
    "2025-12-28 to 2026-07-24: SON is entirely absent and DJF holds only "
    "63 days. Any seasonal correction taken from this file is unvalidated "
    "for autumn and should be rebuilt once Sep-Nov truth exists.",
    "5. **Day-of means 4-8 hours before the local day starts**, i.e. the 00Z "
    "run, roughly 10-16 hours ahead of a typical afternoon maximum. It is "
    "genuinely a forecast made before the max occurs, but it is *not* an "
    "intra-day forecast. MEX's 12Z run does not produce a same-day maximum, "
    "so a later-issued same-day number needs a short-range source "
    "(NBM/HRRR/GEFS), not this archive.",
    "6. **Drops are accounted, not hidden.** Every `dropped_no_truth` row is "
    "a forecast whose target date lies outside the truth window "
    "(2025-12-20..27 at the start, 2026-07-25..31 at the end). No paired day "
    "was discarded for being an outlier, and no error was trimmed, winsorized "
    "or clipped anywhere in this pipeline.",
    "7. **Determinism holds within a checkout.** Input provenance is hashed "
    "over normalized row content rather than raw file bytes, so a CRLF "
    "checkout of the inputs cannot move a published number. The artifacts "
    "themselves are written LF; if they are ever hash-gated in CI, pin "
    "`data/calibration/*.json` and `data/forecast_archive/*.csv` "
    "`eol=lf` in `.gitattributes`.",
    "",
]

_GEFS_CAVEATS: List[str] = [
    "1. **`forecast_high_f` is not the statistic the live provider returns, and "
    "the artifact says so.** Every `*_gefs_v1.json` carries a top-level "
    "`statistic` block. The series behind this report is "
    "`max_t(geavg TMAX)` -- the daily maximum of the ensemble **mean field** -- "
    "plus a measured per-city offset. The live `EnsembleProvider.fetch()` "
    "returns `mean_m(max_t member TMAX)`, the mean of the per-member daily "
    "maxima. `max` and `mean` do not commute, so these are different numbers. "
    "The offset (NY +0.2514, CHI +0.0489, LAX +0.0269, MIA +0.0549 degF, "
    "measured on 20 city-cycles at the full 31 members) is a **constant per "
    "city**: it moves `bias_f` by exactly its own value and cannot move "
    "`sigma_f` at all, so the EC-3 verdict above is insensitive to it.",
    "2. **`spread_f` is populated, and must not be used as a predictive "
    "sigma.** It is `gespr` -- the ensemble standard deviation of the TMAX "
    "field inside a single published interval (3 or 6 h, depending on the "
    "step) -- sampled where `geavg` attains its daily maximum. That is not "
    "the standard deviation of the members' "
    "daily maxima, which NCEP does not publish. Use the calibrated per-bucket "
    "`sigma_f` above. The column ships because it is real data with a "
    "documented meaning, not because it is a substitute for calibration.",
    "3. **Nearest-node sampling at 0.25 degrees, not the station.** Each city "
    "is read at the single nearest GEFS node: KNYC 4.6 km, KMDW 3.8 km, "
    "KLAX 12.3 km, KMIA 8.0 km. The residual bias per city is close to "
    "lead-invariant in the lead table above -- it barely changes from `day_of` "
    "out to 60-84 h -- which is the signature of a **siting/statistic** term "
    "rather than forecast decay. The 0.5 degree product was measured and "
    "rejected: it moves the node 24-31 km from three of the four stations. "
    "See `reports/phase2/ws_f_report.md`.",
    "4. **Seasonal coverage is incomplete, and this source is more exposed to "
    "it than `gfs_mex`.** The paired window is 2025-12-28 to 2026-07-24: SON "
    "is entirely absent, DJF is thin, and -- per the sensitivity table above "
    "-- the 4 degF bound does not hold on the cold half at most cities. Any "
    "seasonal correction taken from this file is unvalidated for autumn.",
    "5. **The CLI settlement window applies here too.** The truth is the CLI "
    "local midnight-to-midnight maximum. A day whose maximum lands overnight "
    "settles far outside any bracket a daytime-max model would price. That is "
    "a live trading risk for this source exactly as it is for `gfs_mex`; "
    "FR-2.3 and the FR-2.4 go/no-go must model or exclude the overnight-max "
    "regime rather than average over it.",
    "6. **Drops are accounted, not hidden.** Every `dropped_no_truth` row is a "
    "forecast whose target date has no row in the CLI truth archive: "
    "2025-12-27 (a cycle preceding the truth window) and 2026-07-25..27 "
    "(targets not yet settled). MIA carries 4 more, and one fewer paired day, "
    "because KMIA has no CLI row at all for 2026-04-11 -- a gap in the truth "
    "archive, recorded in `data/weather_truth/coverage_report.json`, not a day "
    "this pipeline chose to discard. No paired day was dropped for being an "
    "outlier, and no error was trimmed, winsorized or clipped anywhere.",
    "7. **Determinism holds within a checkout.** Input provenance is hashed "
    "over normalized row content rather than raw file bytes, so a CRLF "
    "checkout of the inputs cannot move a published number. The artifacts "
    "themselves are written LF; if they are ever hash-gated in CI, pin "
    "`data/calibration/*.json` and `data/forecast_archive/*.csv` "
    "`eol=lf` in `.gitattributes`.",
    "8. **The GRIB2 decoder behind this series is in-house.** Its independence "
    "from a second implementation is evidenced in "
    "`reports/phase2/ws_g_decoder_independence.md` (comparison against "
    "Open-Meteo's separate Swift GRIB stack), not by the `geavg` cross-check "
    "in `reports/phase2/ec1_ensemble_members.md`, which decodes `geavg` with "
    "the same decoder and therefore cannot detect a global scale, offset or "
    "sign error.",
    "",
]


def _caveat_lines(source: str) -> List[str]:
    if source == SOURCE_GFS_MEX:
        return _GFS_MEX_CAVEATS
    if source == SOURCE_GEFS:
        return _GEFS_CAVEATS
    return [
        "No source-specific caveats are registered for this source. Absence of a "
        "caveat list is not evidence that there are none.",
        "",
    ]


def _reproduction_lines(source: str, version: int) -> List[str]:
    if source == SOURCE_GEFS:
        return [
            "```bash",
            '$env:PYTHONPATH = "."',
            "# fetch (resumable; a completed run costs no network)",
            "python scripts/backfill_ensemble_history.py --start 2025-12-27 --end 2026-07-24",
            "# rebuild the series from the resume cache, then calibrate",
            "python scripts/backfill_ensemble_history.py --offline --start 2025-12-27 "
            "--end 2026-07-24 --build-calibration",
            "# ...or, equivalently and byte-identically, through the generic builder:",
            f"python scripts/build_calibration.py --source {source} --version {version} "
            "--check-deterministic",
            "```",
            "",
            "The two commands emit the same bytes. That is enforced rather than "
            "assumed: the `statistic` block is applied inside "
            "`forecast_calibration.build_all()` via its `SOURCE_ANNOTATORS` "
            "registry, so no producer can omit it. Before that fix the generic "
            "builder stripped the block and emitted a 10 402-byte file where the "
            "committed artifact is 11 932 bytes, with a different `content_hash` "
            "and no statistic warning at all.",
            "",
        ]
    return [
        "```bash",
        '$env:PYTHONPATH = "."',
        "python scripts/backfill_forecasts.py --start 2025-12-20 --end 2026-07-24",
        f"python scripts/build_calibration.py --source {source} --version {version} "
        "--check-deterministic",
        "```",
        "",
    ]


def render_report(
    results: Sequence[CityResult],
    *,
    source: str,
    version: int,
    forecast_csv: str,
    report_date: str,
    determinism_evidence: str,
) -> str:
    sigma_table = day_of_sigma_table(results)
    n_pass = sum(1 for r in sigma_table if r["verdict"] == "PASS")
    ec3 = "MET" if n_pass >= EC3_MIN_CITIES_PASSING else "NOT MET"

    ec2_rows = []
    ec2_ok = True
    for r in results:
        day_of_days = r.payload["coverage"]["day_of_paired_days"]
        total = r.payload["inputs"]["paired_rows"]
        distinct = r.payload["coverage"]["distinct_paired_target_dates"]
        ok = day_of_days >= EC2_MIN_PAIRED_DAYS
        ec2_ok = ec2_ok and ok
        ec2_rows.append(
            f"| {r.city} | {r.station} | {total} | {distinct} | {day_of_days} | "
            f"{r.payload['inputs']['dropped_no_truth']} | "
            f"{r.payload['inputs']['dropped_null_truth']} | "
            f"{'PASS' if ok else 'FAIL'} |"
        )

    lines: List[str] = []
    A = lines.append
    A(f"# Phase 2 forecast calibration report -- {report_date}")
    A("")
    A(_source_header_line(source))
    A(f"- **Calibration version:** v{version}")
    A(
        f"- **Forecast series:** "
        f"`{os.path.relpath(forecast_csv, REPO_ROOT).replace(os.sep, '/')}`"
    )
    A(
        "- **Truth:** `data/weather_truth/cli_daily_high_<STATION>.csv` "
        "(NWS Climatological Report via IEM; verified 835/835 in Phase 1)"
    )
    A(
        "- **Error convention:** `error_f = forecast_high_f - truth_high_f` "
        "(**positive = forecast too warm**)"
    )
    A("")
    A("## Exit criterion 2 -- paired-day coverage and determinism")
    A("")
    A(
        '> "Calibration files exist for all 4 cities, each built from >=60 paired '
        "forecast-vs-CLI days (backfill + live), reporting bias and sigma by lead "
        'time; recomputation is deterministic from inputs (byte-identical on re-run)."'
    )
    A("")
    A(
        "| City | Station | Paired rows (all leads) | Distinct target dates | "
        "Day-of paired days | Dropped: no truth | Dropped: null truth | >=60 day-of |"
    )
    A("|---|---|---|---|---|---|---|---|")
    for row in ec2_rows:
        A(row)
    A("")
    A(determinism_evidence)
    A("")
    A("## Exit criterion 3 -- day-of sigma sanity bound")
    A("")
    A(
        '> "Measured day-of sigma per city is published in the calibration report '
        "and is <=4 degF for at least 3 of 4 cities (sanity bound: published NWS "
        'accuracy ~2.5 degF; a city failing this is excluded, not fudged)."'
    )
    A("")
    A(_day_of_provenance_line(source))
    A("")
    A(
        "| City | Station | n (day-of) | bias (degF) | **sigma (degF)** | MAE (degF) | "
        f"<= {SIGMA_SANITY_BOUND_F:.0f} degF |"
    )
    A("|---|---|---|---|---|---|---|")
    for row in sigma_table:
        A(
            f"| {row['city']} | {row['station']} | {row['n']} | "
            f"{_fmt(row['bias_f'])} | **{_fmt(row['sigma_f'])}** | "
            f"{_fmt(row['mae_f'])} | {row['verdict']} |"
        )
    A("")
    A(
        f"**{n_pass} of {len(sigma_table)} cities within the {SIGMA_SANITY_BOUND_F:.0f} "
        f"degF bound -> EC-3 {ec3}.**"
    )
    A("")
    lines.extend(_day_of_semantics_lines(results))
    lines.extend(_margin_lines(results, source))
    A("## Error by lead time (all cities)")
    A("")
    A(
        "| City | Bucket | n | bias (degF) | sigma (degF) | MAE (degF) | "
        "observed lead h | status |"
    )
    A("|---|---|---|---|---|---|---|---|")
    for r in results:
        for name, _lo, _hi in LEAD_BUCKETS:
            A(_bucket_row(r.city, name, r.payload["by_lead"][name]))
    A("")
    A("## Day-of error by month")
    A("")
    A(
        f"Computed on the `day_of` bucket only. Buckets with n < {MIN_BUCKET_N} are "
        "reported as insufficient and carry no statistics -- they are not merged into "
        "a neighbour to reach a quorum."
    )
    A("")
    A("| City | Month | n | bias (degF) | sigma (degF) | MAE (degF) | status |")
    A("|---|---|---|---|---|---|---|")
    for r in results:
        for month in sorted(r.payload["by_month_day_of"]):
            b = r.payload["by_month_day_of"][month]
            if not b.get("sufficient"):
                A(
                    f"| {r.city} | {month} | {b.get('n', 0)} | -- | -- | -- | "
                    f"insufficient |"
                )
            else:
                A(
                    f"| {r.city} | {month} | {b['n']} | {_fmt(b.get('bias_f'))} | "
                    f"{_fmt(b.get('sigma_f'))} | {_fmt(b.get('mae_f'))} | ok |"
                )
    A("")
    A("## Day-of error by season")
    A("")
    A("| City | Season | n | bias (degF) | sigma (degF) | MAE (degF) | status |")
    A("|---|---|---|---|---|---|---|")
    for r in results:
        for season in sorted(r.payload["by_season_day_of"]):
            b = r.payload["by_season_day_of"][season]
            if not b.get("sufficient"):
                A(
                    f"| {r.city} | {season} | {b.get('n', 0)} | -- | -- | -- | "
                    f"insufficient |"
                )
            else:
                A(
                    f"| {r.city} | {season} | {b['n']} | {_fmt(b.get('bias_f'))} | "
                    f"{_fmt(b.get('sigma_f'))} | {_fmt(b.get('mae_f'))} | ok |"
                )
    A("")
    A("## Caveats -- read these before trusting a number above")
    A("")
    lines.extend(_caveat_lines(source))
    A("## Reproduction")
    A("")
    lines.extend(_reproduction_lines(source, version))
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=SOURCE_GFS_MEX)
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--forecast-csv", default=None)
    ap.add_argument("--truth-dir", default=None)
    ap.add_argument("--out-dir", default=CALIBRATION_DIR)
    ap.add_argument("--report-dir", default=REPORT_DIR)
    ap.add_argument("--report-date", default=None)
    ap.add_argument("--min-bucket-n", type=int, default=MIN_BUCKET_N)
    ap.add_argument(
        "--check-deterministic",
        action="store_true",
        help="build twice from disk and byte-compare the serialized artifacts",
    )
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    forecast_csv = args.forecast_csv or os.path.join(
        FORECAST_ARCHIVE_DIR, f"forecast_series_{args.source}.csv"
    )

    kwargs = dict(
        forecast_csv=forecast_csv,
        stations=STATIONS,
        source=args.source,
        version=args.version,
        truth_dir=args.truth_dir,
        min_bucket_n=args.min_bucket_n,
    )
    results = build_all(**kwargs)

    determinism_evidence = (
        "Determinism was not checked in this run (pass `--check-deterministic`)."
    )
    if args.check_deterministic:
        second = build_all(**kwargs)
        diffs = []
        for a, b in zip(results, second):
            if canonical_bytes(a.payload) != canonical_bytes(b.payload):
                diffs.append(a.city)
        if diffs:
            print(f"DETERMINISM FAILED for {diffs}", file=sys.stderr)
            determinism_evidence = (
                f"**Determinism check FAILED** for {diffs}: two builds over the "
                f"same inputs produced different bytes."
            )
        else:
            hashes = ", ".join(
                f"`{r.city}` `{r.payload['content_hash'][7:19]}`" for r in results
            )
            determinism_evidence = (
                "**Determinism check PASSED.** `build_all()` was run twice against "
                "the same on-disk inputs and the canonical serialization of every "
                "city's payload compared byte-identical "
                f"({len(results)}/{len(results)} cities). Content hashes: {hashes}. "
                "The artifact carries no timestamp, hostname, run id, or filesystem "
                "path; that metadata lives in the `.runlog.json` sidecar, which is "
                "excluded from the comparison."
            )
        if diffs:
            return 2

    written: List[str] = []
    if not args.no_write:
        for r in results:
            r.path = write_calibration(args.out_dir, r.payload)
            write_runlog(
                args.out_dir,
                r.payload,
                {
                    "forecast_csv": os.path.relpath(forecast_csv, REPO_ROOT),
                    "truth_csv": f"data/weather_truth/cli_daily_high_{r.station}.csv",
                    "paired_rows": r.payload["inputs"]["paired_rows"],
                    "argv": list(argv if argv is not None else sys.argv[1:]),
                },
            )
            written.append(r.path)

    report_date = args.report_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = render_report(
        results,
        source=args.source,
        version=args.version,
        forecast_csv=forecast_csv,
        report_date=report_date,
        determinism_evidence=determinism_evidence,
    )
    report_path = os.path.join(
        args.report_dir, report_filename(args.source, report_date)
    )
    if not args.no_write:
        os.makedirs(args.report_dir, exist_ok=True)
        with open(report_path, "wb") as fh:
            # Trailing newline keeps the end-of-file-fixer hook from
            # rewriting this report after it is generated, which would break
            # the report's byte-reproducibility for a cosmetic reason.
            body = report.rstrip("\n") + "\n"
            fh.write(body.encode("utf-8"))

    table = day_of_sigma_table(results)
    n_pass = sum(1 for t in table if t["verdict"] == "PASS")
    ec2_ok = all(
        r.payload["coverage"]["day_of_paired_days"] >= EC2_MIN_PAIRED_DAYS
        for r in results
    )
    print(f"source={args.source} v{args.version}")
    for r in results:
        print(
            f"  {r.city:4s} {r.station}  paired={r.payload['inputs']['paired_rows']:5d}"
            f"  day_of_n={r.payload['coverage']['day_of_paired_days']:4d}"
            f"  hash={r.payload['content_hash'][7:19]}"
        )
    print("day-of sigma table:")
    for t in table:
        print(
            f"  {t['city']:4s} n={t['n']:4d} bias={_fmt(t['bias_f'])} "
            f"sigma={_fmt(t['sigma_f'])} mae={_fmt(t['mae_f'])} -> {t['verdict']}"
        )
    print(
        f"EC-2 (>= {EC2_MIN_PAIRED_DAYS} day-of paired days, all 4 cities): "
        f"{'MET' if ec2_ok else 'NOT MET'}"
    )
    print(
        f"EC-3 ({n_pass}/{len(table)} cities <= {SIGMA_SANITY_BOUND_F:.0f} degF): "
        f"{'MET' if n_pass >= EC3_MIN_CITIES_PASSING else 'NOT MET'}"
    )
    if written:
        print("wrote:")
        for p in written:
            print("  ", os.path.relpath(p, REPO_ROOT))
        print("  ", os.path.relpath(report_path, REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
