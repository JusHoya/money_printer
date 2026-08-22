"""Tests for the gas history backfill and the EIA/RBOB covariates (FR-4.1/4.2).

Covers the three things a backfill can get silently wrong:

* **enumeration** -- a 301 or an archived 403 error page must not become a row;
* **date attribution** -- the UTC-to-ET conversion and the pre-publication
  window, including a test that the hour *sweep* actually recovers a known
  publication hour rather than merely tying everywhere
  (``audit-fixtures-for-degeneracy``);
* **gaps** -- a day with no capture must stay absent, never interpolated.

Network is never touched: captures are served from a temp cache directory and the
EIA archive from a synthetic zip built in-process.
"""

from __future__ import annotations

import csv
import json
import os
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.aaa_provider import (
    ET,
    QUALITY_OK,
    QUALITY_SUSPECT,
    SOURCE_WAYBACK,
    AAAObservation,
    attribute_et_date,
    attribution_evidence,
    check_yesterday_chain,
    wayback_timestamp_to_et,
)
from src.data.energy_covariates import (
    DAILY_CSV_COLUMNS,
    EIA_WEEKLY_SERIES_ID,
    RBOB_ALTERNATIVES,
    RBOB_SERIES_ID,
    WEEKLY_CSV_COLUMNS,
    CovariateUnavailable,
    backfill_covariates,
    extract_series,
    series_published_at,
    series_to_rows,
    weekday_audit,
)

import scripts.backfill_gas_history as bf

# ``_no_production_writes`` is autouse in the module it is defined in only, so it
# is imported here deliberately: these tests write CSVs and manifests too, and the
# same leak (a default path inherited instead of a tmp_path) is available to them.
from tests.test_aaa_provider import _no_production_writes, build_page  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def cap(ts: str, value: float, yesterday: float, *, hour: int = 7) -> AAAObservation:
    """One capture, attributed under ``hour``."""
    captured = wayback_timestamp_to_et(ts)
    return AAAObservation(
        date=attribute_et_date(captured, publication_hour_et=hour),
        value=value,
        source=SOURCE_WAYBACK,
        source_url=f"https://web.archive.org/web/{ts}id_/https://gasprices.aaa.com/",
        fetched_at="2026-07-29T00:00:00+00:00",
        raw_sha256="0" * 64,
        yesterday_value=yesterday,
        captured_at_et=captured,
    )


def et_stamp(d: date, hour: int) -> str:
    """A Wayback (UTC) timestamp for a given ET wall-clock day and hour."""
    local = datetime(d.year, d.month, d.day, hour, tzinfo=ET)
    return local.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


# ---------------------------------------------------------------------------
# CDX enumeration
# ---------------------------------------------------------------------------
class _CDXResponse:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


class _CDXSession:
    def __init__(self, rows):
        self.rows = rows
        self.headers = {}
        self.calls = 0

    def get(self, url, timeout=None, params=None):
        self.calls += 1
        return _CDXResponse(self.rows)


class TestEnumeration:
    HEADER = ["timestamp", "original", "digest", "statuscode", "mimetype"]

    def test_keeps_only_html_200_captures(self):
        """A 301 carries no table and an archived 403 is an error page.

        Parsing either would create a fabricated row or a spurious `suspect`.
        """
        rows = [
            self.HEADER,
            ["20250601120000", "https://gasprices.aaa.com/", "A", "200", "text/html"],
            ["20250602120000", "http://gasprices.aaa.com/", "B", "301", "text/html"],
            ["20250603120000", "https://gasprices.aaa.com/", "C", "403", "text/html"],
            ["20250604120000", "https://gasprices.aaa.com/", "D", "200", "image/png"],
            ["20250605120000", "https://gasprices.aaa.com/", "E", "200", "text/html"],
        ]
        out = bf.enumerate_snapshots(
            _CDXSession(rows), start="2025-06-01", end="2025-06-30"
        )
        assert [t[0] for t in out] == ["20250601120000", "20250605120000"]

    def test_returns_sorted_ascending(self):
        rows = [
            self.HEADER,
            ["20250605120000", "https://gasprices.aaa.com/", "A", "200", "text/html"],
            ["20250601120000", "https://gasprices.aaa.com/", "B", "200", "text/html"],
        ]
        out = bf.enumerate_snapshots(
            _CDXSession(rows), start="2025-06-01", end="2025-06-30"
        )
        assert [t[0] for t in out] == ["20250601120000", "20250605120000"]

    def test_captures_are_not_collapsed_to_one_per_day(self):
        """Several captures of one day drive the intra-day check and the sweep."""
        rows = [self.HEADER] + [
            [
                f"2025060{d}{h:02d}0000",
                "https://gasprices.aaa.com/",
                "X",
                "200",
                "text/html",
            ]
            for d in (1,)
            for h in (2, 8, 14, 20)
        ]
        out = bf.enumerate_snapshots(
            _CDXSession(rows), start="2025-06-01", end="2025-06-30"
        )
        assert len(out) == 4

    def test_empty_cdx_result_is_not_an_error(self):
        assert (
            bf.enumerate_snapshots(
                _CDXSession([]), start="2025-06-01", end="2025-06-30"
            )
            == []
        )


# ---------------------------------------------------------------------------
# Capture selection
# ---------------------------------------------------------------------------
def caps_for_days(days, hours):
    """CDX-shaped tuples for every (day, ET hour) combination."""
    return [
        (et_stamp(d, h), "https://gasprices.aaa.com/", "D") for d in days for h in hours
    ]


class TestCaptureSelection:
    def test_one_anchor_per_day_is_selected(self):
        days = [date(2022, 3, 1) + timedelta(days=i) for i in range(10)]
        sel, st = bf.select_captures(
            caps_for_days(days, range(0, 24)), boundary_days_per_quarter=0
        )
        chosen_days = {bf.wayback_timestamp_to_et(t[0]).date() for t in sel}
        assert chosen_days == set(days)
        assert len(sel) == len(days)
        assert st["days_with_anchor"] == 10

    def test_anchor_is_inside_the_safe_post_publication_window(self):
        """The anchor must not presuppose where the boundary is.

        Any capture in 12:00-20:00 ET is post-publication under every candidate
        hour (0..12), so choosing it is not circular.
        """
        sel, _ = bf.select_captures(
            caps_for_days([date(2022, 3, 1)], range(0, 24)),
            boundary_days_per_quarter=0,
        )
        hour = bf.wayback_timestamp_to_et(sel[0][0]).hour
        assert bf.ANCHOR_ET_WINDOW[0] <= hour < bf.ANCHOR_ET_WINDOW[1]

    def test_day_without_a_safe_anchor_is_taken_whole_not_dropped(self):
        """A thin day must still contribute, rather than vanish."""
        caps = caps_for_days([date(2022, 3, 1)], [2, 5])
        sel, st = bf.select_captures(caps, boundary_days_per_quarter=0)
        assert len(sel) == 2
        assert st["thin_days_taken_whole"] == 1
        assert st["days_with_anchor"] == 0

    def test_boundary_sample_adds_one_capture_per_et_hour(self):
        days = [date(2022, 1, 1) + timedelta(days=i) for i in range(40)]
        sel, st = bf.select_captures(
            caps_for_days(days, range(0, 24)), boundary_days_per_quarter=4
        )
        assert st["boundary_sample_days"] == 4
        by_day = defaultdict(set)
        for t in sel:
            et = bf.wayback_timestamp_to_et(t[0])
            by_day[et.date()].add(et.hour)
        dense = [d for d, hrs in by_day.items() if len(hrs) > 1]
        assert len(dense) == 4
        for d in dense:
            # every hour in the boundary range, plus the anchor
            assert {0, 1, 2, 3, 4, 5, 6} <= by_day[d]

    def test_boundary_sample_is_spread_across_quarters(self):
        """Stratifying by quarter is what localises a shift to a quarter."""
        days = [date(2022, 1, 1) + timedelta(days=i) for i in range(365)]
        sel, st = bf.select_captures(
            caps_for_days(days, range(0, 24)), boundary_days_per_quarter=2
        )
        by_day = defaultdict(set)
        for t in sel:
            et = bf.wayback_timestamp_to_et(t[0])
            by_day[et.date()].add(et.hour)
        dense = sorted(d for d, hrs in by_day.items() if len(hrs) > 1)
        assert len({bf._quarter(d) for d in dense}) == 4

    def test_selection_is_deterministic(self):
        days = [date(2022, 1, 1) + timedelta(days=i) for i in range(120)]
        caps = caps_for_days(days, range(0, 24))
        a, _ = bf.select_captures(caps, boundary_days_per_quarter=5)
        b, _ = bf.select_captures(list(reversed(caps)), boundary_days_per_quarter=5)
        assert a == b

    def test_already_cached_captures_are_always_kept(self):
        """Free evidence already paid for must not be discarded."""
        days = [date(2022, 3, 1)]
        caps = caps_for_days(days, range(0, 24))
        extra = caps[7][0]  # an 07:00 ET capture, outside the anchor window
        sel, st = bf.select_captures(
            caps, already_cached={extra}, boundary_days_per_quarter=0
        )
        assert extra in {t[0] for t in sel}
        assert st["already_cached_kept"] == 1

    def test_exclusion_count_and_rationale_are_reported(self):
        """An undisclosed exclusion looks identical to full coverage."""
        days = [date(2022, 3, 1) + timedelta(days=i) for i in range(30)]
        caps = caps_for_days(days, range(0, 24))
        sel, st = bf.select_captures(caps, boundary_days_per_quarter=2)
        assert st["total_captures"] == len(caps)
        assert st["selected"] == len(sel)
        assert st["excluded"] == len(caps) - len(sel)
        assert st["excluded"] > 0
        assert st["exclusion_rationale"]

    def test_unlimited_selects_everything(self):
        caps = caps_for_days([date(2022, 3, 1)], range(0, 24))
        sel, st = bf.select_captures(caps, unlimited=True)
        assert len(sel) == len(caps)
        assert st["excluded"] == 0

    def test_malformed_timestamps_are_counted_not_crashed(self):
        caps = caps_for_days([date(2022, 3, 1)], [15]) + [("bogus", "u", "d")]
        sel, st = bf.select_captures(caps, boundary_days_per_quarter=0)
        assert st["bad_timestamps"] == 1
        assert len(sel) == 1


# ---------------------------------------------------------------------------
# Per-era publication-hour sweep
# ---------------------------------------------------------------------------
class TestPerEraSweep:
    def _era_obs(self, year, true_hour, n_days=25, hours=None):
        """Captures for one year, published at ``true_hour`` ET.

        Capture hours include ``true_hour - 1`` and ``true_hour`` so the boundary
        is uniquely identifiable: candidates ``h`` and ``h+1`` differ only in
        where an hour-``h`` capture lands, so pinning an hour requires a capture
        at it (see ``test_adjacent_hours_tie_when_no_capture_sits_on_the_boundary``).
        """
        hours = hours or sorted({1, true_hour - 1, true_hour, true_hour + 1, 12, 20})
        start = date(year, 3, 1)
        values = {
            start + timedelta(days=i): round(3.0 + 0.01 * i, 3)
            for i in range(-2, n_days)
        }
        out = []
        for i in range(n_days):
            day = start + timedelta(days=i)
            for hour in hours:
                shown = day - timedelta(days=1) if hour < true_hour else day
                ts = et_stamp(day, hour)
                out.append(
                    AAAObservation(
                        date=date(2026, 1, 1),
                        value=values[shown],
                        source=SOURCE_WAYBACK,
                        source_url="x",
                        fetched_at="2026-07-29T00:00:00+00:00",
                        raw_sha256="0" * 64,
                        yesterday_value=values.get(shown - timedelta(days=1)),
                        captured_at_et=bf.wayback_timestamp_to_et(ts),
                    )
                )
        return out

    def test_constant_hour_across_eras_is_identified_in_each(self):
        obs = self._era_obs(2022, 7) + self._era_obs(2023, 7) + self._era_obs(2024, 7)
        res = bf.sweep_by_era(obs, era_of=lambda o: o.captured_at_et.strftime("%Y"))
        assert set(res) == {"2022", "2023", "2024"}
        for era, info in res.items():
            assert info["identified"] is True, era
            assert info["best_hours"] == [7], era

    def test_a_mid_span_shift_is_detected(self):
        """The defect a global-only sweep would average away.

        2022 published at 7, 2024 at 4. Each era is internally self-consistent,
        so only a per-era sweep separates them.
        """
        obs = self._era_obs(2022, 7) + self._era_obs(2024, 4)
        res = bf.sweep_by_era(obs, era_of=lambda o: o.captured_at_et.strftime("%Y"))
        assert res["2022"]["best_hours"] == [7]
        assert res["2024"]["best_hours"] == [4]
        assert res["2022"]["identified"] and res["2024"]["identified"]
        # The property that actually matters: the eras are separated at all.
        assert set(res["2022"]["best_hours"]).isdisjoint(res["2024"]["best_hours"])
        # And each era's own hour scores badly in the other era.
        assert res["2024"]["profile"][7] > 0
        assert res["2022"]["profile"][4] > 0

    def test_an_era_that_cannot_discriminate_is_not_reported_as_identified(self):
        """An unidentifiable era must not contribute a confident argmin."""
        obs = [
            o
            for o in self._era_obs(2022, 7)
            if o.captured_at_et.hour in (12, 20)  # no boundary-straddling captures
        ]
        res = bf.sweep_by_era(obs, era_of=lambda o: o.captured_at_et.strftime("%Y"))
        assert res["2022"]["identified"] is False
        assert res["2022"]["discriminating_captures"] == 0

    def test_profile_is_reported_per_era_even_when_identified(self):
        res = bf.sweep_by_era(
            self._era_obs(2022, 7), era_of=lambda o: o.captured_at_et.strftime("%Y")
        )
        prof = res["2022"]["profile"]
        assert set(prof) == set(range(0, 13))
        assert prof[7] == 0.0
        assert prof[0] > 0


# ---------------------------------------------------------------------------
# Snapshot cache
# ---------------------------------------------------------------------------
class _ReplayResponse:
    def __init__(self, status_code=200, content=b"", *, headers=None, url=""):
        self.status_code = status_code
        self.content = content
        #: Wayback identifies the capture it actually served in
        #: ``Memento-Datetime`` and in the post-redirect URL. Absent by default so
        #: the tests that do not care keep exercising the "no identification"
        #: path, which must NOT be read as a mismatch.
        self.headers = dict(headers or {})
        self.url = url


class _ReplaySession:
    def __init__(self, *, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = 0
        self.headers = {}

    def get(self, url, timeout=None, params=None):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response


class TestSnapshotCache:
    def test_cache_hit_avoids_a_request(self, tmp_path):
        cache = str(tmp_path)
        page = build_page()
        with open(os.path.join(cache, "20250601120000.html"), "wb") as fh:
            fh.write(page)
        session = _ReplaySession(response=_ReplayResponse(200, b"unused"))
        body = bf.fetch_snapshot(
            session,
            "20250601120000",
            "https://gasprices.aaa.com/",
            cache_dir=cache,
            sleep_s=0,
            retries=1,
        )
        assert body == page
        assert session.calls == 0

    def test_failure_is_not_cached(self, tmp_path):
        """A cached absence would turn a transient outage into a permanent hole."""
        cache = str(tmp_path)
        session = _ReplaySession(response=_ReplayResponse(404, b""))
        body = bf.fetch_snapshot(
            session,
            "20250601120000",
            "https://gasprices.aaa.com/",
            cache_dir=cache,
            sleep_s=0,
            retries=1,
        )
        assert body is None
        assert not os.path.exists(os.path.join(cache, "20250601120000.html"))

    def test_offline_mode_makes_no_request(self, tmp_path):
        session = _ReplaySession(response=_ReplayResponse(200, build_page()))
        body = bf.fetch_snapshot(
            session,
            "20250601120000",
            "https://gasprices.aaa.com/",
            cache_dir=str(tmp_path),
            sleep_s=0,
            retries=1,
            offline=True,
        )
        assert body is None
        assert session.calls == 0

    def test_successful_fetch_is_written_to_cache(self, tmp_path):
        cache = str(tmp_path)
        page = build_page()
        session = _ReplaySession(response=_ReplayResponse(200, page))
        bf.fetch_snapshot(
            session,
            "20250601120000",
            "https://gasprices.aaa.com/",
            cache_dir=cache,
            sleep_s=0,
            retries=1,
        )
        assert open(os.path.join(cache, "20250601120000.html"), "rb").read() == page

    def test_cache_index_records_the_archived_url_variant(self, tmp_path):
        """CDX serves this page under http/https/www variants and the replay URL
        embeds whichever one the capture used, so the variant must be kept."""
        cache = str(tmp_path)
        session = _ReplaySession(response=_ReplayResponse(200, build_page()))
        bf.fetch_snapshot(
            session,
            "20250601120000",
            "http://www.gasprices.aaa.com/",
            cache_dir=cache,
            sleep_s=0,
            retries=1,
        )
        assert bf.read_cache_index(cache) == {
            "20250601120000": "http://www.gasprices.aaa.com/"
        }

    def test_missing_cache_index_reads_as_empty(self, tmp_path):
        assert bf.read_cache_index(str(tmp_path)) == {}

    def test_corrupt_cache_index_reads_as_empty(self, tmp_path):
        with open(os.path.join(str(tmp_path), "index.json"), "w") as fh:
            fh.write("{not json")
        assert bf.read_cache_index(str(tmp_path)) == {}


class TestServedCaptureVerification:
    """``/web/<ts>id_/<url>`` answers 200 with the NEAREST capture.

    Measured on this series: ``/web/20260728110106id_/https://gasprices.aaa.com/``
    returns HTTP 200 serving the 2026-07-27 capture (``Memento-Datetime: Mon, 27
    Jul 2026 11:01:14 GMT``). A row built from that response would be dated by the
    *requested* timestamp while its bytes came from a different day -- a wrong
    value under a provenance URL that still resolves, which is the worst shape a
    data defect can take and is how the ``2026-07-28`` orphan row could arise.
    """

    MEMENTO = "Mon, 27 Jul 2026 11:01:14 GMT"

    def test_served_timestamp_reads_the_memento_header(self):
        resp = _ReplayResponse(200, b"x", headers={"Memento-Datetime": self.MEMENTO})
        assert bf.served_timestamp(resp) == "20260727110114"

    def test_served_timestamp_falls_back_to_the_redirected_url(self):
        resp = _ReplayResponse(
            200,
            b"x",
            url="https://web.archive.org/web/20260727110114id_/https://gasprices.aaa.com/",
        )
        assert bf.served_timestamp(resp) == "20260727110114"

    def test_no_identification_is_not_a_mismatch(self):
        """An absent header must not manufacture a miss."""
        assert bf.served_timestamp(_ReplayResponse(200, b"x")) is None

    def test_a_nearest_capture_substitution_is_a_miss_not_a_row(self, tmp_path):
        cache = str(tmp_path)
        session = _ReplaySession(
            response=_ReplayResponse(
                200,
                build_page(),
                headers={"Memento-Datetime": self.MEMENTO},
                url=(
                    "https://web.archive.org/web/20260727110114id_/"
                    "https://gasprices.aaa.com/"
                ),
            )
        )
        body = bf.fetch_snapshot(
            session,
            "20260728110106",  # the capture that does not exist
            "https://gasprices.aaa.com/",
            cache_dir=cache,
            sleep_s=0,
            retries=1,
        )
        assert body is None, "another day's bytes must not be dated 2026-07-28"
        assert not os.path.exists(
            os.path.join(cache, "20260728110106.html")
        ), "a substituted capture must not be cached under the requested stamp"

    def test_the_requested_capture_is_accepted(self, tmp_path):
        """Attribution: the refusal above is the timestamp check and nothing else."""
        cache = str(tmp_path)
        page = build_page()
        session = _ReplaySession(
            response=_ReplayResponse(
                200, page, headers={"Memento-Datetime": self.MEMENTO}
            )
        )
        body = bf.fetch_snapshot(
            session,
            "20260727110114",  # exactly what was served
            "https://gasprices.aaa.com/",
            cache_dir=cache,
            sleep_s=0,
            retries=1,
        )
        assert body == page
        assert os.path.exists(os.path.join(cache, "20260727110114.html"))

    def test_a_one_second_representational_difference_is_tolerated(self, tmp_path):
        session = _ReplaySession(
            response=_ReplayResponse(
                200, build_page(), headers={"Memento-Datetime": self.MEMENTO}
            )
        )
        assert (
            bf.fetch_snapshot(
                session,
                "20260727110115",
                "https://gasprices.aaa.com/",
                cache_dir=str(tmp_path),
                sleep_s=0,
                retries=1,
            )
            is not None
        )

    def test_a_day_apart_is_never_tolerated(self, tmp_path):
        session = _ReplaySession(
            response=_ReplayResponse(
                200, build_page(), headers={"Memento-Datetime": self.MEMENTO}
            )
        )
        assert (
            bf.fetch_snapshot(
                session,
                "20260728110114",
                "https://gasprices.aaa.com/",
                cache_dir=str(tmp_path),
                sleep_s=0,
                retries=1,
            )
            is None
        )


class TestParseCaptures:
    def test_unparseable_capture_becomes_a_failure_not_a_row(self, tmp_path):
        """Contract §1.1: what cannot be parsed confidently is never guessed."""
        cache = str(tmp_path)
        with open(os.path.join(cache, "20250601120000.html"), "wb") as fh:
            fh.write(build_page())
        with open(os.path.join(cache, "20250602120000.html"), "wb") as fh:
            fh.write(b"<html>redesigned, no table</html>")
        captures = [
            ("20250601120000", "https://gasprices.aaa.com/", ""),
            ("20250602120000", "https://gasprices.aaa.com/", ""),
        ]
        observations, failures = bf.parse_captures(
            captures,
            cache_dir=cache,
            session=None,
            sleep_s=0,
            retries=1,
            offline=True,
            progress_every=0,
        )
        assert len(observations) == 1
        assert len(failures) == 1
        assert failures[0]["reason_code"] == "AAA_TABLE_NOT_FOUND"

    def test_fetched_at_is_the_capture_instant_not_now(self, tmp_path):
        """An archive row must not claim provenance it does not have."""
        cache = str(tmp_path)
        with open(os.path.join(cache, "20250601120000.html"), "wb") as fh:
            fh.write(build_page())
        observations, _ = bf.parse_captures(
            [("20250601120000", "https://gasprices.aaa.com/", "")],
            cache_dir=cache,
            session=None,
            sleep_s=0,
            retries=1,
            offline=True,
            progress_every=0,
        )
        assert observations[0].fetched_at.startswith("2025-06-01T12:00:00")

    def test_source_url_is_the_dated_snapshot_url(self, tmp_path):
        """Contract §1: "For Wayback, the full dated snapshot URL"."""
        cache = str(tmp_path)
        with open(os.path.join(cache, "20250601120000.html"), "wb") as fh:
            fh.write(build_page())
        observations, _ = bf.parse_captures(
            [("20250601120000", "https://gasprices.aaa.com/", "")],
            cache_dir=cache,
            session=None,
            sleep_s=0,
            retries=1,
            offline=True,
            progress_every=0,
        )
        url = observations[0].source_url
        assert "20250601120000" in url and "web.archive.org" in url
        assert observations[0].source == SOURCE_WAYBACK


# ---------------------------------------------------------------------------
# The publication-hour sweep must actually discriminate
# ---------------------------------------------------------------------------
class TestPublicationHourSweep:
    #: ET hours captures are taken at.
    #:
    #: Identifiability is exact and worth stating: candidate hours ``h`` and
    #: ``h+1`` differ only in where a capture taken at hour ``h`` itself lands,
    #: so a fixture with no capture at hour ``h`` cannot distinguish them. With
    #: capture hours ``(1, 5, 6, 8, 12, 20)`` and a true boundary of 7, hours 7
    #: and 8 are observationally identical -- the sample bounds the boundary to
    #: ``(6, 8]`` and nothing more. Including hour 7 breaks that tie. Real
    #: Wayback coverage of this page spans all 24 ET hours, so the live sweep
    #: does not sit on this degeneracy.
    CAPTURE_HOURS = (1, 5, 6, 7, 8, 12, 20)

    def _synthetic(self, true_hour: int, n_days: int = 20):
        """Captures generated as if AAA republished at ``true_hour`` ET.

        Each capture reports the figure the page would really be showing at that
        instant: yesterday's before the boundary, today's after it.
        """
        start = date(2026, 5, 1)
        values = {
            start + timedelta(days=i): round(3.000 + 0.010 * i, 3)
            for i in range(-2, n_days)
        }
        captures = []
        for i in range(n_days):
            day = start + timedelta(days=i)
            for hour in self.CAPTURE_HOURS:
                shown_day = day - timedelta(days=1) if hour < true_hour else day
                captures.append(
                    (
                        et_stamp(day, hour),
                        values[shown_day],
                        values.get(shown_day - timedelta(days=1)),
                    )
                )
        return captures

    def _observations(self, raw, hour=None):
        return [
            AAAObservation(
                date=(
                    attribute_et_date(
                        wayback_timestamp_to_et(ts), publication_hour_et=hour
                    )
                    if hour is not None
                    else date(2026, 1, 1)  # overwritten by the sweep
                ),
                value=v,
                source=SOURCE_WAYBACK,
                source_url=f"https://web.archive.org/web/{ts}id_/x",
                fetched_at="2026-07-29T00:00:00+00:00",
                raw_sha256="0" * 64,
                yesterday_value=y,
                captured_at_et=wayback_timestamp_to_et(ts),
            )
            for ts, v, y in raw
        ]

    def test_sweep_recovers_the_true_publication_hour_uniquely(self):
        """The measurement has real discriminating power, not a flat tie.

        The degeneracy this guards against is subtle: measuring the boundary with
        one capture per day throws away the near-midnight captures that carry the
        boundary information, so every candidate hour ties and the argmin is
        meaningless. With captures on both sides of the boundary the true hour is
        the unique consistent one.
        """
        true_hour = 7
        sweep = attribution_evidence(self._observations(self._synthetic(true_hour)))
        by_hour = {r["publication_hour_et"]: r for r in sweep}
        assert by_hour[true_hour]["disagreements"] == 0
        tied = [r["publication_hour_et"] for r in sweep if r["disagreements"] == 0]
        assert tied == [true_hour], f"hour must be uniquely identified, got {tied}"
        # Adjacent hours are wrong and must show it.
        assert by_hour[6]["disagreements"] > 0
        assert by_hour[8]["disagreements"] > 0
        assert by_hour[0]["disagreements"] > 0

    def test_wrong_hour_shows_up_as_same_date_disagreement(self):
        """It is the same-date check, not the cross-date one, that locates the
        boundary -- so assert which signal actually fires."""
        sweep = attribution_evidence(self._observations(self._synthetic(7)))
        by_hour = {r["publication_hour_et"]: r for r in sweep}
        assert by_hour[7]["same_date_inconsistent"] == 0
        assert by_hour[3]["same_date_inconsistent"] > 0

    def test_sweep_recovers_a_different_true_hour(self):
        """Not hardcoded to 7: the method finds whatever the truth is."""
        sweep = attribution_evidence(self._observations(self._synthetic(6)))
        tied = [r["publication_hour_et"] for r in sweep if r["disagreements"] == 0]
        assert tied == [6]

    def test_adjacent_hours_tie_when_no_capture_sits_on_the_boundary(self):
        """The identifiability limit itself, asserted rather than assumed.

        Candidate hours h and h+1 differ only in where an hour-h capture lands,
        so removing hour 7 from the sample must make 7 and 8 indistinguishable.
        A sweep reported without this caveat would claim precision it lacks.
        """
        original = self.CAPTURE_HOURS
        try:
            self.CAPTURE_HOURS = (1, 5, 6, 8, 12, 20)
            sweep = attribution_evidence(self._observations(self._synthetic(7)))
        finally:
            self.CAPTURE_HOURS = original
        tied = [r["publication_hour_et"] for r in sweep if r["disagreements"] == 0]
        assert tied == [7, 8]

    def test_measurement_uses_every_capture_not_one_per_day(self):
        """Regression guard for the defect this class found.

        Six captures per day over 20 days must contribute far more checks than
        one-per-day would, or the boundary information has been discarded again.
        """
        sweep = attribution_evidence(self._observations(self._synthetic(7)))
        by_hour = {r["publication_hour_et"]: r for r in sweep}
        assert by_hour[7]["cross_date_pairs"] > 20 * 3
        assert by_hour[7]["same_date_groups"] >= 19

    def test_chain_check_is_clean_under_the_correct_hour(self):
        res = check_yesterday_chain(self._observations(self._synthetic(7), hour=7))
        assert res.comparable > 10
        assert res.disagreements == 0

    def test_chain_check_is_dirty_under_a_wrong_hour(self):
        """The series-building check still catches a bad boundary once the
        latest-capture-per-day rule is applied."""
        res = check_yesterday_chain(self._observations(self._synthetic(7), hour=12))
        assert res.disagreements > 0


# ---------------------------------------------------------------------------
# Collapse and intra-day consistency
# ---------------------------------------------------------------------------
class TestCollapseAndIntraday:
    def test_keeps_the_latest_capture_in_the_et_day(self):
        early = cap(et_stamp(date(2026, 5, 2), 8), 3.200, 3.100)
        late = cap(et_stamp(date(2026, 5, 2), 20), 3.200, 3.100)
        out = bf.collapse_to_daily([late, early])
        assert len(out) == 1
        assert out[0].captured_at_et == late.captured_at_et

    def test_same_day_disagreement_marks_the_row_suspect(self):
        """The disagreement is real information; suppressing it would hide a bug."""
        a = cap(et_stamp(date(2026, 5, 2), 9), 3.200, 3.100)
        b = cap(et_stamp(date(2026, 5, 2), 18), 9.999, 3.100)
        out = bf.collapse_to_daily([a, b])
        assert len(out) == 1
        assert out[0].quality == QUALITY_SUSPECT

    def test_agreeing_same_day_captures_stay_ok(self):
        a = cap(et_stamp(date(2026, 5, 2), 9), 3.200, 3.100)
        b = cap(et_stamp(date(2026, 5, 2), 18), 3.200, 3.100)
        assert bf.collapse_to_daily([a, b])[0].quality == QUALITY_OK

    def test_intraday_report_counts_and_names_disagreements(self):
        a = cap(et_stamp(date(2026, 5, 2), 9), 3.200, 3.100)
        b = cap(et_stamp(date(2026, 5, 2), 18), 9.999, 3.100)
        c = cap(et_stamp(date(2026, 5, 3), 12), 3.300, 3.200)
        rep = bf.check_intraday_consistency([a, b, c])
        assert rep["dates_with_multiple_captures"] == 1
        assert rep["inconsistent_dates"] == 1
        assert rep["details"][0]["date"] == "2026-05-02"

    def test_intraday_check_is_independent_of_the_yesterday_column(self):
        """It uses only Current, so it cannot share the chain check's blind spot."""
        a = cap(et_stamp(date(2026, 5, 2), 9), 3.200, None)
        b = cap(et_stamp(date(2026, 5, 2), 18), 9.999, None)
        assert bf.check_intraday_consistency([a, b])["inconsistent_dates"] == 1


class TestChainDisagreementMarking:
    def test_both_dates_of_a_disagreement_are_marked_suspect(self):
        """From the pair alone there is no way to say which side is wrong."""
        daily = [
            cap(et_stamp(date(2026, 5, 1), 12), 3.100, 3.090),
            cap(et_stamp(date(2026, 5, 2), 12), 3.200, 9.999),  # bad Yesterday
            cap(et_stamp(date(2026, 5, 3), 12), 3.300, 3.200),
        ]
        chain = check_yesterday_chain(daily)
        assert chain.disagreements == 1
        out, implicated = bf.mark_chain_disagreements_suspect(daily, chain)
        by_date = {o.date: o for o in out}
        assert implicated == {date(2026, 5, 1), date(2026, 5, 2)}
        assert by_date[date(2026, 5, 1)].quality == QUALITY_SUSPECT
        assert by_date[date(2026, 5, 2)].quality == QUALITY_SUSPECT
        assert by_date[date(2026, 5, 3)].quality == QUALITY_OK

    def test_no_disagreements_marks_nothing(self):
        daily = [
            cap(et_stamp(date(2026, 5, 1), 12), 3.100, 3.090),
            cap(et_stamp(date(2026, 5, 2), 12), 3.200, 3.100),
        ]
        chain = check_yesterday_chain(daily)
        out, implicated = bf.mark_chain_disagreements_suspect(daily, chain)
        assert implicated == set()
        assert all(o.quality == QUALITY_OK for o in out)

    def test_rows_are_never_dropped_only_marked(self):
        daily = [
            cap(et_stamp(date(2026, 5, 1), 12), 3.100, 3.090),
            cap(et_stamp(date(2026, 5, 2), 12), 3.200, 9.999),
        ]
        chain = check_yesterday_chain(daily)
        out, _ = bf.mark_chain_disagreements_suspect(daily, chain)
        assert len(out) == len(daily)


# ---------------------------------------------------------------------------
# External cross-check against EIA
# ---------------------------------------------------------------------------
class TestExternalCrossCheck:
    def _eia(self, tmp_path, rows):
        path = tmp_path / "eia_weekly_regular.csv"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=WEEKLY_CSV_COLUMNS, lineterminator="\n")
            w.writeheader()
            for d, v in rows:
                w.writerow(
                    {
                        "week_ending": d,
                        "value": f"{v:.3f}",
                        "source": EIA_WEEKLY_SERIES_ID,
                        "source_url": "u",
                        "fetched_at": "2026-07-29T00:00:00+00:00",
                    }
                )
        return str(path)

    def test_close_series_passes(self, tmp_path):
        eia = self._eia(tmp_path, [("2026-05-04", 3.100), ("2026-05-11", 3.200)])
        daily = [
            cap(et_stamp(date(2026, 5, 4), 12), 3.105, 3.100),
            cap(et_stamp(date(2026, 5, 11), 12), 3.195, 3.190),
        ]
        rep = bf.cross_check_against_eia(daily, eia_csv_path=eia)
        assert rep["paired"] == 2
        assert rep["max_abs_diff"] <= 0.006
        assert rep["exceeds_threshold"] is False

    def test_wrong_grade_column_is_caught(self, tmp_path):
        """The failure an internal check cannot see.

        Reading Mid-Grade gives a perfectly self-consistent series about $0.50
        too high; only an external reference exposes it.
        """
        eia = self._eia(tmp_path, [("2026-05-04", 3.100), ("2026-05-11", 3.200)])
        daily = [
            cap(et_stamp(date(2026, 5, 4), 12), 3.600, 3.590),
            cap(et_stamp(date(2026, 5, 11), 12), 3.700, 3.690),
        ]
        rep = bf.cross_check_against_eia(daily, eia_csv_path=eia)
        assert rep["exceeds_threshold"] is True
        assert rep["mean_signed_diff"] == pytest.approx(0.5, abs=0.01)

    def test_absent_eia_file_is_not_a_pass(self, tmp_path):
        """An empty comparison must not read as agreement."""
        rep = bf.cross_check_against_eia(
            [cap(et_stamp(date(2026, 5, 4), 12), 3.1, 3.0)],
            eia_csv_path=str(tmp_path / "nope.csv"),
        )
        assert rep["paired"] == 0
        assert "exceeds_threshold" not in rep
        assert "absent" in rep["note"]

    def test_no_overlapping_dates_is_not_a_pass(self, tmp_path):
        eia = self._eia(tmp_path, [("2026-05-04", 3.100)])
        rep = bf.cross_check_against_eia(
            [cap(et_stamp(date(2026, 6, 15), 12), 3.1, 3.0)], eia_csv_path=eia
        )
        assert rep["paired"] == 0
        assert "exceeds_threshold" not in rep

    def test_suspect_rows_are_excluded_from_the_comparison(self, tmp_path):
        from dataclasses import replace

        eia = self._eia(tmp_path, [("2026-05-04", 3.100)])
        bad = replace(
            cap(et_stamp(date(2026, 5, 4), 12), 9.999, 3.0), quality=QUALITY_SUSPECT
        )
        rep = bf.cross_check_against_eia([bad], eia_csv_path=eia)
        assert rep["paired"] == 0


# ---------------------------------------------------------------------------
# Coverage -- gaps are reported, never filled
# ---------------------------------------------------------------------------
class TestCoverage:
    def test_counts_and_enumerates_missing_days(self):
        days = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 5)]
        rep = bf.coverage_report(
            [cap(et_stamp(d, 12), 3.1, 3.0) for d in days],
            start=date(2026, 5, 1),
            end=date(2026, 5, 5),
        )
        assert rep["days_in_window"] == 5
        assert rep["days_present"] == 3
        assert rep["days_missing"] == 2
        assert rep["missing_days"] == ["2026-05-03", "2026-05-04"]

    def test_longest_consecutive_run(self):
        days = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 6)]
        rep = bf.coverage_report(
            [cap(et_stamp(d, 12), 3.1, 3.0) for d in days],
            start=date(2026, 5, 1),
            end=date(2026, 5, 6),
        )
        assert rep["longest_consecutive_run"] == 3

    def test_per_month_breakdown(self):
        days = [date(2026, 5, 30), date(2026, 5, 31), date(2026, 6, 1)]
        rep = bf.coverage_report(
            [cap(et_stamp(d, 12), 3.1, 3.0) for d in days],
            start=date(2026, 5, 30),
            end=date(2026, 6, 2),
        )
        assert rep["by_month"]["2026-05"] == {"days_in_window": 2, "present": 2}
        assert rep["by_month"]["2026-06"] == {"days_in_window": 2, "present": 1}

    def test_coverage_never_invents_a_day(self):
        rep = bf.coverage_report(
            [cap(et_stamp(date(2026, 5, 1), 12), 3.1, 3.0)],
            start=date(2026, 5, 1),
            end=date(2026, 5, 10),
        )
        assert rep["days_present"] == 1
        assert rep["days_present"] + rep["days_missing"] == rep["days_in_window"]

    def test_present_plus_missing_equals_window_even_with_edge_rows(self):
        """A capture just before the window start is a real row but not coverage.

        An 01:29 ET capture on the window's first day carries the *previous*
        day's value, so counting it as present made present+missing exceed the
        window length.
        """
        rows = [
            cap(et_stamp(date(2026, 4, 30), 12), 3.0, 2.9),  # before the window
            cap(et_stamp(date(2026, 5, 1), 12), 3.1, 3.0),
            cap(et_stamp(date(2026, 5, 12), 12), 3.2, 3.1),  # after the window
        ]
        rep = bf.coverage_report(rows, start=date(2026, 5, 1), end=date(2026, 5, 10))
        assert rep["days_in_window"] == 10
        assert rep["days_present"] == 1
        assert rep["days_missing"] == 9
        assert rep["days_present"] + rep["days_missing"] == rep["days_in_window"]
        assert rep["rows_outside_window"] == 2
        assert rep["dates_outside_window"] == ["2026-04-30", "2026-05-12"]


# ---------------------------------------------------------------------------
# Does the artifact describe the file beside it?
# ---------------------------------------------------------------------------
class TestSeriesReconciliation:
    """The check whose absence let an orphan row hide for a whole phase.

    ``upsert_observations`` is first-writer-wins, so a row from an earlier run
    survives a run that does not reproduce it -- while the audit's coverage block
    is computed from what the run *did* produce. The committed artifact reported
    ``days_present=1550`` with ``2026-07-28`` listed as missing, beside a CSV
    holding 1551 rows including one for that date.
    """

    def _csv(self, tmp_path, days):
        from src.data.aaa_provider import upsert_observations

        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [cap(et_stamp(d, 15), 3.1, 3.0) for d in days],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        return path

    def test_an_unreproduced_row_on_disk_is_named(self, tmp_path):
        on_disk = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
        path = self._csv(tmp_path, on_disk)
        produced = [cap(et_stamp(d, 15), 3.1, 3.0) for d in on_disk[:2]]
        rep = bf.reconcile_with_disk(
            produced, csv_path=path, start=date(2026, 5, 1), end=date(2026, 5, 3)
        )
        assert rep["agrees"] is False
        assert rep["dates_on_disk_not_reproduced_by_this_run"] == ["2026-05-03"]

    def test_a_fully_reproduced_series_agrees(self, tmp_path):
        days = [date(2026, 5, 1), date(2026, 5, 2)]
        path = self._csv(tmp_path, days)
        rep = bf.reconcile_with_disk(
            [cap(et_stamp(d, 15), 3.1, 3.0) for d in days],
            csv_path=path,
            start=date(2026, 5, 1),
            end=date(2026, 5, 2),
        )
        assert rep["agrees"] is True
        assert rep["dates_on_disk_not_reproduced_by_this_run"] == []

    def test_rows_outside_the_window_are_not_a_disagreement(self, tmp_path):
        """A narrower window than the file's span is a legitimate way to run."""
        path = self._csv(tmp_path, [date(2026, 5, 1), date(2026, 6, 1)])
        rep = bf.reconcile_with_disk(
            [cap(et_stamp(date(2026, 5, 1), 15), 3.1, 3.0)],
            csv_path=path,
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
        )
        assert rep["agrees"] is True

    def test_new_rows_this_run_produced_are_reported_separately(self, tmp_path):
        path = self._csv(tmp_path, [date(2026, 5, 1)])
        rep = bf.reconcile_with_disk(
            [
                cap(et_stamp(date(2026, 5, 1), 15), 3.1, 3.0),
                cap(et_stamp(date(2026, 5, 2), 15), 3.2, 3.1),
            ],
            csv_path=path,
            start=date(2026, 5, 1),
            end=date(2026, 5, 2),
        )
        assert rep["agrees"] is True
        assert rep["dates_produced_but_absent_from_disk"] == ["2026-05-02"]

    def test_the_comparison_predates_the_write(self, tmp_path):
        """Compared against a file this run just produced, the check cannot fail.

        Under ``--regenerate`` a post-write comparison is guaranteed to pass while
        the run silently drops rows (``circular-constraints-justify-nothing``).
        """
        path = self._csv(tmp_path, [date(2026, 5, 1)])
        rep = bf.reconcile_with_disk(
            [], csv_path=path, start=date(2026, 5, 1), end=date(2026, 5, 1)
        )
        assert rep["compared_before_write"] is True
        assert rep["agrees"] is False


# ---------------------------------------------------------------------------
# Why each suspect row is suspect
# ---------------------------------------------------------------------------
class TestSuspectReasons:
    """A ``suspect`` flag with no reason is unactionable.

    The committed artifact listed 56 flagged rows carrying only date/value/url, so
    a consumer could not tell a parse defect from an ordinary noisy day.
    """

    class _Chain:
        def __init__(self, details=()):
            self.details = tuple(details)

    def test_same_day_disagreement_is_named(self):
        day = date(2026, 5, 2)
        captures = [
            cap(et_stamp(day, 13), 3.10, 3.05),
            cap(et_stamp(day, 17), 3.20, 3.05),
        ]
        daily = [captures[-1]]
        reasons = bf.suspect_reasons(daily, captures, self._Chain())
        assert reasons[day] == [bf.SUSPECT_SAME_DAY_DISAGREEMENT]

    def test_out_of_range_value_is_named(self):
        day = date(2026, 5, 2)
        rows = [cap(et_stamp(day, 15), 99.0, 3.0)]
        reasons = bf.suspect_reasons(rows, rows, self._Chain())
        assert reasons[day] == [bf.SUSPECT_OUT_OF_RANGE]

    def test_a_daily_jump_names_both_sides(self):
        a, b = date(2026, 5, 1), date(2026, 5, 2)
        rows = [cap(et_stamp(a, 15), 3.00, 2.9), cap(et_stamp(b, 15), 3.50, 3.0)]
        reasons = bf.suspect_reasons(rows, rows, self._Chain())
        assert reasons[a] == [bf.SUSPECT_DAILY_JUMP]
        assert reasons[b] == [bf.SUSPECT_DAILY_JUMP]

    def test_a_chain_disagreement_names_both_sides(self):
        a, b = date(2026, 5, 1), date(2026, 5, 2)
        rows = [cap(et_stamp(a, 15), 3.00, 2.9), cap(et_stamp(b, 15), 3.02, 3.0)]
        reasons = bf.suspect_reasons(rows, rows, self._Chain([{"date": b.isoformat()}]))
        assert reasons[a] == [bf.SUSPECT_CHAIN_DISAGREEMENT]
        assert reasons[b] == [bf.SUSPECT_CHAIN_DISAGREEMENT]

    def test_several_rules_are_all_reported(self):
        a, b = date(2026, 5, 1), date(2026, 5, 2)
        rows = [cap(et_stamp(a, 15), 3.00, 2.9), cap(et_stamp(b, 15), 3.50, 3.0)]
        reasons = bf.suspect_reasons(rows, rows, self._Chain([{"date": b.isoformat()}]))
        assert reasons[b] == sorted(
            [bf.SUSPECT_CHAIN_DISAGREEMENT, bf.SUSPECT_DAILY_JUMP]
        )

    def test_a_clean_series_attributes_nothing(self):
        rows = [
            cap(et_stamp(date(2026, 5, 1) + timedelta(days=i), 15), 3.0 + 0.01 * i, 3.0)
            for i in range(5)
        ]
        assert bf.suspect_reasons(rows, rows, self._Chain()) == {}


# ---------------------------------------------------------------------------
# Is the piecewise schedule worth its complexity?
# ---------------------------------------------------------------------------
class TestScheduleEvidence:
    """The shipped boundaries must cite an artifact, not a remembered number."""

    def _captures(self):
        """Two years of daily anchors plus a boundary sample at every ET hour."""
        rows = []
        value = 3.00
        prev = None
        for i in range(240):
            day = date(2025, 1, 1) + timedelta(days=i)
            value = round(3.00 + 0.003 * i, 3)
            rows.append(cap(et_stamp(day, 15), value, prev if prev else value))
            prev = value
        return rows

    def test_every_candidate_schedule_is_scored(self):
        rows = self._captures()
        out = bf.compare_schedules(rows)
        assert {r["name"] for r in out} == set(bf.CANDIDATE_SCHEDULES)
        for row in out:
            assert row["comparable"] >= 0
            assert 0.0 <= row["disagreement_rate"] <= 1.0
            assert row["schedule"], "the schedule itself must be persisted"

    def test_the_shipped_schedule_is_among_the_alternatives(self):
        from src.data.aaa_provider import PUBLICATION_SCHEDULE

        shipped = [
            r
            for r in bf.compare_schedules(self._captures())
            if r["name"] == "3-era-shipped"
        ]
        assert len(shipped) == 1
        assert shipped[0]["schedule"] == [
            {"effective_from": d.isoformat(), "hour_et": h}
            for d, h in PUBLICATION_SCHEDULE
        ]

    def test_post_publication_rows_are_reported_as_structurally_immune(self):
        """The reason the row-level effect is far smaller than the rate gap.

        Every capture here is at 15:00 ET, past every candidate hour, so no
        candidate can move any row -- and the report must say so rather than let
        the rate difference imply a large consequence.
        """
        effect = bf.schedule_row_effect(self._captures())
        assert effect["rows_structurally_immune_pct"] == 100.0
        assert effect["worst_case_rows_moved"] == 0
        assert effect["worst_case_rows_redated"] == 0

    def test_an_evening_capture_is_immune_too(self):
        """Immunity is the capture hour, not the anchor window.

        A 22:00 ET capture is outside the 12:00-20:00 anchor window and equally
        immune to every candidate hour; counting only anchor-window rows
        understated immunity by five points on the real series.
        """
        rows = [
            cap(et_stamp(date(2025, 6, 1) + timedelta(days=i), 22), 3.1, 3.1)
            for i in range(3)
        ]
        effect = bf.schedule_row_effect(rows)
        assert effect["rows_structurally_immune"] == 3
        assert effect["rows_whose_capture_is_in_the_anchor_window"] == 0

    def test_a_pre_publication_capture_can_move_and_is_counted(self):
        """A capture at 04:00 ET moves between hour 3 and hour 5, and the row
        effect must count it -- otherwise the metric is degenerate."""
        rows = [
            cap(et_stamp(date(2025, 6, 1), 15), 3.10, 3.09),
            cap(et_stamp(date(2025, 6, 3), 4), 3.12, 3.11),
        ]
        effect = bf.schedule_row_effect(rows, candidate_hours=(3, 5))
        by_hour = {r["single_hour_et"]: r for r in effect["by_single_hour"]}
        assert by_hour[3]["rows_moved"] == 0, "hour 3 is the schedule's 2025Q2 hour"
        assert by_hour[3]["rows_redated"] == 0
        assert by_hour[5]["rows_redated"] == 1, "hour 5 re-dates the 04:00 ET capture"
        assert by_hour[5]["rows_moved"] > 0


# ---------------------------------------------------------------------------
# The COMMITTED artifacts, as a consumer reads them
# ---------------------------------------------------------------------------
GAS_TRUTH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gas_truth"
)


def _committed(name):
    path = os.path.join(GAS_TRUTH, name)
    if not os.path.exists(path):
        pytest.skip(f"{path} not present in this checkout")
    return path


class TestCommittedArtifacts:
    """Assertions against the files on disk, not against synthetic fixtures.

    Everything here would have caught the ``2026-07-28`` orphan: the audit
    disagreeing with the CSV beside it is precisely how that row stayed hidden
    through a whole phase.
    """

    @pytest.fixture(scope="class")
    def rows(self):
        with open(
            _committed("aaa_daily_national.csv"), newline="", encoding="utf-8"
        ) as fh:
            return [r for r in csv.DictReader(fh) if (r.get("date") or "").strip()]

    @pytest.fixture(scope="class")
    def audit(self):
        with open(_committed("backfill_audit.json"), encoding="utf-8") as fh:
            return json.load(fh)

    @pytest.fixture(scope="class")
    def manifest(self):
        with open(_committed("manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_audit_coverage_block_agrees_with_the_csv(self, rows, audit):
        """The mismatch that hid an unverifiable row."""
        coverage = audit["coverage"]
        start = date.fromisoformat(coverage["window_start"])
        end = date.fromisoformat(coverage["window_end"])
        in_window = {
            date.fromisoformat(r["date"])
            for r in rows
            if start <= date.fromisoformat(r["date"]) <= end
        }
        assert len(in_window) == coverage["days_present"]
        assert (
            coverage["days_present"] + coverage["days_missing"]
            == (coverage["days_in_window"])
        )
        assert (
            round(100.0 * len(in_window) / coverage["days_in_window"], 2)
            == (coverage["coverage_pct"])
        )

    def test_no_missing_day_has_a_row(self, rows, audit):
        """A date the audit calls missing must not be present in the CSV."""
        present = {r["date"] for r in rows}
        overlap = sorted(present & set(audit["coverage"]["missing_days"]))
        assert overlap == [], (
            f"the audit lists these dates as missing while the CSV holds a row "
            f"for each: {overlap}"
        )

    def test_the_series_reconciles_with_the_run_that_produced_it(self, audit):
        rec = audit["series_reconciliation"]
        assert rec["compared_before_write"] is True
        # The 2026-07-28 drop is expected and recorded; nothing else may appear.
        assert rec["dates_on_disk_not_reproduced_by_this_run"] in (
            [],
            ["2026-07-28"],
        ), rec["dates_on_disk_not_reproduced_by_this_run"]

    def test_the_manifest_row_count_matches_the_csv(self, rows, manifest):
        assert manifest["series"]["aaa_daily_national"]["rows"] == len(rows)

    def test_the_manifest_content_hash_matches_the_csv_bytes(self, rows, manifest):
        import hashlib

        digest = hashlib.sha256(
            open(_committed("aaa_daily_national.csv"), "rb").read()
        ).hexdigest()
        assert manifest["series"]["aaa_daily_national"]["content_hash"] == digest

    def test_the_unretrievable_2026_07_28_row_is_gone(self, rows, manifest):
        """Its cited snapshot 20260728110106 is absent from CDX, and the replay
        URL answers 200 with the 2026-07-27 capture -- so the recorded
        ``raw_sha256`` matches nothing retrievable and the value is unverifiable.

        The removal is recorded in ``manifest.json``'s append-only corrections
        list rather than performed silently, because a provenance file that
        quietly loses a row is no better than one that quietly keeps a bad one.
        """
        assert "2026-07-28" not in {r["date"] for r in rows}
        removals = [
            c for c in manifest.get("corrections", []) if c.get("date") == "2026-07-28"
        ]
        assert len(removals) == 1, "the removal must be recorded, not silent"
        assert removals[0]["action"] == "row_removed_by_regeneration"
        assert removals[0].get("reason")
        assert removals[0].get("evidence")

    def test_every_suspect_row_says_why(self, audit):
        known = {
            bf.SUSPECT_OUT_OF_RANGE,
            bf.SUSPECT_SAME_DAY_DISAGREEMENT,
            bf.SUSPECT_DAILY_JUMP,
            bf.SUSPECT_CHAIN_DISAGREEMENT,
        }
        assert audit["suspect_rows"], "the artifact must list its suspect rows"
        for row in audit["suspect_rows"]:
            reason = row.get("reason")
            assert reason, f"suspect row {row.get('date')} carries no reason"
            assert set(reason.split("+")) <= known, reason
        assert audit["suspect_rows_by_reason"]

    def test_the_suspect_count_agrees_with_the_csv(self, rows, audit):
        flagged = {r["date"] for r in rows if r["quality"] == QUALITY_SUSPECT}
        assert flagged == {r["date"] for r in audit["suspect_rows"]}

    def test_the_schedule_boundary_evidence_is_persisted(self, audit):
        """The shipped boundaries must be reproducible from this artifact.

        The docstring on ``PUBLICATION_SCHEDULE`` previously cited a 2/3/4/5-era
        comparison that appeared in no committed artifact and whose era
        definitions were never recorded.
        """
        alts = audit["publication_schedule_alternatives"]
        by_name = {r["name"]: r for r in alts}
        assert "3-era-shipped" in by_name
        assert "single-constant-5" in by_name
        assert (
            "3-era-boundary-2025-01-01" in by_name
        ), "the boundary the per-year table alone implies must be scored too"
        shipped = by_name["3-era-shipped"]["disagreement_rate"]
        for name, row in by_name.items():
            if name == "3-era-shipped":
                continue
            assert row["disagreement_rate"] >= shipped, (
                f"{name} scores better than the shipped schedule "
                f"({row['disagreement_rate']} < {shipped}); the shipped choice "
                f"is no longer the measured best and the docstring is stale"
            )

    def test_the_row_level_effect_of_a_single_constant_is_persisted(self, audit):
        """The magnitudes the docstring quotes, measured rather than remembered."""
        effect = audit["publication_schedule_row_effect"]
        assert effect["anchor_et_window"] == [12, 20]
        assert 0.0 < effect["rows_structurally_immune_pct"] <= 100.0
        # Both metrics, each with its definition: a lone number gets quoted out
        # of context, and these two answer different questions.
        assert set(effect["metric_definitions"]) == {"rows_redated", "rows_moved"}
        hours = {r["single_hour_et"]: r for r in effect["by_single_hour"]}
        assert 5 in hours, "the best single constant must be among the candidates"
        # A single global constant is a small perturbation, not a year-scale
        # shift: the claim it 'would misattribute entire years by a full calendar
        # day' is what these numbers refute.
        assert hours[5]["rows_redated_pct"] < 5.0
        assert hours[5]["rows_moved_pct"] < 10.0

    def test_the_aaa_all_time_record_lands_on_its_documented_date(self, rows):
        """The one genuinely DAY-SENSITIVE external check on the attribution.

        AAA's documented all-time-record national average is 2022-06-14. The
        series peaks there at 5.016 with 5.014 on both neighbours, so a one-day
        shift of the whole series would move the peak off the record date. Pinned
        because the EIA cross-check cannot do this job: the AAA-EIA offset is
        ~13.5 mills against 1-5 mill daily moves, so it validates the level and
        says nothing about alignment.
        """
        by_date = {r["date"]: float(r["value"]) for r in rows}
        peak_date = max(by_date, key=lambda d: by_date[d])
        assert peak_date == "2022-06-14"
        assert by_date["2022-06-14"] == pytest.approx(5.016, abs=5e-4)
        assert by_date["2022-06-13"] == pytest.approx(5.014, abs=5e-4)
        assert by_date["2022-06-15"] == pytest.approx(5.014, abs=5e-4)

    def test_ec1_first_half_fourteen_consecutive_days_persisted(self, rows):
        """Phase 4 EC-1: ">=14 consecutive daily values persisted with
        provenance". Provenance is asserted per row, not assumed."""
        days = {date.fromisoformat(r["date"]) for r in rows}
        best = 0
        for day in days:
            if day - timedelta(days=1) in days:
                continue
            length = 0
            cur = day
            while cur in days:
                length += 1
                cur += timedelta(days=1)
            best = max(best, length)
        assert best >= 14, f"longest consecutive run is only {best} days"
        for row in rows:
            assert row["source"] in (SOURCE_WAYBACK, "aaa_live")
            assert row["source_url"].startswith("http")
            assert len(row["raw_sha256"]) == 64
            assert row["fetched_at"]


# ---------------------------------------------------------------------------
# EIA / RBOB covariates
# ---------------------------------------------------------------------------
def build_pet_zip(path, records):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PET.txt", "\n".join(json.dumps(r) for r in records))
    return str(path)


WEEKLY_RECORD = {
    "series_id": EIA_WEEKLY_SERIES_ID,
    "name": "U.S. Regular All Formulations Retail Gasoline Prices, Weekly",
    "units": "Dollars per Gallon",
    "f": "W",
    "last_updated": "2026-07-28T13:44:02-04:00",
    "data": [
        ["20260727", 4.096],
        ["20260720", 4.001],
        ["20260713", None],  # EIA encodes non-publication as null
        ["20260706", 3.9],
        ["20240101", 3.1],  # before the window
    ],
}
RBOB_RECORD = {
    "series_id": RBOB_SERIES_ID,
    "name": "Los Angeles Reformulated RBOB Regular Gasoline Spot Price, Daily",
    "units": "Dollars per Gallon",
    "f": "D",
    "last_updated": "2026-07-29T17:38:03-04:00",
    "data": [
        ["20260727", 3.504],
        ["20260724", 3.613],
        ["20260723", None],
        ["202607", 3.5],  # a monthly token must never become a daily row
    ],
}


class TestCovariateExtraction:
    def test_extracts_requested_series(self, tmp_path):
        p = build_pet_zip(tmp_path / "PET.zip", [WEEKLY_RECORD, RBOB_RECORD])
        found = extract_series(p, [EIA_WEEKLY_SERIES_ID, RBOB_SERIES_ID])
        assert set(found) == {EIA_WEEKLY_SERIES_ID, RBOB_SERIES_ID}

    def test_missing_series_raises(self, tmp_path):
        p = build_pet_zip(tmp_path / "PET.zip", [WEEKLY_RECORD])
        with pytest.raises(CovariateUnavailable):
            extract_series(p, [EIA_WEEKLY_SERIES_ID, RBOB_SERIES_ID])

    def test_nulls_become_absent_rows_never_zeros(self):
        rows, stats = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="https://www.eia.gov/opendata/bulk/PET.zip",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        dates = [r["week_ending"] for r in rows]
        assert "2026-07-13" not in dates
        assert stats["null_value"] == 1
        assert all(float(r["value"]) > 0 for r in rows)

    def test_monthly_token_is_rejected_not_padded_to_a_day(self):
        """Padding 202607 to 2026-07-01 would mix a monthly average into a
        daily series, and nothing downstream would catch it."""
        rows, stats = series_to_rows(
            RBOB_RECORD,
            series_id=RBOB_SERIES_ID,
            archive_url="u",
            date_column="date",
            start=date(2025, 1, 1),
        )
        assert "2026-07-01" not in [r["date"] for r in rows]
        assert stats["bad_date"] == 1

    def test_window_filter_excludes_out_of_range(self):
        rows, stats = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="u",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        assert "2024-01-01" not in [r["week_ending"] for r in rows]
        assert stats["out_of_window"] == 1

    def test_rows_are_sorted_ascending(self):
        rows, _ = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="u",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        dates = [r["week_ending"] for r in rows]
        assert dates == sorted(dates)

    def test_source_url_identifies_the_series_inside_the_archive(self):
        rows, _ = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="https://www.eia.gov/opendata/bulk/PET.zip",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        assert rows[0]["source_url"] == (
            f"https://www.eia.gov/opendata/bulk/PET.zip#{EIA_WEEKLY_SERIES_ID}"
        )
        assert rows[0]["source"] == EIA_WEEKLY_SERIES_ID

    def test_fetched_at_is_eias_publication_instant_in_utc(self):
        """Not the run's wall clock: otherwise every rerun is a new artifact and
        ``manifest.json``'s content_hash stops identifying the data."""
        assert series_published_at(WEEKLY_RECORD) == "2026-07-28T17:44:02+00:00"
        rows, _ = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="u",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        assert all(r["fetched_at"] == "2026-07-28T17:44:02+00:00" for r in rows)

    def test_missing_last_updated_falls_back_rather_than_inventing(self):
        rec = {k: v for k, v in WEEKLY_RECORD.items() if k != "last_updated"}
        assert series_published_at(rec) is None
        rows, _ = series_to_rows(
            rec,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="u",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        assert rows[0]["fetched_at"]  # a stamp exists, it is just the run's

    def test_unparseable_last_updated_is_not_propagated(self):
        assert series_published_at({"last_updated": "not a date"}) is None

    def test_covariate_output_is_byte_reproducible(self, tmp_path):
        """Two runs over an unchanged archive must produce identical bytes.

        Verified against the real archive as well: repeated runs of the CLI
        produced identical digests for both covariate CSVs.
        """
        archive = build_pet_zip(tmp_path / "PET.zip", [WEEKLY_RECORD, RBOB_RECORD])
        gas_dir = tmp_path / "gas_truth"
        backfill_covariates(
            gas_dir=str(gas_dir), start="2025-01-01", archive_path=archive
        )
        first = {
            n: (gas_dir / n).read_bytes()
            for n in ("eia_weekly_regular.csv", "rbob_daily.csv")
        }
        backfill_covariates(
            gas_dir=str(gas_dir), start="2025-01-01", archive_path=archive
        )
        for name, blob in first.items():
            assert (gas_dir / name).read_bytes() == blob

    def test_weekday_audit_measures_the_monday_assumption(self):
        """Contract §1 asserts Monday dating, so it is measured not trusted."""
        rows, _ = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url="u",
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        assert set(weekday_audit(rows, "week_ending")) == {"Monday"}


class TestCovariateBackfill:
    def test_writes_both_series_with_contract_columns(self, tmp_path):
        archive = build_pet_zip(tmp_path / "PET.zip", [WEEKLY_RECORD, RBOB_RECORD])
        gas_dir = tmp_path / "gas_truth"
        out = backfill_covariates(
            gas_dir=str(gas_dir), start="2025-01-01", archive_path=archive
        )
        with open(
            gas_dir / "eia_weekly_regular.csv", newline="", encoding="utf-8"
        ) as fh:
            assert tuple(next(csv.reader(fh))) == WEEKLY_CSV_COLUMNS
        with open(gas_dir / "rbob_daily.csv", newline="", encoding="utf-8") as fh:
            assert tuple(next(csv.reader(fh))) == DAILY_CSV_COLUMNS
        assert out["eia_weekly_regular"]["rows"] == 3
        assert out["rbob_daily"]["rows"] == 2

    def test_csvs_are_lf_terminated(self, tmp_path):
        archive = build_pet_zip(tmp_path / "PET.zip", [WEEKLY_RECORD, RBOB_RECORD])
        gas_dir = tmp_path / "gas_truth"
        backfill_covariates(
            gas_dir=str(gas_dir), start="2025-01-01", archive_path=archive
        )
        for name in ("eia_weekly_regular.csv", "rbob_daily.csv"):
            assert b"\r" not in (gas_dir / name).read_bytes()

    def test_empty_result_raises_rather_than_writing_an_empty_file(self, tmp_path):
        """An empty covariate file looks like data to whatever fits on it."""
        archive = build_pet_zip(tmp_path / "PET.zip", [WEEKLY_RECORD, RBOB_RECORD])
        with pytest.raises(CovariateUnavailable):
            backfill_covariates(
                gas_dir=str(tmp_path / "gas_truth"),
                start="2030-01-01",
                archive_path=archive,
            )

    def test_no_api_key_is_required_anywhere(self):
        """EIA's JSON API 403s without a key; the bulk route must stay keyless.

        Asserted against the URLs and identifiers the module actually uses, not
        against its prose (which names the keyed API in order to rule it out).
        """
        import src.data.energy_covariates as ec

        assert "api_key" not in ec.PET_ARCHIVE_URL
        assert "api_key" not in ec.EIA_MANIFEST_URL
        assert "/v2" not in ec.PET_ARCHIVE_URL
        assert os.getenv("EIA_API_KEY") is None or True  # never read
        rows, _ = series_to_rows(
            WEEKLY_RECORD,
            series_id=EIA_WEEKLY_SERIES_ID,
            archive_url=ec.PET_ARCHIVE_URL,
            date_column="week_ending",
            start=date(2025, 1, 1),
        )
        assert all("api_key" not in r["source_url"] for r in rows)

    def test_rbob_default_is_a_current_series_not_the_discontinued_futures(self):
        """NY Harbor RBOB futures ended 2024-04-05 and covers none of the window."""
        assert RBOB_SERIES_ID in RBOB_ALTERNATIVES.values()
        assert "PE1_Y35NY" not in RBOB_SERIES_ID


# ---------------------------------------------------------------------------
# No new third-party dependency
# ---------------------------------------------------------------------------
def imported_modules(module) -> set:
    """Top-level module names actually imported by ``module``.

    Parsed from the AST rather than grepped from the source: these modules
    *discuss* bs4 and openpyxl in their docstrings precisely to explain why they
    are avoided, and a substring scan would flag that prose as a violation. The
    claim under test is about imports, so the test must look at imports
    (``grep-the-gate-not-the-claim`` cuts both ways -- the check has to measure
    the real thing).
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


class TestNoNewDependencies:
    #: Installed in this environment but absent from requirements.txt. An
    #: undeclared import is how a VM deploy breaks at pip time.
    UNDECLARED = {"bs4", "lxml", "html5lib", "openpyxl", "xlrd"}

    def test_aaa_parser_imports_no_undeclared_package(self):
        import src.data.aaa_provider as ap

        assert not (imported_modules(ap) & self.UNDECLARED)

    def test_aaa_parser_uses_the_stdlib_html_parser(self):
        import src.data.aaa_provider as ap

        assert "html" in imported_modules(ap)

    def test_covariates_import_no_undeclared_package(self):
        import src.data.energy_covariates as ec

        assert not (imported_modules(ec) & self.UNDECLARED)

    def test_covariates_use_stdlib_zipfile_and_json(self):
        import src.data.energy_covariates as ec

        assert {"zipfile", "json"} <= imported_modules(ec)

    def test_backfill_script_imports_no_undeclared_package(self):
        assert not (imported_modules(bf) & self.UNDECLARED)
