"""Tests for METARProvider — aviation weather data provider.

Covers: T-group parsing (0.1C precision), temperature conversion,
daily max calculation, MarketData output structure, and fallback behavior.
All tests mock the HTTP API — no real network calls.
"""

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

import pytest

from src.data.metar_provider import METARProvider
from src.core.interfaces import MarketData


# ---------------------------------------------------------------------------
# Fixtures / Mock Data
# ---------------------------------------------------------------------------

# PRD FR-1.4: fixtures are keyed to KNYC (Central Park) — the station Kalshi
# settles KXHIGHNY on — not the former KJFK airport proxy.
#
# METARProvider._get_daily_max_temp filters observations by the STATION'S
# local calendar day, so daily-max fixtures are stamped with today's date in
# America/New_York. UTC hours 04:00-23:59 on that date always land on the same
# calendar date in New York (UTC-4/-5), which keeps the fixtures stable at any
# wall-clock time the suite runs.
_TODAY_LOCAL = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

MOCK_METAR_RESPONSE = [
    {
        "icaoId": "KNYC",
        "reportTime": "2026-04-05T15:53:00Z",
        "temp": 27,
        "dewp": 6,
        "wdir": 310,
        "wspd": 8,
        "visib": 10,
        "altim": 30.02,
        "rawOb": "METAR KNYC 051553Z 31008KT 10SM FEW250 27/06 A3002 RMK AO2 SLP167 T02720056",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    }
]

MOCK_METAR_NEGATIVE_TEMPS = [
    {
        "icaoId": "KNYC",
        "reportTime": "2026-01-15T08:00:00Z",
        "temp": -3,
        "dewp": -2,
        "wdir": 20,
        "wspd": 12,
        "visib": 10,
        "altim": 29.85,
        "rawOb": "METAR KNYC 150800Z 02012KT 10SM OVC020 M03/M02 A2985 RMK AO2 SLP108 T10281017",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    }
]

MOCK_METAR_SMALL_POSITIVE = [
    {
        "icaoId": "KNYC",
        "reportTime": "2026-03-10T06:00:00Z",
        "temp": 6,
        "dewp": 1,
        "wdir": 180,
        "wspd": 5,
        "visib": 10,
        "altim": 30.10,
        "rawOb": "METAR KNYC 100600Z 18005KT 10SM CLR 06/01 A3010 RMK AO2 SLP195 T00560012",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    }
]

MOCK_METAR_MIXED_SIGN = [
    {
        "icaoId": "KNYC",
        "reportTime": "2026-04-01T12:00:00Z",
        "temp": 30,
        "dewp": -3,
        "wdir": 270,
        "wspd": 15,
        "visib": 10,
        "altim": 29.92,
        "rawOb": "METAR KNYC 011200Z 27015KT 10SM SCT200 30/M03 A2992 RMK AO2 SLP132 T03001025",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    }
]

MOCK_METAR_NO_TGROUP = [
    {
        "icaoId": "KNYC",
        "reportTime": "2026-04-05T15:53:00Z",
        "temp": 22,
        "dewp": 10,
        "wdir": 310,
        "wspd": 8,
        "visib": 10,
        "altim": 30.02,
        "rawOb": "METAR KNYC 051553Z 31008KT 10SM FEW250 22/10 A3002 RMK AO2 SLP167",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    }
]

# Multiple METARs from the same day for daily-max testing
MOCK_METAR_MULTIPLE = [
    {
        "icaoId": "KNYC",
        "reportTime": f"{_TODAY_LOCAL}T08:00:00Z",
        "temp": 15,
        "dewp": 8,
        "wdir": 180,
        "wspd": 5,
        "visib": 10,
        "altim": 30.00,
        "rawOb": "METAR KNYC 050800Z 18005KT 10SM CLR 15/08 A3000 RMK AO2 SLP160 T01500080",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    },
    {
        "icaoId": "KNYC",
        "reportTime": f"{_TODAY_LOCAL}T12:00:00Z",
        "temp": 22,
        "dewp": 10,
        "wdir": 200,
        "wspd": 8,
        "visib": 10,
        "altim": 30.01,
        "rawOb": "METAR KNYC 051200Z 20008KT 10SM FEW100 22/10 A3001 RMK AO2 SLP165 T02200100",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    },
    {
        "icaoId": "KNYC",
        "reportTime": f"{_TODAY_LOCAL}T15:53:00Z",
        "temp": 27,
        "dewp": 6,
        "wdir": 310,
        "wspd": 8,
        "visib": 10,
        "altim": 30.02,
        "rawOb": "METAR KNYC 051553Z 31008KT 10SM FEW250 27/06 A3002 RMK AO2 SLP167 T02720056",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    },
    {
        "icaoId": "KNYC",
        "reportTime": f"{_TODAY_LOCAL}T18:00:00Z",
        "temp": 25,
        "dewp": 7,
        "wdir": 290,
        "wspd": 10,
        "visib": 10,
        "altim": 30.03,
        "rawOb": "METAR KNYC 051800Z 29010KT 10SM FEW250 25/07 A3003 RMK AO2 SLP170 T02500070",
        "wxString": "",
        "lat": 40.779,
        "lon": -73.9692,
    },
]


def _mock_response(json_data, status_code=200):
    """Build a mock requests.Response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status.return_value = None
    if status_code >= 400:
        import requests as req

        mock_resp.raise_for_status.side_effect = req.exceptions.HTTPError(
            f"HTTP {status_code}"
        )
    return mock_resp


# ===========================================================================
# T-group Parsing (0.1 C precision from METAR remarks)
# ===========================================================================


class TestTGroupParsing:
    """T-group gives temperature to 0.1C: T{sign}{3digits}{sign}{3digits}."""

    @patch("src.data.metar_provider.requests.get")
    def test_tgroup_positive_both(self, mock_get):
        """T02720056 -> temp=27.2C, dewpoint=5.6C"""
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is not None
        assert md.extra["temperature_c"] == pytest.approx(27.2, abs=0.01)

    @patch("src.data.metar_provider.requests.get")
    def test_tgroup_negative_both(self, mock_get):
        """T10281017 -> temp=-2.8C, dewpoint=-1.7C"""
        mock_get.return_value = _mock_response(MOCK_METAR_NEGATIVE_TEMPS)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is not None
        assert md.extra["temperature_c"] == pytest.approx(-2.8, abs=0.01)

    @patch("src.data.metar_provider.requests.get")
    def test_tgroup_small_positive(self, mock_get):
        """T00560012 -> temp=5.6C, dewpoint=1.2C"""
        mock_get.return_value = _mock_response(MOCK_METAR_SMALL_POSITIVE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is not None
        assert md.extra["temperature_c"] == pytest.approx(5.6, abs=0.01)

    @patch("src.data.metar_provider.requests.get")
    def test_tgroup_mixed_sign(self, mock_get):
        """T03001025 -> temp=30.0C, dewpoint=-2.5C"""
        mock_get.return_value = _mock_response(MOCK_METAR_MIXED_SIGN)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is not None
        assert md.extra["temperature_c"] == pytest.approx(30.0, abs=0.01)

    @patch("src.data.metar_provider.requests.get")
    def test_no_tgroup_falls_back_to_whole_degree(self, mock_get):
        """When T-group absent, use the integer `temp` field from API JSON."""
        mock_get.return_value = _mock_response(MOCK_METAR_NO_TGROUP)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is not None
        # Should fall back to whole-degree temp=22
        assert md.extra["temperature_c"] == pytest.approx(22.0, abs=0.01)
        assert md.extra["temp_precision"] == "whole"


# ===========================================================================
# Temperature Conversion (C -> F)
# ===========================================================================


class TestTemperatureConversion:
    @patch("src.data.metar_provider.requests.get")
    def test_27_2c_to_80_96f(self, mock_get):
        """27.2C -> 80.96F"""
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md.extra["temperature_f"] == pytest.approx(80.96, abs=0.01)

    @patch("src.data.metar_provider.requests.get")
    def test_negative_2_8c_to_26_96f(self, mock_get):
        """-2.8C -> 26.96F"""
        mock_get.return_value = _mock_response(MOCK_METAR_NEGATIVE_TEMPS)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md.extra["temperature_f"] == pytest.approx(26.96, abs=0.01)

    @patch("src.data.metar_provider.requests.get")
    def test_0c_to_32f(self, mock_get):
        """0.0C -> 32.0F — edge case at freezing."""
        # Craft a METAR with T00000000 (0.0C temp and dewpoint)
        zero_metar = [
            {
                "icaoId": "KNYC",
                "reportTime": "2026-02-01T12:00:00Z",
                "temp": 0,
                "dewp": 0,
                "wdir": 0,
                "wspd": 0,
                "visib": 10,
                "altim": 29.92,
                "rawOb": "METAR KNYC 011200Z 00000KT 10SM OVC010 00/00 A2992 RMK AO2 SLP132 T00000000",
                "wxString": "",
                "lat": 40.779,
                "lon": -73.9692,
            }
        ]
        mock_get.return_value = _mock_response(zero_metar)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md.extra["temperature_f"] == pytest.approx(32.0, abs=0.01)
        assert md.extra["temperature_c"] == pytest.approx(0.0, abs=0.01)


# ===========================================================================
# MarketData Output Structure
# ===========================================================================


class TestMarketDataStructure:
    @patch("src.data.metar_provider.requests.get")
    def test_returns_market_data_instance(self, mock_get):
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert isinstance(md, MarketData)
        assert md.symbol == "KNYC"

    @patch("src.data.metar_provider.requests.get")
    def test_extra_dict_has_required_keys(self, mock_get):
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        extra = md.extra
        assert extra is not None

        # Temperature fields
        assert "temperature_f" in extra
        assert "temperature_c" in extra
        assert "max_temp_today_f" in extra

        # Metadata fields
        assert "description" in extra
        assert "source" in extra
        assert "metar_raw" in extra
        assert "observation_time" in extra
        assert "metar_age_seconds" in extra
        assert "speci_count" in extra
        assert "temp_precision" in extra

    @patch("src.data.metar_provider.requests.get")
    def test_source_is_live_metar(self, mock_get):
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md.extra["source"] == "live_metar"

    @patch("src.data.metar_provider.requests.get")
    def test_forecast_is_none(self, mock_get):
        """METAR provides observations only, no forecast."""
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md.extra.get("forecast") is None

    @patch("src.data.metar_provider.requests.get")
    def test_tgroup_precision_marker(self, mock_get):
        """When T-group is present, temp_precision should indicate high precision."""
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md.extra["temp_precision"] == "tenths"

    @patch("src.data.metar_provider.requests.get")
    def test_metar_raw_contains_raw_observation(self, mock_get):
        mock_get.return_value = _mock_response(MOCK_METAR_RESPONSE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert "T02720056" in md.extra["metar_raw"]


# ===========================================================================
# Daily Max Calculation
# ===========================================================================


class TestDailyMax:
    @patch("src.data.metar_provider.requests.get")
    def test_daily_max_from_multiple_metars(self, mock_get):
        """With multiple METARs, max_temp_today_f should be the highest."""
        mock_get.return_value = _mock_response(MOCK_METAR_MULTIPLE)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is not None
        # The hottest METAR has T02720056 -> 27.2C -> 80.96F
        assert md.extra["max_temp_today_f"] == pytest.approx(80.96, abs=0.01)


# ===========================================================================
# Fallback / Error Behavior
# ===========================================================================


class TestFallbackBehavior:
    @patch("src.data.metar_provider.requests.get")
    def test_empty_response_returns_none(self, mock_get):
        """API returns empty list -> fetch_latest returns None."""
        mock_get.return_value = _mock_response([])
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is None

    @patch("src.data.metar_provider.requests.get")
    def test_http_error_returns_none(self, mock_get):
        """API returns 500 -> fetch_latest returns None."""
        mock_get.return_value = _mock_response([], status_code=500)
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is None

    @patch("src.data.metar_provider.requests.get")
    def test_network_exception_returns_none(self, mock_get):
        """requests.get raises an exception -> fetch_latest returns None."""
        import requests as req

        mock_get.side_effect = req.exceptions.ConnectionError("Network unreachable")
        provider = METARProvider(stations=["KNYC"])
        md = provider.fetch_latest("KNYC")

        assert md is None
