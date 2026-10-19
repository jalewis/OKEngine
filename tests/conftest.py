"""Global test-layer policy required by the enhanced testing standard.

Legacy tests predate the taxonomy.  Collection assigns a conservative primary
layer from observable test behavior and fails if a test declares conflicting
layers.  Explicit markers always win, which lets files graduate from the
compatibility classifier without a flag day across thousands of test cases.
"""
from __future__ import annotations

import ast
import gc
import importlib.util
import json
import os
import inspect
import re
import runpy
import time
from collections import Counter, defaultdict
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _verify_ci_tmpfs(request):
    """Fail before tests if CI's actual tmp_path storage falls back to disk."""
    if os.environ.get("OKENGINE_CI_TEST_TMPFS") == "1":
        helper = Path(__file__).parents[1] / "ci/test_tmpfs.py"
        if helper.is_file():
            verifier = runpy.run_path(str(helper))
        else:
            raise pytest.UsageError("CI tmpfs verification requested but its private helper is unavailable")
        # A declared tmp_path_factory dependency would classify EVERY unit as I/O.
        # Request it dynamically only for this session-level infrastructure check.
        tmp_path_factory = request.getfixturevalue("tmp_path_factory")
        verifier["verify"](tmp_path_factory.getbasetemp())


PRIMARY_LAYERS = (
    "unit", "integration", "contract", "e2e", "smoke", "resilience",
    "invariant", "security", "performance",
)

_SECURITY = re.compile(
    r"(?:auth|security|adversarial|permission|reserved|secret|token|ownership|"
    r"path_safety|scope|trust_gate|policy_plane|output_contract)"
)
_CONTRACT = re.compile(r"(?:contract|parity|schema_validator|responses_endpoint)")
_INVARIANT = re.compile(
    r"(?:gate|manifest|patches_registered|pins|scrub|domain_boundary|generated|"
    r"cron_toolset_policy|ci_coverage|shell_tool|engine_manifest|deterministic_audit)"
)
_IO_SOURCE = re.compile(
    r"\b(?:Path|tmp_path|tmpdir|subprocess|sqlite3|socket|urlopen|httpx|TestClient|"
    r"docker|runpy|importlib|spec_from_file_location|exec_module|read_text|write_text|"
    r"os\.environ|monkeypatch\.setenv|time\.sleep)\b"
)
_IO_FIXTURES = frozenset({"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"})
_STRICT_SKIPS = False
_UNIT_MAX_SECONDS = 0.100


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("okengine-test-policy")
    group.addoption("--test-layer-report", metavar="PATH", default="")
    group.addoption("--strict-layer-skips", action="store_true", default=False)


REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALLED_DIRS = frozenset({"site-packages", "dist-packages"})


def okengine_source_problem(origin: str | None, repo_root: Path) -> str | None:
    """Why the resolved ``okengine`` package is not this checkout's, or None when it is.

    okengine#994 [24]: ~/.venvs/okengine carries an EDITABLE okengine that points at whichever
    engines/ worktree last ran ``pip install -e``, so a gate run from a release worktree silently
    tested another checkout's source. Accepted: this checkout's ``src/`` (PYTHONPATH, an editable
    install of this clone, or the mutation lane's detached worktree, whose conftest is its own) and
    a regular installed distribution under site-packages/dist-packages (CI installs the candidate
    wheel built from this commit). Anything else is another source tree. Unimportable is not this
    guard's finding (the tests that need the package fail on their own), and neither is a root
    without src/okengine, such as a disposable policy fixture that copies this conftest."""
    src = (repo_root / "src").resolve()
    if origin is None or not (src / "okengine").is_dir():
        return None             # nothing to shadow: a disposable conftest copy has no src/okengine
    path = Path(origin).resolve()
    if path.is_relative_to(src) or _INSTALLED_DIRS & set(path.parts):
        return None
    return (f"okengine resolves to {path.parent}, not this checkout's {src}: these tests would "
            "run against another worktree's source. Run them with PYTHONPATH=src:okengine-mcp "
            "(e.g. `PYTHONPATH=src:okengine-mcp python -m pytest tests/ "
            "--import-mode=importlib`).")


def pytest_configure(config: pytest.Config) -> None:
    global _STRICT_SKIPS
    _STRICT_SKIPS = bool(config.getoption("--strict-layer-skips"))
    spec = importlib.util.find_spec("okengine")
    problem = okengine_source_problem(spec.origin if spec else None, REPO_ROOT)
    if problem:
        raise pytest.UsageError(problem)


def _relative(item: pytest.Item) -> str:
    path = Path(str(item.path)).resolve()
    root = Path(str(item.config.rootpath)).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_function(current: object, module_name: str | None) -> tuple[str, tuple[object, ...]] | None:
    """`current`'s source and the same-module functions it calls by name, or None when it has no
    readable, parseable source."""
    try:
        source = inspect.getsource(current)
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except (IndentationError, SyntaxError):
        return source, ()
    namespace = getattr(current, "__globals__", {})
    helpers = []
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Name):
            continue
        helper = namespace.get(call.func.id)
        if inspect.isfunction(helper) and getattr(helper, "__module__", None) == module_name:
            helpers.append(helper)
    return source, tuple(helpers)


# okengine#1059: collection read and parsed each test's source, and every helper it reaches, once
# per collected item and twice per item (the skip check and the layer inference). Parametrized
# cases and shared helpers made that ~46,000 getsource calls for ~15,700 items, three quarters of a
# collection that took ~100 s of unit-suite's 120 s deadline in CI. Each function is read once.
_FUNCTION_SOURCES: dict[tuple[object, str | None], tuple[str, tuple[object, ...]] | None] = {}
_ITEM_SOURCES: dict[tuple[object, str | None], str] = {}


def _function_source(current: object, module_name: str | None):
    key = (getattr(current, "__func__", current), module_name)
    try:
        return _FUNCTION_SOURCES[key]
    except KeyError:
        found = _FUNCTION_SOURCES[key] = _read_function(current, module_name)
        return found
    except TypeError:               # an unhashable callable: read it uncached
        return _read_function(current, module_name)


def _expanded_source(obj: object) -> str:
    module_name = getattr(obj, "__module__", None)
    pending = [obj]
    seen: set[int] = set()
    chunks: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found = _function_source(current, module_name)
        if found is None:
            continue
        source, helpers = found
        chunks.append(source)
        pending.extend(helpers)
    return "\n".join(chunks)


def _source(item: pytest.Item) -> str:
    obj = getattr(item, "obj", None)
    if obj is not None:
        key = (getattr(obj, "__func__", obj), getattr(obj, "__module__", None))
        try:
            expanded = _ITEM_SOURCES.get(key)
            if expanded is None:
                expanded = _ITEM_SOURCES[key] = _expanded_source(obj)
        except TypeError:           # an unhashable test object: expand it uncached
            expanded = _expanded_source(obj)
        if expanded:
            return expanded
    try:
        return Path(str(item.path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _inferred_layer(item: pytest.Item) -> str:
    rel = _relative(item).lower()
    name = f"{rel}::{item.name.lower()}"
    if rel.endswith("tests/e2e/smoke/test_smoke_curl.py"):
        return "smoke"
    if rel.startswith("tests/e2e/"):
        return "e2e"
    if _SECURITY.search(name):
        return "security"
    if _CONTRACT.search(name):
        return "contract"
    if _INVARIANT.search(name):
        return "invariant"
    fixtures = set(getattr(item, "fixturenames", ()))
    if fixtures & _IO_FIXTURES or _IO_SOURCE.search(_source(item)):
        return "integration"
    return "unit"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    errors: list[str] = []
    for item in items:
        if "pytest.skip" in _source(item):
            item.add_marker(pytest.mark.external)
        explicit = {layer for layer in PRIMARY_LAYERS if item.get_closest_marker(layer)}
        if len(explicit) > 1:
            errors.append(f"{item.nodeid}: conflicting primary layers {sorted(explicit)}")
            continue
        if not explicit:
            item.add_marker(getattr(pytest.mark, _inferred_layer(item)))
        assigned = {layer for layer in PRIMARY_LAYERS if item.get_closest_marker(layer)}
        if len(assigned) != 1:
            errors.append(f"{item.nodeid}: expected exactly one primary layer, got {sorted(assigned)}")
    if not items:
        errors.append("test policy collected zero tests")
    if errors:
        raise pytest.UsageError("global test-layer policy failed:\n  " + "\n  ".join(errors))

    report = config.getoption("--test-layer-report")
    if report:
        grouped: dict[str, list[str]] = defaultdict(list)
        for item in items:
            layer = next(layer for layer in PRIMARY_LAYERS if item.get_closest_marker(layer))
            grouped[layer].append(item.nodeid)
        payload = {
            "schema_version": 1,
            "total": len(items),
            "counts": dict(sorted(Counter(
                layer for layer, nodes in grouped.items() for _ in nodes).items())),
            "layers": {layer: sorted(nodes) for layer, nodes in sorted(grouped.items())},
        }
        target = Path(report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Measure test computation independently of shared-runner descheduling jitter."""
    # Do not charge a unit for cyclic garbage accumulated by every test that ran before it. Without
    # this boundary, an otherwise sub-5ms test can happen to trigger a generation collection and be
    # reported above the 100ms contract depending only on suite order and runner heap state.
    #
    # Collecting generation 0 first is not enough: the automatic FULL (generation-2) collection
    # scans the whole session heap, which in the unit layer is ~15,000 collected items, and it
    # fires on whichever test's allocations cross the threshold. Measured in python:3.12-slim: the
    # single unit test (of 2590) that hit a gen-2 collection used 0.196s CPU, 0.192s of it inside
    # the collector, while the same test alone used 0.005s. So the automatic collector is paused
    # for the timed call only. A test's own computation, including an explicit gc.collect(), is
    # still billed in full.
    unit = bool(_STRICT_SKIPS and item.get_closest_marker("unit"))
    collector_was_enabled = gc.isenabled()
    if unit:
        gc.collect(0)
        gc.disable()
    started = time.process_time()
    yield
    item._okengine_cpu_duration = time.process_time() - started
    if unit and collector_was_enabled:
        gc.enable()
    item.user_properties.append(
        ("okengine_cpu_seconds", f"{item._okengine_cpu_duration:.6f}")
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if _STRICT_SKIPS and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"unexpected skip in blocking layer: {report.nodeid}"
    cpu_duration = float(getattr(item, "_okengine_cpu_duration", report.duration))
    if (
        _STRICT_SKIPS and report.when == "call" and report.passed
        and item.get_closest_marker("unit") and cpu_duration >= _UNIT_MAX_SECONDS
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"unit test exceeded {_UNIT_MAX_SECONDS:.3f}s limit: "
            f"{report.nodeid} used {cpu_duration:.3f}s CPU "
            f"({report.duration:.3f}s wall); optimize it or classify it in the "
            "appropriate non-unit layer"
        )


@pytest.fixture(autouse=True)
def _restore_process_environment():
    """Every test starts from the environment the previous one found.

    Several write-path helpers set WIKI_PATH / OKENGINE_MCP_WRITE_DATE / OKENGINE_BASE_SCHEMA
    straight into os.environ and never restore them, so under pytest-randomly each later test
    inherited whichever values ran last: a write-path test saw "today" as another file's pinned
    date and rejected its own fixture as future-dated, depending only on order. Restoring the
    whole environment after every test removes the class instead of chasing each helper.
    """
    saved = dict(os.environ)
    yield
    if os.environ != saved:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(scope="session", autouse=True)
def _warm_shared_session_caches():
    """Pay once-per-session costs BEFORE any test is timed.

    The unit budget measures process_time around each test's call phase, so a shared lazily-filled
    cache is charged IN FULL to whichever test happens to fill it -- and pytest-randomly moves that
    victim between runs. schema_lib.base_schema() is the live example: mtime-cached, read by 8 test
    files, 0.01s standalone but 0.135s when it is the first caller.

    Two consecutive pipelines failed this way on DIFFERENT tests (test_ci_config, then
    test_core_schema), each comfortably fast in isolation. Chasing the victim does not converge;
    the measurement is what is wrong. Warming here makes the budget measure a test's OWN work,
    which is what it is meant to police -- it does not weaken the limit.
    """
    try:
        import schema_lib  # already on sys.path via the suite's cron-scripts insert
        schema_lib.base_schema()
    except Exception:  # pragma: no cover - warming is best-effort, never a test failure
        pass
