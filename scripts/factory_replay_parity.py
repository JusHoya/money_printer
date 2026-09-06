#!/usr/bin/env python
"""Replay parity: the LIVE path (GenomeStrategy) vs the OFFLINE trade set (fitness.score) -- FR-F3.4.

    python scripts/factory_replay_parity.py [--frames DIR] [--ladders ROOT] [--genomes seeds,picks]
                                            [--run-id run_2026-09-03b] [--only NAME[,NAME]] [--out PATH]
                                            [--strict] [--calibration walk_forward|frozen|live]

For every genome (the gen-0 ``genome.SEEDS`` and the F2 picks A/B/C/ALL69 of
``reports/factory/<run_id>/summary.json``) a fresh ``GenomeStrategy`` is
driven, with ``source='replay'`` inputs, over EVERY hourly candle of EVERY
market in the ladder archive (``data/ladders``, 1,656 markets / 10,750
snapshots) exactly as the weather bot would present them:

* clock  = the candle's ``ts_utc`` (tz-aware UTC);
* ladder = one ``MarketData`` per market of the city-day (the union of the
  day's tickers, as ``ev_analysis.specs_for_city_day`` sees it), quotes from
  the candle at that ts (NaN when the market has no candle at that hour),
  ``close_time``/bracket fields from the market metadata, handed to the
  strategy in ``extra["ladder_markets"]`` the way ``weather_bot.tick`` does;
* forecast provider = the frame's forecast archive CSV (provenance-pinned
  sha) with the frame's availability lag;
* calibration = the frame's walk-forward payloads
  (``ev_analysis.WalkForwardCalibrator``, same source / embargo), reported
  under the calibration-dir identity the promoted spec carries.

``--calibration frozen`` swaps that last input -- and ONLY that input -- for the
``FrozenCalibrationProvider`` ``src/bots/weather_bot.py`` builds live. That is a
DIAGNOSTIC of the transfer gap, never parity evidence: the report is written as
``kind: "replay_parity_diagnostic"`` under a ``_frozen`` filename so it cannot be
mistaken for, or overwrite, the FR-F3.4 artifact. The measured gap for the
deployed genome 0c4b20502f2daf65 is **60 discrepancies** (n_live 144 vs n_offline
130) and ``p_yes_max_abs_diff`` **0.336**, against 0 / 0.0 on the walk-forward
control run of the same genome, same frame, same ladders
(``replay_parity_bfcf94654a3a_frozen.json`` vs ``..._frozen_control.json``). Both
providers read the SAME directory and report the SAME ``sha256``, which is why the
spec now carries ``calibration.kind`` and the strategy's construction guard refuses
paper mode on a mismatch.

``--calibration live`` (2026-09-06, the F4 blocker's closure) serves whatever
``src/bots/weather_bot.py`` ACTUALLY constructs for the spec -- through the bot's
own ``WeatherBot.build_calibration_provider``, not a re-implementation. Since the
bot now builds ``WalkForwardCalibrationProvider`` for every spec whose
``calibration.kind`` is ``walk_forward`` (all six committed specs), the served
provider refits the frame's per-target-date calibration live from the same two
archives; the run aborts unless the provider's archive shas equal the frame
provenance's pins and its embargo equals the frame's. The report is
``kind: "replay_parity"`` if and only if the served provider's kind equals the
kind the frame was proven under (``ev_config.calibration_mode``), otherwise
``replay_parity_diagnostic``; the default filename suffix is ``_live``.

The emitted set ``(market_ticker, ts_utc, contract_side, limit_price)`` is
diffed against ``fitness.score(F, genome.to_mask(g, F), constraints=False)
.trade_rows`` (``price_paid`` == the frame's quote + adverse_fill), and every
row the live path built is compared column-by-column with the frame's row
(``p_yes`` within 1e-9; every other visible column NaN-equal, except the two
dense frame indices ``target_date_code``/``market_code`` which the live path
cannot know). ``source='gefs'`` genomes are replayed on the ``gefs_twin``
frame with the GEFS archive and calibration.

Synthesised fields (documented, not fudged): ``price_mean``, candle
``volume``, ``open_interest``, ``last`` are copied from the candle into the
``MarketData`` because the live ``/markets`` poll carries no candle mean and a
cumulative volume -- none is a GENE_SPEC v1 input. mode=maker genomes are
reported with ``maker_fill_unknowable=True``: the evaluator's ``executable``
for a maker folds the forward-looking fill flags, so their discrepancy count
is expected to be non-zero and does not gate the exit code unless
``--strict``.

Output: ``reports/factory/replay_parity_<search sha12>.json`` (timestamp-free,
``sort_keys``, indent 2). Exit 1 on any discrepancy of a genome whose
executability is reproducible live (all taker genomes; every maker genome
with ``--strict``). Runs inside the factory container (no network).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from src.core.interfaces import MarketData  # noqa: E402
from src.factory import columns as C  # noqa: E402
from src.factory import fitness  # noqa: E402
from src.factory import genome as G  # noqa: E402
from src.factory import promoted as P  # noqa: E402
from src.factory.fees import load_regime, sha256_file  # noqa: E402

FRAMES_ROOT = REPO_ROOT / "data" / "factory" / "frames"
REPORTS_ROOT = REPO_ROOT / "reports" / "factory"
DEFAULT_RUN_ID = "run_2026-09-03b"
PICK_CAMPAIGNS = ("A", "B", "C", "ALL69")
P_YES_TOL = 1e-9
#: dense frame indices the live path cannot reproduce (documented in genome_strategy.py)
SKIP_COLUMNS = ("target_date_code", "market_code")
#: visible columns the live poll cannot carry (compared on replay, flagged in the report)
CANDLE_ONLY_COLUMNS = ("price_mean", "volume", "open_interest", "last")


class ParityAbort(RuntimeError):
    pass


def _die(msg: str, code: int = 1) -> None:
    print(f"replay_parity: ABORT: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def latest_frames_dir(lane: str = "weather") -> Optional[Path]:
    if not FRAMES_ROOT.exists():
        return None
    cands = sorted(p for p in FRAMES_ROOT.iterdir() if p.is_dir() and p.name.startswith(f"{lane}_"))
    return cands[-1] if cands else None


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def verify_pinned_file(prov: Dict[str, Any], key: str, label: str) -> Path:
    """The frame's provenance names the file and its sha; refuse to replay on a different file."""
    ent = prov.get(key) or {}
    path = ent.get("path")
    want = ent.get("sha256")
    if not path or not want:
        raise ParityAbort(f"frame provenance lacks {key}")
    p = _abs(path)
    if not p.exists():
        raise ParityAbort(f"{label} {p} missing")
    got = sha256_file(str(p))
    if got != want:
        raise ParityAbort(f"{label} {path} sha {got[:12]} != frame provenance {want[:12]}; the replay would not reproduce the frame")
    return p


def verify_truth_files(prov: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for city, ent in (prov.get("truth_files") or {}).items():
        p = _abs(ent["path"])
        if not p.exists():
            raise ParityAbort(f"truth file {p} missing")
        got = sha256_file(str(p))
        if got != ent["sha256"]:
            raise ParityAbort(f"truth {ent['path']} sha {got[:12]} != provenance {ent['sha256'][:12]}")
        out[city] = got
    return out


def _committed_spec_kind(genome_id: str) -> Optional[str]:
    """``calibration.kind`` of the COMMITTED promoted spec for ``genome_id`` (None when none exists).

    Informational for ``--calibration live``: the replay builds its own spec from the
    frame, so this records what the spec the sandbox would actually load says.
    """
    try:
        return P.load_promoted(genome_id).calibration.kind
    except Exception:  # noqa: BLE001 -- no committed spec is a normal state for seeds/picks
        return None


def calibration_identity(prov: Dict[str, Any]) -> Tuple[str, str]:
    """(dir, sha) -- the frame's ``calibration_dir.files`` mapping hashed as the spec does."""
    cal = prov.get("calibration_dir") or {}
    files = cal.get("files") or {}
    d = cal.get("path") or "data/calibration"
    want = P.sha256_of_mapping(files)
    got = P.calibration_dir_sha256(str(_abs(d)))
    if got != want:
        raise ParityAbort(f"calibration dir {d} hashes to {got[:12]}, frame provenance says {want[:12]}")
    return d, want


# ---------------------------------------------------------------------------
# ladder archive -> the bot's view
# ---------------------------------------------------------------------------
class DayLadder:
    __slots__ = ("city", "series", "target_date", "markets", "snapshots")

    def __init__(self, city: str, series: str, target_date: str) -> None:
        self.city = city
        self.series = series
        self.target_date = target_date
        self.markets: Dict[str, Dict[str, Any]] = {}  # ticker -> metadata
        self.snapshots: Dict[int, Dict[str, Dict[str, Any]]] = {}  # ts_epoch -> ticker -> candle


def _f(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def load_days(ladder_root: Path) -> Dict[Tuple[str, str], DayLadder]:
    """Every city-day of the archive through ``ev_analysis.load_search_ladders`` (sealed roots refused)."""
    import src.backtest.ev_analysis as ev

    df = ev.load_search_ladders(str(ladder_root))
    if df.empty:
        raise ParityAbort(f"no ladders under {ladder_root}")
    days: Dict[Tuple[str, str], DayLadder] = {}
    ts_epoch = (df["ts_utc"].dt.tz_convert(None).to_numpy().astype("datetime64[s]").astype(np.int64))
    close = df["close_time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ").to_numpy()
    cols = {
        c: df[c].to_numpy()
        for c in (
            "city", "series", "target_date", "market_ticker", "strike_type", "floor_strike", "cap_strike",
            "yes_sub_title", "yes_bid", "yes_ask", "no_bid", "no_ask", "last", "price_mean", "volume",
            "open_interest",
        )
    }
    n = len(df)
    for i in range(n):
        key = (str(cols["city"][i]), str(cols["target_date"][i])[:10])
        day = days.get(key)
        if day is None:
            day = DayLadder(key[0], str(cols["series"][i]), key[1])
            days[key] = day
        t = str(cols["market_ticker"][i])
        if t not in day.markets:
            fs, cs = cols["floor_strike"][i], cols["cap_strike"][i]
            day.markets[t] = {
                "strike_type": None if cols["strike_type"][i] is None or str(cols["strike_type"][i]) == "nan" else str(cols["strike_type"][i]),
                "floor_strike": None if fs is None or (isinstance(fs, float) and math.isnan(fs)) else float(fs),
                "cap_strike": None if cs is None or (isinstance(cs, float) and math.isnan(cs)) else float(cs),
                "yes_sub_title": cols["yes_sub_title"][i],
                "close_time": str(close[i]),
            }
        snap = day.snapshots.setdefault(int(ts_epoch[i]), {})
        snap[t] = {
            "yes_bid": _f(cols["yes_bid"][i]),
            "yes_ask": _f(cols["yes_ask"][i]),
            "no_bid": _f(cols["no_bid"][i]),
            "no_ask": _f(cols["no_ask"][i]),
            "last": _f(cols["last"][i]),
            "price_mean": _f(cols["price_mean"][i]),
            "volume": _f(cols["volume"][i]),
            "open_interest": _f(cols["open_interest"][i]),
        }
    return days


def market_data_at(days: Sequence[DayLadder], ts_epoch: int) -> MarketData:
    """The fused observation ``weather_bot.tick`` hands a strategy for ONE city at ``ts_epoch``.

    ``days`` are the city-days of that city with a candle at this hour (the
    D-1/D/D+1 ladders ``_ladder_for_city`` tracks, all in ONE call). Every
    market of each such day is present (the live poll lists the whole event
    ladder); a market without a candle at this hour carries NaN quotes.
    """
    ts = _dt.datetime.fromtimestamp(ts_epoch, _dt.timezone.utc)
    ladder: List[MarketData] = []
    nan = float("nan")
    for day in days:
        snap = day.snapshots.get(ts_epoch, {})
        for ticker in sorted(day.markets):
            meta = day.markets[ticker]
            c = snap.get(ticker)
            ladder.append(
                MarketData(
                    symbol=ticker,
                    timestamp=ts,
                    price=c["last"] if c else nan,
                    volume=c["volume"] if c else nan,
                    bid=c["yes_bid"] if c else nan,
                    ask=c["yes_ask"] if c else nan,
                    extra={
                        "status": "active",
                        "close_time": meta["close_time"],
                        "source": "replay",
                        "no_bid": c["no_bid"] if c else nan,
                        "no_ask": c["no_ask"] if c else nan,
                        "open_interest": c["open_interest"] if c else nan,
                        "price_mean": c["price_mean"] if c else nan,
                        "strike_type": meta["strike_type"],
                        "floor_strike": meta["floor_strike"],
                        "cap_strike": meta["cap_strike"],
                        "yes_sub_title": meta["yes_sub_title"],
                    },
                )
            )
    active = ladder[0]
    return MarketData(
        symbol=active.symbol,
        timestamp=ts,
        price=active.price,
        volume=active.volume,
        bid=active.bid,
        ask=active.ask,
        extra={
            "city_key": days[0].city,
            "kalshi_series": days[0].series,
            "ladder_markets": ladder,
            "strike_type": active.extra["strike_type"],
            "floor_strike": active.extra["floor_strike"],
            "cap_strike": active.extra["cap_strike"],
        },
    )


def city_calls(days: Dict[Tuple[str, str], DayLadder]) -> List[Tuple[int, str, List[DayLadder]]]:
    """``(ts_epoch, city, [city-days open at ts])`` in time order -- one analyze() call each."""
    by_key: Dict[Tuple[int, str], List[DayLadder]] = {}
    for day in days.values():
        for ts in day.snapshots:
            by_key.setdefault((ts, day.city), []).append(day)
    out = []
    for (ts, city), ds in sorted(by_key.items()):
        out.append((ts, city, sorted(ds, key=lambda d: d.target_date)))
    return out


# ---------------------------------------------------------------------------
# replay-side providers
# ---------------------------------------------------------------------------
CALIBRATION_KINDS = ("walk_forward", "frozen", "live")


class WalkForwardCalibrationProvider:
    """The frame's walk-forward payloads (``WalkForwardCalibrator.calibration_as_of``).

    ``sha256`` is the calibration-DIR identity the promoted spec carries (the
    live ``FrozenCalibrationProvider`` reports the same value for the same
    files); ``kind`` says which payloads are actually served. The per-date
    payload content hashes are recorded in the report.
    """

    kind = "walk_forward"

    def __init__(self, wf: Any, sha256: str) -> None:
        self.wf = wf
        self.sha256 = sha256
        self.payload_hashes: Dict[str, str] = {}
        self.failures: Dict[str, str] = {}

    def payload_for(self, city: str, target_date: str):
        key = f"{city}|{target_date}"
        try:
            payload = self.wf.calibration_as_of(city, target_date)
        except Exception as exc:
            self.failures[key] = str(exc)[:160]
            raise
        self.payload_hashes[key] = str(payload.get("content_hash"))
        return payload


class _FrozenProviderProbe:
    """The LIVE ``FrozenCalibrationProvider``, wrapped only to record what it served.

    This is not a replay stand-in: it delegates every ``payload_for`` to the real
    provider ``src/bots/weather_bot.py`` constructs, so a ``--calibration frozen``
    run measures the transfer gap between the frame's walk-forward payloads and the
    ones the sandbox actually prices with. The wrapper adds the ``payload_hashes`` /
    ``failures`` bookkeeping the report renders for either kind, nothing else.
    """

    kind = "frozen"

    def __init__(self, directory: str, *, source: str) -> None:
        from src.strategies.genome_strategy import FrozenCalibrationProvider

        self._inner = FrozenCalibrationProvider(directory, source=source)
        self.directory = directory
        self.sha256 = self._inner.sha256
        self.payload_hashes: Dict[str, str] = {}
        self.failures: Dict[str, str] = {}

    def payload_for(self, city: str, target_date: str):
        key = f"{city}|{target_date}"
        try:
            payload = self._inner.payload_for(city, target_date)
        except Exception as exc:
            self.failures[key] = str(exc)[:160]
            raise
        self.payload_hashes[key] = str(payload.get("content_hash"))
        return payload


class _LiveProviderProbe:
    """Whatever ``WeatherBot.build_calibration_provider(spec, dir)`` returns, wrapped only to record.

    THE provider the sandbox constructs for this spec, built by the bot's own builder
    (not a stand-in, not a re-implementation). Same ``payload_hashes`` / ``failures``
    bookkeeping as the other two probes; ``kind`` and ``sha256`` are the inner
    provider's, and ``describe()`` (archive shas, embargo, coverage) is what the report
    records so a reader can check it against the frame provenance.
    """

    def __init__(self, spec: Any, directory: str) -> None:
        from src.bots.weather_bot import WeatherBot

        self._inner = WeatherBot.build_calibration_provider(spec, directory)
        self.kind = getattr(self._inner, "kind", None)
        self.sha256 = getattr(self._inner, "sha256", None)
        self.directory = directory
        self.payload_hashes: Dict[str, str] = {}
        self.failures: Dict[str, str] = {}

    def describe(self) -> Dict[str, Any]:
        if hasattr(self._inner, "describe"):
            d = dict(self._inner.describe())
            for k in ("forecast_csv", "truth_dir"):
                if isinstance(d.get(k), str):
                    p = Path(d[k])
                    d[k] = p.relative_to(REPO_ROOT).as_posix() if str(p).startswith(str(REPO_ROOT)) else str(p)
            return d
        return {"kind": self.kind, "calibration_dir_sha256": self.sha256}

    def payload_for(self, city: str, target_date: str):
        key = f"{city}|{target_date}"
        try:
            payload = self._inner.payload_for(city, target_date)
        except Exception as exc:
            self.failures[key] = str(exc)[:160]
            raise
        self.payload_hashes[key] = str(payload.get("content_hash"))
        return payload


class Clock:
    """A settable clock: the replay sets ``now`` to each candle's ts before calling analyze."""

    def __init__(self) -> None:
        self.now = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

    def __call__(self) -> _dt.datetime:
        return self.now


# ---------------------------------------------------------------------------
# genomes under test
# ---------------------------------------------------------------------------
def load_pick_genomes(run_id: str) -> Dict[str, G.Genome]:
    path = REPORTS_ROOT / run_id / "summary.json"
    if not path.exists():
        raise ParityAbort(f"{path} missing (F2 picks)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, G.Genome] = {}
    for camp in PICK_CAMPAIGNS:
        pk = (doc.get("picks") or {}).get(camp) or {}
        gj = pk.get("genome_json")
        if not gj:
            continue
        g = G.Genome.from_json(gj)
        out[f"pick:{camp}:{run_id}"] = g.with_meta(name=f"pick_{camp}")
    return out


def genomes_under_test(which: str, run_id: str, only: Optional[Sequence[str]]) -> Dict[str, Tuple[str, G.Genome]]:
    """name -> (source label for the spec, Genome)."""
    out: Dict[str, Tuple[str, G.Genome]] = {}
    kinds = {k.strip() for k in which.split(",") if k.strip()}
    if "seeds" in kinds:
        for name, g in G.SEEDS.items():
            out[name] = ("seed", g)
    if "picks" in kinds:
        for label, g in load_pick_genomes(run_id).items():
            out[g.name] = (label, g)
    if only:
        keep = set(only)
        out = {k: v for k, v in out.items() if k in keep}
        missing = keep - set(out)
        if missing:
            raise ParityAbort(f"unknown genome name(s) {sorted(missing)}; have {sorted(out)}")
    return out


# ---------------------------------------------------------------------------
# the replay
# ---------------------------------------------------------------------------
def _nan_eq(a: Any, b: Any) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if math.isnan(fa) and math.isnan(fb):
        return True
    return fa == fb


def replay_genome(
    name: str,
    g: G.Genome,
    *,
    F: C.Frame,
    days: Dict[Tuple[str, str], DayLadder],
    spec: P.PromotedSpec,
    forecast_provider: Any,
    calibration_provider: Any,
    fee_regime: Any,
    prob_cache: Dict[Any, Any],
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    from src.strategies.genome_strategy import GenomeStrategy

    # --- offline truth ---------------------------------------------------
    mask = G.to_mask(g, F)
    res = fitness.score(F, mask, constraints=False)
    vis = F.visible
    side = "NO" if int(g.direction) == 1 else "YES"
    offline: Set[Tuple[str, int, str, float]] = set()
    for r in res.trade_rows:
        offline.add((str(F.markets[vis["market_code"][r]]), int(vis["ts_utc"][r]), side, float(vis["price_paid"][r])))

    # frame rows of this genome's (direction, mode) slice, keyed for the row compare
    slice_rows = np.flatnonzero((vis["direction_code"] == g.direction) & (vis["mode_code"] == g.mode))
    index: Dict[Tuple[str, int], int] = {}
    tick = F.markets[vis["market_code"][slice_rows]]
    tss = vis["ts_utc"][slice_rows]
    for j, r in enumerate(slice_rows):
        index[(str(tick[j]), int(tss[j]))] = int(r)

    # --- live path -------------------------------------------------------
    clock = Clock()
    captured: List[Dict[str, Any]] = []
    strat = GenomeStrategy(
        spec, clock=clock, forecast_provider=forecast_provider, fee_regime=fee_regime,
        calibration_provider=calibration_provider, prob_cache=prob_cache, row_sink=captured.append,
    )
    live: Set[Tuple[str, int, str, float]] = set()
    n_markets_seen: Set[str] = set()
    n_snap = 0
    for ts, _city, ds in city_calls(days):
        for day in ds:
            n_markets_seen.update(day.markets)
        clock.now = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
        data = market_data_at(ds, ts)
        n_snap += len(ds)
        for sig in strat.analyze(data):
            live.add((sig.symbol, ts, str(sig.contract_side), float(sig.limit_price)))
            if sig.expiration_time is None or sig.expiration_time.tzinfo is None:
                raise ParityAbort(f"{name}: signal {sig.symbol} lacks a tz-aware expiration_time")

    # --- diff ----------------------------------------------------------------
    missing_live = sorted(offline - live)
    extra_live = sorted(live - offline)
    n_disc = len(missing_live) + len(extra_live)

    # --- row compare -----------------------------------------------------
    cols = [c for c in C.VISIBLE_COLUMNS if c not in SKIP_COLUMNS]
    mism: Dict[str, int] = {}
    p_yes_max = 0.0
    compared = 0
    not_in_frame = 0
    sigma_capped = 0
    examples: List[Dict[str, Any]] = []
    visited: Set[int] = set()
    for row in captured:
        r = index.get((row["market_ticker"], int(row["ts_utc"])))
        if r is None:
            not_in_frame += 1
            if float(row["sigma_f"]) > spec.sigma_cap:
                sigma_capped += 1
            continue
        visited.add(r)
        compared += 1
        d = abs(float(row["p_yes"]) - float(vis["p_yes"][r]))
        if d > p_yes_max:
            p_yes_max = d
        for c in cols:
            if not _nan_eq(row[c], vis[c][r]):
                mism[c] = mism.get(c, 0) + 1
                if len(examples) < 20:
                    examples.append({"col": c, "market": row["market_ticker"], "ts": int(row["ts_utc"]),
                                     "live": None if isinstance(row[c], float) and math.isnan(row[c]) else float(row[c]),
                                     "frame": None if isinstance(vis[c][r], (float, np.floating)) and math.isnan(vis[c][r]) else float(vis[c][r])})
    unvisited = int(len(slice_rows) - len(visited))

    out = {
        "genome_id": spec.genome_id,
        "name": name,
        "source": g.source,
        "direction": C.DIRECTION_LABELS[g.direction],
        "mode": C.MODE_LABELS[g.mode],
        "describe": g.describe(),
        "frame": F.name,
        "n_markets": len(n_markets_seen),
        "n_markets_frame": int(F.n_markets),
        "n_snapshots": n_snap,
        "n_offline": len(offline),
        "n_live": len(live),
        "n_discrepancies": n_disc,
        "missing_live": [list(x) for x in missing_live[:20]],
        "extra_live": [list(x) for x in extra_live[:20]],
        "rows_compared": compared,
        "rows_frame_unvisited": unvisited,
        "rows_live_not_in_frame": not_in_frame,
        "rows_live_sigma_capped": sigma_capped,
        "p_yes_max_abs_diff": p_yes_max,
        "p_yes_within_tol": bool(p_yes_max <= P_YES_TOL),
        "column_mismatches": dict(sorted(mism.items())),
        "column_mismatch_examples": examples,
        "maker_fill_unknowable": bool(g.mode == 1),
        "strategy_stats": dict(strat.stats),
        "offline_shape": {"trades": int(res.trades), "markets": int(res.markets), "dates": int(res.dates),
                          "realized": None if not math.isfinite(res.realized) else float(res.realized)},
    }
    log(
        f"{name:22s} {C.MODE_LABELS[g.mode]:5s} {C.DIRECTION_LABELS[g.direction]:7s} markets {len(n_markets_seen):5d} "
        f"offline {len(offline):4d} live {len(live):4d} disc {n_disc:4d} rows {compared:6d} "
        f"p_yes<= {p_yes_max:.1e} colmis {sum(mism.values())}"
    )
    return out


def run_parity(
    frames_dir: Path,
    *,
    ladder_root: Optional[Path] = None,
    which: str = "seeds,picks",
    run_id: str = DEFAULT_RUN_ID,
    only: Optional[Sequence[str]] = None,
    mode: str = "shadow",
    registry_status: str = "CLOSED",
    family: Optional[str] = None,
    config_sha256: Optional[str] = None,
    calibration_kind: str = "walk_forward",
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run the whole replay; returns the report document (also what ``promote`` consumes)."""
    import src.backtest.ev_analysis as ev
    from src.data.forecast_vintage_provider import ForecastVintageProvider
    from src.factory.gen0 import load_frameset

    # ~10^6 reject lines otherwise; levels restored on exit so a test process is unaffected
    quiet = {name: logging.getLogger(name).level for name in ("MoneyPrinter", "src.calibration.probability_engine")}
    logging.getLogger("MoneyPrinter").setLevel(logging.WARNING)
    logging.getLogger("src.calibration.probability_engine").setLevel(logging.ERROR)
    try:
        return _run_parity(
            frames_dir, ladder_root=ladder_root, which=which, run_id=run_id, only=only, mode=mode,
            registry_status=registry_status, family=family, config_sha256=config_sha256,
            calibration_kind=calibration_kind, log=log,
        )
    finally:
        for name, level in quiet.items():
            logging.getLogger(name).setLevel(level)


def _run_parity(
    frames_dir: Path,
    *,
    ladder_root: Optional[Path],
    which: str,
    run_id: str,
    only: Optional[Sequence[str]],
    mode: str,
    registry_status: str,
    family: Optional[str],
    config_sha256: Optional[str],
    calibration_kind: str,
    log: Callable[[str], None],
) -> Dict[str, Any]:
    import src.backtest.ev_analysis as ev
    from src.data.forecast_vintage_provider import ForecastVintageProvider
    from src.factory.gen0 import load_frameset

    if calibration_kind not in CALIBRATION_KINDS:
        raise ParityAbort(f"unknown --calibration {calibration_kind!r}; expected one of {sorted(CALIBRATION_KINDS)}")

    fs = load_frameset(frames_dir)
    frames = {"gfs_mex": fs.search, "gefs": fs.gefs_twin}
    prov_search = fs.search.provenance
    ladder_root = ladder_root or _abs(prov_search.get("ladder_root") or "data/ladders")
    days = load_days(ladder_root)
    n_ladder_markets = sum(len(d.markets) for d in days.values())
    n_snapshots = sum(len(d.snapshots) for d in days.values())
    log(f"frames {frames_dir.name}: search {fs.search.n_rows} rows / {fs.search.n_markets} markets; "
        f"ladders {ladder_root}: {len(days)} city-days, {n_ladder_markets} markets, {n_snapshots} snapshots")

    fee_regime = load_regime()
    want_fee = (prov_search.get("fee_regime") or {}).get("sha256")
    if want_fee and fee_regime.sha256 != want_fee:
        raise ParityAbort(f"fee regime sha {fee_regime.sha256[:12]} != frame provenance {want_fee[:12]}")
    cal_dir, cal_sha = calibration_identity(prov_search)
    ev_cfg = prov_search.get("ev_config") or {}
    embargo = int(prov_search.get("embargo_days") or ev_cfg.get("embargo_days") or 1)
    lag = int(prov_search.get("availability_lag_min", 240))
    fee_type_frame = str(fee_regime.lookup("KXHIGH", int(fs.search.visible["ts_utc"][0])).fee_type)

    # The kind the frame was PROVEN under (its own calibrator): what a served provider
    # must match for the run to be parity evidence rather than a diagnostic.
    frame_kind = str(ev_cfg.get("calibration_mode") or "walk_forward")
    # The kind stamped on the spec each genome is replayed under. `live` builds the
    # provider FROM the spec, so the spec must say what the frame says -- that is the
    # whole question: does the bot, handed a committed spec, build the frame's provider?
    spec_kind = frame_kind if calibration_kind == "live" else calibration_kind

    # per-source replay providers (built lazily; gefs only if a gefs genome is under test)
    providers: Dict[str, Tuple[Any, Any, Dict[str, Any]]] = {}

    def _providers(source: str, spec: Any):
        if source in providers:
            return providers[source]
        F = frames[source]
        if F is None:
            raise ParityAbort(f"no {source} frame in {frames_dir}")
        prov = F.provenance
        fcsv = verify_pinned_file(prov, "forecast_csv", "forecast archive")
        truth = verify_truth_files(prov)
        src_obj = {s.name: s for s in ev.CANDIDATE_SOURCES}[source]
        if calibration_kind == "frozen":
            # The committed payloads under the spec's calibration dir, one per city, no
            # per-target-date walk-forward refit -- what weather_bot built until 2026-09-06
            # and still builds for a spec that says `frozen`. Same directory, same dir
            # sha -- which is the whole point: the identity the spec carries cannot tell
            # them apart.
            cal = _FrozenProviderProbe(str(_abs(cal_dir)), source=source)
        elif calibration_kind == "live":
            # THE provider src/bots/weather_bot.py constructs for this spec, via the
            # bot's own builder. Its archives must be the frame's pinned files and its
            # embargo the frame's, or the replay could not reproduce the frame for
            # reasons unrelated to the strategy.
            cal = _LiveProviderProbe(spec, str(_abs(cal_dir)))
            d = cal.describe()
            want_f = (prov.get("forecast_csv") or {}).get("sha256")
            if d.get("forecast_sha256") is not None and d.get("forecast_sha256") != want_f:
                raise ParityAbort(
                    f"live provider forecast archive sha {str(d.get('forecast_sha256'))[:12]} != frame "
                    f"provenance {str(want_f)[:12]}"
                )
            for city, sha in (d.get("truth_sha256") or {}).items():
                if truth.get(city) is not None and sha != truth[city]:
                    raise ParityAbort(f"live provider truth sha for {city} {sha[:12]} != frame provenance {truth[city][:12]}")
            if d.get("embargo_days") is not None and int(d["embargo_days"]) != embargo:
                raise ParityAbort(f"live provider embargo_days {d['embargo_days']} != frame provenance {embargo}")
        else:
            wf = ev.WalkForwardCalibrator(src_obj, tuple(C.CITY_LABELS), embargo_days=embargo)
            cal = WalkForwardCalibrationProvider(wf, cal_sha)
        if cal.sha256 != cal_sha:
            raise ParityAbort(
                f"calibration dir sha {str(cal.sha256)[:12]} != frame provenance {cal_sha[:12]}"
            )
        vp = ForecastVintageProvider.from_archive_csv(str(fcsv), lag_min=int(prov.get("availability_lag_min", lag)),
                                                     forecast_source=source)
        info = {"forecast_csv": prov.get("forecast_csv"), "truth_sha256": truth, "embargo_days": embargo,
                "availability_lag_min": int(prov.get("availability_lag_min", lag)), "frame_sha256": prov.get("frame_sha256"),
                "adverse_fill": float(prov.get("adverse_fill", 0.01)), "contracts": int(prov.get("contracts", 20)),
                "sigma_cap": None if prov.get("sigma_cap") is None else float(prov.get("sigma_cap"))}
        if hasattr(cal, "describe"):
            info["calibration_provider"] = cal.describe()
        providers[source] = (vp, cal, info)
        return providers[source]

    gens = genomes_under_test(which, run_id, only)
    if not gens:
        raise ParityAbort("no genomes selected")
    prob_cache: Dict[Any, Any] = {}
    results: Dict[str, Any] = {}
    for name, (src_label, g) in gens.items():
        source = g.source
        F = frames[source]
        if F is None:
            raise ParityAbort(f"no {source} frame in {frames_dir}")
        lag_src = int(F.provenance.get("availability_lag_min", lag))
        spec = P.build_spec(
            g, family=family or str(prov_search.get("family") or "weather/gfs_mex/taker/v1"),
            config_sha256=config_sha256 or "", frame_search_sha256=str(prov_search.get("frame_sha256")),
            calibration_dir=str(_abs(cal_dir)), calibration_sha256=cal_sha,
            calibration_kind=spec_kind, fee_type=fee_type_frame,
            fee_regime_sha256=fee_regime.sha256, adverse_fill=float(prov_search.get("adverse_fill", 0.01)),
            contracts_frame=int(prov_search.get("contracts", 20)), availability_lag_min=lag_src,
            sigma_cap=float(prov_search.get("sigma_cap") or 4.0), mode=mode, registry_status=registry_status,
            source=src_label,
        )
        vp, cal, info = _providers(source, spec)
        r = replay_genome(name, g, F=F, days=days, spec=spec, forecast_provider=vp, calibration_provider=cal,
                          fee_regime=fee_regime, prob_cache=prob_cache, log=log)
        r["spec_source"] = src_label
        r["inputs"] = info
        r["calibration_kind"] = cal.kind
        r["spec_calibration_kind"] = spec.calibration.kind
        r["calibration_kind_matches_frame"] = bool(cal.kind == frame_kind)
        r["calibration_payloads"] = len(cal.payload_hashes)
        r["calibration_failures"] = dict(sorted(cal.failures.items()))
        if calibration_kind == "live":
            r["committed_spec_calibration_kind"] = _committed_spec_kind(spec.genome_id)
        results[name] = r
    served_is_frames = all(r["calibration_kind_matches_frame"] for r in results.values())

    gating = [r for r in results.values() if not r["maker_fill_unknowable"]]
    total_disc = sum(r["n_discrepancies"] for r in results.values())
    gating_disc = sum(r["n_discrepancies"] for r in gating)
    p_ok = all(r["p_yes_within_tol"] for r in results.values())
    doc = {
        # Parity EVIDENCE only when the served provider is the kind the frame was proven
        # under: the frame's own calibrator (walk_forward), or a live-built provider of
        # that same kind. Anything else is a diagnostic of a transfer gap.
        "kind": "replay_parity" if served_is_frames else "replay_parity_diagnostic",
        "frames_dir": frames_dir.name,
        "search_sha256": prov_search.get("frame_sha256"),
        "gefs_twin_sha256": (fs.gefs_twin.provenance or {}).get("frame_sha256") if fs.gefs_twin is not None else None,
        "ladder_root": str(ladder_root.relative_to(REPO_ROOT).as_posix()) if str(ladder_root).startswith(str(REPO_ROOT)) else str(ladder_root),
        "n_city_days": len(days),
        "n_markets_ladder": n_ladder_markets,
        "n_markets_search_frame": int(fs.search.n_markets),
        "n_snapshots": n_snapshots,
        "calibration": {
            "dir": cal_dir, "sha256": cal_sha, "replay_kind": calibration_kind,
            "frame_kind": frame_kind, "spec_kind": spec_kind,
            "served_kinds": sorted({str(r["calibration_kind"]) for r in results.values()}),
            "builder": "src.bots.weather_bot.WeatherBot.build_calibration_provider" if calibration_kind == "live" else None,
        },
        "fee_regime_sha256": fee_regime.sha256,
        "fee_type": fee_type_frame,
        "p_yes_tol": P_YES_TOL,
        "skip_columns": list(SKIP_COLUMNS),
        "candle_only_columns": list(CANDLE_ONLY_COLUMNS),
        "genomes": results,
        "n_genomes": len(results),
        "discrepancies_total": total_disc,
        "discrepancies_gating": gating_disc,
        "p_yes_all_within_tol": p_ok,
        "ok": bool(gating_disc == 0 and p_ok),
        "ok_strict": bool(total_disc == 0 and p_ok),
    }
    return doc


def render_table(doc: Dict[str, Any]) -> str:
    lines = [
        f"replay parity on {doc['frames_dir']} (search sha {str(doc['search_sha256'])[:12]}): "
        f"{doc['n_markets_ladder']} ladder markets / {doc['n_markets_search_frame']} frame markets / {doc['n_snapshots']} snapshots",
        f"{'genome':22s} {'mode':5s} {'dir':7s} {'n_markets':>9s} {'n_offline':>9s} {'n_live':>7s} {'n_disc':>7s} {'rows':>7s} {'p_yes_max':>10s} {'colmis':>6s} note",
    ]
    for name, r in doc["genomes"].items():
        note = "maker: fill flags unknowable live" if r["maker_fill_unknowable"] else ("OK" if r["n_discrepancies"] == 0 else "DISCREPANCY")
        lines.append(
            f"{name:22s} {r['mode']:5s} {r['direction']:7s} {r['n_markets']:9d} {r['n_offline']:9d} {r['n_live']:7d} "
            f"{r['n_discrepancies']:7d} {r['rows_compared']:7d} {r['p_yes_max_abs_diff']:10.1e} {sum(r['column_mismatches'].values()):6d} {note}"
        )
    lines.append(f"discrepancies: gating {doc['discrepancies_gating']} / total {doc['discrepancies_total']}; "
                 f"p_yes within {doc['p_yes_tol']:g}: {doc['p_yes_all_within_tol']}; ok={doc['ok']} (strict {doc['ok_strict']})")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frames", default=None, help="frozen frame dir (default newest under data/factory/frames)")
    ap.add_argument("--ladders", default=None, help="ladder root (default the frame provenance's ladder_root)")
    ap.add_argument("--genomes", default="seeds,picks", help="comma list of seeds,picks")
    ap.add_argument("--run-id", default=DEFAULT_RUN_ID, help="F2 run whose picks are replayed")
    ap.add_argument("--only", default=None, help="comma list of genome names to restrict to")
    ap.add_argument("--out", default=None, help="default reports/factory/replay_parity_<sha12>.json")
    ap.add_argument("--strict", action="store_true", help="maker genomes also gate the exit code")
    ap.add_argument("--calibration", default="walk_forward", choices=list(CALIBRATION_KINDS),
                    help="which calibration provider to serve: walk_forward (the frame's own calibrator, "
                         "the FR-F3.4 gating run); frozen (the committed payloads -- a DIAGNOSTIC of the "
                         "transfer gap, never parity evidence); live (whatever WeatherBot."
                         "build_calibration_provider constructs for the spec -- parity evidence iff its "
                         "kind is the frame's)")
    args = ap.parse_args(argv)

    frames_dir = Path(args.frames) if args.frames else latest_frames_dir()
    if frames_dir is None or not frames_dir.exists():
        _die("no frozen frames found; pass --frames DIR")
    try:
        doc = run_parity(
            frames_dir, ladder_root=Path(args.ladders) if args.ladders else None, which=args.genomes,
            run_id=args.run_id, only=[s.strip() for s in args.only.split(",")] if args.only else None,
            calibration_kind=args.calibration,
        )
    except ParityAbort as exc:
        _die(str(exc))
    from src.factory.report import write_json

    suffix = "" if args.calibration == "walk_forward" else f"_{args.calibration}"
    out = Path(args.out) if args.out else REPORTS_ROOT / f"replay_parity_{str(doc['search_sha256'])[:12]}{suffix}.json"
    write_json(out, doc)
    print(render_table(doc))
    print(f"wrote {out}")
    ok = doc["ok_strict"] if args.strict else doc["ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
