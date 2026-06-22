"""okengine#49: _find_schema must not cache a negative result forever — a long-running
validator/write-server would otherwise stay fail-open for a tree whose schema.yaml is added or
moved later (until restart). Entries expire (TTL) and a vanished positive is re-resolved."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

MOD = Path(__file__).resolve().parent.parent / "tools" / "schema_validator.py"


def _load():
    spec = importlib.util.spec_from_file_location("schema_validator", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["schema_validator"] = m
    spec.loader.exec_module(m)
    return m


def test_negative_lookup_expires_when_schema_added(tmp_path):
    sv = _load()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "x.md"
    page.write_text("---\ntype: x\n---\n", encoding="utf-8")
    assert sv._find_schema(str(page)) is None              # no schema yet -> negative cached

    (tmp_path / "schema.yaml").write_text("okf:\n  required: [type]\n", encoding="utf-8")
    key = str(wiki.resolve())
    assert key in sv._dir_to_schema                         # the negative was cached
    sv._dir_to_schema[key] = (sv.time.monotonic() - 1e6, None)  # force the entry stale

    found = sv._find_schema(str(page))
    assert found is not None and found.name == "schema.yaml"  # re-walked + picked up the new schema


def test_vanished_positive_is_reresolved(tmp_path):
    sv = _load()
    (tmp_path / "schema.yaml").write_text("okf:\n  required: [type]\n", encoding="utf-8")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "x.md"
    page.write_text("---\ntype: x\n---\n", encoding="utf-8")
    assert sv._find_schema(str(page)).name == "schema.yaml"  # positive cached

    (tmp_path / "schema.yaml").unlink()                      # schema removed
    assert sv._find_schema(str(page)) is None                # is_file() guard -> re-resolved, not a dead path
