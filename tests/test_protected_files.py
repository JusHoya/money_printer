"""F3 protected files: the risk/mixin/engine trio must be byte-identical to the F2 base.

PRD_STRATEGY_FACTORY.md Phase F3 exit criterion / F3 sprint contract: ``git diff
38d5fdd -- src/core/risk_manager.py src/bots/mixins.py src/core/matching_engine.py``
is EMPTY (the CONTRA-3 log line and the ``_load_state`` backfill already landed in
F0). Shadow-mode handling therefore lives in ``weather_bot.py``, never in mixins.

Runs git in a subprocess against the *working tree*, so an uncommitted edit is
caught too. Skips cleanly outside a git checkout or when the base commit is not
present (shallow clone, exported tarball).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BASE_COMMIT = "38d5fdd"
PROTECTED = (
    "src/core/risk_manager.py",
    "src/bots/mixins.py",
    "src/core/matching_engine.py",
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60
    )


def _skip_unless_git_checkout() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        pytest.skip("not a git checkout")
    base = _git("cat-file", "-e", f"{BASE_COMMIT}^{{commit}}")
    if base.returncode != 0:
        pytest.skip(f"base commit {BASE_COMMIT} not present in this clone")


def test_protected_files_exist():
    for rel in PROTECTED:
        assert os.path.exists(os.path.join(ROOT, *rel.split("/"))), rel


# 2026-09-04: the F3 accelerated dry run found that binary settlement booked the
# YES-leg payoff against NO entries (every settled NO paper trade sign-flipped).
# The fix is ONE hunk in ``_close_position`` (commit 724d93c), a deliberate,
# owner-ratified deviation: risk_manager.py and mixins.py stay byte-identical and
# matching_engine.py may differ from the base by exactly that hunk.
ENGINE_HUNK_MARKER = "NO-SIDE SETTLEMENT (2026-09-04, F3 dry run finding)"
ENGINE_HUNK_MAX_ADDED = 20


def test_risk_and_mixins_unchanged_since_f2_base():
    _skip_unless_git_checkout()
    files = PROTECTED[:2]
    diff = _git("diff", "--stat", BASE_COMMIT, "--", *files)
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", (
        f"risk_manager/mixins differ from {BASE_COMMIT} (F3 may not touch them):\n{diff.stdout}"
    )


def test_engine_differs_only_by_the_no_side_settlement_hunk():
    _skip_unless_git_checkout()
    diff = _git("diff", "-U0", BASE_COMMIT, "--", PROTECTED[2])
    assert diff.returncode == 0, diff.stderr
    hunks = [l for l in diff.stdout.splitlines() if l.startswith("@@")]
    added = [l for l in diff.stdout.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff.stdout.splitlines() if l.startswith("-") and not l.startswith("---")]
    assert len(hunks) <= 1, f"more than one hunk in matching_engine.py:\n{diff.stdout}"
    assert removed == [], f"matching_engine.py must only ADD the NO-side hunk:\n{diff.stdout}"
    if hunks:
        assert any(ENGINE_HUNK_MARKER in l for l in added), diff.stdout
        assert len(added) <= ENGINE_HUNK_MAX_ADDED, diff.stdout
        assert any("exit_price = 1.0 - exit_price" in l for l in added), diff.stdout


def test_protected_files_have_no_staged_changes():
    _skip_unless_git_checkout()
    staged = _git("diff", "--cached", "--stat", "--", *PROTECTED)
    assert staged.returncode == 0, staged.stderr
    assert staged.stdout.strip() == "", f"staged edits to protected files:\n{staged.stdout}"
