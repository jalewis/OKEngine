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
