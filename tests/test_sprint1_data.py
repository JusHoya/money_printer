"""Tests for Sprint 1 — Data infrastructure, feeds, and feature pipeline.

Covers: MarketStore CRUD, FeaturePipeline, mock providers (GBM, Black-Scholes,
NWS), data quality monitor, WebSocket message parsing, and harvester I/O.
"""

import os
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from src.core.interfaces import MarketData


# ===========================================================================
# 1.4  MarketStore (SQLite)
# ===========================================================================


class TestMarketStore:
    """Tests for the SQLite market data store."""

    @pytest.fixture(autouse=True)
    def _setup_store(self, tmp_path):
        from src.data.market_store import MarketStore

        self.db_path = str(tmp_path / "test_store.db")
        self.store = MarketStore(db_path=self.db_path)

    def test_db_file_created(self):
        assert os.path.exists(self.db_path)

    def test_insert_and_get_tick(self):
        now = datetime.now(timezone.utc).isoformat()
        self.store.insert_tick("BTC-TEST", now, 84000.0, 83999.0, 84001.0, 100, "test")
        ticks = self.store.get_ticks("BTC-TEST")
        assert len(ticks) >= 1
        assert ticks[0]["symbol"] == "BTC-TEST"

    def test_insert_ticks_batch(self):
        now = datetime.now(timezone.utc)
        batch = [
            {
                "symbol": "SYM",
                "timestamp": (now + timedelta(seconds=i)).isoformat(),
                "price": 100.0 + i,
                "bid": 99.0,
                "ask": 101.0,
                "volume": 50,
                "source": "test",
            }
            for i in range(20)
        ]
        inserted = self.store.insert_ticks_batch(batch)
        assert inserted >= 20
        count = self.store.tick_count()
        assert count >= 20

    def test_insert_and_get_candle(self):
        now = datetime.now(timezone.utc).isoformat()
        self.store.insert_candle("SYM", now, 100.0, 105.0, 98.0, 103.0, 500, "15m")
        candles = self.store.get_candles("SYM", "15m")
        assert len(candles) >= 1

    def test_insert_and_get_settlement(self):
        self.store.insert_settlement("MKT-001", "yes", "2026-03-19T12:00:00Z", 1.0)
        settlements = self.store.get_settlements()
        assert len(settlements) >= 1

    def test_insert_orderbook(self):
        now = datetime.now(timezone.utc).isoformat()
        bids = '[["0.50", 10], ["0.49", 20]]'
        asks = '[["0.51", 15], ["0.52", 25]]'
        self.store.insert_orderbook(now, "SYM", bids, asks)

    def test_get_latest_tick(self):
        now = datetime.now(timezone.utc)
        self.store.insert_tick(
            "SYM",
            (now - timedelta(seconds=10)).isoformat(),
            100.0,
            99.0,
            101.0,
            50,
            "test",
        )
        self.store.insert_tick("SYM", now.isoformat(), 105.0, 104.0, 106.0, 60, "test")
        latest = self.store.get_latest_tick("SYM")
        assert latest is not None
        assert latest["price"] == 105.0

    def test_tick_count_and_candle_count(self):
        assert self.store.tick_count() == 0
        self.store.insert_tick(
            "S", datetime.now(timezone.utc).isoformat(), 1.0, 0.9, 1.1, 10, "t"
        )
        assert self.store.tick_count() == 1
        assert self.store.candle_count() == 0

    def test_build_candles_from_ticks(self):
        now = datetime.now(timezone.utc)
        start = now.isoformat()
        for i in range(100):
            ts = (now + timedelta(seconds=i * 6)).isoformat()
            price = 100.0 + np.sin(i / 10) * 5
            self.store.insert_tick("SYM", ts, price, price - 0.5, price + 0.5, 10, "t")
        end = (now + timedelta(seconds=600)).isoformat()
        built = self.store.build_candles("SYM", "1m", start, end)
        assert len(built) >= 1


# ===========================================================================
# 1.5  Feature Pipeline
# ===========================================================================


class TestFeaturePipeline:
    def _make_df(self, n=100):
        """Create a synthetic price DataFrame suitable for feature computation."""
        np.random.seed(42)
        prices = np.cumsum(np.random.randn(n) * 0.5) + 84000
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-03-19", periods=n, freq="1min"),
                "close": prices,
                "volume": np.random.randint(10, 200, n),
                "bid": prices - 0.5,
                "ask": prices + 0.5,
                "high": prices + np.abs(np.random.randn(n)),
                "low": prices - np.abs(np.random.randn(n)),
                "open": prices + np.random.randn(n) * 0.3,
            }
        )

    def test_compute_features_adds_feat_columns(self):
        from src.ml.features import FeaturePipeline

        fp = FeaturePipeline()
        df = self._make_df()
        result = fp.compute_features(df)
        feat_cols = [c for c in result.columns if c.startswith("feat_")]
        assert len(feat_cols) >= 15

    def test_get_feature_names(self):
        from src.ml.features import FeaturePipeline

        fp = FeaturePipeline()
        names = fp.get_feature_names()
        assert len(names) >= 15
        assert all(n.startswith("feat_") for n in names)

    def test_features_include_rsi_macd_bb(self):
        from src.ml.features import FeaturePipeline

        fp = FeaturePipeline()
        df = self._make_df()
        result = fp.compute_features(df)
        cols = result.columns.tolist()
        assert "feat_rsi_14" in cols
        assert "feat_macd_line" in cols
        assert "feat_bb_upper" in cols

    def test_minimum_viable_rows(self):
        """Feature pipeline needs ~15+ rows for ta indicators; shouldn't crash."""
        from src.ml.features import FeaturePipeline

        fp = FeaturePipeline()
        df = self._make_df(20)
        result = fp.compute_features(df)
        assert len(result) == 20

    def test_compute_from_market_data(self):
        from src.ml.features import FeaturePipeline

        fp = FeaturePipeline()
        data = [
            MarketData(
                symbol="TEST",
                timestamp=datetime.now() + timedelta(seconds=i),
                price=100.0 + i * 0.1,
                volume=50,
                bid=99.5 + i * 0.1,
                ask=100.5 + i * 0.1,
                extra={},
            )
            for i in range(50)
        ]
        result = fp.compute_from_market_data(data)
        assert len(result) == 50
        assert any(c.startswith("feat_") for c in result.columns)


# ===========================================================================
# 1.8  Mock Providers (GBM, Black-Scholes, NWS)
# ===========================================================================


class TestMockProviders:
    def test_mock_coinbase_produces_market_data(self):
        from src.data.mock_providers import MockCoinbaseProvider

        provider = MockCoinbaseProvider(initial_price=84000.0, seed=42)
        provider.connect()
        md = provider.fetch_latest("BTC-USD")
        assert md is not None
        assert md.price > 0
        assert md.bid > 0
        assert md.ask > 0

    def test_mock_coinbase_prices_vary(self):
        from src.data.mock_providers import MockCoinbaseProvider

        provider = MockCoinbaseProvider(initial_price=84000.0, seed=42)
        provider.connect()
        prices = [provider.fetch_latest("BTC-USD").price for _ in range(10)]
        assert len(set(prices)) > 1  # Not all the same

    def test_mock_kalshi_produces_market_data(self):
        from src.data.mock_providers import MockCoinbaseProvider, MockKalshiProvider

        btc = MockCoinbaseProvider(initial_price=84000.0, seed=42)
        btc.connect()
        kalshi = MockKalshiProvider(underlying_provider=btc, default_strike=84000.0)
        kalshi.connect()
        md = kalshi.fetch_latest("KXBTC15M-TEST-T84000")
        assert md is not None
        assert 0.0 <= md.bid <= 1.0
        assert 0.0 <= md.ask <= 1.0

    def test_mock_nws_produces_weather_data(self):
        from src.data.mock_providers import MockNWSProvider

        provider = MockNWSProvider(stations=["KJFK"], base_temp_f=55.0, seed=42)
        provider.connect()
        md = provider.fetch_latest("KJFK")
        assert md is not None
        assert "temperature_f" in (md.extra or {})
        temp = md.extra["temperature_f"]
        assert 20 < temp < 120  # Reasonable F range

    def test_mock_nws_has_forecast(self):
        from src.data.mock_providers import MockNWSProvider

        provider = MockNWSProvider(stations=["KJFK"], seed=42)
        provider.connect()
        md = provider.fetch_latest("KJFK")
        forecast = (md.extra or {}).get("forecast", [])
        assert len(forecast) >= 1

    def test_generate_btc_price_path(self):
        from src.data.mock_providers import generate_btc_price_path

        path = generate_btc_price_path(n_steps=100, initial_price=84000.0, seed=42)
        assert len(path) == 100
        assert all(p > 0 for p in path)
        # Prices shouldn't all be identical
        assert len(set(path)) > 1


# ===========================================================================
# 1.7  Data Quality Monitor
# ===========================================================================


class TestDataQualityMonitor:
    def test_staleness_detection(self):
        from src.data.data_quality import DataQualityMonitor

        monitor = DataQualityMonitor(staleness_threshold_s=2.0)
        now = datetime.now(timezone.utc)
        monitor.record_update("SYM", now)
        assert monitor.is_stale("SYM") is False
        # Simulate passage of time by recording an old timestamp
        old = now - timedelta(seconds=5)
        monitor.record_update("OLD_SYM", old)
        assert monitor.is_stale("OLD_SYM") is True

    def test_unknown_symbol_is_stale(self):
        from src.data.data_quality import DataQualityMonitor

        monitor = DataQualityMonitor()
        assert monitor.is_stale("NEVER_SEEN") is True

    def test_deduplication(self):
        from src.data.data_quality import DataQualityMonitor

        monitor = DataQualityMonitor()
        now = datetime.now(timezone.utc)
        data = [
            MarketData("SYM", now, 100.0, 10, 99.0, 101.0, {}),
            MarketData("SYM", now, 100.0, 10, 99.0, 101.0, {}),
            MarketData("SYM", now + timedelta(seconds=1), 101.0, 10, 100.0, 102.0, {}),
        ]
        deduped = monitor.deduplicate(data)
        assert len(deduped) == 2

    def test_timezone_normalization(self):
        from src.data.data_quality import DataQualityMonitor

        # Naive datetime
        naive = datetime(2026, 3, 19, 12, 0, 0)
        result = DataQualityMonitor.normalize_timezone(naive)
        assert result.tzinfo is not None

    def test_get_stale_symbols(self):
        from src.data.data_quality import DataQualityMonitor

        monitor = DataQualityMonitor(staleness_threshold_s=1.0)
        old = datetime.now(timezone.utc) - timedelta(seconds=5)
        monitor.record_update("STALE_ONE", old)
        monitor.record_update("FRESH", datetime.now(timezone.utc))
        stale = monitor.get_stale_symbols()
        assert "STALE_ONE" in stale
        assert "FRESH" not in stale

    def test_get_status(self):
        from src.data.data_quality import DataQualityMonitor

        monitor = DataQualityMonitor()
        monitor.record_update("A", datetime.now(timezone.utc))
        status = monitor.get_status()
        assert "symbols_tracked" in status or isinstance(status, dict)


# ===========================================================================
# 1.1 + 1.2  WebSocket Provider Parsing (unit-level, no network)
# ===========================================================================


class TestCoinbaseWSParsing:
    def test_parse_ticker_message(self):
        from src.data.coinbase_ws import CoinbaseWebSocketProvider

        msg = {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "84123.45",
            "best_bid": "84120.00",
            "best_ask": "84125.00",
            "volume_24h": "12345.67",
            "time": "2026-03-19T14:30:00.000000Z",
        }
        md = CoinbaseWebSocketProvider._parse_ticker(msg)
        assert md is not None
        assert md.price == 84123.45
        assert md.bid == 84120.00
        assert md.ask == 84125.00
        assert md.symbol == "BTC-USD"

    def test_parse_ticker_invalid_returns_none(self):
        from src.data.coinbase_ws import CoinbaseWebSocketProvider

        md = CoinbaseWebSocketProvider._parse_ticker({"type": "ticker"})
        assert md is None


class TestKalshiWSParsing:
    def test_provider_instantiates(self):
        from src.data.kalshi_ws import KalshiWebSocket

        ws = KalshiWebSocket.__new__(KalshiWebSocket)
        # Verify the class exists and has expected methods
        assert hasattr(ws, "subscribe")
        assert hasattr(ws, "fetch_latest")
        assert hasattr(ws, "connect")


# ===========================================================================
# 1.3  Harvester Parquet I/O (no network)
# ===========================================================================


class TestHarvesterIO:
    def test_save_and_load_parquet(self, tmp_path, monkeypatch):
        from src.data.harvester import HistoricalHarvester
        from unittest.mock import MagicMock
        import src.data.harvester as harvester_mod

        monkeypatch.setattr(harvester_mod, "_HISTORICAL_DIR", tmp_path)

        provider = MagicMock()
        h = HistoricalHarvester(provider)

        fake_markets = [
            {"ticker": f"MKT-{i}", "status": "settled", "last_price_dollars": "0.85"}
            for i in range(10)
        ]
        path = h.save_to_parquet(
            fake_markets, market_type="btc_15m", date_str="2026-03-19"
        )
        assert path is not None
        assert path.exists()

        # Load it back using the static method
        df = HistoricalHarvester.load_from_parquet("btc_15m")
        assert len(df) == 10

    def test_save_empty_returns_none(self, tmp_path, monkeypatch):
        from src.data.harvester import HistoricalHarvester
        from unittest.mock import MagicMock
        import src.data.harvester as harvester_mod

        monkeypatch.setattr(harvester_mod, "_HISTORICAL_DIR", tmp_path)

        provider = MagicMock()
        h = HistoricalHarvester(provider)
        result = h.save_to_parquet([], market_type="empty")
        assert result is None


# ===========================================================================
# 1.6  HRRR Provider (structure check, no network)
# ===========================================================================


class TestHRRRProvider:
    def test_provider_instantiates(self):
        from src.data.hrrr_provider import HRRRProvider

        provider = HRRRProvider.__new__(HRRRProvider)
        assert hasattr(provider, "connect")
        assert hasattr(provider, "fetch_latest")
        assert hasattr(provider, "get_divergence")
