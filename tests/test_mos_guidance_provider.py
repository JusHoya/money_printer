"""Tests for the archived MOS/NBM guidance provider (PRD FR-1.6).

Golden-keyed to two real captured API responses under ``tests/fixtures/mos/``:
the 2026-06-01 00Z and 2026-01-10 00Z GFS-MEX runs for all four settlement
stations. Both were fetched live from
``https://mesonet.agron.iastate.edu/api/1/mos.json`` on 2026-07-26 and trimmed
to the fields the module consumes.

Run with::

    python -m pytest tests/test_mos_guidance_provider.py -v
"""

from __future__ import annotations

import json
import os

import pytest

from src.data.iem_cli_provider import STATIONS
from src.data.mos_guidance_provider import (
    FORECAST_FIELDS,
    MODEL_WHITELIST,
    MODELS_WITH_NX,
    GuidanceForecast,
    MOSGuidanceError,
    MOSGuidanceProvider,
    city_code,
    format_runtime,
    lead_hours_for,
    local_day_start_utc,
    parse_runtime,
    run_times,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "mos")
SUMMER_RUN = "2026-06-01T00:00:00Z"
WINTER_RUN = "2026-01-10T00:00:00Z"


def _fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


class _Resp:
    def __init__(self, status, payload=None, text="", url="http://fixture"):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    """Records the exact query it was asked to issue, then replays a fixture."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, list(params or [])))
        return self.response(url, params) if callable(self.response) else self.response


def _provider(response, tmp_path):
    return MOSGuidanceProvider(
        cache_dir=str(tmp_path / "cache"),
        session=_Session(response),
        request_pause=0.0,
    )


# ---------------------------------------------------------------------------
# Timezone / lead arithmetic
# ---------------------------------------------------------------------------
def test_local_day_start_handles_dst_not_a_fixed_offset():
    """A fixed UTC offset would put these two 4 hours apart; the tz db does not."""
    winter = local_day_start_utc("2026-01-10", "America/New_York")
    summer = local_day_start_utc("2026-06-01", "America/New_York")
    assert winter.hour == 5  # EST = UTC-5
    assert summer.hour == 4  # EDT = UTC-4


def test_lead_hours_day_of_is_small_and_positive_for_a_00z_run():
    init = parse_runtime("2026-06-01T00:00:00Z")
    assert lead_hours_for(init, "2026-06-01", "America/New_York") == 4
    assert lead_hours_for(init, "2026-06-01", "America/Chicago") == 5
    assert lead_hours_for(init, "2026-06-01", "America/Los_Angeles") == 7
    # winter shifts each by an hour -- the whole point of using zoneinfo
    winit = parse_runtime("2026-01-10T00:00:00Z")
    assert lead_hours_for(winit, "2026-01-10", "America/New_York") == 5
    assert lead_hours_for(winit, "2026-01-10", "America/Los_Angeles") == 8


def test_lead_hours_is_negative_when_the_run_starts_after_the_local_day():
    init = parse_runtime("2026-06-01T18:00:00Z")  # 14:00 EDT, mid-day
    assert lead_hours_for(init, "2026-06-01", "America/New_York") == -14


def test_run_times_grid():
    rts = run_times("2026-06-01", "2026-06-02", (0, 12))
    assert [format_runtime(r) for r in rts] == [
        "2026-06-01T00:00:00Z",
        "2026-06-01T12:00:00Z",
        "2026-06-02T00:00:00Z",
        "2026-06-02T12:00:00Z",
    ]


def test_city_code_comes_from_the_kalshi_series_ticker():
    assert city_code("KNYC") == "NY"
    assert city_code("KMDW") == "CHI"
    assert city_code("KLAX") == "LAX"
    assert city_code("KMIA") == "MIA"


# ---------------------------------------------------------------------------
# n_x semantics
# ---------------------------------------------------------------------------
def test_only_local_evening_nx_rows_become_daily_highs(tmp_path):
    """A 00Z MEX run holds 15 rows per station: 8 daytime maxima (00Z ftimes,
    local evening) and 7 overnight minima (12Z ftimes, local morning)."""
    p = _provider(_Resp(200, _fixture("mex_4city_2026-06-01T00Z.json")), tmp_path)
    fcs = p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)
    assert len(fcs) == 32  # 4 stations x 8 maxima
    assert p.stats["rows_min_discarded"] == 28  # 4 stations x 7 minima
    assert p.stats["rows_unclassified_hour"] == 0
    assert p.stats["rows_nx_null"] == 0
    assert {f.city for f in fcs} == {"NY", "CHI", "LAX", "MIA"}


def test_the_discarded_half_really_is_the_overnight_minimum(tmp_path):
    """Guards the sign of the whole pipeline: keeping the wrong half would
    calibrate the daily *low* against the daily *high* and still produce a
    plausible-looking file."""
    payload = _fixture("mex_4city_2026-06-01T00Z.json")
    p = _provider(_Resp(200, payload), tmp_path)
    kept = {
        (f.station, f.target_date): f.forecast_high_f
        for f in p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)
    }
    # Every 12Z-ftime n_x in the raw payload is lower than the 00Z-ftime n_x
    # for the same station and forecast day.
    dropped = [
        r
        for r in payload["data"]
        if r["n_x"] is not None and r["ftime"].endswith("12:00")
    ]
    assert dropped
    for row in dropped:
        day = row["ftime"][:10]
        same_day_max = kept.get((row["station"], day))
        if same_day_max is not None:
            assert float(row["n_x"]) < same_day_max, (row["station"], day)


def test_day_of_row_is_the_first_max_of_a_00z_run(tmp_path):
    p = _provider(_Resp(200, _fixture("mex_4city_2026-06-01T00Z.json")), tmp_path)
    fcs = p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)
    ny = sorted((f for f in fcs if f.city == "NY"), key=lambda f: f.target_date)
    assert ny[0].target_date == "2026-06-01"
    assert ny[0].lead_hours == 4
    assert ny[0].init_time_utc == SUMMER_RUN
    lax = sorted((f for f in fcs if f.city == "LAX"), key=lambda f: f.target_date)
    assert lax[0].target_date == "2026-06-01"
    assert lax[0].lead_hours == 7


def test_winter_run_day_of_leads_shift_by_one_hour(tmp_path):
    p = _provider(_Resp(200, _fixture("mex_4city_2026-01-10T00Z.json")), tmp_path)
    fcs = p.fetch_daily_highs(list(STATIONS), "MEX", WINTER_RUN)
    first = {f.city: f for f in sorted(fcs, key=lambda f: f.target_date)[::-1]}
    # rebuild explicitly: earliest target date per city
    first = {}
    for f in sorted(fcs, key=lambda f: (f.city, f.target_date)):
        first.setdefault(f.city, f)
    assert first["NY"].lead_hours == 5
    assert first["CHI"].lead_hours == 6
    assert first["LAX"].lead_hours == 8
    assert all(f.target_date == "2026-01-10" for f in first.values())


def test_spread_is_none_not_zero_when_the_source_publishes_none(tmp_path):
    """Blank means 'not available'. 0.0 would mean 'a perfectly certain
    forecast' and would let a probability engine size as if it were."""
    p = _provider(_Resp(200, _fixture("mex_4city_2026-06-01T00Z.json")), tmp_path)
    fcs = p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)
    assert all(f.spread_f is None for f in fcs)
    assert all(f.as_row()["spread_f"] is None for f in fcs)


def test_row_shape_matches_the_source_agnostic_schema(tmp_path):
    p = _provider(_Resp(200, _fixture("mex_4city_2026-06-01T00Z.json")), tmp_path)
    row = p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)[0].as_row()
    assert tuple(row) == FORECAST_FIELDS


# ---------------------------------------------------------------------------
# Upstream traps
# ---------------------------------------------------------------------------
def test_runtime_is_always_sent(tmp_path):
    """Trap 1: omitting ``runtime`` is an HTTP 200 that returns the LATEST run."""
    p = _provider(_Resp(200, _fixture("mex_4city_2026-06-01T00Z.json")), tmp_path)
    p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)
    _url, params = p.session.calls[0]
    assert ("runtime", SUMMER_RUN) in params
    assert sum(1 for k, _ in params if k == "station") == 4


def test_a_substituted_runtime_is_refused(tmp_path):
    """What trap 1 looks like from the inside: rows carrying a different run."""
    payload = _fixture("mex_4city_2026-06-01T00Z.json")
    for row in payload["data"]:
        row["runtime"] = "2026-07-26 12:00"  # the archive's "latest run" answer
    p = _provider(_Resp(200, payload), tmp_path)
    with pytest.raises(MOSGuidanceError, match="refusing the response"):
        p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)


def test_a_cross_station_substitution_is_refused(tmp_path):
    payload = _fixture("mex_4city_2026-06-01T00Z.json")
    payload["data"][0]["station"] = "KJFK"
    p = _provider(_Resp(200, payload), tmp_path)
    with pytest.raises(MOSGuidanceError, match="cross-station substitution"):
        p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)


def test_a_cross_model_substitution_is_refused(tmp_path):
    payload = _fixture("mex_4city_2026-06-01T00Z.json")
    payload["data"][0]["model"] = "NBS"
    p = _provider(_Resp(200, payload), tmp_path)
    with pytest.raises(MOSGuidanceError, match="cross-model substitution"):
        p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)


def test_404_is_an_archive_gap_not_a_failure(tmp_path):
    p = _provider(_Resp(404, None, text="no results"), tmp_path)
    assert p.fetch_run(["KNYC"], "MEX", "2020-01-01T00:00:00Z") == []
    assert p.stats["runs_missing_404"] == 1
    assert p.stats["runs_failed"] == 0


def test_non_404_http_error_raises(tmp_path):
    p = _provider(_Resp(500, None, text="boom"), tmp_path)
    with pytest.raises(MOSGuidanceError, match="HTTP 500"):
        p.fetch_run(["KNYC"], "MEX", SUMMER_RUN)


def test_model_outside_the_endpoint_whitelist_fails_locally(tmp_path):
    p = _provider(_Resp(200, {"data": []}), tmp_path)
    with pytest.raises(MOSGuidanceError, match="whitelist"):
        p.fetch_run(["KNYC"], "MAV", SUMMER_RUN)
    assert "MAV" not in MODEL_WHITELIST
    assert "MEX" in MODELS_WITH_NX and "NBS" not in MODELS_WITH_NX


def test_comma_station_list_is_rejected_before_the_request(tmp_path):
    p = _provider(_Resp(200, {"data": []}), tmp_path)
    with pytest.raises(MOSGuidanceError, match="comma"):
        p.fetch_run(["KNYC,KMDW"], "MEX", SUMMER_RUN)


def test_nbs_style_null_nx_yields_nothing_and_says_so(tmp_path):
    """NBS/NBE return HTTP 200 with n_x null on every row. That must surface as
    an explicit zero-forecast count, not as a quietly empty calibration."""
    payload = _fixture("mex_4city_2026-06-01T00Z.json")
    for row in payload["data"]:
        row["n_x"] = None
        row["model"] = "NBS"
    p = _provider(_Resp(200, payload), tmp_path)
    fcs = p.fetch_daily_highs(list(STATIONS), "NBS", SUMMER_RUN)
    assert fcs == []
    assert p.stats["rows_nx_null"] == 60
    assert p.stats["forecasts_emitted"] == 0


def test_empty_run_is_not_cached(tmp_path):
    """A transient empty 200 must not freeze that run as a permanent gap."""
    calls = {"n": 0}

    def responder(url, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(200, {"data": []})
        return _Resp(200, _fixture("mex_4city_2026-06-01T00Z.json"))

    p = _provider(responder, tmp_path)
    assert p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN) == []
    assert len(p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)) == 60
    assert calls["n"] == 2


def test_a_populated_run_is_cached_and_replayed(tmp_path):
    calls = {"n": 0}

    def responder(url, params):
        calls["n"] += 1
        return _Resp(200, _fixture("mex_4city_2026-06-01T00Z.json"))

    p = _provider(responder, tmp_path)
    a = p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)
    b = p.fetch_run(list(STATIONS), "MEX", SUMMER_RUN)
    assert calls["n"] == 1
    assert a == b
    assert p.stats["runs_from_cache"] == 1


def test_offline_mode_refuses_the_network_on_a_cache_miss(tmp_path):
    p = MOSGuidanceProvider(cache_dir=str(tmp_path / "c"), offline=True)
    with pytest.raises(MOSGuidanceError, match="offline mode"):
        p.fetch_run(["KNYC"], "MEX", SUMMER_RUN)


# ---------------------------------------------------------------------------
# Mutation guard on the max/min classifier
# ---------------------------------------------------------------------------
def test_utc_hour_classification_would_break_lax(monkeypatch, tmp_path):
    """A UTC hour gate is the bug class this stack was rebuilt to remove.

    Re-pointing the max window at the *UTC* hour of ftime (i.e. treating 00Z as
    'evening') happens to keep the same rows for the eastern cities but assigns
    them to the wrong local date for LAX. This test pins that the module uses
    the local hour by showing the LAX day-of target moves when it does not.
    """
    from src.data import mos_guidance_provider as mod

    p = _provider(_Resp(200, _fixture("mex_4city_2026-06-01T00Z.json")), tmp_path)
    fcs = p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)
    lax = sorted((f for f in fcs if f.city == "LAX"), key=lambda f: f.target_date)
    # ftime 2026-06-02 00:00Z is 2026-06-01 17:00 PDT -> the max for JUNE 1.
    # A naive UTC reading would file it under June 2 and shift every LAX label
    # by a day.
    assert lax[0].target_date == "2026-06-01"
    assert lax[0].provenance.endswith("#ftime=2026-06-02T00:00Z")
    assert mod.MAX_LOCAL_HOUR_RANGE == (14, 24)


def test_unclassifiable_local_hour_is_skipped_not_guessed(tmp_path):
    payload = _fixture("mex_4city_2026-06-01T00Z.json")
    for row in payload["data"]:
        # 18Z -> 14:00 EDT for KNYC (in the max window) but 11:00 PDT for KLAX
        # (in the min window) and 13:00 CDT for KMDW (in NEITHER).
        row["ftime"] = "2026-06-02 18:00"
    p = _provider(_Resp(200, payload), tmp_path)
    p.fetch_daily_highs(list(STATIONS), "MEX", SUMMER_RUN)
    assert p.stats["rows_unclassified_hour"] > 0


def test_guidance_forecast_key_identifies_one_run_one_city_one_day():
    f = GuidanceForecast(
        city="NY",
        station="KNYC",
        target_date="2026-06-01",
        init_time_utc=SUMMER_RUN,
        lead_hours=4,
        source="gfs_mex",
        forecast_high_f=70.0,
        spread_f=None,
        provenance="x",
    )
    assert f.key == ("gfs_mex", "NY", "2026-06-01", SUMMER_RUN)
