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

PROMOTED_DIR = os.path.join(os.path.dirname(__file__), "..", "configs", "factory", "promoted")
SEED_SPEC = "0c4b20502f2daf65"  # fr31a_taker, shadow


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
    spec_with_missing_calibration, monkeypatch, caplog, tmp_path
):
    monkeypatch.setenv("GENOME_STRATEGY_ID", spec_with_missing_calibration)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot

    with caplog.at_level(logging.ERROR):
        bot = WeatherBot()  # must not raise
    assert "genome" not in bot.strategies
    assert "weather" in bot.strategies
    assert bot.genome_spec is None
    assert any("GenomeStrategy REFUSED" in r.getMessage() for r in caplog.records)


def test_bot_without_genome_env_is_unchanged(monkeypatch):
    monkeypatch.delenv("GENOME_STRATEGY_ID", raising=False)
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot()
    assert list(bot.strategies) == ["weather"] or "weather" in bot.strategies
    assert bot.genome_spec is None
