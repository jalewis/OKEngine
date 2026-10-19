"""okengine#1042 [19]: every reader of a legacy scalar `aliases` splits it the way the write path does.

The write path coerces a scalar list field with `okengine.list_scalars.split_list_scalar`: a comma
inside brackets, a date or a number is not a delimiter. The id-index, the identity guard, the alias
dedup, converge, alias accrual and the projector split the same stored string naively on every
comma, so `Cl0p (aka TA505, Graceful Spider), Lace Tempest` was two names to the grounding gate and
the write path but three fragments (`Cl0p (aka TA505`, `Graceful Spider)`, `Lace Tempest`) to the
index and the guard, which then read a rewrite as dropping aliases.

Each reader is driven through its own entry point with the same stored value and must produce the
write path's items. The source scan at the end refuses a new naive alias split.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from okengine import actor_grounding, identity_guard
from okengine.list_scalars import split_list_scalar

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "cron"))
sys.path.insert(0, str(REPO / "okengine-mcp"))
import converge  # noqa: E402
import id_index  # noqa: E402
import id_lib  # noqa: E402

#: The live shape from the finding: a bracketed aside holding a comma, then a second alias.
STORED = "Cl0p (aka TA505, Graceful Spider), Lace Tempest"
WRITE_PATH_ITEMS = ["Cl0p (aka TA505, Graceful Spider)", "Lace Tempest"]


def test_the_write_path_coercion_is_the_reference():
    assert split_list_scalar(STORED) == WRITE_PATH_ITEMS


def test_the_id_index_registers_the_write_path_items():
    index = id_index.IdIndex()
    index._add("clop", "entities/c/clop.md", {"aliases": STORED})
    index._add_identity("entities/c/clop.md", {"name": "Clop", "aliases": STORED})
    assert set(index.aliases) == set(WRITE_PATH_ITEMS)
    assert set(index.alias_to_rels) == {id_lib.normalize_key(a) for a in WRITE_PATH_ITEMS}


def test_the_identity_guard_reads_the_aliases_the_index_records():
    assert identity_guard._indexed_aliases(STORED) == WRITE_PATH_ITEMS


def test_the_grounding_graders_read_the_same_items():
    assert actor_grounding._field_strings({"aliases": STORED}, "aliases") == WRITE_PATH_ITEMS


def test_converge_holds_the_write_path_items_and_accrues_onto_them():
    out = converge.additive_payload({"name": "Clop", "aliases": STORED},
                                    {"aliases": "Graceful Spider), New One"},
                                    alias_ok=lambda alias: True)
    assert out["aliases"] == [*WRITE_PATH_ITEMS, "Graceful Spider)", "New One"]
    held = converge.additive_payload({"name": "Clop", "aliases": STORED},
                                     {"aliases": "Lace Tempest"}, alias_ok=lambda alias: True)
    assert "aliases" not in held, "a held alias is agreement, not an addition"


def test_the_projector_reads_the_same_items():
    from okengine.projection import projector
    assert projector._aliases({"aliases": STORED}) == WRITE_PATH_ITEMS


def test_the_canonical_assembler_indexes_the_write_path_items(tmp_path, monkeypatch):
    """canonical_assemble routes observations to canonicals by name/alias; fragment keys from a
    naive split also counted toward its multi-identifier converge rule."""
    import canonical_assemble
    page = tmp_path / "wiki" / "entities" / "clop.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"---\ntype: entity\nname: Clop\naliases: {STORED}\n---\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(canonical_assemble.entity_resolve, "build_index",
                        lambda records: seen.setdefault("records", records))
    canonical_assemble._canonical_index(tmp_path)
    assert seen["records"] == [("clop", "Clop", WRITE_PATH_ITEMS)]


def test_the_bare_link_normalizer_indexes_the_write_path_items(tmp_path, monkeypatch):
    """normalize_bare_name_links rewrote `[[Graceful Spider)]]`-style fragments to the page; it
    indexes the aliases the write path stores, so a fragment is no name."""
    import normalize_bare_name_links as nb
    page = tmp_path / "wiki" / "entities" / "c" / "clop.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"---\ntype: entity\nname: Clop\naliases: {STORED}\n---\n", encoding="utf-8")
    monkeypatch.setattr(nb, "WIKI", tmp_path / "wiki")
    monkeypatch.setattr(nb, "ENT_DIR", tmp_path / "wiki" / "entities")
    name_index = nb.build_index()[0]
    assert nb._norm(WRITE_PATH_ITEMS[0]) in name_index
    assert nb._norm("Graceful Spider)") not in name_index


def _write_server(root: Path, monkeypatch):
    pytest.importorskip("mcp")
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(
        "okf:\n  required: [type]\ntypes:\n  entity: {}\nstrict_types: false\n"
        "partitioning:\n  namespaces:\n    entities: {strategy: flat}\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    for key in ("OKENGINE_WRITE_ACTOR", "OKENGINE_PACK", "OKENGINE_OUTPUT_CONTRACT_MODE"):
        monkeypatch.delenv(key, raising=False)
    name = f"write_server_1042_{id(root)}"
    spec = importlib.util.spec_from_file_location(name, REPO / "okengine-mcp" / "write_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_alias_dedup_matches_on_the_write_path_items(tmp_path, monkeypatch):
    ws = _write_server(tmp_path, monkeypatch)
    whole = id_lib.normalize_key(WRITE_PATH_ITEMS[0])
    assert ws._alias_matches({"name": "Clop", "aliases": STORED}, "clop", whole, set())
    fragment = id_lib.normalize_key("Graceful Spider)")
    assert not ws._alias_matches({"name": "Clop", "aliases": STORED}, "clop", fragment, set())


def test_alias_accrual_keeps_the_write_path_items(tmp_path, monkeypatch):
    ws = _write_server(tmp_path, monkeypatch)
    assert ws._converge_aliases(STORED, ["New One"]) == [*WRITE_PATH_ITEMS, "New One"]


def test_a_create_naming_a_legacy_alias_converges_into_its_page(tmp_path, monkeypatch):
    """End to end: a legacy page stores the scalar; a create whose name is the bracketed alias is
    the same entity, so the create is routed to that page (here held, because the persisted id index
    does not hold the legacy page yet). Split naively, no fragment matched and a duplicate was made."""
    page = tmp_path / "wiki" / "entities" / "clop.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"---\ntype: entity\nname: Clop\naliases: {STORED}\n---\n# Clop\n",
                    encoding="utf-8")
    ws = _write_server(tmp_path, monkeypatch)
    out = ws._create("entities/cl0p-aka-ta505", f"type: entity\nname: {WRITE_PATH_ITEMS[0]}",
                     "body")
    assert "entities/clop.md" in out, out
    assert not (tmp_path / "wiki" / "entities" / "cl0p-aka-ta505.md").exists()


def test_an_incoming_scalar_alias_list_is_matched_as_the_write_path_items(tmp_path, monkeypatch):
    """The incoming side of the alias dedup: a create carrying the legacy scalar names the page
    whose primary name is the bracketed alias. Split naively, its fragments matched nothing."""
    page = tmp_path / "wiki" / "entities" / "clop.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"---\ntype: entity\nname: {WRITE_PATH_ITEMS[0]}\n---\n# Clop\n",
                    encoding="utf-8")
    ws = _write_server(tmp_path, monkeypatch)
    out = ws._dedup_on_create("entities/new", ws._safe("entities/new"),
                              {"type": "entity", "name": "New", "aliases": STORED}, "")
    assert out is not None and "entities/clop.md" in out, out


def test_id_index_imports_the_package_when_loaded_by_path_from_a_checkout(tmp_path):
    """Real invocation: a checkout runs id_index by path with no package on sys.path. Its own
    bootstrap must find ../../src (as schema_lib does), and it must not shadow an installed copy."""
    probe = ("import sys, runpy; sys.path[:] = [p for p in sys.path if not p.endswith('/src')];"
             f"g = runpy.run_path({str(REPO / 'scripts/cron/id_index.py')!r}, run_name='probe');"
             "print(g['split_list_scalar'].__module__, sys.path.count(str(g['_SRC'])))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            cwd=tmp_path, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["okengine.list_scalars", "1"]


# Every production Python file: an alias reader anywhere re-creates the disagreement if it splits on
# every comma. (The first cut listed six files by hand and missed two lanes; scan them all.)
_ROOTS = ("src", "okengine-mcp", "scripts", "okengine-reader", "okengine-cockpit", "extensions",
          "tools")
_ALIAS_READERS = sorted(str(p.relative_to(REPO)) for root in _ROOTS
                        for p in (REPO / root).rglob("*.py") if "__pycache__" not in p.parts)


def _naive_alias_splits(source: str) -> list[int]:
    """Lines where `<expr>.split(",")` splits an alias value: the receiver names an alias, or the
    split sits in a function named for aliases. (A comma list of FIELD names, converge's `remove`,
    is not an alias value.)"""
    hits = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "split" and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == ","
                    and ("alias" in fn.name.lower()
                         or "alias" in ast.get_source_segment(source, node.func.value).lower())):
                hits.append(node.lineno)
    return sorted(set(hits))


def test_no_alias_reader_splits_on_every_comma():
    assert len(_ALIAS_READERS) > 100, "the scan must cover the production tree"
    hits = {rel: lines for rel in _ALIAS_READERS
            if (lines := _naive_alias_splits((REPO / rel).read_text(encoding="utf-8")))}
    assert not hits, (f"alias values split on every comma: {hits}; read a scalar list field with "
                      "okengine.list_scalars.split_list_scalar, as the write path does (#1042 [19])")


def test_the_scan_refuses_a_naive_alias_split():
    """Negative fixture: the scan flags the exact shape it replaced."""
    bad = ("def read(fm):\n    aliases = fm.get('aliases')\n"
           "    return [a.strip() for a in aliases.split(',') if a.strip()]\n")
    assert _naive_alias_splits(bad) == [3]
    assert _naive_alias_splits("def _aliases(v):\n    return v.split(',')\n") == [2]
    assert _naive_alias_splits("def other(v):\n    return v.split(',')\n") == []
