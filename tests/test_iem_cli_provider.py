"""Offline tests for the CLI ground-truth provider (PRD FR-1.3 / FR-1.6).

Every test but the last runs against ``tests/fixtures/iem_cli_knyc_sample.json``
through a fake HTTP session, so the suite is deterministic and does not touch
the wire. The final test makes one live call and skips cleanly when the archive
is unreachable (or when ``SKIP_NETWORK_TESTS`` is set).

The behaviours these lock down are the ones that would silently corrupt
settlement truth:

* the archive's ``year``-only filter, and its habit of returning the **2019**
  archive with HTTP 200 when ``year`` is not honoured;
* an unknown station returning HTTP 200 with zero rows;
* rows whose ``high`` is absent or the literal ``"M"``;
* caching, so a re-run cannot quietly re-fetch different values.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.data.iem_cli_provider import (  # noqa: E402
    CORE_STATIONS,
    EXPANSION_STATIONS,
    ROW_FIELDS,
    SOURCE_IEM,
    SOURCE_NWS,
    STATIONS,
    IEMCLIError,
    IEMCLIProvider,
    date_range,
    get_station,
    normalize_date,
    normalize_row,
    parse_cli_text,
    station_for_series,
)

FIXTURE_PATH = os.path.join(REPO_ROOT, "tests", "fixtures", "iem_cli_knyc_sample.json")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(
        self, payload, status_code=200, url="https://fake/cli.py?station=KNYC&year=2026"
    ):
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Records every request and replays a queued list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url} params={params}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture()
def fixture_payload():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def provider_factory(tmp_path):
    def _make(responses, **kwargs):
        session = FakeSession(responses)
        provider = IEMCLIProvider(
            cache_dir=str(tmp_path / "cache"),
            session=session,
            request_pause=0.0,
            **kwargs,
        )
        return provider, session

    return _make


# ---------------------------------------------------------------------------
# Station registry
# ---------------------------------------------------------------------------
def test_core_four_stations_map_to_kalshi_series():
    """PRD A4: the four settlement stations and their KXHIGH series."""
    assert set(STATIONS) == {"KNYC", "KMDW", "KLAX", "KMIA"}
    assert STATIONS["KNYC"].series_ticker == "KXHIGHNY"
    assert STATIONS["KMDW"].series_ticker == "KXHIGHCHI"
    assert STATIONS["KLAX"].series_ticker == "KXHIGHLAX"
    assert STATIONS["KMIA"].series_ticker == "KXHIGHMIA"
    assert all(spec.verified for spec in CORE_STATIONS.values())
    assert station_for_series("KXHIGHCHI") == "KMDW"
    assert station_for_series("KXNOPE") is None


def test_chicago_settles_on_midway_not_ohare():
    """Kalshi's Chicago high settles at Midway; ORD is the wrong station."""
    assert STATIONS["KMDW"].station == "KMDW"
    assert STATIONS["KMDW"].cli_location == "MDW"
    assert "KORD" not in STATIONS


def test_unverified_expansion_station_is_refused_by_default():
    """An unprobed expansion city must not be usable by accident."""
    assert EXPANSION_STATIONS
    name = next(iter(EXPANSION_STATIONS))
    assert not EXPANSION_STATIONS[name].verified
    with pytest.raises(IEMCLIError, match="unverified"):
        get_station(name)
    assert get_station(name, allow_unverified=True).city


def test_unknown_station_raises():
    with pytest.raises(IEMCLIError, match="unknown station"):
        get_station("KZZZ")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_normalize_date_accepts_common_shapes():
    from datetime import date, datetime

    assert normalize_date("2026-07-24") == "2026-07-24"
    assert normalize_date(date(2026, 7, 24)) == "2026-07-24"
    assert normalize_date(datetime(2026, 7, 24, 13, 5)) == "2026-07-24"
    assert normalize_date("2026-07-24T12:00:00Z") == "2026-07-24"
    with pytest.raises(IEMCLIError):
        normalize_date("24/07/2026")


def test_date_range_is_inclusive_and_ordered():
    assert date_range("2026-07-22", "2026-07-24") == [
        "2026-07-22",
        "2026-07-23",
        "2026-07-24",
    ]
    with pytest.raises(IEMCLIError):
        date_range("2026-07-24", "2026-07-22")


def test_normalize_row_maps_the_stable_column_order(fixture_payload):
    raw = next(r for r in fixture_payload["results"] if r["valid"] == "2026-07-24")
    row = normalize_row(raw, source_url="https://x", fetched_at="2026-07-25T00:00:00Z")
    assert list(row) == list(ROW_FIELDS)
    assert row["station"] == "KNYC"
    assert row["date"] == "2026-07-24"
    assert row["high"] == 83
    assert row["source"] == SOURCE_IEM
    assert row["source_url"] == "https://x"


@pytest.mark.parametrize("token", [None, "M", "MM", "", "N/A"])
def test_missing_high_tokens_normalize_to_none(token):
    row = normalize_row(
        {"station": "KNYC", "valid": "2026-04-11", "high": token},
        source_url="u",
        fetched_at="t",
    )
    assert row["high"] is None


# ---------------------------------------------------------------------------
# fetch_year / parsing / caching
# ---------------------------------------------------------------------------
def test_fetch_year_parses_and_always_sends_year(provider_factory, fixture_payload):
    provider, session = provider_factory([FakeResponse(fixture_payload)])
    rows = provider.fetch_year("KNYC", 2026)

    assert len(rows) == 7
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    # `year` is the ONLY filter the archive honours; `date` is ignored upstream.
    assert session.calls[0]["params"] == {"station": "KNYC", "year": 2026}
    assert "date" not in session.calls[0]["params"]


def test_fetch_daily_high_returns_the_cli_maximum(provider_factory, fixture_payload):
    provider, _ = provider_factory([FakeResponse(fixture_payload)])
    assert provider.fetch_daily_high("KNYC", "2026-07-24") == 83
    assert provider.fetch_daily_high("KNYC", "2026-07-22") == 85


def test_second_call_is_served_from_cache(provider_factory, fixture_payload):
    """One HTTP call, then the on-disk cache. FakeSession asserts on extras."""
    provider, session = provider_factory([FakeResponse(fixture_payload)])
    assert provider.fetch_daily_high("KNYC", "2026-07-24") == 83
    assert provider.fetch_daily_high("KNYC", "2026-07-23") == 79
    assert len(session.calls) == 1

    # A brand-new provider over the same cache dir must not re-fetch either.
    fresh = IEMCLIProvider(
        cache_dir=provider.cache_dir, session=FakeSession([]), request_pause=0.0
    )
    assert fresh.fetch_daily_high("KNYC", "2026-07-24") == 83
    cache_file = os.path.join(provider.cache_dir, "iem_cli_KNYC_2026.json")
    assert os.path.exists(cache_file)
    with open(cache_file, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    assert blob["_meta"]["station"] == "KNYC"
    assert blob["_meta"]["year"] == 2026
    assert blob["_meta"]["fetched_at"]
    assert blob["_meta"]["source_url"]


def test_in_memory_year_cache_expires_like_the_disk_cache(
    provider_factory, fixture_payload
):
    """FINDING A (the half that made the miss permanent).

    The process-lifetime memo had no expiry, so a long-lived provider -- and
    ``weather_settlement`` holds a module-level singleton -- answered every
    later lookup from the first snapshot it ever took, whatever the on-disk TTL
    said. Memory and disk now share one freshness rule.
    """
    provider, session = provider_factory(
        [FakeResponse(fixture_payload), FakeResponse(fixture_payload)],
        current_year_ttl=0,
    )
    provider.fetch_year("KNYC", 2026)
    provider.fetch_year("KNYC", 2026)
    assert len(session.calls) == 2


def test_a_closed_year_is_still_cached_forever(provider_factory):
    """The caching benefit that must survive the fix: a settled past year.

    A closed year cannot gain rows, so it is served from the memo indefinitely
    even with the current-year TTL set to zero -- one archive call, ever.
    """
    payload_2025 = {
        "results": [
            {
                "station": "KNYC",
                "valid": "2025-12-31",
                "high": 33,
                "low": 25,
                "product": "p",
            }
        ],
        "generated_at": "x",
    }
    provider, session = provider_factory(
        [FakeResponse(payload_2025)], current_year_ttl=0
    )
    assert provider.fetch_daily_high("KNYC", "2025-12-31") == 33
    assert provider.fetch_daily_high("KNYC", "2025-12-31") == 33
    assert provider.fetch_year("KNYC", 2025)
    assert len(session.calls) == 1


def test_unpublished_truth_is_rechecked_without_a_restart(provider_factory, tmp_path):
    """FINDING A, end to end: the verifier's t0/t1/t2 sequence.

    A weather position expires at local midnight; the settlement day's CLI
    product is not issued until ~06:26 local. The first truth lookup therefore
    misses. Before the fix the year snapshot was memoized with no TTL and no
    invalidation, so *every* later retry answered ``None`` from memory and the
    position could only settle after a process restart -- the 15-minute retry
    throttle in ``weather_settlement`` re-entered a function that never asked
    the archive again.

    t0 product unissued  -> None   (1 live call)
    t1 product issued    -> 88     (2 live calls, SAME provider, no restart)
    t2 fresh provider    -> 88     (still 0 further calls: t1 wrote the cache)
    """
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)

    def payload(rows):
        return {"results": rows, "generated_at": "x"}

    row_before = {
        "station": "KNYC",
        "valid": yesterday.isoformat(),
        "high": 86,
        "low": 70,
        "product": "before",
    }
    row_today = {
        "station": "KNYC",
        "valid": today.isoformat(),
        "high": 88,
        "low": 72,
        "product": "after",
    }

    provider, session = provider_factory(
        [
            FakeResponse(payload([row_before])),
            FakeResponse(payload([row_before, row_today])),
        ]
    )

    # t0 -- the CLI product for today has not been issued yet.
    assert provider.fetch_daily_high("KNYC", today.isoformat()) is None
    assert len(session.calls) == 1

    # t1 -- the product is issued. Same long-lived provider, no restart.
    assert provider.fetch_daily_high("KNYC", today.isoformat()) == 88
    assert len(session.calls) == 2

    # t2 -- a restart is no longer what unblocks it; the snapshot t1 persisted
    # already carries the value, so a fresh provider needs no call at all.
    fresh = IEMCLIProvider(
        cache_dir=provider.cache_dir, session=FakeSession([]), request_pause=0.0
    )
    assert fresh.fetch_daily_high("KNYC", today.isoformat()) == 88


def test_a_settled_gap_is_not_rechecked_every_lookup(provider_factory, fixture_payload):
    """The re-check is scoped to dates that could still be published.

    2026-04-11 is present in the archive with no maximum and is months old: the
    gap is real, so repeated lookups must keep being served from the snapshot
    rather than hammering IEM. ``FakeSession`` asserts on any extra request.
    """
    provider, session = provider_factory([FakeResponse(fixture_payload)])
    assert provider.fetch_daily_high("KNYC", "2026-04-11") is None
    assert provider.fetch_daily_high("KNYC", "2026-04-11") is None
    assert provider.fetch_daily_high("KNYC", "2026-05-01") is None
    assert len(session.calls) == 1


def test_pending_ttl_can_throttle_the_recheck(provider_factory, tmp_path):
    """``pending_ttl>0`` bounds the re-check rate for callers without their own.

    ``weather_settlement`` keeps the default 0 because it already throttles a
    given station-day to one lookup per 15 minutes.
    """
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    rows = [
        {
            "station": "KNYC",
            "valid": (today - timedelta(days=1)).isoformat(),
            "high": 86,
            "product": "p",
        }
    ]
    provider, session = provider_factory(
        [FakeResponse({"results": rows, "generated_at": "x"})], pending_ttl=3600.0
    )
    assert provider.fetch_daily_high("KNYC", today.isoformat()) is None
    assert provider.fetch_daily_high("KNYC", today.isoformat()) is None
    assert len(session.calls) == 1  # snapshot is younger than pending_ttl


def test_force_refresh_bypasses_the_cache(provider_factory, fixture_payload):
    provider, session = provider_factory(
        [FakeResponse(fixture_payload), FakeResponse(fixture_payload)]
    )
    provider.fetch_year("KNYC", 2026)
    provider.fetch_year("KNYC", 2026, force_refresh=True)
    assert len(session.calls) == 2


def test_fetch_range_spans_a_year_boundary(provider_factory, fixture_payload):
    """One archive call per calendar year, then filtered to the window."""
    payload_2025 = {
        "results": [
            {
                "station": "KNYC",
                "valid": "2025-12-30",
                "high": 33,
                "low": 27,
                "high_time": "252 PM",
                "product": "p1",
            },
            {
                "station": "KNYC",
                "valid": "2025-12-31",
                "high": 33,
                "low": 25,
                "high_time": "1201 AM",
                "product": "p2",
            },
        ],
        "generated_at": "x",
    }
    provider, session = provider_factory(
        [FakeResponse(payload_2025), FakeResponse(fixture_payload)]
    )
    rows = provider.fetch_range("KNYC", "2025-12-31", "2026-04-11")

    assert [c["params"]["year"] for c in session.calls] == [2025, 2026]
    assert [r["date"] for r in rows] == ["2025-12-31", "2026-04-10", "2026-04-11"]


# ---------------------------------------------------------------------------
# Abort, never default
# ---------------------------------------------------------------------------
def test_http_error_raises_rather_than_defaulting(provider_factory):
    provider, _ = provider_factory([FakeResponse({}, status_code=500)])
    with pytest.raises(IEMCLIError, match="HTTP 500"):
        provider.fetch_year("KNYC", 2026)


def test_malformed_json_raises(provider_factory):
    provider, _ = provider_factory([FakeResponse(ValueError("not json"))])
    with pytest.raises(IEMCLIError, match="non-JSON"):
        provider.fetch_year("KNYC", 2026)


def test_payload_without_results_list_raises(provider_factory):
    provider, _ = provider_factory([FakeResponse({"oops": True})])
    with pytest.raises(IEMCLIError, match="results"):
        provider.fetch_year("KNYC", 2026)


def test_wrong_year_payload_is_rejected_not_stored(provider_factory, tmp_path):
    """The documented 2019 trap.

    Omitting ``year`` (or relying on ``date``) makes the live endpoint return
    the 2019 archive with HTTP 200. If that response ever reaches us for a 2026
    request we must refuse it outright rather than backfill five-year-old
    temperatures as today's settlement truth.
    """
    payload_2019 = {
        "results": [
            {
                "station": "KNYC",
                "valid": "2019-01-01",
                "high": 58,
                "low": 39,
                "product": "p",
            }
        ],
        "generated_at": "x",
    }
    provider, _ = provider_factory([FakeResponse(payload_2019)])
    with pytest.raises(IEMCLIError, match="2019"):
        provider.fetch_year("KNYC", 2026)
    assert not os.path.exists(
        os.path.join(provider.cache_dir, "iem_cli_KNYC_2026.json")
    )


def test_cross_station_substitution_is_refused(provider_factory):
    """Never silently accept a neighbouring station's observations."""
    payload = {
        "results": [
            {
                "station": "KJFK",
                "valid": "2026-07-24",
                "high": 88,
                "low": 70,
                "product": "p",
            }
        ],
        "generated_at": "x",
    }
    provider, _ = provider_factory([FakeResponse(payload)])
    with pytest.raises(IEMCLIError, match="cross-station"):
        provider.fetch_year("KNYC", 2026)


def test_unknown_station_empty_archive_is_reported_not_defaulted(
    provider_factory, caplog
):
    """An unknown station returns HTTP 200 + zero rows -- not an HTTP error."""
    provider, _ = provider_factory([FakeResponse({"results": [], "generated_at": "x"})])
    with caplog.at_level(logging.ERROR):
        rows = provider.fetch_year("KNYC", 2026)
    assert rows == []
    assert "zero rows" in caplog.text


def test_empty_response_is_never_persisted_as_cache(provider_factory, caplog):
    """FINDING B: one transient blip must not truncate a station-year forever.

    ``_save_cache`` used to run before the empty check, and a past year's cache
    is otherwise treated as immutable ("a closed year cannot gain rows"), so a
    single empty 200 during a backfill froze that station-year at zero rows
    until someone deleted the file by hand. Current-year data self-healed via
    the 6h TTL; past years never did.
    """
    real_2024 = {
        "results": [
            {
                "station": "KNYC",
                "valid": "2024-03-01",
                "high": 55,
                "low": 40,
                "product": "p",
            }
        ],
        "generated_at": "x",
    }
    provider, session = provider_factory(
        [FakeResponse({"results": [], "generated_at": "x"}), FakeResponse(real_2024)]
    )
    with caplog.at_level(logging.ERROR):
        assert provider.fetch_year("KNYC", 2024) == []
    assert "zero rows" in caplog.text
    cache_file = os.path.join(provider.cache_dir, "iem_cli_KNYC_2024.json")
    assert not os.path.exists(cache_file), "an empty response was persisted as truth"

    # Not memoized either: the very next call re-queries and gets the real data.
    rows = provider.fetch_year("KNYC", 2024)
    assert [r["high"] for r in rows] == [55]
    assert len(session.calls) == 2
    assert os.path.exists(cache_file)


def test_a_legacy_empty_cache_file_self_heals(provider_factory):
    """A zero-row cache left by an older build must not be trusted either."""
    provider, session = provider_factory(
        [
            FakeResponse(
                {
                    "results": [
                        {
                            "station": "KNYC",
                            "valid": "2024-03-01",
                            "high": 55,
                            "low": 40,
                            "product": "p",
                        }
                    ],
                    "generated_at": "x",
                }
            )
        ]
    )
    os.makedirs(provider.cache_dir, exist_ok=True)
    with open(
        os.path.join(provider.cache_dir, "iem_cli_KNYC_2024.json"),
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            {"_meta": {"station": "KNYC", "year": 2024, "row_count": 0}, "rows": []}, fh
        )

    assert [r["high"] for r in provider.fetch_year("KNYC", 2024)] == [55]
    assert len(session.calls) == 1


def test_missing_date_returns_none_and_logs_error(
    provider_factory, fixture_payload, caplog
):
    provider, _ = provider_factory([FakeResponse(fixture_payload)])
    with caplog.at_level(logging.ERROR):
        assert provider.fetch_daily_high("KNYC", "2026-05-01") is None
    assert "no CLI row" in caplog.text
    assert "not a default" in caplog.text


@pytest.mark.parametrize("gap_date", ["2026-04-10", "2026-04-11"])
def test_row_present_but_high_missing_returns_none(
    provider_factory, fixture_payload, caplog, gap_date
):
    """A null / "M" maximum is an explicit gap, never interpolated."""
    provider, _ = provider_factory([FakeResponse(fixture_payload)])
    with caplog.at_level(logging.ERROR):
        assert provider.fetch_daily_high("KNYC", gap_date) is None
    assert "no maximum temperature" in caplog.text
    # The row itself is still visible so the gap can be reported.
    row = provider.fetch_row("KNYC", gap_date)
    assert row is not None and row["high"] is None


def test_latest_available_skips_rows_without_a_high(provider_factory, fixture_payload):
    provider, _ = provider_factory([FakeResponse(fixture_payload)])
    latest = provider.latest_available("KNYC")
    assert latest["date"] == "2026-07-24"
    assert latest["high"] == 83


def test_offline_mode_refuses_network(tmp_path):
    provider = IEMCLIProvider(cache_dir=str(tmp_path), offline=True, request_pause=0.0)
    with pytest.raises(IEMCLIError, match="offline"):
        provider.fetch_year("KNYC", 2026)


def test_out_of_range_year_is_rejected_before_the_call(provider_factory):
    provider, session = provider_factory([])
    with pytest.raises(IEMCLIError, match="range"):
        provider.fetch_year("KNYC", 3000)
    assert session.calls == []


# ---------------------------------------------------------------------------
# NWS CLI text fallback
# ---------------------------------------------------------------------------
CLI_TEXT = """
000
CDUS41 KOKX 250626
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
226 AM EDT SAT JUL 25 2026

...................................

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR JULY 24 2026...

CLIMATE NORMAL PERIOD 1991 TO 2020

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
...................................................................
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         83    128 PM  97    1999  85     -2       87
  MINIMUM         65    539 AM  56    1893  71     -6       70

MONTHLY SUMMARY
  MAXIMUM TEMPERATURE (F)   85        97      1999
"""


def test_parse_cli_text_extracts_date_and_maximum():
    parsed = parse_cli_text(CLI_TEXT)
    assert parsed["date"] == "2026-07-24"
    assert parsed["high"] == 83
    assert parsed["section"] == "YESTERDAY"
    assert "CENTRAL PARK" in parsed["site"]


def test_parse_cli_text_ignores_the_monthly_maximum_line():
    """The month-to-date block also says MAXIMUM; picking it would report 85."""
    assert parse_cli_text(CLI_TEXT)["high"] == 83


def test_parse_cli_text_rejects_a_non_climate_product():
    assert parse_cli_text("SOME OTHER NWS PRODUCT\nNOTHING HERE") is None
    assert parse_cli_text("") is None


def test_parse_cli_text_without_a_maximum_line_yields_none_high():
    text = CLI_TEXT.replace(
        "  MAXIMUM         83    128 PM  97    1999  85     -2       87", ""
    )
    parsed = parse_cli_text(text)
    assert parsed is not None
    assert parsed["date"] == "2026-07-24"
    assert parsed["high"] is None


def test_nws_fallback_returns_a_row_tagged_with_its_source(provider_factory):
    listing = {"@graph": [{"@id": "https://api.weather.gov/products/abc", "id": "abc"}]}
    provider, _ = provider_factory(
        [FakeResponse(listing), FakeResponse({"productText": CLI_TEXT})]
    )
    row = provider.fetch_daily_high_nws("KNYC", "2026-07-24")
    assert row["high"] == 83
    assert row["source"] == SOURCE_NWS
    assert row["source_url"] == "https://api.weather.gov/products/abc"
    assert list(row) == list(ROW_FIELDS)


def test_nws_fallback_ignores_products_for_other_dates(provider_factory, caplog):
    listing = {"@graph": [{"@id": "https://api.weather.gov/products/abc", "id": "abc"}]}
    provider, _ = provider_factory(
        [FakeResponse(listing), FakeResponse({"productText": CLI_TEXT})]
    )
    with caplog.at_level(logging.ERROR):
        assert provider.fetch_daily_high_nws("KNYC", "2026-07-20") is None
    assert "no product" in caplog.text


def test_fetch_truth_prefers_iem_then_falls_back(provider_factory, fixture_payload):
    listing = {"@graph": [{"@id": "https://api.weather.gov/products/abc", "id": "abc"}]}
    provider, _ = provider_factory(
        [
            FakeResponse(fixture_payload),  # IEM year fetch
            FakeResponse(listing),  # NWS listing (gap date)
            FakeResponse({"productText": CLI_TEXT}),
        ]
    )
    # IEM has this date -> no fallback, source is IEM.
    primary = provider.fetch_truth("KNYC", "2026-07-24")
    assert primary["source"] == SOURCE_IEM and primary["high"] == 83

    # IEM row exists but carries no maximum -> fallback runs. The fixture's
    # 2026-04-10 gap is filled from the (July) product only if dates match, so
    # this asserts the fallback was attempted and honestly returned nothing.
    assert provider.fetch_truth("KNYC", "2026-04-10") is None


def test_fetch_truth_can_disable_the_fallback(provider_factory, fixture_payload):
    provider, session = provider_factory([FakeResponse(fixture_payload)])
    assert provider.fetch_truth("KNYC", "2026-04-10", allow_nws_fallback=False) is None
    assert len(session.calls) == 1  # no NWS listing request


# ---------------------------------------------------------------------------
# Live (network-guarded)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not pytest.importorskip("requests", reason="requests not installed"),
    reason="requests not installed",
)
def test_live_iem_archive_matches_stored_truth():
    """One live call; skips cleanly when the network or archive is unavailable.

    Asserts the contract the module documents: ``year`` filters, rows come back
    for the requested station and year, and a known settled value is present.
    Set ``SKIP_NETWORK_TESTS=1`` to opt out entirely.
    """
    if os.getenv("SKIP_NETWORK_TESTS"):
        pytest.skip("SKIP_NETWORK_TESTS is set")
    provider = IEMCLIProvider(request_pause=0.5)
    try:
        rows = provider.fetch_year("KNYC", 2026, force_refresh=True)
    except IEMCLIError as exc:
        pytest.skip(f"IEM archive unavailable: {exc}")
    if not rows:
        pytest.skip("IEM archive returned no rows for KNYC 2026")

    assert all(r["station"] == "KNYC" for r in rows)
    assert all(r["date"].startswith("2026-") for r in rows)

    by_date = {r["date"]: r["high"] for r in rows}
    if "2026-07-24" in by_date:
        # Cross-checked against Kalshi's own expiration_value (83.00) on 2026-07-25.
        assert by_date["2026-07-24"] == 83
