"""Runtime/factory isolation (PRD_STRATEGY_FACTORY.md Phase F3, FACTORY_ARCHITECTURE section 1.2).

Three falsifiable properties of the maia sandbox image, checked on the dev box:

(a) The import graph of the two runtime entry points (``scripts/run_dashboard.py``,
    ``scripts/run_web_dashboard.py``) reaches nothing under ``src.factory`` except
    the numpy-only runtime slice ``genome`` / ``features`` / ``promoted`` (and their
    own numpy/stdlib-only dependencies ``columns`` / ``fees``). None of the lab
    modules (evolve, procedure, fitness, frame, ledger, multiplicity, null,
    controls, report, gen0, lanes, ...) may load in the runtime process.
(b) ``import src.strategies.genome_strategy`` succeeds with ``lightgbm``,
    ``scipy``, ``pyarrow``, ``torch``, ``xgboost`` blocked via ``sys.modules``:
    the sandbox image must not need them to run the strategy. xfail until the
    STRATEGY workstream lands the module; a hard assertion once the file exists.
(c) Wall-clock rule (FR-F2.5 / F3.1): ``genome_strategy.py``, ``features.py``,
    ``genome.py`` contain no ``datetime.now`` / ``time.time`` / ``utcnow`` and no
    ``from datetime import`` binding the guard cannot intercept.

Each import check runs in a fresh subprocess so this test's own process state
(other tests import the lab modules freely) cannot leak into the verdict.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

RUNTIME_FACTORY_ALLOWED = {
    "src.factory",  # the package __init__ (docstring only)
    "src.factory.genome",
    "src.factory.features",
    "src.factory.promoted",
    # numpy/stdlib-only dependencies of the three above (src/factory/__init__.py
    # names columns/features/genome as the sandbox-safe slice; fees is the
    # fee-regime loader the bot needs to construct GenomeStrategy).
    "src.factory.columns",
    "src.factory.fees",
}
LAB_ONLY = (
    "evolve", "procedure", "fitness", "frame", "ledger", "multiplicity", "null",
    "controls", "report", "gen0", "lanes", "folds", "guards", "registry", "coverage", "bench",
)
BLOCKED_LIBS = ("lightgbm", "scipy", "pyarrow", "torch", "xgboost")
WALL_CLOCK_FILES = (
    "src/strategies/genome_strategy.py",
    "src/factory/features.py",
    "src/factory/genome.py",
)
WALL_CLOCK_RE = re.compile(r"datetime\.now|time\.time|utcnow|from datetime import")


def _run(code: str, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env.pop("PYTHONSTARTUP", None)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        timeout=timeout, env=env,
    )


_IMPORT_GRAPH_CODE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
os.environ.setdefault("MP_DRY_IMPORT", "1")
import scripts.run_dashboard  # noqa
import scripts.run_web_dashboard  # noqa
mods = sorted(m for m in sys.modules if m.startswith("src.") and sys.modules[m] is not None)
print("IMPORT_GRAPH=" + json.dumps(mods))
"""


@pytest.fixture(scope="module")
def runtime_import_graph():
    proc = _run(_IMPORT_GRAPH_CODE)
    assert proc.returncode == 0, f"entry points failed to import:\n{proc.stderr[-3000:]}"
    line = next(l for l in proc.stdout.splitlines() if l.startswith("IMPORT_GRAPH="))
    return json.loads(line[len("IMPORT_GRAPH="):])


def test_runtime_entry_points_do_not_import_lab_factory_modules(runtime_import_graph):
    factory = sorted(m for m in runtime_import_graph if m.startswith("src.factory"))
    forbidden = [m for m in factory if m not in RUNTIME_FACTORY_ALLOWED]
    assert not forbidden, (
        "runtime import graph reaches lab-only factory modules: "
        f"{forbidden}\n(full src.factory list: {factory})"
    )
    for name in LAB_ONLY:
        assert f"src.factory.{name}" not in runtime_import_graph
        assert not any(m.startswith(f"src.factory.{name}.") for m in runtime_import_graph)


def test_runtime_import_graph_is_recorded(runtime_import_graph):
    """Not a gate — prints the actual list so a reviewer can read it in -rA output."""
    factory = [m for m in runtime_import_graph if m.startswith("src.factory")]
    print("src.factory modules reached by the runtime entry points:", factory or "NONE")
    print("all src.* modules:", runtime_import_graph)
    assert isinstance(runtime_import_graph, list) and runtime_import_graph


_BLOCKED_IMPORT_CODE = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
for name in %s:
    sys.modules[name] = None
import src.strategies.genome_strategy  # noqa
loaded = sorted(m for m in sys.modules if m.startswith("src.factory") and sys.modules[m] is not None)
heavy = sorted(m for m in %s if sys.modules.get(m) is not None)
print("GENOME_IMPORT=" + json.dumps({"factory": loaded, "heavy": heavy}))
"""


def test_genome_strategy_imports_without_lab_libraries():
    path = os.path.join(ROOT, "src", "strategies", "genome_strategy.py")
    if not os.path.exists(path):
        pytest.xfail("src/strategies/genome_strategy.py not present yet (STRATEGY workstream, FR-F3.1)")
    proc = _run(_BLOCKED_IMPORT_CODE % (json.dumps(list(BLOCKED_LIBS)), json.dumps(list(BLOCKED_LIBS))))
    assert proc.returncode == 0, (
        "genome_strategy must import with lightgbm/scipy/pyarrow/torch/xgboost blocked:\n"
        + proc.stderr[-3000:]
    )
    line = next(l for l in proc.stdout.splitlines() if l.startswith("GENOME_IMPORT="))
    info = json.loads(line[len("GENOME_IMPORT="):])
    forbidden = [m for m in info["factory"] if m not in RUNTIME_FACTORY_ALLOWED]
    assert not forbidden, f"genome_strategy pulled lab-only factory modules: {forbidden}"
    assert not info["heavy"]


def test_promoted_loader_imports_without_lab_libraries():
    path = os.path.join(ROOT, "src", "factory", "promoted.py")
    if not os.path.exists(path):
        pytest.xfail("src/factory/promoted.py not present yet (STRATEGY workstream)")
    code = _BLOCKED_IMPORT_CODE.replace("import src.strategies.genome_strategy", "import src.factory.promoted")
    proc = _run(code % (json.dumps(list(BLOCKED_LIBS)), json.dumps(list(BLOCKED_LIBS))))
    assert proc.returncode == 0, proc.stderr[-3000:]


@pytest.mark.parametrize("rel", WALL_CLOCK_FILES)
def test_no_wall_clock_in_strategy_and_genome_code(rel):
    path = os.path.join(ROOT, *rel.split("/"))
    if not os.path.exists(path):
        if rel.endswith("genome_strategy.py"):
            pytest.skip("genome_strategy.py not present yet (STRATEGY workstream)")
        pytest.fail(f"{rel} missing")
    hits = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if WALL_CLOCK_RE.search(line):
                hits.append(f"{rel}:{n}: {line.rstrip()}")
    assert not hits, "wall-clock reads / un-interceptable datetime bindings:\n" + "\n".join(hits)
