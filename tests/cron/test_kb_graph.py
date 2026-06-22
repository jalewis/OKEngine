"""kb_graph.py — de-noise IWE reference output (agent memory hygiene).

The agent traverses its knowledge graph via the MCP `find_references` tool, which
runs `kb_graph.py find` → IWE. IWE indexes generated catalog pages (HOT.md, the
Wiki Index, per-directory INDEX pages) that link to everything, so they'd otherwise
dominate every page's `referencedBy`. kb_graph filters them out.
"""
import importlib.util
import sys
from pathlib import Path

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"


def _load(name):
    sys.path.insert(0, str(CRON))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, CRON / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_is_reserved_key(monkeypatch):
    m = _load("kb_graph")
    monkeypatch.setattr(m, "_excluded_namespaces", lambda: frozenset({"operational"}))
    # IWE keys arrive without the .md extension
    for k in ("HOT", "log", "index", "entities/a/INDEX", "INDEX-p02",
              "_review-queue", "x.bak.1", "operational/source-backlink-drain-2026-06-20"):
        assert m._is_reserved_key(k), k
    for k in ("entities/a/autojack", "sources/2026/06/foo", "concepts/x",
              "briefings/2026-06-19", "dashboards/kb-health"):
        assert not m._is_reserved_key(k), k


_SAMPLE = """````markdown #entities/a/autojack
---
title: AutoJack
references:
- key: entities/a/autogen-studio
  title: AutoGen Studio
- key: sources/2026/06/autojack-rce
  title: AutoJack RCE
referencedBy:
- key: entities/a/INDEX
  title: 'Index: entities/a'
  sectionPath:
  - Pages
- key: entities/a/autogen-studio
  title: AutoGen Studio
  sectionPath:
  - Related
- key: index
  title: Wiki Index
  sectionPath:
  - Entities (5170)
- key: sources/2026/06/autojack-rce
  title: AutoJack RCE
type: vulnerability
description: 'references: this colon-key is body, not a list'
tags:
- autojack
"""


def test_filter_drops_catalog_pages_keeps_real_refs(monkeypatch):
    m = _load("kb_graph")
    monkeypatch.setattr(m, "_excluded_namespaces", lambda: frozenset())
    out = m._filter_iwe_refs(_SAMPLE)
    # generated catalogs removed from referencedBy
    assert "entities/a/INDEX" not in out
    assert "key: index\n" not in out and "Wiki Index" not in out
    # real edges preserved (both references and referencedBy)
    assert "entities/a/autogen-studio" in out
    assert out.count("sources/2026/06/autojack-rce") >= 2   # in references + referencedBy
    # page body untouched, including a decoy `references:`-prefixed body line + tag list
    assert "type: vulnerability" in out
    assert "this colon-key is body" in out
    assert "- autojack" in out
