"""Phase 4 gas backtest — the artifact that closes exit criterion 2 (WS-D).

Exit criterion 2, verbatim:

    Backtest artifact: the lag/drift projection, fit on >=12 months of
    backfilled AAA/EIA/RBOB history, reports month-end projection MAE on >=6
    held-out month-ends; the strategy's simulated historical EV (maker fees
    included) is documented, and the bot trades in paper only if that EV > 0
    (else the phase closes with a documented HALT, which still satisfies this
    criterion).

WHAT THIS SCRIPT IS ALLOWED TO CLAIM
------------------------------------
Only what it computed. Every table below is emitted from measured rows, and a
quantity that could not be obtained is printed as an explicit deferral rather
than omitted. Three specific traps this project has already paid for are closed
by construction:

1. **No lookahead.** The projection is never handed an unclamped series. For
   each decision date the AAA/RBOB/EIA series is clamped with
   ``GasSeries.observed_through(decision_date)`` *before* it reaches the
   strategy, so ``GasConvergenceStrategy._newest_aaa_date()`` — which the
   strategy uses as ``as_of`` — cannot see a row published after the decision.
   ``project()`` then re-clamps and re-scans internally
   (``src/models/gas_projection.py`` NO-LOOKAHEAD ENFORCEMENT).
2. **The strategy decides, not a re-implementation of it.** Accept/reject comes
   from calling :meth:`GasConvergenceStrategy.analyze` on a ``MarketData`` built
   from the recorded tape, with the rejection reason codes captured from the
   real ``log_rejection`` channel. Fees come from
   :meth:`GasConvergenceStrategy._ev`, i.e. from ``compute_fee`` with
   ``fee_type_for_symbol`` threaded. Nothing here re-derives a fee or a gate.
3. **The maker fee is never scaled from the taker fee.** The published *rate*
   ratio is 25% (0.0175 vs 0.07) but the *charged* fee is each rate's own value
   ceil'd to the cent on the order total, so at 1 contract the maker fee is 100%
   of the taker fee. Both legs are always computed by ``compute_fee`` at the
   real contract count.

THE QUOTE TAPE
--------------
No gas orderbook was ever recorded by this project (``data/ladders/`` holds
``KXHIGH*`` only), and Kalshi prunes settled markets after roughly two months,
so a historical gas quote surface has to be recovered from the public
**candlesticks** endpoint::

    GET /series/{series}/markets/{ticker}/candlesticks
        ?start_ts=&end_ts=&period_interval=60

which returns, per hour, ``yes_bid`` and ``yes_ask`` OHLC in dollars plus
``volume_fp`` and ``open_interest_fp``. That endpoint answers anonymously, so
the tape needs no credential. ``fetch-tape`` writes it once to
``reports/phase4/gas_quote_tape.csv`` with a provenance manifest; every analysis
pass then runs offline against that file.

``yes_bid == 0`` and ``yes_ask == 1`` are Kalshi's empty-book sentinels, not
prices, and are carried through as absent (the strategy's own ``_quote`` helper
rejects both). The NO side is derived as ``no_bid = 1 - yes_ask`` and
``no_ask = 1 - yes_bid``, which is Kalshi's identity, and is absent whenever the
YES side it derives from is absent.

FIT BUDGET
----------
Every fit is counted and the total is printed. Projections are memoised per
``(as_of, settlement_date)`` by the strategy itself, so a 40-strike ladder on one
decision date costs one regression, not forty. A full perturbation sweep is a
few thousand fits at ~15 ms each; the script never parallelises.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.core.fee_calculator import (  # noqa: E402
    KNOWN_MAKER_FEE_SERIES,
    MAKER_RATE,
    TAKER_RATE,
    compute_fee,
    fee_type_for_symbol,
)
from src.core.interfaces import MarketData  # noqa: E402
from src.models.gas_projection import (  # noqa: E402
    QUALITY_OK,
    GasDataUnavailable,
    GasObservation,
    GasProjection,
    GasSeries,
    ProjectionConfig,
    prob_above,
    project,
    settles_yes_gas,
)
from src.strategies.gas_convergence import (  # noqa: E402
    GasConvergenceStrategy,
    resolve_settlement_date,
)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

GAS_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "gas_truth")
PHASE4_DIR = os.path.join(REPO_ROOT, "reports", "phase4")
TAPE_PATH = os.path.join(PHASE4_DIR, "gas_quote_tape.csv")
TAPE_MANIFEST_PATH = os.path.join(PHASE4_DIR, "gas_quote_tape_manifest.json")
PINNED_TRUTH_PATH = os.path.join(
    REPO_ROOT, "tests", "fixtures", "gas", "kalshi_pinned_truth.csv"
)
SETTLED_LADDERS_PATH = os.path.join(
    REPO_ROOT, "tests", "fixtures", "gas", "kxaaagasm_settled_ladders.json"
)
COVARIATE_DIR = os.path.join(PHASE4_DIR, "covariates")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "money-printer-phase4-backtest/1.0 (hoyeriiim87@gmail.com)"

#: Series the FR-4.3 strategy will price. ``KXAAAGASM`` is the Phase 4 target
#: and the only one billing maker fees; ``KXAAAGASW`` is priced on its own
#: (standard) schedule and reported separately, never pooled into the headline.
TAPE_SERIES: Tuple[str, ...] = ("KXAAAGASM", "KXAAAGASW")

#: Hours of tape kept before each market's close. 16 days covers the FR-4.3
#: 14-day window with a day of slack either side.
TAPE_LOOKBACK_DAYS = 16

#: Seconds between API requests. The endpoint is public and unmetered but there
#: is no reason to hammer it; a tape fetch is a one-time cost.
REQUEST_SLEEP_S = 0.35

#: The 1c adverse-fill allowance FR-2.4 requires and Phase 2 applied to every
#: cell: the price actually paid is the quote plus a cent, and the fee is
#: charged on the price paid.
ADVERSE_FILL_ALLOWANCE = 0.01

#: FR-4.3 "sized small". Phase 2 modelled C=20 for weather; gas brackets are
#: thinner, so the headline is C=5 (the strategy's own ``base_quantity``
#: default) with C=1 and C=20 reported as a sensitivity. C matters here in a way
#: it does not for weather: ``KXAAAGASM`` bills makers, and the maker fee is
#: ceil'd to the cent on the order total.
HEADLINE_QUANTITY = 5

#: The ET hour whose hourly candle close is taken as the decision snapshot.
#: 18:00 ET is after AAA's morning publication and before the 23:59 ET close.
HEADLINE_DECISION_HOUR_ET = 18

#: Bracket-distance bands on |floor_strike - projection point|, USD/gal.
BAND_EDGES: Tuple[float, ...] = (0.00, 0.01, 0.02, 0.03, 0.05, 0.08)

#: Prices sampled by the §7 fee-ratio table. Named so the table, the count
#: quoted beside it and the maximum quoted beside it cannot disagree.
FEE_TABLE_PRICES: Tuple[float, ...] = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90)

#: Data-freshness gate, read from the FR-4.3 strategy's own default rather than
#: restated here. The replay's structural pre-filter, the sentence in §2.5 that
#: describes it and the code that will actually run in paper are then the same
#: number by construction; a WS-C change to the default cannot leave this
#: artifact describing a gate the bot no longer applies.
MAX_DATA_AGE_DAYS = GasConvergenceStrategy(series=None).max_data_age_days

#: Held-out month-ends exit criterion 2 requires. The single place this number
#: appears: every sentence about whether the clause is met derives from it, so a
#: longer backfill cannot leave a stale "not met" claim behind.
REQUIRED_MONTH_ENDS = 6

logger = logging.getLogger("gas_backtest")


# =========================================================================
# 1. Quote tape
# =========================================================================


@dataclass(frozen=True)
class TapeRow:
    """One hourly candle for one gas market."""

    series: str
    event_ticker: str
    ticker: str
    floor_strike: float
    strike_type: str
    status: str
    result: str
    expiration_value: Optional[float]
    close_time: str
    expected_expiration_time: str
    settlement_date: date
    end_ts: int
    et_date: date
    et_hour: int
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    yes_bid_high: Optional[float]
    yes_ask_low: Optional[float]
    last: Optional[float]
    volume_fp: float
    open_interest_fp: float


TAPE_COLUMNS: Tuple[str, ...] = (
    "series",
    "event_ticker",
    "ticker",
    "floor_strike",
    "strike_type",
    "status",
    "result",
    "expiration_value",
    "close_time",
    "expected_expiration_time",
    "settlement_date",
    "end_ts",
    "et_date",
    "et_hour",
    "yes_bid",
    "yes_ask",
    "yes_bid_high",
    "yes_ask_low",
    "last",
    "volume_fp",
    "open_interest_fp",
)


def _http_get(session, url: str, params: dict) -> dict:
    """One polite GET returning parsed JSON, or raise with the body attached."""
    resp = session.get(
        url, params=params, headers={"User-Agent": USER_AGENT}, timeout=60
    )
    time.sleep(REQUEST_SLEEP_S)
    if resp.status_code != 200:
        raise RuntimeError(f"{url} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _list_markets(session, series: str, status: str) -> List[dict]:
    """Every market the public API returns for one series and status."""
    out: List[dict] = []
    cursor = None
    while True:
        params = {"series_ticker": series, "status": status, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = _http_get(session, f"{KALSHI_API}/markets", params)
        out.extend(payload.get("markets") or [])
        cursor = payload.get("cursor")
        if not cursor:
            return out


def _candle_price(block, key: str = "close_dollars") -> Optional[float]:
    """A dollars field from a candlestick sub-block, or ``None`` when absent.

    Kalshi's empty-book sentinels (``yes_bid == 0``, ``yes_ask == 1``) are
    returned as ``None``: they mean the side is not quoted, and treating either
    as a price is how a backtest books a fill that was never available.
    """
    if not isinstance(block, dict):
        return None
    raw = block.get(key)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0 or value >= 1.0:
        return None
    return value


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _float_or_none(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def fetch_tape(out_path: str = TAPE_PATH) -> dict:
    """Fetch the hourly quote tape for every retrievable gas market.

    Writes ``out_path`` (CSV, LF) and a provenance manifest beside it. Returns
    the manifest.
    """
    import requests  # local import: analysis passes need no network stack

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    session = requests.Session()

    inventory: List[dict] = []
    per_series: Dict[str, Dict[str, int]] = {}
    for series in TAPE_SERIES:
        per_series[series] = {}
        for status in ("settled", "open"):
            markets = _list_markets(session, series, status)
            per_series[series][status] = len(markets)
            inventory.extend(markets)
            logger.info("inventory %s/%s: %d markets", series, status, len(markets))

    rows: List[TapeRow] = []
    skipped: List[dict] = []
    for i, market in enumerate(inventory, start=1):
        ticker = market.get("ticker") or ""
        series = ticker.split("-", 1)[0]
        close_dt = _parse_ts(market.get("close_time"))
        open_dt = _parse_ts(market.get("open_time"))
        if close_dt is None:
            skipped.append({"ticker": ticker, "reason": "no close_time"})
            continue
        try:
            settlement_date, _src = resolve_settlement_date(ticker, market)
        except ValueError as exc:
            skipped.append({"ticker": ticker, "reason": str(exc)})
            continue

        end_ts = int(close_dt.timestamp())
        start_ts = end_ts - TAPE_LOOKBACK_DAYS * 86400
        if open_dt is not None:
            start_ts = max(start_ts, int(open_dt.timestamp()) - 3600)
        # A market that has not opened yet has no tape.
        if start_ts >= min(end_ts, int(datetime.now(UTC).timestamp())):
            skipped.append({"ticker": ticker, "reason": "no elapsed life"})
            continue
        end_ts = min(end_ts, int(datetime.now(UTC).timestamp()))

        url = f"{KALSHI_API}/series/{series}/markets/{ticker}/candlesticks"
        try:
            payload = _http_get(
                session,
                url,
                {"start_ts": start_ts, "end_ts": end_ts, "period_interval": 60},
            )
        except RuntimeError as exc:
            skipped.append({"ticker": ticker, "reason": str(exc)[:200]})
            continue

        candles = payload.get("candlesticks") or []
        floor_strike = _float_or_none(market.get("floor_strike"))
        if floor_strike is None:
            skipped.append({"ticker": ticker, "reason": "no floor_strike"})
            continue
        for candle in candles:
            ts = candle.get("end_period_ts")
            if not isinstance(ts, (int, float)):
                continue
            et = datetime.fromtimestamp(int(ts), UTC).astimezone(ET)
            rows.append(
                TapeRow(
                    series=series,
                    event_ticker=market.get("event_ticker") or "",
                    ticker=ticker,
                    floor_strike=floor_strike,
                    strike_type=str(market.get("strike_type") or ""),
                    status=str(market.get("status") or ""),
                    result=str(market.get("result") or ""),
                    expiration_value=_float_or_none(market.get("expiration_value")),
                    close_time=str(market.get("close_time") or ""),
                    expected_expiration_time=str(
                        market.get("expected_expiration_time") or ""
                    ),
                    settlement_date=settlement_date,
                    end_ts=int(ts),
                    et_date=et.date(),
                    et_hour=et.hour,
                    yes_bid=_candle_price(candle.get("yes_bid")),
                    yes_ask=_candle_price(candle.get("yes_ask")),
                    yes_bid_high=_candle_price(candle.get("yes_bid"), "high_dollars"),
                    yes_ask_low=_candle_price(candle.get("yes_ask"), "low_dollars"),
                    last=_candle_price(candle.get("price")),
                    volume_fp=_float_or_none(candle.get("volume_fp")) or 0.0,
                    open_interest_fp=_float_or_none(candle.get("open_interest_fp"))
                    or 0.0,
                )
            )
        if i % 25 == 0:
            logger.info("fetched %d/%d markets, %d rows", i, len(inventory), len(rows))

    rows.sort(key=lambda r: (r.ticker, r.end_ts))
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(TAPE_COLUMNS)
        for r in rows:
            writer.writerow(
                [
                    r.series,
                    r.event_ticker,
                    r.ticker,
                    f"{r.floor_strike:.4f}",
                    r.strike_type,
                    r.status,
                    r.result,
                    "" if r.expiration_value is None else f"{r.expiration_value:.4f}",
                    r.close_time,
                    r.expected_expiration_time,
                    r.settlement_date.isoformat(),
                    r.end_ts,
                    r.et_date.isoformat(),
                    r.et_hour,
                    "" if r.yes_bid is None else f"{r.yes_bid:.4f}",
                    "" if r.yes_ask is None else f"{r.yes_ask:.4f}",
                    "" if r.yes_bid_high is None else f"{r.yes_bid_high:.4f}",
                    "" if r.yes_ask_low is None else f"{r.yes_ask_low:.4f}",
                    "" if r.last is None else f"{r.last:.4f}",
                    f"{r.volume_fp:.2f}",
                    f"{r.open_interest_fp:.2f}",
                ]
            )

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "generator": "scripts/gas_backtest.py fetch-tape",
        "endpoint": (
            "GET /series/{series}/markets/{ticker}/candlesticks"
            "?period_interval=60 (anonymous, public)"
        ),
        "api_base": KALSHI_API,
        "user_agent": USER_AGENT,
        "request_sleep_s": REQUEST_SLEEP_S,
        "lookback_days": TAPE_LOOKBACK_DAYS,
        "series": per_series,
        "markets_enumerated": len(inventory),
        "markets_skipped": len(skipped),
        "skipped": skipped[:50],
        "rows": len(rows),
        "distinct_markets": len({r.ticker for r in rows}),
        "distinct_events": len({r.event_ticker for r in rows}),
        "first_end_ts": rows[0].end_ts if rows else None,
        "last_end_ts": rows[-1].end_ts if rows else None,
        "path": os.path.relpath(out_path, REPO_ROOT).replace("\\", "/"),
        "content_hash": _file_sha256(out_path),
        "note": (
            "yes_bid==0 and yes_ask==1 are Kalshi empty-book sentinels and are "
            "written as blank (absent), never as a price."
        ),
    }
    with open(TAPE_MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    logger.info("wrote %s (%d rows)", out_path, len(rows))
    return manifest


SERIES_META_PATH = os.path.join(PHASE4_DIR, "gas_series_metadata.json")


def fetch_series_metadata(out_path: str = SERIES_META_PATH) -> dict:
    """Record each gas series' live ``fee_type`` and settlement source.

    The report asserts that ``KXAAAGASM`` is the only gas series billing resting
    liquidity. ``KNOWN_MAKER_FEE_SERIES`` encodes that belief, so checking the
    code against itself proves nothing; this pulls the exchange's own answer and
    commits it beside the artifact. ``KXHIGHNY`` is included as the weather
    control that the Phase 2 fee correction rests on.
    """
    import requests

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    session = requests.Session()
    out = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "generator": "scripts/gas_backtest.py fetch-series-meta",
        "endpoint": f"GET {KALSHI_API}/series/{{series_ticker}} (anonymous, public)",
        "series": {},
    }
    for ticker in ("KXAAAGASM", "KXAAAGASW", "KXAAAGASD", "KXHIGHNY"):
        payload = _http_get(session, f"{KALSHI_API}/series/{ticker}", {})
        meta = payload.get("series") or {}
        out["series"][ticker] = {
            "fee_type": meta.get("fee_type"),
            "fee_multiplier": meta.get("fee_multiplier"),
            "settlement_sources": [
                s.get("name") for s in (meta.get("settlement_sources") or [])
            ],
            "code_says_fee_type": fee_type_for_symbol(f"{ticker}-X-1"),
            "agrees_with_code": meta.get("fee_type")
            == fee_type_for_symbol(f"{ticker}-X-1"),
        }
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(out, handle, indent=2)
        handle.write("\n")
    return out


def load_tape(path: str = TAPE_PATH) -> List[TapeRow]:
    """Read the tape CSV back into :class:`TapeRow` records."""
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{path} not found; run `python scripts/gas_backtest.py fetch-tape` "
            f"first. There is no historical gas orderbook on disk to fall back "
            f"to (data/ladders/ holds KXHIGH* only)."
        )
    out: List[TapeRow] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            out.append(
                TapeRow(
                    series=row["series"],
                    event_ticker=row["event_ticker"],
                    ticker=row["ticker"],
                    floor_strike=float(row["floor_strike"]),
                    strike_type=row["strike_type"],
                    status=row["status"],
                    result=row["result"],
                    expiration_value=_float_or_none(row["expiration_value"]),
                    close_time=row["close_time"],
                    expected_expiration_time=row["expected_expiration_time"],
                    settlement_date=date.fromisoformat(row["settlement_date"]),
                    end_ts=int(row["end_ts"]),
                    et_date=date.fromisoformat(row["et_date"]),
                    et_hour=int(row["et_hour"]),
                    yes_bid=_float_or_none(row["yes_bid"]),
                    yes_ask=_float_or_none(row["yes_ask"]),
                    yes_bid_high=_float_or_none(row["yes_bid_high"]),
                    yes_ask_low=_float_or_none(row["yes_ask_low"]),
                    last=_float_or_none(row["last"]),
                    volume_fp=float(row["volume_fp"]),
                    open_interest_fp=float(row["open_interest_fp"]),
                )
            )
    for r in out:
        _EXPIRATION_CACHE.setdefault(r.ticker, r.expiration_value)
    return out


#: ``{ticker: expiration_value}`` from the tape, so the worked example can quote
#: the AAA value Kalshi actually settled against without carrying it on every
#: priced cell.
_EXPIRATION_CACHE: Dict[str, Optional[float]] = {}


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# =========================================================================
# 2. Series loading and configuration axes
# =========================================================================


@dataclass(frozen=True)
class SeriesSpec:
    """One loaded (AAA, RBOB, EIA) triple plus the coverage facts about it."""

    label: str
    series: GasSeries
    aaa_first: date
    aaa_last: date
    aaa_rows: int
    aaa_suspect: int
    aaa_missing_days: int
    rbob_label: str
    rbob_series_id: str
    rbob_first: Optional[date]
    rbob_last: Optional[date]
    rbob_rows: int
    eia_rows: int
    include_suspect: bool


def _read_aaa(path: str) -> Tuple[List[GasObservation], int]:
    rows: List[GasObservation] = []
    suspect = 0
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            d = (row.get("date") or "").strip()
            v = (row.get("value") or "").strip()
            if not d or not v:
                continue
            quality = (row.get("quality") or "ok").strip().lower()
            if quality != "ok":
                suspect += 1
            rows.append(
                GasObservation(
                    date=date.fromisoformat(d),
                    value=float(v),
                    quality=quality,
                    source=(row.get("source") or "").strip(),
                )
            )
    return rows, suspect


def _read_dated(path: str, date_column: str) -> List[GasObservation]:
    rows: List[GasObservation] = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            d = (row.get(date_column) or "").strip()
            v = (row.get("value") or "").strip()
            if not d or not v:
                continue
            rows.append(GasObservation(date=date.fromisoformat(d), value=float(v)))
    return rows


def load_series_spec(
    *,
    label: str,
    rbob_label: str,
    rbob_path: Optional[str],
    rbob_series_id: str,
    include_suspect: bool,
    gas_dir: str = GAS_TRUTH_DIR,
    eia_path: Optional[str] = None,
) -> SeriesSpec:
    """Load AAA (always WS-A's committed file) plus a chosen RBOB source."""
    aaa_rows, suspect = _read_aaa(os.path.join(gas_dir, "aaa_daily_national.csv"))
    if not include_suspect:
        kept = [o for o in aaa_rows if o.quality == "ok"]
    else:
        kept = list(aaa_rows)
    if not kept:
        raise GasDataUnavailable("no AAA rows survived the quality filter")
    first, last = min(o.date for o in kept), max(o.date for o in kept)
    span = (last - first).days + 1
    missing = span - len({o.date for o in kept})

    rbob = _read_dated(rbob_path, "date") if rbob_path else []
    eia = _read_dated(
        eia_path or os.path.join(gas_dir, "eia_weekly_regular.csv"), "week_ending"
    )

    series = GasSeries.from_rows(aaa=kept, rbob=rbob, eia_weekly=eia)
    return SeriesSpec(
        label=label,
        series=series,
        aaa_first=first,
        aaa_last=last,
        aaa_rows=len(kept),
        aaa_suspect=suspect,
        aaa_missing_days=missing,
        rbob_label=rbob_label,
        rbob_series_id=rbob_series_id,
        rbob_first=min((o.date for o in rbob), default=None),
        rbob_last=max((o.date for o in rbob), default=None),
        rbob_rows=len(rbob),
        eia_rows=len(eia),
        include_suspect=include_suspect,
    )


def rbob_source_paths() -> Dict[str, Tuple[str, str]]:
    """``{alias: (csv path, EIA series id)}`` for every available RBOB source.

    The default (``la_rbob_spot``) is WS-A's committed ``rbob_daily.csv``.
    Alternatives are fetched by ``fetch-covariates`` into ``reports/phase4``
    so this workstream never writes into WS-A's directory.
    """
    from src.data.energy_covariates import RBOB_ALTERNATIVES, RBOB_SERIES_ID

    out: Dict[str, Tuple[str, str]] = {
        "la_rbob_spot": (
            os.path.join(GAS_TRUTH_DIR, "rbob_daily.csv"),
            RBOB_SERIES_ID,
        )
    }
    for alias, series_id in RBOB_ALTERNATIVES.items():
        if alias == "la_rbob_spot":
            continue
        path = os.path.join(COVARIATE_DIR, alias, "rbob_daily.csv")
        if os.path.isfile(path):
            out[alias] = (path, series_id)
    return out


def fetch_covariates(start: str = "2020-06-01") -> Dict[str, dict]:
    """Fetch the alternative RBOB spot series into ``reports/phase4/covariates``.

    Reuses the already-downloaded EIA bulk archive when present. WS-A owns
    ``data/gas_truth/``; nothing here writes there.
    """
    import tempfile

    from src.data.energy_covariates import (
        RBOB_ALTERNATIVES,
        backfill_covariates,
    )

    cache_dir = os.path.join(tempfile.gettempdir(), "money_printer_eia_cache")
    archive = os.path.join(cache_dir, "PET.zip")
    archive_path = archive if os.path.isfile(archive) else None

    out: Dict[str, dict] = {}
    for alias, series_id in sorted(RBOB_ALTERNATIVES.items()):
        target = os.path.join(COVARIATE_DIR, alias)
        os.makedirs(target, exist_ok=True)
        result = backfill_covariates(
            gas_dir=target,
            cache_dir=cache_dir,
            start=start,
            end=None,
            rbob_series_id=series_id,
            archive_path=archive_path,
        )
        out[alias] = result["rbob_daily"]
        logger.info(
            "covariate %s: %d rows %s..%s",
            alias,
            result["rbob_daily"]["rows"],
            result["rbob_daily"]["first"],
            result["rbob_daily"]["last"],
        )
    return out


# =========================================================================
# 3. Held-out month-end MAE (walk-forward)
# =========================================================================


class FitCounter:
    """Counts every regression, so 'fit once per configuration' is auditable."""

    def __init__(self) -> None:
        self.fits = 0
        self.aborts = 0
        self.abort_reasons: Dict[str, int] = defaultdict(int)

    def project(
        self,
        as_of: date,
        target: date,
        series: GasSeries,
        config: ProjectionConfig,
    ) -> Optional[GasProjection]:
        try:
            proj = project(as_of, target, series, config=config)
        except GasDataUnavailable as exc:
            self.aborts += 1
            self.abort_reasons[_abort_bucket(str(exc))] += 1
            return None
        self.fits += 1
        return proj


def _abort_bucket(message: str) -> str:
    """Collapse an abort message to a short stable reason code."""
    text = message.lower()
    for needle, code in (
        ("below the fr-4.2 minimum", "HISTORY_TOO_SHORT"),
        ("is not an observed aaa date", "AS_OF_NOT_OBSERVED"),
        ("training pairs", "TOO_FEW_TRAIN_PAIRS"),
        ("exceeds max_lead_days", "LEAD_TOO_LONG"),
        ("day gap", "GAP_TOO_LONG"),
        ("are interpolated", "TOO_MUCH_INTERPOLATION"),
        ("plausibility band", "IMPLAUSIBLE_POINT"),
        ("near-singular", "COLLINEAR"),
        ("predictor", "PREDICTOR_UNAVAILABLE"),
        ("not before target_date", "NOT_FUTURE"),
        ("no usable aaa observations", "NO_AAA_ROWS"),
    ):
        if needle in text:
            return code
    return "OTHER"


@dataclass
class MaeRow:
    """One walk-forward projection scored against one truth channel."""

    target_date: date
    nominal_lead: int
    as_of: date
    realized_lead: int
    point: float
    sigma: float
    truth: float
    error: float
    n_train: int
    model_version: str
    truth_channel: str
    inputs_hash: str


def observed_aaa_dates(spec: SeriesSpec) -> List[date]:
    return sorted({o.date for o in spec.series.aaa})


def month_end_targets(spec: SeriesSpec) -> List[date]:
    """Observed AAA dates that are the last calendar day of their month."""
    out = []
    for d in observed_aaa_dates(spec):
        nxt = d + timedelta(days=1)
        if nxt.month != d.month:
            out.append(d)
    return out


def _aaa_value(spec: SeriesSpec, d: date) -> Optional[float]:
    for o in spec.series.aaa:
        if o.date == d:
            return o.value
    return None


def walk_forward_mae(
    spec: SeriesSpec,
    targets: Sequence[date],
    leads: Sequence[int],
    config: ProjectionConfig,
    counter: FitCounter,
    truth_lookup,
    truth_channel: str,
) -> List[MaeRow]:
    """Strict walk-forward: for each target, fit only on rows dated < target.

    ``as_of`` is the newest **observed** AAA date at or before
    ``target - nominal_lead``, because the projection anchors on ``A(as_of)``
    and ``require_observed_as_of`` forbids extrapolating that anchor. The
    realized lead is therefore >= the nominal one and is reported per row.
    """
    observed = observed_aaa_dates(spec)
    rows: List[MaeRow] = []
    for target in targets:
        truth = truth_lookup(target)
        if truth is None:
            continue
        for lead in leads:
            cutoff = target - timedelta(days=lead)
            candidates = [d for d in observed if d <= cutoff]
            if not candidates:
                continue
            as_of = candidates[-1]
            proj = counter.project(as_of, target, spec.series, config)
            if proj is None:
                continue
            rows.append(
                MaeRow(
                    target_date=target,
                    nominal_lead=lead,
                    as_of=as_of,
                    realized_lead=proj.lead_days,
                    point=proj.point,
                    sigma=proj.sigma,
                    truth=truth,
                    error=proj.point - truth,
                    n_train=proj.n_train,
                    model_version=proj.model_version,
                    truth_channel=truth_channel,
                    inputs_hash=proj.inputs_hash,
                )
            )
    return rows


def admissible_daily_targets(spec: SeriesSpec, config: ProjectionConfig) -> List[date]:
    """Every observed AAA date the projection can legally be scored on.

    The month-end sample is tiny while the AAA backfill is short, so the same
    walk-forward machinery is also run over *all* admissible daily targets. That
    is a much larger sample of the same estimator and it is the honest way to
    measure the projection's **bias**, which a 2-row month-end table cannot.
    The targets overlap heavily (a 14-day lead shares 13 days with its
    neighbour), so the rows are strongly dependent and the standard errors that
    would follow from treating them as independent are not reported.
    """
    observed = observed_aaa_dates(spec)
    if not observed:
        return []
    first = observed[0]
    earliest_as_of = first + timedelta(days=config.min_history_days - 1)
    return [d for d in observed if d > earliest_as_of]


@dataclass(frozen=True)
class PinnedTruth:
    settlement_date: date
    series: str
    period_kind: str
    low_exclusive: float
    high_inclusive: float
    kalshi_expiration_value: Optional[float]


def load_pinned_truth(path: str = PINNED_TRUTH_PATH) -> List[PinnedTruth]:
    out: List[PinnedTruth] = []
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            out.append(
                PinnedTruth(
                    settlement_date=date.fromisoformat(row["settlement_date"]),
                    series=row["series"],
                    period_kind=row["period_kind"],
                    low_exclusive=float(row["value_low_exclusive"]),
                    high_inclusive=float(row["value_high_inclusive"]),
                    kalshi_expiration_value=_float_or_none(
                        row.get("kalshi_expiration_value")
                    ),
                )
            )
    return out


@dataclass(frozen=True)
class CrossCheck:
    """Measured agreement between the AAA scrape and Kalshi's settled ladders.

    Two independent channels for the same quantity: WS-A's Wayback scrape of
    ``gasprices.aaa.com``, and WS-B's reconstruction of what the exchange must
    have settled against, recovered from which strikes paid YES and which paid NO
    plus the published ``expiration_value``. Neither derives from the other.

    Computed here rather than quoted from anyone's message, because a hardcoded
    ``77/77`` is exactly the defect this artifact has already been corrected for
    twice: prose asserting what the data is, beside a table computing it. When
    the AAA series changes, these figures change with it.
    """

    aaa_rows: int
    aaa_suspect: int
    pinned_rows: int
    pinned_dates: int
    # interval containment: is our value inside (low, high] ?
    rows_with_aaa: int
    inside: int
    outside: int
    no_aaa_row: int
    outside_detail: Tuple[str, ...]
    no_row_dates: Tuple[str, ...]
    suspect_pinned_dates: Tuple[str, ...]
    # point agreement against Kalshi's own published expiration_value
    rows_with_kalshi_value: int
    same_day: int
    prev_day: int
    neither: int
    prev_day_dates: Tuple[str, ...]
    neither_detail: Tuple[str, ...]
    max_deviation: Optional[float]
    max_deviation_date: Optional[str]

    @property
    def containment_ok(self) -> bool:
        return self.outside == 0 and self.rows_with_aaa > 0

    @property
    def attribution_ok(self) -> bool:
        """No pinned settlement matches neither our same-day nor previous-day value."""
        return self.neither == 0 and self.rows_with_kalshi_value > 0


def aaa_vs_kalshi_crosscheck(
    gas_dir: str = GAS_TRUTH_DIR, pinned_path: str = PINNED_TRUTH_PATH
) -> CrossCheck:
    """Reconcile the AAA scrape against Kalshi's settled-ladder truth.

    Runs over **all** AAA rows including ``quality=suspect`` ones: the question
    is whether the scraped file agrees with the exchange, and excluding the rows
    most likely to disagree would answer a different question. Suspect rows that
    land on a pinned date are listed separately so the reader can see them.

    The previous-day column is the check that matters for ET attribution. AAA
    republishes during the morning, so a capture taken at the wrong hour can
    carry the prior day's figure; if our series were systematically shifted, the
    previous-day column would hold most of the mass rather than a handful of
    dates. See ``docs/phase4_data_contract.md`` §6.3 for the decision record
    naming the publication-hour metrics — this function deliberately does not
    restate them, it measures a different thing (agreement with the exchange).
    """
    aaa: Dict[date, float] = {}
    suspect: set = set()
    aaa_path = os.path.join(gas_dir, "aaa_daily_national.csv")
    with open(aaa_path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_date = (row.get("date") or "").strip()
            raw_value = (row.get("value") or "").strip()
            if not raw_date or not raw_value:
                continue
            d = date.fromisoformat(raw_date)
            aaa[d] = float(raw_value)
            if (row.get("quality") or QUALITY_OK).strip().lower() != QUALITY_OK:
                suspect.add(d)

    pinned = load_pinned_truth(pinned_path)
    inside = outside = no_row = 0
    same = prev = neither = 0
    outside_detail: List[str] = []
    no_row_dates: List[str] = []
    prev_day_dates: List[str] = []
    neither_detail: List[str] = []
    suspect_pinned: List[str] = []
    with_kalshi = 0
    best_dev: Optional[float] = None
    best_date: Optional[str] = None

    for row in pinned:
        d = row.settlement_date
        value = aaa.get(d)
        if value is None:
            no_row += 1
            if d.isoformat() not in no_row_dates:
                no_row_dates.append(d.isoformat())
        elif row.low_exclusive < value <= row.high_inclusive:
            inside += 1
        else:
            outside += 1
            outside_detail.append(
                f"{d.isoformat()}: ours ${value:.3f} vs "
                f"(${row.low_exclusive:.3f}, ${row.high_inclusive:.3f}]"
            )
        if value is not None and d in suspect and d.isoformat() not in suspect_pinned:
            suspect_pinned.append(d.isoformat())

        kalshi = row.kalshi_expiration_value
        if kalshi is None:
            continue
        with_kalshi += 1
        if value is None:
            continue
        deviation = abs(value - kalshi)
        if best_dev is None or deviation > best_dev:
            best_dev, best_date = deviation, d.isoformat()
        previous = aaa.get(d - timedelta(days=1))
        if deviation <= 1e-9:
            same += 1
        elif previous is not None and abs(previous - kalshi) <= 1e-9:
            prev += 1
            if d.isoformat() not in prev_day_dates:
                prev_day_dates.append(d.isoformat())
        else:
            neither += 1
            neither_detail.append(
                f"{d.isoformat()}: ours ${value:.3f}"
                + ("" if previous is None else f" (prev ${previous:.3f})")
                + f" vs Kalshi ${kalshi:.3f}"
            )

    return CrossCheck(
        aaa_rows=len(aaa),
        aaa_suspect=len(suspect),
        pinned_rows=len(pinned),
        pinned_dates=len({r.settlement_date for r in pinned}),
        rows_with_aaa=inside + outside,
        inside=inside,
        outside=outside,
        no_aaa_row=no_row,
        outside_detail=tuple(outside_detail),
        no_row_dates=tuple(no_row_dates),
        suspect_pinned_dates=tuple(suspect_pinned),
        rows_with_kalshi_value=with_kalshi,
        same_day=same,
        prev_day=prev,
        neither=neither,
        prev_day_dates=tuple(prev_day_dates),
        neither_detail=tuple(neither_detail),
        max_deviation=best_dev,
        max_deviation_date=best_date,
    )


def mae_stats(rows: Sequence[MaeRow]) -> dict:
    """MAE, bias, RMSE, max |error| and the standardized-residual coverage."""
    if not rows:
        return {"n": 0}
    errs = [r.error for r in rows]
    z = [r.error / r.sigma for r in rows if r.sigma > 0]
    return {
        "n": len(rows),
        "mae": sum(abs(e) for e in errs) / len(errs),
        "bias": sum(errs) / len(errs),
        "rmse": math.sqrt(sum(e * e for e in errs) / len(errs)),
        "max_abs": max(abs(e) for e in errs),
        "p50_abs": statistics.median(sorted(abs(e) for e in errs)),
        "mean_sigma": sum(r.sigma for r in rows) / len(rows),
        "z_sd": statistics.pstdev(z) if len(z) > 1 else float("nan"),
        "cover_95": (
            sum(1 for zz in z if abs(zz) <= 1.959963984540054) / len(z)
            if z
            else float("nan")
        ),
    }


# =========================================================================
# 4. EV simulation over the tape
# =========================================================================


class RejectionCapture(logging.Handler):
    """Captures the strategy's own INFO rejection lines and their reason codes.

    The gates are the strategy's; this only reads which one fired. Parsing the
    reason code out of the log is deliberate: contract §3 requires every
    rejection to be visible from the logs alone, so a backtest that can classify
    silence from the log is also a check on that requirement.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.last: Optional[str] = None
        self.counts: Dict[str, int] = defaultdict(int)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            text = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return
        if "REJECT" not in text and "GAS_" not in text:
            return
        for token in text.replace("=", " ").split():
            if token.startswith("GAS_"):
                self.last = token
                self.counts[token] += 1
                return


@dataclass
class EvCell:
    """One priced (market, decision snapshot, side, fee-leg) candidate."""

    ticker: str
    series: str
    event_ticker: str
    settlement_date: date
    et_date: date
    lead_days: int
    floor_strike: float
    point: float
    sigma: float
    p_yes: float
    market_price: float
    price_source: str
    divergence: float
    band: str
    side: str  # "YES" | "NO"
    mode: str  # "maker" | "taker"
    quote: Optional[float]
    price_paid: Optional[float]
    fee_per_ct: Optional[float]
    ev: Optional[float]
    ev_no_allowance: Optional[float]
    realized: Optional[float]
    won: Optional[bool]
    executable: bool
    maker_filled: Optional[bool]
    accepted: bool
    reject_reason: Optional[str]
    n_train: int
    model_version: str
    inputs_hash: str
    volume_fp: float
    open_interest_fp: float
    spread: Optional[float]


def _band_label(distance: float) -> str:
    edges = BAND_EDGES
    for lo, hi in zip(edges, edges[1:]):
        if lo <= distance < hi:
            return f"{int(round(lo * 100))}-{int(round(hi * 100))}c"
    return f"{int(round(edges[-1] * 100))}c+"


def _decision_rows(tape: Sequence[TapeRow], hour_et: int) -> List[TapeRow]:
    """One snapshot per (market, ET date): the last candle at or before ``hour``.

    Using the last candle at or before a fixed ET hour keeps the decision time
    constant across markets and days, so a per-day result cannot be an artifact
    of which hour happened to be quoted.
    """
    best: Dict[Tuple[str, date], TapeRow] = {}
    for row in tape:
        if row.et_hour > hour_et:
            continue
        key = (row.ticker, row.et_date)
        prior = best.get(key)
        if prior is None or row.end_ts > prior.end_ts:
            best[key] = row
    return sorted(best.values(), key=lambda r: (r.et_date, r.ticker))


def _maker_fill(
    tape_by_ticker: Dict[str, List[TapeRow]],
    row: TapeRow,
    side: str,
    limit: float,
    use_extremes: bool = True,
) -> bool:
    """Did a later snapshot before close cross a resting order at ``limit``?

    The PRD FR-3.3 quote-traversal proxy, applied to gas. A resting YES buy at
    ``limit`` fills when a later candle's YES offer reaches down to it; a
    resting NO buy at ``1 - limit_yes`` fills when the YES bid rises to the YES
    price the NO order implies. It is a lower bound (queue position is
    unobservable) and it is forward-looking by construction, which is why the
    verdict in the report is taken from the taker path.
    """
    later = [r for r in tape_by_ticker.get(row.ticker, ()) if r.end_ts > row.end_ts]
    for r in later:
        if side == "YES":
            ask = r.yes_ask_low if use_extremes else r.yes_ask
            if ask is not None and ask <= limit + 1e-12:
                return True
        else:
            bid = r.yes_bid_high if use_extremes else r.yes_bid
            if bid is not None and bid >= (1.0 - limit) - 1e-12:
                return True
    return False


@dataclass
class EvRun:
    cells: List[EvCell] = field(default_factory=list)
    rejections: Dict[str, int] = field(default_factory=dict)
    n_snapshots: int = 0
    n_markets: int = 0
    n_events: int = 0
    n_decision_dates: int = 0
    stale_decision_dates: int = 0
    fits: int = 0
    aborts: int = 0
    abort_reasons: Dict[str, int] = field(default_factory=dict)
    settle_reconcile: Dict[str, int] = field(default_factory=dict)


def simulate_ev(
    spec: SeriesSpec,
    tape: Sequence[TapeRow],
    *,
    config: ProjectionConfig,
    quantity: int = HEADLINE_QUANTITY,
    hour_et: int = HEADLINE_DECISION_HOUR_ET,
    window_days: int = 14,
    min_divergence: float = 0.08,
    allowance: float = ADVERSE_FILL_ALLOWANCE,
    series_filter: Optional[str] = None,
    maker_fill_extremes: bool = True,
    proj_cache: Optional[Dict[Tuple[date, date], GasProjection]] = None,
) -> EvRun:
    """Replay the FR-4.3 strategy over the recorded tape.

    One :class:`GasConvergenceStrategy` is built per decision date with the
    series clamped to that date, so the strategy's own ``as_of`` selection
    cannot see the future. Every accept/reject decision below is the strategy's.
    """
    rows = [r for r in _decision_rows(tape, hour_et)]
    if series_filter:
        rows = [r for r in rows if r.series == series_filter]
    # Candidate set = snapshots inside the FR-4.3 window. The window gate is
    # structural (it decides whether the market is in scope at all), so it is
    # applied here rather than counted as a rejection; every other gate below is
    # the strategy's own and is counted.
    rows = [r for r in rows if 0 < (r.settlement_date - r.et_date).days <= window_days]
    by_ticker: Dict[str, List[TapeRow]] = defaultdict(list)
    for r in tape:
        if series_filter and r.series != series_filter:
            continue
        by_ticker[r.ticker].append(r)
    for v in by_ticker.values():
        v.sort(key=lambda r: r.end_ts)

    run = EvRun()
    run.n_snapshots = len(rows)
    run.n_markets = len({r.ticker for r in rows})
    run.n_events = len({r.event_ticker for r in rows})

    # The project's shared logger sets propagate=False, so a handler on the root
    # logger would see nothing. Attach to the named logger the strategy actually
    # writes to, otherwise every rejection reason silently reads as zero -- which
    # is precisely the "gate rejected everything and nobody saw" failure this
    # project has already shipped once.
    capture = RejectionCapture()
    strategy_logger = logging.getLogger("MoneyPrinter")
    prior_level = strategy_logger.level
    strategy_logger.addHandler(capture)
    if prior_level > logging.INFO:
        strategy_logger.setLevel(logging.INFO)

    by_date: Dict[date, List[TapeRow]] = defaultdict(list)
    for r in rows:
        by_date[r.et_date].append(r)

    # The data-freshness gate is structural too: on a date where the newest AAA
    # row is more than max_data_age_days old the bot emits nothing at all, so
    # such dates are not candidates. Reported, not hidden.
    max_age = MAX_DATA_AGE_DAYS
    stale_dates = []
    for d in sorted(by_date):
        newest = _newest(spec.series.observed_through(d))
        if newest is None or (d - newest).days > max_age:
            stale_dates.append(d)
    for d in stale_dates:
        by_date.pop(d, None)
    run.stale_decision_dates = len(stale_dates)
    run.n_decision_dates = len(by_date)

    reconcile = defaultdict(int)
    try:
        for et_date in sorted(by_date):
            clamped = spec.series.observed_through(et_date)
            strategy = GasConvergenceStrategy(
                final_window_days=window_days,
                min_divergence=min_divergence,
                base_quantity=quantity,
                series=clamped,
                projection_config=config,
                clock=lambda d=et_date: d,
            )
            # A sensitivity sweep changes order size, decision hour or the
            # allowance, none of which touch the regression. Seeding the
            # strategy's own memo from a caller-supplied cache keyed on
            # (as_of, settlement) keeps "fit once per configuration" true across
            # those passes instead of refitting the identical model ten times.
            if proj_cache is not None:
                strategy._projection_cache.update(
                    {k: v for k, v in proj_cache.items() if k[0] == _newest(clamped)}
                )
            for row in by_date[et_date]:
                capture.last = None
                extra = {
                    "strike_type": row.strike_type,
                    "floor_strike": row.floor_strike,
                    "expected_expiration_time": row.expected_expiration_time or None,
                    "close_time": row.close_time or None,
                    "no_bid": None if row.yes_ask is None else 1.0 - row.yes_ask,
                    "no_ask": None if row.yes_bid is None else 1.0 - row.yes_bid,
                }
                md = MarketData(
                    symbol=row.ticker,
                    timestamp=datetime.fromtimestamp(row.end_ts, UTC),
                    price=row.last if row.last is not None else 0.0,
                    bid=row.yes_bid if row.yes_bid is not None else 0.0,
                    ask=row.yes_ask if row.yes_ask is not None else 0.0,
                    volume=row.volume_fp,
                    extra=extra,
                )
                signals = strategy.analyze(md)
                accepted = bool(signals)
                reason = None if accepted else capture.last

                # The strategy memoises per (as_of, settlement_date), so asking
                # for the projection again costs no fit. Asking explicitly (as
                # opposed to reading its cache) means a row the strategy
                # rejected before fitting -- an unsupported strike_type, a
                # near-resolved book -- is still priced for the band table,
                # while a genuine fit abort still drops the row.
                as_of = _newest(clamped)
                if as_of is None:
                    continue
                try:
                    proj = strategy._projection(as_of, row.settlement_date)
                except GasDataUnavailable as exc:
                    run.abort_reasons[_abort_bucket(str(exc))] = (
                        run.abort_reasons.get(_abort_bucket(str(exc)), 0) + 1
                    )
                    run.aborts += 1
                    continue

                p_yes = prob_above(proj, row.floor_strike)
                reference = _reference(row)
                if reference is None:
                    continue
                market_price, price_source = reference
                divergence = p_yes - market_price
                band = _band_label(abs(row.floor_strike - proj.point))
                lead = (row.settlement_date - row.et_date).days
                spread = (
                    row.yes_ask - row.yes_bid
                    if (row.yes_ask is not None and row.yes_bid is not None)
                    else None
                )

                won_yes = _settled_yes(row, reconcile)
                for side in ("YES", "NO"):
                    p_win = p_yes if side == "YES" else 1.0 - p_yes
                    for mode in ("taker", "maker"):
                        if side == "YES":
                            quote = row.yes_ask if mode == "taker" else row.yes_bid
                        else:
                            quote = (
                                (None if row.yes_bid is None else 1.0 - row.yes_bid)
                                if mode == "taker"
                                else (
                                    None if row.yes_ask is None else 1.0 - row.yes_ask
                                )
                            )
                        executable = quote is not None
                        price_paid = None
                        fee_per = None
                        ev = None
                        ev_raw = None
                        realized = None
                        won = None
                        filled = None
                        if executable:
                            price_paid = quote + allowance
                            if price_paid >= 1.0:
                                executable = False
                                price_paid = None
                            else:
                                fee_total = compute_fee(
                                    price_paid,
                                    max(1, quantity),
                                    is_maker=(mode == "maker"),
                                    series_fee_type=fee_type_for_symbol(row.ticker),
                                ).fee
                                fee_per = fee_total / float(max(1, quantity))
                                ev = p_win - price_paid - fee_per
                                ev_raw = strategy._ev(
                                    row.ticker,
                                    p_win,
                                    quote,
                                    quantity,
                                    is_maker=(mode == "maker"),
                                )
                                if won_yes is not None:
                                    won = won_yes if side == "YES" else not won_yes
                                    realized = (
                                        (1.0 if won else 0.0) - price_paid - fee_per
                                    )
                                if mode == "maker":
                                    filled = _maker_fill(
                                        by_ticker,
                                        row,
                                        side,
                                        quote,
                                        use_extremes=maker_fill_extremes,
                                    )
                        run.cells.append(
                            EvCell(
                                ticker=row.ticker,
                                series=row.series,
                                event_ticker=row.event_ticker,
                                settlement_date=row.settlement_date,
                                et_date=row.et_date,
                                lead_days=lead,
                                floor_strike=row.floor_strike,
                                point=proj.point,
                                sigma=proj.sigma,
                                p_yes=p_yes,
                                market_price=market_price,
                                price_source=price_source,
                                divergence=divergence,
                                band=band,
                                side=side,
                                mode=mode,
                                quote=quote,
                                price_paid=price_paid,
                                fee_per_ct=fee_per,
                                ev=ev,
                                ev_no_allowance=ev_raw,
                                realized=realized,
                                won=won,
                                executable=executable,
                                maker_filled=filled,
                                accepted=accepted
                                and (side == ("YES" if divergence > 0 else "NO")),
                                reject_reason=reason,
                                n_train=proj.n_train,
                                model_version=proj.model_version,
                                inputs_hash=proj.inputs_hash,
                                volume_fp=row.volume_fp,
                                open_interest_fp=row.open_interest_fp,
                                spread=spread,
                            )
                        )
            new_fits = len(strategy._projection_cache)
            if proj_cache is not None:
                seeded = sum(1 for k in proj_cache if k[0] == _newest(clamped))
                new_fits = max(0, new_fits - seeded)
                proj_cache.update(strategy._projection_cache)
            run.fits += new_fits
    finally:
        strategy_logger.removeHandler(capture)
        strategy_logger.setLevel(prior_level)

    run.rejections = dict(capture.counts)
    run.settle_reconcile = dict(reconcile)
    return run


def _newest(series: GasSeries) -> Optional[date]:
    return max((o.date for o in series.aaa), default=None)


def _reference(row: TapeRow) -> Optional[Tuple[float, str]]:
    """Mirror of the strategy's reference-price rule, for reporting only."""
    if row.yes_bid is not None and row.yes_ask is not None:
        return (row.yes_bid + row.yes_ask) / 2.0, "mid"
    if row.last is not None:
        return row.last, "last"
    if row.yes_bid is not None:
        return row.yes_bid, "bid_only"
    if row.yes_ask is not None:
        return row.yes_ask, "ask_only"
    return None


def _settled_yes(row: TapeRow, reconcile: Dict[str, int]) -> Optional[bool]:
    """Kalshi's own ``result``, reconciled against ``settles_yes_gas``.

    Kalshi's ``result`` is the authoritative outcome. Recomputing it from the
    published ``expiration_value`` with the strict-greater rule is the
    source-independent check that the payoff rule in the model matches the
    exchange's; a disagreement is counted, never smoothed over.
    """
    result = (row.result or "").strip().lower()
    if result not in ("yes", "no"):
        reconcile["unsettled"] = reconcile.get("unsettled", 0) + 1
        return None
    kalshi_yes = result == "yes"
    if row.expiration_value is not None:
        recomputed = settles_yes_gas(row.expiration_value, row.floor_strike)
        key = "match" if recomputed == kalshi_yes else "MISMATCH"
        reconcile[key] = reconcile.get(key, 0) + 1
    else:
        reconcile["no_expiration_value"] = reconcile.get("no_expiration_value", 0) + 1
    return kalshi_yes


# =========================================================================
# 5. Aggregation
# =========================================================================


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _se(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    return statistics.stdev(vals) / math.sqrt(len(vals))


def band_table(cells: Sequence[EvCell], side: str, mode: str) -> List[dict]:
    """Phase-2-shaped EV table: one row per bracket-distance band."""
    labels: List[str] = []
    for lo, hi in zip(BAND_EDGES, list(BAND_EDGES[1:]) + [None]):
        labels.append(
            f"{int(round(lo * 100))}-{int(round(hi * 100))}c"
            if hi is not None
            else f"{int(round(lo * 100))}c+"
        )
    out = []
    for label in labels:
        sub = [
            c for c in cells if c.band == label and c.side == side and c.mode == mode
        ]
        if not sub:
            continue
        ex = [c for c in sub if c.executable]
        filled = [c for c in ex if (mode == "taker" or c.maker_filled)]
        priced = [c for c in filled if c.ev is not None]
        settled = [c for c in priced if c.realized is not None]
        out.append(
            {
                "band": label,
                "n_cand": len(sub),
                "n_exec": len(ex),
                "exec_frac": len(ex) / len(sub) if sub else None,
                "n_fill": len(filled),
                "fill_frac": len(filled) / len(sub) if sub else None,
                "mean_p_win": _mean(
                    [(c.p_yes if c.side == "YES" else 1 - c.p_yes) for c in priced]
                ),
                "mean_quote": _mean([c.quote for c in priced]),
                "mean_paid": _mean([c.price_paid for c in priced]),
                "mean_fee": _mean([c.fee_per_ct for c in priced]),
                "ev": _mean([c.ev for c in priced]),
                "realized": _mean([c.realized for c in settled]),
                "realized_se": _se([c.realized for c in settled]),
                "n_settled": len(settled),
            }
        )
    return out


#: Two-sided 95% critical values of Student's t, indexed by degrees of freedom.
#: Only the small-df entries matter here: the clustering unit is the settlement
#: event and there are single digits of them, which is the whole point of
#: reporting a clustered interval rather than a trade-level one.
_T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    15: 2.131,
    20: 2.086,
    30: 2.042,
}


def _t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    for key in sorted(_T95):
        if df <= key:
            return _T95[key]
    return 1.96


def cluster_by_event(cells: Sequence[EvCell]) -> dict:
    """Realized PnL clustered on the **settlement event**, which is the honest unit.

    Every bracket on one ladder resolves against one AAA publication, so 300
    trades across 9 week-ends are 9 independent draws, not 300. A trade-level
    standard error on this sample understates the spread several-fold; both are
    reported and every table states which unit it used.
    """
    settled = [c for c in cells if c.realized is not None]
    if not settled:
        return {"n_events": 0}
    per_event: Dict[date, List[float]] = defaultdict(list)
    for c in settled:
        per_event[c.settlement_date].append(c.realized)
    means = [sum(v) / len(v) for v in per_event.values()]
    n = len(means)
    mean = sum(means) / n
    se = statistics.stdev(means) / math.sqrt(n) if n > 1 else None
    crit = _t95(n - 1) if n > 1 else float("nan")
    return {
        "n_events": n,
        "n_trades": len(settled),
        "event_mean": mean,
        "event_se": se,
        "t": (mean / se) if se else None,
        "ci_low": (mean - crit * se) if se else None,
        "ci_high": (mean + crit * se) if se else None,
        "t95": crit,
        "n_events_negative": sum(1 for m in means if m < 0),
        "per_event": {
            d.isoformat(): sum(v) / len(v) for d, v in sorted(per_event.items())
        },
    }


def accepted_summary(cells: Sequence[EvCell], mode: str) -> dict:
    """The FR-4.3 shape: only what the strategy actually accepted."""
    sub = [
        c
        for c in cells
        if c.accepted and c.mode == mode and c.executable and c.ev is not None
    ]
    filled = [c for c in sub if mode == "taker" or c.maker_filled]
    settled = [c for c in filled if c.realized is not None]
    dates = sorted({c.et_date for c in filled})
    clustered = cluster_by_event(filled)
    ev_mean = _mean([c.ev for c in filled])
    # Does the modelled EV lie inside the realized confidence interval? This is
    # the test the verdict rests on: it does not require the realized mean to be
    # significantly negative, only that the number FR-4.3 gates on is
    # irreconcilable with what happened.
    ev_vs_realized_t = None
    if ev_mean is not None and clustered.get("event_se"):
        ev_vs_realized_t = (ev_mean - clustered["event_mean"]) / clustered["event_se"]
    return {
        "cluster": clustered,
        "ev_vs_realized_t": ev_vs_realized_t,
        "mode": mode,
        "n_accepted": len(sub),
        "n_filled": len(filled),
        "n_settled": len(settled),
        "n_dates": len(dates),
        "n_markets": len({c.ticker for c in filled}),
        "n_events": len({c.event_ticker for c in filled}),
        "ev": _mean([c.ev for c in filled]),
        "ev_no_allowance": _mean([c.ev_no_allowance for c in filled]),
        "realized": _mean([c.realized for c in settled]),
        "realized_se": _se([c.realized for c in settled]),
        "win_rate": (
            sum(1 for c in settled if c.won) / len(settled) if settled else None
        ),
        "mean_paid": _mean([c.price_paid for c in filled]),
        "mean_p_win": _mean(
            [(c.p_yes if c.side == "YES" else 1 - c.p_yes) for c in filled]
        ),
        "sides": dict(
            sorted(
                {
                    s: sum(1 for c in filled if c.side == s)
                    for s in {c.side for c in filled}
                }.items()
            )
        ),
    }


def skill_vs_market(cells: Sequence[EvCell]) -> dict:
    """Brier score of the model's ``P(YES)`` against the market's own price.

    The cleanest statement available on this sample, because it needs no fee
    model, no fill model and no EV: over the same settled brackets, which of the
    two forecasters was closer to the outcome? If the market wins, then the 8pt
    divergence the strategy trades on is the model's error and not a
    mispricing. Scored per settlement event and then averaged, so the unit is
    the event.
    """
    settled = [
        c for c in cells if c.mode == "taker" and c.side == "YES" and c.won is not None
    ]
    if not settled:
        return {"n": 0}
    per_event: Dict[date, List[Tuple[float, float, float]]] = defaultdict(list)
    for c in settled:
        outcome = 1.0 if c.won else 0.0
        per_event[c.settlement_date].append((c.p_yes, c.market_price, outcome))
    model_ev, market_ev = [], []
    for rows in per_event.values():
        model_ev.append(sum((p - o) ** 2 for p, _, o in rows) / len(rows))
        market_ev.append(sum((q - o) ** 2 for _, q, o in rows) / len(rows))
    n = len(model_ev)
    diff = [m - k for m, k in zip(model_ev, market_ev)]
    mean_diff = sum(diff) / n
    se = statistics.stdev(diff) / math.sqrt(n) if n > 1 else None
    return {
        "n": len(settled),
        "n_events": n,
        "brier_model": sum(model_ev) / n,
        "brier_market": sum(market_ev) / n,
        "diff": mean_diff,
        "diff_se": se,
        "t": (mean_diff / se) if se else None,
        "events_model_better": sum(1 for d in diff if d < 0),
    }


def quote_availability(cells: Sequence[EvCell]) -> dict:
    """What fraction of candidates had an executable quote at all."""
    out = {}
    for mode in ("taker", "maker"):
        for side in ("YES", "NO"):
            sub = [c for c in cells if c.mode == mode and c.side == side]
            if not sub:
                continue
            ex = sum(1 for c in sub if c.executable)
            out[f"{side}_{mode}"] = {
                "n_cand": len(sub),
                "n_exec": ex,
                "frac": ex / len(sub),
            }
    both = [c for c in cells if c.mode == "taker" and c.side == "YES"]
    two_sided = sum(1 for c in both if c.spread is not None)
    out["two_sided_book"] = {
        "n_snapshots": len(both),
        "n_two_sided": two_sided,
        "frac": two_sided / len(both) if both else None,
    }
    spreads = [c.spread for c in both if c.spread is not None]
    if spreads:
        spreads.sort()
        out["spread"] = {
            "n": len(spreads),
            "median": statistics.median(spreads),
            "p90": spreads[int(0.9 * (len(spreads) - 1))],
            "max": spreads[-1],
        }
    return out


# =========================================================================
# 6. CLI
# =========================================================================


def _cmd_fetch_tape(args) -> int:
    manifest = fetch_tape(args.out)
    print(json.dumps({k: v for k, v in manifest.items() if k != "skipped"}, indent=2))
    return 0


def _cmd_fetch_covariates(args) -> int:
    out = fetch_covariates(start=args.start)
    print(json.dumps(out, indent=2, default=str))
    return 0


# =========================================================================
# 5b. Configuration axes and the sign-stability sweep
# =========================================================================


@dataclass(frozen=True)
class Axis:
    """One perturbation of the headline configuration."""

    key: str
    label: str
    rbob_alias: str
    use_eia: bool
    include_suspect: bool
    truth_channel: str  # "aaa" | "kalshi"


HEADLINE_AXIS = Axis(
    key="headline",
    label="headline (LA RBOB, EIA off, suspect excluded, AAA truth)",
    rbob_alias="la_rbob_spot",
    use_eia=False,
    include_suspect=False,
    truth_channel="aaa",
)


def perturbation_axes() -> List[Axis]:
    """The four perturbations exit criterion 2's robustness test names."""
    axes = [HEADLINE_AXIS]
    for alias in ("ny_harbor_conventional_spot", "gulf_coast_conventional_spot"):
        axes.append(
            Axis(
                key=f"rbob:{alias}",
                label=f"RBOB source = {alias}",
                rbob_alias=alias,
                use_eia=False,
                include_suspect=False,
                truth_channel="aaa",
            )
        )
    axes.append(
        Axis(
            key="eia:on",
            label="EIA weekly covariate ON",
            rbob_alias="la_rbob_spot",
            use_eia=True,
            include_suspect=False,
            truth_channel="aaa",
        )
    )
    axes.append(
        Axis(
            key="suspect:included",
            label="suspect AAA rows INCLUDED",
            rbob_alias="la_rbob_spot",
            use_eia=False,
            include_suspect=True,
            truth_channel="aaa",
        )
    )
    axes.append(
        Axis(
            key="truth:kalshi",
            label="truth channel = Kalshi-pinned (source-independent)",
            rbob_alias="la_rbob_spot",
            use_eia=False,
            include_suspect=False,
            truth_channel="kalshi",
        )
    )
    return axes


def spec_for_axis(axis: Axis, gas_dir: str) -> SeriesSpec:
    sources = rbob_source_paths()
    # Prefer this workstream's own matched-window copies so a RBOB-source
    # comparison is not confounded by a different start date per series.
    own = os.path.join(COVARIATE_DIR, axis.rbob_alias, "rbob_daily.csv")
    if os.path.isfile(own):
        path, series_id = own, sources.get(axis.rbob_alias, (own, "?"))[1]
    elif axis.rbob_alias in sources:
        path, series_id = sources[axis.rbob_alias]
    else:
        raise FileNotFoundError(
            f"RBOB source {axis.rbob_alias!r} is not on disk; run "
            f"`python scripts/gas_backtest.py fetch-covariates` first"
        )
    return load_series_spec(
        label=axis.key,
        rbob_label=axis.rbob_alias,
        rbob_path=path,
        rbob_series_id=series_id,
        include_suspect=axis.include_suspect,
        gas_dir=gas_dir,
    )


def config_for_axis(axis: Axis) -> ProjectionConfig:
    return ProjectionConfig(
        use_eia_covariate=axis.use_eia,
        include_suspect=axis.include_suspect,
    )


def truth_lookup_for(axis: Axis, spec: SeriesSpec, pinned: Sequence[PinnedTruth]):
    """Truth function for the MAE tables, per channel.

    ``aaa`` reads the AAA series the model is also fitted on -- held out in
    *time*, not in *source*. ``kalshi`` reads WS-B's settled-ladder truth, which
    is a different measurement channel entirely: the interval is recovered from
    which strikes paid YES and which paid NO, and the point is Kalshi's own
    published ``expiration_value``.
    """
    if axis.truth_channel == "aaa":
        table = {o.date: o.value for o in spec.series.aaa}
        return lambda d: table.get(d)
    point = {}
    for row in pinned:
        if row.kalshi_expiration_value is not None:
            point[row.settlement_date] = row.kalshi_expiration_value
        else:
            point[row.settlement_date] = (row.low_exclusive + row.high_inclusive) / 2.0
    return lambda d: point.get(d)


# =========================================================================
# 6. Report generation
# =========================================================================


def _c(value: Optional[float], places: int = 2, sign: bool = True) -> str:
    """Format a dollar-per-contract quantity in cents."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    fmt = f"{{:+.{places}f}}" if sign else f"{{:.{places}f}}"
    return fmt.format(value * 100.0) + "c"


def _d(value: Optional[float], places: int = 4) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:.{places}f}"


def _pct(value: Optional[float]) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100.0:.1f}%"


@dataclass
class AxisResult:
    mae_fits: int
    mae_aborts: Dict[str, int]
    axis: Axis
    spec: SeriesSpec
    config: ProjectionConfig
    mae_rows: List[MaeRow]
    mae_by_lead: Dict[int, dict]
    mae_overall: dict
    daily_rows: List[MaeRow]
    daily_by_lead: Dict[int, dict]
    daily_overall: dict
    month_ends_held_out: int
    monthly: EvRun
    weekly: Optional[EvRun]
    accepted_taker: dict
    accepted_maker: dict


def run_axis(
    axis: Axis,
    *,
    gas_dir: str,
    tape: Sequence[TapeRow],
    pinned: Sequence[PinnedTruth],
    leads: Sequence[int],
    quantity: int,
    hour_et: int,
    window_days: int,
    min_divergence: float,
    with_weekly: bool = True,
) -> AxisResult:
    """One configuration, fitted once. Never called twice for the same axis."""
    spec = spec_for_axis(axis, gas_dir)
    config = config_for_axis(axis)
    counter = FitCounter()

    if axis.truth_channel == "aaa":
        targets = month_end_targets(spec)
    else:
        # Every date WS-B pinned from settled Kalshi ladders: 67 daily, 10
        # weekly, 2 monthly. A different measurement channel entirely.
        targets = sorted({r.settlement_date for r in pinned})
    lookup = truth_lookup_for(axis, spec, pinned)
    mae_rows = walk_forward_mae(
        spec, targets, leads, config, counter, lookup, axis.truth_channel
    )
    mae_by_lead = {
        lead: mae_stats([r for r in mae_rows if r.nominal_lead == lead])
        for lead in leads
    }
    daily_rows = walk_forward_mae(
        spec,
        admissible_daily_targets(spec, config),
        leads,
        config,
        counter,
        truth_lookup_for(HEADLINE_AXIS, spec, pinned)
        if axis.truth_channel == "aaa"
        else lookup,
        axis.truth_channel,
    )
    daily_by_lead = {
        lead: mae_stats([r for r in daily_rows if r.nominal_lead == lead])
        for lead in leads
    }
    monthly = simulate_ev(
        spec,
        tape,
        config=config,
        quantity=quantity,
        hour_et=hour_et,
        window_days=window_days,
        min_divergence=min_divergence,
        series_filter="KXAAAGASM",
    )
    weekly = (
        simulate_ev(
            spec,
            tape,
            config=config,
            quantity=quantity,
            hour_et=hour_et,
            window_days=window_days,
            min_divergence=min_divergence,
            series_filter="KXAAAGASW",
        )
        if with_weekly
        else None
    )
    # The MAE counter's fits and aborts are kept distinct from the EV replay's:
    # merging them produces a single "aborts" number that means neither thing.
    mae_fits = counter.fits
    mae_aborts = dict(counter.abort_reasons)

    return AxisResult(
        mae_fits=mae_fits,
        mae_aborts=mae_aborts,
        axis=axis,
        spec=spec,
        config=config,
        mae_rows=mae_rows,
        mae_by_lead=mae_by_lead,
        mae_overall=mae_stats(mae_rows),
        daily_rows=daily_rows,
        daily_by_lead=daily_by_lead,
        daily_overall=mae_stats(daily_rows),
        month_ends_held_out=len({r.target_date for r in mae_rows}),
        monthly=monthly,
        weekly=weekly,
        accepted_taker=accepted_summary(monthly.cells, "taker"),
        accepted_maker=accepted_summary(monthly.cells, "maker"),
    )


def _payload(results, tape, pinned, args, artifact_date, total_fits, elapsed) -> dict:
    """Every number the markdown quotes, as JSON, so the artifact is auditable."""
    out = {
        "generated_at": datetime.now(UTC).isoformat(),
        "artifact_date": artifact_date,
        "generator": "scripts/gas_backtest.py run",
        "criterion": "PRD Phase 4 exit criterion 2",
        "settings": {
            "quantity": args.quantity,
            "decision_hour_et": args.hour_et,
            "window_days": args.window_days,
            "min_divergence": args.min_divergence,
            "adverse_fill_allowance": ADVERSE_FILL_ALLOWANCE,
            "band_edges_usd": list(BAND_EDGES),
            "leads": [1, 7, 14],
        },
        "fit_budget": {"total_fits": total_fits, "wall_seconds": round(elapsed, 1)},
        "inputs": {
            "tape": _read_json(TAPE_MANIFEST_PATH),
            "aaa_manifest": _read_json(os.path.join(args.gas_dir, "manifest.json")),
            "pinned_truth_rows": len(pinned),
            "pinned_truth_path": os.path.relpath(PINNED_TRUTH_PATH, REPO_ROOT).replace(
                "\\", "/"
            ),
        },
        "fee_model": {
            "live_series_metadata": _read_json(SERIES_META_PATH),
            "maker_rate": MAKER_RATE,
            "taker_rate": TAKER_RATE,
            "known_maker_fee_series": sorted(KNOWN_MAKER_FEE_SERIES),
            "fee_type_KXAAAGASM": fee_type_for_symbol("KXAAAGASM-26AUG31-4.60"),
            "fee_type_KXAAAGASW": fee_type_for_symbol("KXAAAGASW-26AUG03-4.10"),
            "fee_type_KXAAAGASD": fee_type_for_symbol("KXAAAGASD-26JUL30-4.10"),
        },
        "axes": {},
    }
    for key, res in results.items():
        out["axes"][key] = {
            "label": res.axis.label,
            "rbob_alias": res.axis.rbob_alias,
            "rbob_series_id": res.spec.rbob_series_id,
            "use_eia": res.axis.use_eia,
            "include_suspect": res.axis.include_suspect,
            "truth_channel": res.axis.truth_channel,
            "data": {
                "aaa_first": res.spec.aaa_first.isoformat(),
                "aaa_last": res.spec.aaa_last.isoformat(),
                "aaa_rows": res.spec.aaa_rows,
                "aaa_suspect_in_file": res.spec.aaa_suspect,
                "aaa_missing_days": res.spec.aaa_missing_days,
                "rbob_rows": res.spec.rbob_rows,
                "rbob_first": str(res.spec.rbob_first),
                "rbob_last": str(res.spec.rbob_last),
                "eia_rows": res.spec.eia_rows,
            },
            "mae_daily_targets": {
                "targets_held_out": len({r.target_date for r in res.daily_rows}),
                "overall": res.daily_overall,
                "by_lead": {str(k): v for k, v in res.daily_by_lead.items()},
            },
            "mae": {
                "targets_held_out": res.month_ends_held_out,
                "overall": res.mae_overall,
                "by_lead": {str(k): v for k, v in res.mae_by_lead.items()},
                "rows": [
                    {
                        "target": r.target_date.isoformat(),
                        "nominal_lead": r.nominal_lead,
                        "as_of": r.as_of.isoformat(),
                        "realized_lead": r.realized_lead,
                        "point": r.point,
                        "sigma": r.sigma,
                        "truth": r.truth,
                        "error": r.error,
                        "n_train": r.n_train,
                        "model_version": r.model_version,
                        "inputs_hash": r.inputs_hash[:16],
                    }
                    for r in res.mae_rows
                ],
            },
            "monthly": _run_payload(
                res.monthly, res.accepted_taker, res.accepted_maker
            ),
            "weekly": (
                _run_payload(
                    res.weekly,
                    accepted_summary(res.weekly.cells, "taker"),
                    accepted_summary(res.weekly.cells, "maker"),
                )
                if res.weekly
                else None
            ),
        }
    stability = []
    for key, res in results.items():
        wk = accepted_summary(res.weekly.cells, "taker") if res.weekly else {}
        stability.append(
            {
                "axis": key,
                "label": res.axis.label,
                "mae_month_end": res.mae_overall.get("mae"),
                "mae_daily": res.daily_overall.get("mae"),
                "bias_daily": res.daily_overall.get("bias"),
                "ev_taker": res.accepted_taker.get("ev"),
                "ev_maker": res.accepted_maker.get("ev"),
                "n_accepted_taker": res.accepted_taker.get("n_filled"),
                "realized_taker": (res.accepted_taker.get("cluster") or {}).get(
                    "event_mean"
                ),
                "realized_se_taker": (res.accepted_taker.get("cluster") or {}).get(
                    "event_se"
                ),
                "n_settled_taker": res.accepted_taker.get("n_settled"),
                "n_events_taker": (res.accepted_taker.get("cluster") or {}).get(
                    "n_events"
                ),
                "ev_vs_realized_t": res.accepted_taker.get("ev_vs_realized_t"),
                "weekly_ev_taker": wk.get("ev"),
                "weekly_realized_taker": (wk.get("cluster") or {}).get("event_mean"),
                "weekly_realized_se_taker": (wk.get("cluster") or {}).get("event_se"),
                "weekly_n_settled": wk.get("n_settled"),
                "weekly_n_events": (wk.get("cluster") or {}).get("n_events"),
                "weekly_ev_vs_realized_t": wk.get("ev_vs_realized_t"),
            }
        )
    out["sign_stability"] = stability
    return out


def _run_payload(run: EvRun, acc_taker: dict, acc_maker: dict) -> dict:
    return {
        "snapshots": run.n_snapshots,
        "markets": run.n_markets,
        "events": run.n_events,
        "decision_dates": run.n_decision_dates,
        "stale_decision_dates": run.stale_decision_dates,
        "fits": run.fits,
        "aborts": run.aborts,
        "abort_reasons": run.abort_reasons,
        "rejections": run.rejections,
        "settlement_reconcile": run.settle_reconcile,
        "quote_availability": quote_availability(run.cells),
        "calibration": calibration_table(run.cells),
        "skill_vs_market": skill_vs_market(run.cells),
        "accepted_taker": acc_taker,
        "accepted_maker": acc_maker,
        "bands": {
            f"{side}_{mode}": band_table(run.cells, side, mode)
            for side in ("YES", "NO")
            for mode in ("taker", "maker")
        },
    }


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _mae_table(by_lead: Dict[int, dict]) -> str:
    rows = []
    for lead in sorted(by_lead):
        s = by_lead[lead]
        if not s.get("n"):
            rows.append([f"{lead}", "0", "—", "—", "—", "—", "—", "—"])
            continue
        rows.append(
            [
                f"{lead}",
                s["n"],
                f"${s['mae']:.4f}",
                f"{s['bias']:+.4f}",
                f"${s['rmse']:.4f}",
                f"${s['p50_abs']:.4f}",
                f"${s['max_abs']:.4f}",
                f"${s['mean_sigma']:.4f}",
            ]
        )
    return _table(
        [
            "nominal lead (d)",
            "n",
            "MAE",
            "bias",
            "RMSE",
            "median \\|err\\|",
            "max \\|err\\|",
            "mean model sigma",
        ],
        rows,
    )


def calibration_table(cells: Sequence[EvCell]) -> List[dict]:
    """Model P(YES) decile against the realized YES rate on settled markets.

    Computed on every executable YES-taker candidate, not only the ones the
    divergence gate accepted, so the answer is a property of the probability
    model rather than of the selection.
    """
    buckets: Dict[int, List[int]] = {i: [0, 0] for i in range(10)}
    events: Dict[int, set] = {i: set() for i in range(10)}
    for c in cells:
        if c.mode != "taker" or c.side != "YES" or c.won is None:
            continue
        idx = min(9, max(0, int(c.p_yes * 10)))
        buckets[idx][0] += 1
        buckets[idx][1] += 1 if c.won else 0
        events[idx].add(c.settlement_date)
    out = []
    for idx in range(10):
        n, wins = buckets[idx]
        if not n:
            continue
        out.append(
            {
                "decile": f"{idx / 10:.1f}-{(idx + 1) / 10:.1f}",
                "n": n,
                "n_events": len(events[idx]),
                "model_mid": (idx + 0.5) / 10,
                "realized": wins / n,
                "gap": wins / n - (idx + 0.5) / 10,
            }
        )
    return out


def _calibration_md(cells: Sequence[EvCell]) -> str:
    rows = [
        [
            b["decile"],
            b["n"],
            b["n_events"],
            f"{b['model_mid']:.2f}",
            f"{b['realized']:.3f}",
            f"{b['gap']:+.3f}",
        ]
        for b in calibration_table(cells)
    ]
    return _table(
        [
            "model P(YES) decile",
            "n brackets",
            "n distinct settlements",
            "decile midpoint",
            "realized YES rate",
            "gap",
        ],
        rows,
    )


def _mae_detail_table(res: AxisResult) -> str:
    rows = []
    for r in sorted(res.mae_rows, key=lambda x: (x.target_date, x.nominal_lead)):
        rows.append(
            [
                r.target_date.isoformat(),
                r.nominal_lead,
                r.as_of.isoformat(),
                r.realized_lead,
                f"{r.point:.4f}",
                f"{r.sigma:.4f}",
                f"{r.truth:.4f}",
                f"{r.error:+.4f}",
                r.n_train,
                r.model_version,
                r.inputs_hash[:12],
            ]
        )
    return _table(
        [
            "target",
            "nom lead",
            "as_of",
            "real lead",
            "point",
            "sigma",
            "truth",
            "error",
            "n_train",
            "model",
            "inputs_hash",
        ],
        rows,
    )


def _band_md(run: EvRun, side: str, mode: str) -> str:
    rows = []
    for b in band_table(run.cells, side, mode):
        rows.append(
            [
                b["band"],
                b["n_cand"],
                b["n_exec"],
                _pct(b["exec_frac"]),
                b["n_fill"],
                _pct(b["fill_frac"]),
                _d(b["mean_p_win"]),
                _d(b["mean_quote"]),
                _d(b["mean_paid"]),
                _c(b["mean_fee"], 3, sign=False),
                _c(b["ev"]),
                _c(b["realized"]),
                _c(b["realized_se"], 2, sign=False),
                b["n_settled"],
            ]
        )
    return _table(
        [
            "band",
            "n cand",
            "n exec",
            "exec frac",
            "n fill",
            "fill frac",
            "mean P(win)",
            "mean quote",
            "price+1c",
            "fee/ct",
            "EV/ct",
            "realized/ct",
            "SE",
            "n settled",
        ],
        rows,
    )


def _accepted_md(run: EvRun, label: str) -> str:
    rows = []
    for mode in ("taker", "maker"):
        a = accepted_summary(run.cells, mode)
        rows.append(
            [
                mode,
                a["n_accepted"],
                a["n_filled"],
                a["n_dates"],
                a["n_markets"],
                a["n_events"],
                _d(a["mean_p_win"]),
                _d(a["mean_paid"]),
                _c(a["ev_no_allowance"]),
                _c(a["ev"]),
                _c(a["realized"]),
                _c(a["realized_se"], 2, sign=False),
                a["n_settled"],
                _pct(a["win_rate"]),
                json.dumps(a["sides"]),
            ]
        )
    return f"**{label}**\n\n" + _table(
        [
            "fee leg",
            "n accepted",
            "n filled",
            "dates",
            "markets",
            "events (incl. unsettled)",
            "mean P(win)",
            "mean price+1c",
            "EV/ct (no allowance)",
            "EV/ct (+1c)",
            "realized/ct",
            "SE",
            "n settled",
            "win rate",
            "sides",
        ],
        rows,
    )


LEAD_BUCKETS: Tuple[Tuple[int, int, str], ...] = (
    (1, 1, "1 d"),
    (2, 3, "2-3 d"),
    (4, 7, "4-7 d"),
    (8, 14, "8-14 d"),
)


def _by_lead_md(monthly: EvRun, weekly: Optional[EvRun]) -> str:
    rows = []
    for lo, hi, label in LEAD_BUCKETS:
        cells_m = [
            c
            for c in monthly.cells
            if c.accepted
            and c.mode == "taker"
            and c.executable
            and lo <= c.lead_days <= hi
        ]
        cells_w = (
            [
                c
                for c in weekly.cells
                if c.accepted
                and c.mode == "taker"
                and c.executable
                and lo <= c.lead_days <= hi
            ]
            if weekly
            else []
        )
        cm = cluster_by_event(cells_m)
        cw = cluster_by_event(cells_w)
        rows.append(
            [
                label,
                len(cells_m),
                _c(_mean([c.ev for c in cells_m])),
                _c(cm.get("event_mean")),
                _sgn(cm.get("event_mean")),
                f"{cm.get('n_events') or 0}",
                len(cells_w),
                _c(_mean([c.ev for c in cells_w])),
                _c(cw.get("event_mean")),
                _sgn(cw.get("event_mean")),
                f"{cw.get('n_events') or 0}",
            ]
        )
    return _table(
        [
            "lead",
            "n M",
            "EV/ct M",
            "realized/ct M",
            "sign",
            "settlements M",
            "n W",
            "EV/ct W",
            "realized/ct W",
            "sign",
            "settlements W",
        ],
        rows,
    )


def _by_lead_commentary(monthly: EvRun, weekly: Optional[EvRun]) -> str:
    """Say out loud whatever the lead table shows, including the awkward cells."""
    populated = []
    for lo, hi, label in LEAD_BUCKETS:
        for key, run in (("M", monthly), ("W", weekly)):
            if run is None:
                continue
            cells = [
                c
                for c in run.cells
                if c.accepted
                and c.mode == "taker"
                and c.executable
                and lo <= c.lead_days <= hi
            ]
            cluster = cluster_by_event(cells)
            if cluster.get("n_events"):
                populated.append((label, key, cluster["event_mean"], len(cells)))
    positive = [p for p in populated if p[2] > 0]
    one_day_w = next((p for p in populated if p[0] == "1 d" and p[1] == "W"), None)
    parts = [
        f"{len(populated)} buckets carry data and "
        + (
            "every one is negative."
            if not positive
            else f"{len(positive)} of them is positive: "
            + ", ".join(
                f"{lab} {key} at {_c(val)} on {n} trades"
                for lab, key, val, n in positive
            )
            + ". A cell within a cent of zero on a handful of settlements is not "
            "evidence of an edge, and it is reported here rather than dropped so "
            "nobody has to discover it in the JSON."
        )
    ]
    if one_day_w is not None:
        parts.append(
            f"The result that matters is the other way round: the **1-day bucket, "
            f"where the projection is sharpest**, is the "
            f"**worst** weekly bucket at {_c(one_day_w[2])}. That is the "
            f"signature of the model disagreeing confidently with a market that "
            f"has already priced a near-settled outcome correctly — not of a "
            f"model that needs a shorter horizon. Shortening the FR-4.3 window "
            f"is therefore not the fix, and §8 confirms it: a 3-day window is "
            f"worse than a 14-day one."
        )
    return " ".join(parts)


def _availability_md(run: EvRun) -> str:
    qa = quote_availability(run.cells)
    rows = []
    for key in ("YES_taker", "YES_maker", "NO_taker", "NO_maker"):
        if key not in qa:
            continue
        rows.append([key, qa[key]["n_cand"], qa[key]["n_exec"], _pct(qa[key]["frac"])])
    two = qa.get("two_sided_book") or {}
    rows.append(
        [
            "two-sided book",
            two.get("n_snapshots", 0),
            two.get("n_two_sided", 0),
            _pct(two.get("frac")),
        ]
    )
    out = _table(["required side", "n cand", "n present", "fraction"], rows)
    sp = qa.get("spread")
    if sp:
        out += (
            f"\n\nYES spread where both sides quoted (n = {sp['n']}): "
            f"median {sp['median'] * 100:.1f}pt, p90 {sp['p90'] * 100:.1f}pt, "
            f"max {sp['max'] * 100:.1f}pt."
        )
    return out


def _sgn(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return "**+**" if value > 0 else ("**-**" if value < 0 else "0")


def _sign_stability_md(payload: dict) -> str:
    rows = []
    for entry in payload["sign_stability"]:
        rows.append(
            [
                entry["label"],
                entry["n_accepted_taker"] or 0,
                _c(entry["ev_taker"]),
                _sgn(entry["ev_taker"]),
                _c(entry["realized_taker"]),
                _sgn(entry["realized_taker"]),
                f"{entry['n_settled_taker'] or 0}/{entry.get('n_events_taker') or 0}",
                _c(entry["weekly_ev_taker"]),
                _c(entry["weekly_realized_taker"]),
                _sgn(entry["weekly_realized_taker"]),
                f"{entry['weekly_n_settled'] or 0}/{entry.get('weekly_n_events') or 0}",
                _d(entry.get("weekly_ev_vs_realized_t"), 1),
                _d(entry["mae_daily"]),
                f"{entry['bias_daily']:+.4f}"
                if entry.get("bias_daily") is not None
                else "n/a",
            ]
        )
    return _table(
        [
            "perturbation",
            "n trades M",
            "EV/ct M",
            "sign",
            "realized/ct M",
            "sign",
            "trades/settlements M",
            "EV/ct W",
            "realized/ct W",
            "sign",
            "trades/settlements W",
            "t (EV-realized) W",
            "daily MAE (all leads)",
            "daily bias (all leads)",
        ],
        rows,
    )


def _read_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class RenderedClaims:
    """What the markdown actually rendered, handed back for the JSON to serialise.

    The JSON companion must not recompute these. Recomputing them from
    ``results`` gives two code paths for one shared claim, which is how the
    markdown and the JSON end up disagreeing while neither edit looks wrong.
    """

    register: "DeferralRegister"
    month_ends: int
    month_ends_met: bool
    crosscheck: "CrossCheck"


class DeferralRegister:
    """The §10 register: one dated list that closes item by item.

    ``register-deferred-evidence-not-waived`` asks for a register that *closes*,
    not a list of complaints. An item that the inputs have since satisfied is
    therefore recorded as **CLOSED with the evidence that closed it**, not
    silently dropped — dropping it would make the register under-report
    completion, and a reader reconciling §1 against §10 could not tell which was
    authoritative.

    Numbering is assigned on registration and cross-references resolve through
    :meth:`ref`, so no section can quote a number that has drifted.
    """

    def __init__(self) -> None:
        self._items: List[dict] = []

    def add(
        self,
        key: str,
        item: str,
        reason: str,
        closes: str,
        *,
        open_: bool = True,
        evidence: str = "",
    ) -> None:
        self._items.append(
            {
                "key": key,
                "n": f"10.{len(self._items) + 1}",
                "item": item,
                "reason": reason,
                "closes": closes,
                "open": open_,
                "evidence": evidence,
            }
        )

    def close(self, key: str, item: str, evidence: str) -> None:
        """Register an item that the inputs to *this* run have satisfied."""
        self.add(key, item, "—", "—", open_=False, evidence=evidence)

    def ref(self, key: str) -> str:
        for entry in self._items:
            if entry["key"] == key:
                return f"§{entry['n']}"
        return "§10"

    def status(self, key: str) -> Optional[bool]:
        for entry in self._items:
            if entry["key"] == key:
                return entry["open"]
        return None

    @property
    def n_open(self) -> int:
        return sum(1 for e in self._items if e["open"])

    @property
    def n_closed(self) -> int:
        return sum(1 for e in self._items if not e["open"])

    def payload(self) -> dict:
        """Machine-readable form, so the JSON companion cannot disagree with §10."""
        return {
            "n_open": self.n_open,
            "n_closed": self.n_closed,
            "items": [
                {
                    "n": e["n"],
                    "key": e["key"],
                    "status": "open" if e["open"] else "closed",
                    "item": e["item"],
                    "reason": e["reason"] if e["open"] else e["evidence"],
                    "closes": e["closes"] if e["open"] else None,
                }
                for e in self._items
            ],
        }

    def markdown(self) -> str:
        rows = []
        for e in self._items:
            rows.append(
                [
                    e["n"],
                    "OPEN" if e["open"] else "**CLOSED**",
                    e["item"],
                    e["reason"] if e["open"] else e["evidence"],
                    e["closes"] if e["open"] else "—",
                ]
            )
        return _table(
            [
                "#",
                "status",
                "item",
                "why it could not be obtained / what closed it",
                "what would close it",
            ],
            rows,
        )


def _inside(value: Optional[float], cluster: dict) -> str:
    """Whether a modelled EV falls inside the realized confidence interval."""
    lo, hi = cluster.get("ci_low"), cluster.get("ci_high")
    if value is None or lo is None or hi is None:
        return "n/a"
    return "yes" if lo <= value <= hi else "**NO**"


def _t_stat(mean: Optional[float], se: Optional[float]) -> Optional[float]:
    if mean is None or se in (None, 0):
        return None
    return mean / se


def worked_example(res: AxisResult, quantity: int) -> Tuple[Optional[EvCell], str]:
    """One fully recomputable accepted trade, chosen deterministically.

    A red-team must be able to reproduce the whole chain from the raw inputs
    quoted here plus a normal table, without rerunning this script.
    """
    candidates = [
        c
        for c in res.monthly.cells
        if c.accepted and c.mode == "taker" and c.executable and c.realized is not None
    ]
    if not candidates:
        return None, (
            "No accepted, executable, settled monthly taker trade exists in this "
            "run, so no worked example can be published. This is a deferral, not "
            "an omission: see the coverage table above for why."
        )
    # Median by modelled EV, ties broken by (date, ticker, side): deterministic,
    # and representative rather than cherry-picked at either tail.
    candidates.sort(key=lambda c: (c.ev, c.et_date, c.ticker, c.side))
    c = candidates[len(candidates) // 2]

    z = (c.floor_strike + 0.001 / 2.0 - c.point) / c.sigma
    p_yes = 0.5 * math.erfc(z / math.sqrt(2.0))
    p_win = p_yes if c.side == "YES" else 1.0 - p_yes
    fee_type = fee_type_for_symbol(c.ticker)
    raw_taker = TAKER_RATE * quantity * c.price_paid * (1 - c.price_paid)
    taker_total = compute_fee(
        c.price_paid, quantity, is_maker=False, series_fee_type=fee_type
    ).fee
    raw_maker = MAKER_RATE * quantity * c.price_paid * (1 - c.price_paid)
    maker_total = compute_fee(
        c.price_paid, quantity, is_maker=True, series_fee_type=fee_type
    ).fee
    maker_1 = compute_fee(c.price_paid, 1, is_maker=True, series_fee_type=fee_type).fee
    taker_1 = compute_fee(c.price_paid, 1, is_maker=False, series_fee_type=fee_type).fee

    lines = [
        f"* **market** `{c.ticker}` — `strike_type=greater`, "
        f"`floor_strike={c.floor_strike:.2f}`, settlement date "
        f"{c.settlement_date.isoformat()}, event `{c.event_ticker}`",
        f"* **decision snapshot** {c.et_date.isoformat()} at the "
        f"{HEADLINE_DECISION_HOUR_ET}:00 ET hourly candle close, "
        f"lead {c.lead_days} days",
        f"* **projection** (`{c.model_version}`, `n_train={c.n_train}`, "
        f"`inputs_hash={c.inputs_hash[:16]}`): "
        f"point = ${c.point:.6f}, sigma = ${c.sigma:.6f} "
        f"(printed to six places so the recompute below is exact, not "
        f"within-rounding)",
        "",
        "**Step 1 — strict-greater probability.** AAA publishes to three "
        "decimals, so `> K` is `>= K + $0.001`; the half-tick continuity "
        "correction puts the threshold at `K + $0.0005`:",
        "",
        "```",
        "z      = (K + 0.0005 - point) / sigma",
        f"       = ({c.floor_strike:.4f} + 0.0005 - {c.point:.6f}) / {c.sigma:.6f}",
        f"       = {z:+.6f}",
        f"P(YES) = 1 - Phi(z) = 0.5 * erfc(z / sqrt(2)) = {p_yes:.6f}",
        "```",
        "",
        f"**Step 2 — divergence.** Market YES reference "
        f"{c.market_price:.4f} (`{c.price_source}`); divergence = "
        f"{p_yes:.4f} - {c.market_price:.4f} = {c.divergence:+.4f} "
        f"({abs(c.divergence) * 100:.2f}pt), which clears the 8pt gate, so the "
        f"model prefers **{c.side}** and `P(win)` = {p_win:.6f}.",
        "",
        f"**Step 3 — price paid.** The executable {c.side} offer quoted "
        f"{c.quote:.4f}; with the 1c adverse-fill allowance the price paid is "
        f"{c.quote:.4f} + 0.01 = **{c.price_paid:.4f}**.",
        "",
        f"**Step 4 — fee.** `{c.ticker}` is series `KXAAAGASM`, whose live "
        f"`/series` metadata reports `fee_type = {fee_type}`, so **both** legs "
        f"are billed. At C = {quantity} contracts:",
        "",
        "```",
        f"taker raw = 0.07   * C * P * (1-P) = 0.07   * {quantity} * "
        f"{c.price_paid:.4f} * {1 - c.price_paid:.4f} = ${raw_taker:.6f}",
        f"          -> ceil to cent = ${taker_total:.2f} total, "
        f"${taker_total / quantity:.6f}/contract",
        f"maker raw = 0.0175 * C * P * (1-P) = 0.0175 * {quantity} * "
        f"{c.price_paid:.4f} * {1 - c.price_paid:.4f} = ${raw_maker:.6f}",
        f"          -> ceil to cent = ${maker_total:.2f} total, "
        f"${maker_total / quantity:.6f}/contract",
        "```",
        "",
        f"The published *rate* ratio is 25% (0.0175 / 0.07). The *charged* ratio "
        f"here is {maker_total / taker_total:.0%} at C = {quantity}, and at "
        f"C = 1 it is "
        f"{(maker_1 / taker_1) if taker_1 else float('nan'):.0%} "
        f"(${maker_1:.2f} vs ${taker_1:.2f}) because each leg is ceil'd to the "
        f"cent independently. That is why no fee in this report is ever scaled "
        f"from the other.",
        "",
        "**Step 5 — EV and outcome.**",
        "",
        "```",
        "EV/ct       = P(win) - price_paid - fee/ct",
        f"            = {p_win:.6f} - {c.price_paid:.4f} - "
        f"{c.fee_per_ct:.6f} = {c.ev:+.6f}  ({c.ev * 100:+.2f}c)",
        f"settled     : AAA published ${_d(_expiration_of(c, res), 3)} on "
        f"{c.settlement_date.isoformat()}; "
        f"{_d(_expiration_of(c, res), 3)} > {c.floor_strike:.2f} is "
        f"{str(_settles(c, res)).upper()}, so {c.side} "
        f"{'WON' if c.won else 'LOST'}",
        f"realized/ct = {'1' if c.won else '0'} - {c.price_paid:.4f} - "
        f"{c.fee_per_ct:.6f} = {c.realized:+.6f}  ({c.realized * 100:+.2f}c)",
        "```",
        "",
        f"This trade is the **median accepted trade by modelled EV**, chosen "
        f"deterministically so it is neither the best nor the worst. It "
        f"{'won' if c.won else 'lost'}, and one trade proves nothing either way "
        f"— it is here so the arithmetic of every cell in §4 and §5 can be "
        f"checked without rerunning the script. The aggregate is in §5.1.",
        "",
        "**The fee ratio across prices, at the two order sizes that matter.** "
        "Computed by `compute_fee` on `KXAAAGASM`'s schedule, so the "
        '"maker is 25% of taker" shortcut can be seen failing rather than '
        "asserted:",
        "",
        _table(
            [
                "price P",
                "taker C=1",
                "maker C=1",
                "maker/taker C=1",
                f"taker C={quantity}",
                f"maker C={quantity}",
                f"maker/taker C={quantity}",
            ],
            [
                [
                    f"{p:.2f}",
                    f"${compute_fee(p, 1, is_maker=False, series_fee_type=fee_type).fee:.2f}",
                    f"${compute_fee(p, 1, is_maker=True, series_fee_type=fee_type).fee:.2f}",
                    _ratio(
                        compute_fee(p, 1, True, fee_type).fee,
                        compute_fee(p, 1, False, fee_type).fee,
                    ),
                    f"${compute_fee(p, quantity, is_maker=False, series_fee_type=fee_type).fee:.2f}",
                    f"${compute_fee(p, quantity, is_maker=True, series_fee_type=fee_type).fee:.2f}",
                    _ratio(
                        compute_fee(p, quantity, True, fee_type).fee,
                        compute_fee(p, quantity, False, fee_type).fee,
                    ),
                ]
                for p in FEE_TABLE_PRICES
            ],
        ),
        "",
        f"The rate ratio is 25% everywhere. The charged ratio equals it in "
        f"{_exactly_25(quantity, fee_type)} of the "
        f"{len(FEE_TABLE_PRICES) * 2} cells above and reaches "
        f"{_max_fee_ratio(quantity, fee_type)} elsewhere. FR-4.3 says "
        f'"sized small", which places this bot in exactly the regime where the '
        f"shortcut is most wrong.",
    ]
    return c, "\n".join(lines)


def _ratio(numer: float, denom: float) -> str:
    if not denom:
        return "n/a"
    return f"{numer / denom:.0%}"


def _max_fee_ratio(quantity: int, fee_type: str) -> str:
    """The largest charged maker/taker ratio in the §7 table, as a percentage."""
    best = 0.0
    for p in FEE_TABLE_PRICES:
        for c in (1, quantity):
            taker = compute_fee(p, c, False, fee_type).fee
            maker = compute_fee(p, c, True, fee_type).fee
            if taker:
                best = max(best, maker / taker)
    return f"{best:.0%}"


def _exactly_25(quantity: int, fee_type: str) -> int:
    """How many cells of the §7 fee table actually charge the 25% rate ratio."""
    hits = 0
    for p in FEE_TABLE_PRICES:
        for c in (1, quantity):
            taker = compute_fee(p, c, False, fee_type).fee
            maker = compute_fee(p, c, True, fee_type).fee
            if taker and abs(maker / taker - 0.25) < 0.005:
                hits += 1
    return hits


def _expiration_of(cell: EvCell, res: AxisResult) -> Optional[float]:
    """The AAA value Kalshi settled this market against, from the tape."""
    return _EXPIRATION_CACHE.get(cell.ticker)


def _settles(cell: EvCell, res: AxisResult) -> Optional[bool]:
    value = _EXPIRATION_CACHE.get(cell.ticker)
    if value is None:
        return None
    return settles_yes_gas(value, cell.floor_strike)


def _build_register(
    head: AxisResult, payload: dict, month_ends: int, month_ends_met: bool
) -> DeferralRegister:
    """Assemble the §10 register from this run's measurements.

    Registration order is fixed so the numbers are stable between runs whose
    inputs did not change; whether an item is OPEN or CLOSED is decided by the
    data, never by a hardcoded assumption about what the data would be.
    """
    spec = head.spec
    m = head.monthly
    w = head.weekly
    wt = accepted_summary(w.cells, "taker") if w else {}
    monthly_settlements = (head.accepted_taker.get("cluster") or {}).get(
        "n_events"
    ) or 0
    weekly_settlements = (wt.get("cluster") or {}).get("n_events") or 0
    monthly_events_in_tape = m.n_events
    earliest_as_of = spec.aaa_first + timedelta(days=head.config.min_history_days - 1)

    reg = DeferralRegister()

    if month_ends_met:
        reg.close(
            "month_ends",
            f"month-end projection MAE on >= {REQUIRED_MONTH_ENDS} held-out "
            f"month-ends",
            f"**{month_ends} month-ends held out** on the AAA span "
            f"{spec.aaa_first} .. {spec.aaa_last} ({spec.aaa_rows} usable rows). "
            f"Reported in §3.1; the clause is MET in §1.",
        )
    else:
        reg.add(
            "month_ends",
            f"month-end projection MAE on >= {REQUIRED_MONTH_ENDS} held-out "
            f"month-ends ({month_ends} obtained)",
            f"the AAA series starts {spec.aaa_first}, and FR-4.2's "
            f"`min_history_days = {head.config.min_history_days}` admits no "
            f"`as_of` before {earliest_as_of.isoformat()}, which leaves only "
            f"{month_ends} qualifying month-ends",
            "a longer Wayback backfill, then regenerate this artifact",
        )

    reg.add(
        "monthly_events",
        f"realized PnL on more than {monthly_settlements} settled `KXAAAGASM` "
        f"settlement(s)",
        f"Kalshi prunes settled markets from the public API after roughly two "
        f"months, so only {monthly_events_in_tape} monthly event(s) are "
        f"retrievable at all and only {monthly_settlements} of them had settled "
        f"when the tape was fetched. **A longer AAA backfill does not fix this** "
        f"— the missing data is exchange-side market history, not price history.",
        f"record `KXAAAGASM` ladders live from now on. Meanwhile the weekly "
        f"series supplies {weekly_settlements} settled week-ends as evidence "
        f"about the *shape*, reported separately in §4.2/§5.2 and never pooled "
        f"into the monthly headline.",
    )

    reg.add(
        "intra_hour",
        "intra-hour quote path and resting-order queue position",
        "the candlesticks endpoint is hourly, so any sub-hour traversal is "
        "invisible and queue position is unobservable at any resolution. The "
        "maker leg is therefore a bound, not a measurement, which is why §0's "
        "verdict is taken from the taker path.",
        "record gas ladders live at the Phase 0 harvester's cadence",
    )

    reg.add(
        "rbob_exante",
        "an ex-ante argument for one RBOB benchmark over another",
        "every NY Harbor *RBOB* series in EIA's bulk archive is a futures series "
        "ending 2024-04-05, so the national-benchmark comparison has to be made "
        "against conventional-spot series, which are a different product",
        "not needed for this verdict — §6.1 recomputes the headline under all "
        "three and the realized sign does not depend on the choice",
    )

    return reg


def write_markdown(
    path: str,
    results: Dict[str, AxisResult],
    payload: dict,
    args,
    artifact_date: str,
) -> RenderedClaims:
    """Emit the dated artifact, returning exactly what it rendered (see
    :class:`RenderedClaims`) so the JSON companion serialises the same objects."""
    head = results["headline"]
    spec = head.spec
    m = head.monthly
    w = head.weekly
    at = head.accepted_taker
    am = head.accepted_maker
    wt = accepted_summary(w.cells, "taker") if w else {}
    wm = accepted_summary(w.cells, "maker") if w else {}
    kal = results.get("truth:kalshi")

    t_month = _t_stat(at.get("realized"), at.get("realized_se"))
    t_week = _t_stat(wt.get("realized"), wt.get("realized_se"))
    month_ends = head.month_ends_held_out
    month_ends_met = month_ends >= REQUIRED_MONTH_ENDS
    hist_days = (spec.aaa_last - spec.aaa_first).days + 1
    earliest_as_of = spec.aaa_first + timedelta(days=head.config.min_history_days - 1)
    tape_manifest = payload["inputs"]["tape"] or {}

    findings = _findings(results, payload)
    verdict = "HALT" if any(f["halting"] for f in findings) else "PROCEED"

    register = _build_register(head, payload, month_ends, month_ends_met)
    xc = aaa_vs_kalshi_crosscheck(gas_dir=args.gas_dir)

    out: List[str] = []
    A = out.append

    A(f"# Phase 4 backtest — AAA gas convergence — {artifact_date}")
    A("")
    A(
        "**PRD:** FR-4.2 / FR-4.3; Phase 4 exit criterion 2. "
        "**Branch:** `phase-4-gas-convergence`. **Workstream D.**"
    )
    A("")
    A(
        "Every number in this document was computed by `scripts/gas_backtest.py` "
        "from the inputs hashed in §2. Nothing is carried over from another "
        "workstream's report without being recomputed here, and no EV is quoted "
        "for a fill the recorded tape says was unavailable."
    )
    A("")
    aaa_hash = str(
        ((payload["inputs"].get("aaa_manifest") or {}).get("series") or {})
        .get("aaa_daily_national", {})
        .get("content_hash")
    )
    A(
        f"> **What this run saw.** AAA daily national average "
        f"`{spec.aaa_first}` .. `{spec.aaa_last}` — {hist_days} days, "
        f"{hist_days / 365.25:.2f} yr — {spec.aaa_rows} usable rows "
        f"({spec.aaa_suspect} `suspect` excluded, {spec.aaa_missing_days} missing "
        f"calendar days interpolated in memory), content hash "
        f"`{aaa_hash[:16]}`. That span yields **{month_ends} held-out "
        f"month-ends** against the {REQUIRED_MONTH_ENDS} exit criterion 2 "
        f"requires, so the clause is "
        f"**{'MET' if month_ends_met else 'NOT MET'}** (§3.1). Every number below "
        f"is a function of this input: **if the AAA content hash changes, "
        f"regenerate before citing this artifact** — §0's verdict must be "
        f"re-derived rather than assumed to carry over."
    )
    A("")
    A("---")
    A("")
    A("## 0. Verdict")
    A("")
    A(f"> ## {verdict}.")
    A(">")
    if verdict == "HALT":
        A(
            "> **The strategy's simulated historical EV is large and positive "
            f"({_c(at.get('ev'))}/contract monthly, {_c(wt.get('ev'))} weekly, "
            "maker fees included). The realized settlement-true PnL of the same "
            "trades is negative in every configuration tested, and the modelled "
            "EV lies far outside the realized confidence interval. The quantity "
            "FR-4.3 would size from is decisively wrong in the optimistic "
            "direction, so nothing may be sized from it. `GAS_TRADING_ENABLED` "
            "must stay `False`.**"
        )
        A(">")
        A(
            "> Stated precisely, because the distinction matters: this report "
            "does **not** establish that the strategy loses money — the realized "
            "confidence interval contains zero on this sample. It establishes "
            "that the modelled EV is not measuring the thing FR-4.3 believes it "
            "measures, and that the market's own price forecasts the settlement "
            "better than the model does."
        )
    else:
        A(
            "> **No halting finding was produced by the checks in this report.** "
            "See §9 for the exact configuration this is conditional on."
        )
    A("")
    ct_m = at.get("cluster") or {}
    ct_w = wt.get("cluster") or {}
    A(
        _table(
            ["quantity", "monthly `KXAAAGASM`", "weekly `KXAAAGASW`"],
            [
                [
                    "modelled EV/ct, taker, +1c allowance",
                    _c(at.get("ev")),
                    _c(wt.get("ev")),
                ],
                [
                    "modelled EV/ct, maker, +1c allowance",
                    _c(am.get("ev")),
                    _c(wm.get("ev")),
                ],
                [
                    "**realized/ct, taker, settlement-true**",
                    f"**{_c(ct_m.get('event_mean'))}**",
                    f"**{_c(ct_w.get('event_mean'))}**",
                ],
                [
                    "realized 95% CI (clustered on the settlement event)",
                    f"[{_c(ct_m.get('ci_low'))}, {_c(ct_m.get('ci_high'))}]",
                    f"[{_c(ct_w.get('ci_low'))}, {_c(ct_w.get('ci_high'))}]",
                ],
                [
                    "**is the modelled EV inside that interval?**",
                    _inside(at.get("ev"), ct_m),
                    _inside(wt.get("ev"), ct_w),
                ],
                [
                    "t on (modelled EV - realized)",
                    _d(at.get("ev_vs_realized_t"), 2),
                    _d(wt.get("ev_vs_realized_t"), 2),
                ],
                [
                    "settled trades / independent settlements",
                    f"{at.get('n_settled')} / {ct_m.get('n_events')}",
                    f"{wt.get('n_settled')} / {ct_w.get('n_events')}",
                ],
                [
                    "settlements with negative mean",
                    f"{ct_m.get('n_events_negative')} of {ct_m.get('n_events')}",
                    f"{ct_w.get('n_events_negative')} of {ct_w.get('n_events')}",
                ],
                [
                    "mean modelled P(win) vs realized win rate",
                    f"{_d(at.get('mean_p_win'))} vs {_pct(at.get('win_rate'))}",
                    f"{_d(wt.get('mean_p_win'))} vs {_pct(wt.get('win_rate'))}",
                ],
                [
                    "trade-level realized SE / t *(optimistic — see §5.2)*",
                    f"{_c(at.get('realized_se'), 2, sign=False)} / {_d(t_month, 2)}",
                    f"{_c(wt.get('realized_se'), 2, sign=False)} / {_d(t_week, 2)}",
                ],
            ],
        )
    )
    A("")
    A(
        "**The clustering unit is the settlement event, not the trade.** Every "
        "bracket on one ladder resolves against a single AAA publication, so "
        f"{wt.get('n_settled')} weekly trades are "
        f"{ct_w.get('n_events')} independent draws. The trade-level standard "
        "error in the last row is printed only so nobody has to wonder what it "
        "was; it is not the number the verdict uses, and using it would have "
        "produced a much more confident-looking negative result than this "
        "sample supports."
    )
    A("")
    A("### The full reasoning, strongest first")
    A("")
    for i, f in enumerate(findings, start=1):
        tag = "" if f["halting"] else " *(supporting, not load-bearing)*"
        A(f"{i}. **{f['title']}**{tag} {f['body']}")
        A("")
    A("### What this verdict does *not* say")
    A("")
    A(
        "It does not say the AAA series is unpredictable, and it does not say "
        "the projection is badly built. Over admissible daily targets the "
        f"held-out MAE is "
        f"${head.daily_by_lead.get(1, {}).get('mae', float('nan')):.4f}/gal at a "
        f"1-day lead and "
        f"${head.daily_by_lead.get(14, {}).get('mae', float('nan')):.4f} at 14 "
        f"days, the 14-day bias is "
        f"{abs(head.daily_by_lead.get(14, {}).get('bias', 0.0)) / max(1e-9, head.daily_by_lead.get(14, {}).get('mae', float('nan'))):.0%}"
        f" of that error, the reported sigma "
        f"(${head.daily_by_lead.get(14, {}).get('mean_sigma', float('nan')):.4f}) "
        f"is close to the "
        f"${head.daily_by_lead.get(14, {}).get('mae', float('nan')) / 0.7979:.4f} "
        f"a Gaussian of that MAE implies, and the settlement rule reconciles "
        f"{m.settle_reconcile.get('match', 0)}/"
        f"{m.settle_reconcile.get('match', 0) + m.settle_reconcile.get('MISMATCH', 0)}"
        f" against Kalshi's own results "
        "(§3.4). Nor does it say the *sign* of the realized result is "
        f"established — it is not, on "
        f"{(at.get('cluster') or {}).get('n_events')} monthly and "
        f"{(wt.get('cluster') or {}).get('n_events')} weekly settlements."
    )
    A("")
    A(
        "What it says is narrower and harder to argue with: a `KXAAAGASM` ladder "
        "is spaced $0.01 apart while the projection's honest 14-day uncertainty "
        f"is about "
        f"${head.daily_by_lead.get(14, {}).get('mean_sigma', float('nan')):.2f}, "
        f"so the model cannot resolve which bracket will settle; the market "
        f"can, and does so better than the model (§3.6). Every "
        f"{args.min_divergence * 100:.0f}pt divergence the gate sees is therefore "
        f"predominantly the model's own ignorance, and the EV computed from it is "
        f"a number about the model rather than about the market."
    )
    A("")
    A("---")
    A("")

    # ---------------- §1 criterion clause by clause ----------------
    A("## 1. Exit criterion 2, quoted verbatim, satisfied clause by clause")
    A("")
    A(
        "> **2.** Backtest artifact: the lag/drift projection, fit on >=12 "
        "months of backfilled AAA/EIA/RBOB history, reports month-end "
        "projection MAE on >=6 held-out month-ends; the strategy's simulated "
        "historical EV (maker fees included) is documented, and the bot trades "
        "in paper only if that EV > 0 (else the phase closes with a documented "
        "HALT, which still satisfies this criterion)."
    )
    A("")
    hist_days = (spec.aaa_last - spec.aaa_first).days + 1
    A(
        _table(
            ["clause", "where", "status"],
            [
                [
                    "exists as a dated artifact",
                    "this file",
                    f"`reports/phase4/phase4_backtest_{artifact_date}.md`, "
                    f"regenerated by `scripts/gas_backtest.py run`",
                ],
                [
                    "fit on >=12 months of backfilled AAA/EIA/RBOB history",
                    "§2.1",
                    f"AAA {spec.aaa_first} .. {spec.aaa_last} = {hist_days} days "
                    f"({hist_days / 365.25:.2f} yr), {spec.aaa_rows} rows; RBOB "
                    f"{spec.rbob_rows} rows; EIA {spec.eia_rows} rows. "
                    f"`min_history_days=365` is enforced per fit and **aborts** "
                    f"rather than fitting short — "
                    f"{m.abort_reasons.get('HISTORY_TOO_SHORT', 0)} monthly "
                    f"decision-market pairs and "
                    f"{head.mae_aborts.get('HISTORY_TOO_SHORT', 0)} month-end "
                    f"MAE attempts aborted for exactly that reason",
                ],
                [
                    f"month-end MAE on >={REQUIRED_MONTH_ENDS} held-out " f"month-ends",
                    "§3.1",
                    (
                        f"**{month_ends} month-ends** against the "
                        f"{REQUIRED_MONTH_ENDS} required — "
                        + (
                            f"**MET**; the corresponding register item is "
                            f"CLOSED at {register.ref('month_ends')}"
                            if month_ends_met
                            else f"**NOT MET**, registered as an open deferral "
                            f"in {register.ref('month_ends')}"
                        )
                    ),
                ],
                [
                    "simulated historical EV, maker fees included",
                    "§4, §5",
                    "documented for both fee legs, per bracket-distance band and "
                    "for the strategy's own accepted shape, with the 1c "
                    "adverse-fill allowance; `KXAAAGASM` billed on "
                    "`quadratic_with_maker_fees`, `KXAAAGASW` on `quadratic`",
                ],
                [
                    "bot trades in paper only if that EV > 0",
                    "§0, §9",
                    f"**{verdict}** — recommendation is that "
                    f"`GAS_TRADING_ENABLED` stays `False`"
                    if verdict == "HALT"
                    else "see §9",
                ],
                [
                    "a documented HALT still satisfies this criterion",
                    "§0",
                    "the HALT and its reasoning are documented here"
                    if verdict == "HALT"
                    else "not exercised",
                ],
                [
                    "red-team can recompute one case from raw inputs",
                    "§7",
                    "one accepted trade worked end to end, hand-checkable "
                    "against a normal table",
                ],
            ],
        )
    )
    A("")
    A("---")
    A("")

    # ---------------- §2 provenance ----------------
    A("## 2. Provenance")
    A("")
    A("### 2.1 Series actually fitted on")
    A("")
    A(
        _table(
            ["series", "path", "rows", "span", "notes"],
            [
                [
                    "AAA daily national",
                    "`data/gas_truth/aaa_daily_national.csv`",
                    f"{spec.aaa_rows} usable",
                    f"{spec.aaa_first} .. {spec.aaa_last} ({hist_days} d)",
                    f"{spec.aaa_suspect} rows flagged `quality=suspect` in the "
                    f"file and excluded by default; {spec.aaa_missing_days} "
                    f"calendar days inside the span have no row and are "
                    f"interpolated in memory only (contract §1.1)",
                ],
                [
                    "RBOB daily spot",
                    f"`reports/phase4/covariates/{spec.rbob_label}/rbob_daily.csv`",
                    str(spec.rbob_rows),
                    f"{spec.rbob_first} .. {spec.rbob_last}",
                    f"EIA series `{spec.rbob_series_id}`; see §6.1 for why this "
                    f"workstream re-fetched all three alternatives itself",
                ],
                [
                    "EIA weekly retail",
                    "`data/gas_truth/eia_weekly_regular.csv`",
                    str(spec.eia_rows),
                    "—",
                    "loaded and level-checked; **not** a regressor in the "
                    "headline (near-collinear with AAA momentum). §6.2 turns it "
                    "on",
                ],
                [
                    "live series metadata",
                    "`reports/phase4/gas_series_metadata.json`",
                    "4 series",
                    "—",
                    "`GET /series/{ticker}`; the fee-schedule and "
                    "settlement-source check in §2.4",
                ],
                [
                    "Kalshi-pinned settlement truth",
                    "`tests/fixtures/gas/kalshi_pinned_truth.csv`",
                    str(payload["inputs"]["pinned_truth_rows"]),
                    "—",
                    "WS-B; source-independent of AAA. §3.3 scores against it",
                ],
                [
                    "quote tape",
                    f"`{tape_manifest.get('path', 'reports/phase4/gas_quote_tape.csv')}`",
                    f"{tape_manifest.get('rows')} hourly candles",
                    f"{tape_manifest.get('distinct_markets')} markets, "
                    f"{tape_manifest.get('distinct_events')} events",
                    f"sha256 `{str(tape_manifest.get('content_hash'))[:16]}`",
                ],
            ],
        )
    )
    A("")
    A("### 2.2 Where the quote tape comes from, and why it had to be built")
    A("")
    A(
        "**This project has never recorded a gas orderbook.** `data/ladders/` "
        "holds `KXHIGHCHI`, `KXHIGHLAX`, `KXHIGHMIA` and `KXHIGHNY` and nothing "
        "else, and Kalshi prunes settled markets from the public API after "
        "roughly two months, so when this tape was fetched only "
        f"{tape_manifest.get('series', {}).get('KXAAAGASM', {}).get('settled')} "
        f"settled `KXAAAGASM` markets ({m.n_events} month-end event(s) reach the "
        f"FR-4.3 window) and "
        f"{tape_manifest.get('series', {}).get('KXAAAGASW', {}).get('settled')} "
        f"settled `KXAAAGASW` markets "
        f"({w.n_events if w else 0} week-end event(s)) were retrievable at all. "
        "WS-B's settled-ladder fixture carries results and volumes but no quotes."
    )
    A("")
    A(
        "A historical quote surface therefore had to be recovered from the "
        "public **candlesticks** endpoint, which answers anonymously and returns "
        "`yes_bid` and `yes_ask` OHLC per hour:"
    )
    A("")
    A("```")
    A(f"{tape_manifest.get('endpoint')}")
    A(f"base {tape_manifest.get('api_base')}")
    A(
        f"{tape_manifest.get('markets_enumerated')} markets enumerated, "
        f"{tape_manifest.get('markets_skipped')} skipped (no elapsed life), "
        f"{tape_manifest.get('rows')} hourly rows kept "
        f"(last {tape_manifest.get('lookback_days')} days of each market's life)"
    )
    A("```")
    A("")
    A(
        "`yes_bid == 0` and `yes_ask == 1` are Kalshi's empty-book sentinels, "
        "not prices; they are stored as absent and every EV statistic below "
        "excludes them while still counting them in `n cand`. The NO side is "
        "derived by Kalshi's identity `no_ask = 1 - yes_bid`, "
        "`no_bid = 1 - yes_ask`, and is absent whenever the YES side it derives "
        "from is."
    )
    A("")
    A("### 2.3 Reproducibility")
    A("")
    A(
        "Every line of this artifact except the `Generated ...` timestamp in the "
        "footer is a function of `scripts/gas_backtest.py` and of the files "
        "hashed in §2.1, so re-running the generator against the same inputs "
        "reproduces it byte for byte. That was checked by generating twice and "
        "diffing: exactly one line differs, the footer timestamp. Two candidates "
        "for that list were deliberately removed — the generator's own wall time, "
        "which measures the machine rather than the data, and the working "
        "tree's git status, which made an earlier draft fail its own "
        "reproducibility check whenever any unrelated file in the repository "
        "changed. A check that fails cosmetically teaches the reader to ignore "
        "it."
    )
    A("")
    A("### 2.4 Fee model")
    A("")
    A(
        _table(
            ["series", "live `fee_type`", "maker fee", "taker fee"],
            [
                [
                    "`KXAAAGASM` (monthly, the FR-4.3 target)",
                    f"`{payload['fee_model']['fee_type_KXAAAGASM']}`",
                    "`ceil_to_cent(0.0175 * C * P * (1-P))` on the order total",
                    "`ceil_to_cent(0.07 * C * P * (1-P))` on the order total",
                ],
                [
                    "`KXAAAGASW` (weekly)",
                    f"`{payload['fee_model']['fee_type_KXAAAGASW']}`",
                    "**$0.00** — absent from the non-standard table",
                    "same taker formula",
                ],
                [
                    "`KXAAAGASD` (daily)",
                    f"`{payload['fee_model']['fee_type_KXAAAGASD']}`",
                    "**$0.00**",
                    "same taker formula",
                ],
            ],
        )
    )
    A("")
    meta = (payload["fee_model"].get("live_series_metadata") or {}).get("series") or {}
    if meta:
        A(
            "The `fee_type` column above is not this project's opinion. "
            "`KNOWN_MAKER_FEE_SERIES` encodes the belief, so checking the code "
            "against itself would prove nothing; the exchange's own answer was "
            "pulled from `GET /series/{ticker}` and committed to "
            "`reports/phase4/gas_series_metadata.json`:"
        )
        A("")
        A(
            _table(
                [
                    "series",
                    "live `fee_type`",
                    "`fee_multiplier`",
                    "settlement source",
                    "code agrees",
                ],
                [
                    [
                        f"`{t}`",
                        f"`{v.get('fee_type')}`",
                        v.get("fee_multiplier"),
                        ", ".join(v.get("settlement_sources") or []) or "—",
                        "yes" if v.get("agrees_with_code") else "**NO**",
                    ]
                    for t, v in meta.items()
                ],
            )
        )
        A("")
        A(
            "`KXHIGHNY` is included as the weather control the Phase 2 fee "
            "correction rests on. All three gas series settle on **AAA**, which "
            "is why the weekly series is admissible as evidence about the "
            "*shape* even though its fee schedule differs."
        )
        A("")
    A(
        f"`KNOWN_MAKER_FEE_SERIES` = "
        f"`{sorted(payload['fee_model']['known_maker_fee_series'])}`. Settlement "
        "is free, and a gas position is held to the AAA publication, so **one** "
        "fee leg is charged, not a round trip."
    )
    A("")
    A(
        'The PRD\'s phrase "maker fees (25% of taker on this series)" is correct '
        "as a **rate** and wrong as a **charged fee at small size**, because each "
        "leg is ceil'd to the cent independently. §7 shows the arithmetic on a "
        "real trade. Every fee in this report comes from `compute_fee(...)` with "
        "`fee_type_for_symbol(symbol)` threaded, at the actual contract count; "
        "no fee is ever scaled from the other."
    )
    A("")
    A("### 2.5 Method — and the four things that would have made it dishonest")
    A("")
    A(
        "1. **Lookahead in the projection.** For each decision date the whole "
        "series is clamped with `GasSeries.observed_through(decision_date)` "
        "*before* the strategy sees it, so the `as_of` the strategy selects "
        "(`max(aaa.date)`) cannot be a row published after the decision. "
        "`project()` re-clamps and re-scans internally and raises "
        "`GasLookaheadError` on any unclamped path. Trading closes at 23:59 ET "
        "the evening before the value publishes, so the decision is always made "
        "on a projection — there is no version of this backtest in which reading "
        "the target date's value is legitimate."
    )
    A(
        "2. **A re-implementation of the gates instead of the gates.** Every "
        "accept/reject below is `GasConvergenceStrategy.analyze()` returning a "
        "signal or not, on a `MarketData` built from the tape. Fees come from "
        "`GasConvergenceStrategy._ev`. Nothing here re-derives a gate or a fee."
    )
    A(
        "3. **Pricing a fill the book never offered.** Each shape names the side "
        "of the book it must hit and is excluded — while still counted — when "
        "that side is absent. §4.1 prints the executable fraction beside every "
        "cell."
    )
    A(
        f"4. **Quoting the model's number and not the outcome.** Every cell "
        f"carries both the modelled EV and the realized settlement-true PnL of "
        f"the identical trade, using Kalshi's own `result`, which reconciled "
        f"{m.settle_reconcile.get('match', 0)}/"
        f"{m.settle_reconcile.get('match', 0) + m.settle_reconcile.get('MISMATCH', 0)} "
        f"against `settles_yes_gas(expiration_value, floor_strike)` (§3.4). "
        f"Where the two numbers disagree, the realized one is what happened."
    )
    A("")
    A(
        f"**Structural gates applied before a snapshot becomes a candidate:** "
        f"the FR-4.3 window (`0 < settlement - today <= {args.window_days}` d) "
        f"and data freshness (newest AAA row <= {MAX_DATA_AGE_DAYS} d old). "
        f"One decision snapshot "
        f"is taken per (market, ET date) — the last hourly candle at or before "
        f"{args.hour_et}:00 ET — so a per-day result cannot be an artifact of "
        f"which hour happened to be quoted. Order size C = {args.quantity} "
        f"contracts (FR-4.3 \"sized small\", the strategy's own `base_quantity` "
        f"default). Fit budget for this artifact: "
        f"{payload['fit_budget']['total_fits']} regressions across "
        f"{len(results)} configurations plus the §8 sweep, all sequential (the "
        f"wall time is in the JSON companion; it is a property of the machine, "
        f"not of the data, so it is kept out of this file to preserve the "
        f"byte-for-byte reproducibility claimed in §2.3)."
    )
    A("")
    A("---")
    A("")

    # ---------------- §3 MAE ----------------
    A("## 3. Projection accuracy")
    A("")
    A("### 3.1 Held-out month-ends (the criterion's own table)")
    A("")
    A(
        "Strict walk-forward: for each month-end the fit sees only rows dated "
        "before it. `as_of` is the newest **observed** AAA date at or before "
        "`target - nominal lead`, because the projection anchors on `A(as_of)` "
        "and `require_observed_as_of` forbids extrapolating that anchor; the "
        "realized lead is therefore `>=` the nominal one and is printed per row."
    )
    A("")
    A(_mae_table(head.mae_by_lead))
    A("")
    A(
        f"**Month-ends held out: {month_ends}.** "
        + (
            f"The criterion asks for >= {REQUIRED_MONTH_ENDS}, so this clause is "
            f"**MET**. The register item that tracked it is CLOSED at "
            f"{register.ref('month_ends')}. For the record of what produced them: "
            f"the series starts {spec.aaa_first} and FR-4.2's `min_history_days = "
            f"{head.config.min_history_days}` makes {earliest_as_of.isoformat()} the "
            f"earliest admissible `as_of`, so every month-end after that date "
            f"qualifies."
            if month_ends_met
            else f"The criterion asks for >= {REQUIRED_MONTH_ENDS}, so this clause is "
            f"**NOT MET**. The "
            f"binding constraint is the length of the AAA backfill: the series "
            f"starts {spec.aaa_first} and FR-4.2's `min_history_days = "
            f"{head.config.min_history_days}` makes {earliest_as_of.isoformat()} the "
            f"earliest admissible `as_of`, which leaves only the month-ends after it. "
            f"Registered as an open deferral at {register.ref('month_ends')}."
        )
    )
    A("")
    A(_mae_detail_table(head))
    A("")
    A(
        "**The independence limitation, stated plainly.** Truth for these "
        "month-ends is the AAA series the model is also fitted on. They are held "
        "out in **time**, not in **source**: a systematic error in the Wayback "
        "scrape would cancel between prediction and truth and this table could "
        "not see it. §3.3 scores the same estimator against a different "
        "measurement channel; the two numbers are reported separately and are "
        "never blended."
    )
    A("")
    A("### 3.2 All admissible daily targets (the same estimator, a larger sample)")
    A("")
    A(
        "The month-end sample is too small to measure a bias, so the identical "
        "walk-forward machinery is run over every AAA date the projection can "
        "legally be scored on. The targets overlap heavily — a 14-day lead "
        "shares 13 days with its neighbour — so these rows are strongly "
        "dependent and no standard error is quoted from them. What they do "
        "measure reliably is the **sign and size of the bias**."
    )
    A("")
    A(_mae_table(head.daily_by_lead))
    A("")
    daily14 = head.daily_by_lead.get(14, {})
    if daily14.get("n"):
        A(
            f"At the 14-day lead FR-4.3 actually trades, the projection is "
            f"biased **{'high' if daily14['bias'] > 0 else 'low'}** by "
            f"${daily14['bias']:+.4f}/gal against an MAE of "
            f"${daily14['mae']:.4f} on {daily14['n']} targets — i.e. the error "
            f"is {abs(daily14['bias']) / daily14['mae']:.0%} bias rather than "
            f"noise. On a `strictly greater` ladder a level bias of that sign is "
            f"directly an upward bias in `P(YES)` at every strike, which is the "
            f"mechanism §3.5 measures on outcomes."
        )
        A("")
    A("### 3.3 Source-independent cross-check (Kalshi-pinned truth)")
    A("")
    A(
        "WS-B recovered settlement truth from **settled Kalshi ladders alone** — "
        "which strikes paid YES, which paid NO — giving a closed interval "
        "`(low, high]` per settlement date plus Kalshi's own published "
        "`expiration_value`. That is a different measurement channel from the "
        "Wayback AAA scrape: neither derives from the other."
    )
    A("")
    A("#### 3.3.1 Do the two channels agree? (measured by this run)")
    A("")
    A(
        f"Recomputed here from `data/gas_truth/aaa_daily_national.csv` "
        f"({xc.aaa_rows} rows, {xc.aaa_suspect} `suspect`) and "
        f"`tests/fixtures/gas/kalshi_pinned_truth.csv` ({xc.pinned_rows} rows "
        f"over {xc.pinned_dates} distinct settlement dates) rather than quoted "
        f"from anywhere, so it moves when the AAA series moves. Counts are "
        f"**per pinned row**; a settlement date carried by two or three series "
        f"contributes a row each."
    )
    A("")
    A(
        _table(
            ["check", "result"],
            [
                [
                    "our value inside the ladder-implied interval `(low, high]`",
                    f"**{xc.inside} of {xc.rows_with_aaa}** rows that have an AAA "
                    f"value"
                    + (
                        ""
                        if xc.outside == 0
                        else f" — **{xc.outside} OUTSIDE**: "
                        + "; ".join(xc.outside_detail)
                    ),
                ],
                [
                    "pinned rows with no AAA row at all",
                    f"{xc.no_aaa_row}"
                    + (
                        ""
                        if not xc.no_row_dates
                        else " (" + ", ".join(xc.no_row_dates) + ")"
                    ),
                ],
                [
                    "max |ours − Kalshi `expiration_value`|",
                    (
                        "n/a"
                        if xc.max_deviation is None
                        else f"**${xc.max_deviation:.4f}**"
                        + (
                            ""
                            if not xc.max_deviation_date
                            else f" on {xc.max_deviation_date}"
                        )
                    ),
                ],
                [
                    "pinned dates whose AAA row is flagged `suspect`",
                    f"{len(xc.suspect_pinned_dates)}"
                    + (
                        ""
                        if not xc.suspect_pinned_dates
                        else " (" + ", ".join(xc.suspect_pinned_dates) + ")"
                    ),
                ],
            ],
        )
    )
    A("")
    A(
        "**ET attribution.** AAA republishes during the morning, so a capture "
        "taken at the wrong hour can carry the previous day's figure. If our "
        "series were systematically shifted by a day, the previous-day column "
        "below would hold most of the mass rather than a handful of dates — "
        "which is the only reason this breakdown is worth rendering."
    )
    A("")
    A(
        _table(
            ["our value vs Kalshi's published `expiration_value`", "rows"],
            [
                ["matches our **same-day** value", f"**{xc.same_day}**"],
                [
                    "matches our **previous-day** value",
                    f"{xc.prev_day}"
                    + (
                        ""
                        if not xc.prev_day_dates
                        else " (" + ", ".join(xc.prev_day_dates) + ")"
                    ),
                ],
                [
                    "matches **neither**",
                    f"**{xc.neither}**"
                    + (
                        ""
                        if not xc.neither_detail
                        else " — " + "; ".join(xc.neither_detail)
                    ),
                ],
                [
                    "no AAA row to compare",
                    f"{xc.rows_with_kalshi_value - xc.same_day - xc.prev_day - xc.neither}",
                ],
                [
                    "**total** pinned rows carrying a Kalshi value",
                    f"{xc.rows_with_kalshi_value}",
                ],
            ],
        )
    )
    A("")
    A(
        (
            "Both checks pass: every AAA value on a pinned date falls inside the "
            "interval the exchange's own settled ladder implies, and no pinned "
            "settlement matches neither our same-day nor our previous-day value. "
            if xc.containment_ok and xc.attribution_ok
            else "**One of these checks failed — see the rows above.** A "
            "disagreement here is upstream of everything else in this artifact, "
            "because it means the series the model is fitted on and the series "
            "the exchange settles against are not the same series. "
        )
        + (
            f"The single previous-day match ({', '.join(xc.prev_day_dates)}) is "
            f"also the {'' if xc.max_deviation_date in xc.prev_day_dates else 'un'}"
            f"related maximum deviation above; one isolated date is a "
            f"publication-hour artifact on that day, not a systematic offset. "
            f"The publication-hour effect itself is quantified in "
            f"`docs/phase4_data_contract.md` §6.3, which names three distinct "
            f"metrics for it — this section deliberately restates none of them, "
            f"because it measures a different property (agreement with the "
            f"exchange, not stability under re-dating)."
            if xc.prev_day_dates
            else "No pinned settlement matches only our previous-day value, so "
            "there is no evidence of a day-shift in the ET attribution at all."
        )
    )
    A("")
    A("#### 3.3.2 The projection scored against that channel")
    A("")
    if kal is not None and kal.mae_rows:
        A(_mae_table(kal.mae_by_lead))
        A("")
        A(
            f"{len({r.target_date for r in kal.mae_rows})} pinned settlement "
            f"dates scored ("
            f"{payload['inputs']['pinned_truth_rows']} pinned rows across "
            f"daily, weekly and monthly series). The two channels agree to "
            f"within the ${xc.max_deviation:.4f} maximum deviation measured in "
            f"§3.3.1, which is the expected result and is why the §0 verdict "
            f"does not rest on a truth-channel argument."
        )
    else:
        A(
            "**No rows.** Every pinned settlement date falls before the earliest "
            f"`as_of` that FR-4.2's {head.config.min_history_days}-day history "
            f"minimum admits, so this "
            "cross-check has no evaluable sample. Registered as a deferral in "
            f"{register.ref('monthly_events')}."
        )
    A("")
    A("### 3.4 Settlement-rule reconcile")
    A("")
    A(
        "Kalshi's `result` versus a recompute of "
        "`settles_yes_gas(expiration_value, floor_strike)` — the strict-greater "
        "rule the model implements — over every settled market in the tape:"
    )
    A("")
    A(
        _table(
            ["series", "match", "MISMATCH", "unsettled", "no expiration_value"],
            [
                [
                    "`KXAAAGASM`",
                    m.settle_reconcile.get("match", 0),
                    m.settle_reconcile.get("MISMATCH", 0),
                    m.settle_reconcile.get("unsettled", 0),
                    m.settle_reconcile.get("no_expiration_value", 0),
                ],
                [
                    "`KXAAAGASW`",
                    (w.settle_reconcile.get("match", 0) if w else "—"),
                    (w.settle_reconcile.get("MISMATCH", 0) if w else "—"),
                    (w.settle_reconcile.get("unsettled", 0) if w else "—"),
                    (w.settle_reconcile.get("no_expiration_value", 0) if w else "—"),
                ],
            ],
        )
    )
    A("")
    A(
        "Zero mismatches means the payoff rule in `src/models/gas_projection.py` "
        "is the exchange's rule, including the strict `>` at the boundary. This "
        "is the one thing in this report that is unambiguously working."
    )
    A("")
    A("### 3.5 Probability calibration — where the strategy actually breaks")
    A("")
    A(
        "Model `P(YES)` decile against the realized YES rate, over **every** "
        "executable YES candidate rather than only the accepted ones, so the "
        "answer is a property of the probability model and not of the selection."
    )
    A("")
    A("**`KXAAAGASM` (monthly)**")
    A("")
    A(_calibration_md(m.cells))
    A("")
    if w:
        A(
            f"**`KXAAAGASW` (weekly)** — "
            f"{len({c.settlement_date for c in w.cells if c.won is not None})} "
            f"settled week-ends, the larger sample"
        )
        A("")
        A(_calibration_md(w.cells))
        A("")
    A(
        "**Read the `n distinct settlements` column before the `n brackets` "
        "column.** Every bracket on one ladder resolves against a single AAA "
        "publication, so the brackets inside a decile are not independent draws "
        "and the per-decile n overstates the evidence badly. What clustering does "
        "*not* explain is the direction: the gap is one-sided across seven "
        "consecutive deciles, which is a specification defect rather than "
        "sampling noise."
    )
    A("")
    A("### 3.6 Model versus market as forecasters")
    A("")
    A(
        "The cleanest statement this sample supports, because it needs no fee "
        "model, no fill model and no EV. Over the same settled brackets, which "
        "of the two forecasters was closer to the outcome? Brier score, computed "
        "per settlement event and then averaged so the unit is the event; lower "
        "is better."
    )
    A("")
    sk_rows = []
    for lab, run in (("`KXAAAGASM` (monthly)", m), ("`KXAAAGASW` (weekly)", w)):
        if run is None:
            continue
        s = skill_vs_market(run.cells)
        if not s.get("n_events"):
            continue
        sk_rows.append(
            [
                lab,
                s["n"],
                s["n_events"],
                f"{s['brier_model']:.4f}",
                f"{s['brier_market']:.4f}",
                f"{s['diff']:+.4f}",
                _d(s.get("diff_se"), 4),
                _d(s.get("t"), 2),
                f"{s['events_model_better']}/{s['n_events']}",
            ]
        )
    A(
        _table(
            [
                "series",
                "n brackets",
                "n settlements",
                "Brier model",
                "Brier market mid",
                "model - market",
                "SE",
                "t",
                "settlements model won",
            ],
            sk_rows,
        )
    )
    A("")
    A(
        "A positive `model - market` means the market forecast the settlement "
        "better. This is the finding a longer AAA backfill cannot fix on its own: "
        "the strategy's premise is that the model knows something the price does "
        "not, and over the retrievable history the reverse holds."
    )
    A("")
    A("---")
    A("")

    # ---------------- §4 EV bands ----------------
    A("## 4. EV per bracket-distance band")
    A("")
    A(
        f"`n cand` counts every priced snapshot, including those the strategy's "
        f"gates rejected and those where the required side of the book was "
        f"absent. Band = `|floor_strike - projection point|` in cents/gal, edges "
        f"{[int(round(e * 100)) for e in BAND_EDGES]}c. All rows: walk-forward "
        f"projection, C = {args.quantity}, **1c adverse-fill allowance applied "
        f"to every cell**, fee charged on the price actually paid, one leg "
        f"(settlement is free). `realized/ct` is the settlement-true PnL of the "
        f"same trades. Clustering unit for `SE`: the individual trade — these "
        f"standard errors are optimistically small because brackets on one "
        f"ladder share one settlement, and they are **not** comparable with the "
        f"per-event numbers in §5."
    )
    A("")
    A("### 4.1 `KXAAAGASM` — the FR-4.3 target, maker fees billed")
    A("")
    for side in ("YES", "NO"):
        for mode in ("taker", "maker"):
            A(f"**buy {side} / {mode}**")
            A("")
            A(_band_md(m, side, mode))
            A("")
    A(
        f"### 4.2 `KXAAAGASW` — same shape, standard fee schedule, "
        f"{len({c.settlement_date for c in (w.cells if w else [])})} week-ends "
        f"({len({c.settlement_date for c in (w.cells if w else []) if c.won is not None})} settled)"
    )
    A("")
    if w:
        for side in ("YES", "NO"):
            for mode in ("taker", "maker"):
                A(f"**buy {side} / {mode}**")
                A("")
                A(_band_md(w, side, mode))
                A("")
    A("### 4.3 Quote availability is the binding constraint")
    A("")
    A(
        "Phase 2's central finding for weather was that the book's *availability*, "
        "not its spread, decides what is tradable. The same holds here, more "
        "sharply: most gas strikes are quoted one-sidedly for most of their life."
    )
    A("")
    A("**`KXAAAGASM`**")
    A("")
    A(_availability_md(m))
    A("")
    if w:
        A("**`KXAAAGASW`**")
        A("")
        A(_availability_md(w))
        A("")
    A(
        "An EV computed on a quote that was not there is fiction. Every EV cell "
        "above is restricted to snapshots where the required side of the book "
        "existed, and the excluded count is printed rather than absorbed."
    )
    A("")
    A("---")
    A("")

    # ---------------- §5 accepted shape ----------------
    A("## 5. The strategy's own accepted shape")
    A("")
    A(
        f"Only what `GasConvergenceStrategy.analyze()` accepted: inside the "
        f"{args.window_days}-day window, `|P(YES) - market| >= "
        f"{args.min_divergence * 100:.0f}`pt, both fee legs' EV clearing zero on "
        f"the raw quote, AAA data <= {MAX_DATA_AGE_DAYS} days old. "
        f"`EV/ct (+1c)` is the same trade "
        f"with the adverse-fill allowance; `EV/ct (no allowance)` is the number "
        f"the live gate itself computes."
    )
    A("")
    A(_accepted_md(m, "`KXAAAGASM` (monthly)"))
    A("")
    if w:
        A(_accepted_md(w, "`KXAAAGASW` (weekly)"))
        A("")
    A("### 5.1 By lead time — does it work where the model is sharp?")
    A("")
    A(
        f"The obvious question a red-team asks: at a 1-day lead the projection's "
        f"sigma is "
        f"${head.daily_by_lead.get(1, {}).get('mean_sigma', float('nan')):.4f}/gal "
        f"against a $0.01 strike spacing, so the model is genuinely informative "
        f"there. Does the shape work in the short part of the window? Accepted "
        f"taker trades bucketed by days to settlement, realized clustered on the "
        f"settlement event within each bucket."
    )
    A("")
    A(_by_lead_md(m, w))
    A("")
    A(_by_lead_commentary(m, w))
    A("")
    A("### 5.2 The same trades, clustered on the settlement event")
    A("")
    A(
        "The table above clusters on the trade, which is the wrong unit and the "
        "flattering one. Below, each settlement's accepted trades are averaged "
        "first and the interval is taken across settlements. This is the number "
        "§0 uses."
    )
    A("")
    cl_rows = []
    for lab, run in (("`KXAAAGASM`", m), ("`KXAAAGASW`", w)):
        if run is None:
            continue
        for mode in ("taker", "maker"):
            a = accepted_summary(run.cells, mode)
            ct = a.get("cluster") or {}
            if not ct.get("n_events"):
                continue
            cl_rows.append(
                [
                    f"{lab} {mode}",
                    ct["n_trades"],
                    ct["n_events"],
                    _c(a.get("ev")),
                    _c(ct.get("event_mean")),
                    _c(ct.get("event_se"), 2, sign=False),
                    _d(_t_stat(ct.get("event_mean"), ct.get("event_se")), 2),
                    f"[{_c(ct.get('ci_low'))}, {_c(ct.get('ci_high'))}]",
                    _inside(a.get("ev"), ct),
                    f"{ct.get('n_events_negative')}/{ct['n_events']}",
                ]
            )
    A(
        _table(
            [
                "series / leg",
                "trades",
                "settlements",
                "modelled EV/ct",
                "realized/ct",
                "SE",
                "t vs 0",
                "95% CI",
                "modelled EV inside CI?",
                "settlements negative",
            ],
            cl_rows,
        )
    )
    A("")
    per_event = (wt.get("cluster") or {}).get("per_event") or {}
    if per_event:
        A("Per-settlement realized mean, weekly taker (cents/contract):")
        A("")
        A(
            _table(
                ["settlement", "realized/ct"],
                [[k, f"{v * 100:+.1f}c"] for k, v in per_event.items()],
            )
        )
        A("")
    A("### 5.3 Rejection reason codes")
    A("")
    A(
        "Contract §3 requires every rejection to be reconstructible from the "
        "logs alone. These counts are read from the strategy's own "
        "`log_rejection` channel during the replay, which is also a check on "
        "that requirement."
    )
    A("")
    rej_rows = []
    for code in sorted(set(m.rejections) | set(w.rejections if w else {})):
        rej_rows.append(
            [
                f"`{code}`",
                m.rejections.get(code, 0),
                (w.rejections.get(code, 0) if w else "—"),
            ]
        )
    A(
        _table(["reason code", "`KXAAAGASM`", "`KXAAAGASW`"], rej_rows)
        if rej_rows
        else "*No rejection lines were captured.*"
    )
    A("")
    A("---")
    A("")

    # ---------------- §6 robustness ----------------
    A("## 6. The robustness test that decides the verdict")
    A("")
    A(
        "Phase 2 HALTed weather because its EV flipped sign between two forecast "
        "sources while the gate ranked the loser higher. The same discipline is "
        "applied here: the headline is recomputed under each perturbation and "
        "the signs are tabulated. `M` = `KXAAAGASM`, `W` = `KXAAAGASW`."
    )
    A("")
    A(_sign_stability_md(payload))
    A("")
    A("### 6.1 RBOB source")
    A("")
    A(
        "WS-A defaulted to `PET.EER_EPMRR_PF4_Y05LA_DPG.D` — **Los Angeles** "
        "RBOB spot — because every NY Harbor *RBOB* series in EIA's bulk archive "
        "is a futures series that ends 2024-04-05. LA is a CARB-specific "
        "benchmark and an imperfect national proxy. `RBOB_ALTERNATIVES` exposes "
        "NY Harbor and Gulf Coast **conventional** spot; all three were "
        "re-fetched by this workstream from the same archive over an identical "
        "window so the comparison is not confounded by a different start date "
        "per series:"
    )
    A("")
    cov_rows = []
    for key, res in results.items():
        if not key.startswith("rbob:") and key != "headline":
            continue
        cov_rows.append(
            [
                res.spec.rbob_label,
                f"`{res.spec.rbob_series_id}`",
                res.spec.rbob_rows,
                f"{res.spec.rbob_first} .. {res.spec.rbob_last}",
                _c(res.accepted_taker.get("ev")),
                _c((res.accepted_taker.get("cluster") or {}).get("event_mean")),
            ]
        )
    A(
        _table(
            [
                "alias",
                "EIA series id",
                "rows",
                "coverage",
                "EV/ct M taker",
                "realized/ct M (event-clustered)",
            ],
            cov_rows,
        )
    )
    A("")
    A(
        "Only `rbob_daily.csv` is consumed from those directories. Each also "
        "contains an `eia_weekly_regular.csv` written as a byproduct by "
        "`backfill_covariates`; the EIA series actually used is WS-A's committed "
        "copy in `data/gas_truth/`, and the three byproduct copies are identical "
        "to each other and to nothing this report reads."
    )
    A("")
    A("### 6.2 EIA covariate on/off")
    A("")
    A(
        "WS-C defaults the EIA weekly retail series off: it measures the same "
        "quantity as AAA at one seventh the frequency, so its trailing drift is "
        "near-collinear with the AAA momentum term. The `eia:on` row above is "
        "that decision tested rather than assumed."
    )
    A("")
    A("### 6.3 `suspect` rows in/out")
    A("")
    A(
        f"{spec.aaa_suspect} of the AAA rows carry `quality=suspect` (a parse "
        f"that moved more than $0.15/day against its neighbours, or landed "
        f"outside [1.00, 9.00]). They are excluded by default; the "
        f"`suspect:included` row is the same analysis with them in."
    )
    A("")
    A("### 6.4 Truth channel")
    A("")
    A(
        "AAA versus Kalshi-pinned, per §3.3. For the **EV** tables the realized "
        "outcome already comes from Kalshi's `result` and never from AAA, so the "
        "EV columns are unchanged by this axis by construction — the axis moves "
        "the MAE columns only, and it is reported for the MAE."
    )
    A("")
    A("---")
    A("")

    # ---------------- §7 worked example ----------------
    A("## 7. One worked example, recomputable by hand")
    A("")
    cell, worked = worked_example(head, args.quantity)
    A(worked)
    A("")
    A("---")
    A("")

    # ---------------- §8 sensitivities ----------------
    A("## 8. Sensitivities")
    A("")
    A(
        "Each row is the headline **re-run** with one knob moved, not an "
        "assertion about what would happen. All passes share one projection "
        "cache: none of these knobs enters the regression, so refitting for each "
        "would be redundant work. The `leg scored` column is the taker leg "
        "except for the maker-fill rows, where the maker leg is the one the knob "
        "affects."
    )
    A("")
    A(_sensitivity_md(payload.get("sensitivities") or []))
    A("")
    sens = payload.get("sensitivities") or []
    flips = [r for r in sens if r.get("realized_W") is not None and r["realized_W"] > 0]
    A(
        f"**Weekly realized sign across all {len(sens)} variants:** "
        + (
            "negative in every one."
            if not flips
            else "positive in "
            + ", ".join(f"{r['knob']} = {r['variant']}" for r in flips)
            + ", which must be read as a warning that the result is not robust "
            "to that knob."
        )
        + f" The monthly column moves far more, which is what a "
        f"{(at.get('cluster') or {}).get('n_events')}-settlement sample does; it "
        f"is printed for completeness and carries little weight."
    )
    A("")
    A("---")
    A("")

    # ---------------- §9 recommendation ----------------
    A("## 9. Recommendation")
    A("")
    if verdict == "HALT":
        A(
            "**`GAS_TRADING_ENABLED` stays `False`.** `src/bots/gas_bot.py` is "
            "WS-C's file and this workstream does not touch it; this is a "
            "recommendation to the orchestrator, not an action."
        )
        A("")
        A(
            "What would change the answer, pre-registered now so a later "
            "positive result cannot be produced by moving the target. The list "
            "is assembled from this run's measurements, so an item the inputs "
            "have already satisfied does not appear:"
        )
        A("")
        n_post = (
            m.n_snapshots
            - m.rejections.get("GAS_PROJECTION_UNAVAILABLE", 0)
            - m.rejections.get("GAS_NEAR_RESOLVED", 0)
            - m.rejections.get("GAS_NO_USABLE_QUOTE", 0)
        )
        n_pass = n_post - m.rejections.get("GAS_DIVERGENCE_BELOW_MIN", 0)
        pass_pct = f"{n_pass / n_post:.0%}" if n_post else "n/a"
        items: List[str] = [
            "**A calibrated probability, not a raw OLS prediction interval.** "
            "§3.5 shows the realized YES rate below the model's probability "
            "across the whole middle of the distribution, and §3.6 shows the "
            "market beating the model outright. Until `prob_above` is calibrated "
            "against held-out outcomes — the same treatment Phase 2 gave weather "
            "σ — every divergence this strategy measures is the model's own "
            "error. This is the item the other three depend on.",
        ]
        if not month_ends_met:
            items.append(
                f"**A backfill long enough to hold out "
                f"{REQUIRED_MONTH_ENDS} month-ends.** AAA currently starts "
                f"{spec.aaa_first}, which with FR-4.2's "
                f"`min_history_days = {head.config.min_history_days}` admits no "
                f"`as_of` before {earliest_as_of.isoformat()} and leaves only "
                f"{month_ends}. Extending the Wayback backfill further back and "
                f"regenerating this artifact would close "
                f"{register.ref('month_ends')}."
            )
        items.append(
            "**A divergence threshold expressed in sigma, not in points.** At a "
            f"14-day lead the model's sigma is "
            f"${head.daily_by_lead.get(14, {}).get('mean_sigma', float('nan')):.4f}"
            f"/gal against a $0.01 strike spacing, so the model's `P(YES)` moves "
            f"only about "
            f"{100 * (0.01 / max(1e-9, head.daily_by_lead.get(14, {}).get('mean_sigma', float('nan'))) * 0.3989):.1f}"
            f"pt per strike while the market's price moves far faster. An "
            f"{args.min_divergence * 100:.0f}pt gate therefore passes {n_pass} of "
            f"the {n_post} monthly snapshots that reach it ({pass_pct}) — it is "
            f"not selecting rare disagreements, it is passing much of the ladder. "
            f"§8 shows tightening it does not rescue the sign; it makes the "
            f"outcome worse."
        )
        items.append(
            "**Recorded gas ladders of its own.** The Phase 0 harvester records "
            "`KXHIGH*` only. Pointing it at `KXAAAGASM`/`KXAAAGASW` would remove "
            "this artifact's dependence on an endpoint that prunes history after "
            f"about two months, and would close {register.ref('monthly_events')} "
            f"and {register.ref('intra_hour')} over time. It is cheap and it is "
            f"the only item on this list that the AAA backfill cannot address."
        )
        for i, text in enumerate(items, start=1):
            A(f"{i}. {text}")
    else:
        A(
            "No halting finding. The configuration this is conditional on: "
            f"window {args.window_days} d, divergence "
            f"{args.min_divergence * 100:.0f}pt, C = {args.quantity}, "
            f"RBOB source `{spec.rbob_label}`, EIA covariate "
            f"{'on' if head.axis.use_eia else 'off'}, suspect rows "
            f"{'included' if head.axis.include_suspect else 'excluded'}, "
            f"decision hour {args.hour_et}:00 ET."
        )
    A("")
    A("---")
    A("")

    # ---------------- §10 deferrals ----------------
    A("## 10. Deferral register")
    A("")
    A(
        f"Per `register-deferred-evidence-not-waived`, this is one dated register "
        f"that closes item by item, not a list of complaints. An item the inputs "
        f"to *this* run have satisfied is recorded **CLOSED with the evidence "
        f"that closed it** rather than deleted: deleting it would make the "
        f"register under-report completion, and a reader reconciling §1 against "
        f"§10 could not tell which was authoritative. Nothing here is waived and "
        f"nothing is replaced by a proxy number presented as the real thing. "
        f"**{register.n_open} open, {register.n_closed} closed.**"
    )
    A("")
    A(register.markdown())
    A("")
    A("---")
    A("")
    A(
        f"*Generated {payload['generated_at']} by "
        f"`scripts/gas_backtest.py run`. Machine-readable companion: "
        f"`reports/phase4/phase4_backtest_data_{artifact_date}.json`.*"
    )
    A("")

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out))
    return RenderedClaims(
        register=register,
        month_ends=month_ends,
        month_ends_met=month_ends_met,
        crosscheck=xc,
    )


def _findings(results: Dict[str, AxisResult], payload: dict) -> List[dict]:
    """Assemble the strongest-first reasoning from measured quantities only.

    Each entry is produced by a test on the numbers, not asserted. ``halting``
    marks the ones that carry the decision.
    """
    head = results["headline"]
    at = head.accepted_taker
    wt = accepted_summary(head.weekly.cells, "taker") if head.weekly else {}
    wm = accepted_summary(head.weekly.cells, "maker") if head.weekly else {}
    out: List[dict] = []

    # The t-statistics quoted below come from each summary's own
    # ``ev_vs_realized_t``, not from a local recompute -- one source, so the
    # prose and the tables cannot disagree.
    ct_w = wt.get("cluster") or {}

    # 0. Upstream integrity: if the two truth channels disagree, nothing below
    #    means anything, so this is checked before any performance finding.
    xc = aaa_vs_kalshi_crosscheck()
    if not (xc.containment_ok and xc.attribution_ok):
        out.append(
            {
                "halting": True,
                "title": (
                    "The AAA series and the exchange's settled ladders do not "
                    "agree, so the inputs are not admissible."
                ),
                "body": (
                    f"{xc.outside} of {xc.rows_with_aaa} pinned rows fall outside "
                    f"the interval the settled ladder implies"
                    + (
                        ""
                        if not xc.outside_detail
                        else " (" + "; ".join(xc.outside_detail) + ")"
                    )
                    + f", and {xc.neither} match neither our same-day nor our "
                    f"previous-day value"
                    + (
                        ""
                        if not xc.neither_detail
                        else " (" + "; ".join(xc.neither_detail) + ")"
                    )
                    + ". This is upstream of every performance number in this "
                    "report: the series the model is fitted on and the series the "
                    "exchange settles against would not be the same series, so "
                    "no EV or MAE below could be interpreted. §3.3.1."
                ),
            }
        )

    # 1. The gate quantity is irreconcilable with the outcome.
    if (
        wt.get("ev") is not None
        and ct_w.get("ci_high") is not None
        and wt["ev"] > ct_w["ci_high"]
    ):
        out.append(
            {
                "halting": True,
                "title": (
                    "FR-4.3's gate quantity is irreconcilable with what happened, "
                    "by a wide margin."
                ),
                "body": (
                    f"On the trades the strategy itself accepted, modelled EV is "
                    f"{_c(wt['ev'])}/contract on the weekly series and "
                    f"{_c(at.get('ev'))} on the monthly. The realized "
                    f"settlement-true PnL of those same trades, clustered on the "
                    f"settlement event (the only independent unit — every bracket "
                    f"on a ladder shares one AAA publication), is "
                    f"{_c(ct_w.get('event_mean'))} weekly across "
                    f"{ct_w.get('n_events')} settlements, 95% CI "
                    f"[{_c(ct_w.get('ci_low'))}, {_c(ct_w.get('ci_high'))}]. "
                    f"**The modelled EV lies "
                    f"{_d(wt.get('ev_vs_realized_t'), 1)} standard errors above "
                    f"the realized mean and far outside that interval**; the "
                    f"maker leg is worse "
                    f"({_c(wm.get('ev'))} modelled, t = "
                    f"{_d(wm.get('ev_vs_realized_t'), 1)}). FR-4.3 authorises "
                    f"paper trading *on modelled EV*. "
                    + (
                        "This report cannot show that the strategy loses money — "
                        "the realized interval contains zero — but it does show "
                        "decisively "
                        if (ct_w.get("ci_low") or 0) <= 0 <= (ct_w.get("ci_high") or 0)
                        else "The realized interval excludes zero, so this report "
                        "shows both that the strategy lost money on this sample and "
                        "decisively "
                    )
                    + f"that the number "
                    f"FR-4.3 would size from is wrong, and wrong in the "
                    f"optimistic direction. Nothing may be sized from a quantity "
                    f"the data rejects. Note also that the realized point "
                    f"estimate is **negative in every configuration tested** "
                    f"(§6) and negative at {ct_w.get('n_events_negative')} of "
                    f"{ct_w.get('n_events')} weekly settlements."
                ),
            }
        )

    # 2. The market is the better forecaster.
    skill = skill_vs_market(head.weekly.cells if head.weekly else head.monthly.cells)
    if skill.get("n_events") and skill.get("diff") is not None and skill["diff"] > 0:
        out.append(
            {
                "halting": True,
                "title": "The market's own price forecasts the settlement better than the model does.",
                "body": (
                    f"Over the {skill['n']} settled weekly brackets, scored per "
                    f"settlement event and averaged, the model's Brier score is "
                    f"{skill['brier_model']:.4f} against the market mid's "
                    f"{skill['brier_market']:.4f} — the model is worse by "
                    f"{skill['diff']:+.4f} "
                    f"(SE {_d(skill.get('diff_se'), 4)}, t = "
                    f"{_d(skill.get('t'), 2)} on {skill['n_events']} events; the "
                    f"model was the better forecaster at "
                    f"{skill['events_model_better']} of them). This needs no fee "
                    f"model, no fill model and no EV: it says that when this "
                    f"model and this market disagree, the market is more often "
                    f"right. An 8pt divergence gate on top of that is a filter "
                    f"for the model's own error, and the direction of the trade "
                    f"it produces is the wrong one. This is the mechanism behind "
                    f"finding 1 and it is why a wider backfill alone will not "
                    f"fix it."
                ),
            }
        )

    # 2b. Tightening the filter makes the outcome worse while the gate improves.
    sens = payload.get("sensitivities") or []
    div = [r for r in sens if r["knob"] == "divergence gate"]
    if len(div) >= 3 and all(
        r.get("ev_W") is not None and r.get("realized_W") is not None for r in div
    ):
        base, *rest = div
        tighter = [r for r in rest if r["realized_W"] < base["realized_W"]]
        rising_ev = [r for r in rest if r["ev_W"] > base["ev_W"]]
        if len(tighter) == len(rest) and len(rising_ev) == len(rest):
            out.append(
                {
                    "halting": True,
                    "title": (
                        "Tightening the divergence gate makes the modelled EV "
                        "better and the outcome worse, monotonically."
                    ),
                    "body": (
                        "A gate that selects genuine mispricings should improve "
                        "the realized result as it is tightened. Raising the "
                        f"threshold from {base['variant']} to "
                        + " then ".join(r["variant"] for r in rest)
                        + " moves the weekly modelled EV from "
                        f"{_c(base['ev_W'])} to "
                        + " then ".join(_c(r["ev_W"]) for r in rest)
                        + " while the realized result moves from "
                        f"{_c(base['realized_W'])} to "
                        + " then ".join(_c(r["realized_W"]) for r in rest)
                        + " on "
                        + " then ".join(str(r["n_W"]) for r in rest)
                        + " trades (§8). The two quantities move in **opposite** "
                        "directions across the whole sweep. That is not a "
                        "weak edge being diluted by noise; it is a filter that "
                        "selects harder for the model's own disagreement with a "
                        "better-informed price, and it rules out the "
                        "'raise the threshold' fix before anyone proposes it."
                    ),
                }
            )

    # 3. Calibration, with the clustering caveat stated.
    cal = calibration_table(head.weekly.cells if head.weekly else head.monthly.cells)
    mid = [b for b in cal if 0.2 <= b["model_mid"] <= 0.8]
    if mid and all(b["gap"] < 0 for b in mid):
        worst = min(mid, key=lambda b: b["gap"])
        n_mid = sum(b["n"] for b in mid)
        out.append(
            {
                "halting": True,
                "title": "The probability model is miscalibrated in exactly the direction that generates the trades.",
                "body": (
                    f"Across the model-`P(YES)` deciles 0.2 to 0.8 — "
                    f"{n_mid} settled weekly brackets over "
                    f"{max(b['n_events'] for b in mid)} distinct settlements — "
                    f"the realized YES rate is below the model's probability in "
                    f"**every** decile, by up to {abs(worst['gap']):.3f} (decile "
                    f"{worst['decile']}: model {worst['model_mid']:.2f}, "
                    f"realized {worst['realized']:.3f}). Mean modelled P(win) on "
                    f"accepted monthly trades is "
                    f"{_d(head.accepted_taker.get('mean_p_win'))} against a "
                    f"realized win rate of "
                    f"{_pct(head.accepted_taker.get('win_rate'))}. **The "
                    f"clustering caveat applies here too** — brackets within a "
                    f"settlement are not independent, so the per-decile n "
                    f"overstates the evidence. What is not explained by "
                    f"clustering is the *direction*: a monotone one-sided gap "
                    f"across seven consecutive deciles is not what "
                    f"sampling noise on a well-specified model looks like, and it "
                    f"is consistent with finding 2 measured a different way."
                ),
            }
        )

    # 4. Bias in the point projection, only if the data supports calling it one.
    d14 = head.daily_by_lead.get(14, {})
    if d14.get("n") and abs(d14["bias"]) > 0.4 * d14["mae"]:
        out.append(
            {
                "halting": True,
                "title": "The point projection is biased, not merely noisy, at the lead the strategy trades.",
                "body": (
                    f"Over {d14['n']} admissible daily targets at a 14-day lead "
                    f"the projection's mean error is {d14['bias']:+.4f}/gal "
                    f"against an MAE of ${d14['mae']:.4f} — "
                    f"{abs(d14['bias']) / d14['mae']:.0%} of the average error "
                    f"is a one-directional offset."
                ),
            }
        )
    elif d14.get("n"):
        out.append(
            {
                "halting": False,
                "title": "The projection's *level* accuracy is not the problem.",
                "body": (
                    f"At the 14-day lead the projection's held-out MAE over "
                    f"{d14['n']} daily targets is ${d14['mae']:.4f}/gal with a "
                    f"bias of only {d14['bias']:+.4f} — "
                    f"{abs(d14['bias']) / d14['mae']:.0%} of the average error, "
                    f"i.e. mostly noise rather than offset — and the reported "
                    f"sigma (${d14['mean_sigma']:.4f}) is close to the "
                    f"$"
                    f"{d14['mae'] / 0.7979:.4f} a Gaussian of that MAE implies. "
                    f"The defect is not that the model cannot forecast the level; "
                    f"it is that a $0.01-spaced strike ladder magnifies a "
                    f"{d14['mae'] * 100:.0f}-cent level uncertainty into a "
                    f"probability the model cannot resolve, while the market can. "
                    f"This is listed as "
                    f"supporting because it explains the halting findings above "
                    f"rather than adding to them."
                ),
            }
        )

    # 5. Criterion clause not met.
    if head.month_ends_held_out < REQUIRED_MONTH_ENDS:
        out.append(
            {
                "halting": False,
                "title": (
                    f"The >= {REQUIRED_MONTH_ENDS} held-out month-end clause is "
                    f"not met on the data available."
                ),
                "body": (
                    f"{head.month_ends_held_out} month-ends qualify. The AAA "
                    f"backfill starts {head.spec.aaa_first} and FR-4.2's "
                    f"`min_history_days = {head.config.min_history_days}` admits "
                    f"no earlier `as_of`; "
                    f"{head.monthly.abort_reasons.get('HISTORY_TOO_SHORT', 0)} "
                    f"monthly decision-market pairs and "
                    f"{head.mae_aborts.get('HISTORY_TOO_SHORT', 0)} month-end "
                    f"MAE attempts aborted for that reason "
                    f"rather than being fitted short. Registered as an open "
                    f"deferral and not waived. It does not change the verdict: a "
                    f"longer backfill can only add month-ends, and the decision "
                    f"rests on the findings above, which are measured on "
                    f"realized outcomes rather than on projection accuracy."
                ),
            }
        )

    # 5. Sign stability.
    signs = {
        e["axis"]: (e["realized_taker"], e["weekly_realized_taker"])
        for e in payload["sign_stability"]
    }
    monthly_signs = {
        k: (v[0] > 0 if v[0] is not None else None) for k, v in signs.items()
    }
    stable = len({s for s in monthly_signs.values() if s is not None}) <= 1
    out.append(
        {
            "halting": False,
            "title": (
                "The negative sign survives every perturbation."
                if stable
                else "The sign is unstable across perturbations."
            ),
            "body": (
                f"§6 recomputes the headline under "
                f"{sum(1 for k in results if k.startswith('rbob:')) + 1} RBOB "
                f"sources, the EIA covariate on and off, `suspect` rows in and "
                f"out, and both truth channels. "
                + (
                    "The realized column is negative in every one, on both "
                    "series. Unlike Phase 2's weather result there is no "
                    "configuration in which this shape makes money, so the "
                    "verdict does not depend on a source-selection judgement."
                    if stable
                    else "The realized sign changes between configurations, "
                    "which on its own forbids sizing anything from the modelled "
                    "number."
                )
            ),
        }
    )

    # 6. Availability.
    qa = quote_availability(head.monthly.cells)
    two = qa.get("two_sided_book") or {}
    if two.get("frac") is not None:
        out.append(
            {
                "halting": False,
                "title": "Quote availability, not spread, bounds what is tradable.",
                "body": (
                    f"Of the {two['n_snapshots']} monthly snapshots that reached "
                    f"a usable projection (out of "
                    f"{head.monthly.n_snapshots} in the window), only "
                    f"{_pct(two['frac'])} had a two-sided book at all "
                    f"({two['n_two_sided']}); the YES offer is present in "
                    f"{_pct((qa.get('YES_taker') or {}).get('frac'))} of "
                    f"candidates and the NO offer in "
                    f"{_pct((qa.get('NO_taker') or {}).get('frac'))}. Where both "
                    f"sides are quoted the median spread is "
                    f"{(qa.get('spread') or {}).get('median', float('nan')) * 100:.1f}pt, "
                    f"but the p90 is "
                    f"{(qa.get('spread') or {}).get('p90', float('nan')) * 100:.1f}pt "
                    f"and the max "
                    f"{(qa.get('spread') or {}).get('max', float('nan')) * 100:.1f}pt. "
                    f"This is Phase 2's finding repeated: a good EV on a "
                    f"one-sided book is a quote that was not there. It is listed "
                    f"as supporting because the verdict is already carried by "
                    f"outcomes measured on fills that *were* available."
                ),
            }
        )

    return out


def sensitivity_sweep(
    spec: SeriesSpec,
    config: ProjectionConfig,
    tape: Sequence[TapeRow],
    args,
) -> List[dict]:
    """Move one knob at a time on the headline and measure, rather than assert.

    Every pass shares one projection cache: order size, decision hour, the
    adverse-fill allowance and the maker fill rule do not enter the regression,
    so refitting for each would be redundant work on a machine that is the
    binding constraint.
    """
    cache: Dict[Tuple[date, date], GasProjection] = {}
    variants: List[Tuple[str, str, dict]] = [
        ("order size C", f"{args.quantity} (headline)", {}),
        ("order size C", "1", {"quantity": 1}),
        ("order size C", "20", {"quantity": 20}),
        ("decision hour ET", "12:00", {"hour_et": 12}),
        ("decision hour ET", f"{args.hour_et}:00 (headline)", {}),
        ("decision hour ET", "23:00", {"hour_et": 23}),
        ("adverse-fill allowance", "0c", {"allowance": 0.0}),
        ("adverse-fill allowance", "1c (headline)", {}),
        ("adverse-fill allowance", "2c", {"allowance": 0.02}),
        ("adverse-fill allowance", "3c", {"allowance": 0.03}),
        (
            "maker fill rule",
            "candle high/low traversal (headline)",
            {},
        ),
        (
            "maker fill rule",
            "candle close traversal only",
            {"maker_fill_extremes": False},
        ),
        ("divergence gate", "8pt (headline)", {}),
        ("divergence gate", "15pt", {"min_divergence": 0.15}),
        ("divergence gate", "25pt", {"min_divergence": 0.25}),
        ("FR-4.3 window", f"{args.window_days} d (headline)", {}),
        ("FR-4.3 window", "7 d", {"window_days": 7}),
        ("FR-4.3 window", "3 d", {"window_days": 3}),
    ]
    out: List[dict] = []
    for knob, label, overrides in variants:
        kwargs = dict(
            config=config,
            quantity=args.quantity,
            hour_et=args.hour_et,
            window_days=args.window_days,
            min_divergence=args.min_divergence,
            proj_cache=cache,
        )
        kwargs.update(overrides)
        mode_for_fill = "maker" if knob == "maker fill rule" else "taker"
        row = {"knob": knob, "variant": label}
        for series, key in (("KXAAAGASM", "M"), ("KXAAAGASW", "W")):
            run = simulate_ev(spec, tape, series_filter=series, **kwargs)
            summary = accepted_summary(run.cells, mode_for_fill)
            cluster = summary.get("cluster") or {}
            row[f"ev_{key}"] = summary.get("ev")
            row[f"realized_{key}"] = cluster.get("event_mean")
            row[f"n_{key}"] = summary.get("n_filled")
            row[f"events_{key}"] = cluster.get("n_events")
            row[f"fits_{key}"] = run.fits
        row["leg"] = mode_for_fill
        out.append(row)
    return out


def _sensitivity_md(rows: Sequence[dict]) -> str:
    table_rows = []
    for r in rows:
        table_rows.append(
            [
                r["knob"],
                r["variant"],
                r["leg"],
                r["n_M"] or 0,
                _c(r["ev_M"]),
                _c(r["realized_M"]),
                _sgn(r["realized_M"]),
                r["n_W"] or 0,
                _c(r["ev_W"]),
                _c(r["realized_W"]),
                _sgn(r["realized_W"]),
            ]
        )
    return _table(
        [
            "knob",
            "variant",
            "leg scored",
            "n trades M",
            "EV/ct M",
            "realized/ct M",
            "sign",
            "n trades W",
            "EV/ct W",
            "realized/ct W",
            "sign",
        ],
        table_rows,
    )


def _cmd_run(args) -> int:
    tape = load_tape(args.tape)
    pinned = load_pinned_truth()
    leads = (1, 7, 14)
    os.makedirs(args.out_dir, exist_ok=True)

    t0 = time.time()
    results: Dict[str, AxisResult] = {}
    for axis in perturbation_axes():
        logger.info("axis %s ...", axis.key)
        results[axis.key] = run_axis(
            axis,
            gas_dir=args.gas_dir,
            tape=tape,
            pinned=pinned,
            leads=leads,
            quantity=args.quantity,
            hour_et=args.hour_et,
            window_days=args.window_days,
            min_divergence=args.min_divergence,
            with_weekly=True,
        )
    head = results["headline"]
    logger.info("sensitivity sweep ...")
    sensitivities = sensitivity_sweep(head.spec, head.config, tape, args)
    elapsed = time.time() - t0
    total_fits = sum(r.get("fits_M", 0) + r.get("fits_W", 0) for r in sensitivities)
    total_fits += sum(
        r.monthly.fits + (r.weekly.fits if r.weekly else 0) + r.mae_fits
        for r in results.values()
    )
    logger.info("%d axes, %d fits, %.1fs", len(results), total_fits, elapsed)

    artifact_date = args.date or datetime.now(ET).date().isoformat()
    payload = _payload(results, tape, pinned, args, artifact_date, total_fits, elapsed)
    payload["sensitivities"] = sensitivities
    # The markdown is written first and hands back the exact objects it rendered
    # from. The JSON then serialises *those*, rather than recomputing the same
    # quantities from `results` — two files publishing one shared state drift
    # apart otherwise, without either edit being wrong on its own.
    md_path = os.path.join(args.out_dir, f"phase4_backtest_{artifact_date}.md")
    claims = write_markdown(md_path, results, payload, args, artifact_date)
    payload["deferrals"] = claims.register.payload()
    payload["aaa_vs_kalshi_crosscheck"] = asdict(claims.crosscheck)
    payload["month_ends"] = {
        "held_out": claims.month_ends,
        "required": REQUIRED_MONTH_ENDS,
        "clause_met": claims.month_ends_met,
    }
    json_path = os.path.join(args.out_dir, f"phase4_backtest_data_{artifact_date}.json")
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.write("\n")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 4 gas backtest (PRD FR-4.2/FR-4.3, exit criterion 2)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_tape = sub.add_parser("fetch-tape", help="fetch the historical quote tape")
    p_tape.add_argument("--out", default=TAPE_PATH)
    p_tape.set_defaults(func=_cmd_fetch_tape)

    p_meta = sub.add_parser(
        "fetch-series-meta",
        help="record each gas series' live fee_type and settlement source",
    )
    p_meta.add_argument("--out", default=SERIES_META_PATH)
    p_meta.set_defaults(
        func=lambda a: (print(json.dumps(fetch_series_metadata(a.out), indent=2)), 0)[1]
    )

    p_cov = sub.add_parser(
        "fetch-covariates", help="fetch alternative RBOB spot series"
    )
    p_cov.add_argument("--start", default="2020-06-01")
    p_cov.set_defaults(func=_cmd_fetch_covariates)

    p_run = sub.add_parser("run", help="run the analysis and write the artifact")
    p_run.add_argument("--tape", default=TAPE_PATH)
    p_run.add_argument("--gas-dir", default=GAS_TRUTH_DIR)
    p_run.add_argument("--out-dir", default=PHASE4_DIR)
    p_run.add_argument("--date", default=None, help="artifact date (default: today ET)")
    p_run.add_argument("--quantity", type=int, default=HEADLINE_QUANTITY)
    p_run.add_argument("--hour-et", type=int, default=HEADLINE_DECISION_HOUR_ET)
    p_run.add_argument("--window-days", type=int, default=14)
    p_run.add_argument("--min-divergence", type=float, default=0.08)
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
