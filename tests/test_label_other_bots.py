"""Tests for the 2026-06-10 label fix in the OTHER harvesting bots.

`src/bots/btc_15m_bot.py` was already de-corrupted (see `tests/test_ml_labels.py`).
This module covers the same class of fix as it propagates to the two remaining
harvesting bots that used the identical bid-else-ask-else-last fallback:

* `src.bots.btc_hourly_bot`  (BTC hourly ladder)
* `src.bots.crypto_15m_bot`  (ETH/SOL/DOGE/XRP 15m)

Both must route the logged contract price through the shared
`_best_observable_price` helper so a cleared/locked book (bid==0, ask pinned at
~1.0) is NOT recorded as a misleading 1.0 -> mislabeled YES.

Also covers the LEAN-config change: XRP joins SOL/DOGE in
`LATENCY_ARB_DISABLED_ASSETS` (0/5 bleeder per the 2026-06-10 review).

All tests are offline/deterministic and only touch this agent's files (plus the
already-frozen `btc_15m_bot` helper).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# The canonical helper + locked-book threshold (single source of truth).
from src.bots.btc_15m_bot import _best_observable_price, _LOCKED_BOOK_HI

# The two bots under test re-export the same helper symbol.
from src.bots import btc_hourly_bot
from src.bots import crypto_15m_bot
from src.bots.crypto_15m_bot import LATENCY_ARB_DISABLED_ASSETS


# --------------------------------------------------------------------------- #
# A) Label fix: cleared/locked book must not yield a misleading price
# --------------------------------------------------------------------------- #


def test_both_bots_use_the_shared_helper():
    """Both bots import the exact same `_best_observable_price` object.

    A single source of truth means there is no chance of the fix drifting
    between bots (e.g. a copy-paste that re-introduces the 1.0 artifact).
    """
    assert btc_hourly_bot._best_observable_price is _best_observable_price
    assert crypto_15m_bot._best_observable_price is _best_observable_price


def test_hourly_bot_cleared_book_not_one():
    """btc_hourly_bot: empty YES book (bid=0), ask pinned at 1.0, last=0.002.

    The logged price must be the real last trade, never the 1.0 artifact.
    """
    helper = btc_hourly_bot._best_observable_price
    assert helper(0.0, 1.0, 0.002) == 0.002
    # No usable last price -> 0.0, never a fabricated 1.0.
    assert helper(0.0, 1.0, 0.0) == 0.0
    # ask exactly at the lock boundary is conservatively treated as locked.
    assert helper(0.0, _LOCKED_BOOK_HI, 0.013) == 0.013


def test_crypto_bot_cleared_book_not_one():
    """crypto_15m_bot: same cleared/locked-book guard as the hourly bot."""
    helper = crypto_15m_bot._best_observable_price
    assert helper(0.0, 1.0, 0.004) == 0.004
    assert helper(0.0, 1.0, 0.0) == 0.0
    assert helper(0.0, _LOCKED_BOOK_HI, 0.021) == 0.021


def test_genuine_books_unaffected_in_both_bots():
    """A normal two-sided / one-sided book is logged unchanged in both bots."""
    for mod in (btc_hourly_bot, crypto_15m_bot):
        helper = mod._best_observable_price
        # Two-sided book -> bid (most reliable observable).
        assert helper(0.62, 0.65, 0.60) == 0.62
        # Genuine one-sided ask (well below the lock boundary) -> ask.
        assert helper(0.0, 0.40, 0.0) == 0.40


# --------------------------------------------------------------------------- #
# B) LEAN config: XRP latency arb disabled (0/5 bleeder)
# --------------------------------------------------------------------------- #


def test_xrp_latency_arb_disabled():
    """XRP joins SOL/DOGE in the latency-arb disable set (2026-06-10 review)."""
    assert "XRP" in LATENCY_ARB_DISABLED_ASSETS


def test_prior_disabled_assets_still_disabled():
    """The fix is additive — SOL and DOGE remain disabled."""
    assert {"SOL", "DOGE"}.issubset(LATENCY_ARB_DISABLED_ASSETS)


def test_eth_latency_arb_still_enabled():
    """ETH stays enabled (watch-only/unproven), not disabled by this change."""
    assert "ETH" not in LATENCY_ARB_DISABLED_ASSETS
