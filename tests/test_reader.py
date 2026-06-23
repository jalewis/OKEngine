import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("nh3")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-reader" / "app.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    sys.path.insert(0, str(APP.parent))   # so app.py's `import limits` resolves
    sys.modules.pop("reader_app", None)
    spec = importlib.util.spec_from_file_location("reader_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["reader_app"] = m
    spec.loader.exec_module(m)
    return m


def test_hidden_pages_are_not_renderable(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_review-queue.md").write_text("---\ntitle: Queue\n---\nsecret\n")
    m = _load(tmp_path, monkeypatch)

    with pytest.raises(m.HTTPException) as ei:
        m._resolve_page("_review-queue")
    assert ei.value.status_code == 403


def test_bare_basename_prefers_canonical_then_refuses_true_ambiguity(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "sources").mkdir()
    (wiki / "notes").mkdir()
    (wiki / "entities" / "acme.md").write_text("---\ntype: entity\n---\n# Acme\n")
    (wiki / "sources" / "acme.md").write_text("---\ntype: source\n---\n# Acme source\n")
    m = _load(tmp_path, monkeypatch)

    # On a bare-name collision the CANONICAL entities/ page wins (okengine#23 wikilink resolution),
    # not a 409 — a multi-source entity has entities/<slug> PLUS per-source copies of the slug.
    assert m._resolve_page("acme") == (wiki / "entities" / "acme.md").resolve()
    # an explicit full path still resolves to exactly that page
    assert m._resolve_page("sources/acme") == (wiki / "sources" / "acme.md").resolve()

    # True ambiguity (multiple non-canonical matches, no entities/ disambiguator) -> 409.
    (wiki / "notes" / "beta.md").write_text("---\ntype: note\n---\n# Beta\n")
    (wiki / "sources" / "beta.md").write_text("---\ntype: source\n---\n# Beta source\n")
    m = _load(tmp_path, monkeypatch)
    with pytest.raises(m.HTTPException) as ei:
        m._resolve_page("beta")
    assert ei.value.status_code == 409


def test_pandoc_passes_a_nonempty_title(tmp_path, monkeypatch):
    # Regression: pandoc standalone HTML/docx defaulted the title to the temp
    # filename stem ("in") whenever no title metadata was supplied. Every export
    # must carry an explicit --metadata title=... .
    m = _load(tmp_path, monkeypatch)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        # pandoc would write the output file; create it so read_bytes() works.
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_bytes(b"%PDF-1.7\n")
        class R: pass
        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._pandoc("# Body\n", "pdf", title="Acme Corp")
    cmd = captured["cmd"]
    assert "--metadata" in cmd
    assert "title=Acme Corp" in cmd

    # Empty/whitespace title falls back to a stable label, never blank.
    m._pandoc("# Body\n", "pdf", title="   ")
    assert "title=OKEngine page" in captured["cmd"]


def test_pdf_export_pins_pydyf_to_match_weasyprint():
    # Root cause of the PDF-export crash: weasyprint was pinned but its transitive
    # dep pydyf was not, so the image pulled pydyf 0.12.x which dropped
    # Stream.transform that weasyprint 62.3 calls. If weasyprint is pinned, pydyf
    # MUST be pinned alongside it.
    req = (REPO / "okengine-reader" / "requirements.txt").read_text()
    if "weasyprint" in req:
        assert "pydyf" in req, "weasyprint is pinned but pydyf is not — transitive drift will break PDF export"


def test_backlink_title_uses_frontmatter_name_not_first_heading(tmp_path, monkeypatch):
    """okengine: a backlink label must be the page's curated name, NOT IWE's title
    (its first heading). Source pages all open with '## Summary' and entity pages
    have no clean H1, so the heading-title made every backlink read 'Summary' or a
    raw slug path — useless in a 'what links here' list."""
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "sources").mkdir()
    (wiki / "entities" / "a" / "andariel.md").write_text(
        "---\ntype: intrusion-set\nname: Andariel\n---\n## Summary\nstuff\n")
    (wiki / "sources" / "s1.md").write_text(
        "---\ntype: source\nname: FireEye Operation Saffron Rose 2013\n---\n## Summary\nx\n")
    (wiki / "entities" / "a" / "raw-name.md").write_text(
        "---\ntype: intrusion-set\n---\n## Summary\nx\n")   # no title/name
    m = _load(tmp_path, monkeypatch)

    # no title/name but a TRUE '# H1' (e.g. a freshly-ingested source) -> the H1,
    # even when a '## Summary' SECTION heading precedes it.
    (wiki / "sources" / "s2.md").write_text(
        "---\ntype: source\n---\n## Summary\nblah\n\n# Crypto Clipper uses Tor for propagation\nbody\n")
    m = _load(tmp_path, monkeypatch)

    assert m._backlink_title("entities/a/andariel") == "Andariel"
    assert m._backlink_title("sources/s1") == "FireEye Operation Saffron Rose 2013"
    assert m._backlink_title("sources/s2") == "Crypto Clipper uses Tor for propagation"
    # no title/name AND no true H1 (only '## Summary') -> de-slugged basename, never 'Summary'
    assert m._backlink_title("entities/a/raw-name") == "raw name"
    # missing file -> graceful de-slugged basename, no exception
    assert m._backlink_title("entities/a/ghost-page") == "ghost page"
