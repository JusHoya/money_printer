"""EIA weekly retail and daily RBOB spot covariates for the gas model (FR-4.1).

WHY THESE TWO SERIES
--------------------
The AAA national average is the settlement index, but it is a *retail* average
and retail follows wholesale with a lag of several days. Two public covariates
carry that leading information:

* **EIA weekly U.S. regular all-formulations retail price** -- the same quantity
  AAA measures, from an independent surveyor. It is an anchor on level: if the
  AAA series drifts away from EIA's, the AAA parse is suspect.
* **Daily RBOB spot** -- the wholesale price retail is chasing. This is the
  actual leading indicator, and the lag is a **fitted parameter** in FR-4.2, not
  an assumed constant (contract §1).

WHY THE BULK ARCHIVE AND NOT THE JSON API
-----------------------------------------
``api.eia.gov/v2`` returns **403 without an API key**. Adding a credential
dependency for public data is avoidable, so this module reads EIA's keyless bulk
archive instead:

``https://api.eia.gov/bulk/manifest.txt`` -> ``https://www.eia.gov/opendata/bulk/PET.zip``

``PET.zip`` is ~55 MB compressed / ~366 MB of JSON-lines series records, read
with stdlib :mod:`zipfile` + :mod:`json`. **No new third-party dependency**: the
``hist_xls`` spreadsheet route would have needed ``xlrd``/``openpyxl``, neither
of which is installed or declared, and a new dependency is a pip install on the
production VM at deploy time (this project has already been bitten by Windows
long-path pip failures -- ``windows-maxpath-breaks-deep-venv-pip``).

The archive is cached to disk after the first download. Re-fetching 55 MB per
invocation would be both slow and rude.

WHICH SERIES IDS, AND ONE THAT IS DISCONTINUED
----------------------------------------------
Verified against ``PET.zip`` on 2026-07-29 (``last_updated``
``2026-07-29T17:38:03-04:00``):

===================================== ========== ================== ==========
series_id                             frequency  coverage           non-null
===================================== ========== ================== ==========
``PET.EMM_EPMR_PTE_NUS_DPG.W``        weekly     1990-08-20 ..      1870
                                                 2026-07-27
``PET.EER_EPMRR_PF4_Y05LA_DPG.D``     daily      2003-03-11 ..      5883
                                                 2026-07-27
``PET.EER_EPMRR_PE1_Y35NY_DPG.D``     daily      2005-10-03 ..      4659
                                                 **2024-04-05**
===================================== ========== ================== ==========

The obvious choice for "RBOB" would be the New York Harbor RBOB series, but
**every NY Harbor RBOB series in the archive is a futures contract series and
ends 2024-04-05** -- EIA discontinued it. It therefore covers none of the Phase
4 window and cannot be used. The only current daily RBOB *spot* series EIA
publishes keylessly is Los Angeles (:data:`RBOB_SERIES_ID`), so that is the
default.

That substitution is a real modelling caveat, not a detail: LA RBOB is a
California-specific benchmark (CARB gasoline, its own refinery constraints) and
is an imperfect proxy for national wholesale. :data:`RBOB_ALTERNATIVES` lists
the current national/Gulf/NY-Harbor *conventional* spot series as
one-line-change alternatives, and ``--rbob-series`` selects one, so whoever fits
the lag in FR-4.2 can compare rather than inherit this choice silently.

GAPS AND NULLS
--------------
EIA encodes non-publication days (holidays, market closures) as ``null``. Per
contract §1.1 those become **absent rows**, never zeros and never interpolated
values -- a zero-dollar RBOB print would poison any lag fit that saw it.

PROVENANCE
----------
Contract §5 requires the exact URL per row. A bare archive URL does not identify
which of ~4,000 series a row came from, so ``source_url`` is the archive URL
with a fragment naming the series
(``...PET.zip#PET.EMM_EPMR_PTE_NUS_DPG.W``) and ``source`` carries the series id
on its own. Both are recorded per row.

``fetched_at`` is the series' **EIA ``last_updated`` instant** (in UTC), not the
wall-clock time this script ran. One rule holds across all three
``data/gas_truth/`` series: *``fetched_at`` is when the source published or
captured the bytes the row came from* -- the Wayback capture instant for
archived AAA rows, EIA's publication instant here. Stamping "now" instead would
make every re-run a different artifact, so ``manifest.json``'s ``content_hash``
would change without the data changing and would stop identifying anything.
Re-running against an unchanged archive is now a byte-identical no-op.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import tempfile
import zipfile
from datetime import date as _date
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - requests is a hard project dependency
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
GAS_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "gas_truth")

EIA_MANIFEST_URL = "https://api.eia.gov/bulk/manifest.txt"
PET_ARCHIVE_URL = "https://www.eia.gov/opendata/bulk/PET.zip"

DEFAULT_USER_AGENT = (
    "money-printer/gas-convergence (+https://github.com/JusHoya/money_printer; "
    "contact hoyeriiim87@gmail.com)"
)

#: The archive is cached **outside** the repository by default: it is ~55 MB and
#: ``.gitignore`` is orchestrator-owned, so an in-repo default would risk
#: committing it.
DEFAULT_CACHE_DIR = os.path.join(tempfile.gettempdir(), "money_printer_eia_cache")

#: U.S. Regular All Formulations Retail Gasoline Prices, Weekly.
#: Contract §1: "EIA weekly U.S. regular all-formulations retail price".
EIA_WEEKLY_SERIES_ID = "PET.EMM_EPMR_PTE_NUS_DPG.W"

#: Los Angeles Reformulated RBOB Regular Gasoline Spot Price, Daily. See the
#: module docstring: the NY Harbor RBOB series are futures and end 2024-04-05.
RBOB_SERIES_ID = "PET.EER_EPMRR_PF4_Y05LA_DPG.D"

#: Current daily wholesale-gasoline spot alternatives, for the FR-4.2 lag fit to
#: compare against rather than inherit :data:`RBOB_SERIES_ID` unexamined.
RBOB_ALTERNATIVES: Dict[str, str] = {
    "la_rbob_spot": "PET.EER_EPMRR_PF4_Y05LA_DPG.D",
    "ny_harbor_conventional_spot": "PET.EER_EPMRU_PF4_Y35NY_DPG.D",
    "gulf_coast_conventional_spot": "PET.EER_EPMRU_PF4_RGC_DPG.D",
}

#: Covers the full AAA backfill span (from 2022-01-01) plus a year of lead-in, so
#: a multi-day lagged covariate has inputs for the span's first days and the EIA
#: external cross-check can reach the oldest AAA rows -- it is the only check that
#: can catch a wrong-column parse in an era whose layout differs.
DEFAULT_START = "2021-01-01"

WEEKLY_CSV_COLUMNS: Tuple[str, ...] = (
    "week_ending",
    "value",
    "source",
    "source_url",
    "fetched_at",
)
DAILY_CSV_COLUMNS: Tuple[str, ...] = (
    "date",
    "value",
    "source",
    "source_url",
    "fetched_at",
)

#: Plausibility window, USD/gal, for a US retail or wholesale gasoline price.
#: Wholesale can legitimately sit well below retail, so the floor is lower than
#: the AAA series'.
MIN_PLAUSIBLE_VALUE = 0.20
MAX_PLAUSIBLE_VALUE = 12.00


class CovariateUnavailable(Exception):
    """The covariate could not be retrieved or contained no usable rows.

    Raised rather than returning an empty series: an empty covariate file looks
    identical to "the covariate has no signal" to whatever fits on it, and per
    ``abort-on-missing-critical-input`` the honest response to a missing input is
    to stop, not to hand downstream code a plausible-looking void.
    """


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Archive retrieval
# ---------------------------------------------------------------------------
def resolve_archive_url(
    *, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 60.0
) -> str:
    """Read the PET archive URL from EIA's bulk manifest.

    The manifest is the documented pointer to the archive, so resolving through
    it means a future relocation is followed rather than 404ing on a URL guessed
    once. Falls back to the known-good URL if the manifest is unreachable -- the
    manifest being down is not a reason to abandon a working download.
    """
    if requests is None:  # pragma: no cover
        return PET_ARCHIVE_URL
    try:
        resp = requests.get(
            EIA_MANIFEST_URL, headers={"User-Agent": user_agent}, timeout=timeout
        )
        resp.raise_for_status()
        url = ((resp.json().get("dataset") or {}).get("PET", {}) or {}).get("accessURL")
        if url:
            return str(url)
    except Exception as exc:
        logger.info(
            "EIA bulk manifest unreadable (%s); using known PET archive URL", exc
        )
    return PET_ARCHIVE_URL


def download_archive(
    *,
    cache_dir: str = DEFAULT_CACHE_DIR,
    url: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 900.0,
    force: bool = False,
) -> str:
    """Return a local path to ``PET.zip``, downloading it only when needed.

    Cached across runs on purpose: the archive is ~55 MB and EIA refreshes it
    once a day, so re-downloading per invocation is wasteful and impolite.
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "PET.zip")
    if os.path.exists(path) and os.path.getsize(path) > 0 and not force:
        logger.info(
            "Using cached EIA archive %s (%d bytes)", path, os.path.getsize(path)
        )
        return path
    if requests is None:  # pragma: no cover
        raise CovariateUnavailable("requests is unavailable in this environment")

    target = url or resolve_archive_url(user_agent=user_agent)
    logger.info("Downloading EIA archive %s ...", target)
    tmp = f"{path}.tmp"
    try:
        with requests.get(
            target, headers={"User-Agent": user_agent}, timeout=timeout, stream=True
        ) as resp:
            if resp.status_code != 200:
                raise CovariateUnavailable(
                    f"GET {target} returned HTTP {resp.status_code}"
                )
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(1 << 20):
                    if chunk:
                        fh.write(chunk)
    except CovariateUnavailable:
        raise
    except Exception as exc:
        raise CovariateUnavailable(f"GET {target} failed: {exc}") from exc
    os.replace(tmp, path)
    logger.info("Downloaded %s (%d bytes)", path, os.path.getsize(path))
    return path


# ---------------------------------------------------------------------------
# Series extraction
# ---------------------------------------------------------------------------
def extract_series(archive_path: str, series_ids: Sequence[str]) -> Dict[str, dict]:
    """Pull the requested series records out of ``PET.zip``.

    Streams the JSON-lines member rather than loading 366 MB into memory, and
    pre-filters with a substring test before paying for :func:`json.loads` --
    only a few thousand of ~130k lines can possibly match.
    """
    wanted = set(series_ids)
    found: Dict[str, dict] = {}
    with zipfile.ZipFile(archive_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not names:
            raise CovariateUnavailable(
                f"no .txt member in {archive_path}: {zf.namelist()}"
            )
        with zf.open(names[0]) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
            for line in stream:
                if not any(sid in line for sid in wanted - set(found)):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("series_id")
                if sid in wanted and sid not in found:
                    found[sid] = rec
                    if len(found) == len(wanted):
                        return found
    missing = wanted - set(found)
    if missing:
        raise CovariateUnavailable(
            f"series not present in {archive_path}: {sorted(missing)}"
        )
    return found


def series_published_at(record: dict) -> Optional[str]:
    """EIA's ``last_updated`` for a series, normalised to a UTC ISO-8601 stamp.

    The archive reports it with an offset (``2026-07-29T17:38:03-04:00``);
    converting to UTC keeps every ``fetched_at`` in the dataset comparable.
    Returns ``None`` when absent so the caller can fall back rather than invent a
    timestamp.
    """
    raw = record.get("last_updated")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_eia_date(token: str) -> Optional[_date]:
    """Parse an EIA period token. Only daily/weekly (``YYYYMMDD``) is accepted.

    A monthly (``YYYYMM``) or annual (``YYYY``) token is rejected rather than
    padded to a day: silently turning ``202607`` into 2026-07-01 would mix a
    monthly average into a daily series, which no downstream check would catch.
    """
    tok = str(token).strip()
    if len(tok) != 8 or not tok.isdigit():
        return None
    try:
        return datetime.strptime(tok, "%Y%m%d").date()
    except ValueError:
        return None


def series_to_rows(
    record: dict,
    *,
    series_id: str,
    archive_url: str,
    date_column: str,
    start: Optional[_date] = None,
    end: Optional[_date] = None,
    fetched_at: Optional[str] = None,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Convert one EIA series record into contract-shaped CSV rows.

    Returns ``(rows, stats)``. ``stats`` counts what was dropped and why, so a
    filter that silently discards everything is visible from the log alone
    (``make-silent-rejections-observable``).
    """
    # EIA's own publication instant, so the output is reproducible; the run's
    # wall clock is only a last resort when the archive omits last_updated.
    stamp = fetched_at or series_published_at(record) or _utc_now_iso()
    source_url = f"{archive_url}#{series_id}"
    rows: List[Dict[str, str]] = []
    stats = {
        "points": 0,
        "null_value": 0,
        "bad_date": 0,
        "out_of_window": 0,
        "implausible": 0,
        "kept": 0,
    }

    for point in record.get("data") or []:
        stats["points"] += 1
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            stats["bad_date"] += 1
            continue
        token, value = point[0], point[1]
        day = _parse_eia_date(token)
        if day is None:
            stats["bad_date"] += 1
            continue
        # Window first, so every count below describes the window actually
        # requested. Counting nulls across all 36 years of history and printing
        # that beside a 19-month row count invites exactly the wrong conclusion
        # about how complete the window is.
        if (start and day < start) or (end and day > end):
            stats["out_of_window"] += 1
            continue
        if value is None:
            # EIA's null = not published (holiday / closure). Contract §1.1: a
            # gap is an absent row, never a zero and never an interpolation.
            stats["null_value"] += 1
            continue
        try:
            val = float(value)
        except (TypeError, ValueError):
            stats["bad_date"] += 1
            continue
        if not (MIN_PLAUSIBLE_VALUE <= val <= MAX_PLAUSIBLE_VALUE):
            logger.info(
                "EIA %s dropping implausible %s = %r (outside [%.2f, %.2f])",
                series_id,
                day.isoformat(),
                val,
                MIN_PLAUSIBLE_VALUE,
                MAX_PLAUSIBLE_VALUE,
            )
            stats["implausible"] += 1
            continue
        rows.append(
            {
                date_column: day.isoformat(),
                "value": f"{val:.3f}",
                "source": series_id,
                "source_url": source_url,
                "fetched_at": stamp,
            }
        )
        stats["kept"] += 1

    rows.sort(key=lambda r: r[date_column])
    return rows, stats


def weekday_audit(rows: Sequence[Dict[str, str]], date_column: str) -> Dict[str, int]:
    """Count rows per weekday.

    Contract §1: ``week_ending`` is "the Monday EIA dates the observation to".
    That is an assertion about the data, so it is measured rather than trusted --
    a series that silently switched to Friday dating would shift every weekly
    covariate by four days.
    """
    counts: Dict[str, int] = {}
    for row in rows:
        try:
            day = _date.fromisoformat(row[date_column])
        except (KeyError, ValueError):
            continue
        name = day.strftime("%A")
        counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def write_csv(rows: Sequence[Dict[str, str]], path: str, columns: Sequence[str]) -> str:
    """Write a covariate series atomically: UTF-8, explicit LF, sorted, header.

    LF is explicit for the same reason as the AAA series: ``manifest.json``'s
    ``content_hash`` is taken over these bytes, and a CRLF checkout would change
    the hash for a file nobody edited
    (``hash-gated-fixtures-need-eol-lf``).
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in columns})
    os.replace(tmp, path)
    return path


def backfill_covariates(
    *,
    gas_dir: str = GAS_TRUTH_DIR,
    cache_dir: str = DEFAULT_CACHE_DIR,
    start: Optional[str] = DEFAULT_START,
    end: Optional[str] = None,
    weekly_series_id: str = EIA_WEEKLY_SERIES_ID,
    rbob_series_id: str = RBOB_SERIES_ID,
    force_download: bool = False,
    archive_path: Optional[str] = None,
    archive_url: Optional[str] = None,
) -> Dict[str, Dict[str, object]]:
    """Write ``eia_weekly_regular.csv`` and ``rbob_daily.csv`` (contract §1).

    :param archive_path: use an already-downloaded ``PET.zip`` (tests, reruns).
    :raises CovariateUnavailable: if either series ends up with zero rows. An
        empty covariate file is worse than a missing one: it looks like data.
    """
    start_d = _date.fromisoformat(start) if start else None
    end_d = _date.fromisoformat(end) if end else None

    resolved_url = archive_url or PET_ARCHIVE_URL
    if archive_path is None:
        resolved_url = archive_url or resolve_archive_url()
        archive_path = download_archive(
            cache_dir=cache_dir, url=resolved_url, force=force_download
        )

    records = extract_series(archive_path, [weekly_series_id, rbob_series_id])
    # No run-time stamp: each series carries EIA's own last_updated instant so
    # the CSVs are byte-reproducible (see the PROVENANCE note in the docstring).
    out: Dict[str, Dict[str, object]] = {}

    weekly_rows, weekly_stats = series_to_rows(
        records[weekly_series_id],
        series_id=weekly_series_id,
        archive_url=resolved_url,
        date_column="week_ending",
        start=start_d,
        end=end_d,
    )
    if not weekly_rows:
        raise CovariateUnavailable(
            f"{weekly_series_id} produced zero rows in "
            f"{start_d}..{end_d} (stats {weekly_stats})"
        )
    weekly_path = write_csv(
        weekly_rows, os.path.join(gas_dir, "eia_weekly_regular.csv"), WEEKLY_CSV_COLUMNS
    )
    weekdays = weekday_audit(weekly_rows, "week_ending")
    if set(weekdays) - {"Monday"}:
        logger.warning(
            "eia_weekly_regular week_ending is not uniformly Monday: %s "
            "(contract §1 assumes Monday dating)",
            weekdays,
        )
    out["eia_weekly_regular"] = {
        "path": weekly_path,
        "series_id": weekly_series_id,
        "rows": len(weekly_rows),
        "first": weekly_rows[0]["week_ending"],
        "last": weekly_rows[-1]["week_ending"],
        "weekday_counts": weekdays,
        "stats": weekly_stats,
    }

    rbob_rows, rbob_stats = series_to_rows(
        records[rbob_series_id],
        series_id=rbob_series_id,
        archive_url=resolved_url,
        date_column="date",
        start=start_d,
        end=end_d,
    )
    if not rbob_rows:
        raise CovariateUnavailable(
            f"{rbob_series_id} produced zero rows in "
            f"{start_d}..{end_d} (stats {rbob_stats})"
        )
    rbob_path = write_csv(
        rbob_rows, os.path.join(gas_dir, "rbob_daily.csv"), DAILY_CSV_COLUMNS
    )
    out["rbob_daily"] = {
        "path": rbob_path,
        "series_id": rbob_series_id,
        "rows": len(rbob_rows),
        "first": rbob_rows[0]["date"],
        "last": rbob_rows[-1]["date"],
        "stats": rbob_stats,
    }

    for name, info in out.items():
        logger.info(
            "%s: %d rows %s .. %s (series %s, dropped null=%d out-of-window=%d)",
            name,
            info["rows"],
            info["first"],
            info["last"],
            info["series_id"],
            info["stats"]["null_value"],  # type: ignore[index]
            info["stats"]["out_of_window"],  # type: ignore[index]
        )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill EIA weekly retail and daily RBOB spot covariates "
        "from EIA's keyless bulk archive (PRD FR-4.1).",
    )
    parser.add_argument("--gas-dir", default=GAS_TRUTH_DIR)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--weekly-series", default=EIA_WEEKLY_SERIES_ID)
    parser.add_argument(
        "--rbob-series",
        default=RBOB_SERIES_ID,
        help=f"one of {sorted(RBOB_ALTERNATIVES.values())} or any EIA series id",
    )
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--archive-path", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    result = backfill_covariates(
        gas_dir=args.gas_dir,
        cache_dir=args.cache_dir,
        start=args.start,
        end=args.end,
        weekly_series_id=args.weekly_series,
        rbob_series_id=args.rbob_series,
        force_download=args.force_download,
        archive_path=args.archive_path,
    )
    for name, info in result.items():
        print(
            f"{name}: rows={info['rows']} {info['first']} .. {info['last']} "
            f"series={info['series_id']} -> {info['path']}",
            flush=True,
        )

    # Contract §1 requires manifest.json to carry a content_hash per series, and
    # the manifest must stay truthful whichever entry point wrote the CSVs -- a
    # hash that no longer matches the bytes on disk is worse than none.
    from src.data.aaa_provider import update_manifest

    manifest = update_manifest(gas_dir=args.gas_dir)
    for name, info in (manifest.get("series") or {}).items():
        print(
            f"manifest {name}: rows={info.get('rows')} "
            f"{info.get('first')} .. {info.get('last')} "
            f"hash={str(info.get('content_hash'))[:16]}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
