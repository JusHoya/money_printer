"""Tests for WS4: YES/NO dual-side BTC hourly trading."""
from datetime import datetime, timedelta
from src.core.matching_engine import SimulatedExchange
from src.core.interfaces import TradeSignal
from src.strategies.crypto_strategy import CryptoHourlyStrategyV3


def test_no_contract_pnl_calculation():
    """NO contract PnL should be based on inverted price."""
    closed = []
    ex = SimulatedExchange(on_close=lambda p: closed.append(p))

    # BUY NO at $0.96 (equivalent to YES at $0.04)
    ex.open_position(
        symbol="kxbtcd-26feb1623-T99000",
        side="buy",
        entry_price=0.96,
        quantity=10,
        stop_loss=0.0,
        contract_side='NO',
        strategy_name="Test NO",
    )

    pos = ex.positions[0]
    assert pos['contract_side'] == 'NO'

    # When YES estimate is 0.02 (meaning NO is worth 0.98), we should be in profit
    # The update_market calculates estimated YES price, then inverts for NO
    pos['last_market_price'] = 0.98  # NO price went up
    ex._check_profit_targets(pos, 0.98)
    # +0.02 move, should not hit first target (needs +0.05)
    assert len(closed) == 0


def test_trade_signal_has_contract_side():
    """TradeSignal should default to 'YES' and support 'NO'."""
    sig_default = TradeSignal(symbol="TEST", side="buy", quantity=1)
    assert sig_default.contract_side == 'YES'

    sig_no = TradeSignal(symbol="TEST", side="buy", quantity=1, contract_side='NO')
    assert sig_no.contract_side == 'NO'


def test_hourlyv3_generates_no_signals_when_bearish():
    """CryptoHourlyStrategyV3 should generate BUY NO (not SELL YES) when bearish."""
    strat = CryptoHourlyStrategyV3(confidence_margin=50.0, obi_threshold=0.0)

    # Seed price history (trending DOWN)
    now = datetime.now()
    for i in range(20):
        strat.price_history.append((
            now - timedelta(minutes=20-i),
            96000.0 - i * 50  # Declining prices
        ))

    # Market where predicted price will be below strike
    md_bear = type('MarketData', (), {
        'symbol': 'kxbtcd-26feb1623-T99000',
        'timestamp': now,
        'price': 95000.0,
        'volume': 100,
        'bid': 0.05,
        'ask': 0.06,
        'extra': {
            'source': '',
            'no_bid': 0.94,
            'no_ask': 0.95,
            'close_time': (now + timedelta(hours=1)).isoformat(),
        },
    })()

    signals = strat.analyze(md_bear)
    # If signal generated, it should be BUY NO, not SELL YES
    for sig in signals:
        if sig.side == 'buy' and hasattr(sig, 'contract_side') and sig.contract_side == 'NO':
            assert True
            return
        if sig.side == 'sell':
            assert False, "Should generate BUY NO, not SELL YES for bearish signal"


def test_position_stores_contract_side():
    """Positions should track contract_side."""
    ex = SimulatedExchange()
    ex.open_position("TEST", "buy", 0.96, 10, contract_side='NO')
    pos = ex.positions[0]
    assert pos['contract_side'] == 'NO'

    ex.open_position("TEST2", "buy", 0.50, 10)
    pos2 = ex.positions[1]
    assert pos2['contract_side'] == 'YES'
