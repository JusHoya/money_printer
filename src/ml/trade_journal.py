"""
Trade journal for recording and analyzing trade outcomes.

Persists every closed trade as JSONL for post-hoc analysis:
win/loss breakdowns, confidence calibration, loss-pattern detection.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeOutcome:
    symbol: str
    strategy_name: str
    entry_time: Optional[str] = None  # ISO string
    exit_time: Optional[str] = None  # ISO string
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    side: str = ""
    contract_side: str = "YES"
    pnl: float = 0.0
    close_reason: str = ""
    model_probability: Optional[float] = None
    model_confidence: Optional[float] = None
    model_used: Optional[str] = None
    strike: Optional[float] = None
    tte_at_entry: Optional[float] = None
    btc_spot_at_entry: Optional[float] = None
    prediction_correct: Optional[bool] = None
    edge_at_entry: Optional[float] = None

    @classmethod
    def from_position(cls, position: dict) -> "TradeOutcome":
        """Create a TradeOutcome from a closed position dict.

        Position dicts come from SimulatedExchange.closed_trades and have keys
        like symbol, strategy_name, entry_price, close_time, pnl, reason, etc.
        The optional ml_context sub-dict carries ML model metadata.
        """
        ml = position.get("ml_context") or {}

        # Parse open_time -> ISO string
        open_time = position.get("open_time")
        if isinstance(open_time, datetime):
            entry_time = open_time.isoformat()
        elif isinstance(open_time, str):
            entry_time = open_time
        else:
            entry_time = None

        # Parse close_time -> ISO string
        close_time = position.get("close_time")
        if isinstance(close_time, datetime):
            exit_time = close_time.isoformat()
        elif isinstance(close_time, str):
            exit_time = close_time
        else:
            exit_time = None

        pnl = float(position.get("pnl", 0.0))

        # Determine if prediction was correct based on PnL
        prediction_correct = pnl > 0 if pnl != 0.0 else None

        # Edge at entry: model_probability minus entry_price (how much edge
        # the model thought it had when the trade was opened)
        model_prob = ml.get("model_probability")
        entry_price = float(position.get("entry_price", 0.0))
        edge = None
        if model_prob is not None and entry_price > 0:
            edge = float(model_prob) - entry_price

        return cls(
            symbol=position.get("symbol", ""),
            strategy_name=position.get("strategy_name", "Unknown"),
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=float(position.get("exit_price", 0.0)),
            quantity=float(position.get("quantity", 0.0)),
            side=position.get("side", ""),
            contract_side=position.get("contract_side", "YES"),
            pnl=pnl,
            close_reason=position.get("reason", ""),
            model_probability=float(model_prob) if model_prob is not None else None,
            model_confidence=float(ml["model_confidence"])
            if ml.get("model_confidence") is not None
            else None,
            model_used=ml.get("model_used"),
            strike=float(position["strike"])
            if position.get("strike") is not None
            else None,
            tte_at_entry=float(ml["tte_at_entry"])
            if ml.get("tte_at_entry") is not None
            else None,
            btc_spot_at_entry=float(ml["btc_spot"])
            if ml.get("btc_spot") is not None
            else None,
            prediction_correct=prediction_correct,
            edge_at_entry=edge,
        )


class TradeJournal:
    """Append-only JSONL journal for trade outcomes."""

    def __init__(self, path: str = "data/trade_journal.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.total_outcomes = 0
        # Count existing lines on startup
        if self.path.exists():
            try:
                with open(self.path, "r") as f:
                    self.total_outcomes = sum(1 for line in f if line.strip())
            except Exception as e:
                logger.warning(f"[TradeJournal] Could not count existing outcomes: {e}")
        logger.info(
            f"[TradeJournal] Initialized with {self.total_outcomes} existing outcomes at {self.path}"
        )

    def record(self, outcome: TradeOutcome) -> None:
        """Append a single outcome to the JSONL file."""
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(asdict(outcome)) + "\n")
            self.total_outcomes += 1
        except Exception as e:
            logger.error(f"[TradeJournal] Failed to record outcome: {e}")

    def load_all(self) -> List[TradeOutcome]:
        """Load all outcomes from disk."""
        outcomes = []
        if not self.path.exists():
            return outcomes
        try:
            with open(self.path, "r") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        outcomes.append(
                            TradeOutcome(
                                **{
                                    k: v
                                    for k, v in data.items()
                                    if k in TradeOutcome.__dataclass_fields__
                                }
                            )
                        )
                    except Exception as e:
                        logger.warning(
                            f"[TradeJournal] Skipping malformed line {line_num}: {e}"
                        )
        except Exception as e:
            logger.error(f"[TradeJournal] Failed to load journal: {e}")
        return outcomes

    def get_sample_count(self) -> int:
        """Return total recorded outcomes."""
        return self.total_outcomes

    def analyze_losses(self, n: int = 50) -> dict:
        """Analyze the most recent n losses.

        Returns a dict with:
        - reason_breakdown: {close_reason: count}
        - avg_pnl_by_confidence: {bucket: avg_pnl}
        - avg_tte_wins_vs_losses: {wins: avg_tte, losses: avg_tte}
        - summary: human-readable string
        """
        all_outcomes = self.load_all()
        losses = [o for o in all_outcomes if o.pnl < 0]
        recent_losses = losses[-n:]

        if not recent_losses:
            return {
                "reason_breakdown": {},
                "avg_pnl_by_confidence": {},
                "avg_tte_wins_vs_losses": {},
                "summary": "No losses recorded yet.",
            }

        # Breakdown by close reason
        reason_counts: dict = {}
        for o in recent_losses:
            reason = o.close_reason or "UNKNOWN"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        # Avg PnL by confidence bucket (using all outcomes for context)
        conf_buckets = _bucket_by_confidence(all_outcomes)
        avg_pnl_by_conf = {}
        for bucket, items in sorted(conf_buckets.items()):
            if items:
                avg_pnl_by_conf[bucket] = round(
                    sum(o.pnl for o in items) / len(items), 4
                )

        # Avg TTE for wins vs losses (all outcomes)
        wins_tte = [
            o.tte_at_entry
            for o in all_outcomes
            if o.pnl > 0 and o.tte_at_entry is not None
        ]
        losses_tte = [
            o.tte_at_entry
            for o in all_outcomes
            if o.pnl < 0 and o.tte_at_entry is not None
        ]
        avg_tte = {
            "wins": round(sum(wins_tte) / len(wins_tte), 4) if wins_tte else None,
            "losses": round(sum(losses_tte) / len(losses_tte), 4)
            if losses_tte
            else None,
        }

        # Build summary
        total_loss = sum(o.pnl for o in recent_losses)
        top_reason = (
            max(reason_counts, key=reason_counts.get) if reason_counts else "N/A"
        )
        summary_parts = [
            f"Analyzed {len(recent_losses)} recent losses (${total_loss:+.2f} total).",
            f"Top close reason: {top_reason} ({reason_counts.get(top_reason, 0)}x).",
        ]
        if avg_tte["wins"] is not None and avg_tte["losses"] is not None:
            summary_parts.append(
                f"Avg TTE: wins={avg_tte['wins']:.1f}min, losses={avg_tte['losses']:.1f}min."
            )

        return {
            "reason_breakdown": reason_counts,
            "avg_pnl_by_confidence": avg_pnl_by_conf,
            "avg_tte_wins_vs_losses": avg_tte,
            "summary": " ".join(summary_parts),
        }

    def win_rate_by_feature(self) -> dict:
        """Win rate bucketed by model_confidence and tte_at_entry ranges.

        Returns:
            {
                "by_confidence": {bucket_label: {wins, total, rate}},
                "by_tte": {bucket_label: {wins, total, rate}},
            }
        """
        all_outcomes = self.load_all()
        if not all_outcomes:
            return {"by_confidence": {}, "by_tte": {}}

        # By confidence
        conf_buckets = _bucket_by_confidence(all_outcomes)
        by_confidence = {}
        for bucket, items in sorted(conf_buckets.items()):
            if items:
                wins = sum(1 for o in items if o.pnl > 0)
                by_confidence[bucket] = {
                    "wins": wins,
                    "total": len(items),
                    "rate": round(wins / len(items), 4),
                }

        # By TTE ranges (minutes)
        tte_ranges = [
            ("0-5min", 0, 5),
            ("5-15min", 5, 15),
            ("15-30min", 15, 30),
            ("30-60min", 30, 60),
            ("60+min", 60, float("inf")),
        ]
        by_tte = {}
        for label, lo, hi in tte_ranges:
            items = [
                o
                for o in all_outcomes
                if o.tte_at_entry is not None and lo <= o.tte_at_entry < hi
            ]
            if items:
                wins = sum(1 for o in items if o.pnl > 0)
                by_tte[label] = {
                    "wins": wins,
                    "total": len(items),
                    "rate": round(wins / len(items), 4),
                }

        return {"by_confidence": by_confidence, "by_tte": by_tte}


def _bucket_by_confidence(outcomes: List[TradeOutcome]) -> dict:
    """Group outcomes into confidence buckets."""
    buckets: dict = {
        "0.0-0.2": [],
        "0.2-0.4": [],
        "0.4-0.6": [],
        "0.6-0.8": [],
        "0.8-1.0": [],
    }
    for o in outcomes:
        if o.model_confidence is None:
            continue
        c = o.model_confidence
        if c < 0.2:
            buckets["0.0-0.2"].append(o)
        elif c < 0.4:
            buckets["0.2-0.4"].append(o)
        elif c < 0.6:
            buckets["0.4-0.6"].append(o)
        elif c < 0.8:
            buckets["0.6-0.8"].append(o)
        else:
            buckets["0.8-1.0"].append(o)
    return buckets
