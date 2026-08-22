"""
Real-time Coinbase WebSocket feed for BTC-USD ticker data.

Connects to wss://ws-feed.exchange.coinbase.com, subscribes to the ticker
channel, and stores incoming ticks in a thread-safe ring buffer. Implements
the DataProvider interface so it can be dropped in wherever CoinbaseProvider
(REST) is used today.

Sprint 1 / Task 1.1 — Money Printer V2
"""

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import websocket  # websocket-client

from src.core.interfaces import DataProvider, MarketData

logger = logging.getLogger("coinbase_ws")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WS_URL = "wss://ws-feed.exchange.coinbase.com"
DEFAULT_PRODUCT_ID = "BTC-USD"
RING_BUFFER_HOURS = 24
# At ~1 tick/sec for an active pair, 24 h ~ 86 400 entries (~15 MB).
MAX_BUFFER_LEN = 100_000
RECONNECT_BASE_DELAY = 1.0  # seconds
RECONNECT_MAX_DELAY = 60.0  # seconds
CONNECT_TIMEOUT = 15  # seconds — max wait in connect()


class CoinbaseWebSocketProvider(DataProvider):
    """
    Streaming DataProvider backed by the Coinbase WebSocket ticker channel.

    Parameters
    ----------
    product_id : str
        Coinbase product id to subscribe to (default ``"BTC-USD"``).
    on_tick : callable, optional
        ``fn(MarketData) -> None`` invoked on every incoming tick.
    store : object, optional
        Object with an ``insert(MarketData)`` method. If provided, every
        tick is forwarded automatically.
    buffer_size : int, optional
        Maximum number of ticks kept in the ring buffer.
    """

    def __init__(
        self,
        product_id: str = DEFAULT_PRODUCT_ID,
        on_tick: Optional[Callable[[MarketData], None]] = None,
        store: Any = None,
        buffer_size: int = MAX_BUFFER_LEN,
    ):
        self.product_id = product_id
        self._on_tick = on_tick
        self._store = store

        # Ring buffer — bounded deque is O(1) append and auto-evicts oldest.
        self._buffer: deque[MarketData] = deque(maxlen=buffer_size)
        self._latest: Optional[MarketData] = None

        # Synchronisation primitives
        self._lock = threading.Lock()
        self._first_msg_event = threading.Event()

        # WebSocket lifecycle
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False
        self._reconnect_delay = RECONNECT_BASE_DELAY

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Start the WebSocket in a daemon thread.

        Blocks until the first ticker message is received or *CONNECT_TIMEOUT*
        seconds elapse.  Returns ``True`` on success.
        """
        if self._running:
            logger.warning("connect() called but already running")
            return True

        self._running = True
        self._first_msg_event.clear()
        self._start_ws_thread()

        got_data = self._first_msg_event.wait(timeout=CONNECT_TIMEOUT)
        if got_data:
            logger.info("Connected — first tick received for %s", self.product_id)
        else:
            logger.warning(
                "connect() timed out after %ds waiting for first tick",
                CONNECT_TIMEOUT,
            )
        return got_data

    def fetch_latest(self, symbol: str = None) -> Optional[MarketData]:
        """Return the most recent MarketData snapshot, or ``None``."""
        with self._lock:
            return self._latest

    # ------------------------------------------------------------------
    # Extended query helpers
    # ------------------------------------------------------------------

    def get_history(self, seconds: int = 3600) -> List[MarketData]:
        """Return ticks from the last *seconds* seconds (newest last)."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        with self._lock:
            return [md for md in self._buffer if md.timestamp >= cutoff]

    def get_price_history(self, seconds: int = 3600) -> List[Tuple[datetime, float]]:
        """Return ``(timestamp, price)`` tuples for the last *seconds* seconds."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        with self._lock:
            return [
                (md.timestamp, md.price)
                for md in self._buffer
                if md.timestamp >= cutoff
            ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Cleanly shut down the WebSocket and background thread."""
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._ws_thread is not None and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        logger.info("WebSocket provider stopped for %s", self.product_id)

    @property
    def is_connected(self) -> bool:
        return self._running and self._first_msg_event.is_set()

    @property
    def tick_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ------------------------------------------------------------------
    # Internal — WebSocket plumbing
    # ------------------------------------------------------------------

    def _start_ws_thread(self) -> None:
        self._ws_thread = threading.Thread(
            target=self._run_forever, daemon=True, name="coinbase-ws"
        )
        self._ws_thread.start()

    def _run_forever(self) -> None:
        """Main loop: connect, run, reconnect on failure."""
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                # run_forever blocks until the socket closes.
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.error("WebSocket run_forever raised: %s", exc)

            if not self._running:
                break

            # Reconnect with exponential back-off
            logger.info("Reconnecting in %.1fs ...", self._reconnect_delay)
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)

    # -- callbacks -----------------------------------------------------

    def _on_open(self, ws) -> None:
        subscribe_msg = json.dumps(
            {
                "type": "subscribe",
                "product_ids": [self.product_id],
                "channels": ["ticker"],
            }
        )
        ws.send(subscribe_msg)
        logger.info("Subscribed to ticker channel for %s", self.product_id)

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON message: %.120s", raw)
            return

        msg_type = msg.get("type")

        # Subscription confirmations — log and move on.
        if msg_type == "subscriptions":
            logger.debug("Subscription confirmed: %s", msg)
            return

        if msg_type == "error":
            logger.error("Coinbase WS error: %s", msg.get("message"))
            return

        if msg_type != "ticker":
            return

        md = self._parse_ticker(msg)
        if md is None:
            return

        with self._lock:
            self._buffer.append(md)
            self._latest = md

        # Reset back-off on successful data receipt.
        self._reconnect_delay = RECONNECT_BASE_DELAY

        # Signal that we have our first tick.
        if not self._first_msg_event.is_set():
            self._first_msg_event.set()

        # Optional callbacks / store integration.
        if self._on_tick is not None:
            try:
                self._on_tick(md)
            except Exception as exc:
                logger.error("on_tick callback error: %s", exc)

        if self._store is not None:
            try:
                self._store.insert(md)
            except Exception as exc:
                logger.error("MarketStore insert error: %s", exc)

    def _on_error(self, ws, error) -> None:
        logger.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info("WebSocket closed (code=%s msg=%s)", close_status_code, close_msg)

    # -- parsing -------------------------------------------------------

    @staticmethod
    def _parse_ticker(msg: Dict[str, Any]) -> Optional[MarketData]:
        """Convert a Coinbase ticker JSON message into a MarketData object."""
        try:
            price = float(msg["price"])
            bid = float(msg.get("best_bid", 0))
            ask = float(msg.get("best_ask", 0))
            volume = float(msg.get("volume_24h", 0))

            # Coinbase sends ISO-8601 timestamps with microsecond precision.
            time_str = msg.get("time")
            if time_str:
                # Handle both "Z" and "+00:00" suffixes.
                ts = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            else:
                ts = datetime.now(timezone.utc)

            return MarketData(
                symbol=msg.get("product_id", DEFAULT_PRODUCT_ID),
                timestamp=ts,
                price=price,
                volume=volume,
                bid=bid,
                ask=ask,
                extra={
                    "source": "live_coinbase_ws",
                    "sequence": msg.get("sequence"),
                    "trade_id": msg.get("trade_id"),
                    "last_size": msg.get("last_size"),
                    "side": msg.get("side"),
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse ticker message: %s — %s", exc, msg)
            return None
