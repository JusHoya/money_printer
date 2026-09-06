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


def test_factory_package_never_names_the_exchange_or_provider_as_a_string():
    """Belt and braces for a lazy ``importlib.import_module("src.core.matching_engine")``."""
    hits = []
    for path in _factory_files():
        text = path.read_text(encoding="utf-8")
        for f in FORBIDDEN_IMPORTS:
            if f in text:
                hits.append(f"{path.relative_to(ROOT).as_posix()} mentions {f}")
    assert not hits, "\n".join(hits)
