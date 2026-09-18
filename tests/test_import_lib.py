"""import_lib + framework import (okengine#154): foreign-vault adoption — dry-run transforms +
the change report. Pure-frontmatter steps preserve other keys; report writes nothing."""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


IL = _load("import_lib", "scripts/import_lib.py")


def _vault(tmp):
    w = tmp / "wiki" / "entities" / "a"
    w.mkdir(parents=True)
    (w / "acme.md").write_text("---\ntype: vendor\nname: Acme\nfounded: 1999\n---\n# Acme\n")
    (w / "globex.md").write_text("---\ntype: company\nname: Globex\n---\n# Globex\n")
    (tmp / "wiki" / "untyped.md").write_text("---\nname: Mystery\n---\n# m\n")
    return tmp / "wiki"


def test_scan(tmp_path):
    inv = IL.scan(_vault(tmp_path))
    assert inv["pages"] == 3 and inv["untyped"] == 1
    assert inv["types"] == {"vendor": 1, "company": 1}


def test_retype_by_type_dryrun_then_apply(tmp_path):
    w = _vault(tmp_path)
    rep = IL.retype_by_type(w, {"company": "vendor"}, apply=False)
    assert rep == ["retype globex.md: company -> vendor"]
    assert "type: company" in (w / "entities/a/globex.md").read_text()   # dry-run wrote nothing
    IL.retype_by_type(w, {"company": "vendor"}, apply=True)
    txt = (w / "entities/a/globex.md").read_text()
    assert "type: vendor" in txt and "name: Globex" in txt               # other keys preserved


def test_set_type_for_slugs(tmp_path):
    w = _vault(tmp_path)
    IL.set_type_for_slugs(w, {"acme": "segment"}, apply=True)
    assert "type: segment" in (w / "entities/a/acme.md").read_text()


def test_remap_fields_rename_and_default(tmp_path):
    w = _vault(tmp_path)
    IL.remap_fields(w, {"vendor": {"rename": {"founded": "since"}, "default": {"tlp": "clear"}}},
                    apply=True)
    t = (w / "entities/a/acme.md").read_text()
    assert "since: 1999" in t and "founded:" not in t and "tlp: clear" in t


def test_import_report_runs_readonly(tmp_path, capsys):
    vault = tmp_path / "src"
    _vault(vault)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]}, "types": {"vendor": {}},
        "type_aliases": {"company": "vendor"}}))
    fi = _load("framework_import", "scripts/framework_import.py")
    rc = fi.main([str(pack), "--vault", str(vault)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "import plan" in out and "vendor" in out
    assert "NOT IN PACK" not in out.split("company")[0]   # company resolves via type_aliases
    # read-only: nothing rewritten
    assert "type: company" in (vault / "wiki/entities/a/globex.md").read_text()


def test_page_filters_split_rewrite_and_scan_plain_pages(tmp_path):
    wiki=tmp_path/"wiki";wiki.mkdir()
    for name in ("_skip.md",".hidden.md","INDEX.md","INDEX-a.md","x.bak.md"):
        (wiki/name).write_text("plain")
    (wiki/"plain.md").write_text("plain")
    (wiki/"scalar.md").write_text("---\n- x\n---\nbody")
    assert {p.name for p in IL._iter_pages(wiki)}=={"plain.md","scalar.md"}
    assert IL._split_fm("plain")== (None,None,None)
    assert IL._fm_get("name: Acme\n","missing") is None
    assert IL._rewrite_type("plain","vendor")=="plain"
    rewritten=IL._rewrite_type("---\nname: Acme\n---\nbody","vendor")
    assert "type: vendor\nname: Acme" in rewritten
    inv=IL.scan(wiki);assert inv["pages"]==2 and inv["untyped"]==2


def test_transform_skip_and_idempotency_paths(tmp_path):
    wiki=_vault(tmp_path)
    assert IL.retype_by_type(wiki,{"vendor":"vendor"},True)==[]
    assert IL.retype_by_type(wiki,{"missing":"x"},True)==[]
    assert IL.set_type_for_slugs(wiki,{"missing":"x","acme":"vendor"},True)==[]
    assert IL.remap_fields(wiki,{"missing":{"default":{"x":"y"}}},True)==[]
    first=IL.remap_fields(wiki,{"vendor":{"default":{"tlp":"clear"}}},True)
    assert first and IL.remap_fields(wiki,{"vendor":{"default":{"tlp":"clear"}}},True)==[]


def test_namespace_map_shards_and_layout_report(tmp_path):
    assert IL.derive_ns_map({"types":{},"type_aliases":{"unknown":"nope"}}).get("unknown") is None
    assert IL._shard("")=="_" and IL._shard("!x")=="_" and IL._shard("9x")=="9"
    assert IL._canonical_path(Path("x/no-date.md"),"sources","slug")=="sources/slug"
    assert IL._canonical_path(Path("x/a.md"),"briefings","a")=="briefings/a"
    wiki=tmp_path/"wiki"
    p=wiki/"wrong/a.md";p.parent.mkdir(parents=True);p.write_text("---\ntype: concept\n---\n")
    q=wiki/"concepts/c.md";q.parent.mkdir(parents=True);q.write_text("---\ntype: concept\n---\n")
    assert IL.layout_misplaced(wiki,{"concept":"concepts"})=={"wrong -> concepts":1}


def test_collapse_missing_sources_and_noncanonical_depth(tmp_path):
    wiki=tmp_path/"wiki";wiki.mkdir()
    assert IL.collapse_source_dates(wiki,False)==[]
    p=wiki/"sources/2026/06/x.md";p.parent.mkdir(parents=True);p.write_text("---\ntype: source\n---\n")
    assert "0 day-dir" in IL.collapse_source_dates(wiki,False)[-1]


def test_plain_frontmatter_skips_transform_loops_and_filtered_sources(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "plain.md").write_text("body")
    typed = wiki / "typed.md"
    typed.write_text("---\ntype: old\na: one\n---\nbody")
    typed2 = wiki / "typed2.md"
    typed2.write_text("---\ntype: old\na: two\n---\nbody")
    assert IL.retype_by_type(wiki, {"old": "new"}, apply=True)
    assert IL.set_type_for_slugs(wiki, {"typed": "curated"}, apply=False)
    assert IL.set_type_for_slugs(wiki, {"typed": "curated", "typed2": "curated"}, apply=True)
    assert IL.remap_fields(
        wiki, {"curated": {"rename": {"a": "renamed"}}}, apply=False,
    )
    assert IL.remap_fields(
        wiki,
        {"curated": {"rename": {"a": "renamed", "absent": "unused"}}},
        apply=True,
    )
    assert IL.remap_fields(
        wiki, {"curated": {"default": {"reviewed": "true"}}}, apply=True,
    )
    assert IL.layout_misplaced(wiki, {"curated": "entities"}) == {
        "typed.md -> entities": 1, "typed2.md -> entities": 1,
    }
    assert "rehome" in IL.rehome_by_type(wiki, {"curated": "entities"}, apply=False)[0]

    src = wiki / "sources"
    src.mkdir()
    for name in ("_skip.md", ".hidden.md", "INDEX.md", "INDEX-a.md"):
        (src / name).write_text("ignored")
    assert "0 day-dir" in IL.collapse_source_dates(wiki, apply=False)[-1]


class _ChangingLinks:
    """Behave like the link regex but report a post-rewrite count change."""
    def __init__(self, real):
        self.real = real
        self.changed = False

    def findall(self, text):
        return ["synthetic"] if self.changed else []

    def sub(self, repl, text):
        self.changed = True
        return self.real.sub(repl, text)


def test_rehome_and_collapse_detect_link_count_invariant(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    misplaced = wiki / "wrong" / "x.md"
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text("---\ntype: concept\n---\nbody")
    monkeypatch.setattr(IL, "_LINK", _ChangingLinks(IL._LINK))
    with pytest.raises(RuntimeError, match="rehome-by-type INVARIANT"):
        IL.rehome_by_type(wiki, {"concept": "concepts"}, apply=True)

    wiki2 = tmp_path / "wiki2"
    dated = wiki2 / "sources/2026/01/02/x.md"
    dated.parent.mkdir(parents=True)
    dated.write_text("---\ntype: source\n---\nbody")
    import re
    monkeypatch.setattr(IL, "_LINK", _ChangingLinks(re.compile(r"\[\[([^\]|#\n]+)([\]#|])")))
    with pytest.raises(RuntimeError, match="collapse-source-dates INVARIANT"):
        IL.collapse_source_dates(wiki2, apply=True)
