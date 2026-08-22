"""Tests for Sprint 4 — Execution Engine & Risk Overhaul.

Covers: fee calculator, order router, circuit breaker, live gateway,
enhanced risk manager, quarter-Kelly sizing, matching engine fees,
and order lifecycle audit trail.
"""

from unittest.mock import MagicMock

import pytest

from src.core.interfaces import TradeSignal


# ===========================================================================
# 4.2  Fee Calculator
# ===========================================================================


class TestFeeCalculator:
    """Fee model, corrected in PRD Phase 2 against the published schedule.

    Kalshi's maker multiplier defaults to **zero**; only the series listed in
    the schedule's "Non-Standard Fees" table (live ``fee_type ==
    "quadratic_with_maker_fees"``) bill resting liquidity. The weather series
    this project trades are not among them, so their maker fee is $0.00 and
    these tests assert that rather than the old unconditional 1.75%. See
    ``reports/phase2/ws_c_fee_verification.md`` for the provenance.
    """

    def test_maker_fee_is_zero_on_standard_series(self):
        from src.core.fee_calculator import maker_fee

        # KXHIGH* weather: fee_type "quadratic", maker multiplier M = 0.
        assert maker_fee(0.50, 1) == 0.0
        assert maker_fee(0.10, 100) == 0.0

    def test_maker_fee_charged_on_maker_fee_series(self):
        from src.core.fee_calculator import FEE_TYPE_WITH_MAKER_FEES, maker_fee

        # 0.0175 * 1 * 0.50 * 0.50 = 0.004375 -> ceil to $0.01
        assert maker_fee(0.50, 1, FEE_TYPE_WITH_MAKER_FEES) == 0.01
        # 0.0175 * 10 * 0.50 * 0.50 = 0.04375 -> ceil to $0.05
        assert maker_fee(0.50, 10, FEE_TYPE_WITH_MAKER_FEES) == 0.05

    def test_fee_type_for_series(self):
        from src.core.fee_calculator import (
            FEE_TYPE_STANDARD,
            FEE_TYPE_WITH_MAKER_FEES,
            fee_type_for_series,
        )

        assert fee_type_for_series("KXHIGHNY") == FEE_TYPE_STANDARD
        assert fee_type_for_series("KXHIGHCHI") == FEE_TYPE_STANDARD
        assert fee_type_for_series("KXAAAGASM") == FEE_TYPE_WITH_MAKER_FEES

    def test_fee_type_for_symbol(self):
        """Runtime call sites hold a market ticker, not a series ticker.

        Series identity has to be recoverable from the symbol, or the fee model
        cannot be threaded anywhere in ``src/`` and every maker order silently
        prices on the standard schedule.
        """
        from src.core.fee_calculator import (
            FEE_TYPE_STANDARD,
            FEE_TYPE_WITH_MAKER_FEES,
            fee_type_for_symbol,
            series_ticker_from_symbol,
        )

        assert series_ticker_from_symbol("KXHIGHNY-26JUL27-B82.5") == "KXHIGHNY"
        assert series_ticker_from_symbol("kxaaagasm-26aug-b3.25") == "KXAAAGASM"
        assert series_ticker_from_symbol("") == ""

        assert fee_type_for_symbol("KXHIGHNY-26JUL27-B82.5") == FEE_TYPE_STANDARD
        assert fee_type_for_symbol("KXAAAGASM-26AUG-B3.25") == FEE_TYPE_WITH_MAKER_FEES
        # Unknown / absent symbol falls to the schedule's documented default.
        assert fee_type_for_symbol("") == FEE_TYPE_STANDARD
        assert fee_type_for_symbol(None) == FEE_TYPE_STANDARD

    def test_taker_fee_at_50c(self):
        from src.core.fee_calculator import taker_fee

        # 0.07 * 1 * 0.50 * 0.50 = 0.0175 -> ceil to $0.02
        fee = taker_fee(0.50, 1)
        assert fee == 0.02

    def test_taker_fee_matches_published_table_on_whole_cents(self):
        """A bare ``ceil(raw*100)`` overcharges when the exact fee is a whole cent.

        ``0.07 * 100 * 0.10 * 0.90`` evaluates to 0.6300000000000002 in binary
        float, so the old implementation returned $0.64 where Kalshi's own
        published table says $0.63.
        """
        from src.core.fee_calculator import taker_fee

        assert taker_fee(0.10, 100) == 0.63
        assert taker_fee(0.50, 100) == 1.75
        assert taker_fee(0.20, 100) == 1.12

    def test_fee_at_extreme_prices(self):
        from src.core.fee_calculator import FEE_TYPE_WITH_MAKER_FEES, maker_fee

        # At price=0.01: 0.0175 * 0.01 * 0.99 = 0.00017325 -> ceil to $0.01
        assert maker_fee(0.01, 1, FEE_TYPE_WITH_MAKER_FEES) == 0.01
        # At price=0.99: same by symmetry
        assert maker_fee(0.99, 1, FEE_TYPE_WITH_MAKER_FEES) == 0.01

    def test_fee_zero_for_invalid_price(self):
        from src.core.fee_calculator import (
            FEE_TYPE_WITH_MAKER_FEES,
            maker_fee,
            taker_fee,
        )

        assert maker_fee(0.0, 1, FEE_TYPE_WITH_MAKER_FEES) == 0.0
        assert maker_fee(1.0, 1, FEE_TYPE_WITH_MAKER_FEES) == 0.0
        assert taker_fee(-0.5, 1) == 0.0

    def test_compute_fee_breakdown(self):
        from src.core.fee_calculator import compute_fee

        result = compute_fee(0.50, 5, is_maker=False)
        assert result.rate_used == "taker"
        assert result.fee > 0
        assert result.per_contract > 0

        maker = compute_fee(0.50, 5, is_maker=True)
        assert maker.rate_used == "maker"
        assert maker.fee == 0.0  # standard series

    def test_ev_after_fees_positive(self):
        from src.core.fee_calculator import ev_after_fees

        # prob=0.70, price=0.50 -> EV = 0.70 - 0.50 - fee > 0
        ev = ev_after_fees(0.70, 0.50)
        assert ev > 0

    def test_ev_after_fees_negative(self):
        from src.core.fee_calculator import ev_after_fees

        # Taker at 50c costs $0.02/contract per leg, so a 0.5pt edge is
        # swamped: EV = 0.005 - 2 * 0.02 < 0.
        ev = ev_after_fees(0.505, 0.50, is_maker=False)
        assert ev < 0

    def test_settlement_exit_charges_one_leg(self):
        """PRD FR-1.5 holds weather to expiry, and settlement is free."""
        from src.core.fee_calculator import (
            EXIT_SETTLEMENT,
            EXIT_TRADE_OUT,
            ev_after_fees,
            taker_fee,
        )

        fee = taker_fee(0.50, 1)
        round_trip = ev_after_fees(0.70, 0.50, is_maker=False, exit_mode=EXIT_TRADE_OUT)
        held = ev_after_fees(0.70, 0.50, is_maker=False, exit_mode=EXIT_SETTLEMENT)
        assert held - round_trip == pytest.approx(fee)

    def test_invalid_exit_mode_rejected(self):
        from src.core.fee_calculator import ev_after_fees

        with pytest.raises(ValueError):
            ev_after_fees(0.70, 0.50, exit_mode="hold_forever")

    def test_trade_is_profitable(self):
        from src.core.fee_calculator import trade_is_profitable

        assert trade_is_profitable(0.70, 0.50) is True
        assert trade_is_profitable(0.505, 0.50, is_maker=False) is False

    def test_taker_fee_4x_maker(self):
        """Taker rate is 4x maker rate on a series that bills makers."""
        from src.core.fee_calculator import (
            FEE_TYPE_WITH_MAKER_FEES,
            maker_fee,
            taker_fee,
        )

        # For 100 contracts at 0.30 where rounding doesn't dominate
        m = maker_fee(0.30, 100, FEE_TYPE_WITH_MAKER_FEES)
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

    def test_maker_leg_is_priced_per_series(self):
        """The maker leg must not be unconditionally free.

        Pricing every maker order on the standard schedule makes maker beat
        taker on EV in every equal-price comparison, whatever the series. On
        KXAAAGASM the maker fee is real and both the fee estimate and the EV
        have to show it.
        """
        router = self._make_router()

        weather = router.route(
            side="buy",
            probability=0.70,
            bid=0.50,
            ask=0.60,
            contracts=10,
            symbol="KXHIGHNY-26JUL27-B82.5",
        )
        gas = router.route(
            side="buy",
            probability=0.70,
            bid=0.50,
            ask=0.60,
            contracts=10,
            symbol="KXAAAGASM-26AUG-B3.25",
        )

        assert weather.is_maker is True and gas.is_maker is True
        assert weather.limit_price == gas.limit_price
        assert weather.fee_estimate == 0.0
        assert gas.fee_estimate > 0.0
        assert gas.ev_per_contract < weather.ev_per_contract


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
        # Taker: a standard-series maker order is fee-free, so the fee-plumbing
        # assertion has to ride the path that actually incurs a charge.
        ex, _ = self._make_exchange()
        ex.open_position(
            "TEST-T50000", "buy", 0.50, 10, strategy_name="S", is_maker=False
        )
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
        ex.open_position("TEST", "buy", 0.50, 10, is_maker=False)
        stats = ex.get_stats()
        assert "total_fees" in stats
        assert stats["total_fees"] > 0

    def test_position_has_fee_info(self):
        ex, _ = self._make_exchange()
        ex.open_position("TEST", "buy", 0.50, 10, is_maker=False)
        pos = ex.positions[0]
        assert "entry_fee" in pos
        assert pos["entry_fee"] > 0
        assert pos["is_maker"] is False

    def test_maker_open_is_fee_free_but_still_recorded(self):
        """A $0 fee must still be booked as a field, not omitted.

        Standard-series maker orders cost nothing; the position record has to
        say so explicitly so a downstream reader can tell "no fee" from
        "fee unknown".
        """
        ex, _ = self._make_exchange()
        ex.open_position("TEST", "buy", 0.50, 10, is_maker=True)
        pos = ex.positions[0]
        assert "entry_fee" in pos
        assert pos["entry_fee"] == 0.0
        assert pos["is_maker"] is True
        assert pos["fill_type"] == "maker"

    def test_unknown_fill_type_is_booked_as_taker(self):
        """A fill whose type the caller could not state costs the taker fee.

        This is the production paper-trading path: nothing between a strategy
        and the exchange knows whether the order rested or crossed. Booking it
        as a maker charged $0.00 once the maker multiplier was corrected to
        zero, so every simulated fill in the ledger the FR-5 capital gate reads
        was free. Assume the expensive side instead.
        """
        ex, _ = self._make_exchange()
        ex.open_position("TEST-T50000", "buy", 0.50, 10, strategy_name="S")

        pos = ex.positions[0]
        # ceil(0.07 * 10 * 0.50 * 0.50) = $0.18
        assert pos["entry_fee"] == pytest.approx(0.18)
        assert ex.total_fees_paid == pytest.approx(0.18)
        assert ex.cumulative_entry_fees == pytest.approx(0.18)
        assert ex.realized_pnl == pytest.approx(-0.18)
        assert ex.get_stats()["taker_fills"] == 1
        assert ex.get_stats()["maker_fills"] == 0

    def test_unknown_fill_type_is_loud_on_the_position_record(self):
        """The record must distinguish an assumed taker from an observed one.

        Otherwise a downstream reader cannot tell a measured taker fill from a
        default, which is how the maker default hid for a whole sprint.
        """
        ex, _ = self._make_exchange()
        ex.open_position("TEST-A", "buy", 0.50, 10)  # fill type not supplied
        ex.open_position("TEST-B", "buy", 0.50, 10, is_maker=False)
        ex.open_position("TEST-C", "buy", 0.50, 10, is_maker=True)

        assert [p["fill_type"] for p in ex.positions] == [
            "taker_assumed",
            "taker",
            "maker",
        ]
        # The audit trail carries the same distinction.
        opens = [a for a in ex.order_audit if a["event"] == "OPEN"]
        assert [a["fill_type"] for a in opens] == ["taker_assumed", "taker", "maker"]

    def test_exit_fee_on_a_record_without_a_fill_type_is_taker(self):
        """A position restored from disk without ``is_maker`` exits as a taker.

        Same rule as the entry: the unknown case must not be the free one.
        """
        ex, _ = self._make_exchange()
        ex.open_position("TEST-T50000", "buy", 0.50, 10, is_maker=False)
        pos = ex.positions[0]
        pos.pop("is_maker")
        pos.pop("fill_type")

        ex._close_position(pos, 0.60, reason="TAKE_PROFIT")

        closed = ex.closed_trades[-1]
        # ceil(0.07 * 10 * 0.60 * 0.40) = $0.17, not the $0.00 maker fee.
        assert closed["exit_fee"] == pytest.approx(0.17)

    def test_maker_fee_series_is_charged_on_the_maker_path(self):
        """The series' fee type reaches the exchange's fee model.

        KXAAAGASM (PRD Phase 4 gas) is on the schedule's "Non-Standard Fees"
        table and bills resting liquidity. Pricing every maker order on the
        standard schedule books $0.00 for it.
        """
        ex, _ = self._make_exchange()
        ex.open_position("KXAAAGASM-26AUG-B3.25", "buy", 0.50, 10, is_maker=True)
        # ceil(0.0175 * 10 * 0.50 * 0.50) = $0.05
        assert ex.positions[0]["entry_fee"] == pytest.approx(0.05)

        ex2, _ = self._make_exchange()
        ex2.open_position("KXHIGHNY-26JUL27-B82.5", "buy", 0.50, 10, is_maker=True)
        assert ex2.positions[0]["entry_fee"] == 0.0

    def test_signal_path_threads_symbol_and_fill_type(self):
        """The live path must hand the fee model everything it needs.

        Two threads have to survive ``_process_signals``:

        * ``symbol`` into ``calculate_kelly_size`` — it carries the series
          identity the maker fee depends on. Without it a KXAAAGASM order sizes
          as if resting liquidity were free.
        * ``is_maker`` into ``record_execution`` — so the exchange books the
          caller's fill type instead of falling back to a default.
        """
        from types import SimpleNamespace

        from src.bots.mixins import SignalProcessorMixin

        class _Host(SignalProcessorMixin):
            pass

        class _Dash:
            def log(self, msg):
                pass

            def record_signal(self, sig, status="", strategy_name=""):
                pass

        class _Risk:
            def __init__(self):
                self.last_trade_time = None
                self.loss_cooldown = {}
                self.exchange = SimpleNamespace(positions=[])
                self.kelly_calls = []
                self.exec_kwargs = []

            def calculate_kelly_size(
                self, confidence, price, strategy_name="", symbol=""
            ):
                self.kelly_calls.append(symbol)
                return 5

            def check_order(self, cost, **kwargs):
                return True

            def record_execution(self, *args, **kwargs):
                self.exec_kwargs.append(kwargs)
                self.exchange.positions.append({})

        risk = _Risk()
        sig = TradeSignal(
            symbol="KXAAAGASM-26AUG-B3.25",
            side="buy",
            quantity=1,
            limit_price=0.40,
            confidence=0.90,
        )
        assert _Host()._process_signals([sig], "S", risk, _Dash()) is True

        assert risk.kelly_calls == ["KXAAAGASM-26AUG-B3.25"]
        assert "is_maker" in risk.exec_kwargs[0]
        # No component in the live loop can state a fill type today, so the
        # signal carries None and the exchange assumes the taker side.
        assert risk.exec_kwargs[0]["is_maker"] is None

    def test_record_execution_threads_the_fill_type(self):
        """RiskManager.record_execution must not swallow the fill type.

        It previously passed nothing, so every live fill silently inherited the
        exchange's default rather than the caller's intent.
        """
        from src.core.risk_manager import RiskManager

        rm_unknown = RiskManager(starting_balance=100.0)
        rm_unknown.record_execution(5.0, "TEST-T50000", "buy", 10, 0.50)
        assert rm_unknown.exchange.positions[0]["fill_type"] == "taker_assumed"
        assert rm_unknown.exchange.total_fees_paid == pytest.approx(0.18)

        rm_maker = RiskManager(starting_balance=100.0)
        rm_maker.record_execution(5.0, "TEST-T50000", "buy", 10, 0.50, is_maker=True)
        assert rm_maker.exchange.positions[0]["fill_type"] == "maker"
        assert rm_maker.exchange.total_fees_paid == 0.0


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
        """Break-even before fees is rejected by the fee term, not by a tie-break.

        The gate prices a taker round trip, so at confidence == price == 0.50
        the EV is 0.50 - 0.50 - 2 x $0.02 = -0.04. Asserting the EV explicitly
        keeps this from degenerating into a test of ``>`` versus ``>=``, which
        is what it had become once the maker fee was corrected to zero.
        """
        from src.bots.mixins import SignalProcessorMixin
        from src.core.fee_calculator import ev_after_fees

        assert ev_after_fees(0.50, 0.50, 1, is_maker=False) == pytest.approx(-0.04)

        sig = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.50
        )
        assert SignalProcessorMixin._ml_ev_gate(sig) is False

    def test_gate_charges_taker_fees(self):
        """The gate charges the taker fee, not the (free) standard-series maker fee.

        A maker-priced gate subtracts $0.00 on every KXHIGH* series, so it
        degenerates to ``confidence > limit_price`` and passes an arbitrarily
        thin edge. The margin the gate carries must be at least one taker fee.
        """
        from src.bots.mixins import SignalProcessorMixin

        thin = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.5001
        )
        assert SignalProcessorMixin._ml_ev_gate(thin) is False

        # Same price, edge now larger than the round-trip taker cost.
        fat = TradeSignal(
            symbol="T", side="buy", quantity=1, limit_price=0.50, confidence=0.55
        )
        assert SignalProcessorMixin._ml_ev_gate(fat) is True
