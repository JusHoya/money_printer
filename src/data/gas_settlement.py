"""Settlement semantics and settlement-truth resolver for the AAA gas markets.

PRD FR-4.4: *"Same settlement-true recorder/reconcile pattern as weather (AAA
published value = truth)."* This module is to ``KXAAAGAS*`` what
:mod:`src.core.bracket_payoff` plus :mod:`src.core.weather_settlement` are to
``KXHIGH*``, and it deliberately follows their shape:

* semantics come **only** from the API fields ``strike_type`` and
  ``floor_strike`` -- never from the ticker (PRD FR-1.1 / Phase 1 EC-2);
* a value that cannot be established is reported as missing, never defaulted
  (project rule ``abort-on-missing-critical-input``);
* the truth lookup reads the FR-4.4 settlement cache first, then the AAA daily
  series, and returns ``None`` rather than substituting anything else.

WHAT THE CONTRACT SETTLES ON
----------------------------
``KXAAAGASM`` settles on the AAA national average retail price for regular
gasoline **as published by AAA on one specific calendar date** -- not a
month-to-date or monthly average. See ``docs/phase4_data_contract.md`` §0.

    "If average regular gas prices for United States are strictly greater than
     $3.89 on Jun 30, 2026 according to AAA, the market resolves to Yes."
    -- rules_primary, KXAAAGASM-26JUN30-3.89, fetched 2026-07-29

STRICTLY GREATER (the whole ballgame)
-------------------------------------
YES pays iff ``value > floor_strike``. A settle exactly equal to the strike
pays **NO**. This is not an inference from the rule text: across the 1,506
settled markets in the three AAA gas series that the public API still returned
on 2026-07-29, **15 markets settled with ``expiration_value == floor_strike``
exactly, and all 15 settled NO** (0 settled YES). A ``>=`` payoff would invert
every one of those 15 published results, and a strict ``>`` payoff reproduces
all 1,506 with zero disagreements. The evidence is committed verbatim at
``tests/fixtures/gas/gas_boundary_ties.json`` and re-run as a golden table by
``tests/test_gas_semantics.py``.

Note that Kalshi's own ``yes_sub_title`` reads ``"Above 3.860"`` on the market
whose settle was exactly ``3.860`` and which paid NO. The sub-title is display
prose, not the rule; ``rules_primary`` carries the word "strictly". Nothing in
this module derives semantics from a sub-title.

The monthly (traded) series is by itself **silent** on the boundary: neither of
the two retrievable month-end settles (4.336, 3.847) landed on a strike. All 15
boundary proofs come from the daily and weekly AAA series, which settle on the
same published AAA number under the same rule text. That is why this module
governs all three series rather than only the one Phase 4 trades.

VERIFIED FIELD SHAPES (probed live 2026-07-29, anonymous read)
--------------------------------------------------------------
``GET https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXAAAGASM&status=settled``

Every settled market carries::

    ticker                   "KXAAAGASM-26JUN30-3.89"
    status                   "finalized"
    result                   "yes" | "no"
    strike_type              "greater"     (136/136 monthly, 1506/1506 overall)
    floor_strike             3.89          (float; 2dp monthly, 3dp daily/weekly)
    cap_strike               absent        (None on every gas market seen)
    expiration_value         "3.847"       <- Kalshi's own settlement input
    close_time               2026-06-30T03:59:00Z  (23:59 ET the day BEFORE)
    expected_expiration_time 2026-06-30T14:00:00Z  (10:00 ET on the date)

Two shape traps, both guarded below:

1. **``status`` values.** The query parameter is ``status=settled`` but the
   returned markets carry ``status="finalized"``. Filtering the response on
   ``status == "settled"`` silently yields zero markets -- the exact
   "reconciled nothing, reported success" failure the coverage floor exists to
   catch. :data:`TERMINAL_STATUSES` accepts the family.
2. **Settled-market retention.** The API stops returning markets for older
   events: on 2026-07-29 the events endpoint listed 33 monthly events back to
   2023-12-31, but only ``26MAY31`` and ``26JUN30`` still carried any markets
   (``/markets?event_ticker=...`` and a direct ``/markets/{ticker}`` read both
   return empty/404 for the rest). Any historical harvest must therefore be
   run and persisted continuously; it cannot be reconstructed later.

PINNING TRUTH FROM SETTLEMENTS ALONE
------------------------------------
Because a ladder is a set of ``greater`` contracts at closely spaced strikes,
the published YES/NO pattern brackets the settled value without consulting AAA
at all:

* every YES at strike ``k`` proves ``value > k``  -> ``low  = max(YES strikes)``
* every NO  at strike ``k`` proves ``value <= k`` -> ``high = min(NO strikes)``

so ``value in (low, high]``. :func:`pin_truth_from_ladder` computes that
interval and refuses a non-monotonic ladder (a YES at or above a NO strike),
which would mean the results contradict the semantics rather than confirm them.
This is the only truth channel in Phase 4 that is independent of the AAA series
a projection is fitted on, which is what makes it admissible as held-out truth.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

#: Owned by workstream A per ``docs/phase4_data_contract.md`` §1. This module
#: only ever reads from it, and tolerates it being absent or partial.
GAS_TRUTH_DIR = os.path.join(REPO_ROOT, "data", "gas_truth")
AAA_DAILY_CSV = os.path.join(GAS_TRUTH_DIR, "aaa_daily_national.csv")

#: Written by the FR-4.4 recorder (``scripts/reconcile_gas.py``). Same shape as
#: the weather cache: ``{"truth": {"<SERIES>|<YYYY-MM-DD>": {...}}, "markets": {...}}``.
SETTLEMENT_CACHE_PATH = os.path.join(GAS_TRUTH_DIR, "settlement_cache.json")
REPORT_DIR = os.path.join(GAS_TRUTH_DIR, "reconcile")

# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------
STRIKE_TYPE_GREATER = "greater"

#: Strike types whose gas semantics have been verified against live settled
#: results. ``less`` and ``between`` gas markets have never been observed; a
#: complement rule for them would be a guess, and :func:`parse_gas_spec`
#: refuses rather than guesses.
VERIFIED_STRIKE_TYPES = (STRIKE_TYPE_GREATER,)

#: Plausibility envelope for a US national average retail gasoline price,
#: USD/gal. A value outside it is a parse failure, not a market event.
VALUE_FLOOR = 1.00
VALUE_CEILING = 9.00

TERMINAL_STATUSES = frozenset({"finalized", "settled", "closed"})

SOURCE_AAA_SERIES = "aaa_daily_national"
SOURCE_KALSHI_SETTLEMENT = "kalshi_settlement"

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_MONTH_ABBR = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

#: ``26JUN30`` -> (26, JUN, 30). Anchored so a strike segment like ``3.89``
#: can never match, and so the *date label* is the only thing read out of a
#: ticker. Direction is never read from a ticker (PRD FR-1.1).
_EVENT_DATE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")


class GasSpecError(ValueError):
    """Raised when gas settlement semantics cannot be established.

    Always a hard abort for the caller. A guessed direction or a defaulted
    strike would settle simulated positions against fiction, which is the
    failure class the Phase 0-4 pivot exists to remove.
    """


class GasTruthError(RuntimeError):
    """Raised when the AAA truth series is present but unusable."""


# ---------------------------------------------------------------------------
# Series registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GasSeriesSpec:
    """One AAA gas series and the cadence of its settlement dates.

    ``expected_ladder_size`` is the smallest ladder observed live on
    2026-07-29 and is what the reconcile coverage floor is scaled from -- it is
    a *floor*, not a prediction.
    """

    series_ticker: str
    cadence: str  # "monthly" | "weekly" | "daily"
    title: str
    expected_ladder_size: int
    timezone: str = "America/New_York"


#: The three AAA national-average series, all verified live 2026-07-29.
#: ``KXAAAGASM`` is the one Phase 4 trades (PRD FR-4.3); the daily and weekly
#: series settle on the same published AAA number under the same rule text and
#: are what supply the boundary evidence and the dense truth history.
GAS_SERIES: Dict[str, GasSeriesSpec] = {
    "KXAAAGASM": GasSeriesSpec("KXAAAGASM", "monthly", "US gas price", 33),
    "KXAAAGASW": GasSeriesSpec("KXAAAGASW", "weekly", "US gas price up", 18),
    "KXAAAGASD": GasSeriesSpec("KXAAAGASD", "daily", "US gas price up", 13),
}

#: The traded series (PRD FR-4.3).
PRIMARY_SERIES = "KXAAAGASM"


def get_series(series_ticker: str) -> GasSeriesSpec:
    key = str(series_ticker).upper().strip()
    if key not in GAS_SERIES:
        raise GasSpecError(
            f"unknown gas series {series_ticker!r} (known: {sorted(GAS_SERIES)})"
        )
    return GAS_SERIES[key]


def is_gas_symbol(symbol: Any) -> bool:
    """True for tickers governed by this module.

    Delegates to :func:`series_for` rather than testing prefixes directly, so a
    neighbouring series (``KXAAAGASMAX``, ``KXAAAGASMIN`` -- annual high/low
    markets on the same underlying) can never be mistaken for one of the three
    this module knows how to settle.
    """
    return series_for(symbol) is not None


def series_for(symbol: Any) -> Optional[str]:
    """``KXAAAGASM-26JUN30-3.89`` -> ``"KXAAAGASM"``. ``None`` when unmapped.

    Longest prefix wins, so ``KXAAAGASMAX`` (a different, annual series) can
    never be swallowed by ``KXAAAGASM``.
    """
    if not symbol:
        return None
    upper = str(symbol).upper()
    for series in sorted(GAS_SERIES, key=len, reverse=True):
        if upper.startswith(series):
            rest = upper[len(series) :]
            # A real ticker continues with the event separator. Without this,
            # KXAAAGASMAX-... would match the KXAAAGASM prefix.
            if rest == "" or rest.startswith("-"):
                return series
    return None


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
def _coerce_strike(value: Any, field: str, ticker: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        # float(True) == 1.0 would quietly become a $1.00/gal strike.
        raise GasSpecError(f"{ticker}: {field}={value!r} is not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise GasSpecError(f"{ticker}: {field}={value!r} is not numeric") from exc
    if math.isnan(result) or math.isinf(result):
        raise GasSpecError(f"{ticker}: {field}={value!r} is not a finite number")
    return result


@dataclass(frozen=True)
class GasSpec:
    """Immutable description of one gas contract's settlement rule."""

    ticker: str
    strike_type: str
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None

    def __post_init__(self) -> None:
        if self.strike_type not in VERIFIED_STRIKE_TYPES:
            raise GasSpecError(
                f"{self.ticker}: strike_type {self.strike_type!r} has no "
                f"verified gas settlement rule (verified: "
                f"{list(VERIFIED_STRIKE_TYPES)}). Probe the live market's "
                f"rules_primary and add a golden row before trading it -- do "
                f"not assume the complement of 'greater'."
            )
        if self.floor_strike is None:
            raise GasSpecError(
                f"{self.ticker}: strike_type='greater' requires floor_strike"
            )
        if self.cap_strike is not None:
            raise GasSpecError(
                f"{self.ticker}: strike_type='greater' carried cap_strike="
                f"{self.cap_strike!r}; no observed gas market does, so the "
                f"semantics are unverified"
            )

    def as_dict(self) -> dict:
        """Serializable form, for caching on a position or a CSV row."""
        return {
            "strike_type": self.strike_type,
            "floor_strike": self.floor_strike,
            "cap_strike": self.cap_strike,
        }

    def describe(self) -> str:
        """Human-readable YES rule.

        Deliberately *not* Kalshi's ``yes_sub_title``: that reads
        ``"Above 3.860"`` on a market whose settle of exactly ``3.860`` paid
        NO. This spells the strict comparison out so nobody reads the boundary
        off a label.
        """
        return f"strictly above {self.floor_strike:g}"


def parse_gas_spec(symbol: str, extra: Optional[Mapping[str, Any]]) -> GasSpec:
    """Build a :class:`GasSpec` from API fields (or ``MarketData.extra``).

    :raises GasSpecError: when the fields are missing or unverified. The ticker
        string is never consulted for direction or for the strike magnitude --
        both come from the API (PRD FR-1.1 / Phase 1 EC-2).
    """
    if not extra:
        raise GasSpecError(
            f"{symbol}: no market fields; cannot establish settlement semantics"
        )
    if not hasattr(extra, "get"):
        raise GasSpecError(
            f"{symbol}: market extra is {type(extra).__name__}, expected a mapping"
        )
    raw_type = extra.get("strike_type")
    if raw_type is None or str(raw_type).strip() == "":
        raise GasSpecError(
            f"{symbol}: strike_type absent from API fields; refusing to infer "
            f"direction from the ticker"
        )
    return GasSpec(
        ticker=symbol,
        strike_type=str(raw_type).strip().lower(),
        floor_strike=_coerce_strike(extra.get("floor_strike"), "floor_strike", symbol),
        cap_strike=_coerce_strike(extra.get("cap_strike"), "cap_strike", symbol),
    )


def spec_from_position(pos: Mapping[str, Any]) -> GasSpec:
    """Rebuild a :class:`GasSpec` from a persisted position record."""
    return parse_gas_spec(pos.get("symbol", "<unknown>"), pos)


def _coerce_value(spec: GasSpec, value: Any) -> float:
    if value is None:
        raise GasSpecError(
            f"{spec.ticker}: cannot settle without a published AAA value"
        )
    if isinstance(value, bool):
        raise GasSpecError(f"{spec.ticker}: settled value {value!r} is not numeric")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise GasSpecError(
            f"{spec.ticker}: settled value {value!r} is not numeric"
        ) from exc
    if math.isnan(out):
        # NaN compares False against every bound, which would settle every
        # contract NO. Abort instead (missing critical input).
        raise GasSpecError(f"{spec.ticker}: settled value is NaN; refusing to settle")
    if math.isinf(out):
        raise GasSpecError(f"{spec.ticker}: settled value is infinite")
    return out


def settles_yes(spec: GasSpec, value: Any) -> bool:
    """True iff the contract settles YES for a published AAA value.

    **Strictly greater than the strike.** ``value == floor_strike`` settles NO;
    see the module docstring for the 15 live markets that prove it.
    """
    return _coerce_value(spec, value) > float(spec.floor_strike)


def settlement_price(spec: GasSpec, value: Any) -> float:
    """Terminal YES price: ``1.00`` on a YES settlement, ``0.00`` otherwise."""
    return 1.0 if settles_yes(spec, value) else 0.0


def expected_result(spec: GasSpec, value: Any) -> str:
    """``"yes"`` / ``"no"``, in Kalshi's own vocabulary."""
    return "yes" if settles_yes(spec, value) else "no"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def normalize_date(value: Any) -> str:
    """Coerce ``date``/``datetime``/``str`` to a ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        raise GasSpecError("empty date")
    candidate = text[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError as exc:
        raise GasSpecError(f"unparseable date {value!r} (want YYYY-MM-DD)") from exc
    return candidate


def event_ticker(series_ticker: str, date: Any) -> str:
    """``("KXAAAGASM", "2026-06-30") -> "KXAAAGASM-26JUN30"``."""
    day = datetime.strptime(normalize_date(date), "%Y-%m-%d").date()
    return (
        f"{str(series_ticker).upper()}-{day.year % 100:02d}"
        f"{_MONTH_ABBR[day.month - 1]}{day.day:02d}"
    )


def settlement_date_for(symbol: Any) -> Optional[_date]:
    """The ET calendar date whose AAA publication settles ``symbol``.

    ``KXAAAGASM-26JUN30-3.89`` -> ``date(2026, 6, 30)``.

    This reads a date *label* out of the ticker -- an identifier, not
    semantics. Direction and strike still come only from the API fields.
    """
    if not symbol:
        return None
    for segment in str(symbol).upper().split("-"):
        match = _EVENT_DATE_RE.match(segment)
        if not match:
            continue
        month = _MONTHS.get(match.group(2))
        if month is None:
            continue
        try:
            return _date(2000 + int(match.group(1)), month, int(match.group(3)))
        except ValueError:
            logger.error(
                "[GasSettle] %s: event segment %s is not a real calendar date",
                symbol,
                segment,
            )
            return None
    logger.error(
        "[GasSettle] %s: no event-date segment (expected e.g. 26JUN30)", symbol
    )
    return None


def month_end(day: _date) -> _date:
    """Last calendar day of ``day``'s month."""
    first_next = (
        _date(day.year + 1, 1, 1)
        if day.month == 12
        else _date(day.year, day.month + 1, 1)
    )
    return first_next - timedelta(days=1)


def settlement_dates(series_ticker: str, end_date: Any, count: int) -> List[str]:
    """The ``count`` most recent settlement dates for a series, ending at or
    before ``end_date``, oldest first.

    Cadence comes from :data:`GAS_SERIES`, so the caller never hard-codes
    "month end" or "Monday" and a reconcile of the daily series cannot
    accidentally be pointed at month boundaries.
    """
    spec = get_series(series_ticker)
    if count < 1:
        raise GasSpecError(f"count must be >= 1 (got {count})")
    end = datetime.strptime(normalize_date(end_date), "%Y-%m-%d").date()
    out: List[_date] = []

    if spec.cadence == "daily":
        out = [end - timedelta(days=offset) for offset in range(count)]
    elif spec.cadence == "weekly":
        # Observed KXAAAGASW settlement dates are all Mondays (weekday 0).
        cursor = end - timedelta(days=(end.weekday() - 0) % 7)
        out = [cursor - timedelta(days=7 * offset) for offset in range(count)]
    else:  # monthly
        cursor = month_end(end)
        if cursor > end:
            cursor = month_end(_date(end.year, end.month, 1) - timedelta(days=1))
        for _ in range(count):
            out.append(cursor)
            cursor = month_end(_date(cursor.year, cursor.month, 1) - timedelta(days=1))

    return [day.isoformat() for day in sorted(out)]


# ---------------------------------------------------------------------------
# Pinning truth from a settled ladder (Kalshi-only, AAA-independent)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PinnedTruth:
    """The published AAA value bracketed by one settled ladder.

    ``value in (low_exclusive, high_inclusive]``. Either bound may be ``None``
    when the ladder is one-sided (every market YES, or every market NO), which
    still constrains the value but does not bracket it.
    """

    settlement_date: str
    series_ticker: str
    event_ticker: str
    low_exclusive: Optional[float]
    high_inclusive: Optional[float]
    n_markets: int
    n_yes: int
    n_no: int
    kalshi_expiration_value: Optional[float]

    @property
    def interval_width(self) -> Optional[float]:
        if self.low_exclusive is None or self.high_inclusive is None:
            return None
        return round(self.high_inclusive - self.low_exclusive, 6)

    @property
    def midpoint(self) -> Optional[float]:
        if self.low_exclusive is None or self.high_inclusive is None:
            return None
        return (self.low_exclusive + self.high_inclusive) / 2.0

    def contains(self, value: Any) -> bool:
        """Is ``value`` consistent with every published result in the ladder?"""
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(candidate):
            return False
        if self.low_exclusive is not None and not candidate > self.low_exclusive:
            return False
        if self.high_inclusive is not None and not candidate <= self.high_inclusive:
            return False
        return True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "settlement_date": self.settlement_date,
            "series": self.series_ticker,
            "event_ticker": self.event_ticker,
            "value_low_exclusive": self.low_exclusive,
            "value_high_inclusive": self.high_inclusive,
            "interval_width": self.interval_width,
            "midpoint": self.midpoint,
            "n_markets": self.n_markets,
            "n_yes": self.n_yes,
            "n_no": self.n_no,
            "kalshi_expiration_value": self.kalshi_expiration_value,
            "source": SOURCE_KALSHI_SETTLEMENT,
        }


def pin_truth_from_ladder(markets: Sequence[Mapping[str, Any]]) -> PinnedTruth:
    """Bracket the settled AAA value from a ladder's published YES/NO results.

    Uses only ``result``, ``strike_type`` and ``floor_strike`` from the API --
    no AAA series, no ``expiration_value``. ``expiration_value`` *is* recorded
    on the result so a caller can cross-check it, but it never sets the bounds:
    the whole point is a truth channel derived from the published outcomes.

    :raises GasSpecError: when the ladder carries no usable settled market, or
        when the results are non-monotonic (a YES at or above a NO strike).
        A contradictory ladder means our reading of the semantics is wrong;
        silently taking the mid-point of an impossible interval is exactly the
        "plausible default" this project forbids.
    """
    if not markets:
        raise GasSpecError("empty ladder: nothing to pin")

    series: Optional[str] = None
    event: Optional[str] = None
    date_label: Optional[str] = None
    yes_strikes: List[float] = []
    no_strikes: List[float] = []
    exp_values: set = set()

    for market in markets:
        ticker = str(market.get("ticker") or "<unknown>")
        result = str(market.get("result") or "").strip().lower()
        if result not in ("yes", "no"):
            continue  # unsettled or voided: contributes no information
        spec = parse_gas_spec(ticker, market)
        if result == "yes":
            yes_strikes.append(float(spec.floor_strike))
        else:
            no_strikes.append(float(spec.floor_strike))
        series = series or series_for(ticker)
        event = event or str(market.get("event_ticker") or "")
        if date_label is None:
            day = settlement_date_for(ticker)
            date_label = day.isoformat() if day else None
        raw = market.get("expiration_value")
        if raw not in (None, ""):
            exp_values.add(str(raw))

    if not yes_strikes and not no_strikes:
        raise GasSpecError(
            f"ladder {event or '<unknown>'} carries no settled yes/no result; "
            f"nothing to pin"
        )

    low = max(yes_strikes) if yes_strikes else None
    high = min(no_strikes) if no_strikes else None
    if low is not None and high is not None and not low < high:
        raise GasSpecError(
            f"ladder {event or '<unknown>'} is non-monotonic: a YES at strike "
            f"{low:g} coexists with a NO at strike {high:g}. Under a strictly-"
            f"greater rule that is impossible, so either a result or our "
            f"reading of strike_type/floor_strike is wrong. Refusing to pin."
        )

    expiration_value: Optional[float] = None
    if len(exp_values) == 1:
        try:
            expiration_value = float(next(iter(exp_values)))
        except (TypeError, ValueError):
            expiration_value = None
    elif len(exp_values) > 1:
        logger.error(
            "[GasSettle] ladder %s reports %d distinct expiration_value(s) %s; "
            "the exchange settled one date on more than one number",
            event,
            len(exp_values),
            sorted(exp_values),
        )

    return PinnedTruth(
        settlement_date=date_label or "",
        series_ticker=series or "",
        event_ticker=event or "",
        low_exclusive=low,
        high_inclusive=high,
        n_markets=len(yes_strikes) + len(no_strikes),
        n_yes=len(yes_strikes),
        n_no=len(no_strikes),
        kalshi_expiration_value=expiration_value,
    )


# ---------------------------------------------------------------------------
# AAA daily series (workstream A's file; read-only here)
# ---------------------------------------------------------------------------
#: Columns required by ``docs/phase4_data_contract.md`` §1.
AAA_REQUIRED_COLUMNS = ("date", "value", "source", "source_url", "fetched_at")


@dataclass(frozen=True)
class AAARow:
    date: str
    value: float
    source: str
    source_url: str
    fetched_at: str
    quality: str = "ok"
    raw_sha256: str = ""


def load_aaa_series(
    path: Optional[str] = None, *, include_suspect: bool = False
) -> Dict[str, AAARow]:
    """``{date -> AAARow}`` from ``data/gas_truth/aaa_daily_national.csv``.

    Tolerant by design, because workstream A writes this file concurrently:

    * a missing file yields ``{}`` and one INFO line -- not an exception, and
      never a fabricated row;
    * a row whose ``value`` will not parse, or falls outside
      ``[VALUE_FLOOR, VALUE_CEILING]``, is dropped with an ERROR naming the
      date, so a bad parse upstream cannot settle a position;
    * ``quality=suspect`` rows are excluded unless ``include_suspect``
      (contract §1.1);
    * gaps stay gaps. Nothing here interpolates.

    :raises GasTruthError: when the file exists but lacks the contract's
        columns. That is a schema break, not missing data, and silently
        reading zero rows from it would look identical to "AAA has published
        nothing yet".
    """
    target = path or AAA_DAILY_CSV
    if not os.path.exists(target):
        logger.info(
            "[GasSettle] AAA series %s is absent; settlement truth will come "
            "from the settlement cache or be reported missing",
            target,
        )
        return {}
    try:
        with open(target, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = [f.strip() for f in (reader.fieldnames or [])]
            missing = [c for c in AAA_REQUIRED_COLUMNS if c not in fieldnames]
            if missing:
                raise GasTruthError(
                    f"{target} is missing required column(s) {missing}; expected "
                    f"the contract §1 schema {list(AAA_REQUIRED_COLUMNS)} "
                    f"(saw {fieldnames})"
                )
            rows = list(reader)
    except GasTruthError:
        raise
    except Exception as exc:
        raise GasTruthError(f"{target} is unreadable: {exc}") from exc

    out: Dict[str, AAARow] = {}
    dropped = 0
    for raw in rows:
        try:
            day = normalize_date(raw.get("date"))
        except GasSpecError:
            dropped += 1
            logger.error(
                "[GasSettle] %s: row with unparseable date %r dropped",
                target,
                raw.get("date"),
            )
            continue
        quality = (raw.get("quality") or "ok").strip().lower() or "ok"
        if quality != "ok" and not include_suspect:
            continue
        try:
            value = float(str(raw.get("value")).strip())
        except (TypeError, ValueError):
            dropped += 1
            logger.error(
                "[GasSettle] %s: %s has a non-numeric value %r; dropped rather "
                "than defaulted",
                target,
                day,
                raw.get("value"),
            )
            continue
        if math.isnan(value) or not (VALUE_FLOOR <= value <= VALUE_CEILING):
            dropped += 1
            logger.error(
                "[GasSettle] %s: %s value %r is outside the plausible range "
                "[%.2f, %.2f]; dropped",
                target,
                day,
                value,
                VALUE_FLOOR,
                VALUE_CEILING,
            )
            continue
        out[day] = AAARow(
            date=day,
            value=value,
            source=(raw.get("source") or "").strip(),
            source_url=(raw.get("source_url") or "").strip(),
            fetched_at=(raw.get("fetched_at") or "").strip(),
            quality=quality,
            raw_sha256=(raw.get("raw_sha256") or "").strip(),
        )
    if dropped:
        logger.error(
            "[GasSettle] %s: %d row(s) dropped as unusable, %d kept",
            target,
            dropped,
            len(out),
        )
    return out


# ---------------------------------------------------------------------------
# Settlement cache
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache_blob: Optional[Dict[str, Any]] = None
_cache_stamp: Optional[Tuple[float, int]] = None


def reset_caches() -> None:
    """Drop every memoized handle. Tests use this; production never needs it."""
    global _cache_blob, _cache_stamp
    with _lock:
        _cache_blob = None
        _cache_stamp = None


def load_settlement_cache(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the FR-4.4 settlement cache, always returning the two sub-maps."""
    target = path or SETTLEMENT_CACHE_PATH
    try:
        with open(target, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
    except FileNotFoundError:
        blob = {}
    except Exception as exc:
        logger.error(
            "[GasSettle] settlement cache %s unreadable (%s); starting empty",
            target,
            exc,
        )
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    blob.setdefault("truth", {})
    blob.setdefault("markets", {})
    return blob


def save_settlement_cache(cache: Mapping[str, Any], path: Optional[str] = None) -> None:
    """Atomically persist the settlement cache."""
    target = path or SETTLEMENT_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(cache, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except Exception as exc:
        logger.error("[GasSettle] could not write settlement cache %s: %s", target, exc)


def _cached_settlement_cache(path: str) -> Dict[str, Any]:
    """Settlement cache, re-read whenever the file on disk changes.

    The recorder rewrites this file from a separate process, so a long-lived
    runtime must not hold a stale copy (the weather module's lesson, reused).
    """
    global _cache_blob, _cache_stamp
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime, stat.st_size)
    except OSError:
        with _lock:
            _cache_blob, _cache_stamp = {"truth": {}, "markets": {}}, None
        return {"truth": {}, "markets": {}}
    if _cache_blob is not None and _cache_stamp == stamp:
        return _cache_blob
    blob = load_settlement_cache(path)
    with _lock:
        _cache_blob, _cache_stamp = blob, stamp
    return blob


def truth_key(series_ticker: str, date: Any) -> str:
    return f"{str(series_ticker).upper()}|{normalize_date(date)}"


def _cached_numeric_value(entry: Any) -> Optional[float]:
    """The numeric ``value`` on a cache entry, or ``None``. Never raises."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("value")
    if raw is None:
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def record_truth(
    cache: Dict[str, Any],
    series_ticker: str,
    date: Any,
    value: Optional[float],
    *,
    source: str,
    source_url: str = "",
    pinned: Optional[PinnedTruth] = None,
) -> None:
    """Write one settlement date's AAA value (and/or pinned interval) to the cache.

    **The recorder can only ever improve the cache.** A run whose AAA series is
    absent or partial passes ``value=None`` for the dates it could not resolve,
    and an unconditional write put ``value: null`` over entries a healthy
    earlier run had established correctly -- the 2026-07-30 red-team's degraded
    runs replaced 19 correct entries that way. That is fail-safe only by
    accident (:func:`_value_from_cache` returns ``None`` on a null, so the
    runtime falls through to the AAA CSV), but the runtime resolver is
    **cache-first**, so a degraded reconcile run demoted the settlement path's
    primary source. A null therefore never displaces a number:

    * ``value`` numeric -> written, replacing whatever was there. A *corrected*
      AAA value must still propagate, so this is not "first write wins".
    * ``value is None`` and the entry already holds a number -> the number and
      its provenance are kept verbatim (including ``recorded_at``, which
      belongs to the run that actually established it); only ``pinned`` is
      refreshed, because the pinned interval is derived from Kalshi's published
      results alone and is not affected by the AAA gap.
    * ``value is None`` and there is nothing to keep -> the null entry is
      written as before, so the pinned interval is still cached and the absence
      is visible.

    A retained entry is stamped ``value_retained: true`` so an operator can see
    at a glance which cached values are no longer backed by the *current* AAA
    series. That marker is the honest cost of the invariant: **a deliberate
    WITHDRAWAL of an AAA row cannot be expressed through this recorder.** From
    here a withdrawn row and a degraded harvest are indistinguishable -- both
    are simply "no row for this date" -- and retaining is the safe side of that
    ambiguity, because the cache-first runtime otherwise loses a value on every
    partial harvest. Purging a withdrawn value is therefore a deliberate
    operator action: delete the entry (or the whole cache, which is a
    regenerable artifact) and re-run the reconcile.
    """
    key = truth_key(series_ticker, date)
    truth = cache.setdefault("truth", {})
    prior = truth.get(key)
    prior_value = _cached_numeric_value(prior)

    if value is None and prior_value is not None:
        entry = dict(prior)  # type: ignore[arg-type]
        entry["value_retained"] = True
        if pinned is not None:
            entry["pinned"] = pinned.as_dict()
        truth[key] = entry
        logger.warning(
            "[GasSettle] %s: this run resolved no AAA value; retaining the "
            "cached %.3f from %s rather than overwriting it with null",
            key,
            prior_value,
            entry.get("recorded_at") or entry.get("source") or "an earlier run",
        )
        return

    entry = {
        "value": None if value is None else float(value),
        "source": source,
        "source_url": source_url,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if pinned is not None:
        entry["pinned"] = pinned.as_dict()
    elif isinstance(prior, dict) and prior.get("pinned") is not None:
        # Keep a previously pinned interval rather than dropping it: it is
        # Kalshi-derived truth and a run that could not re-fetch the ladder has
        # not disproved it.
        entry["pinned"] = prior["pinned"]
    truth[key] = entry


def _value_from_cache(series_ticker: str, day: _date, path: str) -> Optional[float]:
    truth = _cached_settlement_cache(path).get("truth")
    if not isinstance(truth, dict):
        return None
    row = truth.get(truth_key(series_ticker, day))
    if not isinstance(row, dict):
        return None
    value = row.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.error(
            "[GasSettle] settlement cache holds a non-numeric value %r for %s %s",
            value,
            series_ticker,
            day,
        )
        return None


def resolve_settlement_value(
    symbol: str,
    *,
    cache_path: Optional[str] = None,
    aaa_series: Optional[Mapping[str, AAARow]] = None,
    aaa_path: Optional[str] = None,
) -> Optional[float]:
    """The published AAA value that settles ``symbol``, or ``None``.

    Order: FR-4.4 settlement cache, then the AAA daily series. A ``None``
    return is *truth not published yet* and nothing else -- never the last
    observed value, never a projection, never an interpolation. A caller at
    expiry must hold the position and retry on a later sweep. AAA publishes the
    day's national average on the morning of that date (Kalshi's
    ``expected_expiration_time`` is 10:00 ET), so a position expiring at
    midnight legitimately waits hours for its truth.
    """
    series = series_for(symbol)
    day = settlement_date_for(symbol)
    if series is None or day is None:
        if is_gas_symbol(symbol):
            logger.error(
                "[GasSettle] cannot resolve a settlement date/series for %s", symbol
            )
        return None

    cached = _value_from_cache(series, day, cache_path or SETTLEMENT_CACHE_PATH)
    if cached is not None:
        return cached

    if aaa_series is None:
        try:
            aaa_series = load_aaa_series(aaa_path)
        except GasTruthError as exc:
            logger.error(
                "[GasSettle] AAA series unusable (%s); reporting missing truth "
                "for %s rather than a default",
                exc,
                symbol,
            )
            return None
    row = aaa_series.get(day.isoformat())
    if row is None:
        logger.info(
            "[GasSettle] no AAA national average recorded for %s (%s); truth "
            "is not published yet or the day was never harvested",
            day.isoformat(),
            symbol,
        )
        return None
    return float(row.value)


# ---------------------------------------------------------------------------
# Harvest helper (network isolated in the injected fetcher)
# ---------------------------------------------------------------------------
def pin_truth_from_settled_markets(
    markets: Iterable[Mapping[str, Any]],
) -> Dict[str, PinnedTruth]:
    """Group settled markets by event and pin each event's value.

    Returns ``{event_ticker -> PinnedTruth}``. An event whose ladder cannot be
    pinned is logged at ERROR and omitted -- it is never silently replaced by a
    neighbouring event's interval.
    """
    by_event: Dict[str, List[Mapping[str, Any]]] = {}
    for market in markets:
        event = str(market.get("event_ticker") or "")
        if not event:
            continue
        by_event.setdefault(event, []).append(market)

    out: Dict[str, PinnedTruth] = {}
    for event, ladder in sorted(by_event.items()):
        try:
            out[event] = pin_truth_from_ladder(ladder)
        except GasSpecError as exc:
            logger.error("[GasSettle] cannot pin %s: %s", event, exc)
    return out


__all__ = [
    "AAA_DAILY_CSV",
    "AAA_REQUIRED_COLUMNS",
    "AAARow",
    "GAS_SERIES",
    "GAS_TRUTH_DIR",
    "GasSeriesSpec",
    "GasSpec",
    "GasSpecError",
    "GasTruthError",
    "PRIMARY_SERIES",
    "PinnedTruth",
    "REPORT_DIR",
    "SETTLEMENT_CACHE_PATH",
    "SOURCE_AAA_SERIES",
    "SOURCE_KALSHI_SETTLEMENT",
    "STRIKE_TYPE_GREATER",
    "TERMINAL_STATUSES",
    "VALUE_CEILING",
    "VALUE_FLOOR",
    "VERIFIED_STRIKE_TYPES",
    "event_ticker",
    "expected_result",
    "get_series",
    "is_gas_symbol",
    "load_aaa_series",
    "load_settlement_cache",
    "month_end",
    "normalize_date",
    "parse_gas_spec",
    "pin_truth_from_ladder",
    "pin_truth_from_settled_markets",
    "record_truth",
    "reset_caches",
    "resolve_settlement_value",
    "save_settlement_cache",
    "series_for",
    "settlement_date_for",
    "settlement_dates",
    "settlement_price",
    "settles_yes",
    "spec_from_position",
    "truth_key",
]
