import requests
import base64
import time
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from src.core.interfaces import DataProvider, MarketData
from src.utils.logger import logger


class KalshiProvider(DataProvider):
    """
    Data Provider for Kalshi.
    Supports Authenticated (Private) and Anonymous (Public Read-Only) modes.
    """

    PUBLIC_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
    V1_API_URL = "https://api.elections.kalshi.com/v1"

    def __init__(
        self,
        key_id: str = None,
        private_key_path: str = None,
        api_url: str = None,
        read_only: bool = False,
    ):
        self.key_id = key_id
        self.api_url = (api_url or self.PUBLIC_API_URL).rstrip("/")
        self.anonymous = not (key_id and private_key_path)
        self.read_only = read_only

        if not self.anonymous:
            self.private_key = self._load_private_key(private_key_path)
        else:
            self.private_key = None

        self.session = requests.Session()

    def _load_private_key(self, path_or_content: str):
        try:
            # Check if it's a file path
            if os.path.exists(path_or_content):
                with open(path_or_content, "rb") as key_file:
                    key_data = key_file.read()
            else:
                # Treat as raw key content (PEM)
                key_data = path_or_content.encode("utf-8")
                # Add PEM headers if missing
                if b"BEGIN PRIVATE KEY" not in key_data:
                    key_data = (
                        b"-----BEGIN RSA PRIVATE KEY-----\n"
                        + key_data
                        + b"\n-----END RSA PRIVATE KEY-----"
                    )

            return serialization.load_pem_private_key(key_data, password=None)
        except Exception as e:
            logger.error(f"[KalshiProvider] Error loading private key: {e}")
            raise e

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        """Generates the RSA signature for the request."""
        if self.anonymous:
            return ""

        full_path = path
        if not path.startswith("/trade-api/v2"):
            full_path = "/trade-api/v2" + path

        payload = f"{timestamp}{method}{full_path}"

        signature = self.private_key.sign(
            payload.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def place_order(self, symbol: str, side: str, quantity: int, price: float):
        """
        Places an order.
        SAFETY: If read_only is True, this explicitly BLOCKS the request.
        """
        if self.read_only:
            raise RuntimeError(
                f"Trade blocked: Provider is in READ-ONLY mode. (Attempted: {side} {quantity} {symbol})"
            )

        logger.warning("Order placement not implemented for live trading yet.")
        return False

    def connect(self) -> bool:
        """
        Verifies connection by hitting a public endpoint.
        """
        mode = "GHOST (Read-Only)" if self.anonymous else "AUTHENTICATED"
        logger.info(f"[KalshiProvider] Connecting to {self.api_url} in {mode} mode...")
        try:
            url = f"{self.api_url}/exchange/status"
            headers = {"Content-Type": "application/json"}

            if not self.anonymous:
                path = "/exchange/status"
                timestamp = str(int(time.time() * 1000))
                signature = self._sign_request("GET", path, timestamp)
                headers.update(
                    {
                        "KALSHI-ACCESS-KEY": self.key_id,
                        "KALSHI-ACCESS-SIGNATURE": signature,
                        "KALSHI-ACCESS-TIMESTAMP": timestamp,
                    }
                )

            resp = self.session.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                logger.info("[KalshiProvider] Exchange is Online. (Live Prod/Public)")
                return True
            else:
                logger.error(
                    f"[KalshiProvider] Exchange Status Error: {resp.status_code}"
                )
                return False
        except Exception as e:
            logger.error(f"[KalshiProvider] Connection Error: {e}")
            return False

    def get_balance(self) -> float:
        """
        Fetches the user's available balance in cents and returns it in dollars.
        """
        if self.anonymous:
            return 0.0

        path = "/portfolio/balance"
        url = f"{self.api_url}{path}"

        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request("GET", path, timestamp)

        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

        try:
            resp = self.session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            balance_cents = data.get("balance", 0)
            return balance_cents / 100.0
        except Exception as e:
            logger.error(f"[KalshiProvider] Failed to fetch balance: {e}")
            return 0.0

    @staticmethod
    def _parse_price(data: dict, dollar_key: str, cent_key: str) -> float:
        """Extract price from API response, handling both V2 (_dollars string)
        and V1 (cents integer) field formats.

        V2 API returns e.g. ``yes_bid_dollars: "0.0700"`` (string, already dollars).
        V1 API returns e.g. ``yes_bid: 7`` (int, cents) alongside the _dollars field.
        We prefer the _dollars field when present and non-null.
        """
        # Try V2 _dollars field first (string in dollars)
        dollars_val = data.get(dollar_key)
        if dollars_val is not None:
            try:
                return float(dollars_val)
            except (TypeError, ValueError):
                pass
        # Fall back to V1 cents field
        cents_val = data.get(cent_key)
        if cents_val is not None:
            try:
                return float(cents_val) / 100.0
            except (TypeError, ValueError):
                pass
        return 0.0

    def _parse_market_data(self, symbol: str, data: dict, source: str) -> MarketData:
        """Parse a market JSON object into a MarketData instance.

        Handles both V2 API responses (``*_dollars`` string fields) and
        V1 API responses (``yes_bid`` etc. as integer cents).
        """
        yes_bid = self._parse_price(data, "yes_bid_dollars", "yes_bid")
        yes_ask = self._parse_price(data, "yes_ask_dollars", "yes_ask")
        no_bid = self._parse_price(data, "no_bid_dollars", "no_bid")
        no_ask = self._parse_price(data, "no_ask_dollars", "no_ask")
        last_price = self._parse_price(data, "last_price_dollars", "last_price")

        # Volume: try float-point field first, then integer
        volume = 0
        vol_fp = data.get("volume_fp")
        if vol_fp is not None:
            try:
                volume = int(float(vol_fp))
            except (TypeError, ValueError):
                volume = data.get("volume", 0) or 0
        else:
            volume = data.get("volume", 0) or 0

        # Extract strike from floor_strike (BTC) or ticker parsing (weather)
        strike = None
        floor_strike = data.get("floor_strike")
        if floor_strike is not None:
            try:
                strike = float(floor_strike)
            except (TypeError, ValueError):
                pass

        return MarketData(
            symbol=symbol,
            timestamp=datetime.now(),
            price=last_price,
            volume=volume,
            bid=yes_bid,
            ask=yes_ask,
            extra={
                "status": data.get("status"),
                "close_time": data.get("close_time"),
                "source": source,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "strike": strike,
                "strike_type": data.get("strike_type"),
            },
        )

    def _fetch_market_raw(self, symbol: str, api_url: str) -> Optional[dict]:
        """Fetch raw market JSON from a given API base URL."""
        path = f"/markets/{symbol}"
        url = f"{api_url}{path}"

        headers = {"Content-Type": "application/json"}

        if not self.anonymous and api_url == self.api_url:
            timestamp = str(int(time.time() * 1000))
            signature = self._sign_request("GET", path, timestamp)
            headers.update(
                {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp,
                }
            )

        resp = self.session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("market", {})

    def _get_authenticated_headers(self, method: str, path: str) -> dict:
        """Build headers with auth signature for the configured API."""
        headers = {"Content-Type": "application/json"}
        if not self.anonymous:
            timestamp = str(int(time.time() * 1000))
            signature = self._sign_request(method, path, timestamp)
            headers.update(
                {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp,
                }
            )
        return headers

    def search_markets(self, **params) -> list:
        """Search markets with proper authentication. Returns list of market dicts."""
        path = "/markets"
        url = f"{self.api_url}{path}"
        headers = self._get_authenticated_headers("GET", path)
        try:
            resp = self.session.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("markets", []), data.get("cursor")
        except Exception as e:
            logger.error(f"[KalshiProvider] Search Error: {e}")
            return [], None

    def fetch_latest(self, symbol: str) -> MarketData:
        """
        Fetches market data for a specific ticker.
        Falls back to the public production API if the configured (demo) API
        returns all-zero prices (empty orderbook).
        """
        try:
            data = self._fetch_market_raw(symbol, self.api_url)
            result = self._parse_market_data(
                symbol, data, "live_kalshi_ghost" if self.anonymous else "live_kalshi"
            )

            # If all prices are zero and we're on a non-public API (e.g. demo),
            # try the public production API for real orderbook prices.
            if (
                result.bid == 0
                and result.ask == 0
                and result.price == 0
                and self.api_url != self.PUBLIC_API_URL
            ):
                try:
                    pub_data = self._fetch_market_raw(symbol, self.PUBLIC_API_URL)
                    pub_result = self._parse_market_data(
                        symbol, pub_data, "live_kalshi_public"
                    )
                    if pub_result.bid > 0 or pub_result.ask > 0 or pub_result.price > 0:
                        # Preserve status/close_time from primary API, overlay prices
                        pub_result.extra["status"] = result.extra.get("status")
                        pub_result.extra["close_time"] = result.extra.get("close_time")
                        return pub_result
                except Exception:
                    pass  # Fall through to original result

            return result

        except Exception as e:
            logger.error(f"[KalshiProvider] Fetch Error for {symbol}: {e}")
            return None

    def fetch_btc_hourly_markets(self) -> List[MarketData]:
        """
        Special discovery for BTC Hourly markets which are hidden from V2 endpoint.
        Uses V1 Event API to probe for active hourly events.
        """
        markets = []
        # Kalshi BTC hourly markets use ET for their event tickers
        now = datetime.now(ZoneInfo("America/New_York"))

        # Candidate hours: Current hour + next 12 hours
        candidates = []
        for i in range(12):
            t = now + timedelta(hours=i)
            # Format: KXBTCD-YYMMMDDHH (e.g. 26FEB1717)
            # Year: 26 (2-digit)
            yy = t.strftime("%y")
            # Month: FEB (3-char upper)
            mmm = t.strftime("%b").upper()
            # Day: 17 (2-digit)
            dd = t.strftime("%d")
            # Hour: 17 (24-hour)
            hh = t.strftime("%H")

            ticker = f"KXBTCD-{yy}{mmm}{dd}{hh}"
            candidates.append(ticker)

        logger.info(
            f"[KalshiProvider] Probing {len(candidates)} candidate BTC hourly events..."
        )

        for event_ticker in candidates:
            try:
                # Probe V1
                url = f"{self.V1_API_URL}/series/KXBTCD/events/{event_ticker}"
                resp = self.session.get(url, timeout=2)  # Fast timeout

                if resp.status_code == 200:
                    data = resp.json()
                    event = data.get("event", {})
                    raw_markets = event.get("markets", [])

                    if raw_markets:
                        # logger.info(f"[KalshiProvider] FOUND {event_ticker}: {len(raw_markets)} markets")

                        for m in raw_markets:
                            # Check Expiration (Filter out past markets)
                            close_str = m.get("close_date")
                            if close_str:
                                try:
                                    # Handle ISO with Z
                                    # 2026-02-17T05:00:00Z
                                    # We use naive now() for simplicity if system is local, but API is UTC.
                                    # Better to convert everything to UTC aware.
                                    close_dt = datetime.fromisoformat(
                                        close_str.replace("Z", "+00:00")
                                    )
                                    now_utc = datetime.now(timezone.utc)

                                    # Buffer: If closed within last 1 minute, consider closed.
                                    if close_dt <= now_utc:
                                        # logger.debug(f"Skipping expired market: {m.get('ticker_name')} (Closed {close_str})")
                                        continue
                                except Exception:
                                    pass

                            # Map V1 JSON to MarketData (V1 has both cents and _dollars fields)
                            symbol = m.get("ticker_name")
                            md = self._parse_market_data(symbol, m, "v1_discovery")
                            # V1 uses close_date instead of close_time
                            md.extra["close_time"] = m.get("close_date")
                            md.extra["strike_type"] = m.get("strike_type")
                            md.extra["sub_title"] = m.get("sub_title")
                            markets.append(md)
            except Exception:
                # logger.debug(f"Probe failed for {event_ticker}: {e}")
                pass

        return markets
