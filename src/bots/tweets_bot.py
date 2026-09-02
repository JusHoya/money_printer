"""Feed-only harvester for Kalshi's X-settled ("TWEETS") markets plus the X
timeline tape that would price them.

Two feeds, one bot:

1. **Kalshi side** — one ladder list call per series per tick (the gas /
   crypto_annual pattern) over the series whose settlement source is an X
   account, plus an hourly top-3 orderbook depth snapshot.
2. **X side** — :class:`src.data.x_provider.XProvider` polls the tracked
   handles (default: the settlement account of the live series) and appends
   every raw post to ``data/x_feed/x_posts_<UTCdate>.jsonl``. Every real
   poll (one per handle per >=60s, whether or not it returned posts) also
   writes one ``@handle (X)`` row into the dashboard's data log carrying the
   running counts, so the feed's liveness is visible next to the ladder it
   would price.

MARKET FACTS (verified live 2026-09-01)
---------------------------------------
* ``KXELONTWEETS`` (the weekly Elon Musk post-count ladder the X plan was
  written for) has listed NO event since 2025-04-18 — the series is dormant.
  It stays in the default set because a list call against an empty series is
  one cheap request, and the ladder is the reason this lane exists if Kalshi
  relists it.
* ``KXPOTUSTWEETS`` degraded from a weekly count ladder to a monthly binary:
  ``KXPOTUSTWEETS-26OCT01-0`` "Will @realDonaldTrump tweet in Sep 2026?"
  (YES 0.62/0.69, ~32 contracts of volume). Kalshi settles it from X itself.
* The LIVE post-count ladder is **Truth Social**, not X: ``KXTRUTHSOCIAL``
  (weekly, 10 active brackets, 13-17k contracts on the middle brackets,
  settles Saturday 13:59 UTC from Roll Call's Factbase count). It is NOT
  harvested here — its underlying is a different feed (Factbase / Truth
  Social API) that no provider in this repo reads yet.
* Every X-settled series reports ``fee_type == "quadratic"`` with
  ``fee_multiplier == 1`` (the standard schedule).

COST (why the default handle list is what it is)
------------------------------------------------
The X API bills per unique post read. ``@realDonaldTrump`` posts on X a
handful of times a month (his volume is on Truth Social), so tracking him
costs cents. ``@elonmusk`` posts 100+ times a day — roughly $15+/month — for
a ladder that is currently dormant, so he is NOT in the default set; add him
via ``X_TRACK_HANDLES`` only when ``KXELONTWEETS`` relists. The first poll of
any handle backfills up to 100 historical posts (<= $0.50, once).

FEED-ONLY
---------
:data:`TWEETS_TRADING_ENABLED` is ``False`` and there is no strategy behind
it. The X poller is additionally gated by ``X_FEED_ENABLED`` (env): with it
off — the shipped default — the bot logs the disabled state at setup,
harvests only the Kalshi side, and never touches the X API. When it is on but
the account resolution fails (bad token, API outage at container start), the
bot retries ``connect()`` every :data:`X_CONNECT_RETRY_S` instead of staying
dead until the next redeploy.

REGISTRATION IS IN ``src/bots/__init__.py``
-------------------------------------------
No ``@BotRegistry.register`` decorator here — same reasoning as
:mod:`src.bots.gas_bot`: registration is global process state pinned by
``tests/test_lean_config_2026_06_03.py``, so it lives in the package init.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.bots.base import Bot
from src.bots.mixins import SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.data.x_provider import XProvider, _parse_handles
from src.utils.logger import logger

# No strategy exists for the X-settled markets; the flag is a posture marker.
TWEETS_TRADING_ENABLED = False

#: Settlement X account per series (from each series' ``settlement_sources``).
SERIES_SETTLEMENT_HANDLES: Dict[str, str] = {
    "KXPOTUSTWEETS": "realDonaldTrump",
    "KXELONTWEETS": "elonmusk",
}

#: Handles polled when ``X_TRACK_HANDLES`` is unset. Only the account behind
#: the one LIVE market — see the COST section for why @elonmusk is excluded.
DEFAULT_TRACK_HANDLES: Tuple[str, ...] = ("realDonaldTrump",)

#: Hard cap on series harvested per tick (single-threaded market-loop budget;
#: each series costs ~1s per tick).
MAX_TWEETS_SERIES = 6

#: How often a failed X ``connect()`` is retried while the feed is enabled.
X_CONNECT_RETRY_S = 900.0


def _parse_tweets_series() -> Tuple[str, ...]:
    """Parse the ``TWEETS_SERIES`` env override, capped at
    :data:`MAX_TWEETS_SERIES` with a WARNING naming the dropped series."""
    parsed = tuple(
        s.strip().upper()
        for s in os.getenv("TWEETS_SERIES", "KXPOTUSTWEETS,KXELONTWEETS").split(",")
        if s.strip()
    )
    if len(parsed) > MAX_TWEETS_SERIES:
        logger.warning(
            "[Tweets] TWEETS_SERIES names %d series; harvesting only the "
            "first %d (single-threaded market-loop budget). Dropped: %s",
            len(parsed),
            MAX_TWEETS_SERIES,
            ", ".join(parsed[MAX_TWEETS_SERIES:]),
        )
        parsed = parsed[:MAX_TWEETS_SERIES]
    return parsed


def _default_handles() -> List[str]:
    """``X_TRACK_HANDLES`` when set, else :data:`DEFAULT_TRACK_HANDLES`."""
    return _parse_handles(os.getenv("X_TRACK_HANDLES", "")) or list(
        DEFAULT_TRACK_HANDLES
    )


#: Series harvested every tick (``TWEETS_SERIES`` env, comma-separated).
TWEETS_SERIES: Tuple[str, ...] = _parse_tweets_series()


class TweetsBot(Bot, SignalProcessorMixin):
    """Feed-only X-settled-market harvester with the X timeline tape."""

    SERIES: Tuple[str, ...] = TWEETS_SERIES

    # Orderbook depth snapshots are HOURLY only; the per-tick path must never
    # issue per-market orderbook calls (the FR-0.7 harvester cadence rule).
    DEPTH_SNAPSHOT_INTERVAL_S = 3600
    MAX_DEPTH_MARKETS_PER_SERIES = 20

    def __init__(self, x_provider: Optional[XProvider] = None):
        Bot.__init__(self, name="Tweets")
        self.kalshi = None
        self._last_depth_snapshot = 0.0
        self.strategies = {}
        self.x = x_provider if x_provider is not None else XProvider(
            handles=_default_handles()
        )
        self.x_connected = False
        self._next_x_connect = 0.0
        # handle(lower) -> {"since_start": int, "day": "YYYY-MM-DD", "today": int}
        self._x_counts: Dict[str, dict] = {}

    # -- lifecycle -------------------------------------------------------

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        self._connect_x()
        logger.info(
            "[Tweets] setup complete: series=%s trading=%s x_feed=%s handles=%s",
            ",".join(self.SERIES),
            "ENABLED" if TWEETS_TRADING_ENABLED else "FEED-ONLY",
            self._x_state(),
            ",".join(self.x.handles) or "-",
        )

    def get_symbols(self) -> List[str]:
        return list(self.SERIES)

    # -- tick ------------------------------------------------------------

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        if not self.kalshi:
            return []

        # Decide ONCE per tick whether the hourly depth pass is due, so every
        # series is snapshotted in the same pass.
        depth_due = (
            time.time() - self._last_depth_snapshot
        ) >= self.DEPTH_SNAPSHOT_INTERVAL_S

        for series in self.SERIES:
            try:
                ladder = self._ladder(series)
            except Exception as exc:  # noqa: BLE001 - one bad series must not
                # take the tick down; the failure is logged and the next series
                # still gets harvested.
                logger.error("[Tweets] Market Fetch Fail (%s): %s", series, exc)
                continue

            if not ladder:
                logger.info("[Tweets] %s: no active markets returned", series)
                continue

            for market in ladder:
                self._record(market, dashboard)

            for market in ladder:
                price = self._best_price(market)
                if price is not None:
                    risk_manager.update_market_data(market.symbol, price)
                    risk_manager.exchange.update_market_price(market.symbol, price)

            if depth_due:
                self._snapshot_depth(ladder, dashboard)

            if TWEETS_TRADING_ENABLED:
                # No strategy exists; a flipped flag with nothing behind it is
                # a loud misconfiguration, not a silent one.
                logger.warning(
                    "[Tweets] TWEETS_TRADING_ENABLED is True but no strategy "
                    "exists for the X-settled markets; nothing to run"
                )
            else:
                logger.info(
                    "[Tweets] %s FEED-ONLY: %d markets recorded, 0 signals "
                    "emitted (no strategy exists for the X-settled markets)",
                    series,
                    len(ladder),
                )

            time.sleep(1)  # be polite between series

        if depth_due:
            self._last_depth_snapshot = time.time()

        self._poll_x(dashboard)
        return []

    # -- X feed ----------------------------------------------------------

    def _x_state(self) -> str:
        if not self.x.enabled:
            return "DISABLED (X_FEED_ENABLED off)"
        return "CONNECTED" if self.x_connected else "ENABLED-BUT-UNCONNECTED"

    def _connect_x(self) -> bool:
        """``connect()`` the poller; schedule a retry when enabled but failing."""
        try:
            self.x_connected = bool(self.x.connect())
        except Exception as exc:  # noqa: BLE001 - the provider promises a
            # clean False, but a dead feed must never take the bot down.
            logger.warning("[Tweets] X connect raised: %s", exc)
            self.x_connected = False
        if self.x.enabled and not self.x_connected:
            self._next_x_connect = time.monotonic() + X_CONNECT_RETRY_S
            logger.warning(
                "[Tweets] X feed enabled but not connected; retrying in %.0fs",
                X_CONNECT_RETRY_S,
            )
        return self.x_connected

    def _poll_x(self, dashboard) -> None:
        """Poll every tracked handle once (the provider enforces the >=60s
        floor per handle, so a fast tick costs no request)."""
        if not self.x.enabled:
            return
        if not self.x_connected:
            if time.monotonic() >= self._next_x_connect:
                self._connect_x()
            if not self.x_connected:
                return

        for handle in self.x.handles:
            if not self.x.poll_due(handle):
                continue  # inside the >=60s floor: no request, no row
            try:
                posts = self.x.poll_handle(handle)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Tweets] X poll raised for @%s: %s", handle, exc)
                continue
            counts = self._bump_counts(handle, posts)
            if posts:
                latest = posts[0]
                logger.info(
                    "[Tweets] @%s +%d post(s) (today=%d since_start=%d) latest=%s %r",
                    handle,
                    len(posts),
                    counts["today"],
                    counts["since_start"],
                    latest.get("id"),
                    str(latest.get("text") or "")[:80],
                )
            # One data-log row per REAL poll (not per tick): price is today's
            # post count (UTC, by created_at), volume the new posts this poll,
            # last the running total since the feed started. A poll that
            # returned nothing still writes its row — that zero is the feed's
            # heartbeat in the tape and the flat segment of the count series;
            # without it the row appeared once (the backfill) and scrolled
            # out of the dashboard's 100-row data log within a tick.
            dashboard.update_price(
                f"@{handle} (X)",
                float(counts["today"]),
                volume=len(posts),
                last=counts["since_start"],
            )

    def _bump_counts(self, handle: str, posts: List[dict]) -> dict:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = handle.lower()
        counts = self._x_counts.setdefault(
            key, {"since_start": 0, "day": today, "today": 0}
        )
        if counts["day"] != today:
            counts["day"] = today
            counts["today"] = 0
        counts["since_start"] += len(posts)
        counts["today"] += sum(
            1 for p in posts if str(p.get("created_at") or "")[:10] == today
        )
        return counts

    # -- Kalshi helpers --------------------------------------------------

    def _ladder(self, series: str):
        """Full quote ladder for a series in one list call, or ``[]``."""
        if not hasattr(self.kalshi, "fetch_market_ladder"):
            return []
        return self.kalshi.fetch_market_ladder(series) or []

    @staticmethod
    def _best_price(market) -> Optional[float]:
        """Bid, else ask, else last — whichever is a usable price."""
        for candidate in (market.bid, market.ask, market.price):
            try:
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if 0.0 < value < 1.0:
                return value
        return None

    def _record(self, market, dashboard) -> None:
        """Record one market's full quote row for the harvester."""
        extra = market.extra or {}
        best = self._best_price(market) or 0.0
        dashboard.update_price(
            f"{market.symbol} (Market)",
            best,
            bid=market.bid,
            ask=market.ask,
            no_bid=extra.get("no_bid", 0.0),
            no_ask=extra.get("no_ask", 0.0),
            last=market.price,
            volume=market.volume,
            strike_type=extra.get("strike_type"),
            floor_strike=extra.get("floor_strike"),
            cap_strike=extra.get("cap_strike"),
            yes_sub_title=extra.get("yes_sub_title"),
            close_time=extra.get("close_time"),
        )

    def _snapshot_depth(self, ladder, dashboard) -> None:
        """Hourly top-3 orderbook levels, throttled well under the rate limit."""
        for market in ladder[: self.MAX_DEPTH_MARKETS_PER_SERIES]:
            try:
                book = self.kalshi.fetch_orderbook(market.symbol, depth=3)
                if book and (book.get("yes") or book.get("no")):
                    extra = market.extra or {}
                    dashboard.record_depth(
                        market.symbol,
                        book,
                        last_price=market.price,
                        strike_type=extra.get("strike_type"),
                        floor_strike=extra.get("floor_strike"),
                        cap_strike=extra.get("cap_strike"),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Tweets] Depth snapshot failed for %s: %s", market.symbol, exc
                )
            time.sleep(0.15)


__all__ = [
    "DEFAULT_TRACK_HANDLES",
    "MAX_TWEETS_SERIES",
    "SERIES_SETTLEMENT_HANDLES",
    "TWEETS_SERIES",
    "TWEETS_TRADING_ENABLED",
    "TweetsBot",
    "X_CONNECT_RETRY_S",
]
