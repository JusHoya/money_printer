"""Control frames for the factory (PRD_STRATEGY_FACTORY FR-F2.4): ``FrameSet -> FrameSet``.

Design record: ``docs/factory/FACTORY_ARCHITECTURE.md`` section 6.4. Each
transform rewrites ONLY hidden (scorer-side) columns of the ``search`` frame
and its ``gefs_twin`` consistently; the ``parity`` frame passes through
untouched and every visible column stays bit-identical (a genome cannot tell
a control frame from the real one). numpy-only.

The hidden columns are recomputed with the frame's own formulas
(``src/factory/frame.py::from_opportunity_frame``, verified row-for-row on
the frozen frame 2026-09-03):

* ``settles_yes = floor_strike <= high <= cap_strike`` (``between``);
  ``high >= floor_strike + 1`` (``greater``); ``high <= cap_strike - 1``
  (``less``) -- ``src/core/bracket_payoff.yes_bounds`` mirrored in numpy
  (:func:`payoff_settles_yes`); Kalshi settles whole degrees.
* ``result_code = 1 if settles_yes else 0``; ``won = settles_yes == (direction
  == buy_yes)``; ``realized_per_contract = won - price_paid - fee_per_contract``
  where ``executable`` else NaN; ``expiration_value`` is the settled daily
  high (it equals ``cli_high`` wherever CLI truth exists and carries Kalshi's
  value on the four city-days without a CLI record).

The three transforms
--------------------
``snapshot_efficient(fs, seed)`` -- the market is right at every snapshot:
hidden ``won`` is redrawn PER ROW as ``Bernoulli(p_mkt_row)`` with
``p_mkt_row = (yes_bid + yes_ask) / 2`` mapped to the traded side (``1 - mid``
for buy_no). Rows without a two-sided quote (``yes_bid == 0`` or ``yes_ask ==
1``) are physically DROPPED from search and twin (``folds.strip_rows``, twin
index remapped). Every price then equals its win probability minus the
half-spread, the 1c adverse fill and the fee, so no rule has edge; the
matched twin row (same ticker/ts/direction/mode via ``twin_index``) receives
the SAME draw, unmatched twin rows their own. ``settles_yes``/``result_code``
are set per row from the redrawn ``won`` (a per-row null has no market-level
settlement); ``cli_high``/``expiration_value`` are left as they were and are
meaningless under this null.

``residual_shuffle(fs, seed)`` -- per city, the whole-degree forecast
residual ``r = high - round(mu_last)`` of each city-day (``mu_last`` = the
``mu_f`` of the LAST vintage in the frame for that city-day, i.e. the row
with the greatest ``ts_utc``; ``round`` = half-up to the whole degree so the
shifted high stays on Kalshi's whole-degree settlement grid) is circularly
shifted across that city's dates by a seeded non-zero offset; the new high
``high' = round(mu_last) + r_shifted`` re-settles every bracket of the
city-day through :func:`payoff_settles_yes`, in search and twin alike (the
twin shares the truth, not the forecast). The per-city multiset of residuals
is preserved exactly; prices, forecasts and structure are untouched.
``truth_agrees`` / ``payoff_matches_kalshi`` are left as recorded (they
describe the real record, not the shuffled one).

``planted_edge(fs, seed, rule=PLANTED_RULE, edge=0.05)`` -- on the rows the
rule selects (``to_mask(rule) & executable``, all dates), hidden ``won`` is
flipped False -> True on a seeded random subset so the mean realized PnL on
those rows rises by exactly ``round(edge * N) / N`` (each flip adds exactly
1.0 to one row). The subset is STRATIFIED: by the disjoint calendar segments
the campaign calendar induces (A-search, A-embargo, A-validation, B-embargo,
... ) and, within a segment, by "first executable rule row per market" (the
rule's own trade rows) vs the other rule rows -- so every campaign search
window and every validation block, and the rule's own trade set inside each,
carry the planted delta to within rounding rather than a binomial share of it.
Matched twin rows are flipped identically (otherwise the gefs-twin ex-ante
disqualifier would kill the planted rule). ``settles_yes``/``result_code`` of
a flipped row follow its new ``won``.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.factory import folds
from src.factory import genome as G
from src.factory.columns import Frame
from src.factory.frame import FrameSet, frame_sha256

KINDS = ("snapshot", "residual", "planted")

#: The planted-edge rule (a searchable 3-clause genome). Counts on the frozen
#: frame ``data/factory/frames/weather_2026-07-25_bfcf94654a3a`` (2026-09-03):
#:   search-window trades  A 159 / B 230 / C 309 / ALL69 392  (>= 40 everywhere)
#:   validation-block trades  A 61 / B 67 / C 64
#:   rule rows (masked & executable, all dates) 3455
#: sigma_cap = 4.0 is the frame's own cap (it excludes nothing) but every
#: searchable genome carries a sigma_cap clause, so the rule is reachable by
#: the search; the two restrictive clauses are the window and band subsets.
PLANTED_RULE: G.Genome = G.Genome.from_values(
    name="planted_no_win3_bands3_sig4",
    notes="FR-F2.4 planted-edge positive control rule: buy NO, taker, windows {>=24h,12-24h,6-12h}, "
          "bands {3-4F,4-5F,5F+}, sigma_cap 4.0 (3 active clauses).",
    direction="buy_no",
    mode="taker",
    windows=(">=24h", "12-24h", "6-12h"),
    bands=("3-4F", "4-5F", "5F+"),
    sigma_cap=4.0,
    lead_buckets=("short", "medium", "long"),
)
assert G.n_active_clauses(PLANTED_RULE) == 3 and PLANTED_RULE.is_searchable()

__all__ = [
    "KINDS",
    "PLANTED_RULE",
    "city_day_high",
    "control_seed",
    "make_control_frames",
    "market_side_prob",
    "payoff_settles_yes",
    "planted_edge",
    "residual_shuffle",
    "snapshot_efficient",
    "two_sided",
]


# ---------------------------------------------------------------------------
# seeds
# ---------------------------------------------------------------------------
def control_seed(master_seed: int, kind: str, k: int) -> int:
    """``sha256(f"{master_seed}:control:{kind}:{k}")[:8]`` little-endian (mirrors ``evolve.seed_for``)."""
    digest = hashlib.sha256(f"{int(master_seed)}:control:{kind}:{int(k)}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "little")


# ---------------------------------------------------------------------------
# hidden-column formulas (mirrors of frame.py / bracket_payoff.py)
# ---------------------------------------------------------------------------
def payoff_settles_yes(floor_strike: np.ndarray, cap_strike: np.ndarray, strike_type_code: np.ndarray, high: np.ndarray) -> np.ndarray:
    """``bracket_payoff.settles_yes`` vectorised: between / less(1) / greater(2) codes of ``columns.STRIKE_TYPE_LABELS``."""
    fl = np.asarray(floor_strike, dtype=np.float64)
    cp = np.asarray(cap_strike, dtype=np.float64)
    st = np.asarray(strike_type_code)
    h = np.asarray(high, dtype=np.float64)
    lo = np.where(st == 0, fl, np.where(st == 2, fl + 1.0, -np.inf))
    hi = np.where(st == 0, cp, np.where(st == 1, cp - 1.0, np.inf))
    with np.errstate(invalid="ignore"):
        return (h >= lo) & (h <= hi)


def city_day_high(F: Frame) -> np.ndarray:
    """Per-row settled daily high: ``cli_high`` where finite, else ``expiration_value``."""
    hi = F.hidden["cli_high"]
    ev = F.hidden["expiration_value"]
    return np.where(np.isfinite(hi), hi, ev)


def two_sided(F: Frame) -> np.ndarray:
    """Rows with a two-sided quote (``yes_bid > 0 & yes_ask < 1``; the fitness kernel's BSS rule)."""
    v = F.visible
    return (v["yes_bid"] > 0.0) & (v["yes_ask"] < 1.0)


def market_side_prob(F: Frame) -> np.ndarray:
    """Market mid ``(yes_bid + yes_ask)/2`` mapped to the traded side (``1 - mid`` for buy_no)."""
    v = F.visible
    mid = (v["yes_bid"] + v["yes_ask"]) / 2.0
    return np.where(v["direction_code"] == 1, 1.0 - mid, mid)


def _realized(F_vis: Dict[str, np.ndarray], won: np.ndarray) -> np.ndarray:
    r = won.astype(np.float64) - F_vis["price_paid"] - F_vis["fee_per_contract"]
    return np.where(F_vis["executable"], r, np.nan).astype(np.float64)


def _round_half_up(x: np.ndarray) -> np.ndarray:
    return np.floor(np.asarray(x, dtype=np.float64) + 0.5)


def _copy_frame(F: Frame, hidden_updates: Dict[str, np.ndarray], control: Dict[str, Any]) -> Frame:
    """New Frame sharing the visible arrays (never written) with copied/updated hidden arrays."""
    hidden = {k: (hidden_updates[k] if k in hidden_updates else v.copy()) for k, v in F.hidden.items()}
    prov = dict(F.provenance)
    prov["control"] = dict(control)
    prov["control"]["parent_frame_sha256"] = F.provenance.get("frame_sha256")
    out = Frame(
        name=F.name,
        visible=dict(F.visible),
        hidden=hidden,
        dates=F.dates,
        markets=F.markets,
        block_starts=F.block_starts,
        provenance=prov,
        twin_index=F.twin_index,
    )
    out.validate()
    out.provenance["frame_sha256"] = frame_sha256(out)
    return out


def _outcomes_from_won(F: Frame, won: np.ndarray) -> Dict[str, np.ndarray]:
    """Hidden updates when the per-row outcome is given as ``won``."""
    v = F.visible
    won = np.asarray(won, dtype=bool)
    settles = np.where(v["direction_code"] == 0, won, ~won)
    return {
        "won": won,
        "settles_yes": settles.astype(bool),
        "result_code": settles.astype(np.int16),
        "realized_per_contract": _realized(v, won),
    }


def _outcomes_from_high(F: Frame, high: np.ndarray, cli_known: np.ndarray) -> Dict[str, np.ndarray]:
    """Hidden updates when a new settled high is given per row."""
    v = F.visible
    settles = payoff_settles_yes(v["floor_strike"], v["cap_strike"], v["strike_type_code"], high)
    won = settles == (v["direction_code"] == 0)
    return {
        "settles_yes": settles.astype(bool),
        "result_code": settles.astype(np.int16),
        "won": won.astype(bool),
        "realized_per_contract": _realized(v, won),
        "expiration_value": np.asarray(high, dtype=np.float64),
        "cli_high": np.where(cli_known, high, np.nan).astype(np.float64),
    }


def _strip_pair_rows(search: Frame, twin: Optional[Frame], keep_s: np.ndarray, keep_t: Optional[np.ndarray]) -> Tuple[Frame, Optional[Frame]]:
    """``folds.strip_rows`` on both frames with the twin index remapped."""
    if twin is None or keep_t is None:
        s = folds.strip_rows(search, keep_s)
        return s, None
    twin_row_map = np.full(twin.n_rows, -1, dtype=np.int64)
    twin_row_map[keep_t] = np.arange(int(keep_t.sum()), dtype=np.int64)
    s = folds.strip_rows(search, keep_s, twin_row_map=twin_row_map)
    t = folds.strip_rows(twin, keep_t)
    return s, t


def _finish(fs: FrameSet, search: Frame, twin: Optional[Frame], info: Dict[str, Any]) -> FrameSet:
    prov = dict(fs.provenance or {})
    prov["control"] = dict(info)
    prov["frames"] = {
        "parity": (fs.parity.provenance or {}).get("frame_sha256"),
        "search": search.provenance.get("frame_sha256"),
        "gefs_twin": twin.provenance.get("frame_sha256") if twin is not None else None,
    }
    return FrameSet(parity=fs.parity, search=search, gefs_twin=twin, provenance=prov)


# ---------------------------------------------------------------------------
# 1. snapshot-efficient null
# ---------------------------------------------------------------------------
def snapshot_efficient(fs: FrameSet, seed: int) -> FrameSet:
    """Hidden ``won ~ Bernoulli(p_mkt_row)`` per two-sided row; other rows dropped (module docstring)."""
    search, twin = fs.search, fs.gefs_twin
    keep_s = two_sided(search)
    keep_t = two_sided(twin) if twin is not None else None
    s, t = _strip_pair_rows(search, twin, keep_s, keep_t)

    rng = np.random.default_rng(int(seed))
    p_s = market_side_prob(s)
    won_s = rng.random(s.n_rows) < p_s
    info: Dict[str, Any] = {
        "kind": "snapshot",
        "seed": int(seed),
        "search_rows_in": int(search.n_rows),
        "search_rows_kept": int(s.n_rows),
        "search_rows_dropped": int(search.n_rows - s.n_rows),
        "search_dates_dropped": int(search.n_dates - s.n_dates),
        "search_markets_dropped": int(search.n_markets - s.n_markets),
        "mean_p_mkt_search": float(p_s.mean()) if s.n_rows else None,
    }
    s_out = _copy_frame(s, _outcomes_from_won(s, won_s), info)

    t_out = None
    if t is not None:
        p_t = market_side_prob(t)
        won_t = rng.random(t.n_rows) < p_t
        ti = s.twin_index
        n_shared = 0
        if ti is not None:
            ok = ti >= 0
            won_t[ti[ok]] = won_s[ok]  # matched twin rows share the search draw
            n_shared = int(ok.sum())
        t_info = dict(info, twin_rows_in=int(twin.n_rows), twin_rows_kept=int(t.n_rows), twin_rows_shared_draw=n_shared)
        t_out = _copy_frame(t, _outcomes_from_won(t, won_t), t_info)
        info["twin_rows_in"] = int(twin.n_rows)
        info["twin_rows_kept"] = int(t.n_rows)
        info["twin_rows_shared_draw"] = n_shared
    return _finish(fs, s_out, t_out, info)


# ---------------------------------------------------------------------------
# 2. residual-shuffle null
# ---------------------------------------------------------------------------
def _city_day_table(F: Frame) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(key per row, unique keys, mu_last per unique key) with key = city * n_dates + date_code."""
    v = F.visible
    nd = F.n_dates
    key = v["city_code"].astype(np.int64) * nd + v["target_date_code"].astype(np.int64)
    ukeys, inv = np.unique(key, return_inverse=True)
    ts = v["ts_utc"]
    ts_max = np.full(ukeys.shape[0], np.iinfo(np.int64).min, dtype=np.int64)
    np.maximum.at(ts_max, inv, ts)
    is_last = ts == ts_max[inv]
    mu_last = np.full(ukeys.shape[0], np.nan, dtype=np.float64)
    # the last vintage's mu_f is shared by every row at that ts of the city-day; take the first
    rows_last = np.flatnonzero(is_last)
    first_last = {}
    for r in rows_last:
        k = int(inv[r])
        if k not in first_last:
            first_last[k] = r
    for k, r in first_last.items():
        mu_last[k] = v["mu_f"][r]
    return inv, ukeys, mu_last


def residual_shuffle(fs: FrameSet, seed: int) -> FrameSet:
    """Circularly shift each city's whole-degree forecast residuals across dates (module docstring)."""
    search, twin = fs.search, fs.gefs_twin
    nd = search.n_dates
    inv, ukeys, mu_last = _city_day_table(search)
    high_rows = city_day_high(search)
    high_cd = np.full(ukeys.shape[0], np.nan, dtype=np.float64)
    for k in range(ukeys.shape[0]):
        h = high_rows[inv == k]
        h = h[np.isfinite(h)]
        if h.size:
            high_cd[k] = h[0]
    city_cd = ukeys // nd
    date_cd = ukeys % nd
    base = _round_half_up(mu_last)
    resid = high_cd - base  # whole-degree residuals (high is integral)

    rng = np.random.default_rng(int(seed))
    new_resid = resid.copy()
    shifts: Dict[str, int] = {}
    per_city_n: Dict[str, int] = {}
    for c in np.unique(city_cd):
        sel = np.flatnonzero((city_cd == c) & np.isfinite(resid))
        sel = sel[np.argsort(date_cd[sel], kind="stable")]
        n_c = int(sel.shape[0])
        per_city_n[str(int(c))] = n_c
        if n_c < 2:
            shifts[str(int(c))] = 0
            continue
        s = int(rng.integers(1, n_c))
        shifts[str(int(c))] = s
        new_resid[sel] = np.roll(resid[sel], s)
    new_high_cd = base + new_resid
    usable = np.isfinite(new_high_cd)
    # city-days whose high/forecast is unknown keep their recorded outcome
    new_high_cd = np.where(usable, new_high_cd, high_cd)

    lut = {(int(city_cd[k]), str(search.dates[date_cd[k]])): float(new_high_cd[k]) for k in range(ukeys.shape[0])}
    info: Dict[str, Any] = {
        "kind": "residual",
        "seed": int(seed),
        "city_days": int(ukeys.shape[0]),
        "city_days_shifted": int(usable.sum()),
        "city_days_unshifted": int((~usable).sum()),
        "shift_by_city": shifts,
        "residual_dates_by_city": per_city_n,
    }

    def _apply(F: Frame) -> Frame:
        v = F.visible
        keys = [(int(c), str(F.dates[d])) for c, d in zip(v["city_code"], v["target_date_code"])]
        old = city_day_high(F)
        new = np.array([lut.get(k, np.nan) for k in keys], dtype=np.float64)
        missing = ~np.isfinite(new)
        new = np.where(missing, old, new)
        cli_known = np.isfinite(F.hidden["cli_high"])
        upd = _outcomes_from_high(F, new, cli_known)
        changed = int(np.count_nonzero(upd["settles_yes"] != F.hidden["settles_yes"]))
        fi = dict(info, rows=int(F.n_rows), rows_without_table_entry=int(missing.sum()), rows_settlement_changed=changed)
        return _copy_frame(F, upd, fi)

    s_out = _apply(search)
    t_out = _apply(twin) if twin is not None else None
    info["search_rows_settlement_changed"] = s_out.provenance["control"]["rows_settlement_changed"]
    if t_out is not None:
        info["twin_rows_settlement_changed"] = t_out.provenance["control"]["rows_settlement_changed"]
    return _finish(fs, s_out, t_out, info)


# ---------------------------------------------------------------------------
# 3. planted-edge positive control
# ---------------------------------------------------------------------------
def _segments(F: Frame) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Per-date segment id from the campaign calendar (dates outside it: one 'other' segment)."""
    dates = [str(d) for d in F.dates]
    try:
        camps = folds.campaigns(dates)
    except (ValueError, AssertionError):
        return np.zeros(len(dates), dtype=np.int64), {"0": "all (no campaign calendar)"}
    keys: List[Tuple[int, ...]] = []
    for d in dates:
        k = []
        for name in ("A", "B", "C"):
            c = camps[name]
            k.append(0 if d in c.search_dates else 1 if d in c.embargo_dates else 2 if d in c.validation_dates else 3)
        keys.append(tuple(k))
    uniq = sorted(set(keys))
    seg = np.array([uniq.index(k) for k in keys], dtype=np.int64)
    label = {str(i): "/".join(f"{n}:{'sev-'[x]}" for n, x in zip("ABC", k)) for i, k in enumerate(uniq)}
    return seg, label


def planted_edge(
    fs: FrameSet,
    seed: int,
    *,
    rule: G.Genome = PLANTED_RULE,
    edge: float = 0.05,
) -> Tuple[FrameSet, Dict[str, Any]]:
    """Give ``rule`` a ``+edge``/contract realized edge by flipping hidden ``won`` (module docstring)."""
    from src.factory import fitness

    search, twin = fs.search, fs.gefs_twin
    v = search.visible
    mask = G.to_mask(rule, search)
    rule_rows = np.flatnonzero(mask & v["executable"] & np.isfinite(search.hidden["realized_per_contract"]))
    N = int(rule_rows.shape[0])
    trade_rows = G.first_true_per_block(mask & v["executable"], search.block_starts)
    is_trade = np.zeros(search.n_rows, dtype=bool)
    is_trade[trade_rows] = True
    won0 = search.hidden["won"]
    seg_of_date, seg_label = _segments(search)
    seg_row = seg_of_date[v["target_date_code"]]

    rng = np.random.default_rng(int(seed))
    target_total = int(round(float(edge) * N))
    flipped: List[int] = []
    strata: List[Dict[str, Any]] = []
    for sid in np.unique(seg_row[rule_rows]):
        for is_t in (True, False):
            rows = rule_rows[(seg_row[rule_rows] == sid) & (is_trade[rule_rows] == is_t)]
            n_rows = int(rows.shape[0])
            cand = rows[~won0[rows]]
            want = int(round(float(edge) * n_rows))
            take = min(want, int(cand.shape[0]))
            chosen = np.sort(rng.choice(cand, size=take, replace=False)) if take > 0 else np.zeros(0, dtype=np.int64)
            flipped.extend(int(r) for r in chosen)
            strata.append({
                "segment": seg_label[str(int(sid))], "trade_rows": bool(is_t), "n_rows": n_rows,
                "n_false": int(cand.shape[0]), "want": want, "flipped": int(take),
            })
    # settle the total on round(edge * N) exactly
    flipped_set = set(flipped)
    diff = target_total - len(flipped_set)
    if diff > 0:
        pool = np.array([int(r) for r in rule_rows if (not won0[r]) and int(r) not in flipped_set], dtype=np.int64)
        extra = rng.choice(pool, size=min(diff, int(pool.shape[0])), replace=False) if pool.size else np.zeros(0, dtype=np.int64)
        flipped_set.update(int(r) for r in extra)
    elif diff < 0:
        arr = np.array(sorted(flipped_set), dtype=np.int64)
        drop = rng.choice(arr, size=-diff, replace=False)
        flipped_set.difference_update(int(r) for r in drop)
    flip_idx = np.array(sorted(flipped_set), dtype=np.int64)

    won_s = won0.copy()
    won_s[flip_idx] = True
    upd_s = _outcomes_from_won(search, won_s)
    # only flipped rows change; keep every other row's recorded hidden values bit-identical
    for k in ("settles_yes", "result_code", "realized_per_contract"):
        keep = np.ones(search.n_rows, dtype=bool)
        keep[flip_idx] = False
        upd_s[k] = np.where(keep, search.hidden[k], upd_s[k]).astype(search.hidden[k].dtype)

    n_false = int(np.count_nonzero(~won0[rule_rows]))
    info: Dict[str, Any] = {
        "kind": "planted",
        "seed": int(seed),
        "edge": float(edge),
        "rule": rule.to_json(),
        "n_rule_rows": N,
        "n_trade_rows": int(trade_rows.shape[0]),
        "n_false_rows": n_false,
        "target_flips": target_total,
        "n_flipped": int(flip_idx.shape[0]),
        "delta_rows_all_dates": float(flip_idx.shape[0] / N) if N else math.nan,
        "strata": strata,
    }
    s_out = _copy_frame(search, upd_s, info)

    t_out = None
    if twin is not None:
        ti = search.twin_index
        won_t = twin.hidden["won"].copy()
        n_tw = 0
        if ti is not None:
            tf = ti[flip_idx]
            tf = tf[tf >= 0]
            won_t[tf] = True
            n_tw = int(tf.shape[0])
            upd_t = _outcomes_from_won(twin, won_t)
            keep = np.ones(twin.n_rows, dtype=bool)
            keep[tf] = False
            for k in ("settles_yes", "result_code", "realized_per_contract"):
                upd_t[k] = np.where(keep, twin.hidden[k], upd_t[k]).astype(twin.hidden[k].dtype)
        else:
            upd_t = {}
        info["twin_rows_flipped"] = n_tw
        t_out = _copy_frame(twin, upd_t, dict(info, twin_rows_flipped=n_tw))

    # achieved deltas per campaign window (rows, trade rows, and the rule's own scored validation)
    before = search.hidden["realized_per_contract"]
    after = s_out.hidden["realized_per_contract"]
    windows: Dict[str, Any] = {}
    pooled_before: List[float] = []
    pooled_after: List[float] = []
    try:
        camps = folds.campaigns([str(d) for d in search.dates])
    except (ValueError, AssertionError):
        camps = {}
    for name, camp in camps.items():
        entry: Dict[str, Any] = {}
        for role, dates in (("search", camp.search_dates), ("validation", camp.validation_dates)):
            if not dates:
                entry[role] = None
                continue
            dm = folds.date_mask(search, dates)
            rr = rule_rows[dm[rule_rows]]
            tr = trade_rows[dm[trade_rows]]
            r_b = fitness.score(search, mask, date_mask=dm, constraints=False)
            r_a = fitness.score(s_out, mask, date_mask=dm, constraints=False)
            entry[role] = {
                "n_rows": int(rr.shape[0]),
                "delta_rows": float(np.mean(after[rr] - before[rr])) if rr.size else None,
                "n_trade_rows": int(tr.shape[0]),
                "delta_trade_rows": float(np.mean(after[tr] - before[tr])) if tr.size else None,
                "rule_realized_before": r_b.realized,
                "rule_realized_after": r_a.realized,
                "rule_delta": (r_a.realized - r_b.realized) if (r_a.trades and r_b.trades) else None,
                "rule_trades": int(r_a.trades),
            }
            if role == "validation" and name in ("A", "B", "C") and r_a.trades:
                pooled_before.extend(float(x) for x in r_b.per_date_pnl)
                pooled_after.extend(float(x) for x in r_a.per_date_pnl)
        windows[name] = entry
    info["windows"] = windows
    info["rule_pooled_validation_delta"] = (
        float(np.mean(pooled_after) - np.mean(pooled_before)) if pooled_after else None
    )
    info["rule_pooled_validation_dates"] = len(pooled_after)
    return _finish(fs, s_out, t_out, info), info


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------
def make_control_frames(fs: FrameSet, kind: str, seed: int, **kw: Any) -> Tuple[FrameSet, Dict[str, Any]]:
    """``(transformed FrameSet, info)`` for ``kind`` in :data:`KINDS`."""
    if kind == "snapshot":
        out = snapshot_efficient(fs, seed)
        return out, dict(out.provenance.get("control") or {})
    if kind == "residual":
        out = residual_shuffle(fs, seed)
        return out, dict(out.provenance.get("control") or {})
    if kind == "planted":
        return planted_edge(fs, seed, **kw)
    raise ValueError(f"unknown control kind {kind!r}; want one of {KINDS}")
