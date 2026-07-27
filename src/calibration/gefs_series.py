"""GEFS *derived-product* daily highs, for the FR-2.2 historical backfill.

Workstream A's :mod:`src.data.ensemble_provider` answers the live question --
"what does the 31-member GEFS ensemble say tomorrow's high will be" -- by
downloading all 31 members. That is the right shape for one cycle and the wrong
shape for a 200-day backfill: 31 members x ~11 forecast hours x ~210 cycles is
~72 000 ranged requests and roughly 30 GB.

This module reads the two **pre-computed NCEP products** published in the same
directory instead:

* ``geavg`` -- the ensemble *mean* field, and
* ``gespr`` -- the ensemble *spread* (standard deviation) field,

which turns a cycle into 22 ranged requests. It reuses
:mod:`src.data.ensemble_provider` wholesale -- the GRIB2 decoder, the ``.idx``
byte-range subsetting, the ``TMAX`` interval algebra, the nearest-node
selection, the local-calendar-day windowing, the record cache and the
abort-never-default contract. Nothing is reimplemented here; this module is a
*driver*, and every number it produces comes out of that provider.

THE STATISTIC MISMATCH, STATED BEFORE IT IS USED
------------------------------------------------
The live path computes **the mean of the per-member daily maxima**::

    live_f  = mean_m( max_t TMAX(m, t) )

A ``geavg``-based backfill can only compute **the daily maximum of the mean
field**::

    backfill_f = max_t( mean_m TMAX(m, t) )

These are not the same number. ``max`` and ``mean`` do not commute, and Jensen's
inequality puts ``backfill_f <= live_f``. A calibration built on the second and
consumed by the first is a coherence bug unless the gap is measured and carried.

It is measured. :data:`GEAVG_TO_MEMBER_MEAN_OFFSET_F` holds the per-city offset
``live_f - backfill_f`` measured over workstream A's 5 consecutive 00Z cycles x
4 cities at the full 31 members (``reports/phase2/ec1_ensemble_members.json``),
and :func:`offset_for` is the only way to read it. Two properties of that
correction matter and are asserted by the tests:

1. It is a **constant per city**, so it shifts ``bias_f`` by exactly its own
   value and **cannot change ``sigma_f`` at all**. The EC-3 sigma verdict is
   therefore completely insensitive to whether it is applied.
2. It is ~0.03-0.25 F against a forecast-error sigma of order 2-4 F, i.e. two
   orders of magnitude below the quantity being calibrated.

Every emitted row records which statistic it was built on
(:data:`STATISTIC_GEAVG_MAX`) and how much correction was added, so no later
reader can mistake it for the live statistic.

THE SPREAD, AND WHAT IT IS NOT
------------------------------
``gespr`` is the ensemble standard deviation **of the TMAX field inside one
interval**. It is *not* the standard deviation of the members' daily maxima --
that quantity is not published, and computing it would need the members this
module exists to avoid downloading. The emitted ``spread_f`` is
``gespr`` sampled at the interval where ``geavg`` attains its daily maximum
(:data:`SPREAD_RULE`), which is the closest published proxy. It is an
approximation, it is labelled as one in every row's provenance, and
:mod:`scripts.backfill_ensemble_history` measures it against the true member
sigma on the cycles where the members are on disk.

**Unit trap, guarded.** ``gespr`` is a *dispersion* in Kelvin. Converting it
with the affine ``K -> F`` formula would return roughly -458 F. A spread
converts by scale only::

    sigma_F = sigma_K * 9/5

:func:`kelvin_spread_to_fahrenheit` is the only conversion used, and
``tests/test_gefs_backfill.py`` pins that it disagrees with
``kelvin_to_fahrenheit`` on every non-zero input.

RESOLUTION: 0.25 deg, ON MEASUREMENT
------------------------------------
The 0.5 deg product (``pgrb2ap5``) publishes the same ``TMAX:2 m above ground``
field in a record ~3.1x smaller, which is a real transfer saving. It is **not**
used, because at 0.5 deg the nearest grid node moves 24-31 km from three of the
four settlement stations (against 3.8-12.3 km at 0.25 deg) and the resulting
daily highs disagree materially. The measurement is in
``reports/phase2/ws_f_report.md``; :func:`half_degree_nearest_node` exists so
that comparison stays reproducible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.data.ensemble_provider import (
    FIELD_TMAX,
    LatLonGrid,
    CitySpec,
    EnsembleProvider,
    EnsembleUnavailable,
    MEAN_PRODUCT,
    SPREAD_PRODUCT,
    TmaxWindow,
    coverage_of,
    get_city,
    kelvin_to_fahrenheit,
    local_day_bounds_utc,
    spec_nearest_node,
    tmax_windows,
)

logger = logging.getLogger(__name__)

#: The source label stamped into the normalized forecast CSV. Deliberately
#: distinct from workstream A's ``gefs_aws_pgrb2sp25`` runtime tag and from
#: workstream B's ``gfs_mex``: a calibration file named ``*_gefs_v1.json`` is
#: built on the derived products, not on the 31 members.
SOURCE_GEFS_SERIES = "gefs"

#: What ``forecast_high_f`` in an emitted row actually is.
STATISTIC_GEAVG_MAX = "max_t(geavg TMAX) + per-city geavg-to-member-mean offset"

#: What the live provider computes, for contrast.
STATISTIC_MEMBER_MEAN_MAX = "mean_m(max_t member TMAX)"

#: How ``spread_f`` is derived.
SPREAD_RULE = "gespr TMAX at the interval where geavg attains its daily maximum"

#: ``live_f - backfill_f`` in degrees F, per city.
#:
#: Measured, not assumed: the mean over workstream A's 20 city-cycles (5
#: consecutive 00Z cycles 2026-07-20..24 x 4 cities) at the full 31 members,
#: read out of ``reports/phase2/ec1_ensemble_members.json`` field
#: ``runs[].geavg_check.member_mean_minus_geavg_f``. Sample sd and range per
#: city are in ``reports/phase2/ws_f_report.md``; nothing here is rounded to
#: flatter it.
GEAVG_TO_MEMBER_MEAN_OFFSET_F: Dict[str, float] = {
    "NY": 0.2514,
    "CHI": 0.0489,
    "LAX": 0.0269,
    "MIA": 0.0549,
}

#: Provenance for the numbers above, carried into every calibration file.
OFFSET_PROVENANCE: Dict[str, Any] = {
    "quantity": "mean_m(max_t member TMAX) - max_t(mean_m TMAX)",
    "why": (
        "max and mean do not commute; the live path maximises each member then "
        "averages, the backfill averages then maximises"
    ),
    "measured_on": "reports/phase2/ec1_ensemble_members.json runs[].geavg_check",
    "sample": "5 consecutive 00Z cycles 2026-07-20..2026-07-24 x 4 cities, 31 members",
    "n_per_city": 5,
    "units": "degF",
    "effect_on_sigma": (
        "none -- a constant per-city shift moves bias_f by exactly its own "
        "value and leaves sigma_f unchanged"
    ),
}

#: Sanity ceiling on the offset. If a re-measurement ever exceeds this the
#: correction is no longer a rounding term and the decision to use ``geavg`` at
#: all has to be revisited, so it fails loudly instead of being applied.
MAX_TRUSTED_OFFSET_F = 1.0


class GefsSeriesError(RuntimeError):
    """A derived-product daily high could not be assembled."""


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
def kelvin_spread_to_fahrenheit(sigma_kelvin: float) -> float:
    """Convert a *dispersion* in Kelvin to Fahrenheit -- scale only, no offset.

    ``gespr`` is a standard deviation, not a temperature. Running it through the
    affine ``kelvin_to_fahrenheit`` would subtract 273.15 from a number of order
    1 and return roughly -458 F, which is not obviously wrong at a glance in a
    CSV column. This is the only conversion applied to a spread.
    """
    value = float(sigma_kelvin)
    if value < 0.0:
        raise GefsSeriesError(f"negative ensemble spread {value!r} K")
    return value * 9.0 / 5.0


def offset_for(city: str) -> float:
    """The measured ``geavg`` -> member-mean correction for one city, in F."""
    key = str(city).upper().strip()
    if key not in GEAVG_TO_MEMBER_MEAN_OFFSET_F:
        raise GefsSeriesError(
            f"no measured geavg offset for {city!r} "
            f"(have {sorted(GEAVG_TO_MEMBER_MEAN_OFFSET_F)})"
        )
    value = GEAVG_TO_MEMBER_MEAN_OFFSET_F[key]
    if abs(value) > MAX_TRUSTED_OFFSET_F:
        raise GefsSeriesError(
            f"measured geavg offset for {key} is {value} F, beyond the "
            f"{MAX_TRUSTED_OFFSET_F} F ceiling; the derived-product shortcut is "
            f"no longer a rounding term and must be re-justified"
        )
    return value


# ---------------------------------------------------------------------------
# Which forecast hours a (cycle, city, target) needs
# ---------------------------------------------------------------------------
def windows_for(
    spec: CitySpec, target_date: _date, init_time: datetime
) -> Tuple[TmaxWindow, ...]:
    """The ``TMAX`` records covering ``target_date``'s local day from ``init_time``.

    Delegates entirely to workstream A: :func:`local_day_bounds_utc` for the
    timezone-correct day and :func:`tmax_windows` for the minimal covering set.
    The backfill therefore cannot drift away from the live windowing -- there is
    only one implementation.
    """
    day_start, day_end = local_day_bounds_utc(target_date, spec.timezone)
    start_lead = int(round((day_start - init_time).total_seconds() / 3600.0))
    end_lead = int(round((day_end - init_time).total_seconds() / 3600.0))
    return tmax_windows(start_lead, end_lead)


def lead_hours_for(spec: CitySpec, target_date: _date, init_time: datetime) -> int:
    """Hours from the model cycle to the start of the target **local** day.

    This is the same definition workstream A stamps onto
    ``EnsembleForecast.lead_hours`` and the same one workstream B's lead buckets
    are cut on, so the two sources' ``day_of`` buckets mean the same thing.
    """
    day_start, _ = local_day_bounds_utc(target_date, spec.timezone)
    return int(round((day_start - init_time).total_seconds() / 3600.0))


def cycle_plan(
    init_time: datetime,
    lead_days: Sequence[int],
    cities: Sequence[str],
) -> Tuple[Dict[Tuple[str, int], Tuple[TmaxWindow, ...]], Tuple[int, ...]]:
    """Plan one cycle: per (city, lead-day) windows, plus the union of fhours.

    The union is what makes the backfill affordable. One ranged request for
    ``geavg f030`` carries every city's node *and* serves both the day-of and
    the day-ahead target that happen to need it, because
    :meth:`EnsembleProvider.fetch_record_values` caches by (cycle, product,
    fhour) with all requested nodes together.

    A (city, lead-day) whose window is unreachable -- e.g. a negative lead, or
    past the 3-hourly horizon -- is **omitted from the plan and reported by its
    absence**, not silently clamped to a reachable one.
    """
    plan: Dict[Tuple[str, int], Tuple[TmaxWindow, ...]] = {}
    fhours: set = set()
    init_date = init_time.astimezone(timezone.utc).date()
    for city in cities:
        spec = get_city(city)
        for offset in lead_days:
            target = init_date + _days(offset)
            try:
                windows = windows_for(spec, target, init_time)
            except EnsembleUnavailable as exc:
                logger.debug(
                    "cycle %s: %s lead+%d unreachable (%s)",
                    init_time,
                    city,
                    offset,
                    exc.reason_code,
                )
                continue
            plan[(spec.city, offset)] = windows
            fhours.update(w.fhour for w in windows)
    return plan, tuple(sorted(fhours))


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=int(n))


# ---------------------------------------------------------------------------
# One assembled row
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DerivedDailyHigh:
    """One city's daily high from the derived products, plus its provenance."""

    city: str
    station: str
    target_date: _date
    init_time: datetime
    lead_hours: int
    #: ``max_t(geavg)`` before the statistic correction -- the raw measurement.
    raw_high_f: float
    #: The correction added, i.e. :func:`offset_for`.
    offset_f: float
    #: What the calibration consumes: ``raw_high_f + offset_f``.
    forecast_high_f: float
    #: ``gespr`` at :attr:`argmax_fhour`, in F. ``None`` when gespr was not read.
    spread_f: Optional[float]
    argmax_fhour: int
    fhours: Tuple[int, ...]
    covered_lead_hours: Tuple[int, int]
    requested_lead_hours: Tuple[int, int]
    s3_keys: Tuple[str, ...]
    byte_ranges: Tuple[str, ...]

    def provenance_string(self) -> str:
        """The single ``provenance`` CSV cell: re-fetchable and self-describing.

        Everything a reader needs to reproduce or distrust the row -- the
        product, the statistic, the correction that was added, the interval the
        spread came from, and the S3 prefix -- without opening another file.
        """
        cycle = self.init_time.strftime("%Y%m%d")
        hh = self.init_time.strftime("%H")
        return (
            f"s3://noaa-gefs-pds/gefs.{cycle}/{hh}/atmos/pgrb2sp25/"
            f"{{geavg,gespr}}.t{hh}z.pgrb2s.0p25.f"
            f"{{{','.join(f'{f:03d}' for f in self.fhours)}}}"
            f"#statistic=max_t(geavg)+offset;offset_f={self.offset_f:+.4f};"
            f"raw_f={self.raw_high_f:.4f};spread=gespr@f{self.argmax_fhour:03d};"
            f"covered_leads={self.covered_lead_hours[0]}-{self.covered_lead_hours[1]}"
        )

    def csv_row(self) -> Dict[str, Any]:
        """The row in workstream B's normalized forecast-series schema."""
        return {
            "city": self.city,
            "station": self.station,
            "target_date": self.target_date.isoformat(),
            "init_time_utc": self.init_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lead_hours": self.lead_hours,
            "source": SOURCE_GEFS_SERIES,
            "forecast_high_f": f"{self.forecast_high_f:.4f}",
            "spread_f": "" if self.spread_f is None else f"{self.spread_f:.4f}",
            "provenance": self.provenance_string(),
        }


#: The normalized schema workstream B's calibrator reads. Held here as a literal
#: rather than imported so a change on either side is a visible diff, and pinned
#: by ``test_csv_header_matches_the_calibrator_contract``.
FORECAST_FIELDS: Tuple[str, ...] = (
    "city",
    "station",
    "target_date",
    "init_time_utc",
    "lead_hours",
    "source",
    "forecast_high_f",
    "spread_f",
    "provenance",
)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def read_cycle_records(
    provider: EnsembleProvider,
    init_time: datetime,
    fhours: Sequence[int],
    nodes: Sequence[Tuple[int, int]],
    *,
    products: Sequence[str] = (MEAN_PRODUCT, SPREAD_PRODUCT),
    on_record: Optional[Any] = None,
) -> Tuple[Dict[Tuple[str, int], Dict[str, Any]], Dict[Tuple[str, int], str]]:
    """Fetch every (product, fhour) of one cycle once. Returns ``(ok, failures)``.

    Concurrency is bounded by ``provider.max_workers``, which the provider
    itself clamps to 4. That is deliberately the *same* burst width workstream A
    already ran against this bucket -- no pool is nested inside another, because
    :meth:`EnsembleProvider.fetch_record_values` is a single request and does
    not open a pool of its own. Two constraints set the ceiling: the NODD bucket
    answers a fraction of a wide burst with HTTP 503, and this machine
    machine-checks under sustained all-core load.

    Results are collected into a dict keyed by (product, fhour), so the return
    value does not depend on completion order. ``on_record`` is invoked from the
    submitting thread after the pool drains, in a deterministic order, so a
    manifest built from it is reproducible.

    A failure is recorded against its (product, fhour) key and the rest of the
    cycle continues, so one missing record costs one row, not a whole cycle.
    Failures are **never** cached (that rule lives in the provider) and never
    substituted.
    """
    jobs = [(str(p), int(f)) for p in products for f in fhours]

    def work(job: Tuple[str, int]):
        product, fhour = job
        try:
            return job, provider.fetch_record_values(
                init_time, product, fhour, nodes, field_name=FIELD_TMAX
            )
        except EnsembleUnavailable as exc:
            return job, exc

    outcomes: Dict[Tuple[str, int], Any] = {}
    workers = max(1, min(4, int(getattr(provider, "max_workers", 4) or 1)))
    if workers == 1 or len(jobs) <= 1:
        for job in jobs:
            key, outcome = work(job)
            outcomes[key] = outcome
    else:
        from concurrent.futures import ThreadPoolExecutor

        if getattr(provider, "offline", False) is False:
            _ = provider.session  # create the session once, not racily in the pool
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, outcome in pool.map(work, jobs):
                outcomes[key] = outcome

    ok: Dict[Tuple[str, int], Dict[str, Any]] = {}
    failures: Dict[Tuple[str, int], str] = {}
    for job in jobs:
        outcome = outcomes[job]
        if isinstance(outcome, EnsembleUnavailable):
            failures[job] = f"{outcome.reason_code}: {outcome.detail}"
            if on_record is not None:
                on_record(job[0], job[1], None, failures[job])
            continue
        ok[job] = outcome
        if on_record is not None:
            on_record(job[0], job[1], outcome, None)
    return ok, failures


def assemble_daily_high(
    *,
    spec: CitySpec,
    target_date: _date,
    init_time: datetime,
    windows: Sequence[TmaxWindow],
    records: Mapping[Tuple[str, int], Mapping[str, Any]],
) -> DerivedDailyHigh:
    """Build one :class:`DerivedDailyHigh` from already-fetched cycle records.

    Pure given ``records`` -- no I/O -- so the tests exercise the arithmetic,
    the unit handling and the argmax rule against recorded blobs with no
    network.

    Raises :class:`GefsSeriesError` if any interval covering the day is missing.
    A daily maximum computed over a subset of the day is not a daily maximum,
    and quietly returning one is exactly the class of defect this pivot exists
    to remove.
    """
    node = spec_nearest_node(spec)
    node_key = f"{node[0]},{node[1]}"

    mean_k: List[float] = []
    s3_keys: List[str] = []
    ranges: List[str] = []
    for window in windows:
        blob = records.get((MEAN_PRODUCT, window.fhour))
        if blob is None:
            raise GefsSeriesError(
                f"{spec.city} {target_date}: no {MEAN_PRODUCT} record for "
                f"f{window.fhour:03d}; the covering set "
                f"{[w.fhour for w in windows]} is incomplete"
            )
        try:
            mean_k.append(float(blob["nodes_k"][node_key]))
        except KeyError as exc:
            raise GefsSeriesError(
                f"{spec.city} {target_date}: {MEAN_PRODUCT} f{window.fhour:03d} "
                f"carries no value at node {node_key}"
            ) from exc
        meta = blob.get("_meta") or {}
        if meta.get("s3_key"):
            s3_keys.append(str(meta["s3_key"]))
        if meta.get("range"):
            ranges.append(str(meta["range"]))

    best = max(range(len(mean_k)), key=lambda k: mean_k[k])
    argmax_fhour = windows[best].fhour
    raw_high_f = kelvin_to_fahrenheit(mean_k[best])

    spread_f: Optional[float] = None
    spread_blob = records.get((SPREAD_PRODUCT, argmax_fhour))
    if spread_blob is not None:
        raw = spread_blob.get("nodes_k", {}).get(node_key)
        if raw is not None:
            spread_f = round(kelvin_spread_to_fahrenheit(float(raw)), 4)
            meta = spread_blob.get("_meta") or {}
            if meta.get("s3_key"):
                s3_keys.append(str(meta["s3_key"]))
            if meta.get("range"):
                ranges.append(str(meta["range"]))

    offset = offset_for(spec.city)
    covered = coverage_of(windows)
    day_start, day_end = local_day_bounds_utc(target_date, spec.timezone)
    start_lead = int(round((day_start - init_time).total_seconds() / 3600.0))
    end_lead = int(round((day_end - init_time).total_seconds() / 3600.0))

    return DerivedDailyHigh(
        city=spec.city,
        station=spec.station,
        target_date=target_date,
        init_time=init_time,
        lead_hours=start_lead,
        raw_high_f=round(raw_high_f, 4),
        offset_f=offset,
        forecast_high_f=round(raw_high_f + offset, 4),
        spread_f=spread_f,
        argmax_fhour=argmax_fhour,
        fhours=tuple(w.fhour for w in windows),
        covered_lead_hours=covered,
        requested_lead_hours=(start_lead, end_lead),
        s3_keys=tuple(sorted(set(s3_keys))),
        byte_ranges=tuple(sorted(set(ranges))),
    )


# ---------------------------------------------------------------------------
# The block every source="gefs" calibration artifact must carry
# ---------------------------------------------------------------------------
#: Why the ``spread_f`` column must not be used as a predictive sigma.
SPREAD_CAVEAT = (
    "spread_f is the ensemble spread of the TMAX field in one interval, "
    "not the spread of the members' daily maxima, and it is measured to "
    "have essentially no correlation with the realised day's error. Use "
    "the calibrated per-bucket sigma_f, not this per-day spread, as a "
    "predictive sigma."
)


def statistic_block(city: str) -> Dict[str, Any]:
    """The ``statistic`` key stamped into every ``*_gefs_v1.json``.

    Two *different* statistics can both be labelled ``source="gefs"``: the daily
    maximum of the ensemble mean field (what the backfill can produce) and the
    mean of the per-member daily maxima (what ``EnsembleProvider.fetch()``
    returns live). Nothing else in the artifact distinguishes them, so the
    artifact says which one it is, in the artifact.

    Pure and constant given ``city`` -- no clock, hostname, path or run id -- so
    the annotated payload stays byte-identical on re-run, which is what EC-2
    gates on.
    """
    return {
        "built_on": STATISTIC_GEAVG_MAX,
        "live_provider_statistic": STATISTIC_MEMBER_MEAN_MAX,
        "are_they_the_same": False,
        "why_not": OFFSET_PROVENANCE["why"],
        "offset_applied_f": GEAVG_TO_MEMBER_MEAN_OFFSET_F.get(str(city)),
        "offset_provenance": dict(OFFSET_PROVENANCE),
        "offset_effect_on_sigma": OFFSET_PROVENANCE["effect_on_sigma"],
        "spread_rule": SPREAD_RULE,
        "spread_caveat": SPREAD_CAVEAT,
        "product": "gefs pgrb2sp25 geavg + gespr, TMAX 2 m above ground",
        "resolution": "0p25",
        "cycle_hours_utc": [0],
    }


def calibration_annotation(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """:data:`~src.calibration.forecast_calibration.SOURCE_ANNOTATORS` hook.

    Registered against ``source="gefs"`` so that *every* producer of a
    ``*_gefs_v1.json`` -- the workstream-F backfill and the generic
    ``scripts/build_calibration.py --source gefs`` alike -- emits the same
    bytes. Before this hook existed the generic path silently dropped the block,
    producing a shorter file with a different ``content_hash`` and no statistic
    warning at all.
    """
    return {"statistic": statistic_block(str(payload.get("city", "")))}


# ---------------------------------------------------------------------------
# The 0.5 degree comparison (evidence only -- never the backfill path)
# ---------------------------------------------------------------------------
#: ``pgrb2ap5``: the same fields on a 0.5 deg grid. Kept as constants so the
#: resolution comparison in the report can be re-run, not so it can be adopted.
HALF_DEGREE_PREFIX = "gefs.{yyyymmdd}/{hh}/atmos/pgrb2ap5/"
HALF_DEGREE_OBJECT = "{member}.t{hh}z.pgrb2a.0p50.f{fhour:03d}"

GEFS_GRID_0P5 = LatLonGrid(
    ni=720,
    nj=361,
    lat1=90.0,
    lon1=0.0,
    lat2=-90.0,
    lon2=359.5,
    dlat=0.5,
    dlon=0.5,
    scan_mode=0,
)


def half_degree_nearest_node(spec: CitySpec) -> Tuple[int, int]:
    """The 0.5 deg node a city would be read from, for the resolution probe."""
    return GEFS_GRID_0P5.nearest_node(spec.latitude, spec.longitude)


__all__ = [
    "FORECAST_FIELDS",
    "GEAVG_TO_MEMBER_MEAN_OFFSET_F",
    "GEFS_GRID_0P5",
    "HALF_DEGREE_OBJECT",
    "HALF_DEGREE_PREFIX",
    "MAX_TRUSTED_OFFSET_F",
    "OFFSET_PROVENANCE",
    "SOURCE_GEFS_SERIES",
    "SPREAD_CAVEAT",
    "SPREAD_RULE",
    "STATISTIC_GEAVG_MAX",
    "STATISTIC_MEMBER_MEAN_MAX",
    "DerivedDailyHigh",
    "GefsSeriesError",
    "assemble_daily_high",
    "calibration_annotation",
    "cycle_plan",
    "half_degree_nearest_node",
    "kelvin_spread_to_fahrenheit",
    "lead_hours_for",
    "offset_for",
    "read_cycle_records",
    "statistic_block",
    "windows_for",
]
