"""Gen-0 integration (``src.factory.gen0``): converter cross-check, registry-before-score, summary shape.

Real-data tests (the pinned Phase-2 parity frame, ~6 s evaluator build + two
conversions) are marked ``realdata``; the ``run_gen0`` tests use a tiny
synthetic FrameSet saved in the ``freeze-frame`` layout under ``tmp_path``.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pytest
import yaml

from src.factory import fees
from src.factory import fitness as FT
from src.factory import frame as frame_mod
from src.factory import gen0
from src.factory import genome as G
from src.factory import registry as registry_mod
from src.factory import report as report_mod
from tests import factory_testkit as K

realdata = pytest.mark.realdata
REPO = Path(__file__).resolve().parents[1]
FAMILY_YAML = REPO / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml"
ROW_FIELDS = ("trades", "dates", "realized", "boot_lo", "boot_hi", "fit", "constraint_reason")


# ---------------------------------------------------------------------------
# (a) two independent pandas -> Frame converters agree on every seed
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def opp(tmp_path_factory):
    dirs = K.pinned_dirs(tmp_path_factory)
    return K.build_opp(True, dirs)


@pytest.fixture(scope="module")
def frame_pair(opp):
    via_frame = frame_mod.from_opportunity_frame(opp, name="parity", fee_regime=fees.load_regime())
    via_kit = K.opp_to_frame(opp, name="parity")
    return via_frame, via_kit


def _trade_markets(F, res):
    return set(F.markets[F.visible["market_code"][res.trade_rows]].tolist())


@realdata
def test_converters_agree_on_every_seed(frame_pair):
    A, B = frame_pair
    assert A.n_rows == B.n_rows == 251728
    for name, g in G.SEEDS.items():
        ra = FT.score(A, G.to_mask(g, A), constraints=False, genome=g, label=name)
        rb = FT.score(B, G.to_mask(g, B), constraints=False, genome=g, label=name)
        # identical ShapeResult-shaped output (bit-exact: same rows, same order, same arithmetic)
        assert FT.compare(ra, rb.shape_dict(), tol=0.0) == [], name
        assert ra.trades == rb.trades and ra.dates == rb.dates and ra.cities == rb.cities
        assert _trade_markets(A, ra) == _trade_markets(B, rb), name
        assert ra.phenotype_hash == rb.phenotype_hash, name
        # and the same constraint verdict
        ca = FT.score(A, G.to_mask(g, A), constraints=True, genome=g, label=name)
        cb = FT.score(B, G.to_mask(g, B), constraints=True, genome=g, label=name)
        assert ca.constraint_reason == cb.constraint_reason, name
        assert (math.isinf(ca.fit) and math.isinf(cb.fit)) or abs(ca.fit - cb.fit) == 0.0


@realdata
def test_pinned_parity_numbers(frame_pair):
    A, _ = frame_pair
    expect = {"fr31a_taker": 181, "fr31b": 4, "nofilter_no": 664, "far_yes_taker": 813,
              "salvage_5f": 320, "mlweather_fallback": 780, "fr31a_gefs": 181}
    for name, n in expect.items():
        g = G.SEEDS[name]
        r = FT.score(A, G.to_mask(g, A), constraints=False, genome=g, label=name)
        assert r.trades == n, (name, r.trades)
    r = FT.score(A, G.to_mask(G.SEEDS["fr31a_taker"], A), constraints=False)
    assert r.dates == 65 and abs(r.realized - 0.06362903846153846) <= 1e-9
    assert abs(r.boot_lo - 0.01220677083333332) <= 1e-9 and abs(r.boot_hi - 0.1086289246794872) <= 1e-9
    r = FT.score(A, G.to_mask(G.SEEDS["nofilter_no"], A), constraints=False)
    assert abs(r.realized - 0.020873737564770182) <= 1e-9


@realdata
def test_reference_compare_matches_phase2_json(frame_pair):
    A, _ = frame_pair
    refs = gen0.load_phase2_shapes()
    for name, label in G.PHASE2_SHAPE_LABELS.items():
        g = G.SEEDS[name]
        row = gen0._row(FT.score(A, G.to_mask(g, A), constraints=True, genome=g, label=name))
        cmp = gen0.compare_reference(row, refs[label], label)
        assert cmp["matches_1e9"] is True and cmp["fields_differing"] == [], (name, cmp["fields_differing"])
        assert len(cmp["fields_compared"]) >= 20


# ---------------------------------------------------------------------------
# (b)-(d) run_gen0 on a tiny synthetic FrameSet
# ---------------------------------------------------------------------------
def _synthetic_frames_dir(tmp_path: Path) -> Path:
    parity = K.synthetic_frame(n_markets=48, n_snapshots=3, n_dates=24, seed=3, name="parity")
    search = K.synthetic_frame(n_markets=48, n_snapshots=3, n_dates=24, seed=5, name="search")
    twin = K.copy_frame(search, name="gefs_twin")
    twin.hidden["realized_per_contract"] = twin.hidden["realized_per_contract"] * 0.5
    search.twin_index = np.arange(search.n_rows, dtype=np.int64)
    fs = frame_mod.FrameSet(parity=parity, search=search, gefs_twin=twin, provenance={"lane": "weather", "synthetic": True})
    out_dir, shas = gen0.save_frameset_like_freeze(fs, tmp_path / "frames", "weather", "2026-07-25")
    assert (out_dir / "parity").is_dir() and (out_dir / "search").is_dir() and (out_dir / "gefs_twin").is_dir()
    assert (out_dir / "frame.sha256").exists() and (out_dir / "provenance.json").exists()
    return out_dir


def _config(tmp_path: Path, frames_dir: Path) -> dict:
    cfg = yaml.safe_load(FAMILY_YAML.read_text(encoding="utf-8"))
    cfg["_config_path"] = str(FAMILY_YAML)
    cfg["_config_sha256"] = "c" * 64
    cfg.update({
        "frames_dir": str(frames_dir),
        "workers": 2,
        "bench": False,
        "run_id": "gen0_test",
        "git_rev": "deadbeef" * 5,
        "lock_sha256": "l" * 64,
        "fee_regime_sha256": "f" * 64,
        "repo_root": str(tmp_path),
        "registry_path": str(tmp_path / "reports" / "factory" / "registry.jsonl"),
    })
    return cfg


@pytest.fixture
def synthetic_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MP_GIT_REV", "deadbeef" * 5)
    frames_dir = _synthetic_frames_dir(tmp_path)
    return tmp_path, _config(tmp_path, frames_dir)


def test_registry_line_is_written_before_any_score(synthetic_run, monkeypatch):
    tmp_path, cfg = synthetic_run
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("score called")

    monkeypatch.setattr(FT, "score", boom)
    with pytest.raises(RuntimeError, match="score called"):
        gen0.run_gen0(cfg, tmp_path / "out")
    assert len(calls) == 1
    reg = registry_mod.Registry(cfg["registry_path"])
    line = reg.family_line(registry_mod.FAMILY_F1)
    assert line is not None and line["status"] == "OPEN"
    assert line["config_sha256"] == "c" * 64 and line["picker"] == "max_boot_lo_ties_fewer_clauses"
    assert line["cutoff"] == "2026-07-25" and line["thresholds"]["min_trades"] == 40
    assert reg.status(registry_mod.FAMILY_F1) == "OPEN"


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "/" + str(k), k, v
            yield from _walk(v, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


TIMESTAMP_KEYS = {"ts", "timestamp", "generated_at", "created_at", "updated_at", "as_of", "now", "time", "wall_clock"}
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def test_run_gen0_summary_rows_and_report(synthetic_run):
    tmp_path, cfg = synthetic_run
    out = tmp_path / "reports" / "factory" / "gen0_test"
    summary = gen0.run_gen0(cfg, out)

    # every seed, every row kind
    assert set(summary["seeds"]) == set(G.SEEDS)
    for name, e in summary["seeds"].items():
        for k in ("parity_full", "search_full"):
            assert isinstance(e[k], dict), (name, k)
            for f in ROW_FIELDS:
                assert f in e[k], (name, k, f)
            for arr in gen0.ARRAY_FIELDS:
                assert arr not in e[k]
        assert set(e["campaigns"]) == {"A", "B", "C", "ALL69"}
        for c in ("A", "B", "C", "ALL69"):
            assert isinstance(e["campaigns"][c]["search"], dict)
        # synthetic dates 06-01..06-24: campaign A validation (06-19..06-30) is present, B/C/ALL69 have none
        assert isinstance(e["campaigns"]["A"]["validation"], dict)
        assert e["campaigns"]["A"]["validation"]["constraint_reason"] in (None, "NO_TRADES")
        assert e["campaigns"]["ALL69"]["validation"] is None
        assert e["genome"]["gene_spec_version"] == 1 and "phenotype_hash" in e
    assert summary["seeds"]["fr31a_gefs"]["frame_scored"] == "gefs_twin"
    assert summary["seeds"]["fr31a_taker"]["frame_scored"] == "search"
    assert "mlweather_fallback" in summary["seeds"]
    # reference blocks only for the four Phase-2 shapes (compared against the real JSON, so no match on synthetic data)
    for name in G.PHASE2_SHAPE_LABELS:
        assert summary["seeds"][name]["reference"]["label"] == G.PHASE2_SHAPE_LABELS[name]
        assert summary["seeds"][name]["reference"]["matches_1e9"] is False
    assert summary["seeds"]["salvage_5f"]["reference"] is None

    # headline blocks
    assert summary["kind"] == "gen0" and summary["run_id"] == "gen0_test" and summary["family"] == registry_mod.FAMILY_F1
    assert summary["git_rev"] == "deadbeef" * 5 and summary["lock_sha256"] == "l" * 64
    assert summary["throughput"] is None
    assert set(summary["brier_skill_vs_market"]) == {"parity", "search"}
    assert "bss" in summary["brier_skill_vs_market"]["search"]
    assert summary["frame"]["parity_rows"] == 48 * 3 * 4 and summary["frame"]["twin_coverage"] == 1.0
    assert summary["registry_line"]["status"] == "OPEN" and "ts" not in summary["registry_line"]
    assert summary["constraint_order"][0] == "NO_TRADES" and summary["constraint_order"][-1] == "BSS"
    assert set(summary["seed_notes"]) == set(G.SEEDS)
    assert (out / "seed_date_pnl.json").exists()
    vec = json.loads((out / "seed_date_pnl.json").read_text(encoding="utf-8"))
    assert set(vec["seeds"]) == set(G.SEEDS)
    assert "per_date_pnl" in vec["seeds"]["fr31a_taker"]["parity_full"]

    # no wall-clock anywhere in the summary
    for path, key, value in _walk(summary):
        assert key not in TIMESTAMP_KEYS, path
        if isinstance(value, str):
            assert not ISO_DATETIME.search(value), (path, value)

    # (d) the report renders it and latest.json points at it
    paths = report_mod.write_gen0_report(summary, out, reports_root=tmp_path / "reports" / "factory")
    for p in paths.values():
        assert p.exists(), p
    latest = json.loads(paths["latest_json"].read_text(encoding="utf-8"))
    assert latest["summary"] == "gen0_test/summary.json" and latest["board"] == "gen0_test/board.md"
    assert latest["run_id"] == "gen0_test"
    board = paths["board_md"].read_text(encoding="utf-8")
    assert "# Factory board" in board and "weather/gfs_mex/taker/v1" in board
    md = paths["summary_md"].read_text(encoding="utf-8")
    assert "`mlweather_fallback`" in md and "(parity frame" in md and "(search frame" in md
    sj = json.loads(paths["summary_json"].read_text(encoding="utf-8"))
    for path, key, value in _walk(sj):
        assert key not in TIMESTAMP_KEYS, path
    # KILLED rows still render as KILLED after the -inf -> null round-trip
    killed = [n for n, e in sj["seeds"].items() if e["search_full"]["constraint_reason"]]
    if killed:
        assert f"KILLED:{sj['seeds'][killed[0]]['search_full']['constraint_reason']}" in report_mod.render_summary_md(sj)

    # idempotent rerun: the registry line is reused, never duplicated
    summary2 = gen0.run_gen0(cfg, out)
    lines = registry_mod.Registry(cfg["registry_path"]).lines()
    assert len([ln for ln in lines if ln["event"] == "family"]) == 1
    assert summary2["seeds"]["fr31a_taker"]["search_full"] == summary["seeds"]["fr31a_taker"]["search_full"]


def test_changed_config_under_same_family_aborts(synthetic_run):
    tmp_path, cfg = synthetic_run
    gen0.run_gen0(cfg, tmp_path / "out")
    other = dict(cfg, _config_sha256="0" * 64)
    with pytest.raises(registry_mod.RegistryError, match="NEW family name"):
        gen0.run_gen0(other, tmp_path / "out2")


def test_frames_dir_without_frames_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("MP_GIT_REV", "deadbeef" * 5)
    cfg = _config(tmp_path, tmp_path / "nowhere")
    with pytest.raises(gen0.Gen0Error, match="freeze-frame"):
        gen0.run_gen0(cfg, tmp_path / "out")
