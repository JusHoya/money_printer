"""Backtest data loader — reconstructs MarketData from Parquet settlements.

Sprint 5, Task 5.2.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from src.core.interfaces import MarketData

logger = logging.getLogger(__name__)

_HISTORICAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "historical"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val)
        return v if not np.isnan(v) else default
    except (TypeError, ValueError):
        return default


def _parse_strike_from_ticker(ticker: str) -> Optional[float]:
    try:
        parts = str(ticker).split("-")
        return float(re.sub(r"[A-Za-z]", "", parts[-1]))
    except Exception:
        return None


class BacktestDataLoader:
    """Loads Parquet settlement data and reconstructs MarketData objects.

    Each Parquet row becomes a ``(MarketData, outcome_dict)`` pair
    suitable for replay by :class:`BacktestEngine`.
    """

    def __init__(self, data_dir: str = None):
        self.data_dir = Path(data_dir) if data_dir else _HISTORICAL_DIR

    def load_raw(
        self, market_type: str, start_date: str = None, end_date: str = None
    ) -> pd.DataFrame:
        """Load raw Parquet data for a market type."""
        from src.data.harvester import HistoricalHarvester

        # Temporarily override the harvester's data dir
        import src.data.harvester as hmod

        original = hmod._HISTORICAL_DIR
        hmod._HISTORICAL_DIR = self.data_dir
        try:
            df = HistoricalHarvester.load_from_parquet(
                market_type, start_date, end_date
            )
        finally:
            hmod._HISTORICAL_DIR = original
        return df

    def reconstruct_market_data(self, row: pd.Series) -> Tuple[MarketData, dict]:
        """Convert one Parquet row into (MarketData, outcome_dict).

        Uses ``previous_yes_bid/ask_dollars`` for pre-settlement prices.
        Falls back to ``last_price_dollars`` with synthetic spread.
        """
        ticker = str(row.get("ticker", "UNKNOWN"))
        strike = _safe_float(row.get("floor_strike"), 0.0) or _parse_strike_from_ticker(
            ticker
        )
        spot = _safe_float(row.get("expiration_value"), 0.0)

        # Pre-settlement bid/ask (preferred) or synthetic from last_price
        prev_bid = _safe_float(row.get("previous_yes_bid_dollars"), 0.0)
        prev_ask = _safe_float(row.get("previous_yes_ask_dollars"), 0.0)
        last_price = _safe_float(row.get("last_price_dollars"), 0.0)

        if prev_bid > 0 and prev_ask > 0:
            bid, ask = prev_bid, prev_ask
        elif last_price > 0:
            bid = max(0.01, last_price - 0.02)
            ask = min(0.99, last_price + 0.02)
        else:
            bid, ask = 0.45, 0.55  # Conservative fallback

        # Timestamps
        open_time = row.get("open_time", "")
        close_time = row.get("close_time", "")

        try:
            open_dt = datetime.fromisoformat(str(open_time).replace("Z", "+00:00"))
            open_dt = open_dt.replace(tzinfo=None)  # Strip tz for strategy compat
        except Exception:
            open_dt = datetime.now()
        try:
            close_dt = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
            close_dt = close_dt.replace(tzinfo=None)
        except Exception:
            close_dt = open_dt

        # Entry timestamp: midpoint of contract life
        entry_dt = open_dt + (close_dt - open_dt) / 2
        tte_s = max(1.0, (close_dt - entry_dt).total_seconds())

        # NO side prices
        no_bid = _safe_float(
            row.get("no_bid_dollars") or row.get("previous_no_bid_dollars"), 0.0
        )
        no_ask = _safe_float(
            row.get("no_ask_dollars") or row.get("previous_no_ask_dollars"), 0.0
        )

        md = MarketData(
            symbol=ticker,
            timestamp=entry_dt,
            price=spot,
            volume=_safe_float(row.get("volume_fp"), 0.0),
            bid=bid,
            ask=ask,
            extra={
                "strike": strike,
                "spot_price": spot,
                "close_time": close_dt,
                "time_to_expiry": tte_s,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "open_interest": _safe_float(row.get("open_interest_fp"), 0.0),
                "source": "backtest",
            },
        )

        outcome = {
            "result": str(row.get("result", "unknown")).lower(),
            "expiration_value": spot,
            "close_time": close_dt,
        }

        return md, outcome

    def build_replay_sequence(
        self,
        market_type: str,
        start_date: str = None,
        end_date: str = None,
    ) -> List[Tuple[MarketData, dict]]:
        """Load all records, sort chronologically, return replay sequence."""
        df = self.load_raw(market_type, start_date, end_date)
        if df.empty:
            logger.warning("No data for market_type=%s", market_type)
            return []

        # Sort by close_time for chronological replay
        if "close_time" in df.columns:
            df = df.sort_values("close_time").reset_index(drop=True)

        sequence = []
        for _, row in df.iterrows():
            try:
                md, outcome = self.reconstruct_market_data(row)
                if md.extra.get("strike") and outcome["result"] in ("yes", "no"):
                    sequence.append((md, outcome))
            except Exception as exc:
                logger.debug("Skipping row: %s", exc)

        logger.info(
            "Built replay sequence: %d markets from %s", len(sequence), market_type
        )
        return sequence

    def build_multi_market_sequence(
        self, market_types: List[str] = None
    ) -> List[Tuple[MarketData, dict]]:
        """Load and merge multiple market types, sorted chronologically."""
        if market_types is None:
            market_types = ["btc_15m", "btc_hourly"]
            # Add any weather_* dirs
            for d in sorted(self.data_dir.iterdir()):
                if d.is_dir() and d.name.startswith("weather"):
                    market_types.append(d.name)

        all_events = []
        for mt in market_types:
            all_events.extend(self.build_replay_sequence(mt))

        # Sort by timestamp
        all_events.sort(key=lambda x: x[0].timestamp)
        logger.info("Combined replay: %d events from %s", len(all_events), market_types)
        return all_events
