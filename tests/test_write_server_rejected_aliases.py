"""A name a review removed from a page's `aliases` stays out.

Every importer merges aliases ADDITIVELY, so without a record of the removal the next run puts the
name straight back: an identity review removed tool and victim names from actor pages and a
six-hourly sheet import restored them the same morning. `rejected_aliases` is that record. The write
path appends to it when an update or patch drops a well-formed alias and refuses to re-add a listed
name; direct writers (converge, canonical assembly, pack importers) go through
`schema_lib.admissible_aliases`, one definition for all of them. Deleting an entry re-admits a name.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import schema_lib  # noqa: E402

URL = "https://research.example.test/2021/report"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\ntypes:\n  actor:\n    required: [type]\n  entity:\n"
        "    required: [type]\nstrict_types: false\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 7, 15).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _page(vault: Path, fm_yaml: str) -> Path:
    page = vault / "wiki/entities/a/acme.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: actor\nid: entities:acme\ntitle: Acme\n{fm_yaml}---\n# Acme\n",
                    encoding="utf-8")
    return page


def _fm(page: Path) -> dict:
    return yaml.safe_load(page.read_text(encoding="utf-8").split("---")[1])


# ---- the shared rule ---------------------------------------------------------------------------

def test_admissible_aliases_keeps_existing_and_admits_only_new_single_names():
    kept, refused = schema_lib.admissible_aliases(
        ["Acme", URL], ["acme", "Acme Group", "a, b, c", "Hydraq"], ["HYDRAQ"])
    assert kept == ["Acme", URL, "Acme Group"], "existing kept as-is; one new real name admitted"
    assert [v for v, _ in refused] == ["a, b, c", "Hydraq"]
    assert "rejected by review" in refused[1][1]


def test_admissible_aliases_accepts_scalars_and_absent_values():
    assert schema_lib.admissible_aliases("Acme", "Acme Group", None) == (["Acme", "Acme Group"], [])
    assert schema_lib.admissible_aliases(None, [], "") == ([], [])


def test_an_addition_with_no_letters_or_digits_is_dropped_silently():
    # it passes the shape rule (not empty, not a URL) but names nothing, so there is no key to keep
    assert schema_lib.admissible_aliases(["Acme"], ["---"]) == (["Acme"], [])


def test_alias_key_is_spelling_blind():
    assert schema_lib.alias_key("Brown Fox") == schema_lib.alias_key("brown-fox") == "brownfox"
    assert schema_lib.alias_key(None) == ""


def test_removed_aliases_reports_only_dropped_well_formed_names_once():
    before = ["Acme", "Stuxnet", "stuxnet", URL, "Apt 28"]
    assert schema_lib.removed_aliases(before, ["acme", "APT28"]) == ["Stuxnet"]
    assert schema_lib.removed_aliases(None, ["Acme"]) == []


def test_rejected_alias_keys_reads_list_or_scalar():
    assert schema_lib.rejected_alias_keys({"rejected_aliases": ["Hydraq", ""]}) == {"hydraq"}
    assert schema_lib.rejected_alias_keys({"rejected_aliases": "Hydraq"}) == {"hydraq"}
    assert schema_lib.rejected_alias_keys({}) == set()


# ---- the write path ----------------------------------------------------------------------------

def test_update_that_drops_an_alias_records_it(ws):
    mod, vault = ws
    page = _page(vault, "aliases:\n- Acme Group\n- Stuxnet\n")
    out = mod._update("entities/acme", {"aliases": ["Acme Group"]})
    assert out.startswith("updated"), out
    assert _fm(page)["rejected_aliases"] == ["Stuxnet"]


def test_update_cannot_re_add_a_rejected_alias(ws):
    mod, vault = ws
    page = _page(vault, "aliases:\n- Acme Group\nrejected_aliases:\n- Stuxnet\n")
    out = mod._update("entities/acme", {"aliases": ["Acme Group", "stuxnet"]})
    assert out.startswith("rejected:") and "rejected_aliases" in out, out
    assert "stuxnet" not in page.read_text(encoding="utf-8")


def test_removing_the_entry_re_admits_the_name_in_one_update(ws):
    mod, vault = ws
    page = _page(vault, "aliases:\n- Acme Group\nrejected_aliases:\n- Stuxnet\n")
    out = mod._update("entities/acme", {"aliases": ["Acme Group", "Stuxnet"], "rejected_aliases": []})
    assert out.startswith("updated"), out
    fm = _fm(page)
    assert "Stuxnet" in fm["aliases"] and not fm.get("rejected_aliases")


def test_a_grandfathered_listed_alias_does_not_block_other_edits(ws):
    mod, vault = ws
    page = _page(vault, "aliases:\n- Stuxnet\nrejected_aliases:\n- Stuxnet\n")
    out = mod._update("entities/acme", {"aliases": ["Stuxnet", "Acme Group"]})
    assert out.startswith("updated"), out
    assert "Acme Group" in _fm(page)["aliases"]


def test_respelling_or_dropping_junk_is_not_recorded(ws):
    mod, vault = ws
    page = _page(vault, f"aliases:\n- Apt 28\n- {URL}\n")
    out = mod._update("entities/acme", {"aliases": ["APT28"]})
    assert out.startswith("updated"), out
    assert "rejected_aliases" not in _fm(page)


def test_a_name_already_listed_is_not_listed_twice(ws):
    mod, vault = ws
    page = _page(vault, "aliases:\n- Acme Group\n- Stuxnet\nrejected_aliases:\n- STUXNET\n")
    mod._update("entities/acme", {"aliases": ["Acme Group"]})
    assert _fm(page)["rejected_aliases"] == ["STUXNET"]


def test_patch_that_drops_an_alias_records_it(ws):
    mod, vault = ws
    page = _page(vault, "aliases: [Acme Group, Hydraq]\n")
    out = mod._patch("entities/a/acme.md", "aliases: [Acme Group, Hydraq]", "aliases: [Acme Group]")
    assert out.startswith("patched"), out
    assert _fm(page)["rejected_aliases"] == ["Hydraq"]


def test_record_alias_removals_extends_a_scalar_entry(ws):
    mod, _vault = ws
    fm = {"aliases": ["Acme"], "rejected_aliases": "Old Name"}
    assert mod._record_alias_removals(fm, {"aliases": ["Acme", "Hydraq"]}) == ["Hydraq"]
    assert fm["rejected_aliases"] == ["Old Name", "Hydraq"]


# ---- direct writers ----------------------------------------------------------------------------

def test_converge_does_not_carry_junk_or_a_rejected_name_onto_the_canonical():
    import entity_converge
    winner = {"type": "actor", "title": "Acme", "aliases": ["Acme Group"],
              "rejected_aliases": ["Hydraq"]}
    loser = {"type": "actor", "title": "ACME", "aliases": ["Acme Corp", "Hydraq", URL, "x, y, z"],
             "rejected_aliases": ["Stuxnet"]}
    out = entity_converge._union(winner, [loser], ["entities/a/acme-2"])
    assert out["aliases"] == ["Acme Group", "Acme Corp"]
    assert out["rejected_aliases"] == ["Hydraq", "Stuxnet"], "every member's review record survives"


def test_converge_reads_a_scalar_loser_alias():
    import entity_converge
    out = entity_converge._union({"title": "Acme"}, [{"title": "ACME", "aliases": "Acme Corp"}], ["x"])
    assert out["aliases"] == ["Acme Corp"]


def test_canonical_assembly_refuses_junk_and_rejected_names(tmp_path):
    import canonical_assemble as ca
    page = tmp_path / "wiki" / "entities" / "a" / "acme.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: actor\nname: Acme\naliases: [Acme Group]\n"
                    "rejected_aliases: [Hydraq]\n---\n# Acme\n", encoding="utf-8")
    fused = {"name": "Acme", "aliases": ["Acme Corp", "Hydraq", URL]}
    for union in ({"aliases"}, set()):
        ca.write_canonical(tmp_path, "acme", "actor", dict(fused), [], ["feed"],
                           {"union": union}, "2026-07-15")
        fm = _fm(page)
        expected = ["Acme Group", "Acme Corp"] if union else ["Acme Corp"]
        assert fm["aliases"] == expected, (union, fm["aliases"])
        assert fm["rejected_aliases"] == ["Hydraq"], "assembly never drops the review record"


# ---- okengine#864: converge is additive for aliases; only an explicit remove removes ----------
# converge used to REPLACE the owned list, so an upsert that did not know every name dropped the
# rest silently, and neither a shorter list nor remove=aliases was recorded -- the next importer
# put the names straight back (the TAA#48 class).

_ACME = "type: entity\nname: Acme\nid: authority:acme\n"


def _converged(vault):
    return next((vault / "wiki/entities").rglob("acme.md"))


def test_converge_keeps_names_a_shorter_list_leaves_out(ws):
    mod, vault = ws
    mod._converge("entities/acme.md", _ACME + "aliases: [Acme Corp, BadName]\n")
    out = mod._converge("entities/acme.md", _ACME + "aliases: [Acme Corp]\n")
    assert not out.startswith(("refused", "rejected")), out
    assert _fm(_converged(vault))["aliases"] == ["Acme Corp", "BadName"]


def test_converge_appends_a_new_name_once_under_any_spelling(ws):
    mod, vault = ws
    mod._converge("entities/acme.md", _ACME + "aliases: [Acme Corp]\n")
    mod._converge("entities/acme.md", _ACME + "aliases: [acme-corp, Road Runner Supply]\n")
    assert _fm(_converged(vault))["aliases"] == ["Acme Corp", "Road Runner Supply"]


def test_an_explicit_converge_remove_is_recorded_so_importers_cannot_restore_it(ws):
    mod, vault = ws
    mod._converge("entities/acme.md", _ACME + "aliases: [Acme Corp, BadName]\n")
    out = mod._converge("entities/acme.md", _ACME, remove="aliases")
    assert not out.startswith(("refused", "rejected")), out
    fm = _fm(_converged(vault))
    assert "aliases" not in fm
    assert fm[schema_lib.REJECTED_ALIASES_FIELD] == ["Acme Corp", "BadName"]
    kept, refused = schema_lib.admissible_aliases(fm.get("aliases"), ["BadName"],
                                                  fm[schema_lib.REJECTED_ALIASES_FIELD])
    assert kept == [] and refused and "rejected by review" in refused[0][1]


def test_converge_still_refuses_a_review_rejected_name(ws):
    mod, vault = ws
    mod._converge("entities/acme.md", _ACME + "aliases: [Acme Corp]\nrejected_aliases: [BadName]\n")
    out = mod._converge("entities/acme.md", _ACME + "aliases: [Acme Corp, BadName]\n")
    assert out.startswith("rejected"), out
    assert _fm(_converged(vault))["aliases"] == ["Acme Corp"]
