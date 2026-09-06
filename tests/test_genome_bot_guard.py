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


# ---------------------------------------------------------------------------
# F4 blocker (2026-09-06): the bot builds the calibration provider the spec names
# ---------------------------------------------------------------------------
def _respec(tmp_path, kind):
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    from src.factory import promoted

    doc = json.load(open(src, encoding="utf-8"))
    if kind is None:
        doc["calibration"].pop("kind", None)
    else:
        doc["calibration"]["kind"] = kind
    doc["spec_hash"] = promoted.spec_hash_of(doc)
    path = tmp_path / f"{SEED_SPEC}.json"
    path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return str(path)


def test_bot_builds_the_walk_forward_provider_the_committed_spec_names(monkeypatch, mp_caplog, tmp_path):
    """The blocker: parity was proven under walk-forward, the bot built frozen. Now it builds
    what the spec says, the guard sees a match, and the MISMATCH line is gone."""
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    monkeypatch.setenv("GENOME_STRATEGY_ID", SEED_SPEC)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot
    from src.strategies.genome_strategy import WalkForwardCalibrationProvider

    bot = WeatherBot()
    assert bot.genome_refused_reason is None and "genome" in bot.strategies
    strat = bot.strategies["genome"]
    assert bot.genome_spec.calibration.kind == "walk_forward"
    assert isinstance(strat.calibration_provider, WalkForwardCalibrationProvider)
    assert strat.calibration_kind == "walk_forward" and strat.calibration_kind_ok is True
    assert strat.calibration_provider.sha256 == bot.genome_spec.calibration.sha256
    msgs = [r.getMessage() for r in mp_caplog.records]
    assert not any("CALIBRATION PROVIDER MISMATCH" in m for m in msgs)
    assert any("GenomeStrategy calibration provider for" in m and '"kind": "walk_forward"' in m for m in msgs)


def test_bot_builds_frozen_only_for_a_spec_that_says_frozen(monkeypatch, mp_caplog, tmp_path):
    monkeypatch.setenv("GENOME_STRATEGY_ID", _respec(tmp_path, "frozen"))
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot
    from src.strategies.genome_strategy import FrozenCalibrationProvider

    bot = WeatherBot()
    assert bot.genome_refused_reason is None and "genome" in bot.strategies
    strat = bot.strategies["genome"]
    assert isinstance(strat.calibration_provider, FrozenCalibrationProvider)
    assert strat.calibration_kind == "frozen" and strat.calibration_kind_ok is True


def test_bot_keeps_frozen_and_the_warning_for_an_unproven_spec(monkeypatch, mp_caplog, tmp_path):
    monkeypatch.setenv("GENOME_STRATEGY_ID", _respec(tmp_path, None))
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot
    from src.strategies.genome_strategy import FrozenCalibrationProvider

    bot = WeatherBot()
    assert "genome" in bot.strategies  # shadow: warn and continue, exactly as before
    strat = bot.strategies["genome"]
    assert isinstance(strat.calibration_provider, FrozenCalibrationProvider)
    assert strat.calibration_kind_ok is False
    assert any("CALIBRATION PROVIDER MISMATCH" in r.getMessage() for r in mp_caplog.records)


def _redirected_root_with_mutated_ny_truth(tmp_path, n_rows=61, delta=15):
    """The red team's attack root: a copy of the archives with +15 F on 61 NY truth rows."""
    import csv
    import shutil

    from src.factory.promoted import REPO_ROOT

    root = tmp_path / "attack_root"
    (root / "data" / "forecast_archive").mkdir(parents=True)
    (root / "data" / "weather_truth").mkdir(parents=True)
    shutil.copy(os.path.join(REPO_ROOT, "data", "forecast_archive", "forecast_series_gfs_mex.csv"),
                root / "data" / "forecast_archive")
    for st in ("KNYC", "KMDW", "KLAX", "KMIA"):
        shutil.copy(os.path.join(REPO_ROOT, "data", "weather_truth", f"cli_daily_high_{st}.csv"),
                    root / "data" / "weather_truth")
    p = root / "data" / "weather_truth" / "cli_daily_high_KNYC.csv"
    with open(p, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0].keys())
    n = 0
    for r in rows:
        if r["high"] and n < n_rows:
            r["high"] = str(int(r["high"]) + delta)
            n += 1
    assert n == n_rows
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return str(root)


def test_shadow_bot_on_a_redirected_mutated_archive_loads_but_logs_archive_pin_mismatch(monkeypatch, mp_caplog, tmp_path):
    """F4 red team 2026-09-06: dir sha == spec, kind ok, and a different fit -- now visible."""
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    import src.strategies.genome_strategy as gs

    monkeypatch.setattr(gs, "_REPO_ROOT", _redirected_root_with_mutated_ny_truth(tmp_path))
    monkeypatch.setenv("GENOME_STRATEGY_ID", SEED_SPEC)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot()
    strat = bot.strategies["genome"]  # shadow reaches no exchange: loads and continues
    assert bot.genome_refused_reason is None
    assert strat.calibration_provider.sha256 == bot.genome_spec.calibration.sha256  # the old guard's whole view
    assert strat.calibration_kind_ok is True
    assert strat.archive_pins_ok is False and "KNYC" in strat.archive_pins_detail
    assert any("ARCHIVE PIN MISMATCH (shadow)" in r.getMessage() for r in mp_caplog.records)


def test_paper_bot_on_a_redirected_mutated_archive_is_refused(monkeypatch, mp_caplog, tmp_path):
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    import src.strategies.genome_strategy as gs
    from src.factory import promoted

    doc = json.load(open(src, encoding="utf-8"))
    doc["mode"], doc["registry_status"] = "paper", "PROPOSED"
    doc["spec_hash"] = promoted.spec_hash_of(doc)
    spec_path = tmp_path / f"{SEED_SPEC}.json"
    spec_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(gs, "_REPO_ROOT", _redirected_root_with_mutated_ny_truth(tmp_path))
    monkeypatch.setenv("GENOME_STRATEGY_ID", str(spec_path))
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "paper")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot

    monkeypatch.setattr(WeatherBot, "_registry_status", staticmethod(lambda family: "PROPOSED"))
    bot = WeatherBot()  # must not raise
    assert "genome" not in bot.strategies and list(bot.strategies) == ["weather"]
    assert "archives the spec did not pin" in bot.genome_refused_reason and "KNYC" in bot.genome_refused_reason
    assert any("GenomeStrategy REFUSED" in r.getMessage() for r in mp_caplog.records)
    # the same paper spec on the REAL archives constructs, pins agreeing
    monkeypatch.setattr(gs, "_REPO_ROOT", promoted.REPO_ROOT)
    bot = WeatherBot()
    assert bot.genome_refused_reason is None and bot.strategies["genome"].archive_pins_ok is True


def test_missing_walk_forward_archives_refuse_the_genome_and_name_the_deploy_step(monkeypatch, mp_caplog, tmp_path):
    """The maia failure mode: data/calibration is in the bind but the archives are not."""
    src = os.path.join(PROMOTED_DIR, f"{SEED_SPEC}.json")
    if not os.path.exists(src):
        pytest.skip("promoted seed spec not present")
    import src.strategies.genome_strategy as gs

    monkeypatch.setattr(gs, "_REPO_ROOT", str(tmp_path / "no_archives"))  # default archive paths -> absent
    monkeypatch.setenv("GENOME_STRATEGY_ID", SEED_SPEC)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    from src.bots.weather_bot import WeatherBot

    bot = WeatherBot()  # must not raise
    assert "genome" not in bot.strategies
    assert bot.genome_refused_reason and "deploy_f3_shadow.sh step 2b" in bot.genome_refused_reason
    assert any("GenomeStrategy REFUSED" in r.getMessage() for r in mp_caplog.records)
