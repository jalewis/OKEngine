"""An `aliases` entry is a single NAME on every write boundary (okengine#845).

Aliases are match terms other lanes join identities on. On one live vault 44 actor aliases were
URLs and ~50 were comma-joined lists or prose, imported from a spreadsheet's reference and notes
columns; a tool-name alias ("Stuxnet" on a military unit) later drove a dedupe lane to merge an
actor into a malware page. The rule lives in `schema_lib.alias_value_problem` so the write path
and direct-writing lanes apply one definition. Existing aliases are grandfathered, exactly like
legacy source refs: a page carrying old junk stays editable, but no writer can add more.
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


# ---- the rule --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["APT29", "Cozy Bear", "Cyber Army of Russia Reborn",
                                  "Furuno Electric Co., Ltd.", "Olympic Games / Stuxnet", "UNC6240",
                                  "x" * schema_lib.ALIAS_MAX_LEN])
def test_real_names_pass(name):
    assert schema_lib.alias_value_problem(name) is None


@pytest.mark.parametrize("value, why", [
    (URL, "a URL"), ("www.example.test/x", "a URL"), ("HTTP://EXAMPLE.TEST", "a URL"),
    ("Pallas \nBandook", "multi-line text"),
    ("Sakurajima,Kratos,Phoenix squad", "several names in one entry"),
    ("x" * (schema_lib.ALIAS_MAX_LEN + 1), "longer than"),
    ("", "empty"), ("   ", "empty"), (None, "empty"),
])
def test_non_names_are_named_for_what_they_are(value, why):
    assert why in (schema_lib.alias_value_problem(value) or "")


# ---- the guard -------------------------------------------------------------------------------

def test_guard_rejects_a_new_bad_alias_and_says_why(ws):
    mod, vault = ws
    reason = mod._alias_shape_reject(vault / "wiki/entities/acme.md", {"aliases": ["Acme", URL]})
    assert reason and reason.startswith("rejected:") and "a URL" in reason and URL[:40] in reason


def test_guard_grandfathers_aliases_already_on_the_page(ws):
    mod, vault = ws
    prev = {"aliases": ["Acme", URL]}
    assert mod._alias_shape_reject(vault / "wiki/entities/acme.md", prev, prev) is None
    added = {"aliases": ["Acme", URL, "a,b,c"]}
    reason = mod._alias_shape_reject(vault / "wiki/entities/acme.md", added, prev)
    assert reason and "a,b,c" in reason and URL not in reason


def test_guard_reads_a_scalar_alias_and_ignores_absent_ones(ws):
    mod, vault = ws
    page = vault / "wiki/entities/acme.md"
    assert "a URL" in (mod._alias_shape_reject(page, {"aliases": URL}) or "")
    assert mod._alias_shape_reject(page, {"aliases": "Acme"}) is None
    assert mod._alias_shape_reject(page, {}) is None
    assert mod._alias_shape_reject(page, {"aliases": ""}) is None
    assert mod._alias_shape_reject(page, {"aliases": ["Acme"]}, {"aliases": URL}) is None


def test_guard_lists_at_most_five_problems(ws):
    mod, vault = ws
    many = {"aliases": [f"https://x.test/{i}" for i in range(7)]}
    reason = mod._alias_shape_reject(vault / "wiki/entities/acme.md", many)
    assert reason.count("a URL") == 5 and " …" in reason


# ---- every write boundary calls it -----------------------------------------------------------

def test_create_refuses_a_url_alias(ws):
    mod, vault = ws
    out = mod._create("entities/acme", {"type": "actor", "title": "Acme", "aliases": [URL]},
                      "# Acme\n")
    assert "an alias must be a single name" in out, out
    assert not list((vault / "wiki/entities").rglob("acme.md"))


def test_update_refuses_a_new_bad_alias_but_keeps_a_polluted_page_editable(ws):
    mod, vault = ws
    page = vault / "wiki/entities/a/acme.md"   # entities are sharded by first letter
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: actor\nid: entities:acme\ntitle: Acme\naliases:\n- {URL}\n---\n# Acme\n",
                    encoding="utf-8")
    refused = mod._update("entities/acme", {"aliases": [URL, "Acme, Acme Corp, ACME"]})
    assert "an alias must be a single name" in refused, refused
    accepted = mod._update("entities/acme", {"aliases": [URL, "Acme Group"]})
    assert "an alias must be a single name" not in accepted, accepted
    assert "Acme Group" in page.read_text(encoding="utf-8"), accepted


def test_patch_refuses_a_new_bad_alias(ws):
    mod, vault = ws
    page = vault / "wiki/entities/a/acme.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: actor\nid: entities:acme\ntitle: Acme\naliases: []\n---\n# Acme\n", encoding="utf-8")
    out = mod._patch("entities/a/acme.md", "aliases: []", f"aliases: [{URL}]")
    assert "an alias must be a single name" in out, out
    assert URL not in page.read_text(encoding="utf-8")


def test_converge_refuses_a_bad_alias_on_create_and_on_merge(ws):
    mod, vault = ws
    created = mod._converge("entities/acme.md",
                            "type: entity\nname: Acme\nid: authority:acme\naliases: [Acme Corp]\n")
    assert created.startswith("created"), created
    merged = mod._converge("entities/acme.md",
                           f"type: entity\nname: Acme\nid: authority:acme\naliases: [{URL}]\n")
    assert "an alias must be a single name" in merged, merged
    assert URL not in (vault / "wiki/entities/a/acme.md").read_text(encoding="utf-8")
    fresh = mod._converge("entities/other.md",
                          "type: entity\nname: Other\nid: authority:other\naliases: ['a, b, c']\n")
    assert "an alias must be a single name" in fresh, fresh
