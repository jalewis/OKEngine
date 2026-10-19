"""Every global that rebound write-service code reads resolves in the write_server facade (okengine#1057 [3]).

`okengine.write_services.binding.install_services()` rebinds each service function and class method
onto write_server's globals with `types.FunctionType(code, facade, ...)`. A name imported only in
the service module (say transport.py) resolves in that module, so its own tests and ruff F821 pass,
but inside write_server it is a NameError on whatever path first reaches it. v0.15.15 hit exactly
this: `transport._ScopedWriteAuth.__call__` calls `authorization_header`, which write_server has to
import by hand. An error branch or a rarely taken guard would ship green and fail live.

This walks every function the facade actually holds whose globals ARE the facade but whose code was
written in a write-service module, including nested code objects (closures, and comprehensions on
3.11, where they are still separate code objects), and checks each LOAD_GLOBAL / LOAD_NAME name
against the facade and builtins. It matches on opcode NAMES, so it holds across 3.11-3.13 where the
LOAD_GLOBAL encoding changed.
"""
from __future__ import annotations

import builtins
import dis
import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("yaml")

pytestmark = pytest.mark.invariant

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"
SERVICES_DIR = REPO / "src" / "okengine" / "write_services"
_GLOBAL_LOADS = frozenset({"LOAD_GLOBAL", "LOAD_NAME"})


@pytest.fixture(scope="module")
def facade(tmp_path_factory):
    vault = tmp_path_factory.mktemp("rebound-globals")
    (vault / "wiki").mkdir()
    (vault / "schema.yaml").write_text(
        "types: {}\npermissions: {default: {create: true, update: true, delete: false}}\n",
        encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("WIKI_PATH", str(vault))
        for name in ("OKENGINE_WRITE_ACTOR", "OKENGINE_WRITE_TOOLS", "OKENGINE_WRITE_TRANSPORT",
                     "OKENGINE_WRITE_TOKEN", "OKENGINE_MCP_TOKEN"):
            mp.delenv(name, raising=False)
        name = "write_server_rebound_globals"
        spec = importlib.util.spec_from_file_location(name, WS_MOD)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
    return module


def _is_service_code(code: types.CodeType) -> bool:
    return Path(code.co_filename).resolve().parent == SERVICES_DIR.resolve()


def rebound_functions(namespace: dict) -> dict[str, types.FunctionType]:
    """`label -> function` for every write-service function rebound onto `namespace`: top-level
    functions and the plain-function attributes of rebuilt classes whose globals are `namespace`."""
    found: dict[str, types.FunctionType] = {}
    for symbol, value in list(namespace.items()):
        if isinstance(value, types.FunctionType):
            candidates = [(symbol, value)]
        elif isinstance(value, type):
            candidates = [(f"{symbol}.{attr}", member) for attr, member in vars(value).items()
                          if isinstance(member, types.FunctionType)]
        else:
            continue
        for label, function in candidates:
            if function.__globals__ is namespace and _is_service_code(function.__code__):
                found[label] = function
    return found


def _code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def unresolved_globals(namespace: dict) -> dict[str, list[str]]:
    """`label -> sorted names` that rebound code loads as a global but neither `namespace` nor
    builtins defines (empty = every rebound global resolves)."""
    available = set(namespace) | set(vars(builtins))
    missing: dict[str, list[str]] = {}
    for label, function in rebound_functions(namespace).items():
        names = {instr.argval for code in _code_objects(function.__code__)
                 for instr in dis.get_instructions(code) if instr.opname in _GLOBAL_LOADS}
        absent = sorted(n for n in names if isinstance(n, str) and n not in available)
        if absent:
            missing[label] = absent
    return missing


def test_the_walk_covers_the_rebound_service_layer(facade):
    """The premise: a walk that found nothing would pass vacuously."""
    functions = rebound_functions(vars(facade))
    assert "_ScopedWriteAuth.__call__" in functions, sorted(functions)[:20]
    modules = {Path(f.__code__.co_filename).stem for f in functions.values()}
    assert {"transport", "validation", "authorization", "review"} <= modules, sorted(modules)
    assert len(functions) >= 100, f"only {len(functions)} rebound functions walked"
    # Every function and method install_services() rebinds from a covered module is walked.
    walked = {f.__code__ for f in functions.values()}
    skipped = []
    for stem in sorted(modules):
        service = importlib.import_module(f"okengine.write_services.{stem}")
        for symbol, value in vars(service).items():
            if getattr(value, "__module__", None) != service.__name__:
                continue
            members = ([(symbol, value)] if isinstance(value, types.FunctionType) else
                       [(f"{symbol}.{a}", m) for a, m in vars(value).items()
                        if isinstance(m, types.FunctionType)] if isinstance(value, type) else [])
            skipped.extend(f"{stem}.{label}" for label, member in members
                           if member.__code__ not in walked)
    assert not skipped, f"rebound code the walk missed: {skipped}"


def test_every_global_in_rebound_write_service_code_resolves_in_the_facade(facade):
    missing = unresolved_globals(vars(facade))
    assert not missing, (
        "rebound write-service code reads globals write_server does not define; import them into "
        f"okengine-mcp/write_server.py (they would be NameError at call time): {missing}")


def test_the_detector_names_the_method_that_lost_a_facade_import(facade, monkeypatch):
    """Negative fixture: the v0.15.15 shape. Without write_server's hand-added
    `authorization_header` import, the write MCP's auth middleware cannot resolve it."""
    monkeypatch.delitem(vars(facade), "authorization_header")
    missing = unresolved_globals(vars(facade))
    assert "authorization_header" in missing.get("_ScopedWriteAuth.__call__", []), missing


def test_nested_code_is_walked(tmp_path, monkeypatch):
    """Negative fixture for the recursion: a missing name used only inside a closure is found.
    The fixture is a real file outside src/: code compiled under a nonexistent src/ path makes
    CI's `coverage combine` fail with "No source for code"."""
    module = tmp_path / "synthetic.py"
    module.write_text("def outer():\n    def inner():\n        return only_in_a_closure\n"
                      "    return inner\n")
    monkeypatch.setitem(globals(), "SERVICES_DIR", tmp_path)
    namespace: dict = {}
    exec(compile(module.read_text(), str(module), "exec"), namespace)
    assert unresolved_globals(namespace) == {"outer": ["only_in_a_closure"]}
