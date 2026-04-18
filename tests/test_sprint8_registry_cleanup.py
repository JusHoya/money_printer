"""Sprint 8 Stream C — registry cleanup tests.

Verifies:
  8.6  Late Sniper is absent from the bot registry and strategy dict.
  8.7  Empirical Edge NO-side — N/A (class was already deleted in Apr 2026).
  8.8  Deleted strategy classes raise ImportError / AttributeError on access.
  8.9  ml_btc_15m.py contains no stale sig.stop_loss = max(0.01, lp - 0.05) lines.
"""

import importlib
import pathlib
import pytest

# ---------------------------------------------------------------------------
# 8.6  Late Sniper not in registry
# ---------------------------------------------------------------------------


def test_late_sniper_not_in_registry():
    """No bot registered under a name containing 'late' or 'sniper' (case-insensitive)."""
    from src.bots.registry import BotRegistry

    # Trigger registration by importing the concrete bots
    import src.bots.btc_15m_bot  # noqa: F401
    import src.bots.btc_hourly_bot  # noqa: F401
    import src.bots.weather_bot  # noqa: F401

    for name in BotRegistry.list_bots():
        assert (
            "late" not in name.lower()
        ), f"Registry still contains late-sniper bot: {name!r}"
        assert (
            "sniper" not in name.lower()
        ), f"Registry still contains sniper bot: {name!r}"


def test_late_sniper_not_in_btc15m_strategies():
    """BTC15mBot.strategies dict must not contain a 'late_sniper' key."""
    from src.bots.btc_15m_bot import BTC15mBot

    bot = BTC15mBot()
    assert (
        "late_sniper" not in bot.strategies
    ), "BTC15mBot.strategies still contains 'late_sniper' key"
    for key in bot.strategies:
        assert (
            "sniper" not in key.lower()
        ), f"BTC15mBot.strategies still has sniper key: {key!r}"


# ---------------------------------------------------------------------------
# 8.7  Empirical Edge — N/A (deleted in Apr 2026)
# ---------------------------------------------------------------------------


def test_empirical_edge_module_absent():
    """empirical_edge.py was deleted in Apr 2026; module must not be importable.

    If somehow it reappears, this test will catch it.
    The Empirical Edge NO-side task is therefore N/A — the entire class is gone.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.strategies.empirical_edge")


# ---------------------------------------------------------------------------
# 8.8  Deleted strategy classes — import / attribute checks
# ---------------------------------------------------------------------------


def test_dead_strategy_classes_removed():
    """Classes deleted in Sprint 8 must no longer be accessible.

    Classes that were NOT deleted and why:
      - CryptoLongShotFader      : tests/test_stop_loss_fixes.py and
                                   tests/test_strategy_tracking.py import it —
                                   kept to avoid breaking those tests.
      - Crypto15mTrendStrategyV3 : scripts/lab.py, scripts/simulate.py, and
                                   tests/test_sprint6_structural_fixes.py import it —
                                   kept to avoid cascade failures.
    """
    import src.strategies.crypto_strategy as cs
    import src.strategies.weather_strategy as ws

    # --- Deleted: Crypto15mTrendStrategy (V1) ---
    assert not hasattr(
        cs, "Crypto15mTrendStrategy"
    ), "Crypto15mTrendStrategy (V1) should have been deleted"

    # --- Deleted: CryptoHourlyStrategy (V1) ---
    assert not hasattr(
        cs, "CryptoHourlyStrategy"
    ), "CryptoHourlyStrategy (V1) should have been deleted"

    # --- Deleted: CryptoArbitrageStrategy ---
    assert not hasattr(
        cs, "CryptoArbitrageStrategy"
    ), "CryptoArbitrageStrategy should have been deleted"

    # --- Deleted: Crypto15mLateSniper ---
    assert not hasattr(
        cs, "Crypto15mLateSniper"
    ), "Crypto15mLateSniper should have been deleted"

    # --- Deleted: WeatherArbitrageStrategy (V1) ---
    assert not hasattr(
        ws, "WeatherArbitrageStrategy"
    ), "WeatherArbitrageStrategy (V1) should have been deleted"

    # --- Retained (live): CryptoLongShotFader ---
    # NOT deleted — referenced in test_stop_loss_fixes.py and test_strategy_tracking.py
    assert hasattr(
        cs, "CryptoLongShotFader"
    ), "CryptoLongShotFader must still exist (referenced in existing tests)"

    # --- Retained (live): Crypto15mTrendStrategyV3 ---
    # NOT deleted — referenced in scripts/lab.py, scripts/simulate.py, and existing tests
    assert hasattr(
        cs, "Crypto15mTrendStrategyV3"
    ), "Crypto15mTrendStrategyV3 must still exist (referenced in scripts and existing tests)"


# ---------------------------------------------------------------------------
# 8.9  ml_btc_15m.py stale stop_loss lines removed
# ---------------------------------------------------------------------------


def test_ml_btc_15m_no_stale_stop_loss():
    """src/strategies/ml_btc_15m.py must not contain the stale stop_loss assignment."""
    src_path = (
        pathlib.Path(__file__).parent.parent / "src" / "strategies" / "ml_btc_15m.py"
    )
    text = src_path.read_text(encoding="utf-8")
    assert "sig.stop_loss = max(0.01, lp - 0.05)" not in text, (
        "Stale sig.stop_loss = max(0.01, lp - 0.05) still present in ml_btc_15m.py — "
        "the matching engine's is_binary_event check skips stops for 15m contracts "
        "so this field is dead weight."
    )
