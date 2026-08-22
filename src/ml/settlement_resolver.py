"""Background resolver for unsettled Kalshi contracts.

Queues ambiguous contract symbols and resolves them in batches via the
Kalshi API, caching results in settlement_cache.json for future training runs.
Designed to run safely alongside live trading without disrupting other components.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SettlementResolver:
    """Background resolver that periodically checks unsettled contracts via Kalshi API.

    Queues ambiguous contract symbols and resolves them in batches,
    caching results in settlement_cache.json for future training runs.
    """

    def __init__(
        self,
        kalshi_provider=None,
        cache_path="data/models/settlement_cache.json",
    ):
        self._kalshi = kalshi_provider
        self._cache_path = cache_path
        self._pending: List[str] = []
        self._cache: Dict[str, int] = {}
        self._resolved_count = 0
        self._error_count = 0
        self._load_cache()

    def _load_cache(self):
        """Load settlement cache from disk."""
        if not os.path.exists(self._cache_path):
            logger.debug(
                "No settlement cache found at %s, starting fresh", self._cache_path
            )
            return
        try:
            with open(self._cache_path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = data
                logger.info(
                    "Loaded %d cached settlements from %s",
                    len(self._cache),
                    self._cache_path,
                )
            else:
                logger.warning("Settlement cache has unexpected format, starting fresh")
                self._cache = {}
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load settlement cache: %s", e)
            self._cache = {}

    def _save_cache(self):
        """Persist settlement cache to disk."""
        try:
            cache_dir = os.path.dirname(self._cache_path)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._cache, f, indent=2)
            logger.debug(
                "Saved %d settlements to %s", len(self._cache), self._cache_path
            )
        except IOError as e:
            logger.error("Failed to save settlement cache: %s", e)

    def queue_ambiguous(self, symbols: List[str]):
        """Add symbols that need resolution. Skips already-cached ones."""
        added = 0
        for symbol in symbols:
            if symbol in self._cache:
                continue
            if symbol in self._pending:
                continue
            self._pending.append(symbol)
            added += 1
        if added:
            logger.info(
                "Queued %d new symbols for resolution (%d already cached, %d total pending)",
                added,
                len(symbols) - added,
                len(self._pending),
            )

    def get_pending_count(self) -> int:
        """Return number of pending unresolved symbols."""
        return len(self._pending)

    def resolve_batch(self, max_queries=50) -> int:
        """Resolve a batch of pending symbols via Kalshi API.

        Queries the Kalshi API for each pending symbol's settlement status.
        Rate-limited to 0.12s between requests (under 10 req/s limit).

        Returns number of newly resolved symbols.
        """
        if not self._kalshi:
            logger.warning("No Kalshi provider configured, cannot resolve settlements")
            return 0

        if not self._pending:
            return 0

        batch = self._pending[:max_queries]
        self._pending = self._pending[max_queries:]
        resolved = 0
        requeue = []

        logger.info(
            "Resolving batch of %d symbols (%d remaining after batch)",
            len(batch),
            len(self._pending),
        )

        for i, symbol in enumerate(batch):
            # Rate limit between requests (not before the first one)
            if i > 0:
                time.sleep(0.12)

            try:
                market_data = self._kalshi._fetch_market_raw(
                    symbol, self._kalshi.PUBLIC_API_URL
                )

                if market_data is None:
                    logger.debug("No data returned for %s, requeuing", symbol)
                    requeue.append(symbol)
                    continue

                status = market_data.get("status", "")
                result = market_data.get("result", "")

                if status in ("settled", "finalized", "closed") and result in (
                    "yes",
                    "no",
                ):
                    label = 1 if result == "yes" else 0
                    self._cache[symbol] = label
                    resolved += 1
                    self._resolved_count += 1
                    logger.debug("Resolved %s: %s (label=%d)", symbol, result, label)
                else:
                    # Not yet settled, put back for next batch
                    requeue.append(symbol)
                    logger.debug(
                        "Symbol %s not yet settled (status=%s, result=%s), requeuing",
                        symbol,
                        status,
                        result,
                    )

            except Exception as e:
                self._error_count += 1
                requeue.append(symbol)
                logger.warning("Error resolving %s: %s", symbol, e)

        # Put unresolved symbols back at the front of the pending queue
        self._pending = requeue + self._pending

        if resolved > 0:
            self._save_cache()
            logger.info(
                "Resolved %d/%d symbols in batch (%d requeued, %d errors total)",
                resolved,
                len(batch),
                len(requeue),
                self._error_count,
            )

        return resolved

    def get_label(self, symbol: str) -> Optional[int]:
        """Get cached label for a symbol, or None if unresolved."""
        return self._cache.get(symbol)

    def get_stats(self) -> dict:
        """Return resolver statistics."""
        return {
            "cached": len(self._cache),
            "pending": len(self._pending),
            "resolved_this_session": self._resolved_count,
            "errors_this_session": self._error_count,
        }
