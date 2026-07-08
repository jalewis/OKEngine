"""Regression: the glossary reference counter must count aliased/anchored wikilinks.

`_LINK` demanded `]]` immediately after the slug, so a term referenced only via its display alias
(`[[glossary/api-gateway|API gateway]]`) or an anchor (`[[glossary/api-gateway#usage]]`) counted
zero references, never reached MIN_REFS, and a heavily-used undefined term was never surfaced.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "extensions" / "okengine.glossary" / "select_undefined_terms.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(tmp):
    os.environ["WIKI_PATH"] = str(tmp)
    spec = importlib.util.spec_from_file_location("select_undefined_terms", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_undefined_terms"] = m
    spec.loader.exec_module(m)
    return m


def test_link_matches_plain_alias_and_anchor(tmp_path):
    m = _load(tmp_path)
    assert m._LINK.findall("[[glossary/api-gateway]]") == ["api-gateway"]
    assert m._LINK.findall("[[glossary/api-gateway|API gateway]]") == ["api-gateway"]
    assert m._LINK.findall("[[glossary/api-gateway#usage]]") == ["api-gateway"]
    # a paragraph mixing forms counts each occurrence
    txt = "see [[glossary/api-gateway|API gateway]] and [[glossary/api-gateway#usage]] and [[glossary/rate-limit]]"
    assert m._LINK.findall(txt) == ["api-gateway", "api-gateway", "rate-limit"]
