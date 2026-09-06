"""Promoted-spec contract (F3): schema, hash verification, policy refusals, determinism, CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.factory import genome as G  # noqa: E402
from src.factory import promoted as P  # noqa: E402
from src.factory.fees import load_regime  # noqa: E402

CAL_DIR = REPO_ROOT / "data" / "calibration"


@pytest.fixture
def mp_caplog(caplog):
    """`MoneyPrinter` sets propagate=False, so caplog needs its handler attached directly."""
    import logging

    from src.utils.logger import logger as mp_logger

    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    yield caplog
    mp_logger.removeHandler(caplog.handler)


def _spec(mode="shadow", status="CLOSED", genome=None, **over):
    g = genome or G.SEEDS["fr31b"]
    kw = dict(
        family="weather/gfs_mex/taker/v1",
        config_sha256="c" * 64,
        frame_search_sha256="f" * 64,
        calibration_dir=str(CAL_DIR),
        calibration_sha256=P.calibration_dir_sha256(str(CAL_DIR)),
        fee_type="quadratic",
        fee_regime_sha256=load_regime().sha256,
        mode=mode,
        registry_status=status,
        source="seed",
        parity={"frame_sha12": "bfcf94654a3a", "n_discrepancies": 0},
    )
    kw.update(over)
    return P.build_spec(g, **kw)


class TestHashing:
    def test_genome_id_matches_ledger(self):
        pytest.importorskip("pyarrow")
        from src.factory import ledger

        g = G.SEEDS["fr31a_taker"]
        gj = ledger.genome_json(g)
        assert P.genome_json_for(g) == gj
        assert P.genome_id_for(gj) == ledger.genome_id(gj)

    def test_pick_genome_id_reproduces_summary(self):
        path = REPO_ROOT / "reports" / "factory" / "run_2026-09-03b" / "summary.json"
        if not path.exists():
            pytest.skip("F2 summary not on disk")
        doc = json.loads(path.read_text(encoding="utf-8"))
        for camp, pk in (doc.get("picks") or {}).items():
            if pk.get("genome_json"):
                assert P.genome_id_for(pk["genome_json"]) == pk["genome_id"], camp

    def test_spec_hash_verifies_and_detects_tampering(self):
        spec = _spec()
        doc = spec.to_doc()
        assert doc["spec_hash"] == P.spec_hash_of(doc)
        P.from_doc(doc)
        bad = dict(doc)
        bad["adverse_fill"] = 0.02
        with pytest.raises(P.PromotedSpecError, match="spec_hash"):
            P.from_doc(bad)

    def test_genome_id_must_match_genome_json(self):
        doc = _spec().to_doc()
        doc["genome_id"] = "0" * 16
        doc["spec_hash"] = P.spec_hash_of(doc)
        with pytest.raises(P.PromotedSpecError, match="genome_id"):
            P.from_doc(doc)

    def test_calibration_dir_sha_is_the_frame_provenance_mapping_hash(self):
        files = P.hash_calibration_dir(str(CAL_DIR))
        assert files and all(k.startswith("data/calibration/") for k in files)
        assert P.calibration_dir_sha256(str(CAL_DIR)) == P.sha256_of_mapping(files)
        pytest.importorskip("pandas")
        from src.factory import frame as fr

        assert fr._hash_dir(str(CAL_DIR)) == files
        assert fr._sha256_of_mapping(files) == P.sha256_of_mapping(files)


class TestPolicy:
    def test_required_and_unknown_keys(self):
        doc = _spec().to_doc()
        d2 = dict(doc)
        del d2["fee"]
        with pytest.raises(P.PromotedSpecError, match="lacks"):
            P.from_doc(d2)
        d3 = dict(doc)
        d3["promoted_at"] = "2026-09-04T00:00:00Z"  # timestamps are not part of the schema
        with pytest.raises(P.PromotedSpecError, match="unknown"):
            P.from_doc(d3)

    def test_paper_mode_refused_for_closed_family(self):
        with pytest.raises(P.PromotedSpecError, match="paper"):
            _spec(mode="paper", status="CLOSED")
        for ok in P.PAPER_ALLOWED_STATUSES:
            assert _spec(mode="paper", status=ok).mode == "paper"

    def test_fee_type_follows_genome_mode(self):
        assert _spec().fee.type == "taker"
        assert _spec(genome=G.SEEDS["salvage_5f"]).fee.type == "maker"


class TestIO:
    def test_write_load_roundtrip_is_deterministic_and_timestamp_free(self, tmp_path):
        spec = _spec()
        p1 = tmp_path / "a.json"
        p2 = tmp_path / "b.json"
        P.write_promoted(spec, p1)
        P.write_promoted(spec, p2)
        b1, b2 = p1.read_bytes(), p2.read_bytes()
        assert b1 == b2
        assert b1.endswith(b"\n") and b"\r\n" not in b1
        text = b1.decode("utf-8")
        assert json.dumps(json.loads(text), sort_keys=True, indent=2) + "\n" == text
        for banned in ("promoted_at", "generated", "timestamp", "\"ts\""):
            assert banned not in text
        loaded = P.load_promoted(str(p1))
        assert loaded == spec
        assert loaded.genome() == G.SEEDS["fr31b"]
        assert loaded.id8 == spec.genome_id[:8]

    def test_load_by_id_from_directory(self, tmp_path):
        spec = _spec()
        P.write_promoted(spec, tmp_path / f"{spec.genome_id}.json")
        assert P.load_promoted(spec.genome_id, directory=str(tmp_path)).genome_id == spec.genome_id
        with pytest.raises(P.PromotedSpecError, match="no promoted spec"):
            P.load_promoted("deadbeefdeadbeef", directory=str(tmp_path))

    def test_committed_specs_load_and_are_shadow(self):
        d = Path(P.PROMOTED_DIR)
        specs = sorted(d.glob("*.json")) if d.exists() else []
        if not specs:
            pytest.skip("no committed promoted specs")
        for p in specs:
            s = P.load_promoted(str(p))
            assert p.stem == s.genome_id
            assert s.mode == "shadow"  # family #1 is CLOSED: nothing may paper-trade
            assert s.registry_status == "CLOSED"
            assert s.parity.get("n_discrepancies") == 0
            assert s.calibration.sha256 == P.calibration_dir_sha256(str(REPO_ROOT / s.calibration.dir))
            assert s.fee.regime_sha256 == load_regime().sha256


# ---------------------------------------------------------------------------
# scripts/factory.py promote -- refusals that must fire BEFORE any parity work
# ---------------------------------------------------------------------------
def _factory(*args):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "factory.py"), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120,
    )


class TestPromoteCLI:
    def test_promote_is_a_real_subcommand(self):
        r = _factory("promote", "--help")
        assert r.returncode == 0, r.stderr
        assert "--from-seed" in r.stdout and "--from-pick" in r.stdout and "--mode" in r.stdout

    def test_paper_mode_refused_while_family_closed(self, tmp_path):
        gid = P.genome_id_for(P.genome_json_for(G.SEEDS["fr31b"]))
        r = _factory("promote", gid, "--from-seed", "fr31b", "--mode", "paper", "--out-dir", str(tmp_path))
        assert r.returncode == 1
        assert "paper refused" in r.stderr and "CLOSED" in r.stderr
        assert not list(tmp_path.glob("*.json"))

    def test_wrong_id_refused(self, tmp_path):
        r = _factory("promote", "0000000000000000", "--from-seed", "fr31b", "--out-dir", str(tmp_path))
        assert r.returncode == 1 and "refusing to promote the wrong genome" in r.stderr

    def test_maker_seed_refused(self, tmp_path):
        gid = P.genome_id_for(P.genome_json_for(G.SEEDS["salvage_5f"]))
        r = _factory("promote", gid, "--from-seed", "salvage_5f", "--out-dir", str(tmp_path))
        assert r.returncode == 1 and "MAKER" in r.stderr

    def test_needs_exactly_one_source(self):
        r = _factory("promote", "abcdef0123456789")
        assert r.returncode == 1 and "exactly one" in r.stderr


# ---------------------------------------------------------------------------
# The calibration-KIND field (F3 registered deviation / F4 blocker 1).
#
# `dir` and `sha256` are byte-identical for the walk-forward and frozen providers
# -- they read the same files -- so before this field the substitution was
# undetectable. It costs 60 discrepancies and 0.336 of p_yes for 0c4b20502f2daf65
# (reports/factory/replay_parity_bfcf94654a3a_frozen.json vs the walk_forward
# control). These tests pin the field and the guard that reads it.
# ---------------------------------------------------------------------------
class TestCalibrationKind:
    def test_kind_round_trips_and_is_hash_covered(self):
        spec = _spec(calibration_kind="walk_forward")
        assert spec.calibration.kind == "walk_forward"
        doc = spec.to_doc()
        assert doc["calibration"]["kind"] == "walk_forward"
        assert P.from_doc(doc).calibration.kind == "walk_forward"
        # the field is inside spec_hash, so a silent edit cannot survive load
        doc["calibration"]["kind"] = "frozen"
        with pytest.raises(P.PromotedSpecError):
            P.from_doc(doc)

    def test_absent_kind_loads_as_unproven(self):
        doc = _spec().to_doc()
        del doc["calibration"]["kind"]
        doc["spec_hash"] = P.spec_hash_of(doc)
        assert P.from_doc(doc).calibration.kind is None

    def test_every_committed_spec_names_the_provider_parity_ran_under(self):
        """A shipped spec that does not say which provider proved it cannot be paper-promoted."""
        specs = sorted((REPO_ROOT / "configs" / "factory" / "promoted").glob("*.json"))
        assert specs, "no promoted specs on disk"
        for path in specs:
            spec = P.load_promoted(str(path))
            assert spec.calibration.kind == "walk_forward", (
                f"{path.name} records calibration kind {spec.calibration.kind!r}; the per-genome "
                f"parity report for it was served walk_forward payloads"
            )
            report = REPO_ROOT / "reports" / "factory" / f"replay_parity_bfcf94654a3a_{spec.genome_id}.json"
            if report.exists():  # the run that promoted it must agree
                served = json.loads(report.read_text(encoding="utf-8"))["calibration"]["replay_kind"]
                assert spec.calibration.kind == served, f"{path.name}: spec {spec.calibration.kind} != report {served}"


class TestCalibrationKindGuard:
    """Paper REFUSES a provider mismatch; shadow reaches no exchange, so it warns and runs."""

    @staticmethod
    def _build(spec):
        import datetime as dt

        from src.strategies.genome_strategy import FrozenCalibrationProvider, GenomeStrategy

        class _VP:
            lag_min = spec.availability_lag_min

        return GenomeStrategy(
            spec,
            clock=lambda: dt.datetime(2026, 9, 6, 15, 0, tzinfo=dt.timezone.utc),
            forecast_provider=_VP(),
            fee_regime=load_regime(),
            calibration_provider=FrozenCalibrationProvider(str(CAL_DIR), source=spec.forecast_source),
        )

    def test_shadow_warns_but_constructs(self, mp_caplog):
        strategy = self._build(_spec(calibration_kind="walk_forward"))
        assert strategy.calibration_kind == "frozen"
        assert strategy.calibration_kind_ok is False
        # the runtime logger, i.e. maia's log file and /api/logs/tail -- not a silent flag
        assert "CALIBRATION PROVIDER MISMATCH" in mp_caplog.text

    def test_paper_refuses_a_mismatch(self):
        from src.strategies.genome_strategy import GenomeSpecMismatch

        spec = _spec(mode="paper", status="PROPOSED", calibration_kind="walk_forward")
        with pytest.raises(GenomeSpecMismatch, match="refusing paper mode"):
            self._build(spec)

    def test_paper_refuses_a_spec_that_names_no_kind(self):
        """An unproven spec is not a passing one: silence must not read as agreement."""
        from src.strategies.genome_strategy import GenomeSpecMismatch

        doc = _spec(mode="paper", status="PROPOSED").to_doc()
        del doc["calibration"]["kind"]
        doc["spec_hash"] = P.spec_hash_of(doc)
        with pytest.raises(GenomeSpecMismatch, match="records no proven kind"):
            self._build(P.from_doc(doc))

    def test_paper_accepts_the_provider_it_was_proven_under(self):
        strategy = self._build(_spec(mode="paper", status="PROPOSED", calibration_kind="frozen"))
        assert strategy.calibration_kind_ok is True

    def test_the_dir_sha_alone_cannot_see_the_substitution(self):
        """Why the field is needed at all: both providers hash the same directory."""
        import scripts.factory_replay_parity  # noqa: F401  (path shim)

        from src.strategies.genome_strategy import FrozenCalibrationProvider

        spec = _spec(calibration_kind="walk_forward")
        frozen = FrozenCalibrationProvider(str(CAL_DIR), source=spec.forecast_source)
        assert frozen.sha256 == spec.calibration.sha256
        assert frozen.kind != spec.calibration.kind
