"""Kalshi recorded-market-history backfill for the Phase 2 go/no-go EV report.

Why this module exists
----------------------
PRD Phase 2 exit criterion 5 requires the go/no-go EV report to be computed
"on >=30 days of recorded ladders", under **both maker and taker pricing**.
Maker pricing needs a resting-order price, taker pricing needs the price you
would lift -- i.e. bid AND ask. The VM's own harvest CSVs cannot supply that:
of 803 archived ``logs/data_*.csv`` files, 776 carry only a single ``Price``
column, 27 carry bid/ask, and exactly one carries the FR-1.1 bracket columns.
So the ladder history is sourced from **Kalshi's own recorded market history**
instead, via the public candlesticks endpoint.

Upstream contract (verified live, anonymously, 2026-07-26)
----------------------------------------------------------
``GET /trade-api/v2/series/{series}/markets/{market}/candlesticks``
    Query: ``start_ts``, ``end_ts`` (unix seconds), ``period_interval``
    in ``{1, 60, 1440}`` minutes. **Max 5000 candlesticks per response** --
    a wider range returns HTTP 400 ``max candlesticks: 5000``, so
    :meth:`KalshiHistoryClient.fetch_candlesticks` chunks the range.

    Each ``candlesticks[]`` entry::

        {"end_period_ts": 1784214000,
         "open_interest_fp": "189.53", "volume_fp": "205.53",
         "price":   {"open_dollars": "...", "high_dollars": "...",
                     "low_dollars": "...", "close_dollars": "...",
                     "mean_dollars": "...", "previous_dollars": "..."},
         "yes_bid": {"open_dollars": "...", "high_dollars": "...",
                     "low_dollars": "...", "close_dollars": "..."},
         "yes_ask": {"open_dollars": "...", "high_dollars": "...",
                     "low_dollars": "...", "close_dollars": "..."}}

    **The nested keys end in ``_dollars``.** ``c["yes_ask"]["close"]`` is
    ``None`` -- a trap that already bit one probe. Always go through
    :func:`_candle_dollars`.

``GET /trade-api/v2/markets?event_ticker={event}&limit=200``
    Market metadata: ``ticker``, ``status``, ``strike_type``,
    ``floor_strike``, ``cap_strike``, ``yes_sub_title``, ``open_time``,
    ``close_time``, and for settled markets ``result`` and
    ``expiration_value``. Note ``volume`` / ``open_interest`` are ``None`` on
    this endpoint; the populated fields are ``volume_fp`` /
    ``open_interest_fp``.

    Event tickers are ``{SERIES}-{%y%b%d uppercased}``, e.g.
    ``KXHIGHNY-26JUL17``.

**Retention.** Measured 2026-07-26 by bisection: ``/markets`` (and the
``/events?with_nested_markets=true`` variant) return markets only for events
whose target date is >= **2026-05-18**. Older events still resolve HTTP 200
from ``/events`` but carry ``markets: []``, so their bracket semantics and
settlement results are unrecoverable and no ladder can be built for them.
That window is a hard upstream limit, not a bug in this module; the backfill
reports the earliest date it could actually retrieve.

The NO side
-----------
Kalshi quotes only the YES book over this endpoint. The NO side is exact, not
modelled: a resting YES ask at ``a`` **is** a NO bid at ``1 - a``, and a
resting YES bid at ``b`` **is** a NO ask at ``1 - b``, because a YES and a NO
contract on the same market sum to exactly $1.00 at settlement. Hence::

    no_bid = 1 - yes_ask
    no_ask = 1 - yes_bid

No spread is invented, nothing is interpolated.

No-quote sentinels
------------------
An empty book is reported as ``yes_ask = 1.0000`` and/or ``yes_bid = 0.0000``
-- the widest possible quote, not a tradeable price. Those are counted as
"no quote" (``has_quote = False``) and never silently treated as a fill price.
A missing field stays ``None``; it is never coerced to 0.

Bracket semantics
-----------------
``strike_type`` / ``floor_strike`` / ``cap_strike`` come straight from the API
and are settled through :mod:`src.core.bracket_payoff` (PRD FR-1.1/FR-1.2).
Nothing here inspects a ticker suffix letter.

PRD FR-2.4 / Phase 2 exit criterion 5. Workstream C.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.bracket_payoff import (
    BracketSpec,
    BracketSpecError,
    parse_bracket_spec,
    settles_yes,
)
from src.data.kalshi_provider import KalshiProvider

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LADDER_DIR = _PROJECT_ROOT / "data" / "ladders"
MANIFEST_PATH = LADDER_DIR / "manifest.json"
TRUTH_DIR = _PROJECT_ROOT / "data" / "weather_truth"

API_BASE = KalshiProvider.PUBLIC_API_URL

# Kalshi rate limit: stay under 10 req/s. Same 0.12s floor the Phase 0
# harvester uses (src/data/harvester.py::_MIN_REQUEST_INTERVAL).
MIN_REQUEST_INTERVAL = 0.12

# Upstream hard cap on one candlesticks response.
MAX_CANDLES_PER_REQUEST = 5000

# Hourly resolution: 24-39 candles over a weather market's ~39-hour life.
DEFAULT_PERIOD_INTERVAL = 60

VALID_PERIOD_INTERVALS = (1, 60, 1440)

# Earliest event date for which /markets still returns market metadata,
# measured by bisection on 2026-07-26. Advisory only -- the backfill probes
# and records what it actually got; it does not assume this bound.
OBSERVED_RETENTION_FLOOR = dt.date(2026, 5, 18)

# (city_key, series_ticker, settlement_station). Mirrors
# src.bots.weather_bot.WEATHER_CITIES (PRD FR-1.4) without importing the bot
# module into a data-layer script.
WEATHER_CITY_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("NY", "KXHIGHNY", "KNYC"),
    ("CHI", "KXHIGHCHI", "KMDW"),
    ("LAX", "KXHIGHLAX", "KLAX"),
    ("MIA", "KXHIGHMIA", "KMIA"),
)

LADDER_COLUMNS: Tuple[str, ...] = (
    "series",
    "city",
    "station",
    "target_date",
    "event_ticker",
    "market_ticker",
    "ts_utc",
    "minutes_to_close",
    "close_time_utc",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "yes_sub_title",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "last",
    "price_mean",
    "yes_bid_low",
    "yes_ask_high",
    "volume",
    "open_interest",
    "has_quote",
    "result",
    "expiration_value",
    "cli_high",
    "recomputed_yes_expval",
    "recomputed_yes_cli",
    "payoff_matches_kalshi",
    "truth_agrees",
)


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------


def event_ticker_for(series: str, target_date: dt.date) -> str:
    """``("KXHIGHNY", date(2026, 7, 17))`` -> ``"KXHIGHNY-26JUL17"``."""
    return f"{series}-{target_date.strftime('%y%b%d').upper()}"


def _optional_float(value: Any) -> Optional[float]:
    """Float or ``None``. A blank/absent/NaN value stays ``None``.

    Never returns 0.0 for a missing input: a missing quote that reads as
    ``0.00`` is a free contract in an EV report.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _candle_dollars(
    candle: Mapping[str, Any], node: str, field_: str
) -> Optional[float]:
    """Read ``candle[node][f"{field_}_dollars"]`` as a float.

    The ``_dollars`` suffix is mandatory: ``candle["yes_ask"]["close"]``
    returns ``None`` on the live API even when a quote exists.
    """
    sub = candle.get(node)
    if not isinstance(sub, Mapping):
        return None
    return _optional_float(sub.get(f"{field_}_dollars"))


def is_quoted(yes_bid: Optional[float], yes_ask: Optional[float]) -> bool:
    """True when BOTH sides carry a tradeable quote.

    Kalshi reports an empty book as ``yes_bid = 0.0000`` and/or
    ``yes_ask = 1.0000`` -- the widest possible quote. Those sentinels mean
    "nobody is there", not "you can buy YES for a dollar".
    """
    if yes_bid is None or yes_ask is None:
        return False
    return yes_bid > 0.0 and yes_ask < 1.0


def no_side_from_yes(
    yes_bid: Optional[float], yes_ask: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    """``(no_bid, no_ask)`` from the YES book -- an exact identity, not a model.

    A YES and a NO contract on the same market pay exactly $1.00 between them,
    so a resting YES ask at ``a`` is a NO bid at ``1 - a`` and a resting YES
    bid at ``b`` is a NO ask at ``1 - b``. ``None`` propagates.
    """
    no_bid = None if yes_ask is None else round(1.0 - yes_ask, 4)
    no_ask = None if yes_bid is None else round(1.0 - yes_bid, 4)
    return no_bid, no_ask


# ----------------------------------------------------------------------
# HTTP client
# ----------------------------------------------------------------------


class KalshiHistoryError(RuntimeError):
    """A history fetch failed in a way the caller must record, not paper over."""


@dataclass
class RequestRecord:
    """One HTTP call, for the provenance manifest."""

    url: str
    params: Dict[str, Any]
    status: Optional[int]
    fetched_at_utc: str
    error: Optional[str] = None
    items: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "url": self.url,
            "params": self.params,
            "status": self.status,
            "fetched_at_utc": self.fetched_at_utc,
        }
        if self.items is not None:
            out["items"] = self.items
        if self.error:
            out["error"] = self.error
        return out


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class KalshiHistoryClient:
    """Read-only client for Kalshi's recorded market history.

    Works anonymously -- every endpoint used here is public. When a
    :class:`~src.data.kalshi_provider.KalshiProvider` is supplied its
    ``_get_authenticated_headers`` is reused so an authenticated deployment
    signs requests identically; ``KALSHI_KEY_ID`` being empty simply means
    anonymous mode, which is fine and preferred for a bulk read.

    Requests are globally throttled to :data:`MIN_REQUEST_INTERVAL`
    (~8.3 req/s ceiling) even across threads, matching the Phase 0 harvester's
    discipline.
    """

    def __init__(
        self,
        provider: Optional[KalshiProvider] = None,
        api_base: str = API_BASE,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        self.provider = provider or KalshiProvider()
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = self.provider.session
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self.requests: List[RequestRecord] = []
        self._records_lock = threading.Lock()

    # -- plumbing ------------------------------------------------------

    def _throttle(self) -> None:
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - elapsed)
            self._last_request = time.monotonic()

    def _record(self, rec: RequestRecord) -> None:
        with self._records_lock:
            self.requests.append(rec)

    def _get(self, path: str, params: Dict[str, Any]) -> Tuple[int, Any]:
        """GET ``path`` with retries. Returns ``(status_code, json_or_text)``.

        A non-200 is returned, not raised: the caller has to record it in the
        manifest. Only transport errors after ``max_retries`` raise.
        """
        url = f"{self.api_base}{path}"
        headers = self.provider._get_authenticated_headers("GET", path)
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(
                    url, headers=headers, params=params, timeout=self.timeout
                )
            except Exception as exc:  # transport-level
                last_exc = exc
                time.sleep(min(2.0 * (attempt + 1), 5.0))
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_exc = KalshiHistoryError(f"HTTP {resp.status_code} on {path}")
                time.sleep(min(2.0 * (attempt + 1), 5.0))
                continue
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, resp.text
        self._record(
            RequestRecord(url, dict(params), None, _utcnow_iso(), str(last_exc))
        )
        raise KalshiHistoryError(
            f"GET {url} failed after {self.max_retries}: {last_exc}"
        )

    # -- endpoints -----------------------------------------------------

    def fetch_event_markets(
        self, series: str, target_date: dt.date
    ) -> Tuple[List[dict], RequestRecord]:
        """Every market in one city-day's bracket ladder.

        Returns ``([], record)`` -- explicitly empty, never an exception --
        when the event is outside Kalshi's metadata retention window, so the
        manifest can record the day as retrieved-and-empty rather than skipped.
        """
        event = event_ticker_for(series, target_date)
        path = "/markets"
        params = {"event_ticker": event, "limit": 200}
        status, payload = self._get(path, params)
        markets: List[dict] = []
        error = None
        if status == 200 and isinstance(payload, dict):
            markets = payload.get("markets") or []
        else:
            error = f"HTTP {status}"
        rec = RequestRecord(
            url=f"{self.api_base}{path}",
            params=dict(params),
            status=status,
            fetched_at_utc=_utcnow_iso(),
            error=error,
            items=len(markets),
        )
        self._record(rec)
        return markets, rec

    def fetch_candlesticks(
        self,
        series: str,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = DEFAULT_PERIOD_INTERVAL,
    ) -> Tuple[List[dict], List[RequestRecord]]:
        """All candlesticks for one market over ``[start_ts, end_ts]``.

        Chunks the range so no single request can exceed
        :data:`MAX_CANDLES_PER_REQUEST` (the upstream 400 boundary), then
        de-duplicates on ``end_period_ts`` and sorts ascending.
        """
        if period_interval not in VALID_PERIOD_INTERVALS:
            raise ValueError(
                f"period_interval must be one of {VALID_PERIOD_INTERVALS}, "
                f"got {period_interval}"
            )
        path = f"/series/{series}/markets/{market_ticker}/candlesticks"
        span = period_interval * 60 * (MAX_CANDLES_PER_REQUEST - 1)
        out: Dict[int, dict] = {}
        records: List[RequestRecord] = []
        cursor_ts = int(start_ts)
        end_ts = int(end_ts)
        while cursor_ts <= end_ts:
            chunk_end = min(cursor_ts + span, end_ts)
            params = {
                "start_ts": cursor_ts,
                "end_ts": chunk_end,
                "period_interval": period_interval,
            }
            status, payload = self._get(path, params)
            candles: List[dict] = []
            error = None
            if status == 200 and isinstance(payload, dict):
                candles = payload.get("candlesticks") or []
            else:
                error = f"HTTP {status}: {str(payload)[:200]}"
            rec = RequestRecord(
                url=f"{self.api_base}{path}",
                params=dict(params),
                status=status,
                fetched_at_utc=_utcnow_iso(),
                error=error,
                items=len(candles),
            )
            self._record(rec)
            records.append(rec)
            for c in candles:
                ts = c.get("end_period_ts")
                if ts is not None:
                    out[int(ts)] = c
            cursor_ts = chunk_end + 1
        return [out[k] for k in sorted(out)], records

    def fetch_series_meta(self, series: str) -> Tuple[Optional[dict], RequestRecord]:
        """``/series/{series}`` -- carries ``fee_type`` and ``fee_multiplier``."""
        path = f"/series/{series}"
        status, payload = self._get(path, {})
        meta = None
        if status == 200 and isinstance(payload, dict):
            meta = payload.get("series")
        rec = RequestRecord(
            url=f"{self.api_base}{path}",
            params={},
            status=status,
            fetched_at_utc=_utcnow_iso(),
            error=None if status == 200 else f"HTTP {status}",
            items=1 if meta else 0,
        )
        self._record(rec)
        return meta, rec


# ----------------------------------------------------------------------
# Ground truth (read-only join)
# ----------------------------------------------------------------------


def load_cli_truth(station: str, truth_dir: Path = TRUTH_DIR) -> Dict[str, float]:
    """``{"2026-07-17": 86.0, ...}`` from the Phase 1 CLI truth CSV.

    Read-only. Missing file or unparseable high -> the date is simply absent,
    so the caller records "no truth" rather than inventing one.
    """
    path = Path(truth_dir) / f"cli_daily_high_{station}.csv"
    out: Dict[str, float] = {}
    if not path.exists():
        logger.warning("CLI truth file missing: %s", path)
        return out
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            high = _optional_float(row.get("high"))
            date = (row.get("date") or "").strip()
            if date and high is not None:
                out[date] = high
    return out


# ----------------------------------------------------------------------
# Row construction
# ----------------------------------------------------------------------


@dataclass
class DayResult:
    """Everything one (city, date) backfill produced, for CSV + manifest."""

    series: str
    city: str
    station: str
    target_date: dt.date
    event_ticker: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    markets: int = 0
    markets_with_candles: int = 0
    payoff_checked: int = 0
    payoff_matched: int = 0
    payoff_checked_cli: int = 0
    payoff_matched_cli: int = 0
    missing_expiration_value: List[str] = field(default_factory=list)
    truth_checked: int = 0
    truth_disagreements: List[Dict[str, Any]] = field(default_factory=list)
    bracket_spec_errors: List[str] = field(default_factory=list)
    market_detail: List[Dict[str, Any]] = field(default_factory=list)
    http_failures: List[Dict[str, Any]] = field(default_factory=list)
    empty_reason: Optional[str] = None
    fetched_at_utc: str = field(default_factory=_utcnow_iso)

    @property
    def empty(self) -> bool:
        return not self.rows


def _spec_or_none(
    market: Mapping[str, Any], errors: List[str]
) -> Optional[BracketSpec]:
    """Bracket spec from API fields; records the error instead of guessing."""
    ticker = str(market.get("ticker") or "<unknown>")
    try:
        return parse_bracket_spec(
            ticker,
            {
                "strike_type": market.get("strike_type"),
                "floor_strike": market.get("floor_strike"),
                "cap_strike": market.get("cap_strike"),
            },
        )
    except BracketSpecError as exc:
        errors.append(str(exc))
        return None


def _settles_yes_or_none(spec: Optional[BracketSpec], high: Optional[float]):
    if spec is None or high is None:
        return None
    try:
        return settles_yes(spec, high)
    except BracketSpecError:
        return None


def build_day_rows(
    client: KalshiHistoryClient,
    series: str,
    city: str,
    station: str,
    target_date: dt.date,
    truth: Mapping[str, float],
    period_interval: int = DEFAULT_PERIOD_INTERVAL,
) -> DayResult:
    """Fetch and assemble one city-day's ladder history.

    Every market in the event ladder contributes one row per candlestick.
    A day whose event returns zero markets comes back with ``rows == []`` and
    an ``empty_reason`` -- it is reported, never dropped.
    """
    event = event_ticker_for(series, target_date)
    result = DayResult(
        series=series,
        city=city,
        station=station,
        target_date=target_date,
        event_ticker=event,
    )
    date_str = target_date.isoformat()
    cli_high = truth.get(date_str)

    try:
        markets, rec = client.fetch_event_markets(series, target_date)
    except KalshiHistoryError as exc:
        result.empty_reason = f"market metadata fetch failed: {exc}"
        result.http_failures.append({"stage": "markets", "error": str(exc)})
        return result
    if rec.error:
        result.http_failures.append(
            {"stage": "markets", "error": rec.error, "params": rec.params}
        )
    result.markets = len(markets)
    if not markets:
        result.empty_reason = (
            "event returned zero markets (outside Kalshi's market-metadata "
            "retention window, or the event never existed)"
        )
        return result

    # Sort by ticker: /markets does NOT guarantee a stable order (measured
    # 2026-07-27, one city-day of 276 came back permuted between two runs),
    # and an unsorted iteration makes the CSVs non-reproducible byte-for-byte
    # even when every value is identical.
    for market in sorted(markets, key=lambda m: str(m.get("ticker") or "")):
        ticker = str(market.get("ticker") or "")
        spec = _spec_or_none(market, result.bracket_spec_errors)
        expiration_value = _optional_float(market.get("expiration_value"))
        kalshi_result = (market.get("result") or "").strip().lower() or None

        recomputed_expval = _settles_yes_or_none(spec, expiration_value)
        recomputed_cli = _settles_yes_or_none(spec, cli_high)
        payoff_matches = None
        if recomputed_expval is not None and kalshi_result in ("yes", "no"):
            payoff_matches = recomputed_expval == (kalshi_result == "yes")
            result.payoff_checked += 1
            result.payoff_matched += int(payoff_matches)
        elif kalshi_result in ("yes", "no"):
            # Kalshi occasionally publishes a settled market with a BLANK
            # expiration_value. Recorded, not hidden: the market is simply not
            # part of the expiration_value-based agreement denominator.
            result.missing_expiration_value.append(ticker)
        # Independent second check: recompute against the Phase 1 CLI truth.
        # It covers exactly the markets the expiration_value check cannot.
        if recomputed_cli is not None and kalshi_result in ("yes", "no"):
            result.payoff_checked_cli += 1
            result.payoff_matched_cli += int(recomputed_cli == (kalshi_result == "yes"))
        truth_agrees = None
        if expiration_value is not None and cli_high is not None:
            result.truth_checked += 1
            truth_agrees = abs(expiration_value - cli_high) < 1e-9
            if not truth_agrees:
                result.truth_disagreements.append(
                    {
                        "market_ticker": ticker,
                        "kalshi_expiration_value": expiration_value,
                        "cli_high": cli_high,
                        "station": station,
                        "target_date": date_str,
                    }
                )

        open_time = str(market.get("open_time") or "")
        close_time = str(market.get("close_time") or "")
        start_dt = _parse_iso(open_time)
        close_dt = _parse_iso(close_time)
        if start_dt is None or close_dt is None:
            result.market_detail.append(
                {
                    "market_ticker": ticker,
                    "candles": 0,
                    "error": f"unparseable open/close time ({open_time!r}/{close_time!r})",
                }
            )
            continue

        try:
            candles, crecs = client.fetch_candlesticks(
                series,
                ticker,
                int(start_dt.timestamp()),
                int(close_dt.timestamp()),
                period_interval=period_interval,
            )
        except KalshiHistoryError as exc:
            result.http_failures.append(
                {"stage": "candlesticks", "market_ticker": ticker, "error": str(exc)}
            )
            result.market_detail.append(
                {"market_ticker": ticker, "candles": 0, "error": str(exc)}
            )
            continue
        for cr in crecs:
            if cr.error:
                result.http_failures.append(
                    {
                        "stage": "candlesticks",
                        "market_ticker": ticker,
                        "error": cr.error,
                        "params": cr.params,
                    }
                )
        result.market_detail.append(
            {
                "market_ticker": ticker,
                "strike_type": market.get("strike_type"),
                "floor_strike": market.get("floor_strike"),
                "cap_strike": market.get("cap_strike"),
                "result": kalshi_result,
                "candles": len(candles),
                "requests": len(crecs),
            }
        )
        if candles:
            result.markets_with_candles += 1

        for c in candles:
            ts = c.get("end_period_ts")
            if ts is None:
                continue
            ts_dt = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
            yes_bid = _candle_dollars(c, "yes_bid", "close")
            yes_ask = _candle_dollars(c, "yes_ask", "close")
            no_bid, no_ask = no_side_from_yes(yes_bid, yes_ask)
            result.rows.append(
                {
                    "series": series,
                    "city": city,
                    "station": station,
                    "target_date": date_str,
                    "event_ticker": event,
                    "market_ticker": ticker,
                    "ts_utc": ts_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "minutes_to_close": round(
                        (close_dt - ts_dt).total_seconds() / 60.0, 1
                    ),
                    "close_time_utc": close_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "strike_type": market.get("strike_type"),
                    "floor_strike": _optional_float(market.get("floor_strike")),
                    "cap_strike": _optional_float(market.get("cap_strike")),
                    "yes_sub_title": market.get("yes_sub_title"),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "last": _candle_dollars(c, "price", "close"),
                    "price_mean": _candle_dollars(c, "price", "mean"),
                    "yes_bid_low": _candle_dollars(c, "yes_bid", "low"),
                    "yes_ask_high": _candle_dollars(c, "yes_ask", "high"),
                    "volume": _optional_float(c.get("volume_fp")),
                    "open_interest": _optional_float(c.get("open_interest_fp")),
                    "has_quote": is_quoted(yes_bid, yes_ask),
                    "result": kalshi_result,
                    "expiration_value": expiration_value,
                    "cli_high": cli_high,
                    "recomputed_yes_expval": recomputed_expval,
                    "recomputed_yes_cli": recomputed_cli,
                    "payoff_matches_kalshi": payoff_matches,
                    "truth_agrees": truth_agrees,
                }
            )
    return result


def _parse_iso(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------


def day_csv_path(series: str, target_date: dt.date, root: Path = LADDER_DIR) -> Path:
    """``data/ladders/KXHIGHNY/2026-07-17.csv``.

    One file per (city, target_date): a partial rerun rewrites exactly one
    day, and per-day row counts in the manifest are verifiable against the
    files on disk.
    """
    return Path(root) / series / f"{target_date.isoformat()}.csv"


def write_day_csv(result: DayResult, root: Path = LADDER_DIR) -> Optional[Path]:
    """Write one day's rows. Returns ``None`` for an empty day (no file)."""
    if not result.rows:
        return None
    path = day_csv_path(result.series, result.target_date, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=list(LADDER_COLUMNS), extrasaction="ignore"
        )
        writer.writeheader()
        for row in result.rows:
            writer.writerow({k: _csv_value(row.get(k)) for k in LADDER_COLUMNS})
    return path


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


# ----------------------------------------------------------------------
# Loader (the workstream-E entry point)
# ----------------------------------------------------------------------


def load_ladders(
    root: Path = LADDER_DIR,
    cities: Optional[Sequence[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    quoted_only: bool = False,
):
    """Load the backfilled ladder history as a :class:`pandas.DataFrame`.

    Parameters
    ----------
    root
        Ladder directory; defaults to ``data/ladders``.
    cities
        City keys (``"NY"``, ``"CHI"``, ``"LAX"``, ``"MIA"``) or series
        tickers (``"KXHIGHNY"`` ...). ``None`` loads all.
    start_date, end_date
        Inclusive ``YYYY-MM-DD`` bounds on ``target_date``.
    quoted_only
        When ``True``, keep only rows with a two-sided quote
        (``has_quote``). Default ``False`` so the caller sees -- and has to
        decide about -- the unquoted rows rather than having them vanish.

    Returns
    -------
    pandas.DataFrame
        Columns are :data:`LADDER_COLUMNS`. ``yes_bid`` / ``yes_ask`` /
        ``no_bid`` / ``no_ask`` / ``last`` are floats in ``[0, 1]`` and are
        ``NaN`` where the API reported no value -- never 0.0-as-missing.
        ``ts_utc`` and ``close_time_utc`` are tz-aware UTC timestamps.
        Empty DataFrame (with the right columns) when nothing matches.
    """
    import pandas as pd

    root = Path(root)
    wanted_series: Optional[set] = None
    if cities:
        by_key = {c: s for c, s, _ in WEATHER_CITY_SPECS}
        wanted_series = set()
        for c in cities:
            wanted_series.add(by_key.get(str(c).upper(), str(c).upper()))

    frames = []
    for series_dir in sorted(p for p in root.glob("*") if p.is_dir()):
        if wanted_series and series_dir.name not in wanted_series:
            continue
        for csv_path in sorted(series_dir.glob("*.csv")):
            day = csv_path.stem
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                continue
            frames.append(pd.read_csv(csv_path))

    if not frames:
        return pd.DataFrame(columns=list(LADDER_COLUMNS))

    df = pd.concat(frames, ignore_index=True)
    for col in (
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "last",
        "price_mean",
        "yes_bid_low",
        "yes_ask_high",
        "volume",
        "open_interest",
        "floor_strike",
        "cap_strike",
        "expiration_value",
        "cli_high",
        "minutes_to_close",
    ):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in (
        "has_quote",
        "recomputed_yes_expval",
        "recomputed_yes_cli",
        "payoff_matches_kalshi",
        "truth_agrees",
    ):
        if col in df.columns:
            df[col] = df[col].map(
                {"true": True, "false": False, True: True, False: False}
            )
    for col in ("ts_utc", "close_time_utc"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    if quoted_only:
        df = df[df["has_quote"] == True].reset_index(drop=True)  # noqa: E712
    return df.sort_values(
        ["target_date", "series", "market_ticker", "ts_utc"]
    ).reset_index(drop=True)


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    """Provenance manifest written alongside the ladder CSVs."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------------
# Backfill driver
# ----------------------------------------------------------------------


def date_range(start: dt.date, end: dt.date) -> List[dt.date]:
    """Inclusive list of dates from ``start`` to ``end``."""
    if end < start:
        return []
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def backfill(
    start: dt.date,
    end: dt.date,
    city_specs: Sequence[Tuple[str, str, str]] = WEATHER_CITY_SPECS,
    root: Path = LADDER_DIR,
    period_interval: int = DEFAULT_PERIOD_INTERVAL,
    client: Optional[KalshiHistoryClient] = None,
    truth_dir: Path = TRUTH_DIR,
    progress: Optional[Any] = None,
) -> dict:
    """Backfill every (city, date) in the range and write CSVs + manifest.

    Returns the manifest dict. Every requested day appears in
    ``manifest["days"]`` -- including days that came back empty, with the
    reason. Nothing is silently skipped.
    """
    client = client or KalshiHistoryClient()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    truths = {
        station: load_cli_truth(station, truth_dir) for _, _, station in city_specs
    }

    days_meta: List[Dict[str, Any]] = []
    totals = {
        "rows": 0,
        "markets": 0,
        "days_requested": 0,
        "days_with_rows": 0,
        "days_empty": 0,
        "payoff_checked": 0,
        "payoff_matched": 0,
        "payoff_checked_cli": 0,
        "payoff_matched_cli": 0,
        "markets_missing_expiration_value": 0,
        "truth_checked": 0,
        "truth_disagreements": 0,
        "quoted_rows": 0,
    }
    truth_disagreements: List[Dict[str, Any]] = []
    bracket_spec_errors: List[str] = []
    http_failures: List[Dict[str, Any]] = []
    empty_days: List[str] = []
    missing_expiration_value: List[str] = []

    for target_date in date_range(start, end):
        for city, series, station in city_specs:
            totals["days_requested"] += 1
            res = build_day_rows(
                client,
                series,
                city,
                station,
                target_date,
                truths.get(station, {}),
                period_interval=period_interval,
            )
            path = write_day_csv(res, root)
            quoted = sum(1 for r in res.rows if r.get("has_quote"))
            totals["rows"] += len(res.rows)
            totals["markets"] += res.markets
            totals["payoff_checked"] += res.payoff_checked
            totals["payoff_matched"] += res.payoff_matched
            totals["payoff_checked_cli"] += res.payoff_checked_cli
            totals["payoff_matched_cli"] += res.payoff_matched_cli
            totals["markets_missing_expiration_value"] += len(
                res.missing_expiration_value
            )
            missing_expiration_value.extend(res.missing_expiration_value)
            totals["truth_checked"] += res.truth_checked
            totals["truth_disagreements"] += len(res.truth_disagreements)
            totals["quoted_rows"] += quoted
            truth_disagreements.extend(res.truth_disagreements)
            bracket_spec_errors.extend(res.bracket_spec_errors)
            for f in res.http_failures:
                http_failures.append(
                    dict(f, series=series, target_date=target_date.isoformat())
                )
            if res.rows:
                totals["days_with_rows"] += 1
            else:
                totals["days_empty"] += 1
                empty_days.append(f"{series}-{target_date.isoformat()}")
            days_meta.append(
                {
                    "series": series,
                    "city": city,
                    "station": station,
                    "target_date": target_date.isoformat(),
                    "event_ticker": res.event_ticker,
                    "markets": res.markets,
                    "markets_with_candles": res.markets_with_candles,
                    "rows": len(res.rows),
                    "quoted_rows": quoted,
                    "payoff_checked": res.payoff_checked,
                    "payoff_matched": res.payoff_matched,
                    "payoff_checked_cli": res.payoff_checked_cli,
                    "payoff_matched_cli": res.payoff_matched_cli,
                    "missing_expiration_value": res.missing_expiration_value,
                    "truth_checked": res.truth_checked,
                    "truth_disagreements": res.truth_disagreements,
                    "bracket_spec_errors": res.bracket_spec_errors,
                    "market_detail": res.market_detail,
                    "http_failures": res.http_failures,
                    "empty": res.empty,
                    "empty_reason": res.empty_reason,
                    "csv": str(path.relative_to(root)) if path else None,
                    "fetched_at_utc": res.fetched_at_utc,
                }
            )
            if progress:
                progress(days_meta[-1])

    series_meta = {}
    for _, series, _ in city_specs:
        try:
            meta, rec = client.fetch_series_meta(series)
        except KalshiHistoryError as exc:
            series_meta[series] = {"error": str(exc)}
            continue
        series_meta[series] = {
            "fee_type": (meta or {}).get("fee_type"),
            "fee_multiplier": (meta or {}).get("fee_multiplier"),
            "category": (meta or {}).get("category"),
            "settlement_sources": (meta or {}).get("settlement_sources"),
            "url": rec.url,
            "fetched_at_utc": rec.fetched_at_utc,
            "http_status": rec.status,
        }

    manifest = {
        "generated_at_utc": _utcnow_iso(),
        "generator": "scripts/backfill_ladders.py (src.data.kalshi_history.backfill)",
        "prd": "FR-2.4 / Phase 2 exit criterion 5",
        "api_base": client.api_base,
        "auth_mode": "anonymous" if client.provider.anonymous else "authenticated",
        "endpoints": {
            "market_metadata": "GET {api_base}/markets?event_ticker={SERIES}-{%y%b%d}&limit=200",
            "candlesticks": (
                "GET {api_base}/series/{SERIES}/markets/{MARKET}/candlesticks"
                "?start_ts=&end_ts=&period_interval="
            ),
            "series_metadata": "GET {api_base}/series/{SERIES}",
        },
        "request_params": {
            "period_interval_minutes": period_interval,
            "candlestick_window": "market open_time .. close_time (UTC, from /markets)",
            "max_candles_per_request": MAX_CANDLES_PER_REQUEST,
            "min_request_interval_s": MIN_REQUEST_INTERVAL,
        },
        "date_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendar_days": len(date_range(start, end)),
        },
        "cities": [{"city": c, "series": s, "station": st} for c, s, st in city_specs],
        "truth_source": {
            "path": str(Path(truth_dir).relative_to(_PROJECT_ROOT)),
            "files": [f"cli_daily_high_{st}.csv" for _, _, st in city_specs],
            "note": "Phase 1 IEM CLI daily highs; read-only join, never modified.",
        },
        "series_metadata": series_meta,
        "totals": totals,
        "empty_days": empty_days,
        "markets_missing_expiration_value": missing_expiration_value,
        "truth_disagreements": truth_disagreements,
        "bracket_spec_errors": bracket_spec_errors,
        "http_failures": http_failures,
        "http_requests": len(client.requests),
        "days": days_meta,
        "schema": {
            "columns": list(LADDER_COLUMNS),
            "storage": "one CSV per (series, target_date) under data/ladders/<SERIES>/<YYYY-MM-DD>.csv",
            "loader": "src.data.kalshi_history.load_ladders(root, cities, start_date, end_date, quoted_only)",
            "notes": [
                "yes_bid / yes_ask are the CLOSE of each period's quote candle.",
                "yes_bid_low / yes_ask_high are the worst intra-period quotes, for adverse-fill modelling.",
                "no_bid = 1 - yes_ask and no_ask = 1 - yes_bid (exact identity, not a model).",
                "has_quote is False when yes_bid <= 0 or yes_ask >= 1 (Kalshi's empty-book sentinels).",
                "Blank cells mean the API supplied no value; they are never 0.",
                "volume is the volume traded during the period; open_interest is OI at period end.",
            ],
        },
    }
    MANIFEST = Path(root) / "manifest.json"
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=False)
        # Trailing newline keeps the end-of-file-fixer hook from rewriting the
        # manifest after the backfill writes it.
        fh.write("\n")
    return manifest
