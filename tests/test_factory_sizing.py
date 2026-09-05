"""The cold-start sizing predicate, the promotion guard, and the v2 diagnostic.

Acts on ``reports/factory/sizing_cold_start_2026-09-05.md``: the runtime blends a
neutral 0.50 win-rate prior until a strategy has ``MIN_WIN_SAMPLES`` closes, so
``p = 0.6*0.50 + 0.4*confidence <= 0.70`` and any buy above ~70c is sized to
**zero** contracts.  The F3 factory promoted a far-bracket NO shape that buys at
a median 0.84; 117 of its 130 offline trades are unsizable.  Nothing offline
knew that.

The load-bearing test here is
:meth:`TestPredicateMatchesRiskManager.test_agrees_on_every_deployed_trade`: it
drives all 130 of the deployed genome's offline trades through
``src.factory.sizing`` **and** through a real ``RiskManager`` with an empty win
record and asserts they agree row for row.  The predicate is validated against
the real thing, never against the algebra.

The 130 trades are committed as
``tests/fixtures/factory/genome_0c4b20502f2daf65_offline_trades.json`` because
the 60 MB frozen frame is not tracked; :meth:`TestFixtureProvenance` re-derives
them from the frame the memo's appendix uses and asserts equality whenever that
frame is on disk.
"""
from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

from src.core.risk_manager import MIN_WIN_SAMPLES, RiskManager
from src.factory import columns as C
from src.factory import genome as G
from src.factory import sizing as S

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "factory" / "genome_0c4b20502f2daf65_offline_trades.json"
DEPLOYED_SPEC = REPO_ROOT / "configs" / "factory" / "promoted" / "0c4b20502f2daf65.json"
#: The frozen search frame is 60 MB and untracked, so it is present only on a
#: checkout that has built or copied it.  ``MP_TEST_SEARCH_FRAME`` points the
#: provenance tests at one held elsewhere (e.g. the main working copy while this
#: runs from a worktree); without it they skip.
SEARCH_FRAME = Path(
    os.environ.get(
        "MP_TEST_SEARCH_FRAME",
        str(REPO_ROOT / "data" / "factory" / "frames" / "weather_2026-07-25_bfcf94654a3a" / "search"),
    )
)


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    cols = doc["columns"]
    idx = {name: i for i, name in enumerate(cols)}
    rows = doc["rows"]
    return doc, idx, rows


# ---------------------------------------------------------------------------
# 1. the predicate, validated against the real RiskManager
# ---------------------------------------------------------------------------
class TestPredicateMatchesRiskManager:
    def _rm(self, balance: float = 2873.12) -> RiskManager:
        rm = RiskManager(starting_balance=balance, persist_state=False)
        rm.strategy_win_records = {}  # cold start: no closed trades for this strategy
        return rm

    def test_agrees_on_every_deployed_trade(self):
        """All 130 offline trades of 0c4b20502f2daf65: predicate == real Kelly sizer."""
        _doc, idx, rows = load_fixture()
        assert len(rows) == 130
        rm = self._rm()
        disagreements = []
        n_admitted = 0
        for r in rows:
            price = float(r[idx["price_paid"]])
            p_win = float(r[idx["p_win"]])
            ticker = str(r[idx["market_ticker"]])
            qty = rm.calculate_kelly_size(p_win, price, "Genome 0c4b2050", symbol=ticker)
            real = qty >= 1
            mine = S.is_sizable(price, p_win, symbol=ticker)
            n_admitted += int(real)
            if real != mine:
                disagreements.append((ticker, price, p_win, qty, mine))
        assert not disagreements, f"predicate disagrees with RiskManager on {len(disagreements)} rows: {disagreements[:5]}"
        # The memo's headline figures, re-derived here from the real sizer.
        assert n_admitted == 13
        assert len(rows) - n_admitted == 117

    @pytest.mark.parametrize("balance", [100.0, 500.0, 2873.12, 10_000.0, 60_000.0])
    def test_admission_is_balance_independent(self, balance):
        """The stage's kelly_frac scales f; it never changes its sign (memo section 2.3)."""
        _doc, idx, rows = load_fixture()
        rm = self._rm(balance)
        admitted = sum(
            1
            for r in rows
            if rm.calculate_kelly_size(
                float(r[idx["p_win"]]), float(r[idx["price_paid"]]), "Genome 0c4b2050",
                symbol=str(r[idx["market_ticker"]]),
            )
            >= 1
        )
        assert admitted == 13

    def test_vectorised_mask_matches_scalar_predicate(self):
        _doc, idx, rows = load_fixture()
        px = np.array([float(r[idx["price_paid"]]) for r in rows])
        pw = np.array([float(r[idx["p_win"]]) for r in rows])
        tk = np.array([str(r[idx["market_ticker"]]) for r in rows])
        vec = S.sizable_mask(px, pw, symbols=tk)
        scalar = np.array([S.is_sizable(p, c, symbol=s) for p, c, s in zip(px, pw, tk)])
        assert np.array_equal(vec, scalar)
        assert int(vec.sum()) == 13

    def test_agrees_with_risk_manager_on_a_swept_grid(self):
        """Not just the deployed rows: the whole (price, confidence) cent grid."""
        rm = self._rm()
        disagreements = []
        for i in range(1, 100):
            price = i / 100.0
            for j in range(0, 101, 5):
                conf = j / 100.0
                real = rm.calculate_kelly_size(conf, price, "Sweep", symbol="KXHIGHNY-26JUL27-B82.5") >= 1
                if real != S.is_sizable(price, conf, symbol="KXHIGHNY-26JUL27-B82.5"):
                    disagreements.append((price, conf))
        assert not disagreements, f"{len(disagreements)} grid disagreements, first {disagreements[:5]}"

    def test_agrees_on_a_maker_fee_series(self):
        """KXAAAGASM bills resting liquidity, so the fee is not zero -- the predicate reads it."""
        rm = self._rm()
        sym = "KXAAAGASM-26AUG-B3.25"
        assert S.fee_per_contract(0.50, sym) > 0.0
        assert S.fee_per_contract(0.50, "KXHIGHNY-26JUL27-B82.5") == 0.0
        disagreements = [
            (p / 100.0, c / 100.0)
            for p in range(1, 100)
            for c in range(0, 101, 10)
            if (rm.calculate_kelly_size(c / 100.0, p / 100.0, "GasSweep", symbol=sym) >= 1)
            != S.is_sizable(p / 100.0, c / 100.0, symbol=sym)
        ]
        assert not disagreements, f"maker-fee disagreements: {disagreements[:5]}"

    def test_confidence_cannot_reach_a_high_price_at_cold_start(self):
        """There is no caller-side workaround: p <= 0.70 for any confidence in [0, 1]."""
        assert S.blended_p(1.0) == pytest.approx(0.70)
        assert S.blended_p(0.0) == pytest.approx(0.30)
        assert not S.is_sizable(0.84, 1.0)
        # f > 0 is strict, so the highest QUOTABLE price is a cent under the blend
        assert S.price_ceiling(1.0) == pytest.approx(0.69)
        assert S.price_ceiling(0.9988) == pytest.approx(0.69)
        assert not S.is_sizable(0.70, 1.0) and S.is_sizable(0.69, 1.0)

    def test_win_record_lifts_the_ceiling(self):
        """The law is only a *cold-start* law; the predicate takes the realized rate too."""
        assert not S.is_sizable(0.84, 0.95)
        assert S.is_sizable(0.84, 0.95, historical_wr=0.94)
        rm = self._rm()
        rm.strategy_win_records = {"Seeded": [1] * (MIN_WIN_SAMPLES + 10)}
        assert rm.calculate_kelly_size(0.95, 0.84, "Seeded", symbol="KXHIGHNY-26JUL27-B82.5") >= 1

    def test_out_of_range_and_nan_are_not_sizable(self):
        for px in (0.0, 1.0, -0.1, 1.5, float("nan")):
            assert not S.is_sizable(px, 0.99)
        assert not S.is_sizable(0.5, float("nan"))
        assert not bool(S.sizable_mask([np.nan, 0.10], [0.99, 0.99])[0])
        assert bool(S.sizable_mask([np.nan, 0.10], [0.99, 0.99])[1])


# ---------------------------------------------------------------------------
# 2. the audit / promotion guard
# ---------------------------------------------------------------------------
def _no_taker_frame(prices, p_wins, *, n_dates=None, name="guardtest") -> C.Frame:
    """A minimal frame of buy_no/taker rows, one snapshot per market, all executable.

    Shaped so the ``nofilter_no`` seed (buy_no, taker, windows >=24h/12-24h,
    bands 4-5F/5F+) masks every row -- which lets the promotion guard be driven
    through ``scripts/factory.py``'s real ``cmd_promote``.
    """
    n = len(prices)
    dates = np.array([f"2026-06-{i + 1:02d}" for i in range(n_dates or n)], dtype=str)
    markets = np.array([f"KXHIGHNY-26JUN{(i % len(dates)) + 1:02d}-B{80 + i}" for i in range(n)], dtype=str)
    order = np.argsort(markets)
    markets = markets[order]
    prices = [prices[i] for i in order]
    p_wins = [p_wins[i] for i in order]

    vis = {}
    for col, dt in C.VISIBLE_DTYPES.items():
        vis[col] = np.zeros(n, dtype=np.dtype(dt))
    vis["market_code"] = np.arange(n, dtype=np.int32)
    vis["target_date_code"] = np.array([i % len(dates) for i in range(n)], dtype=np.int16)
    vis["ts_utc"] = np.arange(n, dtype=np.int64) + 1_750_000_000
    vis["minutes_to_close"] = np.full(n, 2000.0)
    vis["window_code"] = np.zeros(n, dtype=np.int16)          # ">=24h"
    vis["direction_code"] = np.ones(n, dtype=np.int16)        # buy_no
    vis["mode_code"] = np.zeros(n, dtype=np.int16)            # taker
    vis["band_code"] = np.full(n, 5, dtype=np.int16)          # "5F+"
    vis["lead_bucket_code"] = np.full(n, 2, dtype=np.int16)   # "long"
    vis["lead_hours"] = np.full(n, 72.0)
    vis["p_win"] = np.asarray(p_wins, dtype=np.float64)
    vis["p_yes"] = 1.0 - vis["p_win"]
    vis["sigma_f"] = np.full(n, 2.0)
    vis["mu_f"] = np.full(n, 75.0)
    vis["midpoint_f"] = np.full(n, 85.0)
    vis["distance_f"] = np.full(n, 10.0)
    vis["edge_distance_f"] = np.full(n, 9.5)
    vis["quote"] = np.asarray(prices, dtype=np.float64) - 0.01
    vis["price_paid"] = np.asarray(prices, dtype=np.float64)
    vis["no_ask"] = vis["price_paid"]
    vis["yes_bid"] = 1.0 - vis["price_paid"]
    vis["yes_ask"] = 1.0 - vis["quote"]
    vis["no_bid"] = vis["quote"]
    vis["last"] = vis["yes_bid"]
    vis["price_mean"] = vis["yes_bid"]
    vis["volume"] = np.full(n, 100.0)
    vis["open_interest"] = np.full(n, 50.0)
    vis["fee_per_contract"] = np.zeros(n)
    vis["executable"] = np.ones(n, dtype=bool)
    vis["sandbox_admissible"] = np.ones(n, dtype=bool)
    vis["floor_strike"] = np.full(n, 84.5)
    vis["cap_strike"] = np.full(n, 85.5)
    vis["strike_type_code"] = np.zeros(n, dtype=np.int16)
    vis["city_code"] = np.array([i % 4 for i in range(n)], dtype=np.int16)

    hid = {}
    for col, dt in C.HIDDEN_DTYPES.items():
        hid[col] = np.zeros(n, dtype=np.dtype(dt))
    hid["won"] = np.ones(n, dtype=bool)
    hid["realized_per_contract"] = 1.0 - vis["price_paid"]
    hid["result_code"] = np.zeros(n, dtype=np.int16)
    hid["settles_yes"] = np.zeros(n, dtype=bool)
    hid["expiration_value"] = np.zeros(n)
    hid["cli_high"] = np.full(n, 75.0)
    hid["truth_agrees"] = np.ones(n, dtype=np.int16)
    hid["payoff_matches_kalshi"] = np.ones(n, dtype=np.int16)
    hid["maker_yes_fill"] = np.ones(n, dtype=bool)
    hid["maker_no_fill"] = np.ones(n, dtype=bool)
    hid["fwd_min_ask"] = vis["yes_ask"]
    hid["fwd_max_bid"] = vis["yes_bid"]
    hid["yes_bid_low"] = vis["yes_bid"]
    hid["yes_ask_high"] = vis["yes_ask"]
    hid["ev_per_contract"] = vis["p_win"] - vis["price_paid"]

    fr = C.Frame(
        name=name, visible=vis, hidden=hid, dates=dates, markets=markets,
        block_starts=np.arange(n + 1, dtype=np.int64),
    )
    fr.validate()
    return fr


class TestPromotionGuard:
    def test_fraction_catches_what_the_median_hides(self):
        """The memo's headline is the median; the fraction is sharper.

        Six cheap fills and five dear ones: the MEDIAN price_paid (0.10) sits
        comfortably under the cold-start ceiling, so a median test passes the
        genome -- while 5 of its 11 fills would still be sized to zero.
        """
        prices = [0.10] * 6 + [0.95] * 5
        F = _no_taker_frame(prices, [0.99] * 11)
        audit = S.audit_genome(F, G.SEEDS["nofilter_no"])
        assert audit.n_trades == 11 and audit.n_sizable == 6
        assert audit.median_price < audit.median_ceiling, "the median test would pass this genome"
        assert audit.sizable_fraction == pytest.approx(6 / 11)
        assert not audit.ok

    def test_guard_passes_a_cheap_shape(self):
        F = _no_taker_frame([0.10] * 10, [0.99] * 10)
        audit = S.assert_promotable(F, G.SEEDS["nofilter_no"], label="cheap")
        assert audit.ok and audit.sizable_fraction == 1.0

    def test_guard_refuses_an_expensive_shape_and_names_the_number(self):
        F = _no_taker_frame([0.90] * 9 + [0.10], [0.99] * 10)
        with pytest.raises(S.UnsizableGenomeError) as exc:
            S.assert_promotable(F, G.SEEDS["nofilter_no"], label="expensive")
        msg = str(exc.value)
        assert "1/10" in msg and "10.0%" in msg
        assert S.FINDING_REPORT in msg
        assert "75.0%" in msg

    def test_threshold_may_only_be_tightened(self):
        F = _no_taker_frame([0.10] * 8 + [0.90] * 2, [0.99] * 10)
        assert S.assert_promotable(F, G.SEEDS["nofilter_no"]).ok  # 80% >= 75%
        with pytest.raises(S.UnsizableGenomeError):
            S.assert_promotable(F, G.SEEDS["nofilter_no"], min_fraction=0.90)

    def test_genome_trade_rows_match_the_scorer(self):
        from src.factory import fitness

        F = _no_taker_frame([0.10, 0.90, 0.55, 0.84], [0.99, 0.98, 0.97, 0.96])
        g = G.SEEDS["nofilter_no"]
        assert np.array_equal(
            S.genome_trade_rows(F, g), fitness.score(F, G.to_mask(g, F), constraints=False).trade_rows
        )


# ---------------------------------------------------------------------------
# 3. the guard is wired into `factory.py promote`
# ---------------------------------------------------------------------------
def _factory_module():
    import importlib.util
    import sys

    path = REPO_ROOT / "scripts" / "factory.py"
    spec = importlib.util.spec_from_file_location("factory_cli_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _promote_args(frames_dir: Path, out_dir: Path, seed: str, **over):
    from src.factory import promoted as P

    g = G.SEEDS[seed]
    args = Namespace(
        id=P.genome_id_for(P.genome_json_for(g)), from_seed=seed, from_pick=None, config=None,
        frames=str(frames_dir), ladders=None, run_id=None, mode="shadow", out_dir=str(out_dir),
        min_sizable_fraction=None,
    )
    for k, v in over.items():
        setattr(args, k, v)
    return args


class TestPromoteCliRefusal:
    """`factory.py promote` must die on the guard BEFORE it runs replay parity."""

    def _save(self, F, frames_dir: Path):
        from src.factory import frame as FR

        FR.save(F, str(frames_dir / "search"))

    def test_promote_refuses_an_unsizable_genome(self, tmp_path, capsys):
        fac = _factory_module()
        self._save(_no_taker_frame([0.90] * 9 + [0.10], [0.99] * 10), tmp_path)
        args = _promote_args(tmp_path, tmp_path / "out", "nofilter_no")
        with pytest.raises(SystemExit) as exc:
            fac.cmd_promote(args)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "1/10 offline trades sizable" in err
        assert S.FINDING_REPORT in err
        assert not (tmp_path / "out").exists(), "nothing may be written when the guard fires"

    def test_promote_refuses_to_loosen_the_threshold(self, tmp_path, capsys):
        fac = _factory_module()
        self._save(_no_taker_frame([0.10] * 10, [0.99] * 10), tmp_path)
        args = _promote_args(tmp_path, tmp_path / "out", "nofilter_no", min_sizable_fraction=0.10)
        with pytest.raises(SystemExit):
            fac.cmd_promote(args)
        err = capsys.readouterr().err
        assert "may only be raised" in err

    def test_guard_runs_before_replay_parity(self, tmp_path, monkeypatch, capsys):
        """The refusal must not cost a full replay: parity is never reached."""
        fac = _factory_module()
        self._save(_no_taker_frame([0.90] * 10, [0.99] * 10), tmp_path)
        called = []
        monkeypatch.setattr(
            fac._parity_module(), "run_parity", lambda *a, **k: called.append(1) or {}, raising=True
        )
        with pytest.raises(SystemExit):
            fac.cmd_promote(_promote_args(tmp_path, tmp_path / "out", "nofilter_no"))
        assert called == []


# ---------------------------------------------------------------------------
# 4. the v2 diagnostic
# ---------------------------------------------------------------------------
def _audit_script():
    import importlib.util
    import sys

    path = REPO_ROOT / "scripts" / "factory_sizing_audit.py"
    spec = importlib.util.spec_from_file_location("factory_sizing_audit_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestV2Diagnostic:
    def test_splits_a_trade_set_into_surviving_and_excluded(self):
        mod = _audit_script()
        F = _no_taker_frame([0.10] * 4 + [0.90] * 6, [0.99] * 10)
        doc = mod.audit_genome_shapes(F, G.SEEDS["nofilter_no"])
        shapes = {s["shape"]: s for s in doc["shapes"]}
        assert shapes["registered"]["trades"] == 10
        assert shapes["as_deployed_sizable"]["trades"] == 4
        assert shapes["as_deployed_unsizable"]["trades"] == 6
        assert (
            shapes["as_deployed_sizable"]["trades"] + shapes["as_deployed_unsizable"]["trades"]
            == shapes["registered"]["trades"]
        )
        # realized is date-clustered (HANDOFF section 3 rule 3), so it is a mean over units
        assert shapes["as_deployed_sizable"]["realized_per_contract"] == pytest.approx(0.90)
        assert shapes["as_deployed_unsizable"]["realized_per_contract"] == pytest.approx(0.10)

    def test_universe_mode_needs_no_genome(self):
        mod = _audit_script()
        F = _no_taker_frame([0.10] * 4 + [0.90] * 6, [0.99] * 10)
        doc = mod.audit_universe_shapes(F, direction="buy_no", mode="taker")
        shapes = {s["shape"]: s for s in doc["shapes"]}
        assert shapes["universe"]["trades"] == 10
        assert shapes["v2_repick_sizable"]["trades"] == 4

    def test_diagnostic_is_read_only_over_the_frame(self):
        mod = _audit_script()
        F = _no_taker_frame([0.10] * 4 + [0.90] * 6, [0.99] * 10)
        before = {k: v.copy() for k, v in F.visible.items()}
        before_hidden = {k: v.copy() for k, v in F.hidden.items()}
        mod.audit_genome_shapes(F, G.SEEDS["nofilter_no"])
        mod.audit_universe_shapes(F, direction="buy_no", mode="taker")
        for k, v in F.visible.items():
            assert np.array_equal(v, before[k], equal_nan=np.issubdtype(v.dtype, np.floating)), k
        for k, v in F.hidden.items():
            assert np.array_equal(v, before_hidden[k], equal_nan=np.issubdtype(v.dtype, np.floating)), k
        assert bool(F.visible["sandbox_admissible"].all()), "sandbox_admissible must not be touched"

    def test_cli_runs_and_writes_only_where_asked(self, tmp_path, capsys):
        from src.factory import frame as FR

        mod = _audit_script()
        F = _no_taker_frame([0.10] * 4 + [0.90] * 6, [0.99] * 10)
        FR.save(F, str(tmp_path / "search"))
        out = tmp_path / "audit.json"
        rc = mod.main(["--frames", str(tmp_path), "--seed", "nofilter_no", "--json", str(out)])
        assert rc == 0
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["genome"]["name"] == "nofilter_no"
        # the frame directory is untouched apart from what FR.save already wrote
        assert not (tmp_path / "search" / "sandbox_admissible.npy").exists()


# ---------------------------------------------------------------------------
# 5. fixture provenance -- the committed table IS the frame's trade set
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not SEARCH_FRAME.exists(), reason="frozen search frame not on disk (60 MB, untracked)")
class TestFixtureProvenance:
    def test_fixture_matches_the_frozen_frame(self):
        from src.factory import fitness
        from src.factory import frame as FR

        doc, idx, rows = load_fixture()
        F = FR.load(str(SEARCH_FRAME))
        spec = json.loads(DEPLOYED_SPEC.read_text(encoding="utf-8"))
        assert doc["frame_search_sha256"] == spec["frame_search_sha256"]
        g = G.Genome.from_json(spec["genome_json"])
        trade_rows = fitness.score(F, G.to_mask(g, F), constraints=False, genome=g).trade_rows
        assert len(trade_rows) == len(rows) == spec["parity"]["n_offline"]
        vis = F.visible
        for r, want in zip(trade_rows, rows):
            r = int(r)
            assert str(F.markets[vis["market_code"][r]]) == want[idx["market_ticker"]]
            assert int(vis["ts_utc"][r]) == want[idx["ts_utc"]]
            assert float(vis["price_paid"][r]) == pytest.approx(want[idx["price_paid"]], abs=0, rel=0)
            assert float(vis["p_win"][r]) == pytest.approx(want[idx["p_win"]], abs=0, rel=0)

    def test_deployed_spec_would_be_refused_today(self):
        from src.factory import frame as FR

        F = FR.load(str(SEARCH_FRAME))
        spec = json.loads(DEPLOYED_SPEC.read_text(encoding="utf-8"))
        g = G.Genome.from_json(spec["genome_json"])
        audit = S.audit_genome(F, g, label="0c4b20502f2daf65 (fr31a_taker)")
        assert audit.n_trades == 130 and audit.n_sizable == 13
        assert audit.n_dates == 57 and audit.n_dates_sizable == 12
        assert audit.sizable_fraction == pytest.approx(0.10, abs=0.001)
        with pytest.raises(S.UnsizableGenomeError):
            S.assert_promotable(F, g, label="0c4b20502f2daf65")


def test_fixture_is_tracked_and_small():
    assert FIXTURE.exists()
    assert os.path.getsize(FIXTURE) < 200_000
