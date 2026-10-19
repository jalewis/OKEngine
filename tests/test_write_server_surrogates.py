"""No page frontmatter holds an unpaired UTF-16 surrogate, whichever write tool made it (okengine#894).

A feed writer put an emoji into YAML as a JSON surrogate-pair escape (`"\\ud83d\\udd25"`). YAML
decodes each half alone, so the title raw-compile copied onto a page was two lone surrogates:
not text, with no UTF-8 encoding, and one of them aborted a whole projection rebuild. The write
path now rejoins a well-formed pair into its character and refuses a half with no partner,
naming the field, on every tool that accepts frontmatter.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"
PAIR = '"Providers \\ud83d\\udd25"'
LONE = '"Providers \\ud83d"'


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\ntypes:\n  concept:\n    required: [type]\nstrict_types: false\n",
        encoding="utf-8")
    page = tmp_path / "wiki" / "concepts" / "existing.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\nid: concepts:existing\ntitle: Existing\n---\n# Existing\n",
                    encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 9, 26).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _fm(vault: Path, rel: str) -> dict:
    return yaml.safe_load((vault / "wiki" / f"{rel}.md").read_text(encoding="utf-8").split("---")[1])


def _refused_naming_title(result: str) -> None:
    assert result.startswith("rejected: frontmatter `title` holds an unpaired UTF-16 surrogate"), result


def test_create_rejoins_a_surrogate_pair_into_its_character(ws):
    m, vault = ws
    result = m._create("concepts/flame", f"type: concept\ntitle: {PAIR}\naliases: [{PAIR}]", "# F")
    assert "concepts/flame" in result, result
    fm = _fm(vault, "concepts/flame")
    assert fm["title"] == "Providers \U0001F525"
    assert fm["aliases"] == ["Providers \U0001F525"], "list values are rejoined too"


def test_create_refuses_an_unpaired_half_and_writes_nothing(ws):
    m, vault = ws
    _refused_naming_title(m._create("concepts/flame", f"type: concept\ntitle: {LONE}", "# F"))
    assert not (vault / "wiki" / "concepts" / "flame.md").exists()


def test_create_refuses_an_unpaired_half_passed_as_a_mapping(ws):
    """Deterministic lanes hand the write path a dict, not YAML text; the invariant is the same."""
    m, vault = ws
    _refused_naming_title(m._create("concepts/flame", {"type": "concept", "title": "x \udd25"}))
    assert not (vault / "wiki" / "concepts" / "flame.md").exists()


def test_a_nested_unpaired_half_is_found(ws):
    m, _vault = ws
    result = m._create("concepts/flame", {"type": "concept", "extra": {"k": ["ok", "\ud83d"]}})
    assert result.startswith("rejected: frontmatter `extra` holds an unpaired"), result


def test_update_refuses_an_unpaired_half_and_leaves_the_page(ws):
    m, vault = ws
    before = (vault / "wiki" / "concepts" / "existing.md").read_text(encoding="utf-8")
    _refused_naming_title(m._update("concepts/existing", f"title: {LONE}"))
    assert (vault / "wiki" / "concepts" / "existing.md").read_text(encoding="utf-8") == before


def test_converge_refuses_an_unpaired_half(ws):
    m, vault = ws
    before = (vault / "wiki" / "concepts" / "existing.md").read_text(encoding="utf-8")
    _refused_naming_title(m._converge("concepts/existing", f"type: concept\ntitle: {LONE}"))
    assert (vault / "wiki" / "concepts" / "existing.md").read_text(encoding="utf-8") == before


def test_patch_refuses_a_text_edit_that_leaves_an_unpaired_half(ws):
    m, vault = ws
    before = (vault / "wiki" / "concepts" / "existing.md").read_text(encoding="utf-8")
    _refused_naming_title(m._patch("concepts/existing", "title: Existing", f"title: {LONE}"))
    assert (vault / "wiki" / "concepts" / "existing.md").read_text(encoding="utf-8") == before


def test_malformed_yaml_keeps_its_own_message(ws):
    m, _vault = ws
    assert m._create("concepts/flame", "type: [concept") == \
        "rejected: frontmatter_yaml is not a valid YAML mapping"


@pytest.mark.parametrize("half", ["\ud800", "\udfff"], ids=["first-high", "last-low"])
def test_both_ends_of_the_surrogate_range_are_refused(ws, half):
    m, _vault = ws
    _refused_naming_title(m._create("concepts/flame", {"type": "concept", "title": f"x {half}"}))
