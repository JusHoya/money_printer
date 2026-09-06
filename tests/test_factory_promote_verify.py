"""``factory.py promote``: the FR-F3.4 report is never rewritten silently; ``--verify-committed`` is read-only.

Second red team (2026-09-06): re-promoting ``09fca4bc5ac55470`` rewrote the tracked
per-genome parity report in place; and because the cold-start sizing guard fires for
five of the six committed specs, only that one genome could be re-promoted through the
real path, so the deployed genome's archive pins rested on the backfill script alone.

* ``promote <id> --from-seed NAME`` with an existing ``replay_parity_<sha12>_<id>.json``
  refuses UP FRONT (before the sizing guard and the slow replay) unless
  ``--rewrite-parity-report`` is passed, and says so.
* ``--verify-committed`` builds the spec document through the same builder (parity +
  pin stamping), skips ONLY the sizing guard, writes nothing, and prints
  IDENTICAL / DIFFERS against ``configs/factory/promoted/<id>.json``.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAMES_DIR = Path(os.getenv("MP_FACTORY_FRAMES") or REPO_ROOT / "data" / "factory" / "frames" / "weather_2026-07-25_bfcf94654a3a")
PROMOTED = REPO_ROOT / "configs" / "factory" / "promoted"
REPORTS = REPO_ROOT / "reports" / "factory"
#: the deployed genome: its sizing guard FIRES (10 % sizable), so the real promote path
#: could never re-verify it -- exactly the case --verify-committed exists for
GID, SEED = "0c4b20502f2daf65", "fr31a_taker"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _promote(*extra: str, timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "scripts/factory.py", "promote", GID, "--from-seed", SEED, "--frames", str(FRAMES_DIR), *extra],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout, env=env,
    )


pytestmark = pytest.mark.skipif(
    not (FRAMES_DIR.exists() and (PROMOTED / f"{GID}.json").exists()),
    reason="frozen parity frame / committed spec not present",
)


def test_promote_refuses_to_overwrite_an_existing_parity_report_up_front(tmp_path):
    report = REPORTS / f"replay_parity_bfcf94654a3a_{GID}.json"
    assert report.exists()
    before = _sha(report)
    proc = _promote("--out-dir", str(tmp_path), timeout=120)  # must refuse in seconds, not after a replay
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "already exists and is FR-F3.4 evidence" in proc.stderr
    assert "--rewrite-parity-report" in proc.stderr and "--verify-committed" in proc.stderr
    assert "cold-start sizing" not in proc.stdout  # refused BEFORE the guard and the replay
    assert _sha(report) == before
    assert not (tmp_path / f"{GID}.json").exists()


def test_verify_committed_is_read_only_and_reports_identical():
    spec = PROMOTED / f"{GID}.json"
    report = REPORTS / f"replay_parity_bfcf94654a3a_{GID}.json"
    before = (_sha(spec), _sha(report))
    proc = _promote("--verify-committed")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cold-start sizing guard SKIPPED" in proc.stdout
    assert "parity report NOT written" in proc.stdout
    line = next(l for l in proc.stdout.splitlines() if l.startswith("verify-committed"))
    assert line.startswith(f"verify-committed {GID}: IDENTICAL"), line
    assert "0 disc" in line and "nothing written" in line
    assert (_sha(spec), _sha(report)) == before  # nothing written, nothing touched


def test_verify_committed_reports_differs_with_the_keys(tmp_path):
    """A spec that drifted from what the builder produces must say which keys.

    Verified on a tampered COPY under ``--out-dir`` (read only in this mode): a test must
    never rewrite the tracked spec, even briefly -- a concurrent test run would read the
    tampered file (which is exactly how this test first failed three unrelated tests).
    """
    import json

    spec = PROMOTED / f"{GID}.json"
    original = spec.read_bytes()
    doc = json.loads(original.decode("utf-8"))
    doc["calibration"]["truth_sha256"]["KNYC"] = "f" * 64
    from src.factory import promoted as P

    doc["spec_hash"] = P.spec_hash_of(doc)
    tampered = tmp_path / f"{GID}.json"
    tampered.write_bytes((json.dumps(doc, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    tampered_sha = _sha(tampered)
    proc = _promote("--verify-committed", "--out-dir", str(tmp_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("verify-committed"))
    assert "DIFFERS" in line and "calibration.truth_sha256.KNYC" in line and "spec_hash" in line
    assert _sha(tampered) == tampered_sha  # read only: the copy was not "fixed" either
    assert spec.read_bytes() == original
