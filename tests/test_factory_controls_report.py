"""``src.factory.controls`` + the F2 report (``report.build_family_summary`` & co.) with a fake ``run_procedure``.

The fake (``tests/factory_stats_testkit.fake_run_procedure``) writes real
ledgers / picks.json / oos/pooled.json in the contract layout, so everything
here exercises the disk contract the EVOLVE workstream must honour.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.factory import controls as CT
from src.factory import folds
from src.factory import gen0
from src.factory import registry as registry_mod
from src.factory import report as report_mod
from tests import factory_stats_testkit as SK

REPO = Path(__file__).resolve().parents[1]
TIMESTAMP_KEYS = {"ts", "timestamp", "generated_at", "created_at", "updated_at", "as_of", "now", "time", "wall_clock"}
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "/" + str(k), k, v
            yield from _walk(v, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _assert_timestamp_free(doc):
    for path, key, value in _walk(doc):
        assert key not in TIMESTAMP_KEYS, path
        if isinstance(value, str):
            assert not ISO_DATETIME.search(value), (path, value)


@pytest.fixture(scope="module")
def fs():
    return SK.synthetic_dev_frameset(n_per_city_date=2, n_snapshots=4, seed=13)


@pytest.fixture(scope="module")
def real_run(fs, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("f2run")
    cfg = SK.base_config(tmp)
    run_dir = tmp / "data" / "factory" / "runs" / "f2_test"
    res = SK.write_fake_run(fs, run_dir, cfg, master_seed=20260902, n_random=8, generations=2)
    return SimpleNamespace(tmp=tmp, cfg=cfg, run_dir=run_dir, result=res)


# ---------------------------------------------------------------------------
# fake procedure sanity (the disk contract)
# ---------------------------------------------------------------------------
def test_fake_run_layout(real_run):
    rd = real_run.run_dir
    picks = json.loads((rd / "picks.json").read_text(encoding="utf-8"))
    assert set(picks) == {"A", "B", "C", "ALL69"}
    for c, p in picks.items():
        for k in ("genome_json", "genome_id", "phenotype_hash", "picked_gen", "in_sample", "validation", "n_candidates"):
            assert k in p, (c, k)
        assert (p["validation"] is None) == (c == "ALL69")
    pooled = json.loads((rd / "oos" / "pooled.json").read_text(encoding="utf-8"))
    assert pooled["n_dates"] == len(pooled["per_date"]) and pooled["n_dates"] > 0
    assert {"date", "campaign", "pnl", "trades"} <= set(pooled["per_date"][0])
    assert sorted((rd / "ledger").iterdir()) and (rd / "ledger" / "A" / "gen_000.parquet").exists()
    assert real_run.result.pooled["mean"] == pooled["mean"]


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------
def test_run_controls_and_resume(fs, real_run):
    rd = real_run.run_dir
    fake = functools.partial(SK.fake_run_procedure, n_random=4, generations=1)
    calls = []

    def counting(*a, **k):
        calls.append(k.get("campaigns"))
        return fake(*a, **k)

    summary = CT.run_controls(fs, real_run.cfg, rd, real_run.result, cfg=None, master_seed=20260902,
                              n_snapshot=2, n_residual=2, run_procedure=counting, log=lambda s: None)
    assert len(calls) == 5 and all(c == ("A", "B", "C") for c in calls)
    assert (rd / "controls" / "summary.json").exists()
    on_disk = json.loads((rd / "controls" / "summary.json").read_text(encoding="utf-8"))
    _assert_timestamp_free(on_disk)
    for kind, n in (("snapshot", 2), ("residual", 2), ("planted", 1)):
        blk = on_disk[kind]
        assert blk["n"] == n and blk["n_done"] == n and blk["missing"] == []
        assert len(blk["replicates"]) == n and len(blk["pooled_means"]) == n
        for r in blk["replicates"]:
            assert set(r["p_rc_per_campaign"]) == {"A", "B", "C"} and set(r["picks"]) == {"A", "B", "C"}
            assert r["pooled_matches_procedure"] is True
            assert isinstance(r["boot_lo_gt0"], bool)
            for c in ("A", "B", "C"):
                assert (CT.replicate_dir(rd, kind, r["k"]) / "ledger" / c / "gen_000.parquet").exists()
    sn = on_disk["snapshot"]
    assert {"n_boot_lo_gt0", "ks_p_rc", "real_rank", "pass_boot_lo", "pass_ks", "p_rc_values"} <= set(sn)
    assert sn["ks_p_rc"]["n"] == len(sn["p_rc_values"]) and sn["real_rank"] in (1, 2, 3)
    rs = on_disk["residual"]
    assert {"p95", "real_rank", "real_exceeds_p95"} <= set(rs)
    pl = on_disk["planted"]
    for k in ("rule", "edge", "pick_pooled_on_planted", "pick_pooled_on_original", "captured", "capture_ratio", "pass", "rule_pooled_validation_delta"):
        assert k in pl, k
    assert pl["edge"] == 0.05 and pl["rule"]["name"] == "planted_no_win3_bands3_sig4"
    assert abs(pl["captured"] - (pl["pick_pooled_on_planted"]["mean"] - pl["pick_pooled_on_original"]["mean"])) < 1e-12
    assert on_disk["real_pooled_mean"] == real_run.result.pooled["mean"]
    # status.json carries controls_done (timestamp-free)
    st = json.loads((rd / "status.json").read_text(encoding="utf-8"))
    assert st["controls_done"] == {"snapshot": 2, "residual": 2, "planted": 1} and st["phase"] == "controls"
    _assert_timestamp_free(st)

    # resume: every replicate is skipped, the summary is byte-identical
    def boom(*a, **k):
        raise AssertionError("a finished replicate was re-run")

    before = (rd / "controls" / "summary.json").read_bytes()
    CT.run_controls(fs, real_run.cfg, rd, None, cfg=None, master_seed=20260902, n_snapshot=2, n_residual=2,
                    run_procedure=boom, log=lambda s: None)
    assert (rd / "controls" / "summary.json").read_bytes() == before

    # a partially-done kind reports the missing replicate instead of crashing
    part = CT.summarise_controls(rd, fs, 0.0, kinds=("snapshot",), n_snapshot=3, master_seed=20260902, log=lambda s: None)
    assert part["snapshot"]["n_done"] == 2 and part["snapshot"]["missing"] == [2] and part["snapshot"]["pass_boot_lo"] is False


def test_pooled_stats_matches_kernel():
    from src.factory import fitness

    v = np.array([0.1, -0.05, 0.02, 0.07, -0.01])
    st = CT.pooled_stats(v)
    draws = fitness.bootstrap_draws(v, n_boot=4000, seed=fitness.DEFAULT_SEED)
    assert st["mean"] == float(v.mean()) and st["n_dates"] == 5
    assert st["boot_lo"] == float(np.percentile(draws, 2.5)) and st["boot_hi"] == float(np.percentile(draws, 97.5))
    assert CT.pooled_stats([])["n_dates"] == 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
REQUIRED_TOP = ("picks", "pooled_oos", "multiplicity", "holm", "clustered_dsr", "n_phenotypes", "paired_vs_nofilter",
                "sensitivity", "bss_trades", "phenotype_jaccard", "controls", "blocked_folds", "verdict", "finalists",
                "registry_line", "frame", "campaigns", "evaluations", "tail_ratio")


@pytest.fixture(scope="module")
def reported(fs, real_run, tmp_path_factory):
    # make sure the controls exist (module order independence)
    if not (real_run.run_dir / "controls" / "summary.json").exists():
        CT.run_controls(fs, real_run.cfg, real_run.run_dir, real_run.result, cfg=None, master_seed=20260902, n_snapshot=2,
                        n_residual=2, run_procedure=functools.partial(SK.fake_run_procedure, n_random=4, generations=1), log=lambda s: None)
    reg_path = Path(real_run.cfg["registry_path"])
    reg = registry_mod.Registry(reg_path)
    line = reg.write_family_line(real_run.cfg["family"], lane="weather", source="gfs_mex", mode="taker", gene_spec_version=1,
                                 config_sha256="c" * 64, budget=real_run.cfg["budget"], picker=real_run.cfg["picker"],
                                 thresholds=real_run.cfg["thresholds"], cutoff="2026-07-25")
    registry_line = {k: v for k, v in line.items() if k != "ts"}
    registry_line["status"] = "OPEN"
    summary = report_mod.build_family_summary(real_run.run_dir, fs, real_run.cfg, registry_line)
    out = real_run.tmp / "reports" / "factory" / "f2_test"
    paths = report_mod.write_family_report(summary, out, reports_root=real_run.tmp / "reports" / "factory")
    return SimpleNamespace(summary=summary, paths=paths, out=out, registry_line=registry_line)


def test_summary_has_every_roadmap_field(reported, fs):
    s = reported.summary
    for k in REQUIRED_TOP:
        assert k in s, k
    for c in ("A", "B", "C", "ALL69"):
        p = s["picks"][c]
        for k in ("genes", "genome_json", "phenotype_hash", "in_sample", "n_candidates", "picked_gen", "genome_id"):
            assert k in p, (c, k)
        assert p["in_sample"]["boot_lo"] is not None and p["in_sample_matches_picks_json"] is True
        m = s["multiplicity"][c]
        assert {"p_rc", "p_spa", "L", "D", "n_phenotypes"} <= set(m) and m["pick_in_ledger"] is True
        assert 0.0 <= m["p_rc"] <= 1.0 and m["p_spa"] <= m["p_rc"] + 1e-12
        assert s["n_phenotypes"][c] >= 1
    assert s["picks"]["ALL69"]["validation"] is None and s["picks"]["A"]["validation"] is not None
    po = s["pooled_oos"]
    assert po["n_dates"] == len(po["per_date"]) and po["matches_procedure"] is True and 0.0 <= po["one_sided_p"] <= 1.0
    assert set(s["holm"]["inputs"]) == {s["family"]} and s["holm"]["this_family"]["p_adj"] == po["one_sided_p"]
    d = s["clustered_dsr"]
    assert d["n"] == po["n_dates"] and d["n_trials"] == s["n_phenotypes"]["distinct_abc"] and 0.0 <= d["dsr"] <= 1.0
    # S2: the headline DSR is the MAD-robust one and must be finite; the raw-variance companion sits under "raw"
    assert d["sr_var_source"] in ("ledger_sr_distribution_mad", "single_estimate_sampling_variance")
    assert math.isfinite(d["expected_max_sr"]) and d["expected_max_sr"] < 1e3, d
    assert set(d["raw"]) >= {"dsr", "expected_max_sr", "sr_var_trials"} and d["n_sr_trials_clipped"] >= 0 and d["sr_clip"] == 50.0
    # S1: feasible-set p_RC is the headline, the all-phenotype p sits beside it
    for c in ("A", "B", "C", "ALL69"):
        m = s["multiplicity"][c]
        assert {"p_rc_all", "p_spa_all", "L_feasible", "L_all", "pick_feasible"} <= set(m), c
        assert 1 <= m["L_feasible"] <= m["L_all"] and 0.0 <= m["p_rc_all"] <= 1.0
    pv = s["paired_vs_nofilter"]
    assert pv["baseline"] == "nofilter_no" and set(pv["per_campaign"]) == {"A", "B", "C"} and "boot_lo" in pv["pooled"]
    for k in ("adverse_0.02", "adverse_0.03", "embargo_2"):
        assert s["sensitivity"][k]["sign"] in ("+", "-", "0", None), k
    # S4: without a rebuilt embargo-2 frame the sensitivity is NOT copied from the headline
    emb = s["sensitivity"]["embargo_2"]
    assert emb["available"] is False and emb["sign"] is None and "not computed" in emb["note"]
    assert s["verdict"]["not_applicable"] == ["sign_survives_embargo2"]
    assert "sign_survives_embargo2" not in s["verdict"]["failing"]
    # D2: residual null carries the paired statistic; S3: planted disclosure fields
    rs = s["controls"]["residual"]
    assert {"paired_deltas", "paired_p95", "real_paired_delta", "real_paired_rank", "raw_means", "note"} <= set(rs)
    assert len(rs["paired_deltas"]) == rs["n_done"] and rs["real_paired_rank"] is not None
    assert abs(float(rs["real_paired_delta"]) - float(pv["pooled"]["mean"])) < 1e-12
    pl = s["controls"]["planted"]
    assert {"rule_capture_ratio", "pick_flipped_trades", "pick_validation_trades", "pick_rule_overlap", "note"} <= set(pl)
    assert 0 <= pl["pick_flipped_trades"] <= pl["pick_validation_trades"]
    assert s["verdict"]["residual_real_paired_rank"] == rs["real_paired_rank"]
    assert set(s["bss_trades"]) >= {"A", "B", "C", "pooled"}
    assert set(s["phenotype_jaccard"]["pairs"]) == {"A/B", "A/C", "A/ALL69", "B/C", "B/ALL69", "C/ALL69"}
    assert all(0.0 <= v <= 1.0 for v in s["phenotype_jaccard"]["pairs"].values())
    assert s["controls"]["planted"]["edge"] == 0.05 and s["blocked_folds"]["label"].startswith("in-sample blocks")
    v = s["verdict"]
    assert v["status"] in ("PROPOSED", "CLOSED") and set(v["conditions"]) == set(report_mod.VERDICT_CONDITIONS)
    assert v["status"] == report_mod.verdict(s)
    assert v["controls_complete"] is True and v["residual_real_rank"] == s["controls"]["residual"]["real_rank"]
    assert s["finalists"] and s["finalists"][0]["genome_id"] == s["picks"]["ALL69"]["genome_id"]
    assert s["tail_ratio"]["gate_applicable"] is False
    _assert_timestamp_free(s)


def test_report_files_and_latest(reported):
    p = reported.paths
    for k in ("summary_json", "summary_md", "board_md", "oos_csv", "finalists_json", "status_json", "latest_json"):
        assert p[k].exists(), k
    board = p["board_md"].read_text(encoding="utf-8")
    assert len(board) <= report_mod.BOARD_MAX_CHARS
    assert "VERDICT: " in board and "RESIDUAL-NULL paired rank" in board and reported.summary["verdict"]["status"] in board
    assert "all-phen" in board and "raw rank" in board
    md = p["summary_md"].read_text(encoding="utf-8")
    assert "## VERDICT" in md and "Holm" in md and "Clustered DSR" in md and "planted edge" in md
    csv = p["oos_csv"].read_text(encoding="utf-8").splitlines()
    assert csv[0] == "date,campaign,pnl,trades" and len(csv) == 1 + reported.summary["pooled_oos"]["n_dates"]
    fin = json.loads(p["finalists_json"].read_text(encoding="utf-8"))
    assert fin["all69_pick"]["genome_id"] == reported.summary["picks"]["ALL69"]["genome_id"] and 1 <= len(fin["finalists"]) <= 3
    latest = json.loads(p["latest_json"].read_text(encoding="utf-8"))
    assert latest["run"] == "f2_test" and latest["board"] == "f2_test/board.md" and latest["verdict"] == reported.summary["verdict"]["status"]
    status = json.loads(p["status_json"].read_text(encoding="utf-8"))
    assert status["verdict"] == reported.summary["verdict"]["status"]
    _assert_timestamp_free(status)
    _assert_timestamp_free(json.loads(p["summary_json"].read_text(encoding="utf-8")))


def test_report_twice_is_byte_identical(reported, fs, real_run):
    first = reported.paths["summary_json"].read_bytes()
    again = report_mod.build_family_summary(real_run.run_dir, fs, real_run.cfg, reported.registry_line)
    paths = report_mod.write_family_report(again, reported.out, reports_root=real_run.tmp / "reports" / "factory")
    assert paths["summary_json"].read_bytes() == first
    assert paths["board_md"].read_text(encoding="utf-8") == reported.paths["board_md"].read_text(encoding="utf-8")


def test_verdict_rules():
    base = {
        "thresholds": {"pooled_boot_lo_gt": 0.0, "holm_alpha": 0.05, "p_rc_all69_lt": 0.10},
        "pooled_oos": {"mean": 0.05, "boot_lo": 0.01, "cities": 4},
        "holm": {"this_family": {"p_adj": 0.01}},
        "multiplicity": {"ALL69": {"p_rc": 0.05}},
        "controls": {"snapshot": {"n": 1, "n_done": 1, "pooled_means": [-0.02], "pass_boot_lo": True, "pass_ks": True},
                     "residual": {"n": 1, "n_done": 1, "pooled_means": [0.80], "real_rank": 2,
                                  "paired_deltas": [0.0], "paired_p95": 0.0, "real_paired_delta": 0.02, "real_paired_rank": 1},
                     "planted": {"n": 1, "n_done": 1, "pass": True}},
        "paired_vs_nofilter": {"pooled": {"boot_lo": 0.005, "mean": 0.02}},
        "sensitivity": {"adverse_0.02": {"sign": "+"}, "adverse_0.03": {"sign": "+"}, "embargo_2": {"sign": "+", "available": True}},
        "bss_trades": {"pooled": 0.02},
    }
    # the residual null's RAW means (0.80) no longer matter: the paired delta beats the paired p95 (section 6.4a)
    assert report_mod.verdict(base) == "PROPOSED" and report_mod.evaluate_verdict(base)["failing"] == []
    assert report_mod.evaluate_verdict(base)["not_applicable"] == []
    bad = json.loads(json.dumps(base))
    bad["pooled_oos"]["boot_lo"] = -0.01
    bad["holm"]["this_family"]["p_adj"] = 0.2
    v = report_mod.evaluate_verdict(bad)
    assert v["status"] == "CLOSED" and v["failing"] == ["pooled_boot_lo_gt0", "holm_p_lt_alpha"]
    worse = json.loads(json.dumps(base))
    worse["controls"]["residual"]["paired_p95"] = 0.09
    assert report_mod.evaluate_verdict(worse)["failing"] == ["beats_every_control"]
    snap = json.loads(json.dumps(base))
    snap["controls"]["snapshot"]["pooled_means"] = [0.06]
    assert report_mod.evaluate_verdict(snap)["failing"] == ["beats_every_control"]
    nocontrols = json.loads(json.dumps(base))
    nocontrols["controls"] = None
    assert "beats_every_control" in report_mod.evaluate_verdict(nocontrols)["failing"]
    # S4: an unavailable embargo-2 sensitivity is not applicable, never a failure and never a pass
    noemb = json.loads(json.dumps(base))
    noemb["sensitivity"]["embargo_2"] = {"available": False, "sign": None}
    v2 = report_mod.evaluate_verdict(noemb)
    assert v2["status"] == "PROPOSED" and v2["not_applicable"] == ["sign_survives_embargo2"] and v2["conditions"]["sign_survives_embargo2"] is None
    negemb = json.loads(json.dumps(base))
    negemb["sensitivity"]["embargo_2"] = {"available": True, "sign": "-"}
    assert report_mod.evaluate_verdict(negemb)["failing"] == ["sign_survives_embargo2"]


def test_embargo2_sensitivity_from_a_rebuilt_frame(reported, real_run, fs):
    """S4: a second FrameSet (standing in for `freeze-frame --embargo-days 2`) makes embargo_2 available and applicable."""
    fs2 = SK.synthetic_dev_frameset(n_per_city_date=2, n_snapshots=4, seed=14)
    s = report_mod.build_family_summary(real_run.run_dir, fs, real_run.cfg, reported.registry_line,
                                        sensitivity_fs=fs2, sensitivity_frames_dir="C:/frames/emb2")
    emb = s["sensitivity"]["embargo_2"]
    assert emb["available"] is True and emb["sign"] in ("+", "-", "0", None) and len(emb["frame_sha256"]) == 64
    assert emb["frames_dir"] and set(emb["per_campaign"]) <= {"A", "B", "C"}
    assert s["verdict"]["not_applicable"] == [] and "sign_survives_embargo2" in s["verdict"]["conditions"]
    _assert_timestamp_free(s)


# ---------------------------------------------------------------------------
# CLI: factory.py report <run_id> (in-process, roots monkeypatched into tmp)
# ---------------------------------------------------------------------------
def _load_cli():
    spec = importlib.util.spec_from_file_location("factory_cli", REPO / "scripts" / "factory.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_report_transitions_registry_idempotently(fs, tmp_path, monkeypatch):
    monkeypatch.setenv("MP_GIT_REV", "deadbeef" * 5)
    cli = _load_cli()
    frames_dir, _ = gen0.save_frameset_like_freeze(fs, tmp_path / "frames", "weather", "2026-07-25")
    cfg = cli.load_family_config(cli.DEFAULT_FAMILY_CONFIG)
    runs_root = tmp_path / "runs"
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(cli, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(cli, "REPORTS_ROOT", reports_root)
    run_dir = runs_root / "f2_cli"
    SK.write_fake_run(fs, run_dir, cfg, master_seed=1, frames_dir=frames_dir, n_random=6, generations=1)
    CT.run_controls(fs, cfg, run_dir, None, cfg=None, master_seed=1, n_snapshot=1, n_residual=1,
                    run_procedure=functools.partial(SK.fake_run_procedure, n_random=3, generations=1), log=lambda s: None)
    reg = registry_mod.Registry(reports_root / "registry.jsonl")
    reg.write_family_line(cfg["family"], lane="weather", source="gfs_mex", mode="taker", gene_spec_version=1,
                          config_sha256=cfg["_config_sha256"], budget=cfg["budget"], picker=cfg["picker"],
                          thresholds=cfg["thresholds"], cutoff="2026-07-25")
    parser = cli.build_parser()
    assert "controls" in parser.format_help() and "report" in parser.format_help()
    args = parser.parse_args(["report", "f2_cli"])
    assert cli.cmd_report(args) == 0
    out = reports_root / "f2_cli"
    first = (out / "summary.json").read_bytes()
    status = reg.status(cfg["family"])
    assert status in ("PROPOSED", "CLOSED")
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["verdict"]["status"] == status
    assert (out / "run.json").exists() and (out / "board.md").exists() and (out / "oos_by_date.csv").exists()
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "DONE" and st["reported"] is True and st["verdict"] == status
    # second report: byte-identical, no second transition line
    n_lines = len(reg.lines())
    assert cli.cmd_report(parser.parse_args(["report", "f2_cli"])) == 0
    assert (out / "summary.json").read_bytes() == first
    assert len(reg.lines()) == n_lines and reg.status(cfg["family"]) == status
    latest = json.loads((reports_root / "latest.json").read_text(encoding="utf-8"))
    assert latest["run"] == "f2_cli" and latest["board"] == "f2_cli/board.md"
    # controls subcommand parses
    a = parser.parse_args(["controls", "f2_cli", "--kinds", "snapshot", "--n-snapshot", "3", "--workers", "2"])
    assert a.n_snapshot == 3 and a.kinds == "snapshot" and a.workers == 2


# ---------------------------------------------------------------------------
# F2 integration regression: a NO_FEASIBLE campaign pick (picks.json genome_json null)
# must not crash controls or the report; it is recorded and fails the verdict.
# ---------------------------------------------------------------------------
def _drop_pick(run_dir: Path, campaign: str) -> None:
    p = run_dir / "picks.json"
    picks = json.loads(p.read_text(encoding="utf-8"))
    picks[campaign] = {"genome_json": None, "genome_id": None, "phenotype_hash": None, "picked_gen": None,
                       "in_sample": None, "validation": None, "n_candidates": 0, "reason": "NO_FEASIBLE"}
    p.write_text(json.dumps(picks, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_no_feasible_pick_tolerated(fs, tmp_path):
    cfg = SK.base_config(tmp_path)
    run_dir = tmp_path / "data" / "factory" / "runs" / "f2_nofeasible"
    res = SK.write_fake_run(fs, run_dir, cfg, master_seed=7, n_random=6, generations=1)
    _drop_pick(run_dir, "A")
    assert not CT.pick_present(json.loads((run_dir / "picks.json").read_text(encoding="utf-8"))["A"])
    with pytest.raises(ValueError):
        CT.pick_genome({"genome_json": None, "reason": "NO_FEASIBLE"})

    def fake_missing_a(fs_k, config, rd, **kw):
        out = SK.fake_run_procedure(fs_k, config, rd, n_random=4, generations=1, **kw)
        _drop_pick(Path(rd), "A")
        return out

    ctrl = CT.run_controls(fs, cfg, run_dir, res, cfg=None, master_seed=7, n_snapshot=1, n_residual=1,
                           run_procedure=fake_missing_a, log=lambda s: None)
    for kind in ("snapshot", "residual", "planted"):
        rep = ctrl[kind]["replicates"][0]
        assert rep["picks_missing"] == {"A": "NO_FEASIBLE"} and "A" not in rep["picks"]
        assert rep["pooled"]["n_dates"] > 0  # B and C still pool
    assert ctrl["planted"]["capture_ratio"] == ctrl["planted"]["capture_ratio"]  # finite, B/C only

    summary = report_mod.build_family_summary(run_dir, fs, cfg, {"family": cfg["family"], "status": "OPEN"})
    assert summary["picks_missing"] == {"A": "NO_FEASIBLE"}
    assert summary["picks"]["A"]["missing"] is True and summary["picks"]["A"]["reason"] == "NO_FEASIBLE"
    assert summary["pooled_oos"]["picks_missing"] == {"A": "NO_FEASIBLE"}
    assert "A" not in summary["multiplicity"] and "A" not in summary["phenotype_jaccard"]["markets_per_pick"]
    assert summary["verdict"]["status"] == "CLOSED" and "headline_picks_present" in summary["verdict"]["failing"]
    out = tmp_path / "reports" / "factory" / "f2_nofeasible"
    paths = report_mod.write_family_report(summary, out, reports_root=tmp_path / "reports" / "factory")
    assert Path(paths["summary_json"]).exists() and "CLOSED" in (out / "board.md").read_text(encoding="utf-8")
    again = report_mod.build_family_summary(run_dir, fs, cfg, {"family": cfg["family"], "status": "OPEN"})
    assert json.dumps(report_mod._json_safe(again), sort_keys=True) == json.dumps(report_mod._json_safe(summary), sort_keys=True)
