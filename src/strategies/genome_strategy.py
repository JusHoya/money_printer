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
frame's pre-selection ``sigma_f <= sigma_cap`` row filter, R3 #1),
GENOME_MISSED_HOUR (below); GENOME_SHADOW is logged by the bot. Every reject
goes through ``_reject``, which passes reason/strategy/symbol POSITIONALLY --
so a context key named after one of them (``RESERVED_CONTEXT_KEYS``) is renamed
``ctx_<key>`` rather than raising ``TypeError`` inside ``analyze()``.

Live-conditions parity (F3 red team, 2026-09-05). The offline trade set is the
FIRST masked executable hourly snapshot per market. Live, the strategy can only
claim that snapshot if it evaluated EVERY earlier hour of the market-day:

* **Missed-hour rule.** Per city the strategy remembers the last hour it
  evaluated and what it knows about every recent city-hour (``_hours``). An
  hour is *missed* when the strategy had the chance and lost it:

  (a) a tick reached it outside the top-of-hour tolerance
      (``GENOME_NOT_TOP_OF_HOUR``, recorded as ``missed:late_tick``);
  (b) the process was down -- a restarted strategy whose persisted last hour
      is more than one grid step before its first evaluation (``downtime``);
  (c) the bot could not look -- its Kalshi poll for the city raised
      (``record_poll_failure``) or it never got an observation and skipped the
      city entirely (``record_missed_hour(city, "observation_failure")``).
      Recorded once per city-hour (the bot calls the hook every tick of a
      sustained outage) and RECOVERABLE: the report is about one tick, so a
      later tick of the same hour that has the ladder AND is still inside the
      top-of-hour tolerance evaluates the hour and upgrades the record to
      ``done`` (``[Genome] MISS RECOVERED``). An hour is lost only when no
      in-tolerance tick ever evaluates it. When two ticks report different
      causes the FIRST wins -- it is the one that lost the hour;
  (d) the strategy was not there at all -- an hour with NO record of any kind
      between two evaluated hours (``tick_gap``). Only a ``tick_driven``
      caller can conclude this: the live bot polls every city every tick and
      therefore leaves a record for every hour it is alive for, so a hole in
      that record is a stalled loop, a hung HTTP call or a paused container.
      Only back to ``KEEP_HOURS_S``, though -- older records were *pruned*, and
      a pruned hour is not an hour the loop was absent for;
  (e) the forecast fetch FAILED at an evaluated hour
      (``vintage_fetch_failure``): the provider swallows the fault and returns
      no vintage, but the archive holds the run the offline frame priced that
      hour with, so the city-day closes immediately. A vintage that simply does
      not exist yet (no fetch error) is a data gap -- the frame has no row for
      it either -- and is never a miss.

  Then every city-day visible at the next evaluated hour is marked missed --
  logged once per city-day as ``[Genome] DAY CLOSED ... lost_hour_utc=<h>
  cause=<why>`` -- and rejects ``GENOME_MISSED_HOUR``, carrying that same lost
  hour and cause, for the rest of the market-day: the mask may already have
  been true at the skipped hour, so a later emit would not be the offline
  trade. An hour at which the bot LOOKED and the city had no ladder is a
  *data* gap, not a missed evaluation (``record_no_ladder``, or an empty
  ``ladder_markets`` on the observation; recorded as ``no_data``) -- the frame
  has no row for it either (the ladder archive has candle-less hours), so it
  is never a miss and it does not make the hours around it look like a tick
  gap. This is what keeps the replay parity exact: a replay driver visits only
  the hours the archive holds, leaves ``tick_driven`` False, and (d) never
  fires. A market evaluated at every earlier hour (mask false, book empty, not
  executable ...) and masked-executable now emits normally -- that IS the
  offline rule. Newly tracked city-days that first appear at a missed hour are
  marked missed too (conservative). A late tick counts as a lost chance even on
  a FRESH deploy (no persisted state, no predecessor hour); a fresh deploy
  whose first tick is on time has no predecessor and never marks a miss, so its
  first visible city-days may emit later than the offline first hour --
  ``factory_paper_reconcile.py`` flags those.
* **Persisted state.** ``state_dir`` (the bot passes the forecast-cache dir)
  holds ``genome_state_<genome_id>.json`` -- last evaluated hour per city,
  missed city-days and why, traded (target_date, symbol) -- rewritten
  atomically (write, ``fsync``, ``os.replace``) on every change (``_close_day``
  saves too: the ``vintage_fetch_failure`` closure fires AFTER ``_analyze``'s
  own save and a restart used to reopen the city-day) and loaded at
  construction, so a restarted strategy neither re-emits an already-traded
  market nor claims a first hour it did not see. A state file that cannot be
  USED -- unreadable, undecodable, or decodable with the wrong shapes -- is NOT
  fatal: the strategy starts from an empty state -- conservative, since an empty
  state claims no predecessor hour -- and says so at ERROR
  (``state_recovered_from``). A state file belonging to a DIFFERENT genome is
  still a refusal. ``state_dir=None`` keeps the state in memory (replay parity,
  tests).

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
import hashlib
import inspect
import json
import logging
import math
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np

from src.calibration import forecast_calibration as fcal
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
REASON_MISSED_HOUR = "GENOME_MISSED_HOUR"
REASON_SHADOW = "GENOME_SHADOW"  # logged by weather_bot, never here

#: ``log_rejection``'s own leading parameters. ``_reject`` passes reason/strategy/
#: symbol POSITIONALLY, so a context key with one of these names raised
#: ``TypeError: log_rejection() got multiple values for argument 'reason'`` from
#: inside ``analyze()`` -- crashing the bot's tick on the very path documented as
#: "a skip, never a crash" (maia, 3 of 60 archived city-days). Read off the
#: function itself so a rename in ``risk_manager.py`` cannot silently reopen the
#: hole, and enforced statically by ``tests/test_genome_strategy.py``.
RESERVED_CONTEXT_KEYS = frozenset(
    name
    for name, p in inspect.signature(log_rejection).parameters.items()
    if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
)

#: ``_hours[(city, hour)]`` -- what the strategy knows about one city-hour.
HOUR_DONE = "done"  # evaluated at the top of the hour
HOUR_DONE_LOGGED = "done_logged"  # ... and the off-grid skip was logged once
HOUR_NO_DATA = "no_data"  # the bot LOOKED and the city had no ladder: a data gap, never a miss
HOUR_MISSED_PREFIX = "missed:"  # a lost chance; the suffix is the cause
HOUR_EVALUATED = (HOUR_DONE, HOUR_DONE_LOGGED)

#: why an hour was lost (the ``cause=`` of ``[Genome] MISS`` / ``DAY CLOSED`` / ``GENOME_MISSED_HOUR``)
CAUSE_LATE_TICK = "late_tick"
CAUSE_POLL_FAILURE = "poll_failure"
CAUSE_OBSERVATION_FAILURE = "observation_failure"
CAUSE_DOWNTIME = "downtime"
CAUSE_TICK_GAP = "tick_gap"
CAUSE_VINTAGE_FETCH_FAILURE = "vintage_fetch_failure"
CAUSE_UNKNOWN = "unknown"

#: Evaluator constants the row rebuild needs (``ev_analysis.EVConfig`` defaults, pinned by tests).
REGIME_SINGLE = "single"
SUPPORT_SIGMAS = 8.0
DEFAULT_GRID_S = 3600
DEFAULT_TOP_OF_HOUR_TOLERANCE_S = 120
KEEP_HOURS_S = 48 * 3600
STATE_FILE_FMT = "genome_state_{genome_id}.json"
STATE_KEEP_DAYS = 7
LADDER_KEY = "ladder_markets"
SERIES_PREFIX = "KXHIGH"


class GenomeSpecMismatch(RuntimeError):
    """The live inputs do not match the promoted spec; the strategy refuses to construct."""


def _missed(cause: str) -> str:
    """``"late_tick"`` -> the ``_hours`` value ``"missed:late_tick"`` (one greppable token)."""
    text = str(cause or CAUSE_UNKNOWN).strip().replace(" ", "_") or CAUSE_UNKNOWN
    return HOUR_MISSED_PREFIX + text


def _is_missed(state: Optional[str]) -> bool:
    return bool(state) and str(state).startswith(HOUR_MISSED_PREFIX)


def _cause_of(state: str) -> str:
    return str(state)[len(HOUR_MISSED_PREFIX):] if _is_missed(state) else CAUSE_UNKNOWN


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


#: ``CITY -> IANA timezone`` written into every payload (``== ev_analysis.CITY_TZ``;
#: pinned by ``tests/test_walk_forward_provider.py``). MIA is America/New_York on purpose.
CITY_TZ: Dict[str, str] = {
    "NY": "America/New_York",
    "CHI": "America/Chicago",
    "LAX": "America/Los_Angeles",
    "MIA": "America/New_York",
}

#: The frame's no-lookahead rule: a payload for target_date T is fitted on paired days
#: with ``target_date <= T - embargo_days``. 1 is the literal "strictly before" rule
#: every committed frame was frozen with (``provenance.embargo_days``); the replay
#: parity ``--calibration live`` run aborts if the frame under test says otherwise.
WALK_FORWARD_EMBARGO_DAYS = 1
#: FR-2.2's floor on day-of paired days below which a date is REFUSED rather than priced
#: on a thin fit (``ev_analysis.WalkForwardCalibrator(min_paired_days=60)``).
WALK_FORWARD_MIN_PAIRED_DAYS = 60

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class WalkForwardCalibrationProvider:
    """The frame's walk-forward payloads, refitted live from the archived series (F4 blocker fix).

    ``ev_analysis.WalkForwardCalibrator.calibration_as_of(city, T)`` -- the calibration
    every frozen frame was built with and every replay-parity report was proven under --
    reproduced here through the SAME ``forecast_calibration`` functions from the SAME two
    archives, without importing the lab's pandas harness into the sandbox:

    * the forecast series ``data/forecast_archive/forecast_series_<source>.csv`` and the
      CLI truth ``data/weather_truth/cli_daily_high_<STATION>.csv`` are read ONCE at
      construction (``fcal.load_forecast_series`` / ``fcal.load_truth``), paired per city
      with ``fcal.pair_city``, and fingerprinted exactly as the calibrator does;
    * ``payload_for(city, T)`` keeps the paired days with ``target_date <= T - embargo``,
      refuses (``CalibrationError``, a RuntimeError -> a skip, never a crash) when fewer
      than ``min_paired_days`` day-of days remain, and builds the payload with
      ``fcal.build_city_calibration`` + ``fcal.finalize`` -- byte-for-byte the frame's
      payload for that (city, T), ``content_hash`` included (pinned by
      ``tests/test_walk_forward_provider.py`` against the real calibrator).

    Why this and not the committed ``<CITY>_<source>_v1.json``: those payloads' month
    blocks are fitted on the WHOLE month (n = 31/30/24 for 2026-05/06/07), so pricing a
    May-18 ladder with them reads truth from May 19-31. The engine resolves
    ``by_month_day_of`` first, so the frozen provider prices EVERY ladder day with an
    in-sample month block, while the walk-forward fit at May-18 has 17 May days
    (< MIN_BUCKET_N) and falls to the season / pooled day-of block. That is the whole
    0.336 of ``p_yes``: sigma up to 2.2x wider and bias up to 3.2 F apart, on every
    city-day (53545 of 54159 compared rows), not a bug and not a data-file difference
    (the archives on disk hash to the frame's pins).

    ``sha256`` is still the calibration-DIR identity the spec carries
    (``promoted.calibration_dir_sha256``), so the construction guard's dir check is
    unchanged; the ARCHIVE identities this provider actually prices from are exposed as
    ``forecast_sha256`` / ``truth_sha256`` (whole-file sha256, the same numbers the frame
    provenance pins under ``forecast_csv.sha256`` / ``truth_files[city].sha256``) and
    logged by the bot at load. A payload for a date past the archive's end is the fit as
    of the archive's last day -- still walk-forward (nothing after T-1 can be in it),
    just not growing; ``coverage.last_target_date`` in the payload says how stale.
    """

    kind = "walk_forward"

    def __init__(
        self,
        directory: str,
        *,
        source: str = "gfs_mex",
        version: int = 1,
        forecast_csv: Optional[str] = None,
        truth_dir: Optional[str] = None,
        cities: Sequence[str] = C.CITY_LABELS,
        embargo_days: int = WALK_FORWARD_EMBARGO_DAYS,
        min_paired_days: int = WALK_FORWARD_MIN_PAIRED_DAYS,
    ) -> None:
        self.directory = directory
        self.source = str(source)
        self.version = int(version)
        self.sha256 = calibration_dir_sha256(directory)
        self.cities = tuple(str(c).upper() for c in cities)
        self.embargo_days = int(embargo_days)
        self.min_paired_days = int(min_paired_days)
        self.forecast_csv = forecast_csv or os.path.join(
            _REPO_ROOT, "data", "forecast_archive", f"forecast_series_{self.source}.csv"
        )
        self.truth_dir = truth_dir or os.path.join(_REPO_ROOT, "data", "weather_truth")
        from src.data.forecast_vintage_provider import CITY_STATION  # runtime module, no lab import

        self._station = {c: CITY_STATION[c] for c in self.cities}
        if not os.path.exists(self.forecast_csv):
            raise fcal.CalibrationError(
                f"walk-forward calibration needs the forecast archive {self.forecast_csv}; it is "
                f"tracked in git but the sandbox image excludes data/, so it must be copied into "
                f"the /srv data bind (deploy/pi/deploy_f3_shadow.sh step 2b)"
            )
        missing = [
            fcal.truth_csv_path(st, self.truth_dir)
            for st in self._station.values()
            if not os.path.exists(fcal.truth_csv_path(st, self.truth_dir))
        ]
        if missing:
            raise fcal.CalibrationError(
                f"walk-forward calibration needs the CLI truth files {missing}; they are tracked "
                f"in git but the sandbox image excludes data/, so they must be copied into the "
                f"/srv data bind (deploy/pi/deploy_f3_shadow.sh step 2b)"
            )
        self.forecast_sha256 = _sha256_file(self.forecast_csv)
        self.truth_sha256: Dict[str, str] = {}
        self._paired: Dict[str, List[fcal.PairedDay]] = {}
        self._drops: Dict[str, Mapping[str, int]] = {}
        self._rows_for_city: Dict[str, int] = {}
        self._truth_fingerprint: Dict[str, str] = {}
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._load()

    # -- exactly ev_analysis.WalkForwardCalibrator._load ----------------------
    def _load(self) -> None:
        rows = fcal.load_forecast_series(self.forecast_csv)
        self._forecast_fingerprint = fcal.content_fingerprint(
            rows,
            ("city", "station", "target_date", "init_time_utc", "lead_hours", "source",
             "forecast_high_f", "spread_f"),
        )
        for city in self.cities:
            station = self._station[city]
            truth = fcal.load_truth(station, self.truth_dir)
            self.truth_sha256[city] = _sha256_file(fcal.truth_csv_path(station, self.truth_dir))
            city_rows = [r for r in rows if r["city"] == city]
            paired, drops = fcal.pair_city(city_rows, station, truth)
            self._paired[city] = paired
            self._drops[city] = drops
            self._rows_for_city[city] = len(city_rows)
            self._truth_fingerprint[city] = fcal.content_fingerprint(
                [{"station": station, "date": d, "high": h} for d, h in sorted(truth.items())],
                ("station", "date", "high"),
            )

    def cutoff_for(self, target_date: str) -> str:
        """Latest ``target_date`` of a paired day admissible when pricing ``target_date``."""
        d = _dt.date.fromisoformat(str(target_date)[:10])
        return (d - _dt.timedelta(days=self.embargo_days)).isoformat()

    def archive_last_target_date(self, city: str) -> Optional[str]:
        p = self._paired.get(str(city).upper()) or []
        return max((x.target_date for x in p), default=None)

    # -- exactly ev_analysis.WalkForwardCalibrator.calibration_as_of ----------
    def payload_for(self, city: str, target_date: str) -> Mapping[str, Any]:
        city = str(city).upper()
        target_date = str(target_date)[:10]
        key = (city, target_date)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if city not in self._paired:
            raise fcal.CalibrationError(f"{city}: not one of the calibrated cities {self.cities}")
        cutoff = self.cutoff_for(target_date)
        subset = [p for p in self._paired[city] if p.target_date <= cutoff]
        day_of = {p.target_date for p in subset if bucket_for_lead(p.lead_hours) == fcal.DAY_OF_BUCKET}
        if len(day_of) < self.min_paired_days:
            raise fcal.CalibrationError(
                f"{city} @ {target_date}: only {len(day_of)} day-of paired days available strictly "
                f"before the cutoff {cutoff}; FR-2.2 requires >= {self.min_paired_days}. Refusing to "
                f"price this date rather than fitting a thin calibration and calling it walk-forward."
            )
        payload = fcal.build_city_calibration(
            city=city,
            station=self._station[city],
            timezone_name=CITY_TZ[city],
            source=self.source,
            version=self.version,
            paired=subset,
            drops=self._drops[city],
            forecast_rows_for_city=self._rows_for_city[city],
            forecast_fingerprint=self._forecast_fingerprint,
            truth_fingerprint=self._truth_fingerprint[city],
        )
        payload = fcal.finalize(payload)
        self._cache[key] = payload
        return payload

    def describe(self) -> Dict[str, Any]:
        """What this provider prices from -- for the load log and the parity report."""
        return {
            "kind": self.kind,
            "source": self.source,
            "calibration_dir_sha256": self.sha256,
            "forecast_csv": self.forecast_csv,
            "forecast_sha256": self.forecast_sha256,
            "truth_dir": self.truth_dir,
            "truth_sha256": dict(sorted(self.truth_sha256.items())),
            "embargo_days": self.embargo_days,
            "min_paired_days": self.min_paired_days,
            "archive_last_target_date": {c: self.archive_last_target_date(c) for c in self.cities},
        }


CALIBRATION_PROVIDER_KINDS = ("walk_forward", "frozen")


def build_calibration_provider(
    spec: PromotedSpec,
    directory: str,
    *,
    forecast_csv: Optional[str] = None,
    truth_dir: Optional[str] = None,
) -> Any:
    """The calibration provider ``spec.calibration.kind`` names -- what ``weather_bot`` builds.

    ``walk_forward`` -> :class:`WalkForwardCalibrationProvider` (the provider every
    committed spec's replay parity was proven under); ``frozen`` -> the committed
    payloads. A spec that records NO kind was promoted before the field existed: it gets
    the frozen provider it always got, and the construction guard then refuses paper /
    warns shadow exactly as before. Any other value is a spec error, not a default.
    """
    kind = spec.calibration.kind
    if kind == "walk_forward":
        return WalkForwardCalibrationProvider(
            directory, source=spec.forecast_source, forecast_csv=forecast_csv, truth_dir=truth_dir,
        )
    if kind == "frozen" or kind is None:
        return FrozenCalibrationProvider(directory, source=spec.forecast_source)
    raise GenomeSpecMismatch(
        f"spec.calibration.kind={kind!r} names no calibration provider (known: {CALIBRATION_PROVIDER_KINDS})"
    )


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
        state_dir: Optional[str] = None,
        tick_driven: bool = False,
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
        # (city, hour_epoch) -> HOUR_DONE | HOUR_DONE_LOGGED | HOUR_NO_DATA | "missed:<cause>"
        self._hours: Dict[Tuple[str, int], str] = {}
        # city -> last evaluated hour epoch (missed-hour rule); (city, target_date) -> the
        # (lost hour, cause) that closed it; city -> persisted last hour at construction
        # (downtime detection, consumed on first evaluation)
        self._last_hour: Dict[str, int] = {}
        self._missed_days: Dict[Tuple[str, str], Tuple[int, str]] = {}
        self._resumed: Dict[str, int] = {}
        #: the caller polls every city every grid step and records every hour it is alive
        #: for (the live bot), so an hour with NO record is a stalled loop, not an archive
        #: gap. Replay drivers visit only the hours the archive holds and leave this False.
        self.tick_driven = bool(tick_driven)
        #: set when ``_load_state`` had to start from an empty state (corrupt/unreadable file)
        self.state_recovered_from: Optional[str] = None
        self._warned_context_keys: Set[str] = set()
        self.state_path: Optional[str] = (
            os.path.join(state_dir, STATE_FILE_FMT.format(genome_id=spec.genome_id)) if state_dir else None
        )
        self._prob_cache: Dict[Any, Any] = prob_cache if prob_cache is not None else {}
        #: parity hook: every visible row analyze() evaluates is handed here (replay only)
        self.row_sink = row_sink
        self.stats: Dict[str, int] = {"analyze_calls": 0, "hours_evaluated": 0, "signals": 0, "rejects": 0}
        if quiet_engine:
            # the walk-forward/frozen payloads trigger the engine's fallback WARNINGs on
            # every pricing; lanes/weather.py silences them the same way for the frame
            _engine_logger.setLevel(logging.ERROR)
        self._verify_inputs()
        self._load_state()

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
        # The dir sha above is byte-identical for the walk-forward and frozen providers --
        # they read the SAME files -- so it cannot see a provider substitution. Replay
        # parity is proven under exactly one of them, and the spec now says which. For
        # 0c4b20502f2daf65 the gap is 60 discrepancies / 0.336 of p_yes
        # (reports/factory/replay_parity_bfcf94654a3a_frozen.json vs the control).
        # Paper mode REFUSES a mismatch or an unproven spec; shadow reaches no exchange,
        # so there it is a loud, recorded warning and the run continues.
        self.calibration_kind = getattr(self.calibration_provider, "kind", None)
        self.calibration_kind_ok = (
            spec.calibration.kind is not None and self.calibration_kind == spec.calibration.kind
        )
        if not self.calibration_kind_ok:
            detail = (
                f"calibration provider is {self.calibration_kind!r} but replay parity for this spec "
                f"was proven under {spec.calibration.kind!r}"
                if spec.calibration.kind is not None
                else f"calibration provider is {self.calibration_kind!r} and the spec records no "
                     f"proven kind (promoted before the field existed)"
            )
            if spec.mode == "paper":
                raise GenomeSpecMismatch(
                    f"{detail}; refusing paper mode -- a substituted provider prices a different "
                    f"p_yes from the frame the genome was selected on"
                )
            logger.error(
                "[%s] CALIBRATION PROVIDER MISMATCH (%s): %s. Shadow mode reaches no exchange, so "
                "the run continues, but the emitted set is NOT the frame's trade set.",
                self._name, spec.mode, detail,
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
        hour_epoch = (now_epoch // self.grid_s) * self.grid_s
        ladder = self._ladder_of(data)
        if not ladder:
            # The bot LOOKED and this city has no ladder: a data gap (the frame has
            # no row for the hour either), recorded so the tick-gap rule below can
            # tell it from an hour the strategy was not there for at all.
            gap_city = self._city_key_of(data)
            if gap_city:
                self._mark_hour(gap_city, hour_epoch, HOUR_NO_DATA)
            return []
        city = self._city_of(data, ladder)
        key = (city, hour_epoch)
        state = self._hours.get(key)
        if state in HOUR_EVALUATED:
            # An EVALUATION is the only decision about an hour.
            if state == HOUR_DONE:
                # the bot polls every ~15 s; log the off-grid skip ONCE per (city, hour)
                self._hours[key] = HOUR_DONE_LOGGED
                self._reject(REASON_NOT_TOP_OF_HOUR, ladder[0].symbol, city=city, hour_utc=hour_epoch, evaluated=True)
            return []
        # Everything else the hour may hold -- nothing, HOUR_NO_DATA, or a
        # bot-side ``missed:<cause>`` -- is a report about ONE tick, not a verdict
        # on the hour:
        #   HOUR_NO_DATA -- an earlier tick this hour saw no ladder and this one does;
        #   missed:<cause> -- an earlier tick lost the city (its Kalshi poll raised,
        #     or it never got an observation), and THIS tick has both. An hour is
        #     only truly lost when no in-tolerance tick ever evaluates it, so an
        #     in-tolerance evaluation below upgrades the miss to HOUR_DONE
        #     (``_recover_hour``). Without that, one transient METAR failure at
        #     H+0 s made the identical tick 23 s later -- 97 s INSIDE the
        #     tolerance -- a no-op and closed every visible city-day (D-1/D/D+1,
        #     since the bot's ladder spans three market-days).
        self._prune(hour_epoch)
        if now_epoch - hour_epoch > self.top_of_hour_tolerance_s:
            # ``_mark_hour``, not a bare write: a poll failure already recorded at
            # H+5 s keeps ITS cause rather than being renamed "late_tick" by the
            # tick that shows up at H+3 min (first cause wins). And the reject is
            # logged only for the tick that LOSES the hour -- every later tick of
            # a lost hour is one more duplicate against a 500-line log tail
            # (the bot polls every ~23 s).
            already_missed = _is_missed(state)
            self._mark_hour(city, hour_epoch, _missed(CAUSE_LATE_TICK))
            if not already_missed:
                self._reject(
                    REASON_NOT_TOP_OF_HOUR, ladder[0].symbol, city=city,
                    late_s=int(now_epoch - hour_epoch), tolerance_s=self.top_of_hour_tolerance_s,
                )
            return []
        if _is_missed(state):
            self._recover_hour(city, hour_epoch, str(state), now_epoch)
        self._hours[key] = HOUR_DONE
        self.stats["hours_evaluated"] += 1
        decision_ts = _dt.datetime.fromtimestamp(hour_epoch, _dt.timezone.utc)
        groups = self._group_by_date(ladder)
        prev_hour = self._last_hour.get(city)
        resumed_from = self._resumed.pop(city, None)
        lost_hour, cause = self._lost_chance(city, prev_hour, resumed_from, hour_epoch)
        if lost_hour is not None:
            # the module docstring's missed-hour rule: a chance was lost, so every
            # visible city-day is closed for the rest of the day
            for target_date in groups:
                self._close_day(city, target_date, int(lost_hour), str(cause), hour_epoch)
        self._last_hour[city] = hour_epoch
        self._save_state()

        fee_type = self._fee_type_at(hour_epoch)
        if fee_type != self.spec.fee.fee_type:
            for m in ladder:
                self._reject(REASON_FEE_MISMATCH, m.symbol, fee_type=fee_type, spec=self.spec.fee.fee_type)
            return []

        out: List[TradeSignal] = []
        for target_date, group in groups.items():
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
            miss = self._missed_days.get((city, target_date))
            if miss is not None:
                lost_hour, miss_cause = miss
                self._reject(
                    REASON_MISSED_HOUR, m.symbol, target_date=target_date, hour_utc=_epoch(decision_ts),
                    lost_hour_utc=lost_hour, cause=miss_cause,
                )
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
            self._save_state()
            self.stats["signals"] += 1
            # the quote the limit was derived from, on its own line, so the maia
            # cadence check (scripts/check_maia_emit_cadence.py) can verify
            # limit == quote + adverse_fill in paper mode too (the protected
            # mixin's EMIT line carries no quote)
            logger.info(
                "[Genome] DECIDE strategy=%s symbol=%s contract=%s quote=%.4f limit=%.4f p_win=%.4f target_date=%s",
                self._name, m.symbol, self.contract_side, float(row["quote"]), float(row["price_paid"]),
                float(row["p_win"]), target_date,
            )
            out.append(sig)
        return out

    def _recover_hour(self, city: str, hour_epoch: int, state: str, now_epoch: int) -> None:
        """An in-tolerance tick evaluated an hour an EARLIER tick had recorded as lost.

        The lost record was one tick's report (a failed Kalshi poll, a missing
        observation); this tick has the ladder and is inside the tolerance, so the
        hour is evaluated after all and nothing is forfeited. ``missed_hours``
        therefore counts city-hours still held lost -- it is decremented here and
        the recovery counted separately, so the operator sees both. A
        ``late_tick`` miss cannot reach this: the tolerance gate above returns first.
        """
        cause = _cause_of(state)
        self.stats["missed_hours"] = max(0, self.stats.get("missed_hours", 0) - 1)
        if cause == CAUSE_POLL_FAILURE:
            self.stats["poll_failures"] = max(0, self.stats.get("poll_failures", 0) - 1)
        self.stats["hours_recovered"] = self.stats.get("hours_recovered", 0) + 1
        logger.info(
            "[Genome] MISS RECOVERED strategy=%s city=%s hour_utc=%d cause=%s late_s=%d -- an "
            "in-tolerance tick evaluated the hour after all; no city-day closes",
            self._name, city, int(hour_epoch), cause, int(now_epoch - hour_epoch),
        )

    def _close_day(self, city: str, target_date: str, lost_hour: int, cause: str, hour_epoch: int) -> bool:
        """Close ``(city, target_date)`` for the rest of the market-day and SAY SO once.

        The closure is the only moment the lost hour and its cause are known -- the
        ``GENOME_MISSED_HOUR`` rejects that follow name the current decision hour --
        so it is logged here, exactly once per city-day, at WARNING.

        It also persists itself. ``_analyze`` saves once, BEFORE the per-day loop,
        so the ``vintage_fetch_failure`` closure raised from ``_rows_for_day``
        missed that write entirely: a restart reopened the city-day and re-emitted
        into a day the offline set had already lost.
        """
        key = (city, target_date)
        if key in self._missed_days:
            return False
        self._missed_days[key] = (int(lost_hour), str(cause))
        self.stats["missed_days"] = self.stats.get("missed_days", 0) + 1
        self._save_state()
        logger.warning(
            "[Genome] DAY CLOSED strategy=%s city=%s target_date=%s lost_hour_utc=%d cause=%s hour_utc=%d "
            "-- the mask may already have been true at the lost hour, so every market of this city-day "
            "now rejects %s for the rest of the day",
            self._name, city, target_date, int(lost_hour), cause, int(hour_epoch), REASON_MISSED_HOUR,
        )
        return True

    def _fetch_errors(self) -> int:
        """The forecast provider's cumulative fetch-error count (0 when it keeps none)."""
        stats = getattr(self.forecast_provider, "stats", None)
        try:
            return int(stats.get("fetch_errors", 0)) if stats else 0
        except (AttributeError, TypeError, ValueError):
            return 0

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
                    self._reject(REASON_NOT_EXECUTABLE, m.symbol, cause="bracket_spec", detail=str(exc)[:80])
        if not specs:
            return {}
        errors_before = self._fetch_errors()
        vintage = self.forecast_provider.latest_vintage(city, target_date, decision_ts)
        if vintage is None:
            if reject:
                # Two very different silences (F3 red team). The provider swallows
                # network/archive faults into ``stats["fetch_errors"]`` and returns
                # None either way, so the counter is what tells them apart:
                #   fetch FAILED -> the archive holds the vintage the offline frame
                #     priced this hour with; only the sandbox could not read it, so
                #     the hour is LOST exactly like a failed Kalshi poll and the
                #     city-day closes (it is lost NOW, at an hour already marked
                #     evaluated, so _lost_chance can no longer see it).
                #   no fetch error -> no vintage exists as of this instant; the frame
                #     has no row for it either, so it is a data gap and never a miss.
                fetch_failed = self._fetch_errors() > errors_before
                if fetch_failed:
                    self._close_day(
                        city, target_date, int(_epoch(decision_ts)),
                        CAUSE_VINTAGE_FETCH_FAILURE, int(_epoch(decision_ts)),
                    )
                for sym in specs:
                    self._reject(
                        REASON_NO_VINTAGE, sym, target_date=target_date,
                        as_of=decision_ts.isoformat(), fetch_failed=fetch_failed,
                    )
            return {}
        try:
            probs, width = self._probabilities(city, target_date, vintage, list(specs.values()))
        except (ProbabilityEngineError, ValueError, RuntimeError) as exc:
            # ProbabilityEngineError / CalibrationError / EVAnalysisError-shaped refusals
            # (thin calibration, unbounded ladder...) are a skip, never a crash of the bot
            if reject:
                logger.warning("[%s] %s %s: probability engine refused: %s", self._name, city, target_date, exc)
                for sym in specs:
                    self._reject(REASON_NOT_EXECUTABLE, sym, cause="probability_engine", detail=str(exc)[:80])
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
                    self._reject(REASON_NOT_EXECUTABLE, m.symbol, cause="no_close_time_or_closed")
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
        if yes_ask <= 0.0:
            # kalshi_provider._parse_price returns 0.0 for a missing/null ask; a
            # zero ask is not a quote (features.quote only caps ask < 1.0)
            yes_ask = float("nan")
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

    @staticmethod
    def _city_key_of(data: MarketData) -> Optional[str]:
        """The city from the observation alone (no ladder) -- ``None`` when unknowable.

        The bot stamps ``city_key``/``settlement_station`` on every observation, so
        an EMPTY ladder can still be booked against the right city (the data-gap
        record); a caller that stamps neither simply records nothing.
        """
        extra = data.extra or {}
        city = extra.get("city_key")
        if city:
            return str(city).upper()
        station = extra.get("settlement_station")
        key = city_key_for_station(station) if station else None
        return str(key).upper() if key else None

    def _city_of(self, data: MarketData, ladder: List[MarketData]) -> str:
        city = self._city_key_of(data)
        if city:
            return city
        extra = data.extra or {}
        station = extra.get("settlement_station") or settlement_station_for(ladder[0].symbol)
        key = city_key_for_station(station) if station else None
        if key:
            return str(key).upper()
        # last resort: the series suffix (KXHIGHNY -> NY)
        return self._series_of(ladder[0].symbol)[len(self.series_prefix):]

    def _mark_hour(self, city: str, hour_epoch: int, state: str) -> None:
        """Record what the strategy knows about one city-hour, most-informed wins.

        An evaluation is final (nothing downgrades a ``done`` hour) and a recorded
        miss is never downgraded to a data gap -- the bot can report an empty ladder
        on one tick of an hour and a failed poll on the next. A recorded miss also
        keeps the FIRST cause: the poll failure at H+5 s is what lost the hour, and
        the observation failure (or the late tick) that follows it only inherits an
        hour that was already gone. Overwriting made ``[Genome] DAY CLOSED
        cause=...`` name the wrong one.

        Only an in-tolerance evaluation may raise a miss (``_recover_hour``), and
        it writes ``HOUR_DONE`` directly.
        """
        key = (str(city).upper(), int(hour_epoch))
        current = self._hours.get(key)
        if current in HOUR_EVALUATED or _is_missed(current):
            return
        if state == HOUR_NO_DATA and current is not None:
            return
        self._hours[key] = state

    def _lost_chance(
        self, city: str, prev_hour: Optional[int], resumed_from: Optional[int], hour_epoch: int
    ) -> Tuple[Optional[int], Optional[str]]:
        """The EARLIEST hour before ``hour_epoch`` the strategy HAD and lost, and why.

        Three shapes of lost chance (the module docstring's rule):

        * a recorded miss -- a late tick, a failed Kalshi poll, an observation the
          bot never got. Counts even when no earlier hour was ever evaluated (a
          fresh deploy whose FIRST tick landed late).
        * downtime -- a restart whose persisted last hour is more than one grid step
          back: ``_hours`` is empty in the new process, so the hour after the
          persisted one has no record.
        * a tick gap -- for a ``tick_driven`` caller only. The live bot polls every
          city every tick and records EVERY hour it is alive for (evaluated, late,
          poll failure, or ``no_data``), so an hour with no record at all means the
          loop was not there: a stalled thread, a hung HTTP call, a paused
          container. A replay driver visits only the hours the archive holds, so it
          leaves ``tick_driven`` False and an archive gap stays a data gap -- which
          is what keeps ``factory_replay_parity.py`` exact.
        """
        lower = prev_hour if prev_hour is not None else float("-inf")
        best_hour: Optional[int] = None
        best_cause: Optional[str] = None
        for (c, h), st in self._hours.items():
            if c != city or not _is_missed(st) or not lower < h < hour_epoch:
                continue
            if best_hour is None or h < best_hour:
                best_hour, best_cause = int(h), _cause_of(st)
        start = prev_hour if prev_hour is not None else resumed_from
        if start is not None and (resumed_from is not None or self.tick_driven):
            limit = best_hour if best_hour is not None else hour_epoch
            h = int(start) + self.grid_s
            if resumed_from is None:
                # A tick gap reads the ABSENCE of a record as proof the loop was not
                # there -- which it only is for hours ``_prune`` still keeps. Past
                # the retention horizon the record was DELETED, not never written,
                # so walking further back manufactures a miss out of pruning (a city
                # whose ladder was absent for two days recorded ``no_data`` every
                # hour and still got a ``tick_gap``). ``_prune(hour_epoch)`` ran just
                # above, and it keeps every hour with ``hour_epoch - h <= KEEP_HOURS_S``.
                h = max(h, hour_epoch - KEEP_HOURS_S)
            while h < limit:
                if self._hours.get((city, h)) is None:
                    return h, (CAUSE_DOWNTIME if resumed_from is not None else CAUSE_TICK_GAP)
                h += self.grid_s
        return best_hour, best_cause

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
        cutoff = (_dt.datetime.fromtimestamp(hour_epoch, _dt.timezone.utc)
                  - _dt.timedelta(days=STATE_KEEP_DAYS)).date().isoformat()
        if len(self._traded) > 4096:
            self._traded = {k for k in self._traded if k[0] >= cutoff}
        if self._missed_days:
            self._missed_days = {k: v for k, v in self._missed_days.items() if k[1] >= cutoff}

    # -- persisted state (module docstring) -----------------------------------
    def state_dict(self) -> Dict[str, Any]:
        return {
            "genome_id": self.spec.genome_id,
            "last_hour_epoch": {c: int(h) for c, h in sorted(self._last_hour.items())},
            "missed_days": sorted([list(k) for k in self._missed_days]),
            # Why each day is closed. Additive (2026-09-05): a reader that only knows
            # "missed_days" is unaffected, and a state file written before this key
            # existed loads with cause "unknown".
            "missed_causes": {
                f"{c}|{d}": [int(hour), str(cause)]
                for (c, d), (hour, cause) in sorted(self._missed_days.items())
            },
            "traded": sorted([list(k) for k in self._traded]),
        }

    def _load_state(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError(f"top level is {type(doc).__name__}, not an object")
        except (OSError, ValueError) as exc:
            self._reset_state(exc)
            return
        # A state file naming a DIFFERENT genome is not corruption, it is the wrong
        # file: still a refusal, and RuntimeError is deliberately outside the
        # shape-error tuple below so it cannot be swallowed as "corrupt".
        if doc.get("genome_id") != self.spec.genome_id:
            raise GenomeSpecMismatch(
                f"state file {self.state_path} belongs to genome {doc.get('genome_id')!r}, not {self.spec.genome_id!r}"
            )
        try:
            self._apply_state(doc)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            # A file that DECODES but holds the wrong shapes -- "last_hour_epoch"
            # a list, an hour that is not a number, a "missed_days"/"traded" entry
            # that is not a pair, "missed_causes" not an object -- used to raise
            # straight out of the constructor into WeatherBot.__init__'s broad
            # except, i.e. exactly the deploy-long refusal the unreadable-file
            # recovery below exists to prevent. Same recovery, same loud line.
            self._reset_state(exc)

    def _apply_state(self, doc: Mapping[str, Any]) -> None:
        """Read a decoded state document into the strategy; raises on any wrong shape."""
        self._last_hour = {str(c): int(h) for c, h in dict(doc.get("last_hour_epoch") or {}).items()}
        self._resumed = dict(self._last_hour)
        causes = dict(doc.get("missed_causes") or {})
        missed: Dict[Tuple[str, str], Tuple[int, str]] = {}
        for c, d in (doc.get("missed_days") or []):
            key = (str(c), str(d))
            raw = causes.get(f"{key[0]}|{key[1]}")
            if isinstance(raw, (list, tuple)) and len(raw) == 2:
                missed[key] = (int(raw[0]), str(raw[1]))
            else:  # written before missed_causes existed
                missed[key] = (-1, CAUSE_UNKNOWN)
        traded = {(str(d), str(sym)) for d, sym in (doc.get("traded") or [])}
        self._missed_days = missed
        self._traded = traded

    def _reset_state(self, exc: BaseException) -> None:
        """Start from an empty state and SAY SO (never raise into ``WeatherBot.__init__``).

        A truncated or zero-byte state file is what a Pi power cut leaves behind.
        Starting from an empty state is CONSERVATIVE -- the first evaluated hour
        then has no predecessor, so nothing is claimed as a first masked snapshot
        the process did not see -- but it IS a decision, so it is logged loudly
        here instead of being raised into WeatherBot.__init__'s broad except,
        where one ERROR line refused the whole genome for the rest of the deploy.
        """
        self._last_hour = {}
        self._resumed = {}
        self._missed_days = {}
        self._traded = set()
        self.state_recovered_from = f"{type(exc).__name__}: {exc}"
        self.stats["state_resets"] = self.stats.get("state_resets", 0) + 1
        logger.error(
            "[%s] state file %s is unusable (%s); STARTING FROM AN EMPTY STATE: markets "
            "traded before this restart can be emitted once more and this process has no "
            "predecessor hour for the missed-hour rule",
            self._name, self.state_path, self.state_recovered_from,
        )

    def _save_state(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(self.state_path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.state_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(self.state_dict(), sort_keys=True, indent=2) + "\n")
            # os.replace alone is atomic but not durable: on a Pi power cut the
            # rename can land with the file's blocks still unwritten, which is
            # exactly the zero-byte state file _load_state now has to recover from.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)
        try:  # persist the directory entry too (POSIX; a directory cannot be opened on Windows)
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:  # not supported on this filesystem
            pass
        finally:
            os.close(dir_fd)

    def record_missed_hour(self, city: str, cause: str = CAUSE_POLL_FAILURE) -> None:
        """The bot HAD ``city``'s hour at ``clock()`` and lost it -- record it and say so.

        Recorded exactly like a late tick (``_hours[(city, hour)] = "missed:<cause>"``):
        the market existed and the offline set may hold that hour's trade, so every
        visible city-day is closed at the next evaluated hour
        (``GENOME_MISSED_HOUR``). An hour already evaluated is left alone. Causes the
        bot reports: ``poll_failure`` (its Kalshi call raised) and
        ``observation_failure`` (no observation, so the city was skipped entirely).

        IDEMPOTENT per city-hour, like ``record_no_ladder`` and the once-per-
        city-day ``[Genome] DAY CLOSED`` line. The bot calls this from its tick
        loop, so a sustained METAR/Kalshi outage calls it every ~23 s for the same
        hour: guarding only on ``HOUR_EVALUATED`` re-marked the hour, re-counted
        ``missed_hours``/``poll_failures`` and re-emitted the WARNING on every tick
        (measured: 155 identical lines and ``missed_hours == 155`` for ONE lost
        city-hour, ~620 lines/hour across four cities against a 500-line log tail).
        That flushed the whole diagnostic window with duplicates -- the opposite of
        what the line is for.

        The hour is otherwise INVISIBLE -- the next on-time tick of the same hour
        returns ``[]`` without a reject line -- so this is the one place it can be
        seen; it logs one line.
        """
        hour_epoch = (_epoch(self.clock()) // self.grid_s) * self.grid_s
        city = str(city).upper()
        current = self._hours.get((city, hour_epoch))
        if current in HOUR_EVALUATED or _is_missed(current):
            return
        cause = str(cause or CAUSE_UNKNOWN).strip().replace(" ", "_") or CAUSE_UNKNOWN
        self._mark_hour(city, hour_epoch, _missed(cause))
        self.stats["missed_hours"] = self.stats.get("missed_hours", 0) + 1
        if cause == CAUSE_POLL_FAILURE:
            self.stats["poll_failures"] = self.stats.get("poll_failures", 0) + 1
        logger.warning(
            "[Genome] MISS strategy=%s city=%s hour_utc=%d cause=%s -- the hour is lost; every "
            "city-day visible at the next evaluated hour closes with %s",
            self._name, city, hour_epoch, cause, REASON_MISSED_HOUR,
        )

    def record_poll_failure(self, city: str) -> None:
        """The bot's ladder poll for ``city`` failed: a lost chance (``poll_failure``)."""
        self.record_missed_hour(city, CAUSE_POLL_FAILURE)

    def record_no_ladder(self, city: str) -> None:
        """The bot LOOKED at ``clock()``'s hour and ``city`` had no ladder: a DATA gap.

        The mirror image of ``record_missed_hour`` and deliberately NOT a miss: the
        offline frame has no row for an hour with no candle either, so claiming one
        would break replay parity. It is recorded rather than ignored so the tick-gap
        rule can tell "the bot looked and there was nothing" from "the bot was not
        there" (``_lost_chance``). An hour already evaluated or already lost keeps
        what it has.
        """
        hour_epoch = (_epoch(self.clock()) // self.grid_s) * self.grid_s
        self._mark_hour(city, hour_epoch, HOUR_NO_DATA)

    def _reject(self, code: str, symbol: str, **context: Any) -> None:
        self.stats["rejects"] += 1
        log_rejection(code, self._name, symbol, **self._safe_context(context))

    def _safe_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Rename any context key that collides with ``log_rejection``'s positionals.

        ``_reject`` forwards reason/strategy/symbol positionally, so a caller's
        ``reason="probability_engine"`` used to raise ``TypeError: got multiple
        values for argument 'reason'`` -- inside ``analyze()``, i.e. it crashed the
        bot's tick on the path documented as "a skip, never a crash of the bot".
        The colliding key is renamed (``ctx_<key>``), never dropped, and the
        collision is logged once per key so the class of bug cannot recur silently.
        """
        if not context or RESERVED_CONTEXT_KEYS.isdisjoint(context):
            return context
        out: Dict[str, Any] = {}
        for key, value in context.items():
            if key in RESERVED_CONTEXT_KEYS:
                if key not in self._warned_context_keys:
                    self._warned_context_keys.add(key)
                    logger.warning(
                        "[%s] reject context key %r collides with log_rejection(%s); logged as %r "
                        "(a caller bug: rename it at the call site)",
                        self._name, key, ", ".join(sorted(RESERVED_CONTEXT_KEYS)), f"ctx_{key}",
                    )
                key = f"ctx_{key}"
            out[key] = value
        return out


__all__ = [
    "CAUSE_DOWNTIME",
    "CAUSE_LATE_TICK",
    "CAUSE_OBSERVATION_FAILURE",
    "CAUSE_POLL_FAILURE",
    "CAUSE_TICK_GAP",
    "CAUSE_VINTAGE_FETCH_FAILURE",
    "DEFAULT_TOP_OF_HOUR_TOLERANCE_S",
    "FrozenCalibrationProvider",
    "GenomeSpecMismatch",
    "GenomeStrategy",
    "HOUR_DONE",
    "HOUR_MISSED_PREFIX",
    "HOUR_NO_DATA",
    "LADDER_KEY",
    "REASON_ALREADY_TRADED",
    "REASON_FEE_MISMATCH",
    "REASON_MASK_FALSE",
    "REASON_MISSED_HOUR",
    "REASON_NOT_EXECUTABLE",
    "REASON_NOT_TOP_OF_HOUR",
    "REASON_NO_VINTAGE",
    "REASON_SHADOW",
    "REASON_SIGMA_CAP",
    "REGIME_SINGLE",
    "RESERVED_CONTEXT_KEYS",
    "STATE_FILE_FMT",
    "SUPPORT_SIGMAS",
    "bracket_edge_distance_f",
    "bracket_midpoint_f",
    "ladder_core_width_f",
]
