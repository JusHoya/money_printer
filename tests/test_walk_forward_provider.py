"""``WalkForwardCalibrationProvider`` -- the frame's calibration, served live (F4 blocker, 2026-09-06).

Replay parity for every committed spec was proven under the frame's
``ev_analysis.WalkForwardCalibrator``; the bot built ``FrozenCalibrationProvider`` (60
discrepancies / 0.336 of ``p_yes`` for 0c4b20502f2daf65). The bot now builds the
provider ``spec.calibration.kind`` names. These tests pin the new provider to the
calibrator it replaces:

* ``kind``/``sha256``: what the construction guard reads;
* payload equality (whole dict and ``content_hash``) against the REAL calibrator for
  pinned (city, date) pairs -- the frame's payload, not an approximation of it;
* determinism across constructions;
* the no-lookahead rule: rows dated at or after the cutoff never enter a payload;
* the thin-history refusal and the missing-archive refusal (the maia failure mode);
* ``build_calibration_provider`` follows the spec's kind;
* the sandbox import graph never reaches the lab's pandas harness.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.calibration import forecast_calibration as fcal  # noqa: E402
from src.factory import promoted as P  # noqa: E402
from src.strategies import genome_strategy as gs  # noqa: E402

CAL_DIR = REPO_ROOT / "data" / "calibration"
FCSV = REPO_ROOT / "data" / "forecast_archive" / "forecast_series_gfs_mex.csv"
TRUTH_DIR = REPO_ROOT / "data" / "weather_truth"
SEED = "0c4b20502f2daf65"
CITIES = ("NY", "CHI", "LAX", "MIA")
#: (city, target_date) pairs spread over the frame's ladder range 2026-05-18..2026-07-25,
#: chosen so every day-of resolution branch is exercised: month block insufficient
#: (May-18: 17 May days), season fallback, and a full month block (Jul-25).
PINNED = (("NY", "2026-05-18"), ("CHI", "2026-06-15"), ("LAX", "2026-07-10"), ("MIA", "2026-07-25"))

pytestmark = pytest.mark.skipif(
    not (FCSV.exists() and CAL_DIR.exists() and all((TRUTH_DIR / f"cli_daily_high_{s}.csv").exists()
                                                      for s in ("KNYC", "KMDW", "KLAX", "KMIA"))),
    reason="forecast archive / CLI truth / calibration dir not present",
)


def _spec():
    path = P.resolve_path(SEED, P.PROMOTED_DIR)
    if not os.path.exists(path):
        pytest.skip("promoted seed spec not present")
    return P.load_promoted(SEED)


def _provider(**kw):
    return gs.WalkForwardCalibrationProvider(str(CAL_DIR), source="gfs_mex", **kw)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def test_kind_and_directory_identity_are_what_the_guard_reads():
    spec = _spec()
    prov = _provider()
    assert prov.kind == "walk_forward" == spec.calibration.kind
    assert prov.sha256 == spec.calibration.sha256 == P.calibration_dir_sha256(str(CAL_DIR))
    d = prov.describe()
    assert d["kind"] == "walk_forward" and d["embargo_days"] == 1 and d["min_paired_days"] == 60
    assert len(d["forecast_sha256"]) == 64 and set(d["truth_sha256"]) == {"KNYC", "KMDW", "KLAX", "KMIA"}


def test_city_maps_equal_the_evaluators():
    ev = pytest.importorskip("src.backtest.ev_analysis")
    from src.data.forecast_vintage_provider import CITY_STATION

    assert gs.CITY_TZ == ev.CITY_TZ
    assert CITY_STATION == ev.CITY_STATION


# ---------------------------------------------------------------------------
# the frame's payload, byte for byte
# ---------------------------------------------------------------------------
def test_payload_equals_the_frames_calibrator_for_pinned_dates():
    pytest.importorskip("pandas")
    import src.backtest.ev_analysis as ev

    wf = ev.WalkForwardCalibrator(ev.GFS_MEX, CITIES, embargo_days=1)
    prov = _provider()
    for city, td in PINNED:
        frame_payload = wf.calibration_as_of(city, td)
        live_payload = prov.payload_for(city, td)
        assert live_payload == frame_payload, (city, td)
        assert live_payload["content_hash"] == frame_payload["content_hash"]
        assert fcal.verify_content_hash(live_payload)
        # the calibrator's inputs block is the WHOLE archive, its coverage is the subset
        assert live_payload["coverage"]["last_target_date"] <= prov.cutoff_for(td)


def test_payload_past_the_archive_end_is_the_fit_as_of_the_archives_last_day():
    pytest.importorskip("pandas")
    import src.backtest.ev_analysis as ev

    prov = _provider()
    last = prov.archive_last_target_date("NY")
    assert last is not None
    beyond = "2099-01-01"
    p = prov.payload_for("NY", beyond)
    assert p["coverage"]["last_target_date"] == last
    wf = ev.WalkForwardCalibrator(ev.GFS_MEX, CITIES, embargo_days=1)
    assert p["content_hash"] == wf.calibration_as_of("NY", beyond)["content_hash"]


def test_deterministic_across_two_constructions():
    a, b = _provider(), _provider()
    assert a.describe() == b.describe()
    for city, td in PINNED:
        assert a.payload_for(city, td) == b.payload_for(city, td)
        assert a.payload_for(city, td) is a.payload_for(city, td)  # cached per (city, date)


# ---------------------------------------------------------------------------
# no lookahead: rows at/after the cutoff never enter
# ---------------------------------------------------------------------------
_STATS_KEYS = ("day_of", "by_lead", "by_month_day_of", "by_season_day_of", "coverage")


def _perturbed_archive(tmp_path: Path, city: str, from_date: str):
    """Copy the archives, then corrupt every row of ``city`` dated >= ``from_date``."""
    fdir = tmp_path / "forecast_archive"
    tdir = tmp_path / "weather_truth"
    fdir.mkdir()
    tdir.mkdir()
    station = {"NY": "KNYC", "CHI": "KMDW", "LAX": "KLAX", "MIA": "KMIA"}[city]
    with open(FCSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0].keys())
    touched = 0
    for r in rows:
        if r["city"] == city and r["target_date"] >= from_date:
            r["forecast_high_f"] = str(float(r["forecast_high_f"]) + 50.0)
            touched += 1
    assert touched > 0, "the archive must carry rows at/after the cutoff for this test to bite"
    with open(fdir / FCSV.name, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    for st in ("KNYC", "KMDW", "KLAX", "KMIA"):
        src = TRUTH_DIR / f"cli_daily_high_{st}.csv"
        if st != station:
            shutil.copy(src, tdir / src.name)
            continue
        with open(src, newline="", encoding="utf-8") as fh:
            trows = list(csv.DictReader(fh))
            tfields = list(trows[0].keys())
        for r in trows:
            if r["date"] >= from_date and r["high"]:
                r["high"] = str(int(r["high"]) - 40)
        with open(tdir / src.name, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=tfields)
            w.writeheader()
            w.writerows(trows)
    return str(fdir / FCSV.name), str(tdir)


def test_rows_at_or_after_the_cutoff_never_enter_a_payload(tmp_path):
    """A payload for T is fitted on target_date <= T-1 only: corrupting everything from T-1+1 on
    (forecast AND truth) changes nothing the engine reads -- exactly the frame's rule."""
    city, td = "CHI", "2026-06-15"
    real = _provider()
    cutoff = real.cutoff_for(td)
    assert cutoff == "2026-06-14"
    fcsv, tdir = _perturbed_archive(tmp_path, city, from_date=td)  # rows dated >= T corrupted
    perturbed = gs.WalkForwardCalibrationProvider(str(CAL_DIR), source="gfs_mex", forecast_csv=fcsv, truth_dir=tdir)
    a, b = real.payload_for(city, td), perturbed.payload_for(city, td)
    for k in _STATS_KEYS:
        assert a[k] == b[k], k
    assert a["coverage"]["last_target_date"] == cutoff
    # and the whole-archive fingerprints DID move, proving the corruption was read
    assert a["inputs"]["forecast_content_sha256"] != b["inputs"]["forecast_content_sha256"]
    assert a["inputs"]["truth_content_sha256"] != b["inputs"]["truth_content_sha256"]
    # a corruption strictly before the cutoff, by contrast, is visible
    (tmp_path / "before").mkdir(exist_ok=True)
    fcsv2, tdir2 = _perturbed_archive(tmp_path / "before", city, from_date=cutoff)
    seen = gs.WalkForwardCalibrationProvider(str(CAL_DIR), source="gfs_mex", forecast_csv=fcsv2, truth_dir=tdir2)
    assert seen.payload_for(city, td)["day_of"] != a["day_of"]


def test_embargo_two_withholds_one_more_day():
    p1 = _provider(embargo_days=1).payload_for("NY", "2026-07-25")
    p2 = _provider(embargo_days=2).payload_for("NY", "2026-07-25")
    assert p1["coverage"]["last_target_date"] == "2026-07-24"
    assert p2["coverage"]["last_target_date"] == "2026-07-23"
    assert p1["content_hash"] != p2["content_hash"]


# ---------------------------------------------------------------------------
# archive identity: CRLF-normalised, keyed by station, equal to the spec's pins
# ---------------------------------------------------------------------------
def _crlf_copy(tmp_path: Path):
    """The archives re-written with CRLF line endings (a Windows autocrlf checkout)."""
    fdir = tmp_path / "forecast_archive"
    tdir = tmp_path / "weather_truth"
    fdir.mkdir()
    tdir.mkdir()
    for src, dst in [(FCSV, fdir / FCSV.name)] + [
        (TRUTH_DIR / f"cli_daily_high_{s}.csv", tdir / f"cli_daily_high_{s}.csv") for s in ("KNYC", "KMDW", "KLAX", "KMIA")
    ]:
        dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    return str(fdir / FCSV.name), str(tdir)


def test_archive_shas_are_crlf_normalised_and_match_the_frame_pins(tmp_path):
    """Red team 2026-09-06: raw-byte hashing made an LF checkout with a CRLF provider root
    abort parity on byte-identical content. The provider's shas must be a property of the
    content only -- ``fees.sha256_file`` -- so a CRLF copy reports the same shas AND the
    same payload content_hash."""
    from src.factory.fees import sha256_file

    real = _provider()
    fcsv, tdir = _crlf_copy(tmp_path)
    crlf = gs.WalkForwardCalibrationProvider(str(CAL_DIR), source="gfs_mex", forecast_csv=fcsv, truth_dir=tdir)
    assert crlf.forecast_sha256 == real.forecast_sha256 == sha256_file(str(FCSV))
    assert crlf.truth_sha256 == real.truth_sha256
    assert set(real.truth_sha256) == {"KNYC", "KMDW", "KLAX", "KMIA"}  # keyed by STATION, like the spec
    for city, td in PINNED:
        assert crlf.payload_for(city, td)["content_hash"] == real.payload_for(city, td)["content_hash"]
    # and they are the pins the committed spec carries (== the frame provenance's)
    spec = _spec()
    assert real.forecast_sha256 == spec.calibration.forecast_sha256
    assert real.truth_sha256 == dict(spec.calibration.truth_sha256)
    # a raw-byte hash of the CRLF copy is a DIFFERENT number -- the trap being closed
    import hashlib

    assert hashlib.sha256(Path(fcsv).read_bytes()).hexdigest() != real.forecast_sha256


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_thin_history_is_refused_as_a_runtime_error_the_strategy_skips_on():
    prov = _provider()
    with pytest.raises(fcal.CalibrationError, match="Refusing to price") as ei:
        prov.payload_for("NY", "2026-01-15")
    assert isinstance(ei.value, RuntimeError)  # GenomeStrategy._analyze catches RuntimeError -> skip


def test_missing_archive_names_the_deploy_step(tmp_path):
    with pytest.raises(fcal.CalibrationError, match="deploy_f3_shadow.sh step 2b"):
        gs.WalkForwardCalibrationProvider(str(CAL_DIR), forecast_csv=str(tmp_path / "nope.csv"))
    with pytest.raises(fcal.CalibrationError, match="deploy_f3_shadow.sh step 2b"):
        gs.WalkForwardCalibrationProvider(str(CAL_DIR), truth_dir=str(tmp_path))


def test_unknown_city_is_refused():
    with pytest.raises(fcal.CalibrationError, match="not one of the calibrated cities"):
        _provider().payload_for("DEN", "2026-07-25")


# ---------------------------------------------------------------------------
# build_calibration_provider follows the spec
# ---------------------------------------------------------------------------
def _respec(spec, kind):
    doc = spec.to_doc(with_hash=False)
    if kind is None:
        doc["calibration"].pop("kind", None)
    else:
        doc["calibration"]["kind"] = kind
    doc["spec_hash"] = P.spec_hash_of(doc)
    return P.from_doc(doc)


def test_build_calibration_provider_follows_the_specs_kind():
    spec = _spec()
    wf = gs.build_calibration_provider(spec, str(CAL_DIR))
    assert isinstance(wf, gs.WalkForwardCalibrationProvider) and wf.kind == spec.calibration.kind
    fz = gs.build_calibration_provider(_respec(spec, "frozen"), str(CAL_DIR))
    assert isinstance(fz, gs.FrozenCalibrationProvider) and fz.kind == "frozen"
    unproven = gs.build_calibration_provider(_respec(spec, None), str(CAL_DIR))
    assert isinstance(unproven, gs.FrozenCalibrationProvider)  # unchanged behaviour; the guard refuses paper
    with pytest.raises(gs.GenomeSpecMismatch, match="names no calibration provider"):
        gs.build_calibration_provider(_respec(spec, "bogus"), str(CAL_DIR))
    assert wf.sha256 == fz.sha256 == spec.calibration.sha256  # same dir identity, different payloads


def test_strategy_constructs_with_the_walk_forward_provider_and_kind_ok():
    from src.data.forecast_vintage_provider import ForecastVintageProvider
    from src.factory.fees import load_regime

    spec = _spec()
    prov = gs.build_calibration_provider(spec, str(CAL_DIR))
    import datetime as _dt

    clock = lambda: _dt.datetime(2026, 7, 19, 15, 0, tzinfo=_dt.timezone.utc)  # noqa: E731
    strat = gs.GenomeStrategy(
        spec, clock=clock, forecast_provider=ForecastVintageProvider.from_rows([], lag_min=spec.availability_lag_min),
        fee_regime=load_regime(), calibration_provider=prov,
    )
    assert strat.calibration_kind == "walk_forward" and strat.calibration_kind_ok is True
    assert strat.archive_pins_ok is True and strat.archive_pins_detail is None
    assert strat.archive_pins_live["forecast_sha256"] == spec.calibration.forecast_sha256


def test_paper_refuses_and_shadow_warns_when_the_archives_are_not_the_pinned_ones(tmp_path, caplog):
    """The red team's attack (2026-09-06): +15 F on 61 NY truth rows under a redirected root.
    Dir sha and kind still agree; the ARCHIVE pins do not."""
    import datetime as _dt
    import logging

    from src.data.forecast_vintage_provider import ForecastVintageProvider
    from src.factory.fees import load_regime
    from src.utils.logger import logger as mp_logger

    spec = _spec()
    fcsv, tdir = _perturbed_archive(tmp_path, "NY", from_date="2026-01-01")  # every NY row, both archives
    prov = gs.WalkForwardCalibrationProvider(str(CAL_DIR), source="gfs_mex", forecast_csv=fcsv, truth_dir=tdir)
    assert prov.sha256 == spec.calibration.sha256 and prov.kind == spec.calibration.kind  # the old guard is blind
    clock = lambda: _dt.datetime(2026, 7, 19, 15, 0, tzinfo=_dt.timezone.utc)  # noqa: E731
    kw = dict(clock=clock, forecast_provider=ForecastVintageProvider.from_rows([], lag_min=spec.availability_lag_min),
              fee_regime=load_regime(), calibration_provider=prov)
    # shadow: loud line, run continues, condition readable on the strategy
    caplog.set_level(logging.INFO, logger=mp_logger.name)
    mp_logger.addHandler(caplog.handler)
    try:
        strat = gs.GenomeStrategy(spec, **kw)
    finally:
        mp_logger.removeHandler(caplog.handler)
    assert strat.calibration_kind_ok is True and strat.archive_pins_ok is False
    assert "KNYC" in strat.archive_pins_detail and "forecast archive sha" in strat.archive_pins_detail
    assert any("ARCHIVE PIN MISMATCH (shadow)" in r.getMessage() for r in caplog.records)
    # paper: refused outright
    doc = spec.to_doc(with_hash=False)
    doc["mode"], doc["registry_status"] = "paper", "PROPOSED"
    doc["spec_hash"] = P.spec_hash_of(doc)
    with pytest.raises(gs.GenomeSpecMismatch, match="archives the spec did not pin"):
        gs.GenomeStrategy(P.from_doc(doc), **kw)
    # paper + a walk-forward spec that pins nothing: also refused (silence is not proof)
    doc["calibration"]["forecast_sha256"] = None
    doc["calibration"]["truth_sha256"] = None
    doc["spec_hash"] = P.spec_hash_of(doc)
    good = gs.build_calibration_provider(P.from_doc(doc), str(CAL_DIR))
    with pytest.raises(gs.GenomeSpecMismatch, match="pins no archives"):
        gs.GenomeStrategy(P.from_doc(doc), **dict(kw, calibration_provider=good))
    # frozen provider: pins are not applicable, nothing to refuse
    doc["calibration"]["kind"] = "frozen"
    doc["spec_hash"] = P.spec_hash_of(doc)
    fz = gs.GenomeStrategy(P.from_doc(doc), **dict(kw, calibration_provider=gs.FrozenCalibrationProvider(str(CAL_DIR))))
    assert fz.archive_pins_ok is None and fz.calibration_kind_ok is True


# ---------------------------------------------------------------------------
# import graph
# ---------------------------------------------------------------------------
def test_provider_never_imports_the_lab_harness():
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "for m in ('scipy', 'lightgbm', 'xgboost', 'torch'):\n"
        "    sys.modules[m] = None\n"
        "from src.strategies.genome_strategy import WalkForwardCalibrationProvider\n"
        "p = WalkForwardCalibrationProvider(%r, source='gfs_mex')\n"
        "p.payload_for('NY', '2026-07-25')\n"
        "bad = sorted(m for m in sys.modules if m.startswith('src.backtest') or m == 'src.data.kalshi_provider')\n"
        "print('BAD=' + repr(bad))\n"
    ) % (str(REPO_ROOT), str(CAL_DIR))
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True,
                          timeout=180, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("BAD="))
    assert line == "BAD=[]", line
