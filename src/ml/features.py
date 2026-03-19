"""
Feature engineering pipeline for ML models.

Computes 15+ technical and microstructure features from raw market data.
Features feed into XGBoost and LSTM models (Sprint 2).
"""

from typing import List

import numpy as np
import pandas as pd
import ta

from src.core.interfaces import MarketData


class FeaturePipeline:
    """Stateless feature computation pipeline.

    Transforms raw price/volume DataFrames into feature-enriched DataFrames
    suitable for ML model input.  All computed columns are prefixed with
    ``feat_`` so they can be easily separated from raw data columns.
    """

    # Authoritative list of feature columns this pipeline may produce.
    _FEATURE_NAMES: List[str] = [
        # RSI
        "feat_rsi_14",
        # MACD
        "feat_macd_line",
        "feat_macd_signal",
        "feat_macd_histogram",
        # Bollinger Bands
        "feat_bb_upper",
        "feat_bb_middle",
        "feat_bb_lower",
        # EMAs
        "feat_ema_9",
        "feat_ema_21",
        "feat_ema_50",
        # ATR
        "feat_atr_14",
        # Order Book Imbalance
        "feat_obi",
        # VWAP
        "feat_vwap",
        # Realized Volatility
        "feat_realized_vol_1m",
        "feat_realized_vol_5m",
        "feat_realized_vol_15m",
        # Price Momentum
        "feat_momentum_1m",
        "feat_momentum_5m",
        "feat_momentum_15m",
        # Time-to-Expiry
        "feat_time_to_expiry",
        # Spread Width
        "feat_spread_width",
        # Volume Surge Ratio
        "feat_volume_surge_ratio",
        # Rate of Change
        "feat_roc",
        # Stochastic Oscillator
        "feat_stoch_k",
        "feat_stoch_d",
        # Williams %R
        "feat_williams_r",
    ]

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all ML features and append them to *df*.

        Parameters
        ----------
        df : pd.DataFrame
            Must contain at least a ``price`` **or** ``close`` column.
            Optional columns: ``timestamp``, ``open``, ``high``, ``low``,
            ``volume``, ``bid``, ``ask``, ``expiry``.

        Returns
        -------
        pd.DataFrame
            A copy of *df* with ``feat_*`` columns appended.  NaN values
            from lookback warm-up periods are preserved — the caller
            decides whether to drop or fill them.
        """
        df = df.copy()
        df = self._normalise_columns(df)

        close = df["close"]
        high = df.get("high", close)
        low = df.get("low", close)
        _open = df.get("open", close)  # noqa: F841
        volume = df.get("volume")

        # --- RSI -------------------------------------------------------
        df["feat_rsi_14"] = ta.momentum.RSIIndicator(close=close, window=14).rsi()

        # --- MACD ------------------------------------------------------
        macd = ta.trend.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
        df["feat_macd_line"] = macd.macd()
        df["feat_macd_signal"] = macd.macd_signal()
        df["feat_macd_histogram"] = macd.macd_diff()

        # --- Bollinger Bands -------------------------------------------
        bb = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)
        df["feat_bb_upper"] = bb.bollinger_hband()
        df["feat_bb_middle"] = bb.bollinger_mavg()
        df["feat_bb_lower"] = bb.bollinger_lband()

        # --- EMAs ------------------------------------------------------
        df["feat_ema_9"] = ta.trend.EMAIndicator(close=close, window=9).ema_indicator()
        df["feat_ema_21"] = ta.trend.EMAIndicator(
            close=close, window=21
        ).ema_indicator()
        df["feat_ema_50"] = ta.trend.EMAIndicator(
            close=close, window=50
        ).ema_indicator()

        # --- ATR -------------------------------------------------------
        df["feat_atr_14"] = ta.volatility.AverageTrueRange(
            high=high, low=low, close=close, window=14
        ).average_true_range()

        # --- Order Book Imbalance --------------------------------------
        if "bid" in df.columns and "ask" in df.columns:
            bid_vol = df["bid"].astype(float)
            ask_vol = df["ask"].astype(float)
            total = bid_vol + ask_vol
            df["feat_obi"] = np.where(
                total != 0,
                (bid_vol - ask_vol) / total,
                0.0,
            )
        else:
            df["feat_obi"] = np.nan

        # --- VWAP ------------------------------------------------------
        if volume is not None and not volume.isna().all():
            cum_vol = volume.cumsum()
            cum_vp = (close * volume).cumsum()
            df["feat_vwap"] = np.where(
                cum_vol != 0,
                cum_vp / cum_vol,
                close,
            )
        else:
            df["feat_vwap"] = np.nan

        # --- Realized Volatility (rolling std of log-returns) ----------
        log_ret = np.log(close / close.shift(1))
        df["feat_realized_vol_1m"] = log_ret.rolling(window=1, min_periods=1).std()
        df["feat_realized_vol_5m"] = log_ret.rolling(window=5, min_periods=1).std()
        df["feat_realized_vol_15m"] = log_ret.rolling(window=15, min_periods=1).std()

        # --- Price Momentum (absolute price change over N periods) -----
        df["feat_momentum_1m"] = close.diff(1)
        df["feat_momentum_5m"] = close.diff(5)
        df["feat_momentum_15m"] = close.diff(15)

        # --- Time-to-Expiry (normalised 0..1) --------------------------
        if "expiry" in df.columns and "timestamp" in df.columns:
            expiry = pd.to_datetime(df["expiry"])
            ts = pd.to_datetime(df["timestamp"])
            total_span = (expiry - ts).dt.total_seconds()
            max_span = total_span.max()
            if max_span and max_span > 0:
                df["feat_time_to_expiry"] = total_span / max_span
            else:
                df["feat_time_to_expiry"] = np.nan
        else:
            df["feat_time_to_expiry"] = np.nan

        # --- Spread Width ----------------------------------------------
        if "bid" in df.columns and "ask" in df.columns:
            df["feat_spread_width"] = df["ask"].astype(float) - df["bid"].astype(float)
        else:
            df["feat_spread_width"] = np.nan

        # --- Volume Surge Ratio ----------------------------------------
        if volume is not None and not volume.isna().all():
            rolling_mean_vol = volume.rolling(window=20, min_periods=1).mean()
            df["feat_volume_surge_ratio"] = np.where(
                rolling_mean_vol != 0,
                volume / rolling_mean_vol,
                1.0,
            )
        else:
            df["feat_volume_surge_ratio"] = np.nan

        # --- Price Rate of Change (%) ----------------------------------
        df["feat_roc"] = ta.momentum.ROCIndicator(close=close, window=14).roc()

        # --- Stochastic Oscillator -------------------------------------
        stoch = ta.momentum.StochasticOscillator(
            high=high, low=low, close=close, window=14, smooth_window=3
        )
        df["feat_stoch_k"] = stoch.stoch()
        df["feat_stoch_d"] = stoch.stoch_signal()

        # --- Williams %R -----------------------------------------------
        df["feat_williams_r"] = ta.momentum.WilliamsRIndicator(
            high=high, low=low, close=close, lbp=14
        ).williams_r()

        return df

    def compute_from_ticks(self, ticks: List[dict]) -> pd.DataFrame:
        """Convert raw tick dicts to a feature DataFrame.

        Each dict should have at least ``price`` (or ``close``) and ideally
        ``timestamp`` and ``volume`` keys.  Additional keys (``bid``,
        ``ask``, ``high``, ``low``, ``open``) are used when present.
        """
        df = pd.DataFrame(ticks)
        return self.compute_features(df)

    def compute_from_market_data(self, data: List[MarketData]) -> pd.DataFrame:
        """Convert a list of :class:`MarketData` objects to a feature DataFrame."""
        records = []
        for md in data:
            record = {
                "timestamp": md.timestamp,
                "close": md.price,
                "volume": md.volume,
                "bid": md.bid,
                "ask": md.ask,
            }
            # Pull extra fields that callers may have stashed.
            if md.extra:
                for key in ("high", "low", "open", "expiry"):
                    if key in md.extra:
                        record[key] = md.extra[key]
            records.append(record)
        df = pd.DataFrame(records)
        return self.compute_features(df)

    def get_feature_names(self) -> List[str]:
        """Return the list of all feature column names this pipeline can produce."""
        return list(self._FEATURE_NAMES)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure a ``close`` column exists (aliased from ``price`` if needed)."""
        if "close" not in df.columns and "price" in df.columns:
            df["close"] = df["price"]
        if "close" not in df.columns:
            raise ValueError("DataFrame must contain a 'close' or 'price' column.")
        return df
