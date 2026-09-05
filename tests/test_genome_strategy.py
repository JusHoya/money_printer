"""GenomeStrategy (FR-F3.1/F3.3): row parity, signal shape, refusals, cadence, shadow mode, replay parity."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.bracket_payoff import BracketSpec  # noqa: E402
from src.core.interfaces import MarketData  # noqa: E402
from src.core.weather_settlement import settlement_close_for  # noqa: E402
from src.data.forecast_vintage_provider import ForecastVintageProvider  # noqa: E402
from src.factory import columns as C  # noqa: E402
from src.factory import features as feat  # noqa: E402
from src.factory import genome as G  # noqa: E402
from src.factory import promoted as P  # noqa: E402
from src.factory.fees import load_regime  # noqa: E402
from src.strategies import genome_strategy as gs  # noqa: E402
from src.strategies.genome_strategy import FrozenCalibrationProvider, GenomeSpecMismatch, GenomeStrategy  # noqa: E402
from src.utils.logger import logger as mp_logger  # noqa: E402

UTC = timezone.utc
CAL_DIR = REPO_ROOT / "data" / "calibration"
WALLCLOCK_FILES = (
    REPO_ROOT / "src" / "strategies" / "genome_strategy.py",
    REPO_ROOT / "src" / "factory" / "features.py",
    REPO_ROOT / "src" / "factory" / "genome.py",
)
FRAMES_DIR = os.getenv("MP_FACTORY_FRAMES")  # dev box: the frozen frame lives outside a worktree


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def mp_caplog(caplog):
    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    yield caplog
    mp_logger.removeHandler(caplog.handler)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _spec(genome, mode="shadow", **over):
    kw = dict(
        family="weather/gfs_mex/taker/v1", config_sha256="c" * 64, frame_search_sha256="f" * 64,
        calibration_dir=str(CAL_DIR), calibration_sha256=P.calibration_dir_sha256(str(CAL_DIR)),
        fee_type="quadratic", fee_regime_sha256=load_regime().sha256, mode=mode, registry_status="CLOSED",
        source="seed",
    )
    kw.update(over)
    return P.build_spec(genome, **kw)


TS = datetime(2026, 7, 19, 15, 0, tzinfo=UTC)  # decision instant: 07-19 15:00Z, NY 07-20 ladder is >=24h out
VINTAGE_ROWS = [
    {"city": "NY", "station": "KNYC", "target_date": "2026-07-20", "init_time_utc": "2026-07-19T00:00:00Z",
     "lead_hours": 28, "source": "gfs_mex", "forecast_high_f": 88.0, "spread_f": ""},
    {"city": "NY", "station": "KNYC", "target_date": "2026-07-20", "init_time_utc": "2026-07-20T00:00:00Z",
     "lead_hours": 4, "source": "gfs_mex", "forecast_high_f": 88.0, "spread_f": ""},
]
LADDER = (  # (ticker, strike_type, floor, cap, yes_bid, yes_ask)
    ("KXHIGHNY-26JUL20-T83", "less", None, 83.0, 0.10, 0.14),
    ("KXHIGHNY-26JUL20-B84.5", "between", 84.0, 85.0, 0.12, 0.16),
    ("KXHIGHNY-26JUL20-B86.5", "between", 86.0, 87.0, 0.25, 0.30),
    ("KXHIGHNY-26JUL20-B88.5", "between", 88.0, 89.0, 0.30, 0.35),
    ("KXHIGHNY-26JUL20-B90.5", "between", 90.0, 91.0, 0.08, 0.12),
    ("KXHIGHNY-26JUL20-T91", "greater", 91.0, None, 0.03, 0.06),
)
CLOSE = "2026-07-21T03:59:00Z"


def _ladder(ts=TS, quotes=None):
    out = []
    for i, (t, st, fl, cp, bid, ask) in enumerate(LADDER):
        if quotes and t in quotes:
            bid, ask = quotes[t]
        out.append(MarketData(
            symbol=t, timestamp=ts, price=(bid + ask) / 2, volume=10, bid=bid, ask=ask,
            extra={"status": "active", "close_time": CLOSE, "no_bid": round(1 - ask, 4), "no_ask": round(1 - bid, 4),
                   "strike_type": st, "floor_strike": fl, "cap_strike": cp, "yes_sub_title": ""},
        ))
    return out


def _obs(ts=TS, ladder=None):
    ladder = ladder or _ladder(ts)
    a = ladder[0]
    return MarketData(symbol=a.symbol, timestamp=ts, price=a.price, volume=a.volume, bid=a.bid, ask=a.ask,
                      extra={"city_key": "NY", "kalshi_series": "KXHIGHNY", "settlement_station": "KNYC",
                             "ladder_markets": ladder, "strike_type": a.extra["strike_type"],
                             "floor_strike": a.extra["floor_strike"], "cap_strike": a.extra["cap_strike"]})


def _strategy(genome=None, clock=None, lag=240, **over):
    genome = genome or G.SEEDS["nofilter_no"]
    spec = _spec(genome, **over)
    return GenomeStrategy(
        spec, clock=clock or Clock(TS),
        forecast_provider=ForecastVintageProvider.from_rows(VINTAGE_ROWS, lag_min=lag),
        fee_regime=load_regime(),
        calibration_provider=FrozenCalibrationProvider(str(CAL_DIR), source="gfs_mex"),
    ), spec


def _rejects(caplog, code):
    return [r.getMessage() for r in caplog.records if f"reason={code}" in r.getMessage()]


# ---------------------------------------------------------------------------
# static guarantees
# ---------------------------------------------------------------------------
class TestStatic:
    def test_no_wall_clock_in_strategy_features_genome(self):
        pat = re.compile(r"datetime\.now|time\.time|utcnow|from datetime import")
        hits = []
        for p in WALLCLOCK_FILES:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p.name}:{i}: {line.strip()}")
        assert hits == []

    def test_imports_with_lightgbm_scipy_pyarrow_blocked(self):
        code = (
            "import sys\n"
            "for m in ('lightgbm','scipy','pyarrow'):\n"
            "    sys.modules[m] = None\n"
            "import src.strategies.genome_strategy as g\n"
            "import src.data.forecast_vintage_provider, src.factory.promoted\n"
            "blocked = [m for m in ('lightgbm','scipy','pyarrow') if sys.modules.get(m) is not None]\n"
            "assert not blocked, blocked\n"
            "print('ok', 'pandas' in sys.modules)\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT),
                           env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)), timeout=120)
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("ok")

    def test_vendored_geometry_matches_ev_analysis(self):
        pytest.importorskip("pandas")
        import src.backtest.ev_analysis as ev

        specs = [BracketSpec(t, st, fl, cp) for t, st, fl, cp, _, _ in LADDER]
        w = gs.ladder_core_width_f(specs)
        assert w == ev.ladder_core_width_f(specs) == 2.0
        for s in specs:
            assert gs.bracket_midpoint_f(s, w) == ev.bracket_midpoint_f(s, w)
            for mu in (80.0, 84.2, 88.0, 91.0, 95.5):
                assert gs.bracket_edge_distance_f(s, mu) == ev.bracket_edge_distance_f(s, mu)
        assert gs.REGIME_SINGLE == ev.EVConfig().regime
        assert gs.SUPPORT_SIGMAS == ev.EVConfig().support_sigmas


# ---------------------------------------------------------------------------
# construction refusals
# ---------------------------------------------------------------------------
class TestRefusals:
    def test_calibration_hash_mismatch(self):
        with pytest.raises(GenomeSpecMismatch, match="calibration sha"):
            _strategy(calibration_sha256="0" * 64)

    def test_fee_type_mismatch(self):
        with pytest.raises(GenomeSpecMismatch, match="fee_type"):
            _strategy(fee_type="quadratic_with_maker_fees")

    def test_fee_regime_sha_mismatch(self):
        with pytest.raises(GenomeSpecMismatch, match="fee regime sha"):
            _strategy(fee_regime_sha256="1" * 64)

    def test_fee_type_must_follow_mode(self):
        spec = _spec(G.SEEDS["nofilter_no"])
        doc = spec.to_doc()
        doc["fee"]["type"] = "maker"
        doc["spec_hash"] = P.spec_hash_of(doc)
        bad = P.from_doc(doc)
        with pytest.raises(GenomeSpecMismatch, match="mode"):
            GenomeStrategy(bad, clock=Clock(TS), forecast_provider=ForecastVintageProvider.from_rows(VINTAGE_ROWS),
                           fee_regime=load_regime(), calibration_provider=FrozenCalibrationProvider(str(CAL_DIR)))

    def test_lag_mismatch(self):
        with pytest.raises(GenomeSpecMismatch, match="lag"):
            _strategy(lag=0)

    def test_naive_clock_refused(self):
        with pytest.raises(GenomeSpecMismatch, match="naive"):
            _strategy(clock=Clock(TS.replace(tzinfo=None)))


# ---------------------------------------------------------------------------
# the visible row and the signal
# ---------------------------------------------------------------------------
class TestRowAndSignal:
    def test_build_row_has_every_visible_column_with_frame_dtypes(self):
        strat, spec = _strategy()
        rows = strat.build_rows(_obs(), TS)
        assert set(rows) == {t for t, *_ in LADDER}
        row = rows["KXHIGHNY-26JUL20-T83"]
        for name, dt in C.VISIBLE_DTYPES.items():
            assert name in row, name
            if name in ("target_date_code", "market_code"):
                assert int(row[name]) == -1  # dense frame indices, documented as unreproducible
                continue
            assert np.asarray(row[name]).dtype == np.dtype(dt), (name, np.asarray(row[name]).dtype)
        assert int(row["ts_utc"]) == int(TS.timestamp())
        assert row["minutes_to_close"] == round((datetime.fromisoformat(CLOSE.replace("Z", "+00:00")) - TS).total_seconds() / 60, 1)
        assert int(row["window_code"]) == C.WINDOW_LABELS.index(">=24h")
        assert int(row["direction_code"]) == 1 and int(row["mode_code"]) == 0
        assert int(row["lead_bucket_code"]) == C.lead_bucket_code(28) and row["lead_hours"] == 28.0
        assert row["init_time_utc"] == "2026-07-19T00:00:00Z"  # 07-20 00Z run is inside the 240-min lag at 15:00Z
        # p_win / quote / price / admissibility follow the evaluator + features rules
        assert row["p_win"] == 1.0 - row["p_yes"]
        assert row["quote"] == feat.quote(row["yes_bid"], row["yes_ask"], 1, 0) == 1.0 - 0.10
        assert row["price_paid"] == feat.price_paid(row["quote"], 0.01)
        assert bool(row["sandbox_admissible"]) == bool(feat.sandbox_admissible(row["p_win"], row["price_paid"]))
        assert bool(row["executable"]) == bool(row["sandbox_admissible"])
        t83 = BracketSpec("KXHIGHNY-26JUL20-T83", "less", None, 83.0)
        assert row["midpoint_f"] == gs.bracket_midpoint_f(t83, 2.0) == 81.5  # yes_bounds: <= 82 pays
        assert row["distance_f"] == abs(row["midpoint_f"] - row["mu_f"])
        assert row["edge_distance_f"] == gs.bracket_edge_distance_f(t83, float(row["mu_f"]))
        assert int(row["strike_type_code"]) == C.STRIKE_TYPE_LABELS.index("less")
        assert np.isnan(row["floor_strike"]) and row["cap_strike"] == 83.0
        # the genome cannot read the hidden/context keys through the mask API
        assert bool(G.to_mask(strat.genome, row)) in (True, False)
        with pytest.raises(C.HiddenColumnError):
            C.VisibleOnly(row)["won"]

    def test_row_p_yes_is_the_probability_engine_on_the_full_ladder(self):
        from src.calibration.forecast_calibration import load_calibration
        from src.calibration.probability_engine import bracket_probabilities_point

        strat, _ = _strategy()
        rows = strat.build_rows(_obs(), TS)
        specs = [BracketSpec(t, st, fl, cp) for t, st, fl, cp, _, _ in LADDER]
        res = bracket_probabilities_point(
            city="NY", target_date="2026-07-20", forecast_high_f=88.0, specs=specs,
            calibration=load_calibration(str(CAL_DIR / "NY_gfs_mex_v1.json")), lead_hours=28,
            regime="single", forecast_source="gfs_mex", support_sigmas=8.0,
        )
        for t, *_ in LADDER:
            assert rows[t]["p_yes"] == res.p_yes(t)
            assert rows[t]["mu_f"] == res.mu_f and rows[t]["sigma_f"] == res.sigma_f

    def test_signal_shape_first_in_market_and_already_traded(self, mp_caplog):
        strat, spec = _strategy()
        rows = strat.build_rows(_obs(), TS)
        expected = sorted(t for t, r in rows.items() if bool(G.to_mask(strat.genome, r)) and bool(r["executable"]))
        assert expected, "fixture must yield at least one masked executable market"
        sigs = strat.analyze(_obs())
        assert sorted(s.symbol for s in sigs) == expected
        for s in sigs:
            r = rows[s.symbol]
            assert s.side == "buy" and s.contract_side == "NO" and s.quantity == spec.contracts_frame == 20
            assert s.limit_price == pytest.approx(r["quote"] + 0.01, abs=1e-12)
            assert s.limit_price == r["price_paid"]
            assert s.confidence == r["p_win"]
            assert s.is_maker is False
            assert s.strike_type in ("less", "between", "greater") and s.expiration_time is not None
            assert s.expiration_time.tzinfo is not None and s.expiration_time.utcoffset() is not None
            assert s.expiration_time == settlement_close_for(s.symbol)
            assert s.expiration_time.isoformat() == "2026-07-21T00:00:00-04:00"
            assert s.genome_id == spec.genome_id
        # rejected markets got exactly one reject line each, emitted ones none
        for t, *_ in LADDER:
            n = sum(1 for m in mp_caplog.messages if f"symbol={t} " in m and "REJECT" in m)
            assert n == (0 if t in expected else 1), (t, n)
        # next hour: the same markets are GENOME_ALREADY_TRADED, nothing re-emitted
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=1)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        got = _rejects(mp_caplog, gs.REASON_ALREADY_TRADED)
        assert sorted(re.search(r"symbol=(\S+)", m).group(1) for m in got) == expected

    def test_not_executable_when_book_side_empty(self, mp_caplog):
        strat, _ = _strategy()
        rows = strat.build_rows(_obs(), TS)
        masked = [t for t, r in rows.items() if bool(G.to_mask(strat.genome, r))]
        empty = {t: (0.0, 0.05) for t in masked}  # buy_no taker needs yes_bid > 0
        assert strat.analyze(_obs(ladder=_ladder(quotes=empty))) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_EXECUTABLE)) == len(masked)

    def test_no_vintage_and_sigma_cap(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(datetime(2026, 7, 18, 12, 0, tzinfo=UTC)))
        assert strat.analyze(_obs(ts=strat.clock.now)) == []
        assert len(_rejects(mp_caplog, gs.REASON_NO_VINTAGE)) == len(LADDER)
        mp_caplog.clear()
        strat, _ = _strategy(sigma_cap=0.5)
        assert strat.analyze(_obs()) == []
        assert len(_rejects(mp_caplog, gs.REASON_SIGMA_CAP)) == len(LADDER)

    def test_mask_false_logged_per_market(self, mp_caplog):
        strat, _ = _strategy(genome=G.SEEDS["fr31b"])  # buy_yes, <12h windows, p_win >= 0.95: nothing at >=24h
        assert strat.analyze(_obs()) == []
        assert len(_rejects(mp_caplog, gs.REASON_MASK_FALSE)) == len(LADDER)


# ---------------------------------------------------------------------------
# decision cadence
# ---------------------------------------------------------------------------
class TestCadence:
    def test_top_of_hour_gating(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(TS + timedelta(minutes=5)))
        assert strat.analyze(_obs()) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_TOP_OF_HOUR)) == 1
        mp_caplog.clear()
        assert strat.analyze(_obs()) == []  # same hour, already logged as missed: silent
        assert _rejects(mp_caplog, gs.REASON_NOT_TOP_OF_HOUR) == []
        # inside the first poll of the next hour: evaluated once; the polls after it are one logged skip
        strat.clock.now = TS + timedelta(hours=1, seconds=30)
        sigs = strat.analyze(_obs(ts=TS + timedelta(hours=1)))
        assert sigs and all(int(s.expiration_time.timestamp()) > 0 for s in sigs)
        assert strat.stats["hours_evaluated"] == 1
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=1, seconds=45)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        strat.clock.now = TS + timedelta(hours=1, seconds=60)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_TOP_OF_HOUR)) == 1
        assert strat.stats["hours_evaluated"] == 1

    def test_decision_row_is_snapped_to_the_hour(self):
        strat, _ = _strategy(clock=Clock(TS + timedelta(seconds=40)))
        rows = strat.build_rows(_obs(), strat.clock.now)
        assert int(rows["KXHIGHNY-26JUL20-T83"]["ts_utc"]) == int(TS.timestamp())


# ---------------------------------------------------------------------------
# weather_bot wiring: env, clock injection, shadow mode
# ---------------------------------------------------------------------------
class TestBotShadowMode:
    def _bot(self, tmp_path, monkeypatch, mode_env="shadow", spec_mode="shadow"):
        import src.bots.weather_bot as weather_bot
        from src.bots.weather_bot import CITY_CONFIG, WeatherBot

        spec = _spec(G.SEEDS["nofilter_no"], mode=spec_mode)
        path = P.write_promoted(spec, tmp_path / f"{spec.genome_id}.json")
        monkeypatch.setenv("GENOME_STRATEGY_ID", path)
        monkeypatch.setenv("GENOME_STRATEGY_MODE", mode_env)
        monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
        monkeypatch.setattr("src.bots.weather_bot.time.sleep", lambda s: None)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return TS.astimezone(tz) if tz else TS.replace(tzinfo=None)

        monkeypatch.setattr(weather_bot, "datetime", FrozenDatetime)
        bot = WeatherBot()
        assert list(bot.strategies) == ["genome", "weather"]
        assert bot.genome_shadow is True
        genome = bot.strategies["genome"]
        assert isinstance(genome, GenomeStrategy) and genome.spec.genome_id == spec.genome_id
        # the bot's clock is the ET wall clock (frozen here); the strategy never reads one itself
        assert genome.clock().utcoffset() == timedelta(hours=-4) and genome.clock() == TS
        # swap the live MOS-backed provider for the replay table (no network in tests)
        genome.forecast_provider = ForecastVintageProvider.from_rows(VINTAGE_ROWS, lag_min=240)
        city = CITY_CONFIG["NY"]
        bot.CITIES = (city,)
        obs = MarketData(symbol=city.settlement_station, timestamp=TS, price=0.0, volume=0, bid=0.0, ask=0.0,
                         extra={"temperature_f": 75.0, "max_temp_today_f": 80.0, "forecast": []})
        bot.metar = MagicMock()
        bot.metar.fetch_latest.return_value = obs
        bot.nws = MagicMock()
        bot.nws.fetch_latest.return_value = MarketData(symbol=city.settlement_station, timestamp=TS, price=0.0,
                                                      volume=0, bid=0.0, ask=0.0, extra={"forecast": []})
        bot.kalshi = MagicMock()
        bot.kalshi.fetch_market_ladder.return_value = _ladder()
        bot.kalshi.fetch_orderbook.return_value = {"yes": [(0.4, 10.0)], "no": []}
        return bot, genome

    def test_shadow_emits_then_exactly_one_reject_and_never_reaches_process_signals(self, tmp_path, monkeypatch, mp_caplog):
        bot, genome = self._bot(tmp_path, monkeypatch)
        seen = []

        def _ps(signals, strategy_name, risk_manager, dashboard):
            seen.append(strategy_name)
            assert strategy_name != genome.name, "shadow-mode genome signal reached _process_signals"
            return False

        bot._process_signals = _ps
        v2 = MagicMock(analyze=MagicMock(return_value=[]))
        bot.strategies["weather"] = v2
        bot.tick(MagicMock(), MagicMock())
        assert seen == ["Meteorologist V2"]  # the waterfall continued to V2
        v2.analyze.assert_called_once()
        ladder_seen = v2.analyze.call_args.args[0].extra["ladder_markets"]
        assert [m.symbol for m in ladder_seen] == [t for t, *_ in LADDER]
        emits = [m for m in mp_caplog.messages if m.startswith("[Signal] EMIT strategy=" + genome.name)]
        shadows = _rejects(mp_caplog, "GENOME_SHADOW")
        assert len(emits) >= 1 and len(shadows) == len(emits)
        for e in emits:
            sym = re.search(r"symbol=(\S+)", e).group(1)
            assert sum(1 for s in shadows if f"symbol={sym} " in s) == 1
            price = float(re.search(r"price=(\S+)", e).group(1))
            row = genome.build_rows(_obs(), TS)[sym]
            assert price == pytest.approx(row["quote"] + 0.01, abs=1e-12)
        assert "contract=NO" in emits[0]
        assert genome.stats["signals"] == len(emits)

    def test_env_paper_cannot_override_a_shadow_spec(self, tmp_path, monkeypatch):
        bot, _ = self._bot(tmp_path, monkeypatch, mode_env="paper", spec_mode="shadow")
        assert bot.genome_shadow is True

    def test_bot_without_env_has_no_genome_and_v2_path_unchanged(self, monkeypatch):
        from src.bots.weather_bot import WeatherBot

        monkeypatch.delenv("GENOME_STRATEGY_ID", raising=False)
        bot = WeatherBot()
        assert list(bot.strategies) == ["weather"] and bot.genome_shadow is False


# ---------------------------------------------------------------------------
# replay parity on the frozen frame (dev box / factory container only)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not FRAMES_DIR or not Path(FRAMES_DIR).exists(), reason="MP_FACTORY_FRAMES not set")
class TestReplayParity:
    def test_one_seed_and_one_pick_replay_with_zero_discrepancies(self):
        pytest.importorskip("pandas")
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import factory_replay_parity as rp

        doc = rp.run_parity(Path(FRAMES_DIR), which="seeds,picks", only=["fr31b", "pick_ALL69"], log=lambda s: None)
        assert doc["n_markets_ladder"] == 1656 and doc["n_markets_search_frame"] == 1518
        for name in ("fr31b", "pick_ALL69"):
            r = doc["genomes"][name]
            assert r["n_discrepancies"] == 0 and r["n_offline"] == r["n_live"] > 0
            assert r["p_yes_max_abs_diff"] <= 1e-9 and r["column_mismatches"] == {}
            assert r["rows_frame_unvisited"] == 0
        assert doc["ok"] and doc["ok_strict"]
