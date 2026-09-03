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
        unit = (REPO / "deploy" / "spark" / "systemd" / "mp-factory@.service").read_text(encoding="utf-8")
        assert "Type=oneshot" in unit and "Restart=on-failure" in unit and "StartLimitBurst=5" in unit
        assert "docker-compose.lab.yml run --rm factory python scripts/factory.py resume %i" in unit

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
        b = json.loads(hp.get_factory_board({}))
        assert b["ok"] and b["board_md"].startswith("# Factory board") and b["coverage"]["lanes"]["weather"]["status"] == "READY"
