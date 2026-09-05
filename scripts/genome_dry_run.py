#!/usr/bin/env python
"""Accelerated 24-h weather dry run against ONE archived city-day (F3, INFRA).

PRD_STRATEGY_FACTORY.md Phase F3 exit criterion: *"Every emitted signal has a
tz-aware ``expiration_time`` at settlement-day close; a 24-h dev-box dry run
settles its positions."*  This script is that dry run.  It drives the REAL
``WeatherBot`` -> strategy waterfall -> ``SignalProcessorMixin._process_signals``
-> ``RiskManager`` -> ``SimulatedExchange`` chain through every hourly candle
of one archived city-day, then past the settlement close, publishes the
archived CLI truth and asserts that every position left the book through
``SimulatedExchange._settle_weather_position``.

    python scripts/genome_dry_run.py --city NY --date 2026-07-20
    python scripts/genome_dry_run.py --city NY --date 2026-07-20 \
        --genome-spec configs/factory/promoted/<id>.json --genome-mode shadow

Exit codes: 0 every assertion held; 1 an assertion failed (the report says
which); 2 the run could not be set up (no ladder for the day, genome strategy
not importable/constructible, ...).  The report
``reports/factory/dry_run_<city>_<date>.json`` is timestamp-free (candle
instants are *data*, not wall-clock) so a re-run on the same checkout is
byte-identical.

WHAT IS REAL, WHAT IS INJECTED
------------------------------
Real, executed unmodified: ``WeatherBot.tick`` / ``_ladder_for_city``, the V2
strategy (and ``GenomeStrategy`` when ``--genome-spec`` is given and the
module exists), ``bracket_payoff.attach_spec_to_signals``,
``SignalProcessorMixin._process_signals``, ``RiskManager`` (Kelly, EV gate,
cooldowns, allocation), ``SimulatedExchange`` (fees, marks, the EXPIRATION
sweep, ``_settle_weather_position``, ``_close_position``, ``_save_state``),
``weather_settlement.resolve_settlement_high`` reading a real cache file.
None of the protected files (``risk_manager.py``, ``mixins.py``,
``matching_engine.py``) is modified; instrumentation is by wrapping *instance*
attributes only.

Seams, all named:

1. **Clock.** The runtime reads wall-clock in seven modules (``weather_bot``,
   ``mixins``, ``weather_strategy``, ``ml_weather``, ``matching_engine``,
   ``risk_manager``, ``weather_settlement``) through the names ``datetime`` /
   ``date`` / ``time`` bound at import.  :func:`install_clock` rebinds those
   names to fakes that answer from :class:`DryRunClock` (``datetime.now(tz)``
   -> the clock in ``tz``; naive ``now()`` -> the clock in the host's local
   zone, so ``datetime.now().astimezone()`` stays self-consistent; ``time.time``
   -> the clock's epoch; ``time.sleep`` -> no-op, which is the acceleration).
   The fake ``datetime`` is a subclass with an ``__instancecheck__`` that
   accepts real datetimes, because ``matching_engine`` and ``risk_manager``
   use ``isinstance(v, datetime)`` on values they did not create.  If the bot
   exposes a ``clock`` injection point (FR-F3.3: ``WeatherBot(clock=...)`` or
   a ``clock`` attribute) it is set as well, so once that lands the bot's own
   ET date logic no longer depends on the module patch.  Everything is undone
   in ``finally`` so the process (or a pytest session) is left clean.
2. **Providers.** ``bot.kalshi`` / ``bot.nws`` / ``bot.metar`` are replaced by
   offline stubs replaying the archive: ``fetch_market_ladder`` returns the
   ladder snapshot at the clock's candle (``data/ladders/<SERIES>/<date>.csv``
   via ``ev_analysis.load_search_ladders`` -- sealed roots refused),
   ``nws.fetch_latest`` carries the GFS-MEX vintage usable at the candle
   (``ev_analysis.forecast_vintage_table`` on ``load_forecast_archive``, with
   the frame's availability lag) as one NWS-shaped daytime period.
   ``bot.setup`` is not called (it would build a live METAR client).
3. **Observations (synthetic).** No hourly METAR archive exists on disk.  The
   station observation is reconstructed from the CLI daily low/high/high_time
   in ``data/weather_truth/cli_daily_high_<station>.csv`` as a smooth diurnal
   curve (low at 06:00 local, high at ``high_time``); ``max_temp_today_f`` is
   the running max over that curve on the station's local day.  This is the
   one input that is not archived tape; it only feeds V2's winner-guard and
   velocity branches, never settlement.
4. **Truth.** ``weather_settlement.SETTLEMENT_CACHE_PATH`` is redirected to a
   temp file and the IEM client replaced with an offline stub (never network).
   Truth is *published* into that cache only after the clock has passed the
   settlement close, so the candle that lands exactly on the close exercises
   the SETTLEMENT_TRUTH_PENDING hold-and-retry branch first.
5. **State.** ``risk_manager._DEFAULT_STATE_FILE`` / ``WIN_RATES_PATH`` point
   into a temp directory: the run never reads or writes production state.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import math
import os
import re
import sys
import tempfile
import time as _real_time
from datetime import date as _real_date
from datetime import datetime as _real_datetime
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

EXIT_OK = 0
EXIT_ASSERTION = 1
EXIT_UNAVAILABLE = 2

DEFAULT_LADDER_ROOT = os.path.join(REPO_ROOT, "data", "ladders")
DEFAULT_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "weather_truth")
DEFAULT_REPORT_DIR = os.path.join(REPO_ROOT, "reports", "factory")

#: Close reasons that must never appear on a held-to-settlement position
#: (PRD FR-1.5; mirrors tests/test_weather_lifecycle.py).
FORBIDDEN_CLOSE_REASONS = ("TIME_LIMIT", "CYCLE_RESET", "STOP_LOSS", "TAKE_PROFIT",
                           "PROFIT_TARGET", "MARKET")

#: Modules whose ``datetime`` / ``date`` / ``time`` names are rebound (seam 1).
CLOCK_PATCH_MODULES: Tuple[str, ...] = (
    "src.bots.weather_bot",
    "src.bots.mixins",
    "src.strategies.weather_strategy",
    "src.strategies.ml_weather",
    "src.core.matching_engine",
    "src.core.risk_manager",
    "src.core.weather_settlement",
)

_EMIT_RE = re.compile(r"\[Signal\] EMIT strategy=(?P<strategy>.+?) symbol=(?P<symbol>\S+) ")
_EXEC_RE = re.compile(r"\[Signal\] EXECUTED strategy=(?P<strategy>.+?) symbol=(?P<symbol>\S+) ")
_REJECT_RE = re.compile(
    r"\[Risk\] REJECT strategy=(?P<strategy>.+?) symbol=(?P<symbol>\S+) reason=(?P<reason>\S+)"
)


class DryRunError(RuntimeError):
    """Setup failure (exit 2): the run could not be constructed."""


# ---------------------------------------------------------------------------
# Seam 1: the injected clock
# ---------------------------------------------------------------------------
class DryRunClock:
    """A settable, tz-aware instant. ``now_utc`` is always UTC-aware."""

    def __init__(self, start_utc: _real_datetime) -> None:
        self._now = self._aware(start_utc)

    @staticmethod
    def _aware(dt: _real_datetime) -> _real_datetime:
        if dt.tzinfo is None:
            raise ValueError("DryRunClock needs a tz-aware datetime")
        return dt.astimezone(UTC)

    @property
    def now_utc(self) -> _real_datetime:
        return self._now

    def set(self, dt: _real_datetime) -> None:
        self._now = self._aware(dt)

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def now_et(self) -> _real_datetime:
        """The injection shape FR-F3.3 gives the bot/strategy (ET-aware)."""
        return self._now.astimezone(ET)

    def epoch(self) -> float:
        return self._now.timestamp()


class _FakeDatetimeMeta(type):
    """``isinstance(real_datetime_instance, FakeDatetime)`` must stay True."""

    def __instancecheck__(cls, obj):  # noqa: D401
        return isinstance(obj, _real_datetime)


class _FakeDateMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_date)


def _make_fake_datetime(clock: DryRunClock):
    class FakeDatetime(_real_datetime, metaclass=_FakeDatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            cur = clock.now_utc
            if tz is None:
                # Naive "now" in the host's local zone: what the real call
                # returns, so ``datetime.now().astimezone()`` round-trips.
                return cur.astimezone().replace(tzinfo=None)
            return cur.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return clock.now_utc.replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    FakeDatetime.__name__ = "datetime"
    return FakeDatetime


def _make_fake_date(clock: DryRunClock):
    class FakeDate(_real_date, metaclass=_FakeDateMeta):
        @classmethod
        def today(cls):
            return clock.now_utc.astimezone().date()

    FakeDate.__name__ = "date"
    return FakeDate


class _FakeTime:
    """``time`` module proxy: ``time()`` from the clock, ``sleep`` is a no-op."""

    def __init__(self, clock: DryRunClock) -> None:
        self._clock = clock

    def time(self) -> float:
        return self._clock.epoch()

    def sleep(self, _seconds: float) -> None:  # the acceleration
        return None

    def __getattr__(self, name):
        return getattr(_real_time, name)


def install_clock(clock: DryRunClock, modules=CLOCK_PATCH_MODULES) -> Callable[[], None]:
    """Rebind wall-clock names in ``modules`` to the fakes; returns an undo."""
    import importlib

    fake_dt = _make_fake_datetime(clock)
    fake_date = _make_fake_date(clock)
    fake_time = _FakeTime(clock)
    undo: List[Tuple[Any, str, Any]] = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
        except Exception:  # optional module (ml_weather deps) — skip
            continue
        for attr, real, fake in (
            ("datetime", _real_datetime, fake_dt),
            ("date", _real_date, fake_date),
            ("time", _real_time, fake_time),
        ):
            current = getattr(mod, attr, None)
            if current is real:
                undo.append((mod, attr, current))
                setattr(mod, attr, fake)

    def _restore() -> None:
        for mod, attr, original in reversed(undo):
            setattr(mod, attr, original)

    return _restore


def _bind_bot_clock(bot_cls, clock: DryRunClock):
    """Construct the bot, using the FR-F3.3 ``clock`` hook when it exists."""
    try:
        params = inspect.signature(bot_cls.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    injected = False
    if "clock" in params:
        bot = bot_cls(clock=clock.now_et)
        injected = True
    else:
        bot = bot_cls()
        for attr in ("clock", "_clock"):
            if hasattr(bot, attr) and callable(getattr(bot, attr)):
                setattr(bot, attr, clock.now_et)
                injected = True
                break
    return bot, injected


# ---------------------------------------------------------------------------
# Seam 2: archive replay providers
# ---------------------------------------------------------------------------
def _rel(path: str) -> str:
    """Repo-relative, forward-slash path for reports (absolute when off-repo/drive)."""
    try:
        rel = os.path.relpath(os.path.abspath(path), REPO_ROOT)
    except ValueError:  # Windows: different drive
        rel = os.path.abspath(path)
    return rel.replace("\\", "/")


def _nan_to_none(v):
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except TypeError:
        pass
    return v


def _num(v, default=0.0) -> float:
    v = _nan_to_none(v)
    return float(v) if v is not None else default


def load_city_day(city: str, date: str, ladder_root: str, source: str, lag_min: int):
    """Ladder tape + usable forecast vintages for one city-day (pandas)."""
    import pandas as pd

    import src.backtest.ev_analysis as ev

    ladders = ev.load_search_ladders(ladder_root, cities=[city], start_date=date, end_date=date)
    if ladders.empty:
        raise DryRunError(f"no archived ladder for {city} {date} under {ladder_root}")
    by_name = {s.name: s for s in ev.CANDIDATE_SOURCES}
    if source not in by_name:
        raise DryRunError(f"unknown forecast source {source!r}; have {sorted(by_name)}")
    archive = ev.load_forecast_archive(by_name[source])
    if lag_min:
        archive = archive.copy()
        archive["init_ts"] = archive["init_ts"] + pd.Timedelta(minutes=int(lag_min))
    try:
        vintages = ev.forecast_vintage_table(ladders, archive)
    except ev.EVAnalysisError:
        vintages = pd.DataFrame(
            columns=["city", "target_date", "ts_utc", "init_time_utc", "lead_hours", "forecast_high_f"]
        )
    return ladders, vintages


def load_truth_row(station: str, date: str, truth_dir: str) -> Optional[Dict[str, Any]]:
    """``{"high", "low", "high_time"}`` for ``station``/``date`` from the CLI csv."""
    import pandas as pd

    path = os.path.join(truth_dir, f"cli_daily_high_{station}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, dtype={"high_time": str})
    row = df[(df["station"] == station) & (df["date"] == date)]
    if row.empty:
        return None
    r = row.iloc[-1]
    return {
        "high": _nan_to_none(float(r["high"])) if _nan_to_none(r.get("high")) is not None else None,
        "low": _nan_to_none(float(r["low"])) if _nan_to_none(r.get("low")) is not None else None,
        "high_time": _nan_to_none(r.get("high_time")),
    }


class _MarketDataFactory:
    def __init__(self):
        from src.core.interfaces import MarketData

        self.MarketData = MarketData


class ReplayKalshi(_MarketDataFactory):
    """``fetch_market_ladder`` replays the ladder snapshot at the clock's candle."""

    read_only = True

    def __init__(self, ladders, clock: DryRunClock) -> None:
        super().__init__()
        self._clock = clock
        self._by_ts: Dict[_real_datetime, list] = {}
        for ts, grp in ladders.groupby("ts_utc", sort=True):
            self._by_ts[ts.to_pydatetime().astimezone(UTC)] = self._rows_to_market_data(grp)
        self.ladder_calls = 0
        self.orderbook_calls = 0

    def _rows_to_market_data(self, grp) -> list:
        out = []
        for r in grp.itertuples(index=False):
            ts = r.ts_utc.to_pydatetime()
            close_time = r.close_time_utc
            extra = {
                "no_bid": _num(r.no_bid),
                "no_ask": _num(r.no_ask),
                "status": "active",
                "close_time": close_time.isoformat() if hasattr(close_time, "isoformat") else None,
                "strike_type": _nan_to_none(r.strike_type),
                "floor_strike": _nan_to_none(r.floor_strike),
                "cap_strike": _nan_to_none(r.cap_strike),
                "yes_sub_title": _nan_to_none(r.yes_sub_title),
                "event_ticker": r.event_ticker,
                "open_interest": _num(getattr(r, "open_interest", None)),
                "source": "ladder_replay",
            }
            out.append(
                self.MarketData(
                    symbol=r.market_ticker,
                    timestamp=ts,
                    price=_num(r.last),
                    volume=_num(r.volume),
                    bid=_num(r.yes_bid),
                    ask=_num(r.yes_ask),
                    extra=extra,
                )
            )
        return out

    @property
    def candles(self) -> List[_real_datetime]:
        return sorted(self._by_ts)

    def _current(self) -> list:
        return self._by_ts.get(self._clock.now_utc, [])

    def fetch_market_ladder(self, series_ticker: str, statuses=None, max_pages: int = 3):
        self.ladder_calls += 1
        return [m for m in self._current() if m.symbol.startswith(series_ticker)]

    def fetch_orderbook(self, symbol: str, depth: int = 3):
        self.orderbook_calls += 1
        return {}  # no archived depth at the candle grid -> bot skips the row

    def fetch_latest(self, symbol: str):
        for m in self._current():
            if m.symbol == symbol:
                return m
        return None

    def search_markets(self, **params):
        return [], None


def _parse_high_time(text: Optional[str]) -> float:
    """``'1159 PM'`` -> 23.98 h; ``'307 AM'`` -> 3.12 h; default 15.0."""
    if not text:
        return 15.0
    m = re.match(r"^\s*(\d{1,2})(\d{2})\s*(AM|PM)\s*$", str(text).upper())
    if not m:
        return 15.0
    hour = int(m.group(1)) % 12
    if m.group(3) == "PM":
        hour += 12
    return hour + int(m.group(2)) / 60.0


class SyntheticObservations(_MarketDataFactory):
    """Seam 3: station observations reconstructed from the CLI daily low/high."""

    def __init__(self, station: str, tz_name: str, truth_dir: str, clock: DryRunClock,
                 fallback_day: str) -> None:
        super().__init__()
        self.station = station
        self.tz = ZoneInfo(tz_name)
        self.tz_name = tz_name
        self.truth_dir = truth_dir
        self._clock = clock
        self._fallback_day = fallback_day
        self._rows: Dict[str, Optional[Dict[str, Any]]] = {}

    def _row(self, day: str) -> Dict[str, Any]:
        if day not in self._rows:
            self._rows[day] = load_truth_row(self.station, day, self.truth_dir)
        row = self._rows[day]
        if row is None or row.get("high") is None:
            row = self._rows.get(self._fallback_day) or load_truth_row(
                self.station, self._fallback_day, self.truth_dir
            )
        if row is None or row.get("high") is None:
            raise DryRunError(
                f"no CLI truth for {self.station} {day} (or {self._fallback_day}) under {self.truth_dir}"
            )
        return row

    def curve(self, day: str, hour_local: float) -> Tuple[float, float]:
        """``(temperature_f, running_max_f)`` at ``hour_local`` on ``day``."""
        row = self._row(day)
        high = float(row["high"])
        low = float(row["low"]) if row.get("low") is not None else high - 12.0
        span = max(high - low, 1.0)
        t_low = 6.0
        t_high = max(_parse_high_time(row.get("high_time")), t_low + 1.0)
        start = low + 0.35 * span  # midnight value (evening cool-down)
        if hour_local <= t_low:
            temp = start + (low - start) * (hour_local / t_low)
            running = start
        elif hour_local <= t_high:
            frac = (hour_local - t_low) / (t_high - t_low)
            temp = low + span * (1 - math.cos(math.pi * frac)) / 2.0
            running = max(start, temp)
        else:
            frac = (hour_local - t_high) / max(24.0 - t_high, 0.5)
            temp = high + (start - high) * min(frac, 1.0)
            running = high
        return round(temp, 1), round(running, 1)

    def observation(self) -> Tuple[float, float, str]:
        local = self._clock.now_utc.astimezone(self.tz)
        day = local.date().isoformat()
        hour = local.hour + local.minute / 60.0
        temp, running = self.curve(day, hour)
        return temp, running, day

    def fetch_latest(self, symbol: str):
        temp, running, day = self.observation()
        return self.MarketData(
            symbol=symbol,
            timestamp=self._clock.now_utc,
            price=temp,
            volume=0.0,
            bid=0.0,
            ask=0.0,
            extra={
                "temperature_f": temp,
                "temperature_c": round((temp - 32.0) * 5.0 / 9.0, 2),
                "max_temp_today_f": running,
                "max_temp_local_day": day,
                "source": "live_metar",
                "metar_age_seconds": 0.0,
                "settlement_station": symbol,
                "station_timezone": self.tz_name,
                "provenance": "dry_run:cli_daily_curve",
            },
        )


class ReplayNWS(_MarketDataFactory):
    """``fetch_latest`` = observation + the archived forecast vintage as NWS periods."""

    def __init__(self, obs: SyntheticObservations, vintages, city: str, target_date: str,
                 clock: DryRunClock) -> None:
        super().__init__()
        self._obs = obs
        self._clock = clock
        self._target_date = target_date
        self._vintages: Dict[_real_datetime, Dict[str, Any]] = {}
        if vintages is not None and len(vintages):
            sub = vintages[(vintages["city"] == city) & (vintages["target_date"] == target_date)]
            for r in sub.itertuples(index=False):
                self._vintages[r.ts_utc.to_pydatetime().astimezone(UTC)] = {
                    "init_time_utc": str(r.init_time_utc),
                    "lead_hours": int(r.lead_hours),
                    "forecast_high_f": float(r.forecast_high_f),
                }
        self.vintage_hits = 0
        self.vintage_misses = 0

    def vintage_at(self, ts: _real_datetime) -> Optional[Dict[str, Any]]:
        return self._vintages.get(ts.astimezone(UTC))

    def fetch_latest(self, symbol: str):
        temp, running, day = self._obs.observation()
        vintage = self.vintage_at(self._clock.now_utc)
        if vintage is None:
            self.vintage_misses += 1
            periods = None
        else:
            self.vintage_hits += 1
            target = _real_datetime.fromisoformat(self._target_date)
            periods = [
                {
                    "number": 1,
                    "name": "Today" if day == self._target_date else "Tomorrow",
                    "isDaytime": True,
                    "temperature": int(round(vintage["forecast_high_f"])),
                    "temperatureUnit": "F",
                    "probabilityOfPrecipitation": {"value": 0},
                    "startTime": target.strftime("%Y-%m-%dT06:00:00"),
                    "endTime": target.strftime("%Y-%m-%dT18:00:00"),
                    "detailedForecast": (
                        f"gfs_mex vintage {vintage['init_time_utc']} "
                        f"lead {vintage['lead_hours']}h (archive replay)"
                    ),
                }
            ]
        return self.MarketData(
            symbol=symbol,
            timestamp=self._clock.now_utc,
            price=temp,
            volume=0.0,
            bid=0.0,
            ask=0.0,
            extra={
                "temperature_f": temp,
                "max_temp_today_f": running,
                "source": "live_nws",
                "forecast": periods,
                "settlement_station": symbol,
                "station_timezone": self._obs.tz_name,
                "max_temp_local_day": day,
            },
        )


class RecordingDashboard:
    """The duck-typed surface ``WeatherBot.tick`` / ``_process_signals`` use."""

    def __init__(self) -> None:
        self.prices = 0
        self.depth_rows = 0
        self.logs: List[str] = []
        self.signals: List[Tuple[str, str, str]] = []
        self.alerts: List[str] = []

    def update_price(self, name, price, **kwargs):
        self.prices += 1

    def record_depth(self, symbol, book, **kwargs):
        self.depth_rows += 1

    def log(self, message):
        self.logs.append(str(message))

    def record_signal(self, sig, status="", strategy_name=""):
        self.signals.append((strategy_name, getattr(sig, "symbol", "?"), status))

    def alert(self, message):
        self.alerts.append(str(message))


class _OfflineTruthProvider:
    """Stands in for IEMCLIProvider: never reaches the network."""

    def __init__(self):
        self.calls = 0

    def fetch_daily_high(self, station, date, **kwargs):
        self.calls += 1
        return None


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: List[str] = []

    def emit(self, record):
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Genome strategy (STRATEGY workstream) -- best-effort adapter
# ---------------------------------------------------------------------------
def build_genome_strategy(spec_path: str, clock: DryRunClock, vintages, lag_min: int):
    """Construct ``GenomeStrategy`` from a promoted spec for archive replay.

    Returns ``(strategy, info)``; raises :class:`DryRunError` when the module
    or a constructor input is unavailable in this checkout.  The construction
    order mirrors the F3 contract (``GenomeStrategy(spec, clock=, forecast_provider=,
    fee_regime=, calibration_provider=)``), trying a replay helper from
    ``scripts/factory_replay_parity.py`` first if STRATEGY ships one.
    """
    info: Dict[str, Any] = {"spec_path": _rel(spec_path)}
    try:
        from src.factory.promoted import load_promoted
    except ImportError as exc:
        raise DryRunError(f"src.factory.promoted not importable: {exc}")
    try:
        from src.strategies import genome_strategy as gs_mod
    except ImportError as exc:
        raise DryRunError(f"src.strategies.genome_strategy not importable: {exc}")

    spec = load_promoted(spec_path)
    info["genome_id"] = getattr(spec, "genome_id", None)
    info["spec_mode"] = getattr(spec, "mode", None)

    # 1. A replay factory shipped by the parity script, if any.
    try:
        from scripts import factory_replay_parity as parity  # type: ignore

        for helper in ("build_replay_strategy", "replay_strategy", "strategy_for_replay"):
            fn = getattr(parity, helper, None)
            if callable(fn):
                strategy = fn(spec, clock=clock.now_et, vintage_table=vintages)
                info["constructed_via"] = f"factory_replay_parity.{helper}"
                return strategy, info
    except ImportError:
        pass
    except TypeError:
        pass

    # 2. The contract constructor with replay inputs.
    errors: List[str] = []
    provider = None
    try:
        from src.data import forecast_vintage_provider as fvp

        attempts = (
            lambda: fvp.ForecastVintageProvider.from_table(vintages, lag_min=lag_min),
            lambda: fvp.ForecastVintageProvider(vintages, source="replay", lag_min=lag_min),
            lambda: fvp.ForecastVintageProvider(table=vintages, source="replay", lag_min=lag_min),
        )
        for attempt in attempts:
            try:
                provider = attempt()
                break
            except (TypeError, AttributeError) as exc:
                errors.append(f"provider: {exc}")
    except ImportError as exc:
        errors.append(f"forecast_vintage_provider: {exc}")
    if provider is None:
        raise DryRunError("could not build a replay ForecastVintageProvider: " + "; ".join(errors))

    from src.factory.fees import load_regime

    regime = load_regime()
    calibration = None
    for name in ("CalibrationProvider", "calibration_provider_for", "load_calibration_provider"):
        ctor = getattr(gs_mod, name, None)
        if ctor is None:
            continue
        try:
            calibration = ctor(spec)
            break
        except TypeError:
            try:
                calibration = ctor(getattr(spec.calibration, "dir", None))
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"calibration via {name}: {exc}")
    if calibration is None:
        raise DryRunError(
            "no calibration provider constructor found on genome_strategy "
            "(tried CalibrationProvider / calibration_provider_for / load_calibration_provider): "
            + "; ".join(errors)
        )
    strategy = gs_mod.GenomeStrategy(
        spec,
        clock=clock.now_et,
        forecast_provider=provider,
        fee_regime=regime,
        calibration_provider=calibration,
    )
    info["constructed_via"] = "GenomeStrategy(contract constructor)"
    return strategy, info


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _wrap_analyze(strategy, name: str, sink: List[Tuple[str, Any]], counter: Dict[str, int]):
    original = strategy.analyze

    def analyze(data, *a, **kw):
        counter[name] = counter.get(name, 0) + 1
        result = original(data, *a, **kw)
        signals = result if isinstance(result, list) else ([result] if result else [])
        for sig in signals:
            sink.append((name, sig))
        return result

    strategy.analyze = analyze


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, _real_datetime):
        return obj.isoformat()
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, Path):
        return str(obj)
    return obj


def write_report(path: Path, report: Dict[str, Any]) -> None:
    """``sort_keys=True, indent=2``, trailing newline (factory JSON house style)."""
    try:
        from src.factory.report import write_json

        write_json(Path(path), _json_safe(report))
        return
    except Exception:  # pandas-free fallback; identical bytes
        pass
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_json_safe(report), sort_keys=True, indent=2)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")


def run_dry_run(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    """Execute the dry run; returns ``(report, exit_code)``. Never raises for assertions."""
    from src.bots import weather_bot as wb
    from src.core import weather_settlement as ws
    import src.core.risk_manager as rm_mod

    city_key = args.city.upper()
    if city_key not in wb.CITY_CONFIG:
        raise DryRunError(f"unknown city {args.city!r}; have {sorted(wb.CITY_CONFIG)}")
    city = wb.CITY_CONFIG[city_key]
    station = city.settlement_station

    ladders, vintages = load_city_day(city_key, args.date, args.ladder_root, args.source, args.availability_lag_min)
    truth_row = load_truth_row(station, args.date, args.truth_dir)
    ladder_high = _nan_to_none(ladders["cli_high"].dropna().iloc[0]) if ladders["cli_high"].notna().any() else None
    truth_high = truth_row["high"] if truth_row and truth_row.get("high") is not None else ladder_high
    if truth_high is None:
        raise DryRunError(f"no settlement truth for {station} {args.date} (truth csv or ladder cli_high)")

    tmp_dir = tempfile.mkdtemp(prefix=f"dry_run_{city_key}_{args.date}_")
    state_dir = Path(tmp_dir)
    candles: List[_real_datetime] = []
    clock = DryRunClock(ladders["ts_utc"].min().to_pydatetime())

    log_capture = _LogCapture()
    mp_logger = logging.getLogger("MoneyPrinter")
    mp_logger.addHandler(log_capture)

    restore_clock = install_clock(clock)
    saved_state_file = rm_mod._DEFAULT_STATE_FILE
    saved_win_rates = rm_mod.WIN_RATES_PATH
    saved_cache_path = ws.SETTLEMENT_CACHE_PATH
    saved_mode = os.environ.get("GENOME_STRATEGY_MODE")
    report: Dict[str, Any] = {
        "city": city_key,
        "date": args.date,
        "station": station,
        "series": city.kalshi_series,
        "ladder_root": _rel(args.ladder_root),
        "forecast_source": args.source,
        "availability_lag_min": int(args.availability_lag_min),
        "sim_balance": float(args.sim_balance),
        "weather_trading_enabled": bool(wb.WEATHER_TRADING_ENABLED),
        "ml_weather_enabled": bool(wb.ML_WEATHER_ENABLED),
        "seams": [
            "clock: module datetime/date/time rebinding (+bot clock hook when present)",
            "providers: archive replay stubs for kalshi/nws/metar",
            "observations: synthetic diurnal curve from CLI daily low/high",
            "truth: settlement cache redirected to a temp file, IEM client stubbed",
            "state: exchange_state/win_rates redirected to a temp dir",
        ],
    }
    assertions: Dict[str, Dict[str, Any]] = {}

    def check(name: str, ok: bool, detail: Any = None) -> None:
        assertions[name] = {"ok": bool(ok), "detail": _json_safe(detail)}

    try:
        # --- state + truth isolation (seams 4, 5) ---
        rm_mod._DEFAULT_STATE_FILE = state_dir / "exchange_state.json"
        rm_mod.WIN_RATES_PATH = str(state_dir / "strategy_win_rates.json")
        cache_path = state_dir / "settlement_cache.json"
        cache_path.write_text(json.dumps({"truth": {}, "markets": {}}), encoding="utf-8")
        ws.SETTLEMENT_CACHE_PATH = str(cache_path)
        ws.reset_caches()
        offline_truth = _OfflineTruthProvider()
        ws._provider = offline_truth

        if args.genome_mode:
            os.environ["GENOME_STRATEGY_MODE"] = args.genome_mode

        # --- the real components ---
        dashboard = RecordingDashboard()
        risk = rm_mod.RiskManager(starting_balance=float(args.sim_balance), persist_state=True)
        exchange = risk.exchange
        exchange.on_alert = dashboard.alert
        report["exchange_state_file_in_temp"] = str(exchange._state_file).startswith(str(state_dir)) if getattr(exchange, "_state_file", None) else False

        bot, clock_injected = _bind_bot_clock(wb.WeatherBot, clock)
        bot.CITIES = (city,)
        kalshi = ReplayKalshi(ladders, clock)
        obs = SyntheticObservations(station, city.timezone, args.truth_dir, clock, args.date)
        nws = ReplayNWS(obs, vintages, city_key, args.date, clock)
        bot.kalshi = kalshi
        bot.nws = nws
        bot.metar = obs
        report["bot_clock_injected"] = clock_injected

        genome_info: Dict[str, Any] = {"requested": bool(args.genome_spec)}
        if args.genome_spec:
            strategy, info = build_genome_strategy(
                os.path.abspath(args.genome_spec), clock, vintages, args.availability_lag_min
            )
            genome_info.update(info)
            genome_info["mode"] = args.genome_mode
            # FR-F3.3 insertion: first in declared order, before V2.
            bot.strategies = {"genome": strategy, **bot.strategies}
        report["genome"] = genome_info

        emitted: List[Tuple[str, Any]] = []
        analyze_calls: Dict[str, int] = {}
        for name, strategy in list(bot.strategies.items()):
            _wrap_analyze(strategy, name, emitted, analyze_calls)

        settled_via_helper: List[int] = []
        original_settle = exchange._settle_weather_position

        def counting_settle(pos):
            ok = original_settle(pos)
            if ok:
                settled_via_helper.append(pos.get("id"))
            return ok

        exchange._settle_weather_position = counting_settle

        # --- drive the candles ---
        candles = kalshi.candles
        opened_ids: Dict[int, Dict[str, Any]] = {}
        per_candle: List[Dict[str, Any]] = []
        for ts in candles:
            clock.set(ts)
            n_sig_before = len(emitted)
            bot.tick(risk, dashboard)
            for pos in exchange.positions:
                if pos["id"] not in opened_ids:
                    opened_ids[pos["id"]] = {
                        "symbol": pos["symbol"],
                        "contract_side": pos.get("contract_side"),
                        "quantity": pos.get("quantity"),
                        "entry_price": pos.get("entry_price"),
                        "strategy_name": pos.get("strategy_name"),
                        "expiration_time": pos.get("expiration_time"),
                        "opened_at_candle": ts,
                    }
            per_candle.append(
                {
                    "ts_utc": ts,
                    "signals": len(emitted) - n_sig_before,
                    "open_positions": len(exchange.positions),
                }
            )

        # --- past the settlement close: publish truth, sweep ---
        symbols = sorted(ladders["market_ticker"].unique())
        closes = {ws.settlement_close_for(s) for s in symbols}
        closes.discard(None)
        if not closes:
            raise DryRunError("settlement_close_for returned None for every market")
        settlement_close = max(closes)
        pending_before = [p["id"] for p in exchange.positions]
        clock.set(max(settlement_close, candles[-1]) + timedelta(hours=1))
        blob = json.loads(cache_path.read_text(encoding="utf-8"))
        truth_key = f"{station}|{args.date}"
        blob["truth"][truth_key] = {"high": float(truth_high), "source": "dry_run:cli_daily_high"}
        cache_path.write_text(json.dumps(blob), encoding="utf-8")
        ws._miss_log.clear()
        risk.update_market_data(f"TEMP_{station}", float(truth_high))

        # --- assertions ---
        exp_details = []
        exp_ok = True
        for name, sig in emitted:
            expected = ws.settlement_close_for(sig.symbol)
            exp = getattr(sig, "expiration_time", None)
            aware = exp is not None and exp.tzinfo is not None and exp.utcoffset() is not None
            equal = aware and expected is not None and exp == expected
            exp_ok &= bool(equal)
            exp_details.append(
                {"strategy": name, "symbol": sig.symbol, "expiration_time": exp,
                 "expected": expected, "tz_aware": aware, "equal": bool(equal)}
            )
        check("every_signal_has_settlement_close_expiration", exp_ok, exp_details)

        closed_by_id = {t.get("id"): t for t in exchange.closed_trades}
        settle_details = []
        settle_ok = True
        for pid, meta in opened_ids.items():
            trade = closed_by_id.get(pid)
            via_helper = pid in settled_via_helper
            reason = trade.get("reason") if trade else None
            good = (
                trade is not None
                and reason == "EXPIRATION"
                and via_helper
                and trade.get("settlement_high") is not None
                and not str(reason).startswith(FORBIDDEN_CLOSE_REASONS)
            )
            settle_ok &= bool(good)
            settle_details.append(
                {
                    "id": pid,
                    "symbol": meta["symbol"],
                    "contract_side": meta["contract_side"],
                    "quantity": meta["quantity"],
                    "entry_price": meta["entry_price"],
                    "strategy_name": meta["strategy_name"],
                    "opened_at_candle": meta["opened_at_candle"],
                    "closed": trade is not None,
                    "reason": reason,
                    "via_settle_weather_position": via_helper,
                    "exit_price": trade.get("exit_price") if trade else None,
                    "pnl": trade.get("pnl") if trade else None,
                    "settlement_high": trade.get("settlement_high") if trade else None,
                    "settlement_outcome": trade.get("settlement_outcome") if trade else None,
                    "settlement_rule": trade.get("settlement_rule") if trade else None,
                }
            )
        check("every_position_settled_via_settle_weather_position", settle_ok, settle_details)
        check("no_position_remains_open", not exchange.positions,
              [p["symbol"] for p in exchange.positions])

        # Settlement PnL must follow the contract side: a BUY NO position pays
        # 1.00 when the bracket settles "no". ``_close_position`` books
        # ``exit_price = 1.00 if outcome_is_yes else 0.00`` (a YES-side price)
        # against the NO-side entry price for ``side == "buy"`` regardless of
        # ``contract_side`` (matching_engine.py, "CALCULATE PNL"), while the
        # mark-to-market sweep does invert for NO. Found by this dry run on
        # NY 2026-07-20 (BUY NO B79.5 @0.33, settled "no", booked -16.50
        # instead of +33.50). Protected file -- reported, not fixed here.
        pnl_details = []
        pnl_ok = True
        for t in exchange.closed_trades:
            if t.get("reason") != "EXPIRATION" or t.get("settlement_outcome") not in ("yes", "no"):
                continue
            side_yes = str(t.get("contract_side", "YES")).upper() == "YES"
            won = side_yes == (t["settlement_outcome"] == "yes")
            payoff = 1.0 if won else 0.0
            qty = float(t.get("quantity", 0))
            entry = float(t.get("entry_price", 0.0))
            exit_fee = float(t.get("exit_fee", 0.0) or 0.0)
            if str(t.get("side", "buy")) == "buy":
                expected_pnl = (payoff - entry) * qty - exit_fee
            else:
                expected_pnl = (entry - payoff) * qty - exit_fee
            booked = float(t.get("pnl", 0.0))
            good = abs(expected_pnl - booked) < 1e-6
            pnl_ok &= good
            pnl_details.append(
                {"id": t.get("id"), "symbol": t.get("symbol"), "contract_side": t.get("contract_side"),
                 "settlement_outcome": t.get("settlement_outcome"), "entry_price": entry,
                 "booked_exit_price": t.get("exit_price"), "booked_pnl": booked,
                 "expected_pnl": expected_pnl, "ok": good}
            )
        check("settlement_pnl_matches_contract_side", pnl_ok, pnl_details)
        check("held_open_until_truth_published", len(pending_before) == len(opened_ids),
              {"open_at_close": pending_before, "opened": sorted(opened_ids)})

        # FR-0.4: every EMIT resolves to exactly one EXECUTED or REJECT line.
        pending: Dict[Tuple[str, str], int] = {}
        orphans = 0
        rejects_by_code: Dict[str, int] = {}
        n_emit = n_exec = 0
        for msg in log_capture.messages:
            m = _EMIT_RE.search(msg)
            if m:
                n_emit += 1
                key = (m.group("strategy"), m.group("symbol"))
                pending[key] = pending.get(key, 0) + 1
                continue
            m = _EXEC_RE.search(msg)
            if m:
                n_exec += 1
                key = (m.group("strategy"), m.group("symbol"))
                if pending.get(key):
                    pending[key] -= 1
                else:
                    orphans += 1
                continue
            m = _REJECT_RE.search(msg)
            if m:
                code = m.group("reason")
                rejects_by_code[code] = rejects_by_code.get(code, 0) + 1
                key = (m.group("strategy"), m.group("symbol"))
                if pending.get(key):
                    pending[key] -= 1
        unresolved = sum(pending.values())
        check("fr04_every_emit_has_one_outcome", unresolved == 0 and orphans == 0,
              {"unresolved_emits": unresolved, "orphan_executions": orphans})
        if args.require_position:
            check("at_least_one_position_opened", bool(opened_ids), len(opened_ids))

        stats = exchange.get_stats() if hasattr(exchange, "get_stats") else {}
        report.update(
            {
                "n_candles": len(candles),
                "first_candle_utc": candles[0],
                "last_candle_utc": candles[-1],
                "settlement_close": settlement_close,
                "truth": {
                    "key": truth_key,
                    "high": float(truth_high),
                    "ladder_cli_high": ladder_high,
                    "truth_csv_high": truth_row["high"] if truth_row else None,
                    "agree": (ladder_high is None) or (truth_row is None)
                    or (truth_row.get("high") is None) or float(ladder_high) == float(truth_row["high"]),
                    "iem_network_calls": offline_truth.calls,
                },
                "strategies": list(bot.strategies.keys()),
                "analyze_calls": analyze_calls,
                "forecast_vintages": {"hits": nws.vintage_hits, "misses": nws.vintage_misses},
                "signals": {
                    "emitted": len(emitted),
                    "by_strategy": {k: sum(1 for n, _ in emitted if n == k) for k in bot.strategies},
                    "emit_lines": n_emit,
                    "executed_lines": n_exec,
                    "rejected_by_code": dict(sorted(rejects_by_code.items())),
                },
                "positions": {"opened": len(opened_ids), "settled": len(settled_via_helper),
                              "open_at_end": len(exchange.positions)},
                "pnl": {
                    "realized": stats.get("realized", getattr(exchange, "realized_pnl", None)),
                    "total_fees": getattr(exchange, "total_fees_paid", None),
                    "closed_trades": [
                        {k: t.get(k) for k in ("id", "symbol", "contract_side", "quantity", "entry_price",
                                               "exit_price", "pnl", "reason", "settlement_high",
                                               "settlement_outcome")}
                        for t in exchange.closed_trades
                    ],
                },
                "dashboard": {"price_updates": dashboard.prices, "alerts": dashboard.alerts,
                              "signals_recorded": len(dashboard.signals)},
                "per_candle": per_candle,
                "assertions": assertions,
            }
        )
        report["ok"] = all(a["ok"] for a in assertions.values())
        return report, (EXIT_OK if report["ok"] else EXIT_ASSERTION)
    finally:
        restore_clock()
        rm_mod._DEFAULT_STATE_FILE = saved_state_file
        rm_mod.WIN_RATES_PATH = saved_win_rates
        ws.SETTLEMENT_CACHE_PATH = saved_cache_path
        ws.reset_caches()
        ws._miss_log.clear()
        if saved_mode is None:
            os.environ.pop("GENOME_STRATEGY_MODE", None)
        else:
            os.environ["GENOME_STRATEGY_MODE"] = saved_mode
        mp_logger.removeHandler(log_capture)


def default_report_path(city: str, date: str) -> Path:
    return Path(DEFAULT_REPORT_DIR) / f"dry_run_{city.upper()}_{date}.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--city", required=True, help="NY | CHI | LAX | MIA")
    p.add_argument("--date", required=True, help="target_date YYYY-MM-DD (an archived ladder day)")
    p.add_argument("--ladder-root", default=DEFAULT_LADDER_ROOT)
    p.add_argument("--truth-dir", default=DEFAULT_TRUTH_DIR)
    p.add_argument("--source", default="gfs_mex", help="forecast archive source (gfs_mex | gefs)")
    p.add_argument("--availability-lag-min", type=int, default=240,
                   help="vintage availability lag applied at the join (frame default 240)")
    p.add_argument("--sim-balance", type=float, default=3000.0)
    p.add_argument("--genome-spec", default=None,
                   help="configs/factory/promoted/<id>.json -> insert GenomeStrategy before V2")
    p.add_argument("--genome-mode", default="shadow", choices=("shadow", "paper"),
                   help="exported as GENOME_STRATEGY_MODE for the bot (default shadow)")
    p.add_argument("--require-position", action="store_true",
                   help="fail (exit 1) unless at least one position was opened")
    p.add_argument("--out", default=None, help="report path (default reports/factory/dry_run_<city>_<date>.json)")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out) if args.out else default_report_path(args.city, args.date)
    try:
        report, code = run_dry_run(args)
    except DryRunError as exc:
        print(f"[dry_run] UNAVAILABLE: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    write_report(out, report)
    if not args.quiet:
        sig = report["signals"]
        pos = report["positions"]
        print(
            f"[dry_run] {report['city']} {report['date']}: {report['n_candles']} candles, "
            f"{sig['emitted']} signals ({sig['executed_lines']} executed, "
            f"{sum(sig['rejected_by_code'].values())} rejected), "
            f"{pos['opened']} positions opened, {pos['settled']} settled, "
            f"{pos['open_at_end']} open at end; realized={report['pnl']['realized']}"
        )
        for name, a in report["assertions"].items():
            print(f"[dry_run]   {'PASS' if a['ok'] else 'FAIL'} {name}")
        print(f"[dry_run] report -> {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
