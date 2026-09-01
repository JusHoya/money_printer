"""Mention base-rate strategy scaffold for the Kalshi Mentions category.

Compares a per-(series, word) historical base rate against the market mid and
emits a maker-priced signal only on a large disagreement. This is a SCAFFOLD:
it is real, unit-testable strategy code, but it can never trade today because
:data:`src.bots.mention_bot.MENTION_TRADING_ENABLED` is ``False`` and this
module re-checks that flag on every ``analyze`` call.

MARKET FACTS (verified live 2026-09-01)
---------------------------------------
* The Mentions category spans **95 series** (``KXTRUMPMENTION``,
  ``KXPRESMENTION``, ``KXLEAVITTMENTION``, ...). All report
  ``fee_type == "quadratic"`` with ``fee_multiplier == 1`` — the standard
  schedule, under which the maker fee rounds to ~$0. That is why this scaffold
  prices the maker leg: resting liquidity in these series is effectively free.
* Settlement follows the MENTION.pdf grammar rules: plural and possessive
  forms of the target word COUNT; tense variants do NOT; closed compounds do
  NOT. A base rate computed by naive substring matching over transcripts is
  therefore wrong in both directions.
* The CFTC opened a probe into the category in Aug 2026 (sports mention
  markets were pulled). Delisting risk is real: nothing here may assume the
  category still exists at settlement, which is one more reason the trading
  flag stays off.

ACTIVATION PATH (in order, before any capital gate)
---------------------------------------------------
1. Build ``data/mention_base_rates.json`` from historical transcripts of the
   actual settlement sources — not from intuition. Format:
   ``{series: {word: base_rate}}`` with words uppercased.
2. Encode the MENTION.pdf grammar (plurals/possessives in, tense variants and
   closed compounds out) into the transcript counter that produces those base
   rates, so the base rate measures the same event the market settles on.
3. Only then consider flipping ``MENTION_TRADING_ENABLED`` — and per
   HANDOFF.md, the acceptance evidence must be realized settlement-true
   outcomes, not the modelled edge this scaffold computes.

Every rejection path emits one INFO line via
:func:`src.core.risk_manager.log_rejection` with a stable reason code and the
measured value that failed (the gas-strategy observability contract).
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, List, Optional

from src.core.interfaces import MarketData, Strategy, TradeSignal
from src.core.risk_manager import log_rejection
from src.utils.logger import logger

#: ``{series: {WORD: base_rate}}`` built offline from historical transcripts.
DEFAULT_BASE_RATES_PATH = os.path.join("data", "mention_base_rates.json")

#: Minimum |base_rate - mid| before a signal is worth a resting order.
MIN_EDGE = 0.10

# --- reason codes (stable; grep these in logs) -------------------------
REJECT_TRADING_DISABLED = "MENTION_TRADING_DISABLED"
REJECT_NOT_A_MENTION_MARKET = "MENTION_NOT_A_MENTION_MARKET"
REJECT_BASE_RATES_UNAVAILABLE = "MENTION_BASE_RATES_UNAVAILABLE"
REJECT_NO_BASE_RATE = "MENTION_NO_BASE_RATE"
REJECT_BOOK_ONE_SIDED = "MENTION_BOOK_ONE_SIDED"
REJECT_EDGE_BELOW_MIN = "MENTION_EDGE_BELOW_MIN"


def _trading_enabled() -> bool:
    """Read the bot module's kill switch at call time.

    Imported lazily so this module and :mod:`src.bots.mention_bot` (which
    imports this strategy at module level) do not form an import cycle, and so
    the flag is read live rather than frozen at import.
    """
    from src.bots import mention_bot

    return bool(mention_bot.MENTION_TRADING_ENABLED)


def _optional_price(value) -> Optional[float]:
    """A usable Kalshi quote in ``(0, 1)``, else ``None``."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0.0 or out >= 1.0:
        return None
    return out


def split_mention_symbol(symbol: str) -> Optional[tuple]:
    """``(series, word)`` from a mention ticker, or ``None``.

    Mention tickers are ``SERIES-EVENT-WORD`` (``KXLEAVITTMENTION-26AUG27-FARM``):
    series before the first hyphen, target word after the last. There is no
    date filter anywhere on this path — mention events do not follow the
    weather ``%y%b%d`` convention.
    """
    text = (symbol or "").strip().upper()
    if "MENTION" not in text:
        return None
    parts = text.split("-")
    if len(parts) < 3 or not parts[0] or not parts[-1]:
        return None
    return parts[0], parts[-1]


class MentionStrategy(Strategy):
    """Base-rate vs mid divergence over mention markets, maker-priced."""

    def __init__(
        self,
        base_rates_path: str = DEFAULT_BASE_RATES_PATH,
        min_edge: float = MIN_EDGE,
        base_quantity: int = 5,
    ):
        if base_quantity < 1:
            raise ValueError(f"base_quantity must be >= 1, got {base_quantity}")
        self.base_rates_path = base_rates_path
        self.min_edge = float(min_edge)
        self.base_quantity = int(base_quantity)
        self._base_rates: Optional[Dict[str, Dict[str, float]]] = None
        self._base_rates_error: Optional[str] = None
        self._loaded = False

    def name(self) -> str:
        return "Mention Base Rate"

    # -- base rates ------------------------------------------------------

    def _load_base_rates(self) -> Optional[Dict[str, Dict[str, float]]]:
        """Load the base-rate file once; absence is logged, never defaulted."""
        if self._loaded:
            return self._base_rates
        self._loaded = True
        try:
            with open(self.base_rates_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("base-rates file is not a JSON object")
            self._base_rates = {
                str(series).upper(): {
                    str(word).upper(): float(rate)
                    for word, rate in (words or {}).items()
                }
                for series, words in raw.items()
            }
            logger.info(
                "[Mention] base rates loaded: %d series from %s",
                len(self._base_rates),
                self.base_rates_path,
            )
        except (OSError, ValueError, TypeError) as exc:
            self._base_rates = None
            self._base_rates_error = str(exc)
            logger.warning(
                "[Mention] base rates unavailable (%s): %s. Every signal will "
                "be rejected %s.",
                self.base_rates_path,
                exc,
                REJECT_BASE_RATES_UNAVAILABLE,
            )
        return self._base_rates

    def reload_base_rates(self) -> None:
        """Force a re-read on the next ``analyze`` call."""
        self._loaded = False
        self._base_rates = None
        self._base_rates_error = None

    # -- Strategy ABC ----------------------------------------------------

    def analyze(self, market_data: MarketData) -> List[TradeSignal]:
        """Price one mention market. Returns ``[]`` or a single-signal list."""
        symbol = getattr(market_data, "symbol", "") or ""

        split = split_mention_symbol(symbol)
        if split is None:
            self._reject(REJECT_NOT_A_MENTION_MARKET, symbol)
            return []
        series, word = split

        # The kill switch is re-checked HERE, not only in the bot: a strategy
        # object wired anywhere else must still be inert while the flag is off.
        if not _trading_enabled():
            self._reject(REJECT_TRADING_DISABLED, symbol, series=series, word=word)
            return []

        rates = self._load_base_rates()
        if rates is None:
            self._reject(
                REJECT_BASE_RATES_UNAVAILABLE,
                symbol,
                path=self.base_rates_path,
                detail=self._base_rates_error,
            )
            return []

        base_rate = rates.get(series, {}).get(word)
        if base_rate is None:
            self._reject(REJECT_NO_BASE_RATE, symbol, series=series, word=word)
            return []
        base_rate = max(0.0, min(1.0, float(base_rate)))

        extra = getattr(market_data, "extra", None) or {}
        yes_bid = _optional_price(market_data.bid)
        yes_ask = _optional_price(market_data.ask)
        no_bid = _optional_price(extra.get("no_bid"))

        # A one-sided book has no mid to disagree with, and no resting price
        # that is anchored to a real counterparty.
        if yes_bid is None or yes_ask is None:
            self._reject(
                REJECT_BOOK_ONE_SIDED,
                symbol,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
            )
            return []

        mid = (yes_bid + yes_ask) / 2.0
        edge = base_rate - mid
        if abs(edge) < self.min_edge:
            self._reject(
                REJECT_EDGE_BELOW_MIN,
                symbol,
                edge=edge,
                min_edge=self.min_edge,
                base_rate=base_rate,
                mid=mid,
            )
            return []

        buy_yes = edge > 0
        contract_side = "YES" if buy_yes else "NO"
        # Maker-priced: rest at the current best bid of the side being bought.
        maker_price = yes_bid if buy_yes else no_bid
        if maker_price is None:
            self._reject(
                REJECT_BOOK_ONE_SIDED,
                symbol,
                contract=contract_side,
                detail="no NO-side bid to rest at",
            )
            return []
        p_win = base_rate if buy_yes else 1.0 - base_rate

        signal = TradeSignal(
            symbol=symbol,
            side="buy",
            quantity=self.base_quantity,
            limit_price=maker_price,
            confidence=max(0.01, min(0.99, p_win)),
            contract_side=contract_side,
        )
        logger.info(
            "[Mention] ACCEPT %s contract=%s word=%s base_rate=%.4f mid=%.4f "
            "edge=%+.4f entry=%.4f(maker) qty=%d",
            symbol,
            contract_side,
            word,
            base_rate,
            mid,
            edge,
            maker_price,
            self.base_quantity,
        )
        return [signal]

    def _reject(self, reason: str, symbol: str, **measured) -> None:
        log_rejection(reason, strategy=self.name(), symbol=symbol, **measured)


__all__ = [
    "DEFAULT_BASE_RATES_PATH",
    "MIN_EDGE",
    "MentionStrategy",
    "REJECT_BASE_RATES_UNAVAILABLE",
    "REJECT_BOOK_ONE_SIDED",
    "REJECT_EDGE_BELOW_MIN",
    "REJECT_NO_BASE_RATE",
    "REJECT_NOT_A_MENTION_MARKET",
    "REJECT_TRADING_DISABLED",
    "split_mention_symbol",
]
