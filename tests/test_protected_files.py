"""F3 protected files: the risk/mixin/engine trio must be byte-identical to the F2 base.

PRD_STRATEGY_FACTORY.md Phase F3 exit criterion / F3 sprint contract: ``git diff
38d5fdd -- src/core/risk_manager.py src/bots/mixins.py src/core/matching_engine.py``
is EMPTY (the CONTRA-3 log line and the ``_load_state`` backfill already landed in
F0). Shadow-mode handling therefore lives in ``weather_bot.py``, never in mixins.

The one ratified exception is the NO-side settlement hunk (724d93c), pinned below
by content hash and insertion point rather than by shape, so no other insertion can
wear its marker comment and pass.

Runs git in a subprocess against the *working tree*, so an uncommitted edit is
caught too. Skips cleanly outside a git checkout or when the base commit is not
present (shallow clone, exported tarball).
"""
from __future__ import annotations

import hashlib
import os
import re
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
#
# 2026-09-05: the allow-list is pinned to the hunk's CONTENT, not to its shape.
# The earlier version accepted any single contiguous insertion carrying the marker
# comment and the substring ``exit_price = 1.0 - exit_price`` in <=20 added lines --
# which a hunk that also disabled a risk check would satisfy. What is ratified is
# these exact 15 lines at this exact position, so that is what is asserted.
ENGINE_HUNK_MARKER = "NO-SIDE SETTLEMENT (2026-09-04, F3 dry run finding)"
#: sha256 of the hunk's added lines, ``+`` stripped and joined with ``\n``.
ENGINE_HUNK_SHA256 = "902c043c07ff63fcc33a7d27208add4d841afad0dc9ee721010773b9223bedc7"
#: (first added line number in the new file, number of added lines) from ``-U0``.
ENGINE_HUNK_AT = (2260, 15)


def _added_lines(diff: str) -> list:
    """Diff body lines that ADD content, with the leading ``+`` and any CR removed."""
    return [l[1:].rstrip("\r") for l in diff.splitlines()
            if l.startswith("+") and not l.startswith("+++")]


def _hunk_target(header: str):
    """``@@ -a,b +c,d @@`` -> ``(c, d)``; ``d`` defaults to 1 when omitted."""
    m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", header)
    assert m, f"unparseable hunk header: {header!r}"
    return int(m.group(1)), int(m.group(2) or 1)


def test_risk_and_mixins_unchanged_since_f2_base():
    _skip_unless_git_checkout()
    files = PROTECTED[:2]
    diff = _git("diff", "--stat", BASE_COMMIT, "--", *files)
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", (
        f"risk_manager/mixins differ from {BASE_COMMIT} (F3 may not touch them):\n{diff.stdout}"
    )


def check_engine_diff(diff_text: str) -> None:
    """Raise ``AssertionError`` unless ``diff_text`` is empty or IS the ratified hunk.

    Pure function of the diff text so the rule can be exercised on a synthetic diff
    (see ``test_a_smuggled_hunk_wearing_the_marker_is_rejected``) and not only on
    whatever this checkout happens to contain.
    """
    hunks = [l for l in diff_text.splitlines() if l.startswith("@@")]
    added = _added_lines(diff_text)
    removed = [l for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---")]
    assert len(hunks) <= 1, f"more than one hunk in matching_engine.py:\n{diff_text}"
    assert removed == [], f"matching_engine.py must only ADD the NO-side hunk:\n{diff_text}"
    if not hunks:
        # byte-identical to the base: the deviation was reverted, which this rule
        # permits (the settlement behaviour is covered by
        # tests/test_weather_settlement_semantics.py, not here).
        assert added == [], f"added lines without a hunk header:\n{diff_text}"
        return
    at = _hunk_target(hunks[0])
    assert at == ENGINE_HUNK_AT, (
        f"the one allowed hunk is at {at}, not the ratified {ENGINE_HUNK_AT}:\n{diff_text}"
    )
    got = hashlib.sha256("\n".join(added).encode("utf-8")).hexdigest()
    assert got == ENGINE_HUNK_SHA256, (
        "matching_engine.py's one allowed hunk is NOT the ratified NO-side settlement "
        f"hunk (724d93c): sha256 {got} != {ENGINE_HUNK_SHA256}. Any other edit to this "
        "file is a governance violation, not a test to update -- see HANDOFF.md "
        f"2026-09-05 and PRD_STRATEGY_FACTORY.md Phase F3.\n{diff_text}"
    )
    # Redundant given the hash, kept because it names WHAT the hunk is for a reader
    # who lands on the assertion above.
    assert any(ENGINE_HUNK_MARKER in l for l in added), diff_text
    assert any("exit_price = 1.0 - exit_price" in l for l in added), diff_text


def test_engine_differs_only_by_the_no_side_settlement_hunk():
    _skip_unless_git_checkout()
    diff = _git("diff", "-U0", BASE_COMMIT, "--", PROTECTED[2])
    assert diff.returncode == 0, diff.stderr
    check_engine_diff(diff.stdout)


def test_an_empty_engine_diff_is_accepted():
    check_engine_diff("")


# A hunk that the pre-2026-09-05 allow-list accepted: ONE contiguous insertion, no
# removed lines, <=20 added, carrying the marker comment AND the substring
# ``exit_price = 1.0 - exit_price`` -- while also neutering the stop-loss sweep. The
# shape checks cannot tell it from the ratified hunk; the content hash can.
SMUGGLED_DIFF = """diff --git a/src/core/matching_engine.py b/src/core/matching_engine.py
index a814f50..deadbee 100644
--- a/src/core/matching_engine.py
+++ b/src/core/matching_engine.py
@@ -1401,0 +1402,10 @@ class SimulatedExchange:
+            # --- NO-SIDE SETTLEMENT (2026-09-04, F3 dry run finding) ---
+            # (copied wording; this hunk is NOT the ratified one)
+            if pos.get("contract_side") == "NO":
+                exit_price = 1.0 - exit_price
+            # and, while we are here, stop honouring the stop:
+            if pos.get("stop_loss") is not None:
+                pos["stop_loss"] = None
+            if pos.get("trailing_stop") is not None:
+                pos["trailing_stop"] = None
+
"""

# The same sabotage relocated onto the ratified insertion point, so the position
# check alone would not catch it either.
SMUGGLED_AT_THE_RIGHT_PLACE = SMUGGLED_DIFF.replace(
    "@@ -1401,0 +1402,10 @@", "@@ -2259,0 +2260,10 @@"
)


@pytest.mark.parametrize(
    "bad", [SMUGGLED_DIFF, SMUGGLED_AT_THE_RIGHT_PLACE],
    ids=["elsewhere_in_the_file", "at_the_ratified_line"],
)
def test_a_smuggled_hunk_wearing_the_marker_is_rejected(bad):
    """Shape is not identity: only the ratified bytes at the ratified place pass."""
    # it really does satisfy every check the old allow-list made
    added = _added_lines(bad)
    assert len([l for l in bad.splitlines() if l.startswith("@@")]) == 1
    assert [l for l in bad.splitlines() if l.startswith("-") and not l.startswith("---")] == []
    assert len(added) <= 20
    assert any(ENGINE_HUNK_MARKER in l for l in added)
    assert any("exit_price = 1.0 - exit_price" in l for l in added)
    # ... and is still rejected
    with pytest.raises(AssertionError):
        check_engine_diff(bad)


def test_protected_files_have_no_staged_changes():
    _skip_unless_git_checkout()
    staged = _git("diff", "--cached", "--stat", "--", *PROTECTED)
    assert staged.returncode == 0, staged.stderr
    assert staged.stdout.strip() == "", f"staged edits to protected files:\n{staged.stdout}"
