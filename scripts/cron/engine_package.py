#!/usr/bin/env python3
"""Make the ``okengine`` package importable when this tree is loaded BY PATH.

A deployment pip-installs the engine, so ``import okengine`` just works there. Several
``scripts/cron`` modules are ALSO loaded by path by external consumers — the okpacks-library
compose check does exactly this, and so does any tool that points
``importlib.util.spec_from_file_location`` at one of these files. Those consumers install
nothing, so the package is not importable and the module fails at import time with an opaque
``ModuleNotFoundError: No module named 'okengine'``.

That is not hypothetical. Adding ``corpus_audit -> okengine.actor_identity`` turned every merge
request in okpacks-library red, with nothing on this side to signal a downstream consumer had
broken (okpacks-library#89). Each consumer having to discover and declare our layout is the
wrong contract: a module that can be loaded by path should be importable by path.

**An installed package always wins.** ``ensure()`` imports first and returns untouched if that
succeeds, so a deployment keeps using its installed engine and never silently shadows it with an
adjacent source tree of a different version. The fallback is appended, not inserted, for the same
reason.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: ``scripts/cron/engine_package.py`` -> repo root -> ``src``.
_SRC = Path(__file__).resolve().parents[2] / "src"


def ensure() -> bool:
    """Guarantee ``import okengine`` works. Returns True if the fallback was applied.

    Idempotent and safe to call from module scope. Never raises: a caller that genuinely has no
    package available should fail on its own real import, with its own traceback, rather than on
    a bootstrap helper.
    """
    try:
        import okengine  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        return False
    if not (_SRC / "okengine" / "__init__.py").is_file():
        return False
    src = str(_SRC)
    if src not in sys.path:
        sys.path.append(src)
    return True
