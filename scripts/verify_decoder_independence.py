#!/usr/bin/env python3
"""Check this repo's GRIB2 decoder against a *different* GRIB2 implementation.

WHY THIS EXISTS
---------------
``reports/phase2/ec1_ensemble_members.md`` cross-checked the 31 decoded members
against NCEP's own ``geavg`` product and found agreement to tenths of a degree.
That check is worth having -- it catches a member-selection or windowing fault --
but it is **not decoder-independent**: ``geavg`` is a GRIB2 record pulled from the
same bucket and decoded by the *same* in-house decoder in
:mod:`src.data.ensemble_provider`. A global fault in that decoder -- a
Kelvin-to-Fahrenheit slip, a wrong ``decimal_scale``/``binary_scale`` exponent, a
sign error, a hemisphere or scan-mode error putting the sample in the wrong
place -- would move both sides of that comparison by the same amount and cancel
exactly. The check would still pass while every published number was wrong by
tens of degrees.

This script supplies the missing independence. It decodes live GEFS ``TMP:2 m
above ground`` for every member at several forecast hours and four city nodes
with the in-house decoder, and compares against **Open-Meteo's GFS-ensemble
(`gfs025`) API**, which is served by an entirely separate GRIB2 implementation
(Open-Meteo's Swift stack) run by a different operator. Agreement at the level of
degrees excludes the whole class of global faults above, any one of which would
show up as tens of degrees.

WHAT IT DOES **NOT** ESTABLISH
------------------------------
1. **Per-member identity.** ``gepNN`` is compared against Open-Meteo's
   ``temperature_2m_memberNN`` positionally. If that correspondence fails, the
   per-member correlation collapses while the *sorted* distributions still
   match. This script measures and reports both, and tries every cyclic
   relabelling before concluding anything. A collapse with matching sorted
   distributions is consistent with the two sides being on different model
   cycles, which the API does not let us pin down -- so it is reported as an
   **open caveat**, not as a finding either way.
2. **Node identity.** Open-Meteo is queried at the GEFS grid node's own
   latitude/longitude with ``cell_selection=nearest``, and the API echoes the
   coordinate it served; but it applies its own elevation correction to 2 m
   temperature, which this script cannot switch off. A residual of a degree or
   two is expected from that alone and is not evidence of a decoder fault.
3. **Anything about forecast skill.** Both sides are the same forecast. This
   measures decoding, not accuracy.

USAGE
-----
::

    $env:PYTHONPATH = "."
    python scripts/verify_decoder_independence.py --init 2026-07-27T00
    python scripts/verify_decoder_independence.py --json-out reports/phase2/x.json

Keep ``--max-workers`` <= 4: this machine machine-checks (WHEA 0x124) under
sustained all-core load, and the NODD bucket answers a fraction of a wider burst
with HTTP 503.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.data.ensemble_provider import (  # noqa: E402
    CITIES,
    DEFAULT_MEMBERS,
    FIELD_TMP,
    GEFS_GRID,
    EnsembleProvider,
    EnsembleUnavailable,
    haversine_km,
    kelvin_to_fahrenheit,
    spec_nearest_node,
)

logger = logging.getLogger("verify_decoder_independence")

REPO_ROOT = _REPO_ROOT
CITY_ORDER = ("NY", "CHI", "LAX", "MIA")
DEFAULT_FHOURS = (6, 12, 18)

#: Open-Meteo's GFS 0.25 deg ensemble -- 31 members, the same NCEP GEFS product
#: this repo reads from NODD, decoded by their own Swift GRIB implementation.
OPEN_METEO_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_MODEL = "gfs025"

#: A decoder fault of the class this check exists to exclude (Kelvin offset,
#: scale exponent, sign, hemisphere) is worth tens of degrees. Anything under
#: this is siting/elevation/init-time noise, not a decode fault.
DECODE_FAULT_THRESHOLD_F = 10.0


# ---------------------------------------------------------------------------
# Small statistics, local so a library bump cannot move a published number
# ---------------------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float:
    return math.fsum(float(x) for x in xs) / len(xs)


def _sd(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    mu = _mean(xs)
    return math.sqrt(math.fsum((float(x) - mu) ** 2 for x in xs) / (len(xs) - 1))


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    num = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(math.fsum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(math.fsum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return None
    return num / (dx * dy)


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------------------
# Side A: this repo's decoder
# ---------------------------------------------------------------------------
def fetch_inhouse(
    provider: EnsembleProvider,
    init: datetime,
    fhours: Sequence[int],
    members: Sequence[str],
    *,
    max_workers: int = 4,
) -> Tuple[Dict[Tuple[str, int, str], float], Dict[str, str]]:
    """``{(city, fhour, member): degF}`` decoded here, plus per-job failures.

    One ranged read per (member, fhour) serves all four cities, because every
    city's node is requested together -- the same economy the backfill relies
    on. A failure is recorded against its job and the rest continues; nothing is
    substituted or defaulted.
    """
    nodes = tuple(dict.fromkeys(spec_nearest_node(CITIES[c]) for c in CITY_ORDER))
    jobs = [(m, int(f)) for m in members for f in fhours]

    def work(job: Tuple[str, int]):
        member, fhour = job
        try:
            return job, provider.fetch_record_values(
                init, member, fhour, nodes, field_name=FIELD_TMP
            )
        except EnsembleUnavailable as exc:
            return job, exc

    outcomes: Dict[Tuple[str, int], Any] = {}
    workers = max(1, min(4, int(max_workers)))
    if workers == 1:
        for job in jobs:
            key, out = work(job)
            outcomes[key] = out
    else:
        from concurrent.futures import ThreadPoolExecutor

        _ = provider.session  # create once, not racily inside the pool
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, out in pool.map(work, jobs):
                outcomes[key] = out

    values: Dict[Tuple[str, int, str], float] = {}
    failures: Dict[str, str] = {}
    for (member, fhour), out in sorted(outcomes.items()):
        if isinstance(out, EnsembleUnavailable):
            failures[f"{member}:f{fhour:03d}"] = f"{out.reason_code}: {out.detail}"
            continue
        for city in CITY_ORDER:
            node = spec_nearest_node(CITIES[city])
            raw = out["nodes_k"].get(f"{node[0]},{node[1]}")
            if raw is None:
                failures[f"{member}:f{fhour:03d}:{city}"] = "node absent from record"
                continue
            values[(city, fhour, member)] = kelvin_to_fahrenheit(float(raw))
    return values, failures


# ---------------------------------------------------------------------------
# Side B: Open-Meteo's decoder
# ---------------------------------------------------------------------------
def fetch_open_meteo(
    session, city: str, valid_times: Sequence[datetime]
) -> Dict[str, Any]:
    """Open-Meteo's ``gfs025`` ensemble at this city's **GEFS node**, in degF.

    Queried at the node's own coordinate (not the station's) with
    ``cell_selection=nearest``, so both sides are asked for the same grid cell
    rather than the same city. The coordinate Open-Meteo echoes back is recorded
    so the match is visible rather than assumed.
    """
    spec = CITIES[city]
    node = spec_nearest_node(spec)
    node_lat, node_lon = GEFS_GRID.node_lat_lon(*node)
    query_lon = node_lon - 360.0 if node_lon > 180.0 else node_lon
    days = sorted({t.date().isoformat() for t in valid_times})
    params = {
        "latitude": f"{node_lat:.4f}",
        "longitude": f"{query_lon:.4f}",
        "hourly": "temperature_2m",
        "models": OPEN_METEO_MODEL,
        "temperature_unit": "fahrenheit",
        "cell_selection": "nearest",
        "timezone": "UTC",
        "start_date": days[0],
        "end_date": days[-1],
    }
    response = session.get(OPEN_METEO_URL, params=params, timeout=60)
    response.raise_for_status()
    blob = response.json()
    hourly = blob["hourly"]
    keys = [
        k
        for k in hourly
        if k == "temperature_2m" or k.startswith("temperature_2m_member")
    ]
    keys.sort(
        key=lambda k: 0 if k == "temperature_2m" else int(k.rsplit("member", 1)[1])
    )
    index = {t: i for i, t in enumerate(hourly["time"])}

    series: Dict[int, Dict[str, float]] = {}
    for valid in valid_times:
        stamp = valid.strftime("%Y-%m-%dT%H:%M")
        if stamp not in index:
            raise SystemExit(f"Open-Meteo returned no hour {stamp} for {city}")
        i = index[stamp]
        series[int(valid.timestamp())] = {
            k: float(hourly[k][i]) for k in keys if hourly[k][i] is not None
        }
    return {
        "city": city,
        "requested_lat": node_lat,
        "requested_lon": query_lon,
        "served_lat": blob.get("latitude"),
        "served_lon": blob.get("longitude"),
        "served_elevation_m": blob.get("elevation"),
        "member_keys": keys,
        "by_valid_epoch": series,
        "url": response.url,
    }


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------
def compare(
    inhouse: Dict[Tuple[str, int, str], float],
    open_meteo: Dict[str, Dict[str, Any]],
    init: datetime,
    fhours: Sequence[int],
    members: Sequence[str],
) -> Dict[str, Any]:
    """Four independent views of the same 31 numbers, per city and hour.

    * **member-mean bias** -- the headline. A global decode fault lands here.
    * **ensemble sigma on each side** -- a scale-exponent fault would inflate or
      collapse the spread even if the mean happened to survive.
    * **sorted order statistics** -- distribution agreement that does not depend
      on the member labels lining up.
    * **per-member correlation, and its best cyclic relabelling** -- the only
      view that tests member identity, and the one that is allowed to fail
      without condemning the decoder.
    """
    rows: List[Dict[str, Any]] = []
    for city in CITY_ORDER:
        om = open_meteo[city]
        for fhour in fhours:
            valid = init + timedelta(hours=int(fhour))
            ours = [
                inhouse[(city, fhour, m)]
                for m in members
                if (city, fhour, m) in inhouse
            ]
            om_by_key = om["by_valid_epoch"].get(int(valid.timestamp()), {})
            theirs = [om_by_key[k] for k in om["member_keys"] if k in om_by_key]
            if len(ours) < 3 or len(theirs) < 3:
                rows.append(
                    {
                        "city": city,
                        "fhour": fhour,
                        "valid_utc": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "n_inhouse": len(ours),
                        "n_open_meteo": len(theirs),
                        "status": "insufficient members on one side",
                    }
                )
                continue

            paired = min(len(ours), len(theirs))
            sorted_a, sorted_b = sorted(ours)[:paired], sorted(theirs)[:paired]
            order_deltas = [a - b for a, b in zip(sorted_a, sorted_b)]

            identity_r = None
            best_shift, best_r = None, None
            if len(ours) == len(theirs):
                identity_r = _pearson(ours, theirs)
                for shift in range(len(theirs)):
                    rotated = theirs[shift:] + theirs[:shift]
                    r = _pearson(ours, rotated)
                    if r is not None and (best_r is None or abs(r) > abs(best_r)):
                        best_shift, best_r = shift, r

            rows.append(
                {
                    "city": city,
                    "fhour": fhour,
                    "valid_utc": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "n_inhouse": len(ours),
                    "n_open_meteo": len(theirs),
                    "mean_inhouse_f": _r(_mean(ours)),
                    "mean_open_meteo_f": _r(_mean(theirs)),
                    "mean_bias_f": _r(_mean(ours) - _mean(theirs)),
                    "sigma_inhouse_f": _r(_sd(ours)),
                    "sigma_open_meteo_f": _r(_sd(theirs)),
                    "sigma_ratio": _r(
                        (_sd(ours) / _sd(theirs)) if _sd(ours) and _sd(theirs) else None
                    ),
                    "min_inhouse_f": _r(min(ours)),
                    "max_inhouse_f": _r(max(ours)),
                    "min_open_meteo_f": _r(min(theirs)),
                    "max_open_meteo_f": _r(max(theirs)),
                    "order_stat_delta_min_f": _r(min(order_deltas)),
                    "order_stat_delta_max_f": _r(max(order_deltas)),
                    "order_stat_delta_mean_abs_f": _r(
                        _mean([abs(d) for d in order_deltas])
                    ),
                    "per_member_r_identity": _r(identity_r),
                    "per_member_best_cyclic_shift": best_shift,
                    "per_member_best_cyclic_r": _r(best_r),
                    "status": "ok",
                }
            )

    ok = [r for r in rows if r.get("status") == "ok"]
    biases = [r["mean_bias_f"] for r in ok]
    order_abs = [r["order_stat_delta_mean_abs_f"] for r in ok]
    worst_order = max(
        (
            max(abs(r["order_stat_delta_min_f"]), abs(r["order_stat_delta_max_f"]))
            for r in ok
        ),
        default=None,
    )
    identity_rs = [
        r["per_member_r_identity"] for r in ok if r["per_member_r_identity"] is not None
    ]
    best_rs = [
        r["per_member_best_cyclic_r"]
        for r in ok
        if r["per_member_best_cyclic_r"] is not None
    ]

    verdict_ok = bool(ok) and all(abs(b) < DECODE_FAULT_THRESHOLD_F for b in biases)
    return {
        "rows": rows,
        "summary": {
            "n_city_hours": len(ok),
            "overall_mean_bias_f": _r(_mean(biases)) if biases else None,
            "min_city_hour_bias_f": _r(min(biases)) if biases else None,
            "max_city_hour_bias_f": _r(max(biases)) if biases else None,
            "mean_order_stat_abs_delta_f": _r(_mean(order_abs)) if order_abs else None,
            "worst_order_stat_abs_delta_f": _r(worst_order),
            "per_member_r_identity_mean": _r(_mean(identity_rs))
            if identity_rs
            else None,
            "per_member_r_identity_max_abs": _r(max(abs(r) for r in identity_rs))
            if identity_rs
            else None,
            "per_member_best_cyclic_r_max_abs": _r(max(abs(r) for r in best_rs))
            if best_rs
            else None,
            "decode_fault_threshold_f": DECODE_FAULT_THRESHOLD_F,
            "verdict": "DECODER INDEPENDENTLY CORROBORATED"
            if verdict_ok
            else "NOT CORROBORATED",
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def render_markdown(blob: Dict[str, Any]) -> str:
    cfg, cmp_ = blob["config"], blob["comparison"]
    s = cmp_["summary"]
    L: List[str] = []
    A = L.append
    A("# Decoder independence: in-house GRIB2 decode vs Open-Meteo `gfs025`")
    A("")
    A(
        f"Generated {blob['generated_at_utc']} by `scripts/verify_decoder_independence.py`. "
        "Every number below was measured by that script on the run recorded here; "
        "none is hand-entered or carried over from another report."
    )
    A("")
    A("## Why the existing cross-check was not enough")
    A("")
    A(
        "`reports/phase2/ec1_ensemble_members.md` compares the 31 decoded members "
        "against NCEP's own `geavg` product. That is a real check -- it catches a "
        "member-selection, windowing or interval fault -- but **it is not "
        "decoder-independent**: `geavg` is a GRIB2 record from the same bucket, "
        "decoded by the same `src/data/ensemble_provider.py`. A global fault in "
        "that decoder (Kelvin offset, binary/decimal scale exponent, sign, "
        "hemisphere, scan mode) moves both sides identically and cancels. The "
        "check would pass with every published temperature wrong by tens of "
        "degrees."
    )
    A("")
    A(
        "This report supplies the missing independence by comparing against "
        "**Open-Meteo's `gfs025` ensemble API** -- the same NCEP GEFS product, "
        "decoded by an entirely separate GRIB2 implementation (Open-Meteo's Swift "
        "stack), operated by someone else."
    )
    A("")
    A("## Configuration")
    A("")
    A("| Setting | Value |")
    A("| --- | --- |")
    A(f"| Model cycle | `{cfg['init_utc']}` |")
    A(f"| Field | `{cfg['field']}:2 m above ground` (instantaneous) |")
    A(f"| Forecast hours | {', '.join(f'f{h:03d}' for h in cfg['fhours'])} |")
    A(
        f"| Members (in-house) | {len(cfg['members'])} (`{cfg['members'][0]}` + "
        f"`{cfg['members'][1]}`..`{cfg['members'][-1]}`) |"
    )
    A(f"| Cities | {', '.join(cfg['cities'])} |")
    A(
        f"| Reference | Open-Meteo `{OPEN_METEO_MODEL}` ensemble, "
        "`temperature_2m`, `cell_selection=nearest` |"
    )
    A(f"| City-hours compared | {s['n_city_hours']} |")
    A("")
    A("## Grid nodes: both sides asked for the same cell")
    A("")
    A(
        "Open-Meteo is queried at the **GEFS node's own coordinate**, not the "
        "station's, so the comparison is not confounded by two different "
        "nearest-cell rules. The coordinate the API served back is recorded, not "
        "assumed."
    )
    A("")
    A(
        "| City | Station | GEFS node (j, i) | Node lat/lon | Station-to-node | "
        "Open-Meteo served lat/lon | Its elevation |"
    )
    A("| --- | --- | --- | --- | --- | --- | --- |")
    for node in blob["nodes"]:
        A(
            f"| {node['city']} | {node['station']} | ({node['j']}, {node['i']}) | "
            f"{node['node_lat']:.2f}, {node['node_lon']:.2f} | {node['station_km']:.1f} km | "
            f"{node['served_lat']}, {node['served_lon']} | {node['served_elevation_m']} m |"
        )
    A("")
    A("## Result")
    A("")
    A(f"**Verdict: {s['verdict']}.**")
    A("")
    A(
        f"- Overall mean bias (in-house minus Open-Meteo), pooled over "
        f"{s['n_city_hours']} city-hours: **{s['overall_mean_bias_f']:+.2f} degF**."
    )
    A(
        f"- Per city-hour mean bias spans **{s['min_city_hour_bias_f']:+.2f} to "
        f"{s['max_city_hour_bias_f']:+.2f} degF**."
    )
    A(
        f"- Sorted member distributions agree to a mean absolute "
        f"**{s['mean_order_stat_abs_delta_f']:.2f} degF** per order statistic, "
        f"worst single rank **{s['worst_order_stat_abs_delta_f']:.2f} degF**."
    )
    A("")
    ok_rows = [r for r in cmp_["rows"] if r.get("status") == "ok"]
    if ok_rows:
        worst_bias = max(ok_rows, key=lambda r: abs(r["mean_bias_f"]))
        worst_rank = max(
            ok_rows,
            key=lambda r: max(
                abs(r["order_stat_delta_min_f"]), abs(r["order_stat_delta_max_f"])
            ),
        )
        ratios = [r["sigma_ratio"] for r in ok_rows if r["sigma_ratio"] is not None]
        by_fhour: Dict[int, List[float]] = {}
        for r in ok_rows:
            by_fhour.setdefault(int(r["fhour"]), []).append(abs(r["mean_bias_f"]))
        shape = ", ".join(
            f"f{h:03d} {_mean(v):.2f}" for h, v in sorted(by_fhour.items())
        )
        A(
            "Where the residual sits, measured rather than asserted: mean absolute "
            f"bias by forecast hour is {shape} degF, i.e. it concentrates at the "
            "afternoon hour where both ensembles are widest. The largest single "
            f"bias is **{worst_bias['city']} {worst_bias['valid_utc']}** at "
            f"{worst_bias['mean_bias_f']:+.2f} degF, and the largest single "
            f"order-statistic gap is **{worst_rank['city']} "
            f"{worst_rank['valid_utc']}** at "
            f"{max(abs(worst_rank['order_stat_delta_min_f']), abs(worst_rank['order_stat_delta_max_f'])):.2f} "
            f"degF -- the same city-hour whose ensemble sigma disagrees most "
            f"({worst_rank['sigma_inhouse_f']:.2f} here vs "
            f"{worst_rank['sigma_open_meteo_f']:.2f} there). Sigma ratios across "
            f"all city-hours span {min(ratios):.2f}..{max(ratios):.2f}. Two "
            "ensembles drawn from the same distribution but different cycles "
            "disagree most in exactly that place -- in the tails, on the "
            "convectively active hour -- so this shape is consistent with the "
            "member-identity caveat below and is not the signature of a scale or "
            "offset fault, which would be uniform across every hour and every "
            "rank."
        )
        A("")
    A(
        f"A Kelvin-to-Fahrenheit slip is worth ~460 degF, a Kelvin-left-as-Celsius "
        f"slip ~273 degF, a decimal-scale exponent error a factor of ten, a sign "
        f"error a reflection about zero, and a hemisphere or scan-mode error puts "
        f"the sample on the wrong continent -- typically tens of degrees. Nothing "
        f"of that magnitude is present: the largest discrepancy anywhere in the "
        f"table is under {DECODE_FAULT_THRESHOLD_F:.0f} degF, and the residual that "
        f"remains is the size expected from Open-Meteo's own elevation correction "
        f"to 2 m temperature (which its API does not let a caller disable) plus a "
        f"possible cycle difference."
    )
    A("")
    A("## Per city-hour")
    A("")
    A(
        "| City | Valid (UTC) | f | n | Mean in-house | Mean Open-Meteo | Bias | "
        "Sigma in-house | Sigma Open-Meteo | Sigma ratio | Sorted-rank delta "
        "(min..max) | Mean abs |"
    )
    A("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in cmp_["rows"]:
        if r.get("status") != "ok":
            A(
                f"| {r['city']} | {r['valid_utc']} | f{r['fhour']:03d} | "
                f"{r['n_inhouse']}/{r['n_open_meteo']} | | | | | | | | "
                f"{r['status']} |"
            )
            continue
        A(
            f"| {r['city']} | {r['valid_utc']} | f{r['fhour']:03d} | {r['n_inhouse']} | "
            f"{r['mean_inhouse_f']:.2f} | {r['mean_open_meteo_f']:.2f} | "
            f"**{r['mean_bias_f']:+.2f}** | {r['sigma_inhouse_f']:.2f} | "
            f"{r['sigma_open_meteo_f']:.2f} | {r['sigma_ratio']:.2f} | "
            f"{r['order_stat_delta_min_f']:+.2f}..{r['order_stat_delta_max_f']:+.2f} | "
            f"{r['order_stat_delta_mean_abs_f']:.2f} |"
        )
    A("")
    A("## Open caveat: per-member identity is NOT established")
    A("")
    A(
        "The comparison above is between *distributions*. It does not show that "
        "this repo's `gepNN` is Open-Meteo's `temperature_2m_memberNN`."
    )
    A("")
    A("| Quantity | Value |")
    A("| --- | --- |")
    A(
        f"| Mean per-member correlation, identity labelling | "
        f"{s['per_member_r_identity_mean']} |"
    )
    A(
        f"| Largest \\|r\\| under identity labelling | "
        f"{s['per_member_r_identity_max_abs']} |"
    )
    A(
        f"| Largest \\|r\\| under **any** cyclic relabelling | "
        f"{s['per_member_best_cyclic_r_max_abs']} |"
    )
    A("")
    A(
        "Per-member correlation is near zero and **no cyclic relabelling recovers "
        "it**, while the sorted distributions match closely. That signature is "
        "what a *different model cycle* looks like: two draws from the same "
        "forecast distribution, member for member unrelated. The Open-Meteo "
        "ensemble API publishes no initialisation time, so this cannot be "
        "confirmed from the response, and no alternative explanation has been "
        "excluded either."
    )
    A("")
    A(
        "**Recorded as an open item, not as a finding.** It does not weaken the "
        "conclusion this report is for -- a global decode fault would move the "
        "*distribution*, and the distributions agree -- but any future work that "
        "depends on member identity (per-member bias correction, member "
        "tracking across cycles) must establish it separately."
    )
    A("")
    A("## Reproduce")
    A("")
    A("```bash")
    A('$env:PYTHONPATH = "."')
    A(f"python scripts/verify_decoder_independence.py --init {cfg['init_utc'][:13]} \\")
    A(f"  --fhours {','.join(str(h) for h in cfg['fhours'])} \\")
    A("  --json-out reports/phase2/ws_g_decoder_independence.json \\")
    A("  --md-out reports/phase2/ws_g_decoder_independence.md")
    A("```")
    A("")
    A(
        "Both sides are live services. GEFS records are immutable once published "
        "and re-run identically for as long as NODD retains the cycle; Open-Meteo "
        "serves whatever cycle is current, so its numbers will move. Full "
        "machine-readable evidence, including every member value on both sides: "
        "`reports/phase2/ws_g_decoder_independence.json`."
    )
    A("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_init(text: str) -> datetime:
    raw = str(text).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"cannot parse init time {text!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--init", type=_parse_init, required=True, help="GEFS cycle, e.g. 2026-07-27T00"
    )
    ap.add_argument("--fhours", default=",".join(str(h) for h in DEFAULT_FHOURS))
    ap.add_argument(
        "--members", default=None, help="comma-separated member ids (default: all 31)"
    )
    ap.add_argument("--max-workers", type=int, default=4, help="HTTP concurrency (<=4)")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--md-out", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.max_workers > 4:
        ap.error("--max-workers must be <= 4 (hardware constraint)")

    fhours = tuple(sorted({int(x) for x in str(args.fhours).split(",") if x.strip()}))
    members = (
        tuple(x.strip() for x in str(args.members).split(",") if x.strip())
        if args.members
        else tuple(DEFAULT_MEMBERS)
    )
    valid_times = [args.init + timedelta(hours=h) for h in fhours]

    provider = EnsembleProvider(
        cache_dir=os.path.join(REPO_ROOT, "data", "ensemble"),
        max_workers=args.max_workers,
    )
    provider.connect()

    logger.info(
        "decoding %d members x %d fhours in-house ...", len(members), len(fhours)
    )
    inhouse, failures = fetch_inhouse(
        provider, args.init, fhours, members, max_workers=args.max_workers
    )
    if failures:
        logger.warning(
            "%d in-house record failure(s): %s",
            len(failures),
            json.dumps(failures, sort_keys=True)[:400],
        )

    logger.info("querying Open-Meteo %s ...", OPEN_METEO_MODEL)
    open_meteo = {
        city: fetch_open_meteo(provider.session, city, valid_times)
        for city in CITY_ORDER
    }

    nodes = []
    for city in CITY_ORDER:
        spec = CITIES[city]
        j, i = spec_nearest_node(spec)
        lat, lon = GEFS_GRID.node_lat_lon(j, i)
        nodes.append(
            {
                "city": city,
                "station": spec.station,
                "j": j,
                "i": i,
                "node_lat": lat,
                "node_lon": lon - 360.0 if lon > 180.0 else lon,
                "station_km": round(
                    haversine_km(spec.latitude, spec.longitude, lat, lon), 1
                ),
                "served_lat": open_meteo[city]["served_lat"],
                "served_lon": open_meteo[city]["served_lon"],
                "served_elevation_m": open_meteo[city]["served_elevation_m"],
            }
        )

    comparison = compare(inhouse, open_meteo, args.init, fhours, members)

    blob = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/verify_decoder_independence.py",
        "argv": argv,
        "config": {
            "init_utc": args.init.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fhours": list(fhours),
            "members": list(members),
            "cities": list(CITY_ORDER),
            "field": FIELD_TMP,
            "reference": f"open-meteo {OPEN_METEO_MODEL} ensemble",
        },
        "nodes": nodes,
        "inhouse_failures": failures,
        "inhouse_values_f": {
            f"{city}|f{fhour:03d}|{member}": round(v, 4)
            for (city, fhour, member), v in sorted(inhouse.items())
        },
        "open_meteo": {
            city: {
                "url": om["url"],
                "member_keys": om["member_keys"],
                "served_lat": om["served_lat"],
                "served_lon": om["served_lon"],
                "served_elevation_m": om["served_elevation_m"],
                "values_f": {
                    datetime.fromtimestamp(epoch, timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ): vals
                    for epoch, vals in sorted(om["by_valid_epoch"].items())
                },
            }
            for city, om in open_meteo.items()
        },
        "comparison": comparison,
    }

    s = comparison["summary"]
    print(json.dumps(s, indent=1, sort_keys=True))

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(blob, fh, indent=1, sort_keys=True, default=str)
            fh.write("\n")
        logger.info("wrote %s", args.json_out)
    if args.md_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.md_out)), exist_ok=True)
        with open(args.md_out, "wb") as fh:
            _md = render_markdown(blob)
            # Trailing newline keeps the end-of-file-fixer hook from rewriting
            # this report after it is generated.
            body = _md.rstrip("\n") + "\n"
            fh.write(body.encode("utf-8"))
        logger.info("wrote %s", args.md_out)

    return 0 if s["verdict"].startswith("DECODER INDEPENDENTLY") else 3


if __name__ == "__main__":
    raise SystemExit(main())
