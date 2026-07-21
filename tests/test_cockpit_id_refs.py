"""Bare-id frontmatter refs linkify in the page overlay (okengine#259).

Derived cross-reference fields carry BARE ids/slugs — an enrichment lane stamps e.g.
`exploiting_actors: [G0022, shinyhunters]` or a bare `[CVE-…]` list.
_ref_target is path-shaped only, so these rendered as dead text. _id_index resolves a bare id/slug to
its page for the overlay's meta chips: an OPAQUE id shows the page title (G0022 -> "Sandworm Team"),
a readable slug/self-id stays as written, an ambiguous id gets no guessed link, and a value matching
nothing stays plain text (no over-linking)."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit" / "app.py"


def _load(vault, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def _w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _vault(tmp_path):
    wiki = tmp_path / "wiki"
    # an ATT&CK-imported actor: opaque id G0022 != slug sandworm-team
    _w(wiki / "entities" / "sandworm-team.md",
       "---\ntype: actor\nid: G0022\ntitle: Sandworm Team\n---\nbody\n")
    # a slug-id actor: id == slug (readable)
    _w(wiki / "entities" / "shinyhunters.md",
       "---\ntype: actor\nid: shinyhunters\ntitle: ShinyHunters\n---\nbody\n")
    # the cve carrying the bare reverse-edge refs + a non-reference bare field (must NOT link)
    _w(wiki / "cves" / "CVE-2026-20896.md",
       "---\ntype: cve\ncve_id: CVE-2026-20896\nseverity: critical\n"
       "exploiting_actors:\n- G0022\n- shinyhunters\n---\nbody\n")
    return tmp_path


def test_id_index_labels_and_ambiguity(tmp_path, monkeypatch):
    m = _load(_vault(tmp_path), monkeypatch)
    idx = m._id_index()
    # opaque id -> page + TITLE label; the slug alias -> the slug itself
    assert idx["G0022"]["label"] == "Sandworm Team"
    assert idx["G0022"]["page"] == "entities/sandworm-team"
    assert idx["sandworm-team"]["label"] == "sandworm-team"     # readable slug kept
    assert idx["shinyhunters"]["label"] == "shinyhunters"       # id == slug -> not swapped to title
    # a self-describing cve id resolves but keeps its id as the label
    assert idx["CVE-2026-20896"]["label"] == "CVE-2026-20896"


def test_meta_values_links_bare_ids_only_when_matched(tmp_path, monkeypatch):
    m = _load(_vault(tmp_path), monkeypatch)
    m._id_index()                                               # warm
    vals = m._meta_values(["G0022", "shinyhunters"])
    assert vals[0] == {"text": "Sandworm Team", "page": "entities/sandworm-team"}
    assert vals[1] == {"text": "shinyhunters", "page": "entities/shinyhunters"}
    # a value matching no id/slug is plain text — no over-linking
    assert m._meta_values("critical") == [{"text": "critical"}]
    assert m._meta_values("Oracle") == [{"text": "Oracle"}]     # case-sensitive: no accidental slug hit


def test_overlay_renders_exploiting_actors_as_links(tmp_path, monkeypatch):
    m = _load(_vault(tmp_path), monkeypatch)
    d = m.api_page(path="cves/CVE-2026-20896")
    row = next(r for r in d["meta"] if r["label"].lower() == "exploiting actors")
    assert row["values"] == [
        {"text": "Sandworm Team", "page": "entities/sandworm-team"},
        {"text": "shinyhunters", "page": "entities/shinyhunters"},
    ]
    # the non-reference bare field stays plain text
    sev = next(r for r in d["meta"] if r["label"].lower() == "severity")
    assert sev["values"] == [{"text": "critical"}]


def test_ambiguous_id_is_not_linked(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    # a SECOND page claiming id G0022 -> the token is ambiguous and must be dropped (no guessed link)
    _w(v / "wiki" / "entities" / "impostor.md",
       "---\ntype: actor\nid: G0022\ntitle: Impostor\n---\nbody\n")
    m = _load(v, monkeypatch)
    assert "G0022" not in m._id_index()
    assert m._meta_values("G0022") == [{"text": "G0022"}]       # unresolved -> plain text
