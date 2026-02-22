import time
import threading
import os
import sys
import re

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.visualization.dashboard import Dashboard
from src.data.nws_provider import NWSProvider
from src.data.coinbase_provider import CoinbaseProvider
from src.data.kalshi_provider import KalshiProvider
from src.strategies.weather_strategy import WeatherArbitrageStrategy, WeatherArbitrageStrategyV2
from src.strategies.crypto_strategy import CryptoArbitrageStrategy, CryptoHourlyStrategy, Crypto15mTrendStrategy, Crypto15mTrendStrategyV2, CryptoLongShotFader
from src.core.interfaces import TradeSignal
from src.core.risk_manager import RiskManager
from src.utils.system_utils import prevent_sleep
from src.utils.logger import logger
import os
import copy
from datetime import datetime, timedelta

class OrchestratorEngine:
    def __init__(self):
        self.dashboard = Dashboard()
        self.running = True
        self.risk_manager = RiskManager(starting_balance=100.0)
        
        # Wire the trade-close callback to the dashboard for strategy tracking
        self.risk_manager.exchange.on_close = self._on_trade_close
        
        self.strategies = {
            "weather": WeatherArbitrageStrategyV2(), # UPGRADED TO V2
            "crypto": Crypto15mTrendStrategyV2(),   # UPGRADED TO V2
            "crypto_hr": CryptoHourlyStrategy(),
            "longshot": CryptoLongShotFader()       # NEW: Fade low-prob contracts
        }
        self.dashboard.active_strategies = list(self.strategies.keys())
        self.ticker_cache = {} # Cache resolved tickers: { "KXHIGHNY": "KXHIGHNY-26JAN30-T20" }
        
        # Initialize Providers
        # NWS
        nws_ua = os.getenv("NWS_USER_AGENT", "(MoneyPrinter, test@example.com)")
        # Updated Stations: KNYC (Central Park), KMDW (Midway), KLAX, KMIA
        self.nws_stations = ["KNYC", "KLAX", "KMDW", "KMIA"]
        self.nws = NWSProvider(nws_ua, self.nws_stations)
        
        # Coinbase
        self.coinbase = CoinbaseProvider("BTC-USD")
        
        # Kalshi (For Balance Sync & Live Price Discovery)
        k_id = os.getenv("KALSHI_KEY_ID")
        k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        k_url = os.getenv("KALSHI_API_URL")
        self.kalshi = None
        if k_id and k_key:
            # SAFETY: Always force read_only=True for now
            self.kalshi = KalshiProvider(k_id, k_key, k_url, read_only=True)

    def _on_trade_close(self, position: dict):
        """Callback from OMS when a trade is settled/closed. Reports result to dashboard."""
        strategy_name = position.get('strategy_name', 'Unknown')
        pnl = position.get('pnl', 0.0)
        self.dashboard.record_strategy_trade_result(strategy_name, pnl)
        logger.info(f"[Orchestrator] 📊 Strategy Result: {strategy_name} | PnL: ${pnl:+.2f}")

    def _resolve_smart_ticker(self, series_base, criteria="time"):
        """
        Dynamically finds the best market ticker for a given series.
        criteria="time": Finds nearest future expiration (for Crypto).
        criteria="sentiment": Finds market with highest YES price (for Weather).
        """
        # Check Cache (TTL 60s)
        cached = self.ticker_cache.get(series_base)
        if cached and (time.time() - cached['time'] < 60):
            return cached['ticker']

        if not self.kalshi: return None
        
        # --- SPECIAL CASE: KXBTCHOURLY (V1 Discovery) ---
        if series_base == "KXBTCHOURLY":
             try:
                 # Use the special V1 probe method we added to KalshiProvider
                 v1_markets = self.kalshi.fetch_btc_hourly_markets()
                 if v1_markets:
                     # 1. Group by Expiration (Find Soonest)
                     # Sort by close_time (ISO string)
                     # Filter out any that might have slipped through (just to be safe)
                     future_markets = v1_markets # Already filtered by provider
                     
                     if not future_markets: return None
                     
                     # Sort by time first
                     future_markets.sort(key=lambda x: x.extra.get('close_time', '9999'))
                     
                     # Get the soonest close time
                     soonest_time = future_markets[0].extra.get('close_time')
                     
                     # Filter for only this hour's markets
                     this_hour_markets = [m for m in future_markets if m.extra.get('close_time') == soonest_time]
                     
                     # 2. Find ATM Strike
                     # Get current Spot Price
                     spot_price = 50000.0 # Fallback
                     try:
                         cb_data = self.coinbase.fetch_latest()
                         if cb_data: spot_price = cb_data.price
                     except: pass
                     
                     def get_strike_diff(m):
                         try:
                             # Format: ...-T60999.99
                             strike_part = m.symbol.split('-')[-1]
                             strike_val = float(re.sub(r'[A-Za-z]', '', strike_part))
                             return abs(strike_val - spot_price)
                         except:
                             return 999999.0
                             
                     # Sort by proximity to spot
                     this_hour_markets.sort(key=get_strike_diff)
                     
                     best = this_hour_markets[0].symbol
                     
                     # Cache slightly longer since we did a heavy V1 scan? 
                     # Actually V1 scan is expensive (12 requests). We should cache this for maybe 5 mins?
                     # Existing cache is 60s. That's OK.
                     
                     self.ticker_cache[series_base] = {'ticker': best, 'time': time.time()}
                     logger.info(f"[Dashboard] Smart Resolve {series_base} -> {best} (ATM Strike @ {spot_price})")
                     return best
             except Exception as e:
                 logger.error(f"V1 Discovery Failed: {e}")
                 pass
        
        try:
            # 1. Fetch markets with PAGINATION (Kalshi API does NOT support status filtering)
            # High-volume series like KXBTC15M have 2000+ markets. The active one(s)
            # can be on any page. We paginate until we find active markets or exhaust pages.
            active_markets = []
            cursor = None
            for _ in range(5):  # Max 5 pages (~1000 markets) to prevent runaway
                params = {"series_ticker": series_base, "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                resp = self.kalshi.session.get(f"{self.kalshi.api_url}/markets", params=params)
                if resp.status_code != 200: break
                data = resp.json()
                page_markets = data.get('markets', [])
                active_markets.extend([m for m in page_markets if m.get('status') == 'active'])
                cursor = data.get('cursor')
                if not cursor or not page_markets:
                    break
                if active_markets:
                    break  # Found active markets, no need to keep paginating
            
            if not active_markets: return None

            best_ticker = None
            
            if criteria == "time":
                # Sort by expiration_time (soonest first)
                # Filter for active (already done)
                active_markets.sort(key=lambda x: x.get('expiration_time', '9999'))
                if active_markets:
                    best_ticker = active_markets[0].get('ticker')

            elif criteria == "sentiment":
                # Weather: Find highest YES price (Implied Probability)
                # Filter for TODAY or TOMORROW first to stay relevant
                now = datetime.now()
                target_dates = [now.strftime("%y%b%d").upper(), (now + timedelta(days=1)).strftime("%y%b%d").upper()]
                
                candidates = []
                for m in active_markets:
                    tick = m.get('ticker', '')
                    if any(d in tick for d in target_dates):
                        candidates.append(tick)
                
                # If no date match, check all (fallback)
                if not candidates: candidates = [m.get('ticker') for m in active_markets]
                
                highest_bid = -1.0
                winner = None
                
                # Find Highest Sentiment by fetching latest price for candidates
                # Rate limit safety: Limit to top 5 candidates? 
                # For now, we assume candidates list is small (usually ~5-10 strikes per day)
                for ticker in candidates:
                    data = self.kalshi.fetch_latest(ticker)
                    if data and data.bid > highest_bid:
                        highest_bid = data.bid
                        winner = ticker
                        
                best_ticker = winner

            if best_ticker:
                logger.info(f"[Dashboard] Smart Resolve {series_base} -> {best_ticker} ({criteria})")
                self.ticker_cache[series_base] = {'ticker': best_ticker, 'time': time.time()}
                return best_ticker
                
            return None
            
        except Exception as e:
            logger.error(f"Resolution Error ({series_base}): {e}")
            return None

    def _resolve_btc_ladder(self):
        """
        Resolves the 'Ladder' of BTC Hourly markets:
        1. Center (Closest to Spot)
        2. Lower (Center - $250)
        3. Upper (Center + $250)
        Returns a list of tickers, with Center first.
        """
        if not self.kalshi: return []
        
        try:
            # 1. Fetch V1 Markets
            markets = self.kalshi.fetch_btc_hourly_markets()
            if not markets:
                logger.warning("[Dashboard] No V1 BTC Markets found.")
                return []
            
            # 2. Filter for Soonest Expiration
            markets.sort(key=lambda x: x.extra.get('close_time', '9999'))
            soonest_time = markets[0].extra.get('close_time')
            this_hour_markets = [m for m in markets if m.extra.get('close_time') == soonest_time]
            
            if not this_hour_markets:
                logger.warning(f"[Dashboard] No markets found for soonest hour: {soonest_time}")
                return []
            
            # 3. Get Spot Price

            spot_price = 50000.0
            try:
                cb_data = self.coinbase.fetch_latest()
                if cb_data: spot_price = cb_data.price
            except: pass
            
            # 4. Find Center (Closest to Spot)
            def get_strike(m):
                try:
                    # KXBTCD-YYMMMDDHH-Txxxxx
                    parts = m.symbol.split('-')
                    strike_part = parts[-1]
                    return float(re.sub(r'[A-Za-z]', '', strike_part))
                except Exception as e:
                    logger.error(f"Strike Parse Error ({m.symbol}): {e}")
                    return -1.0
            
            # Parse all valid markets
            valid_markets = [] # (strike, market_obj)
            for m in this_hour_markets:
                s = get_strike(m)
                if s > 0: 
                    valid_markets.append((s, m))
                else:
                    logger.debug(f"Failed to parse strike for: {m.symbol}")
                
            if not valid_markets:
                logger.warning(f"[Dashboard] No valid strikes parsed from {len(this_hour_markets)} markets. Sample: {this_hour_markets[0].symbol}")
                return []
            
            # Sort by distance to spot

            valid_markets.sort(key=lambda x: abs(x[0] - spot_price))
            
            center_strike, center_market = valid_markets[0]
            
            # 5. Find Neighbors (+/- 250)
            ladder_tickers = [center_market.symbol]
            
            # We want specific targets: Center - 250, Center + 250
            targets = [center_strike - 250, center_strike + 250]
            
            for t in targets:
                # Find best match for this target (within small epsilon, e.g. 5.0)
                # re-using valid_markets
                match = next((m for s, m in valid_markets if abs(s - t) < 5.0), None)
                if match:
                    ladder_tickers.append(match.symbol)
            
            return ladder_tickers
            
        except Exception as e:
            logger.error(f"[Dashboard] Ladder Resolve Failed: {e}")
            return []

    def market_loop(self):
        """Background thread to fetch data and run strategies."""
        ticks = 0
        last_heartbeat = time.time()
        
        while self.running:
            try:
                # Heartbeat (Every 60s)
                if time.time() - last_heartbeat > 60:
                    self.dashboard.log("[System] Heartbeat: Market Loop is Alive.")
                    last_heartbeat = time.time()
                    
                # 0. Update Active Positions (PnL & Expiry)
                if self.risk_manager and self.kalshi:
                    # Snapshot positions to avoid modification during iteration issues
                    active_positions = list(self.risk_manager.exchange.positions)
                    for pos in active_positions:
                        symbol = pos['symbol']
                        
                        # Optimization: If we just fetched this symbol in the main loop, skip?
                        # For safety, let's just re-fetch or use cache if implemented.
                        # NWS/Crypto loops update market data, but we need to ensure ALL positions are covered.
                        
                        # If it's a Kalshi market
                        if "KX" in symbol:
                            try:
                                k_data = self.kalshi.fetch_latest(symbol)
                                if k_data:
                                    # Update Price & PnL in Risk Manager
                                    # This triggers 'update_market' in SimulatedExchange, which checks stops/expiry
                                    self.risk_manager.update_market_data(symbol, k_data.price)
                            except Exception:
                                pass
                                
                # 1. Fetch Crypto
                btc_data = self.coinbase.fetch_latest()
                if btc_data:
                    # Clear stale tickers from Dashboard to keep it clean (Basic Rotation)
                    # We'll just rely on the dashboard to overwrite if key matches, 
                    # but if ticker NAME changes (e.g. 15m expires), old one stays.
                    # TODO: Implement a clean sweep in Dashboard class.
                    
                    self.dashboard.update_price("BTC-USD (Coinbase)", btc_data.price)
                    
                    # Try to fetch Live Kalshi BTC Price (High Frequency 15M)
                    btc_15m_resolved = False
                    if self.kalshi:
                        try:
                            # A. Resolve 15M Ticker (TIME priority)
                            btc_15m = self._resolve_smart_ticker("KXBTC15M", criteria="time")
                            if btc_15m:
                                k_data_15 = self.kalshi.fetch_latest(btc_15m)
                                if k_data_15:
                                    self.dashboard.update_price(f"{btc_15m} (15m)", k_data_15.bid)
                                    # FUSE DATA for 15M Strategy
                                    original_spot = btc_data.price  # Save before overwriting
                                    btc_data.bid = k_data_15.bid
                                    btc_data.ask = k_data_15.ask
                                    btc_data.symbol = btc_15m
                                    # Inject spot price so TrendV2 RSI runs on real BTC prices
                                    if btc_data.extra is None:
                                        btc_data.extra = {}
                                    btc_data.extra['spot_price'] = original_spot
                                    self.risk_manager.update_market_data(btc_15m, btc_data.price)
                                    btc_15m_resolved = True
                            else:
                                # Ghost Ticker: No active KXBTC15M markets on Kalshi
                                if ticks % 60 == 0:  # Log every ~5 min to avoid spam
                                    logger.warning("[Dashboard] Ghost Ticker: No active KXBTC15M markets found. 15M strategy SKIPPED.")
                            
                            # B. Resolve HOURLY Ladder (Spot, -250, +250) - LIVE FEED
                            ladder = self._resolve_btc_ladder()
                            
                            if ladder:
                                # Update Dashboard with ALL 3 (or however many found)
                                for ticker in ladder:
                                    k_data_ladder = self.kalshi.fetch_latest(ticker)
                                    if k_data_ladder:
                                        self.dashboard.update_price(f"{ticker} (1h)", k_data_ladder.bid)
                                
                                # Use the CENTER one (First in list) for Strategy
                                center_ticker = ladder[0]
                                k_data_center = self.kalshi.fetch_latest(center_ticker)
                                
                                if k_data_center:
                                    btc_data_hr = copy.deepcopy(btc_data)
                                    btc_data_hr.bid = k_data_center.bid
                                    btc_data_hr.ask = k_data_center.ask
                                    btc_data_hr.symbol = center_ticker
                                    
                                    # Run Hourly Strategy
                                    hr_signals = self.strategies['crypto_hr'].analyze(btc_data_hr)
                                    self._process_signals(hr_signals, strategy_name="Crypto Hourly")

                            else:
                                if ticks % 60 == 0:
                                    logger.warning("[Dashboard] Ghost Ticker: No active BTC Hourly ladder found.")
                        
                        except Exception as e:
                            logger.error(f"Market Fetch Fail (BTC): {e}")
                    
                    # GATE: Only run 15M strategy if we have fused Kalshi option data
                    # Without this, the strategy receives raw BTC spot ($69k) and compares against 0.55
                    if btc_15m_resolved:
                        signals = self.strategies['crypto'].analyze(btc_data)
                        self._process_signals(signals, strategy_name="Trend Catcher V2")
                        # LongShot Fader: also check the 15m option for longshot pricing
                        ls_signals = self.strategies['longshot'].analyze(btc_data)
                        self._process_signals(ls_signals, strategy_name="LongShot Fader")
                else:
                    # Log failure occasionally
                    if ticks % 10 == 0:
                        self.dashboard.log("[System] ⚠️ Coinbase Fetch Failed (Network/Timeout)")

                # 2. Fetch Weather (Iterate all stations)
                # Map NWS Station -> Kalshi Series Ticker
                station_map = {
                    "KNYC": "KXHIGHNY",
                    "KLAX": "KXHIGHLAX",
                    "KMDW": "KXHIGHCHI",
                    "KMIA": "KXHIGHMIA"
                }

                for station in self.nws_stations:
                    nws_data = self.nws.fetch_latest(station)
                    if nws_data:
                        temp = nws_data.extra.get('temperature_f')
                        kalshi_ticker = station_map.get(station)
                        
                        # FETCH LIVE KALSHI PRICE
                        if self.kalshi and kalshi_ticker:
                            try:
                                # Resolve dynamic ticker (SENTIMENT priority)
                                active_ticker = self._resolve_smart_ticker(kalshi_ticker, criteria="sentiment")
                                
                                if active_ticker:
                                    k_data = self.kalshi.fetch_latest(active_ticker)
                                    if k_data:
                                        # Pass Max Temp to Dashboard
                                        max_t = nws_data.extra.get('max_temp_today_f')
                                        self.dashboard.update_price(f"{active_ticker} (Market)", k_data.bid, max_temp=max_t)
                                        
                                        # FUSE DATA: Inject price into NWS object for Strategy
                                        nws_data.bid = k_data.bid
                                        nws_data.ask = k_data.ask
                                        nws_data.price = k_data.price
                                        nws_data.symbol = active_ticker
                            except Exception as e:
                                logger.error(f"Market Fetch Fail ({kalshi_ticker}): {e}")
                        
                        if temp:
                            self.dashboard.update_price(f"{kalshi_ticker or station} (F)", temp)
                            # FEED OMS for Weather Exits (Use resolved ticker for PnL tracking)
                            if active_ticker:
                                self.risk_manager.update_market_data(active_ticker, temp)
                            else:
                                self.risk_manager.update_market_data(f"TEMP_{station}", temp)
                        
                        # Extract PoP for Precip Updates
                        forecasts = nws_data.extra.get('forecast') or []
                        pop_prob = 0.0
                        for period in forecasts:
                             if period.get('isDaytime'):
                                 val = period.get('probabilityOfPrecipitation', {}).get('value', 0)
                                 if val: pop_prob = val / 100.0
                                 break
                        
                        if pop_prob is not None:
                            # Use resolved ticker for PnL tracking if possible
                            if active_ticker:
                                self.risk_manager.update_market_data(f"{active_ticker}_PRECIP", pop_prob)
                            else:
                                self.risk_manager.update_market_data(f"PRECIP_{station}", pop_prob)
                        
                        signals = self.strategies['weather'].analyze(nws_data)
                        self._process_signals(signals, strategy_name="Meteorologist V1")
                    time.sleep(1) # 1 sec between cities

                time.sleep(5) # 5 second tick
                ticks += 1
                
            except Exception as e:
                self.dashboard.log(f"Error in loop: {str(e)}")
                time.sleep(5)

    def _is_weather_slot_full(self, symbol):
        """
        Checks if we already have an active trade for this City + Type.
        Limit: 1 active trade per City per Type (Temp/Precip).
        """
        city = "UNKNOWN"
        type_ = "TEMP"
        
        if "PRECIP" in symbol: type_ = "PRECIP"
        
        # Map Symbol to City
        if "NY" in symbol or "JFK" in symbol: city = "NY"
        elif "CHI" in symbol or "ORD" in symbol: city = "CHI"
        elif "LAX" in symbol: city = "LAX"
        elif "MIA" in symbol: city = "MIA"
        
        slot_key = f"{city}_{type_}"
        
        # Count active positions matching this slot
        count = 0
        if self.risk_manager and self.risk_manager.exchange:
            for pos in self.risk_manager.exchange.positions:
                p_sym = pos['symbol']
                p_city = "UNKNOWN"
                p_type = "TEMP"
                
                if "PRECIP" in p_sym: p_type = "PRECIP"
                
                if "NY" in p_sym or "JFK" in p_sym: p_city = "NY"
                elif "CHI" in p_sym or "ORD" in p_sym: p_city = "CHI"
                elif "LAX" in p_sym: p_city = "LAX"
                elif "MIA" in p_sym: p_city = "MIA"
                
                p_key = f"{p_city}_{p_type}"
                
                if p_key == slot_key:
                    count += 1
                    
        return count >= 1

    def _process_signals(self, signals, strategy_name=None):
        if not signals: return
        if not isinstance(signals, list): signals = [signals]
        
        for sig in signals:
            # Determine Category
            category = "general"
            if "BTC" in sig.symbol or "ETH" in sig.symbol: category = "crypto"
            elif "HIGH" in sig.symbol or "PRECIP" in sig.symbol or "TEMP" in sig.symbol: category = "weather"

            # WEATHER SLOT LIMIT CHECK
            if category == 'weather':
                if self._is_weather_slot_full(sig.symbol):
                    continue


            # DYNAMIC SIZING (FRACTIONAL KELLY)
            if sig.limit_price > 0:
                # Default confidence if not provided
                conf = getattr(sig, 'confidence', 0.55)
                if conf <= 0: conf = 0.55
                
                # Calculate optimal size (Risk Manager handles caps)
                kelly_qty = self.risk_manager.calculate_kelly_size(conf, sig.limit_price)
                
                # Respect Signal's requested quantity if it's explicitly LOWER than Kelly (e.g. exit signal)
                # But for entry, usually signal.qty is just a placeholder or max
                final_qty = kelly_qty
                
                # Update signal
                sig.quantity = final_qty
            
            # Calculate Cost / Collateral
            # User Protocol: Always deduct Price * Quantity (Treat Short as Buying Inverse)
            est_cost = sig.limit_price * sig.quantity
            
            # RISK CHECK
            is_safe = self.risk_manager.check_order(est_cost, category=category)
            
            if is_safe:
                # SAFE: Execute and Record
                # Notional is now same as Cost/Risk per user definition
                notional = sig.limit_price * sig.quantity
                self.dashboard.log(f"EXEC: {sig.side.upper()} {sig.quantity}x {sig.symbol} @ {sig.limit_price} | Debit: ${est_cost:.2f}")
                self.dashboard.record_signal(sig, status="EXECUTED", strategy_name=strategy_name)
                
                # Extract Risk Rules (Attached by Strategy)
                sl = getattr(sig, 'stop_loss', 0.0)
                tr = getattr(sig, 'trailing_rules', None)
                ex = getattr(sig, 'expiration_time', None)
                
                self.risk_manager.record_execution(est_cost, sig.symbol, sig.side, sig.quantity, sig.limit_price, stop_loss=sl, trailing_rules=tr, expiration_time=ex, strategy_name=strategy_name)
            
            else:
                # RISKY:
                # In Live Trading, we would block this.
                # In Data Harvest (Simulation), we WANT to record it to see if it would have won.
                # We log it differently but still save to CSV.
                self.dashboard.log(f"⚠️ HARVEST: {sig.symbol} (Risky but Recorded)")
                self.dashboard.record_signal(sig, status="HARVEST_ONLY", strategy_name=strategy_name)
                # We do NOT deduct balance in Risk Manager to avoid 'bust' simulation stopping the harvest

    def run(self):
        prevent_sleep()
        self.dashboard.log("System Initializing...")
        
        # Connect Providers
        if self.nws.connect():
            self.dashboard.log("NWS Connected")
        else:
            self.dashboard.alert("NWS Connection Failed")
            
        if self.coinbase.connect():
            self.dashboard.log("Coinbase Connected")
        else:
             self.dashboard.alert("Coinbase Connection Failed")

        # Initial Balance Sync (Piggy Bank Mode)
        if self.kalshi:
            try:
                bal = self.kalshi.get_balance()
                self.risk_manager.update_balance(bal)
                self.dashboard.log(f"Piggy Bank Initialized: ${bal:.2f}")
            except Exception as e:
                self.dashboard.alert(f"Balance Sync Failed: {e}")

        # Start Market Thread
        t = threading.Thread(target=self.market_loop)
        t.daemon = True
        t.start()

        self.dashboard.log("Trading Engine STARTED.")

        # Main UI Loop
        while self.running:
            self.dashboard.render(self.risk_manager)
            
            # Simple input handling (blocking) is tough with render loop
            # We use a non-blocking check or just slow refresh
            
            # For this demo, we just sleep and re-render
            # In a real CLI app we'd use 'curses' or 'prompt_toolkit'
            time.sleep(1)
            
            # Only way to exit cleanly without keyboard lib on some terminals is Ctrl+C
            # But let's look for a file-based signal or just Ctrl+C exception
            
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(override=True)
    
    engine = OrchestratorEngine()
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n[System] Shutdown Signal Received.")
