"""Regression: the deterministic bare-name → canonical-entity link normalizer (#153)."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "normalize_bare_name_links.py"


def _load():
    os.environ.setdefault("WIKI_PATH", "/nonexistent-vault")
    spec = importlib.util.spec_from_file_location("normalize_bare_name_links", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["normalize_bare_name_links"] = m
    spec.loader.exec_module(m)
    return m


def test_rewrites_bare_name_preserving_display():
    m = _load()
    idx = {"qilin": {"entities/q/qilin"}, "velvet-ant": {"entities/v/velvet-ant"}}
    valid = {"entities/q/qilin", "qilin", "entities/v/velvet-ant", "velvet-ant"}
    body, fixes = m.rewrite_text("By [[Qilin]] and [[Velvet Ant|the group]].", idx, valid)
    assert "[[entities/q/qilin]]" in body
    assert "[[entities/v/velvet-ant|the group]]" in body      # display preserved
    assert len(fixes) == 2


def test_skips_ambiguous_missing_pathform_junk_and_resolving():
    m = _load()
    idx = {"qilin": {"entities/q/qilin"}, "dup": {"entities/a/dup", "entities/b/dup"}}
    valid = {"entities/q/qilin", "qilin"}
    src = "[[Dup]] [[Nonexistent]] [[concepts/foo]] [[1]] [[qilin]]"
    body, fixes = m.rewrite_text(src, idx, valid)
    assert fixes == []           # ambiguous / missing / path-form / numeric-junk / already-resolving
    assert body == src


def test_idempotent_second_pass_is_noop():
    m = _load()
    idx = {"qilin": {"entities/q/qilin"}}
    valid = {"entities/q/qilin", "qilin"}
    once, f1 = m.rewrite_text("Hit [[Qilin]].", idx, valid)
    twice, f2 = m.rewrite_text(once, idx, valid)
    assert f1 and not f2 and once == twice
