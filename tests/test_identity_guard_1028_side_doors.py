"""okengine#1028 QA round 2, kept as regressions: every side door around the identity guard.

Each test asserts the SAFE outcome. B1 (a curated field emptied to whitespace or a blank list) and
B2 (a comma-joined alias entry the identity index cannot route) were red at 5c5cd613, as was N1 (a
page-creating networked extension granting itself identity edits)."""
from __future__ import annotations
import os, sys, json, hashlib
from pathlib import Path
import pytest, yaml

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "tests"), str(REPO / "scripts/cron")]
import test_identity_guard_1028 as T  # noqa: E402

LANE = "cron:page-quality-enrich"
BASE = {"type": "product", "name": "Dota 2", "title": "Dota 2", "id": "entities:dota-2",
        "aliases": ["Dota 2", "dota2", "DotA"], "description": "A MOBA.", "country": "US",
        "sources": ["sources/a", "[[sources/b|B]]"], "basis": ["sources/c"]}


def _env(tmp_path, monkeypatch, actor=LANE, grant=None):
    m, vault = T._load(tmp_path, monkeypatch, "okpack-competitive", "main", actor=actor, grant=grant)
    page = T._seed(m, "entities/dota-2.md", dict(BASE))
    return m, vault, page


def _same_identity(page):
    fm = T._fm(page)
    return {k: fm.get(k) for k in ("type", "name", "title")} == {"type": "product", "name": "Dota 2", "title": "Dota 2"}


def _cites(fm):
    return {T.guard._citation_key(x) for f in ("sources", "basis") for x in (fm.get(f) or [] if isinstance(fm.get(f), list) else [fm.get(f)])}


# --- scalar identity through every write path -------------------------------------------------
@pytest.mark.parametrize("change", [{"type": "Segment"}, {"type": "SEGMENT"}, {"type": " segment "},
                                    {"name": "dota 2"}, {"title": "Dota  2"}, {"name": None},
                                    {"title": ""}, {"name": ["Dota 2"]}])
@pytest.mark.parametrize("path", ["update", "converge"])
def test_scalar_identity_held(tmp_path, monkeypatch, change, path):
    m, vault, page = _env(tmp_path, monkeypatch)
    fm = {**BASE, **change}
    out = m._update("entities/dota-2.md", fm, None) if path == "update" else m._converge("entities/dota-2.md", fm)
    after = T._fm(page)
    assert {k: after.get(k) for k in change} == {k: BASE[k] for k in change}, (out, after)


def test_patch_title_line_held(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch)
    out = m._patch("entities/dota-2.md", "title: Dota 2", "title: DOTA Two")
    assert T._fm(page)["title"] == "Dota 2", out


def test_patch_type_and_body_together_held(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch)
    text = page.read_text()
    new = text.replace("type: product", "type: segment").replace("Curated body.", "Replaced body.")
    out = m._patch("entities/dota-2.md", text, new)
    assert T._fm(page)["type"] == "product" and "Curated body." in page.read_text(), out


def test_update_body_with_held_rename_keeps_body(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch)
    out = m._update("entities/dota-2.md", {**BASE, "title": "Other"}, "Totally new body.")
    assert "Curated body." in page.read_text() and T._fm(page)["title"] == "Dota 2", out


# --- emptying vs removal ----------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", [], {}, " ", "  \n", [""], [None], ["  "]])
@pytest.mark.parametrize("path", ["update", "converge", "patch"])
def test_curated_field_emptying_held(tmp_path, monkeypatch, value, path):
    m, vault, page = _env(tmp_path, monkeypatch)
    if path == "patch":
        out = m._patch("entities/dota-2.md", "description: A MOBA.", "description: " + json.dumps(value))
    elif path == "update":
        out = m._update("entities/dota-2.md", {**BASE, "description": value}, None)
    else:
        out = m._converge("entities/dota-2.md", {**BASE, "description": value})
    d = T._fm(page).get("description")
    assert isinstance(d, str) and d.strip() == "A MOBA.", (out, d)


@pytest.mark.parametrize("path", ["update-remove-key", "converge-remove", "patch-delete-line"])
def test_curated_field_removal_held(tmp_path, monkeypatch, path):
    m, vault, page = _env(tmp_path, monkeypatch)
    if path == "update-remove-key":
        fm = {k: v for k, v in BASE.items() if k != "country"}
        out = m._update("entities/dota-2.md", fm, None)   # update merges keys; omission is not removal
    elif path == "converge-remove":
        out = m._converge("entities/dota-2.md", dict(BASE), remove="country")
    else:
        out = m._patch("entities/dota-2.md", "country: US\n", "")
    assert T._fm(page).get("country") == "US", out


# --- aliases ----------------------------------------------------------------------------------
@pytest.mark.parametrize("aliases", [["DotA", "dota2"], ["Dota 2, dota2"], "Dota 2", [], None,
                                     ["Dota 2", "dota2", "DotA X"]])
@pytest.mark.parametrize("path", ["update", "converge"])
def test_alias_drop_held(tmp_path, monkeypatch, aliases, path):
    m, vault, page = _env(tmp_path, monkeypatch)
    fm = {**BASE, "aliases": aliases}
    out = m._update("entities/dota-2.md", fm, None) if path == "update" else m._converge("entities/dota-2.md", fm)
    stored = {str(a).casefold() for a in (T._fm(page).get("aliases") or [])}
    assert {"dota 2", "dota2", "dota"} <= stored, (out, T._fm(page).get("aliases"))


def test_alias_comma_join_does_not_lose_entries(tmp_path, monkeypatch):
    """`kept` splits a comma-joined after-entry; what is STORED must still hold each alias."""
    m, vault, page = _env(tmp_path, monkeypatch)
    out = m._update("entities/dota-2.md", {**BASE, "aliases": ["Dota 2, dota2, DotA"]}, None)
    stored = [str(a) for a in (T._fm(page).get("aliases") or [])]
    print("STORED", stored)
    assert {"dota 2", "dota2", "dota"} <= {s.strip().casefold() for s in stored}, (out, stored)


# --- citations --------------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("sources", ["sources/a"]), ("sources", "sources/a"), ("sources", ["[[sources/a]]"]),
    ("sources", ["sources/a, sources/b"]), ("sources", []), ("sources", None),
    ("sources", ["sources/a.md", "sources/x"]), ("basis", []), ("basis", "sources/z"),
    ("basis", None)])
@pytest.mark.parametrize("path", ["update", "converge", "patch"])
def test_citation_drop_held(tmp_path, monkeypatch, field, value, path):
    m, vault, page = _env(tmp_path, monkeypatch)
    if path == "patch":
        text = page.read_text(); fm = yaml.safe_load(text.split("---")[1])
        fm[field] = value
        new = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + text.split("---", 2)[2].lstrip("\n")
        out = m._patch("entities/dota-2.md", text, new)
    elif path == "update":
        out = m._update("entities/dota-2.md", {**BASE, field: value}, None)
    else:
        out = m._converge("entities/dota-2.md", {**BASE, field: value})
    assert {"sources/a", "sources/b", "sources/c"} <= _cites(T._fm(page)), (out, T._fm(page).get(field))


def test_converge_remove_sources_held(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch)
    out = m._converge("entities/dota-2.md", dict(BASE), remove="sources,basis")
    assert {"sources/a", "sources/b", "sources/c"} <= _cites(T._fm(page)), out


def test_converge_onto_legacy_scalar_sources_held(tmp_path, monkeypatch):
    m, vault, _ = _env(tmp_path, monkeypatch)
    page = T._seed(m, "entities/lgd.md", {"type": "competitor", "name": "LGD", "id": "entities:lgd",
                                         "sources": "sources/legacy"})
    out = m._converge("entities/lgd.md", {"type": "competitor", "name": "LGD", "id": "entities:lgd",
                                          "sources": ["sources/new"]})
    assert "sources/legacy" in _cites(T._fm(page)), (out, T._fm(page).get("sources"))


# --- redirects / create-onto-existing ---------------------------------------------------------
def test_create_onto_existing_identity_does_not_rename(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch)
    out = m._create("entities/dota-2.md", yaml.safe_dump({**BASE, "title": "X", "type": "segment"}), "B")
    assert _same_identity(page), out


def test_converge_redirect_by_id_from_other_path_held(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch)
    out = m._converge("entities/dota-two.md", {**BASE, "title": "Dota Two", "type": "segment"}, "New body")
    assert _same_identity(page) and "Curated body." in page.read_text(), out


# --- grants -----------------------------------------------------------------------------------
def test_malformed_grant_grants_nothing(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch, grant={"fields": "type"})
    m._update("entities/dota-2.md", {**BASE, "type": "segment"}, None)
    assert T._fm(page)["type"] == "product"


def test_retype_from_bounds_classifier(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch, actor="cron:schema-classify-drain",
                          grant={"fields": ["type"], "retype_from": ["entity", "@classify_catchall"]})
    m._update("entities/dota-2.md", {**BASE, "type": "segment"}, None)
    assert T._fm(page)["type"] == "product"


def test_citations_grant_does_not_cover_rename(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch, actor="cron:entity-link", grant={"fields": ["citations"]})
    m._update("entities/dota-2.md", {**BASE, "title": "X", "country": None}, None)
    assert T._fm(page)["title"] == "Dota 2" and T._fm(page).get("country") == "US"


# --- networked extension write_capability -----------------------------------------------------
def test_create_capable_extension_cannot_self_grant_identity_edit(tmp_path, monkeypatch):
    """`capabilities.write_policy` comes straight from the extension manifest (pack/operator tier
    too) into the token; nothing applies output_contract's 'a creating lane never holds a grant'."""
    m, vault, page = _env(tmp_path, monkeypatch, actor=None)
    cap = {"operations": ["create", "update", "converge"], "paths": ["entities/**"],
           "update_fields": ["*"], "identity_edit": {"fields": ["type", "title", "name", "aliases", "citations", "remove"]}}
    tok = m._caller_var.set({"kind": "extension", "ext_id": "acme.x", "actor": "extension:acme.x",
                             "write_scopes": ["entities/"], "write_capability": cap})
    try:
        out = m._update("entities/dota-2.md", {**BASE, "type": "segment", "title": "Hijack", "aliases": [], "sources": []}, None)
    finally:
        m._caller_var.reset(tok)
    fm = T._fm(page)
    assert fm["type"] == "product" and fm["title"] == "Dota 2", out


@pytest.mark.parametrize("joined", [["Dota 2, dota2, DotA"], ["Dota 2, dota2"], ["dota2, DotA"]])
@pytest.mark.parametrize("path", ["update", "converge"])
def test_comma_joined_alias_is_unindexed(tmp_path, monkeypatch, joined, path):
    """After the comma-join write, does the identity index still route each old alias here?"""
    m, vault, page = _env(tmp_path, monkeypatch)
    fm = {**BASE, "aliases": joined}
    m._update("entities/dota-2.md", fm, None) if path == "update" else m._converge("entities/dota-2.md", fm)
    import id_index, id_lib
    idx = id_index.IdIndex(); idx._add_identity("entities/d/dota-2.md", T._fm(page))
    missing = [a for a in ("dota2", "DotA") if id_lib.normalize_key(a) not in idx.alias_to_rels]
    assert not missing, (T._fm(page).get("aliases"), missing)
