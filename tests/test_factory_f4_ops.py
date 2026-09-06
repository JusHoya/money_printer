"""F4 OPS (PRD_STRATEGY_FACTORY FR-F4.2; Phase F4 exit criteria 4-5).

Pins, one section each:

1. GATE REGISTRATION FLOW -- ``src.factory.registration`` + ``factory.py register-gate``
   + ``promote --mode paper``: the registration is built from the template and the
   promoted spec, names the PAPER spec's hash, refuses to stamp a commit time that
   git cannot vouch for, and a paper promotion refuses without it.
2. PAPER BOARD ROW -- ``src.factory.paper`` + ``report.render_board(paper=...)``:
   settled ``target_date`` units from the journal joined to ``closed_trades``
   (never equity), the prediction beside it, ``k/n_min`` progress, the honest
   shadow note, and the KILLED marker on HALT / a failed gate.
3. SETTLEMENT LATENCY -- ``scripts/check_settlement_latency.py``: per-position gap
   against the runtime's own close rule, overdue open positions, per-strategy
   lines including the genome's ``NO EVIDENCE`` line.
4. GATE VERDICT FILE -- ``scripts/gate.py`` default ``--out`` and the four
   top-level aliases the criterion names (statistics untouched).
5. WEB ROUTE -- ``GET /api/closed_trades`` is side-effect free, token-free, capped.
6. WEEKLY RECONCILE CRON -- the spark units + wrapper + Hermes watch script exist and
   say what they must.
7. RUNBOOK + HANDOFF DRAFT exist and name the PRD commands.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.factory import genome as G  # noqa: E402
from src.factory import paper as PAPER  # noqa: E402
from src.factory import promoted as P  # noqa: E402
from src.factory import registration as REG  # noqa: E402
from src.factory import report as REPORT  # noqa: E402


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"mp_{name}", REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _spec(mode="shadow", status="CLOSED", seed="fr31a_taker") -> P.PromotedSpec:
    return P.build_spec(
        G.SEEDS[seed], family="weather/gfs_mex/taker/v1", config_sha256="c" * 64,
        frame_search_sha256="f" * 64, calibration_dir="data/calibration", calibration_sha256="d" * 64,
        calibration_kind="walk_forward", fee_type="quadratic", fee_regime_sha256="e" * 64,
        mode=mode, registry_status=status, source="seed",
    )


def _factory(*args, env_extra=None):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "factory.py"), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=180,
    )


def _git_available() -> bool:
    return shutil.which("git") is not None


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


@pytest.fixture
def git_repo(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    r = _git(["init", "-q"], tmp_path)
    assert r.returncode == 0, r.stderr
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    return tmp_path


# ===========================================================================
# 1. GATE REGISTRATION FLOW
# ===========================================================================
class TestRegistrationBuild:
    def test_build_fills_every_placeholder_and_names_the_paper_hash(self):
        spec = _spec()
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        assert doc["genome_id"] == spec.genome_id
        assert doc["strategy_name"] == f"Genome {spec.id8}"
        assert doc["promoted_spec_path"] == f"configs/factory/promoted/{spec.genome_id}.json"
        assert doc["adverse_fill"] == spec.adverse_fill and doc["fee_type"] == "taker"
        assert doc["registration_commit_utc"] is None
        assert doc["schema_version"] == REG.SCHEMA_VERSION
        assert not any(isinstance(v, str) and "REPLACE_ME" in v for v in doc.values())
        # the hash is the PAPER spec's, not the shadow spec's on disk
        paper = _spec(mode="paper", status="PROPOSED")
        assert doc["spec_hash"] == paper.spec_hash != spec.spec_hash
        assert doc["spec_hash"] == REG.paper_spec_hash(spec.to_doc(), registry_status="PROPOSED")

    def test_template_doc_block_is_preserved(self):
        doc = REG.build_registration(_spec().to_doc(), registry_status="PROPOSED")
        assert "_doc" in doc and "registered_before_first_trade" in doc["_doc"]

    def test_write_is_lf_indent2_and_round_trips(self, tmp_path):
        doc = REG.build_registration(_spec().to_doc(), registry_status="PROPOSED")
        p = REG.write_registration(doc, tmp_path / "gate_registration.json")
        raw = p.read_bytes()
        assert b"\r\n" not in raw and raw.endswith(b"\n")
        assert REG.load_registration(p) == doc
        assert list(REG.load_registration(p))[0] == "_doc"


class TestRegistrationCheck:
    def test_ok_registration_has_no_problems_once_stamped(self):
        spec = _spec()
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        assert REG.check_registration(doc, spec.to_doc(), registry_status="PROPOSED", require_commit_time=False) == []
        problems = REG.check_registration(doc, spec.to_doc(), registry_status="PROPOSED")
        assert len(problems) == 1 and "registration_commit_utc is null" in problems[0]
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        assert REG.check_registration(doc, spec.to_doc(), registry_status="PROPOSED") == []

    def test_every_mismatch_is_named(self):
        spec = _spec()
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        other = _spec(seed="fr31b")
        problems = REG.check_registration(doc, other.to_doc(), registry_status="PROPOSED")
        joined = "\n".join(problems)
        assert "genome_id" in joined and "strategy_name" in joined and "spec_hash" in joined
        assert "promoted_spec_path" in joined
        # the same genome under a different registry status is a different paper spec
        problems = REG.check_registration(doc, spec.to_doc(), registry_status="RATIFIED")
        assert problems and all("spec_hash" in p for p in problems)
        # adverse_fill is a registered quantity
        doc2 = dict(doc, adverse_fill=0.02)
        problems = REG.check_registration(doc2, spec.to_doc(), registry_status="PROPOSED")
        assert any("adverse_fill" in p for p in problems)

    def test_expected_hash_override_is_what_promote_checks_after_build(self):
        spec = _spec()
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        paper = _spec(mode="paper", status="PROPOSED")
        assert REG.check_registration(doc, paper.to_doc(), registry_status="PROPOSED", expected_spec_hash=paper.spec_hash) == []
        assert REG.check_registration(doc, paper.to_doc(), registry_status="PROPOSED", expected_spec_hash="0" * 64)


class TestFillCommitTime:
    def _write(self, repo: Path) -> Path:
        doc = REG.build_registration(_spec().to_doc(), registry_status="PROPOSED")
        return REG.write_registration(doc, repo / "configs" / "factory" / "gate_registration.json")

    def test_refuses_untracked(self, git_repo):
        p = self._write(git_repo)
        with pytest.raises(REG.RegistrationError, match="not committed"):
            REG.fill_commit_time(p)
        assert REG.load_registration(p)["registration_commit_utc"] is None

    def test_refuses_uncommitted_edits(self, git_repo):
        p = self._write(git_repo)
        _git(["add", "."], git_repo)
        _git(["commit", "-q", "-m", "register"], git_repo)
        doc = REG.load_registration(p)
        doc["adverse_fill"] = 0.02
        REG.write_registration(doc, p)
        with pytest.raises(REG.RegistrationError, match="uncommitted"):
            REG.fill_commit_time(p)

    def test_fills_from_the_adding_commit(self, git_repo):
        p = self._write(git_repo)
        _git(["add", "."], git_repo)
        r = _git(["commit", "-q", "-m", "register"], git_repo)
        assert r.returncode == 0, r.stderr
        stamp = REG.fill_commit_time(p)
        want = _git(["log", "--diff-filter=A", "--format=%cI", "--", "configs/factory/gate_registration.json"], git_repo).stdout.strip()
        assert stamp == want and stamp
        assert REG.load_registration(p)["registration_commit_utc"] == stamp
        assert REG.git_file_state(p) == "modified"  # the stamp itself is the next commit

    def test_refuses_a_typed_value_that_contradicts_git(self, git_repo):
        p = self._write(git_repo)
        doc = REG.load_registration(p)
        doc["registration_commit_utc"] = "1970-01-01T00:00:00+00:00"
        REG.write_registration(doc, p)
        _git(["add", "."], git_repo)
        _git(["commit", "-q", "-m", "register"], git_repo)
        with pytest.raises(REG.RegistrationError, match="typed value"):
            REG.fill_commit_time(p)

    def test_refuses_outside_a_repo(self, tmp_path):
        if not _git_available():
            pytest.skip("git not installed")
        p = self._write(tmp_path)
        # tmp_path may itself sit inside some repo on a dev box; only assert when it does not
        if REG.git_file_state(p) == "not-a-repo":
            with pytest.raises(REG.RegistrationError):
                REG.fill_commit_time(p)


class TestRegisterGateCLI:
    def test_is_a_real_subcommand(self):
        r = _factory("register-gate", "--help")
        assert r.returncode == 0 and "--fill-commit-time" in r.stdout

    def test_writes_registration_and_prints_the_git_command(self, tmp_path):
        gid = "0c4b20502f2daf65"  # the deployed fr31a_taker spec on disk
        out = tmp_path / "gate_registration.json"
        r = _factory("register-gate", gid, "--registration", str(out), "--allow-closed")
        assert r.returncode == 0, r.stderr
        assert out.exists()
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["genome_id"] == gid and doc["strategy_name"] == "Genome 0c4b2050"
        assert doc["registration_commit_utc"] is None
        assert "git log --diff-filter=A --format=%cI -- configs/factory/gate_registration.json" in r.stdout \
            or "git log --diff-filter=A --format=%cI --" in r.stdout
        assert "register-gate --fill-commit-time" in r.stdout
        spec = P.load_promoted(gid)
        assert doc["spec_hash"] == REG.paper_spec_hash(spec.to_doc(), registry_status="CLOSED")

    def test_refuses_for_a_closed_family_without_allow_closed(self, tmp_path):
        out = tmp_path / "gate_registration.json"
        r = _factory("register-gate", "0c4b20502f2daf65", "--registration", str(out))
        assert r.returncode == 1 and "CLOSED" in r.stderr and not out.exists()

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        out = tmp_path / "gate_registration.json"
        out.write_text("{}", encoding="utf-8")
        r = _factory("register-gate", "0c4b20502f2daf65", "--registration", str(out), "--allow-closed")
        assert r.returncode == 1 and "exists" in r.stderr

    def test_fill_commit_time_refuses_uncommitted_file(self, tmp_path):
        out = tmp_path / "gate_registration.json"
        REG.write_registration(REG.build_registration(_spec().to_doc(), registry_status="PROPOSED"), out)
        r = _factory("register-gate", "--fill-commit-time", "--registration", str(out))
        assert r.returncode == 1 and ("not committed" in r.stderr or "not inside a git checkout" in r.stderr)
        assert json.loads(out.read_text(encoding="utf-8"))["registration_commit_utc"] is None

    def test_maker_genome_has_no_gate(self, tmp_path):
        gid = P.genome_id_for(P.genome_json_for(G.SEEDS["salvage_5f"]))
        spec = P.build_spec(
            G.SEEDS["salvage_5f"], family="weather/gfs_mex/taker/v1", config_sha256="c" * 64,
            frame_search_sha256="f" * 64, calibration_dir="data/calibration", calibration_sha256="d" * 64,
            fee_type="quadratic", fee_regime_sha256="e" * 64, mode="shadow", registry_status="CLOSED", source="seed",
        )
        d = tmp_path / "specs"
        d.mkdir()
        P.write_promoted(spec, d / f"{gid}.json")
        r = _factory("register-gate", gid, "--spec-dir", str(d), "--registration", str(tmp_path / "r.json"), "--allow-closed")
        assert r.returncode == 1 and "MAKER" in r.stderr


class TestPromotePaperRequiresRegistration:
    """``promote --mode paper`` refuses before any parity work unless the registration licenses it."""

    @pytest.fixture
    def factory_mod(self):
        return _load_script("factory")

    def _args(self, registration_path, **over):
        base = dict(
            id="0c4b20502f2daf65", from_seed="fr31a_taker", from_pick=None, mode="paper", frames=None,
            ladders=None, run_id=None, config=None, out_dir=None, min_sizable_fraction=None,
            registration=str(registration_path),
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_closed_family_refusal_still_comes_first(self, factory_mod, tmp_path, capsys):
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(tmp_path / "absent.json"))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "paper refused" in err and "CLOSED" in err
        assert "gate_registration" not in err  # the governance refusal, not the registration one

    def test_missing_registration_refuses_when_family_is_proposed(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(tmp_path / "absent.json"))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "--mode paper refused" in err and "does not exist" in err and "register-gate" in err

    def test_unstamped_registration_refuses(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        spec = P.load_promoted("0c4b20502f2daf65")
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        p = REG.write_registration(doc, tmp_path / "gate_registration.json")
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(p))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "registration_commit_utc is null" in err

    def test_wrong_genome_registration_refuses(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        doc = REG.build_registration(_spec(seed="fr31b").to_doc(), registry_status="PROPOSED")
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        p = REG.write_registration(doc, tmp_path / "gate_registration.json")
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(p))
        assert e.value.code == 1
        assert "genome_id" in capsys.readouterr().err

    def test_shadow_promotion_never_consults_the_registration(self, factory_mod, tmp_path, capsys, monkeypatch):
        """Shadow needs no registration: the refusal it hits next is the frames one, not ours."""
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        monkeypatch.setattr(factory_mod, "_latest_frames_dir", lambda lane: None)
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(tmp_path / "absent.json", mode="shadow"))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "frames" in err and "registration" not in err.lower()


# ===========================================================================
# 2. PAPER BOARD ROW
# ===========================================================================
def _row(symbol, strategy, entry, qty, pnl, target_date, *, settled=True, entry_time="2026-09-10T15:00:05"):
    return {
        "symbol": symbol, "strategy_name": strategy, "entry_time": entry_time, "exit_time": "2026-09-11T04:00:01",
        "entry_price": entry, "exit_price": (1.0 if pnl > 0 else 0.0) if settled else 0.5, "quantity": qty,
        "side": "buy", "contract_side": "NO", "pnl": pnl,
        "close_reason": "EXPIRATION" if settled else "STOP_LOSS",
        "settlement_outcome": ("no" if pnl > 0 else "yes") if settled else None,
        "settlement_high": 80.0, "target_date": target_date,
    }


class TestPaperSettledUnits:
    NAME = "Genome 0c4b2050"

    def test_admission_matches_the_gate(self):
        assert PAPER.is_settled(_row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.8, 10, 1.0, "2026-09-10"))
        assert not PAPER.is_settled(_row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.8, 10, 1.0, "2026-09-10", settled=False))
        r = _row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.8, 10, 1.0, "2026-09-10")
        r["settlement_outcome"] = None
        assert not PAPER.is_settled(r)

    def test_units_are_target_dates_and_fees_come_from_the_ledger(self):
        j = [
            _row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.80, 10, 2.0, "2026-09-10"),
            _row("KXHIGHCHI-26SEP10-B75.5", self.NAME, 0.70, 10, 3.0, "2026-09-10", entry_time="2026-09-10T15:00:07"),
            _row("KXHIGHNY-26SEP11-B80.5", self.NAME, 0.90, 10, -9.0, "2026-09-11", entry_time="2026-09-11T15:00:05"),
            _row("KXHIGHNY-26SEP11-T85", "Meteorologist V2", 0.5, 50, 25.0, "2026-09-11"),  # other strategy
            _row("KXHIGHNY-26SEP12-B80.5", self.NAME, 0.8, 10, 2.0, "2026-09-12", settled=False),  # not settled
        ]
        ledger = [
            {"symbol": "KXHIGHNY-26SEP10-B80.5", "strategy_name": self.NAME, "open_time": "2026-09-10T15:00:05", "entry_fee": 0.11},
            {"symbol": "KXHIGHCHI-26SEP10-B75.5", "strategy_name": self.NAME, "open_time": "2026-09-10T15:00:07", "entry_fee": 0.15},
        ]
        fills = PAPER.settled_fills(j, ledger, strategy_name=self.NAME)
        assert [f["symbol"] for f in fills] == ["KXHIGHNY-26SEP10-B80.5", "KXHIGHCHI-26SEP10-B75.5", "KXHIGHNY-26SEP11-B80.5"]
        assert [f["fee_source"] for f in fills] == ["closed_trades", "closed_trades", "recomputed_taker"]
        assert fills[0]["entry_fee"] == 0.11 and fills[2]["entry_fee"] == PAPER.taker_fee_for("KXHIGHNY-26SEP11-B80.5", 0.90, 10)
        s = PAPER.sandbox_summary(fills)
        assert s["settled_trades"] == 3 and s["settled_target_dates"] == 2
        assert s["target_dates"] == ["2026-09-10", "2026-09-11"]
        net = (2.0 - 0.11) + (3.0 - 0.15) + (-9.0 - fills[2]["entry_fee"])
        assert s["net_pnl"] == pytest.approx(net) and s["contracts"] == 30
        assert s["c_per_contract"] == pytest.approx(net / 30)

    def test_empty_record_is_zero_units_not_an_error(self):
        s = PAPER.sandbox_summary(PAPER.settled_fills([], [], strategy_name=self.NAME))
        assert s == {**s, "settled_trades": 0, "settled_target_dates": 0, "c_per_contract": None}


class TestPaperPrediction:
    def test_family_pick_uses_pooled_oos(self):
        fam = {"run_id": "run_x", "picks": {"ALL69": {"genome_id": "abc"}},
               "pooled_oos": {"mean": 0.0308, "boot_lo": -0.09, "boot_hi": 0.1417, "n_dates": 29, "trades": 49}}
        p = PAPER.prediction_for("abc", family_summary=fam)
        assert p["c_per_contract"] == 0.0308 and "pooled OOS" in p["source"] and p["dates"] == 29

    def test_seed_uses_search_frame_realized(self):
        gid = P.genome_id_for(P.genome_json_for(G.SEEDS["fr31a_taker"]))
        gen0 = {"run_id": "gen0_x", "seeds": {"fr31a_taker": {"search_full": {"realized": 0.0723, "boot_lo": 0.0145, "boot_hi": 0.1225, "dates": 57, "trades": 130}}}}
        p = PAPER.prediction_for(gid, gen0_summary=gen0)
        assert p["c_per_contract"] == 0.0723 and "search frame" in p["source"] and "fr31a_taker" in p["source"]

    def test_unknown_genome_has_no_prediction(self):
        p = PAPER.prediction_for("ffffffffffffffff", family_summary={"picks": {}}, gen0_summary={"seeds": {}})
        assert p["c_per_contract"] is None and p["source"] is None


class TestPaperRowRendering:
    def _paper(self, **over):
        sandbox = {"settled_trades": 0, "settled_target_dates": 0, "contracts": 0.0, "net_pnl": 0.0, "c_per_contract": None, "fee_sources": []}
        pred = {"c_per_contract": 0.0723, "lo": 0.0145, "hi": 0.1225, "source": "search frame, seed fr31a_taker"}
        kw = dict(genome_id="0c4b20502f2daf65", family="weather/gfs_mex/taker/v1", mode="shadow", sandbox=sandbox,
                  prediction=pred, registry_status="CLOSED", n_min=50)
        kw.update(over)
        return PAPER.build_paper_row(**kw)

    def test_shadow_run_is_named_instrumentation_not_gate_evidence(self):
        row = self._paper()
        assert row["status"] == "shadow 0/50" and row["killed"] is None
        assert PAPER.SHADOW_NOTE in row["note"]
        board = REPORT.render_board({"run_id": "x", "family": "f"}, None, row)
        line = next(l for l in board.splitlines() if l.startswith("| PAPER"))
        assert "shadow 0/50" in line and "0/50 target_dates" in line
        assert "sandbox — (0 settled fills)" in line
        assert "pred +0.0723/c [+0.0145, +0.1225]" in line
        assert "shadow run: 0 units (instrumentation, not gate evidence)" in line

    def test_paper_row_shows_sandbox_beside_prediction(self):
        row = self._paper(mode="paper", registry_status="PROPOSED",
                          sandbox={"settled_trades": 7, "settled_target_dates": 5, "contracts": 70.0, "net_pnl": 3.5,
                                   "c_per_contract": 0.05, "fee_sources": ["closed_trades"]})
        assert row["status"] == "paper 5/50" and PAPER.SHADOW_NOTE not in row["note"]
        board = REPORT.render_board({"run_id": "x"}, None, row)
        line = next(l for l in board.splitlines() if l.startswith("| PAPER"))
        assert "sandbox +0.0500/c (7 fills)" in line and "5/50 target_dates" in line and "| 7 |" in line
        assert "pred +0.0723/c" in line

    def test_halt_shows_killed(self):
        row = self._paper(registry_status="HALT")
        assert row["status"] == "KILLED:HALT" and row["killed"] == "HALT"
        board = REPORT.render_board({"run_id": "x"}, None, row)
        assert "| PAPER | KILLED:HALT |" in board
        assert "live-capital" in row["note"]

    def test_failed_gate_shows_killed_but_a_refused_gate_does_not(self):
        assert PAPER.killed_reason("PROPOSED", {"verdict": "FAIL", "refused": False}) == "GATE_FAIL"
        assert PAPER.killed_reason("PROPOSED", {"verdict": "FAIL", "refused": True}) is None
        assert PAPER.killed_reason("PROPOSED", {"verdict": "PASS", "refused": False}) is None
        row = self._paper(registry_status="PROPOSED", gate_verdict={"verdict": "FAIL", "refused": False})
        assert row["status"] == "KILLED:GATE_FAIL"

    def test_absent_paper_keeps_the_f3_placeholder_row(self):
        board = REPORT.render_board({"run_id": "x"}, None, None)
        assert "| PAPER | n/a (F3) |" in board

    def test_n_min_comes_from_the_registration_else_the_template(self, tmp_path):
        assert PAPER.n_min_from_registration(None, REPO_ROOT / "configs" / "factory" / "gate_registration.template.json") == 50
        reg = tmp_path / "r.json"
        reg.write_text(json.dumps({"thresholds": {"n_min": 60}}), encoding="utf-8")
        assert PAPER.n_min_from_registration(reg, None) == 60
        assert PAPER.n_min_from_registration(tmp_path / "absent.json", None) == PAPER.DEFAULT_N_MIN


class TestPaperFileInputsAndCLI:
    def test_board_from_files_renders_the_row(self, tmp_path):
        name = "Genome 0c4b2050"
        journal = tmp_path / "trade_journal.jsonl"
        rows = [_row("KXHIGHNY-26SEP10-B80.5", name, 0.80, 10, 2.0, "2026-09-10"),
                _row("KXHIGHNY-26SEP11-B80.5", name, 0.90, 10, -9.0, "2026-09-11", entry_time="2026-09-11T15:00:05")]
        journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        state = tmp_path / "exchange_state.json"
        state.write_text(json.dumps({"closed_trades": [
            {"symbol": "KXHIGHNY-26SEP10-B80.5", "strategy_name": name, "open_time": "2026-09-10T15:00:05", "entry_fee": 0.11}],
            "positions": []}), encoding="utf-8")
        out = tmp_path / "row.json"
        r = _factory("board", "--paper-state", str(state), "--paper-journal", str(journal),
                     "--genome", "0c4b20502f2daf65", "--mode", "paper", "--paper-json", str(out))
        assert r.returncode == 0, r.stderr
        line = next(l for l in r.stdout.splitlines() if l.startswith("| PAPER"))
        assert "paper 2/50" in line and "(2 fills)" in line and "pred +0.0723/c" in line
        row = json.loads(out.read_text(encoding="utf-8"))
        assert row["settled_target_dates"] == 2 and len(row["fills"]) == 2
        assert row["fills"][0]["fee_source"] == "closed_trades" and row["fills"][1]["fee_source"] == "recomputed_taker"

    def test_board_refuses_both_sources(self, tmp_path):
        r = _factory("board", "--paper-url", "http://127.0.0.1:9", "--paper-journal", str(tmp_path / "j.jsonl"))
        assert r.returncode == 1 and "not both" in r.stderr

    def test_http_loader_reports_what_it_could_not_obtain(self, monkeypatch):
        def fake_get(url, timeout):
            if url.endswith("/api/genome"):
                return {"ok": True, "genome": {"genome_id": "0c4b20502f2daf65", "strategy": "Genome 0c4b2050",
                                               "family": "weather/gfs_mex/taker/v1"}, "modes": {"genome": "shadow"}}
            if "/api/journal" in url:
                return {"ok": True, "count": 1, "trades": [_row("KXHIGHNY-26SEP10-B80.5", "Meteorologist V2", 0.5, 50, 25.0, "2026-09-10")]}
            return None  # /api/closed_trades absent (404)
        monkeypatch.setattr(PAPER, "_get_json", fake_get)
        got = PAPER.load_sandbox_http("http://x")
        assert got["genome_mode"] == "shadow" and got["genome"]["genome_id"] == "0c4b20502f2daf65"
        assert got["closed_trades"] == [] and any("closed_trades" in n for n in got["not_obtained"])
        row = PAPER.paper_row_from_inputs(got, genome_id="0c4b20502f2daf65", family="f", mode="shadow", registry_status="CLOSED")
        assert row["status"] == "shadow 0/50" and "not obtained: closed_trades" in row["note"]


# ===========================================================================
# 3. SETTLEMENT LATENCY
# ===========================================================================
@pytest.fixture(scope="module")
def mod():
    return _load_script("check_settlement_latency")


@pytest.fixture(scope="module")
def tg():
    """tests/test_gate.py's synthetic-record builders, loaded by path (tests/ is not a package)."""
    spec = importlib.util.spec_from_file_location("mp_test_gate_helpers", REPO_ROOT / "tests" / "test_gate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestSettlementLatency:
    def test_close_rule_is_the_runtime_one(self, mod):
        from src.core.weather_settlement import settlement_close_for

        want = settlement_close_for("KXHIGHNY-26SEP05-T79").astimezone(timezone.utc)
        assert mod.market_close_utc("KXHIGHNY-26SEP05-T79", "2026-09-05") == want
        assert want.isoformat().startswith("2026-09-06T04:00")
        assert mod.target_date_of({"symbol": "KXHIGHNY-26SEP03-T83"}) == "2026-09-03"  # ticker label fallback

    def test_measure_pass_fail_anomaly_overdue_and_genome_line(self, mod):
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        rows = [
            {"symbol": "KXHIGHNY-26SEP05-T79", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
             "settlement_outcome": "no", "exit_time": "2026-09-06T04:00:01", "target_date": "2026-09-05"},
            {"symbol": "KXHIGHCHI-26SEP05-B83.5", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
             "settlement_outcome": "yes", "exit_time": "2026-09-09T06:00:00", "target_date": "2026-09-05"},  # > 3 d
            {"symbol": "KXHIGHLAX-26SEP04-B77.5", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
             "settlement_outcome": "yes", "exit_time": "2026-09-05T06:00:00", "target_date": "2026-09-04"},  # before close
            {"symbol": "KXHIGHNY-26SEP03-T83", "strategy_name": "ML Weather", "close_reason": "EXPIRATION",
             "settlement_outcome": "no", "exit_time": "2026-09-04T22:00:00"},  # no target_date -> label
            {"symbol": "KXHIGHNY-26SEP06-B75.5", "strategy_name": "Meteorologist V2", "close_reason": "STOP_LOSS",
             "settlement_outcome": None, "exit_time": "2026-09-06T20:00:00", "target_date": "2026-09-06"},  # not settled
            {"symbol": "KXBTCY-26DEC31-T100000", "strategy_name": "x", "close_reason": "EXPIRATION", "settlement_outcome": "yes",
             "exit_time": "2026-09-06T20:00:00"},  # not weather
        ]
        positions = [
            {"symbol": "KXHIGHMIA-26SEP06-B88.5", "strategy": "Meteorologist V2", "expiration_time": "2026-09-07T00:00:00-04:00"},  # 5.3 d overdue
            {"symbol": "KXHIGHLAX-26SEP11-T83", "strategy": "Meteorologist V2", "expiration_time": "2026-09-12T00:00:00-07:00"},  # fresh
        ]
        rep = mod.measure(rows, positions, max_days=3, now=now, genome_strategy="Genome 0c4b2050")
        assert rep["verdict"] == "FAIL" and rep["measured"] == 4 and rep["failing"] == 2
        by = {r["symbol"]: r for r in rep["positions"]}
        assert by["KXHIGHNY-26SEP05-T79"]["within_bound"] and by["KXHIGHNY-26SEP05-T79"]["gap_days"] == pytest.approx(0.0, abs=1e-4)
        assert not by["KXHIGHCHI-26SEP05-B83.5"]["within_bound"] and by["KXHIGHCHI-26SEP05-B83.5"]["gap_days"] > 3
        assert by["KXHIGHLAX-26SEP04-B77.5"]["anomaly"] == "settled before market close"
        assert by["KXHIGHNY-26SEP03-T83"]["target_date"] == "2026-09-03"
        assert [o["symbol"] for o in rep["overdue_open"]] == ["KXHIGHMIA-26SEP06-B88.5"]
        ps = rep["per_strategy"]
        assert ps["Genome 0c4b2050"]["verdict"].startswith("NO EVIDENCE") and ps["Genome 0c4b2050"]["is_genome"]
        assert ps["Meteorologist V2"]["verdict"] == "FAIL" and ps["ML Weather"]["verdict"] == "PASS"
        text = mod.render_text(rep, source={"base_url": "http://x"}, notes=[])
        assert "NO EVIDENCE (0 settled; shadow books nothing)" in text and "OVERDUE open KXHIGHMIA" in text

    def test_no_data_verdict_and_exit_codes(self, mod, tmp_path):
        rep = mod.measure([], [], max_days=3, genome_strategy="Genome 0c4b2050")
        assert rep["verdict"] == "NO DATA"
        j = tmp_path / "j.jsonl"
        j.write_text("", encoding="utf-8")
        assert mod.main(["--journal", str(j)]) == mod.EXIT_NO_DATA
        assert mod.main([]) == mod.EXIT_USAGE
        row = {"symbol": "KXHIGHNY-26SEP05-T79", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
               "settlement_outcome": "no", "exit_time": "2026-09-06T04:00:01", "target_date": "2026-09-05"}
        j.write_text(json.dumps(row) + "\n", encoding="utf-8")
        out = tmp_path / "rep.json"
        assert mod.main(["--journal", str(j), "--out", str(out), "--genome", "0c4b20502f2daf65"]) == mod.EXIT_PASS
        rep = json.loads(out.read_text(encoding="utf-8"))
        assert rep["verdict"] == "PASS" and "Genome 0c4b2050" in rep["per_strategy"]


# ===========================================================================
# 4. GATE VERDICT FILE
# ===========================================================================
class TestGateVerdictFile:
    def test_default_out_is_reports_factory_gate_genome_id(self, tg, tmp_path, monkeypatch):
        gate = tg.gate
        layout = tg._layout(both_win=8, split=1, both_lose=1, single_wins=23)
        journal, state, registration = tg._write_record(tmp_path, layout)
        reg = json.loads(Path(registration).read_text(encoding="utf-8"))
        assert gate.default_out_path(str(registration), reports_root=str(tmp_path / "rf")) == \
            str(tmp_path / "rf" / f"gate_{reg['genome_id']}.json")
        assert gate.default_out_path(str(tmp_path / "absent.json"), reports_root=str(tmp_path)).endswith("gate_unregistered.json")
        monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
        rc = gate.main(["--journal", str(journal), "--state", str(state), "--registration", str(registration),
                        "--realistic-fills", "true", "--allow-unverified-registration", "--fill-config", "", "--quiet"])
        assert rc == gate.EXIT_PASS
        out = tmp_path / "reports" / "factory" / f"gate_{reg['genome_id']}.json"
        assert out.exists(), "gate_<id>.json was not written to the default path"
        v = json.loads(out.read_text(encoding="utf-8"))
        # the four numbers F4 exit criterion 5 names, at the top level, equal to the sections they alias
        assert v["grouped_count"] == v["units"]["n"] == v["conditions"]["n_units_ge_n_min"]["observed"] >= 50
        assert v["p_exact"] == v["units"]["p_upper_tail"] == v["conditions"]["p_lt_alpha"]["observed"]
        assert v["p_exact_str"] == v["units"]["p_exact_str"]
        assert v["net_pnl"] == v["pnl"]["net"] == v["conditions"]["net_pnl_gt_0"]["observed"]
        assert v["spec_hash_unchanged"] is True and v["spec_hash_unchanged"] == v["conditions"]["spec_hash_unchanged"]["ok"]
        assert v["verdict"] == "PASS"

    def test_explicit_out_still_wins(self, tg, tmp_path, monkeypatch):
        gate = tg.gate
        layout = tg._layout(both_win=8, split=1, both_lose=1, single_wins=23)
        journal, state, registration = tg._write_record(tmp_path, layout)
        monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
        explicit = tmp_path / "v.json"
        rc = gate.main(["--journal", str(journal), "--state", str(state), "--registration", str(registration),
                        "--realistic-fills", "true", "--allow-unverified-registration", "--fill-config", "",
                        "--out", str(explicit), "--quiet"])
        assert rc == gate.EXIT_PASS and explicit.exists()
        assert not (tmp_path / "reports" / "factory").exists()

    def test_underpowered_still_writes_the_file_with_refusal(self, tg, tmp_path, monkeypatch):
        gate = tg.gate
        layout = tg._layout(both_win=8, split=1, both_lose=1, single_wins=23)  # the layout always yields 50 units
        journal, state, registration = tg._write_record(tmp_path, layout)
        # raise the registration's n_min above the record, so the gate is underpowered and must refuse
        reg_doc = json.loads(Path(registration).read_text(encoding="utf-8"))
        reg_doc["thresholds"]["n_min"] = 60
        Path(registration).write_text(json.dumps(reg_doc), encoding="utf-8")
        monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))
        rc = gate.main(["--journal", str(journal), "--state", str(state), "--registration", str(registration),
                        "--realistic-fills", "true", "--allow-unverified-registration", "--fill-config", "", "--quiet"])
        assert rc == gate.EXIT_REFUSED
        reg = json.loads(Path(registration).read_text(encoding="utf-8"))
        v = json.loads((tmp_path / "reports" / "factory" / f"gate_{reg['genome_id']}.json").read_text(encoding="utf-8"))
        assert v["refused"] is True and v["grouped_count"] == 50 and v["p_exact"] is None
        assert any("underpowered" in r for r in v["refusals"])

    def test_factory_gate_wrapper_forwards_to_gate_py(self, tg, tmp_path):
        layout = tg._layout(both_win=8, split=1, both_lose=1, single_wins=23)
        journal, state, registration = tg._write_record(tmp_path, layout)
        out = tmp_path / "wrapped.json"
        r = _factory("gate", "--journal", str(journal), "--state", str(state), "--registration", str(registration),
                     "--realistic-fills", "true", "--allow-unverified-registration", "--fill-config", "",
                     "--out", str(out), "--quiet")
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("PASS") and out.exists()


# ===========================================================================
# 5. WEB ROUTE /api/closed_trades
# ===========================================================================
class TestClosedTradesRoute:
    @pytest.fixture
    def client(self):
        from collections import deque
        from unittest.mock import MagicMock

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from src.web.server import create_app
        from src.web.state_manager import StateManager

        o = MagicMock()
        o.bots = []
        o.active_bots = set()
        o.risk_manager = MagicMock()
        o.risk_manager.balance = 100.0
        o.risk_manager.daily_pnl = 0.0
        o.risk_manager.unrealized_pnl = 0.0
        o.risk_manager.get_current_exposure.return_value = 0.0
        o.risk_manager.exchange.positions = [
            {"symbol": "KXHIGHNY-26SEP07-B75.5", "strategy_name": "Meteorologist V2", "open_time": datetime(2026, 9, 7, 15, 0),
             "expiration_time": datetime(2026, 9, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))},
        ]
        o.risk_manager.exchange.closed_trades = [
            {"symbol": f"KXHIGHNY-26SEP0{i}-B80.5", "strategy_name": "Meteorologist V2" if i % 2 else "Genome 0c4b2050",
             "open_time": datetime(2026, 9, i, 15, 0), "close_time": datetime(2026, 9, i + 1, 4, 0),
             "entry_fee": 0.11, "pnl": 1.0} for i in range(1, 7)
        ]
        o.risk_manager.exchange.get_stats.return_value = {"cumulative_net": 0.0}
        o.dashboard = MagicMock()
        o.dashboard.start_time = datetime.now()
        o.dashboard.latest_prices = {}
        o.dashboard.alerts = deque()
        o.dashboard.logs = deque()
        o.dashboard.strategy_stats = {}
        o.dashboard.mascot = MagicMock()
        o.dashboard.mascot.state = "IDLE"
        o.dashboard.log_dir = None
        o.uptime_seconds = 1.0
        o.cycle_history = []
        o._training_diagnostics = {}
        o._training_history = []
        sm = StateManager(o)
        return TestClient(create_app(sm, o)), sm, o

    def test_serves_ledger_and_positions_json_safe(self, client):
        c, sm, o = client
        resp = c.get("/api/closed_trades")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True and body["count"] == 6 and body["total_closed_trades"] == 6 and body["capped"] is False
        assert body["closed_trades"][0]["entry_fee"] == 0.11
        assert isinstance(body["closed_trades"][0]["open_time"], str)  # datetime serialised
        assert body["positions"][0]["symbol"] == "KXHIGHNY-26SEP07-B75.5"
        assert "market_data" not in body

    def test_strategy_filter_and_cap(self, client):
        c, _, _ = client
        body = c.get("/api/closed_trades?strategy=Genome%200c4b2050").json()
        assert body["count"] == 3 and all(t["strategy_name"] == "Genome 0c4b2050" for t in body["closed_trades"])
        body = c.get("/api/closed_trades?last_n=2").json()
        assert body["count"] == 2 and body["capped"] is True and body["total_closed_trades"] == 6

    def test_side_effect_free_and_token_free(self, client, monkeypatch):
        c, sm, o = client
        sm.snapshot()
        before = len(sm._pnl_history)
        o.risk_manager.exchange.get_stats.reset_mock()
        monkeypatch.setenv("MP_CONTROL_TOKEN", "secret")
        for _ in range(3):
            assert c.get("/api/closed_trades").status_code == 200  # GET never needs the token
        assert len(sm._pnl_history) == before
        o.risk_manager.exchange.get_stats.assert_not_called()

    def test_404_without_an_exchange(self):
        from collections import deque
        from unittest.mock import MagicMock

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from src.web.server import create_app

        o = MagicMock()
        o.risk_manager = None
        o.dashboard = MagicMock()
        o.dashboard.alerts = deque()
        o.dashboard.logs = deque()
        sm = MagicMock()
        c = TestClient(create_app(sm, o))
        assert c.get("/api/closed_trades").status_code == 404


# ===========================================================================
# 6. WEEKLY RECONCILE CRON (alcyone: it needs the frozen frame, which only the lab has)
# ===========================================================================
class TestWeeklyReconcileCron:
    SVC = REPO_ROOT / "deploy" / "spark" / "systemd" / "mp-factory-reconcile.service"
    TMR = REPO_ROOT / "deploy" / "spark" / "systemd" / "mp-factory-reconcile.timer"
    WRAP = REPO_ROOT / "deploy" / "spark" / "factory_reconcile.sh"
    INSTALL = REPO_ROOT / "deploy" / "spark" / "install_factory_reconcile.sh"
    HERMES = REPO_ROOT / "hermes_plugin" / "scripts" / "mp_factory_reconcile.sh"

    def test_units_exist_and_are_weekly_monday_after_the_daily_reconcile(self):
        assert self.SVC.exists() and self.TMR.exists()
        tmr = self.TMR.read_text(encoding="utf-8")
        m = re.search(r"OnCalendar=Mon \*-\*-\* (\d\d):(\d\d):00 UTC", tmr)
        assert m, "timer must fire weekly on Monday at a fixed UTC time"
        hh, mm = int(m.group(1)), int(m.group(2))
        assert (hh, mm) > (13, 30), "must run AFTER maia's daily 13:30Z mp-reconcile-weather.timer (F3_RUNBOOK section 5)"
        assert "Persistent=true" in tmr
        svc = self.SVC.read_text(encoding="utf-8")
        assert "factory_reconcile.sh" in svc and "Type=oneshot" in svc
        assert "MONEY_PRINTER_URL=http://maia.local:8050" in svc  # it reads maia over HTTP
        assert "frozen" in svc.lower() and "frame" in svc.lower()  # the unit says WHY it is on alcyone

    def test_wrapper_runs_the_reconcile_over_http_with_the_frame(self):
        w = self.WRAP.read_text(encoding="utf-8")
        assert "scripts/factory_paper_reconcile.py" in w
        assert "--url" in w and "maia.local:8050" in w
        assert "--from" in w and "--to" in w and "--frames" in w
        assert "--promoted" in w and "configs/factory/promoted" in w
        assert "frame_search_sha256" in w  # resolves the frame the spec was scored on
        assert "docker compose" in w and "docker-compose.lab.yml" in w  # pandas/pyarrow live in the lab image
        for token in ("\r\n",):
            assert token not in w
        assert w.startswith("#!/usr/bin/env bash")

    def test_install_snippet_and_hermes_watch_exist(self):
        assert self.INSTALL.exists() and "mp-factory-reconcile.timer" in self.INSTALL.read_text(encoding="utf-8")
        h = self.HERMES.read_text(encoding="utf-8")
        assert h.startswith("#!/usr/bin/env bash") and "paper_reconcile_" in h
        assert "exit 0" in h  # silent when nothing is wrong (mp_watch.sh pattern)


# ===========================================================================
# 7. RUNBOOK + HANDOFF DRAFT
# ===========================================================================
class TestRunbookAndHandoffDraft:
    def test_factory_runbook_names_the_prd_commands_and_the_kill_path(self):
        doc = (REPO_ROOT / "docs" / "FACTORY.md").read_text(encoding="utf-8")
        for needle in (
            "factory.py holdout --finalists --unseal RATIFIED-",
            "factory.py score --genome",
            "data/ladders_2026-09",
            "register-gate",
            "--fill-commit-time",
            "promote",
            "deploy_f3_shadow.sh",
            "factory_paper_reconcile.py",
            "gate.py",
            "gate_<genome_id>.json",
            "KILLED",
            "append-only",
            "family cap",
            "CLOSED",
            "owner decision",
            "check_settlement_latency.py",
            "mp-factory-reconcile.timer",
        ):
            assert needle in doc, f"docs/FACTORY.md lacks {needle!r}"

    def test_handoff_draft_is_a_skeleton_with_placeholders(self):
        p = REPO_ROOT / "reports" / "factory" / "HANDOFF_F4_draft.md"
        text = p.read_text(encoding="utf-8")
        assert "<" in text and "PASS" in text and "HALT" in text
        assert "grouped_count" in text or "target_date" in text
        assert "either way" in text.lower() or "verdict" in text.lower()
