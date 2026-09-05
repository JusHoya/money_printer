"""Forecast vintages for the sandbox strategy, with the availability-lag rule (FR-F3.2).

The factory frame joins every ladder snapshot to the latest forecast run whose
``init_time_utc + availability_lag <= ts_utc`` (FACTORY_ARCHITECTURE section
4.2 item 3; ``src/factory/lanes/weather.py`` shifts the archive's ``init_ts``
by the lag before ``ev_analysis.forecast_vintage_table``). ``GenomeStrategy``
must see the SAME vintage the frame saw for a given ``(city, target_date,
decision time)``, so the rule lives here, once, behind one method::

    latest_vintage(city, target_date, as_of) -> Vintage | None

Two constructions share that method:

* ``source='replay'`` -- built from the frame's forecast archive CSV
  (``data/forecast_archive/forecast_series_<source>.csv``) or any iterable of
  archive rows. Used by ``scripts/factory_replay_parity.py`` so the strategy is
  driven with exactly the vintage table the frame used. ``fetched_at`` is
  ``None`` (the archive stores model runtimes, not receipt times).
* ``source='live'`` -- wraps ``src.data.mos_guidance_provider.MOSGuidanceProvider``:
  the candidate MEX runs (00Z/12Z) whose ``init + lag <= as_of`` are fetched
  (the provider caches archived runs on disk; 404 = not yet archived, retried
  next hour) and the latest run carrying ``target_date`` wins. Every run seen
  for the first time is appended to ``<cache_dir>/vintages.jsonl`` with
  ``fetched_at`` (from the injected ``clock``) so the lag becomes empirical
  (section 4.2: "made empirical from ``fetched_at`` once the runtime provider
  has recorded a month").

Clock discipline: this module never reads the wall clock. ``as_of`` is
supplied by the caller and ``fetched_at`` comes from the injected ``clock``.
``datetime`` is imported as a module (``_dt``) for types/arithmetic only.
"""
from __future__ import annotations

import bisect
import csv
import datetime as _dt
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
FORECAST_ARCHIVE_DIR = os.path.join(REPO_ROOT, "data", "forecast_archive")
#: F3 contract: ``MP_FORECAST_CACHE_DIR`` overrides; compose mounts /srv/money_printer/data/forecast_cache.
DEFAULT_CACHE_DIR = os.path.join(REPO_ROOT, "data", "forecast_cache")
CACHE_DIR_ENV = "MP_FORECAST_CACHE_DIR"
VINTAGE_LOG_NAME = "vintages.jsonl"

SOURCE_REPLAY = "replay"
SOURCE_LIVE = "live"
DEFAULT_LAG_MIN = 240
DEFAULT_MODEL = "MEX"
#: How many UTC days back the live path looks for a run that forecasts ``target_date``.
LIVE_LOOKBACK_DAYS = 3

#: ``CITY -> settlement station`` for the four KXHIGH cities (== ev_analysis.CITY_STATION).
CITY_STATION: Dict[str, str] = {"NY": "KNYC", "CHI": "KMDW", "LAX": "KLAX", "MIA": "KMIA"}


class ForecastVintageError(RuntimeError):
    """A vintage table could not be built or a live fetch is impossible."""


def _utc(dt: _dt.datetime) -> _dt.datetime:
    if dt.tzinfo is None:
        raise ForecastVintageError("naive datetime given where a tz-aware instant is required")
    return dt.astimezone(_dt.timezone.utc)


def parse_init_time(value: Any) -> _dt.datetime:
    """``'2026-07-19T12:00:00Z'`` / ``'2026-07-19 12:00'`` -> tz-aware UTC datetime."""
    if isinstance(value, _dt.datetime):
        return _utc(value) if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T")
    dt = _dt.datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)


def format_init_time(dt: _dt.datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Vintage:
    """One usable forecast run for ``(city, target_date)``.

    ``init_time_utc`` is the archive string (``YYYY-MM-DDTHH:MM:SSZ``); the
    frame carries it unchanged and ``lead_hours`` is the archive's own integer.
    ``sigma_f`` is the source's published spread (``spread_f``) or ``None``
    (MEX publishes none; never coerced to 0.0). ``fetched_at`` is when the
    live path first saw the run (``None`` on replay).
    """

    city: str
    target_date: str
    init_time_utc: str
    forecast_high_f: float
    lead_hours: int
    source: str
    sigma_f: Optional[float] = None
    fetched_at: Optional[_dt.datetime] = None

    @property
    def init_epoch(self) -> int:
        return int(parse_init_time(self.init_time_utc).timestamp())

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fetched_at"] = None if self.fetched_at is None else format_init_time(self.fetched_at)
        return d


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in ("nan", "none", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


class ForecastVintageProvider:
    """``latest_vintage`` over a replay table or the live MOS archive (module docstring)."""

    def __init__(
        self,
        source: str,
        *,
        lag_min: int = DEFAULT_LAG_MIN,
        rows: Optional[Iterable[Mapping[str, Any]]] = None,
        forecast_source: str = "gfs_mex",
        mos_provider: Any = None,
        clock: Optional[Callable[[], _dt.datetime]] = None,
        cache_dir: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        city_station: Optional[Mapping[str, str]] = None,
        lookback_days: int = LIVE_LOOKBACK_DAYS,
    ) -> None:
        if source not in (SOURCE_REPLAY, SOURCE_LIVE):
            raise ForecastVintageError(f"source must be 'replay' or 'live', got {source!r}")
        if int(lag_min) < 0:
            raise ForecastVintageError("lag_min must be >= 0")
        self.source = source
        self.lag_min = int(lag_min)
        self.forecast_source = str(forecast_source)
        self.model = str(model).upper()
        self.city_station = dict(city_station or CITY_STATION)
        self.lookback_days = int(lookback_days)
        # (city, target_date) -> (sorted init epochs, vintages in that order)
        self._index: Dict[Tuple[str, str], Tuple[List[int], List[Vintage]]] = {}
        self.stats: Dict[str, int] = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "runs_requested": 0,
            "runs_new": 0,
            "runs_empty": 0,
            "fetch_errors": 0,
        }
        if source == SOURCE_REPLAY:
            if rows is None:
                raise ForecastVintageError("replay provider needs archive rows (use from_archive_csv)")
            self._ingest(rows, fetched_at=None)
            self._mos = None
            self._clock = None
            self.cache_dir = None
        else:
            if mos_provider is None:
                raise ForecastVintageError("live provider needs a MOSGuidanceProvider")
            if clock is None:
                raise ForecastVintageError("live provider needs an injected clock (no wall clock here)")
            self._mos = mos_provider
            self._clock = clock
            self.cache_dir = cache_dir or os.getenv(CACHE_DIR_ENV) or DEFAULT_CACHE_DIR
            os.makedirs(self.cache_dir, exist_ok=True)
            self._runs_seen: Dict[Tuple[str, str], bool] = {}  # (station, init_time_utc) -> had rows
            self._load_vintage_log()

    # -- constructors ------------------------------------------------------
    @classmethod
    def from_archive_csv(
        cls, path: str, *, lag_min: int = DEFAULT_LAG_MIN, forecast_source: Optional[str] = None
    ) -> "ForecastVintageProvider":
        """Replay provider from ``forecast_series_<source>.csv`` (rows with an empty high are dropped)."""
        if not os.path.exists(path):
            raise ForecastVintageError(f"forecast archive missing: {path}")
        with open(path, "r", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        src = forecast_source
        if src is None:
            src = rows[0].get("source", "gfs_mex") if rows else "gfs_mex"
        return cls(SOURCE_REPLAY, lag_min=lag_min, rows=rows, forecast_source=str(src))

    @classmethod
    def from_rows(
        cls, rows: Iterable[Mapping[str, Any]], *, lag_min: int = DEFAULT_LAG_MIN, forecast_source: str = "gfs_mex"
    ) -> "ForecastVintageProvider":
        return cls(SOURCE_REPLAY, lag_min=lag_min, rows=rows, forecast_source=forecast_source)

    @classmethod
    def live(
        cls,
        mos_provider: Any,
        *,
        clock: Callable[[], _dt.datetime],
        lag_min: int = DEFAULT_LAG_MIN,
        cache_dir: Optional[str] = None,
        forecast_source: str = "gfs_mex",
        model: str = DEFAULT_MODEL,
    ) -> "ForecastVintageProvider":
        return cls(
            SOURCE_LIVE, lag_min=lag_min, mos_provider=mos_provider, clock=clock, cache_dir=cache_dir,
            forecast_source=forecast_source, model=model,
        )

    # -- table -------------------------------------------------------------
    def _ingest(self, rows: Iterable[Mapping[str, Any]], *, fetched_at: Optional[_dt.datetime]) -> int:
        """Add archive-shaped rows (``city, target_date, init_time_utc, lead_hours, forecast_high_f, spread_f``)."""
        added = 0
        for r in rows:
            high = _float_or_none(r.get("forecast_high_f"))
            if high is None:
                continue  # evaluator: dropna(subset=["forecast_high_f"])
            city = str(r.get("city", "")).upper().strip()
            tdate = str(r.get("target_date", "")).strip()[:10]
            init = format_init_time(parse_init_time(r.get("init_time_utc")))
            lead_raw = r.get("lead_hours")
            lead = int(float(lead_raw)) if lead_raw not in (None, "") else 0
            v = Vintage(
                city=city,
                target_date=tdate,
                init_time_utc=init,
                forecast_high_f=float(high),
                lead_hours=lead,
                source=str(r.get("source") or self.forecast_source),
                sigma_f=_float_or_none(r.get("spread_f")),
                fetched_at=fetched_at,
            )
            key = (city, tdate)
            epochs, vints = self._index.setdefault(key, ([], []))
            e = v.init_epoch
            # stable insert: equal init epochs keep arrival order (merge_asof takes the last)
            i = bisect.bisect_right(epochs, e)
            if i > 0 and epochs[i - 1] == e and vints[i - 1].init_time_utc == init:
                # same run already known for this key: keep the first record (immutable archive)
                continue
            epochs.insert(i, e)
            vints.insert(i, v)
            added += 1
        return added

    def known_runs(self, city: str, target_date: str) -> List[Vintage]:
        epochs, vints = self._index.get((str(city).upper(), str(target_date)[:10]), ([], []))
        return list(vints)

    # -- the rule ------------------------------------------------------------
    def _select(self, city: str, target_date: str, as_of_epoch: int) -> Optional[Vintage]:
        key = (str(city).upper(), str(target_date)[:10])
        entry = self._index.get(key)
        if not entry:
            return None
        epochs, vints = entry
        # latest run with init + lag <= as_of  (merge_asof backward, allow_exact_matches)
        i = bisect.bisect_right(epochs, as_of_epoch - self.lag_min * 60) - 1
        if i < 0:
            return None
        return vints[i]

    def latest_vintage(self, city: str, target_date: str, as_of: _dt.datetime) -> Optional[Vintage]:
        """The latest run for ``(city, target_date)`` with ``init + lag <= as_of``; ``None`` if none."""
        as_of_utc = _utc(as_of)
        as_of_epoch = int(as_of_utc.timestamp())
        self.stats["lookups"] += 1
        if self.source == SOURCE_LIVE:
            try:
                self._refresh_live(str(city).upper(), str(target_date)[:10], as_of_utc)
            except Exception as exc:  # network / archive faults are a NO_VINTAGE, never a crash
                self.stats["fetch_errors"] += 1
                logger.warning("[Vintage] live refresh failed for %s %s: %s", city, target_date, exc)
        v = self._select(city, target_date, as_of_epoch)
        self.stats["hits" if v is not None else "misses"] += 1
        return v

    # -- live path ---------------------------------------------------------
    def _vintage_log_path(self) -> str:
        return os.path.join(str(self.cache_dir), VINTAGE_LOG_NAME)

    def _load_vintage_log(self) -> None:
        """Warm the table from previously recorded live vintages (keeps their original fetched_at)."""
        path = self._vintage_log_path()
        if not os.path.exists(path):
            return
        rows: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                rows.append(d)
        for d in rows:
            fa = d.get("fetched_at")
            fetched = parse_init_time(fa) if fa else None
            self._ingest([d], fetched_at=fetched)
            self._runs_seen[(str(d.get("station", "")).upper(), str(d.get("init_time_utc")))] = True

    def _append_vintage_log(self, station: str, vintages: Sequence[Vintage], as_of: _dt.datetime) -> None:
        path = self._vintage_log_path()
        with open(path, "a", encoding="utf-8") as fh:
            for v in vintages:
                d = v.as_dict()
                d["station"] = station
                d["as_of"] = format_init_time(as_of)
                d["lag_min"] = self.lag_min
                fh.write(json.dumps(d, sort_keys=True) + "\n")

    def _candidate_runs(self, as_of: _dt.datetime) -> List[_dt.datetime]:
        """Model runtimes with ``init + lag <= as_of`` over the lookback window, latest first."""
        from src.data.mos_guidance_provider import MODEL_RUN_HOURS

        hours = MODEL_RUN_HOURS.get(self.model, (0, 12))
        latest_init = as_of - _dt.timedelta(minutes=self.lag_min)
        out: List[_dt.datetime] = []
        day = latest_init.date()
        for back in range(self.lookback_days + 1):
            d = day - _dt.timedelta(days=back)
            for h in hours:
                run = _dt.datetime(d.year, d.month, d.day, int(h), tzinfo=_dt.timezone.utc)
                if run <= latest_init:
                    out.append(run)
        out.sort(reverse=True)
        return out

    def _refresh_live(self, city: str, target_date: str, as_of: _dt.datetime) -> None:
        """Fetch (cached) the candidate runs for ``city`` until one forecasts ``target_date``."""
        station = self.city_station.get(city)
        if station is None:
            raise ForecastVintageError(f"no settlement station known for city {city!r}")
        from src.data.mos_guidance_provider import format_runtime

        for run in self._candidate_runs(as_of):
            init_str = format_runtime(run)
            seen = self._runs_seen.get((station, init_str))
            if seen:
                # already ingested; if it carries target_date we are done, else try an older run
                if any(v.init_time_utc == init_str for v in self.known_runs(city, target_date)):
                    return
                continue
            self.stats["runs_requested"] += 1
            forecasts = self._mos.fetch_daily_highs([station], self.model, run, source=self.forecast_source)
            if not forecasts:
                self.stats["runs_empty"] += 1
                continue  # not archived yet (404): retried on the next call, never cached as a gap
            fetched_at = _utc(self._clock())
            rows = [f.as_row() if hasattr(f, "as_row") else dict(f) for f in forecasts]
            vintages: List[Vintage] = []
            for r in rows:
                high = _float_or_none(r.get("forecast_high_f"))
                if high is None:
                    continue
                vintages.append(
                    Vintage(
                        city=str(r["city"]).upper(),
                        target_date=str(r["target_date"])[:10],
                        init_time_utc=format_init_time(parse_init_time(r["init_time_utc"])),
                        forecast_high_f=float(high),
                        lead_hours=int(r["lead_hours"]),
                        source=str(r.get("source") or self.forecast_source),
                        sigma_f=_float_or_none(r.get("spread_f")),
                        fetched_at=fetched_at,
                    )
                )
            self._ingest(rows, fetched_at=fetched_at)
            self._runs_seen[(station, init_str)] = True
            self.stats["runs_new"] += 1
            self._append_vintage_log(station, vintages, as_of)
            if any(v.target_date == target_date for v in vintages):
                return


__all__ = [
    "CACHE_DIR_ENV",
    "CITY_STATION",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_LAG_MIN",
    "ForecastVintageError",
    "ForecastVintageProvider",
    "SOURCE_LIVE",
    "SOURCE_REPLAY",
    "VINTAGE_LOG_NAME",
    "Vintage",
    "format_init_time",
    "parse_init_time",
]
