import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

from src.bots.base import Bot
from src.bots.registry import BotRegistry
from src.bots.mixins import TickerResolverMixin, SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.core.risk_manager import log_rejection
from src.strategies.weather_strategy import WeatherArbitrageStrategyV2
from src.strategies.ml_weather import MLWeatherStrategy
from src.data.nws_provider import NWSProvider
from src.data.metar_provider import METARProvider
from src.utils.logger import logger
import os


# 2026-09-01 revival decision (revival/pleiades-2026-09): weather PAPER trading
# is re-enabled on the sandbox to exercise the settlement leg through the
# simulator — the one path HANDOFF.md §2 flags as untested against live data
# ("No weather position has ever been opened") — and to generate real PnL/
# time-history for the dashboard. This is NOT a reversal of the Phase 2 HALT:
# no capital verdict changed, and live capital remains structurally impossible
# (KalshiProvider is read_only=True everywhere; place_order raises; all
# execution is SimulatedExchange). History: disabled 2026-06-03 (LEAN review,
# fee bleed) and kept off through the feed-only Phases 1-3.
WEATHER_TRADING_ENABLED = True

# 2026-09-02 (PRD_STRATEGY_FACTORY FR-F0.2): ML Weather is OFF by default and
# owner-only. On the sandbox every executed ML Weather signal carried
# confidence=1.000 because src/strategies/ml_weather.py:251 defaults
# ``hrrr_forecast`` to the NWS high, so the predictor's analytical fallback
# (src/ml/predictor.py:598, ``confidence = max(0.2, 1 - spread/10)``) compared
# a forecast to itself: spread 0 -> confidence 1.0 -> Kelly max -> the
# 50-contract hard cap on every signal, with an implied sigma of 0.5F.
# When False, MLWeatherStrategy is not constructed and the tick goes straight
# to WeatherArbitrageStrategyV2 ("Meteorologist V2"). When True the waterfall
# is exactly what it was before this flag existed. See HANDOFF.md §8.
ML_WEATHER_ENABLED = False

# 2026-09-04 (PRD_STRATEGY_FACTORY FR-F3.3): a promoted factory genome enters
# the waterfall FIRST when ``GENOME_STRATEGY_ID`` names a spec under
# ``configs/factory/promoted/``. The bot injects the clock
# (``datetime.now(ET)`` -- the strategy itself never reads a wall clock), the
# live forecast-vintage provider and the frozen calibration; the strategy
# refuses to construct on a fee-type / calibration-hash mismatch.
# ``GENOME_STRATEGY_MODE=shadow`` (or ``spec.mode == "shadow"``) means the
# bot logs the ``[Signal] EMIT`` line exactly as for any strategy and then
# exactly one ``REJECT ... reason=GENOME_SHADOW`` line WITHOUT handing the
# signal to ``_process_signals`` -- nothing can paper-trade a CLOSED genome.
# The env can only tighten (a "paper" env never overrides a shadow spec).
GENOME_STRATEGY_ID_ENV = "GENOME_STRATEGY_ID"
GENOME_STRATEGY_MODE_ENV = "GENOME_STRATEGY_MODE"
GENOME_SHADOW = "shadow"
GENOME_STRATEGY_KEY = "genome"

# Waterfall key -> the strategy_name that appears in EMIT/EXECUTED/REJECT lines.
STRATEGY_LABELS: Dict[str, str] = {
    "ml_weather": "ML Weather",
    "weather": "Meteorologist V2",
}

# Kalshi dates a weather event by its settlement day in New York time
# (``KXHIGHNY-26SEP01-...``); the tracked-date window must be computed on
# that clock, not the host's (maia runs TZ=UTC). Same convention as
# src/strategies/weather_strategy.py and ml_weather.py (commit 98fd8b1).
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class WeatherCity:
    """One tracked city: market, settlement station, local clock.

    This is the single authoritative per-city record (PRD FR-1.4). It replaced
    four overlapping maps (``STATION_MAP``, ``METAR_STATIONS``,
    ``METAR_TO_KALSHI``, ``METAR_TO_NWS``) whose disagreement is exactly how
    the bot ended up observing the non-settlement airports KJFK/KORD while
    Kalshi settled on KNYC/KMDW.
    """

    key: str  # short city key used in logs and dashboard rows
    kalshi_series: str  # Kalshi series ticker, e.g. "KXHIGHNY"
    settlement_station: str  # the station Kalshi settles the market on
    timezone: str  # IANA tz of the settlement station (PRD FR-3.2)
    station_name: str  # human name, as published by api.weather.gov


# PRD FR-1.4. Settlement stations, NOT nearby airports. The non-settlement
# airports named below are documentation of the defect being fixed; they are
# not station identifiers anywhere in this process:
#   KXHIGHNY  settles on KNYC (Central Park), not the non-settlement KJFK
#   KXHIGHCHI settles on KMDW (Midway),       not the non-settlement KORD
# Measured 2026-07-12..07-25 from the IEM archive, the settlement station and
# its old airport proxy differ by up to 3F (NY) and 2F (CHI) on a daily high —
# enough to flip a 2F-wide Kalshi bracket on roughly half of all days.
# Timezones are the ``timeZone`` field of https://api.weather.gov/stations/<id>
# (probed 2026-07-25) and are consumed by the local-calendar-day running max
# now, and by the Phase 3 local-time trade windows (FR-3.2) later.
WEATHER_CITIES: Tuple[WeatherCity, ...] = (
    WeatherCity(
        key="NY",
        kalshi_series="KXHIGHNY",
        settlement_station="KNYC",
        timezone="America/New_York",
        station_name="New York City, Central Park",
    ),
    WeatherCity(
        key="CHI",
        kalshi_series="KXHIGHCHI",
        settlement_station="KMDW",
        timezone="America/Chicago",
        station_name="Chicago, Chicago Midway Airport",
    ),
    WeatherCity(
        key="LAX",
        kalshi_series="KXHIGHLAX",
        settlement_station="KLAX",
        timezone="America/Los_Angeles",
        station_name="Los Angeles, Los Angeles International Airport",
    ),
    WeatherCity(
        key="MIA",
        kalshi_series="KXHIGHMIA",
        settlement_station="KMIA",
        timezone="America/New_York",
        station_name="Miami, Miami International Airport",
    ),
)

CITY_CONFIG: Dict[str, WeatherCity] = MappingProxyType(
    {c.key: c for c in WEATHER_CITIES}
)
SETTLEMENT_STATIONS: Tuple[str, ...] = tuple(
    c.settlement_station for c in WEATHER_CITIES
)
STATION_TIMEZONES: Dict[str, str] = MappingProxyType(
    {c.settlement_station: c.timezone for c in WEATHER_CITIES}
)
SERIES_BY_STATION: Dict[str, str] = MappingProxyType(
    {c.settlement_station: c.kalshi_series for c in WEATHER_CITIES}
)

# Bracket-semantics fields the FR-1.1 KalshiProvider puts on every market's
# ``extra``. They are forwarded onto the fused observation so strategies can
# call ``parse_bracket_spec(symbol, data.extra)`` without a second fetch.
BRACKET_FIELDS: Tuple[str, ...] = (
    "floor_strike",
    "cap_strike",
    "strike_type",
    "yes_sub_title",
)


@BotRegistry.register("weather")
class WeatherBot(Bot, TickerResolverMixin, SignalProcessorMixin):
    """Feed-only weather bot reading Kalshi's settlement stations (FR-1.4)."""

    # The authoritative city list. Instance-overridable so tests can run a
    # single city; there is no second station map to fall out of sync with.
    CITIES: Tuple[WeatherCity, ...] = WEATHER_CITIES

    # FR-0.7 harvester cadence: orderbook depth snapshots are HOURLY only —
    # the per-tick path must never issue per-market orderbook calls.
    DEPTH_SNAPSHOT_INTERVAL_S = 3600
    # Safety cap on orderbook calls per city per hourly snapshot pass.
    MAX_DEPTH_MARKETS_PER_CITY = 30

    def __init__(self):
        Bot.__init__(self, name="Weather")
        TickerResolverMixin.__init__(self)
        self.kalshi = None
        self.nws = None
        self.metar = None
        # Epoch of last hourly orderbook-depth snapshot (0 = snapshot on
        # first tick so a fresh session records a baseline immediately).
        self._last_depth_snapshot = 0.0

        # Waterfall, in declared order (FR-F3.3): [GenomeStrategy when
        # GENOME_STRATEGY_ID is set] -> [ML Weather, owner-only via
        # ML_WEATHER_ENABLED] -> V2 rule-based. Dict order is the waterfall
        # order; tick() loops over it and stops at the first strategy that
        # trades. ``self.genome_shadow`` is True when the genome's signals are
        # logged (EMIT + GENOME_SHADOW) but never reach _process_signals.
        self.strategies = {}
        self.genome_shadow = False
        self.genome_spec = None
        genome_id = (os.getenv(GENOME_STRATEGY_ID_ENV) or "").strip()
        if genome_id:
            genome_strategy = self._build_genome_strategy(genome_id)
            if genome_strategy is not None:  # None = refused (logged); the bot runs V2 only
                self.strategies[GENOME_STRATEGY_KEY] = genome_strategy
        if ML_WEATHER_ENABLED:
            self.strategies["ml_weather"] = MLWeatherStrategy()
        self.strategies["weather"] = WeatherArbitrageStrategyV2()

    # ── GenomeStrategy wiring (FR-F3.3) ─────────────────────────────

    @staticmethod
    def _genome_clock():
        """The ONE wall-clock read the genome path makes -- in the bot, never in the strategy."""
        return datetime.now(ET)

    def _build_genome_strategy(self, genome_id: str):
        """Construct ``GenomeStrategy`` from a promoted spec; fail fast on any mismatch.

        Imports are deferred so a sandbox without ``GENOME_STRATEGY_ID`` never
        touches the factory modules (``tests/test_factory_isolation.py``).
        """
        from src.data.forecast_vintage_provider import (
            CACHE_DIR_ENV,
            DEFAULT_CACHE_DIR,
            ForecastVintageProvider,
        )
        from src.data.mos_guidance_provider import MOSGuidanceProvider
        from src.factory import fees as fees_mod
        from src.factory.promoted import REPO_ROOT as _REPO_ROOT
        from src.factory.promoted import load_promoted
        from src.strategies.genome_strategy import FrozenCalibrationProvider, GenomeStrategy

        spec = load_promoted(genome_id)
        env_mode = (os.getenv(GENOME_STRATEGY_MODE_ENV) or "").strip().lower()
        # Authorization (F3 red team, 2026-09-05): the env can only TIGHTEN a
        # spec to shadow; asking for paper on a shadow spec is a configuration
        # error and the genome is refused outright rather than silently run in
        # shadow. Paper mode additionally needs the family's CURRENT registry
        # status (reports/factory/registry.jsonl, tracked, shipped in the image)
        # to be PROPOSED/RATIFIED and to match the spec -- spec_hash is
        # integrity, not authorization.
        if env_mode == "paper" and spec.mode == GENOME_SHADOW:
            logger.error(
                "[Weather] GenomeStrategy REFUSED: GENOME_STRATEGY_MODE=paper but spec %s is mode=shadow "
                "(promote it with --mode paper once the family is PROPOSED); running V2 only",
                spec.genome_id,
            )
            self.genome_shadow = False
            self.genome_spec = None
            return None
        self.genome_shadow = spec.mode == GENOME_SHADOW or env_mode == GENOME_SHADOW
        if not self.genome_shadow:
            registry_status = self._registry_status(spec.family)
            if registry_status not in ("PROPOSED", "RATIFIED") or registry_status != spec.registry_status:
                logger.error(
                    "[Weather] GenomeStrategy REFUSED paper mode: family %s registry status is %s, spec says %s "
                    "(paper requires PROPOSED/RATIFIED and a matching spec); running V2 only",
                    spec.family, registry_status, spec.registry_status,
                )
                self.genome_spec = None
                return None
        self.genome_spec = spec
        # Repo-relative spec paths resolve against the checkout, never the CWD.
        cal_dir = spec.calibration.dir
        if not os.path.isabs(cal_dir):
            cal_dir = os.path.join(_REPO_ROOT, cal_dir)
        cache_dir = os.getenv(CACHE_DIR_ENV) or DEFAULT_CACHE_DIR
        mos = MOSGuidanceProvider(cache_dir=os.path.join(cache_dir, "mos"))
        provider = ForecastVintageProvider.live(
            mos,
            clock=self._genome_clock,
            lag_min=spec.availability_lag_min,
            cache_dir=cache_dir,
            forecast_source=spec.forecast_source,
        )
        calibration = FrozenCalibrationProvider(cal_dir, source=spec.forecast_source)
        strategy = GenomeStrategy(
            spec,
            clock=self._genome_clock,
            forecast_provider=provider,
            fee_regime=fees_mod.load_regime(),
            calibration_provider=calibration,
            state_dir=cache_dir,  # persisted traded/missed-hour state survives restarts
        )
        logger.info(
            "[Weather] GenomeStrategy %s loaded (genome_id=%s mode=%s registry=%s shadow=%s)",
            strategy.name, spec.genome_id, spec.mode, spec.registry_status, self.genome_shadow,
        )
        return strategy

    @staticmethod
    def _registry_status(family: str):
        """Current registry status of ``family`` from the tracked registry.jsonl (no factory import)."""
        import json as _json

        from src.factory.promoted import REPO_ROOT as _REPO_ROOT

        path = os.path.join(_REPO_ROOT, "reports", "factory", "registry.jsonl")
        status = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        line = _json.loads(raw)
                    except ValueError:
                        continue
                    if line.get("family") != family:
                        continue
                    if line.get("event") == "family" and status is None:
                        status = "OPEN"
                    elif line.get("event") == "transition":
                        status = line.get("status")
        except OSError:
            return None
        return status

    def _strategy_label(self, key: str, strategy) -> str:
        if key == GENOME_STRATEGY_KEY:
            return str(getattr(strategy, "name", "Genome"))
        return STRATEGY_LABELS.get(key, key)

    def _shadow_signals(self, signals, strategy_name, dashboard) -> None:
        """Shadow mode: the FR-0.4 EMIT line, then exactly one GENOME_SHADOW reject per signal.

        Mirrors ``SignalProcessorMixin._process_signals``'s EMIT format so the
        maia log tooling (``/api/logs/tail``, ``factory_paper_reconcile.py``)
        sees the genome's decisions; the signal is never sized, EV-gated or
        booked (the protected mixin is not touched).
        """
        if not signals:
            return
        if not isinstance(signals, list):
            signals = [signals]
        for sig in signals:
            conf = getattr(sig, "confidence", 0.0)
            quote = getattr(sig, "quote", None)
            quote_s = f"{quote:.4f}" if isinstance(quote, (int, float)) else "n/a"
            logger.info(
                "[Signal] EMIT strategy=%s symbol=%s side=%s contract=%s "
                "price=%s qty=%s confidence=%.3f quote=%s limit=%s",
                strategy_name,
                sig.symbol,
                sig.side,
                getattr(sig, "contract_side", "YES"),
                sig.limit_price,
                sig.quantity,
                conf,
                quote_s,
                sig.limit_price,
            )
            log_rejection(
                "GENOME_SHADOW",
                strategy_name,
                sig.symbol,
                side=sig.side,
                contract=getattr(sig, "contract_side", "YES"),
                price=sig.limit_price,
                quantity=sig.quantity,
                confidence=conf,
                quote=quote,
                limit=sig.limit_price,
                expiration=(
                    sig.expiration_time.isoformat()
                    if getattr(sig, "expiration_time", None) is not None
                    else None
                ),
            )
            try:
                dashboard.record_signal(sig, status="SHADOW", strategy_name=strategy_name)
            except Exception as e:  # the dashboard is a sink, never a gate
                logger.debug(f"[Weather] shadow record_signal failed: {e}")

    # ── Config accessors ────────────────────────────────────────────

    @property
    def settlement_stations(self) -> List[str]:
        """Observation stations for the configured cities (FR-1.4)."""
        return [c.settlement_station for c in self.CITIES]

    @property
    def station_timezones(self) -> Dict[str, str]:
        return {c.settlement_station: c.timezone for c in self.CITIES}

    # Back-compat alias: the orchestrator and older call sites referred to
    # ``nws_stations``. It now resolves to the settlement stations.
    @property
    def nws_stations(self) -> List[str]:
        return self.settlement_stations

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        if nws:
            self.nws = nws
        else:
            nws_ua = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
            self.nws = NWSProvider(nws_ua, self.settlement_stations)
            self.nws.connect()

        # METAR is the primary observation source: probed 2026-07-25, the
        # Aviation Weather Center serves all four settlement stations —
        # including the non-airport KNYC — hourly with T-group (0.1C) precision.
        self.metar = METARProvider(
            self.settlement_stations, station_timezones=self.station_timezones
        )
        self.metar.connect()

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        if not self.nws:
            return []

        # FR-0.7: decide ONCE per tick whether the hourly depth snapshot is
        # due. Timestamp is advanced after the city loop so all cities get
        # snapshotted in the same pass.
        depth_due = (
            time.time() - self._last_depth_snapshot
        ) >= self.DEPTH_SNAPSHOT_INTERVAL_S

        for city in self.CITIES:
            # PRD FR-1.4: ONE station per city — the one Kalshi settles on.
            # Observations, the running daily max, and the forecast all come
            # from it. There is no airport proxy anywhere in this path.
            station = city.settlement_station
            kalshi_ticker = city.kalshi_series
            active_ticker = None

            # --- Fetch observation data (METAR primary, NWS fallback) ---
            obs_data = None
            if self.metar:
                try:
                    metar_data = self.metar.fetch_latest(station)
                    if metar_data:
                        age = (
                            time.time() - metar_data.timestamp.timestamp()
                            if metar_data.timestamp
                            else 0
                        )
                        logger.info(
                            f"[Weather] Using METAR data for settlement station "
                            f"{station} ({city.key}, age: {age:.0f}s)"
                        )
                        obs_data = metar_data
                except Exception as e:
                    logger.warning(f"[Weather] METAR fetch failed for {station}: {e}")

            # Fallback to NWS observations at the SAME settlement station
            nws_data = None
            if obs_data is None:
                nws_data = self.nws.fetch_latest(station)
                if nws_data:
                    logger.info(
                        f"[Weather] Falling back to NWS observations for "
                        f"settlement station {station} ({city.key})"
                    )
                    obs_data = nws_data

            if not obs_data:
                continue

            # --- Merge NWS forecast into METAR observation ---
            # Strategies expect extra["forecast"] for 7-day forecast data.
            # METAR gives better temps; NWS gives forecasts. Both are keyed to
            # the settlement station.
            if nws_data is None:
                nws_data = self.nws.fetch_latest(station)
            if nws_data and nws_data.extra:
                forecast = nws_data.extra.get("forecast")
                if forecast and (
                    not obs_data.extra or not obs_data.extra.get("forecast")
                ):
                    if obs_data.extra is None:
                        obs_data.extra = {}
                    obs_data.extra["forecast"] = forecast

            # City provenance travels with the observation (FR-1.4 / FR-3.2).
            if obs_data.extra is None:
                obs_data.extra = {}
            obs_data.extra["city_key"] = city.key
            obs_data.extra["settlement_station"] = station
            obs_data.extra["station_timezone"] = city.timezone
            obs_data.extra["kalshi_series"] = kalshi_ticker

            temp = obs_data.extra.get("temperature_f")

            # --- FR-0.7 harvester: record the FULL ladder, fuse the active ---
            # One /markets list call per city per tick supplies bid/ask/no-side/
            # last/volume for every bracket; no per-market quote calls.
            k_data = None
            if self.kalshi and kalshi_ticker:
                try:
                    max_t = obs_data.extra.get("max_temp_today_f")
                    ladder = self._ladder_for_city(kalshi_ticker)

                    if ladder:
                        # FR-F3.3: strategies that price the WHOLE ladder
                        # (GenomeStrategy) read it from the observation; the
                        # existing strategies ignore the key. Additive.
                        obs_data.extra["ladder_markets"] = list(ladder)
                        # Active market = highest YES bid (same "sentiment"
                        # criterion the legacy resolver used).
                        k_data = max(ladder, key=lambda m: m.bid)
                        active_ticker = k_data.symbol

                        for m in ladder:
                            best_price = (
                                m.bid
                                if m.bid > 0
                                else (m.ask if m.ask > 0 else m.price)
                            )
                            m_extra = m.extra or {}
                            kwargs = dict(
                                bid=m.bid,
                                ask=m.ask,
                                no_bid=m_extra.get("no_bid", 0.0),
                                no_ask=m_extra.get("no_ask", 0.0),
                                last=m.price,
                                volume=m.volume,
                            )
                            # FR-1.1 bracket semantics ride along with the row.
                            # dashboard.update_price(**kwargs) stashes unknown
                            # keys in latest_prices[...]["extra"] and writes
                            # only its fixed columns, so this is additive.
                            for field in BRACKET_FIELDS:
                                kwargs[field] = m_extra.get(field)
                            if m.symbol == active_ticker and max_t is not None:
                                kwargs["max_temp"] = max_t
                            dashboard.update_price(
                                f"{m.symbol} (Market)", best_price, **kwargs
                            )
                    else:
                        # Fallback: legacy single-market resolution path
                        active_ticker = self._resolve_smart_ticker(
                            kalshi_ticker, criteria="sentiment", kalshi=self.kalshi
                        )
                        if active_ticker:
                            k_data = self.kalshi.fetch_latest(active_ticker)
                            if k_data:
                                best_price = (
                                    k_data.bid
                                    if k_data.bid > 0
                                    else (
                                        k_data.ask if k_data.ask > 0 else k_data.price
                                    )
                                )
                                dashboard.update_price(
                                    f"{active_ticker} (Market)",
                                    best_price,
                                    bid=k_data.bid,
                                    ask=k_data.ask,
                                    no_bid=k_data.extra.get("no_bid", 0.0)
                                    if k_data.extra
                                    else 0.0,
                                    no_ask=k_data.extra.get("no_ask", 0.0)
                                    if k_data.extra
                                    else 0.0,
                                    last=k_data.price,
                                    volume=k_data.volume,
                                    max_temp=max_t,
                                )

                    # Hourly top-3 orderbook depth snapshot (never per tick)
                    if depth_due and ladder:
                        self._snapshot_depth(ladder, dashboard)

                    if k_data and active_ticker:
                        # Fuse Kalshi prices into observation data
                        obs_data.bid = k_data.bid
                        obs_data.ask = k_data.ask
                        obs_data.price = k_data.price
                        obs_data.symbol = active_ticker

                        # PRD FR-1.1: carry the active market's bracket
                        # semantics so strategies can call
                        # parse_bracket_spec(symbol, data.extra) directly.
                        # Fields are written even when absent (as None) so a
                        # stale bracket can never survive onto a new market —
                        # parse_bracket_spec then raises BracketSpecError
                        # rather than letting anything infer direction from
                        # the ticker string. All pre-existing keys are kept.
                        k_extra = k_data.extra or {}
                        for field in BRACKET_FIELDS:
                            obs_data.extra[field] = k_extra.get(field)
                except Exception as e:
                    logger.error(f"[Weather] Market Fetch Fail ({kalshi_ticker}): {e}")
                    # F3 missed-hour rule: a poll the sandbox could not make is
                    # a lost chance for the genome (the archive keeps the
                    # candle), never a silent gap it may fill at a later hour.
                    genome = self.strategies.get(GENOME_STRATEGY_KEY)
                    if genome is not None and hasattr(genome, "record_poll_failure"):
                        try:
                            from src.core.weather_settlement import city_key_for_station

                            genome.record_poll_failure(
                                city_key_for_station(station) or kalshi_ticker.replace("KXHIGH", "")
                            )
                        except Exception as e2:  # never let bookkeeping break the tick
                            logger.warning(f"[Weather] genome poll-failure bookkeeping failed: {e2}")

            # Use real Kalshi market price for position valuation (not raw temp)
            kalshi_market_price = None
            if active_ticker and obs_data.bid > 0:
                kalshi_market_price = obs_data.bid  # fused from k_data above
            elif active_ticker and obs_data.price > 0:
                kalshi_market_price = obs_data.price

            if temp:
                dashboard.update_price(f"{kalshi_ticker or station} (F)", temp)
                if active_ticker and kalshi_market_price and kalshi_market_price > 0:
                    risk_manager.update_market_data(active_ticker, kalshi_market_price)
                    risk_manager.exchange.update_market_price(
                        active_ticker, kalshi_market_price
                    )
                elif active_ticker:
                    risk_manager.update_market_data(active_ticker, temp)
                else:
                    risk_manager.update_market_data(f"TEMP_{station}", temp)

            # Extract PoP for Precip
            forecasts = obs_data.extra.get("forecast") or []
            pop_prob = 0.0
            for period in forecasts:
                if period.get("isDaytime"):
                    val = period.get("probabilityOfPrecipitation", {}).get("value", 0)
                    if val:
                        pop_prob = val / 100.0
                    break

            if pop_prob is not None:
                if active_ticker:
                    risk_manager.update_market_data(f"{active_ticker}_PRECIP", pop_prob)
                else:
                    risk_manager.update_market_data(f"PRECIP_{station}", pop_prob)

            # Waterfall: ML Weather (only when ML_WEATHER_ENABLED built it
            # into self.strategies) → V2 rule-based fallback.
            # 2026-09-01: paper trading re-enabled on the sandbox (see the
            # WEATHER_TRADING_ENABLED comment). Every signal still runs the
            # full risk/EV/Kelly gauntlet in _process_signals.
            # FR-F3.3: the waterfall is ``self.strategies`` in declared order
            # ([genome] -> [ml_weather] -> weather); the first strategy whose
            # signals trade ends the pass. Behaviour-preserving for the
            # existing two: every strategy reached has its (possibly empty)
            # signal list handed to _process_signals exactly as before.
            if WEATHER_TRADING_ENABLED:
                for key, strategy in list(self.strategies.items()):
                    signals = strategy.analyze(obs_data)
                    label = self._strategy_label(key, strategy)
                    if key == GENOME_STRATEGY_KEY and self.genome_shadow:
                        # Shadow: EMIT + one GENOME_SHADOW reject, never
                        # sized/booked; the waterfall continues to V2.
                        self._shadow_signals(signals, label, dashboard)
                        continue
                    traded = self._process_signals(
                        signals,
                        strategy_name=label,
                        risk_manager=risk_manager,
                        dashboard=dashboard,
                    )
                    if traded:
                        break

            time.sleep(1)  # 1 sec between cities

        if depth_due:
            self._last_depth_snapshot = time.time()

        return []

    def _ladder_for_city(self, series_base):
        """Full-quote ladder for a city's series, filtered to tracked dates.

        One /markets list call (via ``fetch_market_ladder``) — no per-market
        fetches. Tracked = yesterday's, today's and tomorrow's events on the
        **Eastern Time** calendar; if none match, the full active ladder is
        returned defensively.

        ET date policy (2026-09-02, PRD_STRATEGY_FACTORY FR-F0.3). The
        previous ``datetime.now()`` was the host wall clock, which on maia is
        UTC: after 00:00Z the "today" fragment rolled to D+1 while the D
        ladders were still open, so the last 5–8 h of every city-day (up to
        04:59Z NY/MIA, 05:59Z CHI, 07:59Z LAX) never reached the tape. The
        ET date alone is not enough either: LAX's D ladder closes 03:59 ET
        on D+1 and CHI's 01:59 ET, so between 00:00 and 03:59 ET the ET
        "today" is already D+1 and D would drop out. Hence D-1 is included
        too. It is one list request either way, and markets that have
        closed are already excluded upstream by ``fetch_market_ladder``'s
        status filter (``active``/``initialized``), so the extra fragment
        can only admit ladders that are genuinely still open.
        """
        if not hasattr(self.kalshi, "fetch_market_ladder"):
            return []
        ladder = self.kalshi.fetch_market_ladder(series_base) or []
        if not ladder:
            return []
        now_et = datetime.now(ET)
        target_dates = [
            (now_et + timedelta(days=offset)).strftime("%y%b%d").upper()
            for offset in (-1, 0, 1)
        ]
        tracked = [m for m in ladder if any(d in m.symbol for d in target_dates)]
        return tracked or ladder

    def _snapshot_depth(self, ladder, dashboard):
        """Record hourly top-3 orderbook levels for each tracked market.

        Called only when the hourly snapshot is due (see tick); throttled
        between calls to stay well under Kalshi rate limits.
        """
        for m in ladder[: self.MAX_DEPTH_MARKETS_PER_CITY]:
            try:
                book = self.kalshi.fetch_orderbook(m.symbol, depth=3)
                if book and (book.get("yes") or book.get("no")):
                    m_extra = m.extra or {}
                    # FR-1.1: a depth row carries the same bracket semantics
                    # as its quote rows, so the hourly book is settleable
                    # offline without joining back to a MARKET_DATA row.
                    dashboard.record_depth(
                        m.symbol,
                        book,
                        last_price=m.price,
                        strike_type=m_extra.get("strike_type"),
                        floor_strike=m_extra.get("floor_strike"),
                        cap_strike=m_extra.get("cap_strike"),
                    )
            except Exception as e:
                logger.warning(f"[Weather] Depth snapshot failed for {m.symbol}: {e}")
            time.sleep(0.15)

    def get_symbols(self) -> List[str]:
        return [c.kalshi_series for c in self.CITIES]
