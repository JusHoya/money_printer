from dataclasses import dataclass
from typing import Optional
from datetime import datetime, date, timedelta

from src.core.matching_engine import SimulatedExchange
from src.core.fee_calculator import ev_after_fees, compute_fee  # noqa: F401
from src.utils.logger import logger


@dataclass
class PortfolioState:
    balance: float
    daily_pnl: float
    active_positions: int
    last_trade_time: datetime


# Bankroll growth stages from PRD Section 10
BANKROLL_STAGES = [
    # (min_balance, max_balance, max_trade_pct, max_positions, kelly_frac, label)
    (0, 500, 0.10, 5, 0.25, "Seed"),
    (500, 2_000, 0.05, 8, 0.25, "Early"),
    (2_000, 10_000, 0.05, 12, 0.30, "Growth"),
    (10_000, 50_000, 0.05, 15, 0.35, "Scale"),
    (50_000, float("inf"), 0.025, 20, 0.25, "Compound"),
]


def _get_bankroll_stage(balance: float) -> tuple:
    """Return the matching bankroll stage parameters."""
    for lo, hi, trade_pct, max_pos, kelly, label in BANKROLL_STAGES:
        if lo <= balance < hi:
            return trade_pct, max_pos, kelly, label
    return 0.025, 20, 0.25, "Compound"


class RiskManager:
    """
    Enforces capital preservation rules and tracks Simulated PnL via OMS.

    Sprint 4 enhancements:
    - Fee-aware EV check (reject if EV < 0 after fees)
    - Bankroll-stage-aware sizing (PRD Section 10)
    - Quarter-Kelly (0.25x) with bankroll-dependent caps
    - Correlation limit (max 2 same-direction BTC contracts)
    - Circuit breaker integration
    """

    def __init__(self, starting_balance: float = 100.0):
        self.balance = starting_balance
        self.starting_balance_day = starting_balance
        # Pass callback to OMS
        self.exchange = SimulatedExchange(on_close=self._on_trade_close)

        self.daily_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.active_positions = 0

        self.last_trade_time = datetime.min
        self.today = date.today()

        self.strategy_pnl = {}

        # RULES (base — overridden by bankroll stage)
        self.MAX_RISK_PER_TRADE_PCT = 0.05
        self.MAX_DAILY_DRAWDOWN_PCT = 0.50
        self.MAX_STRATEGY_DRAWDOWN_PCT = 0.10
        self.MAX_PORTFOLIO_EXPOSURE_PCT = 0.50
        self.MIN_TRADE_INTERVAL_SEC = 10
        self.LOSS_COOLDOWN_SEC = 60
        self.loss_cooldown = {}

        # Sprint 4: correlation limit
        self.MAX_SAME_DIRECTION_BTC = 2

        # Sprint 4: optional circuit breaker (set by orchestrator)
        self.circuit_breaker = None

    def _on_trade_close(self, position: dict):
        """Callback from OMS when a trade is settled/closed."""
        # Sync daily_pnl from exchange (source of truth) BEFORE recalculating balance
        stats = self.exchange.get_stats()
        self.daily_pnl = stats["realized"]
        self._sync_balance()
        pnl = position.get("pnl", 0.0)
        strategy_name = position.get("strategy_name", "Unknown")

        self.strategy_pnl[strategy_name] = (
            self.strategy_pnl.get(strategy_name, 0.0) + pnl
        )

        logger.info(
            f"[Risk] 💰 SETTLEMENT: Profit ${pnl:+.2f} -> Balance: ${self.balance:.2f} | Strategy: {strategy_name}"
        )

        # Per-symbol loss cooldown to prevent re-entry after stop-loss
        if pnl < 0:
            symbol = position.get("symbol", "")
            # Extract series prefix (e.g. KXBTC15M from KXBTC15M-26FEB151330-30)
            prefix = symbol.split("-")[0] if "-" in symbol else symbol
            cooldown_until = datetime.now() + timedelta(seconds=self.LOSS_COOLDOWN_SEC)
            self.loss_cooldown[prefix] = cooldown_until
            logger.info(
                f"[Risk] ⚠️ Loss Cooldown: {prefix} locked until {cooldown_until.strftime('%H:%M:%S')}"
            )

    def _sync_balance(self):
        """
        Calculates available cash balance based on realized PnL and current exposure.
        Formula: Starting Cash + Realized PnL - Cash tied up in open positions.
        """
        exposure = self.get_current_exposure()
        self.balance = self.starting_balance_day + self.daily_pnl - exposure

    def update_balance(self, real_balance: float):
        """Syncs simulated balance with real exchange balance."""
        # Detect if this is the initial sync
        if (
            self.starting_balance_day == 100.0
            and self.balance == 100.0
            and real_balance != 100.0
        ):
            logger.info(
                f"[Risk] [SYNC] Initial Sync: Balance updated to ${real_balance:.2f}"
            )

        self.starting_balance_day = real_balance

        # CRITICAL: Reset internal PnL counters to prevent double counting
        # The 'real_balance' already includes all past PnL.
        self.daily_pnl = 0.0
        self.strategy_pnl = {}
        self.exchange.reset_stats()

        self._sync_balance()

    def update_market_data(self, symbol: str, price: float):
        """Passes live data to OMS to update PnL."""
        self.exchange.update_market(symbol, price)
        stats = self.exchange.get_stats()
        self.daily_pnl = stats["realized"]
        self.unrealized_pnl = stats["unrealized"]

        self._sync_balance()

    def _reset_daily_stats_if_needed(self):
        if date.today() > self.today:
            self.today = date.today()
            self.daily_pnl = 0.0
            self.strategy_pnl = {}
            # Reset exchange realized PnL for the new day?
            # In simulation, we usually keep cumulative, but for 'Daily' reporting:
            # Let's just update the baseline.
            self.starting_balance_day = self.balance
            logger.info("[RiskManager] [NEW DAY] Daily PnL reset.")

    def calculate_kelly_size(self, confidence: float, price: float) -> int:
        """Position sizing using Quarter-Kelly with bankroll-stage awareness.

        Formula: f* = p - q/b, then apply stage-specific Kelly fraction
        and hard caps that scale with portfolio value.

        Bankroll stages (from PRD Section 10):
        - Seed ($0-500): 10% max trade, 0.25x Kelly
        - Early ($500-2k): 5% max trade, 0.25x Kelly
        - Growth ($2k-10k): 5% max trade, 0.30x Kelly
        - Scale ($10k-50k): 5% max trade, 0.35x Kelly
        - Compound ($50k+): 2.5% max trade, 0.25x Kelly
        """
        if price <= 0 or price >= 1.0:
            return 0

        # Bankroll stage determines Kelly fraction and trade cap
        trade_pct, max_positions, kelly_frac, stage = _get_bankroll_stage(self.balance)

        # 1. Odds
        b = (1.0 - price) / price

        # 2. Raw Kelly
        p = confidence
        q = 1.0 - p
        f = p - (q / b)

        # 3. Fractional Kelly (stage-dependent)
        f_fractional = f * kelly_frac
        if f_fractional <= 0:
            return 0

        # 4. Hard cap (stage-dependent trade %)
        f_capped = min(f_fractional, trade_pct)

        # 5. Dollar allocation
        allocation = self.balance * f_capped

        # 6. Convert to contracts
        quantity = int(allocation / price)

        # Cap short exposure on cheap contracts: max $10 at (1-price)*qty
        if price < 0.15:
            max_short_qty = int(10.0 / (1.0 - price))
            quantity = min(quantity, max_short_qty)

        return max(1, min(quantity, 500))

    def get_current_exposure(self, category: Optional[str] = None) -> float:
        """
        Sums the cost of active positions.
        If category is provided, filters by symbol heuristics.
        """
        total = 0.0
        for p in self.exchange.positions:
            # Simple Heuristic for Categorization
            sym = p["symbol"].upper()
            is_crypto = "BTC" in sym or "ETH" in sym
            is_weather = "HIGH" in sym or "PRECIP" in sym or "TEMP" in sym

            match = False
            if category == "crypto" and is_crypto:
                match = True
            elif category == "weather" and is_weather:
                match = True
            elif category is None:
                match = True

            if match:
                total += p["entry_price"] * p["quantity"]
        return total

    def check_order(
        self,
        proposed_cost: float,
        category: str = "general",
        strategy_name: str = None,
        expiration_time=None,
    ) -> bool:
        """Returns True if the order is safe to execute.

        Sprint 4 additions: circuit breaker integration, correlation
        limit, bankroll-stage max positions.
        """
        self._reset_daily_stats_if_needed()

        # 0. Circuit breaker (Sprint 4)
        if self.circuit_breaker is not None:
            market_type = ""
            if category == "crypto":
                market_type = "btc"
            elif category == "weather":
                market_type = "weather"
            if not self.circuit_breaker.can_trade(
                strategy_name=strategy_name or "",
                market_type=market_type,
                daily_pnl=self.daily_pnl,
                bankroll=self.starting_balance_day,
            ):
                logger.warning("[Risk] [REJECT] Circuit breaker tripped")
                return False

        # 0.5. Final Minute Freeze
        if expiration_time:
            expiry_dt = expiration_time
            if isinstance(expiration_time, str):
                try:
                    expiry_dt = datetime.fromisoformat(
                        expiration_time.replace("Z", "+00:00")
                    )
                except Exception:
                    pass
            if isinstance(expiry_dt, datetime):
                now = (
                    datetime.now()
                    if expiry_dt.tzinfo is None
                    else datetime.now().astimezone()
                )
                time_to_expiry_sec = (expiry_dt - now).total_seconds()
                if 0 < time_to_expiry_sec <= 60:
                    logger.warning(
                        "[Risk] [REJECT] FINAL MINUTE FREEZE: %.1fs until expiry.",
                        time_to_expiry_sec,
                    )
                    return False

        # 1. Capital Check
        if proposed_cost > self.balance:
            logger.warning(
                "[Risk] [REJECT] Insufficient Funds ($%.2f < $%.2f)",
                self.balance,
                proposed_cost,
            )
            return False

        # 2. Position Sizing (bankroll-stage-aware)
        trade_pct, max_positions, _, stage = _get_bankroll_stage(self.balance)
        max_trade_size = self.balance * trade_pct
        if proposed_cost > max_trade_size + 1.0:
            logger.warning(
                "[Risk] [REJECT] Position too large for %s stage ($%.2f > $%.2f)",
                stage,
                proposed_cost,
                max_trade_size,
            )
            return False

        # 2.5 Max positions (bankroll-stage-aware)
        if len(self.exchange.positions) >= max_positions:
            logger.warning(
                "[Risk] [REJECT] Max positions for %s stage (%d/%d)",
                stage,
                len(self.exchange.positions),
                max_positions,
            )
            return False

        # 3. Drawdown Limit (Kill Switch)
        if self.daily_pnl < -(self.starting_balance_day * self.MAX_DAILY_DRAWDOWN_PCT):
            logger.warning(
                "[Risk] [KILL] KILL SWITCH: Daily Drawdown Limit Hit ($%.2f)",
                self.daily_pnl,
            )
            return False

        # 3.5 Strategy Drawdown Limit
        if strategy_name:
            strat_pnl = self.strategy_pnl.get(strategy_name, 0.0)
            if strat_pnl < -(
                self.starting_balance_day * self.MAX_STRATEGY_DRAWDOWN_PCT
            ):
                logger.warning(
                    "[Risk] [REJECT] STRATEGY DRAWDOWN: %s ($%.2f PnL)",
                    strategy_name,
                    strat_pnl,
                )
                return False

        # 4. Dynamic Exposure Limit
        current_total_exposure = self.get_current_exposure()
        max_exposure = self.balance * self.MAX_PORTFOLIO_EXPOSURE_PCT
        if (current_total_exposure + proposed_cost) > max_exposure:
            logger.warning(
                "[Risk] [REJECT] Max Portfolio Exposure (%.2f/%.2f)",
                current_total_exposure,
                max_exposure,
            )
            return False

        # 4.5 BTC correlation limit (Sprint 4): max N same-direction BTC contracts
        if category == "crypto":
            buy_count = sum(
                1
                for p in self.exchange.positions
                if ("BTC" in p["symbol"].upper() or "kxbtcd" in p["symbol"])
                and p["side"] == "buy"
            )
            sell_count = sum(
                1
                for p in self.exchange.positions
                if ("BTC" in p["symbol"].upper() or "kxbtcd" in p["symbol"])
                and p["side"] == "sell"
            )
            if max(buy_count, sell_count) >= self.MAX_SAME_DIRECTION_BTC:
                logger.warning(
                    "[Risk] [REJECT] BTC correlation limit (buy=%d sell=%d max=%d)",
                    buy_count,
                    sell_count,
                    self.MAX_SAME_DIRECTION_BTC,
                )
                return False

        # 5. Weather bucket (30%)
        if category == "weather":
            max_weather = self.balance * 0.30
            current_weather = self.get_current_exposure(category="weather")
            if (current_weather + proposed_cost) > max_weather:
                logger.warning(
                    "[Risk] [REJECT] Max Weather Allocation (%.2f/%.2f)",
                    current_weather,
                    max_weather,
                )
                return False

        # 6. Rate Limiting
        seconds_since_last = (datetime.now() - self.last_trade_time).total_seconds()
        if seconds_since_last < self.MIN_TRADE_INTERVAL_SEC:
            logger.info(
                "[Risk] [WAIT] Rate Limit (%.1fs < %ds)",
                seconds_since_last,
                self.MIN_TRADE_INTERVAL_SEC,
            )
            return False

        # 7. Per-Symbol Loss Cooldown (clean expired)
        now = datetime.now()
        expired = [k for k, v in self.loss_cooldown.items() if now >= v]
        for k in expired:
            del self.loss_cooldown[k]

        return True

    def record_execution(
        self,
        cost: float,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        stop_loss: float = 0.0,
        trailing_rules: dict = None,
        expiration_time: any = None,
        strategy_name: str = None,
        contract_side: str = "YES",
        disable_profit_targets: bool = False,
        strike: float = None,
    ):
        """Call this AFTER a trade is executed."""
        # OMS HANDOFF
        # Use exact quantity and price from the signal
        self.exchange.open_position(
            symbol,
            side,
            price,
            quantity,
            stop_loss=stop_loss,
            trailing_rules=trailing_rules,
            expiration_time=expiration_time,
            strategy_name=strategy_name,
            contract_side=contract_side,
            disable_profit_targets=disable_profit_targets,
            strike=strike,
        )

        self._sync_balance()
        self.last_trade_time = datetime.now()
        self.active_positions = len(self.exchange.positions)
        logger.info(f"[Risk] [OK] Trade Recorded. New Balance: ${self.balance:.2f}")

    def record_pnl(self, pnl: float):
        """Manual PnL injection (not typically used with OMS)."""
        # If we use this, we'd need to update realized pnl in exchange or baseline
        self.starting_balance_day += pnl
        self._sync_balance()
