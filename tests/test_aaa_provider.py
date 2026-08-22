"""Tests for the AAA national-average provider (PRD FR-4.1, Phase 4 EC-1).

Phase 4 exit criterion 1 has two halves and both are asserted here:

* ">=14 consecutive daily values persisted with provenance" ->
  :class:`TestConsecutiveDaysAndProvenance`
* "an induced scrape failure produces an alert and zero signals that day" ->
  :class:`TestInducedScrapeFailure`

Fixtures are built in-process rather than committed: ``tests/fixtures/gas/**`` is
WS-B's under the contract's file-ownership table, and a synthetic page lets each
structural failure mode be induced precisely.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from src.core.interfaces import MarketData
from src.data.aaa_provider import (
    AAA_CSV_COLUMNS,
    ET,
    MAX_DAILY_JUMP,
    QUALITY_OK,
    QUALITY_SUSPECT,
    REASON_HTTP_ERROR,
    REASON_NO_DATA,
    REASON_OFFLINE,
    REASON_ROW_NOT_FOUND,
    REASON_SCRAPE_FAILED_TODAY,
    REASON_SITE_OFFLINE,
    REASON_STALE,
    REASON_TABLE_AMBIGUOUS,
    REASON_TABLE_NOT_FOUND,
    REASON_VALUE_UNPARSEABLE,
    SOURCE_LIVE,
    SOURCE_WAYBACK,
    AAAObservation,
    AAAProvider,
    AAAUnavailable,
    attribute_et_date,
    check_yesterday_chain,
    flag_suspect_rows,
    parse_national_table,
    publication_hour_for,
    read_aaa_csv,
    read_manifest,
    read_scrape_failures,
    record_scrape_failure,
    update_manifest,
    upsert_observations,
    wayback_timestamp_to_et,
    write_aaa_csv,
)
from src.models.gas_projection import GasSeries
from src.strategies.gas_convergence import (
    REJECT_SCRAPE_GATE_BLOCKED,
    GasConvergenceStrategy,
)


# ---------------------------------------------------------------------------
# Test hygiene: nothing here may touch the real data directory
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_production_writes():
    """Fail any test that writes into ``data/gas_truth/``.

    Not hypothetical: a provider constructed without ``failures_path`` inherited
    the module default and appended two fabricated 503 records to the real
    ``scrape_failures.json``. Those records would have blocked live gas signals
    for the day -- a test silently disabling production. The default paths are
    bound as function defaults, so they cannot be monkeypatched away; a
    before/after fingerprint of the directory catches every route to it.
    """
    from src.data.aaa_provider import GAS_TRUTH_DIR

    def fingerprint():
        if not os.path.isdir(GAS_TRUTH_DIR):
            return {}
        out = {}
        for name in os.listdir(GAS_TRUTH_DIR):
            path = os.path.join(GAS_TRUTH_DIR, name)
            if os.path.isfile(path):
                st = os.stat(path)
                out[name] = (st.st_mtime_ns, st.st_size)
        return out

    before = fingerprint()
    yield
    after = fingerprint()
    changed = sorted(
        set(before) ^ set(after)
        | {k for k in set(before) & set(after) if before[k] != after[k]}
    )
    assert not changed, (
        f"test wrote to the real {GAS_TRUTH_DIR}: {changed}. Pass explicit "
        f"csv_path/manifest_path/failures_path pointing at tmp_path."
    )


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------
#: Column order as AAA actually renders it (verified live 2026-07-29).
_REAL_COLUMNS = ("Regular", "Mid-Grade", "Premium", "Diesel", "E85")


def build_page(
    *,
    current=("4.091", "4.589", "4.972", "5.329", "3.110"),
    yesterday=("4.099", "4.590", "4.975", "5.321", "3.127"),
    week_ago=("4.060", "4.565", "4.943", "5.181", "3.130"),
    month_ago=("3.860", "4.348", "4.734", "4.859", "2.958"),
    year_ago=("3.137", "3.621", "3.978", "3.735", "2.541"),
    columns=_REAL_COLUMNS,
    heading="National average gas prices",
    include_current_row: bool = True,
    extra_table: str = "",
    money: bool = True,
) -> bytes:
    """Render a page with the same structure as the live AAA landing page."""

    def cells(values):
        return "".join(f"<td>{'$' if money else ''}{v}</td>" for v in values)

    rows = []
    if include_current_row:
        rows.append(f"<tr><td>Current Avg.</td>{cells(current)}</tr>")
    rows.append(f"<tr><td>Yesterday Avg.</td>{cells(yesterday)}</tr>")
    rows.append(f"<tr><td>Week Ago Avg.</td>{cells(week_ago)}</tr>")
    rows.append(f"<tr><td>Month Ago Avg.</td>{cells(month_ago)}</tr>")
    rows.append(f"<tr><td>Year Ago Avg.</td>{cells(year_ago)}</tr>")
    header = "".join(f"<th>{c}</th>" for c in columns)
    # The live page splits the heading across a <span> ("<span>National</span>
    # average gas prices"), which is why the parser normalises heading text
    # rather than string-matching the raw markup.
    if heading.lower().startswith("national "):
        heading_html = f"<span>National</span> {heading[len('National '):]}"
    else:
        heading_html = heading
    html = f"""<!DOCTYPE html><html><head><title>Gas Prices</title></head><body>
      <div class="nav"><table><tr><td>Home</td><td>News</td></tr></table></div>
      <h1 class="nati">{heading_html}</h1>
      <div class="tblwrap"><table class="table-mob">
        <thead><tr><th></th>{header}</tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
      {extra_table}
    </body></html>"""
    return html.encode("utf-8")


AT_NOON_ET = datetime(2026, 7, 29, 12, 0, tzinfo=ET)


def parse(body: bytes, **kw):
    kw.setdefault("source_url", "https://gasprices.aaa.com/")
    kw.setdefault("captured_at", AT_NOON_ET)
    kw.setdefault("source", SOURCE_LIVE)
    return parse_national_table(body, **kw)


def obs(
    day: date,
    value: float,
    *,
    yesterday=None,
    quality=QUALITY_OK,
    captured=None,
    source=SOURCE_WAYBACK,
):
    """One observation.

    ``source`` defaults to ``aaa_wayback`` because that is what the committed
    series is made of -- and because the *default* is the configuration that
    matters: whether a backfilled row can stand in for a live one is exactly the
    question the scrape-failure block turns on, so it must be explicit at every
    call site rather than inherited silently.
    """
    return AAAObservation(
        date=day,
        value=value,
        source=source,
        source_url=f"https://web.archive.org/web/x/{day.isoformat()}",
        fetched_at="2026-07-29T00:00:00+00:00",
        raw_sha256="0" * 64,
        quality=quality,
        yesterday_value=yesterday,
        captured_at_et=captured
        or datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=14),
    )


# ---------------------------------------------------------------------------
# The REAL FR-4.3 signal path, for the end-to-end EC-1 assertion
# ---------------------------------------------------------------------------
def write_history(
    csv_path: str,
    manifest_path: str,
    *,
    end: date,
    days: int = 450,
    source: str = SOURCE_WAYBACK,
) -> None:
    """Persist a dense AAA series ending at ``end``, through the real writer.

    Long enough to clear ``ProjectionConfig.min_history_days`` (365) so the real
    FR-4.2 projection fits, and smooth enough that no row trips the
    ``MAX_DAILY_JUMP`` suspect rule.
    """
    rows = [
        AAAObservation(
            date=end - timedelta(days=days - 1 - i),
            value=round(4.10 + 0.02 * math.sin(i / 9.0), 3),
            source=source,
            source_url=(
                f"https://web.archive.org/web/"
                f"{(end - timedelta(days=days - 1 - i)):%Y%m%d}170000id_/"
                f"https://gasprices.aaa.com/"
            ),
            fetched_at=(
                f"{(end - timedelta(days=days - 1 - i)).isoformat()}T17:00:00+00:00"
            ),
            raw_sha256=hashlib.sha256(
                (end - timedelta(days=days - 1 - i)).isoformat().encode()
            ).hexdigest(),
        )
        for i in range(days)
    ]
    upsert_observations(rows, path=csv_path, manifest_path=manifest_path)


def gas_market(today: date, *, floor_strike: float = 3.50) -> MarketData:
    """One live-shaped ``KXAAAGASM`` bracket, 14 days from settlement.

    The strike sits far below the series level so the model's P(YES) is ~1.0
    against a market quoted near 0.50: the divergence and EV gates pass on their
    own merits, which is what makes a ``[]`` return attributable to the scrape
    gate and nothing else.
    """
    settlement = today + timedelta(days=14)
    close = datetime.combine(
        settlement - timedelta(days=1), datetime.min.time(), tzinfo=ET
    ) + timedelta(hours=23, minutes=59)
    return MarketData(
        symbol=f"KXAAAGASM-{settlement:%y%b%d}".upper() + f"-{floor_strike:.2f}",
        timestamp=datetime.now(timezone.utc),
        price=0.50,
        volume=1000,
        bid=0.48,
        ask=0.52,
        extra={
            "strike_type": "greater",
            "floor_strike": floor_strike,
            "cap_strike": None,
            "no_bid": 0.48,
            "no_ask": 0.52,
            "close_time": close.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )


def real_signal_path(provider: AAAProvider, today: date):
    """Run the REAL :class:`GasConvergenceStrategy` over a real bracket.

    Deliberately not a stub. A two-line ``produce_signals()`` closure defined
    inside a test asserts a property of the closure, not of the system: it cannot
    show that the shipped signal path consults the gate at all, which is the
    defect this replaces (the gate had zero consumers outside its own module).

    The series is loaded from the provider's own CSV directory, so this is the
    same chain the bot runs: persisted truth -> ``GasSeries`` -> strategy.
    """
    series = GasSeries.from_csv_dir(os.path.dirname(os.path.abspath(provider.csv_path)))
    strategy = GasConvergenceStrategy(
        series=series,
        clock=lambda: today,
        gate=lambda: provider.signal_gate(as_of=today),
        gate_cache_seconds=0.0,
    )
    return strategy.analyze(gas_market(today))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class TestParsing:
    def test_parses_three_decimal_layout(self):
        """The layout AAA rendered from 2025-06 through 2026-05."""
        o = parse(build_page(current=("3.140", "3.617", "3.977", "3.727", "2.539")))
        assert o.value == 3.140
        assert o.quality == QUALITY_OK
        assert o.source == SOURCE_LIVE

    def test_parses_four_decimal_layout(self):
        """The layout AAA rendered from 2026-06 onward. Same parser, no branch."""
        o = parse(
            build_page(current=("4.1290", "4.6410", "5.0150", "5.2790", "3.1900"))
        )
        assert o.value == 4.129

    def test_reads_yesterday_and_the_other_comparison_rows(self):
        o = parse(build_page())
        assert o.value == 4.091
        assert o.yesterday_value == 4.099
        assert o.week_ago_value == 4.060
        assert o.month_ago_value == 3.860
        assert o.year_ago_value == 3.137

    def test_regular_column_found_by_header_not_by_position(self):
        """Mutation test for the column selector.

        A hardcoded ``row[1]`` would silently record Mid-Grade as the national
        regular average, and no downstream check would catch it. Move Regular to
        the third data column and the value must still be Regular's.
        """
        o = parse(
            build_page(
                columns=("Mid-Grade", "Premium", "Regular", "Diesel", "E85"),
                current=("4.589", "4.972", "4.091", "5.329", "3.110"),
                yesterday=("4.590", "4.975", "4.099", "5.321", "3.127"),
            )
        )
        assert o.value == 4.091
        assert o.yesterday_value == 4.099

    def test_nbsp_and_entities_are_handled(self):
        body = parse(build_page()).raw_sha256
        page = build_page().replace(b"Current Avg.", b"Current&nbsp;Avg.")
        o = parse(page)
        assert o.value == 4.091
        assert o.raw_sha256 != body  # different bytes, same reading

    def test_raw_sha256_is_over_the_parsed_bytes(self):
        page = build_page()
        o = parse(page)
        assert o.raw_sha256 == hashlib.sha256(page).hexdigest()

    def test_value_without_dollar_sign_still_parses(self):
        o = parse(build_page(money=False))
        assert o.value == 4.091

    def test_trailing_zero_fourth_decimal_is_not_flagged_as_rounded(self):
        """Every 4-decimal figure observed over 14 months ends in 0, so the
        3-decimal store is lossless in practice."""
        o = parse(build_page(current=("4.1290", "1", "1", "1", "1")))
        assert o.value == 4.129
        assert o.rounded_from is None

    def test_nonzero_fourth_decimal_records_the_rounding(self):
        """If AAA ever publishes real 4-decimal precision the row must say so,
        rather than silently discarding a digit.

        The assertion is on the residual bound, not on the rounding direction:
        ``round()`` breaks ties to the nearest representable double, so 4.0915
        goes to 4.091. Which way it lands is immaterial -- $0.0005 is three
        orders below the $0.01 strike granularity -- but losing the record that
        rounding happened at all would not be.
        """
        o = parse(build_page(current=("4.0915", "1", "1", "1", "1")))
        assert abs(o.value - 4.0915) <= 0.0005
        assert o.rounded_from == "4.0915"


class TestParseAborts:
    """Every structural failure aborts with a specific reason code."""

    #: AAA's real maintenance page, as archived (2023-10-15, 905 bytes, HTTP 200).
    MAINTENANCE_PAGE = (
        b"<html>\n<head>\n    <title>Site is offline</title>\n"
        b'    <meta name="autoupdater" content="maintenance">\n'
        b'</head>\n<body><div id="content">Briefly unavailable for scheduled '
        b"maintenance.</div></body></html>"
    )

    def test_maintenance_page_is_named_distinctly(self):
        """AAA serves this with HTTP 200, so a status check cannot catch it.

        It gets its own reason code because the operator response differs
        entirely from a layout change: wait and retry, versus fix the parser.
        """
        with pytest.raises(AAAUnavailable) as ei:
            parse(self.MAINTENANCE_PAGE)
        assert ei.value.reason_code == REASON_SITE_OFFLINE

    def test_maintenance_page_never_yields_a_row(self):
        """The failure mode that matters: a 200 that is not data."""
        with pytest.raises(AAAUnavailable):
            parse(self.MAINTENANCE_PAGE)

    def test_maintenance_wording_inside_page_content_does_not_false_positive(self):
        """A news item using the phrase must not disable the feed."""
        page = build_page().replace(
            b"<h1", b"<p>Our site is offline for maintenance sometimes.</p><h1"
        )
        assert parse(page).value == 4.091

    def test_live_recorder_alerts_and_blocks_on_a_maintenance_page(self, tmp_path):
        """End-to-end: AAA down for maintenance is an EC-1 induced failure."""
        alerts = []
        p = AAAProvider(
            csv_path=str(tmp_path / "aaa.csv"),
            manifest_path=str(tmp_path / "m.json"),
            failures_path=str(tmp_path / "failures.json"),
            session=_FakeSession(response=_FakeResponse(200, self.MAINTENANCE_PAGE)),
            limiter=_NoSleepLimiter(),
            alert_hook=lambda c, d: alerts.append((c, d)),
        )
        today = date(2026, 5, 20)
        with pytest.raises(AAAUnavailable) as ei:
            p.record_daily(as_of=today)
        assert ei.value.reason_code == REASON_SITE_OFFLINE
        assert len(alerts) == 1
        assert read_aaa_csv(str(tmp_path / "aaa.csv")) == []
        assert p.signal_gate(as_of=today).allow is False

    def test_no_table_at_all(self):
        with pytest.raises(AAAUnavailable) as ei:
            parse(b"<html><body><p>Down for maintenance</p></body></html>")
        assert ei.value.reason_code == REASON_TABLE_NOT_FOUND

    def test_empty_body(self):
        with pytest.raises(AAAUnavailable):
            parse(b"")

    def test_missing_current_row(self):
        with pytest.raises(AAAUnavailable) as ei:
            parse(build_page(include_current_row=False))
        assert ei.value.reason_code == REASON_ROW_NOT_FOUND

    def test_non_numeric_current_value(self):
        with pytest.raises(AAAUnavailable) as ei:
            parse(build_page(current=("N/A", "N/A", "N/A", "N/A", "N/A")))
        assert ei.value.reason_code == REASON_VALUE_UNPARSEABLE

    def test_two_matching_tables_without_a_national_heading_is_ambiguous(self):
        """A state table has the same shape. Guessing would record a state index.

        With two candidate tables and no way to tell which is national, the parse
        must abort rather than pick one.
        """
        state_table = (
            "<h2>California average gas prices</h2><table>"
            "<tr><th></th><th>Regular</th></tr>"
            "<tr><td>Current Avg.</td><td>$5.500</td></tr></table>"
        )
        with pytest.raises(AAAUnavailable) as ei:
            parse(build_page(heading="Average gas prices", extra_table=state_table))
        assert ei.value.reason_code == REASON_TABLE_AMBIGUOUS

    def test_national_heading_disambiguates_two_matching_tables(self):
        """When one candidate IS headed National, that one is used."""
        state_table = (
            "<h2>California average gas prices</h2><table>"
            "<tr><th></th><th>Regular</th></tr>"
            "<tr><td>Current Avg.</td><td>$5.500</td></tr></table>"
        )
        o = parse(build_page(extra_table=state_table))
        assert o.value == 4.091  # national, not California's 5.500

    def test_unknown_reason_code_is_rejected(self):
        with pytest.raises(ValueError):
            raise AAAUnavailable("NOT_A_REAL_CODE", "x")


class TestSuspectNotSilent:
    def test_out_of_range_value_is_suspect_not_an_exception(self):
        """Contract §1.1: implausible values are recorded as suspect, visible."""
        o = parse(build_page(current=("99.990", "1", "1", "1", "1")))
        assert o.quality == QUALITY_SUSPECT
        assert o.value == 99.990

    def test_plausible_value_is_ok(self):
        assert parse(build_page()).quality == QUALITY_OK


# ---------------------------------------------------------------------------
# Timezone attribution -- the off-by-one that would bias every fitted lag
# ---------------------------------------------------------------------------
class TestETAttribution:
    def test_wayback_stamp_is_read_as_utc_then_converted(self):
        et = wayback_timestamp_to_et("20260729110138")
        assert et.tzinfo is not None
        assert (et.year, et.month, et.day, et.hour) == (2026, 7, 29, 7)

    def test_malformed_wayback_stamp_rejected(self):
        for bad in ("2026072911013", "not-a-stamp", ""):
            with pytest.raises(ValueError):
                wayback_timestamp_to_et(bad)

    @pytest.mark.parametrize(
        "utc_stamp,expected",
        [
            # 02:00Z in winter (EST, UTC-5) -> 21:00 ET the previous day.
            ("20260101020000", date(2025, 12, 31)),
            # 02:00Z in summer (EDT, UTC-4) -> 22:00 ET the previous day.
            ("20260701020000", date(2026, 6, 30)),
            # Mid-afternoon ET -> same ET day.
            ("20260715180000", date(2026, 7, 15)),
            # 07:00Z summer = 03:00 ET, exactly at the measured publication hour
            # -- the boundary is inclusive, so this is already the new day.
            ("20260715070000", date(2026, 7, 15)),
            # 06:00Z summer = 02:00 ET, one hour before publication: the page
            # still shows the previous day's figure.
            ("20260715060000", date(2026, 7, 14)),
        ],
    )
    def test_utc_to_et_date_attribution(self, utc_stamp, expected):
        assert attribute_et_date(wayback_timestamp_to_et(utc_stamp)) == expected

    def test_across_dst_spring_forward(self):
        """2026-03-08 is US spring-forward. The offset must come from the zone.

        A hardcoded -5 or -4 would misattribute half the year.
        """
        before = wayback_timestamp_to_et("20260307180000")  # EST, UTC-5 -> 13:00
        after = wayback_timestamp_to_et("20260309180000")  # EDT, UTC-4 -> 14:00
        assert before.hour == 13
        assert after.hour == 14
        assert attribute_et_date(before) == date(2026, 3, 7)
        assert attribute_et_date(after) == date(2026, 3, 9)

    def test_dst_fall_back_repeated_hour_is_unambiguous(self):
        """2026-11-01 01:00-02:00 ET happens twice, but UTC->ET never is.

        Converting *from* UTC is always well defined, which is why every entry
        point here takes a UTC instant. The repeated ET hour would only be
        ambiguous if a local wall-clock time were the input -- so this asserts the
        two 01:30 ET instants stay distinct and both land on 2026-11-01.
        """
        first = wayback_timestamp_to_et("20261101053000")  # 01:30 EDT
        second = wayback_timestamp_to_et("20261101063000")  # 01:30 EST
        assert (first.hour, first.minute) == (1, 30)
        assert (second.hour, second.minute) == (1, 30)
        assert first.utcoffset() != second.utcoffset()
        # PEP 495: comparing two aware datetimes in the SAME zone ignores `fold`,
        # so these two compare equal as ET wall clocks despite being an hour
        # apart. Distinctness has to be asserted on the UTC instants -- which is
        # the deeper reason this module keys everything off UTC.
        assert first.astimezone(timezone.utc) != second.astimezone(timezone.utc)
        # Both are before the 07:00 boundary, so both belong to 2026-10-31.
        assert attribute_et_date(first) == date(2026, 10, 31)
        assert attribute_et_date(second) == date(2026, 10, 31)

    def test_publication_hour_shifts_the_boundary(self):
        pre = datetime(2026, 7, 15, 3, 0, tzinfo=ET)
        post = datetime(2026, 7, 15, 9, 0, tzinfo=ET)
        assert attribute_et_date(pre, publication_hour_et=7) == date(2026, 7, 14)
        assert attribute_et_date(post, publication_hour_et=7) == date(2026, 7, 15)
        # With the boundary at midnight both land on the same ET day.
        assert attribute_et_date(pre, publication_hour_et=0) == date(2026, 7, 15)

    def test_measured_publication_schedule_is_pinned(self):
        """Pins the measured era schedule so it cannot drift by accident.

        AAA moved its republication hour twice over 2022-2026 (5 -> 6 -> 3),
        identified independently in each capture year. Changing any entry changes
        the calendar date of potentially every row in an era, so it must be a
        deliberate act backed by a fresh per-era sweep; the backfill refuses to
        write when its own sweep contradicts whatever is set here.
        """
        from src.data.aaa_provider import PUBLICATION_SCHEDULE

        assert PUBLICATION_SCHEDULE == (
            (date(2022, 1, 1), 5),
            (date(2024, 1, 1), 6),
            (date(2025, 4, 1), 3),
        )

    def test_publication_hour_for_resolves_the_era(self):
        from src.data.aaa_provider import publication_hour_for

        assert publication_hour_for(date(2022, 6, 1)) == 5
        assert publication_hour_for(date(2023, 12, 31)) == 5
        assert publication_hour_for(date(2024, 1, 1)) == 6
        assert publication_hour_for(date(2025, 3, 31)) == 6
        assert publication_hour_for(date(2025, 4, 1)) == 3
        assert publication_hour_for(date(2026, 7, 29)) == 3
        # Before the measured span, the earliest known regime is used rather than
        # today's -- extrapolating backwards is a guess either way, but the
        # oldest measured hour is the better guess.
        assert publication_hour_for(date(2021, 1, 1)) == 5

    def test_schedule_is_used_when_no_hour_is_forced(self):
        """A 2022 capture must be judged by 2022's hour, not by 2026's.

        04:00 ET is *before* the 2022 boundary (5) so it belongs to the previous
        day, but *after* the 2025+ boundary (3) so it would belong to the same
        day. Getting this wrong shifts a whole era by one day.
        """
        old = datetime(2022, 6, 15, 4, 0, tzinfo=ET)
        new = datetime(2026, 6, 15, 4, 0, tzinfo=ET)
        assert attribute_et_date(old) == date(2022, 6, 14)
        assert attribute_et_date(new) == date(2026, 6, 15)

    def test_measured_publication_hour_is_pinned(self):
        """Pins the measured constant so it cannot drift by accident.

        03:00 ET was measured over 884 archived captures with a single sharp
        minimum (1.6% vs 13.9% at the previously assumed 07:00). Changing this
        number changes the calendar date of potentially every row in the series,
        so it must be a deliberate act backed by a fresh sweep -- not a side
        effect of an edit. ``scripts/backfill_gas_history.py`` independently
        refuses to write a series whose attribution its own sweep contradicts.
        """
        from src.data.aaa_provider import PUBLICATION_HOUR_ET

        assert PUBLICATION_HOUR_ET == 3

    def test_naive_datetime_is_refused(self):
        """A naive value would inherit the host zone: VM is UTC, AAA is ET."""
        with pytest.raises(ValueError, match="timezone-aware"):
            attribute_et_date(datetime(2026, 7, 15, 12, 0))

    def test_parse_attributes_via_the_capture_instant(self):
        o = parse(
            build_page(), captured_at=datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        )
        # 02:00Z -> 22:00 ET Jul 14 -> after the 07:00 boundary -> Jul 14.
        assert o.date == date(2026, 7, 14)


# ---------------------------------------------------------------------------
# The Yesterday-vs-Current chain check
# ---------------------------------------------------------------------------
class TestYesterdayChain:
    def test_consistent_series_has_zero_disagreements(self):
        values = [3.100, 3.105, 3.110, 3.108]
        start = date(2026, 5, 1)
        series = [
            obs(
                start + timedelta(days=i),
                v,
                yesterday=(values[i - 1] if i else None),
            )
            for i, v in enumerate(values)
        ]
        res = check_yesterday_chain(series)
        assert res.comparable == 3
        assert res.disagreements == 0
        assert res.agreements == 3

    def test_detects_an_off_by_one_attribution(self):
        """Shifting one row's date by a day must show up as a disagreement.

        This is the property that makes the check worth running: it is exactly
        the timezone bug's signature.
        """
        values = [3.100, 3.200, 3.300]
        start = date(2026, 5, 1)
        series = [
            obs(start + timedelta(days=i), v, yesterday=(values[i - 1] if i else None))
            for i, v in enumerate(values)
        ]
        # Corrupt: claim day 2's Yesterday was something else entirely.
        series[2] = obs(start + timedelta(days=2), 3.300, yesterday=9.999)
        res = check_yesterday_chain(series)
        assert res.disagreements == 1
        assert res.details[0]["date"] == (start + timedelta(days=2)).isoformat()

    def test_pairs_across_a_gap_are_skipped_not_counted_as_disagreements(self):
        """ "Yesterday" means the previous calendar day.

        Comparing across a two-day Wayback gap would manufacture disagreements
        that say nothing about the parse.
        """
        series = [
            obs(date(2026, 5, 1), 3.100),
            obs(date(2026, 5, 4), 3.400, yesterday=3.390),
        ]
        res = check_yesterday_chain(series)
        assert res.comparable == 0
        assert res.disagreements == 0
        assert res.skipped_gap == 1

    def test_rows_without_a_yesterday_cell_are_counted_separately(self):
        series = [obs(date(2026, 5, 1), 3.1), obs(date(2026, 5, 2), 3.2)]
        res = check_yesterday_chain(series)
        assert res.skipped_no_yesterday == 2
        assert res.comparable == 0

    def test_rounding_within_tolerance_is_agreement(self):
        series = [
            obs(date(2026, 5, 1), 3.100),
            obs(date(2026, 5, 2), 3.200, yesterday=3.1004),
        ]
        assert check_yesterday_chain(series).disagreements == 0

    def test_duplicate_dates_resolve_to_the_latest_capture_deterministically(self):
        """Otherwise the headline evidence depends on CDX ordering."""
        early = obs(
            date(2026, 5, 2),
            9.999,
            yesterday=9.998,
            captured=datetime(2026, 5, 2, 8, tzinfo=ET),
        )
        late = obs(
            date(2026, 5, 2),
            3.200,
            yesterday=3.100,
            captured=datetime(2026, 5, 2, 20, tzinfo=ET),
        )
        base = [obs(date(2026, 5, 1), 3.100)]
        forward = check_yesterday_chain(base + [early, late])
        reverse = check_yesterday_chain(base + [late, early])
        assert forward.disagreements == reverse.disagreements == 0


class TestSuspectJumps:
    def test_flags_both_sides_of_an_implausible_consecutive_jump(self):
        """Both rows of the offending pair are flagged, and only those.

        From two rows alone there is no way to say which one is wrong, so both
        are marked; the row after a normal move is not implicated.
        """
        series = [
            obs(date(2026, 5, 1), 3.100),
            obs(date(2026, 5, 2), 3.600),  # +0.50 overnight -- the bad pair
            obs(date(2026, 5, 3), 3.610),  # +0.01 -- an ordinary move
        ]
        by_date = {o.date: o for o in flag_suspect_rows(series)}
        assert by_date[date(2026, 5, 1)].quality == QUALITY_SUSPECT
        assert by_date[date(2026, 5, 2)].quality == QUALITY_SUSPECT
        assert by_date[date(2026, 5, 3)].quality == QUALITY_OK

    def test_does_not_flag_a_large_move_across_a_gap(self):
        series = [obs(date(2026, 5, 1), 3.100), obs(date(2026, 5, 6), 3.600)]
        assert all(o.quality == QUALITY_OK for o in flag_suspect_rows(series))

    def test_moves_at_the_threshold_are_not_flagged(self):
        series = [
            obs(date(2026, 5, 1), 3.100),
            obs(date(2026, 5, 2), 3.100 + MAX_DAILY_JUMP),
        ]
        assert all(o.quality == QUALITY_OK for o in flag_suspect_rows(series))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
class TestPersistence:
    def test_csv_has_exactly_the_contract_columns(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        write_aaa_csv([obs(date(2026, 5, 1), 3.1).csv_row()], path=path)
        with open(path, newline="", encoding="utf-8") as fh:
            assert tuple(next(csv.reader(fh))) == AAA_CSV_COLUMNS

    def test_csv_is_lf_and_sorted_ascending(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        rows = [
            obs(date(2026, 5, 3), 3.3).csv_row(),
            obs(date(2026, 5, 1), 3.1).csv_row(),
            obs(date(2026, 5, 2), 3.2).csv_row(),
        ]
        write_aaa_csv(rows, path=path)
        raw = open(path, "rb").read()
        assert b"\r" not in raw, "CRLF would change content_hash on a Linux VM"
        dates = [r["date"] for r in read_aaa_csv(path)]
        assert dates == ["2026-05-01", "2026-05-02", "2026-05-03"]

    def test_value_is_persisted_to_three_decimals(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 1), 4.1)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        assert read_aaa_csv(path)[0]["value"] == "4.100"

    def test_gaps_stay_gaps_no_interpolated_rows(self, tmp_path):
        """Contract §1.1: a missing day is a missing row. There is no filler."""
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 1), 3.1), obs(date(2026, 5, 5), 3.5)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        rows = read_aaa_csv(path)
        assert len(rows) == 2
        assert {r["date"] for r in rows} == {"2026-05-01", "2026-05-05"}
        assert all(r["quality"] != "interpolated" for r in rows)

    def test_existing_row_is_never_edited_and_a_correction_is_appended(self, tmp_path):
        """The raw_sha256 chain is what makes a row evidence.

        A silent rewrite would break it while every test stayed green, so a
        disagreeing re-fetch keeps the row and records the discrepancy.
        """
        path = str(tmp_path / "aaa.csv")
        manifest = str(tmp_path / "m.json")
        day = date(2026, 5, 1)
        upsert_observations([obs(day, 3.100)], path=path, manifest_path=manifest)
        result = upsert_observations(
            [obs(day, 9.999)], path=path, manifest_path=manifest
        )
        assert result["corrections"] == 1
        assert result["added"] == 0
        assert read_aaa_csv(path)[0]["value"] == "3.100"  # untouched
        corrections = read_manifest(manifest)["corrections"]
        assert len(corrections) == 1
        assert corrections[0]["kept_value"] == "3.100"
        assert corrections[0]["observed_value"] == "9.999"

    def test_identical_re_fetch_is_a_no_op(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        manifest = str(tmp_path / "m.json")
        upsert_observations(
            [obs(date(2026, 5, 1), 3.1)], path=path, manifest_path=manifest
        )
        result = upsert_observations(
            [obs(date(2026, 5, 1), 3.1)], path=path, manifest_path=manifest
        )
        assert result["unchanged"] == 1
        assert result["corrections"] == 0

    def test_regenerate_rewrites_rows(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        manifest = str(tmp_path / "m.json")
        day = date(2026, 5, 1)
        upsert_observations([obs(day, 3.100)], path=path, manifest_path=manifest)
        upsert_observations(
            [obs(day, 4.200)], path=path, manifest_path=manifest, regenerate=True
        )
        assert read_aaa_csv(path)[0]["value"] == "4.200"

    def test_regeneration_is_byte_reproducible(self, tmp_path):
        """Same inputs -> same bytes, so ``content_hash`` identifies the inputs.

        Verified end to end against the real backfill too: two ``--regenerate``
        runs over the same Wayback cache produced an identical CSV digest. This
        only holds because a Wayback row's ``fetched_at`` is the capture instant
        rather than "now" -- stamping the wall clock would make every rerun a
        different artifact and the hash meaningless as an identifier.
        """
        path = str(tmp_path / "aaa.csv")
        manifest = str(tmp_path / "m.json")
        rows = [
            obs(date(2026, 5, 1) + timedelta(days=i), 3.100 + i * 0.01)
            for i in range(5)
        ]
        upsert_observations(rows, path=path, manifest_path=manifest, regenerate=True)
        first = open(path, "rb").read()
        upsert_observations(rows, path=path, manifest_path=manifest, regenerate=True)
        assert open(path, "rb").read() == first

    def test_row_order_does_not_affect_the_bytes(self, tmp_path):
        """Sorting is what makes the hash independent of CDX ordering."""
        path_a = str(tmp_path / "a.csv")
        path_b = str(tmp_path / "b.csv")
        rows = [
            obs(date(2026, 5, 1) + timedelta(days=i), 3.100 + i * 0.01)
            for i in range(5)
        ]
        write_aaa_csv([r.csv_row() for r in rows], path=path_a)
        write_aaa_csv([r.csv_row() for r in reversed(rows)], path=path_b)
        assert open(path_a, "rb").read() == open(path_b, "rb").read()

    def test_manifest_content_hash_matches_the_csv_bytes(self, tmp_path):
        gas_dir = tmp_path / "gas_truth"
        gas_dir.mkdir()
        path = str(gas_dir / "aaa_daily_national.csv")
        manifest_path = str(gas_dir / "manifest.json")
        upsert_observations(
            [obs(date(2026, 5, 1), 3.1), obs(date(2026, 5, 2), 3.2)],
            path=path,
            manifest_path=manifest_path,
        )
        manifest = update_manifest(gas_dir=str(gas_dir), manifest_path=manifest_path)
        series = manifest["series"]["aaa_daily_national"]
        assert series["rows"] == 2
        assert series["first"] == "2026-05-01"
        assert series["last"] == "2026-05-02"
        expected = hashlib.sha256(open(path, "rb").read()).hexdigest()
        assert series["content_hash"] == expected

    def test_manifest_counts_by_source_and_suspect(self, tmp_path):
        gas_dir = tmp_path / "gas_truth"
        gas_dir.mkdir()
        path = str(gas_dir / "aaa_daily_national.csv")
        manifest_path = str(gas_dir / "manifest.json")
        rows = [
            obs(date(2026, 5, 1), 3.1),
            obs(date(2026, 5, 2), 3.2, quality=QUALITY_SUSPECT),
        ]
        upsert_observations(rows, path=path, manifest_path=manifest_path)
        series = update_manifest(gas_dir=str(gas_dir), manifest_path=manifest_path)[
            "series"
        ]["aaa_daily_national"]
        assert series["suspect"] == 1
        assert series["by_source"][SOURCE_WAYBACK] == 2

    def test_manifest_has_the_contract_skeleton_when_absent(self, tmp_path):
        m = read_manifest(str(tmp_path / "nope.json"))
        assert set(m["series"]) == {
            "aaa_daily_national",
            "eia_weekly_regular",
            "rbob_daily",
        }
        assert m["corrections"] == []


# ---------------------------------------------------------------------------
# EC-1, half one: >=14 consecutive daily values persisted with provenance
# ---------------------------------------------------------------------------
class TestConsecutiveDaysAndProvenance:
    def _seed(self, tmp_path, n, start=date(2026, 5, 1), **kw):
        path = str(tmp_path / "aaa.csv")
        rows = [obs(start + timedelta(days=i), 3.0 + i * 0.001, **kw) for i in range(n)]
        upsert_observations(rows, path=path, manifest_path=str(tmp_path / "m.json"))
        return path

    def test_fourteen_consecutive_days_are_counted(self, tmp_path):
        path = self._seed(tmp_path, 14)
        p = AAAProvider(csv_path=path, session=object())
        assert p.consecutive_days_recorded() == 14

    def test_a_hole_breaks_the_run(self, tmp_path):
        """A 14-row file with a gap is not 14 *consecutive* days."""
        path = str(tmp_path / "aaa.csv")
        days = [date(2026, 5, 1) + timedelta(days=i) for i in range(15) if i != 5]
        upsert_observations(
            [obs(d, 3.1) for d in days],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(csv_path=path, session=object())
        assert len(days) == 14
        assert p.consecutive_days_recorded() == 9  # only the run ending at the last day

    def test_suspect_rows_do_not_extend_the_run(self, tmp_path):
        """A run counted through a fit-excluded row would overstate the evidence."""
        path = str(tmp_path / "aaa.csv")
        rows = [obs(date(2026, 5, 1) + timedelta(days=i), 3.1) for i in range(10)]
        rows.append(obs(date(2026, 5, 11), 3.2, quality=QUALITY_SUSPECT))
        rows.extend(obs(date(2026, 5, 12) + timedelta(days=i), 3.3) for i in range(3))
        upsert_observations(rows, path=path, manifest_path=str(tmp_path / "m.json"))
        p = AAAProvider(csv_path=path, session=object())
        assert p.consecutive_days_recorded() == 3

    def test_every_persisted_row_carries_full_provenance(self, tmp_path):
        """ "persisted with provenance" is a per-row property, so assert it."""
        path = self._seed(tmp_path, 14)
        for row in read_aaa_csv(path):
            assert row["source"] in {SOURCE_LIVE, SOURCE_WAYBACK}
            assert row["source_url"].startswith("http")
            assert len(row["raw_sha256"]) == 64
            assert row["fetched_at"]
            assert row["quality"] in {QUALITY_OK, QUALITY_SUSPECT}

    def test_empty_series_counts_zero(self, tmp_path):
        p = AAAProvider(csv_path=str(tmp_path / "none.csv"), session=object())
        assert p.consecutive_days_recorded() == 0
        assert p.longest_consecutive_run()["days"] == 0

    def test_longest_run_finds_a_run_that_does_not_end_at_the_newest_row(
        self, tmp_path
    ):
        """EC-1's ">=14 consecutive" is a property of the whole series.

        ``consecutive_days_recorded`` collapses to a small number whenever the
        most recent days contain a suspect row, which would understate evidence
        that is genuinely present earlier in the series.
        """
        path = str(tmp_path / "aaa.csv")
        rows = [obs(date(2026, 5, 1) + timedelta(days=i), 3.1) for i in range(20)]
        rows.append(obs(date(2026, 5, 21), 3.2, quality=QUALITY_SUSPECT))
        rows.append(obs(date(2026, 5, 22), 3.3))
        upsert_observations(rows, path=path, manifest_path=str(tmp_path / "m.json"))
        p = AAAProvider(csv_path=path, session=object())
        assert p.consecutive_days_recorded() == 1  # only 2026-05-22
        longest = p.longest_consecutive_run()
        assert longest["days"] == 20
        assert longest["first"] == "2026-05-01"
        assert longest["last"] == "2026-05-20"

    def test_longest_run_of_a_single_row_is_one(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 1), 3.1)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(csv_path=path, session=object())
        assert p.longest_consecutive_run()["days"] == 1


# ---------------------------------------------------------------------------
# EC-1, half two: an induced scrape failure alerts AND yields zero signals
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content


class _FakeSession:
    """Minimal stand-in for ``requests.Session`` covering both failure shapes."""

    def __init__(self, *, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = 0
        self.headers = {}

    def get(self, url, timeout=None, params=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.response


class _NoSleepLimiter:
    def wait(self, sleeper=None):
        return 0.0


def make_provider(tmp_path, session, alerts):
    return AAAProvider(
        csv_path=str(tmp_path / "aaa.csv"),
        manifest_path=str(tmp_path / "m.json"),
        failures_path=str(tmp_path / "failures.json"),
        session=session,
        limiter=_NoSleepLimiter(),
        alert_hook=lambda code, detail: alerts.append((code, detail)),
    )


class TestInducedScrapeFailure:
    """Phase 4 EC-1: "an induced scrape failure produces an alert and zero
    signals that day"."""

    def test_transport_failure_alerts_once_and_aborts(self, tmp_path):
        alerts = []
        p = make_provider(
            tmp_path,
            _FakeSession(raises=OSError("connection reset")),
            alerts,
        )
        with pytest.raises(AAAUnavailable) as ei:
            p.record_daily()
        assert ei.value.reason_code == REASON_OFFLINE
        assert len(alerts) == 1, "exactly one alert per failure, not zero and not two"
        assert alerts[0][0] == REASON_OFFLINE

    def test_http_error_alerts_and_aborts(self, tmp_path):
        alerts = []
        p = make_provider(tmp_path, _FakeSession(response=_FakeResponse(503)), alerts)
        with pytest.raises(AAAUnavailable) as ei:
            p.record_daily()
        assert ei.value.reason_code == REASON_HTTP_ERROR
        assert len(alerts) == 1

    def test_parse_failure_alerts_and_aborts(self, tmp_path):
        """A 200 with a changed layout must not be treated as success."""
        alerts = []
        p = make_provider(
            tmp_path,
            _FakeSession(response=_FakeResponse(200, b"<html>redesigned</html>")),
            alerts,
        )
        with pytest.raises(AAAUnavailable) as ei:
            p.record_daily()
        assert ei.value.reason_code == REASON_TABLE_NOT_FOUND
        assert len(alerts) == 1

    def test_failure_writes_no_row_at_all(self, tmp_path):
        """No stale value, no last-known-good, no placeholder."""
        alerts = []
        p = make_provider(tmp_path, _FakeSession(response=_FakeResponse(500)), alerts)
        with pytest.raises(AAAUnavailable):
            p.record_daily()
        assert read_aaa_csv(str(tmp_path / "aaa.csv")) == []

    def test_zero_signals_are_producible_on_a_failed_day(self, tmp_path):
        """The "zero signals" half over an EMPTY series.

        Deliberately a gate-level assertion: with no rows at all there is nothing
        for the real FR-4.2 projection to fit, so the real strategy cannot be
        driven here. The end-to-end assertion through the real
        :class:`GasConvergenceStrategy` lives in
        ``TestFailedScrapeBlocksTheDay.test_zero_signals_producible_on_a_failed_
        day_with_fresh_data``, which is the harder and more important case.
        """
        alerts = []
        p = make_provider(tmp_path, _FakeSession(response=_FakeResponse(503)), alerts)
        today = date(2026, 5, 20)

        def produce_signals():
            gate = p.signal_gate(as_of=today)
            if not gate.allow:
                return []
            return [{"symbol": "KXAAAGASM-26MAY31-4.00", "side": "yes"}]

        with pytest.raises(AAAUnavailable):
            p.record_daily()
        assert alerts, "the failure must alert"
        assert produce_signals() == [], "a failed scrape must yield zero signals"

    def test_stale_series_blocks_signals_even_without_a_fetch(self, tmp_path):
        """A process that never ran and one whose scrape failed look the same.

        Freshness is judged from the persisted series, so neither may trade.
        """
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 1), 3.1)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(csv_path=path, session=object(), limiter=_NoSleepLimiter())
        gate = p.signal_gate(as_of=date(2026, 5, 20))
        assert gate.allow is False
        assert gate.reason_code == REASON_STALE
        assert gate.age_days == 19
        assert "19 days old" in gate.detail  # the measured value that failed

    def test_empty_series_blocks_signals(self, tmp_path):
        p = AAAProvider(csv_path=str(tmp_path / "none.csv"), session=object())
        gate = p.signal_gate(as_of=date(2026, 5, 20))
        assert gate.allow is False
        assert gate.reason_code == REASON_NO_DATA

    def test_alert_hook_exception_does_not_mask_the_abort(self, tmp_path):
        def boom(code, detail):
            raise RuntimeError("webhook down")

        p = AAAProvider(
            csv_path=str(tmp_path / "aaa.csv"),
            manifest_path=str(tmp_path / "m.json"),
            failures_path=str(tmp_path / "failures.json"),
            session=_FakeSession(response=_FakeResponse(503)),
            limiter=_NoSleepLimiter(),
            alert_hook=boom,
        )
        with pytest.raises(AAAUnavailable):
            p.record_daily()

    def test_connect_probe_does_not_page_the_operator(self, tmp_path):
        """A reachability probe failing is not an operator-actionable event."""
        alerts = []
        p = make_provider(tmp_path, _FakeSession(response=_FakeResponse(503)), alerts)
        assert p.connect() is False
        assert alerts == []

    def test_probe_failure_does_not_block_signals(self, tmp_path):
        """Only a real recording attempt marks the day failed."""
        alerts = []
        p = make_provider(tmp_path, _FakeSession(response=_FakeResponse(503)), alerts)
        p.connect()
        assert read_scrape_failures(str(tmp_path / "failures.json")) == {}


class TestFailedScrapeBlocksTheDay:
    """EC-1 requires zero signals on a failed-scrape day even when a recent row
    exists -- otherwise the day an operator was alerted about still trades.

    THE CONFIGURATION UNDER TEST IS THE SHIPPED ONE
    -----------------------------------------------
    Every blocking test here used to build its fixture with *yesterday's* row and
    nothing for today. That is the one arrangement in which the mechanism worked,
    and it is not the arrangement that ships: the Wayback backfill writes a row
    for the current day as a matter of course, and the gate cleared a recorded
    failure on the mere existence of a row for the day without ever checking its
    ``source``. So a genuine scrape failure alerted, and then
    ``signal_gate().allow`` came back True.

    Both fixtures are therefore exercised, and named for what they are.
    """

    def _provider(self, tmp_path, alerts, *, rows):
        path = str(tmp_path / "aaa.csv")
        if rows:
            upsert_observations(rows, path=path, manifest_path=str(tmp_path / "m.json"))
        return AAAProvider(
            csv_path=path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=str(tmp_path / "failures.json"),
            session=_FakeSession(response=_FakeResponse(503)),
            limiter=_NoSleepLimiter(),
            alert_hook=lambda c, d: alerts.append((c, d)),
        )

    def _provider_with_yesterdays_row(self, tmp_path, alerts, today):
        """The THIN configuration: fresh enough to trade, no row for today."""
        return self._provider(
            tmp_path, alerts, rows=[obs(today - timedelta(days=1), 4.100)]
        )

    def _provider_with_todays_backfilled_row(self, tmp_path, alerts, today):
        """The SHIPPED configuration: the backfill has written today's row.

        ``source=aaa_wayback``, which is what ``backfill_gas_history.py`` writes
        and what every row of the committed series carries.
        """
        return self._provider(
            tmp_path,
            alerts,
            rows=[
                obs(today - timedelta(days=1), 4.100),
                obs(today, 4.105, source=SOURCE_WAYBACK),
            ],
        )

    # -- baselines: the freshness gate is satisfied in BOTH fixtures ------

    def test_fresh_row_alone_would_have_allowed_signals(self, tmp_path):
        """Baseline: the freshness gate is satisfied, so the block below is
        attributable to the scrape failure and nothing else."""
        today = date(2026, 5, 20)
        p = self._provider_with_yesterdays_row(tmp_path, [], today)
        assert p.signal_gate(as_of=today).allow is True

    def test_backfilled_today_row_alone_would_have_allowed_signals(self, tmp_path):
        """Same baseline for the shipped fixture."""
        today = date(2026, 5, 20)
        p = self._provider_with_todays_backfilled_row(tmp_path, [], today)
        assert p.signal_gate(as_of=today).allow is True

    # -- the block itself, in both configurations ------------------------

    def test_failed_scrape_blocks_despite_a_fresh_row(self, tmp_path):
        today = date(2026, 5, 20)
        alerts = []
        p = self._provider_with_yesterdays_row(tmp_path, alerts, today)
        with pytest.raises(AAAUnavailable):
            p.record_daily(as_of=today)
        gate = p.signal_gate(as_of=today)
        assert gate.allow is False
        assert gate.reason_code == REASON_SCRAPE_FAILED_TODAY
        assert alerts, "the failure must also alert"

    def test_failed_scrape_blocks_despite_a_backfilled_row_for_today(self, tmp_path):
        """The shipped configuration. This is the assertion that used to fail.

        Verbatim red-team shape: a genuine scrape failure alerts exactly once and
        ``signal_gate().allow`` must be False -- it returned True, with
        ``reason_code is None``, because a same-day ``aaa_wayback`` row cleared
        the record.
        """
        today = date(2026, 5, 20)
        alerts = []
        p = self._provider_with_todays_backfilled_row(tmp_path, alerts, today)
        with pytest.raises(AAAUnavailable) as ei:
            p.record_daily(as_of=today)
        assert ei.value.reason_code == REASON_HTTP_ERROR
        assert [a[0] for a in alerts] == [REASON_HTTP_ERROR]
        gate = p.signal_gate(as_of=today)
        assert gate.allow is False
        assert gate.reason_code == REASON_SCRAPE_FAILED_TODAY
        # the measured value that failed: what WAS on disk for the day
        assert "aaa_wayback" in gate.detail
        assert f"no admissible {SOURCE_LIVE} row" in gate.detail

    def test_failure_state_survives_a_new_process(self, tmp_path):
        """The recorder and the strategy are different processes, so an
        in-memory flag would let the bot trade on a failed day."""
        today = date(2026, 5, 20)
        p1 = self._provider_with_todays_backfilled_row(tmp_path, [], today)
        with pytest.raises(AAAUnavailable):
            p1.record_daily(as_of=today)
        # A brand-new provider object, as a separate process would build.
        p2 = AAAProvider(
            csv_path=str(tmp_path / "aaa.csv"),
            manifest_path=str(tmp_path / "m.json"),
            failures_path=str(tmp_path / "failures.json"),
            session=object(),
        )
        assert p2.signal_gate(as_of=today).reason_code == REASON_SCRAPE_FAILED_TODAY

    # -- what may and may not reopen the day -----------------------------

    def test_a_later_live_success_reopens_the_day(self, tmp_path):
        """A transient morning failure must not disable the whole day once a
        real LIVE value has been recorded.

        Was ``test_a_later_success_reopens_the_day``, which wrote a row for today
        with ``source=aaa_wayback`` and asserted ``allow is True`` -- enshrining
        as correct the exact condition that defeated the block. The clearing row
        has to be positive evidence that *today's own fetch* succeeded, so the
        source is now load-bearing and asserted.
        """
        today = date(2026, 5, 20)
        failures = str(tmp_path / "failures.json")
        path = str(tmp_path / "aaa.csv")
        record_scrape_failure(today, REASON_HTTP_ERROR, "503", path=failures)
        upsert_observations(
            [obs(today, 4.200, source=SOURCE_LIVE)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(
            csv_path=path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=failures,
            session=object(),
        )
        gate = p.signal_gate(as_of=today)
        assert gate.allow is True

    def test_a_same_day_backfilled_row_does_not_reopen_the_day(self, tmp_path):
        """The inverse of the test above, and the whole defect in one assertion.

        An archived capture is not evidence that the live path recovered, and
        ``backfill_gas_history.py`` can be re-run at will -- so accepting one
        would let any unrelated process clear an outstanding operator alert.
        """
        today = date(2026, 5, 20)
        failures = str(tmp_path / "failures.json")
        path = str(tmp_path / "aaa.csv")
        record_scrape_failure(today, REASON_HTTP_ERROR, "503", path=failures)
        upsert_observations(
            [obs(today, 4.200, source=SOURCE_WAYBACK)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(
            csv_path=path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=failures,
            session=object(),
        )
        gate = p.signal_gate(as_of=today)
        assert gate.allow is False
        assert gate.reason_code == REASON_SCRAPE_FAILED_TODAY

    def test_a_suspect_live_row_does_not_reopen_the_day(self, tmp_path):
        """A live row the series itself will not admit cannot clear the block.

        Otherwise the day reopens on a value that the freshness gate then refuses
        to use, and trading proceeds off an older row on a day an operator was
        alerted about.
        """
        today = date(2026, 5, 20)
        failures = str(tmp_path / "failures.json")
        path = str(tmp_path / "aaa.csv")
        record_scrape_failure(today, REASON_HTTP_ERROR, "503", path=failures)
        upsert_observations(
            [
                obs(today - timedelta(days=1), 4.100),
                obs(today, 12.0, source=SOURCE_LIVE, quality=QUALITY_SUSPECT),
            ],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(
            csv_path=path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=failures,
            session=object(),
        )
        assert p.signal_gate(as_of=today).reason_code == REASON_SCRAPE_FAILED_TODAY
        # ... and it DOES clear it when the caller has opted into suspect rows
        assert p.signal_gate(as_of=today, allow_suspect=True).allow is True

    def test_yesterdays_failure_does_not_block_today(self, tmp_path):
        today = date(2026, 5, 20)
        failures = str(tmp_path / "failures.json")
        record_scrape_failure(
            today - timedelta(days=1), REASON_HTTP_ERROR, "503", path=failures
        )
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(today, 4.200)], path=path, manifest_path=str(tmp_path / "m.json")
        )
        p = AAAProvider(
            csv_path=path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=failures,
            session=object(),
        )
        assert p.signal_gate(as_of=today).allow is True

    def test_failure_record_carries_reason_and_detail(self, tmp_path):
        today = date(2026, 5, 20)
        failures = str(tmp_path / "failures.json")
        record_scrape_failure(today, REASON_OFFLINE, "connection reset", path=failures)
        rec = read_scrape_failures(failures)[today.isoformat()][0]
        assert rec["reason_code"] == REASON_OFFLINE
        assert rec["detail"] == "connection reset"
        assert rec["at"]

    def test_corrupt_failure_file_reads_as_empty(self, tmp_path):
        p = tmp_path / "failures.json"
        p.write_text("{not json", encoding="utf-8")
        assert read_scrape_failures(str(p)) == {}

    # -- the end-to-end EC-1 assertion, through the REAL strategy ---------

    def test_zero_signals_producible_on_a_failed_day_with_fresh_data(self, tmp_path):
        """EC-1's second half, end to end, in the shipped configuration.

        Drives the real :class:`GasConvergenceStrategy` over a real bracket, with
        a real projection fitted from the real persisted CSV. The previous version
        of this test exercised a two-line ``produce_signals()`` stub defined
        inside itself, so it asserted a property of a lambda -- it would have
        passed with the shipped signal path consulting no gate at all, which is
        exactly what was shipped.
        """
        today = date(2026, 5, 20)
        alerts = []
        csv_path = str(tmp_path / "aaa_daily_national.csv")
        manifest = str(tmp_path / "m.json")
        # The shipped configuration: the backfill has already written today.
        write_history(csv_path, manifest, end=today, source=SOURCE_WAYBACK)
        p = AAAProvider(
            csv_path=csv_path,
            manifest_path=manifest,
            failures_path=str(tmp_path / "failures.json"),
            session=_FakeSession(response=_FakeResponse(503)),
            limiter=_NoSleepLimiter(),
            alert_hook=lambda c, d: alerts.append((c, d)),
        )

        before = real_signal_path(p, today)
        assert len(before) == 1, (
            "the fixture must be able to produce a signal, or the assertion "
            f"below proves nothing: {before}"
        )

        with pytest.raises(AAAUnavailable):
            p.record_daily(as_of=today)
        assert len(alerts) == 1, "the failure must alert exactly once"
        assert (
            real_signal_path(p, today) == []
        ), "a failed scrape must yield zero signals from the real strategy"

    def test_the_real_strategy_names_the_gate_in_its_rejection(self, tmp_path, caplog):
        """Silence must be explained: one INFO line with the gate's reason."""
        today = date(2026, 5, 20)
        csv_path = str(tmp_path / "aaa_daily_national.csv")
        manifest = str(tmp_path / "m.json")
        write_history(csv_path, manifest, end=today, source=SOURCE_WAYBACK)
        p = AAAProvider(
            csv_path=csv_path,
            manifest_path=manifest,
            failures_path=str(tmp_path / "failures.json"),
            session=_FakeSession(response=_FakeResponse(503)),
            limiter=_NoSleepLimiter(),
            alert_hook=lambda c, d: None,
        )
        with pytest.raises(AAAUnavailable):
            p.record_daily(as_of=today)

        collected = []

        class _Collector(logging.Handler):
            def emit(self, record):
                collected.append(record)

        from src.utils.logger import logger as mp_logger

        handler = _Collector(level=logging.DEBUG)
        mp_logger.addHandler(handler)
        try:
            assert real_signal_path(p, today) == []
        finally:
            mp_logger.removeHandler(handler)

        lines = [r.getMessage() for r in collected if "[Risk] REJECT" in r.getMessage()]
        assert len(lines) == 1, lines
        assert f" reason={REJECT_SCRAPE_GATE_BLOCKED} " in lines[0] + " "
        assert f"gate_reason={REASON_SCRAPE_FAILED_TODAY}" in lines[0]


class TestPublicationDayCoupling:
    """The failure record and the observation must key on the SAME ET day.

    ``today_et`` decides which day a failure is recorded against; the row a
    successful fetch writes is dated by :func:`attribute_et_date`. Before the
    publication hour these are different dates -- at 01:00 ET the page still shows
    the previous day's figure -- so a raw-ET ``today_et`` recorded the failure
    against ``D`` while the retry that succeeded wrote a row for ``D-1``. Nothing
    could then clear the block and the day was stranded, mitigated only by a
    docstring asking the operator to run the cron between 12:00 and 20:00 ET.

    Advice is not code, so the coupling is now structural: both come from
    ``attribute_et_date`` with the same ``publication_hour_et``.
    """

    @staticmethod
    def _freeze(monkeypatch, instant: datetime):
        """Pin ``datetime.now`` inside the provider module to ``instant``."""
        import src.data.aaa_provider as mod

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz is not None else instant

        monkeypatch.setattr(mod, "datetime", _Frozen)

    def test_today_et_is_the_attributed_publication_day(self, monkeypatch):
        """01:30 ET on the 20th is still the 19th's published figure."""
        assert publication_hour_for(date(2026, 5, 20)) == 3
        self._freeze(monkeypatch, datetime(2026, 5, 20, 1, 30, tzinfo=ET))
        p = AAAProvider(session=object())
        assert p.today_et() == date(2026, 5, 19)

    def test_after_publication_today_et_is_the_calendar_day(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 5, 20, 15, 0, tzinfo=ET))
        p = AAAProvider(session=object())
        assert p.today_et() == date(2026, 5, 20)

    def test_today_et_agrees_with_attribute_et_date_at_every_hour(self, monkeypatch):
        """The two must never be able to disagree, at any clock time."""
        for hour in range(24):
            instant = datetime(2026, 5, 20, hour, 17, tzinfo=ET)
            self._freeze(monkeypatch, instant)
            p = AAAProvider(session=object())
            assert p.today_et() == attribute_et_date(instant), hour

    def test_an_explicit_as_of_is_honoured_verbatim(self, monkeypatch):
        self._freeze(monkeypatch, datetime(2026, 5, 20, 1, 30, tzinfo=ET))
        p = AAAProvider(session=object())
        assert p.today_et(as_of=date(2001, 1, 1)) == date(2001, 1, 1)

    def test_a_pre_publication_retry_can_clear_its_own_failure(
        self, tmp_path, monkeypatch
    ):
        """The stranding scenario, end to end, at 01:30 ET.

        A failure then a success inside the same pre-publication window must
        leave the day open. Under a raw-ET ``today_et`` the failure keys on
        2026-05-20, the successful fetch writes 2026-05-19, and the block never
        lifts.
        """
        self._freeze(monkeypatch, datetime(2026, 5, 20, 1, 30, tzinfo=ET))
        csv_path = str(tmp_path / "aaa.csv")
        failures = str(tmp_path / "failures.json")

        broken = AAAProvider(
            csv_path=csv_path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=failures,
            session=_FakeSession(response=_FakeResponse(503)),
            limiter=_NoSleepLimiter(),
            alert_hook=lambda c, d: None,
        )
        with pytest.raises(AAAUnavailable):
            broken.record_daily()
        # The failure is keyed on the PUBLICATION day, not the calendar day.
        assert list(read_scrape_failures(failures)) == ["2026-05-19"]
        assert broken.signal_gate().reason_code == REASON_SCRAPE_FAILED_TODAY

        working = AAAProvider(
            csv_path=csv_path,
            manifest_path=str(tmp_path / "m.json"),
            failures_path=failures,
            session=_FakeSession(response=_FakeResponse(200, build_page())),
            limiter=_NoSleepLimiter(),
            alert_hook=lambda c, d: None,
        )
        recorded = working.record_daily()
        assert recorded.date == date(2026, 5, 19)
        assert recorded.source == SOURCE_LIVE
        # Same key on both sides, so the retry clears the block it recorded.
        assert working.signal_gate().allow is True


class TestSignalGateAllows:
    def test_fresh_row_allows_signals(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 20), 3.456)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(csv_path=path, session=object())
        gate = p.signal_gate(as_of=date(2026, 5, 20))
        assert gate.allow is True
        assert gate.age_days == 0
        assert gate.observation is not None
        assert gate.observation.value == 3.456

    def test_two_day_old_row_is_within_the_contract_freshness_gate(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 18), 3.4)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(csv_path=path, session=object())
        assert p.signal_gate(as_of=date(2026, 5, 20)).allow is True
        assert p.signal_gate(as_of=date(2026, 5, 21)).allow is False

    def test_suspect_rows_are_not_admissible_by_default(self, tmp_path):
        path = str(tmp_path / "aaa.csv")
        upsert_observations(
            [obs(date(2026, 5, 20), 3.4, quality=QUALITY_SUSPECT)],
            path=path,
            manifest_path=str(tmp_path / "m.json"),
        )
        p = AAAProvider(csv_path=path, session=object())
        assert p.signal_gate(as_of=date(2026, 5, 20)).allow is False


# ---------------------------------------------------------------------------
# Politeness
# ---------------------------------------------------------------------------
class TestPoliteness:
    def test_user_agent_names_the_project_and_a_contact(self):
        from src.data.aaa_provider import DEFAULT_USER_AGENT

        assert "money-printer" in DEFAULT_USER_AGENT
        assert "hoyeriiim87@gmail.com" in DEFAULT_USER_AGENT

    def test_crawl_delay_matches_robots_txt(self):
        from src.data.aaa_provider import CRAWL_DELAY_SECONDS

        assert CRAWL_DELAY_SECONDS >= 10.0

    def test_limiter_spaces_consecutive_requests(self):
        from src.data.aaa_provider import _CrawlLimiter

        slept = []
        lim = _CrawlLimiter(10.0)
        lim.wait(sleeper=slept.append)
        lim.wait(sleeper=slept.append)
        assert slept and slept[-1] > 0, "second request must be delayed"

    def test_limiter_is_process_wide_not_per_instance(self):
        """Two providers in one process must not together exceed the rate."""
        from src.data.aaa_provider import _AAA_LIMITER

        a = AAAProvider(session=object())
        b = AAAProvider(session=object())
        assert a._limiter is b._limiter is _AAA_LIMITER
