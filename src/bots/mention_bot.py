"""Feed-only harvester bot for the Kalshi Mentions category.

Structure mirrors :mod:`src.bots.gas_bot`: one ladder list call per series per
tick harvests the full quote ladder into the dashboard's recorder, an hourly
top-3 orderbook snapshot records depth, and the strategy scaffold runs only
behind a module-level kill switch.

MARKET FACTS (verified live 2026-09-01)
---------------------------------------
* The Mentions category spans **95 series**; all report
  ``fee_type == "quadratic"`` with ``fee_multiplier == 1``, i.e. the standard
  schedule — the maker fee rounds to ~$0.
* Settlement follows the MENTION.pdf grammar: plurals and possessives of the
  target word count, tense variants do not, closed compounds do not.
* The CFTC opened a probe into the category in Aug 2026 (sports mention
  markets were pulled). The whole category carries delisting risk, which the
  harvest tape is precisely the cheap way to keep watching.

FEED-ONLY BY DEFAULT
--------------------
:data:`MENTION_TRADING_ENABLED` is ``False``. No backtest artifact exists for
the mention engine — ``data/mention_base_rates.json`` has to be built from
historical transcripts with the MENTION.pdf grammar encoded before there is a
verdict this flag could reflect (see the activation path in
:mod:`src.strategies.mention_strategy`). Price and data feeds run regardless;
the 20% mention allocation bucket in :class:`src.core.risk_manager.RiskManager`
already bounds any future flip.

NO DATE FILTER
--------------
The weather bot's ``%y%b%d`` ticker date targeting is deliberately NOT reused:
mention tickers look like ``KXLEAVITTMENTION-26AUG27-<WORD>`` and events open
and close on their own schedule, so the harvest takes every ``active`` and
``initialized`` market ``fetch_market_ladder`` returns.

REGISTRATION IS IN ``src/bots/__init__.py``
-------------------------------------------
No ``@BotRegistry.register`` decorator here — same reasoning as
:mod:`src.bots.gas_bot`: registration is global process state pinned by
``tests/test_lean_config_2026_06_03.py``, so it lives in the package init
where every sanctioned bot registers.
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Tuple

from src.bots.base import Bot
from src.bots.mixins import SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.strategies.mention_strategy import MentionStrategy
from src.utils.logger import logger

# No transcript-derived base rates, no verdict, no trading. Feeds still run.
MENTION_TRADING_ENABLED = False

#: Hard cap on how many series one tick may harvest. The harvest is serial
#: inside the single-threaded market loop with a polite 1s sleep between
#: series, so N series cost >~N seconds per tick — widening toward the full
#: 95-series category would starve every other bot for minutes.
MAX_MENTION_SERIES = 12


def _parse_mention_series() -> Tuple[str, ...]:
    """Parse the ``MENTION_SERIES`` env override, capped at
    :data:`MAX_MENTION_SERIES` with a WARNING naming the dropped series."""
    parsed = tuple(
        s.strip().upper()
        for s in os.getenv(
            "MENTION_SERIES", "KXTRUMPMENTION,KXPRESMENTION,KXLEAVITTMENTION"
        ).split(",")
        if s.strip()
    )
    if len(parsed) > MAX_MENTION_SERIES:
        logger.warning(
            "[Mention] MENTION_SERIES names %d series; harvesting only the "
            "first %d (single-threaded market-loop budget). Dropped: %s",
            len(parsed),
            MAX_MENTION_SERIES,
            ", ".join(parsed[MAX_MENTION_SERIES:]),
        )
        parsed = parsed[:MAX_MENTION_SERIES]
    return parsed


#: Series harvested every tick. Overridable via the ``MENTION_SERIES`` env var
#: (comma-separated) so the sandbox can widen the tape without a code change —
#: but only up to :data:`MAX_MENTION_SERIES`: each series costs ~1s of the
#: shared single-threaded market loop per tick, so entries beyond the cap are
#: dropped (with a WARNING) rather than allowed to starve the other bots.
MENTION_SERIES: Tuple[str, ...] = _parse_mention_series()


class MentionBot(Bot, SignalProcessorMixin):
    """Feed-only mention-market harvester.

    ``TickerResolverMixin`` is deliberately not inherited: the whole ladder is
    fetched in one list call, so there is no "smart ticker" to resolve and no
    per-market quote call to make.
    """

    SERIES: Tuple[str, ...] = MENTION_SERIES

    # Orderbook depth snapshots are HOURLY only; the per-tick path must never
    # issue per-market orderbook calls (the FR-0.7 harvester cadence rule).
    DEPTH_SNAPSHOT_INTERVAL_S = 3600
    # Mention events can carry dozens of words; cap the hourly book calls.
    MAX_DEPTH_MARKETS_PER_SERIES = 20

    def __init__(self):
        Bot.__init__(self, name="Mention")
        self.kalshi = None
        self._last_depth_snapshot = 0.0
        self.strategies = {"mention": MentionStrategy()}

    # -- lifecycle -------------------------------------------------------

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        logger.info(
            "[Mention] setup complete: series=%s trading=%s",
            ",".join(self.SERIES),
            "ENABLED" if MENTION_TRADING_ENABLED else "FEED-ONLY",
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
                logger.error("[Mention] Market Fetch Fail (%s): %s", series, exc)
                continue

            if not ladder:
                logger.info("[Mention] %s: no active markets returned", series)
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

            if MENTION_TRADING_ENABLED:
                strategy = self.strategies["mention"]
                for market in ladder:
                    self._process_signals(
                        strategy.analyze(market),
                        strategy_name=strategy.name(),
                        risk_manager=risk_manager,
                        dashboard=dashboard,
                    )
            else:
                logger.info(
                    "[Mention] %s FEED-ONLY: %d markets recorded, 0 signals "
                    "emitted (MENTION_TRADING_ENABLED=False pending "
                    "transcript-derived base rates)",
                    series,
                    len(ladder),
                )

            time.sleep(1)  # be polite between series

        if depth_due:
            self._last_depth_snapshot = time.time()

        return []

    # -- helpers ---------------------------------------------------------

    def _ladder(self, series: str):
        """Full quote ladder for a series in one list call, or ``[]``.

        No date filter: mention events are not ``%y%b%d``-dated, so every
        active+initialized market the API returns is harvested.
        """
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
        """Record one word-market's full quote row for the harvester."""
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
                    "[Mention] Depth snapshot failed for %s: %s", market.symbol, exc
                )
            time.sleep(0.15)
