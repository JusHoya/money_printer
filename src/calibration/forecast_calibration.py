"""Forecast-error calibration against CLI settlement truth (PRD FR-2.2).

Pairs a normalized forecast series with the CLI daily-high archive and reports,
per city, the forecast **bias** and **error sigma** by lead time and by
month/season -- the numbers the FR-2.3 probability engine turns into
P(bracket).

THE ERROR CONVENTION (stated once, used everywhere)
---------------------------------------------------
::

    error_f = forecast_high_f - truth_high_f

A **positive** error means the forecast was **too warm**. ``bias_f`` is the mean
of that quantity, so a positive ``bias_f`` is a warm bias and the corrected
forecast is ``forecast_high_f - bias_f``. This sign is repeated in the emitted
JSON (``error_convention``), in the report, and in the tests, because a silent
sign inversion here does not degrade the trading edge -- it reverses it, and
every downstream number would still look plausible.

SOURCE-AGNOSTIC BY CONSTRUCTION
-------------------------------
The only forecast input is a CSV in the
:data:`~src.data.mos_guidance_provider.FORECAST_FIELDS` schema::

    city,station,target_date,init_time_utc,lead_hours,source,
    forecast_high_f,spread_f,provenance

Nothing in this module knows what produced those rows. A GEFS ensemble writing
the same columns (with ``spread_f`` populated) calibrates through the same code
path with no change. ``spread_f`` is carried through and reported as *coverage*
(how many paired rows have one) rather than being defaulted: an empty
``spread_f`` means "this source publishes no spread", which is a different fact
from a spread of ``0.0`` and is never turned into one.

A source may still need to carry a fact about *itself* that no CSV column can
express -- most importantly, **which statistic** ``forecast_high_f`` actually
is. That is what :data:`SOURCE_ANNOTATORS` provides: a source-keyed, lazily
imported hook that appends deterministic top-level keys to the payload before it
is sealed. It is deliberately a registry rather than an ``if source ==`` branch,
and it lives here rather than in the producing script so that **every** path
that builds a source emits the same artifact -- see the next section.

ONE SOURCE, ONE ARTIFACT, WHATEVER BUILDS IT
--------------------------------------------
``data/calibration/<CITY>_gefs_v1.json`` was originally produced by
``scripts/backfill_ensemble_history.py``, which appended its ``statistic`` block
*after* calling :func:`build_all`. The documented generic rebuild --
``scripts/build_calibration.py --source gefs`` -- therefore emitted the same
numbers with that block **stripped**, and a different ``content_hash``. The
stripped key is not decoration: it carries the warning that separates the
backfill statistic ``max_t(geavg)`` from the live-path statistic
``mean_m(max_t member)``. A "reproduction" that silently deletes a guard is
worse than one that fails.

Registering the annotation here closes that: both producers call
:func:`build_all`, :func:`build_all` applies the annotation, and the two paths
are byte-identical by construction rather than by convention. The annotation is
required to be a pure function of the payload (no clock, no host, no path), so
determinism is unaffected -- ``tests/test_forecast_calibration.py`` pins both
properties.

WHAT "day_of" MEANS -- AND WHAT IT MAY NOT BE APPLIED TO
--------------------------------------------------------
``day_of`` is the lead bucket ``[-24, 12)`` hours from model runtime to the
**start of the target local day**. In both committed sources every day-of row
comes from a 00Z cycle issued the evening before that local day, at observed
leads of **4-8 h to the start of the day**, i.e. roughly 10-16 h ahead of a
typical afternoon maximum. The bucket's lower edge admits a genuinely intraday
run (negative lead), but no such row exists in either committed sample, and
``lead_hours_observed`` in every emitted bucket is what proves it.

Consequently ``day_of``'s ``bias_f``/``sigma_f`` describe an
**evening-before forecast, not an intraday update**. PRD FR-3.1(b)'s lock-in
strategy needs a *midday re-forecast*; these numbers were not measured on one.
A shorter-lead forecast is normally more accurate, so reusing them there is
conservative in direction -- it widens the predictive distribution and shrinks
the apparent edge -- but the size of the gap is unmeasured, and "conservative"
is not "correct": any strategy that profits from a *wider* distribution (selling
tails, wide brackets) is made to look better, not worse, by the substitution.
Phase 3 must calibrate its own bucket at the lead it actually trades before
pricing on it.

PAIRING IS AN INNER JOIN, AND THE DROPS ARE COUNTED
---------------------------------------------------
A forecast row joins truth on ``(station, target_date)``. A forecast with no
truth row -- or whose truth row carries no maximum -- is **dropped and
counted** (``dropped_no_truth`` / ``dropped_null_truth``), never imputed,
interpolated, or carried forward. Truth is read from
``data/weather_truth/cli_daily_high_<STATION>.csv``, which this module treats as
read-only.

DETERMINISM (Phase 2 exit criterion 2: "byte-identical on re-run")
------------------------------------------------------------------
The emitted calibration file is a pure function of its inputs. Concretely:

* **No wall-clock timestamp, no hostname, no run id, no input file path** is
  written into the artifact. Run metadata goes to a sidecar
  ``<name>.runlog.json``, which is explicitly *not* the artifact under test.
* Input provenance is recorded as a **content** hash of the normalized rows,
  not of the raw file bytes, so a CRLF checkout of the CSVs cannot change the
  artifact. (The artifact itself is still written with ``\\n`` endings and
  should be pinned ``eol=lf`` in ``.gitattributes`` if it is ever hash-gated in
  CI -- see the project's ``hash-gated-fixtures-need-eol-lf`` lesson.)
* Every float is rounded once, at payload-build time, via :func:`_r`.
* Quantiles use :func:`percentile`, a local linear-interpolation implementation
  rather than a library call, so a numpy/statistics version bump cannot move a
  published number.
* Rows are sorted before aggregation; JSON is dumped with ``sort_keys=True``
  and a fixed separator; the file is written in binary with ``\\n``.
* ``content_hash`` is sha256 over the canonicalized payload with the
  ``content_hash`` field removed, so it can be recomputed and checked.

INSUFFICIENT BUCKETS ARE REPORTED, NOT MERGED
---------------------------------------------
A lead/month/season bucket with fewer than :data:`MIN_BUCKET_N` paired days is
emitted as ``{"n": k, "sufficient": false}`` with no statistics. Merging it into
a neighbour to reach a quorum would manufacture a number nobody measured.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
WEATHER_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "weather_truth")
FORECAST_ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "forecast_archive")
CALIBRATION_DIR = os.path.join(REPO_ROOT, "data", "calibration")

#: Bumped whenever the emitted JSON's shape changes. Consumers should refuse a
#: schema_version they do not know rather than reading fields positionally.
SCHEMA_VERSION = 1

ERROR_CONVENTION = (
    "error_f = forecast_high_f - truth_high_f (positive = forecast too warm)"
)

#: Minimum paired days for a bucket to carry statistics. Below this the bucket
#: is reported with its ``n`` and ``sufficient: false`` and nothing else.
MIN_BUCKET_N = 20

#: Half-open ``[lo, hi)`` lead-hour buckets, in report order.
#:
#: ``day_of`` deliberately spans ``[-24, 12)``: a run initialised *during* the
#: target local day (negative lead) and one initialised in the 12 hours before
#: it starts are both "the forecast you have on the morning of the day you are
#: trading". Because that band is wide, every bucket also publishes the
#: ``lead_hours`` actually observed inside it, so nobody has to assume.
LEAD_BUCKETS: Tuple[Tuple[str, int, int], ...] = (
    ("day_of", -24, 12),
    ("lead_12_36", 12, 36),
    ("lead_36_60", 36, 60),
    ("lead_60_84", 60, 84),
    ("lead_84_108", 84, 108),
    ("lead_108_132", 108, 132),
    ("lead_132_156", 132, 156),
    ("lead_156_180", 156, 180),
    ("lead_180_plus", 180, 10**6),
)

DAY_OF_BUCKET = "day_of"

#: Phase 2 exit criterion 3's sanity bound on day-of sigma.
SIGMA_SANITY_BOUND_F = 4.0

_SEASONS = {
    12: "DJF",
    1: "DJF",
    2: "DJF",
    3: "MAM",
    4: "MAM",
    5: "MAM",
    6: "JJA",
    7: "JJA",
    8: "JJA",
    9: "SON",
    10: "SON",
    11: "SON",
}


#: Sources that must carry extra, source-specific top-level keys in their
#: calibration payload. Maps ``source label -> "module:function"``.
#:
#: The target is imported **lazily**, only when that source is actually built,
#: so calibrating ``gfs_mex`` never pulls in the ensemble stack. The function
#: takes the payload built so far and returns a mapping of new top-level keys.
#: It must be a pure function of that payload: no clock, no hostname, no run id,
#: no filesystem path. :func:`annotate` enforces the additive part (it refuses
#: to overwrite an existing key) and ``tests/test_forecast_calibration.py``
#: enforces the rest.
SOURCE_ANNOTATORS: Dict[str, str] = {
    "gefs": "src.calibration.gefs_series:calibration_annotation",
}

#: Keys an annotator may never write, because they are the artifact's identity
#: or its seal.
_PROTECTED_KEYS = frozenset(
    {"schema_version", "city", "station", "source", "version", "content_hash"}
)


class CalibrationError(RuntimeError):
    """A calibration could not be built from the inputs as given."""


# ---------------------------------------------------------------------------
# Deterministic numerics
# ---------------------------------------------------------------------------
def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    """The single rounding point for every published float."""
    if x is None:
        return None
    return round(float(x), nd)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy's ``method="linear"``), locally.

    Implemented here rather than imported so a library version bump cannot
    silently move a number that has already been published in a calibration
    file.
    """
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    n = len(xs)
    if n == 1:
        return xs[0]
    pos = (float(q) / 100.0) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return math.fsum(float(v) for v in values) / len(values)


def stdev_sample(values: Sequence[float]) -> Optional[float]:
    """Sample standard deviation (``ddof=1``). ``None`` for n < 2."""
    n = len(values)
    if n < 2:
        return None
    mu = mean(values)
    ss = math.fsum((float(v) - mu) ** 2 for v in values)
    return math.sqrt(ss / (n - 1))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PairedDay:
    """One forecast joined to its settlement truth."""

    city: str
    station: str
    target_date: str
    init_time_utc: str
    lead_hours: int
    source: str
    forecast_high_f: float
    truth_high_f: float
    spread_f: Optional[float]

    @property
    def error_f(self) -> float:
        """See :data:`ERROR_CONVENTION`. Positive = forecast too warm."""
        return self.forecast_high_f - self.truth_high_f


def truth_csv_path(station: str, truth_dir: Optional[str] = None) -> str:
    return os.path.join(
        truth_dir or WEATHER_TRUTH_DIR, f"cli_daily_high_{station.upper()}.csv"
    )


def load_truth(
    station: str, truth_dir: Optional[str] = None
) -> Dict[str, Optional[int]]:
    """``{date: high}`` from the read-only CLI truth archive.

    A row whose ``high`` is blank maps to ``None`` -- present in the archive but
    carrying no maximum. That is a different fact from an absent date and the
    pairing counts the two separately.
    """
    path = truth_csv_path(station, truth_dir)
    if not os.path.exists(path):
        raise CalibrationError(f"no CLI truth file for {station} at {path}")
    out: Dict[str, Optional[int]] = {}
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("station", "")).upper() != station.upper():
                raise CalibrationError(
                    f"{path} contains station {row.get('station')!r}; refusing a "
                    f"cross-station truth substitution"
                )
            raw = (row.get("high") or "").strip()
            out[row["date"]] = int(raw) if raw else None
    return out


def load_forecast_series(path: str) -> List[Dict[str, Any]]:
    """Read the normalized forecast CSV, validating its header.

    Rows are returned as-read (strings coerced) and sorted; malformed rows raise
    rather than being skipped, because a partially-parsed series silently
    shrinks the sample a criterion is measured on.
    """
    if not os.path.exists(path):
        raise CalibrationError(f"no forecast series at {path}")
    required = {
        "city",
        "station",
        "target_date",
        "init_time_utc",
        "lead_hours",
        "source",
        "forecast_high_f",
        "spread_f",
        "provenance",
    }
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise CalibrationError(
                f"{path} is missing forecast-series columns {sorted(missing)}"
            )
        for i, row in enumerate(reader, start=2):
            try:
                spread = (row.get("spread_f") or "").strip()
                rows.append(
                    {
                        "city": row["city"].strip(),
                        "station": row["station"].strip().upper(),
                        "target_date": row["target_date"].strip(),
                        "init_time_utc": row["init_time_utc"].strip(),
                        "lead_hours": int(row["lead_hours"]),
                        "source": row["source"].strip(),
                        "forecast_high_f": float(row["forecast_high_f"]),
                        # blank stays None; it is NOT 0.0
                        "spread_f": float(spread) if spread else None,
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CalibrationError(f"{path} line {i} is malformed: {exc}") from exc
    rows.sort(key=lambda r: (r["city"], r["target_date"], r["init_time_utc"]))
    return rows


def content_fingerprint(
    rows: Iterable[Mapping[str, Any]], fields: Sequence[str]
) -> str:
    """sha256 over the *normalized content* of rows, not the file's raw bytes.

    Hashing content rather than bytes is what makes the artifact immune to a
    CRLF-vs-LF checkout of its inputs.
    """
    h = hashlib.sha256()
    for row in rows:
        h.update(
            "\x1f".join(
                "" if row.get(f) is None else str(row.get(f)) for f in fields
            ).encode("utf-8")
        )
        h.update(b"\x1e")
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
def pair_city(
    forecast_rows: Sequence[Mapping[str, Any]],
    station: str,
    truth: Mapping[str, Optional[int]],
) -> Tuple[List[PairedDay], Dict[str, int]]:
    """Inner-join one city's forecasts to truth. Returns ``(paired, drop_counts)``."""
    paired: List[PairedDay] = []
    drops = {"dropped_no_truth": 0, "dropped_null_truth": 0}
    for row in forecast_rows:
        if row["station"] != station.upper():
            continue
        if row["target_date"] not in truth:
            drops["dropped_no_truth"] += 1
            continue
        high = truth[row["target_date"]]
        if high is None:
            drops["dropped_null_truth"] += 1
            continue
        paired.append(
            PairedDay(
                city=row["city"],
                station=row["station"],
                target_date=row["target_date"],
                init_time_utc=row["init_time_utc"],
                lead_hours=int(row["lead_hours"]),
                source=row["source"],
                forecast_high_f=float(row["forecast_high_f"]),
                truth_high_f=float(high),
                spread_f=row.get("spread_f"),
            )
        )
    paired.sort(key=lambda p: (p.target_date, p.init_time_utc))
    return paired, drops


def bucket_for_lead(lead_hours: int) -> Optional[str]:
    """Name of the :data:`LEAD_BUCKETS` entry containing ``lead_hours``."""
    for name, lo, hi in LEAD_BUCKETS:
        if lo <= lead_hours < hi:
            return name
    return None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def summarize(
    days: Sequence[PairedDay], *, min_n: int = MIN_BUCKET_N
) -> Dict[str, Any]:
    """Error statistics for one bucket, or an explicit "insufficient" marker.

    Every number is derived from :attr:`PairedDay.error_f`, i.e. from
    :data:`ERROR_CONVENTION`.
    """
    n = len(days)
    if n < min_n:
        return {"n": n, "sufficient": False}
    errors = [d.error_f for d in days]
    leads = [d.lead_hours for d in days]
    spreads = [d.spread_f for d in days if d.spread_f is not None]
    return {
        "n": n,
        "sufficient": True,
        "bias_f": _r(mean(errors)),
        "sigma_f": _r(stdev_sample(errors)),
        "mae_f": _r(mean([abs(e) for e in errors])),
        "rmse_f": _r(math.sqrt(math.fsum(e * e for e in errors) / n)),
        "quantiles_f": {
            "p05": _r(percentile(errors, 5)),
            "p25": _r(percentile(errors, 25)),
            "p50": _r(percentile(errors, 50)),
            "p75": _r(percentile(errors, 75)),
            "p95": _r(percentile(errors, 95)),
        },
        "lead_hours_observed": {
            "min": min(leads),
            "median": _r(percentile(leads, 50)),
            "max": max(leads),
        },
        "distinct_target_dates": len({d.target_date for d in days}),
        "first_target_date": min(d.target_date for d in days),
        "last_target_date": max(d.target_date for d in days),
        "spread_f_coverage": {
            "rows_with_spread": len(spreads),
            "rows_without_spread": n - len(spreads),
            "mean_spread_f": _r(mean(spreads)) if spreads else None,
        },
    }


def _group(days: Sequence[PairedDay], keyfn) -> Dict[str, List[PairedDay]]:
    out: Dict[str, List[PairedDay]] = {}
    for d in days:
        k = keyfn(d)
        if k is None:
            continue
        out.setdefault(k, []).append(d)
    return out


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def build_city_calibration(
    *,
    city: str,
    station: str,
    timezone_name: str,
    source: str,
    version: int,
    paired: Sequence[PairedDay],
    drops: Mapping[str, int],
    forecast_rows_for_city: int,
    forecast_fingerprint: str,
    truth_fingerprint: str,
    min_bucket_n: int = MIN_BUCKET_N,
) -> Dict[str, Any]:
    """Assemble one city's calibration payload (no ``content_hash`` yet).

    Contains **no** timestamp, hostname, run id, or filesystem path -- see the
    determinism section of the module docstring. Everything that varies between
    runs lives in the sidecar runlog.
    """
    by_lead: Dict[str, Any] = {}
    grouped = _group(paired, lambda d: bucket_for_lead(d.lead_hours))
    for name, lo, hi in LEAD_BUCKETS:
        block = summarize(grouped.get(name, []), min_n=min_bucket_n)
        block["lead_hours_range"] = [lo, None if hi >= 10**6 else hi]
        by_lead[name] = block

    day_of_days = grouped.get(DAY_OF_BUCKET, [])
    by_month = {
        k: summarize(v, min_n=min_bucket_n)
        for k, v in _group(day_of_days, lambda d: d.target_date[:7]).items()
    }
    by_season = {
        k: summarize(v, min_n=min_bucket_n)
        for k, v in _group(
            day_of_days, lambda d: _SEASONS[int(d.target_date[5:7])]
        ).items()
    }

    out_of_range = sum(1 for d in paired if bucket_for_lead(d.lead_hours) is None)

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "city": city,
        "station": station,
        "timezone": timezone_name,
        "source": source,
        "version": int(version),
        "error_convention": ERROR_CONVENTION,
        "units": "degF",
        "min_bucket_n": int(min_bucket_n),
        "sigma_sanity_bound_f": SIGMA_SANITY_BOUND_F,
        "inputs": {
            "forecast_rows_for_city": int(forecast_rows_for_city),
            "paired_rows": len(paired),
            "dropped_no_truth": int(drops.get("dropped_no_truth", 0)),
            "dropped_null_truth": int(drops.get("dropped_null_truth", 0)),
            "dropped_lead_out_of_range": int(out_of_range),
            "forecast_content_sha256": forecast_fingerprint,
            "truth_content_sha256": truth_fingerprint,
        },
        "coverage": {
            "distinct_paired_target_dates": len({d.target_date for d in paired}),
            "first_target_date": min((d.target_date for d in paired), default=None),
            "last_target_date": max((d.target_date for d in paired), default=None),
            "day_of_paired_days": len(day_of_days),
            "day_of_distinct_target_dates": len({d.target_date for d in day_of_days}),
        },
        "day_of": by_lead[DAY_OF_BUCKET],
        "by_lead": by_lead,
        "by_month_day_of": by_month,
        "by_season_day_of": by_season,
        "notes": [
            "by_month_day_of and by_season_day_of are computed on the day_of "
            "lead bucket only; pooling all lead times into a month bucket would "
            "mix a 4-hour forecast with a 7-day one.",
            "A bucket with n < min_bucket_n is reported as "
            "{'n': k, 'sufficient': false} and carries no statistics. It is "
            "never merged into a neighbouring bucket to reach a quorum.",
            "spread_f is absent (null) when the source publishes no spread. "
            "That is not the same as a spread of 0.0 and is never encoded as one.",
        ],
    }
    return payload


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------
def source_annotation(source: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Extra top-level keys :data:`SOURCE_ANNOTATORS` requires for ``source``.

    ``{}`` for a source with no registered annotator -- the common case, and the
    reason ``gfs_mex`` artifacts are untouched by this mechanism.
    """
    target = SOURCE_ANNOTATORS.get(str(source))
    if not target:
        return {}
    module_name, sep, func_name = target.partition(":")
    if not sep or not func_name:
        raise CalibrationError(
            f"SOURCE_ANNOTATORS[{source!r}] = {target!r} is not 'module:function'"
        )
    try:
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
    except (ImportError, AttributeError) as exc:
        raise CalibrationError(
            f"source {source!r} declares annotator {target!r}, which could not be "
            f"loaded ({exc}). Refusing to emit an artifact missing its "
            f"source-specific block."
        ) from exc
    block = func(payload)
    if not isinstance(block, dict):
        raise CalibrationError(
            f"{target} returned {type(block).__name__}, expected a dict of "
            f"top-level keys"
        )
    return block


def annotate(payload: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Apply ``source``'s annotation to ``payload`` in place, additively.

    Additive is the whole contract: an annotator may add keys and may never
    change or remove one the calibrator computed. A collision raises rather than
    winning silently, because an annotator quietly overwriting ``day_of`` would
    be indistinguishable from a calibration.
    """
    for key, value in sorted(source_annotation(source, payload).items()):
        if key in _PROTECTED_KEYS or key in payload:
            raise CalibrationError(
                f"source {source!r} annotator tried to overwrite existing key "
                f"{key!r}; annotations are additive only"
            )
        payload[key] = value
    return payload


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """The one serialization used for both the file and the hash.

    ``sort_keys`` fixes key order, the explicit separators fix whitespace,
    ``ensure_ascii`` fixes encoding, and the trailing ``\\n`` is written
    literally so the bytes do not depend on the platform's text-mode newline
    translation.
    """
    return (
        json.dumps(
            payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), indent=1
        )
        + "\n"
    ).encode("utf-8")


def compute_content_hash(payload: Mapping[str, Any]) -> str:
    """sha256 over the canonicalized payload with ``content_hash`` removed."""
    stripped = {k: v for k, v in payload.items() if k != "content_hash"}
    return "sha256:" + hashlib.sha256(canonical_bytes(stripped)).hexdigest()


def finalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Attach ``content_hash`` to a payload (idempotent)."""
    payload = {k: v for k, v in payload.items() if k != "content_hash"}
    payload["content_hash"] = compute_content_hash(payload)
    return payload


def verify_content_hash(payload: Mapping[str, Any]) -> bool:
    stored = payload.get("content_hash")
    return bool(stored) and stored == compute_content_hash(payload)


def calibration_filename(city: str, source: str, version: int) -> str:
    return f"{city}_{source}_v{int(version)}.json"


def write_calibration(out_dir: str, payload: Mapping[str, Any]) -> str:
    """Write ``<CITY>_<SOURCE>_v<N>.json`` in binary, LF, canonical."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(
        out_dir,
        calibration_filename(payload["city"], payload["source"], payload["version"]),
    )
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:  # binary: no newline translation on Windows
        fh.write(canonical_bytes(payload))
    os.replace(tmp, path)
    return path


def write_runlog(
    out_dir: str, payload: Mapping[str, Any], extra: Mapping[str, Any]
) -> str:
    """Write the sidecar run metadata.

    This file carries the wall-clock timestamp and anything else that varies
    between runs, and is **explicitly not the artifact under test** for the
    FR-2.2 determinism criterion. Keeping it separate is what lets the
    calibration file itself be byte-identical on re-run.
    """
    os.makedirs(out_dir, exist_ok=True)
    base = calibration_filename(payload["city"], payload["source"], payload["version"])
    path = os.path.join(out_dir, base.replace(".json", ".runlog.json"))
    blob = {
        "artifact": base,
        "artifact_content_hash": payload.get("content_hash"),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "Run metadata only. The calibration artifact is deterministic; this "
            "sidecar is not, and is excluded from any byte-comparison."
        ),
        **dict(extra),
    }
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(
            (
                json.dumps(blob, sort_keys=True, ensure_ascii=True, indent=1) + "\n"
            ).encode("utf-8")
        )
    os.replace(tmp, path)
    return path


def load_calibration(path: str) -> Dict[str, Any]:
    with open(path, "rb") as fh:
        payload = json.loads(fh.read().decode("utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CalibrationError(
            f"{path} has schema_version {payload.get('schema_version')!r}; this "
            f"build understands {SCHEMA_VERSION}"
        )
    if not verify_content_hash(payload):
        raise CalibrationError(f"{path} content_hash does not match its contents")
    return payload


# ---------------------------------------------------------------------------
# End-to-end build
# ---------------------------------------------------------------------------
@dataclass
class CityResult:
    city: str
    station: str
    payload: Dict[str, Any]
    paired: List[PairedDay]
    path: Optional[str] = None


def build_all(
    *,
    forecast_csv: str,
    stations: Mapping[str, Any],
    source: str,
    version: int,
    truth_dir: Optional[str] = None,
    min_bucket_n: int = MIN_BUCKET_N,
) -> List[CityResult]:
    """Build every city's calibration payload from files on disk.

    ``stations`` maps station id -> an object with ``timezone`` and
    ``series_ticker`` (i.e. :class:`~src.data.iem_cli_provider.StationSpec`).
    Reads only; writing is the caller's job so a test can build twice and
    compare bytes without touching the repo.
    """
    rows = load_forecast_series(forecast_csv)
    rows = [r for r in rows if r["source"] == source]
    if not rows:
        raise CalibrationError(
            f"{forecast_csv} contains no rows with source={source!r}"
        )
    fp_fields = (
        "city",
        "station",
        "target_date",
        "init_time_utc",
        "lead_hours",
        "source",
        "forecast_high_f",
        "spread_f",
    )

    results: List[CityResult] = []
    for station, spec in sorted(stations.items()):
        city = _city_of(spec)
        city_rows = [r for r in rows if r["station"] == station.upper()]
        truth = load_truth(station, truth_dir)
        paired, drops = pair_city(city_rows, station, truth)
        truth_rows = [
            {"station": station.upper(), "date": d, "high": truth[d]}
            for d in sorted(truth)
        ]
        payload = build_city_calibration(
            city=city,
            station=station.upper(),
            timezone_name=getattr(spec, "timezone", ""),
            source=source,
            version=version,
            paired=paired,
            drops=drops,
            forecast_rows_for_city=len(city_rows),
            forecast_fingerprint=content_fingerprint(city_rows, fp_fields),
            truth_fingerprint=content_fingerprint(
                truth_rows, ("station", "date", "high")
            ),
            min_bucket_n=min_bucket_n,
        )
        # Applied here, not in a producing script: this is the single place
        # every producer of this source passes through, so `build_calibration.py
        # --source gefs` and `backfill_ensemble_history.py --build-calibration`
        # cannot emit different files.
        payload = annotate(payload, source)
        results.append(
            CityResult(
                city=city,
                station=station.upper(),
                payload=finalize(payload),
                paired=paired,
            )
        )
    return results


def _city_of(spec: Any) -> str:
    ticker = str(getattr(spec, "series_ticker", "")).upper()
    return ticker[len("KXHIGH") :] if ticker.startswith("KXHIGH") else ticker


def day_of_sigma_table(results: Sequence[CityResult]) -> List[Dict[str, Any]]:
    """Rows for the EC-3 sigma table: one per city, with the pass/fail verdict.

    A city whose day-of bucket is insufficient is reported ``verdict="NO DATA"``
    -- it is neither a pass nor a silent omission.
    """
    table: List[Dict[str, Any]] = []
    for r in results:
        block = r.payload.get("day_of", {})
        n = int(block.get("n", 0))
        sigma = block.get("sigma_f")
        if not block.get("sufficient"):
            verdict = "NO DATA"
        elif sigma is None:
            verdict = "NO DATA"
        elif sigma <= SIGMA_SANITY_BOUND_F:
            verdict = "PASS"
        else:
            verdict = "FAIL"
        table.append(
            {
                "city": r.city,
                "station": r.station,
                "n": n,
                "bias_f": block.get("bias_f"),
                "sigma_f": sigma,
                "mae_f": block.get("mae_f"),
                "verdict": verdict,
            }
        )
    return table


def day_of_sensitivity(result: CityResult) -> Dict[str, Any]:
    """How fragile one city's published day-of sigma is, measured two ways.

    A single pooled sigma next to a bound is not evidence that the bound holds;
    it is one number from one sample. Both checks below run on that same sample,
    add no assumption, and are what turn "passes" into "passes because of what".

    * **Split half by target date.** The committed window is roughly 60%
      warm-season, and warm-season error is smaller at every city measured so
      far. Splitting chronologically shows whether the pooled number is an
      annual average hiding a cold-season failure.
    * **Leave-one-out.** Sigma recomputed with each day removed in turn. If the
      pooled sigma only clears (or only misses) the bound because of one
      extraordinary day, the LOO range straddles it. If the range is tight, the
      sample as a whole -- not an outlier -- is responsible.

    Returns ``{}`` when the day-of bucket has fewer than 4 paired days, which is
    too few for either statistic to mean anything.
    """
    days = sorted(
        (d for d in result.paired if bucket_for_lead(d.lead_hours) == DAY_OF_BUCKET),
        key=lambda d: (d.target_date, d.init_time_utc),
    )
    n = len(days)
    if n < 4:
        return {}
    errors = [d.error_f for d in days]
    half = n // 2
    loo = [stdev_sample(errors[:i] + errors[i + 1 :]) for i in range(n)]
    return {
        "city": result.city,
        "station": result.station,
        "n": n,
        "sigma_f": _r(stdev_sample(errors)),
        "first_half": {
            "n": half,
            "first_target_date": days[0].target_date,
            "last_target_date": days[half - 1].target_date,
            "sigma_f": _r(stdev_sample(errors[:half])),
        },
        "second_half": {
            "n": n - half,
            "first_target_date": days[half].target_date,
            "last_target_date": days[-1].target_date,
            "sigma_f": _r(stdev_sample(errors[half:])),
        },
        "leave_one_out_sigma_f": {"min": _r(min(loo)), "max": _r(max(loo))},
    }


__all__ = [
    "SCHEMA_VERSION",
    "ERROR_CONVENTION",
    "MIN_BUCKET_N",
    "LEAD_BUCKETS",
    "DAY_OF_BUCKET",
    "SIGMA_SANITY_BOUND_F",
    "SOURCE_ANNOTATORS",
    "CALIBRATION_DIR",
    "FORECAST_ARCHIVE_DIR",
    "WEATHER_TRUTH_DIR",
    "CalibrationError",
    "PairedDay",
    "CityResult",
    "percentile",
    "mean",
    "stdev_sample",
    "load_truth",
    "load_forecast_series",
    "content_fingerprint",
    "pair_city",
    "bucket_for_lead",
    "summarize",
    "source_annotation",
    "annotate",
    "build_city_calibration",
    "canonical_bytes",
    "compute_content_hash",
    "finalize",
    "verify_content_hash",
    "calibration_filename",
    "write_calibration",
    "write_runlog",
    "load_calibration",
    "build_all",
    "day_of_sigma_table",
    "day_of_sensitivity",
]
