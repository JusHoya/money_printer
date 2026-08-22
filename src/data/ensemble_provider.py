"""GEFS ensemble provider: a distribution over each city's daily high (F).

PRD FR-2.1. This module answers one question -- *what does the GEFS ensemble
say tomorrow's (or today's) maximum temperature will be at a Kalshi settlement
station, as a distribution rather than a point* -- and it is required to fail
loudly rather than answer it wrongly.

Settlement truth for the KXHIGH* series is the NWS Climatological Report daily
maximum at the settlement station over that station's **local calendar day**
(see :mod:`src.data.iem_cli_provider`). Every windowing decision below is
therefore made in the city's own timezone; there is no naive datetime in this
module.

VERIFIED UPSTREAM CONTRACT (probed live 2026-07-26)
---------------------------------------------------
Source: NOAA Open Data Dissemination mirror of GEFS on S3, **anonymous plain
HTTPS** -- no credentials, no ``boto3``.

===================================================  ========  =================
Request                                              Status    Result
===================================================  ========  =================
``GET /?list-type=2&prefix=gefs.20260725/00/atmos/   200       5346 keys,
pgrb2sp25/&max-keys=1000``                                     paginated via
                                                               ``NextContinuationToken``
``GET gefs.YYYYMMDD/00/atmos/pgrb2sp25/`` for        200       every date
``YYYYMMDD`` in 20260719..20260726                             present
``GET <object>.idx``                                 200       plain text, 38
                                                               records
``GET <object>`` with ``Range: bytes=a-b``           206       exact slice
``https://nomads.ncep.noaa.gov/dods/gefs/...``       301       dead end; not
                                                               used
===================================================  ========  =================

At each forecast hour the ``pgrb2sp25`` directory holds 33 files: ``gec00``
(control) + ``gep01``..``gep30`` (30 perturbed members) = **31 true members**,
plus the derived ``geavg`` (mean) and ``gespr`` (spread), which are *not*
members and are never counted toward :attr:`EnsembleProvider.min_members`.

WHICH FIELD, AND WHY IT MATTERS
-------------------------------
Each ``.grib2`` object has a sibling ``.idx`` listing every record's byte
offset, so a single record is pulled with an HTTP ``Range`` request (~430 KB)
instead of the whole ~14 MB file. The ``.idx`` for ``gep01 f024`` shows both::

    10:3759412:d=2026072500:TMP:2 m above ground:24 hour fcst:ENS=+1
    13:5313475:d=2026072500:TMAX:2 m above ground:18-24 hour max fcst:ENS=+1

``TMP`` is an **instantaneous** 2 m temperature sampled every 3 h. Building a
daily maximum from 3-hourly instantaneous samples systematically *under*
-estimates the true daily max, because the peak almost never lands exactly on a
sample. This module therefore uses **``TMAX``: a genuine max-over-interval
field**, which removes that particular bias at the source. The residual biases
that remain are enumerated under "KNOWN BIASES" below; they are real, they are
what the Phase-2 calibration must absorb, and none of them are hidden.

``TMAX`` interval structure, read off the live ``.idx`` files (f003..f123)::

    f003 -> 0-3      f006 -> 0-6      f009 -> 6-9      f012 -> 6-12
    f021 -> 18-21    f024 -> 18-24    f027 -> 24-27    f030 -> 24-30
    f120 -> 114-120  f123 -> 120-123

i.e. a step at ``6k+3`` covers ``[6k, 6k+3]`` and a step at ``6k+6`` covers
``[6k, 6k+6]``. The half-interval ``[6k+3, 6k+6]`` is never published on its
own, so the finest *tiling* of a long window is 6-hourly, with a 3-hour block
available only at the start of each 6-hour bucket.
:func:`tmax_windows` picks the minimal covering set and the exact covered lead
range is recorded in :attr:`EnsembleForecast.provenance` -- over-coverage is
reported, never silently absorbed.

GRIB2 DECODING (in-house, zero new dependencies)
------------------------------------------------
``cfgrib``/``eccodes`` and ``pygrib`` were both rejected: on this machine
(Python 3.14, Windows) ``pip install eccodes`` resolves to a pure-Python
binding wheel that raises ``RuntimeError: Cannot find the ecCodes library``,
and no binary distribution of the native library (``eccodeslib``,
``eccodes-lib``) exists for the platform. Since exactly one record and a
handful of grid points are needed, the records are decoded here with ``numpy``
and the stdlib only -- both already project dependencies.

The records published in ``pgrb2sp25`` use:

* Section 3 grid definition template **3.0** -- regular lat/lon, ``Ni=1440``,
  ``Nj=721``, ``La1=90N``, ``Lo1=0``, ``Di=Dj=0.25 deg``, scanning mode ``0x00``
  (west->east, north->south, i consecutive). 1 038 240 points.
* Section 5 data representation template **5.3** -- complex packing with
  second-order spatial differencing.
* Section 6 -- no bitmap (indicator 255).

One trap cost real debugging time and is guarded by
:func:`_unpack_complex`: **WMO data templates 7.2/7.3 pad each of the three
group-descriptor lists (references, widths, scaled lengths) to an octet
boundary**, while the widely copied ``g2clib`` ``comunpack`` reference reads
them bit-contiguously. For the live GEFS records ``NG=30714`` at 7 bits per
scaled group length ends 2 bits short of an octet, so a bit-contiguous reader
decodes every packed value 2 bits out of phase. The symptom is not a crash: the
second differences still look like small integers, but they carry a ~0.5 count
mean bias which double-integration amplifies into temperatures of order 1e9 K.

The decoder aligns after every list and keeps three independent consistency
checks, because which one catches a phase error depends on where the error
lands: the group lengths must sum to the declared point count, the packed
values must fit inside section 7, and -- the only one that fires when a shift
leaves both of those satisfied -- the minimum packed value must be exactly 0
once the overall difference minimum has been subtracted. On the record that
originally exposed this bug, only the last of the three caught it.
``tests/test_ensemble_provider.py::test_alignment_guard_is_not_vacuous``
mutates the alignment away and asserts the chain fires.

DECODER PROOF OF CORRECTNESS (recorded 2026-07-26)
--------------------------------------------------
Decoding ``gep01.t00z.pgrb2s.0p25.f024`` record 10 (``TMP:2 m``) of
``gefs.20260725/00``:

* global field range **210.2 K .. 321.3 K** -- physical for a July 2 m
  temperature field;
* row 0 (the north pole, 1440 co-located grid points) decodes to a single
  identical value, as it must;
* the spec invariant ``min(packed) == 0`` holds exactly.

Cross-checked against an artifact this repo did not produce: over 20 city-cycles
the mean of the 31 decoded members' daily highs differs from the daily high
decoded from NCEP's own ``geavg`` product by **-0.05 F to +0.32 F (mean
+0.10 F)**. ``max`` and ``mean`` do not commute, so Jensen puts the member mean
at or above the mean product; a decoder fault would show up as degrees, not
hundredths. ``scripts/fetch_ensemble.py --validate`` re-runs both this check and
the CLI-truth comparison into ``reports/phase2/ec1_ensemble_members.md``.

KNOWN BIASES (documented, not hidden -- Phase-2 calibration must absorb these)
-----------------------------------------------------------------------------
1. **Interval over-coverage.** A local calendar day rarely aligns with the
   6-hour ``TMAX`` buckets of a 00Z cycle, so the covering set spills up to 5 h
   before the local midnight that opens the day and up to 5 h past the one that
   closes it (measured: 4-5 h before / 1-2 h after for the four cities at
   day-ahead lead). The spill is night-time, when the daily max rarely occurs,
   but the *sign* of the resulting bias has not been isolated from the other
   terms below and is not asserted here. ``provenance["coverage"]`` records the
   exact covered lead range beside the requested one on every forecast, so the
   residual stays measurable instead of assumed.
2. **Grid cell vs point station.** A 0.25 deg (~25 km) cell is not a
   thermometer in Central Park. The nearest node to **KLAX (33.938N, 118.389W)
   is 34.00N, 118.50W, 12.3 km away on the Santa Monica Bay shoreline** -- the
   worst node/station mismatch of the four. The nearest node is used regardless
   (no silent re-selection), and a bilinear estimate at the true station
   coordinates is carried alongside as a *diagnostic only* under
   ``provenance["diagnostic_bilinear_f"]``.

   Measured over the 5-day EC-1 sample (day-ahead lead, ensemble mean minus CLI
   truth): NY **+2.9 F**, CHI **+1.6 F**, LAX **+5.8 F** (worst day +9.7 F),
   MIA **-3.2 F**. The bilinear diagnostic barely moves any of them (largest
   change 1.1 F, at MIA), so interpolation is not the fix. Two consequences the
   calibration must confront rather than average away: the biases do not share
   a sign, and at LAX the bias is several times the ensemble's own spread.
3. **Under-dispersion.** Measured member spread over the same sample is 0.5-2.9
   F while the mean-vs-truth error reaches 9.7 F. The raw ensemble spread is a
   *model* spread, not a calibrated predictive sigma; using it directly as
   P(bracket) would price tails far too confidently. Widening it is FR-2.2
   work and is deliberately *not* done here.
4. **Model-timestep max, not continuous max.** ``TMAX`` is the maximum over the
   model's own time steps inside the interval, not a continuous supremum. The
   residual is small relative to (2) but is not zero.

For reference, the bias this module *avoids* by reading ``TMAX`` instead of
3-hourly instantaneous ``TMP`` was measured on the 2026-07-24 00Z cycle across
all 31 members: the instantaneous construction under-estimates the daily high
by **0.42 F (CHI) to 0.91 F (LAX)**. Full numbers:
``reports/phase2/ec1_ensemble_members.md``.

ABORT, NEVER DEFAULT (FR-2.1, project rule ``abort-on-missing-critical-input``)
-------------------------------------------------------------------------------
:meth:`EnsembleProvider.get_forecast_or_abort` is the contract Phase-3
strategies consume. On any failure it emits **exactly one INFO line** carrying
a machine-readable reason code and re-raises :class:`EnsembleUnavailable`, so
signal generation stops. There is no default temperature, no last-known-good
substitution and no bare ``except``. To keep "exactly one INFO line" true, the
internal fetch/decode paths log detail at DEBUG and carry it in the exception
message, which the single INFO line then reproduces in full.

The FR-2.1 degradation path (NWS point forecast widened by a historical error
sigma) is **off by default**, must be enabled explicitly, requires an injected
sigma source -- a degraded forecast is never manufactured from an assumed
spread -- logs one INFO line when it engages, and stamps
``source="nws_point_degraded"`` onto the returned object so no downstream
consumer or report can mistake it for a real ensemble.

CACHING RULES
-------------
Two layers, both under ``data/ensemble/``:

* ``records/`` -- decoded grid-node values per (cycle, member, forecast hour).
  This is what makes a re-run free: one 430 KB range request serves every city
  whose node is in the same record.
* ``forecasts/`` -- assembled :class:`EnsembleForecast` objects per
  (city, target date, cycle).

A published model value is immutable, so both layers are cached without expiry.
**A failure is never cached** -- not an empty listing, not a missing record, not
a decode error -- because a cached absence turns a transient S3 blip into a
permanent hole, which is the failure mode :mod:`src.data.iem_cli_provider`
documents from the CLI archive.
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np

try:  # pragma: no cover - requests is a hard project dependency
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
ENSEMBLE_DIR = os.path.join(REPO_ROOT, "data", "ensemble")
#: Both cache layers live under a single ``cache/`` directory so one
#: ``.gitignore`` entry (``data/ensemble/cache/``) covers everything
#: regenerable, and nothing regenerable can be committed by accident.
RECORD_CACHE_SUBDIR = os.path.join("cache", "records")
FORECAST_CACHE_SUBDIR = os.path.join("cache", "forecasts")

# ---------------------------------------------------------------------------
# Upstream endpoints
# ---------------------------------------------------------------------------
GEFS_BUCKET_URL = "https://noaa-gefs-pds.s3.amazonaws.com"
GEFS_PREFIX_TEMPLATE = "gefs.{yyyymmdd}/{hh}/atmos/pgrb2sp25/"
GEFS_OBJECT_TEMPLATE = "{member}.t{hh}z.pgrb2s.0p25.f{fhour:03d}"

NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"

DEFAULT_USER_AGENT = (
    "money-printer/ensemble-forecast (github.com/JusHoya/money_printer; "
    "contact via repo owner)"
)

#: Source tags stamped onto :attr:`EnsembleForecast.source`. Downstream code and
#: every report must be able to tell a real ensemble from a degraded stand-in.
SOURCE_GEFS = "gefs_aws_pgrb2sp25"
SOURCE_NWS_DEGRADED = "nws_point_degraded"

#: The 31 true GEFS members. ``geavg``/``gespr`` are derived products and are
#: deliberately absent -- counting them would inflate the member count that
#: PRD Phase 2 exit criterion 1 gates on.
CONTROL_MEMBER = "gec00"
PERTURBED_MEMBERS: Tuple[str, ...] = tuple(f"gep{i:02d}" for i in range(1, 31))
DEFAULT_MEMBERS: Tuple[str, ...] = (CONTROL_MEMBER,) + PERTURBED_MEMBERS

#: Derived (non-member) products, exposed for the decoder cross-check only.
MEAN_PRODUCT = "geavg"
SPREAD_PRODUCT = "gespr"

#: The field this module reads, and the instantaneous field it deliberately
#: does not read (see "WHICH FIELD, AND WHY IT MATTERS").
FIELD_TMAX = "TMAX"
FIELD_TMP = "TMP"
LEVEL_2M = "2 m above ground"

#: GEFS pgrb2s publishes 3-hourly steps out to f240.
MAX_LEAD_HOURS = 240
STEP_HOURS = 3

#: Statuses that mean "ask again", not "this does not exist". Observed live:
#: the NODD bucket answers a fraction of a 31-member burst with 503.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

#: PRD Phase 2 exit criterion 1: "returns >=20 members for each of the 4 cities".
DEFAULT_MIN_MEMBERS = 20


# ---------------------------------------------------------------------------
# Reason codes -- every abort path names one of these, and only these.
# ---------------------------------------------------------------------------
REASON_UNKNOWN_CITY = "ENSEMBLE_UNKNOWN_CITY"
REASON_OFFLINE = "ENSEMBLE_OFFLINE"
REASON_S3_UNAVAILABLE = "ENSEMBLE_S3_UNAVAILABLE"
REASON_CYCLE_NOT_PUBLISHED = "ENSEMBLE_CYCLE_NOT_PUBLISHED"
REASON_RECORD_NOT_FOUND = "ENSEMBLE_RECORD_NOT_FOUND"
REASON_DECODE_FAILED = "ENSEMBLE_DECODE_FAILED"
REASON_INSUFFICIENT_MEMBERS = "ENSEMBLE_INSUFFICIENT_MEMBERS"
REASON_LEAD_OUT_OF_RANGE = "ENSEMBLE_LEAD_OUT_OF_RANGE"
REASON_DEGRADED_DISABLED = "ENSEMBLE_DEGRADED_DISABLED"
REASON_DEGRADED_UNCALIBRATED = "ENSEMBLE_DEGRADED_UNCALIBRATED"
REASON_DEGRADED_NWS_UNAVAILABLE = "ENSEMBLE_DEGRADED_NWS_UNAVAILABLE"

REASON_CODES = frozenset(
    {
        REASON_UNKNOWN_CITY,
        REASON_OFFLINE,
        REASON_S3_UNAVAILABLE,
        REASON_CYCLE_NOT_PUBLISHED,
        REASON_RECORD_NOT_FOUND,
        REASON_DECODE_FAILED,
        REASON_INSUFFICIENT_MEMBERS,
        REASON_LEAD_OUT_OF_RANGE,
        REASON_DEGRADED_DISABLED,
        REASON_DEGRADED_UNCALIBRATED,
        REASON_DEGRADED_NWS_UNAVAILABLE,
    }
)


class EnsembleUnavailable(Exception):
    """No honest forecast distribution could be produced.

    Always a hard abort for the caller. There is no safe default for "what will
    the high be": a substituted value produces trades priced against fiction,
    which is exactly the failure class this pivot exists to remove.

    :param reason_code: one of :data:`REASON_CODES`, for machine-readable logs.
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        if reason_code not in REASON_CODES:
            raise ValueError(f"unknown ensemble reason code {reason_code!r}")
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}" if message else reason_code)
        self.detail = message


# ---------------------------------------------------------------------------
# City registry (PRD A4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CitySpec:
    """One Kalshi weather city, its settlement station, and where that is.

    ``latitude``/``longitude`` are the station coordinates published by
    ``https://api.weather.gov/stations/<ICAO>`` (probed 2026-07-26), not
    approximations of the city centre: the forecast has to be sampled where the
    thermometer that settles the contract actually is.
    """

    city: str
    station: str
    series_ticker: str
    timezone: str
    latitude: float
    longitude: float
    station_name: str


CITIES: Dict[str, CitySpec] = {
    "NY": CitySpec(
        city="NY",
        station="KNYC",
        series_ticker="KXHIGHNY",
        timezone="America/New_York",
        latitude=40.78333,
        longitude=-73.96667,
        station_name="New York City, Central Park",
    ),
    "CHI": CitySpec(
        city="CHI",
        station="KMDW",
        series_ticker="KXHIGHCHI",
        timezone="America/Chicago",
        latitude=41.78417,
        longitude=-87.75528,
        station_name="Chicago, Chicago Midway Airport",
    ),
    "LAX": CitySpec(
        city="LAX",
        station="KLAX",
        series_ticker="KXHIGHLAX",
        timezone="America/Los_Angeles",
        latitude=33.93806,
        longitude=-118.38889,
        station_name="Los Angeles, Los Angeles International Airport",
    ),
    "MIA": CitySpec(
        city="MIA",
        station="KMIA",
        series_ticker="KXHIGHMIA",
        timezone="America/New_York",
        latitude=25.79056,
        longitude=-80.31639,
        station_name="Miami, Miami International Airport",
    ),
}


def get_city(city: str) -> CitySpec:
    """Look up a :class:`CitySpec` by short code (``NY``/``CHI``/``LAX``/``MIA``)."""
    key = str(city).upper().strip()
    if key not in CITIES:
        raise EnsembleUnavailable(
            REASON_UNKNOWN_CITY,
            f"{city!r} is not a tracked city (known: {sorted(CITIES)})",
        )
    return CITIES[key]


def verify_station_registry() -> List[str]:
    """Cross-check :data:`CITIES` against the settlement-truth registry.

    The station and timezone a forecast is sampled for must be the same ones
    ground truth is read from, or the whole calibration compares two different
    places. Returns a list of human-readable mismatches (empty when consistent);
    the import-time call logs them at ERROR rather than raising, so a registry
    drift is loud without making this module unimportable.
    """
    problems: List[str] = []
    try:
        from src.data.iem_cli_provider import CORE_STATIONS
    except Exception as exc:  # pragma: no cover - import layout guard
        return [f"could not import the CLI station registry: {exc}"]
    for spec in CITIES.values():
        truth = CORE_STATIONS.get(spec.station)
        if truth is None:
            problems.append(
                f"{spec.city}: station {spec.station} is not a CLI settlement station"
            )
            continue
        if truth.timezone != spec.timezone:
            problems.append(
                f"{spec.city}: timezone {spec.timezone} != CLI registry "
                f"{truth.timezone}"
            )
        if truth.series_ticker != spec.series_ticker:
            problems.append(
                f"{spec.city}: series {spec.series_ticker} != CLI registry "
                f"{truth.series_ticker}"
            )
    return problems


# ---------------------------------------------------------------------------
# Units and small helpers
# ---------------------------------------------------------------------------
def kelvin_to_fahrenheit(kelvin: float) -> float:
    """``K -> F``. GRIB ``TMP``/``TMAX`` are Kelvin; Kalshi settles in F."""
    return (float(kelvin) - 273.15) * 9.0 / 5.0 + 32.0


def fahrenheit_to_kelvin(fahrenheit: float) -> float:
    """``F -> K``, the exact inverse of :func:`kelvin_to_fahrenheit`."""
    return (float(fahrenheit) - 32.0) * 5.0 / 9.0 + 273.15


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_date(value: Any) -> _date:
    """Coerce ``date``/``datetime``/``YYYY-MM-DD`` to a :class:`datetime.date`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"unparseable date {value!r} (want YYYY-MM-DD)") from exc


def _require_aware_utc(value: datetime, what: str) -> datetime:
    """Reject naive datetimes outright.

    A naive hour gate is a documented defect in this project's history (the UTC
    hour-gate bug in the pre-pivot weather stack), so the type system is not
    trusted to keep it out -- it is checked.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got naive {value!r}")
    return value.astimezone(timezone.utc)


def local_day_bounds_utc(target_date: _date, tz_name: str) -> Tuple[datetime, datetime]:
    """``(start, end)`` in UTC of the local calendar day ``target_date``.

    ``end`` is the *next* local midnight, i.e. the half-open interval
    ``[start, end)``. Computed through :mod:`zoneinfo`, so DST transitions
    shorten or lengthen the day exactly as the station's clock does -- a day
    with a spring-forward transition is 23 h long and the window is 23 h wide.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=tz)
    end_local = datetime.combine(
        target_date + timedelta(days=1), datetime.min.time(), tzinfo=tz
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# TMAX interval selection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TmaxWindow:
    """One published ``TMAX`` record and the lead-hour interval it maximises over."""

    fhour: int
    interval_start: int
    interval_end: int


def tmax_interval_for(fhour: int) -> Tuple[int, int]:
    """The ``[start, end]`` lead-hour interval of the ``TMAX`` record at ``fhour``.

    Read off the live ``.idx`` files: a step at ``6k+3`` maximises over
    ``[6k, 6k+3]``, a step at ``6k+6`` over ``[6k, 6k+6]``.
    """
    if fhour <= 0 or fhour % STEP_HOURS != 0:
        raise ValueError(f"f{fhour} is not a published 3-hourly GEFS step")
    if fhour % 6 == 0:
        return fhour - 6, fhour
    return fhour - 3, fhour


def tmax_windows(day_start_lead: int, day_end_lead: int) -> Tuple[TmaxWindow, ...]:
    """Minimal set of ``TMAX`` records whose union covers ``[start, end]``.

    The half-interval ``[6k+3, 6k+6]`` is never published alone, so a window
    that opens inside the second half of a 6-hour bucket must take the whole
    bucket. The over-coverage that produces is reported (see
    :func:`coverage_of`), never silently absorbed.
    """
    if day_end_lead <= day_start_lead:
        raise ValueError(
            f"empty forecast window: start lead {day_start_lead} >= end lead "
            f"{day_end_lead}"
        )
    if day_start_lead < 0:
        raise EnsembleUnavailable(
            REASON_LEAD_OUT_OF_RANGE,
            f"target day starts {abs(day_start_lead)} h before the model cycle; "
            f"the cycle cannot forecast its own past",
        )
    windows: List[TmaxWindow] = []
    cursor = int(day_start_lead)
    while cursor < day_end_lead:
        bucket = (cursor // 6) * 6
        if cursor < bucket + 3 and day_end_lead <= bucket + 3:
            fhour = bucket + 3
        else:
            fhour = bucket + 6
        lo, hi = tmax_interval_for(fhour)
        windows.append(TmaxWindow(fhour=fhour, interval_start=lo, interval_end=hi))
        cursor = hi
    last = windows[-1].fhour
    if last > MAX_LEAD_HOURS:
        raise EnsembleUnavailable(
            REASON_LEAD_OUT_OF_RANGE,
            f"the target day needs f{last:03d}, beyond the f{MAX_LEAD_HOURS:03d} "
            f"3-hourly GEFS pgrb2s horizon",
        )
    return tuple(windows)


def coverage_of(windows: Sequence[TmaxWindow]) -> Tuple[int, int]:
    """``(first interval start, last interval end)`` of a covering set."""
    return (
        min(w.interval_start for w in windows),
        max(w.interval_end for w in windows),
    )


# ---------------------------------------------------------------------------
# Grid geometry (GRIB2 grid definition template 3.0, regular lat/lon)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LatLonGrid:
    """A regular lat/lon grid, as declared by the record's own section 3."""

    ni: int
    nj: int
    lat1: float
    lon1: float
    lat2: float
    lon2: float
    dlat: float
    dlon: float
    scan_mode: int

    def node_index(self, j: int, i: int) -> int:
        """Flat index of grid node ``(j, i)`` under scanning mode ``0x00``."""
        return j * self.ni + i

    def node_lat_lon(self, j: int, i: int) -> Tuple[float, float]:
        """Latitude/longitude of node ``(j, i)``; longitude normalised to (-180, 180]."""
        lat = self.lat1 - j * self.dlat
        lon = (self.lon1 + i * self.dlon) % 360.0
        if lon > 180.0:
            lon -= 360.0
        return lat, lon

    def nearest_node(self, latitude: float, longitude: float) -> Tuple[int, int]:
        """Nearest ``(j, i)`` node to a station. No interpolation, no re-selection."""
        if self.scan_mode != 0:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED,
                f"unsupported GRIB scanning mode 0x{self.scan_mode:02x}; this "
                f"module only implements the +i/-j layout GEFS publishes",
            )
        j = int(round((self.lat1 - float(latitude)) / self.dlat))
        i = int(round(((float(longitude) % 360.0) - self.lon1) / self.dlon)) % self.ni
        j = max(0, min(self.nj - 1, j))
        return j, i

    def surrounding_nodes(
        self, latitude: float, longitude: float
    ) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
        """The four nodes bracketing a station, for the bilinear *diagnostic*."""
        fj = (self.lat1 - float(latitude)) / self.dlat
        fi = ((float(longitude) % 360.0) - self.lon1) / self.dlon
        j0 = max(0, min(self.nj - 2, int(math.floor(fj))))
        i0 = int(math.floor(fi)) % self.ni
        i1 = (i0 + 1) % self.ni
        return (j0, i0), (j0, i1), (j0 + 1, i0), (j0 + 1, i1)

    def bilinear_weights(
        self, latitude: float, longitude: float
    ) -> Tuple[float, float, float, float]:
        """Weights matching :meth:`surrounding_nodes`, in the same order."""
        fj = (self.lat1 - float(latitude)) / self.dlat
        fi = ((float(longitude) % 360.0) - self.lon1) / self.dlon
        tj = fj - math.floor(fj)
        ti = fi - math.floor(fi)
        return (
            (1 - tj) * (1 - ti),
            (1 - tj) * ti,
            tj * (1 - ti),
            tj * ti,
        )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km on the GRIB2 shape-of-earth-6 sphere."""
    radius = 6371.229
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# GRIB2 index (.idx) parsing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IdxRecord:
    """One line of a NCEP ``.idx`` sidecar, plus the byte range it implies."""

    number: int
    offset: int
    end_offset: Optional[int]
    reference: str
    field: str
    level: str
    forecast: str
    tail: str

    @property
    def range_header(self) -> str:
        """The value for an HTTP ``Range:`` header covering exactly this record."""
        if self.end_offset is None:
            return f"bytes={self.offset}-"
        return f"bytes={self.offset}-{self.end_offset - 1}"


def parse_idx(text: str) -> List[IdxRecord]:
    """Parse a ``.idx`` sidecar into records with resolved byte ranges.

    Each line is ``num:offset:d=YYYYMMDDHH:FIELD:LEVEL:FORECAST:REST``. The end
    offset of record *n* is the start offset of record *n+1*; the final record
    runs to end of object.
    """
    rows: List[List[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 6:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED, f"unparseable .idx line: {line!r}"
            )
        rows.append(parts)
    if not rows:
        raise EnsembleUnavailable(REASON_RECORD_NOT_FOUND, "empty .idx sidecar")

    out: List[IdxRecord] = []
    for k, parts in enumerate(rows):
        try:
            number = int(parts[0])
            offset = int(parts[1])
        except ValueError as exc:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED, f"non-numeric .idx offset in {parts!r}"
            ) from exc
        end = int(rows[k + 1][1]) if k + 1 < len(rows) else None
        out.append(
            IdxRecord(
                number=number,
                offset=offset,
                end_offset=end,
                reference=parts[2],
                field=parts[3],
                level=parts[4],
                forecast=parts[5],
                tail=":".join(parts[6:]),
            )
        )
    return out


def select_idx_record(
    records: Sequence[IdxRecord], field_name: str, level: str
) -> IdxRecord:
    """The single record matching ``field_name`` at ``level``.

    Ambiguity is an error rather than a first-match: two ``TMP:2 m`` records
    would mean the product changed shape, and silently taking one of them is how
    a wrong field ends up feeding a strategy.
    """
    hits = [r for r in records if r.field == field_name and r.level == level]
    if not hits:
        raise EnsembleUnavailable(
            REASON_RECORD_NOT_FOUND,
            f"no {field_name}:{level} record in the .idx "
            f"(available: {sorted({r.field for r in records})})",
        )
    if len(hits) > 1:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"{len(hits)} records match {field_name}:{level}; refusing to guess",
        )
    return hits[0]


# ---------------------------------------------------------------------------
# GRIB2 decoding
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Grib2Field:
    """A decoded GRIB2 record: grid, values, and the metadata that identifies it."""

    grid: LatLonGrid
    values: np.ndarray
    decoded_points: int
    total_points: int
    discipline: int
    parameter_category: int
    parameter_number: int
    pdt_number: int
    drs_template: int
    reference_time: datetime
    forecast_time: int
    interval_start: Optional[int]
    interval_end: Optional[int]
    perturbation_number: Optional[int]
    ensemble_type: Optional[int]
    derived_forecast_type: Optional[int]
    ensemble_size: Optional[int]
    statistical_process: Optional[int]

    def at(self, index: int) -> float:
        if index >= self.decoded_points:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED,
                f"grid index {index} was not decoded (stopped at "
                f"{self.decoded_points} points)",
            )
        return float(self.values[index])


def _split_sections(record: bytes) -> Dict[int, bytes]:
    """Split a single GRIB2 message into ``{section number: bytes}``."""
    if len(record) < 20 or record[:4] != b"GRIB":
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED, "byte range does not start with a GRIB header"
        )
    edition = record[7]
    if edition != 2:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED, f"GRIB edition {edition}, expected 2"
        )
    declared = struct.unpack(">Q", record[8:16])[0]
    if declared > len(record):
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"truncated record: header declares {declared} bytes, got {len(record)}",
        )
    sections: Dict[int, bytes] = {0: record[:16]}
    pos = 16
    while pos < declared - 4:
        if record[pos : pos + 4] == b"7777":
            break
        length = struct.unpack(">I", record[pos : pos + 4])[0]
        if length <= 0 or pos + length > declared:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED, f"bad section length {length} at offset {pos}"
            )
        sections[record[pos + 4]] = record[pos : pos + length]
        pos += length
    for required in (1, 3, 4, 5, 6, 7):
        if required not in sections:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED, f"record is missing GRIB2 section {required}"
            )
    return sections


def _signed_grib(raw: int, bits: int = 32) -> int:
    """GRIB2 integers are sign-and-magnitude, not two's complement."""
    sign_bit = 1 << (bits - 1)
    return -(raw & (sign_bit - 1)) if raw & sign_bit else raw


def _parse_grid(section3: bytes) -> LatLonGrid:
    template = struct.unpack(">H", section3[12:14])[0]
    if template != 0:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"grid definition template 3.{template}; only 3.0 (regular lat/lon) "
            f"is implemented",
        )
    t = section3[14:]

    def u4(off: int) -> int:
        return struct.unpack(">I", t[off : off + 4])[0]

    return LatLonGrid(
        ni=u4(16),
        nj=u4(20),
        lat1=_signed_grib(u4(32)) / 1e6,
        lon1=_signed_grib(u4(36)) / 1e6,
        lat2=_signed_grib(u4(41)) / 1e6,
        lon2=_signed_grib(u4(45)) / 1e6,
        dlon=u4(49) / 1e6,
        dlat=u4(53) / 1e6,
        scan_mode=t[57],
    )


def _parse_product(section4: bytes, reference_time: datetime) -> Dict[str, Any]:
    """Read product definition templates 4.1, 4.2, 4.11 and 4.12.

    4.1/4.11 are individual ensemble members (instantaneous / statistically
    processed over an interval); 4.2/4.12 are the *derived* products ``geavg``
    and ``gespr``. The two families are identical up to octet 34 and then
    diverge by exactly one octet -- a member carries
    ``(ensemble type, perturbation number, ensemble size)`` where a derived
    product carries ``(derived type, ensemble size)`` -- which shifts the whole
    statistical-processing block. Getting that off-by-one wrong reads the
    interval out of the wrong bytes, so the two layouts are spelled out rather
    than approximated.

    Parsing the interval from the template (rather than from the ``.idx``
    label) is what lets :meth:`EnsembleProvider.fetch_record_values` verify that
    the record it downloaded really maximises over the hours it asked for. The
    label is a convenience string; the template is the contract.
    """
    pdt = struct.unpack(">H", section4[7:9])[0]
    if pdt not in (1, 2, 11, 12):
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"product definition template 4.{pdt}; only 4.1, 4.2, 4.11 and 4.12 "
            f"are implemented",
        )
    body = section4[9:]
    time_unit = body[8]
    if time_unit != 1:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"forecast time unit indicator {time_unit}; only 1 (hour) is implemented",
        )
    forecast_time = struct.unpack(">i", body[9:13])[0]
    derived = pdt in (2, 12)
    out: Dict[str, Any] = {
        "pdt_number": pdt,
        "parameter_category": body[0],
        "parameter_number": body[1],
        "forecast_time": forecast_time,
        "ensemble_type": None if derived else body[25],
        "perturbation_number": None if derived else body[26],
        "derived_forecast_type": body[25] if derived else None,
        "ensemble_size": body[26] if derived else body[27],
        "interval_start": None,
        "interval_end": None,
        "statistical_process": None,
    }
    if pdt in (11, 12):
        base = 27 if derived else 28
        end = datetime(
            struct.unpack(">H", body[base : base + 2])[0],
            body[base + 2],
            body[base + 3],
            body[base + 4],
            body[base + 5],
            body[base + 6],
            tzinfo=timezone.utc,
        )
        out["statistical_process"] = body[base + 12]
        out["interval_start"] = forecast_time
        out["interval_end"] = int(
            round((end - reference_time).total_seconds() / 3600.0)
        )
    return out


def _read_uints(
    bits: np.ndarray, offset: int, width: int, count: int
) -> Tuple[np.ndarray, int]:
    """Read ``count`` big-endian unsigned ints of ``width`` bits from a bit array."""
    if count == 0:
        return np.zeros(0, dtype=np.int64), offset
    if width == 0:
        return np.zeros(count, dtype=np.int64), offset
    need = width * count
    if offset + need > bits.size:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"data section exhausted: need {need} bits at offset {offset}, "
            f"have {bits.size}",
        )
    block = bits[offset : offset + need].reshape(count, width).astype(np.int64)
    weights = 1 << np.arange(width - 1, -1, -1, dtype=np.int64)
    return block @ weights, offset + need


def _align_octet(offset: int) -> int:
    """Advance a bit offset to the next octet boundary.

    WMO data templates 7.2/7.3 pad each group-descriptor list to an octet
    boundary. Omitting this is the two-bit phase error described in the module
    docstring; it does not crash, it silently produces 1e9 K temperatures.
    """
    return ((offset + 7) // 8) * 8


def _unpack_complex(
    section5: bytes, section7: bytes, max_points: Optional[int]
) -> np.ndarray:
    """Decode data representation template 5.2/5.3 (complex packing).

    ``max_points`` stops group decoding once that many points are available.
    Spatial differencing is a running integration from point 0, so the field can
    be truncated but never sampled randomly.
    """
    ndpts = struct.unpack(">I", section5[5:9])[0]
    template = struct.unpack(">H", section5[9:11])[0]
    reference = struct.unpack(">f", section5[11:15])[0]
    binary_scale = struct.unpack(">h", section5[15:17])[0]
    decimal_scale = struct.unpack(">h", section5[17:19])[0]
    nbits = section5[19]
    missing_management = section5[22]
    if missing_management != 0:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"missing-value management {missing_management} is not implemented",
        )
    ngroups = struct.unpack(">I", section5[31:35])[0]
    ref_width = section5[35]
    nbits_width = section5[36]
    ref_length = struct.unpack(">I", section5[37:41])[0]
    length_increment = section5[41]
    last_length = struct.unpack(">I", section5[42:46])[0]
    nbits_length = section5[46]
    order = section5[47] if template == 3 else 0
    extra_octets = section5[48] if template == 3 else 0

    bits = np.unpackbits(np.frombuffer(section7[5:], dtype=np.uint8))
    offset = 0

    first_values: List[int] = []
    minimum_difference = 0
    if template == 3:
        if extra_octets == 0:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED,
                "template 5.3 declares 0 extra descriptor octets",
            )
        width = extra_octets * 8
        for _ in range(order + 1):
            sign, offset = _read_uints(bits, offset, 1, 1)
            magnitude, offset = _read_uints(bits, offset, width - 1, 1)
            value = int(magnitude[0])
            first_values.append(-value if int(sign[0]) == 1 else value)
        minimum_difference = first_values.pop()
        offset = _align_octet(offset)

    group_refs, offset = _read_uints(bits, offset, nbits, ngroups)
    offset = _align_octet(offset)
    group_widths, offset = _read_uints(bits, offset, nbits_width, ngroups)
    group_widths = group_widths + ref_width
    offset = _align_octet(offset)
    group_lengths, offset = _read_uints(bits, offset, nbits_length, ngroups)
    group_lengths = group_lengths * length_increment + ref_length
    group_lengths[ngroups - 1] = last_length
    offset = _align_octet(offset)

    if int(group_lengths.sum()) != ndpts:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"group lengths sum to {int(group_lengths.sum())}, section 5 declares "
            f"{ndpts} points -- the descriptor lists are out of phase",
        )

    group_bits = group_widths * group_lengths
    group_base = np.concatenate(([offset], offset + np.cumsum(group_bits)[:-1]))

    used = ngroups
    if max_points is not None and max_points < ndpts:
        used = int(np.searchsorted(np.cumsum(group_lengths), max_points, "left")) + 1
        used = min(used, ngroups)

    lengths = group_lengths[:used]
    widths = group_widths[:used]
    total = int(lengths.sum())
    starts = np.concatenate(([0], np.cumsum(lengths)[:-1]))

    per_value_bit = np.repeat(group_base[:used], lengths) + (
        np.arange(total) - np.repeat(starts, lengths)
    ) * np.repeat(widths, lengths)
    per_value_width = np.repeat(widths, lengths)

    packed = np.repeat(group_refs[:used], lengths).astype(np.int64)
    for width in np.unique(per_value_width):
        if width == 0:
            continue
        selected = np.nonzero(per_value_width == width)[0]
        index = per_value_bit[selected][:, None] + np.arange(int(width))[None, :]
        if int(index[-1, -1]) >= bits.size:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED,
                "packed values run past the end of section 7",
            )
        weights = 1 << np.arange(int(width) - 1, -1, -1, dtype=np.int64)
        packed[selected] += bits[index].astype(np.int64) @ weights

    if template == 3:
        if used == ngroups:
            # The spec subtracts the overall minimum from every difference, so
            # the minimum packed value over the *whole* field must be exactly 0.
            # This is the only cheap check that actually discriminates a
            # descriptor-list phase error: a 2-bit shift still leaves the total
            # bit budget inside its octet of slack, and still leaves the group
            # references (read before the misaligned list) intact.
            if int(packed.min()) != 0:
                raise EnsembleUnavailable(
                    REASON_DECODE_FAILED,
                    f"minimum packed difference is {int(packed.min())}, must be 0 "
                    f"after the overall minimum is subtracted -- the "
                    f"group-descriptor lists are out of phase",
                )
        else:
            logger.debug(
                "decoded %d of %d groups; the min-packed-difference invariant "
                "cannot be checked on a truncated field",
                used,
                ngroups,
            )

    if order == 0:
        integrated = packed
    elif order == 1:
        packed[1:] += minimum_difference
        packed[0] = first_values[0]
        integrated = np.cumsum(packed)
    elif order == 2:
        packed[2:] += minimum_difference
        increments = np.concatenate(([first_values[1] - first_values[0]], packed[2:]))
        integrated = np.concatenate(
            ([first_values[0]], first_values[0] + np.cumsum(np.cumsum(increments)))
        )
    else:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"spatial differencing order {order} is not implemented",
        )

    return (reference + integrated.astype(np.float64) * (2.0**binary_scale)) * (
        10.0**-decimal_scale
    )


def _unpack_simple(section5: bytes, section7: bytes) -> np.ndarray:
    """Decode data representation template 5.0 (simple packing)."""
    ndpts = struct.unpack(">I", section5[5:9])[0]
    reference = struct.unpack(">f", section5[11:15])[0]
    binary_scale = struct.unpack(">h", section5[15:17])[0]
    decimal_scale = struct.unpack(">h", section5[17:19])[0]
    nbits = section5[19]
    bits = np.unpackbits(np.frombuffer(section7[5:], dtype=np.uint8))
    packed, _ = _read_uints(bits, 0, nbits, ndpts)
    return (reference + packed.astype(np.float64) * (2.0**binary_scale)) * (
        10.0**-decimal_scale
    )


def decode_grib2_record(
    record: bytes, *, max_points: Optional[int] = None
) -> Grib2Field:
    """Decode one GRIB2 message into a :class:`Grib2Field`.

    ``max_points`` truncates decoding once that many grid points are available.
    It exists for probes and tests only: :class:`EnsembleProvider` never uses
    it, because truncation makes the min-packed-difference invariant
    uncheckable and the full decode costs 0.135 s behind a ~0.9 s download.
    """
    sections = _split_sections(record)
    reference_time = datetime(
        struct.unpack(">H", sections[1][12:14])[0],
        sections[1][14],
        sections[1][15],
        sections[1][16],
        sections[1][17],
        sections[1][18],
        tzinfo=timezone.utc,
    )
    grid = _parse_grid(sections[3])
    product = _parse_product(sections[4], reference_time)

    bitmap_indicator = sections[6][5]
    if bitmap_indicator != 255:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"bitmap indicator {bitmap_indicator}; a bitmapped record would need "
            f"the values re-expanded before indexing and is not implemented",
        )

    drs_template = struct.unpack(">H", sections[5][9:11])[0]
    if drs_template in (2, 3):
        values = _unpack_complex(sections[5], sections[7], max_points)
    elif drs_template == 0:
        values = _unpack_simple(sections[5], sections[7])
    else:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"data representation template 5.{drs_template} is not implemented",
        )

    total = struct.unpack(">I", sections[5][5:9])[0]
    if grid.ni * grid.nj != total:
        raise EnsembleUnavailable(
            REASON_DECODE_FAILED,
            f"grid declares {grid.ni}x{grid.nj} nodes but section 5 declares "
            f"{total} data points",
        )
    return Grib2Field(
        grid=grid,
        values=values,
        decoded_points=int(values.size),
        total_points=total,
        discipline=sections[0][6],
        parameter_category=product["parameter_category"],
        parameter_number=product["parameter_number"],
        pdt_number=product["pdt_number"],
        drs_template=drs_template,
        reference_time=reference_time,
        forecast_time=product["forecast_time"],
        interval_start=product["interval_start"],
        interval_end=product["interval_end"],
        perturbation_number=product["perturbation_number"],
        ensemble_type=product["ensemble_type"],
        derived_forecast_type=product["derived_forecast_type"],
        ensemble_size=product["ensemble_size"],
        statistical_process=product["statistical_process"],
    )


# ---------------------------------------------------------------------------
# The forecast object
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EnsembleForecast:
    """A distribution over one city's daily high temperature, in degrees F.

    ``members_f`` is one daily high per ensemble member: the maximum of that
    member's 2 m ``TMAX`` over the forecast intervals covering the station's
    local calendar day. It is a sample, not a fitted distribution -- turning it
    into P(bracket) is FR-2.3 and belongs to the probability engine.

    ``source`` is the field a report or a strategy must read before trusting the
    numbers: :data:`SOURCE_GEFS` is a real ensemble, :data:`SOURCE_NWS_DEGRADED`
    is a point forecast widened by an injected historical sigma.
    """

    city: str
    station: str
    target_date: _date
    init_time: datetime
    lead_hours: int
    members_f: Tuple[float, ...]
    source: str
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def member_count(self) -> int:
        return len(self.members_f)

    @property
    def mean_f(self) -> float:
        return sum(self.members_f) / len(self.members_f)

    @property
    def sigma_f(self) -> float:
        """Sample standard deviation (n-1) of the member daily highs."""
        n = len(self.members_f)
        if n < 2:
            return 0.0
        mean = self.mean_f
        return math.sqrt(sum((x - mean) ** 2 for x in self.members_f) / (n - 1))

    def quantile_f(self, q: float) -> float:
        """Empirical quantile of the member distribution (linear interpolation)."""
        if not 0.0 <= q <= 1.0:
            raise ValueError(f"quantile {q} outside [0, 1]")
        ordered = sorted(self.members_f)
        if len(ordered) == 1:
            return ordered[0]
        pos = q * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(ordered) - 1)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "city": self.city,
            "station": self.station,
            "target_date": self.target_date.isoformat(),
            "init_time": self.init_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lead_hours": self.lead_hours,
            "members_f": list(self.members_f),
            "source": self.source,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, blob: Mapping[str, Any]) -> "EnsembleForecast":
        return cls(
            city=blob["city"],
            station=blob["station"],
            target_date=_coerce_date(blob["target_date"]),
            init_time=datetime.strptime(
                blob["init_time"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc),
            lead_hours=int(blob["lead_hours"]),
            members_f=tuple(float(x) for x in blob["members_f"]),
            source=blob["source"],
            provenance=dict(blob.get("provenance") or {}),
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class EnsembleProvider:
    """GEFS ensemble daily highs for the PRD A4 cities.

    Shaped like :class:`src.data.iem_cli_provider.IEMCLIProvider` (a
    ``connect()`` plus explicit fetch methods) and, for the same reason,
    deliberately **not** a :class:`src.core.interfaces.DataProvider`: that ABC's
    ``fetch_latest`` returns ``MarketData`` (price/bid/ask/volume), which has no
    honest meaning for a temperature distribution.

    :param cache_dir: root of the two-layer cache; defaults to ``data/ensemble``.
    :param members: which GEFS members to read. Defaults to all 31 true members.
    :param min_members: floor below which the forecast is refused outright
        (PRD Phase 2 exit criterion 1 gates on >=20).
    :param allow_degraded: enable the FR-2.1 NWS degradation path. Off by
        default: a degraded forecast is a materially different object and the
        caller must opt into it.
    :param sigma_provider: ``(city, lead_hours) -> sigma in F``, the historical
        forecast-error spread the degradation path widens the point forecast by.
        Without it the degraded path refuses to run rather than assume a spread.
    :param max_workers: HTTP concurrency. Capped low on purpose -- this runs on
        a laptop that machine-checks under sustained all-core load.
    :param max_retries: retries per request on the statuses in
        :data:`RETRYABLE_STATUSES` and on transport faults. A 404 is never
        retried: it is an answer, not a transient.
    :param retry_backoff: seconds before the first retry; doubles each attempt.
    :param offline: refuse all network calls and serve the cache only.
    """

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        *,
        session: Any = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 60,
        members: Sequence[str] = DEFAULT_MEMBERS,
        min_members: int = DEFAULT_MIN_MEMBERS,
        allow_degraded: bool = False,
        sigma_provider: Any = None,
        max_workers: int = 4,
        max_retries: int = 4,
        retry_backoff: float = 0.5,
        cycle_hour: int = 0,
        publish_delay_hours: int = 6,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir or ENSEMBLE_DIR
        self.record_cache_dir = os.path.join(self.cache_dir, RECORD_CACHE_SUBDIR)
        self.forecast_cache_dir = os.path.join(self.cache_dir, FORECAST_CACHE_SUBDIR)
        self.user_agent = user_agent
        self.timeout = int(timeout)
        self.members = tuple(members)
        self.min_members = int(min_members)
        self.allow_degraded = bool(allow_degraded)
        self.sigma_provider = sigma_provider
        self.max_workers = max(1, min(4, int(max_workers)))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.cycle_hour = int(cycle_hour)
        self.publish_delay_hours = int(publish_delay_hours)
        self.offline = bool(offline)
        self._session = session
        self._idx_memo: Dict[str, List[IdxRecord]] = {}

    # -- plumbing ---------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            if requests is None:  # pragma: no cover
                raise EnsembleUnavailable(
                    REASON_S3_UNAVAILABLE, "requests is unavailable"
                )
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.user_agent})
        return self._session

    def connect(self) -> bool:
        """Verify the bucket answers. Returns ``False`` (logged) rather than raising."""
        os.makedirs(self.record_cache_dir, exist_ok=True)
        os.makedirs(self.forecast_cache_dir, exist_ok=True)
        if self.offline:
            logger.info(
                "EnsembleProvider: offline mode, serving cache at %s", self.cache_dir
            )
            return True
        try:
            response = self.session.get(
                f"{GEFS_BUCKET_URL}/?list-type=2&max-keys=1", timeout=self.timeout
            )
        except Exception as exc:
            logger.error("EnsembleProvider.connect failed: %s", exc)
            return False
        if getattr(response, "status_code", None) != 200:
            logger.error(
                "EnsembleProvider.connect: bucket returned HTTP %s",
                getattr(response, "status_code", None),
            )
            return False
        return True

    def _get(self, url: str, *, headers: Optional[Mapping[str, str]] = None):
        """One GET, with bounded backoff on the statuses S3 uses for "slow down".

        Fetching 31 members x ~6 forecast hours makes several hundred ranged
        requests per cycle, and the NODD bucket answers a fraction of them with
        HTTP 503 under that burst. Left unretried, those transients cost enough
        members to trip the :attr:`min_members` floor and mark a perfectly
        published cycle as unavailable -- which is a false negative, not
        caution. Retries are bounded and only cover retryable statuses and
        transport faults; a 404 is still an immediate, non-retried answer.
        """
        if self.offline:
            raise EnsembleUnavailable(
                REASON_OFFLINE, f"offline mode: refusing network call to {url}"
            )
        last: str = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self.retry_backoff * (2 ** (attempt - 1)))
            try:
                response = self.session.get(
                    url, headers=dict(headers or {}), timeout=self.timeout
                )
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            status = getattr(response, "status_code", None)
            if status not in RETRYABLE_STATUSES:
                return response
            last = f"HTTP {status}"
            logger.debug(
                "GET %s returned %s (attempt %d/%d); backing off",
                url,
                last,
                attempt + 1,
                self.max_retries + 1,
            )
        raise EnsembleUnavailable(
            REASON_S3_UNAVAILABLE,
            f"GET {url} failed after {self.max_retries + 1} attempts ({last})",
        )

    # -- object naming ----------------------------------------------------
    def object_key(self, init_time: datetime, member: str, fhour: int) -> str:
        """S3 key of one member's forecast-hour file."""
        init = _require_aware_utc(init_time, "init_time")
        prefix = GEFS_PREFIX_TEMPLATE.format(
            yyyymmdd=init.strftime("%Y%m%d"), hh=f"{init.hour:02d}"
        )
        name = GEFS_OBJECT_TEMPLATE.format(
            member=member, hh=f"{init.hour:02d}", fhour=fhour
        )
        return prefix + name

    def object_url(self, init_time: datetime, member: str, fhour: int) -> str:
        return f"{GEFS_BUCKET_URL}/{self.object_key(init_time, member, fhour)}"

    # -- record cache -----------------------------------------------------
    def _record_cache_path(
        self, init_time: datetime, member: str, fhour: int, field_name: str
    ) -> str:
        init = _require_aware_utc(init_time, "init_time")
        cycle = init.strftime("%Y%m%d%H")
        return os.path.join(
            self.record_cache_dir,
            cycle,
            f"{member}_f{fhour:03d}_{field_name}.json",
        )

    @staticmethod
    def _node_key(node: Tuple[int, int]) -> str:
        return f"{node[0]},{node[1]}"

    def _load_record_cache(
        self,
        init_time: datetime,
        member: str,
        fhour: int,
        field_name: str,
        nodes: Sequence[Tuple[int, int]],
    ) -> Optional[Dict[str, Any]]:
        path = self._record_cache_path(init_time, member, fhour, field_name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                blob = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.debug(
                "ensemble record cache %s unreadable (%s); ignoring", path, exc
            )
            return None
        values = blob.get("nodes_k")
        if not isinstance(values, dict):
            return None
        if any(self._node_key(node) not in values for node in nodes):
            return None  # a node this caller needs was not in the cached set
        return blob

    def _save_record_cache(self, path: str, blob: Mapping[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(blob, handle, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug("could not write ensemble record cache %s: %s", path, exc)

    # -- one record -------------------------------------------------------
    def fetch_idx(
        self, init_time: datetime, member: str, fhour: int
    ) -> List[IdxRecord]:
        """The ``.idx`` sidecar for one member/forecast-hour object."""
        url = self.object_url(init_time, member, fhour) + ".idx"
        if url in self._idx_memo:
            return self._idx_memo[url]
        if len(self._idx_memo) > 4096:
            # Bounded so a long-lived harvester process cannot grow this memo
            # without limit. Dropping it costs one cheap sidecar re-read.
            self._idx_memo.clear()
        response = self._get(url)
        status = getattr(response, "status_code", None)
        if status == 404:
            raise EnsembleUnavailable(
                REASON_CYCLE_NOT_PUBLISHED,
                f"{url} is not published (HTTP 404)",
            )
        if status != 200:
            raise EnsembleUnavailable(
                REASON_S3_UNAVAILABLE, f"{url} returned HTTP {status}"
            )
        records = parse_idx(response.text)
        self._idx_memo[url] = records
        return records

    def fetch_record_values(
        self,
        init_time: datetime,
        member: str,
        fhour: int,
        nodes: Sequence[Tuple[int, int]],
        *,
        field_name: str = FIELD_TMAX,
    ) -> Dict[str, Any]:
        """Kelvin values at ``nodes`` from one member/forecast-hour record.

        Served from the record cache when every requested node is present.
        On a miss the ``.idx`` is read, exactly one HTTP ``Range`` request pulls
        the record, and the decoded values for every requested node are cached
        together -- so one download serves all four cities.
        """
        nodes = tuple(tuple(int(v) for v in node) for node in nodes)
        cached = self._load_record_cache(init_time, member, fhour, field_name, nodes)
        if cached is not None:
            cached = dict(cached)
            cached["from_cache"] = True
            return cached

        idx = self.fetch_idx(init_time, member, fhour)
        entry = select_idx_record(idx, field_name, LEVEL_2M)
        url = self.object_url(init_time, member, fhour)
        response = self._get(url, headers={"Range": entry.range_header})
        status = getattr(response, "status_code", None)
        if status not in (200, 206):
            raise EnsembleUnavailable(
                REASON_S3_UNAVAILABLE,
                f"range request {entry.range_header} on {url} returned HTTP {status}",
            )
        raw = response.content

        # Decoded in full on purpose. Truncating to the deepest city row is a
        # ~3x saving on a 0.135 s step that sits behind a ~0.9 s download, and
        # it costs the only invariant that catches a descriptor phase error.
        field = decode_grib2_record(raw)

        if field_name == FIELD_TMAX:
            # The record must be the maximum over exactly the interval the step
            # layout promises. Both halves matter: a wrong interval silently
            # maximises over the wrong hours, and a wrong statistical process
            # (mean, minimum) silently answers a different question.
            interval = (field.interval_start, field.interval_end)
            expected = tmax_interval_for(fhour)
            if interval != expected:
                raise EnsembleUnavailable(
                    REASON_DECODE_FAILED,
                    f"{member} f{fhour:03d} {field_name} declares interval "
                    f"{interval}, expected {expected} from the published step layout",
                )
            if field.statistical_process != 2:
                raise EnsembleUnavailable(
                    REASON_DECODE_FAILED,
                    f"{member} f{fhour:03d} {field_name} declares statistical "
                    f"process {field.statistical_process}, expected 2 (maximum)",
                )
        elif field.interval_start is not None:
            raise EnsembleUnavailable(
                REASON_DECODE_FAILED,
                f"{member} f{fhour:03d} {field_name} is statistically processed "
                f"over {field.interval_start}-{field.interval_end} h; an "
                f"instantaneous record was requested",
            )

        values: Dict[str, float] = {}
        for node in nodes:
            index = field.grid.node_index(node[0], node[1])
            values[self._node_key(node)] = round(field.at(index), 4)

        blob = {
            "_meta": {
                "s3_key": self.object_key(init_time, member, fhour),
                "range": entry.range_header,
                "bytes": len(raw),
                "member": member,
                "fhour": fhour,
                "field": field_name,
                "level": LEVEL_2M,
                "interval_start": field.interval_start,
                "interval_end": field.interval_end,
                "statistical_process": field.statistical_process,
                "pdt_number": field.pdt_number,
                "drs_template": field.drs_template,
                "perturbation_number": field.perturbation_number,
                "derived_forecast_type": field.derived_forecast_type,
                "ensemble_size": field.ensemble_size,
                "reference_time": field.reference_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "grid": {
                    "ni": field.grid.ni,
                    "nj": field.grid.nj,
                    "lat1": field.grid.lat1,
                    "lon1": field.grid.lon1,
                    "dlat": field.grid.dlat,
                    "dlon": field.grid.dlon,
                    "scan_mode": field.grid.scan_mode,
                },
                "fetched_at": _utc_now_iso(),
            },
            "nodes_k": values,
        }
        self._save_record_cache(
            self._record_cache_path(init_time, member, fhour, field_name), blob
        )
        blob = dict(blob)
        blob["from_cache"] = False
        return blob

    # -- cycle selection --------------------------------------------------
    def default_init_time(self, reference: Optional[datetime] = None) -> datetime:
        """The most recent model cycle that should be fully published.

        GEFS 00Z ``pgrb2sp25`` files begin landing ~03:47Z and continue for
        hours; :attr:`publish_delay_hours` is the margin before a cycle is
        assumed complete. A cycle that is nonetheless incomplete surfaces as
        :data:`REASON_CYCLE_NOT_PUBLISHED` on the first missing object -- it is
        never quietly swapped for an older one.
        """
        now = _require_aware_utc(reference or datetime.now(timezone.utc), "reference")
        candidate = now.replace(hour=self.cycle_hour, minute=0, second=0, microsecond=0)
        if now - candidate < timedelta(hours=self.publish_delay_hours):
            candidate -= timedelta(days=1)
        return candidate

    # -- the main path ----------------------------------------------------
    def fetch(
        self,
        city: str,
        target_date: Any,
        init_time: Optional[datetime] = None,
        *,
        use_cache: bool = True,
    ) -> EnsembleForecast:
        """Daily-high distribution for ``city`` on its local ``target_date``.

        Raises :class:`EnsembleUnavailable` on any failure -- including fewer
        than :attr:`min_members` decoded members. It never falls back to the
        degraded path on its own; see :meth:`get_forecast_or_abort`.
        """
        spec = get_city(city)
        target = _coerce_date(target_date)
        init = _require_aware_utc(init_time or self.default_init_time(), "init_time")

        day_start, day_end = local_day_bounds_utc(target, spec.timezone)
        start_lead = int(round((day_start - init).total_seconds() / 3600.0))
        end_lead = int(round((day_end - init).total_seconds() / 3600.0))
        windows = tmax_windows(start_lead, end_lead)

        if use_cache:
            cached = self._load_forecast_cache(spec, target, init)
            if cached is not None:
                return cached

        nearest = spec_nearest_node(spec)
        corners = spec_corner_nodes(spec)
        nodes = tuple(dict.fromkeys((nearest,) + corners))

        if not self.offline and self._session is None and requests is not None:
            _ = self.session  # create it once here, not racily inside the pool

        results: Dict[str, Dict[int, Dict[str, Any]]] = {}
        failures: Dict[str, str] = {}

        def work(job: Tuple[str, int]) -> Tuple[str, int, Any]:
            member, fhour = job
            try:
                return (
                    member,
                    fhour,
                    self.fetch_record_values(init, member, fhour, nodes),
                )
            except EnsembleUnavailable as exc:
                return member, fhour, exc

        jobs = [(m, w.fhour) for m in self.members for w in windows]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for member, fhour, outcome in pool.map(work, jobs):
                if isinstance(outcome, EnsembleUnavailable):
                    failures.setdefault(
                        member, f"f{fhour:03d}: {outcome.reason_code}: {outcome.detail}"
                    )
                    continue
                results.setdefault(member, {})[fhour] = outcome

        wanted = {w.fhour for w in windows}
        members_f: List[float] = []
        member_names: List[str] = []
        bilinear_f: List[float] = []
        s3_keys: List[str] = []
        ranges: List[str] = []
        weights = spec_bilinear_weights(spec)

        for member in self.members:
            per_hour = results.get(member) or {}
            if set(per_hour) != wanted:
                failures.setdefault(
                    member,
                    f"only {sorted(per_hour)} of {sorted(wanted)} forecast hours decoded",
                )
                continue
            nearest_k = []
            bilinear_k = []
            for fhour in sorted(wanted):
                blob = per_hour[fhour]
                node_values = blob["nodes_k"]
                nearest_k.append(float(node_values[self._node_key(nearest)]))
                bilinear_k.append(
                    sum(
                        weight * float(node_values[self._node_key(node)])
                        for node, weight in zip(corners, weights)
                    )
                )
                s3_keys.append(blob["_meta"]["s3_key"])
                ranges.append(blob["_meta"]["range"])
            members_f.append(kelvin_to_fahrenheit(max(nearest_k)))
            bilinear_f.append(kelvin_to_fahrenheit(max(bilinear_k)))
            member_names.append(member)

        if len(members_f) < self.min_members:
            raise EnsembleUnavailable(
                REASON_INSUFFICIENT_MEMBERS,
                f"{len(members_f)} of {len(self.members)} members decoded for "
                f"{spec.city} {target} from {init:%Y%m%d}T{init:%H}Z, floor is "
                f"{self.min_members} (first failures: "
                f"{dict(list(failures.items())[:3])})",
            )

        node_lat, node_lon = GEFS_GRID.node_lat_lon(*nearest)
        covered = coverage_of(windows)
        forecast = EnsembleForecast(
            city=spec.city,
            station=spec.station,
            target_date=target,
            init_time=init,
            lead_hours=start_lead,
            members_f=tuple(round(v, 4) for v in members_f),
            source=SOURCE_GEFS,
            provenance={
                "product": "gefs pgrb2sp25",
                "field": FIELD_TMAX,
                "level": LEVEL_2M,
                "bucket": GEFS_BUCKET_URL,
                "members_used": member_names,
                "members_requested": list(self.members),
                "members_failed": failures,
                "s3_keys": sorted(set(s3_keys)),
                "byte_ranges": sorted(set(ranges)),
                "forecast_hours": sorted(wanted),
                "station": {
                    "id": spec.station,
                    "name": spec.station_name,
                    "latitude": spec.latitude,
                    "longitude": spec.longitude,
                    "timezone": spec.timezone,
                },
                "grid_node": {
                    "j": nearest[0],
                    "i": nearest[1],
                    "latitude": node_lat,
                    "longitude": node_lon,
                    "distance_km": round(
                        haversine_km(spec.latitude, spec.longitude, node_lat, node_lon),
                        3,
                    ),
                    "selection": "nearest 0.25 deg node, no interpolation",
                },
                "coverage": {
                    "local_day_start_utc": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "local_day_end_utc": day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "requested_lead_hours": [start_lead, end_lead],
                    "covered_lead_hours": [covered[0], covered[1]],
                    "over_coverage_hours": [
                        start_lead - covered[0],
                        covered[1] - end_lead,
                    ],
                    "note": (
                        "TMAX intervals tile on 6-hour boundaries, so the covered "
                        "window can spill past the local day at both ends; the "
                        "spill is night-time and is a documented Phase-2 "
                        "calibration term"
                    ),
                },
                "diagnostic_bilinear_f": [round(v, 4) for v in bilinear_f],
                "diagnostic_bilinear_note": (
                    "bilinear interpolation to the station coordinates, carried "
                    "for calibration comparison only -- members_f uses the "
                    "nearest node"
                ),
                "fetched_at": _utc_now_iso(),
            },
        )
        self._save_forecast_cache(forecast)
        return forecast

    # -- forecast cache ---------------------------------------------------
    def _forecast_cache_path(
        self, spec: CitySpec, target: _date, init: datetime
    ) -> str:
        return os.path.join(
            self.forecast_cache_dir,
            f"{spec.city}_{target.isoformat()}_{init.strftime('%Y%m%d%H')}Z.json",
        )

    def _load_forecast_cache(
        self, spec: CitySpec, target: _date, init: datetime
    ) -> Optional[EnsembleForecast]:
        path = self._forecast_cache_path(spec, target, init)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                blob = json.load(handle)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.debug("ensemble forecast cache %s unreadable (%s)", path, exc)
            return None
        try:
            forecast = EnsembleForecast.from_dict(blob)
        except Exception as exc:
            logger.debug("ensemble forecast cache %s has a bad shape (%s)", path, exc)
            return None
        if forecast.member_count < self.min_members:
            # A cached forecast that no longer clears the floor is not an answer.
            return None
        return forecast

    def _save_forecast_cache(self, forecast: EnsembleForecast) -> None:
        spec = get_city(forecast.city)
        path = self._forecast_cache_path(spec, forecast.target_date, forecast.init_time)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(forecast.to_dict(), handle, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug("could not write ensemble forecast cache %s: %s", path, exc)

    # -- degradation (FR-2.1) ---------------------------------------------
    def fetch_degraded(
        self,
        city: str,
        target_date: Any,
        init_time: Optional[datetime] = None,
        *,
        member_count: int = 31,
    ) -> EnsembleForecast:
        """NWS point forecast widened by an injected historical error sigma.

        The FR-2.1 fallback, and deliberately awkward to reach: it needs
        :attr:`sigma_provider`, because a spread invented here would be a
        fabricated distribution wearing an ensemble's clothes. The returned
        object is stamped :data:`SOURCE_NWS_DEGRADED` and its provenance says so
        in words as well as in the tag.

        The pseudo-members are fixed Gaussian quantiles, not random draws, so a
        re-run is byte-identical.
        """
        spec = get_city(city)
        target = _coerce_date(target_date)
        init = _require_aware_utc(init_time or datetime.now(timezone.utc), "init_time")

        if self.sigma_provider is None:
            raise EnsembleUnavailable(
                REASON_DEGRADED_UNCALIBRATED,
                f"degraded forecast for {spec.city} needs a historical error "
                f"sigma; no sigma_provider is wired in",
            )
        day_start, _day_end = local_day_bounds_utc(target, spec.timezone)
        lead_hours = int(round((day_start - init).total_seconds() / 3600.0))
        try:
            sigma = float(self.sigma_provider(spec.city, lead_hours))
        except EnsembleUnavailable:
            raise
        except Exception as exc:
            raise EnsembleUnavailable(
                REASON_DEGRADED_UNCALIBRATED,
                f"sigma_provider failed for {spec.city} at lead {lead_hours} h: {exc}",
            ) from exc
        if not math.isfinite(sigma) or sigma <= 0.0:
            raise EnsembleUnavailable(
                REASON_DEGRADED_UNCALIBRATED,
                f"sigma_provider returned {sigma!r} for {spec.city}; a degraded "
                f"forecast needs a positive finite error sigma",
            )

        point_f, point_meta = self._nws_point_high(spec, target)
        normal = NormalDist()
        members = tuple(
            round(point_f + sigma * normal.inv_cdf((k + 0.5) / member_count), 4)
            for k in range(member_count)
        )
        return EnsembleForecast(
            city=spec.city,
            station=spec.station,
            target_date=target,
            init_time=init,
            lead_hours=lead_hours,
            members_f=members,
            source=SOURCE_NWS_DEGRADED,
            provenance={
                "degraded": True,
                "degraded_note": (
                    "NOT a GEFS ensemble: an NWS point forecast spread by a "
                    "historical error sigma into deterministic Gaussian "
                    "quantiles. Any report consuming this must label it degraded."
                ),
                "point_forecast_f": point_f,
                "sigma_f": sigma,
                "member_count": member_count,
                "station": {
                    "id": spec.station,
                    "latitude": spec.latitude,
                    "longitude": spec.longitude,
                    "timezone": spec.timezone,
                },
                "nws": point_meta,
                "fetched_at": _utc_now_iso(),
            },
        )

    def _nws_point_high(
        self, spec: CitySpec, target: _date
    ) -> Tuple[float, Dict[str, Any]]:
        """The NWS daytime-high point forecast (F) for a station's local day.

        NWS daytime periods run roughly 06:00-18:00 local, so this is the
        daytime high, not strictly the calendar-day maximum -- one more reason
        the degraded path is a fallback and not a peer of the ensemble.
        """
        points_url = NWS_POINTS_URL.format(lat=spec.latitude, lon=spec.longitude)
        response = self._get(points_url)
        if getattr(response, "status_code", None) != 200:
            raise EnsembleUnavailable(
                REASON_DEGRADED_NWS_UNAVAILABLE,
                f"{points_url} returned HTTP {getattr(response, 'status_code', None)}",
            )
        try:
            forecast_url = response.json()["properties"]["forecast"]
        except Exception as exc:
            raise EnsembleUnavailable(
                REASON_DEGRADED_NWS_UNAVAILABLE,
                f"{points_url} carried no forecast link: {exc}",
            ) from exc

        response = self._get(forecast_url)
        if getattr(response, "status_code", None) != 200:
            raise EnsembleUnavailable(
                REASON_DEGRADED_NWS_UNAVAILABLE,
                f"{forecast_url} returned HTTP {getattr(response, 'status_code', None)}",
            )
        try:
            periods = response.json()["properties"]["periods"]
        except Exception as exc:
            raise EnsembleUnavailable(
                REASON_DEGRADED_NWS_UNAVAILABLE,
                f"{forecast_url} carried no periods: {exc}",
            ) from exc

        tz = ZoneInfo(spec.timezone)
        for period in periods:
            if not period.get("isDaytime"):
                continue
            start = datetime.fromisoformat(str(period["startTime"]))
            if start.tzinfo is None:
                continue
            if start.astimezone(tz).date() != target:
                continue
            unit = str(period.get("temperatureUnit") or "F").upper()
            value = float(period["temperature"])
            if unit == "C":
                value = value * 9.0 / 5.0 + 32.0
            elif unit != "F":
                raise EnsembleUnavailable(
                    REASON_DEGRADED_NWS_UNAVAILABLE,
                    f"unrecognised NWS temperature unit {unit!r}",
                )
            return value, {
                "points_url": points_url,
                "forecast_url": forecast_url,
                "period_name": period.get("name"),
                "period_start": period.get("startTime"),
                "temperature_unit": unit,
                "note": "NWS daytime period high, not a calendar-day maximum",
            }
        raise EnsembleUnavailable(
            REASON_DEGRADED_NWS_UNAVAILABLE,
            f"no NWS daytime period covers {target} for {spec.station}",
        )

    # -- the contract Phase-3 strategies consume --------------------------
    def get_forecast_or_abort(
        self,
        city: str,
        target_date: Any,
        init_time: Optional[datetime] = None,
        *,
        allow_degraded: Optional[bool] = None,
    ) -> EnsembleForecast:
        """A forecast, or an abort -- never a default.

        On success returns an :class:`EnsembleForecast` whose ``source`` names
        the authority that produced it. On failure emits **exactly one INFO
        line** carrying a reason code and re-raises
        :class:`EnsembleUnavailable`, so the caller's signal generation stops
        (FR-2.1, project rule ``abort-on-missing-critical-input``).

        Degradation, when enabled, is announced on its own INFO line before the
        degraded object is returned; it is never silent.
        """
        degrade = (
            self.allow_degraded if allow_degraded is None else bool(allow_degraded)
        )
        primary: Optional[EnsembleUnavailable] = None
        try:
            return self.fetch(city, target_date, init_time)
        except EnsembleUnavailable as exc:
            primary = exc
        except Exception as exc:  # unexpected: still an abort, never a default
            primary = EnsembleUnavailable(
                REASON_DECODE_FAILED, f"unexpected failure: {exc!r}"
            )

        if degrade:
            try:
                degraded = self.fetch_degraded(city, target_date, init_time)
            except EnsembleUnavailable as exc:
                logger.info(
                    "ENSEMBLE_ABORT city=%s target_date=%s reason=%s "
                    "primary_reason=%s detail=%s",
                    city,
                    target_date,
                    exc.reason_code,
                    primary.reason_code,
                    exc.detail,
                )
                raise exc from primary
            logger.info(
                "ENSEMBLE_DEGRADED city=%s target_date=%s source=%s "
                "primary_reason=%s point_f=%.1f sigma_f=%.2f detail=%s",
                city,
                target_date,
                degraded.source,
                primary.reason_code,
                degraded.provenance.get("point_forecast_f", float("nan")),
                degraded.provenance.get("sigma_f", float("nan")),
                primary.detail,
            )
            return degraded

        logger.info(
            "ENSEMBLE_ABORT city=%s target_date=%s reason=%s detail=%s",
            city,
            target_date,
            primary.reason_code,
            primary.detail,
        )
        raise primary


# ---------------------------------------------------------------------------
# The GEFS 0.25 degree grid, and city node helpers
# ---------------------------------------------------------------------------
#: The grid GEFS ``pgrb2sp25`` publishes, as declared by section 3 of every
#: record (verified live 2026-07-26). Records are still parsed for their own
#: grid; this constant exists so node indices can be resolved without a fetch,
#: and :func:`decode_grib2_record` cross-checks against it implicitly by
#: refusing any record whose section 3 disagrees with its section 5 point count.
GEFS_GRID = LatLonGrid(
    ni=1440,
    nj=721,
    lat1=90.0,
    lon1=0.0,
    lat2=-90.0,
    lon2=359.75,
    dlat=0.25,
    dlon=0.25,
    scan_mode=0,
)


def spec_nearest_node(spec: CitySpec) -> Tuple[int, int]:
    """The 0.25 deg node a city's members are read from."""
    return GEFS_GRID.nearest_node(spec.latitude, spec.longitude)


def spec_corner_nodes(spec: CitySpec) -> Tuple[Tuple[int, int], ...]:
    """The four nodes bracketing a city, for the bilinear diagnostic."""
    return GEFS_GRID.surrounding_nodes(spec.latitude, spec.longitude)


def spec_bilinear_weights(spec: CitySpec) -> Tuple[float, ...]:
    """Bilinear weights matching :func:`spec_corner_nodes`."""
    return GEFS_GRID.bilinear_weights(spec.latitude, spec.longitude)


def city_nodes() -> Dict[str, Dict[str, Any]]:
    """Every tracked city's station, nearest node and the distance between them."""
    out: Dict[str, Dict[str, Any]] = {}
    for key, spec in CITIES.items():
        node = spec_nearest_node(spec)
        lat, lon = GEFS_GRID.node_lat_lon(*node)
        out[key] = {
            "station": spec.station,
            "station_latitude": spec.latitude,
            "station_longitude": spec.longitude,
            "node_j": node[0],
            "node_i": node[1],
            "node_latitude": lat,
            "node_longitude": lon,
            "distance_km": round(
                haversine_km(spec.latitude, spec.longitude, lat, lon), 3
            ),
        }
    return out


_REGISTRY_PROBLEMS = verify_station_registry()
if _REGISTRY_PROBLEMS:  # pragma: no cover - guards a registry drift, not a path
    logger.error(
        "ensemble city registry disagrees with the CLI settlement registry: %s",
        "; ".join(_REGISTRY_PROBLEMS),
    )


__all__ = [
    "CITIES",
    "CitySpec",
    "CONTROL_MEMBER",
    "DEFAULT_MEMBERS",
    "DEFAULT_MIN_MEMBERS",
    "EnsembleForecast",
    "EnsembleProvider",
    "EnsembleUnavailable",
    "FIELD_TMAX",
    "FIELD_TMP",
    "GEFS_BUCKET_URL",
    "GEFS_GRID",
    "Grib2Field",
    "IdxRecord",
    "LEVEL_2M",
    "LatLonGrid",
    "MEAN_PRODUCT",
    "PERTURBED_MEMBERS",
    "REASON_CODES",
    "REASON_CYCLE_NOT_PUBLISHED",
    "REASON_DECODE_FAILED",
    "REASON_DEGRADED_DISABLED",
    "REASON_DEGRADED_NWS_UNAVAILABLE",
    "REASON_DEGRADED_UNCALIBRATED",
    "REASON_INSUFFICIENT_MEMBERS",
    "REASON_LEAD_OUT_OF_RANGE",
    "REASON_OFFLINE",
    "REASON_RECORD_NOT_FOUND",
    "REASON_S3_UNAVAILABLE",
    "REASON_UNKNOWN_CITY",
    "SOURCE_GEFS",
    "SOURCE_NWS_DEGRADED",
    "SPREAD_PRODUCT",
    "TmaxWindow",
    "city_nodes",
    "coverage_of",
    "decode_grib2_record",
    "fahrenheit_to_kelvin",
    "get_city",
    "haversine_km",
    "kelvin_to_fahrenheit",
    "local_day_bounds_utc",
    "parse_idx",
    "select_idx_record",
    "spec_bilinear_weights",
    "spec_corner_nodes",
    "spec_nearest_node",
    "tmax_interval_for",
    "tmax_windows",
    "verify_station_registry",
]
