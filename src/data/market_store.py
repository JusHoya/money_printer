"""
Thread-safe SQLite-backed store for tick data, OHLCV candles,
orderbook snapshots, and settlement records.

Uses WAL journal mode for concurrent reads and threading.local()
for per-thread connections.
"""

import json
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Default database path relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "market_store.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    price      REAL    NOT NULL,
    bid        REAL,
    ask        REAL,
    volume     REAL,
    source     TEXT
);

CREATE TABLE IF NOT EXISTS candles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     REAL,
    period     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbooks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    bids_json  TEXT    NOT NULL,
    asks_json  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id        TEXT    NOT NULL,
    outcome          TEXT    NOT NULL,
    settlement_time  TEXT    NOT NULL,
    settlement_value REAL
);

CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts       ON ticks(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_ticks_ts              ON ticks(timestamp);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_period  ON candles(symbol, period, timestamp);
CREATE INDEX IF NOT EXISTS idx_candles_ts             ON candles(timestamp);
CREATE INDEX IF NOT EXISTS idx_orderbooks_symbol_ts   ON orderbooks(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_settlements_market     ON settlements(market_id);
CREATE INDEX IF NOT EXISTS idx_settlements_time       ON settlements(settlement_time);
"""


def _to_iso(ts) -> str:
    """Convert a datetime or string to ISO 8601 UTC string."""
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    raise TypeError(f"Expected datetime or str, got {type(ts)}")


class MarketStore:
    """Thread-safe SQLite store for market data.

    Each thread gets its own sqlite3 connection via threading.local().
    The database uses WAL journal mode so readers never block writers.
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialize schema on the calling thread's connection
        self._init_schema()
        logger.info("[MarketStore] Initialized at %s", self._db_path)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return the per-thread connection, creating it if needed."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    def close(self) -> None:
        """Close the current thread's connection (if any)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Insert methods
    # ------------------------------------------------------------------

    def insert_tick(
        self,
        symbol: str,
        timestamp,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        volume: Optional[float] = None,
        source: Optional[str] = None,
    ) -> None:
        """Insert a single tick record."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO ticks (timestamp, symbol, price, bid, ask, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_to_iso(timestamp), symbol, price, bid, ask, volume, source),
        )
        conn.commit()

    def insert_ticks_batch(self, ticks: List[Dict[str, Any]]) -> int:
        """Bulk-insert tick records.

        Each dict must have at least 'symbol', 'timestamp', 'price'.
        Optional keys: 'bid', 'ask', 'volume', 'source'.

        Returns the number of rows inserted.
        """
        if not ticks:
            return 0
        rows = [
            (
                _to_iso(t["timestamp"]),
                t["symbol"],
                t["price"],
                t.get("bid"),
                t.get("ask"),
                t.get("volume"),
                t.get("source"),
            )
            for t in ticks
        ]
        conn = self._get_conn()
        conn.executemany(
            "INSERT INTO ticks (timestamp, symbol, price, bid, ask, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)

    def insert_candle(
        self,
        symbol: str,
        timestamp,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: Optional[float] = None,
        period: str = "1m",
    ) -> None:
        """Insert a single OHLCV candle."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO candles (timestamp, symbol, open, high, low, close, volume, period) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_to_iso(timestamp), symbol, open_, high, low, close, volume, period),
        )
        conn.commit()

    def insert_orderbook(
        self,
        symbol: str,
        timestamp,
        bids: Any,
        asks: Any,
    ) -> None:
        """Insert an orderbook snapshot.

        `bids` and `asks` can be any JSON-serializable structure
        (typically list of [price, size] pairs).
        """
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO orderbooks (timestamp, symbol, bids_json, asks_json) "
            "VALUES (?, ?, ?, ?)",
            (
                _to_iso(timestamp),
                symbol,
                json.dumps(bids),
                json.dumps(asks),
            ),
        )
        conn.commit()

    def insert_settlement(
        self,
        market_id: str,
        outcome: str,
        settlement_time,
        settlement_value: Optional[float] = None,
    ) -> None:
        """Insert a settlement record for a resolved market."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO settlements (market_id, outcome, settlement_time, settlement_value) "
            "VALUES (?, ?, ?, ?)",
            (market_id, outcome, _to_iso(settlement_time), settlement_value),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Query methods — all return lists of dicts (via sqlite3.Row)
    # ------------------------------------------------------------------

    @staticmethod
    def _rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        return [dict(row) for row in rows]

    def get_ticks(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query ticks for a symbol within an optional time range.

        Parameters
        ----------
        symbol : str
            Market symbol to query.
        start, end : str, optional
            ISO 8601 UTC bounds (inclusive).
        limit : int, optional
            Maximum number of rows to return (most recent first when
            used without a time range).
        """
        clauses = ["symbol = ?"]
        params: list = [symbol]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)

        sql = (
            f"SELECT * FROM ticks WHERE {' AND '.join(clauses)} ORDER BY timestamp ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def get_latest_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the most recent tick for a symbol, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM ticks WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None

    def get_candles(
        self,
        symbol: str,
        period: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query stored candles for a symbol and period."""
        clauses = ["symbol = ?", "period = ?"]
        params: list = [symbol, period]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)

        sql = f"SELECT * FROM candles WHERE {' AND '.join(clauses)} ORDER BY timestamp ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def get_settlements(
        self,
        market_type: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query settlement records.

        Parameters
        ----------
        market_type : str, optional
            If provided, filters market_id with a LIKE prefix match
            (e.g. 'KXBTC' matches all BTC markets).
        start, end : str, optional
            ISO 8601 UTC bounds on settlement_time.
        """
        clauses: List[str] = []
        params: list = []
        if market_type is not None:
            clauses.append("market_id LIKE ?")
            params.append(f"{market_type}%")
        if start is not None:
            clauses.append("settlement_time >= ?")
            params.append(start)
        if end is not None:
            clauses.append("settlement_time <= ?")
            params.append(end)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM settlements{where} ORDER BY settlement_time ASC"

        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def build_candles(
        self,
        symbol: str,
        period: str,
        start: str,
        end: str,
    ) -> List[Dict[str, Any]]:
        """Aggregate stored ticks into OHLCV candles and persist them.

        Supported periods: '1m', '5m', '15m', '1h', '4h', '1d'.
        Returns the list of generated candle dicts.
        """
        period_minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "1h": 60,
            "4h": 240,
            "1d": 1440,
        }
        minutes = period_minutes.get(period)
        if minutes is None:
            raise ValueError(
                f"Unsupported period '{period}'. "
                f"Choose from {list(period_minutes.keys())}"
            )

        ticks = self.get_ticks(symbol, start=start, end=end)
        if not ticks:
            return []

        # Group ticks into buckets by flooring timestamp to the period
        period_seconds = minutes * 60
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for tick in ticks:
            ts = datetime.fromisoformat(tick["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            epoch = int(ts.timestamp())
            bucket_epoch = epoch - (epoch % period_seconds)
            bucket_key = datetime.fromtimestamp(
                bucket_epoch, tz=timezone.utc
            ).isoformat()
            buckets.setdefault(bucket_key, []).append(tick)

        candles: List[Dict[str, Any]] = []
        for bucket_ts in sorted(buckets.keys()):
            group = buckets[bucket_ts]
            prices = [t["price"] for t in group]
            volumes = [t["volume"] for t in group if t.get("volume") is not None]
            candle = {
                "timestamp": bucket_ts,
                "symbol": symbol,
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": sum(volumes) if volumes else None,
                "period": period,
            }
            candles.append(candle)
            self.insert_candle(
                symbol=symbol,
                timestamp=bucket_ts,
                open_=candle["open"],
                high=candle["high"],
                low=candle["low"],
                close=candle["close"],
                volume=candle["volume"],
                period=period,
            )

        logger.info(
            "[MarketStore] Built %d %s candles for %s (%s → %s)",
            len(candles),
            period,
            symbol,
            start,
            end,
        )
        return candles

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def tick_count(self, symbol: Optional[str] = None) -> int:
        """Return the number of ticks, optionally filtered by symbol."""
        conn = self._get_conn()
        if symbol is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM ticks WHERE symbol = ?", (symbol,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM ticks").fetchone()
        return row["cnt"]

    def candle_count(self, symbol: Optional[str] = None) -> int:
        """Return the number of candles, optionally filtered by symbol."""
        conn = self._get_conn()
        if symbol is not None:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM candles WHERE symbol = ?", (symbol,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM candles").fetchone()
        return row["cnt"]
