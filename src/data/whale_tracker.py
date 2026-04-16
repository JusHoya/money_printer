"""
whale_tracker.py — Monitors Kalshi orderbooks and public trade feed for whale activity.

Surfaces large resting orders, large executed trades, order-flow imbalance, and
unusual volume patterns without requiring any private/authenticated API access
(all methods use the public REST endpoints).

Usage example::

    from src.data.kalshi_provider import KalshiProvider
    from src.data.whale_tracker import WhaleTracker

    provider = KalshiProvider()   # anonymous / read-only is fine
    tracker  = WhaleTracker(provider)

    print(tracker.fetch_orderbook("KXBTCD-26APR1617"))
    print(tracker.get_order_flow_imbalance("KXBTCD-26APR1617"))
    print(tracker.get_volume_profile("KXBTCD-26APR1617"))
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any, Dict, List, Optional

from src.data.kalshi_provider import KalshiProvider
from src.utils.logger import logger


class WhaleTracker:
    """
    Monitors Kalshi orderbooks and public trade feeds for whale activity.

    Wraps a :class:`~src.data.kalshi_provider.KalshiProvider` instance so that
    its session, auth headers, and API URL are reused — no separate credentials
    are required beyond those already supplied to the provider.

    All public endpoint calls work in anonymous (ghost) mode; authenticated
    mode simply adds signed headers for extra rate-limit headroom.
    """

    def __init__(self, provider: KalshiProvider) -> None:
        """
        Args:
            provider: An initialised ``KalshiProvider`` instance.
                      Works in both anonymous and authenticated modes.
        """
        self.provider = provider
        self.api_url  = provider.api_url.rstrip("/")
        self.session  = provider.session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """Perform an authenticated GET and return parsed JSON, or ``None`` on error."""
        url     = f"{self.api_url}{path}"
        headers = self.provider._get_authenticated_headers("GET", path)
        try:
            resp = self.session.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[WhaleTracker] GET {path} failed: {e}")
            return None

    @staticmethod
    def _cents_to_dollars(value: Any) -> float:
        """
        Normalise a Kalshi price to a ``float`` in the ``[0.0, 1.0]`` range.

        Kalshi orderbook levels use integer cents (0-100 scale).
        If *value* is already ≤ 1.0 it is assumed to be in dollar form already.
        """
        try:
            v = float(value)
            return round(v / 100.0, 4) if v > 1.0 else round(v, 4)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Core data fetchers
    # ------------------------------------------------------------------

    def fetch_orderbook(self, ticker: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch the live L2 orderbook for *ticker*.

        Kalshi V2 returns ``yes`` and ``no`` arrays of ``[price_cents, size]``
        pairs.  This method normalises them into canonical bid/ask form:

        * **bids** — YES bids (people buying YES), sorted by price descending.
        * **asks** — YES asks (people selling YES ≡ buying NO), sorted ascending.

        Each level is::

            {"price": float,  # probability / dollar price in [0, 1]
             "size":  int}    # number of contracts at that level

        Returns:
            ``{"bids": [...], "asks": [...]}``; both lists are empty on failure.
        """
        path = f"/markets/{ticker}/orderbook"
        data = self._get(path)
        if not data:
            logger.warning(f"[WhaleTracker] fetch_orderbook: no data for {ticker}")
            return {"bids": [], "asks": []}

        ob        = data.get("orderbook", {})
        yes_levels = ob.get("yes", [])  # YES bids: [[price_cents, size], ...]
        no_levels  = ob.get("no",  [])  # NO  bids: [[price_cents, size], ...]

        # YES bids → buy-side
        bids = sorted(
            [
                {"price": self._cents_to_dollars(p), "size": int(s)}
                for p, s in yes_levels
            ],
            key=lambda x: x["price"],
            reverse=True,
        )

        # NO bid at P cents → YES ask at (100 − P) cents
        asks = sorted(
            [
                {"price": round(1.0 - self._cents_to_dollars(p), 4), "size": int(s)}
                for p, s in no_levels
            ],
            key=lambda x: x["price"],
        )

        logger.info(
            f"[WhaleTracker] Orderbook {ticker}: "
            f"{len(bids)} bid level(s), {len(asks)} ask level(s)"
        )
        return {"bids": bids, "asks": asks}

    def fetch_recent_trades(
        self, ticker: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Fetch the public trade history for *ticker*.

        Args:
            ticker: Kalshi market ticker (e.g. ``"KXBTCD-26APR1617"``).
            limit:  Maximum number of trades to return (default 100, max 1000).

        Returns:
            List of trade dicts, newest first::

                {
                    "trade_id":  str,
                    "price":     float,     # executed YES price in [0, 1]
                    "size":      int,       # contracts filled
                    "side":      str,       # "yes" or "no" (taker side)
                    "timestamp": datetime,  # UTC-aware
                }

            Returns ``[]`` on failure.
        """
        path = f"/markets/{ticker}/trades"
        data = self._get(path, params={"limit": limit})
        if not data:
            logger.warning(f"[WhaleTracker] fetch_recent_trades: no data for {ticker}")
            return []

        raw_trades = data.get("trades", [])
        trades: List[Dict[str, Any]] = []

        for t in raw_trades:
            # Price: prefer V2 _dollars string, fall back to V1 cents int
            price_dollars = t.get("yes_price_dollars")
            if price_dollars is not None:
                try:
                    price = float(price_dollars)
                except (TypeError, ValueError):
                    price = self._cents_to_dollars(t.get("yes_price", 0))
            else:
                price = self._cents_to_dollars(t.get("yes_price", 0))

            # Timestamp: try several possible field names
            ts_raw = (
                t.get("created_time")
                or t.get("trade_time")
                or t.get("timestamp")
            )
            try:
                ts = (
                    datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts_raw
                    else datetime.now(timezone.utc)
                )
            except Exception:
                ts = datetime.now(timezone.utc)

            trades.append(
                {
                    "trade_id":  t.get("trade_id", ""),
                    "price":     price,
                    "size":      int(t.get("count", 0)),
                    "side":      t.get("taker_side", "unknown"),
                    "timestamp": ts,
                }
            )

        logger.info(
            f"[WhaleTracker] fetch_recent_trades {ticker}: {len(trades)} trade(s) returned"
        )
        return trades

    # ------------------------------------------------------------------
    # Whale detection
    # ------------------------------------------------------------------

    def detect_large_orders(
        self, ticker: str, threshold_contracts: int = 500
    ) -> List[Dict[str, Any]]:
        """
        Scan the live orderbook for resting orders that meet or exceed
        *threshold_contracts*.

        Args:
            ticker:               Kalshi market ticker.
            threshold_contracts:  Minimum contract size to flag (default 500).

        Returns:
            List of whale-order dicts::

                {
                    "ticker":    str,
                    "side":      str,   # "bid" or "ask"
                    "price":     float,
                    "size":      int,
                    "threshold": int,
                }

            Empty list if nothing qualifies or the orderbook is unavailable.
        """
        ob     = self.fetch_orderbook(ticker)
        whales: List[Dict[str, Any]] = []

        for level in ob["bids"]:
            if level["size"] >= threshold_contracts:
                whales.append(
                    {
                        "ticker":    ticker,
                        "side":      "bid",
                        "price":     level["price"],
                        "size":      level["size"],
                        "threshold": threshold_contracts,
                    }
                )

        for level in ob["asks"]:
            if level["size"] >= threshold_contracts:
                whales.append(
                    {
                        "ticker":    ticker,
                        "side":      "ask",
                        "price":     level["price"],
                        "size":      level["size"],
                        "threshold": threshold_contracts,
                    }
                )

        if whales:
            logger.info(
                f"[WhaleTracker] detect_large_orders {ticker}: "
                f"{len(whales)} order(s) ≥ {threshold_contracts} contracts"
            )
        else:
            logger.info(
                f"[WhaleTracker] detect_large_orders {ticker}: "
                f"no orders ≥ {threshold_contracts} contracts"
            )

        return whales

    def detect_large_trades(
        self, ticker: str, threshold_contracts: int = 200
    ) -> List[Dict[str, Any]]:
        """
        Scan recent executed trades for whale-sized fills.

        Args:
            ticker:               Kalshi market ticker.
            threshold_contracts:  Minimum fill size to flag (default 200).

        Returns:
            List of large-trade dicts (same shape as
            :meth:`fetch_recent_trades` entries, with an extra ``"threshold"``
            key).  Empty list if nothing qualifies.
        """
        trades = self.fetch_recent_trades(ticker)
        large  = [
            {**t, "threshold": threshold_contracts}
            for t in trades
            if t["size"] >= threshold_contracts
        ]

        if large:
            logger.info(
                f"[WhaleTracker] detect_large_trades {ticker}: "
                f"{len(large)} trade(s) ≥ {threshold_contracts} contracts"
            )
        else:
            logger.info(
                f"[WhaleTracker] detect_large_trades {ticker}: "
                f"no trades ≥ {threshold_contracts} contracts"
            )

        return large

    # ------------------------------------------------------------------
    # Derived analytics
    # ------------------------------------------------------------------

    def get_order_flow_imbalance(self, ticker: str) -> float:
        """
        Compute signed order-flow imbalance (OFI) from current orderbook depth.

        Formula::

            OFI = (Σ bid_sizes − Σ ask_sizes) / (Σ bid_sizes + Σ ask_sizes)

        Args:
            ticker: Kalshi market ticker.

        Returns:
            ``float`` in ``[-1.0, 1.0]``:

            * ``+1.0`` — all quoted liquidity is on the buy side (strong buy pressure)
            * ``-1.0`` — all quoted liquidity is on the sell side (strong sell pressure)
            *  ``0.0`` — balanced book (or empty / unavailable)
        """
        ob         = self.fetch_orderbook(ticker)
        total_bid  = sum(lvl["size"] for lvl in ob["bids"])
        total_ask  = sum(lvl["size"] for lvl in ob["asks"])
        total      = total_bid + total_ask

        if total == 0:
            logger.warning(
                f"[WhaleTracker] get_order_flow_imbalance {ticker}: "
                "empty orderbook — returning 0.0"
            )
            return 0.0

        ofi = (total_bid - total_ask) / total
        logger.info(
            f"[WhaleTracker] OFI {ticker}: {ofi:+.4f} "
            f"(bid_depth={total_bid}, ask_depth={total_ask})"
        )
        return round(ofi, 6)

    def get_volume_profile(self, ticker: str) -> Dict[str, Any]:
        """
        Build a volume profile for *ticker* using recent trades and market details.

        Combines the last 100 trades with all-time market volume and open-interest
        data from the market endpoint, then applies several heuristics to flag
        unusual activity.

        Args:
            ticker: Kalshi market ticker.

        Returns:
            Dict with the following keys:

            =====================  ========  =========================================
            Key                    Type      Description
            =====================  ========  =========================================
            total_volume           int       Sum of contract sizes in the sample
            trade_count            int       Number of trades in the sample
            avg_trade_size         float     Mean contracts per trade
            max_trade_size         int       Largest single fill in the sample
            buy_volume             int       Contracts on taker-side YES
            sell_volume            int       Contracts on taker-side NO
            buy_sell_ratio         float     buy_volume / max(sell_volume, 1)
            stddev_trade_size      float     Std-dev of trade sizes (0 if < 2 trades)
            unusual_activity       bool      True if any heuristic flag fires
            flags                  list[str] Human-readable explanation of each flag
            market_volume_fp       int       Total all-time volume (market endpoint)
            open_interest          int       Current open interest (contracts)
            =====================  ========  =========================================
        """
        trades = self.fetch_recent_trades(ticker, limit=100)

        # ---- Market-level stats from the /markets/{ticker} endpoint ----
        market_volume_fp = 0
        open_interest    = 0
        market_data      = self._get(f"/markets/{ticker}")
        if market_data:
            m     = market_data.get("market", {})
            vol_fp = m.get("volume_fp")
            oi_fp  = m.get("open_interest_fp")
            try:
                market_volume_fp = int(float(vol_fp)) if vol_fp is not None else 0
            except (TypeError, ValueError):
                market_volume_fp = m.get("volume", 0) or 0
            try:
                open_interest = int(float(oi_fp)) if oi_fp is not None else 0
            except (TypeError, ValueError):
                open_interest = m.get("open_interest", 0) or 0

        # ---- Empty sample guard ----------------------------------------
        if not trades:
            logger.warning(
                f"[WhaleTracker] get_volume_profile {ticker}: no trades returned"
            )
            return {
                "total_volume":      0,
                "trade_count":       0,
                "avg_trade_size":    0.0,
                "max_trade_size":    0,
                "buy_volume":        0,
                "sell_volume":       0,
                "buy_sell_ratio":    1.0,
                "stddev_trade_size": 0.0,
                "unusual_activity":  False,
                "flags":             [],
                "market_volume_fp":  market_volume_fp,
                "open_interest":     open_interest,
            }

        # ---- Aggregate stats -------------------------------------------
        sizes      = [t["size"] for t in trades]
        buy_vol    = sum(t["size"] for t in trades if t["side"] == "yes")
        sell_vol   = sum(t["size"] for t in trades if t["side"] == "no")
        total_vol  = sum(sizes)
        avg_size   = mean(sizes)
        max_size   = max(sizes)
        std_size   = stdev(sizes) if len(sizes) >= 2 else 0.0
        bsr        = buy_vol / max(sell_vol, 1)

        # ---- Heuristic flags -------------------------------------------
        flags: List[str] = []

        # 1. A single trade is > 3× the mean (unusually large fill)
        if avg_size > 0 and max_size > 3 * avg_size:
            flags.append(
                f"Max trade size ({max_size}) is "
                f"{max_size / avg_size:.1f}× the sample average ({avg_size:.1f})"
            )

        # 2. Heavily one-sided flow (>80% taker-buy or >80% taker-sell)
        if total_vol > 0:
            buy_pct = buy_vol / total_vol
            if buy_pct > 0.80:
                flags.append(
                    f"Buy-side dominance: {buy_pct:.0%} of volume is taker-buy YES"
                )
            elif buy_pct < 0.20:
                flags.append(
                    f"Sell-side dominance: {1 - buy_pct:.0%} of volume is taker-sell (NO takers)"
                )

        # 3. Any single trade ≥ 200 contracts
        large_count = sum(1 for s in sizes if s >= 200)
        if large_count:
            flags.append(
                f"{large_count} trade(s) ≥ 200 contracts in the last "
                f"{len(trades)} fills"
            )

        # 4. Recent window covers > 10% of all-time market volume
        if market_volume_fp > 0 and total_vol > 0.10 * market_volume_fp:
            flags.append(
                f"Recent {len(trades)}-trade window ({total_vol} contracts) "
                f"is {total_vol / market_volume_fp:.0%} of total market volume "
                f"({market_volume_fp})"
            )

        unusual = len(flags) > 0

        profile: Dict[str, Any] = {
            "total_volume":      total_vol,
            "trade_count":       len(trades),
            "avg_trade_size":    round(avg_size, 2),
            "max_trade_size":    max_size,
            "buy_volume":        buy_vol,
            "sell_volume":       sell_vol,
            "buy_sell_ratio":    round(bsr, 4),
            "stddev_trade_size": round(std_size, 2),
            "unusual_activity":  unusual,
            "flags":             flags,
            "market_volume_fp":  market_volume_fp,
            "open_interest":     open_interest,
        }

        logger.info(
            f"[WhaleTracker] Volume profile {ticker}: "
            f"vol={total_vol}, trades={len(trades)}, "
            f"buy/sell={buy_vol}/{sell_vol}, unusual={unusual}"
            + (f", flags={flags}" if flags else "")
        )
        return profile
