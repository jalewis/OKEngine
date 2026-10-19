"""The write path guards every DECLARED actor type, not the literal `actor` (okengine#1020).

A live pack keeps its adversaries as `threat-actor` and `intrusion-set`. Every guard keyed on
`type == "actor"`, so entity-backfill kept minting malware, campaigns and countries under those
types after the okengine#1012 grounding shipped, and the guard admitted them. The pack now declares
`role: actor` on those types; these tests pin that every guard reads the declaration, and that the
declaration does not reach back into pages stored before it.
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

_SCHEMA = (
    "okf:\n  required: [type]\nstrict_types: false\ntypes:\n"
    "  actor: {required: [type]}\n"
    "  intrusion-set: {required: [type], role: actor}\n"
    "  threat-actor: {required: [type], role: actor}\n"
    "  identity: {required: [type]}\n  malware: {required: [type]}\n"
    "  source: {required: [type]}\n"
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 10, 9).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _source(root: Path, ref: str, title: str, body: str) -> str:
    page = root / "wiki" / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "source", "title": title, "source_kind": "report"}
    page.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n# {title}\n\n{body}\n",
                    encoding="utf-8")
    return ref


def _stored(root: Path, rel: str, fm: dict, body: str = "Profile.") -> Path:
    """A page written before the declaration existed: straight to disk, no write path."""
    page = root / "wiki" / f"{rel}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": f"stored:{page.stem}", **fm}
    page.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n{body}\n", encoding="utf-8")
    return page


def _wazza(root: Path) -> str:
    return _source(root, "sources/2026/10/08/wazza", "Wazza phishkit",
                   "ANY.RUN has identified Wazza, a new phishkit targeting banks.")


def _crew(root: Path) -> str:
    return _source(root, "sources/2026/09/01/crew", "Crew report",
                   "Night Crew claimed the intrusion at a regional bank.")


# --- create and retype are judged on every declared type -------------------------------------------

@pytest.mark.parametrize("typ", ["intrusion-set", "threat-actor", "Intrusion-Set"])
def test_creating_a_declared_actor_type_is_grounded(ws, typ):
    """The sandbox vendor that REPORTED a phishkit, minted under a declared actor type. Red before
    okengine#1020: only `actor` was judged, so this was `created`."""
    m, root = ws
    out = m._create("entities/a/anyrun", {"type": typ, "title": "ANY.RUN",
                                          "sources": [_wazza(root)]}, "")
    assert out.startswith("rejected: creating an actor"), out
    assert not list((root / "wiki").rglob("anyrun.md"))


def test_converging_a_new_declared_actor_type_is_grounded(ws):
    m, root = ws
    fm = {"type": "intrusion-set", "title": "ANY.RUN", "sources": [_wazza(root)]}
    out = m._converge("entities/a/anyrun", yaml.safe_dump(fm), "")
    assert out.startswith("rejected: creating an actor"), out


def test_a_declared_actor_type_the_sources_affirm_is_created(ws):
    m, root = ws
    out = m._create("entities/n/night-crew", {"type": "intrusion-set", "title": "Night Crew",
                                              "sources": [_crew(root)]}, "")
    assert out.startswith("created"), out


@pytest.mark.parametrize("operation", ["update", "patch", "converge"])
def test_retyping_into_a_declared_actor_type_is_grounded(ws, operation):
    m, root = ws
    page = _stored(root, "entities/a/anyrun",
                   {"type": "identity", "title": "ANY.RUN", "sources": [_wazza(root)]})
    before = page.read_bytes()
    if operation == "update":
        out = m._update("entities/a/anyrun", {"type": "threat-actor"})
    elif operation == "patch":
        out = m._patch("entities/a/anyrun", "type: identity", "type: threat-actor")
    else:
        out = m._converge("entities/a/anyrun", "type: threat-actor\ntitle: ANY.RUN\n", "")
    assert out.startswith("rejected: retyping this 'identity' page to actor type"), out
    assert page.read_bytes() == before


def test_admission_refuses_a_class_label_created_as_a_declared_actor_type(ws):
    m, root = ws
    out = m._create("entities/t/threat-actor", {"type": "threat-actor", "title": "Threat Actor",
                                                "sources": [_crew(root)]}, "")
    assert out.startswith("rejected: generic class labels"), out


# --- nothing reaches back into stored pages ---------------------------------------------------------

@pytest.mark.parametrize("fm", [
    {"type": "threat-actor", "title": "ANY.RUN"},                 # ungrounded
    {"type": "intrusion-set", "title": "Threat Actor"},           # a class label
    {"type": "intrusion-set"},                                    # nameless, slug only
])
def test_a_stored_declared_actor_page_keeps_its_update_and_append_path(ws, fm):
    """The declaration must not start refusing enrichment of the pages already stored under it
    (1,882 on the live vault, 998 of them with no title or name)."""
    m, root = ws
    _stored(root, "entities/s/stored", {**fm, "sources": [_wazza(root)]})
    assert m._update("entities/s/stored", {"tags": ["enriched"]}).startswith("updated")
    out = m._append_section("entities/s/stored", "Notes", "- one more observation")
    assert out.startswith(("appended", "updated")), out


def test_the_engine_actor_type_keeps_its_per_write_admission(ws):
    """okengine#846 is unchanged: a stored `actor` page with a class label is still refused."""
    m, root = ws
    _stored(root, "entities/t/threat-actor", {"type": "actor", "title": "Threat Actor"})
    out = m._update("entities/t/threat-actor", {"tags": ["x"]})
    assert out.startswith("rejected: generic class labels"), out


# --- names -------------------------------------------------------------------------------------------

def test_a_nameless_retype_is_looked_for_by_its_slug(ws):
    """A page with no title, name or alias is still called something: its slug. Red before
    okengine#1020: the guard refused it as `name_absent_from_sources`."""
    m, root = ws
    page = root / "wiki" / "entities" / "n" / "night-crew.md"
    fm = {"type": "actor", "sources": [_crew(root)]}
    assert m._actor_grounding_reject(page, fm, {"type": "identity"}) is None
    assert m._actor_grounding_reject(page.with_name("day-crew.md"), fm, {"type": "identity"})


def test_an_aliases_only_page_is_named_by_its_aliases(ws):
    m, root = ws
    out = m._create("entities/n/nc", {"type": "intrusion-set", "aliases": ["Night Crew"],
                                      "sources": [_crew(root)]}, "")
    assert out.startswith("created"), out


def test_a_create_with_no_name_at_all_is_still_refused(ws):
    m, root = ws
    out = m._create("entities/n/night-crew", {"type": "intrusion-set", "sources": [_crew(root)]}, "")
    assert out.startswith("rejected: an actor page must name the actor"), out


# --- the entity-backfill sanitizer ---------------------------------------------------------------------

def test_backfill_requires_actor_type_only_on_the_engine_actor_type(ws):
    """`actor_type` is the engine `actor` type's field; a declared type has its own vocabulary."""
    m, root = ws
    ref = _crew(root)
    _, err = m._entity_backfill_frontmatter(
        "entities/n/night-crew", f"type: intrusion-set\ntitle: Night Crew\nsources: [{ref}]\n",
        "Night Crew is a ransomware group that claimed the intrusion.")
    assert err is None, err
    _, err = m._entity_backfill_frontmatter(
        "entities/n/night-crew", f"type: actor\ntitle: Night Crew\nsources: [{ref}]\n",
        "Night Crew is a ransomware group that claimed the intrusion.")
    assert err and "requires explicit actor_type" in err, err


def test_backfill_applies_admission_and_identity_to_a_declared_type(ws):
    """Red before okengine#1020: the sanitizer only looked at `type: actor`."""
    m, root = ws
    _, err = m._entity_backfill_frontmatter(
        "entities/t/threat-actor", "type: threat-actor\ntitle: Threat Actor\n", "A threat actor.")
    assert err and err.startswith("rejected: generic class labels"), err
    _, err = m._entity_backfill_frontmatter(
        "entities/r/redline", "type: threat-actor\ntitle: RedLine\n", "RedLine steals passwords.")
    assert err and "source-grounded evidence" in err, err


def test_admission_reads_the_title_before_the_name(ws):
    """The class-label check reads what the page is called first: its title."""
    m, root = ws
    out = m._create("entities/t/threat-actor", {"type": "intrusion-set", "title": "Threat Actor",
                                                "name": "Night Crew", "sources": [_crew(root)]}, "")
    assert out.startswith("rejected: generic class labels"), out
