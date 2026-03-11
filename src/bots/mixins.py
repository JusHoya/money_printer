import time
import re
import copy
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from src.core.interfaces import TradeSignal, MarketData
from src.utils.logger import logger


class TickerResolverMixin:
    """Shared ticker resolution logic for all bots."""

    def __init__(self):
        self.ticker_cache: Dict[str, dict] = {}

    def _resolve_smart_ticker(self, series_base, criteria="time", kalshi=None, coinbase=None):
        """
        Dynamically finds the best market ticker for a given series.
        criteria="time": Finds nearest future expiration (for Crypto).
        criteria="sentiment": Finds market with highest YES price (for Weather).
        """
        cached = self.ticker_cache.get(series_base)
        if cached and (time.time() - cached['time'] < 60):
            return cached['ticker']

        if not kalshi: return None

        # SPECIAL CASE: KXBTCHOURLY (V1 Discovery)
        if series_base == "KXBTCHOURLY":
            try:
                v1_markets = kalshi.fetch_btc_hourly_markets()
                if v1_markets:
                    future_markets = v1_markets
                    if not future_markets: return None

                    future_markets.sort(key=lambda x: x.extra.get('close_time', '9999'))
                    soonest_time = future_markets[0].extra.get('close_time')
                    this_hour_markets = [m for m in future_markets if m.extra.get('close_time') == soonest_time]

                    spot_price = 50000.0
                    if coinbase:
                        try:
                            cb_data = coinbase.fetch_latest()
                            if cb_data: spot_price = cb_data.price
                        except: pass

                    def get_strike_diff(m):
                        try:
                            strike_part = m.symbol.split('-')[-1]
                            strike_val = float(re.sub(r'[A-Za-z]', '', strike_part))
                            return abs(strike_val - spot_price)
                        except:
                            return 999999.0

                    this_hour_markets.sort(key=get_strike_diff)
                    best = this_hour_markets[0].symbol

                    self.ticker_cache[series_base] = {'ticker': best, 'time': time.time()}
                    logger.info(f"[Bot] Smart Resolve {series_base} -> {best} (ATM Strike @ {spot_price})")
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
                resp = kalshi.session.get(f"{kalshi.api_url}/markets", params=params)
                if resp.status_code != 200: break
                data = resp.json()
                page_markets = data.get('markets', [])
                active_markets.extend([m for m in page_markets if m.get('status') == 'active'])
                cursor = data.get('cursor')
                if not cursor or not page_markets:
                    break
                if active_markets:
                    break

            if not active_markets: return None

            best_ticker = None

            if criteria == "time":
                active_markets.sort(key=lambda x: x.get('expiration_time', '9999'))
                if active_markets:
                    best_ticker = active_markets[0].get('ticker')

            elif criteria == "sentiment":
                now = datetime.now()
                target_dates = [now.strftime("%y%b%d").upper(), (now + timedelta(days=1)).strftime("%y%b%d").upper()]

                candidates = []
                for m in active_markets:
                    tick = m.get('ticker', '')
                    if any(d in tick for d in target_dates):
                        candidates.append(tick)

                if not candidates: candidates = [m.get('ticker') for m in active_markets]

                highest_bid = -1.0
                winner = None

                for ticker in candidates:
                    data = kalshi.fetch_latest(ticker)
                    if data and data.bid > highest_bid:
                        highest_bid = data.bid
                        winner = ticker

                best_ticker = winner

            if best_ticker:
                logger.info(f"[Bot] Smart Resolve {series_base} -> {best_ticker} ({criteria})")
                self.ticker_cache[series_base] = {'ticker': best_ticker, 'time': time.time()}
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
        if not kalshi: return []

        try:
            markets = kalshi.fetch_btc_hourly_markets()
            if not markets:
                logger.warning("[Bot] No V1 BTC Markets found.")
                return []

            markets.sort(key=lambda x: x.extra.get('close_time', '9999'))
            soonest_time = markets[0].extra.get('close_time')
            this_hour_markets = [m for m in markets if m.extra.get('close_time') == soonest_time]

            if not this_hour_markets:
                return []

            spot_price = 50000.0
            if coinbase:
                try:
                    cb_data = coinbase.fetch_latest()
                    if cb_data: spot_price = cb_data.price
                except: pass

            def get_strike(m):
                try:
                    parts = m.symbol.split('-')
                    strike_part = parts[-1]
                    return float(re.sub(r'[A-Za-z]', '', strike_part))
                except:
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
                match = next((m for s, m in valid_markets if abs(s - t) < 5.0), None)
                if match:
                    ladder_tickers.append(match.symbol)

            return ladder_tickers

        except Exception as e:
            logger.error(f"[Bot] Ladder Resolve Failed: {e}")
            return []


class SignalProcessorMixin:
    """Shared signal processing logic for the orchestrator."""

    def _is_weather_slot_full(self, symbol, risk_manager):
        """Check if we already have an active trade for this City + Type."""
        city = "UNKNOWN"
        type_ = "TEMP"

        if "PRECIP" in symbol: type_ = "PRECIP"

        if "NY" in symbol or "JFK" in symbol: city = "NY"
        elif "CHI" in symbol or "ORD" in symbol: city = "CHI"
        elif "LAX" in symbol: city = "LAX"
        elif "MIA" in symbol: city = "MIA"

        slot_key = f"{city}_{type_}"

        count = 0
        if risk_manager and risk_manager.exchange:
            for pos in risk_manager.exchange.positions:
                p_sym = pos['symbol']
                p_city = "UNKNOWN"
                p_type = "TEMP"

                if "PRECIP" in p_sym: p_type = "PRECIP"

                if "NY" in p_sym or "JFK" in p_sym: p_city = "NY"
                elif "CHI" in p_sym or "ORD" in p_sym: p_city = "CHI"
                elif "LAX" in p_sym: p_city = "LAX"
                elif "MIA" in p_sym: p_city = "MIA"

                if f"{p_city}_{p_type}" == slot_key:
                    count += 1

        return count >= 1

    def _process_signals(self, signals, strategy_name, risk_manager, dashboard):
        """Process signals through risk management and execute if safe."""
        if not signals: return False
        if not isinstance(signals, list): signals = [signals]
        traded = False

        for sig in signals:
            category = "general"
            if "BTC" in sig.symbol or "ETH" in sig.symbol: category = "crypto"
            elif "HIGH" in sig.symbol or "PRECIP" in sig.symbol or "TEMP" in sig.symbol: category = "weather"

            if category == 'weather':
                if self._is_weather_slot_full(sig.symbol, risk_manager):
                    continue

            # Dynamic sizing (Fractional Kelly)
            if sig.limit_price > 0:
                conf = getattr(sig, 'confidence', 0.55)
                if conf <= 0: conf = 0.55
                kelly_qty = risk_manager.calculate_kelly_size(conf, sig.limit_price)
                sig.quantity = kelly_qty

            if sig.quantity < 1:
                logger.debug(f"[Process] Skipping qty=0 signal for {sig.symbol}")
                continue

            # Cost calculation
            if sig.side == 'sell' and getattr(sig, 'contract_side', 'YES') == 'YES':
                est_cost = (1.0 - sig.limit_price) * sig.quantity
            else:
                est_cost = sig.limit_price * sig.quantity

            ex = getattr(sig, 'expiration_time', None)

            # Counter-trade bypass
            is_counter = getattr(sig, 'is_counter_trade', False)
            if is_counter:
                saved_last_trade = risk_manager.last_trade_time
                saved_cooldowns = dict(risk_manager.loss_cooldown)
                risk_manager.last_trade_time = datetime.min
                risk_manager.loss_cooldown.clear()

            is_safe = risk_manager.check_order(est_cost, category=category, strategy_name=strategy_name, expiration_time=ex)

            if is_counter and not is_safe:
                risk_manager.last_trade_time = saved_last_trade
                risk_manager.loss_cooldown = saved_cooldowns

            if is_safe:
                cs_label = getattr(sig, 'contract_side', 'YES')
                dashboard.log(f"EXEC: {sig.side.upper()} {cs_label} {sig.quantity}x {sig.symbol} @ {sig.limit_price} | Debit: ${est_cost:.2f}")
                dashboard.record_signal(sig, status="EXECUTED", strategy_name=strategy_name)

                sl = getattr(sig, 'stop_loss', 0.0)
                tr = getattr(sig, 'trailing_rules', None)
                ex = getattr(sig, 'expiration_time', None)
                cs = getattr(sig, 'contract_side', 'YES')
                dpt = getattr(sig, 'disable_profit_targets', False)
                risk_manager.record_execution(est_cost, sig.symbol, sig.side, sig.quantity, sig.limit_price, stop_loss=sl, trailing_rules=tr, expiration_time=ex, strategy_name=strategy_name, contract_side=cs, disable_profit_targets=dpt)
                traded = True
            else:
                dashboard.log(f"⚠️ HARVEST: {sig.symbol} (Risky but Recorded)")
                dashboard.record_signal(sig, status="HARVEST_ONLY", strategy_name=strategy_name)

        return traded
