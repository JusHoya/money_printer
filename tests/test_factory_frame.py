"""F1 FRAME workstream tests: features, fee regime, frame hardening, lanes, coverage.

Real-data tests (the ~6 s evaluator builds) are marked ``realdata`` so they
can be deselected with ``-m "not realdata"``; the synthetic abort tests run
in well under a second.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import src.backtest.ev_analysis as ev
from src.backtest.sealed_roots import SealedDataError
from src.core.fee_calculator import (
    FEE_TYPE_WITH_MAKER_FEES,
    maker_fee,
    taker_fee,
    trade_is_profitable,
)
from src.factory import coverage as cov
from src.factory import features as feat
from src.factory import fees
from src.factory import frame as fr
from src.factory.columns import (
    BAND_LABELS,
    HIDDEN_COLUMNS,
    VISIBLE_COLUMNS,
    WINDOW_LABELS,
    HiddenColumnError,
    VisibleOnly,
    lead_bucket_code as scalar_lead_bucket_code,
    row_view,
)
from src.factory.lanes import ALL_LANES, WeatherLane
from src.factory.lanes.base import NOT_PROMOTABLE, NOT_READY, READY
from src.factory.lanes.weather import build_opportunities

REPO = Path(__file__).resolve().parent.parent
realdata = pytest.mark.realdata


# ---------------------------------------------------------------------------
# real-data fixtures (module scope: each build is ~4-6 s)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def regime():
    return fees.load_regime()


@pytest.fixture(scope="module")
def parity_opp():
    fa, td = fr.materialise_parity_inputs()
    return build_opportunities("gfs_mex", forecast_archive_dir=fa, truth_dir=td, availability_lag_min=0)


@pytest.fixture(scope="module")
def parity_frame(parity_opp, regime):
    return fr.from_opportunity_frame(
        parity_opp, name="parity", availability_lag_min=0, truth_filter=False, sigma_cap=None,
        fold_sandbox_admissible=False, cutoff=fr.FACTORY_DATA_CUTOFF, fee_regime=regime,
    )


@pytest.fixture(scope="module")
def search_opp():
    return build_opportunities("gfs_mex", availability_lag_min=240)


@pytest.fixture(scope="module")
def search_frame(search_opp, regime):
    return fr.from_opportunity_frame(
        search_opp, name="search", availability_lag_min=240, truth_filter=True, sigma_cap=4.0,
        fold_sandbox_admissible=True, cutoff=fr.FACTORY_DATA_CUTOFF, fee_regime=regime,
    )


@pytest.fixture(scope="module")
def gefs_twin(search_frame, regime):
    opp_g = build_opportunities("gefs", availability_lag_min=240)
    return fr.build_gefs_twin(search_frame, opp_g, fee_regime=regime)


# ---------------------------------------------------------------------------
# features.py -- constants pinned to the evaluator
# ---------------------------------------------------------------------------
def test_feature_constants_equal_evaluator():
    assert feat.BAND_EDGES == ev.BAND_EDGES
    assert feat.TIME_WINDOWS == ev.TIME_WINDOWS
    assert feat.MAX_ORDERABLE_PRICE == ev.MAX_ORDERABLE_PRICE
    assert feat.ADVERSE_FILL_DOLLARS == ev.ADVERSE_FILL_DOLLARS
    assert tuple(n for n, _, _ in ev.TIME_WINDOWS) == WINDOW_LABELS
    assert ev.BAND_LABELS == BAND_LABELS


def test_window_and_band_codes_on_a_synthetic_grid():
    minutes = np.array([-5.0, 0.0, 59.9, 60.0, 179.0, 180.0, 359.0, 360.0, 719.0, 720.0, 1439.0, 1440.0, 5000.0, np.nan])
    got = feat.window_code(minutes)
    want = np.array(
        [WINDOW_LABELS.index(ev.time_window_label(m)) if ev.time_window_label(m) in WINDOW_LABELS else -1
         for m in minutes], dtype=np.int16,
    )
    assert got.dtype == np.int16 and np.array_equal(got, want)
    dist = np.array([-0.5, 0.0, 0.99, 1.0, 2.5, 4.999, 5.0, 40.0, np.nan])
    gb = feat.band_code(dist)
    wb = np.array([BAND_LABELS.index(ev.band_label(d)) if not math.isnan(d) else -1 for d in dist], dtype=np.int16)
    assert np.array_equal(gb, wb)
    lh = np.array([-6.0, 0.0, 11.9, 12.0, 59.0, 60.0, 200.0])
    assert np.array_equal(feat.lead_bucket_code(lh), np.array([scalar_lead_bucket_code(x) for x in lh], dtype=np.int16))


def test_features_are_scalar_safe():
    assert feat.window_code(100.0) == 4 and np.ndim(feat.window_code(100.0)) == 0
    assert feat.band_code(np.float64(3.2)) == 3
    assert feat.band_code(None) == -1
    assert feat.lead_bucket_code(np.int32(30)) == 1
    assert feat.quote(0.2, 0.3, 1, 0) == pytest.approx(0.8)  # taker NO pays 1 - yes_bid
    assert math.isnan(feat.quote(0.0, 0.3, 1, 0))  # empty bid sentinel
    assert math.isnan(feat.quote(0.2, 1.0, 0, 0))  # empty ask sentinel
    assert feat.quote(0.2, 0.3, 0, 1) == 0.2  # maker YES joins the bid
    assert feat.quote(0.2, 0.3, 1, 1) == pytest.approx(0.7)  # maker NO joins 1 - ask
    assert feat.price_paid(0.98) == pytest.approx(0.99)
    assert math.isnan(feat.price_paid(0.99))
    assert feat.far_margin_value(0.1, 0.05, 0.3, 1) == pytest.approx(0.2)
    assert feat.far_margin_value(0.4, 0.3, 0.5, 0) == pytest.approx(0.1)
    assert math.isnan(feat.far_margin_value(0.1, 0.0, 0.3, 0))
    assert bool(feat.sandbox_admissible(0.9, 0.5)) is True
    assert bool(feat.sandbox_admissible(0.5, 0.5)) is False
    assert bool(feat.sandbox_admissible(0.9, np.nan)) is False


def test_sandbox_admissible_matches_scalar_trade_is_profitable_on_a_grid():
    prices = np.round(np.arange(0.01, 0.995, 0.01), 10)
    prices = np.concatenate([prices, np.round(prices[:-1] + 0.005, 10)])
    p_win = np.round(np.arange(0.0, 1.0001, 0.005), 10)
    P, W = np.meshgrid(prices, p_win, indexing="ij")
    got = feat.sandbox_admissible(W.ravel(), P.ravel())
    want = np.array([trade_is_profitable(float(w), float(p), contracts=1, is_maker=False)
                     for p, w in zip(P.ravel(), W.ravel())])
    assert got.dtype == bool and np.array_equal(got, want)
    # ceil-cents boundary: p=0.10, C=1 -> taker fee 0.0063 -> ceil 0.01 -> gate at 0.12
    assert bool(feat.sandbox_admissible(0.12, 0.10)) is False
    assert bool(feat.sandbox_admissible(0.1200001, 0.10)) is True


@realdata
def test_feature_codes_equal_evaluator_labels_on_the_real_frame(parity_opp, parity_frame):
    m = parity_opp["minutes_to_close"].to_numpy(dtype=float)
    want_w = np.array([WINDOW_LABELS.index(w) for w in parity_opp["window"].astype(str)], dtype=np.int16)
    assert np.array_equal(feat.window_code(m), want_w)
    d = parity_opp["distance_f"].to_numpy(dtype=float)
    want_b = np.array([BAND_LABELS.index(b) for b in parity_opp["band"].astype(str)], dtype=np.int16)
    assert np.array_equal(feat.band_code(d), want_b)
    # and the encoded frame agrees with the evaluator's own label columns row by row
    assert set(np.unique(parity_frame.visible["window_code"])) <= set(range(len(WINDOW_LABELS)))
    assert (parity_frame.visible["band_code"] >= 0).all()


@realdata
def test_quote_and_price_paid_equal_evaluator_columns(parity_opp):
    dc = np.where(parity_opp["direction"].astype(str).to_numpy() == "buy_no", 1, 0)
    mc = np.where(parity_opp["mode"].astype(str).to_numpy() == "maker", 1, 0)
    q = feat.quote(parity_opp["yes_bid"].to_numpy(float), parity_opp["yes_ask"].to_numpy(float), dc, mc)
    assert np.array_equal(q, parity_opp["quote"].to_numpy(float), equal_nan=True)
    pp = feat.price_paid(q, 0.01)
    assert np.array_equal(pp, parity_opp["price_paid"].to_numpy(float), equal_nan=True)


# ---------------------------------------------------------------------------
# fees.py
# ---------------------------------------------------------------------------
def test_regime_file_loads_and_is_seeded_per_roadmap(regime):
    prefixes = {r.series_prefix for r in regime.rows}
    assert {"KXHIGH", "KXAAAGASM", "KXBTCY", "KXETHY"} <= prefixes
    high = regime.lookup("KXHIGHNY", 1_780_000_000)
    assert high.fee_type == "quadratic" and high.taker_multiplier == 1.0 and high.maker_multiplier == 0.0
    gas = regime.lookup("KXAAAGASM", 1_780_000_000)
    assert gas.fee_type == FEE_TYPE_WITH_MAKER_FEES and gas.maker_multiplier == 1.0
    for s in ("KXBTCY", "KXETHY"):
        row = regime.lookup(s, 1_780_000_000)
        assert row.taker_multiplier == 1.0 and "unverified" in row.source_note.lower()
    assert regime.sha256 == fees.regime_sha256() and len(regime.sha256) == 64
    with pytest.raises(fees.FeeRegimeError):
        regime.lookup("KXNOSUCHSERIES", 1_780_000_000)


def test_fee_per_contract_scalar_spot_checks(regime):
    ts = np.int64(1_780_000_000)
    got = fees.fee_per_contract([0.10, 0.50, np.nan], ts, "KXHIGHNY", 20, False, regime=regime)
    assert got[0] == taker_fee(0.10, 20) / 20 and got[1] == taker_fee(0.50, 20) / 20 and np.isnan(got[2])
    # maker on the standard schedule is $0; on KXAAAGASM it is the 1.75% quadratic
    assert fees.fee_per_contract([0.5], ts, "KXHIGHNY", 20, True, regime=regime)[0] == 0.0
    assert fees.fee_per_contract([0.5], ts, "KXAAAGASM", 20, True, regime=regime)[0] == pytest.approx(
        maker_fee(0.5, 20, FEE_TYPE_WITH_MAKER_FEES, 1.0) / 20
    )
    # per-row series and maker arrays broadcast
    out = fees.fee_per_contract(
        [0.5, 0.5], [ts, ts], np.array(["KXHIGHNY", "KXAAAGASM"]), 20, np.array([True, True]), regime=regime
    )
    assert out[0] == 0.0 and out[1] > 0
    with pytest.raises(fees.FeeRegimeError):
        fees.fee_per_contract([0.5], ts, "KXUNKNOWN", 20, False, regime=regime)


@realdata
def test_fee_regime_equals_evaluator_fee_on_the_whole_parity_frame(parity_opp, regime):
    ts = fr._epoch_seconds(parity_opp["ts_utc"])
    is_maker = parity_opp["mode"].astype(str).to_numpy() == "maker"
    got = fees.fee_per_contract(
        parity_opp["price_paid"].to_numpy(float), ts, parity_opp["series"].astype(str).to_numpy(),
        20, is_maker, regime=regime,
    )
    want = parity_opp["fee_per_contract"].to_numpy(float)
    assert np.array_equal(np.isnan(got), np.isnan(want))
    ok = ~np.isnan(want)
    assert np.array_equal(got[ok], want[ok])
    # and the same is true through _vector_fee directly
    vf = ev._vector_fee(parity_opp["price_paid"], 20, parity_opp["is_maker"], ev.FEE_TYPE_STANDARD).to_numpy()
    assert np.array_equal(got[ok], vf[ok])


# ---------------------------------------------------------------------------
# frame.py -- synthetic aborts
# ---------------------------------------------------------------------------
def _synthetic_opp(n_ts: int = 3, target_date: str = "2026-07-01", init_time_utc: str = "2026-06-30T12:00:00Z",
                   payoff_matches=True, truth_agrees=True, result="yes", ts0: str = "2026-07-01T00:00:00Z"):
    """A tiny evaluator-shaped opportunity frame (one market, n_ts snapshots, 4 shapes)."""
    ts = pd.date_range(ts0, periods=n_ts, freq="h", tz="UTC")
    tape = pd.DataFrame(
        {
            "series": "KXHIGHNY", "city": "NY", "target_date": target_date,
            "market_ticker": "KXHIGHNY-26JUL01-B80.5", "ts_utc": ts,
            "minutes_to_close": np.linspace(1500, 1000, n_ts), "strike_type": "between",
            "floor_strike": 80.0, "cap_strike": 81.0, "yes_bid": 0.20, "yes_ask": 0.25,
            "no_bid": 0.75, "no_ask": 0.80, "last": 0.22, "price_mean": 0.22, "yes_bid_low": 0.19,
            "yes_ask_high": 0.26, "volume": 10.0, "open_interest": 100.0, "result": result,
            "expiration_value": 1.0 if result == "yes" else 0.0, "cli_high": 80.0,
            "payoff_matches_kalshi": payoff_matches, "truth_agrees": truth_agrees,
            "init_time_utc": init_time_utc, "lead_hours": 24, "mu_f": 80.4, "sigma_f": 3.0,
            "p_yes": 0.30, "midpoint_f": 80.5, "distance_f": 0.1, "edge_distance_f": 0.4,
            "fwd_min_ask": 0.24, "fwd_max_bid": 0.21, "maker_yes_fill": True, "maker_no_fill": False,
        }
    )
    tape["settles_yes"] = tape["result"].astype(str).str.lower().eq("yes")
    parts = []
    for direction in ev.DIRECTIONS:
        for mode in ev.MODES:
            part = tape.copy()
            part["direction"] = direction
            part["mode"] = mode
            part["quote"] = [ev.quote_for_shape(b, a, direction, mode) for b, a in zip(part["yes_bid"], part["yes_ask"])]
            part["quote"] = part["quote"].astype(float)
            part["p_win"] = part["p_yes"] if direction == ev.DIRECTION_YES else 1.0 - part["p_yes"]
            part["won"] = part["settles_yes"] if direction == ev.DIRECTION_YES else ~part["settles_yes"]
            parts.append(part)
    opp = pd.concat(parts, ignore_index=True)
    opp["price_paid"] = (opp["quote"] + 0.01).round(10)
    opp["executable"] = opp["price_paid"].notna()
    opp["is_maker"] = opp["mode"].eq(ev.MODE_MAKER)
    opp["fee_per_contract"] = ev._vector_fee(opp["price_paid"], 20, opp["is_maker"], ev.FEE_TYPE_STANDARD)
    opp["ev_per_contract"] = opp["p_win"] - opp["price_paid"] - opp["fee_per_contract"]
    opp["realized_per_contract"] = opp["won"].astype(float) - opp["price_paid"] - opp["fee_per_contract"]
    return opp


def test_synthetic_frame_builds_and_validates(regime):
    opp = _synthetic_opp()
    f = fr.from_opportunity_frame(opp, name="synthetic", cutoff="2026-07-25", fee_regime=regime)
    f.validate()
    assert f.n_rows == 12 and f.n_markets == 1 and f.n_dates == 1
    assert list(f.dates) == ["2026-07-01"]
    rev = f.provenance["git_rev"]
    assert rev and len(rev.split("+")[0]) == 40  # "<sha>" or "<sha>+dirty"
    assert f.provenance["frame_sha256"] == fr.frame_sha256(f)
    assert f.visible["mode_code"].tolist().count(0) == 6  # taker is code 0
    assert np.array_equal(f.block_starts, np.array([0, 12]))


def test_cutoff_abort_on_a_post_cutoff_date(regime, caplog):
    opp = _synthetic_opp(target_date="2026-07-26")
    with pytest.raises(fr.FrameAbort, match="target_date > cutoff 2026-07-25"):
        fr.from_opportunity_frame(opp, name="t", cutoff="2026-07-25", fee_regime=regime)
    assert any("frame abort" in r.getMessage() for r in caplog.records)


def test_no_lookahead_abort_when_init_plus_lag_exceeds_ts(regime):
    # init 23:00Z, first snapshot 00:00Z the next day: lag 0 passes, lag 240 min does not
    opp = _synthetic_opp(init_time_utc="2026-06-30T23:00:00Z")
    fr.from_opportunity_frame(opp, name="ok", availability_lag_min=0, fee_regime=regime)
    with pytest.raises(fr.FrameAbort, match="violate init_time_utc \\+ 240 min <= ts_utc"):
        fr.from_opportunity_frame(opp, name="bad", availability_lag_min=240, fee_regime=regime)


def test_payoff_mismatch_aborts_even_without_the_truth_filter(regime):
    opp = _synthetic_opp(payoff_matches=False)
    with pytest.raises(fr.FrameAbort, match="payoff_matches_kalshi == False"):
        fr.from_opportunity_frame(opp, name="t", truth_filter=False, fee_regime=regime)


def test_truth_filter_semantics_on_synthetic_markets(regime):
    # truth_agrees None is kept; truth_agrees False and an unsettled result are dropped
    keep = _synthetic_opp(truth_agrees=None)
    f = fr.from_opportunity_frame(keep, name="t", truth_filter=True, fee_regime=regime)
    assert f.provenance["kept_truth_none"] == 1 and f.n_rows == 12
    assert (f.hidden["truth_agrees"] == -1).all()
    bad = _synthetic_opp(truth_agrees=False)
    bad["market_ticker"] = "KXHIGHNY-26JUL01-B82.5"
    both = pd.concat([keep, bad], ignore_index=True)
    f2 = fr.from_opportunity_frame(both, name="t", truth_filter=True, fee_regime=regime)
    assert f2.n_markets == 1 and f2.provenance["dropped_truth_disagree"] == 1
    unsettled = _synthetic_opp(result="")
    with pytest.raises(fr.FrameAbort, match="no rows survive"):
        fr.from_opportunity_frame(unsettled, name="t", truth_filter=True, fee_regime=regime)


def test_hidden_columns_are_absent_from_the_visible_namespace(regime):
    f = fr.from_opportunity_frame(_synthetic_opp(), name="t", fee_regime=regime)
    for name in HIDDEN_COLUMNS:
        assert name not in f.visible
        with pytest.raises(HiddenColumnError):
            f.col(name)
        with pytest.raises(HiddenColumnError):
            VisibleOnly(f.visible)[name]
    with pytest.raises(KeyError):
        f.col("no_such_column")
    row = row_view(f, 0)
    assert set(row) == set(VISIBLE_COLUMNS) and "won" not in row
    assert set(f.hidden) == set(HIDDEN_COLUMNS)


def test_sandbox_fold_and_sigma_cap(regime):
    opp = _synthetic_opp()
    base = fr.from_opportunity_frame(opp, name="t", fee_regime=regime)
    folded = fr.from_opportunity_frame(opp, name="t", fold_sandbox_admissible=True, fee_regime=regime)
    assert np.array_equal(base.visible["sandbox_admissible"], folded.visible["sandbox_admissible"])
    assert np.array_equal(folded.visible["executable"], base.visible["executable"] & base.visible["sandbox_admissible"])
    assert np.isnan(folded.hidden["realized_per_contract"][~folded.visible["executable"]]).all()
    with pytest.raises(fr.FrameAbort, match="no rows survive"):
        fr.from_opportunity_frame(opp, name="t", sigma_cap=2.0, fee_regime=regime)
    assert fr.from_opportunity_frame(opp, name="t", sigma_cap=3.0, fee_regime=regime).n_rows == 12


def test_save_load_round_trip_and_path_independent_sha(tmp_path, regime):
    f = fr.from_opportunity_frame(_synthetic_opp(), name="t", fee_regime=regime)
    f.twin_index = np.full(f.n_rows, -1, dtype=np.int64)
    sha = fr.save(f, str(tmp_path / "a"))
    g = fr.load(str(tmp_path / "a"))
    assert fr.frame_sha256(g) == sha == fr.frame_sha256(f)
    assert g.twin_index is not None and (g.twin_index == -1).all()
    for name in VISIBLE_COLUMNS:
        assert np.array_equal(f.visible[name], g.visible[name], equal_nan=True)
    for name in HIDDEN_COLUMNS:
        assert np.array_equal(f.hidden[name], g.hidden[name], equal_nan=True)
    assert set(os.listdir(tmp_path / "a")) >= {
        "visible.npy", "hidden.npy", "columns.json", "dates.json", "markets.json",
        "provenance.json", "frame.sha256", "twin_index.npy",
    }
    # a byte flip is detected on load
    (tmp_path / "a" / "frame.sha256").write_text("0" * 64 + "  t\n")
    with pytest.raises(fr.FrameAbort, match="frame.sha256"):
        fr.load(str(tmp_path / "a"))


# ---------------------------------------------------------------------------
# parity pin
# ---------------------------------------------------------------------------
def test_parity_pin_is_complete_and_matches_the_phase2_report():
    pin = fr.load_parity_pin()
    ref = json.load(open(REPO / pin["reference"], encoding="utf-8"))["provenance"]
    want = {ref["forecast_csv"].replace("\\", "/"): ref["forecast_csv_sha256"]}
    for t in ref["truth"].values():
        want[t["path"].replace("\\", "/")] = t["file_sha256"]
    got = {f["path"]: f["sha256"] for f in pin["files"]}
    assert got == want
    assert pin["commit"] == "48618cf654771f1fe6eddafbd61600cb1343b857"


def test_materialise_parity_inputs_verifies_sha(tmp_path):
    fa, td = fr.materialise_parity_inputs(dest=str(tmp_path / "pin"))
    pin = fr.load_parity_pin()
    for f in pin["files"]:
        target = Path(fa if "forecast_archive" in f["path"] else td) / Path(f["path"]).name
        assert fr.sha256_file(str(target)) == f["sha256"]
    # a tampered blob is re-materialised from git
    target = Path(fa) / "forecast_series_gfs_mex.csv"
    target.write_bytes(b"tampered")
    fr.materialise_parity_inputs(dest=str(tmp_path / "pin"))
    assert fr.sha256_file(str(target)) == pin["files"][0]["sha256"]
    # a pin with the wrong hash aborts
    bad = dict(pin)
    bad["files"] = [dict(pin["files"][0], sha256="0" * 64)]
    bad_path = tmp_path / "bad_pin.json"
    bad_path.write_text(json.dumps(bad))
    with pytest.raises(fr.FrameAbort, match="sha256"):
        fr.materialise_parity_inputs(dest=str(tmp_path / "pin2"), pin_path=str(bad_path))


# ---------------------------------------------------------------------------
# lanes / coverage / sealed roots
# ---------------------------------------------------------------------------
def test_lane_registry_and_statuses():
    assert set(ALL_LANES) == {"weather", "gas", "mention", "tweets", "crypto_annual"}
    w = ALL_LANES["weather"]().status()
    assert w.state == READY and w.n_units == 69
    assert WeatherLane.independent_unit == "target_date"
    g = ALL_LANES["gas"]().status()
    assert g.state == NOT_PROMOTABLE and g.n_units == 14 and g.reason
    for name in ("mention", "tweets", "crypto_annual"):
        s = ALL_LANES[name]().status()
        assert s.state == NOT_READY and s.n_units == 0 and s.reason
        with pytest.raises(NotImplementedError):
            ALL_LANES[name]().build_frames(None)


def test_coverage_is_timestamp_free(tmp_path):
    c = cov.compute_coverage()
    assert c["floor"] == 40 and {l["lane"] for l in c["lanes"]} == set(ALL_LANES)
    weather = next(l for l in c["lanes"] if l["lane"] == "weather")
    assert weather["searchable"] and weather["next_data_eta"] == "2026-10-03"
    text = json.dumps(c)
    for word in ("generated", "timestamp", "now", "_at"):
        assert word not in text.lower().replace("next_data_eta", "")
    p = tmp_path / "coverage.json"
    cov.write_coverage(str(p))
    assert json.loads(p.read_text()) == c
    before = p.read_bytes()
    cov.write_coverage(str(p))
    assert p.read_bytes() == before


def test_sealed_roots_are_refused_through_build_opportunities():
    with pytest.raises(SealedDataError):
        build_opportunities("gfs_mex", ladder_root=str(REPO / "data" / "ladders_holdout"))
    with pytest.raises(SealedDataError):
        build_opportunities("gfs_mex", ladder_root="data/ladders_2026-09")


# ---------------------------------------------------------------------------
# real-data frames
# ---------------------------------------------------------------------------
@realdata
def test_parity_inputs_reproduce_the_phase2_reference_shape(parity_opp):
    pin = fr.load_parity_pin()["reference_shape"]
    r = ev.evaluate_shape(parity_opp, ev.fr31a_mask(parity_opp) & parity_opp["mode"].eq(ev.MODE_TAKER), "fr31a")
    assert r.trades == pin["trades"] and r.dates == pin["dates"]
    assert r.realized == pin["realized_per_contract"]
    assert r.boot_lo == pin["boot_lo"] and r.boot_hi == pin["boot_hi"]


@realdata
def test_parity_frame_keeps_every_evaluator_row(parity_opp, parity_frame):
    assert (parity_opp["minutes_to_close"] > 0).all()  # the evaluator already dropped <= 0
    assert parity_frame.n_rows == len(parity_opp) == 251_728
    assert parity_frame.n_dates == 69 and parity_frame.n_markets == 1656
    assert parity_frame.provenance["availability_lag_min"] == 0
    assert parity_frame.provenance["lookahead_violations"] == 0
    assert parity_frame.provenance["min_availability_slack_min"] >= 0
    assert parity_frame.provenance["executable_rows"] == int(parity_opp["executable"].sum())
    assert parity_frame.provenance["fee_regime_vs_evaluator"]["rows_differing"] == 0
    assert parity_frame.provenance["fee_regime_vs_evaluator"]["nan_pattern_equal"]
    assert len(parity_frame.provenance["git_rev"].split("+")[0]) == 40  # "<sha>[+dirty]"
    assert parity_frame.provenance["lab_lock_sha256"]
    assert len(parity_frame.provenance["ladder_files"]) >= 276
    assert parity_frame.provenance["forecast_csv"]["sha256"].startswith("850a2a3f44ca")
    assert parity_frame.provenance["calibration_dir"]["loaded_by_walk_forward"] is False
    parity_frame.validate()
    # realized_per_contract equals the evaluator's on executable rows
    order = np.lexsort((
        np.where(parity_opp["mode"].astype(str).to_numpy() == "maker", 1, 0),
        np.where(parity_opp["direction"].astype(str).to_numpy() == "buy_no", 1, 0),
        fr._epoch_seconds(parity_opp["ts_utc"]),
        pd.Series(parity_opp["market_ticker"].astype(str)).map(
            {m: i for i, m in enumerate(parity_frame.markets)}).to_numpy(),
    ))
    want = parity_opp["realized_per_contract"].to_numpy(float)[order]
    assert np.array_equal(parity_frame.hidden["realized_per_contract"], want, equal_nan=True)


@realdata
def test_search_frame_truth_filter_counts_and_hardening(search_opp, search_frame):
    p = search_frame.provenance
    assert p["dropped_truth_disagree"] == 0
    assert p["kept_truth_none"] == 25
    assert p["dropped_payoff_mismatch"] == 0
    assert search_frame.n_dates == 69
    assert p["availability_lag_min"] == 240 and p["lookahead_violations"] == 0
    assert p["min_availability_slack_min"] >= 0
    assert search_opp.attrs["lag_snapshots_revintaged"] > 0
    assert (search_frame.visible["sigma_f"] <= 4.0).all()
    assert np.array_equal(
        search_frame.visible["executable"],
        search_frame.visible["executable"] & search_frame.visible["sandbox_admissible"],
    )
    assert p["executable_rows"] < p["executable_evaluator"]
    assert (search_frame.hidden["result_code"] >= 0).all()
    # the evaluator's lag-0 join would have handed 12Z vintages to rows inside 4 h of init
    init = fr._epoch_seconds(search_opp["init_time_utc"])
    ts = fr._epoch_seconds(search_opp["ts_utc"])
    assert ((ts - init) >= 240 * 60).all()


@realdata
def test_gefs_twin_index(search_frame, gefs_twin):
    ti = search_frame.twin_index
    assert ti is not None and ti.shape == (search_frame.n_rows,)
    share = float((ti >= 0).mean())
    assert 0.5 < share <= 1.0
    ok = ti >= 0
    assert np.array_equal(search_frame.visible["ts_utc"][ok], gefs_twin.visible["ts_utc"][ti[ok]])
    assert np.array_equal(search_frame.visible["direction_code"][ok], gefs_twin.visible["direction_code"][ti[ok]])
    assert np.array_equal(search_frame.visible["mode_code"][ok], gefs_twin.visible["mode_code"][ti[ok]])
    assert np.array_equal(
        search_frame.markets[search_frame.visible["market_code"][ok]],
        gefs_twin.markets[gefs_twin.visible["market_code"][ti[ok]]],
    )
    assert gefs_twin.provenance["source"] == "gefs" and gefs_twin.n_dates == 69
