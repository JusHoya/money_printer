"""FR-F0.5: the search-frame loader refuses the sealed ladder roots.

``PRD_STRATEGY_FACTORY.md`` §4 A3 seals ``data/ladders_holdout`` (holdout-B,
2026-07-26..08-31) and ``data/ladders_2026-09`` (the M0 capture). These tests
pin the refusal at both gates -- ``kalshi_history.load_ladders`` and the
``ev_analysis`` frame builder -- and pin *how* it refuses: by path identity,
not string prefix, and by a ``SEALED`` marker that travels with a copy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
import pytest

from src.backtest import ev_analysis as ev
from src.backtest import sealed_roots as sr
from src.backtest.sealed_roots import (
    SEALED_LADDER_ROOTS,
    SealedDataError,
    assert_frame_not_sealed,
    assert_not_sealed,
    sealed_reason,
)
from src.data.kalshi_history import (
    LADDER_COLUMNS,
    LADDER_DIR,
    _load_ladders_unchecked,
    load_ladders,
)

REPO = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# Which roots are sealed
# ----------------------------------------------------------------------


def test_the_prd_names_exactly_these_two_roots():
    names = {p.name for p in SEALED_LADDER_ROOTS}
    assert names == {"ladders_holdout", "ladders_2026-09"}
    for p in SEALED_LADDER_ROOTS:
        assert p.parent == REPO / "data"


def test_the_default_root_is_not_sealed(tmp_path):
    assert sealed_reason(LADDER_DIR) is None
    assert assert_not_sealed(LADDER_DIR) == LADDER_DIR
    assert LADDER_DIR == REPO / "data" / "ladders"
    # An ordinary (empty) root still loads, with the right columns.
    df = load_ladders(tmp_path)
    assert df.empty and list(df.columns) == list(LADDER_COLUMNS)


# ----------------------------------------------------------------------
# load_ladders refuses
# ----------------------------------------------------------------------


@pytest.mark.parametrize("root", SEALED_LADDER_ROOTS, ids=lambda p: p.name)
def test_load_ladders_refuses_each_sealed_root(root):
    with pytest.raises(SealedDataError) as excinfo:
        load_ladders(root)
    msg = str(excinfo.value)
    assert root.name in msg
    assert "A3" in msg and "FR-F0.5" in msg


@pytest.mark.parametrize("root", SEALED_LADDER_ROOTS, ids=lambda p: p.name)
def test_refusal_does_not_depend_on_the_directory_existing(root):
    # Path identity, not a directory listing: an absent sealed root is still
    # refused, so a fresh clone before the backfill lands is protected too.
    with pytest.raises(SealedDataError):
        load_ladders(root / "KXHIGHNY")


def test_refusal_is_by_path_identity_not_string_prefix(tmp_path, monkeypatch):
    mirror = tmp_path / "repo"
    sealed = (mirror / "data" / "ladders_holdout", mirror / "data" / "ladders_2026-09")
    for p in sealed:
        (p / "KXHIGHNY").mkdir(parents=True)
    (mirror / "data" / "ladders").mkdir()
    monkeypatch.setattr(sr, "SEALED_LADDER_ROOTS", sealed)

    # A dotted path that *resolves* to the sealed root is refused ...
    with pytest.raises(SealedDataError):
        load_ladders(mirror / "data" / "ladders" / ".." / "ladders_holdout")
    # ... so is a series directory inside it ...
    with pytest.raises(SealedDataError):
        load_ladders(sealed[1] / "KXHIGHNY")
    # ... and a relative path from the repo root.
    monkeypatch.chdir(mirror)
    with pytest.raises(SealedDataError):
        load_ladders(Path("data") / "ladders_2026-09")
    if os.name == "nt":
        with pytest.raises(SealedDataError):
            load_ladders(Path("DATA") / "LADDERS_HOLDOUT")

    # But a sibling that merely *starts with* a sealed name is not sealed.
    scratch = mirror / "data" / "ladders_holdout_scratch"
    scratch.mkdir()
    assert sealed_reason(scratch) is None
    assert load_ladders(scratch).empty
    assert load_ladders(mirror / "data" / "ladders").empty


def test_a_sealed_marker_protects_a_copied_directory(tmp_path):
    copy = tmp_path / "somewhere_else" / "ladders_copy"
    (copy / "KXHIGHNY").mkdir(parents=True)
    (copy / sr.SEALED_MARKER).write_text("copied from data/ladders_holdout\n")

    with pytest.raises(SealedDataError) as excinfo:
        load_ladders(copy)
    assert sr.SEALED_MARKER in str(excinfo.value)
    # The marker on an ancestor seals the subtree.
    with pytest.raises(SealedDataError):
        load_ladders(copy / "KXHIGHNY")
    # A sibling without the marker is untouched.
    (tmp_path / "somewhere_else" / "open").mkdir()
    assert load_ladders(tmp_path / "somewhere_else" / "open").empty


def test_error_message_names_root_and_prd_clause():
    root = SEALED_LADDER_ROOTS[0]
    with pytest.raises(SealedDataError) as excinfo:
        assert_not_sealed(root)
    msg = str(excinfo.value)
    assert "ladders_holdout" in msg
    assert sr.PRD_CLAUSE in msg
    assert issubclass(SealedDataError, PermissionError)


# ----------------------------------------------------------------------
# The frame builder is a second gate
# ----------------------------------------------------------------------


def test_loader_stamps_its_origin_on_the_frame(tmp_path):
    df = load_ladders(tmp_path)
    assert df.attrs["ladder_root"] == str(tmp_path.resolve())


def test_frame_builder_refuses_a_frame_stamped_from_a_sealed_root():
    df = pd.DataFrame(columns=list(LADDER_COLUMNS))
    df.attrs["ladder_root"] = str(SEALED_LADDER_ROOTS[1])
    with pytest.raises(SealedDataError):
        assert_frame_not_sealed(df)
    # The guard is the first thing build_opportunity_frame does.
    with pytest.raises(SealedDataError):
        ev.build_opportunity_frame(df, df, df, None)
    # An unstamped frame (hand-built fixture) is not blocked by the guard.
    assert_frame_not_sealed(pd.DataFrame(columns=list(LADDER_COLUMNS)))


def test_unchecked_reader_output_is_still_caught_by_the_frame_gate(tmp_path):
    # The explicit unchecked entry (F4 holdout / --stats) reads, but what it
    # returns carries its origin, so the frame builder refuses it anyway.
    root = SEALED_LADDER_ROOTS[0]
    df = _load_ladders_unchecked(root)
    assert df.attrs["ladder_root"] == str(root.resolve())
    with pytest.raises(SealedDataError):
        ev.build_opportunity_frame(df, df, df, None)


def test_ev_analysis_root_entry_refuses_sealed_and_accepts_open(tmp_path):
    with pytest.raises(SealedDataError):
        ev.load_search_ladders(SEALED_LADDER_ROOTS[1])
    df = ev.load_search_ladders(tmp_path)
    assert df.empty
    assert df.attrs["ladder_root"] == str(tmp_path.resolve())


# ----------------------------------------------------------------------
# The existing report entry point still targets the open default root
# ----------------------------------------------------------------------


def test_go_no_go_still_targets_the_default_root():
    # Static check: scripts/go_no_go.py carries a nested-quote f-string that
    # only Python >= 3.12 (the lab image) parses, so it cannot be imported on
    # a 3.11 host. What matters here is that it calls the gated loader with
    # the default root and nothing else.
    src = (REPO / "scripts" / "go_no_go.py").read_text(encoding="utf-8")
    assert "from src.data.kalshi_history import load_ladders" in src
    assert re.search(r"^\s*ladders = load_ladders\(\)\s*$", src, re.M)
    assert not re.search(r"ladders_holdout|ladders_2026-09", src)
    assert load_ladders.__defaults__[0] == LADDER_DIR
    assert sealed_reason(LADDER_DIR) is None


# ---------------------------------------------------------------------------
# Content gate (red-team finding 2026-09-02): ``attrs`` do not survive
# ``pd.concat`` or a bare ``pd.read_csv`` of a copied CSV, so the path/marker
# gate alone is defeated by ``cp``. Dates are not.
# ---------------------------------------------------------------------------


def _ladder_rows(*dates):
    return pd.DataFrame(
        {
            "city": ["NY"] * len(dates),
            "target_date": list(dates),
            "market_ticker": [f"KXHIGHNY-X-{i}" for i, _ in enumerate(dates)],
        }
    )


def test_frame_gate_refuses_rows_dated_after_the_development_set():
    with pytest.raises(sr.SealedDataError, match="target_date > 2026-07-25"):
        assert_frame_not_sealed(_ladder_rows("2026-07-25", "2026-07-26"))


def test_frame_gate_accepts_rows_inside_the_development_set():
    assert_frame_not_sealed(_ladder_rows("2026-05-18", "2026-07-25"))


def test_frame_gate_catches_a_copied_holdout_csv_without_marker(tmp_path):
    src = Path("data/ladders_holdout/KXHIGHNY/2026-08-01.csv")
    if not src.exists():
        pytest.skip("holdout CSV not on disk")
    copy = tmp_path / "copy" / "KXHIGHNY"
    copy.mkdir(parents=True)
    (copy / "2026-08-01.csv").write_bytes(src.read_bytes())
    df = pd.read_csv(copy / "2026-08-01.csv")
    assert not df.attrs  # exactly the bypass the red team demonstrated
    with pytest.raises(sr.SealedDataError):
        assert_frame_not_sealed(df)


def test_frame_gate_survives_concat_dropping_attrs():
    dev = _ladder_rows("2026-07-01")
    dev.attrs["ladder_root"] = "data/ladders"
    sealed = _ladder_rows("2026-08-15")
    sealed.attrs["ladder_root"] = "data/ladders_holdout"
    merged = pd.concat([dev, sealed], ignore_index=True)
    with pytest.raises(sr.SealedDataError):
        assert_frame_not_sealed(merged)


def test_only_the_sealed_evaluation_attr_opens_the_content_gate():
    df = _ladder_rows("2026-08-15")
    df.attrs[sr.SEALED_EVALUATION_ATTR] = True
    assert_frame_not_sealed(df)  # F4's sanctioned path
    # ...and the flag does not travel through concat, by design.
    assert not pd.concat([df, _ladder_rows("2026-08-16")]).attrs.get(
        sr.SEALED_EVALUATION_ATTR
    )


def test_dev_set_cutoff_matches_the_prd_declaration():
    assert sr.DEV_SET_LAST_DATE == "2026-07-25"
