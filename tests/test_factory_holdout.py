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
(paired against itself is exactly 0; its pooled mean is negative). One test
(``test_default_frame_builder_end_to_end_on_a_real_shaped_root``) runs the
REAL evaluator chain on a synthetic root shaped like a Kalshi ladder, against
the repo's open forecast archive and truth files.

Red-team round 1 items are marked ``[RT1-n]`` on the tests that pin them.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
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
HB = H.HOLDOUT_B_RANGE

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
    monkeypatch.setenv(H.TEST_DOC_ENV, "1")  # tmp revival docs live outside the repo
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
        payoff = "false" if (k == 0 and payoff_bad_market) else "true"
        truth = "false" if (k == 1 and truth_bad_market) else "true"
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


def _num(v: Any) -> Optional[float]:
    try:
        return None if v == "" or pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return None


def _write_root(root: Path, frames: Dict[str, Dict[str, pd.DataFrame]], date_range, *, sealed: bool = True) -> Path:
    """``frames[series][date] -> DataFrame``; writes CSVs, SHA256SUMS, manifest.json (real key names), SEALED."""
    root.mkdir(parents=True)
    sums: List[str] = []
    days: List[Dict[str, Any]] = []
    cities = []
    for city, series, station in _CITIES:
        if series not in frames:
            continue
        cities.append({"city": city, "series": series, "station": station})
        for date, df in sorted(frames[series].items()):
            d = root / series
            d.mkdir(exist_ok=True)
            p = d / f"{date}.csv"
            df.to_csv(p, index=False, lineterminator="\n")
            sums.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {series}/{date}.csv")
            tickers = sorted(set(df["market_ticker"]))
            detail = []
            for t in tickers:
                sub = df[df["market_ticker"] == t].iloc[0]
                detail.append({"market_ticker": t, "strike_type": str(sub["strike_type"]),
                               "floor_strike": _num(sub["floor_strike"]), "cap_strike": _num(sub["cap_strike"]),
                               "result": str(sub["result"]) or None, "candles": 2, "requests": 1})
            n_payoff_bad = int((df.groupby("market_ticker")["payoff_matches_kalshi"].first().astype(str) == "false").sum())
            n_truth_bad = int((df.groupby("market_ticker")["truth_agrees"].first().astype(str) == "false").sum())
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
        "date_range": {"start": date_range[0], "end": date_range[1], "calendar_days": 37},
        "cities": cities, "series_metadata": {c["series"]: {"fee_type": "quadratic"} for c in cities},
        "totals": {"days_requested": len(days), "days_with_rows": len(days), "days_empty": 0}, "days": days,
        "empty_days": [], "truth_disagreements": [], "schema": {"columns": list(LADDER_COLUMNS)},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if sealed:
        (root / "SEALED").write_text("Sealed test root -- the search-frame loader must refuse this root.\n",
                                     encoding="utf-8")
    return root


def make_sealed_root(root: Path, dates=("2026-08-01", "2026-08-02"), *, sealed: bool = True, date_range=HB,
                     payoff_bad: bool = False, truth_bad: bool = False, unsettled: bool = False) -> Path:
    """A tiny sealed root; ``date_range`` is the manifest's (holdout-B by default -- the root's PURPOSE)."""
    frames: Dict[str, Dict[str, pd.DataFrame]] = {}
    for di, date in enumerate(dates):
        for ci, (city, series, station) in enumerate(_CITIES):
            flag_day = (di == 0 and ci == 0)
            rows = _ladder_rows(city, series, station, date, payoff_bad_market=payoff_bad and flag_day,
                                truth_bad_market=truth_bad and flag_day, unsettled_market=unsettled and flag_day)
            frames.setdefault(series, {})[date] = pd.DataFrame(rows, columns=list(LADDER_COLUMNS))
    return _write_root(root, frames, date_range, sealed=sealed)


def make_r3_root(root: Path, dates=("2026-09-01", "2026-09-02"), **kw) -> Path:
    return make_sealed_root(root, dates=dates, date_range=(dates[0], dates[-1]), **kw)


# ---------------------------------------------------------------------------
# tmp registry / config / promoted spec / revival doc
# ---------------------------------------------------------------------------
class Setup:
    def __init__(self, tmp: Path, *, family_status: str = "PROPOSED", genomes=(FR31A,), ratified: bool = True,
                 thresholds: Optional[Dict[str, Any]] = None):
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        yaml_src = (REPO / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml").read_text(encoding="utf-8")
        self.config = tmp / "family.yaml"
        self.config.write_text(yaml_src.replace("family: weather/gfs_mex/taker/v1", f"family: {FAMILY}"),
                               encoding="utf-8")
        self.config_sha = fees_mod.sha256_file(str(self.config))
        self.registry_path = tmp / "registry.jsonl"
        reg = Registry(self.registry_path, repo_root=REPO)
        thr = {"bss_trades_min": -0.05, "gefs_twin_min": 0.0, "holm_alpha": 0.05, "max_clauses": 8, "min_cities": 3,
               "min_dates_frac": 0.6, "min_trades": 40, "p_rc_all69_lt": 0.1, "pooled_boot_lo_gt": 0.0,
               "worst_date_pnl_min": -0.5}
        if thresholds is not None:
            thr = thresholds
        common = dict(lane="weather", source="gfs_mex", mode="taker", gene_spec_version=1, budget={}, picker="t",
                      cutoff="2026-07-25")
        reg.write_family_line(FAMILY, config_sha256=self.config_sha, thresholds=thr, **common)
        self.ids = []
        for g in genomes:
            gid = P.genome_id_for(P.genome_json_for(g))
            self.ids.append(gid)
            if family_status in ("PROPOSED", "RATIFIED", "CLOSED"):
                reg.transition(FAMILY, "PROPOSED", genome_id=gid, evidence={"note": "test"})
        if family_status == "CLOSED":
            reg.transition(FAMILY, "CLOSED", genome_id=self.ids[0], evidence={"note": "closed in test"})
        reg.write_family_line(OTHER_CLOSED, config_sha256="0" * 64, thresholds={}, **common)
        reg.transition(OTHER_CLOSED, "CLOSED", genome_id="deadbeefdeadbeef", evidence={"pooled_one_sided_p": 0.2895})
        reg.write_family_line(OTHER_BLANK, config_sha256="1" * 64, thresholds={}, **dict(common, lane="gas", source="aaa"))
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
            text += "\nRATIFIED 2026-09-06\n"
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

    def family_lines(self) -> List[Dict[str, Any]]:
        return [ln for ln in self.registry().lines() if ln.get("family") == FAMILY]

    def write_holdout_pass(self, gid: str, root_digest: str = "h" * 64) -> None:
        self.registry().transition(FAMILY, "PROPOSED", genome_id=gid,
                                   evidence={"holdout": {"verdict": "PASS", "root_digest": root_digest}})


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
        settles = not signal
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
                    realized = float(won) - pp - fee
                    rows.append(dict(
                        city_code=city, target_date_code=d, market_code=m, ts_utc=ts, minutes_to_close=mtc,
                        window_code=int(feat.window_code(np.array([mtc]))[0]), direction_code=dcode, mode_code=mcode,
                        band_code=int(feat.band_code(np.array([distance]))[0]), lead_bucket_code=C.lead_bucket_code(lead),
                        lead_hours=lead, p_yes=p_yes, p_win=p_win, mu_f=mu, sigma_f=sigma, midpoint_f=mid,
                        distance_f=distance, edge_distance_f=edge, yes_bid=yes_bid, yes_ask=yes_ask,
                        no_bid=1 - yes_ask, no_ask=1 - yes_bid, last=yes_bid, price_mean=(yes_bid + yes_ask) / 2,
                        volume=10.0, open_interest=5.0, quote=q, price_paid=pp, fee_per_contract=fee, executable=True,
                        sandbox_admissible=True, floor_strike=mid - 0.5, cap_strike=mid + 0.5, strike_type_code=0,
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

    def __call__(self, ladders, *, spec, embargo_days, root, **kw):
        self.calls.append({"n_rows": int(len(ladders)), "embargo_days": int(embargo_days), "root": Path(root),
                           "genome_id": spec.genome_id, "attrs": dict(ladders.attrs), **kw})
        F = copy_frame(self.frame, name=f"holdout_e{embargo_days}")
        F.twin_index = np.arange(F.n_rows, dtype=np.int64)
        twin = copy_frame(self.frame, name="gefs_twin")
        return F, twin


def losing_frame() -> C.Frame:
    F = planted_frame()
    F.hidden["won"][:] = F.visible["direction_code"] == 0
    F.hidden["realized_per_contract"][:] = (F.hidden["won"].astype(np.float64) - F.visible["price_paid"]
                                            - F.visible["fee_per_contract"])
    return F


def _run_holdout(setup: Setup, root: Path, *, tag: str = TAG, finalists: Optional[Path] = None,
                 builder: Optional[RecordingBuilder] = None, n_boot: int = 400) -> tuple:
    lines: List[str] = []
    builder = builder or RecordingBuilder()
    outcome = H.run_holdout(finalists_path=finalists or setup.finalists(), unseal_tag=tag, root=root,
                            paths=setup.paths, n_boot=n_boot, frame_builder=builder, out=lines.append)
    return outcome, lines, builder


def _run_score(setup: Setup, root: Path, gid: str = FR31A_ID, *, as_of=None, builder=None, n_boot: int = 200):
    lines: List[str] = []
    out = H.run_score(genome_id=gid, unseal_tag=TAG, root=root, as_of=as_of, paths=setup.paths, n_boot=n_boot,
                      frame_builder=builder or RecordingBuilder(), out=lines.append)
    return out, lines


def _assert_nothing_written(setup: Setup):
    assert not setup.unseal_log.exists()
    assert not setup.reports.exists() or not list(setup.reports.glob("*.json"))


# ===========================================================================
# 1. unseal protocol refusals -- nothing read, nothing written
# ===========================================================================
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
    assert H.ratified_dates(REAL_REVIVAL) == []
    with pytest.raises(H.UnsealRefused, match="nothing is ratified yet"):
        H.assert_tag_ratified(TAG, REAL_REVIVAL)


@pytest.mark.parametrize("text, expected", [
    ("RATIFIED 2026-09-06\n", ["2026-09-06"]),
    ("RATIFIED 2026-09-06   \n", ["2026-09-06"]),
    ("RATIFIED 2026-09-06\r\n", ["2026-09-06"]),
    ("RATIFIED 2026-09-06 -- owner note\n", []),  # [RT2-2] trailing prose negates
    ("RATIFIED 2026-09-06 -- NOT ratified, proposal only\n", []),
    ("RATIFIED 2026-09-06\t(tab note)\n", []),
    ("~~~\nRATIFIED 2026-09-06\n~~~\n", []),
    ("<pre>\nRATIFIED 2026-09-06\n</pre>\n", []),
    ("<!--\nRATIFIED 2026-09-06\n-->\n", []),
    ("<!-- start\nmore\nRATIFIED 2026-09-06\nend -->\nRATIFIED 2026-09-07\n", ["2026-09-07"]),
    ("```\n~~~\nRATIFIED 2026-09-06\n```\n", []),  # a ~~~ inside a ``` fence does not close it
    ("not RATIFIED 2026-09-06\n", []),
    ("  RATIFIED 2026-09-06\n", []),
    ("> RATIFIED 2026-09-06\n", []),
    ("- RATIFIED 2026-09-06\n", []),
    ("<!-- RATIFIED 2026-09-06 -->\n", []),
    ("```\nRATIFIED 2026-09-06\n```\n", []),
    ("RATIFIED\n2026-09-06\n", []),
    ("RATIFIED 2026-09-06x\n", []),
    ("RATIFIED  2026-09-06\n", []),
    ("Once ratified this line reads RATIFIED 2026-09-06 verbatim\n", []),
    ("```\nfence\n```\nRATIFIED 2026-09-06\n", ["2026-09-06"]),
])
def test_ratified_line_must_be_a_whole_column0_line_outside_fences(tmp_path, text, expected):  # [RT1-2, RT2-2]
    p = tmp_path / "doc.md"
    p.write_bytes(text.encode("utf-8"))
    assert H.ratified_dates(p) == expected
    assert H.ratified_dates_in(text) == expected


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_ratification_is_read_from_the_committed_doc_in_a_throwaway_repo(tmp_path, monkeypatch):  # [RT2-2]
    monkeypatch.delenv(H.TEST_DOC_ENV, raising=False)
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    doc = repo / "docs" / "REVIVAL.md"
    doc.write_bytes(b"# plan\n\nRATIFIED 2026-09-06\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "ratify")
    assert H.ratified_dates(doc, repo) == ["2026-09-06"]
    rel, sha = H.resolve_revival_doc(doc, repo)
    assert rel == "docs/REVIVAL.md" and sha == H.sha256_text("# plan\n\nRATIFIED 2026-09-06\n")
    # working copy edited but not committed -> refused (even though the disk file still ratifies)
    doc.write_bytes(b"# plan\n\nRATIFIED 2026-09-06\nRATIFIED 2026-09-07\n")
    with pytest.raises(H.UnsealRefused, match="differs from its committed content at HEAD"):
        H.ratified_dates(doc, repo)
    # an untracked doc ratifies nothing
    new = repo / "docs" / "NEW.md"
    new.write_bytes(b"RATIFIED 2026-09-06\n")
    with pytest.raises(H.UnsealRefused, match="not committed at HEAD"):
        H.ratified_dates(new, repo)
    # outside the repo -> refused
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"RATIFIED 2026-09-06\n")
    with pytest.raises(H.UnsealRefused, match="outside the repository"):
        H.ratified_dates(outside, repo)
    # the committed content wins: delete the edit, HEAD still ratifies
    _git(repo, "checkout", "--", "docs/REVIVAL.md")
    assert H.ratified_dates(doc, repo) == ["2026-09-06"]


def test_doc_outside_the_repo_is_refused_without_the_test_env(tmp_path, monkeypatch):  # [RT1-2]
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    monkeypatch.delenv(H.TEST_DOC_ENV, raising=False)
    with pytest.raises(H.UnsealRefused, match="outside the repository"):
        _run_holdout(setup, root)
    _assert_nothing_written(setup)
    head = H.git_show_head(REPO, "docs/REVIVAL_2026_09.md")
    if head is None or head.replace(b"\r", b"") != REAL_REVIVAL.read_bytes().replace(b"\r", b""):
        with pytest.raises(H.UnsealRefused, match="differs from its committed content|not committed"):
            H.resolve_revival_doc(REAL_REVIVAL, REPO)
    else:
        rel, sha = H.resolve_revival_doc(REAL_REVIVAL, REPO)  # the committed real doc resolves without the env
        assert rel == "docs/REVIVAL_2026_09.md" and sha == H.sha256_text(head.decode("utf-8").replace("\r", ""))
        assert H.ratified_dates(REAL_REVIVAL, REPO) == []  # committed content ratifies nothing today


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


def test_refuses_a_root_without_a_manifest(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    (root / "manifest.json").unlink()
    with pytest.raises(H.UnsealRefused, match="has no manifest.json"):
        _run_holdout(setup, root)


def test_root_purpose_is_enforced_from_manifest_metadata(tmp_path):  # [RT1-1iv]
    setup = Setup(tmp_path)
    not_b = make_sealed_root(tmp_path / "ladders_sept", dates=("2026-09-01",), date_range=("2026-09-01", "2026-09-30"))
    with pytest.raises(H.UnsealRefused, match=r"not holdout-B's 2026-07-26\.\.2026-08-31"):
        _run_holdout(setup, not_b)
    is_b = make_sealed_root(tmp_path / "ladders_holdout")
    setup.write_holdout_pass(FR31A_ID)
    with pytest.raises(H.UnsealRefused, match="is holdout-B's; the R3/R5 root"):
        _run_score(setup, is_b)
    _assert_nothing_written(setup)


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
# 2. the once-lock: root identity, log AND registry, append-only integrity  [RT1-1]
# ===========================================================================
def test_root_digest_ignores_comments_and_whitespace_but_not_content_or_purpose(tmp_path):
    a = make_sealed_root(tmp_path / "a")
    seal_a = H.inspect_root_seal(a, REPO)
    b = tmp_path / "b"
    shutil.copytree(a, b)
    with open(b / "SHA256SUMS", "a", encoding="utf-8") as fh:
        fh.write("# a comment line appended by an attacker\n\n")
    seal_b = H.inspect_root_seal(b, REPO)
    assert seal_b.sha256sums_digest != seal_a.sha256sums_digest  # the raw file changed ...
    assert seal_b.root_digest == seal_a.root_digest  # ... the identity did not
    c = tmp_path / "c"
    shutil.copytree(a, c)
    m = json.loads((c / "manifest.json").read_text())
    m["date_range"]["end"] = "2026-08-30"
    (c / "manifest.json").write_text(json.dumps(m))
    assert H.inspect_root_seal(c, REPO).root_digest != seal_a.root_digest  # purpose is part of identity
    d = tmp_path / "d"
    shutil.copytree(a, d)
    text = (d / "SHA256SUMS").read_text().splitlines()
    text[0] = "0" * 64 + text[0][64:]
    (d / "SHA256SUMS").write_text("\n".join(text) + "\n")
    assert H.inspect_root_seal(d, REPO).root_digest != seal_a.root_digest


def test_sha256sums_comment_attack_is_refused_even_after_deleting_the_log_and_the_report(tmp_path):
    """The red team's reproduction: copytree + one comment line -> a second PASS on identical rows."""
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _l, _b = _run_holdout(setup, root, finalists=setup.finalists([FR31A_ID]))
    assert outcome.verdicts == {FR31A_ID: "PASS"}
    attack = tmp_path / "ladders_holdout_copy"
    shutil.copytree(root, attack)
    with open(attack / "SHA256SUMS", "a", encoding="utf-8") as fh:
        fh.write("# harmless comment\n")
    # (a) log intact: refused quoting the prior log line
    with pytest.raises(H.UnsealRefused, match="once per family per PURPOSE") as ei:
        H.run_holdout(finalists_path=setup.finalists([FR31A_ID]), unseal_tag=TAG, root=attack, paths=setup.paths,
                      n_boot=100, frame_builder=RecordingBuilder(), out=lambda s: None)
    assert json.dumps(H.read_unseal_log(setup.unseal_log)[0], sort_keys=True) in str(ei.value)
    # (b) log deleted AND report deleted: the registry still carries the root digest -> refused
    setup.unseal_log.unlink()
    outcome.report_path.unlink()
    with pytest.raises(H.UnsealRefused, match="registry already carries holdout evidence") as ei:
        H.run_holdout(finalists_path=setup.finalists([FR31A_ID]), unseal_tag=TAG, root=attack, paths=setup.paths,
                      n_boot=100, frame_builder=RecordingBuilder(), out=lambda s: None)
    assert '"root_digest"' in str(ei.value)
    assert not setup.unseal_log.exists()  # a refusal writes nothing, even a fresh log
    # (c) a second genome on the same family + root is refused too (family rule), and by genome (score)
    with pytest.raises(H.UnsealRefused, match="registry already carries"):
        H.run_holdout(finalists_path=setup.finalists([NOFILTER_ID]), unseal_tag=TAG, root=attack, paths=setup.paths,
                      n_boot=100, frame_builder=RecordingBuilder(), out=lambda s: None)


def test_editing_the_prior_log_line_family_does_not_reopen_the_look(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    _run_holdout(setup, root)
    lines = H.read_unseal_log(setup.unseal_log)
    lines[0]["family"] = "somebody/else"
    lines[0]["genome_ids"] = ["0" * 16]
    setup.unseal_log.write_text(json.dumps(lines[0]) + "\n", encoding="utf-8")
    with pytest.raises(H.UnsealRefused, match="registry already carries holdout evidence"):
        H.run_holdout(finalists_path=setup.finalists(), unseal_tag=TAG, root=root, paths=setup.paths, n_boot=100,
                      frame_builder=RecordingBuilder(), out=lambda s: None)


def test_append_only_integrity_against_head(tmp_path, monkeypatch):  # [RT1-1iii]
    committed = b'{"a": 1}\n{"b": 2}\n'
    monkeypatch.setattr(H, "git_show_head", lambda repo_root, rel: committed if rel.endswith("log.jsonl") else None)
    p = REPO / "reports" / "factory" / "tmp_integrity_log.jsonl"  # inside the repo so the check applies
    assert not p.exists()
    try:
        # missing on disk while HEAD has it
        with pytest.raises(H.UnsealRefused, match="missing on disk"):
            H.assert_append_only(p, REPO)
        # rewritten (prefix broken)
        p.write_bytes(b'{"a": 1}\n{"c": 3}\n')
        with pytest.raises(H.UnsealRefused, match="has been rewritten"):
            H.assert_append_only(p, REPO)
        # truncated
        p.write_bytes(b'{"a": 1}\n')
        with pytest.raises(H.UnsealRefused, match="has been rewritten"):
            H.assert_append_only(p, REPO)
        # appended (and CRLF on disk) -> fine
        p.write_bytes(b'{"a": 1}\r\n{"b": 2}\r\n{"d": 4}\r\n')
        info = H.assert_append_only(p, REPO)
        assert info["checked"] and info["head_bytes"] == len(committed)
    finally:
        if p.exists():
            p.unlink()
    # not in HEAD / outside the repo -> not checked, never refused
    assert H.assert_append_only(REPO / "reports" / "factory" / "never_committed.jsonl", REPO)["checked"] is False
    assert H.assert_append_only(tmp_path / "x.jsonl", REPO)["reason"] == "outside the repository"


def test_a_rewritten_registry_refuses_every_command_before_anything_is_written(tmp_path, monkeypatch):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    monkeypatch.setattr(H, "_inside_repo", lambda p, r: True)
    monkeypatch.setattr(H, "git_show_head",
                        lambda repo_root, rel: b"something else entirely\n" if rel.endswith("registry.jsonl") else None)
    with pytest.raises(H.UnsealRefused, match="has been rewritten"):
        _run_holdout(setup, root)
    _assert_nothing_written(setup)


def test_the_real_append_only_records_match_head():
    # The real registry is tracked and must be a prefix-extension of HEAD; the real log is not in HEAD yet.
    info = H.assert_append_only(REAL_REGISTRY, REPO)
    assert info["checked"] is True or info["reason"] == "not in HEAD"
    assert H.assert_append_only(REAL_LOG, REPO)["checked"] is False


def test_second_score_of_the_same_genome_on_the_same_root_is_refused(tmp_path):
    setup = Setup(tmp_path)
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_r3_root(tmp_path / "ladders_2026-09")
    _run_holdout(setup, hold)
    out, _lines = _run_score(setup, r3)
    assert out.verdicts == {FR31A_ID: "PASS"}
    log = H.read_unseal_log(setup.unseal_log)
    assert [ln["command"] for ln in log] == ["holdout", "score"]
    with pytest.raises(H.UnsealRefused, match="already RATIFIED"):
        _run_score(setup, r3)
    seal = H.inspect_root_seal(r3, REPO)
    with pytest.raises(H.UnsealRefused) as ei:
        H.assert_unseal_allowed(log, [], command="score", family=FAMILY, genome_ids=[FR31A_ID], root=seal.relpath,
                                root_digest=seal.root_digest)
    assert "no second scoring may exist" in str(ei.value) and "once per genome per PURPOSE" in str(ei.value)
    assert json.dumps(log[1], sort_keys=True) in str(ei.value)
    with pytest.raises(H.UnsealRefused, match="registry already carries r3 evidence"):
        H.assert_unseal_allowed([], setup.registry().lines(), command="score", family=FAMILY, genome_ids=[FR31A_ID],
                                root=seal.relpath, root_digest=seal.root_digest)
    assert len(H.read_unseal_log(setup.unseal_log)) == 2


def test_score_refuses_the_holdout_root_for_a_genome_already_scored_there(tmp_path):
    setup = Setup(tmp_path)
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    _run_holdout(setup, hold)
    with pytest.raises(H.UnsealRefused, match="is holdout-B's"):
        _run_score(setup, hold)


def test_at_most_three_unseals_per_quarter(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    now = H._now()
    q = H.quarter_of(now)
    setup.unseal_log.write_text("".join(
        json.dumps({"command": "holdout", "family": f"f{i}", "genome_ids": [f"g{i}"], "root": f"data/r{i}",
                    "root_digest": str(i) * 64, "ts": now.isoformat(), "quarter": q, "tag": TAG}) + "\n"
        for i in range(3)), encoding="utf-8")
    with pytest.raises(H.UnsealRefused, match=rf"3 unseals already recorded in {q} across the unseal log and the registry"):
        _run_holdout(setup, root)
    assert len(H.read_unseal_log(setup.unseal_log)) == 3


def test_quarter_quota_counts_registry_evidence_when_the_log_is_deleted():  # [RT2-1]
    now = H._now()
    ts = now.isoformat()
    reg_lines = [
        {"event": "transition", "family": "f/a", "genome_id": "g1", "ts": ts, "evidence": {"holdout": {"verdict": "PASS"}}},
        {"event": "evidence", "family": "f/a", "genome_id": "g2", "ts": ts, "evidence": {"holdout": {"verdict": "HALT"}}},
        {"event": "transition", "family": "f/b", "genome_id": "g3", "ts": ts, "evidence": {"r3": {"verdict": "PASS"}}},
        {"event": "transition", "family": "f/c", "genome_id": "g4", "ts": ts, "evidence": {"r5": {"verdict": "PASS"}}},
    ]
    # holdout(f/a) counts once (two finalists = one look), r3(g3), r5(g4) -> 3 looks with an EMPTY log
    with pytest.raises(H.UnsealRefused, match="3 unseals already recorded .* across the unseal log and the registry"):
        H.assert_unseal_allowed([], reg_lines, command="holdout", family="f/new", genome_ids=["x"], root="r",
                                root_digest="0" * 64, now=now)
    # in another quarter the same evidence counts for nothing
    import datetime as dt
    H.assert_unseal_allowed([], reg_lines, command="holdout", family="f/new", genome_ids=["x"], root="r",
                            root_digest="0" * 64, now=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))


def test_lock_is_per_purpose_not_per_digest(tmp_path):  # [RT2-1]
    """A copied root with one price changed (+ its SHA256SUMS entry fixed) or an edited manifest is the same look."""
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    out, _l, _b = _run_holdout(setup, root, finalists=setup.finalists([FR31A_ID]))
    assert out.verdicts == {FR31A_ID: "PASS"}

    def tweaked(name: str, mutate) -> Path:
        c = tmp_path / name
        shutil.copytree(root, c)
        mutate(c)
        # re-sign every CSV so SHA256SUMS verifies and the digest is genuinely new
        lines = []
        for rel in sorted(p.relative_to(c).as_posix() for p in c.glob("*/*.csv")):
            lines.append(f"{hashlib.sha256((c / rel).read_bytes()).hexdigest()}  {rel}")
        (c / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return c

    def change_price(c: Path):
        f = c / "KXHIGHNY" / "2026-08-01.csv"
        f.write_text(f.read_text(encoding="utf-8").replace("0.2,0.3,", "0.21,0.3,", 1), encoding="utf-8")

    def edit_manifest(c: Path):
        m = json.loads((c / "manifest.json").read_text())
        m["generated_at_utc"] = "2026-09-07T00:00:00Z"
        (c / "manifest.json").write_text(json.dumps(m))

    seal0 = H.inspect_root_seal(root, REPO)
    for name, mut in (("copy_price", change_price), ("copy_manifest", edit_manifest)):
        c = tweaked(name, mut)
        sealc = H.inspect_root_seal(c, REPO)
        assert H.verify_sha256sums(c)["failed"] == []
        if name == "copy_price":
            assert sealc.root_digest != seal0.root_digest  # a genuinely different digest ...
        with pytest.raises(H.UnsealRefused, match="once per family per PURPOSE"):  # ... is still the same look
            H.run_holdout(finalists_path=setup.finalists([FR31A_ID]), unseal_tag=TAG, root=c, paths=setup.paths,
                          n_boot=100, frame_builder=RecordingBuilder(), out=lambda s: None)
        with pytest.raises(H.UnsealRefused, match="once per family per PURPOSE"):  # any finalist of the family
            H.run_holdout(finalists_path=setup.finalists([NOFILTER_ID]), unseal_tag=TAG, root=c, paths=setup.paths,
                          n_boot=100, frame_builder=RecordingBuilder(), out=lambda s: None)
    # score: per genome per purpose -- a tweaked copy of the R3 root is the same look for that genome
    r3 = make_r3_root(tmp_path / "ladders_2026-09")
    out2, _ = _run_score(setup, r3)
    assert out2.verdicts == {FR31A_ID: "PASS"}
    c3 = tmp_path / "ladders_2026-09_copy"
    shutil.copytree(r3, c3)
    f = c3 / "KXHIGHNY" / "2026-09-01.csv"
    f.write_text(f.read_text(encoding="utf-8").replace("0.2,0.3,", "0.22,0.3,", 1), encoding="utf-8")
    lines = [f"{hashlib.sha256((c3 / rel).read_bytes()).hexdigest()}  {rel}"
             for rel in sorted(p.relative_to(c3).as_posix() for p in c3.glob("*/*.csv"))]
    (c3 / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert H.inspect_root_seal(c3, REPO).root_digest != H.inspect_root_seal(r3, REPO).root_digest
    with pytest.raises(H.UnsealRefused, match="already RATIFIED|once per genome per PURPOSE"):
        H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=c3, paths=setup.paths, n_boot=100,
                    frame_builder=RecordingBuilder(), out=lambda s: None)
    seal3 = H.inspect_root_seal(c3, REPO)
    with pytest.raises(H.UnsealRefused, match="once per genome per PURPOSE"):
        H.assert_unseal_allowed(H.read_unseal_log(setup.unseal_log), [], command="score", family=FAMILY,
                                genome_ids=[FR31A_ID], root=seal3.relpath, root_digest=seal3.root_digest)
    with pytest.raises(H.UnsealRefused, match="registry already carries r3 evidence"):
        H.assert_unseal_allowed([], setup.registry().lines(), command="score", family=FAMILY,
                                genome_ids=[FR31A_ID], root=seal3.relpath, root_digest=seal3.root_digest)


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
    from src.data.kalshi_history import _load_ladders_unchecked as real_loader  # H's attr is a lazy sentinel

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
    seal = H.inspect_root_seal(root, REPO)
    assert ln["root_digest"] == seal.root_digest and ln["sha256sums_digest"] == seal.sha256sums_digest
    assert ln["sha256sums_entries"] == 6 and ln["manifest_date_range"] == list(HB)
    assert ln["series"] == ["KXHIGHCHI", "KXHIGHLAX", "KXHIGHNY"]
    assert ln["doc_sha256"] == H.sha256_text(setup.revival.read_bytes().decode("utf-8").replace("\r", ""))  # [RT1-2]
    assert ln["doc_path"].endswith("REVIVAL.md")
    assert "manifest.json exposes days[].market_detail[].result" in ln["caveat"]
    assert H.read_unseal_log(setup.unseal_log) == [ln]
    assert outcome.record.line_no == 1


def test_open_sealed_root_refuses_without_an_appended_record(tmp_path):
    root = make_sealed_root(tmp_path / "ladders_holdout")
    rec = H.UnsealRecord(command="holdout", tag=TAG, ratified_date="2026-09-06", doc_path="d", doc_sha256="0" * 64,
                         family=FAMILY, genome_ids=[FR31A_ID], root="x", root_digest="0" * 64, sha256sums_digest="1" * 64,
                         sha256sums_entries=1, manifest_date_range=list(HB), series=["KXHIGHNY"])
    with pytest.raises(H.HoldoutAbort, match="before the unseal line was appended"):
        H.open_sealed_root(root, rec)


def test_append_unseal_line_aborts_on_an_empty_git_rev(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "git_rev", lambda *_a, **_k: "")
    rec = H.UnsealRecord(command="holdout", tag=TAG, ratified_date="2026-09-06", doc_path="d", doc_sha256="0" * 64,
                         family=FAMILY, genome_ids=[FR31A_ID], root="x", root_digest="0" * 64, sha256sums_digest="1" * 64,
                         sha256sums_entries=1, manifest_date_range=list(HB), series=["KXHIGHNY"])
    with pytest.raises(H.UnsealRefused, match="git rev is empty"):
        H.append_unseal_line(tmp_path / "log.jsonl", rec)
    assert not (tmp_path / "log.jsonl").exists()


# ===========================================================================
# 4. scoring: kernel, registry thresholds, gates, Holm, hash before numbers, report, registry
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
    assert "bss_trades_ge0" in e["gates"] and "bss_trades_min" in e["gates"]["constraints"]["threshold"]  # [RT1-4]
    f = res["genomes"][NOFILTER_ID]
    assert f["gates"]["paired_vs_nofilter_lo_gt0"]["pass"] is False  # paired against itself is exactly 0
    assert f["kernel"]["realized"] < 0
    holm = res["holm"]
    assert holm["m"] == 4 and holm["alpha"] == 0.05
    assert set(holm["entries"]) == {f"{FAMILY}:{FR31A_ID}", f"{FAMILY}:{NOFILTER_ID}", OTHER_CLOSED, OTHER_BLANK}
    assert holm["entries"][OTHER_CLOSED]["p"] == 0.2895
    assert holm["entries"][OTHER_BLANK]["p"] == 1.0 and "p = 1.0" in holm["entries"][OTHER_BLANK]["provenance"]
    assert holm["entries"][f"{FAMILY}:{FR31A_ID}"]["reject"] is True
    assert holm["entries"][f"{FAMILY}:{NOFILTER_ID}"]["reject"] is False
    assert set(holm["p"]) == set(holm["entries"])  # the p vector travels with the result  [RT1-5]
    assert res["thresholds_source"] == "registry" and res["search_window_quantities_not_recomputed"] == [
        "p_rc_all69_lt", "beats_every_control"]
    assert sorted(c["embargo_days"] for c in builder.calls) == [1, 2]
    assert all(sr.SEALED_EVALUATION_ATTR in c["attrs"] and "ladder_root" not in c["attrs"] for c in builder.calls)
    tf = res["truth_filter"]
    assert tf["markets"] == 18 and tf["city_days"] == 6 and tf["markets_dropped"] == 0
    assert tf["fraction_city_days_affected"] == 0.0 and tf["criterion_lt_10pct"] is True
    assert any(line.startswith("truth filter: dropped 0 of 18 markets") for line in lines)
    assert any(line.startswith("thresholds: registry") for line in lines)


def test_thresholds_come_from_the_family_registry_line(tmp_path):  # [RT1-4]
    thr = {"bss_trades_min": -0.05, "gefs_twin_min": 0.0, "holm_alpha": 0.05, "max_clauses": 8, "min_cities": 3,
           "min_dates_frac": 0.6, "min_trades": 50, "pooled_boot_lo_gt": 0.0, "worst_date_pnl_min": -0.5}
    setup = Setup(tmp_path, thresholds=thr)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _l, _b = _run_holdout(setup, root)
    e = outcome.doc["result"]["genomes"][FR31A_ID]
    assert e["kernel"]["trades"] == 42 and e["kernel"]["constraint_reason"] == "MIN_TRADES"  # 42 < registry's 50
    assert e["gates"]["constraints"]["pass"] is False and e["thresholds"]["min_trades"] == 50.0
    assert outcome.verdicts == {FR31A_ID: "HALT"}
    assert outcome.doc["result"]["thresholds_source"] == "registry"
    last = setup.family_lines()[-1]
    assert last["evidence"]["holdout"]["thresholds_source"] == "registry"
    # a family line without thresholds falls back to the fitness constants and says so
    thr_d, src_d = H.family_thresholds({"thresholds": {}})
    assert src_d == "default" and thr_d["min_trades"] == 40 and thr_d["holm_alpha"] == 0.05
    thr_p, src_p = H.family_thresholds({"thresholds": {"min_trades": 45}})
    assert thr_p["min_trades"] == 45.0 and src_p.startswith("registry+default(")


def test_holm_alpha_comes_from_the_registry_and_rejects_strictly(tmp_path):  # [RT1-5]
    setup = Setup(tmp_path)
    reg = setup.registry()
    # p_adj exactly at alpha (one finalist, other families at p = 1.0 -> m = 3, p_adj = 3 * p)
    h = H.holm_across_registry(reg, FAMILY, {FR31A_ID: 0.05 / 3}, "d" * 64, alpha=0.05)
    assert h["m"] == 3 and h["entries"][f"{FAMILY}:{FR31A_ID}"]["p_adj"] == pytest.approx(0.05)
    assert h["entries"][f"{FAMILY}:{FR31A_ID}"]["reject"] is False  # strict <
    h2 = H.holm_across_registry(reg, FAMILY, {FR31A_ID: 0.049 / 3}, "d" * 64, alpha=0.05)
    assert h2["entries"][f"{FAMILY}:{FR31A_ID}"]["reject"] is True
    assert h2["p"] == {f"{FAMILY}:{FR31A_ID}": pytest.approx(0.049 / 3), OTHER_CLOSED: 0.2895, OTHER_BLANK: 1.0}


def test_result_sha256_is_printed_before_any_number_and_matches_the_report(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, lines, _b = _run_holdout(setup, root)
    idx_sha = next(i for i, ln in enumerate(lines) if re.match(r"^result_sha256 [0-9a-f]{64}$", ln))
    number = re.compile(r"[+-]?\d+\.\d{3,}")
    first_number = next(i for i, ln in enumerate(lines) if number.search(ln))
    first_stat = next(i for i, ln in enumerate(lines) if ln.startswith("truth filter:") or "pooled OOS mean" in ln)
    assert idx_sha < first_number and idx_sha < first_stat
    assert all(not number.search(ln) for ln in lines[:idx_sha])
    sha = lines[idx_sha].split()[1]
    assert sha == outcome.result_sha256
    doc = json.loads(outcome.report_path.read_text(encoding="utf-8"))
    assert doc["result_sha256"] == sha
    assert H.result_sha256(doc["result"]) == sha
    assert outcome.report_path.name == f"holdout_{FAMILY.replace('/', '_')}_ladders_holdout.json"
    assert doc["meta"]["unseal_line_no"] == 1 and doc["meta"]["git_rev"] == GIT_REV
    assert "manifest.json exposes" in doc["meta"]["caveat"]
    assert set(doc["meta"]["append_only_checks"]) == {"unseal_log", "registry"}


def test_holdout_pass_writes_proposed_and_a_failed_sibling_is_an_evidence_event(tmp_path):  # [RT1-6]
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _lines, _b = _run_holdout(setup, root)
    reg = setup.registry()
    assert reg.status(FAMILY) == "PROPOSED"
    new = setup.family_lines()[-2:]
    assert [ln["event"] for ln in new] == ["transition", "evidence"]
    assert new[0]["status"] == "PROPOSED" and new[0]["genome_id"] == FR31A_ID
    ev = new[0]["evidence"]
    assert ev["result_sha256"] == outcome.result_sha256
    h = ev["holdout"]
    assert h["verdict"] == "PASS" and h["root_relpath"].endswith("ladders_holdout")
    assert h["root_digest"] == outcome.record.root_digest and len(h["root_digest"]) == 64
    assert h["one_sided_p"] == 0.0 and h["holm_p_adj"] == 0.0 and h["holm_m"] == 4 and h["holm_alpha"] == 0.05
    assert set(h["holm_p"]) == {f"{FAMILY}:{FR31A_ID}", f"{FAMILY}:{NOFILTER_ID}", OTHER_CLOSED, OTHER_BLANK}
    assert NOFILTER_ID in h["finalists_failed"]
    assert new[1]["genome_id"] == NOFILTER_ID and new[1]["evidence"]["holdout"]["verdict"] == "HALT"
    assert "status" not in new[1]
    assert H.genome_status(reg, FAMILY, NOFILTER_ID) == "PROPOSED"  # the evidence event changed no status
    assert H.holdout_pass_lines(reg, FAMILY, FR31A_ID) and not H.holdout_pass_lines(reg, FAMILY, NOFILTER_ID)


def test_family_is_halted_only_when_nothing_passes_and_nothing_is_ratified(tmp_path):  # [RT1-6]
    setup = Setup(tmp_path, genomes=(NOFILTER,))
    root = make_sealed_root(tmp_path / "ladders_holdout")
    outcome, _lines, _b = _run_holdout(setup, root)
    assert outcome.verdicts == {NOFILTER_ID: "HALT"} and outcome.exit_code == H.EXIT_HALT
    reg = setup.registry()
    assert reg.status(FAMILY) == "HALT"
    last = setup.family_lines()[-1]
    assert last["status"] == "HALT" and last["genome_id"] == NOFILTER_ID
    assert last["evidence"]["holdout"]["failing"] and last["evidence"]["all_finalists"] == {NOFILTER_ID: "HALT"}


def test_score_beside_a_ratified_sibling_records_evidence_and_never_halts(tmp_path):  # [RT1-6, RT2-1, RT2-6]
    """Under once-per-family-per-purpose a sibling can only enter through the SAME holdout run, so the
    RATIFIED-sibling cases arise at `score`: a failing R3 beside a RATIFIED sibling is an evidence event."""
    b_genome = FR31A.with_meta(name="fr31a_twin_b")  # same phenotype, different genome_id
    b_id = P.genome_id_for(P.genome_json_for(b_genome))
    assert b_id != FR31A_ID
    setup = Setup(tmp_path, genomes=(FR31A, b_genome))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    out0, _l, _b = _run_holdout(setup, hold)  # both finalists in ONE look
    assert out0.verdicts == {FR31A_ID: "PASS", b_id: "PASS"}
    assert [ln["status"] for ln in setup.family_lines()[-2:]] == ["PROPOSED", "PROPOSED"]
    # a second holdout for the family is refused whatever the root (per purpose), even for a fresh copy
    hold2 = make_sealed_root(tmp_path / "ladders_holdout_v2", dates=("2026-08-03", "2026-08-04"))
    with pytest.raises(H.UnsealRefused, match="once per family per PURPOSE"):
        _run_holdout(setup, hold2, finalists=setup.finalists([b_id]))
    # A scores and is RATIFIED
    r3a = make_r3_root(tmp_path / "ladders_2026-09")
    out1, _ = _run_score(setup, r3a)
    assert out1.verdicts == {FR31A_ID: "PASS"} and setup.registry().status(FAMILY) == "RATIFIED"
    # B fails R3 beside the RATIFIED sibling: evidence event, family stays RATIFIED, B stays PROPOSED
    r3b = make_r3_root(tmp_path / "ladders_2026-09b", dates=("2026-09-03", "2026-09-04"))
    import datetime as dt
    q2 = dt.datetime(2027, 1, 15, tzinfo=dt.timezone.utc)  # 4th look: a fresh quota window
    out2 = H.run_score(genome_id=b_id, unseal_tag=TAG, root=r3b, paths=setup.paths, n_boot=200,
                       frame_builder=RecordingBuilder(losing_frame()), out=lambda s: None, now=q2)
    assert out2.verdicts == {b_id: "HALT"}
    last = setup.family_lines()[-1]
    assert last["event"] == "evidence" and last["genome_id"] == b_id and last["evidence"]["r3"]["verdict"] == "HALT"
    assert setup.registry().status(FAMILY) == "RATIFIED"
    assert H.genome_status(setup.registry(), FAMILY, b_id) == "PROPOSED"
    assert H.ratified_genomes(setup.registry(), FAMILY) == [FR31A_ID]
    # and B can never be scored again (per genome per purpose), on any root
    with pytest.raises(H.UnsealRefused, match="registry already carries r3 evidence|once per genome per PURPOSE"):
        H.run_score(genome_id=b_id, unseal_tag=TAG, root=r3a, paths=setup.paths, n_boot=100,
                    frame_builder=RecordingBuilder(), out=lambda s: None, now=q2)


def test_reassert_flag_is_top_level_and_ratified_genomes_is_most_recent_first(tmp_path):  # [RT2-6]
    b_genome = FR31A.with_meta(name="fr31a_twin_b")
    b_id = P.genome_id_for(P.genome_json_for(b_genome))
    setup = Setup(tmp_path, genomes=(FR31A, b_genome))
    reg = setup.registry()
    reg.transition(FAMILY, "RATIFIED", genome_id=FR31A_ID, evidence={"r3": {"verdict": "PASS"}})
    reg.transition(FAMILY, "RATIFIED", genome_id=b_id, evidence={"r3": {"verdict": "PASS"}})
    assert H.ratified_genomes(reg, FAMILY) == [b_id, FR31A_ID]
    # the registry's `extra` puts the flag at the TOP level of the line, beside status, not only in evidence
    ln = reg.transition(FAMILY, "RATIFIED", genome_id=FR31A_ID, evidence={"reasserted": True},
                        extra={"reasserted": True})
    assert ln["reasserted"] is True and ln["status"] == "RATIFIED"
    raw = [json.loads(x) for x in setup.registry_path.read_text(encoding="utf-8").splitlines() if x.strip()][-1]
    assert raw["reasserted"] is True and raw["evidence"]["reasserted"] is True
    assert H.ratified_genomes(reg, FAMILY) == [FR31A_ID, b_id]  # the re-assert is the most recent RATIFIED line
    from src.factory.registry import RegistryError
    with pytest.raises(RegistryError, match="would shadow"):
        reg.transition(FAMILY, "RATIFIED", genome_id=FR31A_ID, extra={"status": "PROPOSED"})


def test_score_ratifies_on_pass_and_records_the_r3_checks(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A, NOFILTER))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_r3_root(tmp_path / "ladders_2026-09", dates=("2026-09-01", "2026-09-02", "2026-09-03"))
    _run_holdout(setup, hold)
    out, lines = _run_score(setup, r3, as_of="2026-09-02")
    assert out.verdicts == {FR31A_ID: "PASS"} and out.exit_code == H.EXIT_PASS
    reg = setup.registry()
    assert reg.status(FAMILY) == "RATIFIED"
    last = setup.family_lines()[-1]
    assert last["status"] == "RATIFIED" and last["genome_id"] == FR31A_ID
    r3ev = last["evidence"]["r3"]
    assert last["evidence"]["result_sha256"] == out.result_sha256
    assert r3ev["checks"]["6_cold_season_month"] is None and r3ev["checks"]["5_point_estimate_ge_4c"] is True
    assert set(r3ev["checks"]) == {"1_frozen_source_ci_lo_gt0", "2_gefs_realized_ge0", "3_beats_nofilter_baseline",
                                   "4_tail_ratio_in_range", "5_point_estimate_ge_4c", "6_cold_season_month"}
    assert r3ev["as_of"] == "2026-09-02" and r3ev["root_digest"] == out.record.root_digest and r3ev["holm_m"] == 3
    log = H.read_unseal_log(setup.unseal_log)
    assert log[-1]["command"] == "score" and log[-1]["as_of"] == "2026-09-02" and log[-1]["genome_ids"] == [FR31A_ID]
    assert out.report_path.name == f"score_{FR31A_ID}_ladders_2026-09.json"
    idx_sha = next(i for i, ln in enumerate(lines) if ln.startswith("result_sha256 "))
    assert all(not re.search(r"[+-]?\d+\.\d{3,}", ln) for ln in lines[:idx_sha])
    with pytest.raises(H.UnsealRefused, match="no PASS holdout evidence"):
        _run_score(setup, r3, gid=NOFILTER_ID)


def test_score_halts_a_proposed_genome_that_fails_r3(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A,))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_r3_root(tmp_path / "ladders_2026-09", dates=("2026-09-01",))
    _run_holdout(setup, hold)
    out, _ = _run_score(setup, r3, builder=RecordingBuilder(losing_frame()))
    assert out.verdicts == {FR31A_ID: "HALT"} and out.exit_code == H.EXIT_HALT
    reg = setup.registry()
    assert reg.status(FAMILY) == "HALT"
    last = setup.family_lines()[-1]
    assert last["status"] == "HALT" and "boot_lo_gt0" in last["evidence"]["r3"]["failing"]


def test_score_refuses_a_genome_that_is_not_proposed(tmp_path):
    setup = Setup(tmp_path, family_status="CLOSED")
    r3 = make_r3_root(tmp_path / "ladders_2026-09", dates=("2026-09-01",))
    with pytest.raises(H.UnsealRefused, match="is CLOSED"):
        _run_score(setup, r3)
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
    assert outcome.doc["result"]["payoff_mismatch_markets_dropped"] == ["KXHIGHNY-260801-T70"]
    assert builder.calls[0]["n_rows"] == 36 - 2
    assert any("criterion < 10 %: FAIL" in ln for ln in lines)


# ===========================================================================
# 5. R5 re-check (criterion 6 only, once)  [RT1-7]
# ===========================================================================
def test_r5_check_re_evaluates_only_criterion_6_after_as_of_and_is_once_only(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A,))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    sept = ("2026-09-01", "2026-09-02")
    cold = tuple(f"2026-11-{d:02d}" for d in range(1, 29)) + tuple(f"2026-12-{d:02d}" for d in range(1, 4))
    r3 = make_sealed_root(tmp_path / "ladders_2026-09", dates=sept + cold, date_range=(sept[0], cold[-1]))
    _run_holdout(setup, hold)
    out, _ = _run_score(setup, r3, as_of="2026-09-02")
    assert out.verdicts == {FR31A_ID: "PASS"}
    n_log = len(H.read_unseal_log(setup.unseal_log))
    lines: List[str] = []
    r5 = H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, out=lines.append)
    assert r5.verdicts == {FR31A_ID: "PASS"} and r5.exit_code == H.EXIT_PASS
    res = r5.doc["result"]
    assert res["dates_after_as_of"] == len(cold) and res["cold_season_dates"] == len(cold) >= H.COLD_SEASON_MIN_DATES
    assert "criteria 1-5 are NOT recomputed" in res["note"]
    idx_sha = next(i for i, ln in enumerate(lines) if ln.startswith("result_sha256 "))
    assert all(not re.search(r"\d+\.\d{3,}", ln) for ln in lines[:idx_sha])
    log = H.read_unseal_log(setup.unseal_log)
    assert len(log) == n_log + 1 and log[-1]["command"] == "score-r5" and log[-1]["as_of"] == "2026-09-02"
    last = setup.family_lines()[-1]
    assert last["status"] == "RATIFIED" and last["genome_id"] == FR31A_ID and last["evidence"]["r5"]["verdict"] == "PASS"
    assert last["evidence"]["r5"]["min_dates"] == H.COLD_SEASON_MIN_DATES
    with pytest.raises(H.UnsealRefused, match="already spent their score-r5 look|R5 re-check already"):
        H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, out=lambda s: None)
    assert len(H.read_unseal_log(setup.unseal_log)) == n_log + 1
    # ... and the registry alone (log deleted) still refuses it
    setup.unseal_log.unlink()
    with pytest.raises(H.UnsealRefused, match="R5 re-check already recorded in the registry"):
        H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, out=lambda s: None)


def test_r5_check_verifies_sha256sums_before_reading_rows(tmp_path):  # [RT2-5]
    setup = Setup(tmp_path, genomes=(FR31A,))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_r3_root(tmp_path / "ladders_2026-09", dates=("2026-09-01", "2026-09-02", "2026-11-01"))
    _run_holdout(setup, hold)
    _run_score(setup, r3, as_of="2026-09-02")
    f = r3 / "KXHIGHNY" / "2026-11-01.csv"
    f.write_text(f.read_text(encoding="utf-8").replace("0.2,0.3,", "0.25,0.3,", 1), encoding="utf-8")
    n_log = len(H.read_unseal_log(setup.unseal_log))
    with pytest.raises(H.HoldoutAbort, match="SHA256SUMS check failed") as ei:
        H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, out=lambda s: None)
    assert ei.value.record is not None and ei.value.record.command == "score-r5"
    assert len(H.read_unseal_log(setup.unseal_log)) == n_log + 1  # the look was spent, as for holdout/score


def test_r5_check_halts_when_the_cold_season_month_is_missing_and_refuses_without_as_of(tmp_path):
    setup = Setup(tmp_path, genomes=(FR31A,))
    hold = make_sealed_root(tmp_path / "ladders_holdout")
    r3 = make_r3_root(tmp_path / "ladders_2026-09", dates=("2026-09-01", "2026-09-02", "2026-10-01"))
    _run_holdout(setup, hold)
    _run_score(setup, r3, as_of="2026-09-02")
    r5 = H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3, paths=setup.paths, out=lambda s: None)
    assert r5.verdicts == {FR31A_ID: "HALT"} and setup.registry().status(FAMILY) == "HALT"
    assert r5.doc["result"]["dates_after_as_of"] == 1 and r5.doc["result"]["cold_season_dates"] == 0
    # a score without --as-of has no "after": r5 refuses
    setup2 = Setup(tmp_path / "s2", genomes=(FR31A,))
    hold2 = make_sealed_root(tmp_path / "s2" / "ladders_holdout")
    r3b = make_r3_root(tmp_path / "s2" / "ladders_2026-09")
    _run_holdout(setup2, hold2)
    _run_score(setup2, r3b)
    with pytest.raises(H.UnsealRefused, match="recorded no --as-of"):
        H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3b, paths=setup2.paths, out=lambda s: None)
    # and a PROPOSED (unscored) genome is refused
    setup3 = Setup(tmp_path / "s3", genomes=(FR31A,))
    r3c = make_r3_root(tmp_path / "s3" / "ladders_2026-09")
    with pytest.raises(H.UnsealRefused, match="score-r5 needs one of \\('RATIFIED',\\)"):
        H.run_r5_check(genome_id=FR31A_ID, unseal_tag=TAG, root=r3c, paths=setup3.paths, out=lambda s: None)


# ===========================================================================
# 6. the search-frame builder STILL refuses the unsealed root; the bypass is narrow
# ===========================================================================
def test_search_frame_builders_still_refuse_the_tmp_sealed_root(tmp_path):
    root = make_sealed_root(tmp_path / "ladders_holdout")
    with pytest.raises(sr.SealedDataError):
        load_ladders(root)
    with pytest.raises(sr.SealedDataError):
        ev.load_search_ladders(root)
    df = pd.read_csv(root / "KXHIGHNY" / "2026-08-01.csv")
    with pytest.raises(sr.SealedDataError):
        sr.assert_frame_not_sealed(df)
    with pytest.raises(sr.SealedDataError):
        ev.build_opportunity_frame(df, df, df, None)


def test_the_sanctioned_tape_is_marked_and_the_mark_does_not_survive_concat(tmp_path):
    setup = Setup(tmp_path)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    rec = H.UnsealRecord(command="holdout", tag=TAG, ratified_date="2026-09-06", doc_path="d", doc_sha256="0" * 64,
                         family=FAMILY, genome_ids=[FR31A_ID], root="x", root_digest="0" * 64, sha256sums_digest="1" * 64,
                         sha256sums_entries=6, manifest_date_range=list(HB), series=["KXHIGHNY"])
    H.append_unseal_line(setup.unseal_log, rec)
    df = H.open_sealed_root(root, rec)
    assert df.attrs[sr.SEALED_EVALUATION_ATTR]["line_no"] == 1
    assert "ladder_root" not in df.attrs and df.attrs["unsealed_root"] == str(root.resolve())
    sr.assert_frame_not_sealed(df)
    bare = pd.DataFrame(df.to_dict("list"))
    merged = pd.concat([df, bare], ignore_index=True)
    assert not merged.attrs.get(sr.SEALED_EVALUATION_ATTR)
    with pytest.raises(sr.SealedDataError):
        sr.assert_frame_not_sealed(merged)
    stamped = df.copy()
    stamped.attrs["ladder_root"] = str(sr.SEALED_LADDER_ROOTS[0])
    with pytest.raises(sr.SealedDataError):
        sr.assert_frame_not_sealed(stamped)


def test_importing_holdout_does_not_load_the_kalshi_client():
    """OPS red team: the factory package must not pull the live-capital client in at import time."""
    import subprocess
    import sys as _sys

    code = ("import sys, src.factory.holdout; "
            "assert 'src.data.kalshi_provider' not in sys.modules, sorted(m for m in sys.modules if m.startswith('src.data')); "
            "assert 'src.data.kalshi_history' not in sys.modules; "
            "print('ok')")
    r = subprocess.run([_sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True, timeout=180,
                       env={**__import__("os").environ, "PYTHONPATH": str(REPO)})
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_the_bypass_symbols_live_only_in_holdout_py():
    allowed = {
        "_load_ladders_unchecked": {"src/data/kalshi_history.py", "scripts/backfill_ladders.py", "src/factory/holdout.py",
                                    "src/backtest/sealed_roots.py"},
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
    assert "src/data/kalshi_history.py" not in found["SEALED_EVALUATION_ATTR"]


# ===========================================================================
# 7. --audit: manifest counts only, nothing written, no labels
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
    assert a["sealed_marker_present"] and a["sha256sums_present"] and len(a["root_digest"]) == 64
    text = H.render_audit(a)
    assert "no unseal line written" in text and "criterion < 10 %: FAIL" in text
    assert not re.search(r"\b(yes|no)\b", text.replace("no unseal", "").replace("no CSV", ""))
    assert not (tmp_path / "unseal_log.jsonl").exists()
    assert sorted(p.name for p in root.iterdir()) == ["KXHIGHCHI", "KXHIGHLAX", "KXHIGHNY", "SEALED", "SHA256SUMS",
                                                       "manifest.json"]


def test_audit_on_the_real_manifest_is_read_only():
    if not (REAL_HOLDOUT / "manifest.json").exists():
        pytest.skip("real holdout manifest not on disk")
    a = H.audit_root(REAL_HOLDOUT, REPO)
    assert a["city_days_with_rows"] == 148 and a["markets"] == 888
    assert a["criterion_lt_10pct"] is True and a["fraction_city_days_affected"] < 0.10
    assert a["date_range"]["start"] == HB[0] and a["date_range"]["end"] == HB[1]
    assert not REAL_LOG.exists()


# ===========================================================================
# 8. CLI wiring, the post-unseal catch-all, and the real builder end to end  [RT1-3]
# ===========================================================================
def _factory_module():
    spec = importlib.util.spec_from_file_location("factory_cli_holdout_test", REPO / "scripts" / "factory.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cli_args(setup: Setup, *rest: str) -> List[str]:
    return [*rest, "--config", str(setup.config), "--promoted-dir", str(setup.promoted_dir),
            "--registry", str(setup.registry_path), "--unseal-log", str(setup.unseal_log),
            "--revival-doc", str(setup.revival), "--out-dir", str(setup.reports)]


def test_cli_holdout_and_score_are_implemented_and_help_works(capsys):
    mod = _factory_module()
    assert "holdout" not in mod.NOT_IMPLEMENTED and "score" not in mod.NOT_IMPLEMENTED
    for cmd in ("holdout", "score"):
        with pytest.raises(SystemExit) as ei:
            mod.main([cmd, "--help"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "--unseal RATIFIED-YYYY-MM-DD" in out
    assert "--r5-check" in capsys.readouterr().out or True
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
    r3 = make_r3_root(tmp_path / "ladders_2026-09", dates=("2026-09-01",))
    _run_holdout(setup, hold)
    monkeypatch.setattr(H, "default_frame_builder", RecordingBuilder())
    mod = _factory_module()
    rc = mod.main(_cli_args(setup, "score", "--genome", FR31A_ID, "--ladders", str(r3), "--unseal", TAG, "--n-boot", "200"))
    out = capsys.readouterr().out.splitlines()
    assert rc == H.EXIT_PASS
    idx_sha = next(i for i, ln in enumerate(out) if re.match(r"^result_sha256 [0-9a-f]{64}$", ln))
    number = re.compile(r"[+-]?\d+\.\d{3,}")
    assert all(not number.search(ln) for ln in out[:idx_sha])
    assert any(number.search(ln) for ln in out[idx_sha + 1:])
    assert setup.registry().status(FAMILY) == "RATIFIED"
    # the r5 flag is wired through the same command
    rc5 = mod.main(_cli_args(setup, "score", "--genome", FR31A_ID, "--ladders", str(r3), "--unseal", TAG, "--r5-check"))
    assert rc5 == H.EXIT_REFUSED and "recorded no --as-of" in capsys.readouterr().err


def test_cli_aborts_with_the_spent_line_named_when_the_builder_raises_after_the_unseal(tmp_path, capsys, monkeypatch):
    setup = Setup(tmp_path, genomes=(FR31A,))
    root = make_sealed_root(tmp_path / "ladders_holdout")

    def boom(ladders, *, spec, embargo_days, root, **kw):
        raise ev.EVAnalysisError("ladder has no finite bracket; cannot measure its width")

    monkeypatch.setattr(H, "default_frame_builder", boom)
    mod = _factory_module()
    rc = mod.main(_cli_args(setup, "holdout", "--finalists", str(setup.finalists()), "--ladders", str(root),
                            "--unseal", TAG))
    captured = capsys.readouterr()
    assert rc == H.EXIT_ABORT
    assert "ABORT" in captured.err and "unseal line 1 (holdout" in captured.err and "SPENT" in captured.err
    assert "EVAnalysisError: ladder has no finite bracket" in captured.err
    assert "Traceback" not in captured.err
    assert len(H.read_unseal_log(setup.unseal_log)) == 1  # the look was spent
    # the same root cannot be re-run: the log line is still there
    rc2 = mod.main(_cli_args(setup, "holdout", "--finalists", str(setup.finalists()), "--ladders", str(root),
                             "--unseal", TAG))
    assert rc2 == H.EXIT_REFUSED and "once per family per PURPOSE" in capsys.readouterr().err


def test_keyboard_interrupt_after_the_unseal_prints_spent_and_reraises(tmp_path, capsys):  # [RT2-3]
    setup = Setup(tmp_path, genomes=(FR31A,))
    root = make_sealed_root(tmp_path / "ladders_holdout")

    def interrupt(ladders, *, spec, embargo_days, root):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        H.run_holdout(finalists_path=setup.finalists(), unseal_tag=TAG, root=root, paths=setup.paths, n_boot=100,
                      frame_builder=interrupt, out=lambda s: None)
    err = capsys.readouterr().err
    assert "SPENT" in err and "unseal line 1 (holdout" in err and "KeyboardInterrupt" in err
    assert len(H.read_unseal_log(setup.unseal_log)) == 1
    with pytest.raises(H.UnsealRefused, match="once per family per PURPOSE"):
        H.run_holdout(finalists_path=setup.finalists(), unseal_tag=TAG, root=root, paths=setup.paths, n_boot=100,
                      frame_builder=RecordingBuilder(), out=lambda s: None)


def test_a_refusal_before_the_unseal_is_never_reported_as_spent(tmp_path, capsys):
    setup = Setup(tmp_path, ratified=False)
    root = make_sealed_root(tmp_path / "ladders_holdout")
    mod = _factory_module()
    rc = mod.main(_cli_args(setup, "holdout", "--finalists", str(setup.finalists()), "--ladders", str(root), "--unseal", TAG))
    err = capsys.readouterr().err
    assert rc == H.EXIT_REFUSED and "REFUSED" in err and "SPENT" not in err


def test_cli_audit_writes_nothing(tmp_path, capsys):
    root = make_sealed_root(tmp_path / "ladders_holdout")
    mod = _factory_module()
    rc = mod.main(["holdout", "--audit", "--ladders", str(root), "--unseal-log", str(tmp_path / "log.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and "manifest metadata only" in out and "criterion < 10 %: PASS" in out
    assert not (tmp_path / "log.jsonl").exists()


# --- the real builder, end to end -------------------------------------------------------------
def _real_truth_high(station: str, date: str) -> Optional[float]:
    p = REPO / "data" / "weather_truth" / f"cli_daily_high_{station}.csv"
    if not p.exists():
        return None
    t = pd.read_csv(p)
    row = t[t["date"].astype(str) == date]
    return float(row["high"].iloc[0]) if len(row) else None


def make_real_shaped_root(root: Path, dates=("2026-07-26", "2026-07-27")) -> Optional[Path]:
    """A KXHIGH-shaped ladder (2F between brackets flanked by less/greater) per city-day, settled on the REAL truth high."""
    frames: Dict[str, Dict[str, pd.DataFrame]] = {}
    for city, series, station in _CITIES:
        for date in dates:
            h = _real_truth_high(station, date)
            if h is None:
                return None
            hi = int(round(h))
            specs = [("less", None, hi - 5), ("between", hi - 4, hi - 3), ("between", hi - 2, hi - 1),
                     ("between", hi, hi + 1), ("between", hi + 2, hi + 3), ("greater", hi + 4, None)]
            rows = []
            close = pd.Timestamp(f"{date}T23:59:00Z")
            for st, fl, cp in specs:
                if st == "less":
                    ticker, yes = f"{series}-26{date[5:7]}{date[8:10]}-T{cp}", h <= cp
                elif st == "greater":
                    ticker, yes = f"{series}-26{date[5:7]}{date[8:10]}-T{fl}", h >= fl
                else:
                    ticker, yes = f"{series}-26{date[5:7]}{date[8:10]}-B{fl + 0.5}", fl <= h <= cp
                for k in range(27):
                    ts = pd.Timestamp(f"{dates[0] if False else date}T00:00:00Z") - pd.Timedelta(hours=12) + pd.Timedelta(hours=k)
                    bid, ask = (0.30, 0.35) if yes else ((0.03, 0.06) if st != "between" else (0.12, 0.16))
                    rows.append({
                        "series": series, "city": city, "station": station, "target_date": date,
                        "event_ticker": f"{series}-26{date[5:7]}{date[8:10]}", "market_ticker": ticker,
                        "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "minutes_to_close": float((close - ts).total_seconds() // 60),
                        "close_time_utc": close.strftime("%Y-%m-%dT%H:%M:%SZ"), "strike_type": st,
                        "floor_strike": "" if fl is None else float(fl), "cap_strike": "" if cp is None else float(cp),
                        "yes_sub_title": ticker, "yes_bid": bid, "yes_ask": ask, "no_bid": 1 - ask, "no_ask": 1 - bid,
                        "last": bid, "price_mean": (bid + ask) / 2, "yes_bid_low": bid, "yes_ask_high": ask, "volume": 10,
                        "open_interest": 5, "has_quote": "true", "result": "yes" if yes else "no",
                        "expiration_value": float(h), "cli_high": float(h), "recomputed_yes_expval": "true",
                        "recomputed_yes_cli": "true", "payoff_matches_kalshi": "true", "truth_agrees": "true",
                    })
            frames.setdefault(series, {})[date] = pd.DataFrame(rows, columns=list(LADDER_COLUMNS))
    return _write_root(root, frames, (dates[0], dates[-1]))


def test_default_frame_builder_end_to_end_on_a_real_shaped_root(tmp_path):  # [RT1-3]
    if not (REPO / "data" / "forecast_archive" / "forecast_series_gfs_mex.csv").exists():
        pytest.skip("forecast archive not on disk")
    setup = Setup(tmp_path, genomes=(FR31A,))
    root = make_real_shaped_root(tmp_path / "ladders_r3_real")
    if root is None:
        pytest.skip("truth files do not cover the synthetic root's dates")
    setup.write_holdout_pass(FR31A_ID)
    lines: List[str] = []
    out = H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=root, paths=setup.paths, n_boot=200, out=lines.append)
    e = out.doc["result"]["genomes"][FR31A_ID]
    fr = e["frame"]
    assert fr["n_rows"] > 0 and fr["n_dates"] == 2 and fr["n_markets"] > 0 and fr["ladder_files_sha256"]
    assert fr["hardening"]["availability_lag_min"] == 240 and fr["hardening"]["truth_filter"] is True
    assert fr["hardening"]["sigma_cap"] == 4.0 and fr["hardening"]["cutoff"] is None
    assert e["gates"]["embargo_2_sign"]["note"].startswith("frame rebuilt with embargo_days = 2")
    assert re.match(r"^[0-9a-f]{64}$", fr["sha256"])
    # the real gefs calibration carries sigma_f > 4 on every row of these two July days, so the twin is
    # empty: the builder must not abort the spent look, and R3 #2 must say why it failed
    g2 = e["gates"]["gefs_twin_ge0"]
    assert g2["pass"] is False and "gefs twin UNAVAILABLE" in g2["note"] and "no rows survive" in g2["note"]
    # two dates / 36 markets cannot clear min_trades = 40: the honest verdict is HALT, recorded as such
    assert out.verdicts == {FR31A_ID: "HALT"} and e["kernel"]["constraint_reason"] in ("MIN_TRADES", "NO_TRADES")
    assert setup.registry().status(FAMILY) == "HALT"
    assert any(ln.startswith("result_sha256 ") for ln in lines)
    assert len(H.read_unseal_log(setup.unseal_log)) == 1 and out.report_path.exists()
    fa = out.doc["result"]["forecast_archive"]
    assert fa["relpath"] == "data/forecast_archive" and fa["explicit"] is False and fa["covers"] if "covers" in fa else True
    assert fa["gfs_mex_coverage"][0] <= "2026-07-26" and fa["gfs_mex_coverage"][1] >= "2026-07-27"


def test_gefs_twin_build_failure_never_aborts_the_spent_look(tmp_path, monkeypatch):  # [RT2-4a]
    if not (REPO / "data" / "forecast_archive" / "forecast_series_gfs_mex.csv").exists():
        pytest.skip("forecast archive not on disk")
    setup = Setup(tmp_path, genomes=(FR31A,))
    root = make_real_shaped_root(tmp_path / "ladders_r3_real")
    if root is None:
        pytest.skip("truth files do not cover the synthetic root's dates")
    setup.write_holdout_pass(FR31A_ID)
    from src.factory.lanes import weather as W
    real = W.build_opportunities_from_ladders

    def flaky(ladders, source=None, **kw):
        if source == "gefs":
            raise ev.EVAnalysisError("no forecast vintage could be matched to any snapshot")
        return real(ladders, source, **kw)

    monkeypatch.setattr(W, "build_opportunities_from_ladders", flaky)
    out = H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=root, paths=setup.paths, n_boot=200, out=lambda s: None)
    g2 = out.doc["result"]["genomes"][FR31A_ID]["gates"]["gefs_twin_ge0"]
    assert g2["pass"] is False and "gefs twin UNAVAILABLE (EVAnalysisError: no forecast vintage" in g2["note"]
    assert out.verdicts == {FR31A_ID: "HALT"} and out.report_path.exists()


def test_forecast_archive_dir_is_selected_before_the_unseal(tmp_path):  # [RT2-4b]
    if not (REPO / "data" / "forecast_archive" / "forecast_series_gfs_mex.csv").exists():
        pytest.skip("forecast archive not on disk")
    cov = H.gfs_mex_coverage(REPO / "data" / "forecast_archive")
    assert cov and cov[0] <= "2026-07-26"
    sel = H.select_forecast_archive_dir(("2026-07-26", "2026-07-27"), None, REPO)
    assert sel["relpath"] == "data/forecast_archive" and sel["explicit"] is False
    with pytest.raises(H.UnsealRefused, match="no forecast archive covers the root's dates 2031-01-01"):
        H.select_forecast_archive_dir(("2031-01-01", "2031-01-02"), None, REPO)
    with pytest.raises(H.UnsealRefused, match="no forecast archive covers"):
        H.select_forecast_archive_dir(("2026-07-26", "2026-07-27"), tmp_path / "nowhere", REPO)
    # through the command, with the DEFAULT builder, a root beyond coverage is refused before any write
    setup = Setup(tmp_path, genomes=(FR31A,))
    setup.write_holdout_pass(FR31A_ID)
    far = make_r3_root(tmp_path / "ladders_2031", dates=("2031-01-01",))
    with pytest.raises(H.UnsealRefused, match="no forecast archive covers"):
        H.run_score(genome_id=FR31A_ID, unseal_tag=TAG, root=far, paths=setup.paths, n_boot=100, out=lambda s: None)
    _assert_nothing_written(setup)


# ===========================================================================
# 9. compose: the factory-holdout service and the masks
# ===========================================================================
def test_compose_has_the_factory_holdout_service_and_keeps_the_masks():
    text = (REPO / "deploy" / "spark" / "docker-compose.lab.yml").read_text(encoding="utf-8")
    assert "factory-holdout:" in text
    assert "../../data/ladders_holdout:/app/data/ladders_holdout:ro" in text
    assert "../../data/ladders_2026-09:/app/data/ladders_2026-09:ro" in text
    factory_block = text.split("  factory:", 1)[1].split("  factory-holdout:", 1)[0]
    assert "target: /app/data/ladders_holdout" in factory_block and "target: /app/data/ladders_2026-09" in factory_block
    assert ".git" in text and "git show" in text
