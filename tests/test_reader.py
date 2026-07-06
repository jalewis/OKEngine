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


def test_provenance_fields_render_in_secondary(tmp_path, monkeypatch):
    # okengine#90 P3: composition provenance (maintained_by/discovered_by) renders in the collapsed
    # record-keeping panel, labeled — not mixed into the primary domain-intel rows.
    m = _load(tmp_path, monkeypatch)
    panel = m._meta_panel_items({
        "type": "source", "origin": "lab",                        # domain intel -> primary
        "maintained_by": ["okpack-sec", "okpack-ai-research"],    # provenance -> secondary
        "discovered_by": "okpack-sec",
    })
    sec_labels = {i["label"] for i in panel["secondary"]}
    pri_labels = {i["label"] for i in panel["primary"]}
    assert "Maintained by" in sec_labels and "Discovered by" in sec_labels
    assert "Origin" in pri_labels                                 # domain intel stays primary
    mb = next(i for i in panel["secondary"] if i["label"] == "Maintained by")
    assert "okpack-sec" in str(mb["values"]) and "okpack-ai-research" in str(mb["values"])


def test_private_vault_exposed_without_password_refuses(tmp_path, monkeypatch):
    # okengine#90 P4a: a PRIVATE vault bound to a non-loopback host with no reader password must
    # FAIL-CLOSED (refuse to start) rather than serve a private vault to the network.
    (tmp_path / "wiki").mkdir()
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "0.0.0.0")
    monkeypatch.delenv("OKENGINE_READER_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        _load(tmp_path, monkeypatch)


def test_trust_enforcement_allows_loopback_public_and_passworded(tmp_path, monkeypatch):
    # The refusal is narrow: only PRIVATE + exposed + no-password. These three must all start fine.
    (tmp_path / "wiki").mkdir()
    monkeypatch.delenv("OKENGINE_READER_PASSWORD", raising=False)
    monkeypatch.setenv("OKENGINE_TRUST", "private"); monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    assert _load(tmp_path, monkeypatch)                                   # private + loopback: ok
    monkeypatch.setenv("OKENGINE_TRUST", "public"); monkeypatch.setenv("OKENGINE_BIND", "0.0.0.0")
    assert _load(tmp_path, monkeypatch)                                   # public + exposed: ok
    monkeypatch.setenv("OKENGINE_TRUST", "private"); monkeypatch.setenv("OKENGINE_READER_PASSWORD", "s3cret")
    assert _load(tmp_path, monkeypatch)                                   # private + exposed + password: ok


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


def test_inline_panel_svg_survives_sanitizer(tmp_path, monkeypatch):
    """okengine.viz embeds charts as inline SVG in page bodies (the origin-system pattern);
    the nh3 allowlist must pass the static shape/text tags through — while still
    stripping script/event-handler vectors from the same markup."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    body = ('<!-- panel-svg v=abc123 -->\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 520" width="100%">\n'
            '<rect x="0" y="0" width="820" height="520" fill="#fafafa"/>\n'
            '<line x1="50" y1="46" x2="50" y2="464" stroke="#374151"/>\n'
            '<circle cx="200" cy="300" r="5" fill="#1d4ed8"/>\n'
            '<text x="209" y="304" font-size="11" fill="#111827">Node</text>\n'
            '<script>alert(1)</script>\n'
            '<circle cx="1" cy="1" r="1" onload="alert(2)"/>\n'
            '</svg>\n<!-- /panel-svg -->\n')
    html = m.render_md(body)
    assert "<svg" in html and "<circle" in html and "Node" in html
    assert "<script" not in html and "onload" not in html and "alert(1)" not in html


def test_about_reports_purpose_and_composition(tmp_path, monkeypatch):
    """About = deployment purpose + composition, ALL derived from live state files
    (pack.yaml declaration, the installer's CLAUDE.md markers, walk-up subtrees,
    extensions enable-state) — never scraped from the agent persona prose."""
    (tmp_path / "wiki" / "doctrine").mkdir(parents=True)
    (tmp_path / "wiki" / "doctrine" / "schema.yaml").write_text("types: {}\n")
    (tmp_path / "wiki" / "plain").mkdir()          # dir WITHOUT schema -> not a sub-domain
    (tmp_path / "pack.yaml").write_text(
        "name: okpack-x\nversion: 0.2.0\ndescription: Vendor risk watch\n"
        "mission: Track the suppliers we depend on.\n")
    (tmp_path / "CLAUDE.md").write_text(
        "# persona\n\n## Installed domain: doctrine (okpack-doctrine sub-domain)\n\nrules\n"
        "\n## Installed domain: security KB (okpack-sec co-install)\n\nrules\n")
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "extensions.yaml").write_text(
        "enabled:\n  okengine.events: {}\n  okengine.completeness: {}\n")
    m = _load(tmp_path, monkeypatch)
    a = m._about_info()
    assert a["description"] == "Vendor risk watch"
    assert a["mission"] == "Track the suppliers we depend on."
    assert a["installed_domains"] == ["doctrine (okpack-doctrine sub-domain)",
                                      "security KB (okpack-sec co-install)"]
    assert a["sub_domains"] == ["doctrine"]
    assert [e["id"] for e in a["extensions"]] == ["okengine.completeness", "okengine.events"]


def test_about_empty_state_is_calm(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    a = m._about_info()
    assert a["description"] == "" and a["installed_domains"] == [] \
        and a["sub_domains"] == [] and a["extensions"] == []  # list of dicts when populated


def test_about_extensions_prefer_effective_artifact(tmp_path, monkeypatch):
    """Core (default-on) extensions never appear in the opt-in enabled-state file —
    About must read the GENERATED effective artifact when present (found live:
    a fleet running 3 extensions showed 1)."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "extensions.yaml").write_text(
        "enabled:\n  okengine.competitive-analytics: {}\n")
    (tmp_path / ".okengine" / "extensions-effective.yaml").write_text(
        "effective:\n"
        "  - {id: okengine.competitive-analytics, name: Competitive analytics, description: quadrants}\n"
        "  - okengine.contradictions\n"          # legacy plain-id entry still accepted
        "  - {id: okengine.timeline, name: Timeline, description: dated dashboard}\n")
    m = _load(tmp_path, monkeypatch)
    got = m._about_info()["extensions"]
    assert [e["id"] for e in got] == [
        "okengine.competitive-analytics", "okengine.contradictions", "okengine.timeline"]
    assert got[0]["description"] == "quadrants"
    assert got[1]["description"] == ""            # legacy entry -> empty description
