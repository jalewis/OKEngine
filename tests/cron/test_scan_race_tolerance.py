"""Regression: vault scan loops must tolerate pages vanishing mid-scan.

Cron lanes glob whole vault subtrees and read each page, while mover lanes
(reshelve, url-reconcile, schema-type-drain, orphans-drain) relocate pages
concurrently — the same cadence every day, so a glob-then-read without
tolerance is a standing crash (three okcti lanes died on FileNotFoundError
during the 2026-07-13 catch-up stampede).

Two layers:
  1. Behavior — drive the two selector scripts against a vault containing a
     dangling symlink: glob lists it, read_text()/stat() raise FileNotFoundError,
     exactly like a page deleted between glob and read. Deterministic race.
  2. Guard — AST-scan every engine cron script and extension script for an
     unguarded read reached from a loop over a glob, so a new scan loop can't
     reintroduce the class.
"""
import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- behavior

def test_select_raw_batch_tolerates_vanished_source_pages(tmp_path):
    script = REPO / "scripts" / "cron" / "select_raw_batch.py"
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    raw = tmp_path / "raw" / "2026"
    raw.mkdir(parents=True)
    (raw / "f0.txt").write_text("content")
    # dangling symlink: listed by rglob("*.md"), read_text raises FileNotFoundError
    (tmp_path / "wiki" / "sources" / "ghost.md").symlink_to(tmp_path / "wiki" / "sources" / "gone.md")
    env = {"WIKI_PATH": str(tmp_path), "BATCH_SIZE": "5", "MIN_YEAR": "2025",
           "PATH": os.environ.get("PATH", "")}
    r = subprocess.run([sys.executable, str(script)], capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, f"crashed on vanished page:\n{r.stdout}\n{r.stderr}"
    assert "f0.txt" in r.stdout          # the real raw file still drains


def test_select_entity_candidates_tolerates_vanished_pages(tmp_path, capsys):
    pytest.importorskip("yaml")
    mod_path = REPO / "scripts" / "cron" / "select_entity_candidates.py"
    vault = tmp_path / "vault"
    home = tmp_path / "home"
    for rel, body in [
        ("sources/s1.md", "---\ntype: source\npublisher: Acme\n---\n# A source\n"),
        ("entities/a/acme.md", "---\ntype: vendor\n---\n# Acme\n"),
    ]:
        p = vault / "wiki" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    # dangling symlinks in both scanned trees (entities rglob + sources rglob/stat)
    (vault / "wiki" / "entities" / "ghost.md").symlink_to(vault / "wiki" / "entities" / "gone.md")
    (vault / "wiki" / "sources" / "ghost.md").symlink_to(vault / "wiki" / "sources" / "gone.md")

    os.environ["WIKI_PATH"] = str(vault)
    os.environ["HERMES_HOME"] = str(home)
    sys.modules.pop("select_entity_candidates", None)
    spec = importlib.util.spec_from_file_location("select_entity_candidates", mod_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    rc = m.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "entities/a/acme" in out or "entities/acme" in out


# ------------------------------------------------------------------- guard

def _has_glob(node) -> bool:
    return any(
        isinstance(n, ast.Call)
        and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("glob", "rglob", "iglob")
        for n in ast.walk(node)
    )


def _is_read_call(n: ast.Call) -> bool:
    name = getattr(n.func, "attr", getattr(n.func, "id", ""))
    if name in ("read_text", "read_bytes"):
        return True
    if name == "open":
        # only flag reads: open(p), open(p, "r"), open(p, mode="r"), encoding kwargs
        for a in n.args[1:2]:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                return a.value.startswith("r")
        for kw in n.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value).startswith("r")
        return True  # no mode → default "r"
    return False


def _unguarded_reads(nodes) -> list:
    out = []

    def walk(n, guarded):
        if isinstance(n, ast.Lambda):
            return  # deferred execution — judged at its call site, not here
        if isinstance(n, ast.Try):
            guarded = True
        if isinstance(n, ast.Call) and not guarded and _is_read_call(n):
            out.append(n.lineno)
        for c in ast.iter_child_nodes(n):
            walk(c, guarded)

    for x in nodes:
        walk(x, False)
    return out


def _scan_file(path: Path) -> list:
    """Return [(lineno, detail)] for unguarded reads reached from glob loops."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    glob_vars = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and _has_glob(n.value):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    glob_vars.add(t.id)
    helpers = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            reads = _unguarded_reads(n.body)
            if reads:
                helpers[n.name] = reads
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.For):
            iters = [(n.iter, n.body)]
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            iters = [(g.iter, [n]) for g in n.generators]
        else:
            continue
        for it, body in iters:
            from_glob = (
                _has_glob(it)
                or (isinstance(it, ast.Name) and it.id in glob_vars)
                or any(isinstance(a, ast.Name) and a.id in glob_vars for a in ast.walk(it))
            )
            if not from_glob:
                continue
            for ln in _unguarded_reads(body):
                hits.append((ln, "unguarded read in glob loop"))
            for top in body:
                for c in ast.walk(top):
                    if isinstance(c, ast.Call):
                        nm = getattr(c.func, "id", getattr(c.func, "attr", ""))
                        if nm in helpers:
                            hits.append((c.lineno, f"calls {nm}() which reads unguarded at {helpers[nm]}"))
    return sorted(set(hits))


def test_no_unguarded_glob_scan_reads():
    """Every read reached from a glob loop in engine cron scripts and extension
    scripts must sit under a try (skip-on-OSError) — pages move mid-scan."""
    targets = sorted(REPO.glob("scripts/cron/*.py")) + sorted(REPO.glob("extensions/*/[a-z]*.py"))
    assert targets, "no scan targets found — repo layout changed?"
    offenders = []
    for path in targets:
        for ln, detail in _scan_file(path):
            offenders.append(f"{path.relative_to(REPO)}:{ln}: {detail}")
    assert not offenders, (
        "glob-then-read race (page can vanish mid-scan — wrap the read in "
        "try/except OSError → continue):\n" + "\n".join(offenders)
    )
