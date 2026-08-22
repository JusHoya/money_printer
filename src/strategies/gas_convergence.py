"""AAA gas convergence strategy for ``KXAAAGASM`` (PRD FR-4.3).

In the final N days of a settlement period (default 14), compare the FR-4.2
projection's ``P(settle > floor_strike)`` against the market's YES price and buy
the side the model prefers when the two disagree by at least 8 points, sized
small, with this series' **maker fee schedule** in the EV.

EVERY GATE IS LOGGED WITH THE VALUE THAT FAILED
-----------------------------------------------
Contract §3 and ``make-silent-rejections-observable``: a filter whose reject
condition is universally true kills all throughput silently, and this project has
shipped exactly that defect (a near-ATM proximity filter that rejected every
contract for weeks). So every rejection here emits one INFO line through
:func:`src.core.risk_manager.log_rejection` carrying a stable reason code **and
the measured quantity that failed** — the lead in days, the divergence in
points, the staleness in days, the EV in dollars. A day of zero entries is
reconstructible from the log alone, and a gate that has started rejecting
everything shows up as one reason code with the same measured value on every
market.

THE FEE SCHEDULE IS THE POINT OF THREADING THE SYMBOL
-----------------------------------------------------
``KXAAAGASM`` is one of the few Kalshi series in ``KNOWN_MAKER_FEE_SERIES``: it
**does** bill resting liquidity (live ``/series`` metadata reports
``fee_type == "quadratic_with_maker_fees"``). Every fee call here threads the
market symbol so :func:`src.core.fee_calculator.fee_type_for_symbol` applies the
gas schedule; a symbol-less call would silently price the maker leg at the
weather series' zero multiplier, i.e. as free.

Two fee facts that a "maker is 25% of taker" shortcut gets wrong, and which is
why nothing here scales one fee from the other:

* The published **rate** ratio is exactly 25% (maker 0.0175 vs taker 0.07 in the
  quadratic ``M x rate x C x P x (1-P)``).
* The **charged** fee is each rate's own value rounded UP to the cent on the
  order total, so at small order sizes the ratio is nothing like 25%: at
  ``P=0.10, C=1`` both legs round to $0.01 and the maker fee is 100% of the
  taker fee. Fees are therefore always computed from
  :func:`src.core.fee_calculator.compute_fee` at the **actual contract count**,
  and the per-contract cost is the order total divided by the count.

WHICH PRICE THE EV IS GATED AT
------------------------------
Both legs are computed and logged, and **both must clear zero**:

* ``ev_taker`` — enter by crossing the spread (pay ``yes_ask`` / ``no_ask``),
  charged the taker fee. This is the price available right now, so it is the
  executable, conservative number.
* ``ev_maker`` — enter by resting at the current best bid, charged this series'
  maker fee. This is the leg FR-4.3 names, and it is contingent on the resting
  order actually filling.

Requiring both is arithmetically almost the same as requiring ``ev_taker > 0``
(the bid never exceeds the ask), so it adds no filter that could silently kill
throughput — but it keeps the maker fee load-bearing rather than decorative, and
it makes the gap between the two visible on every accepted signal. The emitted
``limit_price`` is the executable price by default: pricing an entry at an
unfilled resting order is the phantom-PnL failure mode this project has already
paid for. Pass ``entry_mode="maker"`` to emit the resting price instead, once
Phase 3's ``realistic_fills`` resting-order model can adjudicate the fill.

Exit is settlement, so fees are charged for **one leg**: Kalshi levies no
settlement fee and a gas position is held to the AAA publication. Note that
``SignalProcessorMixin._ml_ev_gate`` re-checks EV downstream on the pessimistic
taker/round-trip basis, so a marginal signal accepted here can still be rejected
there — visibly, as an ``EV_GATE`` INFO line.

SETTLEMENT DATE COMES FROM API FIELDS
-------------------------------------
Never from the ticker. Verified 2026-07-29 across all three live gas events
(``KXAAAGASM-26JUL31``, ``KXAAAGASM-26AUG31``, ``KXAAAGASW-26AUG03``):
``expected_expiration_time`` is 10:00 ET on the settlement date and
``close_time`` is 23:59 ET the evening before, i.e.
``settlement_date == close_time_ET.date() + 1 day`` on every one. The provider's
``MarketData.extra`` carries ``close_time`` today, so that relationship is the
working path; ``expected_expiration_time`` is preferred when present.
:data:`REJECT_NO_SETTLEMENT_DATE` fires when neither is available — the window
gate is the whole strategy, so guessing its input is not an option.

``strike_type`` and ``floor_strike`` are likewise read from the API fields
(PRD FR-1.1). All observed gas markets are ``strike_type='greater'``; anything
else is rejected rather than interpreted.

THE SCRAPE GATE IS CONSULTED BEFORE ANYTHING ELSE IS PRICED
-----------------------------------------------------------
FR-4.1 and Phase 4 exit criterion 1 require that an induced AAA scrape failure
produces "an alert and **zero signals that day**". The alert is
:mod:`src.data.aaa_provider`'s; the zero-signals half is enforced here, because
this is the only place a gas signal can be created.

:meth:`~src.data.aaa_provider.AAAProvider.signal_gate` is therefore evaluated on
every ``analyze`` call, **before** the settlement window, the freshness check or
the projection — a blocked day must not even fit a model. It is a separate,
stronger condition than the ``max_data_age_days`` freshness check below:
freshness alone is satisfied by yesterday's row, so a day whose scrape failed
would keep trading on it, which is precisely the hole the persisted failure
record exists to close.

Two deliberate properties:

* ``gate=None`` means **use the real gate**, never "no gate". A strategy
  constructed with no arguments — which is how :class:`src.bots.gas_bot.GasBot`
  constructs it — reads ``data/gas_truth/`` through a real provider. There is no
  permissive default anywhere on this path.
* A gate that *raises* is a block, not a pass (``GAS_SCRAPE_GATE_ERROR``). An
  unreadable failure record is an unknown, and an unknown is not a licence to
  trade (``abort-on-missing-critical-input``).

Both outcomes emit one INFO rejection carrying the gate's own reason code and
detail, so a day of silence names its cause in the log.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.core.fee_calculator import compute_fee, fee_type_for_symbol
from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.core.risk_manager import log_rejection
from src.data.aaa_provider import AAAProvider, SignalGate
from src.models.gas_projection import (
    DEFAULT_PROJECTION_CONFIG,
    GasDataUnavailable,
    GasProjection,
    GasSeries,
    ProjectionConfig,
    prob_above,
    project,
)
from src.utils.logger import logger

#: Kalshi settles and closes gas markets on ET wall-clock times, and the
#: final-N-day window is a calendar count, so the "today" this strategy compares
#: against is the ET calendar date. A naive ``datetime.now()`` on the UTC VM
#: rolls the date over at 20:00 ET and would open the window a day early — the
#: same class of defect as the UTC hour-gate bug the weather rebuild deleted.
MARKET_TZ = ZoneInfo("America/New_York")

#: Series this strategy will price. ``KXAAAGASM`` (monthly) is the Phase 4
#: target. ``KXAAAGASW`` (weekly) shares the settlement source but NOT the fee
#: schedule — live metadata reports plain ``quadratic`` for it — so it is
#: admitted only because every fee call threads the symbol and therefore prices
#: it correctly on its own schedule.
GAS_SERIES_TICKERS: Tuple[str, ...] = ("KXAAAGASM", "KXAAAGASW")

#: Kalshi's ``greater`` gas rule: YES iff the published value exceeds the strike.
SUPPORTED_STRIKE_TYPE = "greater"

# --- reason codes (stable; grep these in logs) -------------------------
REJECT_UNSUPPORTED_SERIES = "GAS_UNSUPPORTED_SERIES"
REJECT_SERIES_UNAVAILABLE = "GAS_SERIES_UNAVAILABLE"
REJECT_NO_BRACKET_SPEC = "GAS_BRACKET_SPEC_UNAVAILABLE"
REJECT_UNSUPPORTED_STRIKE_TYPE = "GAS_UNSUPPORTED_STRIKE_TYPE"
REJECT_NO_SETTLEMENT_DATE = "GAS_SETTLEMENT_DATE_UNAVAILABLE"
REJECT_OUTSIDE_WINDOW = "GAS_OUTSIDE_FINAL_WINDOW"
REJECT_DATA_STALE = "GAS_DATA_STALE"
REJECT_PROJECTION_UNAVAILABLE = "GAS_PROJECTION_UNAVAILABLE"
REJECT_NEAR_RESOLVED = "GAS_NEAR_RESOLVED"
REJECT_NO_USABLE_QUOTE = "GAS_NO_USABLE_QUOTE"
REJECT_DIVERGENCE_BELOW_MIN = "GAS_DIVERGENCE_BELOW_MIN"
REJECT_EV_NOT_POSITIVE = "GAS_EV_NOT_POSITIVE"
#: The AAA provider's persisted scrape-failure / freshness gate said no. Carries
#: the provider's own reason code and measured detail (FR-4.1, Phase 4 EC-1).
REJECT_SCRAPE_GATE_BLOCKED = "GAS_SCRAPE_GATE_BLOCKED"
#: The gate could not be evaluated at all. Fail closed: an unreadable failure
#: record is an unknown, and an unknown is not a licence to trade.
REJECT_SCRAPE_GATE_ERROR = "GAS_SCRAPE_GATE_ERROR"

ENTRY_MODE_TAKER = "taker"
ENTRY_MODE_MAKER = "maker"

#: Seconds a gate verdict may be reused across the markets of one ladder pass.
#: A 20-40 bracket ladder would otherwise re-read the failure record and the
#: whole AAA series once per bracket. Bounded rather than per-day on purpose: the
#: recorder is a *different process*, so a failure recorded mid-day must be
#: honoured within the same tick, not tomorrow. Caching a block is always safe;
#: this window is the entire exposure of caching an allow, and it is far shorter
#: than the tick period.
DEFAULT_GATE_CACHE_SECONDS = 60.0

#: Built lazily so importing this module touches no files, and reused so the
#: shipped configuration does not construct a provider per bracket.
_DEFAULT_GATE_PROVIDER: Optional[AAAProvider] = None


def default_signal_gate(*, max_age_days: int = 2) -> SignalGate:
    """The shipped gate: :meth:`AAAProvider.signal_gate` over ``data/gas_truth/``.

    A strategy constructed without an explicit ``gate`` uses this, which is what
    makes the *shipped* configuration gated rather than only the tested one.
    ``max_age_days`` is threaded from the strategy's ``max_data_age_days`` so the
    freshness limit has one source of truth instead of two that can drift.
    """
    global _DEFAULT_GATE_PROVIDER
    if _DEFAULT_GATE_PROVIDER is None:
        _DEFAULT_GATE_PROVIDER = AAAProvider()
    return _DEFAULT_GATE_PROVIDER.signal_gate(max_age_days=max_age_days)


def _today_et() -> date:
    """The ET calendar date. The default clock; injectable for tests."""
    return datetime.now(MARKET_TZ).date()


def series_ticker(symbol: str) -> str:
    """Series component of a Kalshi market ticker (``SERIES-EVENT-STRIKE``)."""
    return (symbol or "").strip().upper().split("-", 1)[0]


def resolve_settlement_date(symbol: str, extra) -> Tuple[date, str]:
    """``(settlement_date, source_field)`` from the market's API fields.

    Priority: an explicit ``settlement_date`` supplied by a caller, then
    ``expected_expiration_time`` (10:00 ET on the settlement date), then
    ``close_time`` (23:59 ET the evening *before*, so ``+1 day``).

    :raises ValueError: when none is available. The final-N-day window is the
        strategy's central gate; a guessed settlement date would move it.
    """
    if not extra or not hasattr(extra, "get"):
        raise ValueError(
            f"{symbol}: no market extra fields; cannot establish the settlement date"
        )

    explicit = extra.get("settlement_date")
    if explicit:
        if isinstance(explicit, datetime):
            return explicit.date(), "settlement_date"
        if isinstance(explicit, date):
            return explicit, "settlement_date"
        try:
            return date.fromisoformat(str(explicit).strip()), "settlement_date"
        except ValueError as exc:
            raise ValueError(
                f"{symbol}: settlement_date={explicit!r} is not an ISO date"
            ) from exc

    expiration = _parse_api_timestamp(extra.get("expected_expiration_time"))
    if expiration is not None:
        return expiration.astimezone(MARKET_TZ).date(), "expected_expiration_time"

    close = _parse_api_timestamp(extra.get("close_time"))
    if close is not None:
        # Verified on every live gas event: close is 23:59 ET the evening
        # before the AAA value publishes.
        return close.astimezone(MARKET_TZ).date() + timedelta(days=1), "close_time"

    raise ValueError(
        f"{symbol}: neither expected_expiration_time nor close_time is present; "
        f"refusing to infer the settlement date from the ticker"
    )


def _parse_api_timestamp(value) -> Optional[datetime]:
    """Parse a Kalshi UTC ISO-8601 timestamp; ``None`` when absent/unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))


class GasConvergenceStrategy(Strategy):
    """FR-4.3 convergence strategy over a ``KXAAAGASM`` bracket ladder."""

    def __init__(
        self,
        final_window_days: int = 14,
        min_divergence: float = 0.08,
        max_data_age_days: int = 2,
        base_quantity: int = 5,
        entry_mode: str = ENTRY_MODE_TAKER,
        near_resolved_bid: float = 0.97,
        near_resolved_ask: float = 0.03,
        series: Optional[GasSeries] = None,
        projection_config: ProjectionConfig = DEFAULT_PROJECTION_CONFIG,
        clock=_today_et,
        gate: Optional[Callable[[], SignalGate]] = None,
        gate_cache_seconds: float = DEFAULT_GATE_CACHE_SECONDS,
    ):
        if entry_mode not in (ENTRY_MODE_TAKER, ENTRY_MODE_MAKER):
            raise ValueError(
                f"entry_mode must be {ENTRY_MODE_TAKER!r} or {ENTRY_MODE_MAKER!r}, "
                f"got {entry_mode!r}"
            )
        if base_quantity < 1:
            raise ValueError(f"base_quantity must be >= 1, got {base_quantity}")
        self.final_window_days = int(final_window_days)
        self.min_divergence = float(min_divergence)
        self.max_data_age_days = int(max_data_age_days)
        self.base_quantity = int(base_quantity)
        self.entry_mode = entry_mode
        self.near_resolved_bid = float(near_resolved_bid)
        self.near_resolved_ask = float(near_resolved_ask)
        self.projection_config = projection_config
        self.clock = clock
        self._series = series
        #: FR-4.1 / EC-1. ``None`` resolves to the REAL provider gate, never to a
        #: permissive no-op -- the shipped configuration must be the gated one.
        self._gate: Callable[[], SignalGate] = gate or (
            lambda: default_signal_gate(max_age_days=self.max_data_age_days)
        )
        self._gate_cache_seconds = max(0.0, float(gate_cache_seconds))
        self._gate_cache: Optional[Tuple[float, SignalGate]] = None
        #: Projections are memoised per (as_of, settlement_date) for the life of
        #: one ladder pass: every bracket in an event shares one fit, so a
        #: 20-market ladder costs one regression, not twenty.
        self._projection_cache = {}

    def name(self) -> str:
        return "Gas Convergence"

    # -- series wiring ---------------------------------------------------

    def set_series(self, series: Optional[GasSeries]) -> None:
        """Install the loaded AAA/RBOB/EIA series and invalidate the fit cache.

        The gate verdict is invalidated too: a series reload means the truth
        directory was just re-read, so the cheapest correct thing is to re-ask.
        """
        self._series = series
        self._projection_cache = {}
        self._gate_cache = None

    @property
    def series(self) -> Optional[GasSeries]:
        return self._series

    # -- Strategy ABC ----------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        """Price one gas bracket. Returns ``[]`` or a single-signal list.

        Every ``[]`` return path above logs exactly one INFO rejection line with
        its reason code and the measured value.
        """
        symbol = getattr(market_data, "symbol", "") or ""
        extra = getattr(market_data, "extra", None)

        series_key = series_ticker(symbol)
        if series_key not in GAS_SERIES_TICKERS:
            self._reject(REJECT_UNSUPPORTED_SERIES, symbol, series=series_key or "-")
            return []

        # --- FR-4.1 / EC-1: today's scrape must not have failed ----------
        # First substantive gate on the path, and deliberately ahead of the
        # projection: a day whose AAA scrape failed produces zero signals, and it
        # does not get a model fitted first.
        gate = self._scrape_gate()
        if not gate.allow:
            gate_reason = gate.reason_code or "-"
            # An unevaluatable gate and a gate that said no are different
            # operator problems, so they get different top-level reason codes
            # rather than one code with the difference buried in a field.
            reason = (
                REJECT_SCRAPE_GATE_ERROR
                if gate_reason == REJECT_SCRAPE_GATE_ERROR
                else REJECT_SCRAPE_GATE_BLOCKED
            )
            self._reject(
                reason,
                symbol,
                gate_reason=None if gate_reason == reason else gate_reason,
                age_days=gate.age_days,
                detail=gate.detail,
            )
            return []

        if self._series is None:
            self._reject(REJECT_SERIES_UNAVAILABLE, symbol)
            return []

        # --- bracket semantics from API fields, never the ticker --------
        if not extra or not hasattr(extra, "get"):
            self._reject(REJECT_NO_BRACKET_SPEC, symbol, detail="no extra fields")
            return []
        strike_type = str(extra.get("strike_type") or "").strip().lower()
        if not strike_type:
            self._reject(REJECT_NO_BRACKET_SPEC, symbol, detail="strike_type absent")
            return []
        if strike_type != SUPPORTED_STRIKE_TYPE:
            self._reject(
                REJECT_UNSUPPORTED_STRIKE_TYPE, symbol, strike_type=strike_type
            )
            return []
        floor_strike = _optional_float(extra.get("floor_strike"))
        if floor_strike is None:
            self._reject(REJECT_NO_BRACKET_SPEC, symbol, detail="floor_strike absent")
            return []

        # --- settlement date and the final-N-day window -----------------
        try:
            settlement_date, date_source = resolve_settlement_date(symbol, extra)
        except ValueError as exc:
            self._reject(REJECT_NO_SETTLEMENT_DATE, symbol, detail=str(exc))
            return []

        today = self.clock()
        lead_days = (settlement_date - today).days
        if not (0 < lead_days <= self.final_window_days):
            self._reject(
                REJECT_OUTSIDE_WINDOW,
                symbol,
                lead_days=lead_days,
                window_days=self.final_window_days,
                settlement_date=settlement_date.isoformat(),
                date_source=date_source,
            )
            return []

        # --- data freshness --------------------------------------------
        newest = self._newest_aaa_date()
        if newest is None:
            self._reject(REJECT_SERIES_UNAVAILABLE, symbol, detail="no AAA rows")
            return []
        staleness_days = (today - newest).days
        if staleness_days > self.max_data_age_days:
            self._reject(
                REJECT_DATA_STALE,
                symbol,
                staleness_days=staleness_days,
                max_age_days=self.max_data_age_days,
                newest_aaa=newest.isoformat(),
            )
            return []

        # --- projection ------------------------------------------------
        try:
            proj = self._projection(newest, settlement_date)
        except GasDataUnavailable as exc:
            self._reject(
                REJECT_PROJECTION_UNAVAILABLE,
                symbol,
                as_of=newest.isoformat(),
                target=settlement_date.isoformat(),
                detail=str(exc),
            )
            return []

        # --- quotes ----------------------------------------------------
        yes_bid = _quote(market_data.bid)
        yes_ask = _quote(market_data.ask)
        no_bid = _quote(extra.get("no_bid"))
        no_ask = _quote(extra.get("no_ask"))
        last = _quote(market_data.price)

        if (yes_bid is not None and yes_bid >= self.near_resolved_bid) or (
            yes_ask is not None and yes_ask <= self.near_resolved_ask
        ):
            self._reject(
                REJECT_NEAR_RESOLVED,
                symbol,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
            )
            return []

        reference = _reference_price(yes_bid, yes_ask, last)
        if reference is None:
            self._reject(
                REJECT_NO_USABLE_QUOTE,
                symbol,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                last=last,
                detail="no usable YES reference price",
            )
            return []
        market_price, price_source = reference

        # --- divergence (contract §3) ----------------------------------
        p_yes = prob_above(proj, floor_strike)
        divergence = p_yes - market_price
        spread = (
            yes_ask - yes_bid if (yes_ask is not None and yes_bid is not None) else None
        )
        if abs(divergence) < self.min_divergence:
            self._reject(
                REJECT_DIVERGENCE_BELOW_MIN,
                symbol,
                divergence=divergence,
                min_divergence=self.min_divergence,
                model_p_yes=p_yes,
                market_price=market_price,
                price_source=price_source,
                floor_strike=floor_strike,
                point=proj.point,
                sigma=proj.sigma,
                lead_days=lead_days,
            )
            return []

        buy_yes = divergence > 0
        contract_side = "YES" if buy_yes else "NO"
        p_win = p_yes if buy_yes else 1.0 - p_yes
        taker_price = yes_ask if buy_yes else no_ask
        maker_price = yes_bid if buy_yes else no_bid

        if taker_price is None or maker_price is None:
            self._reject(
                REJECT_NO_USABLE_QUOTE,
                symbol,
                contract=contract_side,
                taker_price=taker_price,
                maker_price=maker_price,
                detail=f"missing a {contract_side} quote to price both fee legs",
            )
            return []

        # --- fee-aware EV, both legs, gas fee schedule ------------------
        qty = self.base_quantity
        ev_taker = self._ev(symbol, p_win, taker_price, qty, is_maker=False)
        ev_maker = self._ev(symbol, p_win, maker_price, qty, is_maker=True)
        gated_ev = min(ev_taker, ev_maker)
        if gated_ev <= 0.0:
            self._reject(
                REJECT_EV_NOT_POSITIVE,
                symbol,
                contract=contract_side,
                ev_taker=ev_taker,
                ev_maker=ev_maker,
                taker_price=taker_price,
                maker_price=maker_price,
                p_win=p_win,
                quantity=qty,
                fee_type=fee_type_for_symbol(symbol),
            )
            return []

        entry_price = (
            taker_price if self.entry_mode == ENTRY_MODE_TAKER else maker_price
        )

        signal = TradeSignal(
            symbol=symbol,
            side="buy",
            # FR-4.3 "sized small": a fixed base quantity, never scaled by the
            # divergence. RiskManager caps it further; nothing here grows a
            # position because the model is more excited.
            quantity=qty,
            limit_price=entry_price,
            # Kelly sizing downstream reads confidence as P(win), so it must be
            # the model's probability for the side actually bought.
            confidence=max(0.01, min(0.99, p_win)),
            contract_side=contract_side,
            # Carried for the FR-4.4 settlement recorder. NOTE: 'greater' here
            # is the CONTINUOUS strict rule (value > floor), not the whole-degree
            # temperature rule in src.core.bracket_payoff. Gas outcomes are
            # judged by gas_projection.settles_yes_gas.
            strike_type=strike_type,
            floor_strike=floor_strike,
            cap_strike=_optional_float(extra.get("cap_strike")),
        )
        signal.expiration_time = datetime.combine(
            settlement_date, datetime.min.time(), tzinfo=MARKET_TZ
        ) + timedelta(hours=10)
        signal.gas_projection_point = proj.point
        signal.gas_projection_sigma = proj.sigma
        signal.gas_model_version = proj.model_version
        signal.gas_inputs_hash = proj.inputs_hash

        logger.info(
            "[Gas] ACCEPT %s contract=%s lead=%dd settlement=%s(%s) "
            "point=%.3f sigma=%.4f ci=[%.3f, %.3f] floor=%.3f model_p=%.4f "
            "market=%.4f(%s) divergence=%+.4f spread=%s entry=%.4f(%s) "
            "ev_taker=%+.5f ev_maker=%+.5f qty=%d fee_type=%s model=%s",
            symbol,
            contract_side,
            lead_days,
            settlement_date.isoformat(),
            date_source,
            proj.point,
            proj.sigma,
            proj.ci_low,
            proj.ci_high,
            floor_strike,
            p_yes,
            market_price,
            price_source,
            divergence,
            "n/a" if spread is None else f"{spread:.4f}",
            entry_price,
            self.entry_mode,
            ev_taker,
            ev_maker,
            qty,
            fee_type_for_symbol(symbol),
            proj.model_version,
        )
        return [signal]

    # -- internals -------------------------------------------------------

    def _scrape_gate(self) -> SignalGate:
        """The provider's gate verdict, reused for at most
        :data:`DEFAULT_GATE_CACHE_SECONDS` across one ladder pass.

        A gate that raises is converted to a **block**, never swallowed into a
        pass. The exception type and message ride along as the measured value, so
        a strategy that has gone silent because the failure record is unreadable
        is diagnosable from one INFO line.
        """
        now = time.monotonic()
        cached = self._gate_cache
        if cached is not None and (now - cached[0]) < self._gate_cache_seconds:
            return cached[1]
        try:
            gate = self._gate()
        except Exception as exc:  # noqa: BLE001 - any failure here must block
            gate = SignalGate(
                False,
                REJECT_SCRAPE_GATE_ERROR,
                f"gate evaluation raised {type(exc).__name__}: {exc}",
            )
        self._gate_cache = (now, gate)
        return gate

    def _projection(self, as_of: date, settlement_date: date) -> GasProjection:
        """Fit once per (as_of, settlement_date); reuse across the ladder."""
        key = (as_of, settlement_date)
        cached = self._projection_cache.get(key)
        if cached is None:
            cached = project(
                as_of, settlement_date, self._series, config=self.projection_config
            )
            self._projection_cache[key] = cached
        return cached

    def _newest_aaa_date(self) -> Optional[date]:
        series = self._series
        if series is None or not series.aaa:
            return None
        return max(obs.date for obs in series.aaa)

    def _ev(
        self, symbol: str, p_win: float, price: float, quantity: int, is_maker: bool
    ) -> float:
        """Per-contract EV after fees, one leg (held to settlement).

        The fee is Kalshi's quadratic rounded UP to the cent on the **order
        total**, so it is computed at the real contract count and divided back
        out. Charging ``compute_fee(price, 1, ...)`` per contract overstates the
        cost several-fold at small prices, which would reject live trades for a
        cost the exchange does not levy.
        """
        fee_total = compute_fee(
            price,
            max(1, int(quantity)),
            is_maker=is_maker,
            series_fee_type=fee_type_for_symbol(symbol),
        ).fee
        return float(p_win) - float(price) - fee_total / float(max(1, int(quantity)))

    def _reject(self, reason: str, symbol: str, **measured) -> None:
        log_rejection(reason, strategy=self.name(), symbol=symbol, **measured)


def _optional_float(value) -> Optional[float]:
    """Float or ``None``; rejects bools so ``True`` cannot become a $1.00 strike."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _quote(value) -> Optional[float]:
    """A usable Kalshi quote in ``(0, 1)``, else ``None``.

    Zero and one are absent-or-resolved markers on this API, not tradable
    prices, and a fee formula evaluated at either returns $0.00.
    """
    out = _optional_float(value)
    if out is None or out <= 0.0 or out >= 1.0:
        return None
    return out


def _reference_price(
    yes_bid: Optional[float], yes_ask: Optional[float], last: Optional[float]
) -> Optional[Tuple[float, str]]:
    """``(market YES price, source)`` for the contract §3 divergence gate.

    Mid when both sides are quoted, else the last trade, else the single quoted
    side. The source is logged on every accept and reject so a divergence
    measured against a one-sided book is never mistaken for one measured against
    a mid.
    """
    if yes_bid is not None and yes_ask is not None:
        return (yes_bid + yes_ask) / 2.0, "mid"
    if last is not None:
        return last, "last"
    if yes_bid is not None:
        return yes_bid, "bid_only"
    if yes_ask is not None:
        return yes_ask, "ask_only"
    return None


__all__ = [
    "DEFAULT_GATE_CACHE_SECONDS",
    "ENTRY_MODE_MAKER",
    "ENTRY_MODE_TAKER",
    "GAS_SERIES_TICKERS",
    "GasConvergenceStrategy",
    "MARKET_TZ",
    "REJECT_DATA_STALE",
    "REJECT_DIVERGENCE_BELOW_MIN",
    "REJECT_EV_NOT_POSITIVE",
    "REJECT_NEAR_RESOLVED",
    "REJECT_NO_BRACKET_SPEC",
    "REJECT_NO_SETTLEMENT_DATE",
    "REJECT_NO_USABLE_QUOTE",
    "REJECT_OUTSIDE_WINDOW",
    "REJECT_PROJECTION_UNAVAILABLE",
    "REJECT_SCRAPE_GATE_BLOCKED",
    "REJECT_SCRAPE_GATE_ERROR",
    "REJECT_SERIES_UNAVAILABLE",
    "REJECT_UNSUPPORTED_SERIES",
    "REJECT_UNSUPPORTED_STRIKE_TYPE",
    "SUPPORTED_STRIKE_TYPE",
    "default_signal_gate",
    "resolve_settlement_date",
    "series_ticker",
]
