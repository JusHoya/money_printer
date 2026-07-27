"""Backtest data loader — reconstructs MarketData for offline replay.

Two sources:

* **Parquet settlements** (Sprint 5, Task 5.2) — one row per settled market.
* **Harvest CSVs** (``logs/data_*.csv``, PRD FR-0.7) — the per-tick ladder the
  VM records and that Phase 2 calibration and the Phase 2 go/no-go EV report
  replay.

Both paths carry the FR-1.1 bracket semantics (``strike_type`` /
``floor_strike`` / ``cap_strike``) onto ``MarketData.extra`` so
:func:`src.core.bracket_payoff.parse_bracket_spec` works on replayed data with
no metadata re-fetch. Rows harvested before those columns existed cannot be
settled offline; they are COUNTED and reported, never guessed at from the
ticker string.

Sprint 5, Task 5.2 / PRD FR-1.1.
"""

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from src.core.bracket_payoff import (
    BracketSpec,
    BracketSpecError,
    is_weather_symbol,
    parse_bracket_spec,
)
from src.core.interfaces import MarketData

logger = logging.getLogger(__name__)

_HISTORICAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "historical"

# Harvest-CSV column names carrying FR-1.1 bracket semantics. Absent from any
# file written before 2026-07-25; their absence is the "unusable for brackets"
# condition, not a reason to infer anything.
HARVEST_BRACKET_COLUMNS = ("StrikeType", "FloorStrike", "CapStrike")

# The dashboard records market rows under "<TICKER> (Market)".
MARKET_ROW_SUFFIX = " (Market)"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val)
        return v if not np.isnan(v) else default
    except (TypeError, ValueError):
        return default


def _optional_float(val) -> Optional[float]:
    """Float or None. Blank/absent stays None — never coerced to 0.0."""
    if val is None:
        return None
    if isinstance(val, str) and not val.strip():
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) else v


def _parse_strike_from_ticker(ticker: str) -> Optional[float]:
    """Legacy numeric-strike scrape for the crypto Parquet path ONLY.

    It yields a magnitude, never a direction: no caller may derive contract
    direction from it (PRD FR-1.1 / Phase 1 exit criterion 2). Weather bracket
    semantics come exclusively from the API fields.
    """
    try:
        parts = str(ticker).split("-")
        return float(re.sub(r"[A-Za-z]", "", parts[-1]))
    except Exception:
        return None


def strip_market_suffix(symbol: Any) -> str:
    """``"KXHIGHNY-26JUL25-B86.5 (Market)"`` -> ``"KXHIGHNY-26JUL25-B86.5"``."""
    s = str(symbol or "")
    return s[: -len(MARKET_ROW_SUFFIX)] if s.endswith(MARKET_ROW_SUFFIX) else s


def bracket_extra_from_row(row: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """FR-1.1 bracket fields for ``MarketData.extra`` from one harvest row.

    Missing columns and blank cells both become ``None`` so that
    ``parse_bracket_spec`` raises :class:`BracketSpecError` — the correct
    fail-loud behaviour for a row harvested before the columns existed.
    """
    raw_type = row.get("StrikeType")
    if raw_type is None or (isinstance(raw_type, float) and np.isnan(raw_type)):
        strike_type = None
    else:
        strike_type = str(raw_type).strip().lower() or None
    return {
        "strike_type": strike_type,
        "floor_strike": _optional_float(row.get("FloorStrike")),
        "cap_strike": _optional_float(row.get("CapStrike")),
    }


@dataclass
class HarvestBracketStats:
    """Bracket-usability tally for a replayed harvest.

    ``unusable`` rows are weather market rows whose bracket semantics could not
    be established. They are reported explicitly rather than skipped in
    silence: an unexplained row-count drop is how a replay quietly stops being
    the thing it claims to measure.
    """

    files: int = 0
    files_missing_columns: List[str] = field(default_factory=list)
    market_rows: int = 0
    weather_rows: int = 0
    usable: int = 0
    unusable: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def note_failure(self, exc: Exception) -> None:
        self.unusable += 1
        key = str(exc).split(":", 1)[-1].strip()[:80] or exc.__class__.__name__
        self.reasons[key] = self.reasons.get(key, 0) + 1

    def report_lines(self) -> List[str]:
        lines = [
            f"Harvest bracket coverage: {self.usable}/{self.weather_rows} "
            f"weather rows settleable offline "
            f"({self.market_rows} market rows across {self.files} file(s))"
        ]
        if self.unusable:
            lines.append(
                f"{self.unusable} rows unusable: harvested before bracket "
                f"columns existed (PRD FR-1.1) — they cannot be settled "
                f"offline and are excluded from bracket analysis"
            )
            for reason, count in sorted(self.reasons.items(), key=lambda kv: -kv[1])[
                :3
            ]:
                lines.append(f"    {count:>7,} x {reason}")
        if self.files_missing_columns:
            shown = ", ".join(self.files_missing_columns[:3])
            more = (
                f" (+{len(self.files_missing_columns) - 3} more)"
                if len(self.files_missing_columns) > 3
                else ""
            )
            lines.append(f"Legacy (pre-bracket) harvest files: {shown}{more}")
        return lines


def harvest_row_to_market_data(row: Mapping[str, Any]) -> MarketData:
    """Rebuild a :class:`MarketData` from one harvest-CSV row.

    ``extra`` carries the FR-1.1 bracket fields verbatim (``None`` when the
    row predates them) plus the quote columns FR-0.7 records.
    """
    symbol = strip_market_suffix(row.get("Symbol"))
    ts = row.get("Timestamp")
    if not isinstance(ts, datetime):
        try:
            ts = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            ts = datetime.now()

    price = _safe_float(row.get("Price"), 0.0)
    bid = _safe_float(row.get("Bid"), price)
    ask = _safe_float(row.get("Ask"), price)

    extra: Dict[str, Any] = {
        "source": "harvest_csv",
        "raw_symbol": str(row.get("Symbol") or ""),
        "no_bid": _safe_float(row.get("NoBid"), 0.0),
        "no_ask": _safe_float(row.get("NoAsk"), 0.0),
        "last": _optional_float(row.get("Last")),
    }
    extra.update(bracket_extra_from_row(row))
    # Legacy alias: crypto-era consumers read extra["strike"]. It mirrors
    # floor_strike when the API supplied one and is otherwise absent — it is
    # never reconstructed from the ticker (PRD FR-1.1).
    if extra["floor_strike"] is not None:
        extra["strike"] = extra["floor_strike"]

    return MarketData(
        symbol=symbol,
        timestamp=ts,
        price=price,
        volume=_safe_float(row.get("Volume"), 0.0),
        bid=bid,
        ask=ask,
        extra=extra,
    )


def spec_for_harvest_row(
    row: Mapping[str, Any], stats: Optional[HarvestBracketStats] = None
) -> Optional[BracketSpec]:
    """Bracket spec for one harvest row, or ``None`` if it cannot be built.

    The ``BracketSpecError`` is caught HERE (the call site) and tallied, per
    PRD FR-1.1: a pre-bracket row must be reported as unusable, not silently
    dropped and not guessed at.
    """
    symbol = strip_market_suffix(row.get("Symbol"))
    try:
        spec = parse_bracket_spec(symbol, bracket_extra_from_row(row))
    except BracketSpecError as exc:
        if stats is not None:
            stats.note_failure(exc)
        return None
    if stats is not None:
        stats.usable += 1
    return spec


def load_harvest_csv(
    path,
    stats: Optional[HarvestBracketStats] = None,
    market_rows_only: bool = True,
) -> Tuple[List[MarketData], HarvestBracketStats]:
    """Read one ``logs/data_*.csv`` harvest file into ``MarketData`` objects.

    Reads by column NAME (``csv.DictReader``), so a legacy narrow file — one
    written before the FR-1.1 bracket columns were appended — parses without
    raising; its rows simply carry no bracket fields and are counted as
    unusable-for-brackets in the returned :class:`HarvestBracketStats`.
    """
    stats = stats if stats is not None else HarvestBracketStats()
    path = Path(path)
    out: List[MarketData] = []
    if not path.exists():
        logger.warning("Harvest CSV not found: %s", path)
        return out, stats

    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        stats.files += 1
        if not all(c in header for c in HARVEST_BRACKET_COLUMNS):
            stats.files_missing_columns.append(path.name)
        for row in reader:
            if market_rows_only and row.get("Type") != "MARKET_DATA":
                continue
            raw_symbol = str(row.get("Symbol") or "")
            if market_rows_only and not raw_symbol.endswith(MARKET_ROW_SUFFIX):
                continue  # observation/temperature rows, not markets
            stats.market_rows += 1
            md = harvest_row_to_market_data(row)
            if is_weather_symbol(md.symbol):
                stats.weather_rows += 1
                spec_for_harvest_row(row, stats)
            out.append(md)
    return out, stats


def load_harvest_dir(
    log_dir, pattern: str = "data_*.csv"
) -> Tuple[List[MarketData], HarvestBracketStats]:
    """Load every harvest CSV under *log_dir*, chronologically."""
    stats = HarvestBracketStats()
    rows: List[MarketData] = []
    for p in sorted(Path(log_dir).glob(pattern)):
        loaded, stats = load_harvest_csv(p, stats=stats)
        rows.extend(loaded)
    rows.sort(key=lambda md: md.timestamp)
    return rows, stats


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
        floor_strike = _optional_float(row.get("floor_strike"))
        cap_strike = _optional_float(row.get("cap_strike"))
        strike_type = row.get("strike_type")
        if strike_type is not None and str(strike_type).strip():
            strike_type = str(strike_type).strip().lower()
        else:
            strike_type = None
        # Legacy numeric strike for the crypto replay path. Direction is NEVER
        # derived from it (FR-1.1); bracket semantics come from strike_type.
        strike = (floor_strike or 0.0) or _parse_strike_from_ticker(ticker)
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
                # PRD FR-1.1: bracket semantics travel with the replayed row so
                # parse_bracket_spec never has to re-fetch market metadata.
                "strike_type": strike_type,
                "floor_strike": floor_strike,
                "cap_strike": cap_strike,
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
        bracket_blind = 0
        for _, row in df.iterrows():
            try:
                md, outcome = self.reconstruct_market_data(row)
                if md.extra.get("strike") and outcome["result"] in ("yes", "no"):
                    sequence.append((md, outcome))
                    if is_weather_symbol(md.symbol):
                        try:
                            parse_bracket_spec(md.symbol, md.extra)
                        except BracketSpecError:
                            bracket_blind += 1
            except Exception as exc:
                logger.debug("Skipping row: %s", exc)

        logger.info(
            "Built replay sequence: %d markets from %s", len(sequence), market_type
        )
        if bracket_blind:
            # Fail loud, not silent: these rows cannot be settled through
            # bracket_payoff and must not be counted as if they could.
            logger.warning(
                "%d rows unusable: harvested before bracket columns existed "
                "(no strike_type/floor_strike/cap_strike in %s)",
                bracket_blind,
                market_type,
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
