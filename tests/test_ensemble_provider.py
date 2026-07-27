"""Tests for :mod:`src.data.ensemble_provider` (PRD FR-2.1, Phase 2 EC-1).

Offline and deterministic. The only real upstream bytes involved are the
recorded fixtures under ``tests/fixtures/ensemble/``: one GEFS ``TMAX`` record
pulled byte-for-byte from NOAA NODD, its ``.idx`` sidecar, and a manifest that
pins the record's SHA-256 alongside the metadata it decodes to.

What the fixture can and cannot prove
-------------------------------------
The expected node temperatures in the manifest were produced by this repo's
decoder, so on their own they would be a self-consistent golden -- the exact
circularity that makes a green test meaningless. Three things break it:

1. The decoded field is checked against invariants the encoder did not choose:
   a physically plausible global range, the 1440 co-located north-pole nodes
   collapsing to one value, and the GRIB2 spec's own requirement that the
   minimum packed difference be exactly 0.
2. ``test_alignment_guard_is_not_vacuous`` mutates the decoder -- it removes
   the octet alignment that WMO templates 7.2/7.3 mandate -- and asserts the
   guard fires. A gate that has never been shown to fail is not a gate.
3. Outside this file, ``scripts/fetch_ensemble.py --validate`` compares the
   decoded members against NCEP's own ``geavg`` product and against CLI
   settlement truth; both comparisons are recorded in
   ``reports/phase2/ec1_ensemble_members.md``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.ensemble_provider import (  # noqa: E402
    CITIES,
    DEFAULT_MEMBERS,
    FIELD_TMAX,
    FIELD_TMP,
    GEFS_GRID,
    LEVEL_2M,
    REASON_CODES,
    REASON_CYCLE_NOT_PUBLISHED,
    REASON_DECODE_FAILED,
    REASON_DEGRADED_UNCALIBRATED,
    REASON_INSUFFICIENT_MEMBERS,
    REASON_LEAD_OUT_OF_RANGE,
    REASON_UNKNOWN_CITY,
    SOURCE_GEFS,
    SOURCE_NWS_DEGRADED,
    EnsembleForecast,
    EnsembleProvider,
    EnsembleUnavailable,
    coverage_of,
    decode_grib2_record,
    fahrenheit_to_kelvin,
    get_city,
    kelvin_to_fahrenheit,
    local_day_bounds_utc,
    parse_idx,
    select_idx_record,
    spec_nearest_node,
    tmax_interval_for,
    tmax_windows,
    verify_station_registry,
)
from src.data import ensemble_provider as ep  # noqa: E402

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "ensemble"
)
#: The GRIB2 record is stored base64-encoded rather than raw. ``.gitattributes``
#: declares ``tests/fixtures/** text eol=lf``, which forces text treatment and
#: would silently normalise the 9 CRLF byte pairs inside this 431 982-byte
#: binary out of existence on check-in. Base64 is CR-free, so the LF policy is a
#: no-op on it and the decoded bytes stay exactly what NOAA served.
RECORD_PATH = os.path.join(FIXTURE_DIR, "gec00.t00z.pgrb2s.0p25.f030.TMAX.grib2.b64")
IDX_PATH = os.path.join(FIXTURE_DIR, "gec00.t00z.pgrb2s.0p25.f030.idx")
MANIFEST_PATH = os.path.join(FIXTURE_DIR, "manifest.json")

INIT = datetime(2026, 7, 25, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def manifest():
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def record_bytes(manifest):
    with open(RECORD_PATH, "r", encoding="ascii") as handle:
        raw = base64.b64decode(handle.read())
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == manifest["sha256"], (
        "the recorded GRIB2 fixture no longer decodes to its manifest digest -- "
        "the fixture was altered or mangled in transit. It is the exact byte "
        "range NOAA served for "
        f"{manifest['s3_key']} ({manifest['range']}); re-download rather than "
        "adjusting the digest."
    )
    assert len(raw) == manifest["bytes"]
    return raw


@pytest.fixture(scope="module")
def idx_text():
    with open(IDX_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def decoded(record_bytes):
    return decode_grib2_record(record_bytes)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
class TestUnitConversion:
    """GRIB TMP/TMAX are Kelvin; Kalshi settles in whole degrees Fahrenheit."""

    @pytest.mark.parametrize(
        "kelvin, fahrenheit",
        [
            (273.15, 32.0),
            (255.372222222, 0.0),
            (310.927777778, 100.0),
            (300.0, 80.33),
            (0.0, -459.67),
        ],
    )
    def test_known_pairs(self, kelvin, fahrenheit):
        assert kelvin_to_fahrenheit(kelvin) == pytest.approx(fahrenheit, abs=1e-6)

    def test_round_trip(self):
        for f in (-20.0, 0.0, 32.0, 75.5, 98.6, 120.0):
            assert fahrenheit_to_kelvin(
                kelvin_to_fahrenheit(fahrenheit_to_kelvin(f))
            ) == (pytest.approx(fahrenheit_to_kelvin(f), abs=1e-9))

    def test_celsius_formula_is_not_used_by_accident(self):
        """A 300 K reading is 80.33 F, not 26.85 (which would be Celsius)."""
        assert kelvin_to_fahrenheit(300.0) == pytest.approx(80.33, abs=1e-6)
        assert kelvin_to_fahrenheit(300.0) != pytest.approx(26.85, abs=0.1)


# ---------------------------------------------------------------------------
# Local-day windowing
# ---------------------------------------------------------------------------
class TestLocalDayWindowing:
    """Settlement is the CLI max over the station's LOCAL calendar day."""

    @pytest.mark.parametrize(
        "city, target, start_utc, end_utc",
        [
            ("NY", date(2026, 7, 26), "2026-07-26T04:00:00Z", "2026-07-27T04:00:00Z"),
            ("CHI", date(2026, 7, 26), "2026-07-26T05:00:00Z", "2026-07-27T05:00:00Z"),
            ("LAX", date(2026, 7, 26), "2026-07-26T07:00:00Z", "2026-07-27T07:00:00Z"),
            ("MIA", date(2026, 7, 26), "2026-07-26T04:00:00Z", "2026-07-27T04:00:00Z"),
            # January: every one of them is an hour further behind UTC.
            ("NY", date(2026, 1, 15), "2026-01-15T05:00:00Z", "2026-01-16T05:00:00Z"),
            ("CHI", date(2026, 1, 15), "2026-01-15T06:00:00Z", "2026-01-16T06:00:00Z"),
            ("LAX", date(2026, 1, 15), "2026-01-15T08:00:00Z", "2026-01-16T08:00:00Z"),
        ],
    )
    def test_bounds(self, city, target, start_utc, end_utc):
        spec = get_city(city)
        start, end = local_day_bounds_utc(target, spec.timezone)
        assert start.strftime("%Y-%m-%dT%H:%M:%SZ") == start_utc
        assert end.strftime("%Y-%m-%dT%H:%M:%SZ") == end_utc

    def test_spring_forward_day_is_23_hours(self):
        """US DST starts 2026-03-08: the local day is 23 h, not 24."""
        start, end = local_day_bounds_utc(date(2026, 3, 8), "America/New_York")
        assert (end - start) == timedelta(hours=23)

    def test_fall_back_day_is_25_hours(self):
        """US DST ends 2026-11-01: the local day is 25 h, not 24."""
        start, end = local_day_bounds_utc(date(2026, 11, 1), "America/New_York")
        assert (end - start) == timedelta(hours=25)

    def test_same_instant_gives_different_leads_per_city(self):
        """LAX and NY do not share a calendar day boundary."""
        leads = {}
        for city in ("NY", "CHI", "LAX", "MIA"):
            spec = get_city(city)
            start, _ = local_day_bounds_utc(date(2026, 7, 26), spec.timezone)
            leads[city] = int((start - INIT).total_seconds() // 3600)
        assert leads == {"NY": 28, "CHI": 29, "LAX": 31, "MIA": 28}

    def test_all_bounds_are_timezone_aware(self):
        start, end = local_day_bounds_utc(date(2026, 7, 26), "America/Chicago")
        assert start.tzinfo is not None and end.tzinfo is not None


class TestTmaxWindowSelection:
    """The covering set must contain the local day and report its own spill."""

    @pytest.mark.parametrize(
        "fhour, interval",
        [
            (3, (0, 3)),
            (6, (0, 6)),
            (9, (6, 9)),
            (12, (6, 12)),
            (21, (18, 21)),
            (24, (18, 24)),
            (27, (24, 27)),
            (30, (24, 30)),
            (120, (114, 120)),
            (123, (120, 123)),
        ],
    )
    def test_interval_golden_table(self, fhour, interval):
        """Golden table read off live .idx sidecars (see the module docstring)."""
        assert tmax_interval_for(fhour) == interval

    def test_rejects_non_published_step(self):
        with pytest.raises(ValueError):
            tmax_interval_for(25)

    @pytest.mark.parametrize(
        "city, expected_fhours",
        [
            ("NY", [30, 36, 42, 48, 54]),
            ("CHI", [30, 36, 42, 48, 54]),
            ("LAX", [36, 42, 48, 54, 57]),
            ("MIA", [30, 36, 42, 48, 54]),
        ],
    )
    def test_day_ahead_windows(self, city, expected_fhours):
        spec = get_city(city)
        start, end = local_day_bounds_utc(date(2026, 7, 26), spec.timezone)
        windows = tmax_windows(
            int((start - INIT).total_seconds() // 3600),
            int((end - INIT).total_seconds() // 3600),
        )
        assert [w.fhour for w in windows] == expected_fhours

    @pytest.mark.parametrize("city", sorted(CITIES))
    def test_windows_cover_the_whole_local_day(self, city):
        spec = get_city(city)
        for offset in range(0, 4):
            target = date(2026, 7, 26) + timedelta(days=offset)
            start, end = local_day_bounds_utc(target, spec.timezone)
            start_lead = int((start - INIT).total_seconds() // 3600)
            end_lead = int((end - INIT).total_seconds() // 3600)
            windows = tmax_windows(start_lead, end_lead)
            covered = coverage_of(windows)
            assert covered[0] <= start_lead
            assert covered[1] >= end_lead
            # Contiguous: no hole between consecutive intervals.
            for previous, current in zip(windows, windows[1:]):
                assert current.interval_start <= previous.interval_end

    @pytest.mark.parametrize("city", sorted(CITIES))
    def test_over_coverage_stays_under_a_bucket(self, city):
        """Spill exists (it is documented) but can never reach a whole 6 h bucket."""
        spec = get_city(city)
        start, end = local_day_bounds_utc(date(2026, 7, 26), spec.timezone)
        start_lead = int((start - INIT).total_seconds() // 3600)
        end_lead = int((end - INIT).total_seconds() // 3600)
        covered = coverage_of(tmax_windows(start_lead, end_lead))
        assert 0 <= start_lead - covered[0] < 6
        assert 0 <= covered[1] - end_lead < 6

    def test_refuses_a_target_before_the_cycle(self):
        with pytest.raises(EnsembleUnavailable) as excinfo:
            tmax_windows(-4, 20)
        assert excinfo.value.reason_code == REASON_LEAD_OUT_OF_RANGE

    def test_refuses_a_target_past_the_model_horizon(self):
        with pytest.raises(EnsembleUnavailable) as excinfo:
            tmax_windows(340, 364)
        assert excinfo.value.reason_code == REASON_LEAD_OUT_OF_RANGE


# ---------------------------------------------------------------------------
# Grid geometry
# ---------------------------------------------------------------------------
class TestGridNodes:
    def test_grid_matches_the_recorded_record(self, decoded):
        assert (decoded.grid.ni, decoded.grid.nj) == (GEFS_GRID.ni, GEFS_GRID.nj)
        assert decoded.grid.dlat == GEFS_GRID.dlat
        assert decoded.grid.lat1 == GEFS_GRID.lat1
        assert decoded.grid.scan_mode == GEFS_GRID.scan_mode

    @pytest.mark.parametrize(
        "city, node, node_lat, node_lon",
        [
            ("NY", (197, 1144), 40.75, -74.0),
            ("CHI", (193, 1089), 41.75, -87.75),
            ("LAX", (224, 966), 34.0, -118.5),
            ("MIA", (257, 1119), 25.75, -80.25),
        ],
    )
    def test_nearest_node_is_stable(self, city, node, node_lat, node_lon):
        spec = get_city(city)
        assert spec_nearest_node(spec) == node
        assert GEFS_GRID.node_lat_lon(*node) == (node_lat, node_lon)

    def test_lax_node_is_the_documented_offshore_one(self):
        """A known, documented defect of nearest-node selection, not a surprise.

        34.00N / 118.50W is in Santa Monica Bay, ~12 km from the LAX
        thermometer. The module documents it as a calibration term; this test
        exists so a future 'fix' that silently re-selects the node is caught.
        """
        spec = get_city("LAX")
        node = spec_nearest_node(spec)
        lat, lon = GEFS_GRID.node_lat_lon(*node)
        assert (lat, lon) == (34.0, -118.5)
        assert ep.haversine_km(
            spec.latitude, spec.longitude, lat, lon
        ) == pytest.approx(12.35, abs=0.1)

    def test_registry_agrees_with_the_settlement_truth_registry(self):
        assert verify_station_registry() == []


# ---------------------------------------------------------------------------
# GRIB2 decoding
# ---------------------------------------------------------------------------
class TestGrib2Decoder:
    def test_metadata_matches_the_manifest(self, decoded, manifest):
        expected = manifest["grib"]
        assert decoded.pdt_number == expected["pdt_number"]
        assert decoded.drs_template == expected["drs_template"]
        assert decoded.parameter_category == expected["parameter_category"]
        assert decoded.parameter_number == expected["parameter_number"]
        assert decoded.interval_start == expected["interval_start"]
        assert decoded.interval_end == expected["interval_end"]
        assert decoded.total_points == expected["total_points"]
        assert decoded.reference_time == datetime(2026, 7, 25, tzinfo=timezone.utc)

    def test_record_really_is_a_maximum_over_the_declared_interval(self, decoded):
        """Statistical process 2 == maximum. A mean would answer another question."""
        assert decoded.statistical_process == 2
        assert (decoded.interval_start, decoded.interval_end) == tmax_interval_for(30)

    def test_field_is_physically_plausible(self, decoded):
        """An invariant the encoder did not pick: 2 m July temperatures in Kelvin."""
        assert 180.0 < float(decoded.values.min()) < 260.0
        assert 300.0 < float(decoded.values.max()) < 340.0

    def test_north_pole_row_collapses_to_one_value(self, decoded):
        """Row 0 is 1440 grid nodes at the same physical point."""
        pole = decoded.values[: decoded.grid.ni]
        assert float(pole.max() - pole.min()) == pytest.approx(0.0, abs=1e-9)

    def test_city_node_values(self, decoded, manifest):
        for city, expected in manifest["city_nodes"].items():
            node = tuple(expected["node"])
            value = decoded.at(GEFS_GRID.node_index(*node))
            assert value == pytest.approx(expected["kelvin"], abs=1e-3)
            assert kelvin_to_fahrenheit(value) == pytest.approx(
                expected["fahrenheit"], abs=1e-3
            )

    def test_truncated_decode_matches_the_full_one(self, record_bytes, decoded):
        partial = decode_grib2_record(record_bytes, max_points=400_000)
        assert partial.decoded_points >= 400_000
        assert partial.decoded_points < decoded.decoded_points
        node = GEFS_GRID.node_index(*spec_nearest_node(get_city("MIA")))
        assert partial.at(node) == pytest.approx(decoded.at(node), abs=1e-9)

    def test_alignment_guard_is_not_vacuous(self, record_bytes, monkeypatch):
        """Mutation test: drop the WMO octet alignment and the decode must fail.

        Reading the group-descriptor lists bit-contiguously (as the widely
        copied ``g2clib`` reference does) is the exact defect that produced
        1e9 K temperatures during development. It is caught by a chain of three
        checks, and which one fires depends on where the phase error lands:
        the group-length sum, the section-7 bounds check, and the
        min-packed-difference invariant. On this fixture the length sum fires
        first; on the record that originally exposed the bug only the
        min-packed invariant did, which is why all three are kept.
        """
        monkeypatch.setattr(ep, "_align_octet", lambda offset: offset)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            decode_grib2_record(record_bytes)
        assert excinfo.value.reason_code == REASON_DECODE_FAILED
        assert any(
            token in str(excinfo.value)
            for token in ("out of phase", "past the end of section 7", "must be 0")
        )

    def test_rejects_non_grib_bytes(self):
        with pytest.raises(EnsembleUnavailable) as excinfo:
            decode_grib2_record(b"NOTGRIB" + b"\x00" * 64)
        assert excinfo.value.reason_code == REASON_DECODE_FAILED

    def test_rejects_a_truncated_message(self, record_bytes):
        with pytest.raises(EnsembleUnavailable) as excinfo:
            decode_grib2_record(record_bytes[: len(record_bytes) // 2])
        assert excinfo.value.reason_code == REASON_DECODE_FAILED


class TestIdxParsing:
    def test_finds_the_tmax_record(self, idx_text, manifest):
        entry = select_idx_record(parse_idx(idx_text), FIELD_TMAX, LEVEL_2M)
        assert entry.number == manifest["idx_record"]["number"]
        assert entry.range_header == manifest["range"]
        assert entry.forecast == manifest["idx_record"]["forecast"]

    def test_tmp_and_tmax_are_different_records(self, idx_text):
        records = parse_idx(idx_text)
        tmp = select_idx_record(records, FIELD_TMP, LEVEL_2M)
        tmax = select_idx_record(records, FIELD_TMAX, LEVEL_2M)
        assert tmp.offset != tmax.offset

    def test_last_record_range_is_open_ended(self, idx_text):
        records = parse_idx(idx_text)
        assert records[-1].end_offset is None
        assert records[-1].range_header.endswith("-")

    def test_missing_field_is_an_error_not_a_guess(self, idx_text):
        with pytest.raises(EnsembleUnavailable):
            select_idx_record(parse_idx(idx_text), "NOSUCHFIELD", LEVEL_2M)


# ---------------------------------------------------------------------------
# HTTP doubles
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, *, content=b"", text="", payload=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class RecordedSession:
    """Serves the recorded ``.idx`` and record; counts every call."""

    def __init__(self, idx_text, record_bytes, *, fail_urls=()):
        self.idx_text = idx_text
        self.record_bytes = record_bytes
        self.fail_urls = tuple(fail_urls)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, (headers or {}).get("Range")))
        if any(token in url for token in self.fail_urls):
            return FakeResponse(404)
        if url.endswith(".idx"):
            return FakeResponse(200, text=self.idx_text)
        return FakeResponse(206, content=self.record_bytes)


def _provider(tmp_path, session, **kwargs):
    kwargs.setdefault("members", ("gec00",))
    kwargs.setdefault("min_members", 1)
    kwargs.setdefault("max_workers", 1)
    return EnsembleProvider(cache_dir=str(tmp_path), session=session, **kwargs)


class TestRecordFetchAndCache:
    def test_end_to_end_single_record(self, tmp_path, idx_text, record_bytes, manifest):
        session = RecordedSession(idx_text, record_bytes)
        provider = _provider(tmp_path, session)
        node = spec_nearest_node(get_city("NY"))
        blob = provider.fetch_record_values(INIT, "gec00", 30, (node,))

        assert blob["from_cache"] is False
        assert blob["_meta"]["s3_key"] == manifest["s3_key"]
        assert blob["_meta"]["range"] == manifest["range"]
        assert blob["_meta"]["statistical_process"] == 2
        assert blob["nodes_k"][f"{node[0]},{node[1]}"] == pytest.approx(
            manifest["city_nodes"]["NY"]["kelvin"], abs=1e-3
        )
        # One .idx read plus exactly one ranged record read.
        assert len(session.calls) == 2
        assert session.calls[1][1] == manifest["range"]

    def test_second_call_is_served_from_cache(self, tmp_path, idx_text, record_bytes):
        session = RecordedSession(idx_text, record_bytes)
        node = spec_nearest_node(get_city("NY"))
        first = _provider(tmp_path, session)
        first.fetch_record_values(INIT, "gec00", 30, (node,))
        calls_after_first = len(session.calls)

        second = _provider(tmp_path, RecordedSession(idx_text, record_bytes))
        blob = second.fetch_record_values(INIT, "gec00", 30, (node,))
        assert blob["from_cache"] is True
        assert second._session.calls == []
        assert calls_after_first == 2

    def test_one_download_serves_every_city(self, tmp_path, idx_text, record_bytes):
        session = RecordedSession(idx_text, record_bytes)
        provider = _provider(tmp_path, session)
        nodes = tuple(spec_nearest_node(spec) for spec in CITIES.values())
        provider.fetch_record_values(INIT, "gec00", 30, nodes)
        downloads = len(session.calls)
        for node in nodes:
            provider.fetch_record_values(INIT, "gec00", 30, (node,))
        assert len(session.calls) == downloads

    def test_cache_miss_when_a_new_node_is_requested(
        self, tmp_path, idx_text, record_bytes
    ):
        session = RecordedSession(idx_text, record_bytes)
        provider = _provider(tmp_path, session)
        ny = spec_nearest_node(get_city("NY"))
        provider.fetch_record_values(INIT, "gec00", 30, (ny,))
        calls = len(session.calls)
        provider.fetch_record_values(
            INIT, "gec00", 30, (spec_nearest_node(get_city("MIA")),)
        )
        assert len(session.calls) > calls

    def test_wrong_interval_is_refused(self, tmp_path, idx_text, record_bytes):
        """The fixture is the 24-30 h record; asking for f036 must not accept it."""
        session = RecordedSession(idx_text, record_bytes)
        provider = _provider(tmp_path, session)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch_record_values(
                INIT, "gec00", 36, (spec_nearest_node(get_city("NY")),)
            )
        assert excinfo.value.reason_code == REASON_DECODE_FAILED
        assert "interval" in str(excinfo.value)

    def test_transient_503_is_retried(self, tmp_path, idx_text, record_bytes):
        """NODD answers a fraction of a 31-member burst with 503; that is not a gap."""

        class FlakySession(RecordedSession):
            def __init__(self, *args, failures_before_success, **kwargs):
                super().__init__(*args, **kwargs)
                self.remaining = failures_before_success

            def get(self, url, headers=None, timeout=None):
                self.calls.append((url, (headers or {}).get("Range")))
                if not url.endswith(".idx") and self.remaining > 0:
                    self.remaining -= 1
                    return FakeResponse(503)
                if url.endswith(".idx"):
                    return FakeResponse(200, text=self.idx_text)
                return FakeResponse(206, content=self.record_bytes)

        session = FlakySession(idx_text, record_bytes, failures_before_success=2)
        provider = _provider(tmp_path, session, max_retries=4, retry_backoff=0.0)
        blob = provider.fetch_record_values(
            INIT, "gec00", 30, (spec_nearest_node(get_city("NY")),)
        )
        assert blob["from_cache"] is False
        assert len([c for c in session.calls if not c[0].endswith(".idx")]) == 3

    def test_persistent_503_gives_up_and_reports_it(self, tmp_path, idx_text):
        class DeadSession(RecordedSession):
            def get(self, url, headers=None, timeout=None):
                self.calls.append((url, None))
                return FakeResponse(503)

        session = DeadSession(idx_text, b"")
        provider = _provider(tmp_path, session, max_retries=2, retry_backoff=0.0)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch_idx(INIT, "gec00", 30)
        assert excinfo.value.reason_code == ep.REASON_S3_UNAVAILABLE
        assert len(session.calls) == 3

    def test_404_is_not_retried(self, tmp_path, idx_text, record_bytes):
        """A 404 is an answer ("not published"), not a transient."""
        session = RecordedSession(idx_text, record_bytes, fail_urls=("f030",))
        provider = _provider(tmp_path, session, max_retries=4, retry_backoff=0.0)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch_idx(INIT, "gec00", 30)
        assert excinfo.value.reason_code == REASON_CYCLE_NOT_PUBLISHED
        assert len(session.calls) == 1

    def test_failure_is_never_cached(self, tmp_path, idx_text, record_bytes):
        session = RecordedSession(idx_text, record_bytes, fail_urls=("f030",))
        provider = _provider(tmp_path, session)
        with pytest.raises(EnsembleUnavailable):
            provider.fetch_record_values(
                INIT, "gec00", 30, (spec_nearest_node(get_city("NY")),)
            )
        cache_root = os.path.join(str(tmp_path), "records")
        written = [
            os.path.join(root, name)
            for root, _dirs, files in os.walk(cache_root)
            for name in files
        ]
        assert written == []


# ---------------------------------------------------------------------------
# fetch(): windowing, member floor, caching
# ---------------------------------------------------------------------------
class StubRecords:
    """Deterministic per-(member, fhour) node temperatures, in Kelvin.

    Lets the daily-max windowing be tested without needing one real GRIB
    fixture per forecast hour. The decoder itself is covered by the golden
    record above.
    """

    def __init__(self, curve, *, missing=(), fail_members=()):
        self.curve = curve
        self.missing = set(missing)
        self.fail_members = set(fail_members)
        self.calls = []

    def __call__(self, init_time, member, fhour, nodes, *, field_name=FIELD_TMAX):
        self.calls.append((member, fhour))
        if member in self.fail_members:
            raise EnsembleUnavailable(
                REASON_CYCLE_NOT_PUBLISHED, f"{member} f{fhour:03d} is not published"
            )
        if fhour in self.missing:
            raise EnsembleUnavailable(
                REASON_CYCLE_NOT_PUBLISHED, f"f{fhour:03d} is not published"
            )
        offset = DEFAULT_MEMBERS.index(member) if member in DEFAULT_MEMBERS else 0
        return {
            "_meta": {
                "s3_key": f"stub/{member}/f{fhour:03d}",
                "range": "bytes=0-1",
                "interval_start": tmax_interval_for(fhour)[0],
                "interval_end": tmax_interval_for(fhour)[1],
            },
            "nodes_k": {
                f"{j},{i}": self.curve[fhour] + 0.1 * offset for (j, i) in nodes
            },
            "from_cache": False,
        }


#: A synthetic diurnal curve over the leads a day-ahead NY/CHI/MIA window uses.
CURVE = {30: 295.0, 36: 297.0, 42: 305.0, 48: 299.0, 54: 294.0, 57: 293.0}


class TestFetch:
    def test_daily_max_is_the_max_over_the_local_day_windows(
        self, tmp_path, monkeypatch
    ):
        stub = StubRecords(CURVE)
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", stub)
        forecast = provider.fetch("NY", date(2026, 7, 26), INIT)

        assert forecast.source == SOURCE_GEFS
        assert forecast.member_count == 1
        assert forecast.members_f[0] == pytest.approx(
            kelvin_to_fahrenheit(max(CURVE[h] for h in (30, 36, 42, 48, 54))), abs=1e-6
        )
        assert sorted({fhour for _m, fhour in stub.calls}) == [30, 36, 42, 48, 54]

    def test_lax_uses_a_different_window_than_ny(self, tmp_path, monkeypatch):
        stub = StubRecords(CURVE)
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", stub)
        provider.fetch("LAX", date(2026, 7, 26), INIT)
        assert sorted({fhour for _m, fhour in stub.calls}) == [36, 42, 48, 54, 57]

    def test_lead_hours_is_init_to_local_day_start(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", StubRecords(CURVE))
        assert provider.fetch("NY", date(2026, 7, 26), INIT).lead_hours == 28
        assert provider.fetch("LAX", date(2026, 7, 26), INIT).lead_hours == 31

    def test_provenance_records_node_and_coverage(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", StubRecords(CURVE))
        provenance = provider.fetch("NY", date(2026, 7, 26), INIT).provenance
        assert provenance["grid_node"]["j"] == 197
        assert provenance["grid_node"]["i"] == 1144
        assert provenance["grid_node"]["selection"].startswith("nearest")
        assert provenance["station"]["id"] == "KNYC"
        assert provenance["coverage"]["requested_lead_hours"] == [28, 52]
        assert provenance["coverage"]["covered_lead_hours"] == [24, 54]
        assert provenance["coverage"]["over_coverage_hours"] == [4, 2]
        assert provenance["field"] == FIELD_TMAX

    def test_member_floor_rejects_a_thin_ensemble(self, tmp_path, monkeypatch):
        failing = DEFAULT_MEMBERS[5:]
        stub = StubRecords(CURVE, fail_members=failing)
        provider = EnsembleProvider(
            cache_dir=str(tmp_path), session=None, min_members=20, max_workers=1
        )
        monkeypatch.setattr(provider, "fetch_record_values", stub)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch("NY", date(2026, 7, 26), INIT)
        assert excinfo.value.reason_code == REASON_INSUFFICIENT_MEMBERS
        assert "5 of 31" in str(excinfo.value)

    def test_member_floor_accepts_exactly_the_floor(self, tmp_path, monkeypatch):
        stub = StubRecords(CURVE, fail_members=DEFAULT_MEMBERS[20:])
        provider = EnsembleProvider(
            cache_dir=str(tmp_path), session=None, min_members=20, max_workers=1
        )
        monkeypatch.setattr(provider, "fetch_record_values", stub)
        forecast = provider.fetch("NY", date(2026, 7, 26), INIT)
        assert forecast.member_count == 20
        assert len(forecast.provenance["members_failed"]) == 11

    def test_a_member_missing_one_hour_is_dropped_not_maxed_over_a_short_day(
        self, tmp_path, monkeypatch
    ):
        """A partial member would silently under-estimate its own daily max."""

        class PartialStub(StubRecords):
            def __call__(
                self, init_time, member, fhour, nodes, *, field_name=FIELD_TMAX
            ):
                if member == "gep01" and fhour == 42:
                    raise EnsembleUnavailable(REASON_CYCLE_NOT_PUBLISHED, "gap")
                return super().__call__(
                    init_time, member, fhour, nodes, field_name=field_name
                )

        provider = EnsembleProvider(
            cache_dir=str(tmp_path),
            session=None,
            members=("gec00", "gep01", "gep02"),
            min_members=2,
            max_workers=1,
        )
        monkeypatch.setattr(provider, "fetch_record_values", PartialStub(CURVE))
        forecast = provider.fetch("NY", date(2026, 7, 26), INIT)
        assert forecast.provenance["members_used"] == ["gec00", "gep02"]
        assert "gep01" in forecast.provenance["members_failed"]

    def test_forecast_cache_round_trip(self, tmp_path, monkeypatch):
        stub = StubRecords(CURVE)
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", stub)
        first = provider.fetch("NY", date(2026, 7, 26), INIT)
        calls = len(stub.calls)

        second = _provider(tmp_path, None, members=("gec00",), min_members=1)
        blocked = StubRecords(CURVE, fail_members=DEFAULT_MEMBERS)
        monkeypatch.setattr(second, "fetch_record_values", blocked)
        cached = second.fetch("NY", date(2026, 7, 26), INIT)

        assert blocked.calls == []
        assert len(stub.calls) == calls
        assert cached.members_f == first.members_f
        assert cached.source == first.source
        assert cached.target_date == first.target_date
        assert cached.init_time == first.init_time

    def test_cached_forecast_below_the_floor_is_not_reused(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", StubRecords(CURVE))
        provider.fetch("NY", date(2026, 7, 26), INIT)

        strict = _provider(tmp_path, None, members=("gec00",), min_members=20)
        monkeypatch.setattr(strict, "fetch_record_values", StubRecords(CURVE))
        with pytest.raises(EnsembleUnavailable) as excinfo:
            strict.fetch("NY", date(2026, 7, 26), INIT)
        assert excinfo.value.reason_code == REASON_INSUFFICIENT_MEMBERS

    def test_unknown_city_is_refused(self, tmp_path):
        provider = _provider(tmp_path, None)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch("DEN", date(2026, 7, 26), INIT)
        assert excinfo.value.reason_code == REASON_UNKNOWN_CITY

    def test_naive_init_time_is_refused(self, tmp_path):
        provider = _provider(tmp_path, None)
        with pytest.raises(ValueError, match="timezone-aware"):
            provider.fetch("NY", date(2026, 7, 26), datetime(2026, 7, 25, 0))


# ---------------------------------------------------------------------------
# EC-1 second half: abort, never default
# ---------------------------------------------------------------------------
class TestAbortNeverDefault:
    def test_induced_fetch_failure_aborts_with_one_info_line(
        self, tmp_path, idx_text, record_bytes, caplog
    ):
        """Induced S3 failure: one INFO line with a reason code, and no value."""
        session = RecordedSession(idx_text, record_bytes, fail_urls=("pgrb2s",))
        provider = _provider(tmp_path, session, members=DEFAULT_MEMBERS, min_members=20)

        returned = "sentinel"
        with caplog.at_level(logging.INFO, logger="src.data.ensemble_provider"):
            with pytest.raises(EnsembleUnavailable) as excinfo:
                returned = provider.get_forecast_or_abort("NY", date(2026, 7, 26), INIT)

        assert returned == "sentinel", "no value may be produced on the abort path"
        assert excinfo.value.reason_code in REASON_CODES
        assert excinfo.value.reason_code == REASON_INSUFFICIENT_MEMBERS

        info_lines = [
            r
            for r in caplog.records
            if r.name == "src.data.ensemble_provider" and r.levelno >= logging.INFO
        ]
        assert len(info_lines) == 1, f"expected exactly one INFO line, got {info_lines}"
        message = info_lines[0].getMessage()
        assert message.startswith("ENSEMBLE_ABORT ")
        assert f"reason={excinfo.value.reason_code}" in message
        assert "city=NY" in message

    def test_abort_does_not_fall_back_to_a_default_temperature(
        self, tmp_path, idx_text, record_bytes
    ):
        session = RecordedSession(idx_text, record_bytes, fail_urls=("pgrb2s",))
        provider = _provider(tmp_path, session, members=DEFAULT_MEMBERS, min_members=20)
        with pytest.raises(EnsembleUnavailable):
            provider.get_forecast_or_abort("NY", date(2026, 7, 26), INIT)
        cached = os.path.join(str(tmp_path), "forecasts")
        assert not os.path.isdir(cached) or os.listdir(cached) == []

    def test_success_path_logs_nothing_at_info(self, tmp_path, monkeypatch, caplog):
        provider = _provider(tmp_path, None, members=("gec00",), min_members=1)
        monkeypatch.setattr(provider, "fetch_record_values", StubRecords(CURVE))
        with caplog.at_level(logging.INFO, logger="src.data.ensemble_provider"):
            forecast = provider.get_forecast_or_abort("NY", date(2026, 7, 26), INIT)
        assert forecast.source == SOURCE_GEFS
        assert [
            r for r in caplog.records if r.name == "src.data.ensemble_provider"
        ] == []

    def test_every_reason_code_is_registered(self):
        for name, value in vars(ep).items():
            if name.startswith("REASON_") and isinstance(value, str):
                assert value in REASON_CODES


class TestDegradation:
    """FR-2.1's fallback must be explicit, calibrated, and stamped."""

    def test_degradation_is_off_by_default(
        self, tmp_path, idx_text, record_bytes, caplog
    ):
        session = RecordedSession(idx_text, record_bytes, fail_urls=("pgrb2s",))
        provider = _provider(tmp_path, session, members=DEFAULT_MEMBERS, min_members=20)
        assert provider.allow_degraded is False
        with caplog.at_level(logging.INFO, logger="src.data.ensemble_provider"):
            with pytest.raises(EnsembleUnavailable):
                provider.get_forecast_or_abort("NY", date(2026, 7, 26), INIT)
        assert all("ENSEMBLE_DEGRADED" not in r.getMessage() for r in caplog.records)

    def test_degradation_without_a_sigma_source_is_refused(
        self, tmp_path, idx_text, record_bytes, caplog
    ):
        session = RecordedSession(idx_text, record_bytes, fail_urls=("pgrb2s",))
        provider = _provider(
            tmp_path,
            session,
            members=DEFAULT_MEMBERS,
            min_members=20,
            allow_degraded=True,
        )
        with caplog.at_level(logging.INFO, logger="src.data.ensemble_provider"):
            with pytest.raises(EnsembleUnavailable) as excinfo:
                provider.get_forecast_or_abort("NY", date(2026, 7, 26), INIT)
        assert excinfo.value.reason_code == REASON_DEGRADED_UNCALIBRATED
        info_lines = [
            r
            for r in caplog.records
            if r.name == "src.data.ensemble_provider" and r.levelno >= logging.INFO
        ]
        assert len(info_lines) == 1
        assert "ENSEMBLE_ABORT" in info_lines[0].getMessage()

    def test_degraded_forecast_is_stamped_and_announced(self, tmp_path, caplog):
        class NwsSession(RecordedSession):
            def get(self, url, headers=None, timeout=None):
                self.calls.append((url, None))
                if "/points/" in url:
                    return FakeResponse(
                        200,
                        payload={
                            "properties": {
                                "forecast": "https://api.weather.gov/gridpoints/OKX/1,1/forecast"
                            }
                        },
                    )
                if url.endswith("/forecast"):
                    return FakeResponse(
                        200,
                        payload={
                            "properties": {
                                "periods": [
                                    {
                                        "name": "Tonight",
                                        "isDaytime": False,
                                        "startTime": "2026-07-25T20:00:00-04:00",
                                        "temperature": 70,
                                        "temperatureUnit": "F",
                                    },
                                    {
                                        "name": "Sunday",
                                        "isDaytime": True,
                                        "startTime": "2026-07-26T06:00:00-04:00",
                                        "temperature": 88,
                                        "temperatureUnit": "F",
                                    },
                                ]
                            }
                        },
                    )
                return FakeResponse(404)

        session = NwsSession("", b"", fail_urls=("pgrb2s",))
        provider = _provider(
            tmp_path,
            session,
            members=DEFAULT_MEMBERS,
            min_members=20,
            allow_degraded=True,
            sigma_provider=lambda city, lead: 3.0,
        )
        with caplog.at_level(logging.INFO, logger="src.data.ensemble_provider"):
            forecast = provider.get_forecast_or_abort("NY", date(2026, 7, 26), INIT)

        assert forecast.source == SOURCE_NWS_DEGRADED
        assert forecast.source != SOURCE_GEFS
        assert forecast.provenance["degraded"] is True
        assert forecast.provenance["point_forecast_f"] == 88.0
        assert forecast.provenance["sigma_f"] == 3.0
        assert forecast.mean_f == pytest.approx(88.0, abs=1e-6)
        assert forecast.sigma_f == pytest.approx(3.0, abs=0.15)

        announcements = [
            r.getMessage()
            for r in caplog.records
            if "ENSEMBLE_DEGRADED" in r.getMessage()
        ]
        assert len(announcements) == 1
        assert f"source={SOURCE_NWS_DEGRADED}" in announcements[0]
        assert "primary_reason=" in announcements[0]

    def test_degraded_members_are_deterministic(self, tmp_path):
        provider = _provider(
            tmp_path, None, sigma_provider=lambda city, lead: 2.5, allow_degraded=True
        )
        provider._nws_point_high = lambda spec, target: (85.0, {"note": "stub"})
        a = provider.fetch_degraded("NY", date(2026, 7, 26), INIT)
        b = provider.fetch_degraded("NY", date(2026, 7, 26), INIT)
        assert a.members_f == b.members_f
        assert len(set(a.members_f)) == len(a.members_f)

    @pytest.mark.parametrize("sigma", [0.0, -1.0, float("nan")])
    def test_a_nonsense_sigma_is_refused(self, tmp_path, sigma):
        provider = _provider(
            tmp_path, None, sigma_provider=lambda city, lead: sigma, allow_degraded=True
        )
        provider._nws_point_high = lambda spec, target: (85.0, {"note": "stub"})
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch_degraded("NY", date(2026, 7, 26), INIT)
        assert excinfo.value.reason_code == REASON_DEGRADED_UNCALIBRATED


class TestForecastObject:
    def _forecast(self, members):
        return EnsembleForecast(
            city="NY",
            station="KNYC",
            target_date=date(2026, 7, 26),
            init_time=INIT,
            lead_hours=28,
            members_f=tuple(members),
            source=SOURCE_GEFS,
            provenance={},
        )

    def test_statistics(self):
        forecast = self._forecast([80.0, 82.0, 84.0, 86.0, 88.0])
        assert forecast.mean_f == pytest.approx(84.0)
        assert forecast.sigma_f == pytest.approx(3.1622776601683795)
        assert forecast.quantile_f(0.0) == 80.0
        assert forecast.quantile_f(0.5) == 84.0
        assert forecast.quantile_f(1.0) == 88.0

    def test_json_round_trip(self):
        forecast = self._forecast([80.0, 84.0, 88.0])
        restored = EnsembleForecast.from_dict(
            json.loads(json.dumps(forecast.to_dict()))
        )
        assert restored == forecast
        assert restored.init_time.tzinfo is not None

    def test_offline_provider_refuses_the_network(self, tmp_path):
        provider = EnsembleProvider(cache_dir=str(tmp_path), offline=True)
        with pytest.raises(EnsembleUnavailable) as excinfo:
            provider.fetch_idx(INIT, "gec00", 30)
        assert excinfo.value.reason_code == ep.REASON_OFFLINE

    def test_default_init_time_respects_the_publish_delay(self, tmp_path):
        provider = EnsembleProvider(cache_dir=str(tmp_path), publish_delay_hours=6)
        just_after_midnight = datetime(2026, 7, 26, 2, tzinfo=timezone.utc)
        assert provider.default_init_time(just_after_midnight) == datetime(
            2026, 7, 25, 0, tzinfo=timezone.utc
        )
        later = datetime(2026, 7, 26, 9, tzinfo=timezone.utc)
        assert provider.default_init_time(later) == datetime(
            2026, 7, 26, 0, tzinfo=timezone.utc
        )
