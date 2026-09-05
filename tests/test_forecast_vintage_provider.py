"""ForecastVintageProvider (FR-F3.2): the availability-lag rule, replay/live constructions, fetched_at."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data.forecast_vintage_provider import (  # noqa: E402
    ForecastVintageError,
    ForecastVintageProvider,
    Vintage,
    format_init_time,
    parse_init_time,
)

UTC = timezone.utc


def _row(city, tdate, init, high, lead=4, spread=""):
    return {
        "city": city, "station": "KNYC", "target_date": tdate, "init_time_utc": init, "lead_hours": lead,
        "source": "gfs_mex", "forecast_high_f": high, "spread_f": spread, "provenance": "test",
    }


ROWS = [
    _row("NY", "2026-07-20", "2026-07-18T12:00:00Z", 85.0, lead=40),
    _row("NY", "2026-07-20", "2026-07-19T00:00:00Z", 86.0, lead=28),
    _row("NY", "2026-07-20", "2026-07-19T12:00:00Z", 87.0, lead=16),
    _row("NY", "2026-07-20", "2026-07-20T00:00:00Z", 88.0, lead=4),
    _row("NY", "2026-07-20", "2026-07-20T12:00:00Z", "", lead=-8),  # empty high: dropped
    _row("CHI", "2026-07-20", "2026-07-20T00:00:00Z", 90.0, lead=5),
]


class TestReplayRule:
    def test_latest_run_with_init_plus_lag_at_or_before_as_of(self):
        p = ForecastVintageProvider.from_rows(ROWS, lag_min=240)
        assert p.source == "replay" and p.lag_min == 240
        # 12Z run of 07-19 becomes usable at 16:00Z exactly (allow_exact_matches)
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 19, 16, 0, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-19T12:00:00Z" and v.forecast_high_f == 87.0 and v.lead_hours == 16
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 19, 15, 59, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-19T00:00:00Z" and v.forecast_high_f == 86.0
        # the 00Z run of the target day needs 04:00Z
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 20, 4, 0, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-20T00:00:00Z" and v.forecast_high_f == 88.0
        # nothing usable before the first run + lag
        assert p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 18, 15, 59, tzinfo=UTC)) is None
        # the empty-high row never becomes a vintage
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 21, 0, 0, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-20T00:00:00Z"
        # unknown key / other city
        assert p.latest_vintage("NY", "2026-07-21", datetime(2026, 7, 21, 0, 0, tzinfo=UTC)) is None
        assert p.latest_vintage("CHI", "2026-07-20", datetime(2026, 7, 20, 5, 0, tzinfo=UTC)).forecast_high_f == 90.0

    def test_lag_zero_is_the_phase2_convention(self):
        p = ForecastVintageProvider.from_rows(ROWS, lag_min=0)
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 19, 12, 0, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-19T12:00:00Z"

    def test_naive_as_of_refused(self):
        p = ForecastVintageProvider.from_rows(ROWS)
        with pytest.raises(ForecastVintageError, match="naive"):
            p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 19, 16, 0))

    def test_replay_vintage_has_no_fetched_at_and_no_spread(self):
        p = ForecastVintageProvider.from_rows(ROWS)
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        assert isinstance(v, Vintage) and v.fetched_at is None and v.sigma_f is None
        assert v.as_dict()["fetched_at"] is None

    def test_bad_constructions(self):
        with pytest.raises(ForecastVintageError):
            ForecastVintageProvider("bogus")
        with pytest.raises(ForecastVintageError):
            ForecastVintageProvider("replay")
        with pytest.raises(ForecastVintageError):
            ForecastVintageProvider("live", mos_provider=object())  # no clock
        with pytest.raises(ForecastVintageError):
            ForecastVintageProvider.from_archive_csv("does/not/exist.csv")

    def test_parse_and_format_init_time(self):
        for s in ("2026-07-19T12:00:00Z", "2026-07-19 12:00", "2026-07-19T12:00:00+00:00"):
            assert format_init_time(parse_init_time(s)) == "2026-07-19T12:00:00Z"


class TestReplayMatchesEvaluator:
    """The provider reproduces ``ev_analysis.forecast_vintage_table`` under the lag-shifted join."""

    def test_matches_forecast_vintage_table_on_the_real_archive(self):
        pd = pytest.importorskip("pandas")
        import src.backtest.ev_analysis as ev

        csv = REPO_ROOT / "data" / "forecast_archive" / "forecast_series_gfs_mex.csv"
        if not csv.exists():
            pytest.skip("forecast archive not on disk")
        archive = ev.load_forecast_archive(ev.GFS_MEX)
        lag = 240
        lagged = archive.copy()
        lagged["init_ts"] = lagged["init_ts"] + pd.Timedelta(minutes=lag)
        snaps = []
        for city, tdate in (("NY", "2026-07-20"), ("CHI", "2026-06-15"), ("LAX", "2026-07-25"), ("MIA", "2026-05-20")):
            for h in range(0, 48, 3):
                snaps.append({"city": city, "target_date": tdate,
                              "ts_utc": pd.Timestamp(tdate, tz="UTC") - pd.Timedelta(hours=36) + pd.Timedelta(hours=h)})
        ladders = pd.DataFrame(snaps)
        table = ev.forecast_vintage_table(ladders, lagged)
        p = ForecastVintageProvider.from_archive_csv(str(csv), lag_min=lag)
        n = 0
        for r in table.itertuples(index=False):
            v = p.latest_vintage(r.city, r.target_date, r.ts_utc.to_pydatetime())
            assert v is not None, (r.city, r.target_date, r.ts_utc)
            assert v.init_time_utc == r.init_time_utc
            assert v.forecast_high_f == float(r.forecast_high_f)
            assert v.lead_hours == int(r.lead_hours)
            n += 1
        # snapshots the evaluator dropped (no usable vintage) must be None here too
        got = {(r.city, r.target_date, int(r.ts_utc.timestamp())) for r in table.itertuples(index=False)}
        for s in snaps:
            key = (s["city"], s["target_date"], int(s["ts_utc"].timestamp()))
            if key not in got:
                assert p.latest_vintage(s["city"], s["target_date"], s["ts_utc"].to_pydatetime()) is None
        assert n > 20


# ---------------------------------------------------------------------------
# live path over a fake MOS provider
# ---------------------------------------------------------------------------
class _Forecast:
    def __init__(self, city, tdate, init, high, lead):
        self.row = {"city": city, "station": "KNYC", "target_date": tdate, "init_time_utc": init, "lead_hours": lead,
                    "source": "gfs_mex", "forecast_high_f": high, "spread_f": None, "provenance": "fake"}

    def as_row(self):
        return dict(self.row)


class _FakeMOS:
    """Archived runs keyed by runtime; anything else is a 404 (empty list, never cached)."""

    def __init__(self, runs):
        self.runs = runs
        self.calls = []

    def fetch_daily_highs(self, stations, model, runtime, *, source="gfs_mex", force_refresh=False):
        key = runtime.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.calls.append((tuple(stations), model, key))
        return list(self.runs.get(key, []))


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class TestLive:
    def test_live_honours_lag_records_fetched_at_and_retries_404(self, tmp_path):
        runs = {
            "2026-07-19T12:00:00Z": [_Forecast("NY", "2026-07-20", "2026-07-19T12:00:00Z", 87.0, 16),
                                     _Forecast("NY", "2026-07-21", "2026-07-19T12:00:00Z", 84.0, 40)],
            "2026-07-20T00:00:00Z": [_Forecast("NY", "2026-07-20", "2026-07-20T00:00:00Z", 88.0, 4)],
        }
        mos = _FakeMOS(runs)
        clock = _Clock(datetime(2026, 7, 19, 15, 0, 30, tzinfo=UTC))
        p = ForecastVintageProvider.live(mos, clock=clock, lag_min=240, cache_dir=str(tmp_path))
        # 15:00Z: the 12Z run is inside the lag window -> nothing usable
        assert p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 19, 15, 0, tzinfo=UTC)) is None
        assert any(k[2] == "2026-07-19T12:00:00Z" for k in mos.calls) is False  # never even requested
        # 16:00Z: usable; fetched_at comes from the injected clock
        clock.now = datetime(2026, 7, 19, 16, 0, 12, tzinfo=UTC)
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 19, 16, 0, tzinfo=UTC))
        assert v is not None and v.init_time_utc == "2026-07-19T12:00:00Z" and v.forecast_high_f == 87.0
        assert v.fetched_at == clock.now and v.lead_hours == 16
        log = tmp_path / "vintages.jsonl"
        lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert {ln["target_date"] for ln in lines} == {"2026-07-20", "2026-07-21"}
        assert all(ln["fetched_at"] == "2026-07-19T16:00:12Z" and ln["station"] == "KNYC" for ln in lines)
        # the 00Z run of 07-20 is a 404 until it is archived: requested again, never cached as a gap
        n_before = len(mos.calls)
        clock.now = datetime(2026, 7, 20, 4, 0, 5, tzinfo=UTC)
        mos.runs_backup = mos.runs
        mos.runs = {k: v for k, v in runs.items() if k != "2026-07-20T00:00:00Z"}
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 20, 4, 0, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-19T12:00:00Z"
        assert any(k[2] == "2026-07-20T00:00:00Z" for k in mos.calls[n_before:])
        mos.runs = runs
        v = p.latest_vintage("NY", "2026-07-20", datetime(2026, 7, 20, 4, 0, tzinfo=UTC))
        assert v.init_time_utc == "2026-07-20T00:00:00Z" and v.forecast_high_f == 88.0
        assert p.stats["runs_new"] == 2 and p.stats["fetch_errors"] == 0

    def test_live_warm_start_from_the_vintage_log(self, tmp_path):
        mos = _FakeMOS({"2026-07-19T12:00:00Z": [_Forecast("NY", "2026-07-20", "2026-07-19T12:00:00Z", 87.0, 16)]})
        clock = _Clock(datetime(2026, 7, 19, 16, 0, tzinfo=UTC))
        p = ForecastVintageProvider.live(mos, clock=clock, lag_min=240, cache_dir=str(tmp_path))
        assert p.latest_vintage("NY", "2026-07-20", clock.now).forecast_high_f == 87.0
        # a new process: the log warms the table and the run is NOT re-requested
        mos2 = _FakeMOS({})
        p2 = ForecastVintageProvider.live(mos2, clock=clock, lag_min=240, cache_dir=str(tmp_path))
        v = p2.latest_vintage("NY", "2026-07-20", clock.now)
        assert v.forecast_high_f == 87.0 and v.fetched_at == clock.now
        assert not any(k[2] == "2026-07-19T12:00:00Z" for k in mos2.calls)

    def test_live_network_fault_is_a_miss_not_a_crash(self, tmp_path):
        class _Boom:
            def fetch_daily_highs(self, *a, **k):
                raise RuntimeError("archive unreachable")

        clock = _Clock(datetime(2026, 7, 19, 16, 0, tzinfo=UTC))
        p = ForecastVintageProvider.live(_Boom(), clock=clock, lag_min=240, cache_dir=str(tmp_path))
        assert p.latest_vintage("NY", "2026-07-20", clock.now) is None
        assert p.stats["fetch_errors"] == 1

    def test_live_uses_env_cache_dir_and_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "env_cache"))
        p = ForecastVintageProvider.live(_FakeMOS({}), clock=_Clock(datetime(2026, 7, 19, tzinfo=UTC)))
        assert Path(p.cache_dir) == tmp_path / "env_cache" and Path(p.cache_dir).is_dir()
