"""Coinbase → Kalshi latency arbitrage strategy.

============================================================================
MOTHBALLED 2026-07-24 per PRD §4 A2 / review_2026_07_24 §4.
NOT-FOR-CAPITAL EXPERIMENT — this strategy is intentionally unregistered:
no bot wires it and no runtime path instantiates it. Its former consumers
(the crypto 15m bots) were deleted in the Phase 0 teardown.

Revival preconditions (ALL required before it may be wired to any bot):
  - websocket feeds (Coinbase + Kalshi), not polled REST snapshots
  - <1s event loop end-to-end
  - maker/IOC order placement
  - realistic fill model
  - >=200 paper trades of evidence
============================================================================

Monitors a Coinbase WebSocket feed for rapid BTC price moves and
trades the corresponding Kalshi 15-minute contract before the
market reprices.  This is the highest-volume edge identified in
the PRD research.

Sprint 3, Task 3.4 of Money Printer V2.
"""

import logging
import re
from collections import deque
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from src.core.interfaces import MarketData, Strategy, TradeSignal

logger = logging.getLogger(__name__)


class LatencyArbStrategy(Strategy):
    """Coinbase→Kalshi latency arbitrage.

    When BTC moves >*move_threshold* (default 0.3%) within
    *window_seconds* (default 60 s), check whether the Kalshi
    15-min contract price has adjusted.  If not, place a limit
    order to capture the stale-pricing edge.

    The strategy maintains its own spot-price ring buffer built
    from ``extra['spot_price']`` fields injected by the bot layer.
    """

    def __init__(
        self,
        move_threshold: float = 0.003,  # 0.3%
        window_seconds: int = 60,
        min_edge_cents: float = 0.04,  # 4 cents minimum edge
        cooldown_seconds: int = 30,
    ):
        self.move_threshold = move_threshold
        self.window_seconds = window_seconds
        self.min_edge_cents = min_edge_cents
        self.cooldown_seconds = cooldown_seconds
        self._cooldown_until = datetime.min

        # Ring buffer: (timestamp, spot_price)
        self._spot_buf: deque[Tuple[datetime, float]] = deque(maxlen=300)

    def name(self) -> str:
        pct = self.move_threshold * 100
        return f"Latency Arb (>{pct:.1f}% / {self.window_seconds}s)"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _detect_rapid_move(self, now: datetime) -> Optional[float]:
        """Return the fractional price change if a rapid move is detected.

        Scans the ring buffer for the oldest price within the look-back
        window and compares against the most recent price.  Returns
        ``None`` if the buffer is too shallow or no significant move
        occurred.
        """
        if len(self._spot_buf) < 3:
            return None

        cutoff = now - timedelta(seconds=self.window_seconds)
        oldest_in_window = None
        for ts, px in self._spot_buf:
            if ts >= cutoff:
                oldest_in_window = (ts, px)
                break

        if oldest_in_window is None:
            return None

        latest_price = self._spot_buf[-1][1]
        base_price = oldest_in_window[1]
        if base_price <= 0:
            return None

        move = (latest_price - base_price) / base_price
        if abs(move) >= self.move_threshold:
            return move
        return None

    @staticmethod
    def _extract_strike(symbol: str) -> Optional[float]:
        try:
            parts = symbol.split("-")
            return float(re.sub(r"[A-Za-z]", "", parts[-1]))
        except Exception:
            return None

    @staticmethod
    def _is_15m_symbol(symbol: str) -> bool:
        """True for 15-minute crypto tickers (KX{ASSET}15M-...).

        Their series segment ends in ``15M`` and the symbol's LAST hyphen-segment
        is an expiry MINUTE LABEL (00/15/30/45), not a strike — so the
        ``_extract_strike`` symbol-parse fallback must NOT be used for them.
        Daily ``KX{ASSET}D`` tickers return False and fall through to the parse.
        """
        series = (symbol or "").split("-", 1)[0]
        return "15M" in series.upper()

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        extra = market_data.extra or {}
        now = market_data.timestamp or datetime.now()

        # Accumulate spot prices
        spot = extra.get("spot_price")
        if spot and spot > 1.0:
            self._spot_buf.append((now, spot))

        # Cooldown
        if now < self._cooldown_until:
            return signals

        # Need valid Kalshi data
        if market_data.bid <= 0 or market_data.ask <= 0:
            return signals
        if market_data.ask >= 1.0:
            return signals

        # 2026-06-10 fix: prefer the real API floor_strike (extra["strike"],
        # which the former 15m crypto bot injected — that bot was deleted in
        # the Phase 0 teardown; any future consumer must inject it). For 15m crypto
        # tickers (KX{ASSET}15M-...-{MM}) the symbol's LAST segment is the expiry
        # MINUTE LABEL (00/15/30/45), NOT a strike, so _extract_strike returns
        # garbage (e.g. 0.0 for "-00") — which breaks this strategy's fair-value
        # gate AND, once propagated to sig.strike, would make settlement auto-YES
        # (spot >= 0 is always true).
        #
        # 2026-06-26 fix: the symbol-parse fallback is valid ONLY for daily
        # KX{ASSET}D-...-T{strike} markets, whose suffix genuinely encodes the
        # strike. When extra carries no strike on a 15m ticker, ABORT rather than
        # guess — a minute label as the strike is a guaranteed mis-settlement.
        strike = extra.get("strike")
        if strike is None:
            if self._is_15m_symbol(market_data.symbol):
                return signals
            strike = self._extract_strike(market_data.symbol)
        if strike is None:
            return signals
        try:
            strike = float(strike)
        except (TypeError, ValueError):
            return signals
        # Belt-and-suspenders: a non-positive strike must NEVER reach settlement
        # (catches the "-00" minute-label trap and any other garbage). Note this
        # alone does NOT catch "-15"/"-30"/"-45" (they parse > 0) — the 15m-format
        # detection above is what catches those.
        if strike <= 0:
            return signals

        # Detect rapid BTC spot move
        move = self._detect_rapid_move(now)
        if move is None:
            return signals

        latest_spot = self._spot_buf[-1][1]
        close_time = extra.get("close_time")

        # Market-implied estimate (simple: is spot above/below strike?)
        # If BTC jumped UP, YES contracts on nearby strikes should be more expensive
        if move > 0:
            # Spot rallied — YES value should increase.
            # If market ask is still cheap, buy YES.
            if latest_spot > strike:
                # Rough fair value: probability increases with distance from strike
                rough_fair = min(
                    0.95, 0.5 + 0.5 * min(1.0, (latest_spot - strike) / 500)
                )
                edge = rough_fair - market_data.ask
                if edge >= self.min_edge_cents:
                    lp = max(0.01, min(0.99, market_data.ask))
                    sig = TradeSignal(
                        symbol=market_data.symbol,
                        side="buy",
                        quantity=10,
                        limit_price=lp,
                        confidence=min(0.90, 0.7 + edge),
                        contract_side="YES",
                    )
                    # 2026-06-10 fix: carry the real strike so settlement can
                    # positively settle this position instead of fail-safe-NO.
                    sig.strike = strike
                    sig.stop_loss = max(0.01, lp - 0.06)
                    if close_time:
                        sig.expiration_time = close_time
                    logger.info(
                        "[LatArb] BTC +%.2f%% → BUY YES %s @ %.2f (fair ~%.2f edge=%.3f)",
                        move * 100,
                        market_data.symbol,
                        lp,
                        rough_fair,
                        edge,
                    )
                    signals.append(sig)
                    self._cooldown_until = now + timedelta(
                        seconds=self.cooldown_seconds
                    )

        elif move < 0:
            # Spot dropped — NO value should increase
            if latest_spot < strike:
                rough_fair_no = min(
                    0.95, 0.5 + 0.5 * min(1.0, (strike - latest_spot) / 500)
                )
                no_cost = 1.0 - market_data.bid
                edge = rough_fair_no - no_cost
                if edge >= self.min_edge_cents and no_cost > 0.01:
                    lp = max(0.01, min(0.99, no_cost))
                    sig = TradeSignal(
                        symbol=market_data.symbol,
                        side="buy",
                        quantity=10,
                        limit_price=lp,
                        confidence=min(0.90, 0.7 + edge),
                        contract_side="NO",
                    )
                    # 2026-06-10 fix: carry the real strike so settlement can
                    # positively settle this position instead of fail-safe-NO.
                    sig.strike = strike
                    sig.stop_loss = max(0.01, lp - 0.06)
                    if close_time:
                        sig.expiration_time = close_time
                    logger.info(
                        "[LatArb] BTC %.2f%% → BUY NO %s @ %.2f (fair_no ~%.2f edge=%.3f)",
                        move * 100,
                        market_data.symbol,
                        lp,
                        rough_fair_no,
                        edge,
                    )
                    signals.append(sig)
                    self._cooldown_until = now + timedelta(
                        seconds=self.cooldown_seconds
                    )

        return signals
