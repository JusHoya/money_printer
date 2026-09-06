"""No file under ``src/factory/`` references a live-capital flag (PRD_STRATEGY_FACTORY Phase F4, exit criterion 5).

The criterion reads: *"if HALT, ... no file under ``src/factory/`` references a
live-capital flag."* This test makes that a grep that runs on every commit, not
a one-off, and extends ``tests/test_factory_isolation.py`` (which pins the
RUNTIME's import graph) with the FACTORY side: nothing in the lab package
imports the modules through which capital could ever be reached.

THE FLAGS -- enumerated from what ``src/core``, ``src/bots``, ``src/data`` and
``scripts/run_dashboard.py`` actually use to gate capital (grep 2026-09-06), so
the list below is the real surface and not a guess:

* ``read_only``            -- ``KalshiProvider(read_only=True)``: the ctor flag that makes
                              ``place_order`` raise; ``scripts/run_dashboard.py:304`` sets it and
                              ``LiveKalshiGateway`` refuses every write while it is set.
* ``anonymous``            -- ``KalshiProvider.anonymous`` (no key = ghost mode); the gateway's
                              second refusal.
* ``place_order``          -- ``KalshiProvider.place_order``: the ONE method that would submit a
                              real order (blocked by ``read_only``).
* ``LiveKalshiGateway`` / ``live_gateway``
                           -- ``src/core/live_gateway.py``: the only ``ExecutionEngine`` that reaches
                              the real API (``use_production=True`` switches it to prod).
* ``use_production``       -- that gateway's demo-vs-production switch.
* ``WEATHER_TRADING_ENABLED``, ``GAS_TRADING_ENABLED``, ``MENTION_TRADING_ENABLED``,
  ``CRYPTO_ANNUAL_TRADING_ENABLED``, ``TWEETS_TRADING_ENABLED``
                           -- the per-bot module flags that turn a feed-only harvester into a
                              strategy that emits signals into the (simulated) execution path.
* ``api.elections.kalshi.com`` -- the production API host itself.

Names that look like flags but are NOT capital flags, and are therefore allowed:
``GENOME_STRATEGY_MODE`` (shadow/paper, simulated either way), ``MP_REALISTIC_FILLS``
(a fill-model switch on the SIMULATED exchange), ``paper`` / ``shadow`` as words.

IMPORT ISOLATION (factory -> core), extending test_factory_isolation.py:
no module under ``src/factory/`` may import ``src.data.kalshi_provider``,
``src.core.live_gateway`` or ``src.core.matching_engine`` at all; and
``src.core.risk_manager`` may be imported by exactly ONE module, ``sizing.py``,
and only for the ``MIN_WIN_SAMPLES`` constant it mirrors -- the isolation test
already declares ``sizing`` lab-only for that reason ("It imports src.core"),
and a broader import (the class, the sizer) would let a lab module hold a
reference to the object that decides position size.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FACTORY_DIR = ROOT / "src" / "factory"

#: The live-capital surface, pinned by name (see the module docstring for where each comes from).
LIVE_CAPITAL_FLAGS = (
    "read_only",
    "anonymous",
    "place_order",
    "LiveKalshiGateway",
    "live_gateway",
    "use_production",
    "WEATHER_TRADING_ENABLED",
    "GAS_TRADING_ENABLED",
    "MENTION_TRADING_ENABLED",
    "CRYPTO_ANNUAL_TRADING_ENABLED",
    "TWEETS_TRADING_ENABLED",
    "api.elections.kalshi.com",
)
FLAG_RE = re.compile("|".join(re.escape(f) for f in LIVE_CAPITAL_FLAGS))

#: Modules through which capital could be reached; no factory module may import them.
FORBIDDEN_IMPORTS = (
    "src.data.kalshi_provider",
    "src.core.live_gateway",
    "src.core.matching_engine",
)
#: ``src.core.risk_manager`` is importable by exactly this module, for exactly these names.
RISK_MANAGER_ALLOWED = {"src/factory/sizing.py": {"MIN_WIN_SAMPLES"}}


def _factory_files():
    files = sorted(p for p in FACTORY_DIR.rglob("*.py"))
    assert files, f"no python files under {FACTORY_DIR}"
    return files


def test_the_flag_list_is_the_real_surface():
    """Each pinned flag must still exist where the docstring says -- a renamed flag must rename here too."""
    src = (ROOT / "src" / "data" / "kalshi_provider.py").read_text(encoding="utf-8")
    assert "read_only" in src and "anonymous" in src and "def place_order" in src
    gw = (ROOT / "src" / "core" / "live_gateway.py").read_text(encoding="utf-8")
    assert "class LiveKalshiGateway" in gw and "use_production" in gw
    for bot, flag in (
        ("weather_bot.py", "WEATHER_TRADING_ENABLED"),
        ("gas_bot.py", "GAS_TRADING_ENABLED"),
        ("mention_bot.py", "MENTION_TRADING_ENABLED"),
        ("crypto_annual_bot.py", "CRYPTO_ANNUAL_TRADING_ENABLED"),
        ("tweets_bot.py", "TWEETS_TRADING_ENABLED"),
    ):
        assert re.search(rf"^{flag}\s*=", (ROOT / "src" / "bots" / bot).read_text(encoding="utf-8"), re.M), (bot, flag)
    assert "read_only=True" in (ROOT / "scripts" / "run_dashboard.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", _factory_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_factory_file_references_a_live_capital_flag(path: Path):
    hits = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            m = FLAG_RE.search(line)
            if m:
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{n}: {m.group(0)!r} in {line.rstrip()}")
    assert not hits, "live-capital flag referenced under src/factory/:\n" + "\n".join(hits)


def _imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, ()
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, tuple(a.name for a in node.names)


@pytest.mark.parametrize("path", _factory_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_factory_module_imports_the_capital_path(path: Path):
    rel = path.relative_to(ROOT).as_posix()
    bad = []
    for module, names in _imports_of(path):
        if any(module == f or module.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
            bad.append(f"{rel}: import {module}")
        if module == "src.core.risk_manager" or module.startswith("src.core.risk_manager."):
            allowed = RISK_MANAGER_ALLOWED.get(rel)
            if allowed is None:
                bad.append(f"{rel}: imports src.core.risk_manager (only sizing.py may, for MIN_WIN_SAMPLES)")
            elif not names or not set(names) <= allowed:
                bad.append(f"{rel}: imports {sorted(names) or '*'} from src.core.risk_manager; allowed {sorted(allowed)}")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------------------
# TRANSITIVE imports (red team 2026-09-06, item 6): the AST check above sees only what a
# factory module names itself. `import src.factory.holdout` ALSO loads
# src.data.kalshi_provider -- through src.data.kalshi_history, which imports
# KalshiProvider at module level -- and the provider is the one class that can
# place a real order. Every src.* module a factory import pulls in is listed here,
# per module, as an explicit allowlist (measured 2026-09-06 with the scan below;
# package __init__ modules such as `src.core` are not listed, they carry no code).
#
# The list is the contract: a new transitive module fails this test until it is
# named here WITH a reason. Two entries deserve reading:
#   * holdout: `src.data.kalshi_provider` is deliberately NOT allowed, so this test
#     FAILS at 6b8fffb and passes once the HOLDOUT agent makes kalshi_history's
#     provider import lazy (its `_load_ladders_unchecked` reads CSVs and needs no
#     provider).
#   * sizing: `from src.core.risk_manager import MIN_WIN_SAMPLES` drags risk_manager's
#     own module-level graph in -- including src.core.matching_engine, the simulated
#     exchange. That is the declared lab-only exception (test_factory_isolation.py
#     LAB_ONLY: "It imports src.core"); it is allowed here BY NAME so it cannot grow
#     silently, and the direct-import test above still forbids naming the exchange.
# ---------------------------------------------------------------------------
_CORE_FEE = {"src.core.fee_calculator"}
TRANSITIVE_ALLOWED = {
    "src.factory.controls": _CORE_FEE,
    "src.factory.features": _CORE_FEE,
    "src.factory.fees": _CORE_FEE,
    "src.factory.frame": _CORE_FEE,
    "src.factory.gen0": _CORE_FEE,
    "src.factory.null": _CORE_FEE,
    "src.factory.holdout": {
        "src.backtest.sealed_roots",   # the seal (SealedDataError) the holdout must honour
        "src.core.bracket_payoff",     # settles_yes for the truth filter
        "src.core.interfaces",         # MarketData dataclass via kalshi_history
        "src.data.kalshi_history",     # _load_ladders_unchecked (CSV reader) -- provider must be lazy
        "src.utils.logger",
        # NOT allowed: src.data.kalshi_provider (loaded today via kalshi_history:104)
    },
    "src.factory.sizing": {
        "src.core.bracket_payoff", "src.core.fee_calculator", "src.core.matching_engine",
        "src.core.risk_manager", "src.core.weather_settlement", "src.data.gas_settlement",
        "src.data.iem_cli_provider", "src.utils.logger",
    },
}
_PACKAGE_INITS = {"src", "src.core", "src.data", "src.utils", "src.backtest", "src.ml", "src.strategies", "src.bots", "src.web"}

_TRANSITIVE_SCAN = r"""
import json, sys
name = sys.argv[1]
before = set(sys.modules)
__import__(name)
loaded = sorted(m for m in sys.modules if m.startswith("src.") and m not in before
                and not m.startswith("src.factory") and sys.modules[m] is not None)
print("TRANSITIVE=" + json.dumps(loaded))
"""


def _factory_module_names():
    names = []
    for p in _factory_files():
        if p.name == "__init__.py":
            continue
        names.append(".".join(p.relative_to(ROOT).with_suffix("").parts))  # src.factory.lanes.weather etc.
    return sorted(names)


@pytest.mark.parametrize("module", _factory_module_names())
def test_transitive_imports_are_allowlisted_per_module(module: str):
    import subprocess
    import sys

    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("PYTHONSTARTUP", None)
    proc = subprocess.run([sys.executable, "-c", _TRANSITIVE_SCAN, module], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180, env=env)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "?"
        if "ModuleNotFoundError" in tail and ".factory" not in tail:
            pytest.skip(f"{module} needs a lab dependency this box lacks: {tail}")
        pytest.fail(f"{module} failed to import:\n{proc.stderr[-2000:]}")
    line = next(l for l in proc.stdout.splitlines() if l.startswith("TRANSITIVE="))
    loaded = {m for m in json.loads(line[len("TRANSITIVE="):]) if m not in _PACKAGE_INITS}
    allowed = TRANSITIVE_ALLOWED.get(module, set())
    forbidden_hits = sorted(m for m in loaded if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IMPORTS))
    extra = sorted(loaded - allowed)
    assert not extra, (
        f"{module} transitively loads src modules not in its allowlist: {extra}"
        + (f" (of which the capital path: {forbidden_hits})" if forbidden_hits else "")
        + f"\nfull transitive set: {sorted(loaded)}"
    )


def test_factory_package_never_names_the_exchange_or_provider_as_a_string():
    """Belt and braces for a lazy ``importlib.import_module("src.core.matching_engine")``."""
    hits = []
    for path in _factory_files():
        text = path.read_text(encoding="utf-8")
        for f in FORBIDDEN_IMPORTS:
            if f in text:
                hits.append(f"{path.relative_to(ROOT).as_posix()} mentions {f}")
    assert not hits, "\n".join(hits)
