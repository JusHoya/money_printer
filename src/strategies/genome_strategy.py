"""GenomeStrategy -- a promoted factory genome running in the maia sandbox (FR-F3.1).

Rebuilds, from the weather bot's live observation (``MarketData`` whose
``extra["ladder_markets"]`` carries the city's full Kalshi ladder) plus the
injected forecast vintage and calibration, the SAME visible row
``src/factory/frame.py`` builds from the evaluator's opportunity frame, then
evaluates ``genome.to_mask(g, row)`` and emits ONE ``TradeSignal`` per market
per target_date at the first masked EXECUTABLE hourly snapshot -- the exact
rule ``src/factory/fitness.score`` scores offline (``first_true_per_block``).
``scripts/factory_replay_parity.py`` proves the two trade sets identical.

Imports: numpy + the numpy-only factory modules (``genome``, ``features``,
``columns``, ``fees``, ``promoted``) + ``src.calibration.probability_engine``
(stdlib ``math`` only; verified numpy/pandas/scipy-free). The evaluator's
pandas-bound bracket geometry (``bracket_midpoint_f``, ``bracket_edge_distance_f``,
``ladder_core_width_f``) is vendored below and pinned by
``tests/test_genome_strategy.py`` against ``ev_analysis``.

Clock discipline (FR-F2.5/F3.1): this module never reads a wall clock (no
``now()``-style call on the datetime class, no epoch read from the ``time``
module); ``datetime`` is imported as the module ``_dt`` for types and
arithmetic only. Every instant comes from ``clock()``.

Reject codes (``src.core.risk_manager.log_rejection``): GENOME_NO_VINTAGE,
GENOME_MASK_FALSE, GENOME_ALREADY_TRADED, GENOME_FEE_MISMATCH,
GENOME_NOT_TOP_OF_HOUR, GENOME_NOT_EXECUTABLE, GENOME_SIGMA_CAP (the search
frame's pre-selection ``sigma_f <= sigma_cap`` row filter, R3 #1);
GENOME_SHADOW is logged by the bot.

What the live poll cannot reproduce (documented, not fudged):

* ``price_mean`` (candle mean price) and candle ``volume`` are candlestick
  fields; the ``/markets`` poll carries no candle mean and a cumulative
  volume. They are visible columns but no GENE_SPEC v1 gene reads them.
* ``target_date_code`` / ``market_code`` are dense frame indices; set to -1.
* ``last`` is NaN in the frame when a market has never traded; the poll
  reports 0.0. Not a genome input.
* mode=maker: the evaluator's ``executable`` folds the forward-looking
  ``maker_yes_fill``/``maker_no_fill`` flags (will the resting order fill
  before close). Unknowable at decision time; the live path treats a maker
  quote as executable when present. Family #1 is taker-only.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np

from src.calibration.forecast_calibration import (
    bucket_for_lead,
    calibration_filename,
    load_calibration,
)
from src.calibration.probability_engine import (
    ProbabilityEngineError,
    bracket_probabilities_point,
)
from src.core.bracket_payoff import (
    BracketSpec,
    BracketSpecError,
    attach_spec_to_signals,
    parse_bracket_spec,
    yes_bounds,
)
from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.core.risk_manager import log_rejection
from src.core.weather_settlement import (
    city_key_for_station,
    settlement_close_for,
    settlement_date_for,
    settlement_station_for,
)
from src.factory import columns as C
from src.factory import features as feat
from src.factory import fees as fees_mod
from src.factory import genome as G
from src.factory.promoted import PromotedSpec, calibration_dir_sha256

from src.utils.logger import logger  # noqa: E402  (the shared runtime logger)

_engine_logger = logging.getLogger("src.calibration.probability_engine")

# ---------------------------------------------------------------------------
# reject codes
# ---------------------------------------------------------------------------
REASON_NO_VINTAGE = "GENOME_NO_VINTAGE"
REASON_MASK_FALSE = "GENOME_MASK_FALSE"
REASON_ALREADY_TRADED = "GENOME_ALREADY_TRADED"
REASON_FEE_MISMATCH = "GENOME_FEE_MISMATCH"
REASON_NOT_TOP_OF_HOUR = "GENOME_NOT_TOP_OF_HOUR"
REASON_NOT_EXECUTABLE = "GENOME_NOT_EXECUTABLE"
REASON_SIGMA_CAP = "GENOME_SIGMA_CAP"
REASON_SHADOW = "GENOME_SHADOW"  # logged by weather_bot, never here

#: Evaluator constants the row rebuild needs (``ev_analysis.EVConfig`` defaults, pinned by tests).
REGIME_SINGLE = "single"
SUPPORT_SIGMAS = 8.0
DEFAULT_GRID_S = 3600
DEFAULT_TOP_OF_HOUR_TOLERANCE_S = 120
KEEP_HOURS_S = 48 * 3600
LADDER_KEY = "ladder_markets"
SERIES_PREFIX = "KXHIGH"


class GenomeSpecMismatch(RuntimeError):
    """The live inputs do not match the promoted spec; the strategy refuses to construct."""


# ---------------------------------------------------------------------------
# vendored evaluator bracket geometry (ev_analysis is pandas-bound)
# ---------------------------------------------------------------------------
def ladder_core_width_f(specs: Sequence[BracketSpec]) -> float:
    """``ev_analysis.ladder_core_width_f``: median width of the finite ``between`` brackets."""
    widths = sorted(
        {float(s.cap_strike) - float(s.floor_strike) + 1.0 for s in specs if s.strike_type == "between"}
    )
    if not widths:
        raise ValueError("ladder has no finite bracket; cannot measure its width")
    return widths[len(widths) // 2]


def bracket_midpoint_f(spec: BracketSpec, core_width_f: float) -> float:
    """``ev_analysis.bracket_midpoint_f``: bracket midpoint; tails as a virtual core-width bracket."""
    lo, hi = yes_bounds(spec)
    half = (float(core_width_f) - 1.0) / 2.0
    if math.isinf(lo) and math.isinf(hi):
        raise ValueError(f"{spec.ticker}: bracket is unbounded on both sides")
    if math.isinf(lo):
        return float(hi) - half
    if math.isinf(hi):
        return float(lo) + half
    return (float(lo) + float(hi)) / 2.0


def bracket_edge_distance_f(spec: BracketSpec, median_f: float) -> float:
    """``ev_analysis.bracket_edge_distance_f``: distance from the median to the nearest paying degF."""
    lo, hi = yes_bounds(spec)
    if median_f < lo:
        return float(lo) - float(median_f)
    if median_f > hi:
        return float(median_f) - float(hi)
    return 0.0


# ---------------------------------------------------------------------------
# calibration providers
# ---------------------------------------------------------------------------
class FrozenCalibrationProvider:
    """The committed ``<CITY>_<source>_v<N>.json`` payloads, identified by the directory hash.

    ``sha256`` is ``promoted.calibration_dir_sha256`` (the frame provenance's
    ``calibration_dir.files`` mapping hashed as ``frame._sha256_of_mapping``),
    so the live strategy and the frame agree on WHICH calibration files exist
    before a single probability is priced. Payloads are loaded through
    ``forecast_calibration.load_calibration`` (schema + content_hash verified).
    """

    kind = "frozen"

    def __init__(self, directory: str, *, source: str = "gfs_mex", version: int = 1) -> None:
        self.directory = directory
        self.source = str(source)
        self.version = int(version)
        self.sha256 = calibration_dir_sha256(directory)
        self._payloads: Dict[str, Mapping[str, Any]] = {}

    def payload_for(self, city: str, target_date: str) -> Mapping[str, Any]:
        city = str(city).upper()
        p = self._payloads.get(city)
        if p is None:
            path = os.path.join(self.directory, calibration_filename(city, self.source, self.version))
            p = load_calibration(path)
            self._payloads[city] = p
        return p


def _epoch(dt: _dt.datetime) -> int:
    if dt.tzinfo is None:
        raise GenomeSpecMismatch("clock() returned a naive datetime; the strategy needs tz-aware instants")
    return int(dt.astimezone(_dt.timezone.utc).timestamp())


def _nan_if_none(x: Any) -> float:
    if x is None:
        return float("nan")
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# the strategy
# ---------------------------------------------------------------------------
class GenomeStrategy(Strategy):
    """See the module docstring. Construct once per process; ``analyze`` is called every tick."""

    def __init__(
        self,
        spec: PromotedSpec,
        *,
        clock: Callable[[], _dt.datetime],
        forecast_provider: Any,
        fee_regime: fees_mod.FeeRegime,
        calibration_provider: Any,
        adverse_fill: Optional[float] = None,
        top_of_hour_tolerance_s: int = DEFAULT_TOP_OF_HOUR_TOLERANCE_S,
        grid_s: int = DEFAULT_GRID_S,
        series_prefix: str = SERIES_PREFIX,
        prob_cache: Optional[Dict[Any, Any]] = None,
        quiet_engine: bool = True,
        row_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.spec = spec
        self.genome = spec.genome()
        self.clock = clock
        self.forecast_provider = forecast_provider
        self.fee_regime = fee_regime
        self.calibration_provider = calibration_provider
        self.adverse_fill = float(spec.adverse_fill if adverse_fill is None else adverse_fill)
        self.contracts = int(spec.contracts_frame)
        self.sigma_cap = float(spec.sigma_cap)
        self.top_of_hour_tolerance_s = int(top_of_hour_tolerance_s)
        self.grid_s = int(grid_s)
        self.series_prefix = str(series_prefix)
        self.direction_code = int(self.genome.direction)
        self.mode_code = int(self.genome.mode)
        self.is_maker = self.mode_code == C.MODE_LABELS.index("maker")
        self.contract_side = "NO" if self.direction_code == C.DIRECTION_LABELS.index("buy_no") else "YES"
        self._name = f"Genome {spec.id8}"
        # (target_date, symbol) emitted -- the first-in-market rule; pruned by target_date age
        self._traded: Set[Tuple[str, str]] = set()
        # (city, hour_epoch) -> "done" | "missed"
        self._hours: Dict[Tuple[str, int], str] = {}
        self._prob_cache: Dict[Any, Any] = prob_cache if prob_cache is not None else {}
        #: parity hook: every visible row analyze() evaluates is handed here (replay only)
        self.row_sink = row_sink
        self.stats: Dict[str, int] = {"analyze_calls": 0, "hours_evaluated": 0, "signals": 0, "rejects": 0}
        if quiet_engine:
            # the walk-forward/frozen payloads trigger the engine's fallback WARNINGs on
            # every pricing; lanes/weather.py silences them the same way for the frame
            _engine_logger.setLevel(logging.ERROR)
        self._verify_inputs()

    # -- construction-time refusals ---------------------------------------
    def _verify_inputs(self) -> None:
        spec = self.spec
        want_type = "maker" if self.is_maker else "taker"
        if spec.fee.type != want_type:
            raise GenomeSpecMismatch(
                f"spec.fee.type={spec.fee.type!r} but the genome's mode is {want_type!r}"
            )
        if getattr(self.fee_regime, "sha256", None) != spec.fee.regime_sha256:
            raise GenomeSpecMismatch(
                f"fee regime sha {str(getattr(self.fee_regime, 'sha256', ''))[:12]} != spec "
                f"{spec.fee.regime_sha256[:12]} (configs/fees/fee_regime.csv changed since promotion)"
            )
        live_fee_type = self._fee_type_at(_epoch(self.clock()))
        if live_fee_type != spec.fee.fee_type:
            raise GenomeSpecMismatch(
                f"fee_type at clock() is {live_fee_type!r}, spec was promoted under {spec.fee.fee_type!r}"
            )
        cal_sha = getattr(self.calibration_provider, "sha256", None)
        if cal_sha != spec.calibration.sha256:
            raise GenomeSpecMismatch(
                f"calibration sha {str(cal_sha)[:12]} != spec {spec.calibration.sha256[:12]} "
                f"({spec.calibration.dir} changed since promotion)"
            )
        lag = getattr(self.forecast_provider, "lag_min", None)
        if lag is not None and int(lag) != int(spec.availability_lag_min):
            raise GenomeSpecMismatch(
                f"forecast provider lag {lag} min != spec availability_lag_min {spec.availability_lag_min}"
            )
        if abs(self.adverse_fill - float(spec.adverse_fill)) > 1e-12:
            logger.warning(
                "[%s] adverse_fill override %.4f differs from the spec's %.4f (limit prices will not "
                "match the frame's price_paid)", self._name, self.adverse_fill, spec.adverse_fill,
            )

    def _fee_type_at(self, ts_epoch: int) -> str:
        return str(self.fee_regime.lookup(self.series_prefix, int(ts_epoch)).fee_type)

    # -- Strategy interface -------------------------------------------------
    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    def analyze(self, data: MarketData) -> List[TradeSignal]:
        """One evaluation per (market, UTC hour); ``[]`` + one ``log_rejection`` per skipped market."""
        signals = self._analyze(data)
        # Each signal is stamped from ITS OWN market's MarketData (the bot's fused
        # observation only carries the active bracket's spec).
        return signals

    def _analyze(self, data: MarketData) -> List[TradeSignal]:
        self.stats["analyze_calls"] += 1
        now = self.clock()
        now_epoch = _epoch(now)
        ladder = self._ladder_of(data)
        if not ladder:
            return []
        city = self._city_of(data, ladder)
        hour_epoch = (now_epoch // self.grid_s) * self.grid_s
        key = (city, hour_epoch)
        state = self._hours.get(key)
        if state is not None:
            if state == "done":
                # the bot polls every ~15 s; log the off-grid skip ONCE per (city, hour)
                self._hours[key] = "done_logged"
                self._reject(REASON_NOT_TOP_OF_HOUR, ladder[0].symbol, city=city, hour_utc=hour_epoch, evaluated=True)
            return []
        self._prune(hour_epoch)
        if now_epoch - hour_epoch > self.top_of_hour_tolerance_s:
            self._hours[key] = "missed"
            self._reject(
                REASON_NOT_TOP_OF_HOUR, ladder[0].symbol, city=city,
                late_s=int(now_epoch - hour_epoch), tolerance_s=self.top_of_hour_tolerance_s,
            )
            return []
        self._hours[key] = "done"
        self.stats["hours_evaluated"] += 1
        decision_ts = _dt.datetime.fromtimestamp(hour_epoch, _dt.timezone.utc)

        fee_type = self._fee_type_at(hour_epoch)
        if fee_type != self.spec.fee.fee_type:
            for m in ladder:
                self._reject(REASON_FEE_MISMATCH, m.symbol, fee_type=fee_type, spec=self.spec.fee.fee_type)
            return []

        out: List[TradeSignal] = []
        for target_date, group in self._group_by_date(ladder).items():
            out.extend(self._evaluate_day(city, target_date, group, decision_ts))
        return out

    # -- one city-day at one decision instant ------------------------------
    def _evaluate_day(
        self, city: str, target_date: str, group: List[MarketData], decision_ts: _dt.datetime
    ) -> List[TradeSignal]:
        rows = self._rows_for_day(city, target_date, group, decision_ts, reject=True)
        out: List[TradeSignal] = []
        for m in group:
            row = rows.get(m.symbol)
            if row is None:
                continue  # already rejected inside _rows_for_day
            if float(row["sigma_f"]) > self.sigma_cap:
                self._reject(REASON_SIGMA_CAP, m.symbol, sigma_f=float(row["sigma_f"]), cap=self.sigma_cap)
                continue
            if (target_date, m.symbol) in self._traded:
                self._reject(REASON_ALREADY_TRADED, m.symbol, target_date=target_date)
                continue
            masked = bool(G.to_mask(self.genome, row))
            if not masked:
                self._reject(
                    REASON_MASK_FALSE, m.symbol, p_win=float(row["p_win"]), quote=float(row["quote"]),
                    window=int(row["window_code"]), band=int(row["band_code"]),
                )
                continue
            if not bool(row["executable"]):
                self._reject(
                    REASON_NOT_EXECUTABLE, m.symbol, quote=float(row["quote"]),
                    price=float(row["price_paid"]), sandbox_admissible=bool(row["sandbox_admissible"]),
                )
                continue
            sig = self._signal(m, row)
            self._traded.add((target_date, m.symbol))
            self.stats["signals"] += 1
            out.append(sig)
        return out

    def _signal(self, m: MarketData, row: Mapping[str, Any]) -> TradeSignal:
        sig = TradeSignal(
            symbol=m.symbol,
            side="buy",
            quantity=self.contracts,
            limit_price=float(row["price_paid"]),
            confidence=float(row["p_win"]),
            contract_side=self.contract_side,
            strike_type=None,
            floor_strike=None,
            cap_strike=None,
            expiration_time=settlement_close_for(m.symbol),
        )
        sig.is_maker = bool(self.is_maker)  # the exchange books taker fees for False/None
        sig.genome_id = self.spec.genome_id
        sig.p_yes = float(row["p_yes"])
        sig.quote = float(row["quote"])
        # bracket semantics from THIS market's API fields (PRD FR-1.1/FR-1.2)
        attach_spec_to_signals([sig], m)
        return sig

    # -- the visible row ------------------------------------------------------
    def build_row(self, data: MarketData, now: _dt.datetime) -> Optional[Dict[str, Any]]:
        """The visible row for ``data.symbol`` exactly as ``frame.py`` builds it (parity hook).

        ``data.extra["ladder_markets"]`` supplies the city-day ladder (needed
        for the probability engine's tail attribution and the core width);
        without it ``data`` alone is the ladder. ``now`` is snapped to the
        decision grid. ``None`` when no vintage/spec/close time is available.
        """
        rows = self.build_rows(data, now)
        return rows.get(data.symbol)

    def build_rows(self, data: MarketData, now: _dt.datetime) -> Dict[str, Dict[str, Any]]:
        """Visible rows for every market of ``data``'s ladder at ``now`` (no logging, no state)."""
        ladder = self._ladder_of(data)
        if not ladder:
            return {}
        city = self._city_of(data, ladder)
        hour_epoch = (_epoch(now) // self.grid_s) * self.grid_s
        decision_ts = _dt.datetime.fromtimestamp(hour_epoch, _dt.timezone.utc)
        out: Dict[str, Dict[str, Any]] = {}
        for target_date, group in self._group_by_date(ladder).items():
            out.update(self._rows_for_day(city, target_date, group, decision_ts, reject=False))
        return out

    def _rows_for_day(
        self, city: str, target_date: str, group: List[MarketData], decision_ts: _dt.datetime, *, reject: bool
    ) -> Dict[str, Dict[str, Any]]:
        specs: Dict[str, BracketSpec] = {}
        for m in group:
            try:
                specs[m.symbol] = parse_bracket_spec(m.symbol, m.extra)
            except BracketSpecError as exc:
                if reject:
                    self._reject(REASON_NOT_EXECUTABLE, m.symbol, reason="bracket_spec", detail=str(exc)[:80])
        if not specs:
            return {}
        vintage = self.forecast_provider.latest_vintage(city, target_date, decision_ts)
        if vintage is None:
            if reject:
                for sym in specs:
                    self._reject(REASON_NO_VINTAGE, sym, target_date=target_date, as_of=decision_ts.isoformat())
            return {}
        try:
            probs, width = self._probabilities(city, target_date, vintage, list(specs.values()))
        except (ProbabilityEngineError, ValueError, RuntimeError) as exc:
            # ProbabilityEngineError / CalibrationError / EVAnalysisError-shaped refusals
            # (thin calibration, unbounded ladder...) are a skip, never a crash of the bot
            if reject:
                logger.warning("[%s] %s %s: probability engine refused: %s", self._name, city, target_date, exc)
                for sym in specs:
                    self._reject(REASON_NOT_EXECUTABLE, sym, reason="probability_engine", detail=str(exc)[:80])
            return {}
        ts_epoch = _epoch(decision_ts)
        out: Dict[str, Dict[str, Any]] = {}
        for m in group:
            spec = specs.get(m.symbol)
            if spec is None:
                continue
            row = self._row(m, spec, probs, width, vintage, decision_ts, ts_epoch, city, target_date)
            if row is None:
                if reject:
                    self._reject(REASON_NOT_EXECUTABLE, m.symbol, reason="no_close_time_or_closed")
                continue
            out[m.symbol] = row
            if reject and self.row_sink is not None:
                self.row_sink(row)
        return out

    def _probabilities(self, city: str, target_date: str, vintage: Any, specs: List[BracketSpec]):
        """``ev_analysis.build_probability_table``'s one engine call per (city, date, vintage), cached."""
        payload = self.calibration_provider.payload_for(city, target_date)
        key = (
            city, target_date, vintage.init_time_utc, float(vintage.forecast_high_f),
            payload.get("content_hash"),
            tuple(sorted((s.ticker, s.strike_type, s.floor_strike, s.cap_strike) for s in specs)),
        )
        hit = self._prob_cache.get(key)
        if hit is not None:
            return hit
        width = ladder_core_width_f(specs)
        lead = int(vintage.lead_hours)
        bucket = bucket_for_lead(lead)
        result = bracket_probabilities_point(
            city=city,
            target_date=target_date,
            forecast_high_f=float(vintage.forecast_high_f),
            specs=specs,
            calibration=payload,
            lead_hours=None if bucket == "day_of" else lead,
            regime=REGIME_SINGLE,
            forecast_source=self.spec.forecast_source,
            support_sigmas=SUPPORT_SIGMAS,
        )
        self._prob_cache[key] = (result, width)
        return result, width

    def _row(
        self, m: MarketData, spec: BracketSpec, probs: Any, width: float, vintage: Any,
        decision_ts: _dt.datetime, ts_epoch: int, city: str, target_date: str,
    ) -> Optional[Dict[str, Any]]:
        extra = m.extra or {}
        close = extra.get("close_time")
        if not close:
            return None
        try:
            close_dt = _dt.datetime.fromisoformat(str(close).replace("Z", "+00:00"))
        except ValueError:
            return None
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=_dt.timezone.utc)
        # ladder tape convention: round((close - ts)/60, 1); evaluator keeps minutes_to_close > 0
        minutes = round((close_dt - decision_ts).total_seconds() / 60.0, 1)
        if not minutes > 0:
            return None

        p_yes = float(probs.p_yes(spec.ticker))
        mu_f = float(probs.mu_f)
        sigma_f = float(probs.sigma_f)
        midpoint = bracket_midpoint_f(spec, width)
        edge = bracket_edge_distance_f(spec, mu_f)
        distance = abs(midpoint - mu_f)
        yes_bid = _nan_if_none(m.bid)
        yes_ask = _nan_if_none(m.ask)
        dc = np.int16(self.direction_code)
        mc = np.int16(self.mode_code)
        quote = feat.quote(yes_bid, yes_ask, dc, mc)
        price = feat.price_paid(quote, self.adverse_fill)
        # p_win / executable follow build_opportunity_frame per direction
        p_win = p_yes if self.direction_code == 0 else 1.0 - p_yes
        quote_present = not bool(np.isnan(quote))
        price_ok = not np.isnan(price)
        sandbox_ok = bool(feat.sandbox_admissible(p_win, price))
        # taker: fillable == quote present; maker: the frame also requires the forward fill flag,
        # which is unknowable live (module docstring) -- quote present is the best honest reading.
        executable = bool(quote_present and price_ok and sandbox_ok)
        fee = fees_mod.fee_per_contract(
            np.asarray([price], dtype=np.float64), np.asarray([ts_epoch], dtype=np.int64),
            np.asarray([self._series_of(m.symbol)]), self.contracts, self.is_maker, regime=self.fee_regime,
        )[0]
        lead_hours = float(vintage.lead_hours)
        row: Dict[str, Any] = {
            "city_code": np.int16(C.code_for(C.CITY_LABELS, city)),
            "target_date_code": np.int16(-1),  # dense frame index; not reproducible live
            "market_code": np.int32(-1),  # dense frame index; not reproducible live
            "ts_utc": np.int64(ts_epoch),
            "minutes_to_close": np.float64(minutes),
            "window_code": feat.window_code(minutes),
            "direction_code": dc,
            "mode_code": mc,
            "band_code": feat.band_code(distance),
            "lead_bucket_code": feat.lead_bucket_code(lead_hours),
            "lead_hours": np.float64(lead_hours),
            "p_yes": np.float64(p_yes),
            "p_win": np.float64(p_win),
            "mu_f": np.float64(mu_f),
            "sigma_f": np.float64(sigma_f),
            "midpoint_f": np.float64(midpoint),
            "distance_f": np.float64(distance),
            "edge_distance_f": np.float64(edge),
            "yes_bid": np.float64(yes_bid),
            "yes_ask": np.float64(yes_ask),
            "no_bid": np.float64(_nan_if_none(extra.get("no_bid"))),
            "no_ask": np.float64(_nan_if_none(extra.get("no_ask"))),
            "last": np.float64(_nan_if_none(m.price)),
            "price_mean": np.float64(_nan_if_none(extra.get("price_mean"))),
            "volume": np.float64(_nan_if_none(m.volume)),
            "open_interest": np.float64(_nan_if_none(extra.get("open_interest"))),
            "quote": np.float64(quote),
            "price_paid": np.float64(price),
            "fee_per_contract": np.float64(fee),
            "executable": np.bool_(executable),
            "sandbox_admissible": np.bool_(sandbox_ok),
            "floor_strike": np.float64(_nan_if_none(spec.floor_strike)),
            "cap_strike": np.float64(_nan_if_none(spec.cap_strike)),
            "strike_type_code": np.int16(C.code_for(C.STRIKE_TYPE_LABELS, spec.strike_type)),
            # non-visible context for callers/tests (VisibleOnly hides these from to_mask)
            "market_ticker": m.symbol,
            "target_date": target_date,
            "city": city,
            "init_time_utc": vintage.init_time_utc,
        }
        return row

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _ladder_of(data: MarketData) -> List[MarketData]:
        extra = data.extra or {}
        ladder = extra.get(LADDER_KEY)
        if ladder:
            return [m for m in ladder if getattr(m, "symbol", None)]
        if data.symbol and extra.get("strike_type"):
            return [data]
        return []

    @staticmethod
    def _series_of(symbol: str) -> str:
        return str(symbol).split("-", 1)[0].upper()

    def _city_of(self, data: MarketData, ladder: List[MarketData]) -> str:
        extra = data.extra or {}
        city = extra.get("city_key")
        if city:
            return str(city).upper()
        station = extra.get("settlement_station") or settlement_station_for(ladder[0].symbol)
        key = city_key_for_station(station) if station else None
        if key:
            return str(key).upper()
        # last resort: the series suffix (KXHIGHNY -> NY)
        return self._series_of(ladder[0].symbol)[len(self.series_prefix):]

    @staticmethod
    def _group_by_date(ladder: List[MarketData]) -> Dict[str, List[MarketData]]:
        out: Dict[str, List[MarketData]] = {}
        for m in ladder:
            d = settlement_date_for(m.symbol)
            if d is None:
                continue
            out.setdefault(d.isoformat(), []).append(m)
        return dict(sorted(out.items()))

    def _prune(self, hour_epoch: int) -> None:
        old = [k for k in self._hours if hour_epoch - k[1] > KEEP_HOURS_S]
        for k in old:
            del self._hours[k]
        if len(self._traded) > 4096:
            cutoff = (_dt.datetime.fromtimestamp(hour_epoch, _dt.timezone.utc) - _dt.timedelta(days=7)).date().isoformat()
            self._traded = {k for k in self._traded if k[0] >= cutoff}

    def _reject(self, code: str, symbol: str, **context: Any) -> None:
        self.stats["rejects"] += 1
        log_rejection(code, self._name, symbol, **context)


__all__ = [
    "DEFAULT_TOP_OF_HOUR_TOLERANCE_S",
    "FrozenCalibrationProvider",
    "GenomeSpecMismatch",
    "GenomeStrategy",
    "LADDER_KEY",
    "REASON_ALREADY_TRADED",
    "REASON_FEE_MISMATCH",
    "REASON_MASK_FALSE",
    "REASON_NOT_EXECUTABLE",
    "REASON_NOT_TOP_OF_HOUR",
    "REASON_NO_VINTAGE",
    "REASON_SHADOW",
    "REASON_SIGMA_CAP",
    "REGIME_SINGLE",
    "SUPPORT_SIGMAS",
    "bracket_edge_distance_f",
    "bracket_midpoint_f",
    "ladder_core_width_f",
]
