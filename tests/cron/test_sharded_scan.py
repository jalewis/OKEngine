"""Sharded-scan discipline guard.

Engine namespaces can be hierarchically sharded by a pack's schema
(`partitioning`): `sources/<year>/<month>/`, `entities/<type>/<letter>/`, …
A non-recursive `Path.glob("*.md")` on a namespace ROOT then silently sees only
the unsharded top level (~1% of the corpus). In the origin deployment this broke
13 cron scripts + the daily digest after the OKF migration.

Discipline (see docs/sharded-scan-discipline.md): a scanner that means "every
page in this namespace" MUST use `rglob`. A bare `.glob(` is only allowed when it
is genuinely shard-safe — a per-directory walker that recurses itself, an already
shard-leaf dir, a flat non-content dir, or a namespace-dir discovery glob. Each
such call must carry an inline `# glob-ok: <reason>` waiver at the call site, so
the exception is explicit and reviewed rather than an accident.

This test fails on any bare `.glob(` in the engine source tree that lacks a
waiver — forcing the author to either switch to `rglob` or justify the glob.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Engine source roots that scan vault content. tests/ is excluded (this file
# itself talks about `.glob(`), as are generated/runtime trees.
SCAN_ROOTS = ["scripts", "tools", "okengine-mcp", "okengine-reader", "okengine-cockpit"]

# `.rglob(` does not contain the substring `.glob(` (the char before `glob` is
# `r`, not `.`), so a plain search for `.glob(` matches only the non-recursive call.
_GLOB = re.compile(r"\.glob\(")
_WAIVER = "glob-ok"


def _iter_py():
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


def test_no_unwaived_bare_glob_in_engine_scanners():
    violations: list[str] = []
    for p in _iter_py():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not _GLOB.search(line):
                continue
            # Waiver may sit inline or on the immediately-preceding line.
            prev = lines[i - 1] if i > 0 else ""
            if _WAIVER in line or _WAIVER in prev:
                continue
            rel = p.relative_to(REPO).as_posix()
            violations.append(f"{rel}:{i + 1}: {line.strip()}")

    assert not violations, (
        "Bare `.glob(` on possibly-sharded namespaces — use `.rglob(` or add an "
        "inline `# glob-ok: <reason>` waiver (see docs/sharded-scan-discipline.md):\n  "
        + "\n  ".join(violations)
    )


def test_every_waiver_states_a_reason():
    """A waiver must carry a reason after the marker, not just the bare token —
    an empty `# glob-ok` is not a justification."""
    bad: list[str] = []
    pat = re.compile(r"#\s*glob-ok\b[:\s]*(.*)$")
    for p in _iter_py():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines()):
            m = pat.search(line)
            if m and not m.group(1).strip():
                bad.append(f"{p.relative_to(REPO).as_posix()}:{i + 1}")
    assert not bad, "glob-ok waivers missing a reason:\n  " + "\n  ".join(bad)
