"""AAA gas convergence bot for ``KXAAAGASM`` (PRD FR-4.1 / FR-4.3, Phase 4).

Structure mirrors :mod:`src.bots.weather_bot`: one ladder list call per series per
tick harvests the full quote ladder into the dashboard's recorder, an hourly
top-3 orderbook snapshot records depth, and the strategy waterfall runs only
behind a module-level kill switch.

FEED-ONLY BY DEFAULT
--------------------
:data:`GAS_TRADING_ENABLED` is ``False``. PRD Phase 4 exit criterion 2 gates
paper trading on a backtest artifact that fits the FR-4.2 projection on >= 12
months of backfilled history, reports month-end MAE on >= 6 held-out month-ends,
and documents the strategy's simulated historical EV including maker fees —
"the bot trades in paper only if that EV > 0 (else the phase closes with a
documented HALT)". That artifact does not exist yet, so there is no verdict this
flag could be reflecting. It flips when the artifact says so, and not before.
Price and data feeds run regardless, which is what FR-4.1's "values persisted
daily with provenance" needs.

REGISTRATION IS THE ORCHESTRATOR'S
----------------------------------
This module deliberately carries **no** ``@BotRegistry.register`` decorator. Two
reasons, both concrete:

* ``src/bots/registry.py`` and ``src/bots/__init__.py`` are shared files owned by
  the orchestrator (Phase 4 data contract §4).
* ``tests/test_lean_config_2026_06_03.py::test_only_weather_bot_registered``
  asserts ``BotRegistry.list_bots() == ["weather"]``. Registration is global
  process state, so a decorator here would turn that test red as soon as any
  test in the same session imports this module — including this bot's own tests.

To wire the bot, the orchestrator adds ``@BotRegistry.register("gas")`` to
:class:`GasBot`, imports it from ``src/bots/__init__.py``, and updates that
registry assertion in the same commit.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from src.bots.base import Bot
from src.bots.mixins import SignalProcessorMixin
from src.core.interfaces import TradeSignal
from src.models.gas_projection import (
    GasDataUnavailable,
    GasSeries,
)
from src.strategies.gas_convergence import GasConvergenceStrategy
from src.utils.logger import logger

# PRD Phase 4 EC-2: paper trading is gated on a backtest artifact showing EV > 0
# after maker fees. No artifact, no verdict, no trading. Feeds still run.
GAS_TRADING_ENABLED = False

#: Series harvested every tick. ``KXAAAGASM`` (monthly) is the Phase 4 target;
#: ``KXAAAGASW`` (weekly) is harvested for the same settlement source so the
#: week-end projection FR-4.2 mentions has recorded ladders to be evaluated
#: against later. Both are priced on their own fee schedule because every fee
#: call threads the market symbol.
GAS_SERIES: Tuple[str, ...] = ("KXAAAGASM", "KXAAAGASW")

#: Contract §1 location of the persisted truth series (owned by WS-A).
GAS_TRUTH_DIR = Path(__file__).resolve().parents[2] / "data" / "gas_truth"


class GasBot(Bot, SignalProcessorMixin):
    """Feed-only AAA gas convergence bot.

    ``TickerResolverMixin`` is deliberately not inherited: the whole ladder is
    fetched in one list call, so there is no "smart ticker" to resolve and no
    per-market quote call to make.
    """

    SERIES: Tuple[str, ...] = GAS_SERIES

    # Orderbook depth snapshots are HOURLY only; the per-tick path must never
    # issue per-market orderbook calls (the FR-0.7 harvester cadence rule).
    DEPTH_SNAPSHOT_INTERVAL_S = 3600
    MAX_DEPTH_MARKETS_PER_SERIES = 40

    #: The AAA series is appended to once a day, so re-reading it every tick is
    #: pure I/O. Reload hourly.
    SERIES_RELOAD_INTERVAL_S = 3600

    def __init__(self, truth_dir=None):
        Bot.__init__(self, name="Gas")
        self.kalshi = None
        self.truth_dir = Path(truth_dir) if truth_dir else GAS_TRUTH_DIR
        self._last_depth_snapshot = 0.0
        self._series_loaded_at = 0.0
        self._series: Optional[GasSeries] = None
        self._series_error: Optional[str] = None
        self.strategies = {"gas_convergence": GasConvergenceStrategy()}

    # -- lifecycle -------------------------------------------------------

    def setup(self, kalshi, coinbase=None, nws=None, **kwargs):
        self.kalshi = kalshi
        self._reload_series(force=True)
        logger.info(
            "[Gas] setup complete: series=%s trading=%s truth_dir=%s",
            ",".join(self.SERIES),
            "ENABLED" if GAS_TRADING_ENABLED else "FEED-ONLY",
            self.truth_dir,
        )

    def get_symbols(self) -> List[str]:
        return list(self.SERIES)

    # -- truth series ----------------------------------------------------

    def _reload_series(self, force: bool = False) -> Optional[GasSeries]:
        """Load ``data/gas_truth/`` if due. Absence is logged, never defaulted.

        A missing or unusable truth series leaves ``self._series`` at ``None``,
        which makes the strategy reject every market with
        ``GAS_SERIES_UNAVAILABLE`` — visible at INFO — rather than pricing off a
        fabricated history.
        """
        now = time.time()
        if not force and (now - self._series_loaded_at) < self.SERIES_RELOAD_INTERVAL_S:
            return self._series
        self._series_loaded_at = now
        try:
            self._series = GasSeries.from_csv_dir(self.truth_dir)
            self._series_error = None
            newest = max((o.date for o in self._series.aaa), default=None)
            logger.info(
                "[Gas] truth series loaded: aaa=%d rows (newest %s) rbob=%d eia=%d",
                len(self._series.aaa),
                newest.isoformat() if isinstance(newest, date) else "none",
                len(self._series.rbob),
                len(self._series.eia_weekly),
            )
        except GasDataUnavailable as exc:
            self._series = None
            self._series_error = str(exc)
            logger.warning(
                "[Gas] truth series unavailable: %s. Feeds continue; every "
                "signal will be rejected GAS_SERIES_UNAVAILABLE.",
                exc,
            )
        except OSError as exc:
            self._series = None
            self._series_error = str(exc)
            logger.error("[Gas] truth series read failed: %s", exc)
        self.strategies["gas_convergence"].set_series(self._series)
        return self._series

    # -- tick ------------------------------------------------------------

    def tick(self, risk_manager, dashboard) -> List[TradeSignal]:
        if not self.kalshi:
            return []

        self._reload_series()

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
                logger.error("[Gas] Market Fetch Fail (%s): %s", series, exc)
                continue

            if not ladder:
                logger.info("[Gas] %s: no active markets returned", series)
                continue

            for market in ladder:
                self._record(market, dashboard)

            # Valuation mark for the most heavily quoted bracket, so open
            # positions are not marked off a stale print.
            for market in ladder:
                price = self._best_price(market)
                if price is not None:
                    risk_manager.update_market_data(market.symbol, price)
                    risk_manager.exchange.update_market_price(market.symbol, price)

            if depth_due:
                self._snapshot_depth(ladder, dashboard)

            if GAS_TRADING_ENABLED:
                strategy = self.strategies["gas_convergence"]
                for market in ladder:
                    self._process_signals(
                        strategy.analyze(market),
                        strategy_name=strategy.name(),
                        risk_manager=risk_manager,
                        dashboard=dashboard,
                    )
            else:
                logger.info(
                    "[Gas] %s FEED-ONLY: %d markets recorded, 0 signals emitted "
                    "(GAS_TRADING_ENABLED=False pending the Phase 4 EC-2 "
                    "backtest verdict)",
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
                    "[Gas] Depth snapshot failed for %s: %s", market.symbol, exc
                )
            time.sleep(0.15)
