"""GenomeStrategy (FR-F3.1/F3.3): row parity, signal shape, refusals, cadence, shadow mode, replay parity."""
from __future__ import annotations

import ast
import json
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


def _strategy(genome=None, clock=None, lag=240, state_dir=None, **over):
    genome = genome or G.SEEDS["nofilter_no"]
    spec = _spec(genome, **over)
    return GenomeStrategy(
        spec, clock=clock or Clock(TS),
        forecast_provider=ForecastVintageProvider.from_rows(VINTAGE_ROWS, lag_min=lag),
        fee_regime=load_regime(),
        calibration_provider=FrozenCalibrationProvider(str(CAL_DIR), source="gfs_mex"),
        state_dir=state_dir,
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
        # the late FIRST tick was a lost chance (F3 final red team defect 1): the next
        # hour evaluates but every visible city-day is GENOME_MISSED_HOUR
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=1, seconds=30)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        assert len(_rejects(mp_caplog, gs.REASON_MISSED_HOUR)) == len(LADDER)
        assert strat.stats["hours_evaluated"] == 1
        # a fresh strategy whose first tick is inside the first poll of the hour: evaluated
        # once and emits; the polls after it are one logged skip
        strat, _ = _strategy(clock=Clock(TS + timedelta(hours=1, seconds=30)))
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

    # -- missed-hour rule (F3 red team defect 1) -----------------------------
    def test_missed_hour_never_emits_later_in_the_market_day(self, mp_caplog):
        # H evaluated at the top of the hour with an EMPTY book: evaluated, nothing executable
        strat, _ = _strategy(clock=Clock(TS))
        rows = strat.build_rows(_obs(), TS)
        masked = [t for t, r in rows.items() if bool(G.to_mask(strat.genome, r))]
        empty = {t: (0.0, 0.05) for t in masked}
        assert strat.analyze(_obs(ladder=_ladder(quotes=empty))) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_EXECUTABLE)) == len(masked)
        # H+1: the tick lands 3 min late -> the hour is missed
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=1, minutes=3)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_TOP_OF_HOUR)) == 1
        # H+2 on time with a full book: the mask is true and executable, but the
        # strategy cannot know whether it was already true at H+1 -> GENOME_MISSED_HOUR
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert sorted(re.search(r"symbol=(\S+)", m).group(1) for m in got) == sorted(t for t, *_ in LADDER)
        assert strat.stats["signals"] == 0 and strat.stats.get("missed_days") == 1
        # ... and for the rest of the market-day
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=3)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=3))) == []
        assert len(_rejects(mp_caplog, gs.REASON_MISSED_HOUR)) == len(LADDER)

    def test_evaluated_false_then_true_emits_at_the_later_hour(self, mp_caplog):
        # the offline rule: FIRST masked EXECUTABLE snapshot -- an empty book at H
        # counts as evaluated, so the first executable hour H+1 is the trade
        strat, _ = _strategy(clock=Clock(TS))
        rows = strat.build_rows(_obs(), TS)
        expected = sorted(t for t, r in rows.items() if bool(G.to_mask(strat.genome, r)) and bool(r["executable"]))
        empty = {t: (0.0, 0.05) for t in expected}
        assert strat.analyze(_obs(ladder=_ladder(quotes=empty))) == []
        strat.clock.now = TS + timedelta(hours=1)
        sigs = strat.analyze(_obs(ts=TS + timedelta(hours=1)))
        assert sorted(x.symbol for x in sigs) == expected
        assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []
        assert strat.stats.get("missed_days", 0) == 0
        # the decision line carries the quote the limit came from (cadence checker, paper mode)
        decide = [m for m in mp_caplog.messages if m.startswith("[Genome] DECIDE ")]
        assert len(decide) == len(sigs)
        for m in decide:
            q = float(re.search(r"quote=(\S+)", m).group(1))
            lim = float(re.search(r"limit=(\S+)", m).group(1))
            assert lim == pytest.approx(q + 0.01, abs=1e-9)

    # -- persisted state (restart) --------------------------------------------
    def test_restart_with_persisted_state_does_not_re_emit(self, tmp_path, mp_caplog):
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        sigs = strat.analyze(_obs())
        assert sigs
        state_file = tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)
        doc = json.loads(state_file.read_text(encoding="utf-8"))
        assert doc["genome_id"] == spec.genome_id
        assert sorted(sym for _d, sym in doc["traded"]) == sorted(x.symbol for x in sigs)
        assert doc["last_hour_epoch"] == {"NY": int(TS.timestamp())}
        assert doc["missed_days"] == []
        assert not any(k for k in doc if "time" in k and k != "last_hour_epoch")
        # a fresh process one hour later: continuous coverage (no gap), traded markets are not re-emitted
        mp_caplog.clear()
        fresh, _ = _strategy(clock=Clock(TS + timedelta(hours=1)), state_dir=str(tmp_path))
        assert fresh.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        got = _rejects(mp_caplog, gs.REASON_ALREADY_TRADED)
        assert sorted(re.search(r"symbol=(\S+)", m).group(1) for m in got) == sorted(x.symbol for x in sigs)
        assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []

    def test_restart_after_downtime_marks_the_day_missed(self, tmp_path, mp_caplog):
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        rows = strat.build_rows(_obs(), TS)
        masked = [t for t, r in rows.items() if bool(G.to_mask(strat.genome, r))]
        empty = {t: (0.0, 0.05) for t in masked}
        assert strat.analyze(_obs(ladder=_ladder(quotes=empty))) == []  # evaluated at H, nothing executable
        # process down for two hours; the restarted strategy sees H+2 after a gap
        mp_caplog.clear()
        fresh, _ = _strategy(clock=Clock(TS + timedelta(hours=2)), state_dir=str(tmp_path))
        assert fresh.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        assert len(_rejects(mp_caplog, gs.REASON_MISSED_HOUR)) == len(LADDER)
        doc = json.loads((tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)).read_text(encoding="utf-8"))
        assert doc["missed_days"] == [["NY", "2026-07-20"]]

    def test_state_file_of_another_genome_is_refused(self, tmp_path):
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        strat.analyze(_obs())
        other = G.SEEDS["fr31a_taker"]
        other_spec = _spec(other)
        path = tmp_path / gs.STATE_FILE_FMT.format(genome_id=other_spec.genome_id)
        path.write_text(json.dumps({"genome_id": spec.genome_id}), encoding="utf-8")
        with pytest.raises(GenomeSpecMismatch):
            _strategy(genome=other, clock=Clock(TS), state_dir=str(tmp_path))

    # -- empty-ask sentinel (F3 red team defect 4) -----------------------------
    def test_zero_ask_sentinel_is_not_a_quote(self, mp_caplog):
        # kalshi_provider._parse_price returns 0.0 for a missing ask; a buy_yes taker
        # would otherwise see quote 0.00 -> limit 0.01 -> executable
        strat, _ = _strategy(genome=G.SEEDS["far_yes_taker"], clock=Clock(TS))
        rows = strat.build_rows(_obs(), TS)
        masked = [t for t, r in rows.items() if bool(G.to_mask(strat.genome, r))]
        assert masked, "fixture must mask at least one market for far_yes_taker"
        zero_ask = {t: (0.02, 0.0) for t in masked}
        rows0 = strat.build_rows(_obs(ladder=_ladder(quotes=zero_ask)), TS)
        for t in masked:
            assert np.isnan(rows0[t]["yes_ask"]) and np.isnan(rows0[t]["quote"]) and not bool(rows0[t]["executable"])
        assert strat.analyze(_obs(ladder=_ladder(quotes=zero_ask))) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_EXECUTABLE)) == len(masked)


# ---------------------------------------------------------------------------
# weather_bot wiring: env, clock injection, shadow mode
# ---------------------------------------------------------------------------
class TestBotShadowMode:
    def _bot(self, tmp_path, monkeypatch, mode_env="shadow", spec_mode="shadow", registry_status="CLOSED",
             expect_genome=True):
        import src.bots.weather_bot as weather_bot
        from src.bots.weather_bot import CITY_CONFIG, WeatherBot

        spec = _spec(G.SEEDS["nofilter_no"], mode=spec_mode, registry_status=registry_status)
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
        if not expect_genome:
            assert list(bot.strategies) == ["weather"] and bot.genome_spec is None
            return bot, None
        assert list(bot.strategies) == ["genome", "weather"]
        assert bot.genome_shadow is True
        genome = bot.strategies["genome"]
        assert genome.state_path and genome.state_path.startswith(str(tmp_path / "cache"))
        assert isinstance(genome, GenomeStrategy) and genome.spec.genome_id == spec.genome_id
        # the bot reaches every city on every tick, so an hour with no record at all
        # is a stalled loop rather than an archive gap (F3 review, missed-hour hole a)
        assert genome.tick_driven is True
        assert bot.genome_refused_reason is None
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

    def test_env_paper_on_a_shadow_spec_is_refused(self, tmp_path, monkeypatch, mp_caplog):
        bot, _ = self._bot(tmp_path, monkeypatch, mode_env="paper", spec_mode="shadow", expect_genome=False)
        assert bot.genome_shadow is False
        assert any("GenomeStrategy REFUSED" in m and "mode=shadow" in m for m in mp_caplog.messages)

    def test_paper_spec_is_refused_while_the_registry_is_not_proposed(self, tmp_path, monkeypatch, mp_caplog):
        # the spec CLAIMS PROPOSED; the tracked registry says CLOSED for family #1
        bot, _ = self._bot(tmp_path, monkeypatch, mode_env="paper", spec_mode="paper",
                           registry_status="PROPOSED", expect_genome=False)
        assert any("REFUSED paper mode" in m and "CLOSED" in m for m in mp_caplog.messages)

    def test_shadow_emit_line_carries_quote_and_limit(self, tmp_path, monkeypatch, mp_caplog):
        bot, genome = self._bot(tmp_path, monkeypatch)
        bot._process_signals = lambda signals, strategy_name, risk_manager, dashboard: False
        bot.tick(MagicMock(), MagicMock())
        emits = [m for m in mp_caplog.messages if m.startswith("[Signal] EMIT strategy=" + genome.name)]
        shadows = _rejects(mp_caplog, "GENOME_SHADOW")
        assert emits and len(shadows) == len(emits)
        for line in emits + shadows:
            q = float(re.search(r"quote=(\S+)", line).group(1))
            lim = float(re.search(r"limit=(\S+)", line).group(1))
            assert lim == pytest.approx(q + 0.01, abs=1e-9)

    @pytest.mark.parametrize("bad_mode", ["shadw", "off", "1", 'shadow"', "SHADOW ONLY"])
    def test_an_unrecognised_mode_is_refused_instead_of_read_as_not_shadow(
        self, tmp_path, monkeypatch, mp_caplog, bad_mode
    ):
        # F3 review: the env value was only ever COMPARED against "paper"/"shadow",
        # so a typo matched neither, tripped no refusal and was silently treated as
        # not-shadow -- on a PAPER spec a typo'd tightening request would fail OPEN.
        bot, _ = self._bot(tmp_path, monkeypatch, mode_env=bad_mode, spec_mode="paper",
                           registry_status="PROPOSED", expect_genome=False)
        assert bot.genome_shadow is False
        assert any("GenomeStrategy REFUSED" in m and repr(bad_mode.strip().lower()) in m
                   for m in mp_caplog.messages), mp_caplog.messages
        assert bot.genome_refused_reason and "GENOME_STRATEGY_MODE" in bot.genome_refused_reason

    def test_an_empty_mode_still_means_use_the_specs_own_mode(self, tmp_path, monkeypatch):
        bot, genome = self._bot(tmp_path, monkeypatch, mode_env="", spec_mode="shadow")
        assert bot.genome_shadow is True and genome is not None

    def test_every_refusal_records_a_reason_for_the_dashboard(self, tmp_path, monkeypatch):
        bot, _ = self._bot(tmp_path, monkeypatch, mode_env="paper", spec_mode="shadow", expect_genome=False)
        assert bot.genome_refused_reason and "mode=shadow" in bot.genome_refused_reason
        bot, _ = self._bot(tmp_path, monkeypatch, mode_env="paper", spec_mode="paper",
                           registry_status="PROPOSED", expect_genome=False)
        assert bot.genome_refused_reason and "registry status is CLOSED" in bot.genome_refused_reason

    def test_bot_without_env_has_no_genome_and_v2_path_unchanged(self, monkeypatch):
        from src.bots.weather_bot import WeatherBot

        monkeypatch.delenv("GENOME_STRATEGY_ID", raising=False)
        bot = WeatherBot()
        assert list(bot.strategies) == ["weather"] and bot.genome_shadow is False
        assert bot.genome_refused_reason is None  # nothing was asked for, nothing refused


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


# ---------------------------------------------------------------------------
# F3 final red team (2026-09-05): residual defects 1-2 -- fresh-deploy late first
# tick and bot-side poll failure are lost chances, exactly like a late tick.
# ---------------------------------------------------------------------------
def _symbols_of(msgs):
    return sorted(m.split("symbol=")[1].split()[0] for m in msgs)


def test_fresh_deploy_late_first_tick_marks_the_day_missed(mp_caplog):
    # no persisted state, no predecessor hour: the FIRST tick lands 3 min late at H+1
    strat, _ = _strategy(clock=Clock(TS + timedelta(hours=1, minutes=3)))
    assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
    assert len(_rejects(mp_caplog, gs.REASON_NOT_TOP_OF_HOUR)) == 1
    # H+2 on time: the chance at H+1 was lost -> GENOME_MISSED_HOUR, no signal
    mp_caplog.clear()
    strat.clock.now = TS + timedelta(hours=2)
    assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
    got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
    assert _symbols_of(got) == sorted(t for t, *_ in LADDER)
    assert strat.stats["signals"] == 0 and strat.stats.get("missed_days") == 1


def test_bot_poll_failure_is_a_missed_hour(mp_caplog):
    # H evaluated on time with an EMPTY book (nothing executable)
    strat, _ = _strategy(clock=Clock(TS))
    rows = strat.build_rows(_obs(), TS)
    masked = [t for t, r in rows.items() if bool(G.to_mask(strat.genome, r))]
    empty = {t: (0.0, 0.05) for t in masked}
    assert strat.analyze(_obs(ladder=_ladder(quotes=empty))) == []
    # H+1: the bot's Kalshi poll for the city failed -> recorded as a lost chance
    strat.clock.now = TS + timedelta(hours=1, seconds=5)
    strat.record_poll_failure("NY")
    assert strat.stats.get("poll_failures") == 1
    # H+2 on time, full book: GENOME_MISSED_HOUR for every market, no signal
    mp_caplog.clear()
    strat.clock.now = TS + timedelta(hours=2)
    assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
    got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
    assert _symbols_of(got) == sorted(t for t, *_ in LADDER)
    assert strat.stats["signals"] == 0


def test_poll_failure_after_an_evaluated_hour_is_ignored():
    strat, _ = _strategy(clock=Clock(TS))
    strat.analyze(_obs())  # H evaluated (and emitted)
    strat.clock.now = TS + timedelta(seconds=40)
    strat.record_poll_failure("NY")  # same hour, already done -> not a miss
    assert strat.stats.get("poll_failures", 0) == 0
    assert strat._hours[("NY", int(TS.timestamp()))] == "done"


# ---------------------------------------------------------------------------
# F3 review follow-up (2026-09-05)
#   1. a refusal path RAISED instead of skipping (log_rejection kwarg collision)
#   2. the missed-hour rule had three silent holes
#   3. a city-day closing was invisible
#   5. state-file durability and the refusal reason
# ---------------------------------------------------------------------------
SOURCE_FILES = (
    REPO_ROOT / "src" / "strategies" / "genome_strategy.py",
    REPO_ROOT / "src" / "bots" / "weather_bot.py",
)


def _obs_no_ladder(ts=TS):
    """What the bot hands the strategy when the city has NO ladder this tick."""
    return MarketData(symbol="KNYC", timestamp=ts, price=0.0, volume=0, bid=0.0, ask=0.0,
                      extra={"city_key": "NY", "kalshi_series": "KXHIGHNY", "settlement_station": "KNYC"})


def _empty_book_obs(strat, ts=TS):
    """The fixture ladder with every masked market's YES bid pulled: evaluated, nothing executable."""
    rows = strat.build_rows(_obs(ts=ts), ts)
    masked = [t for t, r in rows.items() if bool(G.to_mask(strat.genome, r))]
    return _obs(ts=ts, ladder=_ladder(ts=ts, quotes={t: (0.0, 0.05) for t in masked}))


class FlakyVintages:
    """``latest_vintage`` that FAILS (bumps the provider's fetch_errors, as the live
    one does on a network fault) or simply has nothing, at chosen decision hours."""

    def __init__(self, inner, fail_hours=(), blank_hours=()):
        self.inner = inner
        self.lag_min = inner.lag_min
        self.stats = {"fetch_errors": 0}
        self.fail_hours = {int(h.timestamp()) for h in fail_hours}
        self.blank_hours = {int(h.timestamp()) for h in blank_hours}

    def latest_vintage(self, city, target_date, as_of):
        ts = int(as_of.timestamp())
        if ts in self.fail_hours:
            self.stats["fetch_errors"] += 1  # what the provider does when the fetch raises
            return None
        if ts in self.blank_hours:
            return None
        return self.inner.latest_vintage(city, target_date, as_of)


class TestRejectContextIsNeverAKwargCollision:
    """``_reject`` forwards reason/strategy/symbol positionally into ``log_rejection``."""

    def test_no_call_site_passes_a_reserved_context_key(self):
        # maia: `_reject(..., reason="probability_engine")` raised
        # `TypeError: log_rejection() got multiple values for argument 'reason'`
        # from inside analyze() -- the documented skip crashed the tick instead.
        bad = []
        for path in SOURCE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name not in ("_reject", "log_rejection"):
                    continue
                for kw in node.keywords:
                    if kw.arg in gs.RESERVED_CONTEXT_KEYS:
                        bad.append(f"{path.name}:{node.lineno}: {name}(..., {kw.arg}=...)")
        assert bad == [], bad

    def test_reserved_keys_are_read_off_log_rejection_itself(self):
        assert gs.RESERVED_CONTEXT_KEYS == frozenset({"reason", "strategy", "symbol"})

    def test_a_colliding_context_key_is_renamed_not_raised(self, mp_caplog):
        strat, _ = _strategy()
        strat._reject("GENOME_NOT_EXECUTABLE", "KXHIGHNY-26JUL20-T83", reason="probability_engine", detail="x")
        line = _rejects(mp_caplog, "GENOME_NOT_EXECUTABLE")
        assert len(line) == 1
        assert "ctx_reason=probability_engine" in line[0] and "detail=x" in line[0]
        assert "reason=GENOME_NOT_EXECUTABLE" in line[0]  # the CODE keeps the reason= slot
        assert any("collides with log_rejection" in m for m in mp_caplog.messages)

    def test_probability_engine_refusal_is_a_skip_not_a_crash(self, mp_caplog, monkeypatch):
        strat, _ = _strategy()

        def _boom(*a, **k):
            raise ValueError("thin calibration for NY 2026-07-20")

        monkeypatch.setattr(strat, "_probabilities", _boom)
        assert strat.analyze(_obs()) == []  # must NOT raise: this escaped analyze() and killed the tick
        got = _rejects(mp_caplog, gs.REASON_NOT_EXECUTABLE)
        assert len(got) == len(LADDER)
        assert all("cause=probability_engine" in m for m in got)

    def test_bracket_spec_and_close_time_refusals_are_skips(self, mp_caplog):
        strat, _ = _strategy()
        ladder = _ladder()
        ladder[0].extra = dict(ladder[0].extra, strike_type=None, floor_strike=None, cap_strike=None)
        ladder[1].extra = dict(ladder[1].extra, close_time=None)
        assert strat.analyze(_obs(ladder=ladder)) is not None  # must not raise
        got = _rejects(mp_caplog, gs.REASON_NOT_EXECUTABLE)
        assert any("cause=bracket_spec" in m for m in got)
        assert any("cause=no_close_time_or_closed" in m for m in got)


class TestMissedHourHoles:
    """The rule that makes "the offline trade set is the first masked executable
    snapshot" true live. A hole in it breaks parity the one way parity cannot see."""

    def test_in_process_tick_gap_closes_the_city_day(self, mp_caplog):
        # hole (a): downtime was gated on the restart watermark, so a stalled loop /
        # hung HTTP call / paused container inside ONE process was not a miss.
        strat, _ = _strategy(clock=Clock(TS))
        strat.tick_driven = True  # the live bot polls every city every tick
        assert strat.analyze(_empty_book_obs(strat)) == []  # H evaluated, nothing executable
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=2)  # H+1 never reached the strategy at all
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert _symbols_of(got) == sorted(t for t, *_ in LADDER)
        assert all(f"lost_hour_utc={int(TS.timestamp()) + 3600}" in m for m in got)
        assert all(f"cause={gs.CAUSE_TICK_GAP}" in m for m in got)
        assert strat.stats["signals"] == 0 and strat.stats.get("missed_days") == 1

    def test_a_replay_driver_is_untouched_by_the_tick_gap_rule(self, mp_caplog):
        # parity: a replay visits only the hours the archive HAS; a candle-less hour
        # is a data gap, and tick_driven stays False for every offline driver.
        strat, _ = _strategy(clock=Clock(TS))
        assert strat.tick_driven is False
        assert strat.analyze(_empty_book_obs(strat)) == []
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2)))  # emits, exactly as before
        assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []

    def test_an_hour_the_bot_looked_at_with_no_ladder_is_a_data_gap(self, mp_caplog):
        # the runbook's distinction: no candle in the archive is NOT a miss, even
        # though the strategy evaluated nothing at that hour.
        strat, _ = _strategy(clock=Clock(TS))
        strat.tick_driven = True
        assert strat.analyze(_empty_book_obs(strat)) == []
        strat.clock.now = TS + timedelta(hours=1)
        assert strat.analyze(_obs_no_ladder(TS + timedelta(hours=1))) == []  # the bot LOOKED: nothing there
        assert strat._hours[("NY", int(TS.timestamp()) + 3600)] == gs.HOUR_NO_DATA
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2)))  # emits: no chance was lost
        assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []
        assert strat.stats.get("missed_days", 0) == 0

    def test_record_no_ladder_marks_the_same_data_gap(self):
        strat, _ = _strategy(clock=Clock(TS))
        strat.tick_driven = True
        strat.analyze(_empty_book_obs(strat))
        strat.clock.now = TS + timedelta(hours=1, seconds=5)
        strat.record_no_ladder("NY")
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2)))
        assert strat.stats.get("missed_days", 0) == 0

    def test_a_no_data_hour_is_still_evaluable_when_the_ladder_comes_back(self):
        strat, _ = _strategy(clock=Clock(TS))
        assert strat.analyze(_obs_no_ladder(TS)) == []  # first tick of the hour: no ladder
        assert strat._hours[("NY", int(TS.timestamp()))] == gs.HOUR_NO_DATA
        assert strat.analyze(_obs())  # a later tick of the SAME hour has one -> evaluated
        assert strat._hours[("NY", int(TS.timestamp()))] == gs.HOUR_DONE

    def test_a_failed_forecast_fetch_closes_the_city_day(self, mp_caplog):
        # hole (b): latest_vintage returns None on a network fault too, and only
        # GENOME_NO_VINTAGE was logged -- the hour was silently forfeited.
        strat, _ = _strategy(clock=Clock(TS))
        strat.forecast_provider = FlakyVintages(strat.forecast_provider, fail_hours=[TS])
        assert strat.analyze(_obs()) == []
        no_vintage = _rejects(mp_caplog, gs.REASON_NO_VINTAGE)
        assert len(no_vintage) == len(LADDER) and all("fetch_failed=True" in m for m in no_vintage)
        assert any(
            "DAY CLOSED" in m and f"cause={gs.CAUSE_VINTAGE_FETCH_FAILURE}" in m for m in mp_caplog.messages
        )
        # the next hour prices fine, but the day is closed
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=1)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        assert len(_rejects(mp_caplog, gs.REASON_MISSED_HOUR)) == len(LADDER)

    def test_a_vintage_that_does_not_exist_yet_is_not_a_miss(self, mp_caplog):
        # the mirror image: no fetch error means the frame has no row for the hour
        # either, so the day stays open and the next hour emits normally.
        strat, _ = _strategy(clock=Clock(TS))
        strat.forecast_provider = FlakyVintages(strat.forecast_provider, blank_hours=[TS])
        assert strat.analyze(_obs()) == []
        assert all("fetch_failed=False" in m for m in _rejects(mp_caplog, gs.REASON_NO_VINTAGE))
        strat.clock.now = TS + timedelta(hours=1)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1)))
        assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []


class TestClosureIsVisible:
    def test_the_closure_is_logged_once_per_city_day_with_the_lost_hour_and_cause(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(TS + timedelta(hours=1, minutes=3)))  # late FIRST tick
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        closed = [m for m in mp_caplog.messages if m.startswith("[Genome] DAY CLOSED ")]
        assert len(closed) == 1, closed  # one city-day, one line
        assert "city=NY" in closed[0] and "target_date=2026-07-20" in closed[0]
        assert f"lost_hour_utc={int(TS.timestamp()) + 3600}" in closed[0]
        assert f"cause={gs.CAUSE_LATE_TICK}" in closed[0]
        # ... and the rejects that follow carry the same two facts
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert got and all(f"cause={gs.CAUSE_LATE_TICK}" in m for m in got)
        # the rest of the day adds no second closure line
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=3)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=3))) == []
        assert [m for m in mp_caplog.messages if m.startswith("[Genome] DAY CLOSED ")] == []

    def test_a_recorded_miss_logs_one_line_of_its_own(self, mp_caplog):
        # record_poll_failure marked the hour and logged NOTHING; the hour left no
        # trace at all.
        strat, _ = _strategy(clock=Clock(TS))
        strat.record_poll_failure("NY")
        miss = [m for m in mp_caplog.messages if m.startswith("[Genome] MISS ")]
        assert len(miss) == 1
        assert f"city=NY hour_utc={int(TS.timestamp())}" in miss[0]
        assert f"cause={gs.CAUSE_POLL_FAILURE}" in miss[0]
        assert strat.stats.get("missed_hours") == 1
        # a tick that arrives OUTSIDE the tolerance cannot evaluate the hour, so the
        # hour stays lost -- and says so once, not once per poll
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(minutes=5)
        assert strat.analyze(_obs()) == []
        assert [m for m in mp_caplog.messages if m.startswith("[Genome] MISS ")] == []
        assert strat.stats.get("missed_hours") == 1
        assert strat._hours[("NY", int(TS.timestamp()))] == gs.HOUR_MISSED_PREFIX + gs.CAUSE_POLL_FAILURE

    def test_the_observation_failure_cause_travels_to_the_reject(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(TS))
        strat.record_missed_hour("NY", gs.CAUSE_OBSERVATION_FAILURE)
        strat.clock.now = TS + timedelta(hours=1)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert got and all(f"cause={gs.CAUSE_OBSERVATION_FAILURE}" in m for m in got)
        assert strat.stats.get("poll_failures", 0) == 0  # not every miss is a poll failure


class TestStateDurability:
    def test_a_corrupt_state_file_starts_fresh_loudly_instead_of_refusing(self, tmp_path, mp_caplog):
        # a Pi power cut leaves exactly this; json.load raised, WeatherBot's broad
        # except logged one ERROR and ran V2 only for the rest of the deploy.
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        state_file = tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)
        strat.analyze(_obs())
        assert state_file.exists()
        state_file.write_text("", encoding="utf-8")  # zero bytes
        mp_caplog.clear()
        fresh, _ = _strategy(clock=Clock(TS + timedelta(hours=1)), state_dir=str(tmp_path))  # must not raise
        assert fresh.state_recovered_from and "JSONDecodeError" in fresh.state_recovered_from
        assert any("STARTING FROM AN EMPTY STATE" in m for m in mp_caplog.messages)
        assert fresh.stats.get("state_resets") == 1
        assert fresh.analyze(_obs(ts=TS + timedelta(hours=1)))  # and it trades on

    def test_a_truncated_state_file_is_recovered_the_same_way(self, tmp_path):
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        strat.analyze(_obs())
        state_file = tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)
        state_file.write_text(state_file.read_text(encoding="utf-8")[:40], encoding="utf-8")
        fresh, _ = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        assert fresh.state_recovered_from is not None and fresh._traded == set()

    def test_a_state_file_for_another_genome_is_still_a_refusal(self, tmp_path):
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        strat.analyze(_obs())
        other = G.SEEDS["fr31a_taker"]
        path = tmp_path / gs.STATE_FILE_FMT.format(genome_id=_spec(other).genome_id)
        path.write_text(json.dumps({"genome_id": spec.genome_id}), encoding="utf-8")
        with pytest.raises(GenomeSpecMismatch):
            _strategy(genome=other, clock=Clock(TS), state_dir=str(tmp_path))

    def test_the_state_file_is_fsynced_before_the_rename(self, tmp_path, monkeypatch):
        # os.replace is atomic but not durable: without the fsync the rename can
        # land with the file's blocks unwritten -- the zero-byte file above.
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        monkeypatch.setattr(os, "fsync", lambda fd: calls.append(("fsync", fd)) or real_fsync(fd))
        monkeypatch.setattr(os, "replace", lambda a, b: calls.append(("replace", a)) or real_replace(a, b))
        strat, _ = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        strat.analyze(_obs())
        kinds = [k for k, _ in calls]
        assert "fsync" in kinds and "replace" in kinds
        assert kinds.index("fsync") < kinds.index("replace")

    def test_the_closure_cause_survives_a_restart(self, tmp_path, mp_caplog):
        strat, spec = _strategy(clock=Clock(TS + timedelta(hours=1, minutes=3)), state_dir=str(tmp_path))
        strat.analyze(_obs(ts=TS + timedelta(hours=1)))  # late first tick
        strat.clock.now = TS + timedelta(hours=2)
        strat.analyze(_obs(ts=TS + timedelta(hours=2)))  # closes the day
        doc = json.loads((tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)).read_text(encoding="utf-8"))
        assert doc["missed_days"] == [["NY", "2026-07-20"]]
        assert doc["missed_causes"]["NY|2026-07-20"] == [int(TS.timestamp()) + 3600, gs.CAUSE_LATE_TICK]
        mp_caplog.clear()
        fresh, _ = _strategy(clock=Clock(TS + timedelta(hours=3)), state_dir=str(tmp_path))
        assert fresh.analyze(_obs(ts=TS + timedelta(hours=3))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert got and all(f"cause={gs.CAUSE_LATE_TICK}" in m for m in got)

    def test_a_state_file_written_before_missed_causes_still_loads(self, tmp_path, mp_caplog):
        strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        path = tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)
        path.write_text(json.dumps({"genome_id": spec.genome_id, "last_hour_epoch": {},
                                    "missed_days": [["NY", "2026-07-20"]], "traded": []}), encoding="utf-8")
        fresh, _ = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        assert fresh.analyze(_obs()) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert got and all("cause=unknown" in m for m in got)


# ---------------------------------------------------------------------------
# F3 remediation (2026-09-05): the missed-hour bookkeeping flooded the only
# diagnostic surface it exists to feed, and one transient tick-level failure was
# read as a final verdict on the hour.
# ---------------------------------------------------------------------------
class TestALostHourIsRecordedOnce:
    """maia ticks every ~23 s, so the hook fires ~155x per lost city-hour per city.

    Every one of those used to re-mark the hour, re-count ``missed_hours`` and
    re-emit ``[Genome] MISS`` -- ~620 lines/hour across four cities against a
    500-line log tail, i.e. one outage erased the whole diagnostic window.
    """

    HOUR_TICKS = 155  # what the reviewer measured for ONE distinct lost city-hour

    def test_a_sustained_outage_records_one_miss_per_city_hour(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(TS))
        for i in range(self.HOUR_TICKS):
            strat.clock.now = TS + timedelta(seconds=23 * i)  # all inside hour H
            strat.record_missed_hour("NY", gs.CAUSE_OBSERVATION_FAILURE)
        miss = [m for m in mp_caplog.messages if m.startswith("[Genome] MISS ")]
        assert len(miss) == 1, f"{len(miss)} lines for one lost city-hour"
        assert strat.stats.get("missed_hours") == 1
        assert strat._hours[("NY", int(TS.timestamp()))] == gs.HOUR_MISSED_PREFIX + gs.CAUSE_OBSERVATION_FAILURE

    def test_a_sustained_poll_outage_counts_one_poll_failure_per_city_hour(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(TS))
        for i in range(self.HOUR_TICKS):
            strat.clock.now = TS + timedelta(seconds=23 * i)
            strat.record_poll_failure("NY")
        assert strat.stats.get("poll_failures") == 1 and strat.stats.get("missed_hours") == 1
        assert len([m for m in mp_caplog.messages if m.startswith("[Genome] MISS ")]) == 1

    def test_a_late_hour_logs_one_reject_however_often_it_is_polled(self, mp_caplog):
        # the same flood on the other recording path: every late poll of a lost
        # hour used to add a GENOME_NOT_TOP_OF_HOUR line
        strat, _ = _strategy(clock=Clock(TS + timedelta(minutes=5)))
        for i in range(20):
            strat.clock.now = TS + timedelta(minutes=5, seconds=23 * i)
            assert strat.analyze(_obs()) == []
        assert len(_rejects(mp_caplog, gs.REASON_NOT_TOP_OF_HOUR)) == 1

    def test_the_hour_is_still_lost_and_still_closes_the_day(self, mp_caplog):
        # idempotence must not turn a real miss into a no-op
        strat, _ = _strategy(clock=Clock(TS))
        assert strat.analyze(_empty_book_obs(strat)) == []  # H evaluated, nothing executable
        for i in range(40):
            strat.clock.now = TS + timedelta(hours=1, seconds=23 * i)
            strat.record_missed_hour("NY", gs.CAUSE_OBSERVATION_FAILURE)
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert _symbols_of(got) == sorted(t for t, *_ in LADDER)
        assert all(f"cause={gs.CAUSE_OBSERVATION_FAILURE}" in m for m in got)
        assert strat.stats.get("missed_days") == 1


class TestATransientTickFailureIsRecoverable:
    """A bot-side miss reports ONE TICK, not the hour.

    maia's control emits ``KXHIGHNY-26JUL20-T83`` at H+23 s. With one METAR
    failure at H+0 s the identical H+23 s tick emitted nothing and the city-day
    closed -- 97 s INSIDE the 120 s tolerance, and for three market-days at once
    (``_ladder_for_city`` returns D-1/D/D+1). ``HOUR_NO_DATA`` already had this
    recovery hatch; a bot-side skip is equally recoverable within its own hour.
    """

    def test_the_next_in_tolerance_tick_evaluates_the_hour_after_all(self, mp_caplog):
        strat, _ = _strategy(clock=Clock(TS))
        strat.tick_driven = True
        strat.record_missed_hour("NY", gs.CAUSE_OBSERVATION_FAILURE)  # H+0 s: no METAR, city skipped
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(seconds=23)  # the very next maia tick
        sigs = strat.analyze(_obs())
        assert sigs, "an in-tolerance tick with a ladder must evaluate the hour"
        assert strat._hours[("NY", int(TS.timestamp()))] == gs.HOUR_DONE
        assert strat.stats.get("missed_hours", 0) == 0 and strat.stats.get("hours_recovered") == 1
        assert [m for m in mp_caplog.messages if m.startswith("[Genome] DAY CLOSED ")] == []
        recovered = [m for m in mp_caplog.messages if m.startswith("[Genome] MISS RECOVERED ")]
        assert len(recovered) == 1 and f"cause={gs.CAUSE_OBSERVATION_FAILURE}" in recovered[0]
        # ... and the market-day is still open at the next hour
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=1)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []
        assert strat.stats.get("missed_days", 0) == 0

    def test_a_recovered_poll_failure_is_not_counted_as_one(self):
        strat, _ = _strategy(clock=Clock(TS))
        strat.record_poll_failure("NY")
        assert strat.stats["poll_failures"] == 1
        strat.clock.now = TS + timedelta(seconds=23)
        assert strat.analyze(_obs())
        assert strat.stats["poll_failures"] == 0 and strat.stats["missed_hours"] == 0

    def test_an_hour_no_in_tolerance_tick_ever_evaluates_is_still_lost(self, mp_caplog):
        # the semantics that must survive: only an IN-TOLERANCE evaluation recovers
        strat, _ = _strategy(clock=Clock(TS))
        assert strat.analyze(_empty_book_obs(strat)) == []
        strat.clock.now = TS + timedelta(hours=1, seconds=5)
        strat.record_missed_hour("NY", gs.CAUSE_OBSERVATION_FAILURE)
        strat.clock.now = TS + timedelta(hours=1, minutes=4)  # back, but far too late
        assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
        assert strat._hours[("NY", int(TS.timestamp()) + 3600)].startswith(gs.HOUR_MISSED_PREFIX)
        mp_caplog.clear()
        strat.clock.now = TS + timedelta(hours=2)
        assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        assert len(_rejects(mp_caplog, gs.REASON_MISSED_HOUR)) == len(LADDER)

    def test_a_restart_gap_and_a_tick_gap_still_close_the_day(self, tmp_path, mp_caplog):
        # recovery is per-hour and per-tick; it must not reach the two gap rules
        strat, _ = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
        assert strat.analyze(_empty_book_obs(strat)) == []
        fresh, _ = _strategy(clock=Clock(TS + timedelta(hours=2)), state_dir=str(tmp_path))
        assert fresh.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert len(got) == len(LADDER) and all(f"cause={gs.CAUSE_DOWNTIME}" in m for m in got)
        mp_caplog.clear()
        tick, _ = _strategy(clock=Clock(TS))
        tick.tick_driven = True
        assert tick.analyze(_empty_book_obs(tick)) == []
        tick.clock.now = TS + timedelta(hours=2)  # H+1 never reached the strategy
        assert tick.analyze(_obs(ts=TS + timedelta(hours=2))) == []
        got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
        assert len(got) == len(LADDER) and all(f"cause={gs.CAUSE_TICK_GAP}" in m for m in got)


def test_the_first_recorded_cause_of_a_lost_hour_wins(mp_caplog):
    # a poll failure at H+5 s is what lost the hour; the observation failure and
    # the late tick that follow only inherit an hour that was already gone, and
    # "[Genome] DAY CLOSED cause=..." must not name one of them.
    strat, _ = _strategy(clock=Clock(TS))
    assert strat.analyze(_empty_book_obs(strat)) == []
    h1 = int(TS.timestamp()) + 3600
    strat.clock.now = TS + timedelta(hours=1, seconds=5)
    strat.record_poll_failure("NY")
    strat.clock.now = TS + timedelta(hours=1, seconds=30)
    strat.record_missed_hour("NY", gs.CAUSE_OBSERVATION_FAILURE)
    strat.clock.now = TS + timedelta(hours=1, minutes=3)  # a late tick over the same hour
    assert strat.analyze(_obs(ts=TS + timedelta(hours=1))) == []
    assert strat._hours[("NY", h1)] == gs.HOUR_MISSED_PREFIX + gs.CAUSE_POLL_FAILURE
    mp_caplog.clear()
    strat.clock.now = TS + timedelta(hours=2)
    assert strat.analyze(_obs(ts=TS + timedelta(hours=2))) == []
    closed = [m for m in mp_caplog.messages if m.startswith("[Genome] DAY CLOSED ")]
    assert len(closed) == 1 and f"cause={gs.CAUSE_POLL_FAILURE}" in closed[0]
    assert all(f"cause={gs.CAUSE_POLL_FAILURE}" in m for m in _rejects(mp_caplog, gs.REASON_MISSED_HOUR))


def test_a_pruned_hour_is_not_a_tick_gap(mp_caplog):
    # _prune drops _hours entries older than KEEP_HOURS_S, and _lost_chance read
    # the resulting hole as "the loop was not there". A city whose ladder was
    # absent for two days recorded a data gap EVERY hour and still got a tick_gap.
    strat, _ = _strategy(clock=Clock(TS))
    strat.tick_driven = True
    assert strat.analyze(_empty_book_obs(strat)) == []  # H evaluated
    hours = gs.KEEP_HOURS_S // 3600 + 3
    for i in range(1, hours):  # the bot LOOKED every hour and there was no ladder
        strat.clock.now = TS + timedelta(hours=i, seconds=5)
        strat.record_no_ladder("NY")
    mp_caplog.clear()
    strat.clock.now = TS + timedelta(hours=hours)
    strat.analyze(_obs(ts=TS + timedelta(hours=hours)))
    pruned = int(TS.timestamp()) + 3600
    assert ("NY", pruned) not in strat._hours, "the fixture must actually prune the first gap hour"
    assert [m for m in mp_caplog.messages if m.startswith("[Genome] DAY CLOSED ")] == []
    assert _rejects(mp_caplog, gs.REASON_MISSED_HOUR) == []
    assert strat.stats.get("missed_days", 0) == 0


def test_a_failed_vintage_fetch_closure_survives_a_restart(tmp_path, mp_caplog):
    # _analyze saves BEFORE the per-day loop, but this closure fires from
    # _rows_for_day inside it: the restart reopened the city-day and re-emitted.
    strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
    strat.forecast_provider = FlakyVintages(strat.forecast_provider, fail_hours=[TS])
    assert strat.analyze(_obs()) == []
    doc = json.loads((tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)).read_text(encoding="utf-8"))
    assert doc["missed_days"] == [["NY", "2026-07-20"]]
    assert doc["missed_causes"]["NY|2026-07-20"] == [int(TS.timestamp()), gs.CAUSE_VINTAGE_FETCH_FAILURE]
    mp_caplog.clear()
    fresh, _ = _strategy(clock=Clock(TS + timedelta(hours=1)), state_dir=str(tmp_path))
    assert fresh.analyze(_obs(ts=TS + timedelta(hours=1))) == []
    got = _rejects(mp_caplog, gs.REASON_MISSED_HOUR)
    assert _symbols_of(got) == sorted(t for t, *_ in LADDER)
    assert all(f"cause={gs.CAUSE_VINTAGE_FETCH_FAILURE}" in m for m in got)


#: state files that DECODE but hold the wrong shapes -- each raised straight out of
#: the constructor into WeatherBot.__init__'s broad except, refusing the genome for
#: the rest of the deploy, which is exactly what the unreadable-file recovery exists
#: to prevent.
CORRUPT_STATE_SHAPES = {
    "last_hour_epoch_is_a_list": {"last_hour_epoch": ["NYC"], "missed_days": [], "traded": []},
    "an_hour_that_is_not_a_number": {"last_hour_epoch": {"NY": "soon"}, "missed_days": [], "traded": []},
    "missed_days_entry_is_not_a_pair": {"last_hour_epoch": {}, "missed_days": [["NY"]], "traded": []},
    "traded_is_a_bare_string": {"last_hour_epoch": {}, "missed_days": [], "traded": "KXHIGHNY-26JUL20-T83"},
    "missed_causes_is_a_list": {"last_hour_epoch": {}, "missed_days": [["NY", "2026-07-20"]],
                                "missed_causes": ["NY|2026-07-20"], "traded": []},
}


@pytest.mark.parametrize("shape", sorted(CORRUPT_STATE_SHAPES))
def test_a_decodable_but_corrupt_state_file_starts_fresh_instead_of_refusing(shape, tmp_path, mp_caplog):
    strat, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
    path = tmp_path / gs.STATE_FILE_FMT.format(genome_id=spec.genome_id)
    doc = dict(CORRUPT_STATE_SHAPES[shape], genome_id=spec.genome_id)
    path.write_text(json.dumps(doc), encoding="utf-8")
    mp_caplog.clear()
    fresh, _ = _strategy(clock=Clock(TS), state_dir=str(tmp_path))  # must not raise
    assert fresh.state_recovered_from, shape
    assert fresh.stats.get("state_resets") == 1
    assert any("STARTING FROM AN EMPTY STATE" in m for m in mp_caplog.messages)
    # nothing half-applied survives the reset
    assert fresh._traded == set() and fresh._missed_days == {} and fresh._last_hour == {}
    assert fresh.analyze(_obs())  # and it trades on


def test_a_state_file_naming_another_genome_is_not_read_as_corruption(tmp_path):
    # the shape-error recovery must not swallow the one refusal that IS fatal
    _s, spec = _strategy(clock=Clock(TS), state_dir=str(tmp_path))
    other = G.SEEDS["fr31a_taker"]
    path = tmp_path / gs.STATE_FILE_FMT.format(genome_id=_spec(other).genome_id)
    path.write_text(json.dumps({"genome_id": spec.genome_id, "last_hour_epoch": ["NYC"]}), encoding="utf-8")
    with pytest.raises(GenomeSpecMismatch):
        _strategy(genome=other, clock=Clock(TS), state_dir=str(tmp_path))
