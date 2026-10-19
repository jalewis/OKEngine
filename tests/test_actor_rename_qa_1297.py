"""okengine#1014, QA of !1297: renames a lane must be allowed, and renames that must be refused.

Each `legit_*` case is a write a lane must be allowed (a vendor's alias its own cited source glosses,
a respelling); each `hostile_*` must be refused (a rename onto a name the deployment already holds
as a vendor or malware family, sourced or not). The Earth Preta wording is copied from a live
vault's vendor source pages."""
import pytest

from tests.test_actor_rename_1014 import _source, _write, ws  # noqa: F401  (fixture)

# Real wording from a live vault's vendor source pages for `earth-preta` (copied, not paraphrased)
AKA_PAREN = ("Earth Preta (aka Mustang Panda, Bronze President) deployed DOPLUGS, a customized "
             "PlugX variant acting as a downloader, to target Asian governments.")
AKA_BARE = ("Earth Preta (Mustang Panda) ran large-scale cyberespionage campaign targeting "
            "government, academic, research sectors.")


def _page(root, fm_extra, body_src, typ="intrusion-set"):
    ref = _source(root, "sources/2026/10/01/tm", body_src)
    fm = {"id": "x:earth-preta", "type": typ, "sources": [ref]} | fm_extra
    return _write(root, "entities/e/earth-preta", fm)


@pytest.mark.parametrize("named", [False, True], ids=["nameless", "titled"])
@pytest.mark.parametrize("src", [AKA_PAREN, AKA_BARE], ids=["aka_paren", "paren"])
def test_legit_vendor_alias_from_own_sources(ws, named, src):
    m, root = ws
    _page(root, {"title": "Earth Preta"} if named else {}, src)
    out = m._update("entities/e/earth-preta", {"aliases": ["Mustang Panda"]})
    assert out.startswith("updated"), out


@pytest.mark.parametrize("named", [False, True], ids=["nameless", "titled"])
def test_legit_vendor_alias_converge(ws, named):
    m, root = ws
    _page(root, {"title": "Earth Preta"} if named else {}, AKA_PAREN)
    fm = {"type": "intrusion-set", "id": "x:earth-preta", "aliases": ["Mustang Panda"]}
    if named:
        fm["title"] = "Earth Preta"
    out = m._converge("entities/e/earth-preta", fm, "")
    assert out.startswith("converged"), out


@pytest.mark.parametrize("old,new", [("APT28", "APT 28"), ("Fancy Bear", "FancyBear"),
                                     ("Sandworm Team", "Sandworm-Team"), ("TA505", "ta505")])
def test_legit_respelling(ws, old, new):
    m, root = ws
    ref = _source(root, "sources/2026/10/01/r", f"{old} is a threat group that targeted banks.")
    _write(root, "entities/x/xgrp", {"id": "x:xgrp", "type": "intrusion-set", "title": old,
                                     "sources": [ref]})
    out = m._update("entities/x/xgrp", {"title": new})
    assert out.startswith("updated"), out


def test_hostile_rename_of_unsourced_actor_to_existing_vendor(ws):
    """23% of one live vault's actor pages (348/1481) have no resolvable source."""
    m, root = ws
    _write(root, "entities/i/acme-corp", {"id": "x:acme", "type": "identity", "title": "Acme Corp"})
    _write(root, "entities/n/night-crew", {"id": "x:nc", "type": "intrusion-set", "title": "Night Crew"})
    out = m._update("entities/n/night-crew", {"title": "Acme Corp", "name": "Acme Corp"})
    assert out.startswith("rejected"), out


@pytest.mark.parametrize("via", ["update", "patch", "converge"])
def test_hostile_rename_to_namesake(ws, via):
    m, root = ws
    _write(root, "entities/r/redline", {"id": "x:rl", "type": "malware", "title": "RedLine"})
    ref = _source(root, "sources/2026/10/01/nc", "Night Crew is a threat group. RedLine steals passwords.")
    p = _write(root, "entities/n/night-crew", {"id": "x:nc", "type": "intrusion-set",
                                                "title": "Night Crew", "sources": [ref]})
    if via == "update":
        out = m._update("entities/n/night-crew", {"title": "RedLine"})
    elif via == "patch":
        out = m._patch("entities/n/night-crew", "title: Night Crew", "title: RedLine")
    else:
        out = m._converge("entities/n/night-crew", {"type": "intrusion-set", "id": "x:nc",
                                                     "title": "RedLine"}, "")
    assert out.startswith("rejected"), (out, p.read_text())


@pytest.mark.parametrize("src,new", [
    ("Night Crew is a threat group. RedLine steals passwords.", "RedLine"),
    ("Night Crew is a threat group that targeted banks.", "RedLine"),
    ("Night Crew claimed the attack. CrowdStrike reported the intrusion.", "CrowdStrike"),
    ("Night Crew is a ransomware gang. Researchers at CrowdStrike tracked it.", "CrowdStrike"),
    ("Night Crew, a threat group, used Cobalt Strike to move laterally.", "Cobalt Strike"),
])
def test_hostile_rename_sourced_variants(ws, src, new):
    m, root = ws
    _write(root, "entities/r/redline", {"id": "x:rl", "type": "malware", "title": "RedLine"})
    _write(root, "entities/c/crowdstrike", {"id": "x:cs", "type": "identity", "title": "CrowdStrike"})
    _write(root, "entities/c/cobalt-strike", {"id": "x:cobalt", "type": "malware", "title": "Cobalt Strike"})
    ref = _source(root, "sources/2026/10/01/nc", src)
    _write(root, "entities/n/night-crew", {"id": "x:nc", "type": "intrusion-set",
                                            "title": "Night Crew", "sources": [ref]})
    out = m._update("entities/n/night-crew", {"title": new, "name": new})
    assert out.startswith("rejected"), out


# --- delta QA of !1297: a tie must bind to the glossed name, and never hide a namesake ----------

VENDOR_GLOSS = ("Detailed CrowdStrike analysis of LIMINAL PANDA (formerly LightBasin), a likely "
                "China-nexus threat actor.")


def _liminal(root):
    ref = _source(root, "sources/2026/10/01/cs", VENDOR_GLOSS)
    _write(root, "entities/c/crowdstrike", {"id": "x:cs", "type": "identity", "title": "CrowdStrike"})
    _write(root, "entities/p/panda-stealer", {"id": "x:ps", "type": "malware",
                                              "title": "Panda Stealer"})
    return _write(root, "entities/l/liminal-panda", {"id": "x:lp", "type": "intrusion-set",
                                                     "sources": [ref]})


@pytest.mark.parametrize("new", ["CrowdStrike", "Panda Stealer"])
@pytest.mark.parametrize("via", ["update", "converge"])
def test_hostile_rename_onto_the_reporting_vendor_or_a_namesake_is_refused(ws, new, via):
    """Red before: the vendor gloss "CrowdStrike analysis of LIMINAL PANDA (formerly LightBasin)"
    tied the reporting vendor to the actor (and "Panda" tied Panda Stealer), so the rename skipped
    judgment before the namesake check ran."""
    m, root = ws
    page = _liminal(root)
    before = page.read_bytes()
    if via == "update":
        out = m._update("entities/l/liminal-panda", {"title": new, "name": new})
    else:
        out = m._converge("entities/l/liminal-panda", {"type": "intrusion-set", "id": "x:lp",
                                                        "title": new}, "")
    assert out.startswith("rejected: renaming this actor to"), out
    assert page.read_bytes() == before


def test_the_glossed_alias_of_the_same_source_still_applies(ws):
    m, root = ws
    _liminal(root)
    assert m._update("entities/l/liminal-panda", {"aliases": ["LightBasin"]}).startswith("updated")


def test_a_shared_word_cannot_tie_away_a_held_namesake(ws):
    """Red on d70d281c: "Cobalt Group" -> "Cobalt Strike" was tied by the shared word and never
    judged. A name the deployment holds as a non-actor is judged before any tie."""
    m, root = ws
    _write(root, "entities/c/cobalt-strike", {"id": "x:cobs", "type": "malware",
                                              "title": "Cobalt Strike"})
    ref = _source(root, "sources/2026/10/01/cg", "Cobalt Group targeted banks across Europe.")
    _write(root, "entities/c/cobalt-group", {"id": "x:cg", "type": "intrusion-set",
                                             "title": "Cobalt Group", "sources": [ref]})
    out = m._update("entities/c/cobalt-group", {"title": "Cobalt Strike", "name": "Cobalt Strike",
                                                "aliases": ["Cobalt Group"]})
    assert out.startswith("rejected: renaming this actor to 'Cobalt Strike'"), out


@pytest.mark.parametrize("alias", ["Lyceum APT", "Lyceum Group"])
@pytest.mark.parametrize("via", ["update", "converge"])
def test_a_name_extension_of_a_nameless_page_with_aliases_applies(ws, alias, via):
    """Overblock QA of !1297 (red on 54c562c0): `lyceum` has no title but keeps HEXANE as an alias;
    "Lyceum APT" extends its slug and is the same actor."""
    m, root = ws
    ref = _source(root, "sources/2026/10/01/ly", "HEXANE targeted oil and gas companies.")
    _write(root, "entities/l/lyceum", {"id": "x:ly", "type": "intrusion-set",
                                       "aliases": ["HEXANE"], "sources": [ref]})
    if via == "update":
        out = m._update("entities/l/lyceum", {"aliases": ["HEXANE", alias]})
    else:
        out = m._converge("entities/l/lyceum", {"type": "intrusion-set", "id": "x:ly",
                                                 "aliases": ["HEXANE", alias]}, "")
    assert out.startswith(("updated", "converged")), out


def test_a_suffix_extension_of_a_titled_actor_applies(ws):
    m, root = ws
    ref = _source(root, "sources/2026/10/01/wk", "Wekby targeted health care providers.")
    _write(root, "entities/w/wekby", {"id": "x:wk", "type": "intrusion-set", "title": "Wekby",
                                      "sources": [ref]})
    assert m._update("entities/w/wekby", {"aliases": ["Wekby Group"]}).startswith("updated")


# okengine#1030: unheld organization and software names that share one distinctive word with a
# well-sourced actor. No page holds them, so the namesake check cannot catch them; before the fix
# the shared word tied them and the rename was never judged.
_SHARED_WORD_HOSTILE = [
    ("liminal-panda", "Liminal Panda", "Liminal Labs"),
    ("liminal-panda", "Liminal Panda", "Panda Bank"),
    ("liminal-panda", "Liminal Panda", "Liminal Security Inc"),
    ("earth-preta", "Earth Preta", "Preta Bank"),
    ("earth-preta", "Earth Preta", "Earth Bank"),
    ("cobalt-gypsy", "Cobalt Gypsy", "Gypsy Stealer"),
    ("boggy-serpens", "Boggy Serpens", "Serpens Labs"),
    ("gamaredon", "Gamaredon", "Gamaredon Labs"),
]


def _sourced_actor(root, slug, title, named=True):
    """A well-sourced actor page. Most live pages carry no title (they answer to their slug and
    aliases), which is where QA landed every hostile name."""
    ref = _source(root, f"sources/2026/10/01/{slug}",
                  f"{title} is a threat group that targeted telecommunications providers.")
    fm = {"id": f"x:{slug}", "type": "intrusion-set"} | ({"title": title} if named else {})
    return _write(root, f"entities/{slug[0]}/{slug}", fm | {"sources": [ref]})


@pytest.mark.parametrize("named", [False, True], ids=["nameless", "titled"])
@pytest.mark.parametrize("via", ["update", "converge", "patch", "alias"])
@pytest.mark.parametrize("slug,title,new", _SHARED_WORD_HOSTILE)
def test_a_shared_word_does_not_tie_an_unheld_org_or_software_name(ws, slug, title, new, via,
                                                                    named):
    m, root = ws
    page = _sourced_actor(root, slug, title, named)
    before = page.read_bytes()
    path = f"entities/{slug[0]}/{slug}"
    kept = {"title": title} if named else {}
    if via == "update":
        out = m._update(path, {"title": new, "name": new})
    elif via == "converge":
        out = m._converge(path, {"type": "intrusion-set", "id": f"x:{slug}", "title": new}, "")
    elif via == "patch":
        out = m._patch(path, "type: intrusion-set", f"type: intrusion-set\nname: {new}")
    else:
        out = m._update(path, kept | {"aliases": [new]})
    assert out.startswith("rejected: renaming this actor to"), out
    assert page.read_bytes() == before


@pytest.mark.parametrize("alias", ["Wekby Group", "Wekby APT", "Wekby Ransomware Gang", "Wekby 2"])
@pytest.mark.parametrize("via", ["update", "converge"])
def test_a_kind_word_or_numeral_extension_still_applies(ws, alias, via):
    m, root = ws
    page = _sourced_actor(root, "wekby", "Wekby")
    if via == "update":
        out = m._update("entities/w/wekby", {"aliases": [alias]})
    else:
        out = m._converge("entities/w/wekby", {"type": "intrusion-set", "id": "x:wekby",
                                                "title": "Wekby", "aliases": [alias]}, "")
    assert out.startswith(("updated", "converged")), out
    assert alias in page.read_text()
