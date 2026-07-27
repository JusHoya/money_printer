"""
Kalshi WebSocket feed for real-time orderbook and trade data.

Connects to the Kalshi V2 WebSocket API and maintains per-market orderbook
state, exposing updates via the standard DataProvider interface as well as
optional callbacks.

Sprint 1 / Task 1.2 — Money Printer V2

FR-1.1 bracket semantics over the WebSocket
-------------------------------------------
Investigated against Kalshi's published AsyncAPI specification
(https://docs.kalshi.com/asyncapi.yaml, retrieved 2026-07-25). Finding:

* The quote/print channels this provider consumes — ``orderbook_delta``,
  ``orderbook_snapshot``, ``ticker`` and ``trade`` — carry **no** market
  metadata at all. Their payloads are ``market_ticker`` plus price/quantity
  levels; ``strike_type``, ``floor_strike``, ``cap_strike`` and
  ``yes_sub_title`` appear nowhere in them.
* Those fields DO travel on the separate ``market_lifecycle_v2`` channel:
  inside ``msg.additional_metadata`` on an ``event_type=created`` message, and
  as top-level ``msg`` fields on ``event_type=metadata_updated``.

So the feed can be made FR-1.1 compliant, but only by subscribing to a second
channel — and even then a market that was created *before* this process
connected emits no lifecycle message, so its semantics can never arrive over
the socket alone. That gap is closed by :meth:`seed_bracket_metadata`, which
lets the REST ``KalshiProvider`` (the FR-1.1 authority) prime the cache for
markets already open.

Consequently this module does BOTH:

1. subscribes to ``market_lifecycle_v2`` and caches every bracket field it
   sees, merging it into ``MarketData.extra`` on ``fetch_latest``; and
2. when a market's bracket semantics are still unknown, logs loudly (ERROR,
   once per ticker) and emits the bracket keys as ``None`` — which makes
   ``src.core.bracket_payoff.parse_bracket_spec`` raise ``BracketSpecError``
   downstream. It never emits a silently bracket-blind ``MarketData``, and it
   never infers direction from the ticker string.

This provider is currently unwired from the runtime (PRD §4 A2 mothballed the
latency-arb strategy that used it); websocket feeds are a named revival
precondition, so it is kept FR-1.1 compliant rather than left to rot.
"""

import base64
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Dict, List, Optional

import requests
import websocket
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.core.interfaces import DataProvider, MarketData

logger = logging.getLogger("kalshi_ws")

# PRD FR-1.1. The only fields any code may use to establish contract
# direction. Mirrors src.bots.weather_bot.BRACKET_FIELDS.
BRACKET_FIELDS = ("strike_type", "floor_strike", "cap_strike", "yes_sub_title")

# Channels carrying market metadata, per the AsyncAPI spec (see module
# docstring). Quote channels carry none.
LIFECYCLE_CHANNELS = ("market_lifecycle_v2", "market_lifecycle")
QUOTE_CHANNELS = ("orderbook_delta", "trade")


# ---------------------------------------------------------------------------
# Internal dataclasses for per-market state
# ---------------------------------------------------------------------------


class _OrderbookState:
    """Thread-safe orderbook snapshot for a single market."""

    __slots__ = (
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "last_price",
        "volume",
        "updated_at",
        "lock",
    )

    def __init__(self):
        self.yes_bid: float = 0.0
        self.yes_ask: float = 0.0
        self.no_bid: float = 0.0
        self.no_ask: float = 0.0
        self.last_price: float = 0.0
        self.volume: int = 0
        self.updated_at: Optional[datetime] = None
        self.lock = threading.Lock()

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            self.updated_at = datetime.now()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "yes_bid": self.yes_bid,
                "yes_ask": self.yes_ask,
                "no_bid": self.no_bid,
                "no_ask": self.no_ask,
                "last_price": self.last_price,
                "volume": self.volume,
                "updated_at": self.updated_at,
            }


class _TradeRecord:
    """A single observed trade."""

    __slots__ = ("ticker", "price", "count", "taker_side", "ts")

    def __init__(
        self, ticker: str, price: float, count: int, taker_side: str, ts: datetime
    ):
        self.ticker = ticker
        self.price = price
        self.count = count
        self.taker_side = taker_side
        self.ts = ts


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class KalshiWebSocket(DataProvider):
    """Real-time Kalshi market data via the V2 WebSocket API.

    Parameters
    ----------
    key_id : str, optional
        Kalshi API key ID.  Falls back to ``KALSHI_KEY_ID`` env var.
    private_key_path : str, optional
        Path to PEM private key.  Falls back to ``KALSHI_PRIVATE_KEY_PATH``.
    api_url : str, optional
        REST API base URL (used only for the initial auth / token fetch).
        Defaults to ``KALSHI_API_URL`` env var or the public production URL.
    ws_url : str, optional
        WebSocket endpoint.  Defaults to the Kalshi V2 production WS URL.
    max_trades : int
        Ring-buffer size per market for recent trades (default 200).
    """

    PUBLIC_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
    PUBLIC_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    # Reconnect back-off parameters
    _BASE_DELAY = 1.0  # seconds
    _MAX_DELAY = 60.0  # seconds
    _BACKOFF_FACTOR = 2.0

    def __init__(
        self,
        key_id: str = None,
        private_key_path: str = None,
        api_url: str = None,
        ws_url: str = None,
        max_trades: int = 200,
    ):
        self.key_id = key_id or os.environ.get("KALSHI_KEY_ID")
        self._private_key_path = private_key_path or os.environ.get(
            "KALSHI_PRIVATE_KEY_PATH"
        )
        self.api_url = (
            api_url or os.environ.get("KALSHI_API_URL") or self.PUBLIC_API_URL
        ).rstrip("/")
        self.ws_url = (ws_url or self.PUBLIC_WS_URL).rstrip("/")
        self._max_trades = max_trades

        # Auth state
        self._private_key = None
        self._authenticated = False
        self._anonymous = not (self.key_id and self._private_key_path)

        # Load key eagerly so we fail fast on bad config
        if not self._anonymous:
            self._private_key = self._load_private_key(self._private_key_path)

        # WebSocket plumbing
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._msg_id = 0
        self._msg_id_lock = threading.Lock()
        self._reconnect_delay = self._BASE_DELAY

        # Market state (ticker -> _OrderbookState)
        self._books: Dict[str, _OrderbookState] = {}
        self._books_lock = threading.Lock()

        # Recent trades (ticker -> deque[_TradeRecord])
        self._trades: Dict[str, deque] = {}
        self._trades_lock = threading.Lock()

        # FR-1.1 bracket semantics (ticker -> {strike_type, floor_strike,
        # cap_strike, yes_sub_title}), sourced from market_lifecycle_v2
        # messages and/or seeded from the REST provider. Never inferred.
        self._brackets: Dict[str, Dict[str, object]] = {}
        self._brackets_lock = threading.Lock()
        # Tickers we have already screamed about, so the ERROR is one per
        # market rather than one per tick.
        self._bracket_warned: set = set()

        # Subscriptions requested by caller (persisted across reconnects)
        self._subscribed_tickers: List[str] = []
        self._subscribed_lock = threading.Lock()

        # Optional user callbacks
        self.on_orderbook_update: Optional[Callable[[str, dict], None]] = None
        self.on_trade: Optional[Callable[[str, _TradeRecord], None]] = None

    # ------------------------------------------------------------------
    # Key loading & signing (mirrors KalshiProvider)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_private_key(path_or_content: str):
        if os.path.exists(path_or_content):
            with open(path_or_content, "rb") as fh:
                key_data = fh.read()
        else:
            key_data = path_or_content.encode("utf-8")
            if (
                b"BEGIN PRIVATE KEY" not in key_data
                and b"BEGIN RSA PRIVATE KEY" not in key_data
            ):
                key_data = (
                    b"-----BEGIN RSA PRIVATE KEY-----\n"
                    + key_data
                    + b"\n-----END RSA PRIVATE KEY-----"
                )
        return serialization.load_pem_private_key(key_data, password=None)

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        if self._anonymous or self._private_key is None:
            return ""
        full_path = path if path.startswith("/trade-api/v2") else f"/trade-api/v2{path}"
        payload = f"{timestamp}{method}{full_path}"
        signature = self._private_key.sign(
            payload.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _get_auth_headers(self, method: str, path: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if not self._anonymous:
            ts = str(int(time.time() * 1000))
            sig = self._sign_request(method, path, ts)
            headers.update(
                {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-SIGNATURE": sig,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                }
            )
        return headers

    # ------------------------------------------------------------------
    # Price parsing (shared with KalshiProvider)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_price(value) -> float:
        """Convert a price value to float.

        Handles dollar-strings (``"0.45"``), integer cents (``45``), and
        plain floats.  Returns 0.0 on failure.
        """
        if value is None:
            return 0.0
        try:
            f = float(value)
            # Heuristic: values > 1.0 are likely cents (V1 compat)
            return f / 100.0 if f > 1.0 else f
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Establish the WebSocket connection.

        1. Optionally verify REST connectivity (proves auth works).
        2. Start the WebSocket in a daemon thread.
        3. Wait up to 10 s for the connection handshake.

        Returns ``True`` if the WebSocket is open.
        """
        if self._connected.is_set():
            logger.info("[KalshiWS] Already connected")
            return True

        # Step 1: quick REST health-check (non-blocking if it fails)
        mode = "ANONYMOUS" if self._anonymous else "AUTHENTICATED"
        logger.info(
            "[KalshiWS] Connecting (%s) — REST check against %s", mode, self.api_url
        )
        try:
            path = "/exchange/status"
            url = f"{self.api_url}{path}"
            headers = self._get_auth_headers("GET", path)
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                logger.info("[KalshiWS] REST health-check OK")
            else:
                logger.warning(
                    "[KalshiWS] REST health-check returned %s", resp.status_code
                )
        except Exception as exc:
            logger.warning("[KalshiWS] REST health-check failed: %s", exc)

        # Step 2: open WebSocket
        self._stop_event.clear()
        self._connected.clear()
        self._start_ws_thread()

        # Step 3: wait for handshake
        if self._connected.wait(timeout=10):
            logger.info("[KalshiWS] WebSocket connected to %s", self.ws_url)
            return True

        logger.error("[KalshiWS] WebSocket connection timed out")
        return False

    def fetch_latest(self, symbol: str) -> Optional[MarketData]:
        """Return the latest cached orderbook state for *symbol*.

        Returns ``None`` if we have never received data for this market.
        """
        with self._books_lock:
            book = self._books.get(symbol)
        if book is None:
            return None

        snap = book.snapshot()

        # Gather recent trades for the extra dict
        with self._trades_lock:
            trade_buf = self._trades.get(symbol)
            recent = list(trade_buf)[-10:] if trade_buf else []

        recent_trades = [
            {
                "price": t.price,
                "count": t.count,
                "taker_side": t.taker_side,
                "ts": t.ts.isoformat(),
            }
            for t in recent
        ]

        extra = {
            "source": "kalshi_ws",
            "no_bid": snap["no_bid"],
            "no_ask": snap["no_ask"],
            "recent_trades": recent_trades,
        }
        # PRD FR-1.1: bracket semantics on every emitted MarketData. Unknown
        # semantics are emitted as None (never guessed, never omitted) after a
        # loud one-shot ERROR, so parse_bracket_spec fails downstream instead
        # of anything inferring direction from the ticker.
        extra.update(self._bracket_extra(symbol))

        return MarketData(
            symbol=symbol,
            timestamp=snap["updated_at"] or datetime.now(),
            price=snap["last_price"],
            volume=snap["volume"],
            bid=snap["yes_bid"],
            ask=snap["yes_ask"],
            extra=extra,
        )

    # ------------------------------------------------------------------
    # FR-1.1 bracket metadata
    # ------------------------------------------------------------------

    def seed_bracket_metadata(self, ticker: str, fields: dict) -> None:
        """Prime a market's bracket semantics from an authoritative source.

        The WebSocket only announces metadata at lifecycle events, so a market
        opened before this process connected never sends one. Callers holding
        REST data (``KalshiProvider`` — the FR-1.1 authority) seed it here.
        Only the four bracket fields are stored; anything else is ignored.
        """
        if not ticker or not fields:
            return
        known = {k: fields.get(k) for k in BRACKET_FIELDS if fields.get(k) is not None}
        if not known:
            return
        with self._brackets_lock:
            self._brackets.setdefault(ticker, {}).update(known)
        self._bracket_warned.discard(ticker)

    def get_bracket_metadata(self, ticker: str) -> Optional[dict]:
        """Cached bracket fields for *ticker*, or ``None`` if unknown."""
        with self._brackets_lock:
            cached = self._brackets.get(ticker)
            return dict(cached) if cached else None

    def _bracket_extra(self, ticker: str) -> dict:
        """The four FR-1.1 keys for ``MarketData.extra``, always all present."""
        cached = self.get_bracket_metadata(ticker) or {}
        if not cached.get("strike_type"):
            if ticker not in self._bracket_warned:
                self._bracket_warned.add(ticker)
                logger.error(
                    "[KalshiWS] %s: no bracket semantics known (no "
                    "market_lifecycle_v2 message seen and no REST seed). "
                    "Emitting strike_type=None so bracket_payoff aborts; "
                    "call seed_bracket_metadata() from the REST provider "
                    "before trading this market (PRD FR-1.1).",
                    ticker,
                )
        return {k: cached.get(k) for k in BRACKET_FIELDS}

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, market_tickers: List[str]) -> None:
        """Subscribe to orderbook + trade channels for the given tickers.

        Subscriptions are remembered and automatically re-sent on reconnect.
        """
        if not market_tickers:
            return

        with self._subscribed_lock:
            for t in market_tickers:
                if t not in self._subscribed_tickers:
                    self._subscribed_tickers.append(t)

        # Ensure book / trade structures exist
        with self._books_lock:
            for t in market_tickers:
                if t not in self._books:
                    self._books[t] = _OrderbookState()
        with self._trades_lock:
            for t in market_tickers:
                if t not in self._trades:
                    self._trades[t] = deque(maxlen=self._max_trades)

        # Send subscription messages if we are already connected
        if self._connected.is_set() and self._ws is not None:
            self._send_subscribe(market_tickers)

    def unsubscribe(self, market_tickers: List[str]) -> None:
        """Unsubscribe from the given tickers."""
        if not market_tickers:
            return

        with self._subscribed_lock:
            self._subscribed_tickers = [
                t for t in self._subscribed_tickers if t not in market_tickers
            ]

        if self._connected.is_set() and self._ws is not None:
            self._send_unsubscribe(market_tickers)

    def _send_subscribe(self, tickers: List[str]) -> None:
        """Subscribe to quotes, trades, AND market lifecycle.

        ``market_lifecycle_v2`` is the only channel that carries the FR-1.1
        bracket fields (see module docstring); without it the feed could never
        establish contract direction from the API.
        """
        for channel in QUOTE_CHANNELS + ("market_lifecycle_v2",):
            msg = {
                "id": self._next_msg_id(),
                "cmd": "subscribe",
                "params": {
                    "channels": [channel],
                    "market_tickers": tickers,
                },
            }
            self._ws_send(msg)
            logger.info("[KalshiWS] Subscribed to %s for %s", channel, tickers)

    def _send_unsubscribe(self, tickers: List[str]) -> None:
        for channel in QUOTE_CHANNELS + ("market_lifecycle_v2",):
            msg = {
                "id": self._next_msg_id(),
                "cmd": "unsubscribe",
                "params": {
                    "channels": [channel],
                    "market_tickers": tickers,
                },
            }
            self._ws_send(msg)
            logger.info("[KalshiWS] Unsubscribed from %s for %s", channel, tickers)

    # ------------------------------------------------------------------
    # Internal: WebSocket lifecycle
    # ------------------------------------------------------------------

    def _next_msg_id(self) -> int:
        with self._msg_id_lock:
            self._msg_id += 1
            return self._msg_id

    def _ws_send(self, payload: dict) -> None:
        try:
            if self._ws and self._connected.is_set():
                self._ws.send(json.dumps(payload))
        except Exception as exc:
            logger.error("[KalshiWS] Send failed: %s", exc)

    def _start_ws_thread(self) -> None:
        """Create the WebSocketApp and run it in a daemon thread."""
        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws_thread = threading.Thread(
            target=self._run_ws_loop,
            name="kalshi-ws",
            daemon=True,
        )
        self._ws_thread.start()

    def _run_ws_loop(self) -> None:
        """Entry point for the daemon thread.  Handles auto-reconnect."""
        while not self._stop_event.is_set():
            try:
                self._ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                )
            except Exception as exc:
                logger.error("[KalshiWS] run_forever raised: %s", exc)

            # If stop was requested, exit cleanly
            if self._stop_event.is_set():
                break

            # Auto-reconnect with exponential back-off
            delay = self._reconnect_delay
            logger.info("[KalshiWS] Reconnecting in %.1f s ...", delay)
            self._stop_event.wait(timeout=delay)
            if self._stop_event.is_set():
                break

            self._reconnect_delay = min(
                self._reconnect_delay * self._BACKOFF_FACTOR,
                self._MAX_DELAY,
            )

            # Re-create the WebSocketApp for a fresh connection
            self._connected.clear()
            self._ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, ws) -> None:
        logger.info("[KalshiWS] Connection opened")
        self._reconnect_delay = self._BASE_DELAY  # reset back-off on success

        # Authenticate if we have credentials
        if not self._anonymous:
            self._send_login()

        self._connected.set()

        # Re-subscribe to any previously requested tickers
        with self._subscribed_lock:
            tickers = list(self._subscribed_tickers)
        if tickers:
            self._send_subscribe(tickers)

    def _send_login(self) -> None:
        """Authenticate the WebSocket session using RSA-signed credentials.

        Kalshi V2 WebSocket login uses the same RSA-PSS signing scheme as
        the REST API.  We sign the string ``{timestamp}GET/trade-api/ws/v2``
        and send the three auth fields in a ``login`` command.
        """
        ts = str(int(time.time() * 1000))
        # Sign against the WebSocket path
        ws_path = "/trade-api/ws/v2"
        payload_str = f"{ts}GET{ws_path}"
        signature = self._private_key.sign(
            payload_str.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        login_msg = {
            "id": self._next_msg_id(),
            "cmd": "login",
            "params": {
                "key_id": self.key_id,
                "signature": sig_b64,
                "timestamp": ts,
            },
        }
        self._ws_send(login_msg)
        logger.info(
            "[KalshiWS] Login command sent (key=%s...)",
            self.key_id[:8] if self.key_id else "?",
        )

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[KalshiWS] Non-JSON message: %s", raw[:200])
            return

        msg_type = msg.get("type")

        # Command responses (login ack, subscribe ack, errors)
        if msg_type == "response":
            self._handle_response(msg)
            return

        # Channel data. Kalshi tags data frames with ``type`` (e.g.
        # "market_lifecycle_v2"); ``channel`` is used by some proxies, so
        # accept either.
        channel = msg.get("channel") or msg_type
        if channel == "orderbook_delta":
            self._handle_orderbook_delta(msg)
        elif channel == "orderbook_snapshot":
            self._handle_orderbook_snapshot(msg)
        elif channel == "trade":
            self._handle_trade(msg)
        elif channel in LIFECYCLE_CHANNELS:
            self._handle_market_lifecycle(msg)
        elif msg_type == "error":
            logger.error("[KalshiWS] Server error: %s", msg)
        else:
            logger.debug(
                "[KalshiWS] Unhandled message type=%s channel=%s", msg_type, channel
            )

    def _on_error(self, ws, error) -> None:
        logger.error("[KalshiWS] Error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected.clear()
        self._authenticated = False
        logger.info(
            "[KalshiWS] Connection closed (code=%s msg=%s)",
            close_status_code,
            close_msg,
        )

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_response(self, msg: dict) -> None:
        """Handle command responses (login ack, subscription ack, errors)."""
        msg_id = msg.get("id")
        error = msg.get("error")
        if error:
            logger.error("[KalshiWS] Command %s failed: %s", msg_id, error)
            return

        result = msg.get("result", {})
        # Login success comes back with a member_id
        if "member_id" in result:
            self._authenticated = True
            logger.info("[KalshiWS] Authenticated (member_id=%s)", result["member_id"])
        else:
            logger.debug("[KalshiWS] Command %s acknowledged: %s", msg_id, result)

    def _handle_orderbook_snapshot(self, msg: dict) -> None:
        """Process a full orderbook snapshot message."""
        data = msg.get("data", msg.get("msg", {}))
        ticker = data.get("market_ticker")
        if not ticker:
            return

        book = self._get_or_create_book(ticker)

        yes_bids = data.get("yes", data.get("yes_bids", []))
        yes_asks = data.get("no", data.get("yes_asks", []))

        best_bid = self._best_price(yes_bids, side="bid")
        best_ask = self._best_price(yes_asks, side="ask")

        updates = {
            "yes_bid": best_bid,
            "yes_ask": best_ask,
            "no_bid": max(0.0, 1.0 - best_ask) if best_ask > 0 else 0.0,
            "no_ask": max(0.0, 1.0 - best_bid) if best_bid > 0 else 0.0,
        }
        book.update(**updates)

        snap = book.snapshot()
        if self.on_orderbook_update:
            self._safe_callback(self.on_orderbook_update, ticker, snap)

    def _handle_orderbook_delta(self, msg: dict) -> None:
        """Process an incremental orderbook delta message."""
        data = msg.get("data", msg.get("msg", {}))
        ticker = data.get("market_ticker")
        if not ticker:
            return

        book = self._get_or_create_book(ticker)

        # The delta may contain updated price/quantity for yes and no sides.
        # Extract best bid/ask from the delta's price array if present.
        updates = {}

        price = data.get("price")
        if price is not None:
            updates["last_price"] = self._parse_price(price)

        yes_bid = data.get("yes_bid") or data.get("yes_bid_dollars")
        if yes_bid is not None:
            updates["yes_bid"] = self._parse_price(yes_bid)

        yes_ask = data.get("yes_ask") or data.get("yes_ask_dollars")
        if yes_ask is not None:
            updates["yes_ask"] = self._parse_price(yes_ask)

        no_bid = data.get("no_bid") or data.get("no_bid_dollars")
        if no_bid is not None:
            updates["no_bid"] = self._parse_price(no_bid)

        no_ask = data.get("no_ask") or data.get("no_ask_dollars")
        if no_ask is not None:
            updates["no_ask"] = self._parse_price(no_ask)

        # Some delta formats deliver arrays of [price, delta_qty] pairs.
        # Extract best bid / ask from those arrays.
        for side_key, update_field in [
            ("yes", "yes_bid"),
            ("no", "no_bid"),
        ]:
            side_data = data.get(side_key, [])
            if isinstance(side_data, list) and side_data:
                best = self._best_price(side_data, side="bid")
                if best > 0:
                    updates[update_field] = best

        # Derive complementary no prices from yes when the delta only
        # carries yes-side updates.
        if "yes_bid" in updates and "no_ask" not in updates:
            updates["no_ask"] = max(0.0, 1.0 - updates["yes_bid"])
        if "yes_ask" in updates and "no_bid" not in updates:
            updates["no_bid"] = max(0.0, 1.0 - updates["yes_ask"])

        if updates:
            book.update(**updates)

        snap = book.snapshot()
        if self.on_orderbook_update:
            self._safe_callback(self.on_orderbook_update, ticker, snap)

    def _handle_market_lifecycle(self, msg: dict) -> None:
        """Cache FR-1.1 bracket semantics from a market_lifecycle_v2 message.

        Two shapes carry them (AsyncAPI spec, 2026-07-25):

        * ``event_type=created``  -> fields live under ``additional_metadata``
        * ``event_type=metadata_updated`` -> fields are top-level in ``msg``

        Both are merged into the per-ticker cache; a field absent from an
        update never clears a previously known value, and nothing here reads
        the ticker string.
        """
        data = msg.get("msg", msg.get("data", {})) or {}
        ticker = data.get("market_ticker")
        if not ticker:
            return

        meta = data.get("additional_metadata") or {}
        fields = {}
        for key in BRACKET_FIELDS:
            # Top-level wins (metadata_updated is the more recent truth).
            value = data.get(key)
            if value is None:
                value = meta.get(key)
            if value is not None:
                fields[key] = value

        if not fields:
            logger.debug(
                "[KalshiWS] %s lifecycle event %s carried no bracket fields",
                ticker,
                data.get("event_type"),
            )
            return

        self.seed_bracket_metadata(ticker, fields)
        logger.info(
            "[KalshiWS] %s bracket semantics from lifecycle (%s): %s",
            ticker,
            data.get("event_type"),
            {
                k: fields[k]
                for k in ("strike_type", "floor_strike", "cap_strike")
                if k in fields
            },
        )

    def _handle_trade(self, msg: dict) -> None:
        """Process a trade notification message."""
        data = msg.get("data", msg.get("msg", {}))
        ticker = data.get("market_ticker")
        if not ticker:
            return

        trade_price = self._parse_price(data.get("yes_price", data.get("price", 0)))
        count = int(data.get("count", data.get("quantity", 1)))
        taker_side = data.get("taker_side", data.get("side", "unknown"))

        record = _TradeRecord(
            ticker=ticker,
            price=trade_price,
            count=count,
            taker_side=taker_side,
            ts=datetime.now(),
        )

        with self._trades_lock:
            if ticker not in self._trades:
                self._trades[ticker] = deque(maxlen=self._max_trades)
            self._trades[ticker].append(record)

        # Update last_price in the orderbook state
        book = self._get_or_create_book(ticker)
        book.update(last_price=trade_price)

        if self.on_trade:
            self._safe_callback(self.on_trade, ticker, record)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_create_book(self, ticker: str) -> _OrderbookState:
        with self._books_lock:
            if ticker not in self._books:
                self._books[ticker] = _OrderbookState()
            return self._books[ticker]

    @staticmethod
    def _best_price(levels, side: str = "bid") -> float:
        """Extract the best price from a list of ``[price, qty]`` pairs.

        For bids we want the highest price; for asks the lowest (non-zero).
        """
        if not levels or not isinstance(levels, list):
            return 0.0
        try:
            prices = []
            for entry in levels:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    p, q = float(entry[0]), float(entry[1])
                    # A qty of 0 means the level was removed
                    if q > 0:
                        # Normalise: if >1 assume cents
                        prices.append(p / 100.0 if p > 1.0 else p)
                elif isinstance(entry, (int, float)):
                    p = float(entry)
                    prices.append(p / 100.0 if p > 1.0 else p)
            if not prices:
                return 0.0
            return max(prices) if side == "bid" else min(prices)
        except Exception:
            return 0.0

    @staticmethod
    def _safe_callback(fn: Callable, *args) -> None:
        """Invoke a user callback without letting it crash the WS thread."""
        try:
            fn(*args)
        except Exception as exc:
            logger.error("[KalshiWS] Callback error: %s", exc)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def subscribed_tickers(self) -> List[str]:
        with self._subscribed_lock:
            return list(self._subscribed_tickers)

    def get_recent_trades(self, ticker: str, n: int = 10) -> List[dict]:
        """Return the *n* most recent trades for *ticker* as dicts."""
        with self._trades_lock:
            buf = self._trades.get(ticker)
            if not buf:
                return []
            trades = list(buf)[-n:]
        return [
            {
                "ticker": t.ticker,
                "price": t.price,
                "count": t.count,
                "taker_side": t.taker_side,
                "ts": t.ts.isoformat(),
            }
            for t in trades
        ]

    def get_orderbook(self, ticker: str) -> Optional[dict]:
        """Return a snapshot of the current orderbook for *ticker*."""
        with self._books_lock:
            book = self._books.get(ticker)
        if book is None:
            return None
        return book.snapshot()

    def stop(self) -> None:
        """Shut down the WebSocket connection and background thread."""
        if not hasattr(self, "_stop_event"):
            # __del__ can reach a partially-constructed instance (e.g. one
            # built with __new__ in a test, or a failed __init__); raising
            # from a deallocator is noise, not information.
            return
        logger.info("[KalshiWS] Stopping ...")
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        self._connected.clear()
        self._authenticated = False
        logger.info("[KalshiWS] Stopped")

    def __del__(self):
        self.stop()
