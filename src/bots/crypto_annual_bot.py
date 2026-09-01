"""Feed-only harvester bot for the Kalshi annual crypto range ladders.

Structure mirrors :mod:`src.bots.gas_bot`: one ladder list call per series per
tick harvests the full quote ladder into the dashboard's recorder, and an
hourly top-3 orderbook snapshot records depth. There is no strategy waterfall
at all — this bot exists to build the tape.

MARKET FACTS (verified live 2026-09-01)
---------------------------------------
``KXBTCY`` / ``KXETHY`` are annual BTC/ETH range ladders: $5k-wide brackets,
settling 2027-01-01. This is NOT the short-horizon crypto the 2026-07-24
review proved structurally unwinnable — that verdict priced a 2.25-4.5pt fee
floor against a 2.1-2.8pt signal ceiling on 15-minute/hourly horizons; an
annual ladder is a different fee-to-horizon regime, which is the only reason
harvesting it is worth a tick.

FEE MULTIPLIER FINDING — UNVERIFIED, TREATED AS STANDARD
--------------------------------------------------------
The live API reports ``fee_multiplier == 0`` for both series, which taken at
face value would make them literally fee-free. No trade has verified that a
fill actually books $0.00, and an understated fee model is the exact
optimistic-EV failure mode behind both HALT verdicts — so
``src.core.fee_calculator.SERIES_FEE_MULTIPLIER`` keeps both entries at the
conservative 1.0. A demo-API trade whose fill receipt shows the charged fee is
the required evidence before that entry (or any EV math downstream of it)
changes.

FEED-ONLY BY DEFAULT
--------------------
:data:`CRYPTO_ANNUAL_TRADING_ENABLED` is ``False`` and there is no strategy to
enable: no engine has been proposed, let alone adjudicated, for these ladders.
The flag exists so the posture is greppable and testable alongside every other
bot's kill switch.

REGISTRATION IS IN ``src/bots/__init__.py``
-------------------------------------------
No ``@BotRegistry.register`` decorator here — same reasoning as
:mod:`src.bots.gas_bot`: registration is global process state pinned by
``tests/test_lean_config_2026_06_03.py``, so it lives in the package init.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from src.bots.base import Bot
from src.bots.mixins import SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.utils.logger import logger

# No strategy exists for the annual ladders; the flag is a posture marker, not
# a gate in front of anything runnable yet.
CRYPTO_ANNUAL_TRADING_ENABLED = False

#: Annual range ladders harvested every tick. Both settle 2027-01-01 on $5k
#: brackets.
CRYPTO_ANNUAL_SERIES: Tuple[str, ...] = ("KXBTCY", "KXETHY")


class CryptoAnnualBot(Bot, SignalProcessorMixin):
    """Feed-only annual BTC/ETH range-ladder harvester.

    ``TickerResolverMixin`` is deliberately not inherited: the whole ladder is
    fetched in one list call, so there is no "smart ticker" to resolve and no
    per-market quote call to make.
    """

    SERIES: Tuple[str, ...] = CRYPTO_ANNUAL_SERIES

    # Orderbook depth snapshots are HOURLY only; the per-tick path must never
    # issue per-market orderbook calls (the FR-0.7 harvester cadence rule).
    DEPTH_SNAPSHOT_INTERVAL_S = 3600
    # A $5k-bracket annual ladder spans a wide strike range; cap book calls.
    MAX_DEPTH_MARKETS_PER_SERIES = 30

    def __init__(self):
        Bot.__init__(self, name="CryptoAnnual")
        self.kalshi = None
        self._last_depth_snapshot = 0.0
        self.strategies = {}

    # -- lifecycle -------------------------------------------------------

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        logger.info(
            "[CryptoAnnual] setup complete: series=%s trading=%s",
            ",".join(self.SERIES),
            "ENABLED" if CRYPTO_ANNUAL_TRADING_ENABLED else "FEED-ONLY",
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
                logger.error("[CryptoAnnual] Market Fetch Fail (%s): %s", series, exc)
                continue

            if not ladder:
                logger.info("[CryptoAnnual] %s: no active markets returned", series)
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

            if CRYPTO_ANNUAL_TRADING_ENABLED:
                # No strategy exists; a flipped flag with nothing behind it is
                # a loud misconfiguration, not a silent one.
                logger.warning(
                    "[CryptoAnnual] CRYPTO_ANNUAL_TRADING_ENABLED is True but "
                    "no strategy exists for the annual ladders; nothing to run"
                )
            else:
                logger.info(
                    "[CryptoAnnual] %s FEED-ONLY: %d markets recorded, 0 "
                    "signals emitted (no strategy exists for the annual "
                    "ladders)",
                    series,
                    len(ladder),
                )

            time.sleep(1)  # be polite between series

        if depth_due:
            self._last_depth_snapshot = time.time()

        return []

    # -- helpers ---------------------------------------------------------

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
        """Record one bracket's full quote row for the harvester."""
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
                    "[CryptoAnnual] Depth snapshot failed for %s: %s",
                    market.symbol,
                    exc,
                )
            time.sleep(0.15)
