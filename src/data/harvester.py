"""
Historical Kalshi market data harvester.

Downloads settled/finalized contracts and outcomes from the Kalshi API,
stores them in both SQLite (via MarketStore) and Parquet files partitioned
by market type and date.

Sprint 1 — Task 1.3
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src.data.kalshi_provider import KalshiProvider
from src.data.market_store import MarketStore

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_HISTORICAL_DIR = _PROJECT_ROOT / "data" / "historical"

# Kalshi API rate limit: stay under 10 req/s
_MIN_REQUEST_INTERVAL = 0.12  # seconds between requests

# Retry configuration
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds; exponential backoff multiplier

# Weather series tickers for major cities
WEATHER_SERIES = [
    "KXHIGHNY",  # New York
    "KXHIGHLA",  # Los Angeles
    "KXHIGHCH",  # Chicago
    "KXHIGHMI",  # Miami
]

# Settled market statuses (Kalshi uses several for closed markets)
_SETTLED_STATUSES = ("settled", "finalized", "closed")


class HistoricalHarvester:
    """Batch downloader for historical Kalshi market data.

    Fetches settled contracts via the KalshiProvider search API,
    persists settlements to MarketStore (SQLite) and saves raw
    market dicts to Parquet files under ``data/historical/``.
    """

    def __init__(
        self,
        kalshi_provider: KalshiProvider,
        store: MarketStore = None,
    ):
        self.provider = kalshi_provider
        self.store = store or MarketStore()
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Sleep if needed to stay under API rate limit."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.monotonic()

    # ------------------------------------------------------------------
    # Core harvest logic
    # ------------------------------------------------------------------

    def harvest_settled_markets(
        self,
        series_ticker: str,
        limit: int = 1000,
    ) -> List[dict]:
        """Download all settled/finalized/closed markets for a series.

        Paginates through the Kalshi search API, stores each settlement
        in MarketStore, and returns the full list of raw market dicts.

        Parameters
        ----------
        series_ticker : str
            Kalshi series ticker (e.g. ``"KXBTC15M"``).
        limit : int
            Page size per API request (max 1000).

        Returns
        -------
        list[dict]
            All harvested market dicts from the API.
        """
        all_markets: List[dict] = []
        cursor: Optional[str] = None
        page = 0

        logger.info(
            "[Harvester] Starting harvest for series=%s (settled/finalized/closed)",
            series_ticker,
        )

        while True:
            page += 1
            params: Dict[str, object] = {
                "series_ticker": series_ticker,
                "status": "settled",
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor

            markets, next_cursor = self._search_with_retry(params)

            if not markets:
                # Also try "finalized" and "closed" on the first empty page
                # if we got nothing at all yet
                if not all_markets:
                    for alt_status in ("finalized", "closed"):
                        params["status"] = alt_status
                        markets, next_cursor = self._search_with_retry(params)
                        if markets:
                            break
                if not markets:
                    break

            all_markets.extend(markets)
            logger.info(
                "[Harvester] %s — page %d: fetched %d markets (total: %d)",
                series_ticker,
                page,
                len(markets),
                len(all_markets),
            )

            # Persist each settlement to SQLite
            for mkt in markets:
                self._store_settlement(mkt)

            # Pagination: stop when no next cursor or we got fewer than limit
            if not next_cursor or len(markets) < limit:
                break
            cursor = next_cursor

        logger.info(
            "[Harvester] Finished %s — %d markets harvested",
            series_ticker,
            len(all_markets),
        )
        return all_markets

    def _search_with_retry(self, params: dict) -> tuple:
        """Call provider.search_markets with throttling and retries.

        Returns (markets_list, next_cursor).
        """
        for attempt in range(1, _MAX_RETRIES + 1):
            self._throttle()
            try:
                result = self.provider.search_markets(**params)
                # search_markets returns (list, cursor)
                if isinstance(result, tuple) and len(result) == 2:
                    return result
                # Defensive: if it returns just a list
                return (result if isinstance(result, list) else []), None
            except Exception as exc:
                wait = _RETRY_BACKOFF_BASE**attempt
                logger.warning(
                    "[Harvester] search_markets attempt %d/%d failed: %s — "
                    "retrying in %.1fs",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)

        logger.error("[Harvester] search_markets failed after %d retries", _MAX_RETRIES)
        return [], None

    def _store_settlement(self, market: dict) -> None:
        """Persist a single market's settlement to MarketStore."""
        market_id = market.get("ticker") or market.get("ticker_name") or ""
        outcome = market.get("result", market.get("outcome", "unknown"))
        settlement_time = (
            market.get("close_time")
            or market.get("settlement_time")
            or market.get("expiration_time")
            or datetime.now(timezone.utc).isoformat()
        )

        # Settlement value: try last_price_dollars first, then last_price
        settlement_value = None
        lp_dollars = market.get("last_price_dollars")
        if lp_dollars is not None:
            try:
                settlement_value = float(lp_dollars)
            except (TypeError, ValueError):
                pass
        if settlement_value is None:
            lp = market.get("last_price")
            if lp is not None:
                try:
                    settlement_value = float(lp) / 100.0
                except (TypeError, ValueError):
                    pass

        try:
            self.store.insert_settlement(
                market_id=market_id,
                outcome=str(outcome),
                settlement_time=settlement_time,
                settlement_value=settlement_value,
            )
        except Exception as exc:
            logger.warning(
                "[Harvester] Failed to store settlement for %s: %s",
                market_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Market-specific harvesters
    # ------------------------------------------------------------------

    def harvest_btc_15m(self, months_back: int = 6) -> int:
        """Harvest settled BTC 15-minute markets.

        Parameters
        ----------
        months_back : int
            How many months of history to target (used for logging;
            the API returns all settled markets for the series).

        Returns
        -------
        int
            Number of markets harvested.
        """
        logger.info(
            "[Harvester] Harvesting BTC 15m markets (target: %d months back)",
            months_back,
        )
        markets = self.harvest_settled_markets(series_ticker="KXBTC15M")

        if markets:
            self.save_to_parquet(markets, market_type="btc_15m")

        logger.info("[Harvester] BTC 15m harvest complete: %d markets", len(markets))
        return len(markets)

    def harvest_weather(self, months_back: int = 6) -> int:
        """Harvest settled weather markets for major cities.

        Parameters
        ----------
        months_back : int
            Target history depth (for logging).

        Returns
        -------
        int
            Total number of weather markets harvested across all cities.
        """
        logger.info(
            "[Harvester] Harvesting weather markets (target: %d months back)",
            months_back,
        )
        total = 0
        all_weather_markets: List[dict] = []

        for series in WEATHER_SERIES:
            markets = self.harvest_settled_markets(series_ticker=series)
            all_weather_markets.extend(markets)
            total += len(markets)
            logger.info("[Harvester] %s: %d markets", series, len(markets))

        if all_weather_markets:
            self.save_to_parquet(all_weather_markets, market_type="weather")

        logger.info("[Harvester] Weather harvest complete: %d total markets", total)
        return total

    def harvest_all(self, months_back: int = 6) -> dict:
        """Run all harvesters and return a summary.

        Returns
        -------
        dict
            Keys: ``btc_15m``, ``weather``, ``total``, ``timestamp``.
        """
        logger.info(
            "[Harvester] === Full harvest starting (months_back=%d) ===", months_back
        )
        start = time.monotonic()

        btc_count = self.harvest_btc_15m(months_back=months_back)
        weather_count = self.harvest_weather(months_back=months_back)

        elapsed = time.monotonic() - start
        summary = {
            "btc_15m": btc_count,
            "weather": weather_count,
            "total": btc_count + weather_count,
            "elapsed_seconds": round(elapsed, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "[Harvester] === Full harvest complete: %d markets in %.1fs ===",
            summary["total"],
            elapsed,
        )
        return summary

    # ------------------------------------------------------------------
    # Parquet I/O
    # ------------------------------------------------------------------

    def save_to_parquet(
        self,
        data: List[dict],
        market_type: str,
        date_str: str = None,
    ) -> Path:
        """Save harvested market data to a Parquet file.

        Files are written to ``data/historical/{market_type}/{date}.parquet``.

        Parameters
        ----------
        data : list[dict]
            Raw market dicts from the Kalshi API.
        market_type : str
            Subdirectory name (e.g. ``"btc_15m"``, ``"weather"``).
        date_str : str, optional
            Filename stem. Defaults to today's date (``YYYY-MM-DD``).

        Returns
        -------
        Path
            The written Parquet file path.
        """
        if not data:
            logger.warning("[Harvester] save_to_parquet called with empty data")
            return None

        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        out_dir = _HISTORICAL_DIR / market_type
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_str}.parquet"

        df = pd.DataFrame(data)

        # Normalize column types so Parquet doesn't choke on mixed types
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, (dict, list))).any():
                df[col] = df[col].apply(
                    lambda x: str(x) if isinstance(x, (dict, list)) else x
                )

        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, str(out_path))

        logger.info(
            "[Harvester] Saved %d records to %s",
            len(data),
            out_path,
        )
        return out_path

    @staticmethod
    def load_from_parquet(
        market_type: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame:
        """Load historical data from Parquet files.

        Reads all ``.parquet`` files under
        ``data/historical/{market_type}/`` and optionally filters by
        the date encoded in filenames (``YYYY-MM-DD.parquet``).

        Parameters
        ----------
        market_type : str
            Subdirectory name (e.g. ``"btc_15m"``, ``"weather"``).
        start_date : str, optional
            Inclusive lower bound (``YYYY-MM-DD``).
        end_date : str, optional
            Inclusive upper bound (``YYYY-MM-DD``).

        Returns
        -------
        pd.DataFrame
            Combined dataframe from all matching Parquet files.
        """
        parquet_dir = _HISTORICAL_DIR / market_type

        if not parquet_dir.exists():
            logger.warning("[Harvester] Directory does not exist: %s", parquet_dir)
            return pd.DataFrame()

        files = sorted(parquet_dir.glob("*.parquet"))
        if not files:
            logger.warning("[Harvester] No parquet files in %s", parquet_dir)
            return pd.DataFrame()

        # Filter files by date in filename
        selected: List[Path] = []
        for f in files:
            file_date = f.stem  # e.g. "2025-09-18"
            if start_date and file_date < start_date:
                continue
            if end_date and file_date > end_date:
                continue
            selected.append(f)

        if not selected:
            logger.warning(
                "[Harvester] No parquet files match date range [%s, %s] in %s",
                start_date,
                end_date,
                parquet_dir,
            )
            return pd.DataFrame()

        frames = [pd.read_parquet(str(f)) for f in selected]
        combined = pd.concat(frames, ignore_index=True)

        logger.info(
            "[Harvester] Loaded %d records from %d files (%s)",
            len(combined),
            len(selected),
            market_type,
        )
        return combined
