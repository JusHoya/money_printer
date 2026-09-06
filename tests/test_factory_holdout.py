"""FR-F4.1: ``factory.py holdout`` / ``factory.py score`` and the unseal protocol (src/factory/holdout.py).

Everything runs on SYNTHETIC sealed roots under ``tmp_path`` (a ``SEALED``
marker, a ``SHA256SUMS``, a ``manifest.json`` with the real key names, tiny
ladder CSVs in the ``LADDER_COLUMNS`` schema), a tmp registry with a PROPOSED
genome, a tmp promoted spec and a tmp copy of the revival doc carrying a
``RATIFIED`` line. The real ``data/ladders_holdout``, the real registry and
the real ``reports/factory/unseal_log.jsonl`` are asserted untouched after
every test (``_real_state_guard``).

The fitness kernel is exercised on a PLANTED frame (one market per city-day,
alternating signal/noise markets) so that ``fr31a_taker`` passes every
section 5.8 gate honestly and ``nofilter_no`` fails deterministically
(paired against itself is exactly 0; its pooled mean is negative).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from src.backtest import ev_analysis as ev
from src.backtest import sealed_roots as sr
from src.data.kalshi_history import LADDER_COLUMNS, load_ladders
from src.factory import columns as C
from src.factory import features as feat
from src.factory import fees as fees_mod
from src.factory import genome as G
from src.factory import holdout as H
from src.factory import promoted as P
from src.factory.registry import Registry
from tests.factory_testkit import copy_frame

REPO = Path(__file__).resolve().parent.parent
REAL_LOG = REPO / "reports" / "factory" / "unseal_log.jsonl"
REAL_REGISTRY = REPO / "reports" / "factory" / "registry.jsonl"
REAL_HOLDOUT = REPO / "data" / "ladders_holdout"
REAL_REVIVAL = REPO / "docs" / "REVIVAL_2026_09.md"
FAMILY = "weather/gfs_mex/taker/vtest"
OTHER_CLOSED = "weather/gfs_mex/taker/vclosed"
OTHER_BLANK = "gas/aaa/taker/vblank"
TAG = "RATIFIED-2026-09-06"
GIT_REV = "f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4"

FR31A = G.SEEDS["fr31a_taker"]
NOFILTER = G.SEEDS["nofilter_no"]
FR31A_ID = P.genome_id_for(P.genome_json_for(FR31A))
NOFILTER_ID = P.genome_id_for(P.genome_json_for(NOFILTER))


# ---------------------------------------------------------------------------
# real-state guard: nothing real is read for rows, written, or created
# ---------------------------------------------------------------------------
def _sha(p: Path) -> Optional[str]:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _real_snapshot() -> Dict[str, Any]:
    reports = REPO / "reports" / "factory"
    return {
        "unseal_log_exists": REAL_LOG.exists(),
        "registry": _sha(REAL_REGISTRY),
        "holdout_sums": _sha(REAL_HOLDOUT / "SHA256SUMS"),
        "holdout_sealed": _sha(REAL_HOLDOUT / "SEALED"),
        "holdout_manifest": _sha(REAL_HOLDOUT / "manifest.json"),
        "reports": sorted(p.name for p in reports.glob("holdout_*")) + sorted(p.name for p in reports.glob("score_*")),
    }


@pytest.fixture(autouse=True)
def _real_state_guard(monkeypatch):
    monkeypatch.setenv("MP_GIT_REV", GIT_REV)
    before = _real_snapshot()
    assert before["unseal_log_exists"] is False, "the real unseal log must not exist while these tests run"
    yield
    after = _real_snapshot()
    assert after == before
    assert not REAL_LOG.exists()


# ---------------------------------------------------------------------------
# synthetic sealed roots
# ---------------------------------------------------------------------------
_CITIES = (("NY", "KXHIGHNY", "KNYC"), ("CHI", "KXHIGHCHI", "KMDW"), ("LAX", "KXHIGHLAX", "KLAX"))


def _ladder_rows(city: str, series: str, station: str, date: str, *, payoff_bad_market: bool = False,
                 truth_bad_market: bool = False, unsettled_market: bool = False) -> List[Dict[str, Any]]:
    rows = []
    for k in range(3):
        ticker = f"{series}-26{date[5:7]}{date[8:10]}-T{70 + k}"
        result = "yes" if k % 2 else "no"
        payoff = "true"
        truth = "true"
        if k == 0 and payoff_bad_market:
            payoff = "false"
        if k == 1 and truth_bad_market:
            truth = "false"
        if k == 2 and unsettled_market:
            result = ""
        for si in range(2):
            rows.append({
                "series": series, "city": city, "station": station, "target_date": date,
                "event_ticker": f"{series}-26{date[5:7]}{date[8:10]}", "market_ticker": ticker,
                "ts_utc": f"{date}T{10 + si:02d}:00:00+00:00", "minutes_to_close": 1500 - 60 * si,
                "close_time_utc": f"{date}T23:59:00+00:00", "strike_type": "greater", "floor_strike": 70 + k,
                "cap_strike": "", "yes_sub_title": f"{70 + k} or above", "yes_bid": 0.2 + 0.05 * k,
                "yes_ask": 0.3 + 0.05 * k, "no_bid": 0.7 - 0.05 * k, "no_ask": 0.8 - 0.05 * k, "last": 0.25,
                "price_mean": 0.25, "yes_bid_low": 0.2, "yes_ask_high": 0.35, "volume": 10, "open_interest": 5,
                "has_quote": "true", "result": result, "expiration_value": 72.0, "cli_high": 72.0,
                "recomputed_yes_expval": "true", "recomputed_yes_cli": "true", "payoff_matches_kalshi": payoff,
                "truth_agrees": truth,
            })
    return rows


def make_sealed_root(root: Path, dates=("2026-08-01", "2026-08-02"), *, sealed: bool = True,
                     payoff_bad: bool = False, truth_bad: bool = False, unsettled: bool = False) -> Path:
    """A tiny sealed root: SEALED, SHA256SUMS, manifest.json (real key names), CSVs in LADDER_COLUMNS."""
    root.mkdir(parents=True)
    sums: List[str] = []
    days: List[Dict[str, Any]] = []
    for di, date in enumerate(dates):
        for ci, (city, series, station) in enumerate(_CITIES):
            flag_day = (di == 0 and ci == 0)
            rows = _ladder_rows(city, series, station, date, payoff_bad_market=payoff_bad and flag_day,
                                truth_bad_market=truth_bad and flag_day, unsettled_market=unsettled and flag_day)
            df = pd.DataFrame(rows, columns=list(LADDER_COLUMNS))
            d = root / series
            d.mkdir(exist_ok=True)
            p = d / f"{date}.csv"
            df.to_csv(p, index=False, lineterminator="\n")
            sums.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {series}/{date}.csv")
            tickers = sorted(set(df["market_ticker"]))
            detail = []
            for t in tickers:
                sub = df[df["market_ticker"] == t].iloc[0]
                detail.append({"market_ticker": t, "strike_type": "greater", "floor_strike": float(sub["floor_strike"]),
                               "cap_strike": None, "result": str(sub["result"]) or None, "candles": 2, "requests": 1})
            n_payoff_bad = int((df.groupby("market_ticker")["payoff_matches_kalshi"].first() == "false").sum())
            n_truth_bad = int((df.groupby("market_ticker")["truth_agrees"].first() == "false").sum())
            days.append({
                "city": city, "series": series, "station": station, "target_date": date, "csv": f"{series}/{date}.csv",
                "event_ticker": f"{series}-x", "fetched_at_utc": "2026-09-02T21:00:00Z", "empty": False,
                "empty_reason": None, "rows": int(len(df)), "quoted_rows": int(len(df)), "markets": len(tickers),
                "markets_with_candles": len(tickers), "payoff_checked": len(tickers),
                "payoff_matched": len(tickers) - n_payoff_bad, "payoff_checked_cli": len(tickers),
                "payoff_matched_cli": len(tickers) - n_payoff_bad, "truth_checked": len(tickers),
                "truth_disagreements": [{"market_ticker": "x"}] * n_truth_bad, "missing_expiration_value": [],
                "bracket_spec_errors": [], "http_failures": [], "market_detail": detail,
            })
    (root / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    manifest = {
        "generated_at_utc": "2026-09-02T21:31:14Z", "generator": "test", "prd": "test",
        "date_range": {"start": dates[0], "end": dates[-1], "calendar_days": len(dates)},
        "cities": [{"city": c, "series": s, "station": st} for c, s, st in _CITIES],
        "totals": {"days_requested": len(days), "days_with_rows": len(days), "days_empty": 0}, "days": days,
        "empty_days": [], "truth_disagreements": [], "schema": {"columns": list(LADDER_COLUMNS)},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if sealed:
        (root / "SEALED").write_text("Sealed test root -- the search-frame loader must refuse this root.\n",
                                     encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# tmp registry / config / promoted spec / revival doc
# ---------------------------------------------------------------------------
class Setup:
    def __init__(self, tmp: Path, *, family_status: str = "PROPOSED", genomes=(FR31A,), ratified: bool = True):
        self.tmp = tmp
        yaml_src = (REPO / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml").read_text(encoding="utf-8")
        self.config = tmp / "family.yaml"
        self.config.write_text(yaml_src.replace("family: weather/gfs_mex/taker/v1", f"family: {FAMILY}"),
                               encoding="utf-8")
        self.config_sha = fees_mod.sha256_file(str(self.config))
        self.registry_path = tmp / "registry.jsonl"
        reg = Registry(self.registry_path, repo_root=REPO)
        common = dict(lane="weather", source="gfs_mex", mode="taker", gene_spec_version=1, budget={}, picker="t",
                      thresholds={}, cutoff="2026-07-25")
        reg.write_family_line(FAMILY, config_sha256=self.config_sha, **common)
        self.ids = []
        for g in genomes:
            gid = P.genome_id_for(P.genome_json_for(g))
            self.ids.append(gid)
            if family_status in ("PROPOSED", "RATIFIED", "CLOSED"):
                reg.transition(FAMILY, "PROPOSED", genome_id=gid, evidence={"note": "test"})
        if family_status == "CLOSED":
            reg.transition(FAMILY, "CLOSED", genome_id=self.ids[0], evidence={"note": "closed in test"})
        # another family, CLOSED with a pooled-validation p (counts toward Holm) and one with no p at all
        reg.write_family_line(OTHER_CLOSED, config_sha256="0" * 64, **common)
        reg.transition(OTHER_CLOSED, "CLOSED", genome_id="deadbeefdeadbeef", evidence={"pooled_one_sided_p": 0.2895})
        reg.write_family_line(OTHER_BLANK, config_sha256="1" * 64, **dict(common, lane="gas", source="aaa"))
        self.promoted_dir = tmp / "promoted"
        self.promoted_dir.mkdir()
        for g in genomes:
            spec = P.build_spec(g, family=FAMILY, config_sha256=self.config_sha, frame_search_sha256="a" * 64,
                                calibration_dir=str(tmp), calibration_sha256="b" * 64, fee_type="quadratic",
                                fee_regime_sha256="", mode="shadow", registry_status="PROPOSED")
            P.write_promoted(spec, self.promoted_dir / f"{spec.genome_id}.json")
        self.revival = tmp / "REVIVAL.md"
        text = "# tmp copy\n\nNothing here is ratified yet.\n"
        if ratified:
            text += "\nRATIFIED 2026-09-06 -- test copy\n"
        self.revival.write_text(text, encoding="utf-8")
        self.unseal_log = tmp / "unseal_log.jsonl"
        self.reports = tmp / "reports"
        self.paths = H.HoldoutPaths(repo_root=REPO, unseal_log=self.unseal_log, revival_doc=self.revival,
                                    registry=self.registry_path, promoted_dir=self.promoted_dir,
                                    reports_dir=self.reports, family_config=self.config)

    def finalists(self, ids=None, family: str = FAMILY) -> Path:
        p = self.tmp / "finalists.json"
        p.write_text(json.dumps({"family": family, "finalists": list(ids if ids is not None else self.ids)}),
                     encoding="utf-8")
        return p

    def registry(self) -> Registry:
        return Registry(self.registry_path, repo_root=REPO)


# ---------------------------------------------------------------------------
# the planted frame
# ---------------------------------------------------------------------------
def planted_frame(n_dates: int = 21, name: str = "planted", tail_city_day: Optional[int] = 5) -> C.Frame:
    """One market per city-day; signal markets ((date + city) even) are what fr31a trades and NO wins there.

    Noise markets are outside fr31a's mask (far_margin: p_yes 0.5 > yes_ask -
    0.08) but inside nofilter_no's (band 5F+), and NO loses there, so the
    no-filter baseline is dragged negative on every date. One city-day
    carries a settled high 3 sigma above mu so the |z| >= 2.5 tail ratio is
    1 / (84 * 0.0124) = 0.96, inside [0.8, 1.25].
    """
    rows = []
    dates = np.array([f"2026-08-{d + 1:02d}" for d in range(n_dates)], dtype=str)
    n_markets = 4 * n_dates
    markets = np.array([f"KXHIGH-{m:04d}" for m in range(n_markets)], dtype=str)
    for m in range(n_markets):
        d, city = divmod(m, 4)
        signal = (d + city) % 2 == 0
        mu, sigma = 80.0, 2.0
        distance = 6.0
        mid = mu + distance
        edge = distance - 0.5
        yes_bid = 0.20 + 0.01 * (m % 5)
        yes_ask = yes_bid + 0.10
        p_yes = 0.02 if signal else 0.50
        settles = not signal  # NO wins on signal markets, loses on noise markets
        high = mu + (3.0 * sigma if (tail_city_day is not None and m == tail_city_day) else 0.0)
        lead = 16.0
        for si in range(3):
            ts = 1_785_000_000 + d * 86400 + si * 3600
            mtc = 2000.0 - 100.0 * si
            for dcode, direction in enumerate(C.DIRECTION_LABELS):
                for mcode, mode in enumerate(C.MODE_LABELS):
                    is_maker = mcode == 1
                    if direction == "buy_yes":
                        q = yes_ask if not is_maker else yes_bid
                        p_win, won = p_yes, settles
                    else:
                        q = (1 - yes_bid) if not is_maker else (1 - yes_ask)
                        p_win, won = 1 - p_yes, not settles
                    pp = round(q + 0.01, 10)
                    fee = 0.0 if is_maker else 0.01
                    ex = True
                    realized = float(won) - pp - fee
                    rows.append(dict(
                        city_code=city, target_date_code=d, market_code=m, ts_utc=ts, minutes_to_close=mtc,
                        window_code=int(feat.window_code(np.array([mtc]))[0]), direction_code=dcode, mode_code=mcode,
                        band_code=int(feat.band_code(np.array([distance]))[0]), lead_bucket_code=C.lead_bucket_code(lead),
                        lead_hours=lead, p_yes=p_yes, p_win=p_win, mu_f=mu, sigma_f=sigma, midpoint_f=mid,
                        distance_f=distance, edge_distance_f=edge, yes_bid=yes_bid, yes_ask=yes_ask,
                        no_bid=1 - yes_ask, no_ask=1 - yes_bid, last=yes_bid, price_mean=(yes_bid + yes_ask) / 2,
                        volume=10.0, open_interest=5.0, quote=q, price_paid=pp, fee_per_contract=fee, executable=ex,
                        sandbox_admissible=ex, floor_strike=mid - 0.5, cap_strike=mid + 0.5, strike_type_code=0,
                        won=won, realized_per_contract=realized, result_code=int(settles), settles_yes=settles,
                        expiration_value=high, cli_high=high, truth_agrees=1, payoff_matches_kalshi=1,
                        maker_yes_fill=True, maker_no_fill=True, fwd_min_ask=yes_ask, fwd_max_bid=yes_bid,
                        yes_bid_low=yes_bid, yes_ask_high=yes_ask, ev_per_contract=p_win - pp - fee,
                    ))
    df = pd.DataFrame(rows)
    order = np.lexsort((df["mode_code"].to_numpy(), df["direction_code"].to_numpy(), df["ts_utc"].to_numpy(),
                        df["market_code"].to_numpy()))
    df = df.iloc[order].reset_index(drop=True)
    visible = {k: df[k].to_numpy().astype(dt) for k, dt in C.VISIBLE_DTYPES.items()}
    hidden = {k: df[k].to_numpy().astype(dt) for k, dt in C.HIDDEN_DTYPES.items()}
    block_starts = np.searchsorted(visible["market_code"], np.arange(n_markets + 1)).astype(np.int64)
    F = C.Frame(name=name, visible=visible, hidden=hidden, dates=dates, markets=markets, block_starts=block_starts,
                provenance={"frame_sha256": "planted" + "0" * 57, "availability_lag_min": 240, "truth_filter": True})
    F.validate()
    F.twin_index = np.arange(F.n_rows, dtype=np.int64)
    return F


class RecordingBuilder:
    """A frame builder that hands out the planted frame and records every call."""

    def __init__(self, frame: Optional[C.Frame] = None):
        self.frame = frame or planted_frame()
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, ladders, *, spec, embargo_days, root):
        self.calls.append({"n_rows": int(len(ladders)), "embargo_days": int(embargo_days), "root": Path(root),
                           "genome_id": spec.genome_id, "attrs": dict(ladders.attrs)})
        F = copy_frame(self.frame, name=f"holdout_e{embargo_days}")
        F.twin_index = np.arange(F.n_rows, dtype=np.int64)
        twin = copy_frame(self.frame, name="gefs_twin")
        return F, twin


def _run_holdout(setup: Setup, root: Path, *, tag: str = TAG, finalists: Optional[Path] = None,
                 builder: Optional[RecordingBuilder] = None, n_boot: int = 400) -> tuple:
    lines: List[str] = []
    builder = builder or RecordingBuilder()
    outcome = H.run_holdout(finalists_path=finalists or setup.finalists(), unseal_tag=tag, root=root,
                            paths=setup.paths, n_boot=n_boot, frame_builder=builder, out=lines.append)
    return outcome, lines, builder


# ===========================================================================
# 1. unseal protocol refusals -- nothing read, nothing written
# ===========================================================================
def _assert_nothing_written(setup: Setup):
    assert not setup.unseal_log.exists()
    assert not setup.reports.exists() or not list(setup.reports.glob("*.json"))


def test_refuses_without_tag(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(H.UnsealRefused, match="--unseal RATIFIED-<date> is required"):
        _run_holdout(setup, root, tag=None)
    _assert_nothing_written(setup)


@pytest.mark.parametrize("tag", ["RATIFIED-2026-09-07", "ratified-2026-09-06", "2026-09-06", "RATIFIED"])
def test_refuses_wrong_or_malformed_tag(tmp_path, tag):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(H.UnsealRefused, match="RATIFIED"):
        _run_holdout(setup, root, tag=tag)
    _assert_nothing_written(setup)


def test_refuses_when_the_doc_is_not_ratified(tmp_path):
    setup = Setup(tmp_path, ratified=False)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(H.UnsealRefused, match="nothing is ratified yet"):
        _run_holdout(setup, root)
    _assert_nothing_written(setup)


def test_the_real_revival_doc_carries_no_ratified_line_today():
    # The real command must refuse today; this pins the reason (read-only on the real doc).
    assert H.ratified_dates(REAL_REVIVAL) == []
    with pytest.raises(H.UnsealRefused, match="nothing is ratified yet"):
        H.assert_tag_ratified(TAG, REAL_REVIVAL)


def test_refuses_more_than_three_finalists(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    fin = setup.finalists(["a" * 16, "b" * 16, "c" * 16, "d" * 16])
    with pytest.raises(H.UnsealRefused, match=r"names 4 genomes; at most 3"):
        _run_holdout(setup, root, finalists=fin)
    _assert_nothing_written(setup)


def test_refuses_a_closed_family_genome(tmp_path):
    setup = Setup(tmp_path, family_status="CLOSED")
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(H.UnsealRefused, match="is CLOSED in the registry.*a rerun is a NEW family"):
        _run_holdout(setup, root)
    _assert_nothing_written(setup)


def test_refuses_a_genome_not_on_a_proposed_line(tmp_path):
    setup = Setup(tmp_path, family_status="OPEN")
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(H.UnsealRefused, match="not on any PROPOSED/RATIFIED transition line"):
        _run_holdout(setup, root)


def test_refuses_a_root_without_the_sealed_marker(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_copy", sealed=False)
    with pytest.raises(H.UnsealRefused, match="carries no SEALED marker"):
        _run_holdout(setup, root)
    _assert_nothing_written(setup)


def test_refuses_a_root_dated_inside_the_development_set(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_dev", dates=("2026-07-20", "2026-08-01"))
    with pytest.raises(H.UnsealRefused, match="dated <= 2026-07-25"):
        _run_holdout(setup, root)


def test_refuses_a_promoted_spec_from_another_family_or_config(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    spec = P.build_spec(FR31A, family=FAMILY, config_sha256="c" * 64, frame_search_sha256="a" * 64,
                        calibration_dir=str(tmp_path), calibration_sha256="b" * 64, fee_type="quadratic",
                        fee_regime_sha256="", mode="shadow", registry_status="PROPOSED")
    P.write_promoted(spec, setup.promoted_dir / f"{spec.genome_id}.json")
    with pytest.raises(H.UnsealRefused, match="config_sha256 .* != registry family line"):
        _run_holdout(setup, root)


# ===========================================================================
# 2. once per family per root / once per genome per root / quarter quota
# ===========================================================================
def test_second_unseal_for_the_same_family_and_root_is_refused_quoting_the_prior_line(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _lines, _b = _run_holdout(setup, root)
    assert outcome.verdicts == {FR31A_ID: "PASS"}
    first = H.read_unseal_log(setup.unseal_log)
    assert len(first) == 1
    # a second family look on the same root, with a different finalist file, is refused
    setup2_fin = setup.finalists([FR31A_ID])
    with pytest.raises(H.UnsealRefused) as ei:
        H.run_holdout(finalists_path=setup2_fin, unseal_tag=TAG, root=root, paths=setup.paths, n_boot=100,
                      frame_builder=RecordingBuilder(), out=lambda s: None)
    msg = str(ei.value)
    assert "once per family per root" in msg
    assert json.dumps(first[0], sort_keys=True) in msg  # the prior line, verbatim
    assert len(H.read_unseal_log(setup.unseal_log)) == 1  # nothing appended by the refusal


def test_second_score_of_the_same_genome_on_the_same_root_is_refused(tmp_path):
    setup = Setup(tmp_path)
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_sealed_root(tmp_path / "ladders_2026-09", dates=("2026-09-01", "2026-09-02"))
    _run_holdout(setup, hold)
    lines: List[str] = []
    out = H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, n_boot=200,
                      frame_builder=RecordingBuilder(), out=lines.append)
    assert out.verdicts == {FR31A_ID: "PASS"}
    log = H.read_unseal_log(setup.unseal_log)
    assert [ln["command"] for ln in log] == ["holdout", "score"]
    # a second scoring is refused at every layer: the genome is now RATIFIED (not PROPOSED) ...
    with pytest.raises(H.UnsealRefused, match="already RATIFIED"):
        H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, n_boot=200,
                    frame_builder=RecordingBuilder(), out=lambda s: None)
    # ... and, independently, the unseal log itself refuses the same genome on the same root, quoting the line
    seal = H.inspect_root_seal(r3, REPO)
    with pytest.raises(H.UnsealRefused) as ei:
        H.assert_unseal_allowed(log, command="score", family=FAMILY, genome_ids=[FR31A_ID], root=seal.relpath,
                                root_sha256=seal.sha256sums_digest)
    assert "no second scoring of the same genome on the same root" in str(ei.value)
    assert json.dumps(log[1], sort_keys=True) in str(ei.value)
    assert H.genome_status(setup.registry(), FAMILY, FR31A_ID) == "RATIFIED"
    assert len(H.read_unseal_log(setup.unseal_log)) == 2


def test_score_refuses_the_holdout_root_for_a_genome_already_scored_there(tmp_path):
    setup = Setup(tmp_path)
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    _run_holdout(setup, hold)
    with pytest.raises(H.UnsealRefused, match="already scored on root .* by 'holdout'"):
        H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=hold, paths=setup.paths, n_boot=100,
                    frame_builder=RecordingBuilder(), out=lambda s: None)


def test_at_most_three_unseals_per_quarter(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    now = H._now()
    q = H.quarter_of(now)
    setup.unseal_log.write_text("".join(
        json.dumps({"command": "holdout", "family": f"f{i}", "genome_ids": [f"g{i}"], "root": f"data/r{i}",
                    "root_sha256": str(i) * 64, "ts": now.isoformat(), "quarter": q, "tag": TAG}) + "\n"
        for i in range(3)), encoding="utf-8")
    with pytest.raises(H.UnsealRefused, match=rf"3 unseals already recorded in {q} \(cap 3 per quarter\)"):
        _run_holdout(setup, root)
    assert len(H.read_unseal_log(setup.unseal_log)) == 3


def test_quarter_of():
    assert H.quarter_of("2026-09-06T12:00:00+00:00") == "2026Q3"
    assert H.quarter_of("2026-10-01T00:00:00+00:00") == "2026Q4"
    assert H.quarter_of("2026-01-31T00:00:00Z") == "2026Q1"


# ===========================================================================
# 3. the log line is written BEFORE any price row is read
# ===========================================================================
def test_unseal_line_is_on_disk_before_the_first_row_is_read(tmp_path, monkeypatch):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    seen: Dict[str, Any] = {}
    real_loader = H._load_ladders_unchecked

    def spy(r, *a, **k):
        seen["log_at_read"] = H.read_unseal_log(setup.unseal_log)
        seen["root"] = Path(r)
        return real_loader(r, *a, **k)

    monkeypatch.setattr(H, "_load_ladders_unchecked", spy)
    outcome, _lines, _b = _run_holdout(setup, root)
    assert seen["root"] == root
    assert len(seen["log_at_read"]) == 1, "the unseal line must be appended before the tape is opened"
    ln = seen["log_at_read"][0]
    assert ln["command"] == "holdout" and ln["family"] == FAMILY and ln["genome_ids"] == [FR31A_ID]
    assert ln["tag"] == TAG and ln["ratified_date"] == "2026-09-06"
    assert ln["git_rev"] == GIT_REV and ln["ts"] and ln["quarter"] == H.quarter_of(ln["ts"])
    assert ln["root_sha256"] == hashlib.sha256((root / "SHA256SUMS").read_bytes()).hexdigest()
    assert ln["sha256sums_entries"] == 6
    assert "manifest.json exposes days[].market_detail[].result" in ln["caveat"]
    # append-only: the line survives the run and the record carries its line number
    assert H.read_unseal_log(setup.unseal_log) == [ln]
    assert outcome.record.line_no == 1


def test_open_sealed_root_refuses_without_an_appended_record(tmp_path):
    root = make_sealed_root(tmp_path / "ladders_holdout")
    rec = H.UnsealRecord(command="holdout", tag=TAG, ratified_date="2026-09-06", family=FAMILY, genome_ids=[FR31A_ID],
                         root="x", root_sha256="0" * 64, sha256sums_entries=1)
    with pytest.raises(H.HoldoutAbort, match="before the unseal line was appended"):
        H.open_sealed_root(root, rec)


def test_append_unseal_line_aborts_on_an_empty_git_rev(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "git_rev", lambda *_a, **_k: "")
    rec = H.UnsealRecord(command="holdout", tag=TAG, ratified_date="2026-09-06", family=FAMILY, genome_ids=[FR31A_ID],
                         root="x", root_sha256="0" * 64, sha256sums_entries=1)
    with pytest.raises(H.UnsealRefused, match="git rev is empty"):
        H.append_unseal_line(tmp_path / "log.jsonl", rec)
    assert not (tmp_path / "log.jsonl").exists()


# ===========================================================================
# 4. scoring: kernel, gates, Holm, hash before numbers, report, registry
# ===========================================================================
def test_holdout_pass_and_fail_verdicts_with_holm_across_families(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, lines, builder = _run_holdout(setup, root)
    assert outcome.verdicts == {FR31A_ID: "PASS", NOFILTER_ID: "HALT"}
    assert outcome.exit_code == H.EXIT_PASS
    res = outcome.doc["result"]
    e = res["genomes"][FR31A_ID]
    assert e["kernel"]["trades"] == 42 and e["kernel"]["dates"] == 21 and e["kernel"]["cities"] == 4
    assert e["kernel"]["realized"] > 0.04 and e["kernel"]["boot_lo"] > 0
    assert e["one_sided_p"] == 0.0
    assert all(g["pass"] for g in e["gates"].values()), {k: g for k, g in e["gates"].items() if not g["pass"]}
    assert e["r3"]["6_cold_season_month"]["pass"] is None  # PENDING, never failed
    assert e["gates"]["tail_ratio_in_range"]["value"] == pytest.approx(1 / (84 * 2 * (1 - 0.9937903346742238)), rel=1e-6)
    f = res["genomes"][NOFILTER_ID]
    assert f["gates"]["paired_vs_nofilter_lo_gt0"]["pass"] is False  # paired against itself is exactly 0
    assert f["kernel"]["realized"] < 0
    # Holm: the two finalists + the two other registry families (one with a pooled p, one unknown -> 1.0)
    holm = res["holm"]
    assert holm["m"] == 4 and holm["alpha"] == 0.05
    keys = set(holm["entries"])
    assert keys == {f"{FAMILY}:{FR31A_ID}", f"{FAMILY}:{NOFILTER_ID}", OTHER_CLOSED, OTHER_BLANK}
    assert holm["entries"][OTHER_CLOSED]["p"] == 0.2895
    assert holm["entries"][OTHER_BLANK]["p"] == 1.0 and "p = 1.0" in holm["entries"][OTHER_BLANK]["provenance"]
    assert holm["entries"][f"{FAMILY}:{FR31A_ID}"]["reject"] is True
    assert holm["entries"][f"{FAMILY}:{NOFILTER_ID}"]["reject"] is False
    # the embargo-2 sensitivity rebuilt the frame exactly once, with the search frame's other settings
    assert sorted(c["embargo_days"] for c in builder.calls) == [1, 2]
    assert all(sr.SEALED_EVALUATION_ATTR in c["attrs"] and "ladder_root" not in c["attrs"] for c in builder.calls)
    # truth filter accounting from the tape (no drops planted here)
    tf = res["truth_filter"]
    assert tf["markets"] == 18 and tf["city_days"] == 6 and tf["markets_dropped"] == 0
    assert tf["fraction_city_days_affected"] == 0.0 and tf["criterion_lt_10pct"] is True
    assert any(line.startswith("truth filter: dropped 0 of 18 markets") for line in lines)


def test_result_sha256_is_printed_before_any_number_and_matches_the_report(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, lines, _b = _run_holdout(setup, root)
    idx_sha = next(i for i, ln in enumerate(lines) if re.match(r"^result_sha256 [0-9a-f]{64}$", ln))
    number = re.compile(r"[+-]?\d+\.\d{3,}")  # any formatted statistic
    first_number = next(i for i, ln in enumerate(lines) if number.search(ln))
    first_stat = next(i for i, ln in enumerate(lines) if ln.startswith("truth filter:") or "pooled OOS mean" in ln)
    assert idx_sha < first_number and idx_sha < first_stat
    assert all(not number.search(ln) for ln in lines[:idx_sha])
    sha = lines[idx_sha].split()[1]
    assert sha == outcome.result_sha256
    doc = json.loads(outcome.report_path.read_text(encoding="utf-8"))
    assert doc["result_sha256"] == sha
    assert H.result_sha256(doc["result"]) == sha  # recomputable from the canonical numbers alone
    assert outcome.report_path.name == f"holdout_{FAMILY.replace('/', '_')}_ladders_holdout.json"
    assert doc["meta"]["unseal_line_no"] == 1 and doc["meta"]["git_rev"] == GIT_REV
    assert "manifest.json exposes" in doc["meta"]["caveat"]


def test_registry_transitions_after_a_holdout(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _lines, _b = _run_holdout(setup, root)
    reg = setup.registry()
    assert reg.status(FAMILY) == "PROPOSED"  # re-asserted with evidence; no new status invented
    last = [ln for ln in reg.lines() if ln.get("family") == FAMILY][-1]
    assert last["status"] == "PROPOSED" and last["genome_id"] == FR31A_ID
    ev = last["evidence"]
    assert ev["result_sha256"] == outcome.result_sha256
    assert ev["holdout"]["verdict"] == "PASS" and ev["holdout"]["root"].endswith("ladders_holdout")
    assert ev["holdout"]["root_sha256"] == outcome.record.root_sha256
    assert ev["holdout"]["one_sided_p"] == 0.0 and ev["holdout"]["holm_p_adj"] == 0.0
    assert NOFILTER_ID in ev["holdout"]["finalists_failed"]
    assert H.holdout_pass_lines(reg, FAMILY, FR31A_ID)


def test_family_is_halted_when_no_finalist_passes(tmp_path):
    setup = Setup(tmp_path, genomes=(NOFILTER,))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _lines, _b = _run_holdout(setup, root)
    assert outcome.verdicts == {NOFILTER_ID: "HALT"} and outcome.exit_code == H.EXIT_HALT
    reg = setup.registry()
    assert reg.status(FAMILY) == "HALT"
    last = [ln for ln in reg.lines() if ln.get("family") == FAMILY][-1]
    assert last["status"] == "HALT" and last["genome_id"] == NOFILTER_ID
    assert last["evidence"]["holdout"]["failing"]


def test_score_ratifies_on_pass_and_halts_on_failure(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_sealed_root(tmp_path / "ladders_2026-09", dates=("2026-09-01", "2026-09-02", "2026-09-03"))
    _run_holdout(setup, hold)
    lines: List[str] = []
    out = H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, as_of="2026-09-02", paths=setup.paths,
                      n_boot=200, frame_builder=RecordingBuilder(), out=lines.append)
    assert out.verdicts == {FR31A_ID: "PASS"} and out.exit_code == H.EXIT_PASS
    reg = setup.registry()
    assert reg.status(FAMILY) == "RATIFIED"
    last = [ln for ln in reg.lines() if ln.get("family") == FAMILY][-1]
    assert last["status"] == "RATIFIED" and last["genome_id"] == FR31A_ID
    r3ev = last["evidence"]["r3"]
    assert last["evidence"]["result_sha256"] == out.result_sha256
    assert r3ev["checks"]["6_cold_season_month"] is None and r3ev["checks"]["5_point_estimate_ge_4c"] is True
    assert set(r3ev["checks"]) == {"1_frozen_source_ci_lo_gt0", "2_gefs_realized_ge0", "3_beats_nofilter_baseline",
                                   "4_tail_ratio_in_range", "5_point_estimate_ge_4c", "6_cold_season_month"}
    # --as-of restricted the tape and is recorded in the unseal line
    log = H.read_unseal_log(setup.unseal_log)
    assert log[-1]["command"] == "score" and log[-1]["as_of"] == "2026-09-02" and log[-1]["genome_ids"] == [FR31A_ID]
    assert out.report_path.name == f"score_{FR31A_ID}_ladders_2026-09.json"
    idx_sha = next(i for i, ln in enumerate(lines) if ln.startswith("result_sha256 "))
    assert all(not re.search(r"[+-]?\d+\.\d{3,}", ln) for ln in lines[:idx_sha])
    # a failing genome (never cleared the holdout) is refused; once it has, R3 failure = HALT #3
    with pytest.raises(H.UnsealRefused, match="no PASS holdout evidence"):
        H.run_score(genome_id=NOFILTER_ID, unseal_tag=TAG, root=r3, paths=setup.paths, n_boot=100,
                    frame_builder=RecordingBuilder(), out=lambda s: None)


def test_score_halts_a_proposed_genome_that_fails_r3(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A,))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_sealed_root(tmp_path / "ladders_2026-09", dates=("2026-09-01",))
    _run_holdout(setup, hold)
    # on the R3 root the planted edge is gone: NO loses everywhere
    F = planted_frame()
    F.hidden["won"][:] = F.visible["direction_code"] == 0
    F.hidden["realized_per_contract"][:] = F.hidden["won"].astype(np.float64) - F.visible["price_paid"] - F.visible["fee_per_contract"]
    out = H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, n_boot=200,
                      frame_builder=RecordingBuilder(F), out=lambda s: None)
    assert out.verdicts == {FR31A_ID: "HALT"} and out.exit_code == H.EXIT_HALT
    reg = setup.registry()
    assert reg.status(FAMILY) == "HALT"
    last = [ln for ln in reg.lines() if ln.get("family") == FAMILY][-1]
    assert last["status"] == "HALT" and "boot_lo_gt0" in last["evidence"]["r3"]["failing"]


def test_score_refuses_a_genome_that_is_not_proposed(tmp_path):
    setup = Setup(tmp_path, family_status="CLOSED")
    r3 = make_sealed_root(tmp_path / "ladders_2026-09", dates=("2026-09-01",))
    with pytest.raises(H.UnsealRefused, match="is CLOSED"):
        H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, n_boot=100,
                    frame_builder=RecordingBuilder(), out=lambda s: None)
    _assert_nothing_written(setup)


def test_truth_filter_stats_count_drops_per_market_and_city_day(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout", payoff_bad=True, truth_bad=True, unsettled=True)
    outcome, lines, builder = _run_holdout(setup, root)
    tf = outcome.doc["result"]["truth_filter"]
    assert tf["markets"] == 18 and tf["city_days"] == 6
    assert tf["markets_dropped"] == 3 and tf["markets_dropped_payoff_mismatch"] == 1
    assert tf["markets_dropped_truth_disagree"] == 1 and tf["markets_dropped_result_unsettled"] == 1
    assert tf["city_days_affected"] == 1 and tf["city_days_fully_dropped"] == 1
    assert tf["fraction_city_days_affected"] == pytest.approx(1 / 6) and tf["criterion_lt_10pct"] is False
    # the payoff-mismatch market was removed from the tape before the frame builder saw it
    assert outcome.doc["result"]["payoff_mismatch_markets_dropped"] == ["KXHIGHNY-260801-T70"]
    assert builder.calls[0]["n_rows"] == 36 - 2
    assert any("criterion < 10 %: FAIL" in ln for ln in lines)


# ===========================================================================
# 5. the search-frame builder STILL refuses the unsealed root
# ===========================================================================
def test_search_frame_builders_still_refuse_the_tmp_sealed_root(tmp_path):
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(sr.SealedDataError):
        load_ladders(root)
    with pytest.raises(sr.SealedDataError):
        ev.load_search_ladders(root)
    # a bare read of a copied CSV is caught by the content gate (dates > 2026-07-25)
    df = pd.read_csv(root / "KXHIGHNY" / "2026-08-01.csv")
    with pytest.raises(sr.SealedDataError):
        sr.assert_frame_not_sealed(df)
    with pytest.raises(sr.SealedDataError):
        ev.build_opportunity_frame(df, df, df, None)


def test_the_sanctioned_tape_is_marked_and_the_mark_does_not_survive_concat(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    rec = H.UnsealRecord(command="holdout", tag=TAG, ratified_date="2026-09-06", family=FAMILY, genome_ids=[FR31A_ID],
                         root="x", root_sha256="0" * 64, sha256sums_entries=6)
    H.append_unseal_line(setup.unseal_log, rec)
    df = H.open_sealed_root(root, rec)
    assert df.attrs[sr.SEALED_EVALUATION_ATTR]["line_no"] == 1
    assert "ladder_root" not in df.attrs and df.attrs["unsealed_root"] == str(root.resolve())
    sr.assert_frame_not_sealed(df)  # the sanctioned evaluation passes the content gate
    bare = pd.DataFrame(df.to_dict("list"))  # no attrs, like a bare read_csv of a copy
    merged = pd.concat([df, bare], ignore_index=True)
    assert not merged.attrs.get(sr.SEALED_EVALUATION_ATTR)
    with pytest.raises(sr.SealedDataError):
        sr.assert_frame_not_sealed(merged)  # ...and the mark is fragile by design
    stamped = df.copy()
    stamped.attrs["ladder_root"] = str(sr.SEALED_LADDER_ROOTS[0])
    with pytest.raises(sr.SealedDataError):
        sr.assert_frame_not_sealed(stamped)  # the origin gate is untouched


def test_the_bypass_symbols_live_only_in_holdout_py():
    allowed = {
        "_load_ladders_unchecked": {"src/data/kalshi_history.py", "scripts/backfill_ladders.py", "src/factory/holdout.py",
                                    "src/backtest/sealed_roots.py"},  # the docstring that names the sanctioned readers
        "SEALED_EVALUATION_ATTR": {"src/backtest/sealed_roots.py", "src/factory/holdout.py"},
        "open_sealed_root": {"src/factory/holdout.py"},
    }
    found: Dict[str, set] = {k: set() for k in allowed}
    for top in ("src", "scripts"):
        for p in (REPO / top).rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = p.relative_to(REPO).as_posix()
            for sym in allowed:
                if re.search(rf"\b{sym}\b", text):
                    found[sym].add(rel)
    for sym, files in found.items():
        assert files <= allowed[sym], f"{sym} referenced outside its allow-list: {sorted(files - allowed[sym])}"
        assert "src/factory/holdout.py" in files
    # kalshi_history defines the unchecked reader; backfill_ladders --stats is the pre-existing descriptive
    # reader the sealed_roots docstring sanctions. Neither sets the evaluation attr.
    assert "src/data/kalshi_history.py" not in found["SEALED_EVALUATION_ATTR"]


# ===========================================================================
# 6. --audit: manifest counts only, nothing written, no labels
# ===========================================================================
def test_audit_reads_only_the_manifest_and_writes_nothing(tmp_path, monkeypatch):
    root = make_sealed_root(tmp_path / "ladders_holdout", truth_bad=True, unsettled=True)
    opened: List[Path] = []
    real_open = pd.read_csv

    def spy(*a, **k):
        opened.append(a[0])
        return real_open(*a, **k)

    monkeypatch.setattr(pd, "read_csv", spy)
    monkeypatch.setattr(H, "_load_ladders_unchecked", lambda *a, **k: pytest.fail("audit must not open the tape"))
    a = H.audit_root(root, REPO)
    assert opened == []
    assert a["city_days_with_rows"] == 6 and a["markets"] == 18
    assert a["markets_result_settled"] == 17 and a["markets_result_unsettled"] == 1
    assert a["truth_disagreements"] == 1 and a["payoff_mismatched"] == 0
    assert a["city_days_affected"] == 1 and a["fraction_city_days_affected"] == pytest.approx(1 / 6)
    assert a["criterion_lt_10pct"] is False
    assert a["sealed_marker_present"] and a["sha256sums_present"]
    text = H.render_audit(a)
    assert "no unseal line written" in text and "criterion < 10 %: FAIL" in text
    assert not re.search(r"\b(yes|no)\b", text.replace("no unseal", "").replace("no CSV", ""))  # counts, never labels
    assert not (tmp_path / "unseal_log.jsonl").exists()
    assert sorted(p.name for p in root.iterdir()) == ["KXHIGHCHI", "KXHIGHLAX", "KXHIGHNY", "SEALED", "SHA256SUMS",
                                                       "manifest.json"]


def test_audit_on_the_real_manifest_is_read_only():
    if not (REAL_HOLDOUT / "manifest.json").exists():
        pytest.skip("real holdout manifest not on disk")
    a = H.audit_root(REAL_HOLDOUT, REPO)
    assert a["city_days_with_rows"] == 148 and a["markets"] == 888
    assert a["criterion_lt_10pct"] is True and a["fraction_city_days_affected"] < 0.10
    assert not REAL_LOG.exists()


# ===========================================================================
# 7. CLI wiring
# ===========================================================================
def _factory_module():
    spec = importlib.util.spec_from_file_location("factory_cli_holdout_test", REPO / "scripts" / "factory.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_holdout_and_score_are_implemented_and_help_works(capsys):
    mod = _factory_module()
    assert "holdout" not in mod.NOT_IMPLEMENTED and "score" not in mod.NOT_IMPLEMENTED
    for cmd in ("holdout", "score"):
        with pytest.raises(SystemExit) as ei:
            mod.main([cmd, "--help"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "--unseal RATIFIED-YYYY-MM-DD" in out
    src = (REPO / "scripts" / "factory.py").read_text(encoding="utf-8")
    assert 'NOT_IMPLEMENTED: tuple = ()' in src


def test_cli_refuses_today_against_the_real_doc_without_writing(tmp_path, capsys):
    mod = _factory_module()
    fin = tmp_path / "finalists.json"
    fin.write_text(json.dumps({"family": "weather/gfs_mex/taker/v1", "finalists": ["0c4b20502f2daf65"]}))
    rc = mod.main(["holdout", "--finalists", str(fin), "--unseal", "RATIFIED-2026-09-06",
                   "--unseal-log", str(tmp_path / "log.jsonl"), "--registry", str(tmp_path / "reg.jsonl")])
    err = capsys.readouterr().err
    assert rc == H.EXIT_REFUSED and "REFUSED" in err and "nothing is ratified yet" in err
    assert not (tmp_path / "log.jsonl").exists()


def test_cli_score_prints_the_hash_before_the_numbers(tmp_path, capsys, monkeypatch):
    setup = Setup(tmp_path, genomes=(FR31A,))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_sealed_root(tmp_path / "ladders_2026-09", dates=("2026-09-01",))
    _run_holdout(setup, hold)
    monkeypatch.setattr(H, "default_frame_builder", RecordingBuilder())
    mod = _factory_module()
    rc = mod.main(["score", "--genome", FR31A_ID, "--ladders", str(r3), "--unseal", TAG, "--n-boot", "200",
                   "--config", str(setup.config), "--promoted-dir", str(setup.promoted_dir),
                   "--registry", str(setup.registry_path), "--unseal-log", str(setup.unseal_log),
                   "--revival-doc", str(setup.revival), "--out-dir", str(setup.reports)])
    out = capsys.readouterr().out.splitlines()
    assert rc == H.EXIT_PASS
    idx_sha = next(i for i, ln in enumerate(out) if re.match(r"^result_sha256 [0-9a-f]{64}$", ln))
    number = re.compile(r"[+-]?\d+\.\d{3,}")
    assert all(not number.search(ln) for ln in out[:idx_sha])
    assert any(number.search(ln) for ln in out[idx_sha + 1:])
    assert setup.registry().status(FAMILY) == "RATIFIED"


def test_cli_audit_writes_nothing(tmp_path, capsys):
    root = make_sealed_root(tmp_path / "ladders_holdout")
    mod = _factory_module()
    rc = mod.main(["holdout", "--audit", "--ladders", str(root), "--unseal-log", str(tmp_path / "log.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and "manifest metadata only" in out and "criterion < 10 %: PASS" in out
    assert not (tmp_path / "log.jsonl").exists()


# ===========================================================================
# 8. compose: the factory-holdout service and the masks
# ===========================================================================
def test_compose_has_the_factory_holdout_service_and_keeps_the_masks():
    text = (REPO / "deploy" / "spark" / "docker-compose.lab.yml").read_text(encoding="utf-8")
    assert "factory-holdout:" in text
    assert "../../data/ladders_holdout:/app/data/ladders_holdout:ro" in text
    assert "../../data/ladders_2026-09:/app/data/ladders_2026-09:ro" in text
    factory_block = text.split("  factory:", 1)[1].split("  factory-holdout:", 1)[0]
    assert "target: /app/data/ladders_holdout" in factory_block and "target: /app/data/ladders_2026-09" in factory_block
    assert ".git" in text and "git show" in text  # the recorded caveat is written down, not fixed here
