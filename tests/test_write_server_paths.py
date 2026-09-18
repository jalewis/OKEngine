"""Path containment and reference normalisation on the write path (okengine#462, T1).

`_safe()` decides where an agent-supplied path actually lands. Its failure modes are
NOT classic escapes -- those the resolve guard catches. They are the paths that stay
*inside* wiki/ while landing in the wrong place, silently creating duplicate
canonicals that later surface as partition dups and dangling refs:

  wiki/sources/x        doubled prefix   -> wiki/wiki/sources/x   (okengine#31)
  /opt/vault/wiki/x     over-qualified   -> wiki/opt/vault/...    (okengine#31/#34)
  entities/acme         wrong shard      -> duplicate canonical   (okengine#48)

The reference helpers (`_strip_wikilink`, `_looks_like_ref_list`, `_flatten_strip`)
undo the nested-list shape YAML produces for a bare `[[x]]` value -- the mangling
behind several historical field-drift defects.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "sources" / "2026").mkdir(parents=True)
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\nstrict_types: false\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def rel_to_wiki(p, vault):
    return p.relative_to((vault / "wiki").resolve()).as_posix()


# ── _safe: containment and the "inside but wrong" cases ──────────────────────

def test_safe_resolves_a_plain_relative_path_and_forces_md(ws):
    mod, vault = ws
    p = mod._safe("sources/2026/x")
    assert p is not None and rel_to_wiki(p, vault) == "sources/2026/x.md"


def test_safe_refuses_escapes_outside_the_wiki(ws):
    mod, _ = ws
    for escape in ("../../etc/passwd", "sources/../../../../etc/hosts"):
        assert mod._safe(escape) is None, escape


def test_safe_strips_a_redundant_wiki_prefix(ws):
    """A doubled prefix stays INSIDE wiki/, so the escape guard cannot catch it --
    it would silently misfile every page and break raw-drain dedup (okengine#31)."""
    mod, vault = ws
    assert rel_to_wiki(mod._safe("wiki/sources/2026/x"), vault) == "sources/2026/x.md"


def test_safe_collapses_an_over_qualified_absolute_path(ws):
    """An agent following "prefer the absolute form" guidance may pass the full vault
    path to a write tool; unhandled that lands in a shadow tree (okengine#31/#34)."""
    mod, vault = ws
    for over in (f"{vault}/wiki/sources/2026/x",
                 f"{str(vault).lstrip('/')}/wiki/sources/2026/x"):
        resolved = mod._safe(over)
        assert resolved is not None, over
        assert rel_to_wiki(resolved, vault) == "sources/2026/x.md", over


def test_safe_appends_md_without_truncating_a_dotted_slug(ws):
    mod, vault = ws
    p = mod._safe("cves/openssl-3.0.7-advisory")
    assert rel_to_wiki(p, vault) == "cves/openssl-3.0.7-advisory.md"


def test_partition_redirect_cannot_bypass_reserved_file_guard(ws, monkeypatch):
    mod, vault = ws
    reserved = vault / "wiki" / "README.md"
    monkeypatch.setitem(mod._create.__globals__, "_partitioned_create_path",
                        lambda _path, _fm: reserved)

    result = mod._create("sources/new-page", {"type": "source"}, "# New page\n")

    assert result.startswith("refused:") and "reserved" in result.lower()
    assert not reserved.exists()


# ── _normalize_entity_shard: one canonical per entity (okengine#48) ──────────

def test_flat_entity_path_is_normalised_to_the_shard_layout(ws):
    mod, _ = ws
    assert mod._normalize_entity_shard("entities/acme.md") == "entities/a/acme.md"


def test_shard_letters_are_recomputed_from_the_slug_not_trusted(ws):
    """An agent that picks the wrong shard must not create a stale duplicate."""
    mod, _ = ws
    assert mod._normalize_entity_shard("entities/z/acme.md") == "entities/a/acme.md"


def test_other_namespaces_are_left_untouched(ws):
    mod, _ = ws
    for rel in ("sources/2026/06/x.md", "concepts/phishing.md", "cves/2026/CVE-1.md"):
        assert mod._normalize_entity_shard(rel) == rel


def test_multi_char_intermediate_segments_are_left_alone(ws):
    """That is some other layout, not the shard scheme."""
    mod, _ = ws
    assert mod._normalize_entity_shard("entities/threat-actors/acme.md") == \
        "entities/threat-actors/acme.md"


def test_normalize_shard_tolerates_an_empty_stem(ws):
    mod, _ = ws
    assert mod._normalize_entity_shard("entities/.md") == "entities/.md"


# ── reference-list normalisation ─────────────────────────────────────────────

def test_strip_wikilink_unwraps_links_and_passes_other_values_through(ws):
    mod, _ = ws
    assert mod._strip_wikilink("[[concepts/x]]") == "concepts/x"
    assert mod._strip_wikilink("[[concepts/x|Display]]") == "concepts/x"
    assert mod._strip_wikilink("[[concepts/x#anchor]]") == "concepts/x"
    assert mod._strip_wikilink("  [[concepts/x]]  ") == "concepts/x"
    assert mod._strip_wikilink("concepts/x") == "concepts/x", "plain path unchanged"
    assert mod._strip_wikilink(7) == 7, "non-strings pass through untouched"


def test_looks_like_ref_list_detects_the_shapes_that_need_canonicalising(ws):
    mod, _ = ws
    assert mod._looks_like_ref_list([["concepts/x"]]), "YAML nests a bare [[x]] value"
    assert mod._looks_like_ref_list(["[[concepts/x]]"])
    assert not mod._looks_like_ref_list(["concepts/x"]), "already canonical"
    assert not mod._looks_like_ref_list("concepts/x"), "not a list"
    assert not mod._looks_like_ref_list([])


def test_flatten_strip_flattens_unwraps_and_dedups_in_order(ws):
    mod, _ = ws
    value = [[["[[concepts/x]]"]], "[[concepts/y]]", "concepts/x", "", None]
    assert mod._flatten_strip(value) == ["concepts/x", "concepts/y"]
