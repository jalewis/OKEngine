"""A tombstone retires a page onto the SAME identity, never onto nothing wrong (okengine#101).

On one live vault 42 of 82 tombstoned actors had no valid successor. Fifteen carried a
"Different entity from X" reason -- a distinctness verdict applied as a retirement, which lost
Lazarus Group, Andariel, Ke3chang and White Bear. Others pointed at a malware page (Unit 8200 ->
Stuxnet), at themselves (Fox Kitten), at a tombstone (APT45 -> Andariel), or at a path that never
existed. Each case below is one of those, refused at the boundary every writer crosses; the
legitimate retirements (generic labels, a real duplicate onto its live canonical) still pass.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"


def _page(vault: Path, rel: str, fm: str) -> Path:
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}\n---\n# page\n", encoding="utf-8")
    return p


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\ntypes:\n  actor:\n    required: [type]\n  malware:\n"
        "    required: [type]\n  source:\n    required: [type]\nstrict_types: false\n",
        encoding="utf-8")
    _page(tmp_path, "entities/l/a/lazarus-group", "type: actor\nid: entities:lazarus-group\ntitle: Lazarus Group")
    _page(tmp_path, "entities/a/p/apt38", "type: actor\nid: entities:apt38\ntitle: APT38")
    _page(tmp_path, "entities/s/t/stuxnet", "type: malware\nid: entities:stuxnet\ntitle: Stuxnet")
    _page(tmp_path, "entities/a/n/andariel",
          "type: actor\nid: entities:andariel\ntitle: Andariel\nstatus: tombstoned")
    _page(tmp_path, "entities/u/n/unknown-actor", "type: actor\nid: entities:unknown-actor\ntitle: Unknown Actor")
    _page(tmp_path, "sources/2026/a", "type: source\nid: sources:a\ntitle: A")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 7, 15).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _status(vault, rel):
    return (vault / "wiki" / f"{rel}.md").read_text(encoding="utf-8")


def test_a_different_entity_verdict_never_retires_the_page(ws):
    mod, vault = ws
    out = mod._tombstone("entities/l/a/lazarus-group", "Different entity from Sapphire Sleet")
    assert "subjects are DIFFERENT" in out, out
    assert "tombstoned" not in _status(vault, "entities/l/a/lazarus-group")


@pytest.mark.parametrize("reason", ["separate actor with related tooling", "distinct group",
                                    "Not the same identity as APT15"])
def test_every_phrasing_of_distinctness_is_refused(ws, reason):
    mod, _vault = ws
    assert "subjects are DIFFERENT" in mod._tombstone("entities/l/a/lazarus-group", reason)


def test_a_duplicate_must_name_its_canonical(ws):
    mod, _vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of Lazarus Group")
    assert "pass superseded_by" in out, out


def test_a_successor_that_does_not_exist_is_refused(ws):
    mod, _vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of APT28",
                         "entities/a/apt28-russia-aligned-apt-group")
    assert "does not resolve" in out, out


def test_a_page_cannot_supersede_itself(ws):
    mod, _vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate", "entities/a/p/apt38")
    assert "cannot supersede itself" in out, out


def test_a_tombstoned_successor_is_refused(ws):
    mod, _vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of Andariel", "entities/a/n/andariel")
    assert "is itself tombstoned" in out, out


def test_an_actor_is_never_superseded_by_malware(ws):
    mod, _vault = ws
    out = mod._tombstone("entities/l/a/lazarus-group", "Duplicate of Stuxnet", "entities/s/t/stuxnet")
    assert "is type 'malware', not 'actor'" in out, out


def test_a_real_duplicate_onto_its_live_canonical_is_accepted(ws):
    mod, vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of Lazarus Group",
                         "entities/l/a/lazarus-group")
    assert "rejected" not in out and "refused" not in out, out
    assert "status: tombstoned" in _status(vault, "entities/a/p/apt38")


def test_an_unsharded_successor_path_is_found_where_it_sits(ws):
    mod, vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of Lazarus Group",
                         "[[entities/lazarus-group]]")
    assert "rejected" not in out, out


def test_a_generic_label_retires_without_a_successor(ws):
    mod, vault = ws
    out = mod._tombstone("entities/u/n/unknown-actor", "generic class label, not a named threat actor")
    assert "rejected" not in out and "refused" not in out, out


def test_sources_are_outside_this_guard(ws):
    mod, _vault = ws
    assert mod._tombstone_successor_reject(Path("/v/wiki/sources/2026/a.md"), {"type": "source"},
                                           "different entity", "sources/none") is None


def test_a_successor_off_its_seat_is_still_found(ws):
    """A live page can sit off its canonical seat (a flat copy the reshelve lane has not moved yet).
    The successor check must find it wherever it is, not reject a real canonical on a path shape."""
    mod, vault = ws
    _page(vault, "entities/sapphire-sleet", "type: actor\nid: entities:sapphire-sleet\ntitle: Sapphire Sleet")
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of Sapphire Sleet",
                         "entities/s/a/sapphire-sleet")
    assert "rejected" not in out, out


# --- okengine#863: only tombstone_entity may retire a page ---------------------------------------
# status / tombstone_reason / superseded_by are ordinary frontmatter keys and `status: tombstoned`
# is valid for every type, so the #101 guard above was one call away from irrelevant: the very
# payload it refuses went straight through update_entity, patch_entity and converge.

_DISTINCT = {"status": "tombstoned", "tombstone_reason": "Different entity from Sapphire Sleet",
             "superseded_by": "entities/s/t/stuxnet"}


def test_update_cannot_retire_a_page(ws):
    mod, vault = ws
    out = mod._update("entities/l/a/lazarus-group", dict(_DISTINCT))
    assert out.startswith("refused:") and "tombstone_entity" in out, out
    assert "tombstoned" not in _status(vault, "entities/l/a/lazarus-group")


def test_update_cannot_retire_a_page_even_with_a_valid_reason(ws):
    """The transition itself belongs to tombstone_entity, which ALSO keeps the page's record; a
    well-formed duplicate retirement must go through it, not around it."""
    mod, vault = ws
    out = mod._update("entities/a/p/apt38", {"status": "tombstoned",
                      "tombstone_reason": "Duplicate of Lazarus Group",
                      "superseded_by": "entities/l/a/lazarus-group"})
    assert out.startswith("refused:"), out
    assert "tombstoned" not in _status(vault, "entities/a/p/apt38")


def test_patch_cannot_retire_a_page(ws):
    mod, vault = ws
    out = mod._patch("entities/l/a/lazarus-group", "title: Lazarus Group",
                     "title: Lazarus Group\nstatus: tombstoned\n"
                     "tombstone_reason: Different entity from Sapphire Sleet")
    assert out.startswith("refused:") and "tombstone_entity" in out, out
    assert "tombstoned" not in _status(vault, "entities/l/a/lazarus-group")


def test_converge_cannot_retire_a_page(ws):
    mod, vault = ws
    out = mod._converge("entities/l/a/lazarus-group",
                        "type: actor\nid: entities:lazarus-group\ntitle: Lazarus Group\n"
                        "status: tombstoned\ntombstone_reason: Different entity from Sapphire Sleet")
    assert "tombstone_entity" in str(out), out
    assert "tombstoned" not in _status(vault, "entities/l/a/lazarus-group")


def test_an_ordinary_update_is_unaffected(ws):
    mod, vault = ws
    out = mod._update("entities/l/a/lazarus-group", {"title": "Lazarus Group (APT38 umbrella)"})
    assert out.startswith("updated"), out
    assert "status: tombstoned" not in _status(vault, "entities/l/a/lazarus-group")


def test_tombstone_entity_still_retires_a_real_duplicate(ws):
    mod, vault = ws
    out = mod._tombstone("entities/a/p/apt38", "Duplicate of Lazarus Group",
                         superseded_by="entities/l/a/lazarus-group")
    assert not out.startswith(("refused", "rejected")), out
    assert "status: tombstoned" in _status(vault, "entities/a/p/apt38")
