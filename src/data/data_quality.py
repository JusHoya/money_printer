"""
Data Quality Monitor (Sprint 1, Task 1.7)

Provides staleness detection, gap detection, duplicate filtering, and
timezone normalization for market data feeds. Thread-safe for use in
the multi-bot dashboard environment.
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from src.core.interfaces import MarketData

logger = logging.getLogger(__name__)


class DataQualityMonitor:
    """
    Monitors data quality across all market data feeds.

    Tracks update timestamps per symbol to detect stale feeds, finds
    gaps in time series, deduplicates MarketData records, and normalizes
    timezones to UTC.

    Thread-safe: all state mutations are protected by a lock.
    """

    def __init__(self, staleness_threshold_s: float = 10.0):
        """
        :param staleness_threshold_s: Seconds without an update before a
                                       symbol is considered stale.
        """
        self.staleness_threshold_s = staleness_threshold_s
        self._last_update: Dict[str, datetime] = {}
        self._update_count: Dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Staleness tracking
    # ------------------------------------------------------------------

    def record_update(self, symbol: str, timestamp: datetime) -> None:
        """
        Record that data was received for a symbol.

        :param symbol: Market/feed identifier
        :param timestamp: Timestamp of the received data point
        """
        ts = self.normalize_timezone(timestamp)
        with self._lock:
            self._last_update[symbol] = ts
            self._update_count[symbol] = self._update_count.get(symbol, 0) + 1

    def is_stale(self, symbol: str) -> bool:
        """
        Check if a symbol's data feed is stale.

        :param symbol: Market/feed identifier
        :returns: True if no data received for longer than the staleness
                  threshold, or if the symbol has never been seen.
        """
        with self._lock:
            last = self._last_update.get(symbol)

        if last is None:
            return True

        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed > self.staleness_threshold_s

    def get_stale_symbols(self) -> List[str]:
        """Return all symbols that are currently stale."""
        with self._lock:
            symbols = list(self._last_update.keys())

        return [s for s in symbols if self.is_stale(s)]

    # ------------------------------------------------------------------
    # Gap detection
    # ------------------------------------------------------------------

    def check_gap(
        self,
        symbol: str,
        timestamps: List[datetime],
        max_gap_s: float = 60.0,
    ) -> List[Tuple[datetime, datetime, float]]:
        """
        Find gaps in a timestamp sequence that exceed max_gap_s.

        :param symbol: Symbol name (for logging)
        :param timestamps: List of datetimes to check (need not be sorted)
        :param max_gap_s: Maximum acceptable gap in seconds
        :returns: List of (gap_start, gap_end, gap_seconds) tuples
        """
        if len(timestamps) < 2:
            return []

        # Normalize and sort
        sorted_ts = sorted(self.normalize_timezone(t) for t in timestamps)
        gaps = []

        for i in range(1, len(sorted_ts)):
            delta = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
            if delta > max_gap_s:
                gaps.append((sorted_ts[i - 1], sorted_ts[i], round(delta, 2)))

        if gaps:
            logger.warning(
                "DataQuality: %d gap(s) detected for %s (max_gap=%.1fs)",
                len(gaps),
                symbol,
                max_gap_s,
            )

        return gaps

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def deduplicate(self, data: List[MarketData]) -> List[MarketData]:
        """
        Remove duplicate MarketData entries by (symbol, timestamp).

        Preserves the first occurrence of each unique (symbol, timestamp)
        pair and maintains original ordering.

        :param data: List of MarketData objects
        :returns: Deduplicated list
        """
        seen = set()
        result = []

        for item in data:
            ts = self.normalize_timezone(item.timestamp)
            key = (item.symbol, ts)
            if key not in seen:
                seen.add(key)
                result.append(item)

        removed = len(data) - len(result)
        if removed > 0:
            logger.info(
                "DataQuality: deduplicated %d records -> %d (removed %d)",
                len(data),
                len(result),
                removed,
            )

        return result

    # ------------------------------------------------------------------
    # Timezone normalization
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_timezone(dt: datetime) -> datetime:
        """
        Ensure a datetime is in UTC.

        - Aware datetimes are converted to UTC.
        - Naive datetimes are assumed to already be UTC and get the
          timezone attached.

        :param dt: Input datetime
        :returns: Timezone-aware datetime in UTC
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, dict]:
        """
        Summary of all monitored symbols with staleness info.

        :returns: Dict keyed by symbol, each value is a dict with:
                  - last_update: ISO timestamp of last update (or None)
                  - stale: bool
                  - elapsed_s: seconds since last update (or None)
                  - update_count: total updates recorded
        """
        now = datetime.now(timezone.utc)
        status = {}

        with self._lock:
            symbols = list(self._last_update.keys())
            snapshot = {s: self._last_update[s] for s in symbols}
            counts = {s: self._update_count.get(s, 0) for s in symbols}

        for symbol in symbols:
            last = snapshot[symbol]
            elapsed = (now - last).total_seconds()
            status[symbol] = {
                "last_update": last.isoformat(),
                "stale": elapsed > self.staleness_threshold_s,
                "elapsed_s": round(elapsed, 2),
                "update_count": counts[symbol],
            }

        return status
