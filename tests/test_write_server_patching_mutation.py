"""Observable contracts of patch_entity and append_section (okengine#612).

Each test drives the enforced write server (`okengine-mcp/write_server.py`) against a real temp
vault and asserts what a caller or the next reader can observe: the stored page bytes, the
verdict string, and the capability decision an extension caller receives. Capability cases run
as a networked extension caller holding a real policy-plane capability; the only substitution
is `_effective_policy`, the composed policy document a deployment would discover on disk.

The section-insertion and body-integrity helpers are pure functions of the body text, so their
contracts are pinned directly as well as through append_section.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  entity: {}
  concept: {}
strict_types: false
partitioning:
  namespaces:
    concepts: {strategy: flat}
"""


def _load(root: Path, monkeypatch, schema: str = _SCHEMA):
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(schema, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    name = f"write_server_patching_mutation_{id(root)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch), tmp_path


def _fresh(value: str) -> str:
    """An equal string that is a distinct object, like an argument parsed from a request."""
    out = "".join(list(value))
    assert out == value and out is not value
    return out


def _page(root: Path, slug: str = "doc") -> Path:
    return root / "wiki" / "concepts" / f"{slug}.md"


def _fm(page: Path) -> dict:
    return yaml.safe_load(page.read_text(encoding="utf-8").split("---\n", 2)[1]) or {}


def _body(page: Path) -> str:
    return page.read_text(encoding="utf-8").split("---\n", 2)[2]


def _seed(m, root: Path, body: str = "first line\nsecond line\n", **fm) -> Path:
    fields = {"type": "concept", "name": "Doc", "summary": "one", **fm}
    assert m._create("concepts/doc", fields, body) == "created concepts/doc.md v1"
    return _page(root)


class _ExtensionCaller:
    """Run writes as a networked extension holding one real policy-plane capability."""

    def __init__(self, m, monkeypatch, **capability):
        capability.setdefault("rule_id", "ext-write")
        capability.setdefault("paths", ["**"])
        self.m = m
        monkeypatch.setattr(m, "_effective_policy", lambda: {
            "rules": [{"id": "ext-write", "remediation": "narrow the write"}],
            "capabilities": {},
        })
        self.caller = {"kind": "extension", "actor": "extension:probe", "ext_id": "probe",
                       "write_scopes": ["**"], "write_capability": capability}

    def __enter__(self):
        self.token = self.m._caller_var.set(self.caller)
        return self

    def __exit__(self, *exc):
        self.m._caller_var.reset(self.token)


# --- patch: argument checks and exact results -----------------------------------------------


def test_patch_rejects_equal_old_and_new_strings_by_value(vault):
    m, root = vault
    page = _seed(m, root)
    before = page.read_text(encoding="utf-8")
    result = m._patch("concepts/doc", _fresh("first line"), _fresh("first line"))
    assert result == "rejected: old_string and new_string are identical"
    assert page.read_text(encoding="utf-8") == before


def test_patch_reports_exactly_120_chars_of_a_frontmatter_yaml_error(vault):
    m, root = vault
    page = _seed(m, root)
    old = "summary: one"
    new = "summary: [unclosed, " + "x" * 60 + "\nextra: value"
    candidate = page.read_text(encoding="utf-8").replace(old, new, 1)
    with pytest.raises(yaml.YAMLError) as caught:
        yaml.safe_load(candidate.split("---\n", 2)[1])
    detail = str(caught.value)
    assert len(detail) > 121

    result = m._patch("concepts/doc", old, new)

    assert result == f"rejected: edit produced invalid frontmatter YAML: {detail[:120]}"


def test_patch_stores_the_body_without_an_added_or_lost_leading_character(vault):
    m, root = vault
    page = _seed(m, root, body="alpha\nbeta\n")

    assert m._patch("concepts/doc", "beta", "gamma") == "patched concepts/doc.md v2"

    text = page.read_text(encoding="utf-8")
    assert text.endswith("\n---\nalpha\ngamma\n"), text
    assert _body(page) == "alpha\ngamma\n"


def test_patch_that_removes_a_legacy_malformed_heading_is_allowed(vault):
    m, root = vault
    page = _seed(m, root, body="intro\n")
    raw = page.read_text(encoding="utf-8") + "## ## Broken\nmore\n"
    page.write_text(raw, encoding="utf-8")

    assert m._patch("concepts/doc", "## ## Broken", "## Fixed") == "patched concepts/doc.md v2"
    assert "## Fixed" in _body(page)


def test_patch_that_removes_a_legacy_panel_heading_is_allowed(vault):
    m, root = vault
    page = _seed(m, root, body="intro\n")
    page.write_text(page.read_text(encoding="utf-8") + "## References\n- x\n", encoding="utf-8")

    assert m._patch("concepts/doc", "## References\n", "## Notes\n") == "patched concepts/doc.md v2"


# --- patch: capability inputs --------------------------------------------------------------


def _ext(m, monkeypatch, **cap):
    cap.setdefault("operations", ["patch", "append"])
    cap.setdefault("types", ["*"])
    cap.setdefault("update_fields", ["*"])
    cap.setdefault("body", "allow")
    return _ExtensionCaller(m, monkeypatch, **cap)


def test_patch_capability_sees_only_the_fields_whose_values_change(vault, monkeypatch):
    m, root = vault
    _seed(m, root)
    with _ext(m, monkeypatch, update_fields=["summary"]):
        result = m._patch("concepts/doc", "summary: one", "summary: two")
    assert result == "patched concepts/doc.md v2"


def test_patch_capability_counts_a_changed_existing_field(vault, monkeypatch):
    m, root = vault
    page = _seed(m, root)
    with _ext(m, monkeypatch, update_fields=["summary"]):
        result = m._patch("concepts/doc", "name: Doc", "name: Renamed")
    assert result.startswith("rejected: ") and "fields exceed the update allowlist" in result
    assert _fm(page)["name"] == "Doc"


def test_patch_capability_counts_an_added_field(vault, monkeypatch):
    m, root = vault
    page = _seed(m, root)
    with _ext(m, monkeypatch, update_fields=["summary"]):
        result = m._patch("concepts/doc", "summary: one", "summary: one\ncolour: red")
    assert result.startswith("rejected: ") and "fields exceed the update allowlist" in result
    assert "colour" not in _fm(page)


def test_frontmatter_only_patch_is_not_a_body_change(vault, monkeypatch):
    m, root = vault
    _seed(m, root)
    with _ext(m, monkeypatch, body="deny"):
        result = m._patch("concepts/doc", "summary: one", "summary: two")
    assert result == "patched concepts/doc.md v2"


@pytest.mark.parametrize("old,new", [("first line", "zzz line"), ("second line", "aaa line")])
def test_body_patch_is_a_body_replacement(vault, monkeypatch, old, new):
    m, root = vault
    page = _seed(m, root)
    with _ext(m, monkeypatch, body="deny"):
        result = m._patch("concepts/doc", old, new)
    assert result.startswith("rejected: ") and "body mutation is denied" in result
    assert old in _body(page)


def test_patch_capability_judges_the_patched_type(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA.replace("    concepts:", "    things: {strategy: flat}\n    concepts:"))
    assert m._create("things/doc", {"type": "entity", "name": "Doc"}, "b") == "created things/doc.md v1"
    from okengine import identity_guard   # granted (okengine#1028): this pins the capability
    monkeypatch.setattr(m, "_identity_edit_grant", lambda: identity_guard.ALL)
    with _ext(m, monkeypatch, types=["entity"]):
        allowed = m._patch("things/doc", "name: Doc", "name: Doc Two")
        changed = m._patch("things/doc", "type: entity", "type: concept")
    assert allowed == "patched things/doc.md v2"
    assert changed.startswith("rejected: ") and "page type 'concept' is not allowed" in changed


# --- append_section ------------------------------------------------------------------------


def test_append_capability_judges_the_stored_page_type(vault, monkeypatch):
    m, root = vault
    _seed(m, root)
    with _ext(m, monkeypatch, types=["concept"]):
        result = m._append_section("concepts/doc", "Notes", "new note")
    assert result == "appended to 'Notes' in concepts/doc.md v2 (section created)"


def test_append_by_an_extension_never_stamps_its_id_on_an_existing_page(vault, monkeypatch):
    m, root = vault
    page = _seed(m, root)
    with _ext(m, monkeypatch):
        m._append_section("concepts/doc", "Notes", "new note")
    assert "extension_id" not in _fm(page)


def test_append_into_an_existing_section_keeps_the_page_layout(vault):
    m, root = vault
    page = _seed(m, root, body="# Title\n## Notes\nold note\n\n\n## Other\nz\n")
    result = m._append_section("concepts/doc", "Notes", "new note")
    assert result == "appended to 'Notes' in concepts/doc.md v2 (appended to existing section)"
    assert _body(page) == "# Title\n## Notes\nold note\n\nnew note\n\n## Other\nz\n"


# --- _insert_into_section ------------------------------------------------------------------


def test_insert_matches_the_named_heading_not_one_sorting_after_it(vault):
    m, _ = vault
    body = "## Zeta\nz\n## Alpha\na\n"
    out, where = m._insert_into_section(body, "Alpha", "new")
    assert where == "appended to existing section"
    assert out == "## Zeta\nz\n## Alpha\na\n\nnew\n"


def test_insert_targets_the_first_of_two_same_named_sections(vault):
    m, _ = vault
    body = "## Notes\none\n## Notes\ntwo\n"
    out, _ = m._insert_into_section(body, "notes", "new")
    assert out == "## Notes\none\n\nnew\n\n## Notes\ntwo\n"


def test_insert_keeps_subsections_inside_the_section(vault):
    m, _ = vault
    body = "## Notes\nx\n### Detail\ny\n## Other\nz"
    out, _ = m._insert_into_section(body, "Notes", "new")
    assert out == "## Notes\nx\n### Detail\ny\n\nnew\n\n## Other\nz"


def test_insert_stops_at_a_higher_level_heading(vault):
    m, _ = vault
    body = "## Notes\nx\n# Next\ny"
    out, _ = m._insert_into_section(body, "Notes", "new")
    assert out == "## Notes\nx\n\nnew\n\n# Next\ny"


def test_insert_stops_at_the_first_following_sibling(vault):
    m, _ = vault
    body = "## Notes\nx\n## A\na\n## B\nb"
    out, _ = m._insert_into_section(body, "Notes", "new")
    assert out == "## Notes\nx\n\nnew\n\n## A\na\n## B\nb"


def test_insert_into_a_section_at_the_top_of_the_body(vault):
    m, _ = vault
    out, _ = m._insert_into_section("## Notes\nx\n## Other\ny", "Notes", "new")
    assert out == "## Notes\nx\n\nnew\n\n## Other\ny"


def test_insert_into_a_section_at_an_odd_line_index(vault):
    m, _ = vault
    out, _ = m._insert_into_section("# Title\n## Notes\nx\n## Other\ny", "Notes", "new")
    assert out == "# Title\n## Notes\nx\n\nnew\n\n## Other\ny"


def test_insert_into_an_empty_section_directly_followed_by_a_sibling(vault):
    m, _ = vault
    out, _ = m._insert_into_section("## Notes\n## Other\ny", "Notes", "new")
    assert out == "## Notes\n\nnew\n\n## Other\ny"


def test_insert_trims_every_trailing_blank_line_of_the_section(vault):
    m, _ = vault
    body = "a\nb\nc\n## Notes\nx\n\n\n\n## Other\ny"
    out, _ = m._insert_into_section(body, "Notes", "new")
    assert out == "a\nb\nc\n## Notes\nx\n\nnew\n\n## Other\ny"


def test_insert_trims_blank_lines_of_an_empty_section_down_to_its_heading(vault):
    m, _ = vault
    body = "a\n## Notes\n\n\n## Other\ny"
    out, _ = m._insert_into_section(body, "Notes", "new")
    assert out == "a\n## Notes\n\nnew\n\n## Other\ny"


def test_insert_at_the_end_of_the_body_keeps_one_terminal_newline(vault):
    m, _ = vault
    out, _ = m._insert_into_section("## Notes\nx\n\n", "Notes", "new")
    assert out == "## Notes\nx\n\nnew\n"


# --- body integrity --------------------------------------------------------------------------


def test_an_indented_fence_closes_on_an_unindented_fence(vault):
    m, _ = vault
    malformed, _ = m._body_integrity_counts("  ```\n## ## hidden\n```\n## ## Counted\n")
    assert malformed == 1


def test_a_fence_closes_only_on_the_same_fence_character(vault):
    m, _ = vault
    malformed, _ = m._body_integrity_counts("~~~\n```\n## ## hidden\n~~~\n")
    assert malformed == 0
    malformed, _ = m._body_integrity_counts("```\n~~~\n## ## hidden\n```\n")
    assert malformed == 0


def test_a_fence_closes_on_a_run_of_equal_or_greater_length(vault):
    m, _ = vault
    malformed, _ = m._body_integrity_counts("```\n## ## hidden\n```\n## ## Counted\n")
    assert malformed == 1
    malformed, _ = m._body_integrity_counts("```\n## ## hidden\n````\n## ## Counted\n")
    assert malformed == 1
    malformed, _ = m._body_integrity_counts("````\n```\n## ## hidden\n````\n")
    assert malformed == 0


def test_body_integrity_allows_fewer_defects_than_before(vault):
    m, _ = vault
    assert m._body_integrity_reject("## ## a\n## ## b\n", "## ## a\n") is None
    assert m._body_integrity_reject("## References\n## References\n", "## References\n") is None


def test_patch_capability_falls_back_to_the_stored_type_when_the_edit_drops_it(vault, monkeypatch):
    m, root = vault
    page = _seed(m, root)
    with _ext(m, monkeypatch, types=["concept"]):
        result = m._patch("concepts/doc", "type: concept\n", "")
    # Whatever later gate refuses a type-less page, the capability must judge the stored type.
    assert "page type" not in result, result
    assert _fm(page)["type"] == "concept"
