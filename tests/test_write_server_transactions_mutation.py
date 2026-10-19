"""Observable contracts of the create/update/tombstone/flag transactions (okengine#612).

Each test drives the enforced write path (`okengine-mcp/write_server.py`) against a real
temp vault and asserts what a caller or the next writer can observe: where the page
landed, its stored frontmatter, the version it was stamped with, the policy verdict, the
review queue, the log ledger, and how a later write deduplicates against it.

Capability tests run as a networked extension caller with a real policy-plane
capability; the only substitution is `_effective_policy`, the composed policy document
the deployment would otherwise discover on disk.
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
  entity: {required: [type, name]}
  concept: {}
  topic: {}
  tool: {}
  actor: {}
  threat_actor: {}
type_aliases: {threat_actor: actor}
strict_types: false
partitioning:
  namespaces:
    entities: {strategy: by-letter}
    concepts: {strategy: flat}
    topics: {strategy: flat}
    catalog: {strategy: by-type}
"""


def _load(root: Path, monkeypatch, schema: str = _SCHEMA):
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(schema, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    name = f"write_server_txn_mutation_{id(root)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch), tmp_path


def _fm(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{page} has no frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1]) or {}


def _write_page(page: Path, fm: dict, body: str = "body\n") -> None:
    """A page placed on disk by a no_agent lane, not through the write server."""
    fm = {"id": f"{page.parent.name}:{page.stem}", **fm}
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body,
                    encoding="utf-8")


def _log(root: Path) -> list[str]:
    log = root / "wiki" / "log.md"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _queue(root: Path) -> str:
    queue = root / "wiki" / "_review-queue.md"
    return queue.read_text(encoding="utf-8") if queue.exists() else ""


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


# --- create: partition routing -------------------------------------------------------------


def test_create_routes_to_a_partition_seat_that_sorts_after_the_flat_key(vault):
    m, root = vault
    # by-type files `catalog/alpha` under `catalog/tool/`: the canonical path sorts AFTER the
    # flat spelling, so the redirect must be decided by inequality, never by ordering.
    result = m._create("catalog/alpha", {"type": "tool", "name": "Alpha"}, "body")
    assert result == "created catalog/tool/alpha.md v1"
    assert (root / "wiki/catalog/tool/alpha.md").is_file()
    assert not (root / "wiki/catalog/alpha.md").exists(), "a flat duplicate seat was minted"


# --- create: underscore type spellings ----------------------------------------------------


def test_underscore_type_declared_only_as_an_alias_is_admitted(tmp_path, monkeypatch):
    schema = _SCHEMA.replace("  threat_actor: {}\n", "")
    m = _load(tmp_path, monkeypatch, schema)
    result = m._create("entities/ta-one", {"type": "threat_actor", "name": "TA One",
                                           "sources": [_cite(m, "TA One")]}, "body")
    assert result == "created entities/t/ta-one.md v1"
    assert _fm(tmp_path / "wiki/entities/t/ta-one.md")["type"] == "actor"


def test_underscore_type_declared_as_both_type_and_alias_is_admitted(vault):
    m, root = vault
    result = m._create("entities/ta-two", {"type": "threat_actor", "name": "TA Two",
                                           "sources": [_cite(m, "TA Two")]}, "body")
    assert result == "created entities/t/ta-two.md v1"


def test_undeclared_underscore_type_is_rejected(vault):
    m, root = vault
    result = m._create("entities/x", {"type": "rogue_type", "name": "X"}, "body")
    assert result == "rejected: type 'rogue_type' is not declared by the governing schema"


# --- create/update: body authority --------------------------------------------------------


def test_create_body_change_is_derived_from_whether_a_body_is_written(vault, monkeypatch):
    m, root = vault
    with _ExtensionCaller(m, monkeypatch, operations=["create"], types=["*"],
                          update_fields=["*"], body="deny"):
        denied = m._create("concepts/with-body", {"type": "concept", "name": "With"}, "text")
        allowed = m._create("concepts/no-body", {"type": "concept", "name": "Without"}, "")
    assert denied.startswith("rejected: ") and "body mutation is denied" in denied
    assert not (root / "wiki/concepts/with-body.md").exists()
    assert allowed == "created concepts/no-body.md v1"


def test_update_body_change_is_derived_from_whether_the_body_is_supplied(vault, monkeypatch):
    m, root = vault
    assert m._create("concepts/doc", {"type": "concept", "name": "Doc"}, "old") \
        == "created concepts/doc.md v1"
    with _ExtensionCaller(m, monkeypatch, operations=["update"], types=["*"],
                          update_fields=["*"], body="deny"):
        metadata_only = m._update("concepts/doc", {"summary": "s"})
        rewrite = m._update("concepts/doc", None, "new body")
    assert metadata_only == "updated concepts/doc.md v2"
    assert rewrite.startswith("rejected: ") and "body mutation is denied" in rewrite
    assert (root / "wiki/concepts/doc.md").read_text(encoding="utf-8").endswith("old")


# --- update: page-type authority ----------------------------------------------------------


def test_update_capability_sees_the_stored_type_when_the_patch_keeps_it(vault, monkeypatch):
    m, root = vault
    m._create("entities/acme", {"type": "entity", "name": "Acme"}, "body")
    with _ExtensionCaller(m, monkeypatch, operations=["update"], types=["entity"],
                          update_fields=["*"], body="allow"):
        result = m._update("entities/a/acme", {"summary": "s"})
    assert result == "updated entities/a/acme.md v2"


def test_update_capability_judges_the_resulting_type_not_the_stored_one(vault, monkeypatch):
    m, root = vault
    m._create("entities/acme", {"type": "entity", "name": "Acme"}, "body")
    with _ExtensionCaller(m, monkeypatch, operations=["update"], types=["entity"],
                          update_fields=["*"], body="allow"):
        result = m._update("entities/a/acme", {"type": "concept"})
    assert result.startswith("rejected: ")
    assert "page type 'concept' is not allowed" in result
    assert _fm(root / "wiki/entities/a/acme.md")["type"] == "entity"


# --- composition provenance ---------------------------------------------------------------


def test_create_records_the_discovering_pack(vault, monkeypatch):
    m, root = vault
    monkeypatch.setenv("OKENGINE_PACK", "pack-a")
    m._create("concepts/found", {"type": "concept", "name": "Found"}, "body")
    fm = _fm(root / "wiki/concepts/found.md")
    assert fm["discovered_by"] == "pack-a"
    assert fm["maintained_by"] == ["pack-a"]


def test_update_adds_a_maintainer_but_never_claims_discovery(vault, monkeypatch):
    m, root = vault
    m._create("concepts/legacy", {"type": "concept", "name": "Legacy"}, "body")
    monkeypatch.setenv("OKENGINE_PACK", "pack-b")
    assert m._update("concepts/legacy", {"summary": "s"}) == "updated concepts/legacy.md v2"
    fm = _fm(root / "wiki/concepts/legacy.md")
    assert fm["maintained_by"] == ["pack-b"]
    assert "discovered_by" not in fm, "a later maintainer was recorded as the discoverer"


# --- create: write-synchronous identity claims --------------------------------------------


@pytest.mark.parametrize("path, fm", [
    ("concepts/widget", {"type": "concept", "name": "Widget"}),
    ("topics/widget", {"type": "topic", "name": "Widget"}),
    ("entities/retired", {"type": "entity", "name": "Retired", "status": "tombstoned"}),
], ids=["concept", "topic", "tombstoned-entity"])
def test_only_live_entities_join_the_in_process_name_index(vault, path, fm):
    m, root = vault
    assert m._create(path, fm, "body").startswith("created ")
    # A lane writes a live entity straight to disk. The in-process name index has no
    # entity yet, so the next create's alias dedup must still scan the disk and find it.
    _write_page(root / "wiki/entities/e/existing-corp.md",
                {"type": "entity", "name": "Existing Corp", "id": "entities:existing-corp"})
    result = m._create("entities/newco", {"type": "entity", "name": "NewCo",
                                          "aliases": ["Existing Corp"]}, "body")
    # Never mint a second canonical for an alias-matched live entity. An entity the id index
    # does not hold cannot be converged into yet, so the write is refused by name -- it used to
    # recurse converge -> create -> dedup until the interpreter limit (a bogus "not valid YAML"
    # rejection, or an uncaught RecursionError on Python 3.11).
    assert not (root / "wiki/entities/n/newco.md").exists(), (
        f"{path} entered the entity name index, so dedup skipped the disk scan: {result}")
    assert (root / "wiki/entities/e/existing-corp.md").is_file()
    assert result.startswith("refused: ") and "entities/e/existing-corp" in result, result
    assert "recursion" not in result


def test_a_live_entity_with_any_status_is_claimed_for_the_next_create(vault):
    m, root = vault
    _write_page(root / "wiki/entities/s/seed.md",
                {"type": "entity", "name": "Seed", "id": "entities:seed"})
    assert m._create("entities/seed-two", {"type": "entity", "name": "Seed Two"}, "b") \
        .startswith("created ")  # builds the index from disk (seed is indexed)
    assert m._create("entities/vendor", {"type": "entity", "name": "Vendor Co",
                                         "status": "verified"}, "body") \
        == "created entities/v/vendor.md v1"
    result = m._create("entities/other", {"type": "entity", "name": "Other",
                                          "aliases": ["Vendor Co"]}, "body")
    assert not (root / "wiki/entities/o/other.md").exists(), result
    assert "entities/v/vendor.md" in result


# --- update: immutable fields --------------------------------------------------------------


def test_echoing_an_unchanged_immutable_field_is_not_a_reverted_change(vault):
    m, root = vault
    m._create("concepts/stable", {"type": "concept", "name": "Stable"}, "body")
    result = m._update("concepts/stable",
                       "created: '2026-06-15'\nid: concepts:stable\nsummary: s\n")
    assert result == "updated concepts/stable.md v2"
    assert "needs_review" not in _fm(root / "wiki/concepts/stable.md")
    assert "concepts/stable.md" not in _queue(root)


def test_changing_an_immutable_field_to_a_greater_value_is_reverted_and_flagged(vault):
    m, root = vault
    m._create("concepts/stable", {"type": "concept", "name": "Stable"}, "body")
    result = m._update("concepts/stable", {"id": "concepts:zzz", "summary": "s"})
    assert result.startswith("updated concepts/stable.md v2")
    fm = _fm(root / "wiki/concepts/stable.md")
    assert fm["id"] == "concepts:stable"
    assert fm["needs_review"] is True
    assert "immutable field change reverted: id" in _queue(root)


# --- update: link review is scoped to a body rewrite ---------------------------------------


def test_metadata_update_does_not_re_flag_links_already_in_the_body(vault):
    m, root = vault
    page = root / "wiki/concepts/legacy-links.md"
    _write_page(page, {"type": "concept", "name": "Legacy", "version": 1},
                "See [[concepts/nowhere]].\n")
    assert m._update("concepts/legacy-links", {"summary": "s"}) \
        == "updated concepts/legacy-links.md v2"
    assert "needs_review" not in _fm(page)
    assert "legacy-links" not in _queue(root)


def test_body_rewrite_flags_the_unresolvable_links_it_introduces(vault):
    m, root = vault
    m._create("concepts/fresh", {"type": "concept", "name": "Fresh"}, "clean")
    result = m._update("concepts/fresh", None, "See [[concepts/nowhere]].")
    assert result.startswith("updated concepts/fresh.md v2")
    assert _fm(root / "wiki/concepts/fresh.md")["needs_review"] is True
    assert "[[concepts/nowhere]] (no such page)" in _queue(root)


# --- version stamping ----------------------------------------------------------------------


def test_update_increments_an_existing_version_by_one(vault):
    m, root = vault
    page = root / "wiki/concepts/v.md"
    _write_page(page, {"type": "concept", "name": "V", "version": 2})
    assert m._update("concepts/v", {"summary": "s"}) == "updated concepts/v.md v3"
    assert _fm(page)["version"] == 3


def test_update_of_a_page_without_a_version_stamps_version_two(vault):
    m, root = vault
    page = root / "wiki/concepts/v.md"
    _write_page(page, {"type": "concept", "name": "V"})
    assert m._update("concepts/v", {"summary": "s"}) == "updated concepts/v.md v2"
    assert _fm(page)["version"] == 2


def test_update_of_a_page_with_a_non_integer_version_restarts_at_two(vault):
    m, root = vault
    page = root / "wiki/concepts/v.md"
    _write_page(page, {"type": "concept", "name": "V", "version": "draft"})
    assert m._update("concepts/v", {"summary": "s"}) == "updated concepts/v.md v2"
    assert _fm(page)["version"] == 2


@pytest.mark.parametrize("stored, expected", [
    ({"version": 2}, 3), ({}, 2), ({"version": "draft"}, 2),
], ids=["v2", "missing", "non-integer"])
def test_tombstone_stamps_the_next_version(vault, stored, expected):
    m, root = vault
    page = root / "wiki/concepts/t.md"
    _write_page(page, {"type": "concept", "name": "T", **stored})
    result = m._tombstone("concepts/t", "obsolete")
    assert result == f"tombstoned concepts/t.md v{expected} (file retained, not deleted)"
    assert _fm(page)["version"] == expected


@pytest.mark.parametrize("stored, expected", [({"version": 2}, 3), ({}, 2)],
                         ids=["v2", "missing"])
def test_section_append_stamps_the_next_version(vault, stored, expected):
    m, root = vault
    page = root / "wiki/concepts/s.md"
    _write_page(page, {"type": "concept", "name": "S", **stored}, "# S\n\n## Notes\n\nold\n")
    result = m._append_section("concepts/s", "Notes", "new note")
    assert not result.startswith(("rejected", "refused")), result
    assert _fm(page)["version"] == expected


# --- tombstone -----------------------------------------------------------------------------


def test_tombstone_with_an_unresolvable_successor_outside_entities_still_retires(vault):
    m, root = vault
    m._create("concepts/old", {"type": "concept", "name": "Old"}, "body")
    result = m._tombstone("concepts/old", "replaced", superseded_by="concepts/missing")
    assert result == "tombstoned concepts/old.md v2 (file retained, not deleted)"
    fm = _fm(root / "wiki/concepts/old.md")
    assert fm["status"] == "tombstoned"
    assert fm["superseded_by"] == "concepts/missing"


def test_tombstone_capability_counts_superseded_by_only_when_supplied(vault, monkeypatch):
    m, root = vault
    m._create("concepts/a", {"type": "concept", "name": "A"}, "body")
    m._create("concepts/b", {"type": "concept", "name": "B"}, "body")
    m._create("concepts/c", {"type": "concept", "name": "C"}, "body")
    fields = ["status", "tombstone_reason", "last_updated", "version"]
    with _ExtensionCaller(m, monkeypatch, operations=["tombstone"], types=["*"],
                          update_fields=fields, body="deny"):
        plain = m._tombstone("concepts/a", "obsolete")
        successor = m._tombstone("concepts/b", "replaced", superseded_by="concepts/c")
    assert plain == "tombstoned concepts/a.md v2 (file retained, not deleted)"
    assert successor.startswith("rejected: ")
    assert "fields exceed the update allowlist" in successor
    assert _fm(root / "wiki/concepts/b.md").get("status") != "tombstoned"


def test_tombstone_capability_sees_the_stored_page_type(vault, monkeypatch):
    m, root = vault
    m._create("concepts/typed", {"type": "concept", "name": "Typed"}, "body")
    with _ExtensionCaller(m, monkeypatch, operations=["tombstone"], types=["concept"],
                          update_fields=["*"], body="deny"):
        result = m._tombstone("concepts/typed", "obsolete")
    assert result == "tombstoned concepts/typed.md v2 (file retained, not deleted)"


def test_tombstone_preserves_the_creating_extension_and_lane(vault):
    m, root = vault
    page = root / "wiki/concepts/owned.md"
    _write_page(page, {"type": "concept", "name": "Owned", "version": 1,
                       "extension_id": "ext-a", "producer_lane": "lane-a"})
    assert m._tombstone("concepts/owned", "obsolete").startswith("tombstoned ")
    fm = _fm(page)
    assert fm["extension_id"] == "ext-a"
    assert fm["producer_lane"] == "lane-a"


def test_a_tombstoned_id_stays_retired_after_its_file_is_removed(vault):
    m, root = vault
    m._create("concepts/gone", {"type": "concept", "name": "Gone"}, "body")
    assert m._tombstone("concepts/gone", "obsolete").startswith("tombstoned ")
    (root / "wiki/concepts/gone.md").unlink()  # e.g. a purge lane removed the retired file
    result = m._converge("concepts/gone", {"type": "concept", "name": "Gone"}, "again")
    assert result.startswith("refused: id concepts:gone is tombstoned"), result
    assert not (root / "wiki/concepts/gone.md").exists()


# --- flag ----------------------------------------------------------------------------------


def test_flag_log_distinguishes_a_new_queue_row_from_a_repeat(vault):
    m, root = vault
    m._create("concepts/doubt", {"type": "concept", "name": "Doubt"}, "body")
    assert m._flag("concepts/doubt", "claim needs a source") \
        == "flagged concepts/doubt.md for review — queued in _review-queue.md"
    assert m._flag("concepts/doubt", "claim needs a source") \
        == "already flagged concepts/doubt.md for review — queue unchanged"
    entries = [line for line in _log(root) if "concepts/doubt.md —" in line]
    assert entries == [
        "- 2026-06-15 mcp-write flag concepts/doubt.md — claim needs a source",
        "- 2026-06-15 mcp-write flag already-queued concepts/doubt.md — claim needs a source",
    ]


def test_update_that_blanks_the_type_is_authorized_against_the_stored_type(vault, monkeypatch):
    m, root = vault
    from okengine import identity_guard   # granted (okengine#1028): this pins the capability
    monkeypatch.setattr(m, "_identity_edit_grant", lambda: identity_guard.ALL)
    m._create("entities/acme", {"type": "entity", "name": "Acme"}, "body")
    with _ExtensionCaller(m, monkeypatch, operations=["update"], types=["entity"],
                          update_fields=["*"], body="allow"):
        result = m._update("entities/a/acme", {"type": ""})
    # The capability check falls back to the stored type, so the write reaches the schema
    # gate, which names the real defect rather than a blank-type capability refusal.
    assert result == "rejected: missing required field(s): type"
    assert _fm(root / "wiki/entities/a/acme.md")["type"] == "entity"


def _cite(m, name):
    """A canonical source that names `name` as a threat group, so creating it is grounded in
    evidence rather than in the page's own prose (okengine#1012)."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    ref = f"sources/2026/01/01/{slug}-report"
    page = m._wiki() / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: source\ntitle: {name} report\n---\n\n"
                    f"{name} is a threat group that targeted banks.\n", encoding="utf-8")
    return ref
