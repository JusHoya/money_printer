from typing import List, Dict, Optional, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import re
import math
import json
import os
import random
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from src.utils.logger import logger
from src.core.bracket_payoff import (
    BracketSpecError,
    is_weather_symbol,
    settles_yes,
    spec_from_position,
)
from src.core.weather_settlement import (
    SETTLEMENT_TRUTH_GRACE_HOURS,
    city_key_for_station,
    city_keys,
    hours_since_settlement_day_close,
    resolve_settlement_high,
    settlement_close_for,
)

# PRD FR-4.4 (Phase 4). The AAA gas payoff and its truth resolver live in
# src/data/gas_settlement.py (workstream B) and are CONSUMED here, never
# reimplemented: its rule is `value > floor_strike` strictly, proven against
# 1,506 published settlements including 15 that landed exactly on their strike
# and all paid NO. Imported under gas_* aliases because the two payoff modules
# expose deliberately identical function names for deliberately different rules
# — a bare `settles_yes` in this file must keep meaning the temperature one.
from src.data.gas_settlement import (
    GasSpecError,
    get_series as get_gas_series,
    is_gas_symbol,
    resolve_settlement_value as resolve_gas_settlement_value,
    series_for as gas_series_for,
    settlement_date_for as gas_settlement_date_for,
    settles_yes as gas_settles_yes,
    spec_from_position as gas_spec_from_position,
)


def is_held_to_settlement(symbol) -> bool:
    """True for every contract family that may leave the book ONLY by settling.

    One predicate, two families, so the "held to settlement" invariant is
    defined in exactly one place:

    * weather (``KXHIGH*``) — PRD FR-1.5, settles on the NWS Climatological
      Report daily high (FR-1.2);
    * AAA gas (``KXAAAGAS*``) — PRD FR-4.4, settles on the AAA national average
      published for one calendar date.

    Both are binary event contracts whose outcome is undetermined until an
    external authority publishes a number. Every non-settlement close reason —
    ``TIME_LIMIT``, cycle-reset liquidation, stop-losses, profit targets,
    ``EARLY_SETTLEMENT`` — crystallizes ordinary quote oscillation instead, and
    that is exactly why the project's weather PnL history (188 ``TIME_LIMIT``
    and 69 ``CYCLE_RESET`` closes) was declared meaningless.

    The two families are provably disjoint (``KXHIGH`` vs ``KXAAAGAS``), which
    ``tests/test_gas_lifecycle.py`` pins, so a symbol can never be governed by
    both payoff rules.
    """
    return is_weather_symbol(symbol) or is_gas_symbol(symbol)


#: How long after an AAA settlement date *begins* a missing published value
#: stops being ordinary latency and starts being a fault worth paging about.
#:
#: This is measured from local midnight at the START of the settlement date,
#: not its close, because gas truth is published on the morning OF the
#: settlement date (Kalshi's ``expected_expiration_time`` is 10:00 ET on the
#: date itself) whereas a weather CLI product is issued the morning AFTER the
#: settlement day. Measuring gas from the day's close — the weather
#: convention — would leave the age negative until the date ended and so could
#: never escalate a value that was already ~14 hours late. 36 hours therefore
#: puts the escalation at noon ET on the following day: about 26 hours after
#: the expected publication, and a full extra recorder run of slack.
GAS_SETTLEMENT_TRUTH_GRACE_HOURS = 36.0


def _hours_since_gas_settlement_date_open(symbol, now: Optional[datetime] = None):
    """Hours since the AAA settlement date began, in the series' timezone.

    Negative before the date starts. ``None`` when the series, the date label
    or the timezone cannot be established — which makes the caller treat
    missing truth as PENDING rather than escalating on a symbol it cannot even
    date.
    """
    series = gas_series_for(symbol)
    day = gas_settlement_date_for(symbol)
    if series is None or day is None:
        return None
    try:
        tz = ZoneInfo(get_gas_series(series).timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError, GasSpecError) as exc:
        logger.error(
            "[OMS] %s: gas settlement timezone unavailable (%s); refusing to "
            "age the settlement date in UTC",
            symbol,
            exc,
        )
        return None
    date_open = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return (current - date_open).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_STATE_FILE = _REPO_ROOT / "data" / "exchange_state.json"
_SCHEMA_VERSION = 1


def _dt_to_str(v):
    """Convert datetime to ISO string; pass through non-datetime values."""
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _str_to_dt(v):
    """Convert ISO string back to datetime where applicable."""
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
    return v


def _serialize_position(pos: dict) -> dict:
    """Deep-copy a position dict and convert all datetime fields to strings."""
    out = {}
    for k, v in pos.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            # profit_targets list contains plain dicts — no datetimes inside
            out[k] = v
        else:
            out[k] = v
    return out


def _deserialize_position(pos: dict) -> dict:
    """Restore datetime fields in a deserialized position dict."""
    dt_keys = {"open_time", "close_time", "expiration_time"}
    out = {}
    for k, v in pos.items():
        if k in dt_keys and isinstance(v, str):
            try:
                out[k] = datetime.fromisoformat(v)
            except ValueError:
                out[k] = None
        else:
            out[k] = v
    return out


# ==============================================================================
# ORDER BOOK & LIMIT ORDER SUPPORT
# ==============================================================================


class OrderStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class LimitOrder:
    """Represents a limit order waiting to be filled."""

    order_id: int
    symbol: str
    side: str  # 'buy' or 'sell'
    limit_price: float
    quantity: int
    created_at: datetime
    expires_at: datetime
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_at: Optional[datetime] = None
    stop_loss: float = 0.0
    trailing_rules: Optional[dict] = None
    expiration_time: Optional[datetime] = None  # Contract expiration


@dataclass
class OrderBookLevel:
    """A single price level in the order book."""

    price: float
    quantity: int
    order_count: int = 1


class LimitOrderBook:
    """
    Manages limit orders and simulates order book depth tracking.

    Features:
    - Limit order placement with patience timeout
    - Order book depth simulation
    - Spread analysis for market-making opportunities
    - Fill simulation based on price movement
    """

    def __init__(
        self, on_fill: Optional[Callable] = None, default_patience_seconds: int = 30
    ):
        self.pending_orders: Dict[int, LimitOrder] = {}
        self.filled_orders: List[LimitOrder] = []
        self.cancelled_orders: List[LimitOrder] = []
        self.next_order_id = 1
        self.on_fill = on_fill
        self.default_patience = default_patience_seconds

        # Simulated order book state (for depth tracking)
        self.order_books: Dict[str, Dict] = {}  # {symbol: {'bids': [], 'asks': []}}

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        limit_price: float,
        quantity: int,
        patience_seconds: Optional[int] = None,
        stop_loss: float = 0.0,
        trailing_rules: Optional[dict] = None,
        contract_expiration: Optional[datetime] = None,
    ) -> LimitOrder:
        """
        Place a limit order with patience timeout.

        :param patience_seconds: How long to wait for fill before cancelling
        :returns: The created LimitOrder
        """
        patience = patience_seconds or self.default_patience
        now = datetime.now()

        order = LimitOrder(
            order_id=self.next_order_id,
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            quantity=quantity,
            created_at=now,
            expires_at=now + timedelta(seconds=patience),
            stop_loss=stop_loss,
            trailing_rules=trailing_rules,
            expiration_time=contract_expiration,
        )

        self.pending_orders[order.order_id] = order
        self.next_order_id += 1

        logger.info(
            f"[OrderBook] 📋 LIMIT ORDER #{order.order_id}: {side.upper()} {quantity}x {symbol} @ {limit_price:.2f} (patience: {patience}s)"
        )

        return order

    def update_order_book(self, symbol: str, bids: List[tuple], asks: List[tuple]):
        """
        Update the simulated order book for a symbol.

        :param bids: List of (price, quantity) tuples, sorted descending
        :param asks: List of (price, quantity) tuples, sorted ascending
        """
        self.order_books[symbol] = {
            "bids": [OrderBookLevel(price=p, quantity=q) for p, q in bids],
            "asks": [OrderBookLevel(price=p, quantity=q) for p, q in asks],
            "updated_at": datetime.now(),
        }

    def get_spread_info(self, symbol: str) -> Optional[Dict]:
        """
        Get spread information for a symbol.

        Returns: {
            'bid': best_bid,
            'ask': best_ask,
            'spread': spread,
            'spread_pct': spread_percentage,
            'mid': midpoint
        }
        """
        if symbol not in self.order_books:
            return None

        book = self.order_books[symbol]
        if not book["bids"] or not book["asks"]:
            return None

        best_bid = book["bids"][0].price
        best_ask = book["asks"][0].price
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2
        spread_pct = (spread / mid) * 100 if mid > 0 else 0

        return {
            "bid": best_bid,
            "ask": best_ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "mid": mid,
            "bid_depth": sum(lvl.quantity for lvl in book["bids"][:3]),
            "ask_depth": sum(lvl.quantity for lvl in book["asks"][:3]),
        }

    def check_fills(
        self, symbol: str, current_bid: float, current_ask: float
    ) -> List[LimitOrder]:
        """
        Check if any pending orders can be filled at current prices.

        :returns: List of orders that were just filled
        """
        now = datetime.now()
        newly_filled = []

        for order_id, order in list(self.pending_orders.items()):
            if order.symbol != symbol:
                continue

            # Check expiration
            if now >= order.expires_at:
                order.status = OrderStatus.EXPIRED
                self.cancelled_orders.append(order)
                del self.pending_orders[order_id]
                logger.info(
                    f"[OrderBook] ⏰ EXPIRED: Order #{order_id} (patience exceeded)"
                )
                continue

            # Check fill conditions
            filled = False
            fill_price = 0.0

            if order.side == "buy":
                # Buy limit fills if ask drops to or below limit price
                if current_ask <= order.limit_price:
                    filled = True
                    fill_price = current_ask
            else:  # sell
                # Sell limit fills if bid rises to or at/above limit price
                if current_bid >= order.limit_price:
                    filled = True
                    fill_price = current_bid

            if filled:
                order.status = OrderStatus.FILLED
                order.filled_price = fill_price
                order.filled_at = now
                newly_filled.append(order)
                self.filled_orders.append(order)
                del self.pending_orders[order_id]

                logger.info(
                    f"[OrderBook] ✅ FILLED: Order #{order_id} @ {fill_price:.2f} (limit was {order.limit_price:.2f})"
                )

                if self.on_fill:
                    self.on_fill(order)

        return newly_filled

    def cancel_order(self, order_id: int) -> bool:
        """Cancel a pending order."""
        if order_id in self.pending_orders:
            order = self.pending_orders[order_id]
            order.status = OrderStatus.CANCELLED
            self.cancelled_orders.append(order)
            del self.pending_orders[order_id]
            logger.info(f"[OrderBook] ❌ CANCELLED: Order #{order_id}")
            return True
        return False

    def cancel_all_for_symbol(self, symbol: str) -> int:
        """Cancel all pending orders for a symbol."""
        cancelled = 0
        for order_id in list(self.pending_orders.keys()):
            if self.pending_orders[order_id].symbol == symbol:
                self.cancel_order(order_id)
                cancelled += 1
        return cancelled

    def get_pending_count(self) -> int:
        return len(self.pending_orders)

    def get_stats(self) -> Dict:
        return {
            "pending": len(self.pending_orders),
            "filled": len(self.filled_orders),
            "cancelled": len(
                [o for o in self.cancelled_orders if o.status == OrderStatus.CANCELLED]
            ),
            "expired": len(
                [o for o in self.cancelled_orders if o.status == OrderStatus.EXPIRED]
            ),
        }


# ==============================================================================
# SIMULATED EXCHANGE (ENHANCED)
# ==============================================================================


class SimulatedExchange:
    """
    A lightweight Order Matching System (OMS) for paper trading.
    Tracks positions and simulates fills/exits based on live market data.

    Sprint 4 enhancements:
    - Fee deduction on every trade (maker/taker)
    - Realistic fill simulation (maker fills ~60%)
    - Full order audit trail
    """

    # ------------------------------------------------------------------
    # Task E: penny-floor fill model knobs (only consulted when
    # ``realistic_fills`` is True; class-level so studies can tweak without
    # touching the constructor signature).
    # ------------------------------------------------------------------
    PENNY_FLOOR_LO = 0.01  # inclusive lower bound of the "penny floor" band
    PENNY_FLOOR_HI = 0.05  # inclusive upper bound
    DEFAULT_PENNY_FILL_PROB = 0.5  # base fill probability inside the band

    def __init__(
        self,
        on_close=None,
        state_file: Optional[Path] = None,
        realistic_fills: bool = False,
        penny_fill_prob: float = None,
        fill_rng_seed: Optional[int] = None,
    ):
        self.positions = []  # List of active trades
        self.closed_trades = []  # History
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.on_close = on_close  # Callback function(position)
        # Optional operator-visible alert sink, wired by the orchestrator to
        # ``Dashboard.alert``. Used for conditions an operator must see even
        # though the engine keeps running (e.g. a weather position whose
        # bracket semantics cannot be established). None = log-only.
        self.on_alert = None
        # Monotonic position-id floor. Position ids were derived purely from
        # ``len(positions) + len(closed_trades) + 1``; that is fine while every
        # position is closed at each cycle boundary, but FR-1.5 lets weather
        # positions survive the boundary, so an id must never be reissued while
        # its owner is still open. See _allocate_position_id.
        self._next_id = 1

        # Simulation Settings
        self.TAKE_PROFIT_PCT = 0.15
        self.STOP_LOSS_PCT = 0.15
        self.TIME_LIMIT_MIN = 60

        # Sprint 4: fee tracking
        self.total_fees_paid = 0.0
        self.maker_fills = 0
        self.taker_fills = 0

        # ------------------------------------------------------------------
        # Task F (2026-06-03): immutable cumulative ledger.
        #
        # ``realized_pnl`` is a FRAGMENT, not a lifetime total: reset_stats()
        # zeroes it on every balance sync (see reset_stats below). To expose a
        # trustworthy lifetime net for real-capital gating we keep three
        # monotonic accumulators that reset_stats() NEVER touches. They are
        # incremented on every fill/close in lockstep with realized_pnl/fees
        # and are persisted across restarts. ``closed_trades`` remains the
        # source of truth; these scalars are a cheap, reconcilable cache.
        # ------------------------------------------------------------------
        self.cumulative_realized_pnl = (
            0.0  # sum of every close's net pnl (post exit-fee)
        )
        self.cumulative_fees_paid = 0.0  # all fees ever paid (entry + exit)
        self.cumulative_entry_fees = 0.0  # entry fees only (pnl is NOT net of these)

        # ------------------------------------------------------------------
        # Task E (2026-06-03): configurable probabilistic fill model.
        #
        # DEFAULT OFF. When False the exchange is byte-identical to the legacy
        # behaviour (every requested open fills at the requested price), so the
        # live paper-trading regime is unchanged. When True, orders resting at
        # the penny floor ($0.01-$0.05) only fill with probability < 1 and
        # suffer an adverse-selection penalty (a contract that has already
        # repriced toward a win is *less* likely to have actually filled at the
        # floor). Used by scripts/measure_fill_realism.py and offline studies
        # to quantify how much of the edge is penny-floor lottery wins.
        # ------------------------------------------------------------------
        self.realistic_fills = realistic_fills
        self.penny_fill_prob = (
            self.DEFAULT_PENNY_FILL_PROB if penny_fill_prob is None else penny_fill_prob
        )
        # Dedicated RNG so fill draws are reproducible and never perturb the
        # global random stream. Only constructed/used when realistic_fills.
        self._fill_rng = random.Random(fill_rng_seed)
        # Diagnostics: how many penny-floor orders were skipped vs requested.
        self.penny_floor_requested = 0
        self.penny_floor_skipped = 0

        # Sprint 4: order audit trail
        self.order_audit: list = []

        # Sprint 8: disk persistence.
        # When state_file is None persistence is DISABLED — no reads or writes.
        # Pass state_file explicitly (e.g. _DEFAULT_STATE_FILE) to enable.
        self._state_file: Optional[Path] = (
            Path(state_file) if state_file is not None else None
        )
        self._load_state()

    # ------------------------------------------------------------------
    # Sprint 8: persistence helpers
    # ------------------------------------------------------------------

    def _load_state(self):
        """Load exchange state from disk on startup.

        If state_file is None (persistence disabled), returns immediately.
        If the file is missing, start fresh (normal cold-start).
        If the file is corrupt or has the wrong schema_version, rename it
        to ``*.corrupt-<timestamp>`` and start fresh without crashing.
        """
        if self._state_file is None:
            return  # Persistence disabled

        path = self._state_file
        if not path.exists():
            return  # Fresh start — nothing to restore

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            if data.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError(
                    f"Unexpected schema_version {data.get('schema_version')!r}"
                )

            # Restore scalar counters
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.unrealized_pnl = float(data.get("unrealized_pnl", 0.0))
            self.total_fees_paid = float(data.get("total_fees_paid", 0.0))
            self.maker_fills = int(data.get("maker_fills", 0))
            self.taker_fills = int(data.get("taker_fills", 0))

            # Restore open positions (deserialize datetime strings). Drop
            # qty<=0 shells at load so the no-ghost invariant holds even for
            # state files written before the FR-0.6 partial-close fix.
            loaded_positions = [
                _deserialize_position(p) for p in data.get("positions", [])
            ]
            self.positions = [
                pos for pos in loaded_positions if pos.get("quantity", 0) > 0
            ]
            n_shells_dropped = len(loaded_positions) - len(self.positions)

            # PRD FR-F0.1: weather positions persisted before the expiration
            # stamp existed (maia's 2026-09-01 book) carry no expiration_time,
            # so the EXPIRATION CHECK skips them forever. Backfill the
            # settlement-day close so they can settle on the next sweep.
            n_expiry_backfilled = self._backfill_weather_expirations()

            # Restore closed trades (deserialize datetime strings)
            self.closed_trades = [
                _deserialize_position(p) for p in data.get("closed_trades", [])
            ]

            # Task F: restore the immutable cumulative ledger. Older state files
            # written before this field existed won't have the keys; in that
            # case backfill from closed_trades so the lifetime total is correct
            # on first upgrade rather than silently starting from zero.
            if "cumulative_realized_pnl" in data:
                self.cumulative_realized_pnl = float(data["cumulative_realized_pnl"])
                self.cumulative_fees_paid = float(data.get("cumulative_fees_paid", 0.0))
                self.cumulative_entry_fees = float(
                    data.get("cumulative_entry_fees", 0.0)
                )
            else:
                self._backfill_cumulative_from_closed_trades()

            # Task F: reconcile the scalar cache against the source of truth.
            self._reconcile_cumulative(context="load")

            logger.info(
                f"[OMS] Loaded exchange state: {len(self.positions)} open, "
                f"{len(self.closed_trades)} closed, realized=${self.realized_pnl:+.2f}, "
                f"cumulative_net=${self.get_cumulative_net_pnl():+.2f}"
            )

            # Restored positions and closed rows own their ids; never reissue
            # one. ``next_position_id`` is authoritative when present (written
            # by _save_state since 2026-07-25); _bump_next_id() then raises it
            # to the open+closed high-water mark, which is also the whole
            # recovery story for a legacy state file that predates the key.
            self._next_id = max(
                int(getattr(self, "_next_id", 1)),
                int(data.get("next_position_id", 1) or 1),
            )
            self._bump_next_id()
            self._warn_on_duplicate_ids(context="load")

            if n_shells_dropped:
                logger.info(
                    f"[OMS] STATE HYGIENE: dropped {n_shells_dropped} qty<=0 "
                    f"shell position(s) at load; persisting cleaned state"
                )
            if n_shells_dropped or n_expiry_backfilled:
                self._save_state()

        except Exception as exc:
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            corrupt_path = path.with_suffix(f".json.corrupt-{ts}")
            try:
                path.rename(corrupt_path)
            except Exception:
                pass  # Best-effort rename
            logger.warning(
                f"[OMS] Corrupt/incompatible exchange state file ({exc}). "
                f"Renamed to {corrupt_path.name}. Starting fresh."
            )
            # Reset to blank slate (defaults already set in __init__ before _load_state)
            self.positions = []
            self.closed_trades = []
            self.realized_pnl = 0.0
            self.unrealized_pnl = 0.0
            self.total_fees_paid = 0.0
            self.maker_fills = 0
            self.taker_fills = 0
            # Task F: a corrupt file means we have no trustworthy history —
            # the cumulative ledger must reset alongside closed_trades so the
            # two stay reconciled.
            self.cumulative_realized_pnl = 0.0
            self.cumulative_fees_paid = 0.0
            self.cumulative_entry_fees = 0.0
            self._next_id = 1

    def _backfill_weather_expirations(self) -> int:
        """Stamp the settlement-day close onto open weather positions lacking one.

        PRD FR-F0.1. Returns the number of positions changed; logs one INFO
        line per position so the operator can see exactly which restored rows
        were repaired. A position that already carries an expiration (aware or
        naive) is left alone, as is every non-weather position.
        """
        count = 0
        for pos in self.positions:
            if pos.get("expiration_time") is not None:
                continue
            symbol = pos.get("symbol", "")
            if not is_weather_symbol(symbol):
                continue
            close = settlement_close_for(symbol)
            if close is None:
                continue
            pos["expiration_time"] = close
            count += 1
            logger.info(
                "[OMS] BACKFILL expiration_time id=%s symbol=%s -> %s "
                "(settlement-day close, FR-F0.1)",
                pos.get("id"),
                symbol,
                close.isoformat(),
            )
        return count

    def _warn_on_duplicate_ids(self, context: str = ""):
        """Log once if the restored book already contains duplicate ids.

        A state file written before ``next_position_id`` existed can carry
        duplicates (the production VM has 158 among 1583 closed rows). We do
        NOT rewrite history to repair them — the journal, the reconciler and
        every archived analysis reference those ids. Loading recovers to the
        open+closed high-water mark (see ``_bump_next_id``) so no *new*
        duplicate is created, and this makes the pre-existing damage visible
        instead of silent.
        """
        all_ids = [p.get("id") for p in self.positions] + [
            t.get("id") for t in self.closed_trades
        ]
        int_ids = [i for i in all_ids if isinstance(i, int)]
        dupes = len(int_ids) - len(set(int_ids))
        if dupes:
            logger.warning(
                "[OMS] POSITION ID DUPLICATES (%s): %d duplicate id(s) across "
                "%d open + %d closed rows in the restored state. Pre-existing "
                "history is left untouched; new ids resume above %d so no "
                "further collision is created.",
                context,
                dupes,
                len(self.positions),
                len(self.closed_trades),
                self._next_id - 1,
            )

    def _save_state(self):
        """Atomically persist exchange state to disk.

        No-op when state_file is None (persistence disabled).
        Uses ``tempfile + os.replace`` so a crash mid-write cannot corrupt
        the state file.  The ``data/`` directory is created if missing.
        """
        if self._state_file is None:
            return  # Persistence disabled

        path = self._state_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            payload = {
                "schema_version": _SCHEMA_VERSION,
                "saved_at": datetime.now().isoformat(),
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized_pnl,
                "total_fees_paid": self.total_fees_paid,
                "maker_fills": self.maker_fills,
                "taker_fills": self.taker_fills,
                # Monotonic id floor. Without it a restart recomputed the floor
                # from the open book alone and handed the next position an id
                # that a closed trade already owned (FR-1.5 makes that the
                # normal case, not a corner case).
                "next_position_id": int(getattr(self, "_next_id", 1)),
                # Task F: immutable cumulative ledger (never zeroed by reset_stats)
                "cumulative_realized_pnl": self.cumulative_realized_pnl,
                "cumulative_fees_paid": self.cumulative_fees_paid,
                "cumulative_entry_fees": self.cumulative_entry_fees,
                "positions": [_serialize_position(p) for p in self.positions],
                "closed_trades": [_serialize_position(p) for p in self.closed_trades],
            }

            # Atomic write: write to temp file in the same directory, then rename
            dir_ = str(path.parent)
            fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, default=str)
                os.replace(tmp_path, str(path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        except Exception as exc:
            logger.warning(f"[OMS] Failed to persist exchange state: {exc}")

    # ------------------------------------------------------------------
    # Task F (2026-06-03): immutable cumulative ledger
    # ------------------------------------------------------------------

    # Tolerance for reconciliation warnings. closed_trades stores money to the
    # cent, so any divergence above half a cent is a real bug, not float dust.
    _RECONCILE_TOLERANCE = 0.005

    def _backfill_cumulative_from_closed_trades(self):
        """Rebuild the cumulative accumulators from ``closed_trades``.

        Used when upgrading a state file written before the cumulative ledger
        existed. ``pnl`` on each closed trade is already net of its exit fee,
        so the cumulative net = sum(pnl) - sum(entry_fee).
        """
        self.cumulative_realized_pnl = sum(
            float(t.get("pnl", 0.0)) for t in self.closed_trades
        )
        self.cumulative_entry_fees = sum(
            float(t.get("entry_fee", 0.0)) for t in self.closed_trades
        )
        exit_fees = sum(float(t.get("exit_fee", 0.0)) for t in self.closed_trades)
        self.cumulative_fees_paid = self.cumulative_entry_fees + exit_fees
        logger.info(
            f"[OMS] Backfilled cumulative ledger from {len(self.closed_trades)} "
            f"closed trades: net=${self.get_cumulative_net_pnl():+.2f}"
        )

    def _reconcile_cumulative(self, context: str = ""):
        """Warn if the cumulative scalar has drifted from closed_trades.

        ``closed_trades`` is the source of truth; the scalar is a cheap cache.
        A divergence beyond a cent means a close path forgot to bump the
        accumulator (or vice versa) — surface it loudly rather than trust a
        silently-wrong lifetime total when gating real capital.
        """
        truth = sum(float(t.get("pnl", 0.0)) for t in self.closed_trades)
        drift = self.cumulative_realized_pnl - truth
        if abs(drift) > self._RECONCILE_TOLERANCE:
            logger.warning(
                f"[OMS] Cumulative ledger divergence ({context}): "
                f"scalar=${self.cumulative_realized_pnl:+.2f} vs "
                f"sum(closed_trades)=${truth:+.2f} (drift=${drift:+.2f}). "
                f"closed_trades is authoritative."
            )

    def get_cumulative_net_pnl(self) -> float:
        """Return the true lifetime net PnL, net of ALL fees.

        Each closed trade's ``pnl`` is already net of its exit fee but NOT its
        entry fee (the entry fee is deducted into ``realized_pnl`` separately
        at open time). So the lifetime net after every fee is:

            sum(closed_trades.pnl) - sum(entry_fees)

        We use the immutable accumulators (not the reset-prone ``realized_pnl``)
        so this survives balance syncs and restarts.
        """
        return self.cumulative_realized_pnl - self.cumulative_entry_fees

    # ------------------------------------------------------------------
    # Task E (2026-06-03): probabilistic penny-floor fill model
    # ------------------------------------------------------------------

    def penny_floor_fill_probability(
        self, entry_price: float, side: str, contract_side: str = "YES"
    ) -> float:
        """Probability that a penny-floor resting order actually fills.

        Only the cheap band (``PENNY_FLOOR_LO``..``PENNY_FLOOR_HI``) is
        derated; everything else fills with probability 1.0 so the model is a
        strict, isolated overlay on the legacy behaviour.

        Inside the band the probability is ``penny_fill_prob`` scaled by a
        queue-position / adverse-selection penalty: the cheaper the contract,
        the deeper the queue at the floor and the worse the adverse selection
        (a $0.01 YES that is destined to win was almost certainly never the
        order that got filled at the floor). We model that by ramping the
        probability down linearly toward the bottom of the band:

            penalty = 0.5 + 0.5 * (price - LO) / (HI - LO)   in [0.5, 1.0]

        so a $0.05 order keeps the full base probability while a $0.01 order
        keeps only half of it. Returns 1.0 when the model is disabled or the
        price is outside the band.
        """
        if not self.realistic_fills:
            return 1.0
        # Effective price the resting buy is competing at. For a "buy" of a YES
        # contract the relevant price is entry_price; for a NO buy the order
        # rests at (1 - entry_price) on the YES book but the penny-floor risk
        # is governed by whichever side is cheap, so use the smaller leg.
        price = entry_price
        if contract_side == "NO":
            price = min(entry_price, 1.0 - entry_price)
        if price < self.PENNY_FLOOR_LO or price > self.PENNY_FLOOR_HI:
            return 1.0
        band = max(1e-9, self.PENNY_FLOOR_HI - self.PENNY_FLOOR_LO)
        penalty = 0.5 + 0.5 * (price - self.PENNY_FLOOR_LO) / band  # [0.5, 1.0]
        return max(0.0, min(1.0, self.penny_fill_prob * penalty))

    # ------------------------------------------------------------------
    # Operator alerts + position ids
    # ------------------------------------------------------------------

    def _alert(self, message: str):
        """Surface an operator-visible alert, if a sink is wired. Never raises."""
        if not self.on_alert:
            return
        try:
            self.on_alert(message)
        except Exception as exc:  # an alert sink must never break settlement
            logger.warning("[OMS] alert sink failed (%s): %s", exc, message)

    def _known_position_ids(self) -> set:
        """Every id this exchange currently accounts for — open AND closed.

        ``closed_trades`` matters as much as ``positions``: the trade journal,
        the reconciler and Phase 1 exit criterion 5 all join a position to its
        closed row by id, so a reissued id silently merges two different trades
        in the evidence. (The production VM state carries 158 duplicate ids
        among 1583 closed rows from the era when only the open book was
        consulted.)
        """
        ids = {p.get("id") for p in self.positions if isinstance(p.get("id"), int)}
        ids |= {t.get("id") for t in self.closed_trades if isinstance(t.get("id"), int)}
        return ids

    def _allocate_position_id(self) -> int:
        """Next position id, unique against every open position and closed trade.

        The legacy formula ``len(positions) + len(closed_trades) + 1`` is kept
        as the floor so ids are unchanged for the ordinary case (fresh exchange,
        every position closed at each cycle boundary). ``_next_id`` raises that
        floor when positions survive a boundary (FR-1.5) or are restored from
        disk.

        Guarantee: the returned id is not in use by any position in
        ``self.positions`` and does not appear on any row in
        ``self.closed_trades``. It is NOT a guarantee about rows that have
        already been evicted from ``closed_trades`` (nothing evicts them today)
        or about ids issued by a *different* exchange instance writing the same
        journal.
        """
        candidate = len(self.positions) + len(self.closed_trades) + 1
        pid = max(candidate, int(getattr(self, "_next_id", 1)))
        in_use = self._known_position_ids()
        while pid in in_use:
            pid += 1
        self._next_id = pid + 1
        return pid

    def _bump_next_id(self):
        """Raise the id floor above every id this exchange has ever issued.

        Open positions alone are not enough: a high-id weather survivor (FR-1.5)
        pins ``_next_id`` above a dense closed list, which is exactly the state
        in which the legacy ``len(positions)+len(closed)+1`` floor lands back
        inside the closed range and starts reissuing ids.
        """
        ids = self._known_position_ids()
        self._next_id = max(list(ids) + [int(getattr(self, "_next_id", 1)) - 1]) + 1

    def open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: int,
        stop_loss: float = 0.0,
        trailing_rules: dict = None,
        expiration_time=None,
        strategy_name: str = None,
        contract_side: str = "YES",
        disable_profit_targets: bool = False,
        is_maker: Optional[bool] = None,
        strike: float = None,
        strike_type: str = None,
        floor_strike: float = None,
        cap_strike: float = None,
    ):
        """Records a new position with fee deduction and audit trail.

        Sprint 4: deducts estimated fees from realized PnL and tracks
        maker/taker fill counts.

        ``is_maker``
            The fill type, when the caller knows it. ``None`` means *unknown*,
            which is the state of the production paper-trading path today: no
            order router runs in the live loop, so nothing between the strategy
            and this method can say whether the order rested or crossed. An
            unknown fill is booked as a **taker** — the expensive side.

            This defaulted to ``True`` before PRD Phase 2 corrected the maker
            multiplier to zero. That combination booked $0.00 of fees on every
            simulated fill, which flatters exactly the ledger the FR-5 capital
            gate reads. Booking the free side of an unknown fill understates
            cost; booking the expensive side understates edge, and only the
            second error is safe to make before risking capital.

        Task E: when ``realistic_fills`` is enabled a penny-floor resting order
        may fail to fill — in that case NO position is created, no fee is paid,
        and the method returns ``None`` early. When ``realistic_fills`` is False
        (the live default) this block is skipped entirely and behaviour is
        byte-identical to the legacy implementation.
        """
        # --- Task E: probabilistic penny-floor fill gate (default OFF) ---
        if self.realistic_fills:
            fill_prob = self.penny_floor_fill_probability(
                entry_price, side, contract_side
            )
            if fill_prob < 1.0:
                self.penny_floor_requested += 1
                if self._fill_rng.random() >= fill_prob:
                    # Order did NOT fill at the penny floor.
                    self.penny_floor_skipped += 1
                    self.order_audit.append(
                        {
                            "event": "NO_FILL",
                            "symbol": symbol,
                            "side": side,
                            "quantity": quantity,
                            "price": entry_price,
                            "fill_prob": round(fill_prob, 4),
                            "strategy": strategy_name,
                            "timestamp": datetime.now().isoformat(),
                        }
                    )
                    logger.info(
                        f"[OMS] 🪙 NO FILL (penny floor): {side.upper()} {quantity}x "
                        f"{symbol} @ {entry_price:.2f} (p_fill={fill_prob:.2f})"
                    )
                    return None

        expiry_dt = None
        if expiration_time:
            if isinstance(expiration_time, str):
                try:
                    expiry_dt = datetime.fromisoformat(
                        expiration_time.replace("Z", "+00:00")
                    )
                except Exception:
                    expiry_dt = None
            else:
                expiry_dt = expiration_time

        # PRD FR-F0.1: a weather position must always carry its settlement-day
        # close, or the EXPIRATION CHECK in update_market never reaches
        # _settle_weather_position and the position never leaves the book.
        # The signal path stamps this in attach_spec_to_signals; this is the
        # backstop for any caller that did not. A supplied value is kept.
        if expiry_dt is None and is_weather_symbol(symbol):
            expiry_dt = settlement_close_for(symbol)
            if expiry_dt is not None:
                logger.info(
                    "[OMS] expiration_time backfilled for %s -> %s "
                    "(settlement-day close, FR-F0.1)",
                    symbol,
                    expiry_dt.isoformat(),
                )

        # Sprint 4: compute and deduct fee
        from src.core.fee_calculator import compute_fee, fee_type_for_symbol

        # Unknown fill type is booked as taker. ``fill_type`` keeps the
        # distinction visible on the position record so a downstream reader can
        # tell an assumed taker from an observed one instead of having to infer
        # it from a silently defaulted boolean.
        fill_type_known = is_maker is not None
        effective_is_maker = bool(is_maker) if fill_type_known else False
        if effective_is_maker:
            fill_type = "maker"
        elif fill_type_known:
            fill_type = "taker"
        else:
            fill_type = "taker_assumed"

        # The maker multiplier is a per-series property; derive it from the
        # ticker rather than assuming the standard schedule, so a series that
        # does bill resting liquidity (KXAAAGASM, PRD Phase 4) is charged.
        fee_result = compute_fee(
            entry_price,
            quantity,
            is_maker=effective_is_maker,
            series_fee_type=fee_type_for_symbol(symbol),
        )
        self.total_fees_paid += fee_result.fee
        self.realized_pnl -= fee_result.fee  # Fees reduce realized PnL

        # Task F: mirror the entry fee into the immutable cumulative ledger.
        self.cumulative_fees_paid += fee_result.fee
        self.cumulative_entry_fees += fee_result.fee

        if effective_is_maker:
            self.maker_fills += 1
        else:
            self.taker_fills += 1

        position = {
            "id": self._allocate_position_id(),
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "current_price": entry_price,
            "quantity": quantity,
            "original_quantity": quantity,
            "open_time": datetime.now(),
            "pnl": 0.0,
            "stop_loss": stop_loss,
            "trailing_rules": trailing_rules,
            "trailing_activated": False,
            "expiration_time": expiry_dt,
            "strategy_name": strategy_name or "Unknown",
            "last_market_price": 0,  # 0 = not yet updated by real market data
            "contract_side": contract_side,
            "strike": strike,
            # PRD FR-1.1/FR-1.2: the bracket's settlement semantics, cached
            # verbatim from the API fields at open time. Settlement rebuilds a
            # BracketSpec from these three keys (bracket_payoff.spec_from_position)
            # and never re-derives direction from the ticker. A position opened
            # without them settles SETTLEMENT_UNRESOLVED rather than guessing.
            "strike_type": strike_type,
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
            # PRD FR-1.5 (defence 1 of 3): a binary weather bracket is held to
            # settlement, so it never carries a profit-target ladder — whatever
            # the caller asked for. The production signal path leaves
            # ``disable_profit_targets`` at its default False
            # (src/bots/mixins.py reads getattr(sig, "disable_profit_targets",
            # False) and no weather strategy sets it), so honouring the caller
            # here meant every live weather position got the ladder and exited
            # on an invented mark instead of settling via FR-1.2.
            # PRD FR-4.4 widens this to AAA gas on the same reasoning; the
            # family test is the shared ``is_held_to_settlement`` predicate so a
            # third settlement family cannot be onboarded and miss this.
            "profit_targets": []
            if (disable_profit_targets or is_held_to_settlement(symbol))
            else [
                # Alt A: 1/2 + 1/2, no residual. Empirically the 1/3+1/3+1/3
                # ladder's residual tranche bled $240/24h via EARLY_SETTLEMENT
                # at 4.2% WR while the option value of riding past +0.30 was
                # exactly +$31.50 in 24h — deeply negative optionality.
                {"move": 0.15, "exit_pct": 0.50, "hit": False},
                {"move": 0.30, "exit_pct": 1.00, "hit": False},
            ],
            "entry_fee": fee_result.fee,
            "is_maker": effective_is_maker,
            # "maker" / "taker" (observed) or "taker_assumed" (fill type was
            # not supplied and the expensive side was booked).
            "fill_type": fill_type,
        }
        self.positions.append(position)

        # Sprint 4: audit trail entry
        self.order_audit.append(
            {
                "event": "OPEN",
                "position_id": position["id"],
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": entry_price,
                "fee": fee_result.fee,
                "is_maker": effective_is_maker,
                "fill_type": fill_type,
                "strategy": strategy_name,
                "timestamp": datetime.now().isoformat(),
            }
        )

    def update_market_price(self, symbol: str, real_price: float):
        """Cache a real Kalshi market price on matching positions."""
        for pos in self.positions:
            if symbol in pos["symbol"] or pos["symbol"] in symbol:
                pos["last_market_price"] = real_price

    @staticmethod
    def _map_symbol_fragment(fragment: str) -> str:
        """Station id -> the city fragment that appears in the Kalshi ticker.

        PRD FR-1.4: the mapping is derived from the one city registry
        (``KNYC``->``NY``, ``KMDW``->``CHI``, ``KLAX``->``LAX``, ``KMIA``->``MIA``),
        so it tracks the settlement stations automatically. The old hardcoded
        table mapped the *non-settlement* airports ``KJFK``->``NY`` and
        ``KORD``->``CHI`` and had no ``KMDW`` entry at all — after FR-1.4 the
        bot emits ``TEMP_KMDW``/``PRECIP_KMDW``, which that table silently
        failed to route to any Chicago position.
        """
        if fragment == "BTC":
            return "BTC"
        return city_key_for_station(fragment) or fragment

    def update_market(self, symbol_fragment: str, current_spot_price: float):
        """
        Updates the valuation of open positions based on the underlying spot price.
        """
        # Determine Update Type
        update_type = "GENERIC"
        target_fragment = symbol_fragment

        if symbol_fragment.startswith("PRECIP_"):
            update_type = "PRECIP"
            target_fragment = self._map_symbol_fragment(
                symbol_fragment.replace("PRECIP_", "")
            )
        elif symbol_fragment.startswith("TEMP_"):
            update_type = "TEMP"
            target_fragment = self._map_symbol_fragment(
                symbol_fragment.replace("TEMP_", "")
            )
        else:
            target_fragment = self._map_symbol_fragment(symbol_fragment)
            if target_fragment in city_keys():
                update_type = "TEMP"

        # Timestamp for expiration comparisons (unused directly but keeps code intent clear)

        for pos in self.positions[:]:
            # --- FR-0.6 STATE HYGIENE: drop qty<=0 shells ---
            # A position emptied by partial profit-target closes carries no
            # economic exposure; all its PnL was booked (and on_close fired)
            # at each partial close. Legacy code could persist such a shell
            # as "open" (the stuck id-1582 from 2026-07-06), so the sweep
            # drops any it encounters — before the expiration check, so an
            # expired shell never produces a bogus settlement row.
            if pos.get("quantity", 0) <= 0:
                self.positions.remove(pos)
                logger.info(
                    "[OMS] STATE HYGIENE: removed qty<=0 shell position "
                    "id=%s symbol=%s (original_qty=%s) from open list",
                    pos.get("id"),
                    pos.get("symbol"),
                    pos.get("original_quantity"),
                )
                self._save_state()
                continue

            is_weather = is_weather_symbol(pos["symbol"])
            is_gas = is_gas_symbol(pos["symbol"])
            # PRD FR-1.5 (weather) + FR-4.4 (gas): one flag for every family
            # that may only leave the book by settling. Every exemption below
            # tests THIS, so onboarding a family to ``is_held_to_settlement``
            # carries all of them at once.
            held_to_settlement = is_weather or is_gas

            # --- EXPIRATION CHECK ---
            if pos.get("expiration_time"):
                exp = pos["expiration_time"]
                # Ensure comparison is aware vs aware or naive vs naive
                if exp.tzinfo is None:
                    comp_now = datetime.now()
                else:
                    comp_now = datetime.now().astimezone()

                if comp_now >= exp:
                    if is_weather:
                        # PRD FR-1.2/FR-1.5: a weather contract settles on the
                        # NWS Climatological Report daily high, which is not
                        # published until the following morning. `current_spot_price`
                        # here is a Kalshi market price (or a raw temperature),
                        # never the settlement high — resolve the real thing.
                        if self._settle_weather_position(pos):
                            continue
                        # Truth not published yet: hold the position and retry
                        # on a later sweep (logged inside the helper). Skip the
                        # rest of this position's valuation — the market is gone.
                        continue
                    if is_gas:
                        # PRD FR-4.4: an AAA gas contract settles on the AAA
                        # national average published for its settlement date.
                        # Trading closes the evening BEFORE that number exists,
                        # so `current_spot_price` at expiry is a quote or a
                        # projection and never the settled value — resolve the
                        # published one, or hold and retry.
                        if self._settle_gas_position(pos):
                            continue
                        continue
                    self._close_position(pos, current_spot_price, reason="EXPIRATION")
                    continue

            if target_fragment not in pos["symbol"]:
                # Special Case: BTC matches kxbtcd (case mismatch / alias)
                if target_fragment == "BTC" and "kxbtcd" in pos["symbol"]:
                    pass
                else:
                    continue
            if update_type == "PRECIP" and "PRECIP" not in pos["symbol"]:
                continue
            if (
                update_type == "TEMP"
                and "TEMP" not in pos["symbol"]
                and "KXHIGH" not in pos["symbol"]
            ):
                continue

            # Grace period: skip profit targets and stops for the first 30s
            # after opening. Prevents tanh estimation from instantly gaming
            # the entry price on the same tick.
            age_seconds = (datetime.now() - pos["open_time"]).total_seconds()
            age = age_seconds / 60
            in_grace_period = age_seconds < 30

            # Binary event contracts: expiry IS the stop-loss.
            # Research (Whelan et al.): stops on binary contracts crystallize
            # losses from normal price oscillation, not thesis invalidation.
            # Max loss is already bounded at entry_price * quantity.
            # PRD FR-1.5 extends this to weather: a daily-high contract is a
            # binary event too, and a stop-loss on one crystallizes a loss from
            # ordinary intraday oscillation while the settlement outcome is
            # still undetermined. Expiry IS the stop. PRD FR-4.4 extends it
            # again to AAA gas, for which the argument is even stronger: the
            # settled value is not published until after trading has closed.
            is_binary_event = (
                "KXBTC15M" in pos["symbol"]
                or "KXBTCD" in pos["symbol"]
                or "kxbtcd" in pos["symbol"]
                or held_to_settlement
            )

            # Check Time Limit (Legacy fallback)
            # PRD FR-1.5/FR-4.4: held-to-settlement positions are exempt — a
            # 60-minute wall clock must never close one. A gas contract's
            # horizon is up to 14 days (GAS_FINAL_WINDOW_DAYS), so every gas
            # position would otherwise be closed by this within the hour.
            if age >= self.TIME_LIMIT_MIN and not held_to_settlement:
                # Use estimated option price, NOT raw spot price
                self._close_position(
                    pos,
                    pos.get("current_price", pos["entry_price"]),
                    reason="TIME_LIMIT",
                )
                continue

            # Calculate Synthetic PnL
            try:
                estimated_price = pos["entry_price"]

                # --- CASE 1: PRECIPITATION ---
                if "PRECIP" in pos["symbol"]:
                    estimated_price = current_spot_price
                # --- CASE 2a: HELD-TO-SETTLEMENT (weather bracket / AAA gas) ---
                # PRD FR-1.2/FR-1.5. A bracket's value is P(YES), which the tanh
                # estimator cannot express: it maps a signed distance to a
                # strike, and a `between` bracket has TWO bounds while a `less`
                # bracket pays in the opposite direction from what the estimator
                # assumed. Running it on KXHIGH (scale=10.0) manufactured
                # phantom marks that the profit-target ladder then traded
                # against. There is exactly one honest mark for a binary
                # weather contract: the observed Kalshi price. With no observed
                # price the mark holds at entry (unrealized PnL 0) until one
                # arrives or the position settles.
                # PRD FR-4.4 routes AAA gas here for the same reason — a
                # $4.60/gal strike against a ~$3-5/gal underlying would give the
                # tanh estimator a distance of ~0.0005 scale units and mark
                # every contract at ~0.50 regardless of the projection.
                elif held_to_settlement:
                    lmp = pos.get("last_market_price", 0) or 0
                    if lmp > 0:
                        estimated_price = lmp
                    else:
                        estimated_price = pos["entry_price"]
                        logger.debug(
                            "[OMS] %s: no observed Kalshi price yet; holding mark "
                            "at entry %.2f (no synthetic price for a binary "
                            "event contract)",
                            pos["symbol"],
                            pos["entry_price"],
                        )

                # --- CASE 2b: STRIKE BASED (legacy crypto: KXBTC, kxbtcd) ---
                elif "KXBTC" in pos["symbol"] or "kxbtcd" in pos["symbol"]:
                    try:
                        parts = pos["symbol"].split("-")
                        strike_str = parts[-1]
                        # Crypto 15m/hourly tickers are all "is spot above the
                        # strike?" markets, and no crypto ticker suffix begins
                        # with "B" (they end in a minute label 00/15/30/45 or a
                        # T-prefixed strike), so the deleted suffix-letter test
                        # evaluated to True on every crypto symbol it ever saw.
                        # Hardcoding True is therefore behaviour-preserving, and
                        # it removes the last direction-inference-from-ticker in
                        # this valuation path (PRD FR-1.1).
                        is_above = True
                        parsed_strike = float(re.sub(r"[A-Za-z]", "", strike_str))

                        # Use cached strike if available — ticker suffix can be
                        # a market index (e.g., "-45"), not the real BTC strike
                        cached_strike = pos.get("strike")
                        if cached_strike and cached_strike > 1000:
                            strike = cached_strike
                        elif (
                            "KXBTC" in pos["symbol"] or "kxbtcd" in pos["symbol"]
                        ) and parsed_strike < 1000:
                            # Parsed value is a market index, not a strike.
                            # Use entry_price as neutral valuation until real
                            # market price is cached.
                            strike = current_spot_price
                        else:
                            strike = parsed_strike

                        if is_above:
                            diff = current_spot_price - strike
                        else:
                            diff = strike - current_spot_price

                        # Sigmoid-like scaling using tanh for smooth probability
                        # mapping. Crypto only: dollars matter, so scale=1000.
                        # (The KXHIGH scale=10.0 branch is gone — weather is
                        # handled in CASE 2a above and never reaches tanh.)
                        scale = 1000.0
                        normalized_diff = diff / scale
                        # tanh maps (-inf, inf) -> (-1, 1), then scale to price range
                        probability_shift = math.tanh(normalized_diff) * 0.49
                        tanh_estimate = max(0.01, min(0.99, 0.50 + probability_shift))

                        # Prefer real Kalshi market price over tanh estimate
                        # for ALL contract types when available. Tanh creates
                        # unrealistic profits -- especially for BTC 15m where
                        # profit targets were firing $0.03-0.08 above reality.
                        # Tanh is only used as a fallback when no real orderbook
                        # price has been cached yet.
                        lmp = pos.get("last_market_price", 0)
                        has_real_price = lmp > 0
                        is_btc_15m = "KXBTC15M" in pos["symbol"]

                        if has_real_price:
                            estimated_price = lmp
                            if is_btc_15m:
                                logger.debug(
                                    f"[OMS] BTC 15m using real market price {lmp:.2f} "
                                    f"(tanh estimate was {tanh_estimate:.2f}, "
                                    f"diff={abs(lmp - tanh_estimate):.3f})"
                                )
                        else:
                            estimated_price = tanh_estimate
                            if is_btc_15m:
                                logger.debug(
                                    f"[OMS] BTC 15m falling back to tanh {tanh_estimate:.2f} "
                                    f"(no real market price cached yet)"
                                )
                    except Exception as e:
                        logger.warning(
                            f"[OMS] Price calc error for {pos['symbol']}: {e}"
                        )
                        estimated_price = pos["entry_price"]

                # --- COMMON PNL CALC ---
                # For NO contracts, invert the estimated price
                if pos.get("contract_side") == "NO":
                    display_price = 1.0 - estimated_price
                else:
                    display_price = estimated_price

                pos["current_price"] = display_price
                if pos["side"] == "buy":
                    pos["pnl"] = (display_price - pos["entry_price"]) * pos["quantity"]
                else:
                    pos["pnl"] = (pos["entry_price"] - display_price) * pos["quantity"]

                # --- EARLY SETTLEMENT (Liquidity/Heuristic) ---
                # If price is pegged at 0.99 or 0.01 for a sustained period (10m), assume market has decided.
                # PRD FR-1.5/FR-4.4 exempt every held-to-settlement family: this
                # heuristic invents a 1.00/0.00 outcome from a price peg, which
                # is precisely NOT "settle via FR-1.2/FR-4.4". A weather bracket
                # resolves against the CLI daily high at expiry and a gas
                # contract against the published AAA value, and nothing else.
                if age >= 10 and not held_to_settlement:
                    if (pos["side"] == "buy" and estimated_price >= 0.99) or (
                        pos["side"] == "sell" and estimated_price <= 0.01
                    ):
                        self._close_position(
                            pos, current_spot_price, reason="EARLY_SETTLEMENT"
                        )
                        continue
                    if (pos["side"] == "buy" and estimated_price <= 0.01) or (
                        pos["side"] == "sell" and estimated_price >= 0.99
                    ):
                        self._close_position(
                            pos, current_spot_price, reason="EARLY_SETTLEMENT"
                        )
                        continue

                # --- PROFIT TARGET LADDER (Partial Exits) ---
                # Skip during grace period to prevent instant tanh gaming.
                # PRD FR-1.5 (defence 2 of 3) exempts weather, and FR-4.4 gas: a
                # profit-target exit closes the position at an invented mark with
                # no settled truth, which is precisely NOT "settle via
                # FR-1.2/FR-4.4". Positions restored from a state file written
                # before the ladder was withheld at open still carry targets, so
                # the skip must live at the call site too, not only at open time.
                if (
                    not in_grace_period
                    and not held_to_settlement
                    and self._check_profit_targets(pos, display_price)
                ):
                    continue

                # --- STOP LOSS / TRAILING LOGIC (Price Based) ---
                # Skip stops for short-duration binary contracts — expiry IS the stop.
                if pos["stop_loss"] > 0 and not in_grace_period and not is_binary_event:
                    # 1. Check Trailing Trigger
                    if pos.get("trailing_rules") and not pos["trailing_activated"]:
                        trig = pos["trailing_rules"].get("trigger", 999)

                        # Trigger condition depends on side
                        activated = False
                        if pos["side"] == "buy" and estimated_price >= trig:
                            activated = True
                        elif pos["side"] == "sell" and estimated_price <= trig:
                            activated = True

                        if activated:
                            new_sl = pos["trailing_rules"].get(
                                "new_sl", pos["stop_loss"]
                            )
                            pos["stop_loss"] = new_sl
                            pos["trailing_activated"] = True
                            logger.info(
                                f"[OMS] ⛓️ Trailing Stop Activated for {pos['symbol']}: SL moved to {new_sl}"
                            )

                    # 2. Check Stop Loss Hit
                    hit = False
                    if pos["side"] == "buy" and estimated_price <= pos["stop_loss"]:
                        hit = True
                    elif pos["side"] == "sell" and estimated_price >= pos["stop_loss"]:
                        hit = True

                    if hit:
                        # Use last_market_price for exit, not raw sigmoid estimate
                        lmp_exit = pos.get("last_market_price", 0)
                        safe_exit = lmp_exit if lmp_exit > 0 else pos["entry_price"]
                        self._close_position(
                            pos,
                            safe_exit,
                            reason=f"STOP_LOSS_PRICE ({pos['stop_loss']})",
                        )
                        continue

                # Fallback: PCT stops and the PCT take-profit. Skipped during
                # the grace period and for every binary event contract —
                # which includes weather and gas (``is_binary_event`` above ORs
                # in ``held_to_settlement``). PRD FR-1.5/FR-4.4: TAKE_PROFIT
                # closes at an invented mark with no settled truth, exactly like
                # the ladder, so neither family must ever reach it.
                if in_grace_period or is_binary_event:
                    continue
                pnl_pct = (
                    pos["pnl"] / (pos["entry_price"] * pos["quantity"])
                    if pos["entry_price"] > 0
                    else 0
                )
                if pnl_pct >= self.TAKE_PROFIT_PCT:
                    self._close_position(pos, display_price, reason="TAKE_PROFIT")
                elif pnl_pct <= -self.STOP_LOSS_PCT and pos["stop_loss"] == 0:
                    logger.warning(
                        f"[OMS] PCT STOP triggered for {pos['symbol']} (pnl_pct={pnl_pct:.2%}). Consider adding explicit stop_loss."
                    )
                    # Use last_market_price or entry_price, never raw sigmoid
                    lmp_pct = pos.get("last_market_price", 0)
                    safe_exit = lmp_pct if lmp_pct > 0 else pos["entry_price"]
                    self._close_position(pos, safe_exit, reason="STOP_LOSS_PCT")

            except Exception as e:
                logger.error(f"[OMS] PnL calculation error for {pos['symbol']}: {e}")

        self.unrealized_pnl = sum(p["pnl"] for p in self.positions)

    def _check_profit_targets(self, pos, current_price) -> bool:
        """
        Check profit target ladder for partial exits.
        Returns True if position was fully closed.
        """
        targets = pos.get("profit_targets", [])
        if not targets:
            return False

        # PRD FR-1.5 (defence 3 of 3, the hard guard). A partial profit-target
        # close never routes through _close_position, so it bypassed the
        # invariant guard entirely: a production-shaped weather position with
        # the default ladder was fully closed on two PROFIT_TARGET tranches at
        # an invented exit price and with no settlement_high. Refuse here as
        # well as at the two call sites, so reverting either one is caught
        # rather than absorbed.
        if self._held_to_settlement_close_refused(
            pos, "PROFIT_TARGET", context="_check_profit_targets"
        ):
            return False

        entry = pos["entry_price"]
        if pos["side"] == "buy":
            price_move = current_price - entry
        else:
            price_move = entry - current_price

        fully_closed = False
        for target in targets:
            if target["hit"]:
                continue
            if price_move >= target["move"] - 1e-9:
                target["hit"] = True
                exit_qty = max(1, int(pos["quantity"] * target["exit_pct"]))
                if exit_qty >= pos["quantity"]:
                    exit_qty = pos["quantity"]
                    fully_closed = True

                # Calculate partial PnL
                if pos["side"] == "buy":
                    partial_pnl = (current_price - entry) * exit_qty
                else:
                    partial_pnl = (entry - current_price) * exit_qty

                # Compute and deduct exit fee for partial close. The entry's
                # fill type carries to the exit; a position record that predates
                # the field (restored state, hand-built fixture) is treated as a
                # taker, matching open_position's unknown-fill rule.
                from src.core.fee_calculator import compute_fee, fee_type_for_symbol

                exit_fee_result = compute_fee(
                    current_price,
                    exit_qty,
                    is_maker=pos.get("is_maker", False),
                    series_fee_type=fee_type_for_symbol(pos["symbol"]),
                )
                exit_fee = exit_fee_result.fee
                self.total_fees_paid += exit_fee
                partial_pnl -= exit_fee

                pos["quantity"] -= exit_qty
                self.realized_pnl += partial_pnl

                # Task F: mirror into the immutable cumulative ledger.
                # partial_pnl is already net of this exit fee, matching the
                # ``pnl`` field stored on the partial_trade below. The entry fee
                # was counted once at open time, so we do NOT touch
                # cumulative_entry_fees here.
                self.cumulative_realized_pnl += partial_pnl
                self.cumulative_fees_paid += exit_fee

                partial_trade = dict(pos)
                partial_trade["exit_price"] = current_price
                partial_trade["pnl"] = partial_pnl
                partial_trade["close_time"] = datetime.now()
                partial_trade["reason"] = f"PROFIT_TARGET (+{target['move']:.2f})"
                partial_trade["quantity"] = exit_qty
                partial_trade["exit_fee"] = exit_fee
                self.closed_trades.append(partial_trade)

                logger.info(
                    f"[OMS] 🎯 PROFIT TARGET +{target['move']:.2f}: Closed {exit_qty}x {pos['symbol']} | PnL: ${partial_pnl:+.2f} (exit fee: ${exit_fee:.2f})"
                )

                if self.on_close:
                    self.on_close(partial_trade)

                # FR-0.6: the final partial close IS the close. Remove the
                # emptied position from the open list BEFORE persisting —
                # the legacy order (save first, remove after, in memory
                # only) wrote a qty-0 "open" shell to exchange_state.json
                # that a restart then resurrected (the stuck id-1582 bug).
                # on_close has already fired above, exactly as for any
                # partial close, so RiskManager win-rate/journal semantics
                # are unchanged.
                if fully_closed or pos["quantity"] <= 0:
                    if pos in self.positions:
                        self.positions.remove(pos)
                    self._save_state()
                    return True

                # Sprint 8: persist after each partial-close event
                self._save_state()

        # Clean up ghost positions with qty reduced to 0 by rounding
        if pos["quantity"] <= 0:
            if pos in self.positions:
                self.positions.remove(pos)
                logger.info(
                    "[OMS] STATE HYGIENE: removed rounding-ghost qty<=0 "
                    "position id=%s symbol=%s from open list",
                    pos.get("id"),
                    pos.get("symbol"),
                )
                self._save_state()
            return True

        return False

    # ------------------------------------------------------------------
    # FR-1.5 / FR-4.4 close-reason policy for held-to-settlement positions
    # ------------------------------------------------------------------
    # The guard is a WHITELIST, not a blacklist: a weather bracket or an AAA gas
    # contract may only leave the book through settlement. Enumerating forbidden
    # reasons was provably incomplete — the original tuple below covered
    # TIME_LIMIT / CYCLE_RESET / STOP_LOSS* but not TAKE_PROFIT, PROFIT_TARGET,
    # EARLY_SETTLEMENT or the default reason="MARKET", and a weather position
    # was observed closing TAKE_PROFIT with no guard, no alert and no test
    # failure. Any reason not listed here is refused, so a close reason added
    # in the future cannot silently bypass the invariant.
    #
    #   EXPIRATION            -> FR-1.2 payoff against the published CLI high,
    #                            or the FR-4.4 payoff against the published AAA
    #                            national average
    #   SETTLEMENT_UNRESOLVED -> set by _weather_exit_price / _gas_exit_price
    #                            AFTER the guard, when the payoff semantics are
    #                            unavailable; the position closes flat at entry,
    #                            loudly.
    #
    # Deliberately ONE whitelist for both families rather than one per family:
    # gas introduces no new close reason, and a per-family list would be a
    # second place to forget.
    _SETTLEMENT_CLOSE_REASONS = ("EXPIRATION", "SETTLEMENT_UNRESOLVED")

    # Every non-settlement reason string that exists in this file, in
    # run_dashboard.py and in src/backtest/engine.py, kept as explicit
    # documentation of what the whitelist excludes. Matched by prefix so every
    # STOP_LOSS_*/PROFIT_TARGET (+x) variant is covered. It is NOT the
    # enforcement mechanism (the whitelist above is);
    # tests/test_weather_lifecycle.py walks the sources and asserts every
    # literal reason is classified by one list or the other.
    _FORBIDDEN_SETTLEMENT_CLOSE_PREFIXES = (
        "TIME_LIMIT",
        "CYCLE_RESET",
        "STOP_LOSS",
        "TAKE_PROFIT",
        "PROFIT_TARGET",
        "EARLY_SETTLEMENT",
        "MARKET",
    )

    # Phase 1 names, retained as aliases to the SAME tuples. They are read by
    # ``tests/test_weather_lifecycle.py`` (a Phase 1 exit-criterion file owned
    # outside this workstream), so renaming without aliasing would have meant
    # editing a golden weather test to widen a guard for gas.
    _WEATHER_SETTLEMENT_CLOSE_REASONS = _SETTLEMENT_CLOSE_REASONS
    _WEATHER_FORBIDDEN_CLOSE_PREFIXES = _FORBIDDEN_SETTLEMENT_CLOSE_PREFIXES

    def _held_to_settlement_close_refused(
        self, pos, reason, context: str = "close"
    ) -> bool:
        """True when ``reason`` must not be applied to held-to-settlement ``pos``.

        The single enforcement point for FR-1.5's (weather) and FR-4.4's (AAA
        gas) "held to settlement" rule, shared by :meth:`_close_position` (full
        closes) and :meth:`_check_profit_targets` (partial closes, which never
        route through ``_close_position`` and so had no guard at all).

        Widened from weather-only by testing the shared
        :func:`is_held_to_settlement` predicate instead of ``is_weather_symbol``.
        Weather behaviour is unchanged: the predicate returns exactly
        ``is_weather_symbol`` for every ``KXHIGH*`` ticker, the whitelist is the
        same tuple object it always was, and the log/alert strings are byte
        identical apart from naming the family.
        """
        symbol = pos.get("symbol", "")
        if not is_held_to_settlement(symbol):
            return False
        if str(reason) in self._SETTLEMENT_CLOSE_REASONS:
            return False

        family = "weather" if is_weather_symbol(symbol) else "gas"
        logger.error(
            "[OMS] FR-1.5 VIOLATION REFUSED (%s): attempt to close %s "
            "position id=%s %s with reason=%s. Held-to-settlement positions are "
            "exempt from TIME_LIMIT, cycle-reset liquidation, stop-losses, "
            "profit targets and early settlement; they are held to settlement "
            "(FR-1.2 weather / FR-4.4 gas). No-op.",
            context,
            family,
            pos.get("id"),
            symbol,
            reason,
        )
        # PRD §6 budgets <1 false alarm/day. A caller that keeps trying is
        # re-evaluated on every tick, so alert once per (position, reason) and
        # leave the per-tick record to the ERROR log above — a reverted
        # exemption must not turn into an alert storm.
        alerted = pos.setdefault("_fr15_alerted_reasons", [])
        if str(reason) not in alerted:
            alerted.append(str(reason))
            self._alert(f"FR-1.5 REFUSED | {symbol} | close reason {reason} blocked")
        return True

    #: Phase 1 name for the guard above, retained because
    #: ``tests/test_weather_lifecycle.py`` calls it directly. It is an alias to
    #: the one implementation, not a second guard.
    _weather_close_refused = _held_to_settlement_close_refused

    def _settle_weather_position(self, pos) -> bool:
        """Settle one expired weather position against published CLI truth.

        Returns ``True`` when the position was closed (settled, or explicitly
        closed flat as ``SETTLEMENT_UNRESOLVED``), ``False`` when settlement
        truth is not published yet and the position must stay open for a later
        sweep. PRD FR-1.2 / FR-1.5.
        """
        symbol = pos["symbol"]
        high = resolve_settlement_high(symbol)
        if high is not None:
            self._close_position(pos, float(high), reason="EXPIRATION")
            return True

        # No published high. The position is NOT closed and NOT marked: the
        # running observed max is not the CLI product and guessing settles the
        # book against fiction (abort-on-missing-critical-input).
        age_h = hours_since_settlement_day_close(symbol)
        pending_key = "_settlement_pending_logged"
        if age_h is not None and age_h > SETTLEMENT_TRUTH_GRACE_HOURS:
            logger.error(
                "[OMS] SETTLEMENT_TRUTH_OVERDUE %s: no CLI daily high %.1fh after "
                "the settlement day closed (grace %.0fh). Position id=%s held open; "
                "check the FR-1.3 settlement recorder.",
                symbol,
                age_h,
                SETTLEMENT_TRUTH_GRACE_HOURS,
                pos.get("id"),
            )
            if pos.get(pending_key) != "OVERDUE":  # alert once, then log only
                self._alert(
                    f"SETTLEMENT TRUTH OVERDUE | {symbol} | "
                    f"{age_h:.0f}h past close, position held open"
                )
                pos[pending_key] = "OVERDUE"
        else:
            logger.info(
                "[OMS] SETTLEMENT_TRUTH_PENDING %s: CLI daily high not published "
                "yet (%s); holding position id=%s open for a later sweep",
                symbol,
                f"{age_h:+.1f}h vs day close" if age_h is not None else "age unknown",
                pos.get("id"),
            )
            pos[pending_key] = "PENDING"
        return False

    def _settle_gas_position(self, pos) -> bool:
        """Settle one expired AAA gas position against the published AAA value.

        Structural mirror of :meth:`_settle_weather_position`: returns ``True``
        when the position was closed (settled, or explicitly closed flat as
        ``SETTLEMENT_UNRESOLVED``), ``False`` when the AAA national average for
        the settlement date is not published yet and the position must stay open
        for a later sweep. PRD FR-4.4.

        Truth is resolved through workstream B's
        :func:`src.data.gas_settlement.resolve_settlement_value`, which reads
        the FR-4.4 settlement cache first and then the AAA daily series, and
        returns ``None`` for *not published yet* and nothing else — never the
        last observed value, never a projection, never an interpolation. This
        method therefore **aborts rather than guesses**: with no published value
        the position is held open and, past the grace window, paged about. A
        gas contract closes trading the evening BEFORE its value exists, so a
        wait of hours at expiry is the normal case, not a fault.
        """
        symbol = pos["symbol"]
        value = resolve_gas_settlement_value(symbol)
        if value is not None:
            self._close_position(pos, float(value), reason="EXPIRATION")
            return True

        # No published AAA value. The position is NOT closed and NOT marked:
        # yesterday's national average is not this settlement date's, and a
        # projection is what we were betting against in the first place
        # (abort-on-missing-critical-input).
        age_h = _hours_since_gas_settlement_date_open(symbol)
        pending_key = "_settlement_pending_logged"
        if age_h is not None and age_h > GAS_SETTLEMENT_TRUTH_GRACE_HOURS:
            logger.error(
                "[OMS] SETTLEMENT_TRUTH_OVERDUE %s: no published AAA national "
                "average %.1fh after the settlement date began (grace %.0fh, AAA "
                "publishes ~10:00 ET on the date). Position id=%s held open; "
                "check the FR-4.4 gas settlement recorder.",
                symbol,
                age_h,
                GAS_SETTLEMENT_TRUTH_GRACE_HOURS,
                pos.get("id"),
            )
            if pos.get(pending_key) != "OVERDUE":  # alert once, then log only
                self._alert(
                    f"SETTLEMENT TRUTH OVERDUE | {symbol} | "
                    f"{age_h:.0f}h past the settlement date, position held open"
                )
                pos[pending_key] = "OVERDUE"
        else:
            logger.info(
                "[OMS] SETTLEMENT_TRUTH_PENDING %s: AAA national average not "
                "published yet (%s); holding position id=%s open for a later sweep",
                symbol,
                f"{age_h:+.1f}h into the settlement date"
                if age_h is not None
                else "age unknown",
                pos.get("id"),
            )
            pos[pending_key] = "PENDING"
        return False

    def _gas_exit_price(self, pos, final_value):
        """``(exit_price, reason_override)`` for a settling AAA gas position.

        The FR-4.4 counterpart of :meth:`_weather_exit_price`, with the same
        contract: ``None`` means settlement truth is unavailable and the caller
        must leave the position open and retry later; ``reason_override`` is
        ``None`` to keep the caller's reason.

        ``final_value`` is the **published AAA national average in USD/gal**.
        ``None`` makes this resolve it itself.

        The payoff is workstream B's, not a local reimplementation: YES pays iff
        ``value > floor_strike``, strictly, and a value exactly equal to the
        strike pays NO (15 live markets settled exactly on their strike and all
        15 paid NO). Direction and strike come from the API fields cached on the
        position at open time, never from the ticker (PRD FR-1.1).
        """
        symbol = pos.get("symbol", "")

        # Defence in depth against a misrouted symbol. Routing is by the same
        # predicate that selected this method, so this cannot fire in the
        # current code — it exists so that if it ever could, a gas contract is
        # closed flat and loudly rather than priced by a rule that does not
        # govern it.
        if is_weather_symbol(symbol):
            logger.error(
                "[OMS] SETTLEMENT_UNRESOLVED %s (id=%s): a weather bracket "
                "reached the FR-4.4 gas payoff. The two rules differ (gas: "
                "value > floor_strike; temperature 'greater': high >= floor+1), "
                "so pricing it here would be wrong. Closing flat at entry.",
                symbol,
                pos.get("id"),
            )
            self._alert(
                f"SETTLEMENT UNRESOLVED | {symbol} | weather symbol misrouted to "
                f"the gas payoff — closed flat at entry"
            )
            pos["settlement_error"] = "weather symbol routed to the gas payoff"
            return (pos["entry_price"], "SETTLEMENT_UNRESOLVED")

        try:
            spec = gas_spec_from_position(pos)
        except GasSpecError as exc:
            # A position opened without the API fields cached. Settling it NO
            # would be a systematically-wrong "safe" default — most ladder rungs
            # settle NO, so it would look plausible and be wrong — and the
            # project's abort-on-missing-critical-input rule forbids it. Close
            # flat instead: zero PnL, loudly.
            logger.error(
                "[OMS] SETTLEMENT_UNRESOLVED %s (id=%s): gas settlement "
                "semantics unavailable (%s). Refusing to fabricate a settlement "
                "outcome; closing flat at entry price $%.2f with zero PnL.",
                symbol,
                pos.get("id"),
                exc,
                pos.get("entry_price", 0.0),
            )
            self._alert(
                f"SETTLEMENT UNRESOLVED | {symbol} | no gas spec on position — "
                f"closed flat at entry"
            )
            pos["settlement_error"] = str(exc)
            return (pos["entry_price"], "SETTLEMENT_UNRESOLVED")

        value = final_value
        if value is None:
            value = resolve_gas_settlement_value(symbol)
        if value is None:
            return None

        try:
            outcome_is_yes = gas_settles_yes(spec, value)
        except GasSpecError as exc:
            logger.error(
                "[OMS] SETTLEMENT_UNRESOLVED %s (id=%s): %s. Closing flat at "
                "entry price with zero PnL.",
                symbol,
                pos.get("id"),
                exc,
            )
            self._alert(
                f"SETTLEMENT UNRESOLVED | {symbol} | unusable AAA value — "
                f"closed flat at entry"
            )
            pos["settlement_error"] = str(exc)
            return (pos["entry_price"], "SETTLEMENT_UNRESOLVED")

        # Provenance: what settled this, and under which rule. Deliberately
        # ``settlement_value`` and not ``settlement_high`` — the units are
        # USD/gal, not F, and reusing the weather key would let a reader join a
        # gas row to a temperature truth table without noticing.
        pos["settlement_value"] = float(value)
        pos["settlement_spec"] = spec.as_dict()
        pos["settlement_rule"] = spec.describe()
        pos["settlement_outcome"] = "yes" if outcome_is_yes else "no"
        logger.info(
            "[OMS] FR-4.4 SETTLED %s: AAA national average $%.3f/gal vs %s (%s) -> %s",
            symbol,
            float(value),
            spec.describe(),
            spec.strike_type,
            "YES" if outcome_is_yes else "NO",
        )
        return (1.00 if outcome_is_yes else 0.00, None)

    def _weather_exit_price(self, pos, final_spot_price):
        """``(exit_price, reason_override)`` for a settling weather position.

        Returns ``None`` when settlement truth is unavailable, meaning the
        caller must leave the position open and retry later.
        ``reason_override`` is ``None`` to keep the caller's reason.

        ``final_spot_price`` is the **settlement daily high in F** (the only
        runtime caller, :meth:`_settle_weather_position`, resolves it from the
        FR-1.3 truth cache first; ``src/backtest/engine.py`` passes Kalshi's own
        ``expiration_value``). ``None`` makes this resolve the high itself.

        PRD FR-1.2: direction comes from the API's ``strike_type`` via
        :mod:`src.core.bracket_payoff`, never from the ticker. The settlement
        high and the exact bracket spec used are stamped onto the position so
        the journal and ``exchange_state.json`` record what the outcome was
        computed from.
        """
        symbol = pos.get("symbol", "")

        # PRD FR-4.4, structural: an AAA gas contract must NEVER be priced by
        # the temperature payoff. The two `greater` rules genuinely differ —
        # bracket_payoff.yes_bounds turns a `greater` strike into
        # ``high >= floor + 1`` because a daily high is an integer count of
        # degrees, so a $4.60/gal gas strike run through it would demand $5.60
        # to pay YES. Routing already selects _gas_exit_price for these symbols,
        # so this cannot fire in the current code; it is here so that a future
        # caller that reaches the wrong payoff closes flat and loudly instead of
        # settling against a rule that does not govern it.
        if is_gas_symbol(symbol):
            logger.error(
                "[OMS] SETTLEMENT_UNRESOLVED %s (id=%s): a gas contract reached "
                "the FR-1.2 temperature payoff, whose 'greater' rule is "
                "high >= floor+1, not value > floor. Refusing to price it here; "
                "closing flat at entry.",
                symbol,
                pos.get("id"),
            )
            self._alert(
                f"SETTLEMENT UNRESOLVED | {symbol} | gas symbol misrouted to the "
                f"temperature payoff — closed flat at entry"
            )
            pos["settlement_error"] = "gas symbol routed to the temperature payoff"
            return (pos["entry_price"], "SETTLEMENT_UNRESOLVED")

        try:
            spec = spec_from_position(pos)
        except BracketSpecError as exc:
            # A legacy position opened before FR-1.1 cached the API fields.
            # Settling it NO would be a systematically-wrong "safe" default
            # (~90% of brackets settle NO, so it would look plausible and be
            # wrong): the project's abort-on-missing-critical-input rule
            # forbids it. Close flat instead — zero PnL, loudly.
            logger.error(
                "[OMS] SETTLEMENT_UNRESOLVED %s (id=%s): bracket semantics "
                "unavailable (%s). Refusing to fabricate a settlement outcome; "
                "closing flat at entry price $%.2f with zero PnL.",
                symbol,
                pos.get("id"),
                exc,
                pos.get("entry_price", 0.0),
            )
            self._alert(
                f"SETTLEMENT UNRESOLVED | {symbol} | no bracket spec on "
                f"position — closed flat at entry"
            )
            pos["settlement_error"] = str(exc)
            return (pos["entry_price"], "SETTLEMENT_UNRESOLVED")

        high = final_spot_price
        if high is None:
            high = resolve_settlement_high(symbol)
        if high is None:
            return None

        try:
            outcome_is_yes = settles_yes(spec, high)
        except BracketSpecError as exc:
            logger.error(
                "[OMS] SETTLEMENT_UNRESOLVED %s (id=%s): %s. Closing flat at "
                "entry price with zero PnL.",
                symbol,
                pos.get("id"),
                exc,
            )
            self._alert(
                f"SETTLEMENT UNRESOLVED | {symbol} | unusable daily high — "
                f"closed flat at entry"
            )
            pos["settlement_error"] = str(exc)
            return (pos["entry_price"], "SETTLEMENT_UNRESOLVED")

        # Provenance: what settled this, and under which rule.
        pos["settlement_high"] = float(high)
        pos["settlement_spec"] = spec.as_dict()
        pos["settlement_rule"] = spec.describe()
        pos["settlement_outcome"] = "yes" if outcome_is_yes else "no"
        logger.info(
            "[OMS] FR-1.2 SETTLED %s: daily high %.0fF vs %s (%s) -> %s",
            symbol,
            float(high),
            spec.describe(),
            spec.strike_type,
            "YES" if outcome_is_yes else "NO",
        )
        return (1.00 if outcome_is_yes else 0.00, None)

    def _close_position(self, pos, final_spot_price, reason="MARKET"):
        """
        Simulates a closing trade.

        For BINARY SETTLEMENT (EXPIRATION, EARLY_SETTLEMENT):
            final_spot_price = the underlying spot value.
            * weather (KXHIGH*): the **settlement daily high in F**, as published
              in the NWS Climatological Report — not a market price, not the
              running observed max. Callers that do not have it must use
              ``_settle_weather_position`` (or pass ``None``) so the position is
              held open instead of settled against a substitute.
            * AAA gas (KXAAAGAS*): the **published AAA national average in
              USD/gal** for the settlement date. Callers that do not have it must
              use ``_settle_gas_position`` (or pass ``None``) so the position is
              held open instead of settled against a stale or assumed value.
            * crypto: the underlying spot ($).
            exit_price is determined as 1.00 or 0.00 based on outcome.

        For NON-SETTLEMENT closes (TAKE_PROFIT, STOP_LOSS, TIME_LIMIT):
            final_spot_price should be the ESTIMATED OPTION PRICE (0.00-1.00),
            NOT the raw underlying spot price.

        FR-1.5 / FR-4.4: a held-to-settlement position (weather or gas) accepts
        ONLY the settlement reasons in ``_SETTLEMENT_CLOSE_REASONS``. Every other
        reason is refused (logged + alerted) and the position stays open.
        """
        try:
            symbol = pos.get("symbol", "")
            is_weather = is_weather_symbol(symbol)
            is_gas = is_gas_symbol(symbol)

            # --- FR-1.5 / FR-4.4 HARD INVARIANT ------------------------
            # Weather and gas positions persist across cycles and settle via
            # FR-1.2 / FR-4.4. Refuse every non-settlement close outright rather
            # than relying on each caller to remember, so "no TIME_LIMIT or
            # CYCLE_RESET close reason appears on any held-to-settlement
            # position" is an enforced property.
            if self._held_to_settlement_close_refused(
                pos, reason, context="_close_position"
            ):
                return

            # Determine Exit Price Strategy
            is_binary_settlement = reason in ["EXPIRATION", "EARLY_SETTLEMENT"]

            if is_binary_settlement and is_weather:
                # --- FR-1.2 WEATHER SETTLEMENT (shared payoff module) ---
                resolved = self._weather_exit_price(pos, final_spot_price)
                if resolved is None:
                    return  # truth pending — position stays open (logged inside)
                exit_price, reason_override = resolved
                if reason_override:
                    reason = reason_override
            elif is_binary_settlement and is_gas:
                # --- FR-4.4 AAA GAS SETTLEMENT (workstream B's payoff) ---
                # Placed as its own branch BEFORE the legacy path so a gas
                # symbol can never reach either the temperature payoff above or
                # the crypto strike parsing below (which would read "4.60" out
                # of the ticker suffix and compare a price to a price).
                resolved = self._gas_exit_price(pos, final_spot_price)
                if resolved is None:
                    return  # truth pending — position stays open (logged inside)
                exit_price, reason_override = resolved
                if reason_override:
                    reason = reason_override
            elif is_binary_settlement:
                # --- BINARY SETTLEMENT LOGIC (0.00 or 1.00) ---
                outcome_is_yes = False

                if "PRECIP" in pos["symbol"]:
                    if final_spot_price > 0.50:
                        outcome_is_yes = True
                else:
                    # ----------------------------------------------------------
                    # LEGACY CRYPTO SETTLEMENT (weather and gas never reach here
                    # — KXHIGH* is routed to the FR-1.2 payoff module above and
                    # KXAAAGAS* to the FR-4.4 one).
                    #
                    # 2026-06-10 SETTLEMENT FIX (strike resolution)
                    #
                    # The legacy code parsed the LAST dash-segment of the ticker
                    # as the settlement strike. That is correct for hourly
                    # (KXBTCD-...-T78499.99) tickers whose suffix IS the strike,
                    # but it is WRONG for 15m crypto tickers
                    # (KXBTC15M-26JUN032130-30, KXETH15M-...-00, KXSOL15M-...-15,
                    #  KXDOGE15M-...-45, KXXRP15M-...-00) where the last segment
                    # is the EXPIRY MINUTE LABEL (00/15/30/45), NOT a strike.
                    # Parsing it as a strike forced near-100% auto-YES (e.g. -00
                    # parses 0 so any positive spot >= 0 settled YES). Actual
                    # Kalshi base rate is ~8% YES (settlement_cache.json: 9/114).
                    #
                    # The real per-asset strike (API floor_strike) is cached on
                    # the position as pos["strike"] (open_position stores it at
                    # line ~757). Mirror the sibling pattern in update_market
                    # "CASE 2: STRIKE BASED" (lines ~893-923) which prefers the
                    # cached strike over the suffix — but WITHOUT that path's
                    # `cached_strike > 1000` magnitude heuristic, which is wrong
                    # for sub-1000 alt strikes (SOL ~$150, DOGE ~$0.4, XRP ~$2,
                    # ETH ~$3k). Rule: if pos["strike"] is present, USE IT; only
                    # fall back to the suffix when strike is absent AND the
                    # suffix is a plausible real strike (hourly). For a
                    # 15m-crypto family with NO cached strike (current
                    # latency_arb reality — ETH/SOL/DOGE/XRP never set
                    # sig.strike, so pos["strike"] is None) we FAIL-SAFE to NO
                    # rather than auto-YES off the minute label.
                    # ----------------------------------------------------------
                    _CRYPTO_15M_FAMILIES = (
                        "KXBTC15M",
                        "KXETH15M",
                        "KXSOL15M",
                        "KXDOGE15M",
                        "KXXRP15M",
                    )
                    is_15m_crypto = any(
                        fam in pos["symbol"] for fam in _CRYPTO_15M_FAMILIES
                    )

                    try:
                        # Defensive suffix parse (FALLBACK only).
                        parts = pos["symbol"].split("-")
                        strike_str = parts[-1]
                        # Every ticker that still reaches this branch is a
                        # crypto "is spot above the strike?" market — 15m
                        # (suffix = minute label 00/15/30/45) or hourly (suffix
                        # = T-prefixed strike). No crypto suffix has ever begun
                        # with "B", so the deleted suffix-letter test was True
                        # on every symbol it saw here and hardcoding True is
                        # behaviour-preserving. Weather, the only family that
                        # ever had a below-bucket, is handled by the FR-1.2
                        # payoff module above and derives direction from the
                        # API's strike_type (PRD FR-1.1).
                        is_above = True
                        try:
                            parsed_strike = float(re.sub(r"[A-Za-z]", "", strike_str))
                        except Exception:
                            parsed_strike = None

                        cached_strike = pos.get("strike")

                        # 2026-06-10 defensive: a real strike is always > 0
                        # (crypto spot / weather temp). Reject 0 or negative —
                        # e.g. a minute-label 0.0 that slips through from a
                        # strategy that mis-parsed the ticker — so it fail-safes
                        # to NO below instead of auto-YES (spot >= 0 always true).
                        if cached_strike is not None and cached_strike > 0:
                            # Real API floor_strike — always preferred. YES iff
                            # spot >= strike, which is what a KX*15M / KXBTCD
                            # "above strike?" market pays.
                            strike = float(cached_strike)
                            if is_above:
                                outcome_is_yes = final_spot_price >= strike
                            else:
                                outcome_is_yes = final_spot_price <= strike
                        elif is_15m_crypto:
                            # latency_arb missing-strike case: the suffix is a
                            # minute label, NOT a strike. We cannot determine the
                            # outcome, so FAIL-SAFE to NO (Kalshi base rate is
                            # ~8% YES). This removes the fictional auto-YES profit
                            # and surfaces the latency_arb sig.strike gap loudly.
                            logger.error(
                                "[OMS] 2026-06-10 settlement fix: missing cached "
                                "strike for 15m crypto %s; cannot determine "
                                "settlement, defaulting outcome to NO (no "
                                "auto-YES). reason=%s",
                                pos["symbol"],
                                reason,
                            )
                            outcome_is_yes = False
                        elif parsed_strike is not None:
                            # Hourly (KXBTCD-...-T78499.99): the suffix IS the
                            # real strike — preserve exact legacy behavior.
                            strike = parsed_strike
                            if is_above:
                                outcome_is_yes = final_spot_price >= strike
                            else:
                                outcome_is_yes = final_spot_price <= strike
                        else:
                            # Unparseable suffix, no cached strike: fail-safe.
                            outcome_is_yes = False
                    except Exception:
                        outcome_is_yes = False  # Fail safe

                exit_price = 1.00 if outcome_is_yes else 0.00
            else:
                # --- NON-SETTLEMENT: Use estimated option price ---
                # final_spot_price here should already BE the estimated price
                # (passed from update_market using pos['current_price'])
                exit_price = final_spot_price

            # --- NO-SIDE SETTLEMENT (2026-09-04, F3 dry run finding) ---
            # ``entry_price`` of a NO position is the NO cost (the
            # mark-to-market sweep above inverts the YES estimate the same
            # way), but every binary settlement branch returns the payoff of
            # the YES leg (1.00 when the bracket settles yes). Booking that
            # against a NO entry flipped the sign of every settled NO paper
            # trade (a winning NO at 0.33 closed at 0.00 = -0.33/contract).
            # The unresolved flat close returns entry_price and is excluded.
            if (
                is_binary_settlement
                and reason != "SETTLEMENT_UNRESOLVED"
                and pos.get("contract_side") == "NO"
            ):
                exit_price = 1.0 - exit_price

            # --- SANITY CHECK: Binary options must be in [0.00, 1.00] ---
            if exit_price > 1.0 or exit_price < 0.0:
                logger.error(
                    f"[OMS] SANITY FAIL: exit_price={exit_price:.4f} for {pos['symbol']} "
                    f"(reason={reason}). Raw spot leaked! Clamping to entry_price."
                )
                exit_price = pos["entry_price"]  # Neutral close (no PnL)

            # --- CALCULATE PNL ---
            if pos["side"] == "buy":
                total_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
            else:
                total_pnl = (pos["entry_price"] - exit_price) * pos["quantity"]

            # Compute and deduct exit fee. Same rule as the entry: a position
            # whose fill type was never recorded is charged as a taker, never
            # as the free maker side. (A settlement close prices at 0.00/1.00,
            # where every fee formula is zero — Kalshi charges no settlement
            # fee — so this term only bites on a traded-out exit.)
            from src.core.fee_calculator import compute_fee, fee_type_for_symbol

            exit_fee_result = compute_fee(
                exit_price,
                pos["quantity"],
                is_maker=pos.get("is_maker", False),
                series_fee_type=fee_type_for_symbol(pos["symbol"]),
            )
            exit_fee = exit_fee_result.fee
            self.total_fees_paid += exit_fee
            total_pnl -= exit_fee

            pos["exit_price"] = exit_price
            pos["pnl"] = total_pnl
            pos["close_time"] = datetime.now()
            pos["reason"] = reason
            pos["exit_fee"] = exit_fee

            self.realized_pnl += total_pnl

            # Task F: mirror into the immutable cumulative ledger. total_pnl is
            # already net of this exit fee (matching the stored ``pnl`` field),
            # so cumulative_realized_pnl reconciles with sum(closed_trades.pnl).
            # The entry fee was counted once at open time.
            self.cumulative_realized_pnl += total_pnl
            self.cumulative_fees_paid += exit_fee

            self.closed_trades.append(pos)
            self.positions.remove(pos)

            # Sprint 4: audit trail for close
            self.order_audit.append(
                {
                    "event": "CLOSE",
                    "position_id": pos.get("id"),
                    "symbol": pos["symbol"],
                    "reason": reason,
                    "exit_price": exit_price,
                    "exit_fee": exit_fee,
                    "pnl": total_pnl,
                    "timestamp": datetime.now().isoformat(),
                }
            )

            label = "WIN" if total_pnl > 0 else "LOSS"
            # Explicit Log for User Clarity
            logger.info(f"[OMS] 🔨 CLOSED {pos['symbol']} ({reason})")
            logger.info(
                f"      Entry: ${pos['entry_price']:.2f} | Exit: ${exit_price:.2f} | Qty: {pos['quantity']}"
            )
            logger.info(
                f"      Realized PnL: ${total_pnl:+.2f} ({label}) (exit fee: ${exit_fee:.2f})"
            )

            if self.on_close:
                self.on_close(pos)

            # Sprint 8: persist state after every close
            self._save_state()

        except Exception as e:
            logger.error(f"[OMS] Error closing position: {e}")

    def get_stats(self):
        total_fills = self.maker_fills + self.taker_fills
        return {
            "realized": self.realized_pnl,
            "unrealized": self.unrealized_pnl,
            "open_count": len(self.positions),
            "total_fees": self.total_fees_paid,
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
            "maker_ratio": self.maker_fills / total_fills if total_fills > 0 else 1.0,
            # Task F: lifetime totals that survive reset_stats / balance syncs.
            "cumulative_net": self.get_cumulative_net_pnl(),
            "cumulative_realized": self.cumulative_realized_pnl,
            "cumulative_fees": self.cumulative_fees_paid,
        }

    def reset_stats(self):
        """Reset the *fragment* realized-PnL counter (used after a balance sync).

        This intentionally only zeroes ``realized_pnl`` — the per-sync fragment
        that the risk manager folds into the running balance. The immutable
        cumulative ledger (``cumulative_realized_pnl``, ``cumulative_fees_paid``,
        ``cumulative_entry_fees``) is NEVER touched here: it is the trustworthy
        lifetime total exposed via ``get_cumulative_net_pnl()`` for real-capital
        gating. Do NOT add cumulative_* resets to this method.
        """
        self.realized_pnl = 0.0
