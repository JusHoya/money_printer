"""A genome that cannot be built must never take the sandbox down (maia 2026-09-05).

The promoted spec points at ``data/calibration``; on maia the image excludes ``data/``
and the ``/srv/money_printer/data`` bind shadows ``/app/data``, so the directory was
absent, ``FrozenCalibrationProvider`` raised inside ``WeatherBot.__init__`` and the
container crash-looped. The bot now logs ``GenomeStrategy REFUSED`` and runs V2 only.
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from src.utils.logger import logger as mp_logger

PROMOTED_DIR = os.path.join(os.path.dirname(__file__), "..", "configs", "factory", "promoted")
SEED_SPEC = "0c4b20502f2daf65"  # fr31a_taker, shadow


@pytest.fixture
def mp_caplog(caplog):
    """``src.utils.logger`` sets propagate=False, so caplog sees nothing without this."""
    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    yield caplog
    mp_logger.removeHandler(caplog.handler)


@pytest.fixture
def spec_with_missing_calibration(tmp_path):
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    from src.factory import promoted

    doc = json.load(open(src, encoding="utf-8"))
    doc["calibration"]["dir"] = str(tmp_path / "nope" / "calibration")  # absolute, absent
    doc["spec_hash"] = promoted.spec_hash_of(doc)
    path = tmp_path / f"{SEED_SPEC}.json"
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return str(path)


def test_missing_calibration_dir_refuses_the_genome_instead_of_crashing(
    spec_with_missing_calibration, monkeypatch, mp_caplog, tmp_path
):
    monkeypatch.setenv("GENOME_STRATEGY_ID", spec_with_missing_calibration)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot()  # must not raise
    assert "genome" not in bot.strategies
    assert "weather" in bot.strategies
    assert bot.genome_spec is None
    assert any("GenomeStrategy REFUSED" in r.getMessage() for r in mp_caplog.records)
    # the refusal is otherwise invisible over HTTP: /api/status reads this attribute
    assert bot.genome_refused_reason and "GenomeSpecMismatch" in bot.genome_refused_reason


def test_bot_without_genome_env_is_unchanged(monkeypatch):
    monkeypatch.delenv("GENOME_STRATEGY_ID", raising=False)
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot()
    assert list(bot.strategies) == ["weather"] or "weather" in bot.strategies
    assert bot.genome_spec is None
    assert bot.genome_refused_reason is None


def test_every_city_skip_tells_the_genome_which_kind_of_skip_it_was(monkeypatch):
    """F3 review hole (c): the miss hook was wired ONLY to the Kalshi except path.

    An observation failure skips the city before Kalshi is ever polled (a lost
    chance) and an empty ladder means the bot looked and saw nothing (a data gap).
    Both used to leave the hour with no record at all.
    """
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    import src.bots.weather_bot as weather_bot
    from src.core.interfaces import MarketData

    monkeypatch.delenv("GENOME_STRATEGY_ID", raising=False)
    monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
    monkeypatch.setattr("src.bots.weather_bot.time.sleep", lambda s: None)
    bot = weather_bot.WeatherBot()
    city = weather_bot.CITY_CONFIG["NY"]
    bot.CITIES = (city,)
    genome = MagicMock(spec=["analyze", "record_missed_hour", "record_no_ladder", "name"])
    genome.analyze.return_value = []
    genome.name = "Genome test"
    bot.strategies = {weather_bot.GENOME_STRATEGY_KEY: genome, "weather": MagicMock(analyze=lambda d: [])}
    bot._process_signals = lambda signals, strategy_name, risk_manager, dashboard: False
    bot.nws = MagicMock()
    bot.metar = MagicMock()
    bot.kalshi = MagicMock()

    # 1. no observation at all -> the city is skipped entirely: a LOST hour
    bot.metar.fetch_latest.return_value = None
    bot.nws.fetch_latest.return_value = None
    bot.tick(MagicMock(), MagicMock())
    genome.record_missed_hour.assert_called_once_with("NY", "observation_failure")
    genome.record_no_ladder.assert_not_called()

    # 2. an observation but an EMPTY ladder -> the bot looked: a DATA gap
    genome.record_missed_hour.reset_mock()
    obs = MarketData(symbol="KNYC", timestamp=datetime.now(timezone.utc), price=0.0, volume=0,
                     bid=0.0, ask=0.0, extra={"temperature_f": 70.0, "max_temp_today_f": 75.0})
    bot.metar.fetch_latest.return_value = obs
    bot.nws.fetch_latest.return_value = None
    bot.kalshi.fetch_market_ladder.return_value = []
    bot._resolve_smart_ticker = lambda *a, **k: None
    bot.tick(MagicMock(), MagicMock())
    genome.record_no_ladder.assert_called_once_with("NY")
    genome.record_missed_hour.assert_not_called()

    # 3. the Kalshi poll raises -> a LOST hour, named as such
    genome.record_no_ladder.reset_mock()
    bot.kalshi.fetch_market_ladder.side_effect = RuntimeError("503")
    bot.tick(MagicMock(), MagicMock())
    genome.record_missed_hour.assert_called_once_with("NY", "poll_failure")


def test_a_corrupt_state_file_does_not_refuse_the_genome_for_the_deploy(monkeypatch, mp_caplog, tmp_path):
    """A state file that DECODES but holds the wrong shapes must not be fatal.

    ``_load_state`` recovered only from files it could not decode; a decodable one
    whose ``last_hour_epoch`` is a list (or whose hour is not a number, or whose
    ``missed_days``/``traded`` entries are not pairs) raised out of the
    constructor into ``WeatherBot.__init__``'s broad except -- the genome was then
    REFUSED for the rest of the deploy, which is exactly what the unreadable-file
    recovery exists to prevent.
    """
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    from src.factory.promoted import load_promoted

    cache = tmp_path / "cache"
    cache.mkdir()
    spec = load_promoted(src)
    state = cache / f"genome_state_{spec.genome_id}.json"
    state.write_text(
        json.dumps({"genome_id": spec.genome_id, "last_hour_epoch": ["NYC"], "missed_days": [], "traded": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GENOME_STRATEGY_ID", src)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(cache))
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot()
    assert bot.genome_refused_reason is None
    assert "genome" in bot.strategies, "a corrupt state file must not cost the deploy its genome"
    assert bot.strategies["genome"].state_recovered_from
    assert any("STARTING FROM AN EMPTY STATE" in r.getMessage() for r in mp_caplog.records)
