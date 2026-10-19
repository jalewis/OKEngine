"""Observable contracts of the write-path policy guards (okengine#612).

Each test loads the enforced write server (`okengine-mcp/write_server.py`) against a real temp
vault and asserts a verdict of the policy service it binds: the existing-page frontmatter
check, the log ledger, vault-relative keys, the tombstone and source-receipt refusals, namespace
resolution for walk-up sub-domains, per-namespace permissions, the declared-namespace and
type-home guards, the soft review flags, and field-drift normalization.

Several cases build an equal but distinct string (`_fresh`) where production compares a value
parsed from YAML or JSON with another parsed value: those comparisons must be by value, never
by object identity.
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
  source: {}
  tool: {}
strict_types: false
partitioning:
  namespaces:
    entities: {strategy: flat}
    concepts: {strategy: flat}
    findings: {strategy: flat}
"""


def _load(root: Path, monkeypatch, schema: str = _SCHEMA):
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(schema, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    name = f"write_server_policy_mutation_{id(root)}"
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
    """An equal string that is a distinct object, like a value parsed from a request."""
    out = "".join(list(value))
    assert out == value and out is not value
    return out


# --- existing-page frontmatter, ledger, keys ------------------------------------------------


def test_invalid_existing_frontmatter_reports_exactly_120_chars_of_the_yaml_error(vault):
    m, root = vault
    page = root / "wiki" / "concepts" / "broken.md"
    page.parent.mkdir(parents=True)
    bad = "key: [unclosed, list, " + "x" * 50 + "\nother: value\n"
    page.write_text("---\n" + bad + "---\nbody\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError) as caught:
        yaml.safe_load(bad)
    detail = str(caught.value)
    assert len(detail) > 121, "the fixture must produce an error longer than the cut"

    result = m._frontmatter_error(page)

    assert result == f"existing page has invalid frontmatter YAML: {detail[:120]}"


def test_log_ledger_creates_a_missing_vault_tree(tmp_path, monkeypatch):
    m = _load(tmp_path / "boot", monkeypatch)
    fresh_root = tmp_path / "not" / "yet" / "there"
    monkeypatch.setenv("WIKI_PATH", str(fresh_root))

    m._append_log("- first line")

    assert (fresh_root / "wiki" / "log.md").read_text(encoding="utf-8") == "- first line\n"


def test_rel_keys_a_resolved_page_under_a_symlinked_vault(tmp_path, monkeypatch):
    real = tmp_path / "real"
    m = _load(real, monkeypatch)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("WIKI_PATH", str(link))
    page = (real / "wiki" / "entities" / "acme.md").resolve()

    assert m._rel(page) == "entities/acme.md"


# --- tombstone and receipt refusals ----------------------------------------------------------


def test_a_status_sorting_after_tombstoned_is_not_a_tombstone(vault):
    m, root = vault
    page = root / "wiki" / "entities" / "acme.md"
    assert m._tombstone_refuse({"status": "unverified"}, page) is None
    assert m._tombstone_refuse({"status": " Tombstoned "}, page).startswith(
        "refused: entities/acme.md is tombstoned"
    )


@pytest.mark.parametrize("current", ["active", "unverified", "", None])
def test_retiring_a_live_page_outside_tombstone_entity_is_refused(vault, current):
    m, root = vault
    page = root / "wiki" / "entities" / "acme.md"
    cur = {} if current is None else {"status": current}

    result = m._tombstone_transition_refuse(cur, {"status": _fresh("tombstoned")}, page)

    assert result is not None and result.startswith(
        "refused: entities/acme.md would become tombstoned"
    )


@pytest.mark.parametrize("new_status", ["unverified", "zz", "active", ""])
def test_a_status_that_is_not_tombstoned_is_never_a_retirement(vault, new_status):
    m, root = vault
    page = root / "wiki" / "entities" / "acme.md"
    assert m._tombstone_transition_refuse({"status": "active"}, {"status": new_status}, page) is None


def test_an_already_tombstoned_page_keeping_its_status_is_not_a_new_retirement(vault):
    m, root = vault
    page = root / "wiki" / "entities" / "acme.md"
    cur = {"status": _fresh("tombstoned")}
    assert m._tombstone_transition_refuse(cur, {"status": _fresh("Tombstoned")}, page) is None


@pytest.mark.parametrize("page_type", ["concept", "tool"])
def test_duplicate_receipt_status_is_refused_only_on_source_pages(vault, page_type):
    m, root = vault
    page = root / "wiki" / "concepts" / "x.md"
    assert m._source_receipt_refuse({"type": page_type, "status": "duplicate-receipt"}, page) is None


def test_a_source_status_sorting_after_duplicate_receipt_is_allowed(vault):
    m, root = vault
    page = root / "wiki" / "sources" / "x.md"
    assert m._source_receipt_refuse({"type": "source", "status": "verified"}, page) is None
    refused = m._source_receipt_refuse({"type": _fresh("Source"), "status": "duplicate-receipt"},
                                       page)
    assert refused.startswith("rejected: sources/x.md cannot store status duplicate-receipt")


def test_placeholder_url_guard_ignores_a_type_sorting_after_source(vault):
    m, root = vault
    page = root / "wiki" / "tools" / "x.md"
    assert m._placeholder_url_refuse({"type": "tool", "url": "https://example.com/a"}, page) is None
    assert m._placeholder_url_refuse({"type": "source", "url": "https://example.com/a"},
                                     page) is not None


# --- namespace resolution --------------------------------------------------------------------


def _container(root: Path, *parts: str) -> None:
    d = root.joinpath("wiki", *parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "schema.yaml").write_text("okf: {}\n", encoding="utf-8")


def test_namespace_skips_three_nested_subdomain_containers(vault):
    m, root = vault
    _container(root, "c1")
    _container(root, "c1", "c2")
    _container(root, "c1", "c2", "c3")
    page = root / "wiki" / "c1" / "c2" / "c3" / "entities" / "acme.md"
    assert m._namespace(page) == "entities"


def test_namespace_skips_two_nested_subdomain_containers(vault):
    m, root = vault
    _container(root, "c1")
    _container(root, "c1", "c2")
    page = root / "wiki" / "c1" / "c2" / "entities" / "acme.md"
    assert m._namespace(page) == "entities"


def test_namespace_of_a_page_directly_in_a_container_is_never_the_container(vault):
    m, root = vault
    _container(root, "acme")
    assert m._namespace(root / "wiki" / "acme" / "overview.md") == "overview.md"


# --- permissions -----------------------------------------------------------------------------


def _with_permissions(tmp_path, monkeypatch, block: str):
    return _load(tmp_path, monkeypatch, _SCHEMA + block)


def test_create_denial_does_not_deny_an_update(tmp_path, monkeypatch):
    m = _with_permissions(tmp_path, monkeypatch,
                          "permissions:\n  namespaces:\n    findings: {create: false}\n")
    page = tmp_path / "wiki" / "findings" / "f.md"
    assert m._policy_reject(page, {"type": "concept"}, "update") is None
    assert m._policy_reject(page, {"type": "concept"}, "create") == (
        "namespace 'findings' is not agent-writable (create denied; human-authored)"
    )


@pytest.mark.parametrize("op", ["create", "update"])
def test_only_an_explicit_false_denies_a_namespace(tmp_path, monkeypatch, op):
    m = _with_permissions(tmp_path, monkeypatch,
                          "permissions:\n  namespaces:\n    findings: {create: 0, update: 0}\n")
    page = tmp_path / "wiki" / "findings" / "f.md"
    assert m._policy_reject(page, {"type": "concept"}, op) is None


# --- declared-namespace and type-home guards -------------------------------------------------


def test_an_excluded_dir_is_writable_even_when_declared_namespaces_exist(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA + "exclude: [wiki/operational/]\n")
    assert m._namespace_reject(tmp_path / "wiki" / "operational" / "x.md") is None


def test_a_namespace_both_declared_and_excluded_stays_writable(tmp_path, monkeypatch):
    schema = _SCHEMA + "    operational: {strategy: flat}\nexclude: [wiki/operational/]\n"
    m = _load(tmp_path, monkeypatch, schema)
    assert m._namespace_reject(tmp_path / "wiki" / "operational" / "x.md") is None


def test_an_unreadable_exclude_list_fails_open_for_both_namespace_guards(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA + "exclude: wiki/operational/\n")
    page = tmp_path / "wiki" / "concepts" / "x.md"
    assert m._namespace_reject(tmp_path / "wiki" / "stray" / "x.md") is None
    assert m._type_namespace_reject(page, {"type": "source"}) is None


def test_undeclared_namespace_rejection_names_the_closest_declared_one(vault):
    m, root = vault
    result = m._namespace_reject(root / "wiki" / "concept" / "x.md")
    assert result.startswith("namespace 'concept/' is not declared in schema.yaml (declared ")
    assert result.endswith(
        "]) — did you mean 'concepts/'? — a page written here forks into a stray tree the "
        "dashboards/index never see (okengine#115)"
    )


def test_a_type_whose_home_is_not_declared_here_is_not_enforced(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA + "type_namespaces: {tool: tools}\n")
    # `tool` homes to `tools/`, which this schema does not declare.
    assert m._type_namespace_reject(tmp_path / "wiki" / "concepts" / "x.md", {"type": "tool"}) is None


def _with_sources(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch, _SCHEMA + "    sources: {strategy: flat}\n")


def test_changing_a_page_type_to_one_homed_elsewhere_is_rejected(tmp_path, monkeypatch):
    m = _with_sources(tmp_path, monkeypatch)
    page = tmp_path / "wiki" / "concepts" / "x.md"
    result = m._type_ns_reject_on_change(page, {"type": "source"}, {"type": "tool"})
    assert result is not None and result.startswith("type 'source' belongs in 'sources/'")


def test_an_unchanged_type_on_a_grandfathered_page_is_not_rejected(tmp_path, monkeypatch):
    m = _with_sources(tmp_path, monkeypatch)
    page = tmp_path / "wiki" / "concepts" / "x.md"
    assert m._type_ns_reject_on_change(page, {"type": _fresh("source")},
                                       {"type": _fresh("source")}) is None


# --- soft review flags -----------------------------------------------------------------------

_REVIEW = """\
review:
  confidence_review_values: [confirmed]
"""


@pytest.fixture
def review_vault(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch, _SCHEMA + _REVIEW), tmp_path


def test_a_new_categorical_confidence_is_flagged_on_create(review_vault):
    m, root = review_vault
    page = root / "wiki" / "concepts" / "x.md"
    assert m._review_flags(page, {"confidence": "Confirmed"}) == [
        "agent asserted categorical `confidence: Confirmed`"
    ]


def test_an_unchanged_categorical_confidence_never_re_flags_on_update(review_vault):
    m, root = review_vault
    page = root / "wiki" / "concepts" / "x.md"
    prev = {"confidence": _fresh("confirmed")}
    assert m._review_flags(page, {"confidence": _fresh("Confirmed")}, prev=prev) == []


def test_a_categorical_confidence_changed_on_update_is_flagged(review_vault):
    m, root = review_vault
    page = root / "wiki" / "concepts" / "x.md"
    assert m._review_flags(page, {"confidence": "confirmed"}, prev={"confidence": "low"}) == [
        "agent asserted categorical `confidence: confirmed`"
    ]


def test_a_declared_confidence_field_name_is_the_one_reviewed(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA + _REVIEW + "  confidence_field: certainty\n")
    page = tmp_path / "wiki" / "concepts" / "x.md"
    assert m._review_flags(page, {"certainty": "confirmed"}) == [
        "agent asserted categorical `certainty: confirmed`"
    ]
    assert m._review_flags(page, {"confidence": "confirmed"}) == []


def test_an_absent_confidence_field_is_never_flagged(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA + "review:\n  confidence_review_values: ['']\n")
    page = tmp_path / "wiki" / "concepts" / "x.md"
    assert m._review_flags(page, {"type": "concept"}) == []


# --- field-drift normalization ---------------------------------------------------------------


def test_allowed_and_always_known_fields_are_not_drift(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, _SCHEMA + "allowed:\n  concept: [type, name]\n")
    page = tmp_path / "wiki" / "concepts" / "x.md"
    out, flags = m._normalize_drift({"type": "concept", "name": "X", "id": "concepts:x"}, page)
    assert flags == []
    out, flags = m._normalize_drift({"type": "concept", "name": "X", "colour": "red"}, page)
    assert flags == ["unknown field(s) for type `concept` (not in schema): colour"]


def test_a_field_alias_target_is_a_known_field(tmp_path, monkeypatch):
    schema = (_SCHEMA + "allowed:\n  concept: [name]\n"
              "field_aliases:\n  country: suspected_origin\n")
    m = _load(tmp_path, monkeypatch, schema)
    page = tmp_path / "wiki" / "concepts" / "x.md"
    out, flags = m._normalize_drift({"type": "concept", "name": "X", "country": "CN"}, page)
    assert out["suspected_origin"] == "CN" and "country" not in out
    assert flags == []
