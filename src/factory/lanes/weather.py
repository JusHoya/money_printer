"""Weather lane (KXHIGH NY/CHI/LAX/MIA) -- the only READY lane (FACTORY_ARCHITECTURE section 1.1).

``build_opportunities`` runs the **unchanged** evaluator chain that
``scripts/go_no_go.py`` runs::

    ladders  = ev.load_search_ladders(root)              # sealed roots refused
    archive  = ev.load_forecast_archive(source)
    vintages = ev.forecast_vintage_table(ladders, archive_with_lag)
    wf       = ev.WalkForwardCalibrator(source, cities, embargo_days)
    probs    = ev.build_probability_table(ladders, vintages, wf, source, cfg)
    opp      = ev.build_opportunity_frame(ladders, probs, vintages, cfg)

with two parametrisations the evaluator does not expose as arguments:

* **input directories** -- ``forecast_archive_dir`` goes through
  ``CalibrationSource.forecast_csv`` (a proper parameter); ``truth_dir`` has
  no parameter on ``WalkForwardCalibrator`` (it reads the module global
  ``ev_analysis.WEATHER_TRUTH_DIR``), so it is set for the duration of the
  calibrator's construction only, under a lock, and restored. This is how
  the parity frame reads the pinned Phase-2 blobs.
* **availability lag** -- see ``frame.py``'s module docstring: the archive's
  ``init_ts`` join key is shifted forward by the lag before
  ``forecast_vintage_table`` so each snapshot takes the latest vintage with
  ``init + lag <= ts``; ``init_time_utc`` / ``lead_hours`` stay the archive's.

The result carries ``opp.attrs`` describing every input path so
``frame.from_opportunity_frame`` can hash them into provenance.
"""
from __future__ import annotations

import contextlib
import dataclasses
import glob
import logging
import os
import threading
from typing import Any, Dict, Optional, Tuple

from src.factory.lanes.base import (
    NOT_READY,
    READY,
    SEARCH_FLOOR_UNITS,
    Lane,
    LaneStatus,
)

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
DEFAULT_LADDER_ROOT = os.path.join(REPO_ROOT, "data", "ladders")
CITIES: Tuple[str, ...] = ("NY", "CHI", "LAX", "MIA")
CITY_STATION = {"NY": "KNYC", "CHI": "KMDW", "LAX": "KLAX", "MIA": "KMIA"}
#: Retention deadline for backfilling holdout-B (architecture section 4.1).
HOLDOUT_B_RETENTION_ETA = "2026-10-03"

_TRUTH_DIR_LOCK = threading.Lock()


def count_target_dates(root: str = DEFAULT_LADDER_ROOT) -> int:
    """Distinct ``<date>.csv`` stems across the series directories (no pandas)."""
    if not os.path.isdir(root):
        return 0
    stems = set()
    for p in glob.glob(os.path.join(root, "*", "*.csv")):
        stems.add(os.path.splitext(os.path.basename(p))[0])
    return len(stems)


@contextlib.contextmanager
def _truth_dir(ev, truth_dir: Optional[str]):
    """Temporarily point ``ev_analysis.WEATHER_TRUTH_DIR`` at ``truth_dir``."""
    if truth_dir is None:
        yield
        return
    with _TRUTH_DIR_LOCK:
        old = ev.WEATHER_TRUTH_DIR
        ev.WEATHER_TRUTH_DIR = str(truth_dir)
        try:
            yield
        finally:
            ev.WEATHER_TRUTH_DIR = old


def _source_for(ev, source: Any, forecast_archive_dir: Optional[str]):
    src = source
    if src is None:
        src = ev.GFS_MEX
    elif isinstance(src, str):
        by_name = {s.name: s for s in ev.CANDIDATE_SOURCES}
        if src not in by_name:
            raise ValueError(f"unknown forecast source {src!r}; have {sorted(by_name)}")
        src = by_name[src]
    if forecast_archive_dir is not None:
        src = dataclasses.replace(
            src,
            forecast_csv=os.path.join(forecast_archive_dir, f"forecast_series_{src.name}.csv"),
        )
    return src


def build_opportunities(
    source: Any = None,
    *,
    forecast_archive_dir: Optional[str] = None,
    truth_dir: Optional[str] = None,
    embargo_days: int = 1,
    contracts: int = 20,
    adverse_fill: float = 0.01,
    ladder_root: Optional[str] = None,
    availability_lag_min: int = 0,
    cities: Tuple[str, ...] = CITIES,
):
    """The unchanged evaluator chain -> ``ev_analysis`` opportunity frame (pandas).

    ``source`` is an ``ev_analysis.CalibrationSource`` or its name
    (``"gfs_mex"`` default, ``"gefs"``); ``ladder_root`` goes through
    ``ev.load_search_ladders`` so sealed roots raise ``SealedDataError``.
    ``availability_lag_min`` applies the section 4.2 item 3 vintage rule at
    the join (module docstring).
    """
    import pandas as pd

    import src.backtest.ev_analysis as ev

    logging.getLogger("src.calibration.probability_engine").setLevel(logging.ERROR)
    src = _source_for(ev, source, forecast_archive_dir)
    root = ladder_root or DEFAULT_LADDER_ROOT
    ladders = ev.load_search_ladders(root)
    if ladders.empty:
        raise ev.EVAnalysisError(f"no ladders under {root}")
    archive = ev.load_forecast_archive(src)

    lag = int(availability_lag_min)
    if lag < 0:
        raise ValueError("availability_lag_min must be >= 0")
    if lag:
        lagged = archive.copy()
        lagged["init_ts"] = lagged["init_ts"] + pd.Timedelta(minutes=lag)
        vintages = ev.forecast_vintage_table(ladders, lagged)
        base = ev.forecast_vintage_table(ladders, archive)
        lag_stats = _lag_stats(base, vintages)
    else:
        vintages = ev.forecast_vintage_table(ladders, archive)
        lag_stats = {"lag_snapshots_revintaged": 0, "lag_snapshots_dropped": 0,
                     "lag_snapshots_total": int(len(vintages))}

    cfg = ev.EVConfig(
        calibration_mode=ev.CALIB_WALK_FORWARD,
        contracts=int(contracts),
        adverse_fill_dollars=float(adverse_fill),
        embargo_days=int(embargo_days),
    )
    with _truth_dir(ev, truth_dir):
        wf = ev.WalkForwardCalibrator(src, tuple(cities), embargo_days=int(embargo_days))
        truth_root = ev.WEATHER_TRUTH_DIR
    probs = ev.build_probability_table(ladders, vintages, wf, src, cfg)
    opp = ev.build_opportunity_frame(ladders, probs, vintages, cfg)

    opp.attrs.update(
        {
            "source": src.name,
            "source_version": src.version,
            "forecast_csv": src.resolved_forecast_csv(),
            "truth_dir": truth_root,
            "truth_files": {
                c: os.path.join(truth_root, f"cli_daily_high_{CITY_STATION[c]}.csv")
                for c in cities
            },
            "calibration_dir": src.calibration_dir,
            "ladder_root": ladders.attrs.get("ladder_root", os.path.abspath(root)),
            "ev_config": dataclasses.asdict(cfg),
            "embargo_days": int(embargo_days),
            "availability_lag_min": lag,
            "vintage_rows": int(len(vintages)),
            "probability_rows": int(len(probs)),
            "probability_failures": list(probs.attrs.get("failures", [])),
            **lag_stats,
        }
    )
    return opp


def _lag_stats(base, lagged) -> Dict[str, int]:
    keys = ["city", "target_date", "ts_utc"]
    m = base[keys + ["init_time_utc"]].merge(
        lagged[keys + ["init_time_utc"]], on=keys, how="left", suffixes=("_0", "_lag")
    )
    dropped = m["init_time_utc_lag"].isna()
    changed = (~dropped) & (
        m["init_time_utc_0"].astype(str).to_numpy() != m["init_time_utc_lag"].astype(str).to_numpy()
    )
    return {
        "lag_snapshots_total": int(len(base)),
        "lag_snapshots_revintaged": int(changed.sum()),
        "lag_snapshots_dropped": int(dropped.sum()),
    }


class WeatherLane(Lane):
    name = "weather"
    independent_unit = "target_date"

    def __init__(self, ladder_root: Optional[str] = None) -> None:
        self.ladder_root = ladder_root or DEFAULT_LADDER_ROOT

    def status(self) -> LaneStatus:
        n = count_target_dates(self.ladder_root)
        if n >= SEARCH_FLOOR_UNITS:
            return LaneStatus(
                state=READY, n_units=n,
                reason=f"{n} development target_dates under data/ladders (>= {SEARCH_FLOOR_UNITS})",
                next_data_eta=HOLDOUT_B_RETENTION_ETA,
            )
        return LaneStatus(
            state=NOT_READY, n_units=n,
            reason=f"only {n} target_dates under {self.ladder_root}",
            next_data_eta=HOLDOUT_B_RETENTION_ETA,
        )

    def build_opportunities(self, source: Any = None, **kwargs):
        kwargs.setdefault("ladder_root", self.ladder_root)
        return build_opportunities(source, **kwargs)

    def build_frames(self, config):
        """parity (pinned inputs, lag 0) + search (hardened) + gefs twin."""
        from src.factory import fees as fees_mod
        from src.factory import frame as fr

        regime = fees_mod.load_regime(config.fee_regime_path)
        common = dict(
            embargo_days=config.embargo_days, contracts=config.contracts,
            adverse_fill=config.adverse_fill, ladder_root=config.ladder_root or self.ladder_root,
        )
        fa_dir, truth_dir = fr.materialise_parity_inputs(config.parity_dest, config.parity_inputs)
        opp_par = build_opportunities(
            config.source, forecast_archive_dir=fa_dir, truth_dir=truth_dir,
            availability_lag_min=0, **common,
        )
        parity = fr.from_opportunity_frame(
            opp_par, name="parity", availability_lag_min=0, truth_filter=False,
            sigma_cap=None, fold_sandbox_admissible=False, cutoff=config.cutoff,
            fee_regime=regime, adverse_fill=config.adverse_fill, contracts=config.contracts,
        )
        del opp_par
        hardening = dict(
            availability_lag_min=config.availability_lag_min, truth_filter=True,
            sigma_cap=config.sigma_cap, fold_sandbox_admissible=True, cutoff=config.cutoff,
            fee_regime=regime, adverse_fill=config.adverse_fill, contracts=config.contracts,
        )
        opp_s = build_opportunities(
            config.source, availability_lag_min=config.availability_lag_min, **common
        )
        search = fr.from_opportunity_frame(opp_s, name="search", **hardening)
        del opp_s
        opp_g = build_opportunities(
            "gefs", availability_lag_min=config.availability_lag_min, **common
        )
        twin = fr.build_gefs_twin(search, opp_g, **hardening)
        del opp_g
        prov = {
            "lane": self.name,
            "independent_unit": self.independent_unit,
            "config": config.as_dict(),
            "parity_pin": fr.load_parity_pin(config.parity_inputs),
            "git_rev": parity.provenance["git_rev"],
            "lab_lock_sha256": parity.provenance.get("lab_lock_sha256"),
            "frames": {
                n: f.provenance.get("frame_sha256")
                for n, f in (("parity", parity), ("search", search), ("gefs_twin", twin))
            },
        }
        return fr.FrameSet(parity=parity, search=search, gefs_twin=twin, provenance=prov)


__all__ = [
    "CITIES",
    "CITY_STATION",
    "DEFAULT_LADDER_ROOT",
    "HOLDOUT_B_RETENTION_ETA",
    "WeatherLane",
    "build_opportunities",
    "count_target_dates",
]
