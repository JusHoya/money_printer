"""F4 blocker 1: the sandbox must be able to READ the family registry the paper gate depends on.

``WeatherBot._registry_status(family)`` opens ``<REPO_ROOT>/reports/factory/registry.jsonl``
to confirm a family's CURRENT status before paper mode is allowed.  Two defects, both
structural (they would break a ``weather/gfs_mex/taker/v2`` family exactly as they break
family #1):

1. **The file never reached the container.**  ``.dockerignore`` excludes ``reports/`` from
   the build context and no compose bind restored it, so inside ``mp-sandbox`` the path did
   not exist, ``except OSError: return None`` always fired, and the gate could never return
   an authorizing value.  It failed CLOSED, so nothing was unsafe -- but the check was
   decorative, and in F4 it would refuse for a reason that *looks* like a governance
   decision and is not.  Fixed by a read-only bind of the live tracked directory (NOT by
   baking the file into the image: a build-time snapshot is exactly what a "CURRENT status"
   check must not trust).

2. **The diagnosis was ambiguous.**  ``return None`` meant both "the registry file is
   missing/unreadable" (a misconfigured deployment) and "the file was read and this family
   is not in it" (a normal governance refusal).  Those need different operator responses.
   The unreadable case now raises ``RegistryUnavailable`` and is reported as a deployment
   fault; every other outcome still fails closed.

What is verified by EXECUTION here: the code-side behaviour, reproduced against a tree with
no ``reports/`` (the container's filesystem shape) and against one with the file.
What is verified by CONFIGURATION READING: that the compose bind exists, is read-only, has
the checkout (not a copy) on the host side, and lands on exactly the container path the
code opens -- docker cannot run on the dev box, so those are asserted against the files.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.bots.weather_bot as weather_bot  # noqa: E402
import src.factory.promoted as P  # noqa: E402
from src.bots.weather_bot import REGISTRY_RELPATH, RegistryUnavailable, WeatherBot  # noqa: E402
from src.utils.logger import logger as mp_logger  # noqa: E402

FAMILY = "weather/gfs_mex/taker/v1"
FAMILY_V2 = "weather/gfs_mex/taker/v2"  # the family F4 opens; the machinery must serve it too
COMPOSE = REPO_ROOT / "deploy" / "pi" / "docker-compose.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
CONTAINER_APP_DIR = "/app"


def _write_registry(root: Path, family: str, status: str) -> Path:
    """Write a minimal registry.jsonl under ``root`` the way the factory writes it."""
    path = root / Path(REGISTRY_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"event": "family", "family": family, "status": "OPEN", "ts": "2026-09-03T00:00:00+00:00"},
        {"event": "transition", "family": family, "status": status, "ts": "2026-09-06T00:00:00+00:00"},
    ]
    path.write_text("".join(json.dumps(o, sort_keys=True) + "\n" for o in lines), encoding="utf-8")
    return path


@pytest.fixture
def mp_caplog(caplog):
    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    yield caplog
    mp_logger.removeHandler(caplog.handler)


@pytest.fixture
def container_shape(tmp_path, monkeypatch):
    """A REPO_ROOT with no ``reports/`` at all -- what ``mp-sandbox`` looked like."""
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(P, "REPO_ROOT", str(root))
    assert not (root / "reports").exists()
    return root


# ---------------------------------------------------------------------------
# 1. the read itself: missing file != family not authorized
# ---------------------------------------------------------------------------
class TestRegistryReadDiagnosis:
    def test_a_missing_registry_file_is_a_deployment_fault_not_a_status(self, container_shape):
        # BEFORE: returned None, indistinguishable from "the family is not in the registry".
        with pytest.raises(RegistryUnavailable) as exc:
            WeatherBot._registry_status(FAMILY)
        msg = str(exc.value)
        assert REGISTRY_RELPATH in msg.replace(os.sep, "/"), msg   # names the path it tried
        assert str(container_shape) in msg, msg                    # and where it resolved it

    def test_a_directory_where_the_file_should_be_is_also_a_deployment_fault(self, container_shape):
        # Docker creates a DIRECTORY at a bind target whose host path is missing; that
        # must read as "the deployment is wrong", not as "this family is unknown".
        (container_shape / Path(REGISTRY_RELPATH)).mkdir(parents=True)
        with pytest.raises(RegistryUnavailable):
            WeatherBot._registry_status(FAMILY)

    def test_a_readable_registry_that_lacks_the_family_is_a_normal_none(self, tmp_path, monkeypatch):
        root = tmp_path / "app"
        _write_registry(root, FAMILY, "PROPOSED")
        monkeypatch.setattr(P, "REPO_ROOT", str(root))
        # The file was read; this family simply is not in it. Not a deployment fault.
        assert WeatherBot._registry_status(FAMILY_V2) is None

    @pytest.mark.parametrize("status", ["OPEN", "CLOSED", "PROPOSED", "RATIFIED", "HALT"])
    def test_a_present_registry_yields_the_current_status(self, tmp_path, monkeypatch, status):
        root = tmp_path / "app"
        _write_registry(root, FAMILY_V2, status)
        monkeypatch.setattr(P, "REPO_ROOT", str(root))
        assert WeatherBot._registry_status(FAMILY_V2) == status

    def test_the_real_checkout_still_reads_family_one_as_closed(self):
        # The tracked file in THIS checkout; guards the fixture against drifting from reality.
        assert WeatherBot._registry_status(FAMILY) == "CLOSED"


# ---------------------------------------------------------------------------
# 2. the gate: the container shape must refuse LOUDLY, a good tree must authorize
# ---------------------------------------------------------------------------
def _paper_bot(tmp_path, monkeypatch, family=FAMILY_V2):
    """Construct WeatherBot with a paper spec + GENOME_STRATEGY_MODE=paper."""
    from src.factory import genome as G
    from src.factory.fees import load_regime

    cal = str(REPO_ROOT / "data" / "calibration")
    spec = P.build_spec(
        G.SEEDS["nofilter_no"], family=family, config_sha256="c" * 64,
        frame_search_sha256="f" * 64, calibration_dir=cal,
        calibration_sha256=P.calibration_dir_sha256(cal), fee_type="quadratic",
        fee_regime_sha256=load_regime().sha256, mode="paper", registry_status="PROPOSED",
        source="seed",
    )
    path = P.write_promoted(spec, tmp_path / f"{spec.genome_id}.json")
    monkeypatch.setenv("GENOME_STRATEGY_ID", path)
    monkeypatch.setenv("GENOME_STRATEGY_MODE", "paper")
    monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
    return WeatherBot()


class TestPaperGateEndToEnd:
    def test_the_container_shape_refuses_paper_as_a_deployment_fault(
        self, tmp_path, monkeypatch, container_shape, mp_caplog
    ):
        # BEFORE the fix this refused with "registry status is None", which reads as a
        # governance decision about the family. It is not: the file was never there.
        bot = _paper_bot(tmp_path, monkeypatch)
        assert list(bot.strategies) == ["weather"] and bot.genome_spec is None  # fail-closed
        loud = [m for m in mp_caplog.messages if "GenomeStrategy REFUSED" in m]
        assert loud, mp_caplog.messages
        blob = " ".join(loud)
        assert "MISCONFIGURED" in blob, blob                       # names the fault class
        assert REGISTRY_RELPATH in blob.replace(os.sep, "/"), blob  # names the missing path
        assert "docker-compose" in blob, blob                       # names the fix
        assert "status is None" not in blob, blob                   # never a fake status
        assert bot.genome_refused_reason and "registry" in bot.genome_refused_reason.lower()
        assert "MISCONFIGURED" in bot.genome_refused_reason         # visible on the dashboard

    def test_a_readable_registry_saying_proposed_authorizes_paper(self, tmp_path, monkeypatch):
        root = tmp_path / "app"
        _write_registry(root, FAMILY_V2, "PROPOSED")
        monkeypatch.setattr(P, "REPO_ROOT", str(root))
        bot = _paper_bot(tmp_path, monkeypatch)
        assert bot.genome_refused_reason is None, bot.genome_refused_reason
        assert list(bot.strategies) == ["genome", "weather"]
        assert bot.genome_shadow is False and bot.genome_spec is not None

    def test_a_readable_registry_saying_closed_refuses_as_governance_not_deployment(
        self, tmp_path, monkeypatch, mp_caplog
    ):
        root = tmp_path / "app"
        _write_registry(root, FAMILY_V2, "CLOSED")
        monkeypatch.setattr(P, "REPO_ROOT", str(root))
        bot = _paper_bot(tmp_path, monkeypatch)
        assert list(bot.strategies) == ["weather"] and bot.genome_spec is None
        blob = " ".join(m for m in mp_caplog.messages if "REFUSED" in m)
        assert "CLOSED" in blob and "MISCONFIGURED" not in blob, blob
        assert "MISCONFIGURED" not in (bot.genome_refused_reason or "")

    @staticmethod
    def _shadow_bot(tmp_path, monkeypatch):
        from src.factory import genome as G
        from src.factory.fees import load_regime

        cal = str(REPO_ROOT / "data" / "calibration")
        spec = P.build_spec(
            G.SEEDS["nofilter_no"], family=FAMILY_V2, config_sha256="c" * 64,
            frame_search_sha256="f" * 64, calibration_dir=cal,
            calibration_sha256=P.calibration_dir_sha256(cal), fee_type="quadratic",
            fee_regime_sha256=load_regime().sha256, mode="shadow", registry_status="CLOSED",
            source="seed",
        )
        path = P.write_promoted(spec, tmp_path / f"{spec.genome_id}.json")
        monkeypatch.setenv("GENOME_STRATEGY_ID", path)
        monkeypatch.setenv("GENOME_STRATEGY_MODE", "shadow")  # what compose pins today
        monkeypatch.setenv("MP_FORECAST_CACHE_DIR", str(tmp_path / "cache"))
        monkeypatch.setattr(weather_bot, "WEATHER_TRADING_ENABLED", True)
        return WeatherBot()

    def test_shadow_mode_warns_early_that_paper_could_not_be_authorized(
        self, tmp_path, monkeypatch, container_shape, mp_caplog
    ):
        # Shadow never consults the registry, so a broken deployment would stay silent
        # until F4 flipped to paper. Surface it at shadow time -- log only, no behaviour
        # change: the genome still loads and still runs in shadow.
        bot = self._shadow_bot(tmp_path, monkeypatch)
        assert list(bot.strategies) == ["genome", "weather"]     # unchanged behaviour
        assert bot.genome_shadow is True and bot.genome_refused_reason is None
        assert any("MISCONFIGURED" in m and REGISTRY_RELPATH in m.replace(os.sep, "/")
                   for m in mp_caplog.messages), mp_caplog.messages

    def test_the_deployed_shadow_path_is_unchanged_and_silent_once_the_bind_exists(
        self, tmp_path, monkeypatch, mp_caplog
    ):
        # This is what maia runs today and what it will run after this branch deploys:
        # GENOME_STRATEGY_MODE=shadow on a shadow spec. Nothing new is said, nothing new
        # is refused -- the only difference is that the file is now reachable.
        root = tmp_path / "app"
        _write_registry(root, FAMILY_V2, "CLOSED")
        monkeypatch.setattr(P, "REPO_ROOT", str(root))
        bot = self._shadow_bot(tmp_path, monkeypatch)
        assert list(bot.strategies) == ["genome", "weather"]
        assert bot.genome_shadow is True and bot.genome_refused_reason is None
        assert not any("MISCONFIGURED" in m for m in mp_caplog.messages), mp_caplog.messages


# ---------------------------------------------------------------------------
# 3. the deployment: a LIVE bind, never a build-time snapshot
#    (verified by reading the config -- docker is not available on this box)
# ---------------------------------------------------------------------------
class TestSandboxDeploymentServesTheRegistry:
    @staticmethod
    def _sandbox_volumes():
        doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
        return [str(v) for v in doc["services"]["sandbox"]["volumes"]]

    def test_the_image_still_excludes_reports(self):
        # The fix must NOT be "bake it in": a build-time copy of a CURRENT-status file
        # would report PROPOSED for a family closed after the image was built.
        ignored = [l.strip() for l in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()]
        assert "reports/" in ignored, "reports/ was un-ignored -- the registry must not be baked in"

    def test_compose_binds_the_registry_the_code_actually_opens(self):
        container_target = CONTAINER_APP_DIR + "/" + REGISTRY_RELPATH
        binds = [v for v in self._sandbox_volumes() if len(v.split(":")) >= 2]
        covering = []
        for v in binds:
            host, target = v.split(":")[0], v.split(":")[1]
            if container_target == target or container_target.startswith(target.rstrip("/") + "/"):
                covering.append((host, target, v))
        assert covering, f"nothing mounts {container_target}; volumes were {binds}"
        host, _, entry = covering[0]
        assert entry.endswith(":ro"), f"the sandbox must never write the registry: {entry}"
        # Host side is the CHECKOUT, resolved relative to the compose file's directory --
        # so what the container reads is what `git pull` just wrote, not a copy that can rot.
        assert host.startswith(".."), f"host side must be the live checkout, got {host!r}"
        resolved = (COMPOSE.parent / host).resolve()
        assert resolved == (REPO_ROOT / "reports" / "factory").resolve(), resolved
        assert resolved.is_dir(), resolved

    def test_every_compose_invocation_resolves_the_relative_bind_to_this_checkout(self):
        # Compose resolves a relative host path against the PROJECT directory, which is the
        # directory of the first -f file. Every invocation in the repo passes
        # -f <...>/deploy/pi/docker-compose.yml, so `../../reports/factory` is always the
        # checkout the deploy just `git pull`ed -- never some other tree, never a copy.
        seen = 0
        for path in (REPO_ROOT / "deploy" / "pi" / "deploy_f3_shadow.sh",
                     REPO_ROOT / "deploy" / "pi" / "bootstrap_maia.sh"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "docker compose" not in line or "docker-compose.yml" not in line:
                    continue
                seen += 1
                assert "-f" in line, f"{path.name}: compose without -f changes the project dir: {line}"
                assert "--project-directory" not in line, \
                    f"{path.name}: --project-directory re-bases the relative bind: {line}"
        assert seen >= 2, f"found only {seen} compose invocations to check"

    def test_the_deploy_reports_the_registry_bind_without_gating_on_it(self):
        # A shadow deploy does not need the registry, so proving the bind must not be able
        # to FAIL a deploy that is otherwise fine -- it reports, so the bind is verified on
        # maia long before F4 flips to paper.
        text = (REPO_ROOT / "deploy" / "pi" / "deploy_f3_shadow.sh").read_text(encoding="utf-8")
        assert "/app/" + REGISTRY_RELPATH in text, "the deploy never looks at the registry bind"
        check = [l for l in text.splitlines() if "REGISTRY_IN_CONTAINER" in l and "docker exec" in l]
        assert check, text[-2000:]
        assert not any("die " in l for l in check), f"the registry check must not gate the deploy: {check}"

    def test_the_shadow_warning_cannot_flip_the_deploys_verdict(self):
        # deploy_f3_shadow.sh greps the container log for "GenomeStrategy" and dies on
        # "GenomeStrategy REFUSED". The new shadow-mode warning is a DIAGNOSTIC on a
        # working deploy, so it must not carry that token.
        src = (REPO_ROOT / "src" / "bots" / "weather_bot.py").read_text(encoding="utf-8")
        warn = src.split("shadow mode is unaffected", 1)
        assert len(warn) == 2, "premise changed: the shadow-mode registry warning is gone"
        line = "shadow mode is unaffected" + warn[1].split('",', 1)[0]
        assert "GenomeStrategy" not in line, f"would be read as a REFUSED/loaded line: {line}"

    def test_the_bind_is_a_directory_so_a_git_rename_is_visible(self):
        # git updates a tracked file by writing a temp file and renaming over it: a NEW
        # inode. A single-file bind pins the inode at container-create time and would go
        # stale exactly the way the image does; a directory bind resolves the name per open.
        binds = self._sandbox_volumes()
        assert not any(b.split(":")[1] == CONTAINER_APP_DIR + "/" + REGISTRY_RELPATH
                       for b in binds if len(b.split(":")) >= 2), \
            "single-file bind: a `git pull` that rewrites registry.jsonl would not be seen"
