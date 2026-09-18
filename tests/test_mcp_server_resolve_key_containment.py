"""okengine#660: the read-MCP `_resolve_key()` fell back to `(WIKI / target).is_file()` with no
containment check, so `retrieve_context("../CLAUDE")` served the vault-root persona file and a
symlink inside wiki/ pointing outside the vault was read too. `get_page` already went through
`_safe()`; the graph tools must use the same containment. Pins both routes and the on-disk key
normalization so an in-vault `..` cannot yield a non-canonical key."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

SRV = Path(__file__).resolve().parent.parent / "okengine-mcp" / "server.py"


def _load(monkeypatch, vault: Path):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    spec = importlib.util.spec_from_file_location("okengine_server_containment", SRV)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vault(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "acme.md").write_text("---\ntype: entity\n---\nAcme body\n")
    (tmp_path / "CLAUDE.md").write_text("PERSONA SECRET\n")
    (wiki / ".backlinks.json").write_text(json.dumps(
        {"backlinks": {"entities/a/acme": []}, "pages": 1, "targets": 1, "edges": 0}))
    return tmp_path


def test_resolve_key_refuses_parent_traversal(tmp_path, monkeypatch):
    m = _load(monkeypatch, _vault(tmp_path))
    bl = {"entities/a/acme": []}
    for escape in ("../CLAUDE", "../../etc/passwd", "entities/../../CLAUDE", "/../CLAUDE"):
        assert m._resolve_key(escape, bl) is None, f"{escape!r} must not resolve"


def test_retrieve_context_never_serves_outside_the_wiki(tmp_path, monkeypatch):
    m = _load(monkeypatch, _vault(tmp_path))
    out = m.retrieve_context("../CLAUDE")
    assert "PERSONA SECRET" not in out
    assert "no such page" in out or "not found" in out or "ambiguous" in out or "unresolved" in out.lower()


def test_find_references_never_resolves_outside_the_wiki(tmp_path, monkeypatch):
    m = _load(monkeypatch, _vault(tmp_path))
    out = m.find_references("../CLAUDE")
    assert "PERSONA SECRET" not in out and "# ../CLAUDE" not in out
    assert "not found or ambiguous" in out


def test_symlink_escaping_the_vault_is_refused(tmp_path, monkeypatch):
    root = _vault(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("OUTSIDE SECRET\n")
    link = root / "wiki" / "entities" / "a" / "leak.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem")
    m = _load(monkeypatch, root)
    assert m._resolve_key("entities/a/leak", {}) is None
    assert "OUTSIDE SECRET" not in m.retrieve_context("entities/a/leak")


def test_in_vault_dotdot_normalizes_to_the_canonical_key(tmp_path, monkeypatch):
    """`entities/a/../a/acme` stays inside wiki/ -- allowed, but the key handed back must be the
    canonical `entities/a/acme`, never the raw spelling (downstream lookups key the graph by it)."""
    m = _load(monkeypatch, _vault(tmp_path))
    assert m._resolve_key("entities/a/../a/acme", {}) == "entities/a/acme"
    assert m._resolve_key("entities/a/acme.md", {}) == "entities/a/acme"


def test_poisoned_artifact_key_cannot_read_outside_the_wiki(tmp_path, monkeypatch):
    """The artifact is cron-written, but treat its keys as data: a key that escapes wiki/ must not
    turn into a read. `_resolve_key` returns it (exact-key hit); the page read still goes through
    `_safe()` and degrades to the explicit placeholder instead of the file."""
    root = _vault(tmp_path)
    (root / "wiki" / ".backlinks.json").write_text(json.dumps(
        {"backlinks": {"../CLAUDE": []}, "pages": 1, "targets": 1, "edges": 0}))
    m = _load(monkeypatch, root)
    out = m.retrieve_context("../CLAUDE")
    assert "PERSONA SECRET" not in out
    assert "(page body unavailable)" in out
