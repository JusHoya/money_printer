"""Station-correctness tests for the weather feed (PRD FR-1.4, Phase 1 exit 6).

The defect under test: Kalshi settles ``KXHIGHNY`` on **KNYC** (Central Park)
and ``KXHIGHCHI`` on **KMDW** (Chicago Midway), while the bot observed
**KJFK** and **KORD**. Measured against the IEM archive over 2026-07-12..25
the settlement station and its old airport proxy differ by up to 3F (NY) and
2F (CHI) on a daily high -- enough to flip a 2F-wide bracket on about half of
all days, and thus to poison every observation-driven decision (running daily
max, lock-in logic, calibration).

Coverage:
1.  the authoritative city config maps each city to its settlement station,
    Kalshi series and IANA timezone;
2.  a hard guard that no airport proxy can reappear on a strategy-reachable
    path (AST-level, so comments documenting the defect do not mask a real
    regression);
3.  FR-1.1 bracket fields survive the tick's Kalshi/observation fusion;
4.  the running daily max is scoped to the station's LOCAL calendar day, not
    a UTC day;
5.  a network-guarded live cross-check of KNYC/KMDW observed daily highs
    against the IEM station archive (Phase 1 exit criterion 6).

Run: $env:PYTHONPATH="."; python -m pytest tests/test_weather_stations.py -v
"""

import ast
import io
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bots.weather_bot import (  # noqa: E402
    BRACKET_FIELDS,
    CITY_CONFIG,
    SETTLEMENT_STATIONS,
    STATION_TIMEZONES,
    WEATHER_CITIES,
    WeatherBot,
)
from src.core.bracket_payoff import parse_bracket_spec, settles_yes  # noqa: E402
from src.core.interfaces import MarketData  # noqa: E402
from src.data.metar_provider import (  # noqa: E402
    DEFAULT_STATION_TIMEZONES,
    METARProvider,
    parse_tgroup,
)
from src.data.nws_provider import NWSProvider  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Files that must never name an airport proxy on a strategy-reachable path.
GUARDED_SOURCES = (
    os.path.join(REPO_ROOT, "src", "bots", "weather_bot.py"),
    os.path.join(REPO_ROOT, "src", "data", "metar_provider.py"),
    os.path.join(REPO_ROOT, "src", "data", "nws_provider.py"),
)

# The airport proxies the bot used to observe. Neither settles any KXHIGH market.
AIRPORT_PROXIES = ("KJFK", "KORD")
_PROXY_RE = re.compile("|".join(AIRPORT_PROXIES))
_NON_SETTLEMENT_MARKER = re.compile(r"non-settlement", re.IGNORECASE)


# ===========================================================================
# 1. Authoritative city config
# ===========================================================================


class TestCityConfig:
    """One config structure carries market, station, and clock per city."""

    EXPECTED = {
        # city key: (Kalshi series, settlement station, IANA timezone)
        "NY": ("KXHIGHNY", "KNYC", "America/New_York"),
        "CHI": ("KXHIGHCHI", "KMDW", "America/Chicago"),
        "LAX": ("KXHIGHLAX", "KLAX", "America/Los_Angeles"),
        "MIA": ("KXHIGHMIA", "KMIA", "America/New_York"),
    }

    def test_every_tracked_city_present(self):
        assert set(CITY_CONFIG) == set(self.EXPECTED)
        assert len(WEATHER_CITIES) == len(self.EXPECTED)

    @pytest.mark.parametrize("city_key", sorted(EXPECTED))
    def test_city_maps_to_settlement_station(self, city_key):
        series, station, tz_name = self.EXPECTED[city_key]
        city = CITY_CONFIG[city_key]

        assert city.kalshi_series == series
        assert city.settlement_station == station, (
            f"{city_key}: Kalshi settles {series} on {station}; config says "
            f"{city.settlement_station}"
        )
        assert city.timezone == tz_name

    @pytest.mark.parametrize("city_key", sorted(EXPECTED))
    def test_timezone_is_a_loadable_iana_zone(self, city_key):
        """FR-3.2 depends on these being real zones, not free text."""
        tz = ZoneInfo(CITY_CONFIG[city_key].timezone)
        assert tz.key == CITY_CONFIG[city_key].timezone

    def test_ny_is_central_park_not_jfk(self):
        assert CITY_CONFIG["NY"].settlement_station == "KNYC"
        assert CITY_CONFIG["NY"].settlement_station not in AIRPORT_PROXIES

    def test_chi_is_midway_not_ohare(self):
        assert CITY_CONFIG["CHI"].settlement_station == "KMDW"
        assert CITY_CONFIG["CHI"].settlement_station not in AIRPORT_PROXIES

    def test_derived_lookups_agree_with_city_records(self):
        assert SETTLEMENT_STATIONS == tuple(
            c.settlement_station for c in WEATHER_CITIES
        )
        assert STATION_TIMEZONES == {
            c.settlement_station: c.timezone for c in WEATHER_CITIES
        }

    def test_provider_default_timezones_match_city_config(self):
        """METARProvider's standalone fallback must not drift from the bot."""
        assert dict(STATION_TIMEZONES) == dict(DEFAULT_STATION_TIMEZONES)

    def test_bot_exposes_settlement_stations_only(self):
        bot = WeatherBot.__new__(WeatherBot)
        assert bot.settlement_stations == ["KNYC", "KMDW", "KLAX", "KMIA"]
        # Back-compat alias used by older call sites resolves to the same list.
        assert bot.nws_stations == bot.settlement_stations
        assert bot.get_symbols() == [
            "KXHIGHNY",
            "KXHIGHCHI",
            "KXHIGHLAX",
            "KXHIGHMIA",
        ]

    def test_metar_provider_default_stations_are_settlement_stations(self):
        assert METARProvider.DEFAULT_STATIONS == ["KNYC", "KMDW", "KLAX", "KMIA"]
        assert not set(METARProvider().stations) & set(AIRPORT_PROXIES)

    def test_nws_provider_default_station_is_a_settlement_station(self):
        provider = NWSProvider("(test, test@example.com)")
        assert provider.stations == ["KNYC"]


# ===========================================================================
# 2. Guard: no airport proxy on a strategy-reachable path
# ===========================================================================


def _docstring_constants(tree):
    """Identity set of Constant nodes used as module/class/function docstrings."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _executable_proxy_hits(path):
    """(file, line, snippet) for every KJFK/KORD in *executable* source.

    Comments are absent from the AST and docstrings are excluded explicitly,
    so this sees only values and names that code can actually evaluate --
    string literals, identifiers, attribute names, keyword arguments.
    A regression that reintroduces an airport proxy as data lands here even
    if a nearby comment carries the non-settlement marker.
    """
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source, filename=path)
    docstrings = _docstring_constants(tree)
    hits = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if _PROXY_RE.search(node.value.upper()):
                hits.append((path, node.lineno, repr(node.value)[:90]))
        elif isinstance(node, ast.Name) and _PROXY_RE.search(node.id.upper()):
            hits.append((path, node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and _PROXY_RE.search(node.attr.upper()):
            hits.append((path, node.lineno, node.attr))
        elif isinstance(node, ast.arg) and _PROXY_RE.search(node.arg.upper()):
            hits.append((path, node.lineno, node.arg))
        elif isinstance(node, ast.keyword) and node.arg:
            if _PROXY_RE.search(node.arg.upper()):
                hits.append((path, node.lineno, node.arg))
    return hits


def _unlabelled_proxy_lines(path):
    """(file, line, text) for KJFK/KORD text lacking a non-settlement marker.

    A mention is permitted only when the token ``non-settlement`` appears on
    the same line or within the three preceding lines -- i.e. the reference is
    explicitly labelled as not a settlement station.
    """
    lines = io.open(path, encoding="utf-8").read().splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if not _PROXY_RE.search(line.upper()):
            continue
        window = lines[max(0, i - 3) : i + 1]
        if any(_NON_SETTLEMENT_MARKER.search(w) for w in window):
            continue
        offenders.append((path, i + 1, line.strip()))
    return offenders


class TestNoAirportProxyReachesStrategies:
    @pytest.mark.parametrize("path", GUARDED_SOURCES, ids=os.path.basename)
    def test_no_airport_proxy_in_executable_code(self, path):
        hits = _executable_proxy_hits(path)
        assert not hits, (
            "KJFK/KORD found in executable code (PRD FR-1.4):\n"
            + "\n".join(
                f"  {os.path.relpath(f, REPO_ROOT)}:{ln}  ->  {snippet}"
                for f, ln, snippet in hits
            )
        )

    @pytest.mark.parametrize("path", GUARDED_SOURCES, ids=os.path.basename)
    def test_every_airport_proxy_mention_is_labelled_non_settlement(self, path):
        offenders = _unlabelled_proxy_lines(path)
        assert not offenders, (
            "KJFK/KORD mentioned without a 'non-settlement' label within 3 "
            "lines (PRD FR-1.4):\n"
            + "\n".join(
                f"  {os.path.relpath(f, REPO_ROOT)}:{ln}  ->  {text}"
                for f, ln, text in offenders
            )
        )

    def test_guard_would_catch_a_regression(self, tmp_path):
        """Mutation check: the guard must actually fail on a reintroduction."""
        bad = tmp_path / "regressed.py"
        bad.write_text(
            'STATIONS = ["KJFK", "KLAX", "KORD", "KMIA"]\n', encoding="utf-8"
        )
        hits = _executable_proxy_hits(str(bad))
        assert len(hits) == 2
        assert {h[1] for h in hits} == {1}

    def test_label_guard_would_catch_an_unlabelled_mention(self, tmp_path):
        bad = tmp_path / "commented.py"
        bad.write_text("# fall back to KORD when Midway is quiet\n", encoding="utf-8")
        assert _unlabelled_proxy_lines(str(bad))

    def test_no_proxy_in_any_observation_station_the_bot_uses(self):
        bot = WeatherBot.__new__(WeatherBot)
        reachable = set(bot.settlement_stations) | set(bot.nws_stations)
        reachable |= set(METARProvider.DEFAULT_STATIONS)
        assert not reachable & set(AIRPORT_PROXIES)


# ===========================================================================
# 3. FR-1.1 bracket fields survive the tick fusion
# ===========================================================================


def _today_code():
    return datetime.now().strftime("%y%b%d").upper()


def _market(symbol, bid, ask, strike_type, floor_strike, cap_strike, sub_title):
    return MarketData(
        symbol=symbol,
        timestamp=datetime.now(),
        price=bid,
        volume=100,
        bid=bid,
        ask=ask,
        extra={
            "no_bid": round(1 - ask, 2),
            "no_ask": round(1 - bid, 2),
            "strike_type": strike_type,
            "floor_strike": floor_strike,
            "cap_strike": cap_strike,
            "yes_sub_title": sub_title,
        },
    )


def _fake_ladder():
    d = _today_code()
    return [
        _market(f"KXHIGHNY-{d}-B86.5", 0.72, 0.74, "between", 86.0, 87.0, "86° to 87°"),
        _market(f"KXHIGHNY-{d}-T87", 0.15, 0.18, "greater", 87.0, None, "88° or above"),
        _market(f"KXHIGHNY-{d}-T80", 0.03, 0.05, "less", None, 80.0, "79° or below"),
    ]


def _bot_with_fake_kalshi(ladder):
    """WeatherBot wired to mocks — no network, no ML model load."""
    bot = WeatherBot.__new__(WeatherBot)
    bot.name = "Weather"
    bot.ticker_cache = {}
    bot._last_depth_snapshot = 1e18  # never due: no depth calls in this test
    bot.CITIES = (CITY_CONFIG["NY"],)

    obs = MarketData(
        symbol="KNYC",
        timestamp=datetime.now(),
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={
            "temperature_f": 84.0,
            "max_temp_today_f": 86.0,
            "source": "live_metar",
            "forecast": [],
            "station_name": "KNYC",
        },
    )
    bot.metar = MagicMock()
    bot.metar.fetch_latest.return_value = obs

    bot.nws = MagicMock()
    bot.nws.fetch_latest.return_value = MarketData(
        symbol="KNYC",
        timestamp=datetime.now(),
        price=0.0,
        volume=0,
        bid=0.0,
        ask=0.0,
        extra={"forecast": [{"isDaytime": True, "temperature": 87}]},
    )

    bot.kalshi = MagicMock()
    bot.kalshi.fetch_market_ladder.return_value = ladder
    return bot, obs


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr("src.bots.weather_bot.time.sleep", lambda s: None)


class TestBracketFieldPropagation:
    def test_obs_extra_carries_bracket_fields_after_tick(self):
        ladder = _fake_ladder()
        bot, obs = _bot_with_fake_kalshi(ladder)

        bot.tick(MagicMock(), MagicMock())

        # Active market = highest YES bid = the B86.5 'between' bracket.
        active = max(ladder, key=lambda m: m.bid)
        assert obs.symbol == active.symbol
        for field in ("floor_strike", "cap_strike", "strike_type"):
            assert field in obs.extra, f"{field} missing from fused obs_data.extra"
            assert obs.extra[field] == active.extra[field]
        assert obs.extra["yes_sub_title"] == active.extra["yes_sub_title"]

    def test_fused_extra_is_directly_parseable_by_bracket_payoff(self):
        """A strategy can call parse_bracket_spec without re-fetching."""
        ladder = _fake_ladder()
        bot, obs = _bot_with_fake_kalshi(ladder)

        bot.tick(MagicMock(), MagicMock())

        spec = parse_bracket_spec(obs.symbol, obs.extra)
        assert spec.strike_type == "between"
        assert settles_yes(spec, 86.0) is True
        assert settles_yes(spec, 88.0) is False

    def test_existing_observation_keys_are_preserved(self):
        ladder = _fake_ladder()
        bot, obs = _bot_with_fake_kalshi(ladder)

        bot.tick(MagicMock(), MagicMock())

        assert obs.extra["temperature_f"] == 84.0
        assert obs.extra["max_temp_today_f"] == 86.0
        assert obs.extra["source"] == "live_metar"
        # City provenance is added, never a substitute for the above.
        assert obs.extra["settlement_station"] == "KNYC"
        assert obs.extra["station_timezone"] == "America/New_York"
        assert obs.extra["city_key"] == "NY"

    def test_bracket_fields_present_even_when_api_omits_them(self):
        """Absent fields land as None so no stale bracket can survive."""
        d = _today_code()
        bare = MarketData(
            symbol=f"KXHIGHNY-{d}-B90.5",
            timestamp=datetime.now(),
            price=0.5,
            volume=1,
            bid=0.5,
            ask=0.52,
            extra={"no_bid": 0.48, "no_ask": 0.5},
        )
        bot, obs = _bot_with_fake_kalshi([bare])

        bot.tick(MagicMock(), MagicMock())

        for field in BRACKET_FIELDS:
            assert field in obs.extra
            assert obs.extra[field] is None
        with pytest.raises(Exception):
            parse_bracket_spec(obs.symbol, obs.extra)

    def test_ladder_rows_carry_bracket_fields_to_the_dashboard(self):
        ladder = _fake_ladder()
        bot, _ = _bot_with_fake_kalshi(ladder)
        dashboard = MagicMock()

        bot.tick(MagicMock(), dashboard)

        by_symbol = {
            c[0][0].split(" ")[0]: c[1]
            for c in dashboard.update_price.call_args_list
            if c[0][0].endswith("(Market)")
        }
        assert set(by_symbol) == {m.symbol for m in ladder}
        for m in ladder:
            kwargs = by_symbol[m.symbol]
            assert kwargs["strike_type"] == m.extra["strike_type"]
            assert kwargs["floor_strike"] == m.extra["floor_strike"]
            assert kwargs["cap_strike"] == m.extra["cap_strike"]
            # FR-0.7 quote columns are untouched.
            assert kwargs["bid"] == m.bid and kwargs["ask"] == m.ask


# ===========================================================================
# 4. Running daily max is scoped to the station's LOCAL calendar day
# ===========================================================================

# Los Angeles is the sharpest test of the historical UTC-day bug: UTC midnight
# falls at 17:00 local, so a UTC-day reset lands inside the heating window and
# a UTC-day window pulls in the previous local evening.
_LA = ZoneInfo("America/Los_Angeles")


def _metar_obs(local_dt, temp_c, station="KLAX"):
    """Synthetic METAR carrying an obsTime epoch and a matching T-group."""
    sign = "1" if temp_c < 0 else "0"
    tenths = f"{abs(int(round(temp_c * 10))):03d}"
    utc = local_dt.astimezone(timezone.utc)
    return {
        "icaoId": station,
        "obsTime": int(utc.timestamp()),
        "reportTime": utc.strftime("%Y-%m-%dT%H:00:00.000Z"),
        "temp": temp_c,
        "rawOb": f"METAR {station} AUTO RMK AO2 T{sign}{tenths}0100",
    }


def _straddling_observations():
    """Two observations whose UTC day agrees but whose LOCAL day does not.

    ``hot`` is 18:00 on the PREVIOUS local day, which is 01:00-02:00Z on the
    current local date -- so a UTC-day filter wrongly counts it.
    ``cool`` is 10:00 on the CURRENT local day.
    """
    local_today = datetime.now(_LA).date()
    hot_local = datetime.combine(
        local_today - timedelta(days=1), datetime.min.time(), tzinfo=_LA
    ).replace(hour=18)
    cool_local = datetime.combine(local_today, datetime.min.time(), tzinfo=_LA).replace(
        hour=10
    )

    hot = _metar_obs(hot_local, 35.0)  # 95.0 F -- yesterday local, today UTC
    cool = _metar_obs(cool_local, 20.0)  # 68.0 F -- today local
    # Sanity: the fixture only discriminates if both land on today's UTC date.
    utc_today = datetime.now(timezone.utc).date()
    assert (
        datetime.fromtimestamp(hot["obsTime"], tz=timezone.utc).date() == utc_today
    ), "fixture precondition: the hot observation must share today's UTC date"
    return [cool, hot]


class TestLocalCalendarDayMax:
    def test_metar_daily_max_uses_station_local_day_not_utc_day(self, monkeypatch):
        observations = _straddling_observations()
        provider = METARProvider(stations=["KLAX"])
        monkeypatch.setattr(provider, "_api_call", lambda *a, **k: list(observations))

        max_f = provider._get_daily_max_temp("KLAX")

        assert max_f == pytest.approx(68.0, abs=0.05), (
            "daily max must cover the station's LOCAL calendar day; got "
            f"{max_f} (95.0 would mean the previous local evening leaked in "
            "via a UTC-day filter)"
        )

    def test_metar_daily_max_requests_a_window_covering_local_midnight(self):
        provider = METARProvider(stations=["KLAX"])
        hours = provider._hours_covering_local_day(_LA)
        now_local = datetime.now(_LA)
        elapsed = now_local.hour + now_local.minute / 60.0
        assert hours >= elapsed, "history window must reach back to local midnight"
        assert hours <= METARProvider.MAX_DAILY_MAX_HOURS

    def test_metar_daily_max_declines_without_a_timezone(self, monkeypatch):
        """No tz -> no daily max, never a UTC-day approximation."""
        provider = METARProvider(stations=["KXXX"], station_timezones={})
        monkeypatch.setattr(provider, "_api_call", lambda *a, **k: [])
        assert provider._get_daily_max_temp("KXXX") is None

    def test_nws_daily_max_uses_station_local_day_not_utc_day(self):
        local_today = datetime.now(_LA).date()
        hot_local = datetime.combine(
            local_today - timedelta(days=1), datetime.min.time(), tzinfo=_LA
        ).replace(hour=18)
        cool_local = datetime.combine(
            local_today, datetime.min.time(), tzinfo=_LA
        ).replace(hour=10)

        features = [
            {
                "properties": {
                    "timestamp": cool_local.astimezone(timezone.utc).isoformat(),
                    "temperature": {"value": 20.0},
                }
            },
            {
                "properties": {
                    "timestamp": hot_local.astimezone(timezone.utc).isoformat(),
                    "temperature": {"value": 35.0},
                }
            },
        ]

        max_f = NWSProvider._max_from_features(features, _LA)
        assert max_f == pytest.approx(68.0, abs=0.05)

    def test_metar_marks_the_local_day_the_max_covers(self, monkeypatch):
        observations = _straddling_observations()
        provider = METARProvider(stations=["KLAX"])
        monkeypatch.setattr(provider, "_api_call", lambda *a, **k: list(observations))

        md = provider.fetch_latest("KLAX")

        assert md.extra["station_timezone"] == "America/Los_Angeles"
        assert md.extra["max_temp_local_day"] == datetime.now(_LA).date().isoformat()
        assert md.extra["settlement_station"] == "KLAX"


# ===========================================================================
# 5. Live cross-check vs the IEM archive (Phase 1 exit criterion 6)
# ===========================================================================
#
# WHAT ESTABLISHES IDENTITY, AND WHAT DOES NOT
# --------------------------------------------
# The previous version of this section asserted a temperature TOLERANCE BAND
# (-2.5 .. +1.0 F) between our observed daily max and the IEM archive, on the
# theory that reading the wrong station would breach it. A 2026-07-25 red-team
# measured that theory and it is false for Chicago:
#
#   KJFK vs KNYC  07-24 -4.02 BREACH | 07-23 -0.92 PASS | 07-22 +2.08 BREACH
#   KORD vs KMDW  07-24 -0.92 PASS   | 07-23 +0.00 PASS | 07-22 +0.94 PASS
#
# i.e. a regression pointing CHI at its airport proxy passed 3/3 days. The
# obvious repair -- "assert the series is CLOSER to the settlement station's
# IEM series than to the proxy's" -- was measured too, and is also blind
# (2026-07-25, last 3 complete local days):
#
#   correctly-configured KMDW feed: MAE 0.68 F vs IEM/MDW, 1.65 F vs IEM/ORD
#   REGRESSED       KORD feed     : MAE 0.62 F vs IEM/MDW, 0.99 F vs IEM/ORD
#
# The regressed feed is ALSO closer to the settlement series, because routine
# METAR sampling undershoots the ASOS full-stream max by about as much as
# O'Hare runs above Midway. A gate that passes in both worlds has no power,
# so asserting it would be a gate blind to its own target.
#
# Identity is therefore established by IDENTITY: the production METAR feed for
# the configured station must report that station's ICAO, that station's
# published name, and that station's physical site -- within 3 km of the IEM
# series we archive against and unambiguously far from the proxy's site. The
# margin is ~8x (0.0-0.3 km vs 23.4-24.4 km), and a proxy-pointed city fails
# it by construction -- proven live by
# ``test_a_proxy_pointed_feed_fails_the_identity_check``.
#
# The temperature comparison is kept, reported, and asserted ONLY on the side
# where it is physically derived rather than empirically calibrated:
#
#   OVERSHOOT (obs above IEM): our max is taken over a SUBSET of the reports
#     IEM summarises, so it cannot exceed the full-stream max except by
#     rounding -- T-group precision 0.1 C = 0.18 F plus IEM's whole-F
#     reporting 0.5 F = 0.68 F. Gate at 1.0 F.
#   UNDERSHOOT (obs below IEM): unbounded in principle -- a short peak between
#     hourly reports is simply not observed, and the AWC history window can
#     truncate the start of a local day. The old 2.5 F sat 0.46 F above a
#     single week's worst case (-2.04 F), which is a flake generator, not a
#     test. It is widened to a GROSS-FAILURE bound that catches a decode,
#     unit or feed error (a C-read-as-F would show tens of degrees) and is
#     documented as NOT an identity check.
#
# NOTE for Phase 1 FR-1.3: neither number is the settlement value. Kalshi
# settles on the NWS CLI product; station observations are only the running
# proxy. The settlement recorder owns CLI truth.
IEM_MAX_OVERSHOOT_F = 1.0  # derived: 0.18 F T-group + 0.5 F IEM rounding
IEM_MAX_UNDERSHOOT_F = 6.0  # gross decode/unit/feed failure, NOT identity

# Max distance between our observation site and the IEM series we compare it
# to. Measured 2026-07-25: 0.0 km (KNYC) / 0.3 km (KMDW) to the correct series
# vs 23.4 km / 24.4 km to the non-settlement airport series.
IEM_SITE_MATCH_KM = 3.0
# Minimum distance to the NON-settlement proxy's IEM site. This is the
# discriminative half: a feed pointed at the proxy sits ~0 km from it.
IEM_PROXY_MIN_KM = 10.0

# (city key, IEM id for the settlement station, IEM network, station-name
# fragment, non-settlement proxy ICAO, that proxy's IEM id). Station, series
# and timezone are read from the production registry, never re-listed here.
LIVE_CITIES = [
    ("NY", "NYC", "NY_ASOS", "Central Park", "KJFK", "JFK"),
    ("CHI", "MDW", "IL_ASOS", "Midway", "KORD", "ORD"),
]

_TGROUP_RE = re.compile(r"T(\d{8})")


def _requires_network():
    if os.getenv("MP_SKIP_NETWORK_TESTS"):
        pytest.skip("MP_SKIP_NETWORK_TESTS set")
    return pytest.importorskip("requests")


def _get(requests_mod, url, params, timeout=45):
    try:
        resp = requests_mod.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # offline / rate-limited / upstream outage
        pytest.skip(f"live source unavailable ({url}): {type(e).__name__}: {e}")


def _production_provider(station, tz_name):
    """A ``METARProvider`` wired exactly as ``WeatherBot.setup`` wires it."""
    return METARProvider(stations=[station], station_timezones={station: tz_name})


def _production_observations(provider, station, hours=3):
    """Raw observations through the PRODUCTION HTTP path, or skip if offline."""
    obs = provider._api_call(ids=station, hours=hours, retries=0)
    if not obs:
        pytest.skip(f"no live METAR for {station} (offline or upstream outage)")
    return obs


def _production_daily_max_by_local_day(provider, station, tz, hours=96):
    """{local date: max temp F} built from production decode + tz handling.

    Uses ``METARProvider._api_call`` (the production request), ``parse_tgroup``
    (the production 0.1 C decode) and ``METARProvider._observation_datetime``
    (the production timestamp rule) so this measures the pipeline rather than
    a re-implementation of it — the defect the red-team flagged in the old
    version, which called ``requests`` directly with hardcoded ICAOs and
    exercised no production code at all.
    """
    obs = provider._api_call(ids=station, hours=hours, retries=0)
    by_day = {}
    for o in obs or []:
        obs_dt = METARProvider._observation_datetime(o)
        if obs_dt is None:
            continue
        raw = o.get("rawOb", "") or ""
        tgroup = parse_tgroup(raw) if raw else None
        if tgroup is not None:
            temp_c = tgroup[0]
        elif o.get("temp") is not None:
            temp_c = float(o["temp"])
        else:
            continue
        day = obs_dt.astimezone(tz).date()
        by_day[day] = max(by_day.get(day, temp_c), temp_c)
    return {d: c * 9.0 / 5.0 + 32.0 for d, c in by_day.items()}


def _haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _iem_daily_max(requests_mod, station, network, sdate, edate):
    payload = _get(
        requests_mod,
        "https://mesonet.agron.iastate.edu/api/1/daily.json",
        {"station": station, "network": network, "sdate": sdate, "edate": edate},
    )
    out = {}
    for row in (payload or {}).get("data", []):
        if row.get("max_tmpf") is None:
            continue
        out[datetime.fromisoformat(row["date"]).date()] = float(row["max_tmpf"])
    return out


def _iem_site(requests_mod, network, iem_id):
    """(lon, lat) of an IEM station, or a skip if the network is unavailable."""
    geo = _get(
        requests_mod,
        f"https://mesonet.agron.iastate.edu/api/1/network/{network}.geojson",
        {},
    )
    match = next(
        (
            f
            for f in (geo or {}).get("features", [])
            if f.get("properties", {}).get("id") == iem_id
        ),
        None,
    )
    assert match is not None, f"IEM {network} has no station id {iem_id}"
    lon, lat = match["geometry"]["coordinates"][:2]
    return float(lon), float(lat)


def assert_feed_is_the_settlement_station(
    requests_mod, provider, station, iem_id, network, name_fragment, proxy_iem_id
):
    """THE discriminative identity check (Phase 1 exit criterion 6).

    Given a production ``METARProvider`` and the station it was configured
    with, prove the feed really is that settlement station:

    1. the observation's ICAO and published name are the settlement station's;
    2. its physical site is within :data:`IEM_SITE_MATCH_KM` of the IEM series
       we archive and reconcile against;
    3. and at least :data:`IEM_PROXY_MIN_KM` from the non-settlement proxy's
       IEM site — the half that gives the check power, since a proxy-pointed
       feed sits ~0 km from the proxy.

    Raises ``AssertionError`` on any failure; that is what
    ``test_a_proxy_pointed_feed_fails_the_identity_check`` exercises.
    """
    latest = _production_observations(provider, station)[0]

    assert latest.get("icaoId") == station, (
        f"feed for {station} returned observations from " f"{latest.get('icaoId')!r}"
    )
    assert name_fragment.lower() in (latest.get("name") or "").lower(), (
        f"{station} feed reports station name {latest.get('name')!r}; expected "
        f"the settlement station ({name_fragment})"
    )
    assert station in (latest.get("rawOb") or "")

    lon, lat = float(latest["lon"]), float(latest["lat"])
    iem_lon, iem_lat = _iem_site(requests_mod, network, iem_id)
    proxy_lon, proxy_lat = _iem_site(requests_mod, network, proxy_iem_id)

    km_settlement = _haversine_km(lon, lat, iem_lon, iem_lat)
    km_proxy = _haversine_km(lon, lat, proxy_lon, proxy_lat)

    assert km_settlement <= IEM_SITE_MATCH_KM, (
        f"the observation feed configured as {station} sits {km_settlement:.2f} km "
        f"from IEM {network}/{iem_id} (limit {IEM_SITE_MATCH_KM} km) and "
        f"{km_proxy:.2f} km from the non-settlement {network}/{proxy_iem_id} — "
        f"this feed is not the settlement station"
    )
    assert km_proxy >= IEM_PROXY_MIN_KM, (
        f"the observation feed configured as {station} sits only {km_proxy:.2f} km "
        f"from the non-settlement {network}/{proxy_iem_id}; identity is ambiguous"
    )
    return km_settlement, km_proxy


@pytest.mark.parametrize(
    "city_key,iem_id,network,name_fragment,proxy_icao,proxy_iem_id",
    LIVE_CITIES,
    ids=[c[0] for c in LIVE_CITIES],
)
class TestLiveSettlementStationCrossCheck:
    """Exit criterion 6: recorded observations match the settlement station.

    Every test here drives the production ``METARProvider`` built from the
    production ``CITY_CONFIG``; nothing hardcodes a station ID.
    """

    def test_production_feed_is_unambiguously_the_settlement_station(
        self, city_key, iem_id, network, name_fragment, proxy_icao, proxy_iem_id
    ):
        requests_mod = _requires_network()
        city = CITY_CONFIG[city_key]
        provider = _production_provider(city.settlement_station, city.timezone)

        km_settlement, km_proxy = assert_feed_is_the_settlement_station(
            requests_mod,
            provider,
            city.settlement_station,
            iem_id,
            network,
            name_fragment,
            proxy_iem_id,
        )
        # Report the margin so a drift toward ambiguity is visible before it
        # becomes a failure.
        assert km_proxy / max(km_settlement, 0.05) > 3.0, (
            f"{city.settlement_station}: {km_settlement:.2f} km to "
            f"{iem_id} vs {km_proxy:.2f} km to {proxy_iem_id} — margin too thin"
        )

    def test_a_proxy_pointed_feed_fails_the_identity_check(
        self, city_key, iem_id, network, name_fragment, proxy_icao, proxy_iem_id
    ):
        """POWER PROOF: the regression this criterion exists to catch.

        Configure the city's provider with its non-settlement airport — the
        exact defect FR-1.4 removed — and the identity check must fail. The
        old tolerance-band cross-check passed this 3/3 days for Chicago,
        which is why it was replaced.
        """
        requests_mod = _requires_network()
        city = CITY_CONFIG[city_key]
        regressed = _production_provider(proxy_icao, city.timezone)

        with pytest.raises(AssertionError) as exc:
            assert_feed_is_the_settlement_station(
                requests_mod,
                regressed,
                proxy_icao,
                iem_id,
                network,
                name_fragment,
                proxy_iem_id,
            )
        assert proxy_icao != city.settlement_station
        assert str(exc.value)

    def test_fetch_latest_reports_the_settlement_station_as_provenance(
        self, city_key, iem_id, network, name_fragment, proxy_icao, proxy_iem_id
    ):
        """The MarketData a strategy receives names the station it came from."""
        _requires_network()
        city = CITY_CONFIG[city_key]
        provider = _production_provider(city.settlement_station, city.timezone)
        _production_observations(provider, city.settlement_station)

        md = provider.fetch_latest(city.settlement_station)
        if md is None:
            pytest.skip(f"no live observation for {city.settlement_station}")
        assert md.symbol == city.settlement_station
        assert md.extra["settlement_station"] == city.settlement_station
        assert md.extra["station_timezone"] == city.timezone
        assert md.extra["source"] == "live_metar"
        assert city.settlement_station in (md.extra.get("metar_raw") or "")
        assert md.extra["settlement_station"] not in AIRPORT_PROXIES

    def test_daily_max_is_free_of_decode_or_unit_error(
        self, city_key, iem_id, network, name_fragment, proxy_icao, proxy_iem_id
    ):
        """Data-integrity band, NOT an identity test (see the note above).

        The OVERSHOOT side is derived: our max is taken over a subset of the
        reports IEM summarises, so it cannot exceed the full-stream max except
        by rounding (0.18 F + 0.5 F). The UNDERSHOOT side is a gross-failure
        bound only — station identity is established by
        ``test_production_feed_is_unambiguously_the_settlement_station``.
        """
        requests_mod = _requires_network()
        city = CITY_CONFIG[city_key]
        tz = ZoneInfo(city.timezone)
        provider = _production_provider(city.settlement_station, city.timezone)
        local_today = datetime.now(tz).date()
        wanted = [local_today - timedelta(days=n) for n in (1, 2, 3)]

        observed = _production_daily_max_by_local_day(
            provider, city.settlement_station, tz
        )
        archive = _iem_daily_max(
            requests_mod,
            iem_id,
            network,
            (local_today - timedelta(days=6)).isoformat(),
            local_today.isoformat(),
        )
        proxy_archive = _iem_daily_max(
            requests_mod,
            proxy_iem_id,
            network,
            (local_today - timedelta(days=6)).isoformat(),
            local_today.isoformat(),
        )

        compared, failures = [], []
        for day in wanted:
            if day not in observed or day not in archive:
                continue
            delta = observed[day] - archive[day]
            proxy_delta = (
                observed[day] - proxy_archive[day] if day in proxy_archive else None
            )
            compared.append((day, observed[day], archive[day], delta, proxy_delta))
            if not (-IEM_MAX_UNDERSHOOT_F <= delta <= IEM_MAX_OVERSHOOT_F):
                failures.append((day, delta))

        if len(compared) < 2:
            pytest.skip(
                f"{city.settlement_station}: only {len(compared)} of 3 days "
                f"available from both sources (upstream retention)"
            )

        report = "\n".join(
            f"  {d}  observed={o:.2f}F  IEM/{iem_id}={a:.1f}F  delta={dl:+.2f}F"
            + (
                f"  (vs IEM/{proxy_iem_id}: {pd:+.2f}F)"
                if pd is not None
                else "  (proxy series unavailable)"
            )
            for d, o, a, dl, pd in compared
        )
        assert not failures, (
            f"{city.settlement_station} daily max is outside the "
            f"[-{IEM_MAX_UNDERSHOOT_F}, +{IEM_MAX_OVERSHOOT_F}] F integrity "
            f"band — suspect a decode, unit or feed error, not a wrong "
            f"station:\n{report}"
        )


def test_the_tolerance_band_is_documented_as_non_discriminative():
    """Pin the reclassification so it cannot silently revert to a gate.

    The band must stay loose enough on the undershoot side that it is honestly
    a data-integrity check, and the discriminative work must stay with the
    geographic identity check. If someone tightens it back toward the observed
    worst case (-2.04 F measured 2026-07-21..25) they reintroduce a gate that
    flakes and still cannot catch a proxy-pointed city.
    """
    assert IEM_MAX_UNDERSHOOT_F >= 4.0, (
        "IEM_MAX_UNDERSHOOT_F has been tightened back toward the observed "
        "worst case; that band was measured to catch 0/3 KORD days"
    )
    assert IEM_MAX_OVERSHOOT_F <= 1.0  # derived, may only be tightened
    assert (
        IEM_PROXY_MIN_KM >= 3.0 * IEM_SITE_MATCH_KM
    ), "the identity check's discriminative margin has been eroded"


# ===========================================================================
# 6. The NWS_STATION_ID override cannot smuggle a non-settlement station in
# ===========================================================================
#
# The defect this section closes (measured 2026-07-25):
#
#   scripts/simulate.py:63  station = os.getenv("NWS_STATION_ID", <default>)
#   $ grep -n NWS .env                         -> NWS_STATION_ID=KJFK
#   $ gcloud ... "grep -i NWS_STATION_ID .env" -> NWS_STATION_ID=KJFK  (LIVE VM)
#   docs/gcloud_vm_deploy.md:143               -> NWS_STATION_ID=KJFK
#
# so `simulate.py --bot weather --live` built NWSProvider(ua, "KJFK") and fed
# WeatherArbitrageStrategyV2 a non-settlement microclimate. The pre-existing
# guard only asserted the literal DEFAULT and was blind to the override.
#
# `.env` is gitignored on both the workstation and the VM, so no commit can
# fix it. The code therefore refuses the value outright
# (abort-on-missing-critical-input) rather than observing the wrong station.

from scripts.simulate import (  # noqa: E402
    NonSettlementStationError,
    STATION_ENV_VAR,
    resolve_settlement_station,
)


class TestStationOverrideIsValidated:
    def test_default_is_the_ny_settlement_station(self):
        assert (
            resolve_settlement_station(env={}) == CITY_CONFIG["NY"].settlement_station
        )
        assert resolve_settlement_station(env={}) == "KNYC"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_override_falls_back_to_the_settlement_default(self, blank):
        env = {} if blank is None else {STATION_ENV_VAR: blank}
        assert resolve_settlement_station(env=env) == "KNYC"

    @pytest.mark.parametrize("station", sorted(SETTLEMENT_STATIONS))
    def test_settlement_stations_are_accepted(self, station):
        assert resolve_settlement_station(env={STATION_ENV_VAR: station}) == station
        # Case and whitespace are normalised, not rejected.
        assert (
            resolve_settlement_station(env={STATION_ENV_VAR: f" {station.lower()} "})
            == station
        )

    @pytest.mark.parametrize("proxy", AIRPORT_PROXIES)
    def test_airport_proxy_override_aborts(self, proxy):
        """THE regression: the value that is actually in the VM's .env."""
        with pytest.raises(NonSettlementStationError) as exc:
            resolve_settlement_station(env={STATION_ENV_VAR: proxy})
        message = str(exc.value)
        assert proxy in message
        # The error must tell the operator what to do, not just what is wrong.
        assert STATION_ENV_VAR in message
        for station in SETTLEMENT_STATIONS:
            assert station in message

    @pytest.mark.parametrize(
        "station", ["KEWR", "KLGA", "KMIA2", "knyc-1", "KDFW", "garbage", "K"]
    )
    def test_any_other_station_aborts(self, station):
        with pytest.raises(NonSettlementStationError):
            resolve_settlement_station(env={STATION_ENV_VAR: station})

    def test_live_simulation_never_builds_a_provider_for_a_proxy(self, monkeypatch):
        """End-to-end on the real code path, not on the source text.

        ``simulate.py --bot weather --live`` is a strategy-input path: the
        NWSProvider it constructs feeds ``WeatherArbitrageStrategyV2``. With
        the proxy in the environment the run must abort BEFORE any provider is
        constructed.
        """
        import scripts.simulate as sim

        built = []

        class _SpyNWSProvider:
            def __init__(self, user_agent, station=None, *a, **k):
                built.append(station)

        monkeypatch.setattr(
            "src.data.nws_provider.NWSProvider", _SpyNWSProvider, raising=True
        )
        monkeypatch.setenv(STATION_ENV_VAR, "KJFK")
        # ``load_dotenv`` must not put the file's value back over the monkeypatch.
        monkeypatch.setattr(sim, "load_dotenv", lambda *a, **k: None)

        with pytest.raises(NonSettlementStationError):
            sim.run_simulation("weather", days=1, optimize=False, use_live=True)
        assert built == [], (
            f"a provider was constructed for {built} before the abort — the "
            f"wrong station already reached a strategy input"
        )

    def test_mock_simulation_uses_a_settlement_station(self):
        """The offline path is a strategy input too (it runs a real strategy)."""
        from src.data.mock_providers import _DEFAULT_STATIONS, MockNWSProvider

        assert MockNWSProvider().stations == ["KNYC"]
        assert not set(_DEFAULT_STATIONS) & set(AIRPORT_PROXIES)

    def test_the_deploy_doc_does_not_prescribe_a_proxy(self):
        """docs/gcloud_vm_deploy.md was the source of the VM's bad value."""
        path = os.path.join(REPO_ROOT, "docs", "gcloud_vm_deploy.md")
        for i, line in enumerate(
            io.open(path, encoding="utf-8").read().splitlines(), 1
        ):
            stripped = line.strip().lstrip("#").strip()
            if stripped.startswith("NWS_STATION_ID="):
                value = stripped.split("=", 1)[1].strip()
                assert value in SETTLEMENT_STATIONS, (
                    f"docs/gcloud_vm_deploy.md:{i} tells the operator to set "
                    f"NWS_STATION_ID={value}, which is not a settlement station"
                )

    def test_the_env_template_does_not_prescribe_a_proxy(self):
        path = os.path.join(REPO_ROOT, ".env.example")
        if not os.path.exists(path):
            pytest.skip("no .env.example in this checkout")
        for i, line in enumerate(
            io.open(path, encoding="utf-8").read().splitlines(), 1
        ):
            stripped = line.strip().lstrip("#").strip()
            if stripped.startswith("NWS_STATION_ID="):
                value = stripped.split("=", 1)[1].strip()
                assert (
                    value in SETTLEMENT_STATIONS
                ), f".env.example:{i} prescribes NWS_STATION_ID={value}"
