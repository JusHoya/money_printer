import time
import re
from datetime import datetime, timedelta
from typing import Dict
from src.core.interfaces import TradeSignal
from src.core.fee_calculator import trade_is_profitable
from src.core.risk_manager import MAX_CONTRACTS, RejectReason, log_rejection
from src.utils.logger import logger

# FR-0.4: mixins-local rejection reason codes — skips decided inside
# _process_signals itself (not by the RiskManager). Logged through the shared
# risk_manager.log_rejection() so the format and vocabulary stay uniform.
REASON_WEATHER_SLOT_FULL = "WEATHER_SLOT_FULL"
REASON_MISSING_LIMIT_PRICE = "MISSING_LIMIT_PRICE"


class TickerResolverMixin:
    """Shared ticker resolution logic for all bots."""

    def __init__(self):
        self.ticker_cache: Dict[str, dict] = {}

    def _resolve_smart_ticker(
        self, series_base, criteria="time", kalshi=None, coinbase=None
    ):
        """
        Dynamically finds the best market ticker for a given series.
        criteria="time": Finds nearest future expiration (for Crypto).
        criteria="sentiment": Finds market with highest YES price (for Weather).
        """
        cached = self.ticker_cache.get(series_base)
        if cached and (time.time() - cached["time"] < 60):
            return cached["ticker"]

        if not kalshi:
            return None

        # SPECIAL CASE: KXBTCHOURLY (V1 Discovery)
        if series_base == "KXBTCHOURLY":
            try:
                v1_markets = kalshi.fetch_btc_hourly_markets()
                if v1_markets:
                    future_markets = v1_markets
                    if not future_markets:
                        return None

                    future_markets.sort(key=lambda x: x.extra.get("close_time", "9999"))
                    soonest_time = future_markets[0].extra.get("close_time")
                    this_hour_markets = [
                        m
                        for m in future_markets
                        if m.extra.get("close_time") == soonest_time
                    ]

                    spot_price = 50000.0
                    if coinbase:
                        try:
                            cb_data = coinbase.fetch_latest()
                            if cb_data:
                                spot_price = cb_data.price
                        except Exception:
                            pass

                    def get_strike_diff(m):
                        try:
                            strike_part = m.symbol.split("-")[-1]
                            strike_val = float(re.sub(r"[A-Za-z]", "", strike_part))
                            return abs(strike_val - spot_price)
                        except Exception:
                            return 999999.0

                    this_hour_markets.sort(key=get_strike_diff)
                    best = this_hour_markets[0].symbol

                    self.ticker_cache[series_base] = {
                        "ticker": best,
                        "time": time.time(),
                    }
                    logger.info(
                        f"[Bot] Smart Resolve {series_base} -> {best} (ATM Strike @ {spot_price})"
                    )
                    return best
            except Exception as e:
                logger.error(f"V1 Discovery Failed: {e}")

        try:
            active_markets = []
            cursor = None
            for _ in range(5):
                params = {"series_ticker": series_base, "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                result = kalshi.search_markets(**params)
                if isinstance(result, tuple):
                    page_markets, cursor = result
                else:
                    page_markets, cursor = result, None
                if not page_markets:
                    break
                # Include 'active' and 'initialized' (pre-open) markets;
                # exclude 'closed', 'finalized', 'settled'
                active_markets.extend(
                    [
                        m
                        for m in page_markets
                        if m.get("status") in ("active", "initialized")
                    ]
                )
                if not cursor:
                    break
                if active_markets:
                    break

            if not active_markets:
                return None

            best_ticker = None

            if criteria == "time":
                active_markets.sort(key=lambda x: x.get("expiration_time", "9999"))
                if active_markets:
                    # For BTC 15m: multiple strikes share the same expiration.
                    # Group by soonest expiration and pick the ATM strike.
                    soonest_exp = active_markets[0].get("expiration_time")
                    same_exp = [
                        m
                        for m in active_markets
                        if m.get("expiration_time") == soonest_exp
                    ]

                    if len(same_exp) > 1 and coinbase:
                        spot_price = None
                        try:
                            cb_data = coinbase.fetch_latest()
                            if cb_data:
                                spot_price = cb_data.price
                        except Exception:
                            pass

                        if spot_price:

                            def _strike_dist(m):
                                fs = m.get("floor_strike")
                                if fs is not None:
                                    try:
                                        return abs(float(fs) - spot_price)
                                    except (TypeError, ValueError):
                                        pass
                                # Fallback: parse from ticker suffix
                                try:
                                    parts = m.get("ticker", "").split("-")
                                    val = float(re.sub(r"[A-Za-z]", "", parts[-1]))
                                    return abs(val - spot_price)
                                except Exception:
                                    return 999999.0

                            same_exp.sort(key=_strike_dist)
                            best = same_exp[0]
                            best_ticker = best.get("ticker")
                            fs = best.get("floor_strike", "?")
                            logger.info(
                                f"[Bot] ATM Resolve: {best_ticker} "
                                f"(strike={fs}, spot={spot_price:.0f}, "
                                f"{len(same_exp)} candidates)"
                            )
                        else:
                            best_ticker = same_exp[0].get("ticker")
                    else:
                        best_ticker = active_markets[0].get("ticker")

            elif criteria == "sentiment":
                now = datetime.now()
                target_dates = [
                    now.strftime("%y%b%d").upper(),
                    (now + timedelta(days=1)).strftime("%y%b%d").upper(),
                ]

                candidates = []
                for m in active_markets:
                    tick = m.get("ticker", "")
                    if any(d in tick for d in target_dates):
                        candidates.append(tick)

                if not candidates:
                    candidates = [m.get("ticker") for m in active_markets]

                highest_bid = -1.0
                winner = None

                for ticker in candidates:
                    data = kalshi.fetch_latest(ticker)
                    if data and data.bid > highest_bid:
                        highest_bid = data.bid
                        winner = ticker

                best_ticker = winner

            if best_ticker:
                logger.info(
                    f"[Bot] Smart Resolve {series_base} -> {best_ticker} ({criteria})"
                )
                self.ticker_cache[series_base] = {
                    "ticker": best_ticker,
                    "time": time.time(),
                }
                return best_ticker

            return None

        except Exception as e:
            logger.error(f"Resolution Error ({series_base}): {e}")
            return None

    def _resolve_btc_ladder(self, kalshi=None, coinbase=None):
        """
        Resolves the 'Ladder' of BTC Hourly markets:
        Center (closest to spot), Lower (-$250), Upper (+$250).
        """
        if not kalshi:
            return []

        try:
            markets = kalshi.fetch_btc_hourly_markets()
            if not markets:
                logger.warning("[Bot] No V1 BTC Markets found.")
                return []

            markets.sort(key=lambda x: x.extra.get("close_time", "9999"))
            soonest_time = markets[0].extra.get("close_time")
            this_hour_markets = [
                m for m in markets if m.extra.get("close_time") == soonest_time
            ]

            if not this_hour_markets:
                return []

            spot_price = 50000.0
            if coinbase:
                try:
                    cb_data = coinbase.fetch_latest()
                    if cb_data:
                        spot_price = cb_data.price
                except Exception:
                    pass

            def get_strike(m):
                try:
                    parts = m.symbol.split("-")
                    strike_part = parts[-1]
                    return float(re.sub(r"[A-Za-z]", "", strike_part))
                except Exception:
                    return -1.0

            valid_markets = []
            for m in this_hour_markets:
                s = get_strike(m)
                if s > 0:
                    valid_markets.append((s, m))

            if not valid_markets:
                return []

            valid_markets.sort(key=lambda x: abs(x[0] - spot_price))
            center_strike, center_market = valid_markets[0]

            ladder_tickers = [center_market.symbol]
            targets = [center_strike - 250, center_strike + 250]

            for t in targets:
                match = next((m for s, m in valid_markets if abs(s - t) < 150.0), None)
                if match:
                    ladder_tickers.append(match.symbol)

            return ladder_tickers

        except Exception as e:
            logger.error(f"[Bot] Ladder Resolve Failed: {e}")
            return []


class SignalProcessorMixin:
    """Shared signal processing logic for the orchestrator.

    Sprint 3 addition: ML gating layer rejects signals whose
    expected value is negative after estimated fees. The gate prices the
    taker path — see :meth:`_ml_ev_gate` for why assuming a maker fill
    silently disarmed it once the maker multiplier was corrected to zero.
    """

    @staticmethod
    def _ml_ev_gate(sig: TradeSignal) -> bool:
        """Return True if the signal has positive expected value after fees.

        Uses the signal's own ``confidence`` as P(win) and the ``limit_price``
        as the cost, and prices the fee **pessimistically** on both axes:

        - **Taker, not maker.** A pre-trade gate does not know how the order
          will fill; a resting order can be crossed before it rests. Pricing
          the maker path is doubly wrong since PRD Phase 2 corrected the maker
          multiplier to zero on the standard schedule: the fee term vanished
          and this gate collapsed to ``confidence > limit_price``, admitting a
          sub-basis-point edge (``conf=0.5001`` at ``lp=0.50``). The taker
          formula is uniform across every series, so charging it also makes the
          gate independent of series identity — it cannot under-charge a
          ``KXAAAGASM`` order the way a series-blind maker price would.
        - **Round trip, not settlement.** The gate is symbol-agnostic and has
          no evidence the position will be held to expiry, so it charges both
          legs. A position that *is* held to settlement pays one leg (Kalshi
          levies no settlement fee) and therefore beats this estimate.

        Both choices err toward rejecting a marginal trade rather than booking
        one whose modelled edge is smaller than its true cost.
        """
        conf = getattr(sig, "confidence", 0.0)
        lp = sig.limit_price
        if lp is None or lp <= 0:
            return False
        return trade_is_profitable(conf, lp, contracts=1, is_maker=False)

    def _is_weather_slot_full(self, symbol, risk_manager):
        """Check if we already have an active trade for this City + Type."""
        city = "UNKNOWN"
        type_ = "TEMP"

        if "PRECIP" in symbol:
            type_ = "PRECIP"

        if "NY" in symbol or "JFK" in symbol:
            city = "NY"
        elif "CHI" in symbol or "ORD" in symbol:
            city = "CHI"
        elif "LAX" in symbol:
            city = "LAX"
        elif "MIA" in symbol:
            city = "MIA"

        slot_key = f"{city}_{type_}"

        count = 0
        if risk_manager and risk_manager.exchange:
            for pos in risk_manager.exchange.positions:
                p_sym = pos["symbol"]
                p_city = "UNKNOWN"
                p_type = "TEMP"

                if "PRECIP" in p_sym:
                    p_type = "PRECIP"

                if "NY" in p_sym or "JFK" in p_sym:
                    p_city = "NY"
                elif "CHI" in p_sym or "ORD" in p_sym:
                    p_city = "CHI"
                elif "LAX" in p_sym:
                    p_city = "LAX"
                elif "MIA" in p_sym:
                    p_city = "MIA"

                if f"{p_city}_{p_type}" == slot_key:
                    count += 1

        return count >= 1

    def _process_signals(self, signals, strategy_name, risk_manager, dashboard):
        """Process signals through risk management and execute if safe.

        FR-0.4 logging contract: every emitted signal logs one INFO line
        ([Signal] EMIT ...), and then either EXECUTES (logged at INFO) or
        produces EXACTLY ONE INFO rejection line with a stable reason code:

        - mixins-local skips log here via log_rejection()
          (WEATHER_SLOT_FULL, MISSING_LIMIT_PRICE, EV_GATE);
        - Kelly zero-sizing logs KELLY_ZERO inside
          RiskManager.calculate_kelly_size — NOT re-logged here;
        - risk rejections log their reason inside RiskManager.check_order —
          NOT re-logged here.
        """
        if not signals:
            return False
        if not isinstance(signals, list):
            signals = [signals]
        traded = False

        for sig in signals:
            conf = getattr(sig, "confidence", 0.0)

            # FR-0.4: every signal a strategy emits is visible at INFO.
            logger.info(
                "[Signal] EMIT strategy=%s symbol=%s side=%s contract=%s "
                "price=%s qty=%s confidence=%.3f",
                strategy_name,
                sig.symbol,
                sig.side,
                getattr(sig, "contract_side", "YES"),
                sig.limit_price,
                sig.quantity,
                conf,
            )

            # MENTION is checked FIRST: the last ticker segment of a
            # KX*MENTION market is an arbitrary spoken word
            # (KXTRUMPMENTION-26AUG27-ETHEREUM), so the series marker must win
            # over any word-suffix collision with the crypto/weather
            # substrings. KXBTCY/KXETHY annual ladders classify as crypto via
            # the existing "BTC"/"ETH" substrings.
            category = "general"
            if "MENTION" in sig.symbol:
                category = "mention"
            elif "BTC" in sig.symbol or "ETH" in sig.symbol:
                category = "crypto"
            elif "HIGH" in sig.symbol or "PRECIP" in sig.symbol or "TEMP" in sig.symbol:
                category = "weather"

            if category == "weather":
                if self._is_weather_slot_full(sig.symbol, risk_manager):
                    log_rejection(
                        REASON_WEATHER_SLOT_FULL,
                        strategy_name,
                        sig.symbol,
                        side=sig.side,
                        price=sig.limit_price,
                        quantity=sig.quantity,
                    )
                    continue

            # Missing-field abort (FR-0.4): without a usable limit price the
            # signal can be neither sized nor EV-checked — reject explicitly
            # instead of falling through (abort-on-missing-critical-input).
            if sig.limit_price is None or sig.limit_price <= 0:
                log_rejection(
                    REASON_MISSING_LIMIT_PRICE,
                    strategy_name,
                    sig.symbol,
                    side=sig.side,
                    price=sig.limit_price,
                    quantity=sig.quantity,
                )
                continue

            # Dynamic sizing (Fractional Kelly)
            conf_for_sizing = conf if conf > 0 else 0.55
            kelly_qty = risk_manager.calculate_kelly_size(
                conf_for_sizing, sig.limit_price, strategy_name, symbol=sig.symbol
            )
            # FR-F0.4 (2026-09-02): clamp to the exchange's per-entry cap HERE
            # so the cost estimate, check_order and the EXECUTED log line all
            # see the quantity that record_execution will actually book.
            # Kelly can return up to 75; record_execution caps at 50.
            sig.quantity = min(kelly_qty, MAX_CONTRACTS)

            if sig.quantity < 1:
                # KELLY_ZERO was already logged at INFO inside
                # calculate_kelly_size — do not emit a second rejection line.
                continue

            # ML EV gating: reject signals with negative EV after fees
            if not self._ml_ev_gate(sig):
                log_rejection(
                    RejectReason.EV_GATE,
                    strategy_name,
                    sig.symbol,
                    side=sig.side,
                    price=sig.limit_price,
                    quantity=sig.quantity,
                    confidence=conf,
                )
                continue

            # Cost calculation
            if sig.side == "sell" and getattr(sig, "contract_side", "YES") == "YES":
                est_cost = (1.0 - sig.limit_price) * sig.quantity
            else:
                est_cost = sig.limit_price * sig.quantity

            ex = getattr(sig, "expiration_time", None)

            # Counter-trade bypass
            is_counter = getattr(sig, "is_counter_trade", False)
            if is_counter:
                saved_last_trade = risk_manager.last_trade_time
                saved_cooldowns = dict(risk_manager.loss_cooldown)
                risk_manager.last_trade_time = datetime.min
                risk_manager.loss_cooldown.clear()

            # A False return logs exactly one INFO rejection line (with its
            # reason code and this signal context) inside check_order.
            is_safe = risk_manager.check_order(
                est_cost,
                category=category,
                strategy_name=strategy_name,
                expiration_time=ex,
                symbol=sig.symbol,
                side=sig.side,
                price=sig.limit_price,
                quantity=sig.quantity,
            )

            if is_counter:
                risk_manager.last_trade_time = saved_last_trade
                risk_manager.loss_cooldown = saved_cooldowns

            if is_safe:
                cs_label = getattr(sig, "contract_side", "YES")
                logger.info(
                    "[Signal] EXECUTED strategy=%s symbol=%s side=%s contract=%s "
                    "price=%s qty=%s cost=%.2f confidence=%.3f",
                    strategy_name,
                    sig.symbol,
                    sig.side,
                    cs_label,
                    sig.limit_price,
                    sig.quantity,
                    est_cost,
                    conf,
                )
                dashboard.log(
                    f"EXEC: {sig.side.upper()} {cs_label} {sig.quantity}x {sig.symbol} @ {sig.limit_price} | Debit: ${est_cost:.2f}"
                )
                dashboard.record_signal(
                    sig, status="EXECUTED", strategy_name=strategy_name
                )

                sl = getattr(sig, "stop_loss", 0.0)
                tr = getattr(sig, "trailing_rules", None)
                ex = getattr(sig, "expiration_time", None)
                cs = getattr(sig, "contract_side", "YES")
                dpt = getattr(sig, "disable_profit_targets", False)
                # Extract real strike for position tracking (avoids
                # tanh mispricing from ticker index suffixes)
                strike_val = getattr(sig, "strike", None)
                risk_manager.record_execution(
                    est_cost,
                    sig.symbol,
                    sig.side,
                    sig.quantity,
                    sig.limit_price,
                    stop_loss=sl,
                    trailing_rules=tr,
                    expiration_time=ex,
                    strategy_name=strategy_name,
                    contract_side=cs,
                    disable_profit_targets=dpt,
                    strike=strike_val,
                    # PRD FR-1.1/FR-1.2: carry the API bracket semantics from
                    # the signal onto the position so settlement can use
                    # bracket_payoff instead of inferring from the ticker.
                    strike_type=getattr(sig, "strike_type", None),
                    floor_strike=getattr(sig, "floor_strike", None),
                    cap_strike=getattr(sig, "cap_strike", None),
                    # Fill type, when a strategy or router can state it. No
                    # component in the live loop can today, so this is None and
                    # the exchange books the fill as a taker rather than as the
                    # free maker side.
                    is_maker=getattr(sig, "is_maker", None),
                )
                # Store ML context in position for trade journal
                positions = risk_manager.exchange.positions
                if positions:
                    positions[-1]["ml_context"] = {
                        "model_probability": getattr(sig, "model_probability", None),
                        "model_confidence": sig.confidence,
                        "model_used": getattr(sig, "model_used", None),
                        "btc_spot": getattr(sig, "btc_spot", None),
                        "tte_at_entry": getattr(sig, "tte_at_entry", None),
                        "nws_forecast_high": getattr(sig, "nws_forecast_high", None),
                    }
                traded = True
            else:
                # The INFO rejection line (with reason code) was already
                # emitted inside check_order — only dashboard bookkeeping here.
                dashboard.log(f"⚠️ HARVEST: {sig.symbol} (Risky but Recorded)")
                dashboard.record_signal(
                    sig, status="HARVEST_ONLY", strategy_name=strategy_name
                )

        return traded
