"""okengine#1028: identity changes to an existing page need more than pack ownership.

With a pack principal (okengine#1027) and real owner names (a composed schema that declares them,
or okengine#1026's resolver), the owning pack's model lanes pass the ownership check. Lanes
routinely attempt retypes, renames and curated-field removals that are wrong; the replay of the
logged conflicts found every case below. Each is now HELD: the stored value stays, the change is
queued for review, and the rest of the write applies. A lane with an explicit `identity_edit`
grant, and the operator principal, apply them directly."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from okengine import identity_guard as guard

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import output_contract  # noqa: E402

_OWNED = {
    "okpack-competitive": ("product", "segment", "competitor"),
    "okpack-threat-actors": ("actor", "identity", "publisher", "malware"),
}


def _schema(pack: str, resolver: str) -> str:
    types = {t: {"required": ["type"], "owner": pack} for t in _OWNED[pack]}
    types["entity"] = {"required": ["type"]}
    doc = {"types": types, "partitioning": {"namespaces": {"entities": {"strategy": "flat"}}},
           "classify_catchall": ["organization"]}
    if resolver == "1298":   # the composed bookkeeping placeholder, resolved by schema_lib (#1026)
        doc["owners"] = {"types": {t: "pack" for t in _OWNED[pack]}}
    return yaml.safe_dump(doc, sort_keys=False)


def _load(tmp_path: Path, monkeypatch, pack: str, resolver: str = "main",
          actor: str | None = "cron:page-quality-enrich", grant: dict | None = None,
          capability_gate: bool = False):
    vault = tmp_path / "vault"
    (vault / "wiki" / "entities").mkdir(parents=True)
    (vault / "pack.yaml").write_text(f"name: {pack}\n")
    (vault / "wiki" / "schema.yaml").write_text(_schema(pack, resolver))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-16")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "report")
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    for name in ("OKENGINE_WRITE_ACTOR", "OKENGINE_CRON_JOBS", "OKENGINE_WRITE_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    if actor:
        monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    if grant is not None:
        contract = {"api": 1, "allowed_namespaces": ["*"], "allowed_types": ["*"],
                    "operations": ["update", "patch", "converge"], "required_fields": ["type"],
                    "required_relationships": [], "body": {"required": False},
                    "unknown_fields": "allow", "unresolved_links": "allow",
                    "placeholder_links": "allow", "completion": "run", "identity_edit": grant}
        raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        jobs = vault / "jobs.json"
        jobs.write_text(json.dumps({"jobs": [{
            "name": actor.removeprefix("cron:"), "output_contract": contract,
            "output_contract_digest": "sha256:" + hashlib.sha256(raw.encode()).hexdigest()}]}))
        monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
    spec = importlib.util.spec_from_file_location("write_server_identity_1028", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server_identity_1028"] = m
    spec.loader.exec_module(m)
    if not capability_gate:   # the real gate runs in test_extension_identity_grant_1032 (#1032 [6])
        monkeypatch.setattr(m, "_capability_reject", lambda *a, **k: None)   # not under test here
    return m, vault


def _seed(m, rel: str, fm: dict, body: str = "Curated body.") -> Path:
    page = m._safe(rel)   # the canonical (partitioned) seat the write path resolves
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body + "\n")
    return page


def _fm(page: Path) -> dict:
    return yaml.safe_load(page.read_text().split("---")[1])


def _logged(vault: Path) -> str:
    return (vault / "wiki" / "log.md").read_text()


_HAFNIUM = {"type": "actor", "name": "HAFNIUM", "title": "HAFNIUM", "id": "entities:hafnium",
            "aliases": ["Silk Typhoon", "HAFNIUM"], "country": "CN",
            "actor_type": "nation-state", "description": "Targets US entities."}

RESOLVERS = pytest.mark.parametrize("resolver", ["main", "1298"])


# --- the replayed cases, as the owning pack's lanes ----------------------------------------


@RESOLVERS
def test_a_competitive_retype_and_rename_are_held_while_enrichment_applies(tmp_path, monkeypatch,
                                                                          resolver):
    m, vault = _load(tmp_path, monkeypatch, "okpack-competitive", resolver)
    dota = _seed(m, "entities/dota-2.md", {"type": "product", "name": "Dota 2",
                                               "id": "entities:dota-2"})
    lgd = _seed(m, "entities/lgd-gaming.md", {"type": "competitor", "name": "LGD Gaming",
                                                  "title": "LGD Gaming", "id": "entities:lgd-gaming"})
    out = m._converge("entities/dota-2.md", {"type": "segment", "name": "Dota 2",
                                             "id": "entities:dota-2",
                                             "description": "A MOBA title."}, "New body.")
    assert out.startswith("converged"), out
    assert _fm(dota)["type"] == "product" and _fm(dota)["description"] == "A MOBA title."
    assert _fm(dota)["needs_review"] is True and "Curated body." in dota.read_text()
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:broken-wikilinks-drain")
    out = m._converge("entities/lgd-gaming.md", {"type": "competitor", "name": "LGD",
                                                 "title": "LGD", "id": "entities:lgd-gaming"})
    assert out.startswith("converged"), out
    assert (_fm(lgd)["name"], _fm(lgd)["title"]) == ("LGD Gaming", "LGD Gaming")
    assert "field `type`: 'product' -> 'segment'" in _logged(vault)
    assert "field `name`: 'LGD Gaming' -> 'LGD'" in _logged(vault)


@RESOLVERS
@pytest.mark.parametrize("retype", ["identity", "publisher"])
def test_an_actor_is_not_retyped_out_of_actor_by_its_owning_lane(tmp_path, monkeypatch,
                                                                  resolver, retype):
    # page-quality-enrich: entity-backfill's additive gate (#962) already drops a retype.
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", resolver)
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._converge("entities/hafnium.md", {**_HAFNIUM, "type": retype})
    assert out.startswith("converged"), out
    assert _fm(page)["type"] == "actor" and _fm(page)["needs_review"] is True


@RESOLVERS
def test_removing_an_actors_curated_fields_is_held(tmp_path, monkeypatch, resolver):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", resolver)
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._converge("entities/hafnium.md", {"type": "actor", "name": "HAFNIUM",
                                                       "id": "entities:hafnium"},
                      remove="aliases,country,actor_type,description")
    assert "-0 removed" in out, out
    fm = _fm(page)
    assert (fm["aliases"], fm["country"], fm["actor_type"], fm["description"]) == (
        ["Silk Typhoon", "HAFNIUM"], "CN", "nation-state", "Targets US entities.")
    for field in ("aliases", "country", "actor_type", "description"):
        assert f"field `{field}`" in _logged(vault)


@RESOLVERS
def test_a_malware_retitle_is_held_with_its_body(tmp_path, monkeypatch, resolver):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", resolver)
    page = _seed(m, "entities/xloader.md", {"type": "malware", "title": "XLoader",
                                                "id": "entities:xloader"}, "XLoader body.")
    out = m._converge("entities/xloader.md", {"type": "malware", "title": "Formbook",
                                              "id": "entities:xloader", "platform": "windows"}, "Formbook body.")
    assert out.startswith("converged"), out
    assert _fm(page)["title"] == "XLoader" and _fm(page)["platform"] == "windows"
    assert "XLoader body." in page.read_text() and "Formbook body." not in page.read_text()
    assert "body replacement in the same write is held" in _logged(vault)


# --- the other write paths ----------------------------------------------------------------


def test_update_cannot_retype_rename_or_clear_a_curated_field(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._update("entities/hafnium.md", {"type": "identity", "name": "Hafnium Corp",
                                            "country": None, "motivation": "espionage"},
                    "Replaced body.")
    assert out.startswith("updated") and "flagged for review" in out, out
    fm = _fm(page)
    assert (fm["type"], fm["name"], fm["country"]) == ("actor", "HAFNIUM", "CN")
    assert fm["motivation"] == "espionage" and "Curated body." in page.read_text()


def test_update_without_a_body_holds_only_the_frontmatter(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    assert m._update("entities/hafnium.md", {"name": "Hafnium Corp"}).startswith("updated")
    assert _fm(page)["name"] == "HAFNIUM" and "body replacement" not in _logged(vault)


def test_patch_cannot_retype_or_drop_an_alias(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._patch("entities/hafnium.md", "type: actor", "type: identity")
    assert out.startswith("patched"), out
    out = m._patch("entities/hafnium.md", "- Silk Typhoon\n", "")
    assert out.startswith("patched"), out
    fm = _fm(page)
    assert fm["type"] == "actor" and "Silk Typhoon" in fm["aliases"]


def test_patch_holds_a_body_edit_made_with_a_held_rename(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._patch("entities/hafnium.md", "description: Targets US entities.\n---\nCurated body.",
                   "description: Targets US entities.\n---\nRenamed body.")
    assert out.startswith("patched"), out
    assert "Renamed body." in page.read_text()          # a body-only edit is not identity
    text = page.read_text()
    span = text[text.index("title: HAFNIUM"):]           # one edit across frontmatter and body
    out = m._patch("entities/hafnium.md", span,
                   span.replace("title: HAFNIUM", "title: Other").replace("Renamed body.",
                                                                          "Other body."))
    assert out.startswith("patched"), out
    assert _fm(page)["title"] == "HAFNIUM" and "Renamed body." in page.read_text()
    assert "body replacement in the same write is held" in _logged(vault)


def test_a_create_that_dedup_redirects_cannot_rename_the_existing_page(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._create("entities/hafnium.md", {**_HAFNIUM, "name": "Other"}, "Body.")
    assert _fm(page)["name"] == "HAFNIUM", out


# --- what still applies --------------------------------------------------------------------


def test_adding_an_alias_and_backfilling_a_missing_title_are_not_identity_changes(tmp_path,
                                                                                  monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/apt29.md", {"type": "actor", "name": "APT29",
                                              "id": "entities:apt29", "aliases": ["Cozy Bear"]})
    out = m._converge("entities/apt29.md", {"type": "actor", "name": "APT29", "title": "APT29",
                                            "id": "entities:apt29",
                                            "aliases": ["Cozy Bear", "Midnight Blizzard"]})
    assert "0 conflict(s)" in out and "review" not in out, out
    assert _fm(page)["title"] == "APT29" and "Midnight Blizzard" in _fm(page)["aliases"]


def test_the_operator_principal_applies_identity_changes(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", actor=None)
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._converge("entities/hafnium.md", {**_HAFNIUM, "title": "Silk Typhoon"},
                      remove="country")
    assert "-1 removed" in out, out
    assert _fm(page)["title"] == "Silk Typhoon" and "country" not in _fm(page)


def test_a_granted_lane_applies_only_what_it_was_granted(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", actor="cron:curator",
                     grant={"fields": ["title", "remove"]})
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    m._converge("entities/hafnium.md", {**_HAFNIUM, "title": "Silk Typhoon", "type": "identity"},
                remove="country")
    fm = _fm(page)
    assert fm["title"] == "Silk Typhoon" and "country" not in fm and fm["type"] == "actor"


def test_the_classifier_grant_retypes_only_out_of_a_catch_all(tmp_path, monkeypatch):
    grant = {"fields": ["type"], "retype_from": ["entity", guard.CATCHALL]}
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors",
                     actor="cron:schema-classify-drain", grant=grant)
    bare = _seed(m, "entities/bare.md", {"type": "entity", "name": "Bare", "id": "entities:bare"})
    org = _seed(m, "entities/org.md", {"type": "organization", "name": "Org",
                                           "id": "entities:org"})
    actor = _seed(m, "entities/hafnium.md", _HAFNIUM)
    m._update("entities/bare.md", {"type": "publisher"})
    m._update("entities/org.md", {"type": "identity"})
    m._update("entities/hafnium.md", {"type": "identity"})
    assert (_fm(bare)["type"], _fm(org)["type"], _fm(actor)["type"]) == (
        "publisher", "identity", "actor")


# --- the guard itself ----------------------------------------------------------------------


def test_identity_changes_finds_each_kind_and_ignores_exempt_and_new_fields():
    before = {"type": "actor", "title": "A", "name": "", "aliases": ["X", "Y"], "country": "CN",
              "version": 3, "empty": ""}
    after = {"type": "identity", "title": " A ", "name": "New", "aliases": "x",
             "country": [], "region": "EU"}
    changes = guard.identity_changes(before, after, exempt={"version"})
    # `name: New` on a page called "A" (its title) is a rename, though the page had no `name`.
    assert [(c.field, c.kind) for c in changes] == [
        ("type", "retype"), ("name", "rename"), ("aliases", "alias-removal"),
        ("country", "removal")]
    assert (changes[1].before, changes[1].after) == ("A", "New")
    assert changes[2].before == ["Y"]


def test_hold_restores_scalars_and_appends_dropped_aliases():
    before = {"type": "actor", "aliases": ["X", "Y"], "country": "CN"}
    after = {"type": "identity", "aliases": "X"}
    held = guard.identity_changes(before, after)
    guard.hold(after, before, held)
    assert after == {"type": "actor", "aliases": ["X", "Y"], "country": "CN"}


@pytest.mark.parametrize(("grant", "expected"), [
    (None, guard.NONE), ("type", guard.NONE), ({"fields": "type"}, guard.NONE),
    ({"fields": [1]}, guard.NONE), ({"fields": ["type", 1]}, guard.NONE),
    ({"fields": ["type"], "retype_from": "entity"}, guard.NONE),
    ({"fields": ["type"], "retype_from": ["entity", 2]}, guard.NONE),
    ({"fields": ["type"], "retype_from": [2]}, guard.NONE),
    ({"fields": ["type", "bogus"]}, guard.Permission(frozenset({"type"}))),
    ({"fields": ["type"], "retype_from": ["entity"]},
     guard.Permission(frozenset({"type"}), frozenset({"entity"}))),
])
def test_a_malformed_grant_grants_nothing(grant, expected):
    assert guard.permission(grant) == expected


def test_permits_honors_fields_and_retype_sources():
    retype = guard.Change("type", "retype", "organization", "vendor")
    removal = guard.Change("country", "removal", "CN")
    typed = guard.Permission(frozenset({"type"}), frozenset({"entity", guard.CATCHALL}))
    assert guard.permits(typed, retype, before_type="organization", catchall=["organization"])
    assert not guard.permits(typed, retype, before_type="organization")
    assert guard.permits(typed, retype, before_type=" entity ")
    assert not guard.permits(typed, removal, before_type="actor")
    assert guard.permits(guard.Permission(frozenset({"type"})), retype, before_type="actor")
    assert guard.permits(guard.ALL, removal, before_type="actor")
    plain = guard.Permission(frozenset({"type"}), frozenset({"entity"}))
    assert not guard.permits(plain, retype, before_type="organization",
                             catchall=["organization"])
    assert guard.permits(guard.Permission(frozenset({"remove"})), removal, before_type="x")


def test_each_scalar_has_its_kind_and_none_is_also_a_removal():
    changes = guard.identity_changes({"type": "a", "title": "b", "name": "c"}, {})
    assert [(c.field, c.kind) for c in changes] == [
        ("type", "retype"), ("title", "rename"), ("name", "rename")]
    assert [c.grant for c in changes] == ["type", "title", "name"]
    dropped = guard.identity_changes({"aliases": ["X"]}, {})
    assert [(c.kind, c.grant) for c in dropped] == [("alias-removal", "aliases")]


def test_a_rename_is_not_bound_by_retype_sources():
    grant = guard.Permission(frozenset({"type", "title"}), frozenset({"entity"}))
    rename = guard.Change("title", "rename", "A", "B")
    assert guard.permits(grant, rename, before_type="actor")
    aliases = guard.Change("aliases", "alias-removal", ["X"])
    assert guard.permits(guard.Permission(frozenset({"aliases"})), aliases, before_type="actor")


def test_the_catch_all_token_is_never_a_type_itself():
    grant = guard.Permission(frozenset({"type"}), frozenset({guard.CATCHALL}))
    retype = guard.Change("type", "retype", guard.CATCHALL, "vendor")
    assert not guard.permits(grant, retype, before_type=guard.CATCHALL)


def test_the_catch_all_token_is_only_ever_expanded_where_granted():
    grant = guard.Permission(frozenset({"type"}), frozenset({"entity"}))
    retype = guard.Change("type", "retype", guard.CATCHALL, "vendor")
    assert not guard.permits(grant, retype, before_type=guard.CATCHALL)


@pytest.mark.parametrize(("change", "detail"), [
    (guard.Change("aliases", "alias-removal", ["X"]), "removal of ['X']"),
    (guard.Change("country", "removal", "CN"), "removal of 'CN'"),
    (guard.Change("type", "retype", "actor", "identity"), "'actor' -> 'identity'"),
])
def test_each_flag_names_the_field_and_what_was_held(change, detail):
    text = guard.flag(change)
    assert f"field `{change.field}`: {detail};" in text and "okengine#1028" in text


def test_changes_and_grants_are_immutable_values():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        guard.NONE.fields = guard.FIELDS
    with pytest.raises(dataclasses.FrozenInstanceError):
        guard.Change("type", "retype", "a").kind = "removal"


def test_holds_body_only_for_retypes_and_renames():
    assert not guard.holds_body([guard.Change("country", "removal", "CN"),
                                 guard.Change("aliases", "alias-removal", ["X"])])
    assert guard.holds_body([guard.Change("name", "rename", "A", "B")])


# --- the contract that grants it -----------------------------------------------------------

_CONTRACT = {"api": 1, "allowed_namespaces": ["*"], "allowed_types": ["*"],
             "operations": ["update", "converge"], "required_fields": [],
             "required_relationships": [], "body": {}, "unknown_fields": "allow",
             "unresolved_links": "allow", "placeholder_links": "allow",
             "completion": "run"}


def test_the_contract_and_the_write_path_name_the_same_identity_fields():
    assert output_contract.IDENTITY_FIELDS == set(guard.FIELDS)


@pytest.mark.parametrize(("grant", "error"), [
    ({"fields": ["type"], "retype_from": ["entity"]}, None),
    ("type", "identity_edit must be an object"),
    ({"fields": ["type"], "extra": 1}, "unknown key(s): ['extra']"),
    ({"fields": ["bogus"]}, "unsupported value(s): ['bogus']"),
    ({"fields": []}, "fields must not be empty"),
    ({"fields": ["type"], "retype_from": []}, "retype_from must not be empty"),
    ({"fields": ["title"], "retype_from": ["entity"]}, "retype_from requires `type`"),
])
def test_contract_validation_of_the_grant(grant, error):
    errors = output_contract.validate({**_CONTRACT, "identity_edit": grant})
    assert (errors == []) if error is None else any(error in e for e in errors), errors


def test_a_lane_that_creates_pages_never_holds_the_grant():
    errors = output_contract.validate({**_CONTRACT, "operations": ["create", "update"],
                                       "identity_edit": {"fields": ["remove"]}})
    assert any("not allowed on a lane that creates pages" in e for e in errors), errors


def test_a_pack_may_narrow_an_engine_grant_but_never_add_one():
    floor = {**_CONTRACT, "identity_edit": {"fields": ["type", "title"],
                                            "retype_from": ["entity", "org"]}}
    narrowed = output_contract.compose(floor, {**_CONTRACT, "identity_edit": {
        "fields": ["type"], "retype_from": ["org", "x"]}})
    assert narrowed["identity_edit"] == {"fields": ["type"], "retype_from": ["org"]}
    assert output_contract.compose(floor, dict(_CONTRACT))["identity_edit"] == floor["identity_edit"]
    restricted = output_contract.compose(
        {**_CONTRACT, "identity_edit": {"fields": ["type"]}},
        {**_CONTRACT, "identity_edit": {"fields": ["type"], "retype_from": ["entity"]}})
    assert restricted["identity_edit"] == {"fields": ["type"], "retype_from": ["entity"]}
    emptied = output_contract.compose(floor, {**_CONTRACT, "identity_edit": {
        "fields": ["type"], "retype_from": ["none-shared"]}})
    assert "identity_edit" not in emptied
    disjoint = output_contract.compose(floor, {**_CONTRACT, "identity_edit": {"fields": ["name"]}})
    assert "identity_edit" not in disjoint
    with pytest.raises(ValueError, match="identity_edit policy may not widen"):
        output_contract.compose(dict(_CONTRACT), {**_CONTRACT, "identity_edit": {"fields": ["type"]}})


def test_the_engine_grants_only_the_classifier_retype_and_the_citation_repairs():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    granted = {j["name"]: j["output_contract"]["identity_edit"] for j in jobs
               if "identity_edit" in (j.get("output_contract") or {})}
    assert granted == {"schema-classify-drain": {"fields": ["type"],
                                                 "retype_from": ["entity", guard.CATCHALL]},
                       # the deterministic citation-repair lanes re-seat or repoint citations
                       "entity-link": {"fields": ["citations"]},
                       "dedup-same-url": {"fields": ["citations"]}}


def test_a_narrowed_grant_without_type_drops_its_retype_sources():
    floor = {**_CONTRACT, "identity_edit": {"fields": ["type", "title"],
                                            "retype_from": ["entity"]}}
    composed = output_contract.compose(floor, {**_CONTRACT, "identity_edit": {"fields": ["title"]}})
    assert composed["identity_edit"] == {"fields": ["title"]}


def test_an_untyped_legacy_page_is_still_guarded(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    after = {"title": "Renamed"}
    flags, hold_body, held = m._identity_hold(m._safe("entities/legacy.md"), {"title": "Legacy"},
                                              after)
    assert after == {"title": "Legacy"} and held == {"title"} and hold_body and len(flags) == 1


def test_an_unreadable_type_vocabulary_judges_the_stored_spelling(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)

    def broken(*_args, **_kwargs):
        raise ValueError("unreadable")

    monkeypatch.setattr(m.schema_lib, "canonical_type", broken)
    view = m._identity_view(page, {"type": "Actor "})
    assert view["type"] == "Actor "


# --- what the facade hold exempts and how it reads the stored page --------------------------


def test_engine_review_and_stamp_keys_are_never_identity(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    before = {"type": "actor", "maintained_by": ["p"], "needs_review": True,
              "last_updated": "2026-01-01", "review_state": "open", "version": 3}
    after = {"type": "actor"}
    assert m._identity_hold(m._safe("entities/x.md"), before, after) == ([], False, set())


def test_a_legacy_alias_key_the_write_retires_is_not_a_removal(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    monkeypatch.setattr(m, "_field_aliases", lambda _p: {"hx": "tlp"})
    before = {"type": "actor", "hx": "amber", "tlp": "red"}
    assert m._identity_hold(m._safe("entities/x.md"), before, {"type": "actor", "tlp": "red"})[0] == []


def test_a_type_spelled_by_its_alias_is_not_a_retype(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    monkeypatch.setattr(m.schema_lib, "canonical_type",
                        lambda _schema, t: {"threat-actor": "actor"}.get(t, t))
    assert m._identity_hold(m._safe("entities/x.md"), {"type": "threat-actor"},
                            {"type": "actor"})[0] == []


def _body_flags(vault):
    log = vault / "wiki" / "log.md"
    return log.read_text().count("body replacement in the same write is held") if log.exists() else 0


@pytest.mark.parametrize("body", ["Curated body.", "A smaller body.", "Z larger body."])
def test_update_holds_only_a_changed_body(tmp_path, monkeypatch, body):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    m._update("entities/hafnium.md", {"name": "Other"}, body)
    assert "Curated body." in page.read_text()
    assert _body_flags(vault) == (0 if body == "Curated body." else 1)


@pytest.mark.parametrize("body", ["Curated body.", "A smaller body.", "Z larger body.", ""])
def test_converge_holds_only_a_changed_body(tmp_path, monkeypatch, body):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    m._converge("entities/hafnium.md", {**_HAFNIUM, "title": "Other"}, body)
    assert "Curated body." in page.read_text()
    assert _body_flags(vault) == (0 if body in ("Curated body.", "") else 1)


def test_converge_without_an_identity_change_never_flags_its_body(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    m._converge("entities/hafnium.md", dict(_HAFNIUM), "A new body entirely, long enough.")
    assert _body_flags(vault) == 0 and "A new body entirely" in page.read_text()


@pytest.mark.parametrize("replacement", ["Curated body.", "A smaller body.", "Z larger body."])
def test_patch_holds_only_a_changed_body(tmp_path, monkeypatch, replacement):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    text = page.read_text()
    span = text[text.index("description:"):]
    m._patch("entities/hafnium.md", span, span.replace("description:", "title: Other\n"
             "description:", 1).replace("Curated body.", replacement))
    assert "Curated body." in page.read_text()
    assert _body_flags(vault) == (0 if replacement == "Curated body." else 1)


def test_composition_without_retype_sources_on_either_side():
    floor = {**_CONTRACT, "identity_edit": {"fields": ["type", "title"]}}
    composed = output_contract.compose(floor, {**_CONTRACT, "identity_edit": {"fields": ["type"]}})
    assert composed["identity_edit"] == {"fields": ["type"]}
    emptied = output_contract.compose(
        {**_CONTRACT, "identity_edit": {"fields": ["type", "title"], "retype_from": ["a"]}},
        {**_CONTRACT, "identity_edit": {"fields": ["type", "title"], "retype_from": ["b"]}})
    assert emptied["identity_edit"] == {"fields": ["title"]}


# --- citations are never dropped by a lane --------------------------------------------------

_CITED = {**_HAFNIUM, "sources": ["sources/2026/01/a", "[[sources/2026/01/b]]"]}


def _sources(vault: Path, *names: str) -> None:
    """Canonical source pages, so the fabricated-source guard admits the citations."""
    for name in names:
        page = vault / "wiki" / "sources" / "2026" / f"{name}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntype: source\ntitle: S\n---\nbody\n", encoding="utf-8")


@RESOLVERS
def test_a_converge_with_fewer_sources_keeps_every_citation_and_adds_new_ones(tmp_path,
                                                                             monkeypatch, resolver):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", resolver)
    _sources(vault, "01/a", "01/b", "02/c")
    page = _seed(m, "entities/hafnium.md", _CITED)
    out = m._converge("entities/hafnium.md", {**_HAFNIUM, "sources": [
        "sources/2026/01/b.md", "sources/2026/02/c"]})
    assert out.startswith("converged"), out
    assert _fm(page)["sources"] == ["sources/2026/01/a", "[[sources/2026/01/b]]",
                                    "sources/2026/02/c"]


def test_a_non_owner_converge_may_add_a_citation_but_never_drop_one(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-competitive")   # actor pages are not its own
    monkeypatch.setattr(m, "_governing", lambda _p: {"types": {"actor": {"owner": "other-pack"}}})
    _sources(vault, "01/a", "01/b", "02/c")
    page = _seed(m, "entities/hafnium.md", _CITED)
    m._converge("entities/hafnium.md", {"type": "actor", "id": "entities:hafnium",
                                        "sources": ["sources/2026/02/c"]})
    assert _fm(page)["sources"] == [*_CITED["sources"], "sources/2026/02/c"]


def test_removing_or_shortening_sources_is_held_for_review(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    _sources(vault, "01/a", "01/b", "03/d")
    page = _seed(m, "entities/hafnium.md", _CITED)
    m._converge("entities/hafnium.md", {"type": "actor", "id": "entities:hafnium"},
                remove="sources")
    # Held: every citation stays, restored in its canonical spelling.
    assert _fm(page)["sources"] == ["sources/2026/01/a", "sources/2026/01/b"]
    m._update("entities/hafnium.md", {"sources": ["sources/2026/01/a", "sources/2026/03/d"]})
    assert _fm(page)["sources"] == ["sources/2026/01/a", "sources/2026/03/d",
                                    "sources/2026/01/b"]   # restored in its canonical spelling
    assert "field `sources`: removal of ['sources/2026/01/b']" in _logged(vault)


def test_aliases_are_judged_as_the_identity_index_reads_them():
    """A comma-joined LIST entry is one alias the index cannot route, so joining two aliases
    into it drops both (QA r2 B2). A scalar string is split, as the index splits it."""
    joined = guard.identity_changes({"aliases": ["A", "B"]}, {"aliases": ["A, B", "C"]})
    assert [(c.kind, c.before) for c in joined] == [("alias-removal", ["A", "B"])]
    assert guard.identity_changes({"aliases": "A, B"}, {"aliases": ["B", "A"]}) == []
    assert guard.identity_changes({"aliases": ["A"]}, {"aliases": 7}) == [
        guard.Change("aliases", "alias-removal", ["A"])]


def test_a_citation_respelled_is_not_a_removal():
    before = {"sources": ["[[sources/a|A]]", "sources/b.md", "sources/c#x"], "basis": ["concepts/z"]}
    after = {"sources": ["sources/a", "sources/b", "sources/c"], "basis": ["[[concepts/z]]"]}
    assert guard.identity_changes(before, after) == []
    dropped = guard.identity_changes({"basis": ["concepts/z"]}, {})
    assert [(c.field, c.kind, c.grant) for c in dropped] == [
        ("basis", "citation-removal", "citations")]
    after = {"basis": "concepts/y"}
    guard.hold(after, {"basis": ["concepts/z"]}, dropped)
    assert after == {"basis": ["concepts/y", "concepts/z"]}


def test_converge_and_the_guard_name_the_same_citation_fields():
    import converge
    assert converge.CITATION_KEYS == set(guard.CITATIONS)


# --- a dangling declared relationship is repaired, never held ---------------------------------


def _dangling(m, monkeypatch, contract, before):
    monkeypatch.setattr(m._output_contract, "resolve", lambda _caller: (contract, "lane"))
    return m._dangling_reference_removal(guard.Change("prediction_candidate", "removal", before),
                                         m._safe("lacuna/gap.md"))


def test_only_a_declared_relationship_whose_every_target_is_absent_is_dangling(tmp_path,
                                                                             monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    real = vault / "wiki" / "predictions" / "real.md"
    real.parent.mkdir(parents=True)
    real.write_text("---\ntype: prediction\n---\nbody\n")
    declared = {"optional_relationships": ["prediction_candidate"]}
    other = vault / "wiki" / "concepts" / "aaa.md"      # a stem that sorts before every probe
    other.parent.mkdir(parents=True)
    other.write_text("---\ntype: concept\n---\nbody\n")
    assert _dangling(m, monkeypatch, declared, "predictions/missing")
    assert _dangling(m, monkeypatch, declared, "predictions/2026/10/deep-missing")
    assert _dangling(m, monkeypatch, declared, "predictions/aaa/missing")   # only the stem counts
    assert _dangling(m, monkeypatch, {"required_relationships": ["prediction_candidate"]},
                     ["predictions/missing", "predictions/gone"])
    assert not _dangling(m, monkeypatch, declared, ["predictions/missing", "predictions/real"])
    assert not _dangling(m, monkeypatch, declared, "predictions/real")
    assert not _dangling(m, monkeypatch, {"optional_relationships": ["other"]},
                         "predictions/missing")
    assert not _dangling(m, monkeypatch, None, "predictions/missing")   # chat: no lane contract
    monkeypatch.setattr(m._output_contract, "resolve", lambda _caller: (declared, "lane"))
    assert not m._dangling_reference_removal(guard.Change("aliases", "alias-removal", ["X"]),
                                             m._safe("lacuna/gap.md"))
    # The page under write may share the stem; any OTHER page with it is an existing target.
    own = vault / "wiki" / "lacuna" / "missing.md"
    own.parent.mkdir(parents=True)
    own.write_text("---\ntype: lacuna\n---\nbody\n")
    change = guard.Change("prediction_candidate", "removal", "predictions/missing")
    assert m._dangling_reference_removal(change, own)
    assert not m._dangling_reference_removal(change, m._safe("lacuna/gap.md"))
    for not_a_path in ("CN", "predictions:p", "[[p]]", "predictions/", "/x"):
        assert not _dangling(m, monkeypatch, declared, not_a_path), not_a_path


def test_only_a_fully_bracketed_citation_is_unwrapped_by_the_guard():
    assert guard._citation_key("[[sources/x|X]]") == "sources/x"
    assert guard._citation_key("[[sources/x") == "[[sources/x"
    assert guard._citation_key("sources/x]]") == "sources/x]]"


def test_a_lane_rename_of_an_actor_is_held_before_it_reaches_1014(tmp_path, monkeypatch):
    """With !1297 merged: a lane's actor rename is held by #1028 first (stored name kept, queued for
    review), so it is never applied whatever #1014 would judge. The operator, who holds every
    identity grant, still meets #1014's rename judgment: tests/test_actor_rename_1014.py."""
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    out = m._converge("entities/hafnium.md", {**_HAFNIUM, "name": "Day Crew", "title": "Day Crew"})
    assert out.startswith("converged") and _fm(page)["name"] == "HAFNIUM", out
    assert "field `name`: 'HAFNIUM' -> 'Day Crew'" in _logged(vault)


def test_a_non_creating_extension_applies_only_its_declared_grant(tmp_path, monkeypatch):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors", actor=None)
    page = _seed(m, "entities/hafnium.md", _HAFNIUM)
    capability = {"operations": ["update"], "paths": ["entities/**"], "update_fields": ["*"],
                  "body": "allow", "identity_edit": {"fields": ["title"]}}
    token = m._caller_var.set({"kind": "extension", "ext_id": "acme.curate",
                               "actor": "extension:acme.curate", "write_scopes": ["entities/"],
                               "write_capability": capability})
    try:
        m._update("entities/hafnium.md", {"title": "Silk Typhoon", "type": "identity"})
    finally:
        m._caller_var.reset(token)
    fm = _fm(page)
    assert fm["title"] == "Silk Typhoon" and fm["type"] == "actor"



# --- v0.15.11 audit #5: title and name are one identity -----------------------------------------


@pytest.mark.parametrize(("stored", "added"), [({"title": "XLoader"}, {"name": "Formbook"}),
                                               ({"name": "XLoader"}, {"title": "Formbook"})])
@pytest.mark.parametrize("path", ["update", "converge"])
def test_adding_the_other_display_key_with_another_name_is_a_held_rename(tmp_path, monkeypatch,
                                                                           stored, added, path):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/xloader.md", {"type": "malware", "id": "entities:xloader", **stored})
    if path == "update":
        out = m._update("entities/xloader.md", added)
    else:
        out = m._converge("entities/xloader.md",
                          {"type": "malware", "id": "entities:xloader", **stored, **added})
    fm = _fm(page)
    assert list(added)[0] not in fm and fm["needs_review"] is True, (out, fm)
    assert "'XLoader' -> 'Formbook'" in _logged(vault)


@pytest.mark.parametrize(("stored", "added"), [({"title": "XLoader"}, {"name": "xloader"}),
                                               ({"name": "X Loader"}, {"title": "x-loader"})])
def test_adding_the_other_display_key_with_the_same_name_applies(tmp_path, monkeypatch, stored,
                                                                 added):
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/xloader.md", {"type": "malware", "id": "entities:xloader", **stored})
    out = m._update("entities/xloader.md", added)
    assert _fm(page)[list(added)[0]] == list(added.values())[0], out
    assert "review" not in out


@pytest.mark.parametrize(("added", "held"), [({"title": "Xloader"}, False),
                                              ({"name": "xloader"}, False),
                                              ({"title": "Formbook"}, True)])
def test_a_nameless_page_may_gain_only_its_own_name(tmp_path, monkeypatch, added, held):
    """A page with neither key is called by its stem (as the index keys it): gaining that name
    is the slug-titling #1014 accepts; gaining another is a rename."""
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    page = _seed(m, "entities/xloader.md", {"type": "malware", "id": "entities:xloader"})
    m._update("entities/xloader.md", added)
    field, value = next(iter(added.items()))
    assert (_fm(page).get(field) == value) is not held, _fm(page)


def test_the_pure_rule_without_a_stem_never_holds_a_nameless_page():
    assert guard.identity_changes({"type": "x"}, {"type": "x", "title": "Anything"}) == []



def test_only_the_alias_spellings_of_a_held_field_are_dropped(tmp_path, monkeypatch):
    """!1305 QA N1: a key alias renamed onto a held field later is held with it; other aliases
    are left for the write path's own renaming."""
    m, vault = _load(tmp_path, monkeypatch, "okpack-threat-actors")
    monkeypatch.setattr(m, "_field_aliases", lambda _p: {"display_name": "name", "hx": "tlp"})
    after = {"type": "actor", "name": "Other", "display_name": "Other", "hx": "amber"}
    flags, _body, held = m._identity_hold(m._safe("entities/x.md"),
                                          {"type": "actor", "name": "HAFNIUM"}, after)
    assert held == {"name"} and after == {"type": "actor", "name": "HAFNIUM", "hx": "amber"}
