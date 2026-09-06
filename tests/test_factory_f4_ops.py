"""F4 OPS (PRD_STRATEGY_FACTORY FR-F4.2; Phase F4 exit criteria 4-5).

Pins, one section each (red-team fixes of 2026-09-06 marked BROKEN-A/B, 3, 4, 5, 6):

1. GATE REGISTRATION FLOW -- ``src.factory.registration`` + ``factory.py register-gate``
   + ``promote --mode paper``: one ``gate_registration_<genome_id>.json`` per genome
   (BROKEN-B), no ``_doc`` block so gate.py's placeholder check passes (BROKEN-A), an
   unknown template schema refuses, a re-issue needs ``git rm`` + commit first, and
   ``promote --mode paper`` reconciles the stamp with git the way gate.py does. The
   ROUND TRIP register-gate -> commit -> --fill-commit-time -> gate.py writes a verdict.
2. PAPER BOARD ROW -- own header (4b), two labelled factory numbers and no "prediction"
   (4a), the family's CURRENT registry status in the board header (4c), gate.py's own
   admission + unit key so pre-``target_date`` rows count (4d).
3. SETTLEMENT LATENCY -- gap against the sandbox's own close; the output says the rows
   settled in-process and that reconcile_weather.py is not evidenced over HTTP (5).
4. GATE VERDICT FILE -- default ``--out`` and the four top-level aliases.
5. WEB ROUTE -- ``GET /api/closed_trades`` is side-effect free, token-free, capped.
6. WEEKLY RECONCILE CRON -- host-side name resolution + ``--add-host``, loud exit codes,
   ``FRAME_MISSING`` with no fallback, same-day Hermes failure line (3).
7. RUNBOOK + HANDOFF DRAFT -- the real ``holdout`` / ``score`` invocations (6).
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

DEPLOYED = "0c4b20502f2daf65"  # the fr31a_taker spec on disk, deployed on maia in shadow


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


def _factory(*args, env_extra=None, cwd=None):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "factory.py"), *args],
        capture_output=True, text=True, cwd=str(cwd or REPO_ROOT), env=env, timeout=180,
    )


def _git_available() -> bool:
    return shutil.which("git") is not None


def _git(args, cwd, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, env=env)


def _commit(repo, message, when=None):
    env = {}
    if when:
        env = {"GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    r = _git(["commit", "-q", "-m", message], repo, env)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def git_repo(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    r = _git(["init", "-q"], tmp_path)
    assert r.returncode == 0, r.stderr
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "configs" / "factory").mkdir(parents=True)
    return tmp_path


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


# ===========================================================================
# 1. GATE REGISTRATION FLOW
# ===========================================================================
class TestRegistrationBuild:
    def test_build_fills_every_field_and_names_the_paper_hash(self):
        spec = _spec()
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        assert doc["genome_id"] == spec.genome_id
        assert doc["strategy_name"] == f"Genome {spec.id8}"
        assert doc["promoted_spec_path"] == f"configs/factory/promoted/{spec.genome_id}.json"
        assert doc["adverse_fill"] == spec.adverse_fill and doc["fee_type"] == "taker"
        assert doc["registration_commit_utc"] is None
        assert doc["schema_version"] == REG.SCHEMA_VERSION
        paper = _spec(mode="paper", status="PROPOSED")
        assert doc["spec_hash"] == paper.spec_hash != spec.spec_hash
        assert doc["spec_hash"] == REG.paper_spec_hash(spec.to_doc(), registry_status="PROPOSED")

    def test_no_placeholder_token_survives_anywhere_and_no_doc_block(self):
        """BROKEN-A: the template's _doc._about says 'fill every REPLACE_ME' verbatim, and gate.py
        refused any registration that preserved it. The generated file carries no _doc at all."""
        doc = REG.build_registration(_spec().to_doc(), registry_status="PROPOSED")
        assert "REPLACE_ME" not in json.dumps(doc)
        assert "_doc" not in doc and "_doc_ref" in doc and "gate_registration.template.json" in doc["_doc_ref"]
        assert list(doc)[0] == "_doc_ref"
        assert REG.registration_relpath(doc["genome_id"]) in doc["registered_before_first_trade"]

    def test_unknown_template_schema_refuses(self):
        tpl = json.loads(REG.TEMPLATE_PATH.read_text(encoding="utf-8"))
        tpl["schema_version"] = 2
        with pytest.raises(REG.RegistrationError, match="schema_version"):
            REG.build_registration(_spec().to_doc(), registry_status="PROPOSED", template=tpl)

    def test_per_genome_path(self):
        assert REG.registration_path("abc").name == "gate_registration_abc.json"
        assert REG.registration_relpath("abc") == "configs/factory/gate_registration_abc.json"

    def test_write_is_lf_indent2_and_round_trips(self, tmp_path):
        doc = REG.build_registration(_spec().to_doc(), registry_status="PROPOSED")
        p = REG.write_registration(doc, REG.registration_path(doc["genome_id"], tmp_path))
        raw = p.read_bytes()
        assert b"\r\n" not in raw and raw.endswith(b"\n")
        assert REG.load_registration(p) == doc


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
        joined = "\n".join(REG.check_registration(doc, other.to_doc(), registry_status="PROPOSED"))
        assert "genome_id" in joined and "strategy_name" in joined and "spec_hash" in joined
        assert "promoted_spec_path" in joined
        problems = REG.check_registration(doc, spec.to_doc(), registry_status="RATIFIED")
        assert problems and all("spec_hash" in p for p in problems)
        assert any("adverse_fill" in p for p in REG.check_registration(
            dict(doc, adverse_fill=0.02), spec.to_doc(), registry_status="PROPOSED"))

    def test_expected_hash_override_is_what_promote_checks_after_build(self):
        spec = _spec()
        doc = REG.build_registration(spec.to_doc(), registry_status="PROPOSED")
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        paper = _spec(mode="paper", status="PROPOSED")
        assert REG.check_registration(doc, paper.to_doc(), registry_status="PROPOSED", expected_spec_hash=paper.spec_hash) == []
        assert REG.check_registration(doc, paper.to_doc(), registry_status="PROPOSED", expected_spec_hash="0" * 64)


def _write_reg(repo: Path, spec: P.PromotedSpec, status="PROPOSED") -> Path:
    doc = REG.build_registration(spec.to_doc(), registry_status=status)
    return REG.write_registration(doc, repo / "configs" / "factory" / REG.REGISTRATION_FMT.format(genome_id=spec.genome_id))


class TestFillCommitTimeAndReconcile:
    def test_refuses_untracked(self, git_repo):
        p = _write_reg(git_repo, _spec())
        with pytest.raises(REG.RegistrationError, match="not committed"):
            REG.fill_commit_time(p)
        assert REG.load_registration(p)["registration_commit_utc"] is None

    def test_refuses_uncommitted_edits(self, git_repo):
        p = _write_reg(git_repo, _spec())
        _git(["add", "."], git_repo)
        _commit(git_repo, "register")
        doc = REG.load_registration(p)
        doc["adverse_fill"] = 0.02
        REG.write_registration(doc, p)
        with pytest.raises(REG.RegistrationError, match="uncommitted"):
            REG.fill_commit_time(p)

    def test_fills_from_the_adding_commit_and_reconciles(self, git_repo):
        p = _write_reg(git_repo, _spec())
        _git(["add", "."], git_repo)
        _commit(git_repo, "register", when="2026-09-07T12:00:00+00:00")
        stamp = REG.fill_commit_time(p)
        want = _git(["log", "--diff-filter=A", "--format=%cI", "--", p.name], git_repo / "configs" / "factory").stdout.strip()
        assert stamp == want and stamp.startswith("2026-09-07T12:00:00")
        reg = REG.load_registration(p)
        assert reg["registration_commit_utc"] == stamp
        # the stamp edit is itself uncommitted: reconcile says so, and clears after the commit
        assert any("uncommitted" in x for x in REG.reconcile_commit_time(reg, p))
        _git(["add", "."], git_repo)
        _commit(git_repo, "stamp")
        assert REG.reconcile_commit_time(reg, p) == []
        # a typed value that git contradicts is a problem, not a licence
        assert any("does not reconcile" in x for x in REG.reconcile_commit_time(
            dict(reg, registration_commit_utc="2026-01-01T00:00:00+00:00"), p))

    def test_refuses_a_typed_value_that_contradicts_git(self, git_repo):
        p = _write_reg(git_repo, _spec())
        doc = REG.load_registration(p)
        doc["registration_commit_utc"] = "1970-01-01T00:00:00+00:00"
        REG.write_registration(doc, p)
        _git(["add", "."], git_repo)
        _commit(git_repo, "register")
        with pytest.raises(REG.RegistrationError, match="typed value"):
            REG.fill_commit_time(p)

    def test_reissue_for_a_second_genome_stamps_the_second_commit(self, git_repo):
        """BROKEN-B: a shared file re-issued for genome B inherited A's add-date. Per-genome files
        are each added once; B's stamp is B's commit, and git vouches for it."""
        a, b = _spec(seed="fr31a_taker"), _spec(seed="fr31b")
        pa = _write_reg(git_repo, a)
        _git(["add", "."], git_repo)
        _commit(git_repo, "register A", when="2026-09-07T12:00:00+00:00")
        pb = _write_reg(git_repo, b)
        _git(["add", "."], git_repo)
        _commit(git_repo, "register B", when="2026-09-09T15:30:00+00:00")
        sa, sb = REG.fill_commit_time(pa), REG.fill_commit_time(pb)
        assert sa.startswith("2026-09-07T12:00:00") and sb.startswith("2026-09-09T15:30:00") and sa != sb
        assert pa.name != pb.name

    def test_reissue_rule_refuses_an_existing_or_tracked_file(self, git_repo):
        p = _write_reg(git_repo, _spec())
        assert any("exists" in x and "git rm" in x for x in REG.reissue_problems(p))
        _git(["add", "."], git_repo)
        _commit(git_repo, "register")
        p.unlink()  # deleted in the working tree but still in HEAD -> still not a fresh add
        assert any("still tracked in HEAD" in x for x in REG.reissue_problems(p))
        _git(["rm", "-q", "--", str(p)], git_repo)
        _commit(git_repo, "withdraw")
        assert REG.reissue_problems(p) == []


class TestRegisterGateCLI:
    def test_is_a_real_subcommand_without_force(self):
        r = _factory("register-gate", "--help")
        assert r.returncode == 0 and "--fill-commit-time" in r.stdout and "--force" not in r.stdout

    def test_writes_per_genome_registration_and_prints_the_git_command(self, tmp_path):
        out = tmp_path / REG.REGISTRATION_FMT.format(genome_id=DEPLOYED)
        r = _factory("register-gate", DEPLOYED, "--registration", str(out), "--allow-closed")
        assert r.returncode == 0, r.stderr
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["genome_id"] == DEPLOYED and doc["strategy_name"] == "Genome 0c4b2050"
        assert doc["registration_commit_utc"] is None and "_doc" not in doc and "REPLACE_ME" not in out.read_text(encoding="utf-8")
        assert "git log --diff-filter=A --format=%cI --" in r.stdout
        assert f"register-gate --fill-commit-time {DEPLOYED}" in r.stdout
        spec = P.load_promoted(DEPLOYED)
        assert doc["spec_hash"] == REG.paper_spec_hash(spec.to_doc(), registry_status="CLOSED")

    def test_refuses_for_a_closed_family_without_allow_closed(self, tmp_path):
        out = tmp_path / "r.json"
        r = _factory("register-gate", DEPLOYED, "--registration", str(out))
        assert r.returncode == 1 and "CLOSED" in r.stderr and not out.exists()

    def test_refuses_to_overwrite_and_names_the_reissue_path(self, tmp_path):
        out = tmp_path / "r.json"
        out.write_text("{}", encoding="utf-8")
        r = _factory("register-gate", DEPLOYED, "--registration", str(out), "--allow-closed")
        assert r.returncode == 1 and "exists" in r.stderr and "git rm" in r.stderr

    def test_fill_commit_time_needs_the_id_and_refuses_an_uncommitted_file(self, tmp_path):
        r = _factory("register-gate", "--fill-commit-time")
        assert r.returncode == 1 and "genome id" in r.stderr
        out = tmp_path / "r.json"
        REG.write_registration(REG.build_registration(_spec().to_doc(), registry_status="PROPOSED"), out)
        r = _factory("register-gate", "--fill-commit-time", DEPLOYED, "--registration", str(out))
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


class TestRegistrationRoundTripThroughGate:
    """register-gate -> commit -> --fill-commit-time -> gate.py writes gate_<id>.json (BROKEN-A round trip)."""

    def test_round_trip(self, git_repo, tg, tmp_path, monkeypatch):
        gate = tg.gate
        reg_path = git_repo / "configs" / "factory" / REG.REGISTRATION_FMT.format(genome_id=DEPLOYED)
        r = _factory("register-gate", DEPLOYED, "--registration", str(reg_path), "--allow-closed")
        assert r.returncode == 0, r.stderr
        _git(["add", "."], git_repo)
        _commit(git_repo, "gate: register", when="2026-09-07T12:00:00+00:00")
        r = _factory("register-gate", "--fill-commit-time", DEPLOYED, "--registration", str(reg_path))
        assert r.returncode == 0, r.stderr
        _git(["add", "."], git_repo)
        _commit(git_repo, "gate: stamp")
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        assert reg["registration_commit_utc"].startswith("2026-09-07T12:00:00")
        # the synthetic maia-shaped record from tests/test_gate.py, under the deployed id's strategy name
        layout = tg._layout(both_win=8, split=1, both_lose=1, single_wins=23)
        journal, state, _own_reg = tg._write_record(tmp_path, layout)
        monkeypatch.setattr(gate, "REPO_ROOT", str(tmp_path))  # default verdict path -> tmp
        monkeypatch.chdir(REPO_ROOT)  # promoted_spec_path is repo-relative
        rc = gate.main(["--journal", str(journal), "--state", str(state), "--registration", str(reg_path),
                        "--realistic-fills", "true", "--fill-config", "", "--quiet"])
        out = tmp_path / "reports" / "factory" / f"gate_{DEPLOYED}.json"
        assert out.exists(), "the documented path produced no gate_<id>.json"
        v = json.loads(out.read_text(encoding="utf-8"))
        assert v["refused"] is False, v["refusals"]
        assert not any("REPLACE_ME" in r for r in v["refusals"])
        assert rc in (gate.EXIT_PASS, gate.EXIT_FAIL) and v["verdict"] in ("PASS", "FAIL")
        rb = v["conditions"]["registered_before_first_trade"]
        assert rb["verified"] is True and rb["git_added_commit_utc"] == reg["registration_commit_utc"]
        # the shadow spec on disk is not the paper spec the registration names -> honest FAIL there
        assert v["spec_hash_unchanged"] is False and v["grouped_count"] == 50

    def test_second_genome_registration_is_its_own_add_commit_for_the_gate(self, git_repo, tg, tmp_path, monkeypatch):
        gate = tg.gate
        pa = _write_reg(git_repo, _spec(seed="fr31a_taker"))
        _git(["add", "."], git_repo)
        _commit(git_repo, "register A", when="2026-05-01T00:00:00+00:00")
        pb = _write_reg(git_repo, _spec(seed="fr31b"))
        _git(["add", "."], git_repo)
        _commit(git_repo, "register B", when="2026-05-02T00:00:00+00:00")
        REG.fill_commit_time(pa)
        REG.fill_commit_time(pb)
        _git(["add", "."], git_repo)
        _commit(git_repo, "stamps")
        regb = json.loads(pb.read_text(encoding="utf-8"))
        got, _ = gate.git_added_commit_utc(str(pb))
        assert got == regb["registration_commit_utc"] and got.startswith("2026-05-02")
        gota, _ = gate.git_added_commit_utc(str(pa))
        assert gota.startswith("2026-05-01") and gota != got


class TestPromotePaperRequiresRegistration:
    """``promote --mode paper`` refuses before any parity work unless the registration licenses it."""

    @pytest.fixture
    def factory_mod(self):
        return _load_script("factory")

    def _args(self, registration_path, **over):
        base = dict(
            id=DEPLOYED, from_seed="fr31a_taker", from_pick=None, mode="paper", frames=None,
            ladders=None, run_id=None, config=None, out_dir=None, min_sizable_fraction=None,
            registration=str(registration_path) if registration_path else None,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_closed_family_refusal_still_comes_first(self, factory_mod, tmp_path, capsys):
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(tmp_path / "absent.json"))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "paper refused" in err and "CLOSED" in err and "gate_registration" not in err

    def test_missing_registration_refuses_when_family_is_proposed(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(tmp_path / "absent.json"))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "--mode paper refused" in err and "does not exist" in err and "register-gate" in err

    def test_default_registration_path_is_per_genome(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        with pytest.raises(SystemExit):
            factory_mod.cmd_promote(self._args(None))
        assert f"gate_registration_{DEPLOYED}.json" in capsys.readouterr().err

    def test_unstamped_registration_refuses(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        p = REG.write_registration(REG.build_registration(P.load_promoted(DEPLOYED).to_doc(), registry_status="PROPOSED"),
                                   tmp_path / "r.json")
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(p))
        assert e.value.code == 1 and "registration_commit_utc is null" in capsys.readouterr().err

    def test_typed_stamp_git_cannot_vouch_for_refuses(self, factory_mod, tmp_path, capsys, monkeypatch):
        """Non-null is not enough: the stamp must reconcile with git like gate.py (BROKEN-B)."""
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        doc = REG.build_registration(P.load_promoted(DEPLOYED).to_doc(), registry_status="PROPOSED")
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        p = REG.write_registration(doc, tmp_path / "r.json")  # untracked: git cannot vouch
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(p))
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "cannot be verified" in err or "does not reconcile" in err

    def test_wrong_genome_registration_refuses(self, factory_mod, tmp_path, capsys, monkeypatch):
        from src.factory import registry as R

        monkeypatch.setattr(R.Registry, "status", lambda self, family: "PROPOSED")
        doc = REG.build_registration(_spec(seed="fr31b").to_doc(), registry_status="PROPOSED")
        doc["registration_commit_utc"] = "2026-09-07T12:00:00+00:00"
        p = REG.write_registration(doc, tmp_path / "r.json")
        with pytest.raises(SystemExit) as e:
            factory_mod.cmd_promote(self._args(p))
        assert e.value.code == 1 and "genome_id" in capsys.readouterr().err

    def test_shadow_promotion_never_consults_the_registration(self, factory_mod, tmp_path, capsys, monkeypatch):
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
    row = {
        "symbol": symbol, "strategy_name": strategy, "entry_time": entry_time, "exit_time": "2026-09-11T04:00:01",
        "entry_price": entry, "exit_price": (1.0 if pnl > 0 else 0.0) if settled else 0.5, "quantity": qty,
        "side": "buy", "contract_side": "NO", "pnl": pnl,
        "close_reason": "EXPIRATION" if settled else "STOP_LOSS",
        "settlement_outcome": ("no" if pnl > 0 else "yes") if settled else None,
        "settlement_high": 80.0,
    }
    if target_date is not None:
        row["target_date"] = target_date
    return row


class TestPaperSettledUnits:
    NAME = "Genome 0c4b2050"

    def test_admission_and_unit_key_are_gate_py_s_own(self):
        gate = PAPER.gate_module()
        row = _row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.8, 10, 1.0, "2026-09-10")
        assert PAPER.is_settled(row) is bool(gate._is_settled(row)[0]) is True
        assert not PAPER.is_settled(_row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.8, 10, 1.0, "2026-09-10", settled=False))
        # 4d: a journal row that predates the target_date field takes the ticker's label, like the gate
        legacy = _row("KXHIGHNY-26SEP03-T83", self.NAME, 0.8, 10, 1.0, None)
        assert PAPER.unit_key(legacy) == gate._target_date(legacy) == "2026-09-03"

    def test_units_are_target_dates_and_fees_come_from_the_ledger(self):
        j = [
            _row("KXHIGHNY-26SEP10-B80.5", self.NAME, 0.80, 10, 2.0, "2026-09-10"),
            _row("KXHIGHCHI-26SEP10-B75.5", self.NAME, 0.70, 10, 3.0, "2026-09-10", entry_time="2026-09-10T15:00:07"),
            _row("KXHIGHNY-26SEP11-B80.5", self.NAME, 0.90, 10, -9.0, "2026-09-11", entry_time="2026-09-11T15:00:05"),
            _row("KXHIGHNY-26SEP03-T83", self.NAME, 0.60, 10, 1.0, None, entry_time="2026-09-03T15:00:05"),  # no target_date
            _row("KXHIGHNY-26SEP11-T85", "Meteorologist V2", 0.5, 50, 25.0, "2026-09-11"),  # other strategy
            _row("KXHIGHNY-26SEP12-B80.5", self.NAME, 0.8, 10, 2.0, "2026-09-12", settled=False),  # not settled
        ]
        ledger = [
            {"symbol": "KXHIGHNY-26SEP10-B80.5", "strategy_name": self.NAME, "open_time": "2026-09-10T15:00:05", "entry_fee": 0.11},
            {"symbol": "KXHIGHCHI-26SEP10-B75.5", "strategy_name": self.NAME, "open_time": "2026-09-10T15:00:07", "entry_fee": 0.15},
        ]
        fills = PAPER.settled_fills(j, ledger, strategy_name=self.NAME)
        assert [f["symbol"] for f in fills] == ["KXHIGHNY-26SEP10-B80.5", "KXHIGHCHI-26SEP10-B75.5",
                                                "KXHIGHNY-26SEP11-B80.5", "KXHIGHNY-26SEP03-T83"]
        assert [f["fee_source"] for f in fills] == ["closed_trades", "closed_trades", "recomputed_taker", "recomputed_taker"]
        assert fills[3]["target_date"] == "2026-09-03"  # from the ticker label
        s = PAPER.sandbox_summary(fills)
        assert s["settled_trades"] == 4 and s["settled_target_dates"] == 3
        assert s["target_dates"] == ["2026-09-03", "2026-09-10", "2026-09-11"]

    def test_empty_record_is_zero_units_not_an_error(self):
        s = PAPER.sandbox_summary(PAPER.settled_fills([], [], strategy_name=self.NAME))
        assert s["settled_trades"] == 0 and s["settled_target_dates"] == 0 and s["c_per_contract"] is None


class TestPaperFactoryNumbers:
    FAM = {"run_id": "run_x", "picks": {"ALL69": {"genome_id": "abc", "in_sample": {"realized": 0.11, "dates": 60, "trades": 90}}},
           "pooled_oos": {"mean": 0.0308, "boot_lo": -0.09, "boot_hi": 0.1417, "n_dates": 29, "trades": 49}}

    def test_family_pooled_oos_is_the_family_s_not_the_genome_s(self):
        f = PAPER.family_pooled_oos(self.FAM)
        assert f["c_per_contract"] == 0.0308 and f["dates"] == 29 and "picks abc" in f["source"]
        assert PAPER.family_pooled_oos(None)["c_per_contract"] is None

    def test_genome_in_sample_for_seed_and_pick_is_labelled_in_sample(self):
        gid = P.genome_id_for(P.genome_json_for(G.SEEDS["fr31a_taker"]))
        gen0 = {"run_id": "gen0_x", "seeds": {"fr31a_taker": {"search_full": {"realized": 0.0723, "boot_lo": 0.0145, "boot_hi": 0.1225, "dates": 57, "trades": 130}}}}
        g = PAPER.genome_in_sample(gid, gen0_summary=gen0)
        assert g["c_per_contract"] == 0.0723 and "in-sample" in g["source"] and "fr31a_taker" in g["source"]
        pk = PAPER.genome_in_sample("abc", family_summary=self.FAM)
        assert pk["c_per_contract"] == 0.11 and "in-sample" in pk["source"]
        both = PAPER.factory_numbers_for(gid, family_summary=self.FAM, gen0_summary=gen0)
        assert set(both) == {"family_pooled_oos", "genome_in_sample"}
        assert "pred" not in json.dumps(both).lower()

    def test_unknown_genome_has_no_in_sample_number(self):
        assert PAPER.genome_in_sample("ffffffffffffffff", family_summary={"picks": {}}, gen0_summary={"seeds": {}})["c_per_contract"] is None


class TestPaperRowRendering:
    def _paper(self, **over):
        sandbox = {"settled_trades": 0, "settled_target_dates": 0, "contracts": 0.0, "net_pnl": 0.0, "c_per_contract": None, "fee_sources": []}
        numbers = {
            "family_pooled_oos": {"c_per_contract": 0.0308, "lo": -0.09, "hi": 0.1417, "dates": 29, "trades": 49, "source": "picks a, b of run_x"},
            "genome_in_sample": {"c_per_contract": 0.0723, "lo": 0.0145, "hi": 0.1225, "dates": 57, "trades": 130, "source": "in-sample: seed fr31a_taker search_full, gen0_x"},
        }
        kw = dict(genome_id=DEPLOYED, family="weather/gfs_mex/taker/v1", mode="shadow", sandbox=sandbox,
                  factory_numbers=numbers, registry_status="CLOSED", n_min=50)
        kw.update(over)
        return PAPER.build_paper_row(**kw)

    @staticmethod
    def _paper_lines(board: str):
        lines = board.splitlines()
        hdr = next(i for i, l in enumerate(lines) if l.startswith("| PAPER | status |"))
        return lines[hdr], lines[hdr + 2]

    def test_paper_block_has_its_own_header_and_both_labelled_numbers(self):
        board = REPORT.render_board({"run_id": "x", "family": "f"}, None, self._paper())
        header, row = self._paper_lines(board)
        assert "family pooled OOS c/contract" in header and "genome in-sample c/contract" in header
        assert "NOT a prediction" in header and "never equity" in header
        assert "+0.0308/c [-0.0900, +0.1417] n=29d/49t" in row and "+0.0723/c [+0.0145, +0.1225] n=57d/130t" in row
        assert "in-sample" in row and "pred " not in row.lower()
        assert "| — (0 settled fills) |" in row and "| 0/50 |" in row
        assert "shadow run: 0 units (instrumentation, not gate evidence)" in row
        # the family table's own header is NOT the PAPER row's header
        assert "| PAPER |" not in [l for l in board.splitlines() if l.startswith("| weather")][0]
        lane_header = next(l for l in board.splitlines() if l.startswith("| lane |"))
        assert "PAPER" not in lane_header

    def test_registry_status_in_header_is_the_latest_transition(self):
        summary = {"run_id": "x", "registry_line": {"status": "OPEN"}}
        assert "registry OPEN (summary registry_line at run time)" in REPORT.render_board(summary, None, None)
        assert "registry CLOSED (registry latest transition)" in REPORT.render_board(summary, None, None, registry_status="CLOSED")

    def test_paper_row_shows_sandbox_beside_the_numbers(self):
        row = self._paper(mode="paper", registry_status="PROPOSED",
                          sandbox={"settled_trades": 7, "settled_target_dates": 5, "contracts": 70.0, "net_pnl": 3.5,
                                   "c_per_contract": 0.05, "fee_sources": ["closed_trades"]})
        assert row["status"] == "paper 5/50" and PAPER.SHADOW_NOTE not in row["note"]
        _, line = self._paper_lines(REPORT.render_board({"run_id": "x"}, None, row))
        assert "| +0.0500/c (7 fills) |" in line and "| 5/50 |" in line and "| 7 |" in line

    def test_halt_shows_killed(self):
        row = self._paper(registry_status="HALT")
        assert row["status"] == "KILLED:HALT" and row["killed"] == "HALT"
        _, line = self._paper_lines(REPORT.render_board({"run_id": "x"}, None, row))
        assert line.startswith("| PAPER | KILLED:HALT |") and "live-capital" in row["note"]

    def test_failed_gate_shows_killed_but_a_refused_gate_does_not(self):
        assert PAPER.killed_reason("PROPOSED", {"verdict": "FAIL", "refused": False}) == "GATE_FAIL"
        assert PAPER.killed_reason("PROPOSED", {"verdict": "FAIL", "refused": True}) is None
        assert PAPER.killed_reason("PROPOSED", {"verdict": "PASS", "refused": False}) is None
        assert self._paper(registry_status="PROPOSED", gate_verdict={"verdict": "FAIL", "refused": False})["status"] == "KILLED:GATE_FAIL"

    def test_absent_paper_keeps_the_f3_placeholder_row(self):
        _, line = self._paper_lines(REPORT.render_board({"run_id": "x"}, None, None))
        assert line.startswith("| PAPER | n/a (F3) |")

    def test_n_min_comes_from_the_registration_else_the_template(self, tmp_path):
        assert PAPER.n_min_from_registration(None, REG.TEMPLATE_PATH) == 50
        reg = tmp_path / "r.json"
        reg.write_text(json.dumps({"thresholds": {"n_min": 60}}), encoding="utf-8")
        assert PAPER.n_min_from_registration(reg, None) == 60
        assert PAPER.n_min_from_registration(tmp_path / "absent.json", None) == PAPER.DEFAULT_N_MIN


class TestPaperFileInputsAndCLI:
    def test_board_from_files_renders_the_block_and_the_current_registry_status(self, tmp_path):
        name = "Genome 0c4b2050"
        journal = tmp_path / "trade_journal.jsonl"
        rows = [_row("KXHIGHNY-26SEP10-B80.5", name, 0.80, 10, 2.0, "2026-09-10"),
                _row("KXHIGHNY-26SEP11-B80.5", name, 0.90, 10, -9.0, "2026-09-11", entry_time="2026-09-11T15:00:05"),
                _row("KXHIGHNY-26SEP03-T83", name, 0.60, 10, 1.0, None, entry_time="2026-09-03T15:00:05")]
        journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        state = tmp_path / "exchange_state.json"
        state.write_text(json.dumps({"closed_trades": [
            {"symbol": "KXHIGHNY-26SEP10-B80.5", "strategy_name": name, "open_time": "2026-09-10T15:00:05", "entry_fee": 0.11}],
            "positions": []}), encoding="utf-8")
        out = tmp_path / "row.json"
        r = _factory("board", "--paper-state", str(state), "--paper-journal", str(journal),
                     "--genome", DEPLOYED, "--mode", "paper", "--paper-json", str(out))
        assert r.returncode == 0, r.stderr
        assert "registry CLOSED (registry latest transition)" in r.stdout  # 4c: family #1 is CLOSED, gen0 said OPEN
        header = next(l for l in r.stdout.splitlines() if l.startswith("| PAPER | status |"))
        line = r.stdout.splitlines()[r.stdout.splitlines().index(header) + 2]
        assert "paper 3/50" in line and "(3 fills)" in line and "in-sample" in line and "pred " not in line.lower()
        row = json.loads(out.read_text(encoding="utf-8"))
        assert row["settled_target_dates"] == 3 and len(row["fills"]) == 3
        assert row["family_pooled_oos"]["c_per_contract"] == pytest.approx(0.03083620689655171)
        assert row["genome_in_sample"]["c_per_contract"] == pytest.approx(0.07228684210526316)
        assert "prediction_c_per_contract" not in row

    def test_board_refuses_both_sources(self, tmp_path):
        r = _factory("board", "--paper-url", "http://127.0.0.1:9", "--paper-journal", str(tmp_path / "j.jsonl"))
        assert r.returncode == 1 and "not both" in r.stderr

    def test_http_loader_reports_what_it_could_not_obtain(self, monkeypatch):
        def fake_get(url, timeout):
            if url.endswith("/api/genome"):
                return {"ok": True, "genome": {"genome_id": DEPLOYED, "strategy": "Genome 0c4b2050",
                                               "family": "weather/gfs_mex/taker/v1"}, "modes": {"genome": "shadow"}}
            if "/api/journal" in url:
                return {"ok": True, "count": 1, "trades": [_row("KXHIGHNY-26SEP10-B80.5", "Meteorologist V2", 0.5, 50, 25.0, "2026-09-10")]}
            return None
        monkeypatch.setattr(PAPER, "_get_json", fake_get)
        got = PAPER.load_sandbox_http("http://x")
        assert got["genome_mode"] == "shadow" and got["genome"]["genome_id"] == DEPLOYED
        assert got["closed_trades"] == [] and any("closed_trades" in n for n in got["not_obtained"])
        row = PAPER.paper_row_from_inputs(got, genome_id=DEPLOYED, family="f", mode="shadow", registry_status="CLOSED")
        assert row["status"] == "shadow 0/50" and "not obtained: closed_trades" in row["note"]


# ===========================================================================
# 3. SETTLEMENT LATENCY
# ===========================================================================
class TestSettlementLatency:
    def test_reference_is_the_sandbox_s_own_close_not_kalshi_s(self, mod):
        from src.core.weather_settlement import settlement_close_for

        want = settlement_close_for("KXHIGHNY-26SEP05-T79").astimezone(timezone.utc)
        assert mod.market_close_utc("KXHIGHNY-26SEP05-T79", "2026-09-05") == want
        assert want.isoformat().startswith("2026-09-06T04:00")  # Kalshi's close_time would be 05:00Z
        assert mod.target_date_of({"symbol": "KXHIGHNY-26SEP03-T83"}) == "2026-09-03"
        doc = mod.__doc__
        assert "settlement_close_for" in doc and "close_time" in doc and "NOT a Kalshi instant" in doc
        assert "the instant the exchange stamps as expiration_time" not in doc

    def test_measure_pass_fail_anomaly_overdue_and_genome_line(self, mod):
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        rows = [
            {"symbol": "KXHIGHNY-26SEP05-T79", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
             "settlement_outcome": "no", "exit_time": "2026-09-06T04:00:01", "target_date": "2026-09-05"},
            {"symbol": "KXHIGHCHI-26SEP05-B83.5", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
             "settlement_outcome": "yes", "exit_time": "2026-09-09T06:00:00", "target_date": "2026-09-05"},
            {"symbol": "KXHIGHLAX-26SEP04-B77.5", "strategy_name": "Meteorologist V2", "close_reason": "EXPIRATION",
             "settlement_outcome": "yes", "exit_time": "2026-09-05T06:00:00", "target_date": "2026-09-04"},
            {"symbol": "KXHIGHNY-26SEP03-T83", "strategy_name": "ML Weather", "close_reason": "EXPIRATION",
             "settlement_outcome": "no", "exit_time": "2026-09-04T22:00:00"},
            {"symbol": "KXHIGHNY-26SEP06-B75.5", "strategy_name": "Meteorologist V2", "close_reason": "STOP_LOSS",
             "settlement_outcome": None, "exit_time": "2026-09-06T20:00:00", "target_date": "2026-09-06"},
            {"symbol": "KXBTCY-26DEC31-T100000", "strategy_name": "x", "close_reason": "EXPIRATION", "settlement_outcome": "yes",
             "exit_time": "2026-09-06T20:00:00"},
        ]
        positions = [
            {"symbol": "KXHIGHMIA-26SEP06-B88.5", "strategy": "Meteorologist V2", "expiration_time": "2026-09-07T00:00:00-04:00"},
            {"symbol": "KXHIGHLAX-26SEP11-T83", "strategy": "Meteorologist V2", "expiration_time": "2026-09-12T00:00:00-07:00"},
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
        assert "reconcile_weather.py not involved" in rep["settled_by"] and "not Kalshi close_time" in rep["reference_instant"]
        text = mod.render_text(rep, source={"base_url": "http://x"}, notes=[])
        assert "NO EVIDENCE (0 settled; shadow books nothing)" in text and "OVERDUE open KXHIGHMIA" in text
        assert "in-process EXPIRATION check for EVERY measured row" in text
        assert "reconcile_weather.py was NOT involved in any measured row" in text
        assert "cannot be evidenced over HTTP" in text and "reference instant: the SANDBOX's own settlement-day close" in text

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
        assert mod.main(["--journal", str(j), "--out", str(out), "--genome", DEPLOYED]) == mod.EXIT_PASS
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
        assert v["grouped_count"] == v["units"]["n"] == v["conditions"]["n_units_ge_n_min"]["observed"] >= 50
        assert v["p_exact"] == v["units"]["p_upper_tail"] == v["conditions"]["p_lt_alpha"]["observed"]
        assert v["p_exact_str"] == v["units"]["p_exact_str"]
        assert v["net_pnl"] == v["pnl"]["net"] == v["conditions"]["net_pnl_gt_0"]["observed"]
        assert v["spec_hash_unchanged"] is True and v["spec_hash_unchanged"] == v["conditions"]["spec_hash_unchanged"]["ok"]
        assert v["verdict"] == "PASS"

    def test_placeholder_check_ignores_doc_keys_but_not_data(self, tg, tmp_path):
        gate = tg.gate
        layout = tg._layout(both_win=8, split=1, both_lose=1, single_wins=23)
        _j, _s, registration = tg._write_record(tmp_path, layout)
        reg = json.loads(Path(registration).read_text(encoding="utf-8"))
        reg["_doc"] = {"_about": "fill every REPLACE_ME"}
        Path(registration).write_text(json.dumps(reg), encoding="utf-8")
        assert gate.load_registration(str(registration))["genome_id"] == reg["genome_id"]
        reg["strategy_name"] = "Genome REPLACE_ME"
        Path(registration).write_text(json.dumps(reg), encoding="utf-8")
        with pytest.raises(gate.GateError, match="REPLACE_ME"):
            gate.load_registration(str(registration))

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
        assert isinstance(body["closed_trades"][0]["open_time"], str)
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
            assert c.get("/api/closed_trades").status_code == 200
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
        c = TestClient(create_app(MagicMock(), o))
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
        assert (int(m.group(1)), int(m.group(2))) > (13, 30)
        assert "Persistent=true" in tmr
        svc = self.SVC.read_text(encoding="utf-8")
        assert "factory_reconcile.sh" in svc and "Type=oneshot" in svc
        assert "MONEY_PRINTER_URL=http://maia.local:8050" in svc
        assert "frozen" in svc.lower() and "frame" in svc.lower()

    def test_wrapper_resolves_the_host_on_the_host_and_refuses_loudly(self):
        """Item 3: maia.local does not resolve inside the lab container; the wrapper must resolve on the
        host, pass --add-host, exit 4 HOST_UNRESOLVED / 5 FRAME_MISSING, and never fall back to another frame."""
        w = self.WRAP.read_text(encoding="utf-8")
        assert w.startswith("#!/usr/bin/env bash") and "\r\n" not in w
        assert "scripts/factory_paper_reconcile.py" in w and "--url" in w and "maia.local:8050" in w
        assert "--from" in w and "--to" in w and "--frames" in w and "--promoted" in w
        assert "getent hosts" in w and "avahi-resolve" in w and '--add-host "$HOSTNAME_PART:$SANDBOX_IP"' in w
        assert "EXIT_HOST_UNRESOLVED=4" in w and "HOST_UNRESOLVED" in w
        assert "EXIT_FRAME_MISSING=5" in w and "FRAME_MISSING $SHA12" in w
        assert "frame_search_sha256" in w
        assert "newest=" not in w and "fall back" not in w.lower().replace("no fallback", "")  # no frame fallback path
        assert "docker compose" in w and "docker-compose.lab.yml" in w
        assert "last_run.json" in w and "write_status" in w and "trap" in w  # status on every exit

    def test_wrapper_dry_run_exits_frame_missing_without_docker(self, tmp_path):
        """Runs the wrapper for real (bash) against a throwaway checkout with a spec but no frame."""
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not available")
        repo = tmp_path / "repo"
        (repo / "configs" / "factory" / "promoted").mkdir(parents=True)
        (repo / "deploy" / "spark").mkdir(parents=True)
        (repo / "data" / "factory" / "frames").mkdir(parents=True)
        shutil.copy(self.WRAP, repo / "deploy" / "spark" / "factory_reconcile.sh")
        shutil.copy(REPO_ROOT / "configs" / "factory" / "promoted" / f"{DEPLOYED}.json",
                    repo / "configs" / "factory" / "promoted" / f"{DEPLOYED}.json")
        state_dir = tmp_path / "state"
        env = dict(os.environ, MP_REPO_DIR=str(repo), MP_GENOME_ID=DEPLOYED, MP_SANDBOX_IP="192.168.50.41",
                   MP_RECONCILE_STATE_DIR=str(state_dir), MP_RECONCILE_DRY_RUN="1")
        r = subprocess.run([bash, str(repo / "deploy" / "spark" / "factory_reconcile.sh")], capture_output=True,
                           text=True, env=env, timeout=120)
        assert r.returncode == 5, (r.stdout, r.stderr)
        assert "FRAME_MISSING bfcf94654a3a" in r.stderr
        status = json.loads((state_dir / "last_run.json").read_text(encoding="utf-8"))
        assert status["exit"] == 5 and status["reason"].startswith("FRAME_MISSING")
        # with the frame present the dry run plans and stops before docker (exit 0)
        frame = repo / "data" / "factory" / "frames" / "weather_2026-07-25_bfcf94654a3a"
        (frame / "search").mkdir(parents=True)
        r = subprocess.run([bash, str(repo / "deploy" / "spark" / "factory_reconcile.sh")], capture_output=True,
                           text=True, env=env, timeout=120)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "maia.local -> 192.168.50.41" in r.stdout and "dry run" in r.stdout
        # and an unresolvable host is exit 4 before anything else
        env_bad = dict(env, MP_SANDBOX_IP="", MONEY_PRINTER_URL="http://no-such-host-mp.invalid:8050")
        r = subprocess.run([bash, str(repo / "deploy" / "spark" / "factory_reconcile.sh")], capture_output=True,
                           text=True, env=env_bad, timeout=120)
        assert r.returncode == 4 and "HOST_UNRESOLVED" in r.stderr

    def test_install_snippet_and_hermes_watch(self):
        inst = self.INSTALL.read_text(encoding="utf-8")
        assert "mp-factory-reconcile.timer" in inst and "sudo" in inst and "enable --now" in inst
        assert "NEEDS ROOT" in inst and "STARTS THE TIMER" in inst and "FRAME_MISSING" in inst and "--add-host" in inst
        h = self.HERMES.read_text(encoding="utf-8")
        assert h.startswith("#!/usr/bin/env bash") and "paper_reconcile_" in h and "exit 0" in h
        assert "last_run.json" in h and "HOST_UNRESOLVED" in h and "FRAME_MISSING" in h and "SAME DAY" in h

    def test_hermes_watch_reports_a_failed_run_the_same_day(self, tmp_path):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not available")
        state_dir = tmp_path / "run_state"
        state_dir.mkdir()
        (state_dir / "last_run.json").write_text(json.dumps({
            "finished_utc": "2026-09-07T14:31:00Z", "exit": 4, "reason": "HOST_UNRESOLVED maia.local",
            "genome_id": DEPLOYED, "window": "2026-08-31..2026-09-06", "frame": "", "sandbox_ip": "", "report": "", "host": "alcyone",
        }), encoding="utf-8")
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "paper_reconcile_2026-08-31_2026-09-06.json").write_text(json.dumps({"summary": {"verdict": "OK"}}), encoding="utf-8")
        env = dict(os.environ, MP_RECONCILE_STATE_DIR=str(state_dir), MONEY_PRINTER_FACTORY_DIR=str(reports),
                   HERMES_HOME=str(tmp_path / "hermes"))
        r = subprocess.run([bash, str(self.HERMES)], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "HOST_UNRESOLVED" in r.stdout and "2026-09-07T14:31:00Z" in r.stdout
        r = subprocess.run([bash, str(self.HERMES)], capture_output=True, text=True, env=env, timeout=60)
        assert r.stdout.strip() == ""  # deduped: the same failed run is not re-posted


# ===========================================================================
# 7. RUNBOOK + HANDOFF DRAFT
# ===========================================================================
class TestRunbookAndHandoffDraft:
    def test_factory_runbook_names_the_real_commands_and_the_kill_path(self):
        doc = (REPO_ROOT / "docs" / "FACTORY.md").read_text(encoding="utf-8")
        for needle in (
            # the REAL invocations (factory.py holdout --help / score --help at 6b8fffb): --finalists takes a
            # FILE, --unseal is REQUIRED on both
            "factory.py holdout --finalists reports/factory/finalists.json --unseal RATIFIED-",
            "factory.py holdout --audit",
            "factory.py score --genome <id> --ladders data/ladders_2026-09 --unseal RATIFIED-",
            "register-gate",
            "--fill-commit-time <id>",
            "gate_registration_<id>.json",
            "git rm",
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
            "FRAME_MISSING",
            "--add-host",
            "HOST_UNRESOLVED",
            "needs root",
            "family pooled OOS",
            "in-sample",
        ):
            assert needle in doc, f"docs/FACTORY.md lacks {needle!r}"
        for wrong in (
            "holdout --finalists --unseal",  # --finalists without its FILE argument
            "being implemented concurrently",
            "score --genome <id> --ladders data/ladders_2026-09\n",  # score without --unseal
            "the factory's\nprediction",
        ):
            assert wrong not in doc, f"docs/FACTORY.md still says {wrong!r}"

    def test_handoff_draft_is_a_skeleton_with_placeholders(self):
        text = (REPO_ROOT / "reports" / "factory" / "HANDOFF_F4_draft.md").read_text(encoding="utf-8")
        assert "<" in text and "PASS" in text and "HALT" in text
        assert "grouped_count" in text or "target_date" in text
        assert "either way" in text.lower() or "verdict" in text.lower()
