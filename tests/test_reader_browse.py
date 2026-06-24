"""Regression: the reader hides schema-excluded dirs and generated INDEX pages
from the browse rail, page lists, and search globs (#25)."""
import importlib
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
READER = REPO / "okengine-reader"

pytest.importorskip("fastapi")
pytest.importorskip("nh3")


@pytest.fixture(autouse=True)
def _restore_env():
    """_load() sets os.environ['VAULT_DIR'] directly; snapshot + restore env around every test so
    a leaked VAULT_DIR can't make a later test (or file) resolve against the wrong vault."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _load(vault: Path):
    os.environ["VAULT_DIR"] = str(vault)
    sys.path.insert(0, str(READER))           # for `import limits`
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", READER / "app.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["app"] = m
    spec.loader.exec_module(m)
    return m


def _mk(p: Path, type_="source"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: {type_}\ntitle: {p.stem}\n---\n# {p.stem}\n")


def _build_vault(root: Path):
    (root / "schema.yaml").write_text("exclude: [wiki/operational/, wiki/dashboards/]\n")
    w = root / "wiki"
    _mk(w / "sources" / "real-source.md")
    _mk(w / "sources" / "INDEX.md", "dashboard")                 # generated per-dir index
    _mk(w / "sources" / "arxiv" / "INDEX.md", "dashboard")
    _mk(w / "sources" / "arxiv" / "2026" / "06" / "paper.md")    # nested real source
    _mk(w / "operational" / "op-note.md", "operational")         # schema-excluded dir
    _mk(w / "dashboards" / "hot.md", "dashboard")                # schema-excluded dir
    return root


def test_excluded_dirs_surfaces_dashboards_hides_operational(tmp_path):
    """okengine#117: schema `exclude:` scopes CONFORMANCE, not reader visibility. The reader
    SURFACES dashboards/ (synthesized digests meant to be read) and hides only operator-internal
    excludes like operational/."""
    m = _load(_build_vault(tmp_path))
    assert m._excluded_dirs() == frozenset({"operational"})   # dashboards surfaced, operational hidden


def test_browse_rail_surfaces_dashboards_hides_operational(tmp_path):
    m = _load(_build_vault(tmp_path))
    tree = m.api_tree()["dirs"]
    rail = {d["dir"] for d in tree}
    assert "sources" in rail
    assert "operational" not in rail            # operator-internal -> hidden
    assert "dashboards" in rail                 # synthesized digests -> surfaced (the brief/HOT payoff)
    assert {d["dir"]: d["derived"] for d in tree}.get("dashboards") is True   # flagged generated


def test_browse_rail_marks_derived_namespaces(tmp_path):
    """A namespace of type:dashboard pages (briefings) is flagged derived; curated
    knowledge namespaces are not."""
    w = tmp_path / "wiki"
    _mk(w / "sources" / "a.md", "source")
    _mk(w / "entities" / "e.md", "entity")
    _mk(w / "briefings" / "2026-06-18.md", "dashboard")          # generated brief
    m = _load(tmp_path)
    by = {d["dir"]: d["derived"] for d in m.api_tree()["dirs"]}
    assert by == {"sources": False, "entities": False, "briefings": True}


def test_page_list_hides_generated_index_pages(tmp_path):
    m = _load(_build_vault(tmp_path))
    pages = m._scan_dir("sources")
    stems = {Path(p["path"]).name for p in pages}
    assert "real-source" in stems and "paper" in stems
    assert "INDEX" not in stems                                  # generated index pages gone


def test_excluded_dir_listing_is_empty(tmp_path):
    m = _load(_build_vault(tmp_path))
    assert m._scan_dir("operational") == []


def test_skip_covers_generated_indexes(tmp_path):
    m = _load(tmp_path)
    for n in ("INDEX.md", "index.md", "INDEX-p02.md", "_reserved.md", "x.bak.1.md"):
        assert m._skip(n)
    assert not m._skip("real-source.md")


def test_skip_backlink_src_excludes_generated_and_operational(tmp_path):
    """Backlink-graph hygiene: HOT.md (markdown links to every hot-set page) and other
    generated/operational artifacts must NOT contribute "what links here" edges, while
    real namespaces still do. IWE keys arrive without the .md extension."""
    m = _load(_build_vault(tmp_path))   # excludes operational/ + dashboards/
    # reserved root artifacts (_skip misses HOT/log; we add them) + generated indexes
    for k in ("HOT", "log", "index", "index.bak.20260619", "_review-queue"):
        assert m._skip_backlink_src(k), k
    # pages under an exclude:-ed namespace
    assert m._skip_backlink_src("operational/op-note")
    assert m._skip_backlink_src("dashboards/hot")
    # real references are KEPT (incl. briefings — a brief citing a source is a real edge)
    for k in ("sources/2026/06/real-source", "entities/a/autojack", "briefings/2026-06-19"):
        assert not m._skip_backlink_src(k), k


def test_meta_panel_surfaces_frontmatter(tmp_path):
    """The page info panel renders structured frontmatter (origin, aliases, links) so a
    thin importer stub still shows its intel. Domain-agnostic; URL/ref fields become links."""
    m = _load(tmp_path)
    fm = {"type": "intrusion-set", "name": "APT 6", "aliases": ["1.php Group", "Group X"],
          "suspected_origin": "China", "target_sectors": ["Government"],
          "tgc_card": "https://apt.etda.or.th/x", "tlp": "clear",
          "refs": [{"std": "mitre-attack", "id": "G0007", "url": "https://attack.mitre.org/groups/G0007"}],
          "empty": "", "blank_list": []}
    fm["last_updated"] = "2026-06-20"      # record-keeping -> secondary (collapsed)
    out = m._meta_panel_items(fm)
    prim = {it["label"]: it for it in out["primary"]}
    sec = {it["label"] for it in out["secondary"]}
    assert "Type" not in prim and "Name" not in prim          # shown in header, not repeated
    assert "Empty" not in prim and "Blank list" not in prim   # empties skipped
    # knowledge fields are SURFACED (primary)
    assert prim["Suspected origin"]["values"] == [{"text": "China"}]
    assert {v["text"] for v in prim["Aliases"]["values"]} == {"1.php Group", "Group X"}
    tgc = prim["Tgc card"]["values"][0]
    assert tgc["url"].startswith("https://") and tgc["text"] == "apt.etda.or.th"   # friendly host label
    ref = prim["Refs"]["values"][0]                                        # list-of-dict -> link
    assert ref["url"] == "https://attack.mitre.org/groups/G0007" and ref["text"] == "G0007"
    # record-keeping is tucked into secondary (collapsed), not surfaced
    assert "Tlp" in sec and "Last updated" in sec
    assert "Aliases" not in sec and "Suspected origin" not in sec


def test_index_cache_busts_assets(tmp_path):
    """index() appends a content-hash ?v= to app.js/style.css so a reader update isn't
    served stale from the browser's heuristic cache."""
    m = _load(tmp_path)
    html = m.index()
    assert "/static/app.js?v=" in html and "/static/style.css?v=" in html


def test_about_panel_wired(tmp_path):
    """The reader ships an About affordance linking back to the project (#26)."""
    html = (READER / "static" / "index.html").read_text()
    assert 'id="about-btn"' in html and 'id="about-overlay"' in html
    assert 'id="about-repo"' in html                     # configurable project link (no hardcoded URL)
    assert "linkedin.com/in/jasonalewis" in html        # maintainer credit
    assert "about-shortcuts" in html and "<kbd>/</kbd>" in html   # keyboard shortcuts line
    assert "about-license" in html and "Apache-2.0" in html       # license note
    js = (READER / "static" / "app.js").read_text()
    assert "openAbout" in js and "closeAbout" in js      # open/close behaviour


def test_about_api_reports_vault_and_versions(tmp_path, monkeypatch):
    """/api/about surfaces the vault name (pack.yaml) + engine/Hermes pins (#1/#2) and a
    deployment-configured project_url (env wins, pack.yaml fallback; engine hardcodes none)."""
    monkeypatch.delenv("OKENGINE_PROJECT_URL", raising=False)
    (tmp_path / "pack.yaml").write_text(
        "name: okpack-demo\nversion: 1.2.0\ntrust: public\nproject_url: https://example.org/okengine\n")
    (tmp_path / "engine.version").write_text("engine: okengine\nversion: v0.2.0\nhermes_pin: v2026.6.19\n")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    a = m._about_info()
    assert a["vault"] == "okpack-demo" and a["vault_version"] == "1.2.0"
    assert a["engine_version"] == "v0.2.0" and a["hermes_pin"] == "v2026.6.19"
    assert a["project_url"] == "https://example.org/okengine"   # pack.yaml fallback
    monkeypatch.setenv("OKENGINE_PROJECT_URL", "https://env.example/repo")
    assert m._about_info()["project_url"] == "https://env.example/repo"   # env wins
    # the endpoint returns the about info plus a chat-availability flag
    resp = m.api_about()
    assert m._about_info().items() <= resp.items()
    assert "chat_enabled" in resp


def test_about_api_tolerates_missing_files(tmp_path):
    """A definition checkout without pack.yaml/engine.version -> empty strings, no crash."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    assert m._about_info() == {"vault": "", "vault_version": "", "engine_version": "",
                              "hermes_pin": "", "project_url": ""}


def test_display_groups_browse_by_kind(tmp_path):
    """Pack-declared display_groups let the reader browse entities BY KIND (e.g.
    'Threat actors' = intrusion-set + threat-actor) across namespaces."""
    (tmp_path / "schema.yaml").write_text(
        "display_groups:\n"
        "  Threat actors: [intrusion-set, threat-actor]\n"
        "  Malware & tooling: [malware, tool]\n")
    w = tmp_path / "wiki"
    _mk(w / "entities" / "a" / "apt42.md", "intrusion-set")
    _mk(w / "entities" / "e" / "evil-corp.md", "threat-actor")
    _mk(w / "entities" / "s" / "socgholish.md", "malware")
    _mk(w / "entities" / "c" / "cve-2024-1.md", "vulnerability")   # in no group
    m = _load(tmp_path)
    groups = {g["label"]: g["count"] for g in m.api_groups()["groups"]}
    assert groups == {"Threat actors": 2, "Malware & tooling": 1}   # order-preserving dict ok
    ta = {p["path"] for p in m.api_pages(group="Threat actors")["pages"]}
    assert ta == {"entities/a/apt42", "entities/e/evil-corp"}       # both types, across letters
    with pytest.raises(m.HTTPException) as ei:
        m.api_pages(group="Nope")
    assert ei.value.status_code == 404


def test_no_display_groups_is_empty(tmp_path):
    """A pack that declares no display_groups -> /api/groups empty (rail stays hidden)."""
    (tmp_path / "schema.yaml").write_text("exclude: []\n")
    _mk(tmp_path / "wiki" / "sources" / "s.md")
    m = _load(tmp_path)
    assert m.api_groups()["groups"] == []


def test_rail_top_section_pins_output_namespaces(tmp_path):
    """rail_top_section pins synthesized-output namespaces (briefings/predictions/…)
    to the top of the rail; only members that have pages are listed."""
    (tmp_path / "schema.yaml").write_text(
        "rail_top_section:\n  label: ANALYSIS\n  namespaces: [briefings, trends, predictions]\n")
    w = tmp_path / "wiki"
    _mk(w / "briefings" / "2026-06-19.md", "dashboard")
    _mk(w / "predictions" / "p1.md", "prediction")
    _mk(w / "entities" / "e1.md", "entity")
    # 'trends' declared but has no pages -> not listed
    m = _load(tmp_path)
    top = m.api_tree()["top_section"]
    assert top["label"] == "ANALYSIS"
    assert top["namespaces"] == ["briefings", "predictions"]   # order preserved, trends absent
    assert {d["dir"] for d in m.api_tree()["dirs"]} >= {"briefings", "predictions", "entities"}


def test_no_rail_top_section_is_empty(tmp_path):
    (tmp_path / "schema.yaml").write_text("exclude: []\n")
    _mk(tmp_path / "wiki" / "sources" / "s.md")
    m = _load(tmp_path)
    assert m.api_tree()["top_section"] == {"label": "", "namespaces": []}


def test_wikilink_resolves_canonical_over_observation(tmp_path):
    """A loose/type-prefixed wikilink to a multi-source entity must resolve to the CANONICAL
    page, not 404 because the same slug also exists under the excluded observations/ layer
    (the daily-brief broken-link bug). Exact paths to an observation still resolve."""
    (tmp_path / "schema.yaml").write_text("exclude: [wiki/observations/]\n")
    w = tmp_path / "wiki"
    _mk(w / "entities" / "s" / "sapphire-sleet.md", "intrusion-set")            # canonical
    _mk(w / "observations" / "microsoft" / "s" / "sapphire-sleet.md", "intrusion-set")  # same slug, excluded
    m = _load(tmp_path)
    cp = m._resolve_page("intrusion-set/s/sapphire-sleet")                      # wrong namespace prefix
    assert cp.name == "sapphire-sleet.md" and cp.parent.parent.name == "entities"
    cp2 = m._resolve_page("observations/microsoft/s/sapphire-sleet")           # exact -> observation still works
    assert "observations" in cp2.as_posix()


def test_rate_limit_bounded_by_default_off_public(tmp_path, monkeypatch):
    """okengine#53: the expensive-endpoint rate limit is bounded by default even off-public
    (not 0/unlimited); OKENGINE_READER_RATE=0 still explicitly disables it."""
    monkeypatch.delenv("OKENGINE_READER_PUBLIC", raising=False)
    monkeypatch.delenv("OKENGINE_READER_RATE", raising=False)
    (tmp_path / "schema.yaml").write_text("exclude: []\n")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    assert m._RATE.per_min == 300 and m._RATE.per_min > 0


def test_about_prefers_runtime_marker_over_declared_pin(tmp_path):
    """okengine#119: the About reports the ACTUAL deployed engine/Hermes (the ensure-runtime
    marker), not the pack's declared engine.version pins — which can be stale/wrong (a pack
    pinned to an older engine still deploys on a newer one, and its hermes_pin then lies)."""
    v = tmp_path
    (v / "wiki").mkdir()
    (v / "pack.yaml").write_text("name: testpack\nversion: 0.2.1\n")
    (v / "engine.version").write_text("engine: okengine\nversion: v0.3.3\nhermes_pin: v2026.6.5\n")
    m = _load(v)
    a = m._about_info()
    assert a["engine_version"] == "v0.3.3" and a["hermes_pin"] == "v2026.6.5"   # no marker -> declared pin
    (v / ".hermes-data").mkdir(exist_ok=True)
    (v / ".hermes-data" / "engine-runtime.yaml").write_text(
        "engine_release: v0.3.5\nhermes_pin: v2026.6.19\nhermes_sha: 2bd1977\nengine_sha: e8862c2\n")
    a2 = m._about_info()
    assert a2["engine_version"] == "v0.3.5" and a2["hermes_pin"] == "v2026.6.19"   # marker wins
    assert a2["vault_version"] == "0.2.1"                                          # pack version unchanged
