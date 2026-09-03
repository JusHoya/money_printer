"""F1 INFRA workstream tests: folds, ledger, registry, report, CLI, compose, .gitignore, Hermes.

PRD_STRATEGY_FACTORY FR-F1.4 / FR-F1.6; docs/factory/FACTORY_ARCHITECTURE.md
sections 6.1-6.3, 7.1, 7.3, 8, 10.

    python -m pytest tests/test_factory_infra.py -q
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.factory import columns as C  # noqa: E402
from src.factory import folds  # noqa: E402
from src.factory import ledger as ledger_mod  # noqa: E402
from src.factory import registry as registry_mod  # noqa: E402
from src.factory import report as report_mod  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _dev_dates_from_ladders() -> list:
    """The 69 development dates, from data/ladders/KXHIGHNY/*.csv or the manifest."""
    ny = REPO / "data" / "ladders" / "KXHIGHNY"
    dates = sorted(p.stem for p in ny.glob("*.csv")) if ny.exists() else []
    if not dates:
        from src.data.kalshi_history import load_manifest

        man = load_manifest()
        dates = sorted(set(d for v in (man.get("files") or {}).values() for d in v) if isinstance(man.get("files"), dict) else [])
    return [d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]


def make_frame(dates, n_markets=6, rows_per_market=5, with_twin=False, name="test", seed=0) -> C.Frame:
    """Synthetic Frame with every column at the contract dtype; rows sorted by (market, ts)."""
    rng = np.random.default_rng(seed)
    dates = np.asarray(sorted(dates), dtype=str)
    n = n_markets * rows_per_market
    markets = np.asarray([f"KXHIGHNY-{i:03d}" for i in range(n_markets)], dtype=str)
    mc = np.repeat(np.arange(n_markets, dtype=np.int32), rows_per_market)
    # each market belongs to one date; spread markets over the dates
    market_date = (np.arange(n_markets) % len(dates)).astype(np.int16)
    dc = market_date[mc]
    ts = (1_750_000_000 + mc.astype(np.int64) * 100_000 + np.tile(np.arange(rows_per_market), n_markets) * 3600).astype(np.int64)
    vis = {}
    for col, dt in C.VISIBLE_DTYPES.items():
        if col == "market_code":
            vis[col] = mc
        elif col == "target_date_code":
            vis[col] = dc
        elif col == "ts_utc":
            vis[col] = ts
        elif col == "city_code":
            vis[col] = (mc % 4).astype(np.int16)
        elif dt == "bool":
            vis[col] = rng.random(n) < 0.7
        elif dt.startswith("int"):
            vis[col] = (rng.integers(0, 3, n)).astype(dt)
        else:
            vis[col] = rng.random(n).astype(dt)
    hid = {}
    for col, dt in C.HIDDEN_DTYPES.items():
        if dt == "bool":
            hid[col] = rng.random(n) < 0.5
        elif dt.startswith("int"):
            hid[col] = rng.integers(-1, 2, n).astype(dt)
        else:
            hid[col] = rng.standard_normal(n).astype(dt)
    block_starts = np.arange(0, n + 1, rows_per_market, dtype=np.int64)
    twin = np.arange(n, dtype=np.int64) if with_twin else None
    f = C.Frame(name=name, visible=vis, hidden=hid, dates=dates, markets=markets,
                block_starts=block_starts, provenance={"synthetic": True}, twin_index=twin)
    f.validate()
    return f


# A deliberately minimal stand-in used ONLY if src.factory.genome is not importable
# yet (concurrent workstream). The real module wins whenever it imports.
@dataclass
class _StandInGenome:
    genes: tuple

    def to_json(self) -> str:
        return json.dumps({"genes": list(self.genes), "standin": True}, sort_keys=True)


def _genomes(k: int):
    try:
        from src.factory import genome as G  # real module (numpy-only)

        seeds = list(getattr(G, "SEEDS", {}).values())
        if seeds:
            return [seeds[i % len(seeds)] for i in range(k)], True
    except Exception:
        pass
    return [_StandInGenome((i, i + 1)) for i in range(k)], False


@dataclass
class _Result:
    fit: float
    constraint_reason: str = ""
    trades: int = 50
    dates: int = 40
    cities: tuple = ("NY", "CHI", "LAX")
    realized: float = 0.05
    realized_se: float = 0.02
    t_stat: float = 2.5
    boot_lo: float = 0.01
    boot_hi: float = 0.09
    worst_date_pnl: float = -0.3
    bss_trades: float = 0.02
    phenotype_hash: str = "ph0"
    per_date_pnl: tuple = (0.1, -0.2, 0.3)
    per_date_codes: tuple = (0, 1, 2)


# ---------------------------------------------------------------------------
# folds
# ---------------------------------------------------------------------------
class TestFolds:
    def test_calendar_counts_on_69_dates(self):
        dates = _dev_dates_from_ladders()
        assert len(dates) == 69, "expected the 69 development ladder dates"
        assert dates[0] == "2026-05-18" and dates[-1] == "2026-07-25"
        cs = folds.campaigns(dates)
        assert set(cs) == {"A", "B", "C", "ALL69"}
        for name, (n_s, n_v) in folds.EXPECTED_COUNTS.items():
            assert len(cs[name].search_dates) == n_s, name
            assert len(cs[name].validation_dates) == n_v, name
        assert cs["A"].embargo_dates == ("2026-06-17", "2026-06-18")
        assert cs["B"].embargo_dates == ("2026-07-01", "2026-07-02")
        assert cs["C"].embargo_dates == ("2026-07-15", "2026-07-16")
        assert cs["ALL69"].embargo_dates == () and cs["ALL69"].validation_dates == ()
        # search / embargo / validation never overlap; anchored at 05-18
        for c in cs.values():
            assert not set(c.search_dates) & set(c.validation_dates)
            assert not set(c.search_dates) & set(c.embargo_dates)
            assert c.search_dates[0] == "2026-05-18"
        # pooled OOS = 33 validation dates
        assert sum(len(cs[k].validation_dates) for k in "ABC") == 33

    def test_campaigns_refuse_post_cutoff_dates(self):
        with pytest.raises(ValueError):
            folds.campaigns(["2026-07-24", "2026-07-26"])

    def test_campaigns_intersect_partial_frames(self):
        cs = folds.campaigns(["2026-05-18", "2026-06-17", "2026-06-20", "2026-07-25"])
        assert cs["A"].search_dates == ("2026-05-18",)
        assert cs["A"].embargo_dates == ("2026-06-17",)
        assert cs["A"].validation_dates == ("2026-06-20",)

    def test_blocked_kfold_purge(self):
        dates = folds.DEV_DATES
        fs = folds.blocked_kfold(dates, k=5, purge_days=2)
        assert len(fs) == 5
        sizes = [len(f.held) for f in fs]
        assert set(sizes) <= {13, 14} and sum(sizes) == 69
        # contiguous, ordered, disjoint
        seen = []
        for f in fs:
            assert list(f.held) == list(dates[len(seen):len(seen) + len(f.held)])
            seen += list(f.held)
        # purge = the 2 calendar-adjacent dates on both sides of the block
        mid = fs[2]
        first, last = mid.held[0], mid.held[-1]
        expected_purge = tuple(sorted(
            [folds.date_range(*(d, d))[0] for d in (
                (np.datetime64(first) - np.timedelta64(2, "D")).astype(str),
                (np.datetime64(first) - np.timedelta64(1, "D")).astype(str),
                (np.datetime64(last) + np.timedelta64(1, "D")).astype(str),
                (np.datetime64(last) + np.timedelta64(2, "D")).astype(str),
            )]
        ))
        assert mid.purge == expected_purge
        assert not set(mid.purge) & set(mid.held)
        assert not set(mid.train) & (set(mid.held) | set(mid.purge))
        assert len(mid.train) == 69 - len(mid.held) - 4
        # edge blocks only purge on the inside
        assert len(fs[0].purge) == 2 and len(fs[-1].purge) == 2

    def test_strip_rows_validates_and_remaps(self):
        dates = ["2026-05-18", "2026-05-19", "2026-05-20"]
        F = make_frame(dates, n_markets=6, rows_per_market=4, with_twin=False)
        keep_dates = ["2026-05-18", "2026-05-20"]
        keep = folds.date_mask(F, keep_dates)
        S = folds.strip_rows(F, keep, stripped_dates=["2026-05-19"])
        S.validate()
        assert S.n_rows == int(keep.sum())
        assert list(S.dates) == keep_dates
        assert S.n_markets == 4 and list(S.markets) == [m for m, d in zip(F.markets, [0, 1, 2, 0, 1, 2]) if d != 1]
        assert set(np.unique(S.visible["market_code"]).tolist()) == {0, 1, 2, 3}
        assert S.block_starts.tolist() == [0, 4, 8, 12, 16]
        assert S.provenance["stripped_dates"] == ["2026-05-19"]
        assert S.provenance["parent_dates"] == dates
        # validation+embargo rows are physically absent
        assert not np.isin(S.dates, ["2026-05-19"]).any()
        # hidden columns sliced identically to visible ones
        assert np.array_equal(S.hidden["won"], F.hidden["won"][keep])

    def test_strip_pair_remaps_twin(self):
        dates = ["2026-05-18", "2026-05-19", "2026-05-20"]
        S0 = make_frame(dates, n_markets=6, rows_per_market=3, with_twin=True, name="search")
        T0 = make_frame(dates, n_markets=6, rows_per_market=3, with_twin=False, name="gefs_twin", seed=1)
        # twin_index: reverse pairing within the same market block (same dates) to exercise the map
        S0.twin_index = np.arange(S0.n_rows, dtype=np.int64)
        S, T = folds.strip_pair(S0, T0, ["2026-05-18", "2026-05-20"])
        S.validate(); T.validate()
        assert S.n_rows == T.n_rows
        assert (S.twin_index >= 0).all()
        # every remapped pointer lands on a twin row with the same market/date
        assert np.array_equal(S.visible["target_date_code"], T.visible["target_date_code"][S.twin_index])
        assert np.array_equal(S.visible["market_code"], T.visible["market_code"][S.twin_index])
        assert "twin_index_refers_to" not in S.provenance
        assert S.provenance["stripped_dates"] == ["2026-05-19"]

    def test_strip_to_campaign_removes_embargo_and_validation(self):
        dates = list(folds.DEV_DATES)
        F = make_frame(dates, n_markets=69, rows_per_market=2)
        cs = folds.campaigns(dates)
        W, _ = folds.strip_to_campaign(F, None, cs["A"])
        assert set(W.dates) == set(cs["A"].search_dates)
        assert not (set(W.dates) & set(cs["A"].validation_dates))
        assert not (set(W.dates) & set(cs["A"].embargo_dates))
        # everything outside the search window is gone: embargo + validation + later dates
        assert set(cs["A"].stripped_dates) <= set(W.provenance["stripped_dates"])
        assert set(W.provenance["stripped_dates"]) == set(dates) - set(cs["A"].search_dates)
        assert W.provenance["campaign"] == "A"


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
class TestLedger:
    def test_write_then_evaluate(self, tmp_path):
        genomes, real = _genomes(4)
        L = ledger_mod.Ledger(tmp_path / "run", "A")
        ids = L.append_unscored(0, genomes)
        assert len(ids) == 4 and ids[0] == "A/g000/00000"
        t = L.read_gen(0)
        assert t.num_rows == 4
        assert set(t.column("status").to_pylist()) == {"UNSCORED"}
        assert all(math.isnan(x) for x in t.column("fitness").to_pylist())
        assert all(t.column("genome_json").to_pylist())
        # "crash" here: rows persist on disk, UNSCORED, no tmp file left behind
        assert L.unscored() == ids
        assert not list((tmp_path / "run" / "ledger" / "A").glob("*.tmp.*"))
        results = [
            _Result(fit=0.01, phenotype_hash="ph_a"),
            _Result(fit=-math.inf, constraint_reason="MIN_TRADES", phenotype_hash="ph_b"),
            _Result(fit=0.02, phenotype_hash="ph_a"),  # duplicate phenotype
            None,
        ]
        L.mark_scored(0, results)
        t = L.read_gen(0)
        assert t.column("status").to_pylist() == ["SCORED", "KILLED", "SCORED", "KILLED"]
        assert t.column("reason").to_pylist() == ["", "MIN_TRADES", "", "NO_RESULT"]
        assert t.column("per_date_pnl").to_pylist()[0] == [0.1, -0.2, 0.3]
        assert t.column("per_date_codes").to_pylist()[0] == [0, 1, 2]
        assert L.phenotypes() == {"ph_a", "ph_b"}
        s = L.summary()
        assert s["n_scored"] == 2 and s["n_killed"] == 2 and s["n_unscored"] == 0
        assert s["n_phenotypes"] == 2 and s["best_fitness"] == 0.02
        assert L.unscored() == []
        # a scored generation is written once
        with pytest.raises(ledger_mod.LedgerError):
            L.append_unscored(0, genomes)
        # the schema is what the docstring says
        assert t.schema.equals(ledger_mod.SCHEMA)
        # the real genome module is what production rows carry
        if real:
            assert '"standin"' not in t.column("genome_json").to_pylist()[0]

    def test_mark_scored_atomic_and_length_checked(self, tmp_path):
        genomes, _ = _genomes(3)
        L = ledger_mod.Ledger(tmp_path / "run", "B")
        L.append_unscored(1, genomes)
        with pytest.raises(ledger_mod.LedgerError):
            L.mark_scored(1, [_Result(0.1)])
        # failed call leaves the UNSCORED file intact
        assert set(L.read_gen(1).column("status").to_pylist()) == {"UNSCORED"}
        L.mark_scored(1, [_Result(0.1, phenotype_hash=f"p{i}") for i in range(3)])
        assert L.read_all().num_rows == 3
        assert L.generations() == [1]
        df = L.read_all(as_pandas=True)
        assert list(df["status"]) == ["SCORED"] * 3

    def test_unscored_generation_can_be_redone(self, tmp_path):
        genomes, _ = _genomes(2)
        L = ledger_mod.Ledger(tmp_path / "run", "C")
        L.append_unscored(0, genomes)
        L.append_unscored(0, genomes)  # crash-resume of an unscored gen is allowed
        assert L.read_gen(0).num_rows == 2


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def _line(self, R, family=registry_mod.FAMILY_F1, **kw):
        base = dict(lane="weather", source="gfs_mex", mode="taker", gene_spec_version=1,
                    config_sha256="abc", budget={"population": 400}, picker="max_boot_lo_ties_fewer_clauses",
                    thresholds={"min_trades": 40}, cutoff="2026-07-25")
        base.update(kw)
        return R.write_family_line(family, **base)

    def test_family_line_before_results(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MP_GIT_REV", "deadbeef")
        R = registry_mod.Registry(tmp_path / "registry.jsonl")
        with pytest.raises(registry_mod.RegistryError):
            R.assert_registered(registry_mod.FAMILY_F1)
        line = self._line(R)
        assert line["status"] == "OPEN" and line["git_rev"] == "deadbeef" and "ts" in line
        assert R.assert_registered(registry_mod.FAMILY_F1)["family"] == registry_mod.FAMILY_F1
        assert R.status(registry_mod.FAMILY_F1) == "OPEN"
        with pytest.raises(registry_mod.RegistryError):
            self._line(R)  # second OPEN line refused
        R.transition(registry_mod.FAMILY_F1, "PROPOSED", genome_id="g1", evidence={"boot_lo": 0.01})
        assert R.status(registry_mod.FAMILY_F1) == "PROPOSED"
        with pytest.raises(registry_mod.RegistryError):
            self._line(R)  # still open
        R.transition(registry_mod.FAMILY_F1, "CLOSED", evidence={"reason": "holm"})
        with pytest.raises(registry_mod.RegistryError):
            R.transition(registry_mod.FAMILY_F1, "PROPOSED")
        with pytest.raises(registry_mod.RegistryError):
            R.transition("weather/x/y/v1", "PROPOSED")
        with pytest.raises(registry_mod.RegistryError):
            R.transition(registry_mod.FAMILY_F1, "BOGUS")
        # append-only: 3 lines, all JSON
        raw = (tmp_path / "registry.jsonl").read_text().splitlines()
        assert len(raw) == 3 and all(json.loads(x) for x in raw)

    def test_family_cap_and_empty_git_rev(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MP_GIT_REV", "cafe")
        R = registry_mod.Registry(tmp_path / "registry.jsonl")
        for i in range(2):
            self._line(R, family=f"lane/src/mode/v{i}", family_cap=2)
        with pytest.raises(registry_mod.RegistryError):
            self._line(R, family="lane/src/mode/v9", family_cap=2)
        monkeypatch.setenv("MP_GIT_REV", "")
        monkeypatch.setattr(registry_mod, "git_rev", lambda *_a, **_k: "")
        with pytest.raises(registry_mod.RegistryError):
            self._line(R, family="lane/src/mode/v3")

    def test_real_git_rev_nonempty_in_checkout(self):
        assert registry_mod.git_rev(REPO)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _minimal_summary():
    row = dict(trades=181, markets=100, city_days=120, dates=65, realized=0.0636, realized_se=0.0248,
               t_stat=2.57, boot_lo=0.0122, boot_hi=0.1086, win_rate=0.6, losing_dates=20,
               worst_date_pnl=-0.3, mean_price_paid=0.4, mean_fee=0.01, fit=0.0122, constraint_reason="")
    return {
        "run_id": "gen0_2026-09-02", "kind": "gen0", "family": registry_mod.FAMILY_F1, "git_rev": "abc",
        "lock_sha256": "l", "fee_regime_sha256": "f",
        "frame": {"parity_sha256": "p" * 64, "search_sha256": "s" * 64, "gefs_twin_sha256": "g" * 64,
                  "parity_rows": 1000, "search_rows": 900, "provenance": {"path": "C:\\x\\y"}},
        "registry_line": {"status": "OPEN", "picker": "max_boot_lo_ties_fewer_clauses", "config_sha256": "c" * 64, "cutoff": "2026-07-25"},
        "seeds": {
            "fr31a_taker": {"genome": {}, "notes": "", "phenotype_hash": "h1", "parity_full": row, "search_full": row,
                            "campaigns": {"A": {"search": row, "validation": row}, "B": {"search": row, "validation": row}, "C": {"search": row, "validation": row}},
                            "reference": {"label": "fr31a", "matches_1e9": True, "fields_differing": []}},
            "nofilter_no": {"phenotype_hash": "h2", "parity_full": dict(row, trades=664, realized=0.0209)},
            "mlweather_fallback": {"phenotype_hash": "h3", "notes": "confidence 1.000 fallback", "parity_full": dict(row, fit=-math.inf, constraint_reason="MIN_TRADES")},
        },
        "brier_skill_vs_market": {"bss": -0.02, "ci_lo": -0.05, "ci_hi": 0.01, "n_rows": 5000, "n_dates": 69},
        "throughput": {"evals_per_s": 3500.0, "workers": 16, "peak_rss_mb": 900.0, "host": "alcyone"},
    }


class TestReport:
    def test_write_gen0_report(self, tmp_path):
        root = tmp_path / "reports" / "factory"
        out = root / "gen0_2026-09-02"
        coverage = {"lanes": {"weather": {"status": "READY", "n_units": 69, "independent_unit": "target_date"},
                              "gas": {"status": "NOT_PROMOTABLE", "n_units": 14, "next_data_eta": "2026-10"}}}
        paths = report_mod.write_gen0_report(_minimal_summary(), out, coverage=coverage)
        for p in paths.values():
            assert p.exists(), p
        sj = json.loads(paths["summary_json"].read_text())
        assert sj["frame"]["provenance"]["path"] == "C:/x/y"  # separators normalised
        assert sj["seeds"]["mlweather_fallback"]["parity_full"]["fit"] is None  # -inf -> null
        text = paths["summary_json"].read_text()
        assert text == json.dumps(json.loads(text), sort_keys=True, indent=2) + "\n"
        md = paths["summary_md"].read_text(encoding="utf-8")
        assert registry_mod.FAMILY_F1 in md and "OPEN" in md
        assert "matches within 1e-9: **yes**" in md
        assert "`mlweather_fallback`" in md and "KILLED:MIN_TRADES" in md
        assert "Brier skill" in md and "3500.0 evals/s" in md
        board = paths["board_md"].read_text(encoding="utf-8")
        assert "| weather |" in board and "| gas |" in board and "| PAPER |" in board
        assert "n/a (F3)" in board and "n/a (F2)" in board
        assert "NOT_PROMOTABLE(14)" in board
        for col in report_mod.BOARD_COLUMNS:
            assert col in board
        latest = json.loads(paths["latest_json"].read_text())
        assert latest["summary"] == "gen0_2026-09-02/summary.json"
        assert latest["board"] == "gen0_2026-09-02/board.md"
        assert latest["headline"]["parity_check"]["matches_1e9"] is True
        assert latest["headline"]["n_phenotypes"] == 3

    def test_status_json_timestamp_free(self):
        st = report_mod.render_status_json(_minimal_summary())
        json.dumps(st)

        def walk(o, path=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    lk = k.lower()
                    assert not any(h in lk for h in ("timestamp", "time", "_at", "generated", "created", "updated", "now")), path + "/" + k
                    walk(v, path + "/" + k)
            elif isinstance(o, list):
                for v in o:
                    walk(v, path)
            elif isinstance(o, str):
                assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", o), o

        walk(st)
        assert st["run_id"] == "gen0_2026-09-02" and st["parity_matches_1e9"] is True

    def test_render_tolerates_missing_keys(self):
        md = report_mod.render_summary_md({"run_id": "x"})
        assert report_mod.DASH in md
        board = report_mod.render_board({"run_id": "x"}, None, None)
        assert "| weather |" in board
        board2 = report_mod.render_board(None, {"weather": {"status": "READY"}}, None)
        assert "| weather | READY |" in board2
        st = report_mod.render_status_json({})
        assert st["run_id"] is None

    def test_board_is_deterministic(self):
        s = _minimal_summary()
        assert report_mod.render_board(s, None) == report_mod.render_board(json.loads(json.dumps(s)), None)


# ---------------------------------------------------------------------------
# CLI, compose, .gitignore, config
# ---------------------------------------------------------------------------
class TestCliAndDeploy:
    def test_factory_help(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "factory.py"), "--help"],
                           capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r.returncode == 0, r.stderr
        for sub in ("freeze-frame", "gen0", "board", "coverage", "status", "run", "holdout"):
            assert sub in r.stdout

    def test_stubs_exit_2(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "factory.py"), "run", "--config", "x"],
                           capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r.returncode == 2 and "not implemented in F1" in r.stderr

    def test_compose_factory_services(self):
        doc = yaml.safe_load((REPO / "deploy" / "spark" / "docker-compose.lab.yml").read_text(encoding="utf-8"))
        svcs = doc["services"]
        assert {"lab", "factory", "factory-holdout"} <= set(svcs)
        # lab unchanged in the ways that matter
        assert "network_mode" not in svcs["lab"] and svcs["lab"]["volumes"][0] == "../..:/app"
        for name in ("factory", "factory-holdout"):
            s = svcs[name]
            assert s["network_mode"] == "none", name
            assert s["cpuset"] == "0-3,5-9,10-11,15-19", name
            assert "gpus" not in s and "deploy" not in s, name
            assert s["cpu_shares"] == 512 and s["mem_limit"] == "24g"
            assert s["user"] == "${LAB_UID:-1000}:${LAB_GID:-1000}"
            env = s["environment"]
            assert env["HOME"] == "/tmp" and env["TZ"] == "UTC" and env["PYTHONPATH"] == "/app"
            assert str(env["PYTHONDONTWRITEBYTECODE"]) == "1"
            assert s["entrypoint"] == ["nice", "-n", "10"]
            vols = s["volumes"]
            assert "../..:/app:ro" in vols
            assert "../../data/factory:/app/data/factory:rw" in vols
            assert "../../reports/factory:/app/reports/factory:rw" in vols
            assert not any(isinstance(v, str) and "/archive" in v for v in vols), name
            logs = [v for v in vols if isinstance(v, dict) and v.get("target") == "/app/logs"]
            # PyYAML is YAML 1.1 and leaves `0o1777` as a string; compose (YAML 1.2)
            # reads it as the octal int -- exactly the literal the lab service needs.
            assert logs and logs[0]["type"] == "tmpfs" and logs[0]["tmpfs"]["mode"] in (0o1777, "0o1777")
        # factory: sealed roots are NOT bound; they are masked by empty read-only tmpfs
        fv = svcs["factory"]["volumes"]
        assert not any(isinstance(v, str) and ("ladders_holdout" in v or "ladders_2026-09" in v) for v in fv)
        masks = {v["target"]: v for v in fv if isinstance(v, dict) and v.get("type") == "tmpfs"}
        for sealed in ("/app/data/ladders_holdout", "/app/data/ladders_2026-09"):
            assert sealed in masks and masks[sealed].get("read_only") is True
        # factory-holdout: sealed roots bound read-only, and only there
        hv = svcs["factory-holdout"]["volumes"]
        assert "../../data/ladders_holdout:/app/data/ladders_holdout:ro" in hv
        assert "../../data/ladders_2026-09:/app/data/ladders_2026-09:ro" in hv

    def test_gitignore_entries(self):
        gi = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        for entry in ("data/factory/", "reports/factory/*/gen_*", "reports/factory/*/controls/"):
            assert entry in gi, entry
        for keep in ("reports/factory/*/summary.*", "reports/factory/", "reports/factory/*", "registry.jsonl", "latest.json", "coverage.json", "board.md"):
            assert keep not in gi, keep

    def test_family_config(self):
        cfg = yaml.safe_load((REPO / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml").read_text(encoding="utf-8"))
        assert cfg["family"] == registry_mod.FAMILY_F1
        assert cfg["gene_spec_version"] == 1 and cfg["frame"]["cutoff"] == "2026-07-25"
        assert cfg["frame"]["availability_lag_min"] == 240 and cfg["frame"]["sigma_cap"] == 4.0
        assert cfg["campaigns"] == ["A", "B", "C", "ALL69"]
        assert cfg["picker"] == "max_boot_lo_ties_fewer_clauses"
        t = cfg["thresholds"]
        assert t["min_trades"] == 40 and t["min_dates_frac"] == 0.6 and t["min_cities"] == 3
        assert t["worst_date_pnl_min"] == -0.5 and t["bss_trades_min"] == -0.05 and t["max_clauses"] == 8
        assert t["holm_alpha"] == 0.05 and t["p_rc_all69_lt"] == 0.10 and t["pooled_boot_lo_gt"] == 0.0
        assert cfg["budget"]["placeholder"] is True
        assert cfg["family_cap"] == 6
        assert set(cfg["seeds"]) == {"fr31a_taker", "fr31b", "nofilter_no", "salvage_5f", "mlweather_fallback", "fr31a_gefs", "far_yes_taker"}

    def test_systemd_unit(self):
        import configparser

        unit_path = REPO / "deploy" / "spark" / "systemd" / "mp-factory@.service"
        unit = unit_path.read_text(encoding="utf-8")
        assert "\r" not in unit, "unit must be LF (systemd parser)"
        cp = configparser.ConfigParser(strict=False, interpolation=None, allow_no_value=True)
        cp.optionxform = str  # systemd keys are case-sensitive
        cp.read_string(unit)
        assert set(cp.sections()) >= {"Unit", "Service"}
        u, s = cp["Unit"], cp["Service"]
        assert "%i" in u["Description"] and u["StartLimitBurst"] == "5" and u["StartLimitIntervalSec"] == "1h"
        assert s["Type"] == "oneshot" and s["Restart"] == "on-failure" and s["RestartSec"] == "30"
        assert s["TimeoutStartSec"] == "6h" and s["Nice"] == "10"
        assert s["StandardOutput"] == "journal" and s["StandardError"] == "journal"
        assert s["WorkingDirectory"] == "%h/projects/money_printer"
        assert s["ExecStart"].endswith("deploy/spark/mp_factory_run.sh %i")
        assert "MP_REPO_DIR=%h/projects/money_printer" in unit and "TZ=UTC" in unit
        # the old direct compose ExecStart is gone: the wrapper owns run -> controls -> report -> notify
        assert "factory.py resume %i" not in unit

    def test_board_monitor_script_conventions(self):
        sh = (REPO / "hermes_plugin" / "scripts" / "mp_factory_board.sh").read_text(encoding="utf-8")
        assert sh.startswith("#!/usr/bin/env bash")
        assert "set -u" in sh and "sha256sum" in sh and "MONEY_PRINTER_FACTORY_DIR" in sh
        assert "--no-agent" in sh and "discord:1491982736989093961" in sh
        assert "--provider custom --model ykarout/Qwen3.5-9B-NVFP4" in sh
        assert "mp_factory_board.sha" in sh


# ---------------------------------------------------------------------------
# Hermes plugin
# ---------------------------------------------------------------------------
class TestHermesPlugin:
    def _plugin(self):
        import importlib

        sys.path.insert(0, str(REPO))
        import hermes_plugin  # noqa: WPS433

        return importlib.reload(hermes_plugin)

    def test_tools_registered(self):
        hp = self._plugin()
        assert "mp_factory_status" in hp.TOOLS and "mp_factory_board" in hp.TOOLS
        assert len(hp.TOOLS) == 15
        registered = {}

        class Ctx:
            def register_tool(self, name, toolset, schema, handler, description):
                registered[name] = (toolset, schema, handler, description)

        hp.register(Ctx())
        assert {"mp_factory_status", "mp_factory_board"} <= set(registered)
        assert registered["mp_factory_status"][3]
        y = yaml.safe_load((REPO / "hermes_plugin" / "plugin.yaml").read_text(encoding="utf-8"))
        assert {"mp_factory_status", "mp_factory_board"} <= set(y["provides_tools"])
        assert "15 tools" in hp.__doc__

    def test_missing_dir_is_graceful(self, tmp_path, monkeypatch):
        hp = self._plugin()
        monkeypatch.setenv("MONEY_PRINTER_FACTORY_DIR", str(tmp_path / "nope"))
        for fn in (hp.get_factory_status, hp.get_factory_board):
            body = json.loads(fn({}))
            assert body["ok"] is False and "MONEY_PRINTER_FACTORY_DIR" in body["error"]
        monkeypatch.setenv("MONEY_PRINTER_FACTORY_DIR", str(tmp_path))
        body = json.loads(hp.get_factory_status({}))
        assert body["ok"] is False and "no factory run yet" in body["error"]

    def test_reads_written_report(self, tmp_path, monkeypatch):
        hp = self._plugin()
        root = tmp_path / "reports" / "factory"
        report_mod.write_gen0_report(_minimal_summary(), root / "gen0_2026-09-02", coverage={"lanes": {"weather": {"status": "READY"}}})
        (root / "coverage.json").write_text(json.dumps({"lanes": {"weather": {"status": "READY"}}}))
        monkeypatch.setenv("MONEY_PRINTER_FACTORY_DIR", str(root))
        st = json.loads(hp.get_factory_status({}))
        assert st["ok"] and st["run_id"] == "gen0_2026-09-02" and st["registry_status"] == "OPEN"
        assert st["parity_fr31a"]["kernel"]["trades"] == 181 and st["parity_fr31a"]["matches_1e9"] is True
        assert st["seeds"]["nofilter_no"]["parity_full"]["trades"] == 664
        assert "mlweather_fallback" in st["seeds"]
        assert st["throughput"]["evals_per_s"] == 3500.0
        # the numbers are the summary's numbers, not re-derived
        sj = json.loads((root / "gen0_2026-09-02" / "summary.json").read_text())
        assert st["brier_skill_vs_market"] == sj["brier_skill_vs_market"]
        # the gen-0 report carries a status pointer, so active_run is the gen-0 status.json
        assert st["active_run"]["run_id"] == "gen0_2026-09-02" and st["active_run"]["status"]["phase"] == "F1"
        b = json.loads(hp.get_factory_board({}))
        assert b["ok"] and b["board_md"].startswith("# Factory board") and b["coverage"]["lanes"]["weather"]["status"] == "READY"

    def test_active_run_status(self, tmp_path, monkeypatch):
        """F2: latest.json {active_run, status} -> the run's status.json, completion.txt, bench compare."""
        hp = self._plugin()
        assert hp.PLUGIN_VERSION == "2.3.0"
        y = yaml.safe_load((REPO / "hermes_plugin" / "plugin.yaml").read_text(encoding="utf-8"))
        assert str(y["version"]) == hp.PLUGIN_VERSION
        root = tmp_path / "reports" / "factory"
        run = root / "run_2026-09-04"
        run.mkdir(parents=True)
        status = {"run_id": "run_2026-09-04", "state": "RUNNING", "phase": "evolve", "campaign": "B", "gen": 17,
                  "n_gens": 60, "best_fit": 0.0123, "n_phenotypes": 812, "evaluations": 6800,
                  "picks_done": ["A"], "controls_done": {}}
        (run / "status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")
        (run / "completion.txt").write_text("factory run run_2026-09-04: DONE\nverdict: CLOSED\n")
        (run / "bench.json").write_text(json.dumps({"mp_vllm": {"compare": {"p50_change_pct": 3.1, "pass": True}},
                                                    "factory": {"throughput": {"evals_per_s": 6100.0}}}))
        monkeypatch.setenv("MONEY_PRINTER_FACTORY_DIR", str(root))
        # 1. an active run with NO summary yet is ok:true with active_run populated
        (root / "latest.json").write_text(json.dumps({"active_run": "run_2026-09-04", "status": "run_2026-09-04/status.json",
                                                      "board": "gen0_2026-09-02/board.md"}))
        st = json.loads(hp.get_factory_status({}))
        assert st["ok"] and st["summary_path"] is None
        ar = st["active_run"]
        assert ar["run_id"] == "run_2026-09-04" and ar["state"] == "RUNNING" and ar["gen"] == 17 and ar["n_gens"] == 60
        assert ar["campaign"] == "B" and ar["best_fit"] == 0.0123 and ar["evaluations"] == 6800 and ar["picks_done"] == ["A"]
        assert ar["status"] == status
        assert ar["completion"].startswith("factory run run_2026-09-04: DONE")
        assert ar["bench"]["mp_vllm_compare"]["pass"] is True and ar["bench"]["factory_throughput"]["evals_per_s"] == 6100.0
        # 2. alongside the gen-0 headline once a summary pointer exists
        report_mod.write_gen0_report(_minimal_summary(), root / "gen0_2026-09-02")
        latest = json.loads((root / "latest.json").read_text())
        latest.update({"active_run": "run_2026-09-04", "status": "run_2026-09-04/status.json"})
        (root / "latest.json").write_text(json.dumps(latest))
        st = json.loads(hp.get_factory_status({}))
        assert st["ok"] and st["run_id"] == "gen0_2026-09-02" and st["parity_fr31a"]["kernel"]["trades"] == 181
        assert st["active_run"]["run_id"] == "run_2026-09-04" and st["active_run"]["state"] == "RUNNING"
        # 3. a dangling status pointer is reported, not raised
        latest["status"] = "run_missing/status.json"
        (root / "latest.json").write_text(json.dumps(latest))
        st = json.loads(hp.get_factory_status({}))
        assert st["ok"] and st["active_run"]["error"] == "status.json missing"
        # board semantics unchanged
        b = json.loads(hp.get_factory_board({}))
        assert b["ok"] and b["board_path"] == "gen0_2026-09-02/board.md"


# ---------------------------------------------------------------------------
# F2 INFRA: unit wrapper / notify / monitor scripts, coexistence bench,
# registry CLOSED refusal, gen0 overwrite refusal, .gitignore
# ---------------------------------------------------------------------------
def _bash():
    """A bash that runs the repo's POSIX scripts (Git Bash on Windows); None when absent."""
    import shutil

    cands = [os.environ.get("MP_TEST_BASH"), r"C:\Program Files\Git\bin\bash.exe",
             r"C:\Program Files\Git\usr\bin\bash.exe", shutil.which("bash")]
    for c in cands:
        if c and os.path.exists(c):
            try:
                r = subprocess.run([c, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and "ok" in r.stdout:
                return c
    return None


def _sh(script: Path, *args: str, env=None, cwd=None, timeout=120):
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run([bash, script.as_posix(), *args], capture_output=True, text=True,
                          env=full, cwd=str(cwd or REPO), timeout=timeout)


SCRIPTS = [
    REPO / "deploy" / "spark" / "mp_factory_run.sh",
    REPO / "deploy" / "spark" / "mp_factory_notify.sh",
    REPO / "deploy" / "spark" / "install_factory_unit.sh",
    REPO / "hermes_plugin" / "scripts" / "mp_factory_monitor.sh",
]


class TestF2Scripts:
    def test_scripts_are_lf_and_pass_bash_n(self):
        bash = _bash()
        for p in SCRIPTS:
            text = p.read_text(encoding="utf-8")
            assert text.startswith("#!/usr/bin/env bash"), p
            assert "\r" not in text, f"{p} must be LF"
            if bash:
                r = subprocess.run([bash, "-n", p.as_posix()], capture_output=True, text=True, timeout=60)
                assert r.returncode == 0, f"{p}: {r.stderr}"
        if bash is None:
            pytest.skip("no bash: syntax not checked")

    def test_wrapper_conventions(self):
        sh = (REPO / "deploy" / "spark" / "mp_factory_run.sh").read_text(encoding="utf-8")
        assert "set -euo pipefail" in sh
        assert 'run --rm factory python scripts/factory.py "$@"' in sh
        for step in ('run --run-id "$RUN_ID"', 'controls "$RUN_ID"', 'report "$RUN_ID"'):
            assert step in sh, step
        assert "--resume" in sh and 'run.json' in sh                      # auto-resume
        assert "free -g" in sh and "resources.log" in sh and "SAMPLE_S" in sh
        assert "mp_factory_notify.sh" in sh and '"$STATE"' in sh
        assert "trap finish EXIT" in sh                                   # sampler stopped + notify on every exit
        assert "factory_bench_coexist.py" in sh and "--throughput-from" in sh
        assert "git add" not in sh                                        # the wrapper never commits

    def test_notify_conventions(self):
        sh = (REPO / "deploy" / "spark" / "mp_factory_notify.sh").read_text(encoding="utf-8")
        assert "set -euo pipefail" in sh and "completion.txt" in sh
        assert "discord:1491982736989093961" in sh
        assert 'send --to "$TARGET" --subject "$subject" --file "$OUT"' in sh   # form verified on alcyone 2026-09-03
        assert "exit 0" in sh                                                   # delivery failure is never fatal

    def test_notify_writes_completion_without_hermes(self, tmp_path):
        run = tmp_path / "reports" / "factory" / "run_x"
        run.mkdir(parents=True)
        (run / "summary.json").write_text(json.dumps({
            "family": registry_mod.FAMILY_F1, "verdict": "CLOSED",
            "pooled": {"mean": 0.0123, "se": 0.01, "t_stat": 1.23, "boot_lo": -0.0071, "boot_hi": 0.0317, "n_dates": 33},
        }))
        env = {"MP_REPO_DIR": tmp_path.as_posix(), "MP_FACTORY_NO_SEND": "1", "MP_FACTORY_WALL_S": "5400",
               "MP_FACTORY_USED_GIB": "27/28/31"}
        r = _sh(REPO / "deploy" / "spark" / "mp_factory_notify.sh", "run_x", "DONE", env=env)
        assert r.returncode == 0, r.stderr
        body = (run / "completion.txt").read_text(encoding="utf-8")
        lines = body.splitlines()
        assert lines[0] == "factory run run_x: DONE"
        assert "family: weather/gfs_mex/taker/v1" in lines and "verdict: CLOSED" in lines
        assert any(l.startswith("pooled OOS (33 validation dates): mean=+0.0123 se=+0.0100 t=+1.23 boot=[-0.0071,+0.0317] n_dates=33") for l in lines)
        assert "wall_s: 5400" in lines and "used_gib before/after/peak: 27/28/31" in lines
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", body)     # timestamp-free
        # no summary at all: still written, with n/a fields; FAILED state accepted; bad args exit 2
        r = _sh(REPO / "deploy" / "spark" / "mp_factory_notify.sh", "run_y", "FAILED", env=env)
        assert r.returncode == 0, r.stderr
        b2 = (tmp_path / "reports" / "factory" / "run_y" / "completion.txt").read_text()
        assert b2.startswith("factory run run_y: FAILED") and "verdict: n/a" in b2
        r = _sh(REPO / "deploy" / "spark" / "mp_factory_notify.sh", "run_y", "MAYBE", env=env)
        assert r.returncode == 2
        # a hermes binary that fails must not change the exit code
        fake = tmp_path / "hermes"
        fake.write_text("#!/usr/bin/env bash\nexit 1\n")
        fake.chmod(0o755)
        env2 = dict(env, HERMES_BIN=fake.as_posix())
        env2.pop("MP_FACTORY_NO_SEND")
        r = _sh(REPO / "deploy" / "spark" / "mp_factory_notify.sh", "run_x", "DONE", env=env2)
        assert r.returncode == 0 and "hermes send failed" in r.stderr

    def test_wrapper_dry_run_and_arg_validation(self, tmp_path):
        run = REPO / "deploy" / "spark" / "mp_factory_run.sh"
        r = _sh(run, "bad id!", env={"MP_FACTORY_DRY_RUN": "1"})
        assert r.returncode == 2 and "bad run_id" in r.stderr
        r = _sh(run, env={"MP_FACTORY_DRY_RUN": "1"})
        assert r.returncode == 2 and "usage" in r.stderr
        # dry run against a throwaway "checkout": plan printed, resume detected, docker never called
        fake_repo = tmp_path / "repo"
        (fake_repo / "deploy" / "spark").mkdir(parents=True)
        (fake_repo / "deploy" / "spark" / "docker-compose.lab.yml").write_text("services: {}\n")
        (fake_repo / "data" / "factory" / "runs" / "run_z").mkdir(parents=True)
        (fake_repo / "data" / "factory" / "runs" / "run_z" / "run.json").write_text("{}")
        subprocess.run(["git", "init", "-q", str(fake_repo)], check=True, timeout=60)
        subprocess.run(["git", "-C", str(fake_repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True, timeout=60)
        r = _sh(run, "run_z", env={"MP_FACTORY_DRY_RUN": "1", "MP_REPO_DIR": fake_repo.as_posix()}, cwd=fake_repo)
        assert r.returncode == 0, r.stderr + r.stdout
        assert "resuming (--resume)" in r.stdout and "dry run: stopping before docker" in r.stdout
        assert "plan: run --run-id run_z --resume" in r.stdout
        assert (fake_repo / "reports" / "factory" / "run_z").is_dir()      # bind dirs created up front

    def test_monitor_conventions(self):
        sh = (REPO / "hermes_plugin" / "scripts" / "mp_factory_monitor.sh").read_text(encoding="utf-8")
        assert "set -u" in sh and "sha256sum" in sh and "MONEY_PRINTER_FACTORY_DIR" in sh
        assert "hermes cron create 10m --name mp-factory-monitor --no-agent" in sh
        assert "--script mp_factory_monitor.sh --deliver discord:1491982736989093961" in sh
        assert "--provider custom --model ykarout/Qwen3.5-9B-NVFP4" in sh
        assert "mp_factory_monitor.sha" in sh and "completion.txt" in sh

    def test_monitor_posts_only_on_change(self, tmp_path):
        mon = REPO / "hermes_plugin" / "scripts" / "mp_factory_monitor.sh"
        root = tmp_path / "reports" / "factory"
        run = root / "run_x"
        run.mkdir(parents=True)
        env = {"MONEY_PRINTER_FACTORY_DIR": root.as_posix(), "MP_FACTORY_MONITOR_STATE": (tmp_path / "state" / "m.sha").as_posix()}
        # no latest.json -> silent, exit 0
        r = _sh(mon, env=env)
        assert r.returncode == 0 and r.stdout == ""
        # latest.json without a status pointer -> silent
        (root / "latest.json").write_text(json.dumps({"board": "run_x/board.md"}))
        r = _sh(mon, env=env)
        assert r.returncode == 0 and r.stdout == ""
        # active run -> one compact line
        (root / "latest.json").write_text(json.dumps({"active_run": "run_x", "status": "run_x/status.json", "board": "run_x/board.md"}))
        status = {"run_id": "run_x", "state": "RUNNING", "phase": "evolve", "campaign": "A", "gen": 3, "n_gens": 60,
                  "best_fit": 0.0123, "n_phenotypes": 812, "evaluations": 1600, "picks_done": [], "controls_done": {}}
        (run / "status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")
        r = _sh(mon, env=env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.splitlines() == ["factory run_x RUNNING evolve A gen 3/60 best_fit +0.0123 phenotypes 812 evals 1600"]
        # same bytes -> silent
        r = _sh(mon, env=env)
        assert r.stdout == ""
        # progress -> posts again
        status.update({"gen": 4, "evaluations": 2000, "best_fit": 0.02})
        (run / "status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")
        r = _sh(mon, env=env)
        assert r.stdout.splitlines() == ["factory run_x RUNNING evolve A gen 4/60 best_fit +0.0200 phenotypes 812 evals 2000"]
        # completion.txt appears -> line + its content, once
        (run / "completion.txt").write_text("factory run run_x: DONE\nverdict: CLOSED\n")
        r = _sh(mon, env=env)
        out = r.stdout.splitlines()
        assert out[0].startswith("factory run_x RUNNING") and out[1:] == ["factory run run_x: DONE", "verdict: CLOSED"]
        r = _sh(mon, env=env)
        assert r.stdout == ""
        # a later status change repeats the line but NOT the completion text
        status.update({"state": "DONE", "phase": "report"})
        (run / "status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")
        r = _sh(mon, env=env)
        assert r.stdout.splitlines() == ["factory run_x DONE report A gen 4/60 best_fit +0.0200 phenotypes 812 evals 2000"]
        # gen-0 style status.json (no state/gen keys) still yields a line with dashes
        (run / "status.json").write_text(json.dumps({"run_id": "gen0_x", "kind": "gen0", "phase": "F1"}) + "\n")
        r = _sh(mon, env=env)
        assert r.stdout.splitlines() == ["factory gen0_x - F1 - gen -/- best_fit - phenotypes - evals -"]

    def test_install_script_conventions(self):
        sh = (REPO / "deploy" / "spark" / "install_factory_unit.sh").read_text(encoding="utf-8")
        assert 'UNIT_DIR="$HOME/.config/systemd/user"' in sh and 'mkdir -p "$UNIT_DIR"' in sh
        assert "systemctl --user daemon-reload" in sh and "enable-linger" in sh
        assert "sudo install" not in sh and "sudo systemctl" not in sh    # user unit: no root (the usermod hint is text)
        # nothing is enabled or started by the installer: only comments / echo hints mention start
        code = [l for l in sh.splitlines() if l.strip() and not l.lstrip().startswith("#") and "echo" not in l]
        assert not any("systemctl --user enable" in l or "systemctl --user start" in l for l in code)


class _FakeVLLM:
    """Minimal OpenAI-compatible streaming server: role chunk, N content chunks, usage, [DONE]."""

    def __init__(self, n_tokens=6, gap_s=0.003, ttft_s=0.01):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        import time as _time

        outer = self
        self.requests = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def chunk(delta, **extra):
                    d = {"id": "x", "object": "chat.completion.chunk", "model": body.get("model"),
                         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    d.update(extra)
                    return ("data: " + json.dumps(d) + "\n\n").encode()

                self.wfile.write(b": keep-alive\n\n")
                self.wfile.write(chunk({"role": "assistant"}))
                self.wfile.flush()
                _time.sleep(ttft_s)
                for i in range(n_tokens):
                    self.wfile.write(chunk({"content": f"tok{i} "}))
                    self.wfile.flush()
                    _time.sleep(gap_s)
                self.wfile.write(b'data: {"choices": [], "usage": {"completion_tokens": %d}}\n\n' % n_tokens)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *a):  # silence
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake_vllm():
    s = _FakeVLLM()
    try:
        yield s
    finally:
        s.close()


def _bench_mod():
    import importlib.util

    spec = importlib.util.spec_from_file_location("factory_bench_coexist", REPO / "scripts" / "factory_bench_coexist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBenchCoexist:
    def test_stdlib_only(self):
        src = (REPO / "scripts" / "factory_bench_coexist.py").read_text(encoding="utf-8")
        for bad in ("import numpy", "import requests", "import pandas", "from src."):
            assert bad not in src, bad

    def test_sse_parsing_with_fake_clock(self):
        b = _bench_mod()
        lines = [
            b": keep-alive\n",
            b"\n",
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            b"event: ping\n",
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
            b'data: {"choices":[{"delta":{}}]}\n',
            b'data: not json\n',
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n',
            b'data: {"choices":[],"usage":{"completion_tokens":3}}\n',
            b"data: [DONE]\n",
            b'data: {"choices":[{"delta":{"content":"IGNORED after DONE"}}]}\n',
        ]
        ticks = iter([1.0, 1.5, 2.5, 3.0])      # one per content chunk, then the end stamp
        s = b.time_stream(lines, t_send=0.4, clock=lambda: next(ticks))
        assert s["n_tokens"] == 3 and s["n_chars"] == len("Hello world")
        assert s["ttft_ms"] == pytest.approx(600.0)
        assert s["inter_token_ms"] == pytest.approx([500.0, 1000.0])
        assert s["gen_ms"] == pytest.approx(1500.0) and s["total_ms"] == pytest.approx(2600.0)
        assert s["usage"] == {"completion_tokens": 3}
        assert list(b.sse_payloads([b"data: [DONE]", b'data: {"a":1}'])) == []

    def test_percentile_and_aggregate(self):
        b = _bench_mod()
        assert b.percentile([], 50) is None and b.percentile([7.0], 90) == 7.0
        assert b.percentile([1, 2, 3, 4], 50) == 2.5 and b.percentile([1, 2, 3, 4], 90) == pytest.approx(3.7)
        samples = [
            {"ttft_ms": 100.0, "inter_token_ms": [10.0, 20.0, 30.0], "n_tokens": 4, "gen_ms": 60.0, "total_ms": 200.0},
            {"ttft_ms": 120.0, "inter_token_ms": [10.0, 10.0, 10.0], "n_tokens": 4, "gen_ms": 30.0, "total_ms": 180.0},
        ]
        a = b.aggregate(samples, label="idle", endpoint="e", model="m", max_tokens=8)
        assert a["n"] == 2 and a["p50_inter_token_ms"] == 10.0 and a["p90_inter_token_ms"] == 25.0
        assert a["p50_ttft_ms"] == 110.0 and a["tokens_per_s"] == pytest.approx(8 / 0.09, rel=1e-3)
        assert a["prompt_sha256"] == __import__("hashlib").sha256(b.FIXED_PROMPT.encode()).hexdigest()
        assert len(a["samples"]) == 2

    def test_compare_math(self):
        b = _bench_mod()
        c = b.compare({"p50_inter_token_ms": 20.0, "p50_ttft_ms": 100.0}, {"p50_inter_token_ms": 22.0, "p50_ttft_ms": 110.0})
        assert c["p50_change_pct"] == 10.0 and c["pass"] is True and c["ttft_change_pct"] == 10.0
        c = b.compare({"p50_inter_token_ms": 20.0}, {"p50_inter_token_ms": 23.0})
        assert c["p50_change_pct"] == 15.0 and c["pass"] is False
        c = b.compare({"p50_inter_token_ms": 20.0}, {"p50_inter_token_ms": 17.0})
        assert c["p50_change_pct"] == -15.0 and c["pass"] is False           # |change| counts both ways
        c = b.compare({"p50_inter_token_ms": 20.0}, {"p50_inter_token_ms": 18.5}, threshold_pct=10)
        assert c["p50_change_pct"] == -7.5 and c["pass"] is True
        assert b.compare(None, {"p50_inter_token_ms": 1.0})["pass"] is None
        assert b.compare({"p50_inter_token_ms": 0.0}, {"p50_inter_token_ms": 1.0})["pass"] is None

    def test_throughput_from_tolerates_absence(self, tmp_path):
        b = _bench_mod()
        t = b.throughput_from([str(tmp_path / "missing.json")])
        assert t["evaluations"] is None and t["evals_per_s"] is None and t["sources"][0]["found"] is False
        st = tmp_path / "status.json"
        st.write_text(json.dumps({"run_id": "r", "state": "DONE", "evaluations": 216000, "gen": 60, "n_gens": 60}))
        t = b.throughput_from([str(st), str(tmp_path / "run.json")], wall_s=120.0)
        assert t["evaluations"] == 216000 and t["wall_s"] == 120.0 and t["evals_per_s"] == 1800.0
        assert t["state"] == "DONE" and t["run_id"] == "r"
        rj = tmp_path / "run.json"
        rj.write_text(json.dumps({"throughput": {"evaluations": 5, "wall_s": 2.0, "evals_per_s": 2.5}}))
        t = b.throughput_from([str(rj)])
        assert t["evaluations"] == 5 and t["evals_per_s"] == 2.5 and t["evals_per_s_reported"] == 2.5

    def test_end_to_end_against_fake_server(self, tmp_path, fake_vllm):
        b = _bench_mod()
        out = tmp_path / "bench.json"
        rc = b.main(["--label", "idle", "--n", "3", "--max-tokens", "8", "--warmup", "1",
                     "--endpoint", fake_vllm.url, "--model", "fake/model", "--out", str(out)])
        assert rc == 0
        doc = json.loads(out.read_text(encoding="utf-8"))
        idle = doc["mp_vllm"]["idle"]
        assert idle["n"] == 3 and idle["n_ok"] == 3 and idle["tokens_total"] == 18 and idle["gaps_total"] == 15
        assert idle["p50_inter_token_ms"] > 0 and idle["p50_ttft_ms"] > 0 and idle["tokens_per_s"] > 0
        assert idle["model"] == "fake/model" and idle["max_tokens"] == 8
        # the request is the fixed prompt, streamed
        path, body = fake_vllm.requests[-1]
        assert path == "/v1/chat/completions" and body["stream"] is True and body["max_tokens"] == 8
        assert body["messages"][-1]["content"] == b.FIXED_PROMPT
        assert len(fake_vllm.requests) == 4                                  # 1 warmup + 3 samples
        # JSON conventions: sorted, indent 2, trailing newline, no timestamp
        text = out.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), sort_keys=True, indent=2) + "\n"
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
        # running + throughput + extras merge without clobbering idle
        rc = b.main(["--label", "running", "--n", "2", "--max-tokens", "8", "--warmup", "0",
                     "--endpoint", fake_vllm.url, "--model", "fake/model", "--out", str(out),
                     "--wall-s", "100", "--extra", "used_gib_peak=31", "--extra", "state=DONE"])
        assert rc == 0
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["mp_vllm"]["idle"] == idle and doc["mp_vllm"]["running"]["n"] == 2
        assert doc["factory"]["host"] == {"used_gib_peak": 31, "state": "DONE"}
        assert doc["factory"]["throughput"]["wall_s"] == 100.0
        # compare writes the verdict; pass is a bool either way (same server, so it should pass)
        rc = b.main(["--compare", "--out", str(out)])
        doc = json.loads(out.read_text(encoding="utf-8"))
        cmp_ = doc["mp_vllm"]["compare"]
        assert isinstance(cmp_["p50_change_pct"], float) and isinstance(cmp_["pass"], bool)
        assert rc == (0 if cmp_["pass"] else 3)
        # a crafted failing pair returns 3
        doc["mp_vllm"]["running"]["p50_inter_token_ms"] = doc["mp_vllm"]["idle"]["p50_inter_token_ms"] * 1.5
        out.write_text(json.dumps(doc))
        assert b.main(["--compare", "--out", str(out)]) == 3

    def test_cli_help_and_nothing_to_do(self, tmp_path):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "factory_bench_coexist.py"), "--help"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "--label" in r.stdout and "--compare" in r.stdout and "--throughput-from" in r.stdout
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "factory_bench_coexist.py"), "--out", str(tmp_path / "b.json")],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 2 and "nothing to do" in r.stderr


class TestF1RedTeamCarryOvers:
    def test_registry_refuses_closed_or_halt_family(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MP_GIT_REV", "deadbeef")
        R = registry_mod.Registry(tmp_path / "registry.jsonl")
        kw = dict(lane="weather", source="gfs_mex", mode="taker", gene_spec_version=1, config_sha256="abc",
                  budget={"population": 400}, picker="max_boot_lo_ties_fewer_clauses", thresholds={"min_trades": 40},
                  cutoff="2026-07-25")
        R.write_family_line("lane/a/b/v1", **kw)
        R.transition("lane/a/b/v1", "CLOSED", evidence={"reason": "holm"})
        with pytest.raises(registry_mod.RegistryError, match="CLOSED"):
            R.write_family_line("lane/a/b/v1", **kw)
        R.write_family_line("lane/a/b/v2", **kw)                             # a new name is the sanctioned path
        R.transition("lane/a/b/v2", "HALT")
        with pytest.raises(registry_mod.RegistryError, match="HALT"):
            R.write_family_line("lane/a/b/v2", **kw)
        line = R.write_family_line("lane/a/b/v2", allow_reopen=True, **kw)  # explicit, on purpose
        assert line["status"] == "OPEN"
        assert len((tmp_path / "registry.jsonl").read_text().splitlines()) == 5

    def test_gen0_refuses_same_day_overwrite(self, tmp_path):
        from src.factory import gen0

        root = tmp_path / "reports" / "factory"
        out = root / "gen0_2026-09-04"
        gen0.refuse_overwrite(out, "gen0_2026-09-04", root)                   # nothing there: fine
        out.mkdir(parents=True)
        (out / "summary.json").write_text("{}")
        with pytest.raises(gen0.Gen0Error, match="summary.json"):
            gen0.refuse_overwrite(out, "gen0_2026-09-04", root)
        gen0.refuse_overwrite(out, "gen0_2026-09-04", root, force=True)       # --force
        (out / "summary.json").unlink()
        (root / "latest.json").write_text(json.dumps({"run_id": "gen0_2026-09-04", "summary": "gen0_2026-09-04/summary.json"}))
        with pytest.raises(gen0.Gen0Error, match="latest.json"):
            gen0.refuse_overwrite(out, "gen0_2026-09-04", root)
        gen0.refuse_overwrite(out, "gen0_2026-09-05", root)                   # a new run id is fine
        gen0.refuse_overwrite(out, "gen0_2026-09-04", root, force=True)

    def test_gen0_cli_has_force(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "factory.py"), "gen0", "--help"],
                           capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r.returncode == 0 and "--force" in r.stdout
        src = (REPO / "scripts" / "factory.py").read_text(encoding="utf-8")
        assert "refuse_overwrite(out_dir, run_id, REPORTS_ROOT, force=bool(args.force))" in src

    def test_family_yaml_still_hashes_to_registry_line(self):
        """The byte-frozen YAML must hash (CRLF-normalised, as load_family_config does) to the registry's config_sha256."""
        from src.factory.fees import sha256_file

        sha = sha256_file(str(REPO / "configs" / "factory" / "weather_gfs_mex_taker_v1.yaml"))
        lines = [json.loads(l) for l in (REPO / "reports" / "factory" / "registry.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        fam = [l for l in lines if l.get("event") == "family" and l.get("family") == registry_mod.FAMILY_F1]
        assert fam and fam[0]["config_sha256"] == sha
        assert sha.startswith("e679631add8e")

    def test_gitignore_f2_layout(self):
        gi = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        for entry in ("data/factory/", "reports/factory/*/gen_*", "reports/factory/*/controls/",
                      "reports/factory/*/resources.log", "reports/factory/*/frames/"):
            assert entry in gi, entry
        ignored = ["reports/factory/run_x/controls/summary.json", "reports/factory/run_x/gen_001.parquet",
                   "reports/factory/run_x/resources.log", "reports/factory/run_x/frames/search/a.parquet",
                   "data/factory/runs/run_x/run.json", "data/factory/frames/x/parity/a.parquet"]
        tracked = [f"reports/factory/run_x/{f}" for f in ("summary.json", "summary.md", "oos_by_date.csv", "finalists.json",
                                                            "board.md", "status.json", "run.json", "bench.json", "completion.txt")]
        tracked += ["reports/factory/latest.json", "reports/factory/registry.jsonl", "reports/factory/coverage.json"]
        r = subprocess.run(["git", "check-ignore", "--no-index", *ignored, *tracked], capture_output=True, text=True,
                           cwd=str(REPO), timeout=60)
        hits = set(r.stdout.split())
        assert hits == set(ignored), f"ignored mismatch: extra={hits - set(ignored)} missing={set(ignored) - hits}"
