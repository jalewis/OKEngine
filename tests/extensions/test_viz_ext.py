"""okengine.viz (okengine#156): Wardley map generator — config axes + graph-ubiquity fallback,
self-declared two-axis panel, no schema fragment."""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.viz"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def test_manifest_valid():
    man = _load("extension_manifest", REPO / "scripts/extension_manifest.py")
    m = yaml.safe_load((EXT / "extension.yaml").read_text())
    errors, _ = man.validate_manifest(m)
    assert not errors, errors
    # since #172 viz ships a fragment: extends core `concept` with the axis fields
    assert m["schema"] == ["schema/viz.schema.yaml"]
    assert m["operations"]["wardley-refresh"].get("prompt_file") is None   # no_agent
    assert m["operations"]["concept-enrich"].get("prompt_file") is None    # no_agent drain


def _concept(d, slug, evolution=None):
    p = d / slug[0] / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntype: concept\ntitle: {slug.title()}\n"
    if evolution:
        fm += f"evolution: {evolution}\n"
    p.write_text(fm + "---\n# c\n")


def test_map_heuristic_and_field(tmp_path, monkeypatch):
    w = tmp_path / "wiki"
    c = w / "concepts"
    _concept(c, "popular")          # will be referenced a lot -> high ubiquity x
    _concept(c, "niche")            # referenced once
    _concept(c, "graded", evolution="commodity")   # explicit field -> x ~0.87
    # referrers: many pages cite [[concepts/popular]]
    ent = w / "entities" / "a"
    ent.mkdir(parents=True)
    for i in range(5):
        # alternate flat and letter-sharded link forms — both must count (48k/2.8k split in a live vault)
        link = "[[concepts/p/popular]]" if i % 2 else "[[concepts/popular]]"
        (ent / f"e{i}.md").write_text(f"---\ntype: entity\n---\nsee {link}\n")
    (ent / "n.md").write_text("---\ntype: entity\n---\n[[concepts/niche]]\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    viz = _load("build_wardley_map", EXT / "build_wardley_map.py")
    assert viz.main() == 0
    dash = (w / "dashboards" / "wardley.md").read_text()
    assert "panel:" in dash and "two-axis" in dash            # self-declared panel
    assert "popular" in dash and "graded" in dash
    assert "HEURISTIC" in dash                                 # popular/niche lack the field
    # graded has the explicit commodity field -> x 0.87
    assert "0.87" in dash


def test_anchor_scopes_to_neighborhood(tmp_path, monkeypatch):
    """VIZ_ANCHOR: the map keeps only concepts the anchor links directly + concepts linked from
    entities the anchor links (1 hop); global hub concepts outside that neighborhood drop out."""
    w = tmp_path / "wiki"
    c = w / "concepts"
    _concept(c, "market-core")      # linked directly by the watchlist
    _concept(c, "rival-tech")       # linked by a watchlist entity (1 hop)
    _concept(c, "global-hub")       # heavily referenced but OUTSIDE the neighborhood
    # concept→concept dependency: market-core builds on rival-tech (a value-chain edge)
    (c / "m" / "market-core.md").write_text(
        "---\ntype: concept\ntitle: Market-Core\n---\nbuilds on [[concepts/r/rival-tech]]\n")
    ent = w / "entities" / "vendor" / "r"
    ent.mkdir(parents=True)
    (ent / "rivalcorp.md").write_text("---\ntype: entity\n---\nships [[concepts/rival-tech]]\n")
    for i in range(9):              # global-hub out-references everything in scope
        d = w / "sources" / f"s{i}.md"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text("---\ntype: source\n---\n[[concepts/global-hub]]\n")
    wl = c / "w" / "watchlist.md"
    wl.parent.mkdir(parents=True, exist_ok=True)
    wl.write_text("---\ntype: concept\ntitle: Watchlist\n---\n"
                  "track [[entities/vendor/r/rivalcorp|RivalCorp]] and [[concepts/market-core]]\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("VIZ_ANCHOR", "concepts/w/watchlist.md")
    viz = _load("build_wardley_map_anchored", EXT / "build_wardley_map.py")
    assert viz.main() == 0
    dash = (w / "dashboards" / "wardley.md").read_text()
    assert "market-core" in dash and "rival-tech" in dash      # direct + 1-hop in scope
    assert "global-hub" not in dash                            # hub outside the neighborhood: gone
    assert "Scoped to the neighborhood" in dash                # provenance line
    # the anchor page itself is meta, not a node
    assert "[[concepts/watchlist" not in dash
    # Wardley fidelity: evolution stage bands + the concept→concept value-chain edge
    import json as _json
    panel = _json.loads(next(l for l in dash.splitlines() if l.startswith("panel: "))[7:])
    assert [b["label"] for b in panel["x_bands"]] == ["Genesis", "Custom", "Product", "Commodity"]
    assert ["market-core", "rival-tech"] in panel["edges"]
    # the chart is server-rendered INTO the body (origin-system pattern): shows anywhere md renders
    assert "<!-- panel-svg" in dash and "<svg " in dash and "Genesis" in dash


def test_wardley_defensive_helpers_missing_anchor_scan_race_and_cap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "missing"))
    empty = _load("build_wardley_map_missing", EXT / "build_wardley_map.py")
    assert empty.main() == 0
    assert "no concepts" in capsys.readouterr().out
    assert empty._fm(tmp_path / "missing.md") == {}
    assert empty._num("150%") == 1.0 and empty._num("-1") == 0.0
    assert empty._num("bad") is None
    assert empty._pctile({}) == {} and empty._pctile({"one": 4}) == {"one": .5}

    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    concepts = wiki / "concepts"
    for slug in ("alpha", "beta", "gamma"):
        _concept(concepts, slug)
    (concepts / "_hidden.md").write_text("---\ntype: concept\n---\n")
    sources = wiki / "sources"
    sources.mkdir()
    raced = sources / "raced.md"
    raced.write_text("[[concepts/alpha]]")
    (sources / "unknown.md").write_text("[[concepts/ghost]]")
    (sources / "INDEX.md").write_text("[[concepts/beta]]")
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("vanished"))
        if path == raced else original(path, *args, **kwargs),
    )
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("VIZ_ANCHOR", "dashboards/missing.md")
    monkeypatch.setenv("WARDLEY_MAX_NODES", "1")
    module = _load("build_wardley_map_edges", EXT / "build_wardley_map.py")
    assert module.main() == 0
    out = capsys.readouterr().out
    assert "anchor not found" in out and "capped to top 1" in out


def test_panel_svg_upsert_idempotent():
    ps = _load("panel_svg_vt", EXT / "panel_svg.py")
    panel = {"kind": "two-axis", "x_label": "X", "y_label": "Y",
             "x_bands": [{"label": "L", "from": 0, "to": 0.5}, {"label": "R", "from": 0.5, "to": 1}],
             "edges": [["a", "b"]],
             "nodes": [{"label": "A", "slug": "a", "x": 0.2, "y": 0.8},
                       {"label": "B", "slug": "b", "x": 0.7, "y": 0.3}]}
    body = "# Title\nprose\n"
    once = ps.upsert_block(body, panel)
    assert "<svg " in once and once.index("# Title") < once.index("<svg ")
    assert ps.upsert_block(once, panel) is None            # same data -> untouched
    panel["nodes"][0]["x"] = 0.25
    twice = ps.upsert_block(once, panel)                   # changed data -> replaced, not duplicated
    assert twice.count("<svg ") == 1 and "prose" in twice


def test_panel_svg_overlap_and_lane_guard_edges(monkeypatch):
    ps = _load("panel_svg_overlap", EXT / "panel_svg.py")
    panel = {
        "kind": "two-axis",
        "nodes": [
            {"label": "A", "slug": "a", "x": .5, "y": .5},
            {"label": "B", "slug": "b", "x": .5, "y": .5},
        ],
    }
    rendered = ps.render_panel_svg(panel)
    assert rendered and "stroke-width=\"0.6\"" in rendered
    assert ps.svg_block({"kind": "bar-chart"}) is None
    assert ps.upsert_block("# body", {"kind": "bar-chart"}) is None
    monkeypatch.setattr(ps, "render_panel_svg", lambda panel: (_ for _ in ()).throw(RuntimeError()))
    assert ps.svg_block(panel) is None


def test_render_panel_svgs_drain(tmp_path, monkeypatch):
    d = tmp_path / "wiki" / "dashboards" / "competitive"
    d.mkdir(parents=True)
    (d / "quadrant-x.md").write_text(
        "---\ntype: dashboard\ntitle: Q\npanel:\n  kind: two-axis\n  x_label: X\n  y_label: Y\n"
        "  nodes:\n    - {label: A, slug: a, x: 0.5, y: 0.5}\n---\n# Q\nprose\n")
    (d / "plain.md").write_text("---\ntype: dashboard\ntitle: P\n---\n# P\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    r = _load("render_panel_svgs_vt", EXT / "render_panel_svgs.py")
    assert r.main() == 0
    q = (d / "quadrant-x.md").read_text()
    assert "<svg " in q and "prose" in q
    assert "<svg" not in (d / "plain.md").read_text()      # no panel -> untouched
    assert r.main() == 0                                   # second run: all current
    assert (d / "quadrant-x.md").read_text() == q


def test_render_panel_svgs_defensive_scan_edges(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    empty = _load("render_panel_svgs_empty", EXT / "render_panel_svgs.py")
    assert empty.main() == 0
    assert "no dashboards" in capsys.readouterr().out

    d = tmp_path / "wiki/dashboards"
    d.mkdir(parents=True)
    fixtures = {
        "_private.md": "ignored",
        "INDEX.md": "ignored",
        "plain.md": "body without frontmatter",
        "bad-yaml.md": "---\n[broken\n---\nbody",
        "wrong-panel.md": "---\npanel: string\n---\nbody",
        "wrong-kind.md": "---\npanel: {kind: table}\n---\nbody",
        "moved.md": "---\npanel: {kind: two-axis}\n---\nbody",
    }
    for name, content in fixtures.items():
        (d / name).write_text(content)
    module = _load("render_panel_svgs_edges", EXT / "render_panel_svgs.py")
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "moved.md":
            raise OSError("removed during scan")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert module.main() == 0
    assert "0 rendered, 0 already current" in capsys.readouterr().out
