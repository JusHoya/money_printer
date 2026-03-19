"""
Mock data providers for offline simulation and testing.

Generates realistic synthetic data using mathematical models:
- BTC prices via Geometric Brownian Motion with Poisson jump diffusion
- Kalshi contract prices via Black-Scholes-like probability model
- Weather observations via sinusoidal daily cycle with noise

Sprint 1, Task 1.8 — No network access required.
"""

import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import norm

from src.core.interfaces import DataProvider, MarketData

logger = logging.getLogger(__name__)


# ======================================================================
# Helper: BTC price path generation
# ======================================================================


def generate_btc_price_path(
    n_steps: int,
    dt: float = 1.0,
    initial_price: float = 70_000.0,
    drift: float = 0.0001,
    volatility: float = 0.002,
    jump_intensity: float = 0.01,
    jump_size: float = 0.005,
    seed: Optional[int] = None,
) -> List[float]:
    """Generate a realistic BTC price path using GBM with jump diffusion.

    Model: dS/S = mu*dt + sigma*dW + J*dN

    Parameters
    ----------
    n_steps : int
        Number of price steps to generate.
    dt : float
        Time increment per step (in arbitrary units; 1.0 = one tick).
    initial_price : float
        Starting BTC price in USD.
    drift : float
        Annualised drift (mu) per step. Positive = upward bias.
    volatility : float
        Volatility (sigma) per step. Controls normal price fluctuation.
    jump_intensity : float
        Poisson intensity (lambda) — probability of a jump each step.
    jump_size : float
        Mean absolute jump magnitude as a fraction of price.
    seed : int, optional
        Random seed for reproducibility.

    Returns
    -------
    List[float]
        Price path of length *n_steps* starting from *initial_price*.
    """
    rng = np.random.default_rng(seed)
    prices = np.empty(n_steps)
    prices[0] = initial_price

    sqrt_dt = math.sqrt(dt)

    for i in range(1, n_steps):
        # Diffusion component (GBM)
        z = rng.standard_normal()
        diffusion = (drift - 0.5 * volatility**2) * dt + volatility * sqrt_dt * z

        # Jump component (compound Poisson)
        n_jumps = rng.poisson(jump_intensity * dt)
        jump = 0.0
        if n_jumps > 0:
            # Each jump: lognormal with mean=jump_size, slight asymmetry
            jump_returns = rng.normal(0, jump_size, size=n_jumps)
            jump = float(np.sum(jump_returns))

        prices[i] = prices[i - 1] * math.exp(diffusion + jump)

    return prices.tolist()


# ======================================================================
# MockCoinbaseProvider — synthetic BTC-USD
# ======================================================================


class MockCoinbaseProvider(DataProvider):
    """Simulate BTC-USD prices via Geometric Brownian Motion with jumps.

    Each ``fetch_latest()`` call advances the internal simulation by one
    tick.  The full price history is stored for strategy look-back.

    Parameters
    ----------
    initial_price : float
        Starting BTC-USD price.
    drift : float
        Per-step drift (mu).
    volatility : float
        Per-step volatility (sigma).
    jump_intensity : float
        Poisson jump arrival rate per step.
    jump_size : float
        Mean jump magnitude (fraction of price).
    spread_bps : float
        Half-spread in basis points of price (default 1 bp = 0.01%).
    seed : int, optional
        RNG seed for reproducibility.
    """

    def __init__(
        self,
        product_id: str = "BTC-USD",
        initial_price: float = 70_000.0,
        drift: float = 0.0001,
        volatility: float = 0.002,
        jump_intensity: float = 0.01,
        jump_size: float = 0.005,
        spread_bps: float = 1.0,
        seed: Optional[int] = None,
    ):
        self.product_id = product_id
        self._initial_price = initial_price
        self._drift = drift
        self._volatility = volatility
        self._jump_intensity = jump_intensity
        self._jump_size = jump_size
        self._spread_bps = spread_bps
        self._seed = seed

        self._rng = np.random.default_rng(seed)
        self._price: float = initial_price
        self._price_history: List[float] = [initial_price]
        self._step: int = 0
        self._connected: bool = False
        self._start_time: datetime = datetime.now(timezone.utc)

    # ---- DataProvider interface ----

    def connect(self) -> bool:
        self._connected = True
        logger.info(
            "[MockCoinbase] Connected (simulated). Initial price=%.2f",
            self._initial_price,
        )
        return True

    def fetch_latest(self, symbol: str = None) -> MarketData:
        """Advance simulation by one step and return the new price."""
        self._step += 1

        # --- GBM with jump diffusion ---
        sqrt_dt = 1.0
        z = float(self._rng.standard_normal())
        diffusion = (
            self._drift - 0.5 * self._volatility**2
        ) + self._volatility * sqrt_dt * z

        n_jumps = int(self._rng.poisson(self._jump_intensity))
        jump = 0.0
        if n_jumps > 0:
            jump_returns = self._rng.normal(0, self._jump_size, size=n_jumps)
            jump = float(np.sum(jump_returns))

        self._price = self._price * math.exp(diffusion + jump)
        self._price_history.append(self._price)

        # --- Bid / Ask spread ---
        half_spread = self._price * (self._spread_bps / 10_000.0)
        bid = self._price - half_spread
        ask = self._price + half_spread

        # --- Synthetic volume (mean-reverting with noise) ---
        base_volume = 150.0
        volume = max(0.0, base_volume + float(self._rng.normal(0, 40)))

        target_sym = symbol if symbol else self.product_id
        ts = self._start_time + timedelta(seconds=self._step)

        return MarketData(
            symbol=target_sym,
            timestamp=ts,
            price=round(self._price, 2),
            volume=round(volume, 4),
            bid=round(bid, 2),
            ask=round(ask, 2),
            extra={
                "source": "mock_coinbase",
                "step": self._step,
                "drift": self._drift,
                "volatility": self._volatility,
            },
        )

    # ---- Convenience accessors ----

    @property
    def price_history(self) -> List[float]:
        """Full list of generated prices (including initial)."""
        return list(self._price_history)

    @property
    def current_price(self) -> float:
        return self._price

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset simulation to initial state."""
        self._rng = np.random.default_rng(seed if seed is not None else self._seed)
        self._price = self._initial_price
        self._price_history = [self._initial_price]
        self._step = 0
        self._start_time = datetime.now(timezone.utc)


# ======================================================================
# MockKalshiProvider — synthetic Kalshi contract prices
# ======================================================================


class MockKalshiProvider(DataProvider):
    """Generate realistic Kalshi contract prices that respond to an
    underlying asset price.

    For BTC 15-minute contracts the yes-price is derived from a
    Black-Scholes-inspired probability::

        P(above_strike) = norm.cdf((current - strike) / (vol * sqrt(T)))

    Bid/ask spread is randomised between 1-3 cents.  Market status
    advances through a lifecycle:
    ``initialized -> active -> finalized -> settled``.

    Parameters
    ----------
    underlying_provider : MockCoinbaseProvider, optional
        If given, the Kalshi provider reads the current underlying price
        from this provider instead of using an internal default.
    default_strike : float
        Default strike price for contracts when not specified in the symbol.
    implied_vol : float
        Implied volatility used in the probability model.
    contract_duration_s : float
        Contract duration in seconds (default 900 = 15 minutes).
    spread_range : tuple
        (min, max) bid-ask spread in dollars (e.g. (0.01, 0.03)).
    seed : int, optional
        RNG seed.
    """

    # Market status lifecycle
    STATUS_SEQUENCE = ("initialized", "active", "finalized", "settled")

    def __init__(
        self,
        underlying_provider: Optional["MockCoinbaseProvider"] = None,
        default_strike: float = 70_000.0,
        implied_vol: float = 0.002,
        contract_duration_s: float = 900.0,
        spread_range: Tuple[float, float] = (0.01, 0.03),
        seed: Optional[int] = None,
    ):
        self._underlying = underlying_provider
        self._default_strike = default_strike
        self._implied_vol = implied_vol
        self._contract_duration_s = contract_duration_s
        self._spread_range = spread_range
        self._rng = np.random.default_rng(seed)
        self._connected: bool = False

        # Per-contract state:  symbol -> {strike, start_step, status_idx, ...}
        self._contracts: Dict[str, Dict[str, Any]] = {}
        self._step: int = 0
        self._start_time: datetime = datetime.now(timezone.utc)

    # ---- DataProvider interface ----

    def connect(self) -> bool:
        self._connected = True
        logger.info("[MockKalshi] Connected to simulated exchange.")
        return True

    def fetch_latest(self, symbol: str) -> MarketData:
        """Return a realistic Kalshi-style MarketData for *symbol*.

        The symbol is expected to encode a strike price (e.g.
        ``KXBTC-70000`` or ``KXBTC-26MAR25-70000``).  If no strike can
        be parsed, ``default_strike`` is used.
        """
        self._step += 1

        # --- Determine underlying price ---
        if self._underlying is not None:
            underlying_price = self._underlying.current_price
        else:
            underlying_price = self._default_strike  # flat fallback

        # --- Parse strike from symbol ---
        strike = self._parse_strike(symbol)

        # --- Ensure contract state exists ---
        if symbol not in self._contracts:
            self._contracts[symbol] = {
                "strike": strike,
                "start_step": self._step,
                "status_idx": 0,  # initialized
                "settled_outcome": None,
            }

        contract = self._contracts[symbol]

        # --- Time remaining (fraction of contract duration) ---
        elapsed_steps = self._step - contract["start_step"]
        total_steps = max(1.0, self._contract_duration_s)
        time_remaining = max(0.0, 1.0 - elapsed_steps / total_steps)

        # --- Advance status lifecycle ---
        if time_remaining > 0.5:
            contract["status_idx"] = min(contract["status_idx"], 0)  # initialized
        elif time_remaining > 0.0:
            contract["status_idx"] = max(contract["status_idx"], 1)  # active
        else:
            if contract["status_idx"] < 2:
                # Settle: did underlying finish above the strike?
                contract["settled_outcome"] = (
                    "yes" if underlying_price >= strike else "no"
                )
                contract["status_idx"] = 3  # settled

        status = self.STATUS_SEQUENCE[min(contract["status_idx"], 3)]

        # --- Compute yes price ---
        if status in ("finalized", "settled"):
            yes_price = 1.0 if contract["settled_outcome"] == "yes" else 0.0
        else:
            yes_price = self._compute_yes_price(
                underlying_price, strike, time_remaining
            )

        # --- Bid / Ask spread ---
        spread = float(self._rng.uniform(*self._spread_range))
        half_spread = spread / 2.0
        yes_bid = max(0.01, round(yes_price - half_spread, 2))
        yes_ask = min(0.99, round(yes_price + half_spread, 2))
        # Ensure bid < ask
        if yes_bid >= yes_ask:
            yes_bid = max(0.01, yes_ask - 0.01)

        no_bid = max(0.01, round(1.0 - yes_ask, 2))
        no_ask = min(0.99, round(1.0 - yes_bid, 2))

        # --- Synthetic volume ---
        volume = int(self._rng.integers(50, 500))

        ts = self._start_time + timedelta(seconds=self._step)

        return MarketData(
            symbol=symbol,
            timestamp=ts,
            price=round(yes_price, 2),
            volume=volume,
            bid=yes_bid,
            ask=yes_ask,
            extra={
                "source": "mock_kalshi",
                "status": status,
                "strike": strike,
                "underlying_price": round(underlying_price, 2),
                "time_remaining": round(time_remaining, 4),
                "yes_bid_dollars": f"{yes_bid:.4f}",
                "yes_ask_dollars": f"{yes_ask:.4f}",
                "no_bid_dollars": f"{no_bid:.4f}",
                "no_ask_dollars": f"{no_ask:.4f}",
                "last_price_dollars": f"{round(yes_price, 2):.4f}",
                "volume_fp": f"{volume:.2f}",
                "settled_outcome": contract.get("settled_outcome"),
            },
        )

    # ---- Internal helpers ----

    def _parse_strike(self, symbol: str) -> float:
        """Try to extract a numeric strike from the symbol string.

        Handles formats like ``KXBTC-70000``, ``KXBTC-26MAR25-70000``,
        ``KXBTCD-2025-03-26T12:00-70500``, or plain ``70000``.
        Returns ``default_strike`` if parsing fails.
        """
        parts = symbol.replace("T", "-").split("-")
        for part in reversed(parts):
            try:
                val = float(part)
                if val > 1000:  # looks like a BTC strike
                    return val
            except ValueError:
                continue
        return self._default_strike

    def _compute_yes_price(
        self,
        current: float,
        strike: float,
        time_remaining: float,
    ) -> float:
        """Black-Scholes-like probability that underlying finishes above strike.

        P(above) = norm.cdf((current - strike) / (vol * sqrt(T_remaining)))
        """
        if time_remaining <= 0:
            return 1.0 if current >= strike else 0.0

        denom = self._implied_vol * current * math.sqrt(time_remaining)
        if denom < 1e-12:
            return 1.0 if current >= strike else 0.0

        d = (current - strike) / denom
        prob = float(norm.cdf(d))

        # Clamp to Kalshi price bounds [0.01, 0.99]
        return max(0.01, min(0.99, round(prob, 2)))

    # ---- Contract management ----

    def register_contract(
        self,
        symbol: str,
        strike: float,
        duration_steps: Optional[int] = None,
    ) -> None:
        """Pre-register a contract with an explicit strike and duration."""
        self._contracts[symbol] = {
            "strike": strike,
            "start_step": self._step,
            "status_idx": 0,
            "settled_outcome": None,
        }
        if duration_steps is not None:
            self._contract_duration_s = float(duration_steps)

    def get_contract_status(self, symbol: str) -> Optional[str]:
        """Return the current lifecycle status of a contract."""
        contract = self._contracts.get(symbol)
        if contract is None:
            return None
        return self.STATUS_SEQUENCE[min(contract["status_idx"], 3)]

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset all contract state."""
        self._rng = np.random.default_rng(seed)
        self._contracts.clear()
        self._step = 0
        self._start_time = datetime.now(timezone.utc)


# ======================================================================
# MockNWSProvider — synthetic weather observations
# ======================================================================

# Default station configurations: station_id -> (name, base_temp_f, amplitude_f)
_DEFAULT_STATIONS: Dict[str, Tuple[str, float, float]] = {
    "KJFK": ("John F. Kennedy International Airport, NY", 55.0, 15.0),
    "KLAX": ("Los Angeles International Airport, CA", 68.0, 12.0),
    "KORD": ("O'Hare International Airport, Chicago, IL", 50.0, 18.0),
    "KIAH": ("George Bush Intercontinental Airport, Houston, TX", 72.0, 14.0),
    "KDEN": ("Denver International Airport, CO", 48.0, 22.0),
}


class MockNWSProvider(DataProvider):
    """Simulate National Weather Service observations and forecasts.

    Temperature follows a sinusoidal daily cycle::

        T(t) = base_temp + amplitude * sin(2*pi*(hour - 6)/24) + noise

    Forecasts are generated with controllable divergence from the
    simulated observations, enabling weather-strategy edge testing.

    Parameters
    ----------
    stations : str or list of str, optional
        Station ID(s) to simulate.  Defaults to ``["KJFK"]``.
    base_temp_f : float, optional
        Override base temperature (Fahrenheit). If None, uses per-station
        defaults from ``_DEFAULT_STATIONS``.
    amplitude_f : float, optional
        Override daily amplitude. If None, uses per-station defaults.
    noise_std_f : float
        Standard deviation of random temperature noise (Fahrenheit).
    forecast_bias_f : float
        Systematic forecast bias (positive = forecast runs warm).
    forecast_noise_f : float
        Random noise added to forecasts beyond the observation noise.
    seed : int, optional
        RNG seed.
    """

    def __init__(
        self,
        stations: Optional[Any] = None,
        base_temp_f: Optional[float] = None,
        amplitude_f: Optional[float] = None,
        noise_std_f: float = 2.0,
        forecast_bias_f: float = 1.5,
        forecast_noise_f: float = 3.0,
        seed: Optional[int] = None,
    ):
        if stations is None:
            self._station_ids = ["KJFK"]
        elif isinstance(stations, str):
            self._station_ids = [stations]
        else:
            self._station_ids = list(stations)

        self._base_override = base_temp_f
        self._amp_override = amplitude_f
        self._noise_std = noise_std_f
        self._forecast_bias = forecast_bias_f
        self._forecast_noise = forecast_noise_f

        self._rng = np.random.default_rng(seed)
        self._step: int = 0
        self._connected: bool = False
        self._start_time: datetime = datetime.now(timezone.utc)

        # Track daily high per station
        self._daily_highs: Dict[str, float] = {}
        self._last_day: Dict[str, int] = {}

    # ---- DataProvider interface ----

    def connect(self) -> bool:
        self._connected = True
        for sid in self._station_ids:
            name = _DEFAULT_STATIONS.get(sid, (sid, 60.0, 15.0))[0]
            logger.info("[MockNWS] Connected to %s (%s) — simulated.", sid, name)
        return True

    def fetch_latest(self, symbol: str = None) -> MarketData:
        """Generate a weather observation with forecast for *symbol* (station ID)."""
        self._step += 1
        station_id = symbol if symbol else self._station_ids[0]

        ts = self._start_time + timedelta(seconds=self._step * 60)  # 1 min per step
        hour_of_day = ts.hour + ts.minute / 60.0

        # --- Station parameters ---
        defaults = _DEFAULT_STATIONS.get(station_id, (station_id, 60.0, 15.0))
        station_name = defaults[0]
        base_temp = (
            self._base_override if self._base_override is not None else defaults[1]
        )
        amplitude = (
            self._amp_override if self._amp_override is not None else defaults[2]
        )

        # --- Sinusoidal daily cycle ---
        # Peak around 2-3 PM (hour ~14), trough around 5-6 AM (hour ~5)
        # sin peaks at pi/2 → we shift so peak is at hour 14:
        #   sin(2*pi*(hour - 6)/24) peaks when hour=12 (close enough)
        temp_f = (
            base_temp
            + amplitude * math.sin(2.0 * math.pi * (hour_of_day - 6.0) / 24.0)
            + float(self._rng.normal(0, self._noise_std))
        )
        temp_f = round(temp_f, 1)

        # --- Track daily high ---
        day_num = ts.timetuple().tm_yday
        prev_day = self._last_day.get(station_id, -1)

        if day_num != prev_day:
            # New day — reset daily high
            self._daily_highs[station_id] = temp_f
            self._last_day[station_id] = day_num
        else:
            self._daily_highs[station_id] = max(
                self._daily_highs.get(station_id, temp_f), temp_f
            )

        max_temp_today = self._daily_highs[station_id]

        # --- Generate forecast periods ---
        forecast = self._generate_forecast(station_id, ts, base_temp, amplitude)

        return MarketData(
            symbol=station_id,
            timestamp=ts,
            price=0.0,
            volume=0,
            bid=0,
            ask=0,
            extra={
                "temperature_f": temp_f,
                "max_temp_today_f": max_temp_today,
                "temperature_c": round((temp_f - 32.0) * 5.0 / 9.0, 1),
                "description": self._weather_description(temp_f),
                "forecast": forecast,
                "station_name": station_name,
                "source": "mock_nws",
                "step": self._step,
            },
        )

    # ---- Forecast generation ----

    def _generate_forecast(
        self,
        station_id: str,
        current_ts: datetime,
        base_temp: float,
        amplitude: float,
    ) -> List[Dict[str, Any]]:
        """Generate a 7-day forecast (14 periods: day/night alternating).

        Forecasts intentionally diverge from the sinusoidal model to
        simulate real-world forecast error that weather strategies can
        exploit.
        """
        periods: List[Dict[str, Any]] = []
        for i in range(14):
            is_daytime = i % 2 == 0
            period_offset = timedelta(hours=12 * i)
            period_ts = current_ts + period_offset
            hour = 14.0 if is_daytime else 4.0  # peak / trough representative hours

            # "True" temperature from the sinusoidal model
            true_temp = base_temp + amplitude * math.sin(
                2.0 * math.pi * (hour - 6.0) / 24.0
            )

            # Forecast = true + bias + noise (gets noisier for later periods)
            noise_scale = self._forecast_noise * (1.0 + i * 0.15)
            forecast_temp = round(
                true_temp
                + self._forecast_bias
                + float(self._rng.normal(0, noise_scale)),
                0,
            )

            period_name = f"{'Today' if i < 2 else period_ts.strftime('%A')}" + (
                "" if is_daytime else " Night"
            )

            periods.append(
                {
                    "number": i + 1,
                    "name": period_name,
                    "isDaytime": is_daytime,
                    "temperature": int(forecast_temp),
                    "temperatureUnit": "F",
                    "startTime": (period_ts).isoformat(),
                    "endTime": (period_ts + timedelta(hours=12)).isoformat(),
                    "shortForecast": self._weather_description(forecast_temp),
                }
            )

        return periods

    @staticmethod
    def _weather_description(temp_f: float) -> str:
        """Return a simple description based on temperature."""
        if temp_f > 95:
            return "Extreme Heat"
        elif temp_f > 85:
            return "Hot and Sunny"
        elif temp_f > 70:
            return "Warm and Partly Cloudy"
        elif temp_f > 55:
            return "Mild"
        elif temp_f > 40:
            return "Cool"
        elif temp_f > 25:
            return "Cold"
        else:
            return "Frigid"

    # ---- Multi-station helpers ----

    @property
    def stations(self) -> List[str]:
        """Return the list of configured station IDs."""
        return list(self._station_ids)

    def fetch_all_stations(self) -> List[MarketData]:
        """Convenience: fetch observations for every configured station."""
        return [self.fetch_latest(sid) for sid in self._station_ids]

    def reset(self, seed: Optional[int] = None) -> None:
        """Reset simulation state."""
        self._rng = np.random.default_rng(seed)
        self._step = 0
        self._daily_highs.clear()
        self._last_day.clear()
        self._start_time = datetime.now(timezone.utc)


# ======================================================================
# MockMarketStore — in-memory implementation of MarketStore
# ======================================================================


class MockMarketStore:
    """In-memory drop-in replacement for ``MarketStore``.

    Mirrors the public method signatures of the SQLite-backed
    ``src.data.market_store.MarketStore`` but stores everything in
    plain Python lists, so it requires no filesystem or database.

    Suitable for unit tests and offline simulations.
    """

    def __init__(self, db_path: Optional[str] = None):
        # db_path accepted for interface compatibility but ignored
        self._ticks: List[Dict[str, Any]] = []
        self._candles: List[Dict[str, Any]] = []
        self._orderbooks: List[Dict[str, Any]] = []
        self._settlements: List[Dict[str, Any]] = []
        self._next_id: Dict[str, int] = {
            "ticks": 1,
            "candles": 1,
            "orderbooks": 1,
            "settlements": 1,
        }

    # ------------------------------------------------------------------
    # Insert methods
    # ------------------------------------------------------------------

    def insert_tick(
        self,
        symbol: str,
        timestamp,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        volume: Optional[float] = None,
        source: Optional[str] = None,
    ) -> None:
        self._ticks.append(
            {
                "id": self._next_id["ticks"],
                "timestamp": self._to_iso(timestamp),
                "symbol": symbol,
                "price": price,
                "bid": bid,
                "ask": ask,
                "volume": volume,
                "source": source,
            }
        )
        self._next_id["ticks"] += 1

    def insert_ticks_batch(self, ticks: List[Dict[str, Any]]) -> int:
        if not ticks:
            return 0
        for t in ticks:
            self.insert_tick(
                symbol=t["symbol"],
                timestamp=t["timestamp"],
                price=t["price"],
                bid=t.get("bid"),
                ask=t.get("ask"),
                volume=t.get("volume"),
                source=t.get("source"),
            )
        return len(ticks)

    def insert_candle(
        self,
        symbol: str,
        timestamp,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: Optional[float] = None,
        period: str = "1m",
    ) -> None:
        self._candles.append(
            {
                "id": self._next_id["candles"],
                "timestamp": self._to_iso(timestamp),
                "symbol": symbol,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "period": period,
            }
        )
        self._next_id["candles"] += 1

    def insert_orderbook(
        self,
        symbol: str,
        timestamp,
        bids: Any,
        asks: Any,
    ) -> None:
        import json

        self._orderbooks.append(
            {
                "id": self._next_id["orderbooks"],
                "timestamp": self._to_iso(timestamp),
                "symbol": symbol,
                "bids_json": json.dumps(bids),
                "asks_json": json.dumps(asks),
            }
        )
        self._next_id["orderbooks"] += 1

    def insert_settlement(
        self,
        market_id: str,
        outcome: str,
        settlement_time,
        settlement_value: Optional[float] = None,
    ) -> None:
        self._settlements.append(
            {
                "id": self._next_id["settlements"],
                "market_id": market_id,
                "outcome": outcome,
                "settlement_time": self._to_iso(settlement_time),
                "settlement_value": settlement_value,
            }
        )
        self._next_id["settlements"] += 1

    # Also support the `.insert(market_data)` shorthand used by coinbase_ws
    def insert(self, md: MarketData) -> None:
        """Convenience: insert a MarketData object as a tick."""
        self.insert_tick(
            symbol=md.symbol,
            timestamp=md.timestamp,
            price=md.price,
            bid=md.bid,
            ask=md.ask,
            volume=md.volume,
            source=(md.extra or {}).get("source"),
        )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_ticks(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        results = [t for t in self._ticks if t["symbol"] == symbol]
        if start is not None:
            results = [t for t in results if t["timestamp"] >= start]
        if end is not None:
            results = [t for t in results if t["timestamp"] <= end]
        results.sort(key=lambda t: t["timestamp"])
        if limit is not None:
            results = results[:limit]
        return results

    def get_latest_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        matching = [t for t in self._ticks if t["symbol"] == symbol]
        if not matching:
            return None
        return max(matching, key=lambda t: t["timestamp"])

    def get_candles(
        self,
        symbol: str,
        period: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        results = [
            c for c in self._candles if c["symbol"] == symbol and c["period"] == period
        ]
        if start is not None:
            results = [c for c in results if c["timestamp"] >= start]
        if end is not None:
            results = [c for c in results if c["timestamp"] <= end]
        results.sort(key=lambda c: c["timestamp"])
        if limit is not None:
            results = results[:limit]
        return results

    def get_settlements(
        self,
        market_type: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results = list(self._settlements)
        if market_type is not None:
            results = [s for s in results if s["market_id"].startswith(market_type)]
        if start is not None:
            results = [s for s in results if s["settlement_time"] >= start]
        if end is not None:
            results = [s for s in results if s["settlement_time"] <= end]
        results.sort(key=lambda s: s["settlement_time"])
        return results

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def tick_count(self, symbol: Optional[str] = None) -> int:
        if symbol is not None:
            return sum(1 for t in self._ticks if t["symbol"] == symbol)
        return len(self._ticks)

    def candle_count(self, symbol: Optional[str] = None) -> int:
        if symbol is not None:
            return sum(1 for c in self._candles if c["symbol"] == symbol)
        return len(self._candles)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def build_candles(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
    ) -> List[Dict[str, Any]]:
        """Aggregate stored ticks into OHLCV candles (mirrors MarketStore)."""
        period_minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "1h": 60,
            "4h": 240,
            "1d": 1440,
        }
        minutes = period_minutes.get(period)
        if minutes is None:
            raise ValueError(
                f"Unsupported period '{period}'. "
                f"Choose from {list(period_minutes.keys())}"
            )

        ticks = self.get_ticks(symbol, start=start, end=end)
        if not ticks:
            return []

        period_seconds = minutes * 60
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for tick in ticks:
            ts = datetime.fromisoformat(tick["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            epoch = int(ts.timestamp())
            bucket_epoch = epoch - (epoch % period_seconds)
            bucket_key = datetime.fromtimestamp(
                bucket_epoch, tz=timezone.utc
            ).isoformat()
            buckets.setdefault(bucket_key, []).append(tick)

        candles: List[Dict[str, Any]] = []
        for bucket_ts in sorted(buckets.keys()):
            group = buckets[bucket_ts]
            prices = [t["price"] for t in group]
            volumes = [t["volume"] for t in group if t.get("volume") is not None]
            candle = {
                "timestamp": bucket_ts,
                "symbol": symbol,
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": sum(volumes) if volumes else None,
                "period": period,
            }
            candles.append(candle)
            self.insert_candle(
                symbol=symbol,
                timestamp=bucket_ts,
                open_=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
                volume=candle["volume"],
                period=period,
            )

        return candles

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def close(self) -> None:
        """No-op (no resources to release)."""
        pass

    def clear(self) -> None:
        """Remove all stored data."""
        self._ticks.clear()
        self._candles.clear()
        self._orderbooks.clear()
        self._settlements.clear()

    @staticmethod
    def _to_iso(ts) -> str:
        if isinstance(ts, str):
            return ts
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.isoformat()
        raise TypeError(f"Expected datetime or str, got {type(ts)}")
