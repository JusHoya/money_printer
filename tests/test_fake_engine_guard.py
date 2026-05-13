"""Defense-in-depth tests for the FakeEngine startup invariant.

Background: on 2026-04-16 the trading system silently fell into a
"FakeEngine fallback" mode and ran for ~2 weeks against synthetic
KX-TEST-* / KX-LOSE-* contracts with no operator-visible alert.

These tests cover three layers:

1. Provider — `is_test_symbol` recognises known synthetic prefixes and
   `search_markets` filters them out (with a WARNING log).
2. Orchestrator startup — if discovery returns only test symbols (or
   nothing), the orchestrator halts via SystemExit.
3. Orchestrator per-cycle — the same check still passes when real
   symbols are present alongside test ones.
"""

import logging
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.kalshi_provider import (
    TEST_SYMBOL_PATTERNS,
    is_test_symbol,
)


class TestIsTestSymbol(unittest.TestCase):
    def test_is_test_symbol_recognizes_known_patterns(self):
        self.assertTrue(is_test_symbol("KX-TEST-50000"))
        self.assertTrue(is_test_symbol("KX-LOSE-1"))
        self.assertTrue(is_test_symbol("KX-FAKE-abc"))

    def test_is_test_symbol_passes_real_symbols(self):
        self.assertFalse(is_test_symbol("KXBTCD-26APR1523"))
        self.assertFalse(is_test_symbol("KXHIGHNY-26MAR19-T80"))
        self.assertFalse(is_test_symbol("KXBTC15M-26APR291245-B65000"))
        self.assertFalse(is_test_symbol(""))
        self.assertFalse(is_test_symbol(None))

    def test_patterns_tuple_contains_expected_prefixes(self):
        for prefix in ("KX-TEST", "KX-LOSE", "KX-FAKE"):
            self.assertIn(prefix, TEST_SYMBOL_PATTERNS)


class TestProviderFilters(unittest.TestCase):
    """Layer 1: the provider must drop test symbols and log a WARNING."""

    def test_provider_filters_test_symbols_from_discovery(self):
        from src.data.kalshi_provider import KalshiProvider

        provider = KalshiProvider.__new__(KalshiProvider)
        provider.api_url = "https://example.invalid"
        provider.anonymous = True
        provider.key_id = None

        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "markets": [
                {"ticker": "KXBTCD-26APR1523"},
                {"ticker": "KX-TEST-50000"},
                {"ticker": "KX-LOSE-1"},
                {"ticker": "KXHIGHNY-26MAR19-T80"},
            ],
            "cursor": "",
        }

        provider.session = mock.Mock()
        provider.session.get.return_value = fake_resp

        with self.assertLogs("MoneyPrinter", level="WARNING") as caplog:
            markets, cursor = provider.search_markets()

        tickers = [m["ticker"] for m in markets]
        self.assertEqual(tickers, ["KXBTCD-26APR1523", "KXHIGHNY-26MAR19-T80"])
        self.assertTrue(
            any("test/fixture" in rec.getMessage() for rec in caplog.records),
            f"Expected a 'test/fixture' WARNING, got: {[r.getMessage() for r in caplog.records]}",
        )


def _make_minimal_orchestrator(kalshi_mock):
    """Construct an OrchestratorEngine without running its real __init__.

    Provides only the attributes referenced by `_check_fake_engine` and
    the surrounding control flow. Avoids the heavy RiskManager / bot
    setup that the real ctor performs.
    """
    from scripts.run_dashboard import OrchestratorEngine

    eng = OrchestratorEngine.__new__(OrchestratorEngine)
    eng.kalshi = kalshi_mock
    eng.dashboard = types.SimpleNamespace(alert=lambda *_a, **_kw: None)
    eng.running = True
    return eng


class TestOrchestratorGuard(unittest.TestCase):
    """Layers 2 & 3: orchestrator must halt when discovery is fixture-only."""

    def test_orchestrator_halts_when_all_symbols_test(self):
        kalshi = mock.Mock()
        kalshi.search_markets.return_value = (
            [
                {"ticker": "KX-TEST-50000"},
                {"ticker": "KX-LOSE-1"},
                {"ticker": "KX-FAKE-9"},
            ],
            None,
        )
        eng = _make_minimal_orchestrator(kalshi)

        with self.assertLogs("MoneyPrinter", level="ERROR") as caplog:
            ok = eng._check_fake_engine(context="startup")

        self.assertFalse(ok)
        self.assertTrue(
            any("FakeEngine detected" in rec.getMessage() for rec in caplog.records),
            f"Expected FakeEngine ERROR, got: {[r.getMessage() for r in caplog.records]}",
        )

    def test_orchestrator_halts_when_discovery_empty(self):
        kalshi = mock.Mock()
        kalshi.search_markets.return_value = ([], None)
        eng = _make_minimal_orchestrator(kalshi)

        with self.assertLogs("MoneyPrinter", level="ERROR"):
            ok = eng._check_fake_engine(context="startup")

        self.assertFalse(ok)

    def test_orchestrator_proceeds_when_real_symbols_present(self):
        kalshi = mock.Mock()
        kalshi.search_markets.return_value = (
            [
                {"ticker": "KXBTCD-26APR1523"},
                {"ticker": "KX-TEST-50000"},
                {"ticker": "KXHIGHNY-26MAR19-T80"},
            ],
            None,
        )
        eng = _make_minimal_orchestrator(kalshi)

        # Even with a stray test symbol mixed in, the guard must allow startup.
        # It should still emit a WARNING about the leak.
        logger = logging.getLogger("MoneyPrinter")
        prev_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            with self.assertLogs("MoneyPrinter", level="WARNING") as caplog:
                ok = eng._check_fake_engine(context="recheck")
        finally:
            logger.setLevel(prev_level)

        self.assertTrue(ok)
        self.assertTrue(
            any("test symbol" in rec.getMessage() for rec in caplog.records),
            f"Expected leak WARNING, got: {[r.getMessage() for r in caplog.records]}",
        )

    def test_orchestrator_passes_when_kalshi_not_configured(self):
        eng = _make_minimal_orchestrator(kalshi_mock=None)
        self.assertTrue(eng._check_fake_engine(context="startup"))


if __name__ == "__main__":
    unittest.main()
