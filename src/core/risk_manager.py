import json
import os
from collections import deque
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, date, timedelta, timezone

from src.core.matching_engine import SimulatedExchange, _DEFAULT_STATE_FILE
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

FEE_TOLERANCE = 0.02

WIN_RATES_PATH = os.path.join("data", "strategy_win_rates.json")

# FR-0.6: recency-windowed win-rate tracking. Only the last WIN_RATE_WINDOW
# closed-trade outcomes per strategy are retained, so a poisoned era can no
# longer zero-size signals forever (the "ML BTC 15m": [30, 1048] deadlock).
WIN_RATE_WINDOW = 50
# Win-rate influences Kelly sizing only once the window holds this many samples.
MIN_WIN_SAMPLES = 20


class RejectReason:
    """FR-0.4 stable reason codes for risk rejections and zero-sizing.

    Every rejection emitted by the RiskManager logs one of these codes at
    INFO via :func:`log_rejection`. ``EV_GATE`` is defined here so the EV
    gate (which lives with signal processing, not in this class) shares the
    same stable vocabulary and log format.
    """

    KELLY_ZERO = "KELLY_ZERO"
    EV_GATE = "EV_GATE"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    DAILY_DRAWDOWN = "DAILY_DRAWDOWN"
    STRATEGY_DRAWDOWN = "STRATEGY_DRAWDOWN"
    COOLDOWN_SYMBOL = "COOLDOWN_SYMBOL"
    COOLDOWN_STRATEGY = "COOLDOWN_STRATEGY"
    TRADE_INTERVAL = "TRADE_INTERVAL"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    DAILY_TRADE_CAP = "DAILY_TRADE_CAP"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    FINAL_MINUTE_FREEZE = "FINAL_MINUTE_FREEZE"
    POSITION_TOO_LARGE = "POSITION_TOO_LARGE"
    MAX_POSITIONS = "MAX_POSITIONS"
    CORRELATION_LIMIT = "CORRELATION_LIMIT"
    WEATHER_ALLOCATION = "WEATHER_ALLOCATION"


def log_rejection(reason: str, strategy: str = "", symbol: str = "", **context):
    """Emit the FR-0.4 rejection line at INFO with a stable, greppable shape.

    Format::

        [Risk] REJECT strategy=<name> symbol=<ticker> reason=<CODE> k=v ...

    ``None`` context values are dropped; floats are compacted (0.3700 -> 0.37).
    Public so signal-processing code (EV gate, sizing callers) can reuse the
    exact same format and vocabulary.
    """
    parts = [
        f"[Risk] REJECT strategy={strategy or '-'}",
        f"symbol={symbol or '-'}",
        f"reason={reason}",
    ]
    for key, value in context.items():
        if value is None:
            continue
        if isinstance(value, float):
            text = f"{value:.4f}".rstrip("0").rstrip(".")
            if text in ("", "-"):
                text = "0"
        else:
            text = str(value)
        parts.append(f"{key}={text}")
    logger.info(" ".join(parts))


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

    def __init__(self, starting_balance: float = 100.0, persist_state: bool = False):
        self.balance = starting_balance
        self.starting_balance_day = starting_balance
        # Pass callback to OMS. Production (run_dashboard.py) passes persist_state=True
        # so positions survive VM restarts; tests/backtests leave it False to stay in-memory.
        self._persist_state = persist_state
        self.exchange = SimulatedExchange(
            on_close=self._on_trade_close,
            state_file=_DEFAULT_STATE_FILE if persist_state else None,
        )

        self.daily_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.active_positions = 0

        self.last_trade_time = datetime.min
        self.today = date.today()
        # Baseline for converting cumulative exchange.realized_pnl into
        # today-only daily_pnl. Reset at each UTC day boundary.
        self._realized_at_day_start = self.exchange.realized_pnl
        self.drawdown_kill_triggered = False

        self.strategy_pnl = {}
        # High-water mark per strategy for drawdown gating
        self.strategy_peak_pnl: dict = {}

        # RULES (base — overridden by bankroll stage)
        self.MAX_RISK_PER_TRADE_PCT = 0.05
        self.MAX_DAILY_DRAWDOWN_PCT = 0.50
        self.MAX_STRATEGY_DRAWDOWN_PCT = 0.10
        self.MAX_PORTFOLIO_EXPOSURE_PCT = 0.50
        self.MIN_TRADE_INTERVAL_SEC = 10
        self.LOSS_COOLDOWN_SEC = 900
        self.loss_cooldown = {}

        # Sprint 6: daily trade cap to force selectivity
        self.MAX_DAILY_TRADES = 40
        self.daily_trade_count = 0

        # Sprint 4: correlation limit
        self.MAX_SAME_DIRECTION_BTC = 2

        # Sprint 4: optional circuit breaker (set by orchestrator)
        self.circuit_breaker = None

        # FR-0.6: recency-windowed win records for calibrated Kelly sizing.
        # strategy_name -> deque of 1/0 outcomes (maxlen=WIN_RATE_WINDOW).
        # Replaces the Sprint 6 cumulative-forever (wins, total) counters.
        self.strategy_win_records: dict = {}
        self._win_rate_updated: dict = {}  # strategy_name -> iso timestamp
        self._load_win_rates()

        # Sprint 6: escalating cooldown after consecutive losses
        self.consecutive_losses: dict = {}  # strategy_name -> count
        self.ESCALATING_COOLDOWNS = [60, 180, 600]  # 1min, 3min, 10min
        self.strategy_cooldown: dict = {}  # strategy_name -> cooldown_until

    def _on_trade_close(self, position: dict):
        """Callback from OMS when a trade is settled/closed."""
        # Detect day rollover before recomputing daily_pnl so a stale baseline
        # doesn't leak yesterday's cumulative loss into today.
        self._reset_daily_stats_if_needed()
        # daily_pnl is today-only: cumulative exchange PnL minus the value
        # captured at this UTC day's start.
        stats = self.exchange.get_stats()
        self.daily_pnl = stats["realized"] - self._realized_at_day_start
        self._sync_balance()
        pnl = position.get("pnl", 0.0)
        strategy_name = position.get("strategy_name", "Unknown")

        self.strategy_pnl[strategy_name] = (
            self.strategy_pnl.get(strategy_name, 0.0) + pnl
        )
        # Track per-strategy peak P&L for allocation-aware drawdown gating
        current = self.strategy_pnl[strategy_name]
        prev_peak = self.strategy_peak_pnl.get(strategy_name, 0.0)
        if current > prev_peak:
            self.strategy_peak_pnl[strategy_name] = current

        logger.info(
            f"[Risk] 💰 SETTLEMENT: Profit ${pnl:+.2f} -> Balance: ${self.balance:.2f} | Strategy: {strategy_name}"
        )

        # FR-0.6: recency-windowed win record (last WIN_RATE_WINDOW closed
        # trades per strategy). The deque evicts the oldest outcome once
        # full, so no single poisoned era can dominate sizing forever.
        record = self.strategy_win_records.get(strategy_name)
        if record is None:
            record = deque(maxlen=WIN_RATE_WINDOW)
            self.strategy_win_records[strategy_name] = record
        record.append(1 if pnl > -FEE_TOLERANCE else 0)
        self._win_rate_updated[strategy_name] = datetime.now(timezone.utc).isoformat()

        # Sprint 6: Escalating cooldown after consecutive losses
        if pnl > -FEE_TOLERANCE:
            self.consecutive_losses[strategy_name] = 0
        else:
            streak = self.consecutive_losses.get(strategy_name, 0) + 1
            self.consecutive_losses[strategy_name] = streak
            idx = min(streak - 1, len(self.ESCALATING_COOLDOWNS) - 1)
            cooldown_secs = self.ESCALATING_COOLDOWNS[idx]
            cooldown_until = datetime.now() + timedelta(seconds=cooldown_secs)
            self.strategy_cooldown[strategy_name] = cooldown_until
            logger.info(
                f"[Risk] ⚠️ Strategy Cooldown: {strategy_name} streak={streak} "
                f"locked {cooldown_secs}s until {cooldown_until.strftime('%H:%M:%S')}"
            )

        # Per-symbol loss cooldown to prevent re-entry on same market
        if pnl < -FEE_TOLERANCE:
            symbol = position.get("symbol", "")
            # Ban exact ticker (not series prefix) to prevent re-entry on same market
            # while still allowing trades on other markets in the same series
            cooldown_until = datetime.now() + timedelta(seconds=self.LOSS_COOLDOWN_SEC)
            self.loss_cooldown[symbol] = cooldown_until
            logger.info(
                f"[Risk] ⚠️ Loss Cooldown: {symbol} locked until {cooldown_until.strftime('%H:%M:%S')}"
            )

        # Persist win rates after every close — debouncing meant short
        # runs that crashed before reaching the 10-close threshold lost
        # all their data, leaving strategy_win_rates.json empty. Gated
        # on persist_state so unit tests don't pollute the prod file.
        if self._persist_state:
            self._save_win_rates()

    def _load_win_rates(self):
        """Load persisted per-strategy win windows from disk if available.

        FR-0.6 format: ``{"Strategy": {"window": [1, 0, ...], "updated": iso}}``.
        Legacy-format entries (``[wins, total]`` cumulative counters) are
        deliberately IGNORED — the pivot resets win-rate history, so a legacy
        entry is treated as absent (neutral 0.5 prior in Kelly) rather than
        migrated. The load never crashes on an old, mixed, or malformed file.
        """
        self.strategy_win_records = {}
        self._win_rate_updated = {}
        try:
            if not os.path.exists(WIN_RATES_PATH):
                return
            with open(WIN_RATES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("[Risk] Win-rate file is not a JSON object; ignoring it")
                return
            ignored_legacy = 0
            for name, value in data.items():
                if isinstance(value, dict) and isinstance(value.get("window"), list):
                    window = [1 if x else 0 for x in value["window"]]
                    window = window[-WIN_RATE_WINDOW:]
                    self.strategy_win_records[name] = deque(
                        window, maxlen=WIN_RATE_WINDOW
                    )
                    if value.get("updated"):
                        self._win_rate_updated[name] = value["updated"]
                else:
                    # Legacy [wins, total] (or unknown shape): pivot reset.
                    ignored_legacy += 1
            if ignored_legacy:
                logger.info(
                    "[Risk] Ignored %d legacy-format win-rate entries "
                    "(deliberate pivot reset; scripts/purge_stale_state.py "
                    "archives them)",
                    ignored_legacy,
                )
            if self.strategy_win_records:
                logger.info(
                    "[Risk] Loaded win windows for %d strategies",
                    len(self.strategy_win_records),
                )
        except Exception as e:
            logger.warning(f"[Risk] Could not load win rates: {e}")
            self.strategy_win_records = {}
            self._win_rate_updated = {}

    def _save_win_rates(self):
        """Persist per-strategy win windows to disk (FR-0.6 format)."""
        try:
            os.makedirs(os.path.dirname(WIN_RATES_PATH) or ".", exist_ok=True)
            payload = {
                name: {
                    "window": [int(x) for x in record],
                    "updated": self._win_rate_updated.get(name)
                    or datetime.now(timezone.utc).isoformat(),
                }
                for name, record in self.strategy_win_records.items()
            }
            with open(WIN_RATES_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.warning(f"[Risk] Could not save win rates: {e}")

    def _sync_balance(self):
        """
        Calculates available cash balance based on realized PnL and current exposure.
        Formula: Starting Cash + Realized PnL - Cash tied up in open positions.
        """
        exposure = self.get_current_exposure()
        self.balance = (
            self.starting_balance_day
            + self.daily_pnl
            + self.exchange.unrealized_pnl
            - exposure
        )

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
        self._realized_at_day_start = self.exchange.realized_pnl

        self._sync_balance()

    def update_market_data(self, symbol: str, price: float):
        """Passes live data to OMS to update PnL."""
        self._reset_daily_stats_if_needed()
        self.exchange.update_market(symbol, price)
        stats = self.exchange.get_stats()
        self.daily_pnl = stats["realized"] - self._realized_at_day_start
        self.unrealized_pnl = stats["unrealized"]

        self._sync_balance()

    def _reset_daily_stats_if_needed(self):
        if date.today() > self.today:
            self.today = date.today()
            self.daily_pnl = 0.0
            self.strategy_pnl = {}
            self.daily_trade_count = 0
            # Capture today's baseline so daily_pnl = exchange.realized_pnl
            # minus this snapshot. Without this, daily_pnl reads cumulative
            # PnL since process start, tripping daily-drawdown gates on
            # all-time loss.
            self._realized_at_day_start = self.exchange.realized_pnl
            self.starting_balance_day = self.balance
            self.drawdown_kill_triggered = False
            logger.info("[RiskManager] [NEW DAY] Daily PnL reset.")

    def calculate_kelly_size(
        self,
        confidence: float,
        price: float,
        strategy_name: str = "",
        symbol: str = "",
    ) -> int:
        """Position sizing using Quarter-Kelly with bankroll-stage awareness.

        Sprint 6: Uses historical win rate (not raw model confidence) to
        prevent oversizing. Research (Bertsimas & Mundru 2024): when model
        confidence is 0.95 but actual WR is 0.45, full-Kelly sizing is
        catastrophic. Blend historical WR with dampened confidence.

        FR-0.6: the historical win rate comes from the recency window (last
        WIN_RATE_WINDOW closed trades); with fewer than MIN_WIN_SAMPLES in
        the window the neutral 0.5 prior is used, so a reset strategy sizes
        like an unknown one instead of inheriting a poisoned history.

        FR-0.4: every zero return is logged at INFO with reason=KELLY_ZERO —
        this was the silent kill-switch (skips were DEBUG-only downstream).
        """
        if price <= 0 or price >= 1.0:
            log_rejection(
                RejectReason.KELLY_ZERO,
                strategy_name,
                symbol,
                detail="PRICE_OUT_OF_RANGE",
                price=price,
                confidence=confidence,
            )
            return 0

        # Bankroll stage determines Kelly fraction and trade cap
        trade_pct, max_positions, kelly_frac, stage = _get_bankroll_stage(self.balance)

        # 1. Fee-adjusted odds — Kalshi charges maker fees on both entry and
        # settlement, so effective payoff shrinks and effective loss grows.
        # Using raw b = (1-price)/price systematically oversizes thin edges.
        fee_per = compute_fee(price, 1, is_maker=True).per_contract
        net_win = (1.0 - price) - 2.0 * fee_per
        net_loss = price + 2.0 * fee_per
        if net_win <= 0:
            log_rejection(
                RejectReason.KELLY_ZERO,
                strategy_name,
                symbol,
                detail="FEES_EXCEED_PAYOFF",
                price=price,
                fee_per=fee_per,
            )
            return 0
        b = net_win / net_loss

        # 2. Calibrated probability using historical win rate
        # Blend: 60% historical WR + 40% dampened confidence
        historical_wr = 0.50  # Neutral prior until the window has data
        n_samples = 0
        if strategy_name:
            record = self.strategy_win_records.get(strategy_name)
            if record:
                n_samples = len(record)
                if n_samples >= MIN_WIN_SAMPLES:  # Need sufficient sample
                    historical_wr = sum(record) / n_samples

        p = 0.6 * historical_wr + 0.4 * confidence
        q = 1.0 - p
        f = p - (q / b)

        # 3. Fractional Kelly (stage-dependent)
        f_fractional = f * kelly_frac

        if f_fractional <= 0:
            log_rejection(
                RejectReason.KELLY_ZERO,
                strategy_name,
                symbol,
                p=p,
                price=price,
                b=b,
                f=f,
                win_rate=historical_wr,
                samples=n_samples,
                confidence=confidence,
            )
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

        # Hard cap at 75 contracts. Raised from 50 after disabling bleeder
        # strategies (LongshotFaderV2, CryptoHourlyV3) — remaining strategies
        # are either ML-gated or pure arb and can safely scale.
        return max(1, min(quantity, 75))

    def get_current_exposure(self, category: Optional[str] = None) -> float:
        """
        Sums the collateral cost of active positions.
        Short YES (sell YES) locks up (1-price)*qty, not price*qty.
        """
        total = 0.0
        for p in self.exchange.positions:
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
                if p.get("side") == "sell" and p.get("contract_side", "YES") == "YES":
                    total += (1.0 - p["entry_price"]) * p["quantity"]
                else:
                    total += p["entry_price"] * p["quantity"]
        return total

    def check_order(
        self,
        proposed_cost: float,
        category: str = "general",
        strategy_name: str = None,
        expiration_time=None,
        symbol: str = "",
        side: str = "",
        price: float = None,
        quantity: int = None,
    ) -> bool:
        """Returns True if the order is safe to execute.

        Sprint 4 additions: circuit breaker integration, correlation
        limit, bankroll-stage max positions.
        Sprint 6 additions: daily trade cap, exact-ticker cooldown,
        escalating strategy cooldown, entry price filter.
        FR-0.4: every rejection logs at INFO via log_rejection with a
        stable reason code. ``side``/``price``/``quantity`` are optional
        signal context passed through to the log line when provided.
        """
        self._reset_daily_stats_if_needed()
        strat = strategy_name or ""
        sig_ctx = {"side": side or None, "price": price, "quantity": quantity}

        # Daily trade cap
        if self.daily_trade_count >= self.MAX_DAILY_TRADES:
            log_rejection(
                RejectReason.DAILY_TRADE_CAP,
                strat,
                symbol,
                count=self.daily_trade_count,
                cap=self.MAX_DAILY_TRADES,
                **sig_ctx,
            )
            return False

        # Sprint 6: Escalating strategy cooldown
        if strategy_name and strategy_name in self.strategy_cooldown:
            now = datetime.now()
            if now < self.strategy_cooldown[strategy_name]:
                remaining = (
                    self.strategy_cooldown[strategy_name] - now
                ).total_seconds()
                log_rejection(
                    RejectReason.COOLDOWN_STRATEGY,
                    strat,
                    symbol,
                    remaining_s=round(remaining),
                    **sig_ctx,
                )
                return False
            else:
                del self.strategy_cooldown[strategy_name]

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
                log_rejection(
                    RejectReason.CIRCUIT_BREAKER,
                    strat,
                    symbol,
                    daily_pnl=self.daily_pnl,
                    **sig_ctx,
                )
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
                    log_rejection(
                        RejectReason.FINAL_MINUTE_FREEZE,
                        strat,
                        symbol,
                        tte_s=round(time_to_expiry_sec, 1),
                        **sig_ctx,
                    )
                    return False

        # 1. Capital Check
        if proposed_cost > self.balance:
            log_rejection(
                RejectReason.INSUFFICIENT_BALANCE,
                strat,
                symbol,
                balance=self.balance,
                cost=proposed_cost,
                **sig_ctx,
            )
            return False

        # 2. Position Sizing (bankroll-stage-aware)
        trade_pct, max_positions, _, stage = _get_bankroll_stage(self.balance)
        max_trade_size = self.balance * trade_pct
        if proposed_cost > max_trade_size + 1.0:
            log_rejection(
                RejectReason.POSITION_TOO_LARGE,
                strat,
                symbol,
                stage=stage,
                cost=proposed_cost,
                max=max_trade_size,
                **sig_ctx,
            )
            return False

        # 2.5 Max positions (bankroll-stage-aware)
        if len(self.exchange.positions) >= max_positions:
            log_rejection(
                RejectReason.MAX_POSITIONS,
                strat,
                symbol,
                stage=stage,
                open=len(self.exchange.positions),
                max=max_positions,
                **sig_ctx,
            )
            return False

        # 3. Drawdown Limit (Kill Switch)
        if self.daily_pnl < -(self.starting_balance_day * self.MAX_DAILY_DRAWDOWN_PCT):
            self.drawdown_kill_triggered = True
            log_rejection(
                RejectReason.DAILY_DRAWDOWN,
                strat,
                symbol,
                daily_pnl=self.daily_pnl,
                limit=-(self.starting_balance_day * self.MAX_DAILY_DRAWDOWN_PCT),
                kill_switch=True,
                **sig_ctx,
            )
            return False

        # 3.5 Strategy Drawdown Limit (peak-relative)
        # Old code used starting_balance_day * 0.10 as the cap, which under-
        # punished losing strategies that had never made money and over-
        # punished winning strategies whose peak P&L exceeded that flat $10.
        # Now: drawdown = peak - current. Limit = 10% of the strategy's
        # capital base, where the base is starting_balance_day plus its
        # peak P&L (i.e. the largest pool it ever effectively controlled).
        if strategy_name:
            strat_pnl = self.strategy_pnl.get(strategy_name, 0.0)
            peak = self.strategy_peak_pnl.get(strategy_name, 0.0)
            capital_base = self.starting_balance_day + max(peak, 0.0)
            drawdown = peak - strat_pnl
            if drawdown > capital_base * self.MAX_STRATEGY_DRAWDOWN_PCT:
                log_rejection(
                    RejectReason.STRATEGY_DRAWDOWN,
                    strat,
                    symbol,
                    peak=peak,
                    pnl=strat_pnl,
                    dd=drawdown,
                    base=capital_base,
                    **sig_ctx,
                )
                return False

        # 4. Dynamic Exposure Limit
        current_total_exposure = self.get_current_exposure()
        max_exposure = self.balance * self.MAX_PORTFOLIO_EXPOSURE_PCT
        if (current_total_exposure + proposed_cost) > max_exposure:
            log_rejection(
                RejectReason.MAX_EXPOSURE,
                strat,
                symbol,
                exposure=current_total_exposure,
                cost=proposed_cost,
                max=max_exposure,
                **sig_ctx,
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
                log_rejection(
                    RejectReason.CORRELATION_LIMIT,
                    strat,
                    symbol,
                    buy=buy_count,
                    sell=sell_count,
                    max=self.MAX_SAME_DIRECTION_BTC,
                    **sig_ctx,
                )
                return False

        # 5. Weather bucket (30%)
        if category == "weather":
            max_weather = self.balance * 0.30
            current_weather = self.get_current_exposure(category="weather")
            if (current_weather + proposed_cost) > max_weather:
                log_rejection(
                    RejectReason.WEATHER_ALLOCATION,
                    strat,
                    symbol,
                    exposure=current_weather,
                    cost=proposed_cost,
                    max=max_weather,
                    **sig_ctx,
                )
                return False

        # 6. Rate Limiting
        seconds_since_last = (datetime.now() - self.last_trade_time).total_seconds()
        if seconds_since_last < self.MIN_TRADE_INTERVAL_SEC:
            log_rejection(
                RejectReason.TRADE_INTERVAL,
                strat,
                symbol,
                since_s=round(seconds_since_last, 1),
                min_s=self.MIN_TRADE_INTERVAL_SEC,
                **sig_ctx,
            )
            return False

        # 7. Per-Symbol Loss Cooldown (clean expired, then check)
        now = datetime.now()
        expired = [k for k, v in self.loss_cooldown.items() if now >= v]
        for k in expired:
            del self.loss_cooldown[k]

        if symbol:
            # Check exact ticker match
            if symbol in self.loss_cooldown:
                log_rejection(
                    RejectReason.COOLDOWN_SYMBOL,
                    strat,
                    symbol,
                    scope="exact",
                    until=self.loss_cooldown[symbol].strftime("%H:%M:%S"),
                    **sig_ctx,
                )
                return False
            # Secondary guard: check series prefix
            prefix = symbol.split("-")[0] if "-" in symbol else symbol
            if prefix in self.loss_cooldown:
                log_rejection(
                    RejectReason.COOLDOWN_SYMBOL,
                    strat,
                    symbol,
                    scope="series",
                    series=prefix,
                    until=self.loss_cooldown[prefix].strftime("%H:%M:%S"),
                    **sig_ctx,
                )
                return False

        self.daily_trade_count += 1
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
        # Hard position size cap: never record more than 50 contracts per entry
        MAX_CONTRACTS = 50
        if quantity > MAX_CONTRACTS:
            logger.warning(
                "[Risk] Position size capped: %d → %d contracts (%s)",
                quantity,
                MAX_CONTRACTS,
                symbol,
            )
            quantity = MAX_CONTRACTS
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
