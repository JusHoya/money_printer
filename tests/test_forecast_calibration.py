"""Tests for the FR-2.2 forecast-calibration pipeline.

Covers the two things that can silently invalidate a calibration -- the error
**sign** and the **determinism** of the emitted artifact -- plus the pairing and
minimum-sample rules.

The determinism test does what Phase 2 exit criterion 2 actually asks for: it
re-runs the build **from disk** and byte-compares the written files. Re-
serializing an in-memory object would prove nothing, because the object is the
same object.

Run with::

    python -m pytest tests/test_forecast_calibration.py -v
"""

from __future__ import annotations

import csv
import json
import os
import re
from typing import List

import pytest

from src.calibration.forecast_calibration import (
    CALIBRATION_DIR,
    DAY_OF_BUCKET,
    ERROR_CONVENTION,
    LEAD_BUCKETS,
    MIN_BUCKET_N,
    SCHEMA_VERSION,
    SIGMA_SANITY_BOUND_F,
    CalibrationError,
    PairedDay,
    bucket_for_lead,
    build_all,
    build_city_calibration,
    canonical_bytes,
    content_fingerprint,
    day_of_sigma_table,
    finalize,
    load_calibration,
    load_forecast_series,
    load_truth,
    mean,
    pair_city,
    percentile,
    stdev_sample,
    summarize,
    verify_content_hash,
    write_calibration,
)
from src.data.iem_cli_provider import STATIONS
from src.data.mos_guidance_provider import FORECAST_FIELDS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_FORECAST_CSV = os.path.join(
    REPO_ROOT, "data", "forecast_archive", "forecast_series_gfs_mex.csv"
)


# ---------------------------------------------------------------------------
# Synthetic fixtures (no network, no dependence on the live backfill)
# ---------------------------------------------------------------------------
def _write_truth(tmp_path, station, rows):
    d = tmp_path / "truth"
    d.mkdir(exist_ok=True)
    p = d / f"cli_daily_high_{station}.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(
            [
                "station",
                "date",
                "high",
                "low",
                "high_time",
                "source",
                "source_url",
                "product",
                "fetched_at",
            ]
        )
        for date, high in rows:
            w.writerow(
                [
                    station,
                    date,
                    "" if high is None else high,
                    "",
                    "",
                    "iem_cli",
                    "",
                    "",
                    "",
                ]
            )
    return str(d)


def _write_forecasts(tmp_path, rows, name="forecast_series_test.csv"):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(FORECAST_FIELDS), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(p)


def _fc(city, station, date, init, lead, high, spread=None, source="test_src"):
    return {
        "city": city,
        "station": station,
        "target_date": date,
        "init_time_utc": init,
        "lead_hours": lead,
        "source": source,
        "forecast_high_f": high,
        "spread_f": "" if spread is None else spread,
        "provenance": "unit-test",
    }


def _days(n, *, forecast, truth, lead=5, city="NY", station="KNYC"):
    out: List[PairedDay] = []
    for i in range(n):
        out.append(
            PairedDay(
                city=city,
                station=station,
                target_date=f"2026-01-{i + 1:02d}",
                init_time_utc=f"2026-01-{i + 1:02d}T00:00:00Z",
                lead_hours=lead,
                source="test_src",
                forecast_high_f=float(
                    forecast[i] if isinstance(forecast, list) else forecast
                ),
                truth_high_f=float(truth[i] if isinstance(truth, list) else truth),
                spread_f=None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# The error convention -- the sign that inverts the whole edge if wrong
# ---------------------------------------------------------------------------
def test_error_is_forecast_minus_truth_positive_means_too_warm():
    d = PairedDay(
        "NY",
        "KNYC",
        "2026-06-01",
        "2026-06-01T00:00:00Z",
        4,
        "test_src",
        90.0,
        85.0,
        None,
    )
    assert d.error_f == pytest.approx(5.0)
    cold = PairedDay(
        "NY",
        "KNYC",
        "2026-06-01",
        "2026-06-01T00:00:00Z",
        4,
        "test_src",
        80.0,
        85.0,
        None,
    )
    assert cold.error_f == pytest.approx(-5.0)


def test_bias_sign_propagates_into_the_summary_and_the_published_string():
    warm = summarize(_days(25, forecast=93.0, truth=90.0), min_n=5)
    assert warm["bias_f"] == pytest.approx(3.0)
    cold = summarize(_days(25, forecast=87.0, truth=90.0), min_n=5)
    assert cold["bias_f"] == pytest.approx(-3.0)
    assert "forecast_high_f - truth_high_f" in ERROR_CONVENTION
    assert "positive = forecast too warm" in ERROR_CONVENTION


def test_a_swapped_error_convention_changes_the_published_bias_sign():
    """Mutation guard: if someone 'simplifies' the convention to
    truth - forecast, this is the test that goes red."""
    days = _days(25, forecast=93.0, truth=90.0)
    published = summarize(days, min_n=5)["bias_f"]
    swapped = mean([d.truth_high_f - d.forecast_high_f for d in days])
    assert published == pytest.approx(-swapped)
    assert published > 0


# ---------------------------------------------------------------------------
# Numerics
# ---------------------------------------------------------------------------
def test_percentile_matches_linear_interpolation_by_hand():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert percentile(xs, 0) == 1.0
    assert percentile(xs, 100) == 4.0
    assert percentile(xs, 50) == pytest.approx(2.5)
    assert percentile(xs, 25) == pytest.approx(1.75)
    assert percentile([7.0], 42) == 7.0
    assert percentile([], 50) is None


def test_sigma_is_the_sample_standard_deviation_ddof_1():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert stdev_sample(vals) == pytest.approx(1.5811388300841898)
    assert stdev_sample([3.0]) is None


def test_sigma_of_a_known_error_sequence():
    errs = [-2, -1, 0, 1, 2] * 5  # n=25, mean 0
    days = _days(25, forecast=[90 + e for e in errs], truth=90)
    s = summarize(days, min_n=5)
    assert s["bias_f"] == pytest.approx(0.0)
    # published floats are rounded once, at payload-build time, to 4 dp
    assert s["sigma_f"] == round(1.4433756729740643, 4)
    assert s["mae_f"] == pytest.approx(1.2)
    assert s["quantiles_f"]["p50"] == 0.0


# ---------------------------------------------------------------------------
# Pairing: inner join, drops counted, never imputed
# ---------------------------------------------------------------------------
def test_pairing_is_an_inner_join_and_drops_are_counted(tmp_path):
    truth_dir = _write_truth(
        tmp_path,
        "KNYC",
        [
            ("2026-01-01", 40),
            ("2026-01-02", 41),
            ("2026-01-03", None),
        ],
    )
    csv_path = _write_forecasts(
        tmp_path,
        [
            _fc("NY", "KNYC", "2026-01-01", "2026-01-01T00:00:00Z", 5, 42),
            _fc("NY", "KNYC", "2026-01-02", "2026-01-02T00:00:00Z", 5, 39),
            _fc(
                "NY", "KNYC", "2026-01-03", "2026-01-03T00:00:00Z", 5, 50
            ),  # null truth
            _fc(
                "NY", "KNYC", "2026-01-09", "2026-01-09T00:00:00Z", 5, 55
            ),  # no truth row
        ],
    )
    rows = load_forecast_series(csv_path)
    truth = load_truth("KNYC", truth_dir)
    paired, drops = pair_city(rows, "KNYC", truth)
    assert [p.target_date for p in paired] == ["2026-01-01", "2026-01-02"]
    assert drops == {"dropped_no_truth": 1, "dropped_null_truth": 1}
    # nothing was invented to fill the holes
    assert len(paired) == 2


def test_blank_spread_loads_as_none_never_zero(tmp_path):
    csv_path = _write_forecasts(
        tmp_path,
        [
            _fc("NY", "KNYC", "2026-01-01", "2026-01-01T00:00:00Z", 5, 42),
            _fc("NY", "KNYC", "2026-01-02", "2026-01-02T00:00:00Z", 5, 42, spread=0.0),
        ],
    )
    rows = load_forecast_series(csv_path)
    assert rows[0]["spread_f"] is None
    assert rows[1]["spread_f"] == 0.0  # an explicit zero stays an explicit zero


def test_spread_coverage_is_reported_not_defaulted():
    days = _days(25, forecast=90.0, truth=90.0)
    s = summarize(days, min_n=5)
    assert s["spread_f_coverage"]["rows_with_spread"] == 0
    assert s["spread_f_coverage"]["rows_without_spread"] == 25
    assert s["spread_f_coverage"]["mean_spread_f"] is None


def test_malformed_forecast_row_raises_instead_of_shrinking_the_sample(tmp_path):
    p = tmp_path / "bad.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(FORECAST_FIELDS) + "\n")
        fh.write("NY,KNYC,2026-01-01,2026-01-01T00:00:00Z,NOT_AN_INT,s,42,,prov\n")
    with pytest.raises(CalibrationError, match="malformed"):
        load_forecast_series(str(p))


def test_missing_column_is_rejected(tmp_path):
    p = tmp_path / "short.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write("city,station,target_date\nNY,KNYC,2026-01-01\n")
    with pytest.raises(CalibrationError, match="missing forecast-series columns"):
        load_forecast_series(str(p))


# ---------------------------------------------------------------------------
# Bucketing and the minimum-n rule
# ---------------------------------------------------------------------------
def test_lead_buckets_are_contiguous_and_day_of_is_first():
    assert LEAD_BUCKETS[0][0] == DAY_OF_BUCKET
    for (_n1, _lo1, hi1), (_n2, lo2, _hi2) in zip(LEAD_BUCKETS, LEAD_BUCKETS[1:]):
        assert hi1 == lo2
    assert bucket_for_lead(4) == DAY_OF_BUCKET
    assert bucket_for_lead(8) == DAY_OF_BUCKET
    assert bucket_for_lead(-14) == DAY_OF_BUCKET
    assert bucket_for_lead(16) == "lead_12_36"
    assert bucket_for_lead(172) == "lead_156_180"
    assert bucket_for_lead(-999) is None


def test_an_undersized_bucket_is_marked_insufficient_and_carries_no_stats():
    s = summarize(_days(MIN_BUCKET_N - 1, forecast=90.0, truth=88.0))
    assert s == {"n": MIN_BUCKET_N - 1, "sufficient": False}
    assert "bias_f" not in s and "sigma_f" not in s


def test_an_insufficient_bucket_is_never_merged_into_a_neighbour(tmp_path):
    """Two thin buckets must stay two thin buckets, not one adequate one."""
    days = _days(10, forecast=90.0, truth=88.0, lead=5)
    days += _days(10, forecast=90.0, truth=88.0, lead=20)
    payload = build_city_calibration(
        city="NY",
        station="KNYC",
        timezone_name="America/New_York",
        source="test_src",
        version=1,
        paired=days,
        drops={},
        forecast_rows_for_city=20,
        forecast_fingerprint="x",
        truth_fingerprint="y",
    )
    assert payload["by_lead"][DAY_OF_BUCKET]["sufficient"] is False
    assert payload["by_lead"]["lead_12_36"]["sufficient"] is False
    assert payload["day_of"]["n"] == 10


def test_lead_hours_observed_is_published_so_the_bucket_width_is_never_assumed():
    days = _days(25, forecast=90.0, truth=88.0, lead=5)
    s = summarize(days, min_n=5)
    assert s["lead_hours_observed"] == {"min": 5, "median": 5.0, "max": 5}


# ---------------------------------------------------------------------------
# Determinism (Phase 2 exit criterion 2)
# ---------------------------------------------------------------------------
_TIMESTAMPY = re.compile(
    r"(generated_at|fetched_at|created_at|timestamp|hostname|host|run_id|runid|"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2})",
    re.IGNORECASE,
)


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def test_artifact_contains_no_wallclock_or_host_metadata(tmp_path):
    days = _days(30, forecast=90.0, truth=88.0)
    payload = finalize(
        build_city_calibration(
            city="NY",
            station="KNYC",
            timezone_name="America/New_York",
            source="test_src",
            version=1,
            paired=days,
            drops={},
            forecast_rows_for_city=30,
            forecast_fingerprint="a",
            truth_fingerprint="b",
        )
    )
    offenders = []
    for path, value in _walk(payload):
        text = str(value)
        # target dates are YYYY-MM-DD, which is fine; a full timestamp is not.
        if _TIMESTAMPY.search(path) or _TIMESTAMPY.search(text):
            offenders.append((path, value))
    assert offenders == [], f"non-deterministic metadata in the artifact: {offenders}"


def test_content_hash_excludes_itself_and_verifies():
    days = _days(30, forecast=90.0, truth=88.0)
    payload = finalize(
        build_city_calibration(
            city="NY",
            station="KNYC",
            timezone_name="America/New_York",
            source="test_src",
            version=1,
            paired=days,
            drops={},
            forecast_rows_for_city=30,
            forecast_fingerprint="a",
            truth_fingerprint="b",
        )
    )
    assert payload["content_hash"].startswith("sha256:")
    assert verify_content_hash(payload)
    # finalize is idempotent
    assert finalize(dict(payload))["content_hash"] == payload["content_hash"]
    # tampering with a number invalidates the hash
    tampered = json.loads(json.dumps(payload))
    assert tampered["day_of"]["bias_f"] == 2.0
    tampered["day_of"]["bias_f"] = 0.0
    assert not verify_content_hash(tampered)


def test_written_file_is_lf_and_matches_the_canonical_bytes(tmp_path):
    days = _days(30, forecast=90.0, truth=88.0)
    payload = finalize(
        build_city_calibration(
            city="NY",
            station="KNYC",
            timezone_name="America/New_York",
            source="test_src",
            version=1,
            paired=days,
            drops={},
            forecast_rows_for_city=30,
            forecast_fingerprint="a",
            truth_fingerprint="b",
        )
    )
    path = write_calibration(str(tmp_path), payload)
    raw = open(path, "rb").read()
    assert b"\r\n" not in raw, "CRLF in the artifact would break any hash gate"
    assert raw == canonical_bytes(payload)
    assert raw.endswith(b"\n")
    assert os.path.basename(path) == "NY_test_src_v1.json"
    assert load_calibration(path)["content_hash"] == payload["content_hash"]


def test_content_fingerprint_is_immune_to_line_endings(tmp_path):
    """Provenance hashes the normalized content, not the file bytes, so a CRLF
    checkout of an input cannot change the artifact."""
    rows = [_fc("NY", "KNYC", "2026-01-01", "2026-01-01T00:00:00Z", 5, 42)]
    lf = tmp_path / "lf.csv"
    crlf = tmp_path / "crlf.csv"
    header = ",".join(FORECAST_FIELDS)
    body = ",".join(str(rows[0][k]) for k in FORECAST_FIELDS)
    open(lf, "wb").write((header + "\n" + body + "\n").encode())
    open(crlf, "wb").write((header + "\r\n" + body + "\r\n").encode())
    fields = (
        "city",
        "station",
        "target_date",
        "init_time_utc",
        "lead_hours",
        "source",
        "forecast_high_f",
        "spread_f",
    )
    a = content_fingerprint(load_forecast_series(str(lf)), fields)
    b = content_fingerprint(load_forecast_series(str(crlf)), fields)
    assert a == b


@pytest.mark.skipif(
    not os.path.exists(REAL_FORECAST_CSV),
    reason="run scripts/backfill_forecasts.py first",
)
def test_rebuild_from_disk_is_byte_identical(tmp_path):
    """Phase 2 EC-2's actual requirement.

    Builds twice **from the files on disk**, writes both to separate
    directories, and byte-compares. Re-serializing one in-memory payload would
    not test anything.
    """
    kwargs = dict(
        forecast_csv=REAL_FORECAST_CSV,
        stations=STATIONS,
        source="gfs_mex",
        version=1,
    )
    first = build_all(**kwargs)
    second = build_all(**kwargs)
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    assert len(first) == 4
    for a, b in zip(first, second):
        pa = write_calibration(str(d1), a.payload)
        pb = write_calibration(str(d2), b.payload)
        assert open(pa, "rb").read() == open(pb, "rb").read(), a.city


@pytest.mark.skipif(
    not os.path.exists(REAL_FORECAST_CSV),
    reason="run scripts/backfill_forecasts.py first",
)
def test_committed_artifacts_match_a_fresh_rebuild(tmp_path):
    """The committed calibration files must be reproducible from the committed
    inputs -- otherwise they are just numbers someone typed."""
    rebuilt = build_all(
        forecast_csv=REAL_FORECAST_CSV,
        stations=STATIONS,
        source="gfs_mex",
        version=1,
    )
    for r in rebuilt:
        committed = os.path.join(CALIBRATION_DIR, f"{r.city}_gfs_mex_v1.json")
        if not os.path.exists(committed):
            pytest.skip(f"{committed} not built yet")
        assert open(committed, "rb").read() == canonical_bytes(r.payload), r.city


# ---------------------------------------------------------------------------
# Sigma table (Phase 2 exit criterion 3)
# ---------------------------------------------------------------------------
def test_sigma_table_verdicts_are_pass_fail_or_no_data():
    def _res(city, days):
        payload = finalize(
            build_city_calibration(
                city=city,
                station="K" + city,
                timezone_name="UTC",
                source="s",
                version=1,
                paired=days,
                drops={},
                forecast_rows_for_city=len(days),
                forecast_fingerprint="a",
                truth_fingerprint="b",
                min_bucket_n=5,
            )
        )
        from src.calibration.forecast_calibration import CityResult

        return CityResult(
            city=city, station="K" + city, payload=payload, paired=list(days)
        )

    tight = _days(25, forecast=[90 + (i % 3) - 1 for i in range(25)], truth=90)
    wide = _days(
        25, forecast=[90 + 10 * ((i % 2) * 2 - 1) for i in range(25)], truth=90
    )
    thin = _days(3, forecast=90.0, truth=90.0)
    table = day_of_sigma_table([_res("AA", tight), _res("BB", wide), _res("CC", thin)])
    verdicts = {t["city"]: t["verdict"] for t in table}
    assert verdicts["AA"] == "PASS"
    assert verdicts["BB"] == "FAIL"
    assert verdicts["CC"] == "NO DATA"
    assert all(t["sigma_f"] is None or isinstance(t["sigma_f"], float) for t in table)


def test_sanity_bound_is_the_prd_number():
    assert SIGMA_SANITY_BOUND_F == 4.0


def test_schema_version_is_pinned_and_load_rejects_a_stranger(tmp_path):
    days = _days(30, forecast=90.0, truth=88.0)
    payload = finalize(
        build_city_calibration(
            city="NY",
            station="KNYC",
            timezone_name="America/New_York",
            source="test_src",
            version=1,
            paired=days,
            drops={},
            forecast_rows_for_city=30,
            forecast_fingerprint="a",
            truth_fingerprint="b",
        )
    )
    assert payload["schema_version"] == SCHEMA_VERSION
    path = write_calibration(str(tmp_path), payload)
    blob = json.loads(open(path, "rb").read().decode())
    blob["schema_version"] = SCHEMA_VERSION + 99
    open(path, "wb").write(
        (
            json.dumps(blob, sort_keys=True, separators=(",", ":"), indent=1) + "\n"
        ).encode()
    )
    with pytest.raises(CalibrationError, match="schema_version"):
        load_calibration(path)


def test_truth_loader_refuses_a_cross_station_file(tmp_path):
    d = tmp_path / "truth"
    d.mkdir()
    p = d / "cli_daily_high_KNYC.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["station", "date", "high"])
        w.writerow(["KMDW", "2026-01-01", 40])
    with pytest.raises(CalibrationError, match="cross-station"):
        load_truth("KNYC", str(d))


# ---------------------------------------------------------------------------
# Source annotations -- the E1 defect: two producers, two different artifacts
# ---------------------------------------------------------------------------
# `data/calibration/<CITY>_gefs_v1.json` carries a top-level `statistic` block
# whose warning is the only thing stopping a reader confusing the backfill
# statistic `max_t(geavg)` with the live-path `mean_m(max_t member)`. It used to
# be stamped by `scripts/backfill_ensemble_history.py` *after* `build_all()`, so
# the documented generic rebuild (`scripts/build_calibration.py --source gefs`)
# emitted the same numbers with the block stripped: a 10 402-byte file where the
# committed one is 11 932, a different `content_hash`, and no warning at all.
# These tests pin the fix -- the annotation is applied inside `build_all()`, so
# every producer of the source gets it.
class TestSourceAnnotations:
    @staticmethod
    def _series(tmp_path, source, city="NY", station="KNYC", n=25):
        rows, truth = [], []
        for i in range(n):
            date = f"2026-01-{i + 1:02d}"
            rows.append(
                _fc(
                    city,
                    station,
                    date,
                    f"{date}T00:00:00Z",
                    5,
                    f"{80 + (i % 5)}",
                    source=source,
                )
            )
            truth.append((date, 80))
        return (
            _write_forecasts(tmp_path, rows, name=f"series_{source}.csv"),
            _write_truth(tmp_path, station, truth),
        )

    @staticmethod
    def _stations():
        class _Spec:
            timezone = "America/New_York"
            series_ticker = "KXHIGHNY"

        return {"KNYC": _Spec()}

    def test_gefs_build_carries_the_statistic_block(self, tmp_path):
        csv_path, truth_dir = self._series(tmp_path, "gefs")
        payload = build_all(
            forecast_csv=csv_path,
            stations=self._stations(),
            source="gefs",
            version=1,
            truth_dir=truth_dir,
            min_bucket_n=5,
        )[0].payload
        block = payload["statistic"]
        assert block["are_they_the_same"] is False
        assert "geavg" in block["built_on"]
        assert "member" in block["live_provider_statistic"]
        assert verify_content_hash(
            payload
        ), "the annotation must be applied before the hash is sealed"

    def test_an_unannotated_source_gains_nothing(self, tmp_path):
        csv_path, truth_dir = self._series(tmp_path, "gfs_mex")
        payload = build_all(
            forecast_csv=csv_path,
            stations=self._stations(),
            source="gfs_mex",
            version=1,
            truth_dir=truth_dir,
            min_bucket_n=5,
        )[0].payload
        assert "statistic" not in payload, (
            "gfs_mex must be untouched by the annotator mechanism; its committed "
            "artifacts are byte-gated"
        )

    def test_both_producers_emit_the_same_bytes(self, tmp_path):
        """The E1 regression itself: generic builder and backfill must agree."""
        import scripts.backfill_ensemble_history as bh

        csv_path, truth_dir = self._series(tmp_path, "gefs")
        payload = build_all(
            forecast_csv=csv_path,
            stations=self._stations(),
            source="gefs",
            version=1,
            truth_dir=truth_dir,
            min_bucket_n=5,
        )[0].payload
        assert canonical_bytes(payload) == canonical_bytes(
            bh.stamp_statistic(payload)
        ), (
            "stamp_statistic() must be a no-op on a payload build_all() already "
            "annotated -- otherwise the two producers write different files again"
        )

    def test_the_annotation_carries_no_wallclock_or_path(self, tmp_path):
        csv_path, truth_dir = self._series(tmp_path, "gefs")
        kwargs = dict(
            forecast_csv=csv_path,
            stations=self._stations(),
            source="gefs",
            version=1,
            truth_dir=truth_dir,
            min_bucket_n=5,
        )
        first = build_all(**kwargs)[0].payload
        second = build_all(**kwargs)[0].payload
        assert canonical_bytes(first) == canonical_bytes(second)
        text = json.dumps(first["statistic"])
        for banned in ("generated_at", "hostname", "run_id", "fetched_at", "Users"):
            assert banned not in text, banned

    def test_annotations_are_additive_only(self):
        from src.calibration.forecast_calibration import annotate

        payload = {"city": "NY", "source": "gefs", "statistic": {"mine": True}}
        with pytest.raises(CalibrationError, match="additive only"):
            annotate(payload, "gefs")

    def test_a_broken_annotator_fails_loudly(self, monkeypatch):
        """An unloadable annotator raises; it never quietly emits a stripped file.

        Degrading silently is exactly the defect -- the artifact still looks
        authoritative while its guard is gone.
        """
        from src.calibration import forecast_calibration as fc

        monkeypatch.setitem(fc.SOURCE_ANNOTATORS, "gefs", "no.such.module:nope")
        with pytest.raises(CalibrationError, match="could not be loaded"):
            fc.source_annotation("gefs", {"city": "NY"})

    def test_the_registered_source_label_matches_the_real_constant(self):
        from src.calibration.forecast_calibration import SOURCE_ANNOTATORS
        from src.calibration.gefs_series import SOURCE_GEFS_SERIES

        assert SOURCE_GEFS_SERIES in SOURCE_ANNOTATORS


# ---------------------------------------------------------------------------
# The report: one file per source (E2), and the day-of semantics (E4)
# ---------------------------------------------------------------------------
class TestCalibrationReport:
    @staticmethod
    def _script():
        import importlib.util

        path = os.path.join(REPO_ROOT, "scripts", "build_calibration.py")
        spec = importlib.util.spec_from_file_location("_build_calibration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_report_filename_is_keyed_by_source(self):
        """Both sources used to write `calibration_report_<date>.md`, so building
        the second silently overwrote the first source's EC-3 sigma table."""
        bc = self._script()
        a = bc.report_filename("gfs_mex", "2026-07-26")
        b = bc.report_filename("gefs", "2026-07-26")
        assert a != b
        assert "gfs_mex" in a and "gefs" in b

    def test_the_gefs_label_matches_the_real_constant(self):
        from src.calibration.gefs_series import SOURCE_GEFS_SERIES

        assert self._script().SOURCE_GEFS == SOURCE_GEFS_SERIES

    @pytest.mark.skipif(
        not os.path.exists(REAL_FORECAST_CSV),
        reason="run scripts/backfill_forecasts.py first",
    )
    def test_the_report_states_what_day_of_may_not_be_used_for(self):
        """FR-3.1(b) needs a midday re-forecast; these sigma are evening-before."""
        bc = self._script()
        results = build_all(
            forecast_csv=REAL_FORECAST_CSV,
            stations=STATIONS,
            source="gfs_mex",
            version=1,
        )
        text = bc.render_report(
            results,
            source="gfs_mex",
            version=1,
            forecast_csv=REAL_FORECAST_CSV,
            report_date="2026-07-26",
            determinism_evidence="(not checked)",
        )
        assert "FR-3.1(b)" in text
        assert "not an intraday update" in text
        assert "must not be applied to one" in text


# ---------------------------------------------------------------------------
# Sigma fragility -- the numbers behind the published knife-edge discussion
# ---------------------------------------------------------------------------
def test_day_of_sensitivity_splits_chronologically_and_leaves_one_out():
    from src.calibration.forecast_calibration import CityResult, day_of_sensitivity

    # First 10 days err by +/-4, second 10 by +/-1: a deliberately unstable
    # sample whose pooled sigma hides a materially worse first half.
    errors = [4.0 if i % 2 else -4.0 for i in range(10)]
    errors += [1.0 if i % 2 else -1.0 for i in range(10)]
    days = _days(20, forecast=[90 + e for e in errors], truth=90)
    result = CityResult(city="NY", station="KNYC", payload={}, paired=days)
    s = day_of_sensitivity(result)

    assert s["n"] == 20
    assert s["first_half"]["n"] == 10 and s["second_half"]["n"] == 10
    assert s["first_half"]["first_target_date"] == "2026-01-01"
    assert s["second_half"]["first_target_date"] == "2026-01-11"
    assert (
        s["first_half"]["sigma_f"] > s["sigma_f"] > s["second_half"]["sigma_f"]
    ), "a pooled sigma that hides a worse first half is the whole point"
    loo = s["leave_one_out_sigma_f"]
    assert loo["min"] <= s["sigma_f"] <= loo["max"]


def test_day_of_sensitivity_declines_to_report_on_a_tiny_sample():
    from src.calibration.forecast_calibration import CityResult, day_of_sensitivity

    days = _days(3, forecast=90.0, truth=88.0)
    assert (
        day_of_sensitivity(
            CityResult(city="NY", station="KNYC", payload={}, paired=days)
        )
        == {}
    )
