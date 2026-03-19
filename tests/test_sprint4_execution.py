"""Tests for Sprint 4 — Execution Engine & Risk Overhaul.

Covers: fee calculator, order router, circuit breaker, live gateway,
enhanced risk manager, quarter-Kelly sizing, matching engine fees,
and order lifecycle audit trail.
"""

from unittest.mock import MagicMock


from src.core.interfaces import TradeSignal


# ===========================================================================
# 4.2  Fee Calculator
# ===========================================================================


class TestFeeCalculator:
    def test_maker_fee_at_50c(self):
        from src.core.fee_calculator import maker_fee

        # 0.0175 * 1 * 0.50 * 0.50 = 0.004375 -> ceil to $0.01
        fee = maker_fee(0.50, 1)
        assert fee == 0.01

    def test_taker_fee_at_50c(self):
        from src.core.fee_calculator import taker_fee

        # 0.07 * 1 * 0.50 * 0.50 = 0.0175 -> ceil to $0.02
        fee = taker_fee(0.50, 1)
        assert fee == 0.02

    def test_maker_fee_multiple_contracts(self):
        from src.core.fee_calculator import maker_fee

        # 0.0175 * 10 * 0.50 * 0.50 = 0.04375 -> ceil to $0.05
        fee = maker_fee(0.50, 10)
        assert fee == 0.05

    def test_fee_at_extreme_prices(self):
        from src.core.fee_calculator import maker_fee

        # At price=0.01: 0.0175 * 0.01 * 0.99 = 0.00017325 -> ceil to $0.01
        assert maker_fee(0.01, 1) == 0.01
        # At price=0.99: same by symmetry
        assert maker_fee(0.99, 1) == 0.01

    def test_fee_zero_for_invalid_price(self):
        from src.core.fee_calculator import maker_fee, taker_fee

        assert maker_fee(0.0, 1) == 0.0
        assert maker_fee(1.0, 1) == 0.0
        assert taker_fee(-0.5, 1) == 0.0

    def test_compute_fee_breakdown(self):
        from src.core.fee_calculator import compute_fee

        result = compute_fee(0.50, 5, is_maker=True)
        assert result.rate_used == "maker"
        assert result.fee > 0
        assert result.per_contract > 0

    def test_ev_after_fees_positive(self):
        from src.core.fee_calculator import ev_after_fees

        # prob=0.70, price=0.50 -> EV = 0.70 - 0.50 - fee > 0
        ev = ev_after_fees(0.70, 0.50)
        assert ev > 0

    def test_ev_after_fees_negative(self):
        from src.core.fee_calculator import ev_after_fees

        # prob=0.505, price=0.50 -> EV = 0.005 - 0.01 fee < 0
        ev = ev_after_fees(0.505, 0.50)
        assert ev < 0

    def test_trade_is_profitable(self):
        from src.core.fee_calculator import trade_is_profitable

        assert trade_is_profitable(0.70, 0.50) is True
        assert trade_is_profitable(0.505, 0.50) is False

    def test_taker_fee_4x_maker(self):
        """Taker rate is 4x maker rate."""
        from src.core.fee_calculator import maker_fee, taker_fee

        # For 100 contracts at 0.30 where rounding doesn't dominate
        m = maker_fee(0.30, 100)
        t = taker_fee(0.30, 100)
        ratio = t / m if m > 0 else 0
        assert 3.5 < ratio < 4.5  # ~4x


# ===========================================================================
# 4.1  Order Router
# ===========================================================================


class TestOrderRouter:
    def _make_router(self):
        from src.core.order_router import OrderRouter

        return OrderRouter(spread_buffer=0.02, urgency_threshold=0.10)

    def test_default_is_maker(self):
        router = self._make_router()
        decision = router.route(
            side="buy", probability=0.70, bid=0.50, ask=0.60, contracts=5
        )
        assert decision.order_type == "limit"
        assert decision.is_maker is True
        assert decision.limit_price is not None

    def test_urgent_flag_uses_taker(self):
        router = self._make_router()
        decision = router.route(
            side="buy",
            probability=0.80,
            bid=0.50,
            ask=0.55,
            contracts=5,
            urgent=True,
        )
        assert decision.order_type == "market"
        assert decision.is_maker is False

    def test_limit_price_within_spread(self):
        router = self._make_router()
        decision = router.route(side="buy", probability=0.70, bid=0.50, ask=0.60)
        assert decision.limit_price is not None
        assert 0.50 <= decision.limit_price <= 0.60

    def test_sell_side_routing(self):
        router = self._make_router()
        decision = router.route(side="sell", probability=0.70, bid=0.50, ask=0.60)
        assert decision.order_type == "limit"
        assert decision.limit_price is not None
        assert 0.50 <= decision.limit_price <= 0.60

    def test_maker_ratio_tracking(self):
        router = self._make_router()
        for _ in range(9):
            router.route(side="buy", probability=0.70, bid=0.50, ask=0.60)
        router.route(side="buy", probability=0.80, bid=0.50, ask=0.55, urgent=True)
        ratio = router.get_maker_ratio()
        assert 0.85 <= ratio <= 0.95  # 9 maker + 1 taker = 90%

    def test_recommended_price_used(self):
        router = self._make_router()
        decision = router.route(
            side="buy",
            probability=0.70,
            bid=0.50,
            ask=0.60,
            recommended_price=0.54,
        )
        assert decision.limit_price == 0.54


# ===========================================================================
# 4.7  Circuit Breaker
# ===========================================================================


class TestCircuitBreaker:
    def _make_breaker(self):
        from src.core.circuit_breaker import CircuitBreaker

        return CircuitBreaker(
            max_daily_loss_pct=0.05,
            max_consecutive_losses=3,
            max_brier_score=0.30,
        )

    def test_kill_switch(self):
        cb = self._make_breaker()
        assert cb.can_trade(bankroll=100) is True
        cb.activate_kill_switch("test")
        assert cb.can_trade(bankroll=100) is False
        assert cb.is_killed is True

    def test_kill_switch_deactivate(self):
        cb = self._make_breaker()
        cb.activate_kill_switch("test")
        cb.deactivate_kill_switch()
        assert cb.can_trade(bankroll=100) is True

    def test_daily_loss_halt(self):
        cb = self._make_breaker()
        # 6% loss on $100 bankroll (exceeds 5%)
        assert cb.can_trade(daily_pnl=-6.0, bankroll=100) is False

    def test_daily_loss_ok(self):
        cb = self._make_breaker()
        # 3% loss (within 5%)
        assert cb.can_trade(daily_pnl=-3.0, bankroll=100) is True

    def test_consecutive_loss_pauses_strategy(self):
        cb = self._make_breaker()
        cb.record_trade_result("TestStrat", won=False)
        cb.record_trade_result("TestStrat", won=False)
        assert cb.is_strategy_paused("TestStrat") is False
        cb.record_trade_result("TestStrat", won=False)  # 3rd loss
        assert cb.is_strategy_paused("TestStrat") is True
        assert cb.can_trade(strategy_name="TestStrat", bankroll=100) is False

    def test_win_resets_streak(self):
        cb = self._make_breaker()
        cb.record_trade_result("S", won=False)
        cb.record_trade_result("S", won=False)
        cb.record_trade_result("S", won=True)  # Reset
        cb.record_trade_result("S", won=False)
        assert cb.is_strategy_paused("S") is False

    def test_brier_score_pauses_market(self):
        cb = self._make_breaker()
        cb.check_brier_score("btc", 0.35)  # > 0.30
        assert cb.is_market_type_paused("btc") is True
        assert cb.can_trade(market_type="btc", bankroll=100) is False

    def test_brier_score_auto_unpauses(self):
        cb = self._make_breaker()
        cb.check_brier_score("btc", 0.35)
        assert cb.is_market_type_paused("btc") is True
        cb.check_brier_score("btc", 0.20)  # Recovered
        assert cb.is_market_type_paused("btc") is False

    def test_status_report(self):
        cb = self._make_breaker()
        status = cb.get_status()
        assert "kill_switch" in status
        assert "paused_strategies" in status
        assert "trigger_log" in status


# ===========================================================================
# 4.3  Live Gateway
# ===========================================================================


class TestLiveGateway:
    def _make_gateway(self, read_only=True):
        from src.core.live_gateway import LiveKalshiGateway

        provider = MagicMock()
        provider.read_only = read_only
        provider.anonymous = False
        provider.session = MagicMock()
        provider._get_authenticated_headers = MagicMock(return_value={})
        provider.get_balance = MagicMock(return_value=300.0)
        return LiveKalshiGateway(provider, use_production=False), provider

    def test_read_only_blocks_orders(self):
        gw, _ = self._make_gateway(read_only=True)
        sig = TradeSignal(symbol="TEST", side="buy", quantity=5, limit_price=0.50)
        assert gw.execute(sig) is False

    def test_anonymous_blocks_orders(self):
        from src.core.live_gateway import LiveKalshiGateway

        provider = MagicMock()
        provider.read_only = False
        provider.anonymous = True
        gw = LiveKalshiGateway(provider)
        sig = TradeSignal(symbol="TEST", side="buy", quantity=5, limit_price=0.50)
        assert gw.execute(sig) is False

    def test_order_history_tracked(self):
        gw, provider = self._make_gateway(read_only=False)
        resp = MagicMock()
        resp.status_code = 201
        resp.text = '{"order": {"order_id": "abc123", "status": "resting"}}'
        resp.json.return_value = {"order": {"order_id": "abc123", "status": "resting"}}
        provider.session.post.return_value = resp

        sig = TradeSignal(symbol="TEST", side="buy", quantity=5, limit_price=0.50)
        result = gw.execute(sig)
        assert result is True
        assert len(gw.get_order_history()) == 1

    def test_uses_demo_api_by_default(self):
        from src.core.live_gateway import LiveKalshiGateway

        provider = MagicMock()
        gw = LiveKalshiGateway(provider, use_production=False)
        assert "demo" in gw._api_url

    def test_get_balance(self):
        gw, provider = self._make_gateway()
        assert gw.get_balance() == 300.0


# ===========================================================================
# 4.4 + 4.5  Enhanced Risk Manager & Quarter-Kelly
# ===========================================================================


class TestEnhancedRiskManager:
    def _make_rm(self, balance=300.0):
        from src.core.risk_manager import RiskManager

        rm = RiskManager(starting_balance=balance)
        return rm

    def test_quarter_kelly_sizing(self):
        rm = self._make_rm(300.0)
        # At $300 balance (Seed stage): 0.25x Kelly
        qty = rm.calculate_kelly_size(0.70, 0.50)
        assert qty >= 1
        assert qty <= 500
        # Cost should be within 10% of bankroll (Seed stage cap)
        cost = qty * 0.50
        assert cost <= 300.0 * 0.10 + 1

    def test_bankroll_stage_seed(self):
        from src.core.risk_manager import _get_bankroll_stage

        pct, max_pos, kelly, label = _get_bankroll_stage(300.0)
        assert label == "Seed"
        assert pct == 0.10
        assert kelly == 0.25
        assert max_pos == 5

    def test_bankroll_stage_growth(self):
        from src.core.risk_manager import _get_bankroll_stage

        pct, max_pos, kelly, label = _get_bankroll_stage(5000.0)
        assert label == "Growth"
        assert kelly == 0.30
        assert max_pos == 12

    def test_bankroll_stage_compound(self):
        from src.core.risk_manager import _get_bankroll_stage

        pct, max_pos, kelly, label = _get_bankroll_stage(100_000.0)
        assert label == "Compound"
        assert pct == 0.025

    def test_max_positions_check(self):
        rm = self._make_rm(300.0)
        # Seed stage max = 5 positions
        for i in range(5):
            rm.exchange.positions.append(
                {"symbol": f"SYM{i}", "entry_price": 0.50, "quantity": 1, "side": "buy"}
            )
        assert rm.check_order(5.0, category="crypto") is False

    def test_circuit_breaker_integration(self):
        from src.core.circuit_breaker import CircuitBreaker

        rm = self._make_rm(300.0)
        cb = CircuitBreaker()
        rm.circuit_breaker = cb
        assert rm.check_order(5.0, category="crypto") is True

        cb.activate_kill_switch("test")
        assert rm.check_order(5.0, category="crypto") is False

    def test_btc_correlation_limit(self):
        rm = self._make_rm(300.0)
        # Add 2 BTC buy positions
        for i in range(2):
            rm.exchange.positions.append(
                {
                    "symbol": f"KXBTC15M-T{i}",
                    "entry_price": 0.50,
                    "quantity": 1,
                    "side": "buy",
                }
            )
        # 3rd BTC buy should be rejected
        assert rm.check_order(5.0, category="crypto") is False

    def test_cheap_short_cap(self):
        rm = self._make_rm(300.0)
        qty = rm.calculate_kelly_size(0.95, 0.05)
        # Max exposure on cheap short: $10 / (1-0.05) ≈ 10.5
        assert qty <= 11


# ===========================================================================
# 4.6  Simulated Exchange Enhancements
# ===========================================================================


class TestExchangeEnhancements:
    def _make_exchange(self):
        from src.core.matching_engine import SimulatedExchange

        closed = []
        ex = SimulatedExchange(on_close=lambda p: closed.append(p))
        return ex, closed

    def test_fee_deducted_on_open(self):
        ex, _ = self._make_exchange()
        ex.open_position("TEST-T50000", "buy", 0.50, 10, strategy_name="S")
        assert ex.total_fees_paid > 0
        assert ex.realized_pnl < 0  # Fees reduce PnL

    def test_maker_taker_tracking(self):
        ex, _ = self._make_exchange()
        ex.open_position("T1", "buy", 0.50, 5, is_maker=True)
        ex.open_position("T2", "buy", 0.50, 5, is_maker=False)
        stats = ex.get_stats()
        assert stats["maker_fills"] == 1
        assert stats["taker_fills"] == 1
        assert stats["maker_ratio"] == 0.5

    def test_audit_trail_on_open(self):
        ex, _ = self._make_exchange()
        ex.open_position("TEST", "buy", 0.50, 10, strategy_name="ML15m")
        assert len(ex.order_audit) == 1
        assert ex.order_audit[0]["event"] == "OPEN"
        assert ex.order_audit[0]["symbol"] == "TEST"

    def test_audit_trail_on_close(self):
        ex, _ = self._make_exchange()
        ex.open_position("TEST-T50000", "buy", 0.50, 10)
        pos = ex.positions[0]
        ex._close_position(pos, 0.60, reason="TAKE_PROFIT")
        close_entries = [a for a in ex.order_audit if a["event"] == "CLOSE"]
        assert len(close_entries) == 1
        assert close_entries[0]["reason"] == "TAKE_PROFIT"

    def test_fee_in_stats(self):
        ex, _ = self._make_exchange()
        ex.open_position("TEST", "buy", 0.50, 10)
        stats = ex.get_stats()
        assert "total_fees" in stats
        assert stats["total_fees"] > 0

    def test_position_has_fee_info(self):
        ex, _ = self._make_exchange()
        ex.open_position("TEST", "buy", 0.50, 10, is_maker=True)
        pos = ex.positions[0]
        assert "entry_fee" in pos
        assert pos["entry_fee"] > 0
        assert pos["is_maker"] is True


# ===========================================================================
# 4.8  ML gating still works with new fee_calculator
# ===========================================================================


class TestMLGatingWithFeeCalculator:
    def test_positive_ev_passes(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.70
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is True

    def test_negative_ev_rejected(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.55, confidence=0.52
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is False

    def test_break_even_rejected(self):
        from src.bots.mixins import SignalProcessorMixin

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.50
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is False
