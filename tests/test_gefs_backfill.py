"""Offline tests for the GEFS derived-product backfill (workstream F).

No test here touches the network. The one real-data fixture,
``tests/fixtures/gefs_backfill/cycle_2026072000_records.json``, holds the
decoded node values of 22 real records (``geavg`` + ``gespr``, ``TMAX`` 2 m,
cycle 2026-07-20 00Z) exactly as the provider's record cache wrote them. It is
plain JSON, so it needs no ``.gitattributes`` binary exception.

The tests that carry weight are the ones that would catch a *plausible* wrong
answer rather than a crash:

* :class:`TestSpreadUnits` -- a spread converted with the affine K->F formula
  gives roughly -458 F, which is wrong by 460 degrees and still looks like a
  number in a CSV column.
* :class:`TestOffsetIsMeasuredNotTyped` -- recomputes the published per-city
  offset constants from workstream A's committed 31-member artifact, so the
  constants cannot drift from the measurement they claim to be.
* :class:`TestOffsetCannotMoveSigma` -- the load-bearing claim of the whole
  design. A constant per-city correction shifts ``bias_f`` by exactly its own
  value and leaves ``sigma_f`` untouched, so the EC-3 sigma verdict does not
  depend on whether the correction is applied.
* :class:`TestIncompleteCoverageIsAnError` -- a daily maximum computed over
  part of the day is not a daily maximum. It must raise, not return a smaller
  number.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.calibration import forecast_calibration as fc  # noqa: E402  (path insert above must run first)
from src.calibration.gefs_series import (  # noqa: E402  (path insert above must run first)
    FORECAST_FIELDS,
    GEAVG_TO_MEMBER_MEAN_OFFSET_F,
    GEFS_GRID_0P5,
    MAX_TRUSTED_OFFSET_F,
    SOURCE_GEFS_SERIES,
    GefsSeriesError,
    assemble_daily_high,
    cycle_plan,
    half_degree_nearest_node,
    kelvin_spread_to_fahrenheit,
    lead_hours_for,
    offset_for,
    windows_for,
)
from src.data.ensemble_provider import (  # noqa: E402  (path insert above must run first)
    CITIES,
    GEFS_GRID,
    MEAN_PRODUCT,
    SPREAD_PRODUCT,
    EnsembleUnavailable,
    get_city,
    haversine_km,
    kelvin_to_fahrenheit,
    spec_nearest_node,
)

FIXTURE = os.path.join(
    _HERE, "fixtures", "gefs_backfill", "cycle_2026072000_records.json"
)
EC1_ARTIFACT = os.path.join(_ROOT, "reports", "phase2", "ec1_ensemble_members.json")

INIT = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cycle_records():
    """The recorded cycle, keyed the way :func:`assemble_daily_high` wants it."""
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    out = {}
    for key, record in blob["records"].items():
        product, fhour = key.split("|")
        out[(product, int(fhour))] = record
    return out


# ---------------------------------------------------------------------------
class TestSpreadUnits:
    """``gespr`` is a dispersion. The affine K->F conversion would destroy it."""

    def test_spread_conversion_is_scale_only(self):
        assert kelvin_spread_to_fahrenheit(1.0) == pytest.approx(1.8)
        assert kelvin_spread_to_fahrenheit(0.0) == pytest.approx(0.0)
        assert kelvin_spread_to_fahrenheit(2.5) == pytest.approx(4.5)

    def test_affine_conversion_of_a_spread_is_catastrophically_wrong(self):
        # This is the trap, made explicit: a 1.5 K ensemble spread is 2.7 F. Run
        # through kelvin_to_fahrenheit it becomes about -457 F -- a number that
        # is wrong by 460 degrees and still parses fine as a CSV float.
        assert kelvin_spread_to_fahrenheit(1.5) == pytest.approx(2.7)
        assert kelvin_to_fahrenheit(1.5) < -450.0

    @pytest.mark.parametrize("k", [0.1, 0.5, 1.0, 2.0, 5.0, 12.0])
    def test_the_two_conversions_never_agree_on_a_nonzero_spread(self, k):
        assert kelvin_spread_to_fahrenheit(k) != pytest.approx(kelvin_to_fahrenheit(k))

    def test_a_negative_spread_is_refused(self):
        with pytest.raises(GefsSeriesError):
            kelvin_spread_to_fahrenheit(-0.1)


# ---------------------------------------------------------------------------
class TestOffsetIsMeasuredNotTyped:
    """The published constants must equal the artifact they cite."""

    def test_constants_reproduce_the_ec1_artifact(self):
        with open(EC1_ARTIFACT, "r", encoding="utf-8") as fh:
            artifact = json.load(fh)
        per_city = {}
        for run in artifact["runs"]:
            check = run.get("geavg_check") or {}
            if "member_mean_minus_geavg_f" not in check:
                continue
            per_city.setdefault(run["city"], []).append(
                check["member_mean_minus_geavg_f"]
            )

        assert set(per_city) == set(
            GEAVG_TO_MEMBER_MEAN_OFFSET_F
        ), "the offset table must cover exactly the cities the artifact measured"
        for city, values in per_city.items():
            assert len(values) >= 5, f"{city}: only {len(values)} measured cycles"
            measured = round(sum(values) / len(values), 4)
            assert (
                GEAVG_TO_MEMBER_MEAN_OFFSET_F[city] == pytest.approx(measured, abs=5e-5)
            ), f"{city}: published {GEAVG_TO_MEMBER_MEAN_OFFSET_F[city]} != measured {measured}"

    def test_every_offset_is_small_against_the_sigma_being_calibrated(self):
        # The whole justification for the geavg shortcut. Forecast-error sigma is
        # of order 2-4 F; if the statistic gap were the same size, the shortcut
        # would be unusable regardless of how carefully it was corrected.
        assert max(abs(v) for v in GEAVG_TO_MEMBER_MEAN_OFFSET_F.values()) < 0.5

    def test_unknown_city_raises_rather_than_defaulting_to_zero(self):
        with pytest.raises(GefsSeriesError):
            offset_for("SEA")

    def test_the_ceiling_guard_is_not_vacuous(self, monkeypatch):
        # Mutation test: push a measured offset past the trusted ceiling and the
        # guard must fire. A guard nobody has seen fail is not a guard.
        monkeypatch.setitem(
            GEAVG_TO_MEMBER_MEAN_OFFSET_F, "NY", MAX_TRUSTED_OFFSET_F + 0.01
        )
        with pytest.raises(GefsSeriesError, match="ceiling"):
            offset_for("NY")


# ---------------------------------------------------------------------------
class TestOffsetCannotMoveSigma:
    """A constant per-city shift moves bias by exactly itself, and sigma by 0."""

    @staticmethod
    def _calibrate(tmp_path, rows):
        csv_path = tmp_path / "series.csv"
        with open(csv_path, "w", encoding="utf-8", newline="\n") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=list(FORECAST_FIELDS), lineterminator="\n"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        truth_dir = tmp_path / "truth"
        truth_dir.mkdir(exist_ok=True)
        return csv_path, truth_dir

    def test_bias_shifts_by_the_offset_and_sigma_does_not_move(self, tmp_path):
        offset = 0.2514
        errors = [
            -3.0,
            1.5,
            0.0,
            4.25,
            -1.75,
            2.5,
            -0.5,
            3.0,
            -2.25,
            1.0,
            0.75,
            -4.0,
            2.75,
            -1.25,
            0.5,
            3.5,
            -2.5,
            1.25,
            -0.75,
            2.0,
            -3.5,
            0.25,
            1.75,
            -1.5,
            4.0,
        ]
        truths = [80] * len(errors)

        rows_raw, rows_off, truth_rows = [], [], []
        for k, (err, truth) in enumerate(zip(errors, truths)):
            day = date(2026, 3, 1).toordinal() + k
            target = date.fromordinal(day).isoformat()
            base = {
                "city": "NY",
                "station": "KNYC",
                "target_date": target,
                "init_time_utc": f"{target}T00:00:00Z",
                "lead_hours": 4,
                "source": SOURCE_GEFS_SERIES,
                "spread_f": "1.5",
                "provenance": "test",
            }
            rows_raw.append({**base, "forecast_high_f": f"{truth + err:.4f}"})
            rows_off.append({**base, "forecast_high_f": f"{truth + err + offset:.4f}"})
            truth_rows.append({"station": "KNYC", "date": target, "high": truth})

        csv_raw, truth_dir = self._calibrate(tmp_path, rows_raw)
        with open(
            truth_dir / "cli_daily_high_KNYC.csv", "w", encoding="utf-8", newline="\n"
        ) as fh:
            w = csv.DictWriter(
                fh, fieldnames=["station", "date", "high"], lineterminator="\n"
            )
            w.writeheader()
            for r in truth_rows:
                w.writerow(r)
        csv_off = tmp_path / "series_off.csv"
        with open(csv_off, "w", encoding="utf-8", newline="\n") as fh:
            w = csv.DictWriter(
                fh, fieldnames=list(FORECAST_FIELDS), lineterminator="\n"
            )
            w.writeheader()
            for r in rows_off:
                w.writerow(r)

        class _Spec:
            timezone = "America/New_York"
            series_ticker = "KXHIGHNY"

        stations = {"KNYC": _Spec()}
        raw = fc.build_all(
            forecast_csv=str(csv_raw),
            stations=stations,
            source=SOURCE_GEFS_SERIES,
            version=1,
            truth_dir=str(truth_dir),
        )[0].payload["day_of"]
        shifted = fc.build_all(
            forecast_csv=str(csv_off),
            stations=stations,
            source=SOURCE_GEFS_SERIES,
            version=1,
            truth_dir=str(truth_dir),
        )[0].payload["day_of"]

        assert raw["sufficient"] and shifted["sufficient"]
        assert shifted["sigma_f"] == pytest.approx(
            raw["sigma_f"], abs=1e-9
        ), "a constant shift changed sigma -- the design's central claim is false"
        assert shifted["bias_f"] == pytest.approx(raw["bias_f"] + offset, abs=1e-3)


# ---------------------------------------------------------------------------
class TestWindowing:
    """The backfill must window exactly the way the live provider does."""

    # Golden fhour sets for a 00Z cycle, read off the same tmax_windows() the
    # live path uses. If these move, the two sources' day_of buckets stop
    # meaning the same thing.
    GOLDEN = {
        (0, "NY"): ((6, 12, 18, 24, 30), 4),
        (0, "CHI"): ((6, 12, 18, 24, 30), 5),
        (0, "LAX"): ((12, 18, 24, 30, 33), 7),
        (0, "MIA"): ((6, 12, 18, 24, 30), 4),
        (1, "NY"): ((30, 36, 42, 48, 54), 28),
        (1, "CHI"): ((30, 36, 42, 48, 54), 29),
        (1, "LAX"): ((36, 42, 48, 54, 57), 31),
        (1, "MIA"): ((30, 36, 42, 48, 54), 28),
    }

    @pytest.mark.parametrize("key", sorted(GOLDEN))
    def test_windows_and_lead_hours_match_the_live_definition(self, key):
        lead_day, city = key
        fhours, lead = self.GOLDEN[key]
        spec = get_city(city)
        target = INIT.date().replace(day=20 + lead_day)
        assert tuple(w.fhour for w in windows_for(spec, target, INIT)) == fhours
        assert lead_hours_for(spec, target, INIT) == lead

    def test_lead_hours_are_timezone_aware_not_utc(self):
        # A naive UTC reading would give the same lead for every city. The UTC
        # hour-gate bug in the pre-pivot weather stack was exactly this defect.
        leads = {
            c: lead_hours_for(get_city(c), date(2026, 7, 21), INIT)
            for c in ("NY", "CHI", "LAX", "MIA")
        }
        assert len(set(leads.values())) > 1, leads
        assert leads["LAX"] > leads["CHI"] > leads["NY"] == leads["MIA"]

    def test_cycle_plan_unions_forecast_hours_across_cities(self):
        plan, fhours = cycle_plan(INIT, (0, 1), ("NY", "CHI", "LAX", "MIA"))
        assert len(plan) == 8
        assert fhours == (6, 12, 18, 24, 30, 33, 36, 42, 48, 54, 57)
        # The union is strictly smaller than the sum: that saving is the whole
        # reason a 200-cycle backfill is affordable.
        assert len(fhours) < sum(len(w) for w in plan.values())

    def test_an_unreachable_lead_is_omitted_not_clamped(self):
        # lead-day -1 asks a cycle to forecast its own past.
        plan, _ = cycle_plan(INIT, (-1, 0), ("NY",))
        assert ("NY", -1) not in plan
        assert ("NY", 0) in plan


# ---------------------------------------------------------------------------
class TestAssembly:
    def _build(self, records, city="NY", lead_day=1):
        spec = get_city(city)
        target = date(2026, 7, 20 + lead_day)
        return assemble_daily_high(
            spec=spec,
            target_date=target,
            init_time=INIT,
            windows=windows_for(spec, target, INIT),
            records=records,
        )

    def test_reproduces_workstream_a_geavg_daily_high(self, cycle_records):
        # reports/phase2/ec1_ensemble_members.json runs[].geavg_check.daily_high_f
        # for target 2026-07-21, produced by a different script in a different
        # workstream. Matching it end-to-end means the node, the windowing and
        # the interval algebra all agree with the live path.
        expected = {"NY": 83.03, "CHI": 84.47, "LAX": 81.41, "MIA": 88.79}
        for city, value in expected.items():
            got = self._build(cycle_records, city=city, lead_day=1)
            assert got.raw_high_f == pytest.approx(value, abs=5e-3), city

    def test_forecast_high_is_raw_plus_the_city_offset(self, cycle_records):
        for city in ("NY", "CHI", "LAX", "MIA"):
            got = self._build(cycle_records, city=city)
            assert got.offset_f == offset_for(city)
            assert got.forecast_high_f == pytest.approx(
                got.raw_high_f + offset_for(city), abs=1e-4
            )

    def test_spread_comes_from_the_argmax_interval_not_the_max_of_spreads(
        self, cycle_records
    ):
        got = self._build(cycle_records, city="NY", lead_day=1)
        node = spec_nearest_node(get_city("NY"))
        node_key = f"{node[0]},{node[1]}"
        at_argmax = kelvin_spread_to_fahrenheit(
            float(
                cycle_records[(SPREAD_PRODUCT, got.argmax_fhour)]["nodes_k"][node_key]
            )
        )
        assert got.spread_f == pytest.approx(at_argmax, abs=1e-4)

        windows = windows_for(get_city("NY"), date(2026, 7, 21), INIT)
        spreads = [
            kelvin_spread_to_fahrenheit(
                float(cycle_records[(SPREAD_PRODUCT, w.fhour)]["nodes_k"][node_key])
            )
            for w in windows
        ]
        # The fixture must actually discriminate the rule: if every interval had
        # the same spread this test would pass for the wrong reason.
        assert max(spreads) - min(spreads) > 0.1, spreads

    def test_spread_is_a_plausible_dispersion_not_an_affine_conversion(
        self, cycle_records
    ):
        for city in ("NY", "CHI", "LAX", "MIA"):
            got = self._build(cycle_records, city=city)
            assert got.spread_f is not None
            assert 0.0 <= got.spread_f < 30.0, (city, got.spread_f)

    def test_argmax_fhour_is_one_of_the_covering_windows(self, cycle_records):
        got = self._build(cycle_records, city="LAX", lead_day=1)
        assert got.argmax_fhour in got.fhours

    def test_provenance_names_the_statistic_and_the_offset(self, cycle_records):
        got = self._build(cycle_records, city="NY")
        text = got.provenance_string()
        assert "geavg" in text and "gespr" in text
        assert "statistic=max_t(geavg)+offset" in text
        assert f"offset_f={got.offset_f:+.4f}" in text
        assert f"raw_f={got.raw_high_f:.4f}" in text
        assert f"f{got.argmax_fhour:03d}" in text
        assert "noaa-gefs-pds" in text

    def test_csv_row_matches_the_calibrator_schema(self, cycle_records):
        row = self._build(cycle_records).csv_row()
        assert set(row) == set(FORECAST_FIELDS)
        assert row["source"] == SOURCE_GEFS_SERIES
        # Distinct from workstream B's source and from the live runtime tag, so
        # no calibration file can be confused for the other.
        assert row["source"] not in ("gfs_mex", "gefs_aws_pgrb2sp25")
        float(row["forecast_high_f"])
        float(row["spread_f"])

    def test_the_calibrator_ingests_the_row_unchanged(self, cycle_records, tmp_path):
        rows = [
            self._build(cycle_records, city=c).csv_row()
            for c in ("NY", "CHI", "LAX", "MIA")
        ]
        path = tmp_path / "series.csv"
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            w = csv.DictWriter(
                fh, fieldnames=list(FORECAST_FIELDS), lineterminator="\n"
            )
            w.writeheader()
            for r in rows:
                w.writerow(r)
        parsed = fc.load_forecast_series(str(path))
        assert len(parsed) == 4
        assert all(p["source"] == SOURCE_GEFS_SERIES for p in parsed)
        # spread_f must survive as a real float, not as the None that means
        # "this source publishes no spread".
        assert all(isinstance(p["spread_f"], float) for p in parsed)


# ---------------------------------------------------------------------------
class TestIncompleteCoverageIsAnError:
    def test_a_missing_mean_record_raises_instead_of_maximising_over_less(
        self, cycle_records
    ):
        spec = get_city("NY")
        target = date(2026, 7, 21)
        windows = windows_for(spec, target, INIT)
        crippled = {
            k: v
            for k, v in cycle_records.items()
            if k != (MEAN_PRODUCT, windows[0].fhour)
        }
        with pytest.raises(GefsSeriesError, match="incomplete"):
            assemble_daily_high(
                spec=spec,
                target_date=target,
                init_time=INIT,
                windows=windows,
                records=crippled,
            )

    def test_a_record_missing_this_citys_node_raises(self, cycle_records):
        spec = get_city("NY")
        target = date(2026, 7, 21)
        windows = windows_for(spec, target, INIT)
        node = spec_nearest_node(spec)
        stripped = dict(cycle_records)
        blob = dict(stripped[(MEAN_PRODUCT, windows[0].fhour)])
        blob["nodes_k"] = {
            k: v for k, v in blob["nodes_k"].items() if k != f"{node[0]},{node[1]}"
        }
        stripped[(MEAN_PRODUCT, windows[0].fhour)] = blob
        with pytest.raises(GefsSeriesError, match="no value at node"):
            assemble_daily_high(
                spec=spec,
                target_date=target,
                init_time=INIT,
                windows=windows,
                records=stripped,
            )

    def test_a_missing_spread_leaves_spread_f_null_not_zero(self, cycle_records):
        # "this record was not read" and "the ensemble agreed perfectly" are
        # different facts. Encoding the first as 0.0 would size positions as if
        # the forecast were certain.
        spec = get_city("NY")
        target = date(2026, 7, 21)
        windows = windows_for(spec, target, INIT)
        no_spread = {k: v for k, v in cycle_records.items() if k[0] != SPREAD_PRODUCT}
        got = assemble_daily_high(
            spec=spec,
            target_date=target,
            init_time=INIT,
            windows=windows,
            records=no_spread,
        )
        assert got.spread_f is None
        assert got.csv_row()["spread_f"] == ""


# ---------------------------------------------------------------------------
class TestResolutionChoice:
    """Evidence that 0.5 deg was rejected on geometry, not on preference."""

    def test_half_degree_moves_the_node_away_from_three_of_four_stations(self):
        moved = {}
        for city, spec in CITIES.items():
            n25 = spec_nearest_node(spec)
            n50 = half_degree_nearest_node(spec)
            d25 = haversine_km(
                spec.latitude, spec.longitude, *GEFS_GRID.node_lat_lon(*n25)
            )
            d50 = haversine_km(
                spec.latitude, spec.longitude, *GEFS_GRID_0P5.node_lat_lon(*n50)
            )
            moved[city] = (round(d25, 1), round(d50, 1))
        assert moved["LAX"][0] == pytest.approx(moved["LAX"][1], abs=0.05)
        for city in ("NY", "CHI", "MIA"):
            assert moved[city][1] > moved[city][0] + 10.0, (city, moved[city])

    def test_the_half_degree_grid_constant_is_self_consistent(self):
        assert GEFS_GRID_0P5.ni * GEFS_GRID_0P5.dlon == pytest.approx(360.0)
        assert (GEFS_GRID_0P5.nj - 1) * GEFS_GRID_0P5.dlat == pytest.approx(180.0)


# ---------------------------------------------------------------------------
class TestSeriesCsvWriter:
    def _rows(self):
        import scripts.backfill_ensemble_history as bh  # noqa: F401

        return [
            {
                "city": c,
                "station": get_city(c).station,
                "target_date": d,
                "init_time_utc": f"{d}T00:00:00Z",
                "lead_hours": lh,
                "source": SOURCE_GEFS_SERIES,
                "forecast_high_f": "80.0000",
                "spread_f": "1.5000",
                "provenance": "p",
            }
            for c, d, lh in [
                ("NY", "2026-03-02", 28),
                ("CHI", "2026-03-01", 5),
                ("NY", "2026-03-01", 4),
                ("LAX", "2026-03-01", 7),
            ]
        ]

    def test_output_is_lf_and_deterministically_ordered(self, tmp_path):
        import scripts.backfill_ensemble_history as bh

        rows = self._rows()
        a = tmp_path / "a.csv"
        b = tmp_path / "b.csv"
        bh.write_series_csv(rows, str(a))
        bh.write_series_csv(list(reversed(rows)), str(b))
        raw = a.read_bytes()
        assert b"\r\n" not in raw
        assert raw == b.read_bytes(), "row order leaked into the output bytes"
        parsed = list(csv.DictReader(raw.decode("utf-8").splitlines()))
        assert [r["target_date"] for r in parsed] == [
            "2026-03-01",
            "2026-03-01",
            "2026-03-01",
            "2026-03-02",
        ]
        assert list(parsed[0].keys()) == list(FORECAST_FIELDS)


# ---------------------------------------------------------------------------
class TestManifestAndResume:
    def test_a_failed_record_makes_the_cycle_incomplete_and_is_named(
        self, monkeypatch, tmp_path
    ):
        import scripts.backfill_ensemble_history as bh

        class _Provider:
            max_workers = 1
            offline = True

            def __init__(self, records):
                self._records = records

            def fetch_record_values(
                self, init, member, fhour, nodes, *, field_name="TMAX"
            ):
                key = (member, int(fhour))
                if key == (MEAN_PRODUCT, 30):
                    raise EnsembleUnavailable(
                        "ENSEMBLE_S3_UNAVAILABLE",
                        f"induced failure on {member} f{fhour:03d}",
                    )
                if key not in self._records:
                    raise EnsembleUnavailable("ENSEMBLE_RECORD_NOT_FOUND", str(key))
                return dict(self._records[key])

        with open(FIXTURE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        records = {}
        for key, record in blob["records"].items():
            product, fhour = key.split("|")
            records[(product, int(fhour))] = record

        result = bh.backfill_cycle(_Provider(records), INIT, (0, 1))
        assert result["complete"] is False
        assert "geavg:f030" in result["record_failures"]
        assert result["row_errors"], "a row that lost a covering interval must be named"
        assert any(r.get("error") for r in result["records"])

        manifest = tmp_path / "m.json"
        bh.write_manifest(
            {
                "cycles": [result],
                "missing": [{"cycle": result["cycle"]}],
                "stats": {"cycles_requested": 1},
            },
            str(manifest),
            argv=["--test"],
        )
        written = json.loads(manifest.read_text(encoding="utf-8"))
        assert written["missing"], "an incomplete cycle must appear under 'missing'"
        assert written["resolution"] == "0p25"
        assert written["geavg_to_member_mean_offset_f"] == dict(
            GEAVG_TO_MEMBER_MEAN_OFFSET_F
        )
        keys = [
            r["s3_key"]
            for c in written["cycles"]
            for r in c["records"]
            if r.get("s3_key")
        ]
        assert keys and all("noaa-gefs-pds" not in k for k in keys)
        assert all(k.startswith("gefs.20260720/00/atmos/pgrb2sp25/") for k in keys)
        ranges = [
            r["range"]
            for c in written["cycles"]
            for r in c["records"]
            if r.get("range")
        ]
        assert ranges and all(r.startswith("bytes=") for r in ranges)

    def test_offline_rebuild_refuses_to_emit_a_shorter_series(
        self, monkeypatch, tmp_path
    ):
        import scripts.backfill_ensemble_history as bh

        monkeypatch.setattr(bh, "SERIES_CACHE_DIR", str(tmp_path / "empty"))
        with pytest.raises(SystemExit, match="Refusing to emit a shorter series"):
            bh.rebuild_from_cache(date(2026, 7, 20), date(2026, 7, 21), (0, 1))


# ---------------------------------------------------------------------------
SERIES_CSV = os.path.join(_ROOT, "data", "forecast_archive", "forecast_series_gefs.csv")
CALIBRATION_DIR = os.path.join(_ROOT, "data", "calibration")

pytestmark_needs_series = pytest.mark.skipif(
    not os.path.exists(SERIES_CSV), reason="the GEFS series has not been backfilled yet"
)


@pytestmark_needs_series
class TestCommittedArtifacts:
    """Phase 2 EC-2: the calibration must be byte-identical on re-run."""

    @staticmethod
    def _build(out_dir):
        import scripts.backfill_ensemble_history as bh

        from src.data.iem_cli_provider import CORE_STATIONS

        results = fc.build_all(
            forecast_csv=SERIES_CSV,
            stations=CORE_STATIONS,
            source=SOURCE_GEFS_SERIES,
            version=1,
        )
        return {
            r.city: fc.write_calibration(str(out_dir), bh.stamp_statistic(r.payload))
            for r in results
        }

    def test_the_artifact_names_the_statistic_it_was_built_on(self):
        # The one thing that stops a *_gefs_v1.json from being mistaken for a
        # calibration of the live member-mean statistic.
        for city in ("NY", "CHI", "LAX", "MIA"):
            path = os.path.join(CALIBRATION_DIR, f"{city}_{SOURCE_GEFS_SERIES}_v1.json")
            if not os.path.exists(path):
                pytest.skip(f"{path} not built yet")
            payload = fc.load_calibration(path)
            block = payload["statistic"]
            assert block["are_they_the_same"] is False
            assert "geavg" in block["built_on"]
            assert "member" in block["live_provider_statistic"]
            assert block["offset_applied_f"] == GEAVG_TO_MEMBER_MEAN_OFFSET_F[city]

    def test_stamping_is_additive_and_carries_no_wallclock(self):
        import scripts.backfill_ensemble_history as bh

        base = {
            "city": "NY",
            "source": SOURCE_GEFS_SERIES,
            "version": 1,
            "day_of": {"n": 1},
        }
        stamped = bh.stamp_statistic(fc.finalize(dict(base)))
        for key, value in base.items():
            assert stamped[key] == value, f"{key} was altered by stamping"
        assert fc.verify_content_hash(stamped)
        text = json.dumps(stamped)
        for banned in ("generated_at", "hostname", "run_id", "fetched_at", "Users"):
            assert banned not in text

    def test_rebuild_from_disk_into_two_directories_is_byte_identical(self, tmp_path):
        # Built twice from the files on disk into two separate directories and
        # compared as *file bytes* -- not as a re-serialized in-memory object,
        # which would prove much less.
        a = self._build(tmp_path / "a")
        b = self._build(tmp_path / "b")
        assert set(a) == set(b) == {"NY", "CHI", "LAX", "MIA"}
        for city in sorted(a):
            with open(a[city], "rb") as fh:
                left = fh.read()
            with open(b[city], "rb") as fh:
                right = fh.read()
            assert left == right, f"{city}: rebuild is not byte-identical"
            assert b"\r\n" not in left, f"{city}: artifact written with CRLF"

    def test_committed_artifacts_match_a_fresh_rebuild(self, tmp_path):
        fresh = self._build(tmp_path / "fresh")
        for city, path in sorted(fresh.items()):
            committed = os.path.join(
                CALIBRATION_DIR, f"{city}_{SOURCE_GEFS_SERIES}_v1.json"
            )
            if not os.path.exists(committed):
                pytest.skip(f"{committed} not built yet")
            with open(path, "rb") as fh:
                built = fh.read()
            with open(committed, "rb") as fh:
                stored = fh.read()
            assert built == stored, f"{city}: committed calibration != fresh rebuild"

    def test_committed_calibration_loads_and_verifies_its_own_hash(self):
        for city in ("NY", "CHI", "LAX", "MIA"):
            path = os.path.join(CALIBRATION_DIR, f"{city}_{SOURCE_GEFS_SERIES}_v1.json")
            if not os.path.exists(path):
                pytest.skip(f"{path} not built yet")
            payload = fc.load_calibration(path)  # re-verifies content_hash
            assert payload["source"] == SOURCE_GEFS_SERIES
            assert payload["city"] == city

    def test_the_gefs_series_populates_spread_unlike_gfs_mex(self):
        rows = fc.load_forecast_series(SERIES_CSV)
        with_spread = [r for r in rows if r["spread_f"] is not None]
        assert len(with_spread) / max(1, len(rows)) > 0.95, (
            "spread_f is the column workstream B could not populate at all; if it "
            "is mostly empty here the gespr read is not working"
        )
        assert all(0.0 <= r["spread_f"] < 30.0 for r in with_spread)

    def test_the_series_is_lf_and_carries_only_the_gefs_source(self):
        with open(SERIES_CSV, "rb") as fh:
            raw = fh.read()
        assert b"\r\n" not in raw
        rows = fc.load_forecast_series(SERIES_CSV)
        assert {r["source"] for r in rows} == {SOURCE_GEFS_SERIES}
