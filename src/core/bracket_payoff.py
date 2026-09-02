"""Single source of truth for Kalshi daily-high bracket payoffs (PRD FR-1.2).

Every code path that needs to know whether a weather contract settles YES --
strategies, the simulated exchange's settlement path, the reconcile report --
must go through this module. Nothing here inspects the ticker string: contract
direction is derived exclusively from the API fields ``strike_type``,
``floor_strike`` and ``cap_strike`` (PRD FR-1.1).

Semantics, verified against the live Kalshi API on 2026-07-25:

===============  =============================  ==============================
``strike_type``  Example                        YES settles when
===============  =============================  ==============================
``between``      ``KXHIGHNY-26JUL25-B86.5``     ``floor <= high <= cap``
                 floor=86 cap=87 "86 to 87"
``greater``      ``KXHIGHNY-26JUL25-T87``       ``high >= floor + 1``
                 floor=87 "88 or above"
``less``         ``KXHIGHNY-26JUL25-T80``       ``high <= cap - 1``
                 cap=80 "79 or below"
===============  =============================  ==============================

Note the off-by-one on the one-sided types: a ``greater`` contract with
``floor_strike=87`` does NOT pay at 87, and a ``less`` contract with
``cap_strike=80`` does NOT pay at 80. Kalshi's own rule text is strict --
"is greater than 88", "is less than 81" -- and daily highs settle as whole
degrees.

The legacy parser read the direction off the ticker's suffix letter (B versus
T) and treated every T contract as one-sided "above". That gets ``between``
partially wrong and ``less`` exactly backwards; it is deleted, and
``tests/test_bracket_payoff.py`` keeps a mutation test that proves the golden
table can still detect its reintroduction. Grepping ``src/`` for a suffix
letter test must return zero hits (PRD Phase 1 exit criterion 2), which is
why this docstring spells the pattern out in prose rather than in code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

STRIKE_TYPE_BETWEEN = "between"
STRIKE_TYPE_GREATER = "greater"
STRIKE_TYPE_LESS = "less"

VALID_STRIKE_TYPES = (
    STRIKE_TYPE_BETWEEN,
    STRIKE_TYPE_GREATER,
    STRIKE_TYPE_LESS,
)

# Daily-high market families this module governs.
WEATHER_SERIES_PREFIXES = ("KXHIGH",)


class BracketSpecError(ValueError):
    """Raised when bracket semantics cannot be established from API fields.

    Callers must treat this as a hard abort (never a silent default): a
    missing ``strike_type`` means we do not know which way the contract pays,
    and guessing is how the historical weather book lost money.
    """


def _coerce_optional_float(value: Any, field: str, ticker: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        # float(True) == 1.0 would quietly become a 1-degree strike.
        raise BracketSpecError(f"{ticker}: {field}={value!r} is not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BracketSpecError(f"{ticker}: {field}={value!r} is not numeric") from exc
    if math.isnan(result) or math.isinf(result):
        raise BracketSpecError(f"{ticker}: {field}={value!r} is not a finite number")
    return result


@dataclass(frozen=True)
class BracketSpec:
    """Immutable description of one bracket's settlement rule."""

    ticker: str
    strike_type: str
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None

    def __post_init__(self) -> None:
        if self.strike_type not in VALID_STRIKE_TYPES:
            raise BracketSpecError(
                f"{self.ticker}: unknown strike_type {self.strike_type!r} "
                f"(expected one of {VALID_STRIKE_TYPES})"
            )
        if self.strike_type == STRIKE_TYPE_BETWEEN:
            if self.floor_strike is None or self.cap_strike is None:
                raise BracketSpecError(
                    f"{self.ticker}: strike_type='between' requires both "
                    f"floor_strike and cap_strike "
                    f"(got floor={self.floor_strike!r}, cap={self.cap_strike!r})"
                )
            if self.cap_strike < self.floor_strike:
                raise BracketSpecError(
                    f"{self.ticker}: cap_strike {self.cap_strike} is below "
                    f"floor_strike {self.floor_strike}"
                )
        elif self.strike_type == STRIKE_TYPE_GREATER:
            if self.floor_strike is None:
                raise BracketSpecError(
                    f"{self.ticker}: strike_type='greater' requires floor_strike"
                )
        elif self.strike_type == STRIKE_TYPE_LESS:
            if self.cap_strike is None:
                raise BracketSpecError(
                    f"{self.ticker}: strike_type='less' requires cap_strike"
                )

    def as_dict(self) -> dict:
        """Serializable form, for caching on a position or a CSV row."""
        return {
            "strike_type": self.strike_type,
            "floor_strike": self.floor_strike,
            "cap_strike": self.cap_strike,
        }

    def describe(self) -> str:
        """Human-readable rule, mirroring Kalshi's ``yes_sub_title``."""
        lo, hi = yes_bounds(self)
        if math.isinf(lo):
            return f"{hi:g} or below"
        if math.isinf(hi):
            return f"{lo:g} or above"
        if lo == hi:
            return f"exactly {lo:g}"
        return f"{lo:g} to {hi:g}"


def is_weather_symbol(symbol: str) -> bool:
    """True for daily-high market tickers governed by this module."""
    if not symbol:
        return False
    upper = str(symbol).upper()
    return any(prefix in upper for prefix in WEATHER_SERIES_PREFIXES)


def parse_bracket_spec(symbol: str, extra: Optional[Mapping[str, Any]]) -> BracketSpec:
    """Build a :class:`BracketSpec` from ``MarketData.extra``.

    :raises BracketSpecError: if the API fields are missing or inconsistent.
        The ticker string is never consulted for direction.
    """
    if not extra:
        raise BracketSpecError(
            f"{symbol}: no market extra fields; cannot establish bracket semantics"
        )
    if not hasattr(extra, "get"):
        raise BracketSpecError(
            f"{symbol}: market extra is {type(extra).__name__}, expected a mapping"
        )
    raw_type = extra.get("strike_type")
    if raw_type is None or str(raw_type).strip() == "":
        raise BracketSpecError(
            f"{symbol}: strike_type absent from API fields; refusing to infer "
            f"direction from the ticker"
        )
    return BracketSpec(
        ticker=symbol,
        strike_type=str(raw_type).strip().lower(),
        floor_strike=_coerce_optional_float(
            extra.get("floor_strike"), "floor_strike", symbol
        ),
        cap_strike=_coerce_optional_float(
            extra.get("cap_strike"), "cap_strike", symbol
        ),
    )


def spec_from_position(pos: Mapping[str, Any]) -> BracketSpec:
    """Rebuild a :class:`BracketSpec` from a persisted position record.

    ``SimulatedExchange.open_position`` caches ``strike_type`` /
    ``floor_strike`` / ``cap_strike`` on the position so settlement never has
    to re-derive them from the ticker.
    """
    return parse_bracket_spec(pos.get("symbol", "<unknown>"), pos)


def attach_spec_to_signals(signals, market_data):
    """Stamp bracket semantics from ``market_data`` onto outgoing signals.

    The chain ``strategy -> TradeSignal -> RiskManager.record_execution ->
    SimulatedExchange.open_position -> position record -> settlement`` has to
    carry ``strike_type``/``floor_strike``/``cap_strike`` end to end: a
    position that reaches expiry without them cannot be settled through
    :func:`settles_yes` and is closed ``SETTLEMENT_UNRESOLVED`` instead. Call
    this at every strategy's return boundary so no emitting path can forget.

    Non-weather signals and markets whose semantics cannot be established are
    returned untouched — the strategy itself is responsible for rejecting the
    latter (they never reach here with a signal attached).

    PRD FR-F0.1: every weather signal also leaves here with a tz-aware
    ``expiration_time`` -- the settlement-day close from
    :func:`src.core.weather_settlement.settlement_close_for`. The exchange's
    EXPIRATION CHECK only runs for positions that carry one, and no weather
    strategy ever set it, so the FR-1.2 settlement branch was unreachable from
    the live path. A caller-supplied non-``None`` value is never overridden;
    non-weather symbols are not stamped.
    """
    if not signals:
        return signals
    market_symbol = getattr(market_data, "symbol", "")
    for signal in signals:
        _stamp_expiration(signal, market_symbol)
    extra = getattr(market_data, "extra", None)
    try:
        spec = parse_bracket_spec(market_symbol, extra)
    except BracketSpecError:
        return signals
    for signal in signals:
        signal.strike_type = spec.strike_type
        signal.floor_strike = spec.floor_strike
        signal.cap_strike = spec.cap_strike
    return signals


def _stamp_expiration(signal, fallback_symbol: str) -> None:
    """Set ``signal.expiration_time`` to the settlement-day close, if absent."""
    if getattr(signal, "expiration_time", None) is not None:
        return
    symbol = getattr(signal, "symbol", None) or fallback_symbol
    if not is_weather_symbol(symbol):
        return
    # Deferred: weather_settlement imports this module at load time (see its
    # module docstring), so a top-level import here would close the cycle.
    from src.core.weather_settlement import settlement_close_for

    close = settlement_close_for(symbol)
    if close is not None:
        signal.expiration_time = close


def yes_bounds(spec: BracketSpec) -> tuple:
    """Inclusive ``(low, high)`` daily-high interval that settles YES.

    Open ends are ``-math.inf`` / ``math.inf``.
    """
    if spec.strike_type == STRIKE_TYPE_BETWEEN:
        return (float(spec.floor_strike), float(spec.cap_strike))
    if spec.strike_type == STRIKE_TYPE_GREATER:
        return (float(spec.floor_strike) + 1.0, math.inf)
    # STRIKE_TYPE_LESS
    return (-math.inf, float(spec.cap_strike) - 1.0)


def settles_yes(spec: BracketSpec, high_temp: float) -> bool:
    """True iff the contract settles YES for an observed daily high.

    :param high_temp: the settlement station's daily maximum temperature (F),
        as published in the NWS Climatological Report.
    """
    if high_temp is None:
        raise BracketSpecError(
            f"{spec.ticker}: cannot settle without an observed daily high"
        )
    if isinstance(high_temp, bool):
        raise BracketSpecError(
            f"{spec.ticker}: observed daily high {high_temp!r} is not numeric"
        )
    try:
        value = float(high_temp)
    except (TypeError, ValueError) as exc:
        raise BracketSpecError(
            f"{spec.ticker}: observed daily high {high_temp!r} is not numeric"
        ) from exc
    if math.isnan(value):
        # NaN silently compares False against every bound, which would settle
        # every contract NO. Abort instead (missing critical input).
        raise BracketSpecError(
            f"{spec.ticker}: observed daily high is NaN; refusing to settle"
        )
    lo, hi = yes_bounds(spec)
    return lo <= value <= hi


def settlement_price(spec: BracketSpec, high_temp: float) -> float:
    """Terminal YES price: ``1.00`` on a YES settlement, ``0.00`` otherwise."""
    return 1.0 if settles_yes(spec, high_temp) else 0.0


def p_yes_from_cdf(spec: BracketSpec, cdf: Callable[[float], float]) -> float:
    """P(YES) under a distribution over the daily high.

    :param cdf: ``cdf(x) -> P(daily_high <= x)``. Daily highs settle as whole
        degrees, so the probability mass of the inclusive band ``[lo, hi]`` is
        ``cdf(hi) - cdf(lo - 1)``.
    """
    lo, hi = yes_bounds(spec)

    def _eval(x: float) -> float:
        try:
            value = float(cdf(x))
        except (TypeError, ValueError) as exc:
            raise BracketSpecError(
                f"{spec.ticker}: cdf({x}) did not return a number"
            ) from exc
        if math.isnan(value):
            raise BracketSpecError(f"{spec.ticker}: cdf({x}) returned NaN")
        return value

    upper = 1.0 if math.isinf(hi) else _eval(hi)
    lower = 0.0 if math.isinf(lo) else _eval(lo - 1.0)
    return max(0.0, min(1.0, upper - lower))
