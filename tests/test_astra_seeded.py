"""Economic and failure-case checks for the offline seed methods; stdlib only."""
import copy
import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from src.astra_seeded.methods import (
    Book, acquisition_cost, brier_targets, rebalance_delta, scan_baskets, timestamp,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/astra_seeded/synthetic.json"


class SeedMethodsTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def scan(self, data=None, **kwargs):
        d = data or self.data
        return scan_baskets(d["certificate"], [Book.from_dict(b) for b in d["books"]],
                            as_of=timestamp(d["as_of"]), cash_available="300", **kwargs)

    def test_brier_matches_gradient_difference_without_spread(self):
        self.assertEqual(brier_targets(["0.6", "0.4"], ["0.5"] * 2,
                                      ["0.5"] * 2, 10), (Decimal(2), Decimal(-2)))

    def test_spread_reduces_target_and_inside_spread_does_not_bet(self):
        self.assertEqual(brier_targets(["0.6", "0.4"], ["0.49"] * 2,
                                      ["0.51"] * 2, 10), (Decimal("1.8"), Decimal("-1.8")))
        self.assertEqual(brier_targets(["0.5"] * 2, ["0.49"] * 2,
                                      ["0.51"] * 2, 10), (Decimal(0), Decimal(0)))

    def test_rebalance_uses_actual_fills_and_pending_orders(self):
        self.assertEqual(rebalance_delta([3, -3], [1, -1], [2, -2]), (0, 0))
        self.assertEqual(rebalance_delta([0, 0], [2, -2], [0, 0]), (-2, 2))

    def test_bad_probability_and_crossed_band_rejected(self):
        for p in (["NaN", 0], ["Infinity", 0], [True, 0], ["1.1", "-0.1"], ["0.7", "0.7"]):
            with self.subTest(p=p), self.assertRaises(ValueError):
                brier_targets(p, ["0.4"] * 2, ["0.6"] * 2, 10)
        with self.assertRaises(ValueError):
            brier_targets(["0.5"] * 2, ["0.7"] * 2, ["0.6"] * 2, 10)

    def test_ask_is_opposing_bid_and_depth_is_not_invented(self):
        book = Book.from_dict(self.data["books"][0])
        self.assertEqual(book.asks("yes")[0].price, Decimal("0.30"))
        self.assertEqual(acquisition_cost(book, "yes", 5), Decimal("1.58"))
        self.assertIsNone(acquisition_cost(book, "yes", 6))
        self.assertTrue(all(r["contracts_per_leg"] <= 5 for r in self.scan()))

    def test_depth_walk_charges_worse_prices(self):
        row = self.data["books"][0]
        row["no_bids"] = [["0.70", "1"], ["0.60", "1"]]
        book = Book.from_dict(row)
        self.assertEqual(acquisition_cost(book, "yes", 2), Decimal("0.74"))

    def test_subcent_notional_is_aligned_in_all_in_cost(self):
        row = self.data["books"][0]
        row["no_bids"] = [["0.6667", "1"]]
        self.assertEqual(acquisition_cost(Book.from_dict(row), "yes", 1), Decimal("0.35"))

    def test_fees_can_erase_apparent_arbitrage(self):
        for b in self.data["books"]:
            b["no_bids"] = [["0.67", "5"]]
        self.assertEqual(self.scan(adverse_per_leg="0"), [])

    def test_no_incentives_or_fee_discount_assumed(self):
        baseline = self.scan()
        for b in self.data["books"]:
            b["taker_multiplier"] = "0"
        free = self.scan()
        self.assertGreater(max(r["conditional_net_surplus"] for r in free),
                           max(r["conditional_net_surplus"] for r in baseline))
        del self.data["books"][0]["taker_multiplier"]
        with self.assertRaises(KeyError):
            self.scan()

    def test_no_basket_payout_in_every_possible_ordinary_resolution(self):
        for b in self.data["books"]:
            b["yes_bids"], b["no_bids"] = [["0.40", "5"]], [["0.59", "5"]]
        candidates = self.scan()
        self.assertTrue(candidates)
        self.assertTrue(all(r["side"] == "no" for r in candidates))
        for r in candidates:
            for winner in range(3):
                payout = sum(int(i != winner) * r["contracts_per_leg"] for i in range(3))
                self.assertEqual(payout, r["conditional_settlement_payout"])
                self.assertGreater(payout - r["cash_required"], 0)

    def test_yes_basket_payout_and_surplus_in_every_resolution(self):
        candidates = self.scan()
        self.assertTrue(candidates)
        for r in candidates:
            self.assertEqual(r["side"], "yes")
            for winner in range(3):
                payout = sum(int(i == winner) * r["contracts_per_leg"] for i in range(3))
                self.assertEqual(payout - r["cash_required"], r["conditional_net_surplus"])

    def test_cash_limit_includes_fees_and_adverse_allowance(self):
        d = self.data
        result = scan_baskets(d["certificate"], [Book.from_dict(b) for b in d["books"]],
                              as_of=timestamp(d["as_of"]), cash_available="0.98")
        self.assertEqual(result, [])

    def test_missing_outcome_or_bad_certificate_is_refused(self):
        for key in ("exhaustive", "rules_reviewed", "ordinary_binary", "mutually_exclusive"):
            d = copy.deepcopy(self.data)
            d["certificate"][key] = "true"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.scan(d)
        self.data["books"].pop()
        with self.assertRaises(ValueError):
            self.scan()

    def test_duplicate_markets_are_refused(self):
        self.data["books"][1] = self.data["books"][0]
        with self.assertRaises(ValueError):
            self.scan()

    def test_stale_future_and_asynchronous_books_are_refused(self):
        for stamp in ("2026-09-06T17:59:57Z", "2026-09-06T18:00:01Z", "2026-09-06T17:59:58Z"):
            d = copy.deepcopy(self.data)
            d["books"][0]["observed_at"] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.scan(d)

    def test_empty_books_and_invalid_levels(self):
        self.data["books"][0]["no_bids"] = []
        self.assertEqual(self.scan(), [])
        for level in (["0", "1"], ["0.7", "0"], ["0.7", "NaN"]):
            self.data["books"][0]["no_bids"] = [level]
            with self.subTest(level=level), self.assertRaises(ValueError):
                self.scan()

    def test_cli_runs_without_credentials_and_labels_output(self):
        p = subprocess.run([sys.executable, str(ROOT / "scripts/astra_seeded.py"), str(FIXTURE)],
                           cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertIs(out["profitability_verified"], False)
        self.assertEqual(out["brier_coordinate_targets"], ["2.80", "-2.80"])

    def test_cli_refuses_legacy_or_sealed_input_before_read(self):
        for rel in ("data/ladders_holdout/DO_NOT_READ.json", "data/astra_seeded/sealed/DO_NOT_READ.json"):
            p = subprocess.run([sys.executable, str(ROOT / "scripts/astra_seeded.py"), rel],
                               cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 2)
            self.assertIn("allowed Astra development root", p.stderr)


if __name__ == "__main__":
    unittest.main()
