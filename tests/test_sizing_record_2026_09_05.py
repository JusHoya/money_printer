"""Regression tests for how the 2026-09-05 cold-start sizing finding is RECORDED.

Source of truth: ``reports/factory/sizing_cold_start_2026-09-05.md``.

WHY THIS FILE EXISTS
--------------------
The finding is easy to write down wrongly, and every wrong version points the
reader at the same bad action -- loosening the risk gauntlet. The memo's
conclusion is the opposite: ``calculate_kelly_size`` behaved exactly as
designed, and the factory promoted a shape the sandbox cannot express. The
record therefore has to keep four things straight, and each is asserted below:

1. **It is a PROMOTION defect, not a risk-manager defect.** The frame folds the
   mixin's EV gate into ``sandbox_admissible`` (which passed 130/130 and never
   binds) and does not model ``calculate_kelly_size > 0`` (which zeroes 117 of
   those 130). Options that edit ``src/core/risk_manager.py`` are REJECTED by
   owner decision, 2026-09-05.
2. **The maia shadow run is instrumentation, not a gate run.** It has already
   produced its finding (the ``KELLY_ZERO`` evidence). It books nothing, so it
   accrues ZERO settled ``target_date`` units toward FR-5.2's >= 50.
3. **The corrected numbers.** ``+0.0705/contract`` was the REJECTED subset -- the
   117 trades the runtime cannot size -- not the full set. The full set is
   ``+0.0750`` per-trade and ``+0.0723`` date-clustered. Under the corrected gate
   null the full genome clears n=50 in-sample at p=0.047 against alpha 0.05,
   with a unit-win-rate CI of [0.667, 0.875] against a null of 0.683.
4. **The holdout-B clock.** ``data/ladders_holdout/`` expires ~2026-10-03 and is
   the only virgin root the project gets. It is the one decision here with a hard
   external deadline and must be stated plainly, not left implicit.

These are documentation assertions on purpose. The finding is a governance fact,
not a runtime behaviour -- nothing under ``src/`` changed and nothing should have
(``risk_manager.py``, ``mixins.py`` and ``matching_engine.py`` stay protected).
What can regress is the *record*, silently, in a later edit. That is what this
pins.

An arithmetic check is included so the quoted statistics are not merely copied:
the Wilson interval and the cold-start ceiling are re-derived here from scratch.
"""

import io
import math
import os
import re
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

HANDOFF = os.path.join(REPO, "HANDOFF.md")
PRD = os.path.join(REPO, "PRD_STRATEGY_FACTORY.md")
ROADMAP = os.path.join(REPO, "docs", "factory", "FACTORY_ROADMAP.md")
MEMO = os.path.join(REPO, "reports", "factory", "sizing_cold_start_2026-09-05.md")

MEMO_REL = "reports/factory/sizing_cold_start_2026-09-05.md"


def read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def norm(text):
    """Collapse whitespace so assertions survive line re-wrapping."""
    return re.sub(r"\s+", " ", text)


class TestMemoIsTheSourceOfTruth(unittest.TestCase):
    def test_memo_exists_and_carries_the_correction(self):
        self.assertTrue(os.path.exists(MEMO), "%s is the source of truth" % MEMO_REL)
        memo = norm(read(MEMO))
        # The memo's own correction: +0.0705 is the rejected subset.
        self.assertIn("+0.0705 is the **rejected** subset, not the full set", memo)

    def test_every_doc_points_at_the_memo(self):
        for path in (HANDOFF, PRD, ROADMAP):
            self.assertIn(
                MEMO_REL,
                read(path),
                "%s must cite the memo rather than restate its option analysis"
                % os.path.basename(path),
            )


class TestItIsRecordedAsAPromotionDefect(unittest.TestCase):
    """Rule 1 -- the record must not invite loosening the risk gauntlet."""

    def test_handoff_names_it_a_promotion_defect(self):
        text = norm(read(HANDOFF))
        self.assertIn("defect in the promotion, not in the RiskManager", text)

    def test_handoff_records_the_rejection_of_risk_manager_edits(self):
        text = norm(read(HANDOFF))
        self.assertRegex(
            text,
            r"editing `src/core/risk_manager\.py`|"
            r"`src/core/risk_manager\.py`[^.]{0,120}\*\*REJECTED\*\*",
        )
        self.assertIn("REJECTED", text)

    def test_p_win_sizing_is_held_as_general_policy_not_a_genome_unblock(self):
        text = norm(read(HANDOFF))
        self.assertIn("general\nsizing-policy question".replace("\n", " "), text)
        self.assertIn("never introduced to unblock a specific genome", text)

    def test_docs_explain_which_gate_the_frame_failed_to_model(self):
        """sandbox_admissible modelled the EV gate; the Kelly gate binds."""
        for path in (HANDOFF, ROADMAP):
            text = norm(read(path))
            self.assertIn("sandbox_admissible", text)
            self.assertIn("never bind", text, os.path.basename(path))

    def test_prd_lab_ne_sandbox_risk_is_marked_realised(self):
        text = norm(read(PRD))
        self.assertIn("REALISED 2026-09-05", text)
        self.assertIn("the stated mitigation was insufficient", text)


class TestShadowRunIsNotAGateRun(unittest.TestCase):
    """Rule 2 -- the shadow run accrues no FR-5.2 evidence."""

    def test_handoff_says_instrumentation_not_a_gate_run(self):
        text = norm(read(HANDOFF))
        self.assertIn("shadow run is instrumentation, not a gate run", text)

    def test_all_three_docs_state_zero_units_accrued(self):
        for path, needle in (
            (HANDOFF, "accruing gate evidence"),
            (PRD, "accrues ZERO units toward this criterion"),
            (ROADMAP, "accrues ZERO units toward this criterion"),
        ):
            self.assertIn(needle, norm(read(path)), os.path.basename(path))

    def test_what_the_run_does_produce_is_named(self):
        text = norm(read(HANDOFF))
        self.assertIn("KELLY_ZERO", text)
        self.assertIn("already complete", text)


class TestCorrectedNumbers(unittest.TestCase):
    """Rule 3 -- the two figures to fix and the one to add."""

    def test_full_set_edge_is_recorded_as_0750_and_0723(self):
        for path in (HANDOFF, PRD):
            text = norm(read(path))
            self.assertIn("+0.0750", text, os.path.basename(path))
            self.assertIn("+0.0723", text, os.path.basename(path))

    def test_0705_is_never_attributed_to_the_full_set(self):
        """The old mislabel must not survive anywhere in the record.

        Scoped to per-contract prose: HANDOFF section 1 legitimately quotes an
        unrelated ``$0.0705/gal`` AAA-gas forecast MAE, which this must not flag.
        """
        for path in (HANDOFF, PRD, ROADMAP):
            text = norm(read(path))
            for m in re.finditer(r"0\.0705", text):
                window = text[max(0, m.start() - 200) : m.end() + 200]
                if "/gal" in window:
                    continue  # the gas MAE, a different quantity entirely
                self.assertIn(
                    "rejected",
                    window.lower(),
                    "%s quotes 0.0705 per contract without marking it the "
                    "REJECTED subset: %r" % (os.path.basename(path), window),
                )

    def test_gate_margin_is_recorded(self):
        for path in (HANDOFF, PRD):
            text = norm(read(path))
            self.assertIn("p = 0.047", text, os.path.basename(path))
            self.assertIn("[0.667, 0.875]", text, os.path.basename(path))
            self.assertIn("0.683", text, os.path.basename(path))

    def test_time_to_gate_is_recorded_with_its_band(self):
        for path in (HANDOFF, PRD, ROADMAP):
            text = norm(read(path))
            self.assertIn("287", text, os.path.basename(path))
            self.assertIn("208", text, os.path.basename(path))
            self.assertIn("367", text, os.path.basename(path))

    def test_handoff_does_not_repeat_the_old_050_price_cut(self):
        """The cut is at ~0.70 (f>0 iff p>price), never ~0.50."""
        text = norm(read(HANDOFF))
        self.assertNotIn("negative for any price above ~0.50", text)
        self.assertIn("`p > price`", text)


class TestHoldoutDeadlineIsStatedPlainly(unittest.TestCase):
    """Rule 4 -- the one decision with a hard external clock."""

    def test_handoff_names_the_deadline_and_whose_call_it_is(self):
        text = norm(read(HANDOFF))
        self.assertIn("2026-10-03", text)
        self.assertIn("only\nvirgin root".replace("\n", " "), text)
        self.assertIn("owner call", text)

    def test_prd_registers_it_as_an_owner_decision_with_no_default(self):
        text = norm(read(PRD))
        self.assertIn("hard external clock", text)
        self.assertIn("No default is encoded", text)

    def test_roadmap_calls_the_anchor_a_decision(self):
        text = norm(read(ROADMAP))
        self.assertIn("anchor is a decision, not just a date", text)


class TestV2RouteIsRecordedWithItsCost(unittest.TestCase):
    """Option 5 -- the identified fix, its honest cost, and its scope."""

    def test_v2_family_name_is_recorded(self):
        for path in (HANDOFF, PRD, ROADMAP):
            self.assertIn(
                "weather/gfs_mex/taker/v2", norm(read(path)), os.path.basename(path)
            )

    def test_honest_cost_is_recorded(self):
        for path in (HANDOFF, PRD, ROADMAP):
            text = norm(read(path))
            self.assertIn("86 %", text, os.path.basename(path))
            self.assertIn("0.048", text, os.path.basename(path))
            self.assertIn("CLOSED", text, os.path.basename(path))

    def test_it_is_marked_a_separate_phase_not_this_branch(self):
        for path in (HANDOFF, PRD, ROADMAP):
            text = norm(read(path))
            self.assertRegex(
                text,
                r"separate phase|its own phase",
                "%s must scope the v2 re-search out of this branch"
                % os.path.basename(path),
            )
            self.assertIn("frame freeze", text, os.path.basename(path))

    def test_frame_schema_invariants_are_named_as_load_bearing(self):
        for path in (HANDOFF, PRD, ROADMAP):
            text = norm(read(path))
            self.assertIn("frame_search_sha256", text, os.path.basename(path))
            self.assertIn("n_discrepancies", text, os.path.basename(path))


class TestQuotedArithmeticIsActuallyRight(unittest.TestCase):
    """Re-derive the quoted statistics rather than trusting the transcription."""

    def test_cold_start_ceiling_is_070(self):
        # p = 0.6*wr + 0.4*confidence, wr pinned at 0.50, confidence <= 1.0
        self.assertAlmostEqual(0.6 * 0.50 + 0.4 * 1.0, 0.70, places=10)
        # and reaching p = 0.80 would need an out-of-range confidence
        self.assertAlmostEqual((0.80 - 0.30) / 0.40, 1.25, places=10)

    def test_kelly_sign_flips_exactly_at_price_equals_p(self):
        """Zero fee: f = p - q/b with b = (1-price)/price is > 0 iff p > price."""

        def f(p, price):
            b = (1.0 - price) / price
            return p - (1.0 - p) / b

        self.assertGreater(f(0.70, 0.68), 0.0)
        self.assertAlmostEqual(f(0.70, 0.70), 0.0, places=12)
        self.assertLess(f(0.70, 0.71), 0.0)
        # the genome's median price paid is far above the ceiling
        self.assertLess(f(0.70, 0.84), 0.0)

    def test_wilson_ci_for_the_full_genome_unit_win_rate(self):
        lo, hi = wilson(45, 57)
        self.assertAlmostEqual(lo, 0.667, places=3)
        self.assertAlmostEqual(hi, 0.875, places=3)
        # the null sits inside the interval -> not significantly above breakeven
        self.assertLess(lo, 0.683)

    def test_days_to_fifty_units_at_the_measured_rate(self):
        rate = 12.0 / 69.0  # sizable target_date units per frame-day
        self.assertAlmostEqual(rate, 0.1739, places=4)
        self.assertAlmostEqual(50.0 / rate, 287.5, places=1)

    def test_117_of_130_is_ninety_percent(self):
        self.assertAlmostEqual(117.0 / 130.0, 0.90, places=10)
        self.assertAlmostEqual(13.0 / 130.0, 0.10, places=10)


def wilson(k, n, z=1.959963984540054):
    p = k / float(n)
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    return centre - half, centre + half


if __name__ == "__main__":
    unittest.main()
