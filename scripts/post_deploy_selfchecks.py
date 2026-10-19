#!/usr/bin/env python3
"""post_deploy_verify.sh step [9]: run the shared deployment checks host-side, from THIS checkout.

Usage (what the verifier runs, from the deployment dir)::

    python3 <engine>/scripts/post_deploy_selfchecks.py <deployment>

Output is one tab-separated row per finding, ``LEVEL<TAB>CHECK<TAB>AREA<TAB>MESSAGE``, then a
terminal ``END`` row. ``CHECK`` is the registered check name that produced the finding (``-`` when
no check did), which is what ``framework doctor --checks`` accepts; ``AREA`` is the finding's own
label, which often is not a check name (``tool search``, ``validator``, ``sub-domains``), so a hint
built from it pointed the operator at an unknown check (okengine#1042 [21]). Fields are never
empty because bash ``read`` with a tab ``IFS`` collapses consecutive tabs.

The ``END`` row is the completion proof: a run that crashed, was killed, or could not encode its
output leaves no ``END``, and the verifier reports that as a FAIL rather than reading the silence
as "no findings" (okengine#1042 [15]).

Imports are pinned to the engine checkout this file lives in (okengine#1043). The deployer's
``python3`` may carry another okengine in its site-packages, an editable install pointing at an old
worktree or a stale top-level ``tools`` package, and the checks then judged the deployment with
that code: the v0.15.12 roll failed on a stale validator the checkout did not contain. The checkout
paths go first on ``sys.path``, engine modules something imported before this ran (a ``.pth``
``import`` line) are dropped, and any engine module that still resolves outside the checkout (or
the deployment's staged ``.hermes-data/scripts``) is reported as an import-provenance FAIL naming
both paths.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
#: Top-level module names the engine owns besides its script stems.
_PACKAGES = ("okengine", "tools")


def engine_paths(engine: Path) -> list[str]:
    """Where this checkout's modules live, in import-priority order. (`tools` is bound directly,
    see bind_tools.)"""
    return [str(engine / "scripts" / "cron"), str(engine / "scripts"), str(engine / "src")]


def import_roots(engine: Path, deployment: str) -> list[str]:
    """The directories an engine module may legitimately be loaded from: the checkout's import
    paths and its `tools/`, plus the deployment's staged scripts (deploy-cron-scripts copies this
    checkout's libs there, and check_schema imports the staged schema_lib on purpose).

    Deliberately not the whole checkout: anything can sit below it (a test's tmp dir does, under
    the mutation lanes' TMPDIR), and a module loaded from there is not this engine's code."""
    paths = [*engine_paths(engine), str(engine / "tools"),
             os.path.join(deployment, ".hermes-data", "scripts")]
    return [os.path.realpath(path) for path in paths]


def owned_names(engine: Path) -> set[str]:
    """Top-level module names that must come from the engine checkout."""
    names = set(_PACKAGES)
    for directory in (engine / "scripts" / "cron", engine / "scripts"):
        names.update(path.stem for path in directory.glob("*.py"))  # glob-ok: flat source dirs
    # A stem that names a stdlib module is not the engine's to claim: judging `types` by origin
    # would report the interpreter's own stdlib as a foreign engine module.
    return names - set(sys.stdlib_module_names)


def module_origin(module) -> str | None:
    """The file a module was loaded from (a namespace package's first portion), or None."""
    origin = getattr(module, "__file__", None)
    if origin:
        return origin
    portions = list(getattr(module, "__path__", None) or [])
    return portions[0] if portions else None


def _under(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def foreign_modules(names: set[str], roots: list[str], modules) -> list[tuple[str, str]]:
    """(module, origin) for each loaded engine module that resolves outside `roots`."""
    found = []
    for name in sorted(modules):
        module = modules[name]
        if name.split(".")[0] not in names or module is None:
            continue
        origin = module_origin(module)
        if origin and not _under(origin, roots):
            found.append((name, origin))
    return found


def bind_tools(engine: Path, modules) -> None:
    """Bind `tools` to the checkout's own directory and nothing else.

    The engine's `tools/` is a namespace package (no ``__init__.py``), and a regular ``tools``
    package anywhere on the path beats a namespace portion however early that portion sits, so path
    order alone cannot pin it. Binding the package with a single search location makes
    ``tools.schema_validator`` this checkout's file."""
    location = engine / "tools"
    if not location.is_dir():
        return
    spec = importlib.machinery.ModuleSpec("tools", None)
    spec.submodule_search_locations = [str(location)]     # this, not is_package, makes a package
    modules["tools"] = importlib.util.module_from_spec(spec)


def pin(engine: Path, roots: list[str], path: list[str], modules) -> None:
    """Put the checkout first on `path`, drop engine modules already loaded from elsewhere (a
    ``.pth`` import line runs before this script does), and bind `tools` to the checkout."""
    wanted = engine_paths(engine)
    path[:] = wanted + [entry for entry in path if entry not in wanted]
    for name, _origin in foreign_modules(owned_names(engine), roots, modules):
        del modules[name]
    bind_tools(engine, modules)


def _row(level: str, check: str, area: str, msg: str) -> str:
    return "\t".join((level, check or "-", area or "-", " ".join(str(msg).split()) or "-"))


def run_checks(checks, deployment: str):
    """Every registered check except the in-container ones, each run on its own so a finding keeps
    the name of the check that produced it. A generator: rows are printed as they are produced, so
    a run killed part-way still shows what it found before it died."""
    checks.configure(deployment, data=os.path.join(deployment, ".hermes-data"),
                     hermes=os.path.join(deployment, ".no-baked-image-on-host"),
                     env=checks.host_env(deployment))
    for name in checks.CHECKS:
        if name in checks.POST_DEPLOY_HOST_EXCLUDED:
            continue
        for level, area, msg in checks.run([name]):
            yield _row(level, name, area, msg)


def _emit(row: str) -> None:
    print(row, flush=True)


def main(argv: list[str], engine: Path = ENGINE, emit=_emit) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    deployment = argv[1] if argv[1:] else os.getcwd()
    roots = import_roots(engine, deployment)
    pin(engine, roots, sys.path, sys.modules)
    count = 0
    try:
        import deployment_checks as checks
        for row in run_checks(checks, deployment):
            emit(row)
            count += 1
    except Exception as exc:  # the driver itself, not a check: run() already contains those
        emit(_row("ERR", "-", "-", f"{type(exc).__name__}: {exc}"))
        count += 1
    for name, origin in foreign_modules(owned_names(engine), roots, sys.modules):
        emit(_row("FAIL", "-", "engine-import", (
            f"{name} was imported from {origin}, not from the engine checkout being deployed "
            f"({engine}); the checks above judged this deployment with that code. The "
            f"interpreter {sys.executable} carries another okengine on its path")))
        count += 1
    emit(_row("END", "-", "-", str(count)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
