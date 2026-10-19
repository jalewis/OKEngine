"""!1305 QA (v0.15.11 audit #5), kept as regressions: title and name are one identity, through
every write path and spelling. `test_field_alias_spelling_of_name` was red at 0c143b9f: a held
`name` came back through a pack key alias (`display_name: name`) renamed onto it after the hold."""
from __future__ import annotations
import os, sys
from pathlib import Path
import pytest, yaml

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "tests"), str(REPO / "scripts/cron")]
import test_identity_guard_1028 as T  # noqa: E402
import id_lib  # noqa: E402


def _key(fm, stem):
    return id_lib.normalize_key(str(fm.get("name") or fm.get("title") or stem))


def _env(tmp_path, monkeypatch, fm, rel="entities/lyceum.md", schema_extra=None):
    m, vault = T._load(tmp_path, monkeypatch, "okpack-threat-actors", "main")
    if schema_extra:
        s = yaml.safe_load((vault / "wiki/schema.yaml").read_text()); s.update(schema_extra)
        (vault / "wiki/schema.yaml").write_text(yaml.safe_dump(s))
    page = T._seed(m, rel, fm)
    return m, vault, page


def _write(m, path, rel, fm, body=None):
    if path == "update":
        return m._update(rel, fm, body)
    if path == "converge":
        return m._converge(rel, fm, body or "")
    raise AssertionError


PATHS = pytest.mark.parametrize("path", ["update", "converge"])
STORED = {"type": "actor", "title": "Lyceum", "id": "entities:lyceum", "description": "d"}


@PATHS
@pytest.mark.parametrize("add", [{"name": "Lyceum APT"}, {"name": "Hexane", "title": "Lyceum"},
                                 {"name": "LYCEUM APT"}])
def test_title_only_page_cannot_be_rekeyed(tmp_path, monkeypatch, path, add):
    m, vault, page = _env(tmp_path, monkeypatch, dict(STORED))
    out = _write(m, path, "entities/lyceum.md", {**STORED, **add, "description": "enriched"})
    fm = T._fm(page)
    assert _key(fm, "lyceum") == "lyceum" and fm["description"] == "enriched", (out, fm)


@PATHS
@pytest.mark.parametrize("add", [{"name": "Lyceum APT"}, {"name": "Hexane", "title": "Hexane"},
                                 {"name": "lyceum", "title": "Lyceum APT"},
                                 {"name": "Lyceum APT", "title": "lyceum"}])
def test_nameless_page_both_keys(tmp_path, monkeypatch, path, add):
    base = {"type": "malware", "id": "entities:lyceum", "description": "d"}
    m, vault, page = _env(tmp_path, monkeypatch, dict(base))
    out = _write(m, path, "entities/lyceum.md", {**base, **add, "description": "enriched"})
    fm = T._fm(page)
    shown = {k: fm.get(k) for k in ("name", "title")}
    assert _key(fm, "lyceum") == "lyceum" and fm["description"] == "enriched", (out, shown)
    for k in ("name", "title"):   # neither display key may carry a different identity
        if fm.get(k):
            assert id_lib.normalize_key(str(fm[k])) == "lyceum", (out, shown)


@PATHS
def test_fold_then_change(tmp_path, monkeypatch, path):
    m, vault, page = _env(tmp_path, monkeypatch, dict(STORED))
    _write(m, path, "entities/lyceum.md", {**STORED, "name": "LYCEUM!"})   # folds to the same key
    out = _write(m, path, "entities/lyceum.md", {**STORED, "name": "Hexane"})
    fm = T._fm(page)
    assert _key(fm, "lyceum") == "lyceum", (out, fm)


@PATHS
def test_slug_like_title_gains_name(tmp_path, monkeypatch, path):
    stored = {"type": "actor", "title": "lyceum", "id": "entities:lyceum"}
    m, vault, page = _env(tmp_path, monkeypatch, dict(stored))
    out = _write(m, path, "entities/lyceum.md", {**stored, "name": "Lyceum"})   # same identity: applies
    assert T._fm(page).get("name") == "Lyceum", out
    out = _write(m, path, "entities/lyceum.md", {**stored, "name": "Lyceum Group"})
    assert T._fm(page).get("name") == "Lyceum", out


@PATHS
def test_held_addition_keeps_body(tmp_path, monkeypatch, path):
    m, vault, page = _env(tmp_path, monkeypatch, dict(STORED))
    out = _write(m, path, "entities/lyceum.md", {**STORED, "name": "Hexane"}, body="Different body.")
    assert "Curated body." in page.read_text() and "name" not in T._fm(page), out


def test_patch_adds_name_line(tmp_path, monkeypatch):
    m, vault, page = _env(tmp_path, monkeypatch, dict(STORED))
    out = m._patch("entities/lyceum.md", "title: Lyceum\n", "title: Lyceum\nname: Hexane\n")
    fm = T._fm(page)
    assert _key(fm, "lyceum") == "lyceum", (out, fm)


@PATHS
def test_field_alias_spelling_of_name(tmp_path, monkeypatch, path):
    """Latent (no live pack declares one): a pack key alias that renames to `name`."""
    extra = {"drift": {"field_aliases": {"display_name": "name"}}, "field_aliases": {"display_name": "name"}}
    m, vault, page = _env(tmp_path, monkeypatch, dict(STORED), schema_extra=extra)
    out = _write(m, path, "entities/lyceum.md", {**STORED, "display_name": "Hexane"})
    fm = T._fm(page)
    assert _key(fm, "lyceum") == "lyceum", (out, fm)
