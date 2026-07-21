import importlib.util
import re
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


def test_composed_schema_is_reader_authority_when_present(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("display_groups: {Stale: [source]}\n")
    artifact = tmp_path / ".okengine" / "composed-schema.yaml"
    artifact.parent.mkdir()
    artifact.write_text("display_groups: {Composed: [actor]}\n")
    m = _load(tmp_path, monkeypatch)
    assert m._governing_schema_path() == artifact
    assert m._display_groups() == [("Composed", frozenset({"actor"}))]


def test_provenance_fields_render_in_secondary(tmp_path, monkeypatch):
    # okengine#90 P3: composition provenance (maintained_by/discovered_by) renders in the collapsed
    # record-keeping panel, labeled — not mixed into the primary domain-intel rows.
    m = _load(tmp_path, monkeypatch)
    panel = m._meta_panel_items({
        "type": "source", "origin": "lab",                        # domain intel -> primary
        "maintained_by": ["okpack-cti", "okpack-ai-research"],    # provenance -> secondary
        "discovered_by": "okpack-cti",
    })
    sec_labels = {i["label"] for i in panel["secondary"]}
    pri_labels = {i["label"] for i in panel["primary"]}
    assert "Maintained by" in sec_labels and "Discovered by" in sec_labels
    assert "Origin" in pri_labels                                 # domain intel stays primary
    mb = next(i for i in panel["secondary"] if i["label"] == "Maintained by")
    assert "okpack-cti" in str(mb["values"]) and "okpack-ai-research" in str(mb["values"])


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
    (tmp_path / "wiki" / "fintech").mkdir(parents=True)
    (tmp_path / "wiki" / "fintech" / "schema.yaml").write_text("types: {}\n")
    (tmp_path / "wiki" / "plain").mkdir()          # dir WITHOUT schema -> not a sub-domain
    (tmp_path / "pack.yaml").write_text(
        "name: okpack-x\nversion: 0.2.0\ndescription: Vendor risk watch\n"
        "mission: Track the suppliers we depend on.\n")
    (tmp_path / "CLAUDE.md").write_text(
        "# persona\n\n## Installed domain: fintech (okpack-fintech sub-domain)\n\nrules\n"
        "\n## Installed domain: security KB (okpack-cti co-install)\n\nrules\n")
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "extensions.yaml").write_text(
        "enabled:\n  okengine.events: {}\n  okengine.completeness: {}\n")
    m = _load(tmp_path, monkeypatch)
    a = m._about_info()
    assert a["description"] == "Vendor risk watch"
    assert a["mission"] == "Track the suppliers we depend on."
    assert a["installed_domains"] == ["fintech (okpack-fintech sub-domain)",
                                      "security KB (okpack-cti co-install)"]
    assert a["sub_domains"] == ["fintech"]
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


def test_source_citation_links_the_original_article(tmp_path, monkeypatch):
    """Regression (live complaint, thrice): the daily brief's 'Source:' citation must link the
    analyst STRAIGHT to the ORIGINAL article. The prior fix left the title as an internal
    wikilink and demoted the real url: to a tiny ↗ glyph — so the obvious click still landed on
    the source stub, not the article. Now the source TITLE itself is the external link to the
    source page's url: frontmatter (anchor must SURVIVE the nh3 allowlist), no glyph. A source
    with no http(s) url falls back to the internal wikilink."""
    (tmp_path / "wiki" / "sources" / "2026" / "07").mkdir(parents=True)
    (tmp_path / "wiki" / "sources" / "2026" / "07" / "kaspersky-report.md").write_text(
        "---\ntype: source\ntitle: Device Code Phishing report\n"
        "url: https://securelist.example/device-code\n---\nbody\n", encoding="utf-8")
    (tmp_path / "wiki" / "sources" / "2026" / "07" / "no-url.md").write_text(
        "---\ntype: source\ntitle: Local note\n---\nbody\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html = m.render_md("Item.\nSource: [[sources/2026/07/kaspersky-report]]\n\n"
                       "Other.\nSource: [[sources/2026/07/no-url]]\n")
    # the TITLE is the clickable external link to the article (survives nh3), no demoted glyph
    assert 'href="https://securelist.example/device-code"' in html
    assert 'class="ext"' in html and "Device Code Phishing report" in html
    assert "&#8599;" not in html and "↗" not in html
    # the url'd source no longer renders an internal wikilink — the citation IS the article
    assert 'data-page="sources/2026/07/kaspersky-report"' not in html
    assert html.count("securelist.example") == 1
    # no url -> fall back to the internal wikilink (still reachable, just no external link)
    assert 'data-page="sources/2026/07/no-url"' in html


def test_embed_ambiguous_basename_resolves_to_none(tmp_path, monkeypatch):
    """L7: an ambiguous ![[dup]] embed (two files same basename) resolves to None, not an arbitrary
    first match; a unique basename still resolves (mirrors _link_title's uniqueness guard)."""
    (tmp_path / "wiki" / "a").mkdir(parents=True)
    (tmp_path / "wiki" / "b").mkdir(parents=True)
    (tmp_path / "wiki" / "a" / "dup.md").write_text("---\ntype: x\n---\nAAA")
    (tmp_path / "wiki" / "b" / "dup.md").write_text("---\ntype: x\n---\nBBB")
    (tmp_path / "wiki" / "a" / "solo.md").write_text("---\ntype: x\n---\nSOLO")
    m = _load(tmp_path, monkeypatch)
    m._EMBED_PATH_CACHE.clear()
    assert m._embed_rglob("dup.md") is None            # ambiguous -> unresolved
    assert m._embed_rglob("solo.md") is not None       # unique -> resolves


def test_embed_multi_source_entity_prefers_entities_page(tmp_path, monkeypatch):
    """#16: an embed of a multi-source entity (entities/<l>/slug PLUS observations/<src>/slug copies
    sharing the basename) resolves to the ENTITY page, not 'missing' — the naive len==1 gate (L7)
    regressed this by returning None for any multi-hit basename."""
    (tmp_path / "wiki" / "entities" / "a").mkdir(parents=True)
    (tmp_path / "wiki" / "observations" / "src1" / "a").mkdir(parents=True)
    (tmp_path / "wiki" / "entities" / "a" / "apt29.md").write_text("---\ntype: actor\ntitle: APT29\n---\nE")
    (tmp_path / "wiki" / "observations" / "src1" / "a" / "apt29.md").write_text("---\ntype: observation\n---\nO")
    m = _load(tmp_path, monkeypatch)
    m._EMBED_PATH_CACHE.clear()
    hit = m._embed_rglob("apt29.md")
    assert hit is not None and hit.parts[-3] == "entities"   # the entity, not None


def test_walkup_subdomain_multi_source_entity_resolves(tmp_path, monkeypatch):  # invariant-audit HIGH
    """In a WALK-UP co-installed vault the namespace is nested under a sub-domain container
    (wiki/<subdomain>/entities/... + wiki/<subdomain>/observations/...). The parts[0]-only check saw
    the sub-domain container, never fired the excluded-drop/entities-preference, and left the basename
    409-ambiguous. _ns_dirs makes it layout-agnostic."""
    sub = tmp_path / "wiki" / "cti-sub"
    (tmp_path / "schema.yaml").write_text("exclude: [observations]\n")
    (sub / "entities" / "a").mkdir(parents=True)
    (sub / "observations" / "src1" / "a").mkdir(parents=True)
    (sub / "entities" / "a" / "apt29.md").write_text("---\ntype: actor\ntitle: APT29\n---\nE")
    (sub / "observations" / "src1" / "a" / "apt29.md").write_text("---\ntype: observation\n---\nO")
    m = _load(tmp_path, monkeypatch)
    got = m._resolve_basename("apt29.md")
    assert got is not None and "entities" in got.parts and "observations" not in got.parts
    assert m._resolve_page("apt29") == (sub / "entities" / "a" / "apt29.md").resolve()  # no spurious 409


def test_shape_conflicts_survives_scalar_values(tmp_path, monkeypatch):
    """conflicts.values authored as scalars (`values: [high, medium]`, or a bare string that then
    iterates per-character) must not 500 the page view — the inner loop called .get() on each value
    assuming dicts (invariant-audit M28). `conflicts` is in _OKF_ALWAYS so the write path applies NO
    shape check, so a scalar reaches the reader unchecked and it must degrade, not crash."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    # scalar ENTRIES (list of scalars / bare string) AND scalar CONTAINERS (values is a non-iterable
    # scalar, e.g. 42) must all degrade, not 500 — the round-2 re-verify showed the entry-only guard
    # still crashed on `values: 42` (TypeError in the loop header, before the isinstance guard runs).
    fm = {"conflicts": [{"field": "severity", "headline": "high", "values": ["high", "medium"]},
                        {"field": "actor", "headline": "x", "values": "scalar-string"},
                        {"field": "count", "headline": "y", "values": 42},
                        {"field": "flag", "headline": "z", "values": True}]}
    out = m._shape_conflicts(fm)                       # must not raise AttributeError/TypeError
    assert isinstance(out, list) and len(out) == 4
    assert all(c["values"] == [] for c in out)         # every malformed shape skipped cleanly
    # a scalar `conflicts` container itself must not crash the loop header either
    assert m._shape_conflicts({"conflicts": 42}) == []
    assert m._shape_conflicts({"conflicts": "nope"}) == []
    # THIRD container: a well-shaped entry whose `sources` is a scalar must not crash (round-2 re-verify)
    fm2 = {"conflicts": [{"field": "f", "headline": "h",
                          "values": [{"value": "high", "sources": 42},
                                     {"value": "low", "sources": True}]}]}
    out2 = m._shape_conflicts(fm2)
    assert out2[0]["values"][0]["sources"] == [] and out2[0]["values"][1]["sources"] == []


def test_browse_tree_excludes_nested_namespace_in_walkup(tmp_path, monkeypatch):
    """api_tree's page count must not include pages in an EXCLUDED namespace nested under a walk-up
    sub-domain — the root-anchored `d.name in excluded` only dropped a TOP-LEVEL excluded dir, so a
    walk-up <subdomain>/observations/ leaked into the browse count (invariant-audit M-1310)."""
    (tmp_path / "schema.yaml").write_text("exclude: [observations]\n", encoding="utf-8")
    sd = tmp_path / "wiki" / "acme-sub"
    (sd / "observations" / "s1").mkdir(parents=True)
    (sd / "observations" / "s1" / "obs1.md").write_text("---\ntype: observation\n---\nO\n", encoding="utf-8")
    (sd / "entities" / "a").mkdir(parents=True)
    (sd / "entities" / "a" / "apt.md").write_text("---\ntype: actor\n---\nE\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    acme = next((x for x in m.api_tree()["dirs"] if x["dir"] == "acme-sub"), None)
    assert acme is not None
    assert acme["count"] == 1, "nested excluded observations must not be counted in browse"
    # the count MUST agree with the served ledger (_scan_dir behind api_pages) — the round-2 re-verify
    # caught api_tree excluding the nested page while _scan_dir still listed it (count 1 vs list 2).
    served = m._scan_dir("acme-sub")
    assert len(served) == acme["count"], f"browse count {acme['count']} != served list {len(served)}"
    assert all("observations" not in r["path"] for r in served), "excluded namespace leaked into the ledger"


def test_scan_dir_and_tree_hide_archived_subdir(tmp_path, monkeypatch):
    """Browse count + served ledger must hide reserved _archive/ SUB-DIR pages (leaf-only _skip missed
    them), so discovery surfaces AGREE with search's ripgrep `!_*` pruning (round-3 re-verify)."""
    (tmp_path / "wiki" / "entities" / "a").mkdir(parents=True)
    (tmp_path / "wiki" / "entities" / "a" / "live.md").write_text("---\ntype: actor\n---\nL\n", encoding="utf-8")
    arch = tmp_path / "wiki" / "entities" / "_archive" / "2026"
    arch.mkdir(parents=True)
    (arch / "retired.md").write_text("---\ntype: actor\n---\nR\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    tree = {x["dir"]: x["count"] for x in m.api_tree()["dirs"]}
    assert tree.get("entities") == 1, "archived _archive/ page must not be counted in browse"
    served = m._scan_dir("entities")
    assert len(served) == 1 and all("_archive" not in r["path"] for r in served)


def test_search_exclusion_is_any_depth(tmp_path, monkeypatch):
    """The search ripgrep ignore for an excluded namespace must match at ANY depth (`!**/{d}/**`),
    not root-anchored (`!{d}/**`) — else a walk-up <subdomain>/observations/ leaks into search
    results (invariant-audit M-1310). Capture the rg argv without needing ripgrep installed."""
    import subprocess as _sp
    (tmp_path / "schema.yaml").write_text("exclude: [observations]\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_guard", lambda *a, **k: (lambda: None))   # bypass rate-limit guard
    cap = {}

    class _P:
        stdout = ""
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **k: (cap.__setitem__("cmd", cmd), _P())[1])
    m.api_search(None, q="hello")
    assert "!**/observations/**" in cap["cmd"], cap["cmd"]
    assert "!observations/**" not in cap["cmd"], "root-anchored exclusion still present"
    # reserved-dir prune must EXEMPT the bare-`_` reshard bucket: `!_?*` (underscore + ≥1 char), never
    # `!_*` which also drops entities/x/_/x-force.md — search must agree with browse (batch-2 gate).
    assert "!_?*" in cap["cmd"] and "!_*" not in cap["cmd"], cap["cmd"]


def test_backlinks_skip_archived_source_at_any_depth(tmp_path, monkeypatch):
    """_skip_backlink_src must drop a source in a reserved _archive/ sub-dir at ANY depth — a leaf-only
    check let archived pages contribute 'what links here' edges that browse + search already hide, an
    inconsistency (batch-2 completeness re-verify). Live pages still contribute."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    assert m._skip_backlink_src("entities/_archive/oldactor") is True
    assert m._skip_backlink_src("entities/_archive/2026/oldactor") is True
    assert m._skip_backlink_src("entities/a/liveactor") is False


def test_reshard_bucket_page_stays_visible(tmp_path, monkeypatch):
    """Reader twin of the over-drop guard: the bare-`_` reshard bucket (entities/x/_/x-force.md) must
    stay visible in browse count + ledger, never dropped by the reserved-segment check (batch-2)."""
    b = tmp_path / "wiki" / "entities" / "x" / "_"; b.mkdir(parents=True)
    (b / "x-force.md").write_text("---\ntype: actor\nname: X-Force\n---\nbody\n", encoding="utf-8")
    ea = tmp_path / "wiki" / "entities" / "a"; ea.mkdir(parents=True)
    (ea / "apt.md").write_text("---\ntype: actor\n---\nA\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m._is_reserved_seg("_archive") is True and m._is_reserved_seg("_") is False
    tree = {x["dir"]: x["count"] for x in m.api_tree()["dirs"]}
    assert tree.get("entities") == 2, "the _ reshard bucket page must be counted, not over-dropped"
    assert any("x-force" in r["path"] for r in m._scan_dir("entities"))


def test_observations_by_canonical_hides_archived(tmp_path, monkeypatch):
    """Reader _observations_by_canonical twin (the cockpit's was fixed first) must skip _archive/
    retired observations so the page overlay's source drill-down agrees with browse/search (batch-2)."""
    live = tmp_path / "wiki" / "observations" / "s1"; live.mkdir(parents=True)
    (live / "o1.md").write_text("---\ncanonical: apt29\n---\nlive\n", encoding="utf-8")
    arch = tmp_path / "wiki" / "observations" / "_archive"; arch.mkdir(parents=True)
    (arch / "old.md").write_text("---\ncanonical: apt29\n---\nretired\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    # Simulate a freshly-booted host: monotonic() near 0 (a CI runner / just-started deployment). If
    # the cache inits to (0.0, {}) instead of -inf, `now - 0.0 < _DIR_TTL` reads the EMPTY initial
    # entry as "fresh" and the index is blank for 15 min after start. Pin it deterministically.
    monkeypatch.setattr(m.time, "monotonic", lambda: 5.0)
    obs = m._observations_by_canonical().get("apt29", [])
    assert len(obs) == 1, "archived observation must not appear in the canonical index"
    assert all("_archive" not in str(o) for o in obs)


def test_no_fresh_host_vulnerable_ttl_caches():  # reader + cockpit: monotonic-vs-uptime cache trap
    """A monotonic()-TTL-gated `_*_CACHE` must init its timestamp to -inf (so the first call always
    misses), UNLESS its payload sentinel is None (an `is not None` guard makes 0.0 safe). A finite
    `(0.0, <EMPTY>)` init reads the empty entry as 'fresh' on a low-uptime host — a CI runner or any
    just-started container — and serves BLANK data for up to the TTL (blank canonical drill-down,
    blank source-reliability grades, blank page-quality badges). This bit BOTH the reader and cockpit
    apps (5 caches); scan both so it can't regress in either."""
    bad = []
    for app in (REPO / "okengine-reader" / "app.py", REPO / "okengine-cockpit" / "app.py"):
        # Case-insensitive name + optional annotation: the original `_\w*CACHE` (uppercase only,
        # annotated module-level only) let a lowercase `_review_snapshot_cache = (0.0, [], [])`
        # ship the exact trap this guard exists for (2026-07-19 UI sweep) — and an invalidate
        # helper REASSIGNING `(0.0, <empty>)` is the same bug wearing a different line.
        for m in re.finditer(r"(_\w*[Cc][Aa][Cc][Hh][Ee]\w*)\s*(?::[^=\n]*)?=\s*"
                             r"\((?:0\.0|0),\s*(?:\{\}|\[\]|frozenset\(\)|\(\s*\))",
                             app.read_text()):
            bad.append(f"{app.name}:{m.group(1)}")
    assert not bad, ("TTL caches init to a finite timestamp with an EMPTY payload — they serve "
                     f"stale-empty on a freshly-booted host; init to float('-inf') instead: {bad}")


def test_editing_flag_defaults_on_and_honors_falsey(tmp_path, monkeypatch):
    """okengine#257: the reader's editing flag (surfaced in /api/about as `editing_enabled` so the
    UI can show a read-only indicator) defaults on and turns off on a falsey OKENGINE_EDITING."""
    monkeypatch.delenv("OKENGINE_EDITING", raising=False)
    assert _load(tmp_path, monkeypatch)._EDITING is True          # unset -> on (back-compat)
    monkeypatch.setenv("OKENGINE_EDITING", "0")
    assert _load(tmp_path, monkeypatch)._EDITING is False
    monkeypatch.setenv("OKENGINE_EDITING", "off")
    assert _load(tmp_path, monkeypatch)._EDITING is False
    monkeypatch.setenv("OKENGINE_EDITING", "1")
    assert _load(tmp_path, monkeypatch)._EDITING is True
