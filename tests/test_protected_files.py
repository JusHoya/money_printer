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


def test_protected_files_unchanged_since_f2_base():
    _skip_unless_git_checkout()
    diff = _git("diff", "--stat", BASE_COMMIT, "--", *PROTECTED)
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", (
        f"protected files differ from {BASE_COMMIT} (F3 may not touch them):\n{diff.stdout}"
    )
    # Belt and braces: the exit-code form, which also covers mode changes.
    quiet = _git("diff", "--quiet", BASE_COMMIT, "--", *PROTECTED)
    assert quiet.returncode == 0


def test_protected_files_have_no_staged_changes():
    _skip_unless_git_checkout()
    staged = _git("diff", "--cached", "--stat", BASE_COMMIT, "--", *PROTECTED)
    assert staged.returncode == 0, staged.stderr
    assert staged.stdout.strip() == "", f"staged edits to protected files:\n{staged.stdout}"
