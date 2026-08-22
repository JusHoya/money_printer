"""Sprint 8 Stream C — registry cleanup tests.

Phase 0 teardown (2026-07-24, PRD FR-0.1): the crypto bots and strategies
(btc_15m_bot, btc_hourly_bot, crypto_strategy, ml_btc_15m, ...) were deleted
outright, superseding most of the original Sprint 8 assertions. What remains:

  8.6  No late-sniper bot in the registry.
  8.8  Long-deleted strategy modules stay unimportable — now including the
       Phase 0 deletions.
"""

import importlib
import pytest

# ---------------------------------------------------------------------------
# 8.6  Late Sniper not in registry
# ---------------------------------------------------------------------------


def test_late_sniper_not_in_registry():
    """No bot registered under a name containing 'late' or 'sniper' (case-insensitive)."""
    from src.bots.registry import BotRegistry

    # Trigger registration by importing the bot package (weather only post-teardown)
    import src.bots  # noqa: F401

    for name in BotRegistry.list_bots():
        assert (
            "late" not in name.lower()
        ), f"Registry still contains late-sniper bot: {name!r}"
        assert (
            "sniper" not in name.lower()
        ), f"Registry still contains sniper bot: {name!r}"


# ---------------------------------------------------------------------------
# 8.8  Deleted strategy modules must not be importable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        # Deleted Apr 2026:
        "src.strategies.empirical_edge",
        "src.strategies.time_decay",
        "src.strategies.late_sniper",
        "src.strategies.crypto_hourly_v3",
        # Deleted in the Phase 0 teardown (2026-07-24, PRD FR-0.1):
        "src.strategies.crypto_strategy",
        "src.strategies.ml_btc_15m",
        "src.strategies.ml_btc_hourly",
        "src.strategies.longshot_fader_v2",
        "src.strategies.cross_spread_arb",
        "src.bots.btc_15m_bot",
        "src.bots.btc_hourly_bot",
        "src.bots.crypto_15m_bot",
    ],
)
def test_deleted_module_not_importable(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)
