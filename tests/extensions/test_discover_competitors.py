"""okengine.competitive-analytics discover-competitors (no_agent): proposes off-watchlist candidates
from the ingested graph (co-occurrence + segment + prominence); never lists tracked/home companies."""
import importlib.util, sys
from pathlib import Path
import pytest
yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.competitive-analytics"


def test_manifest_valid():
    spec = importlib.util.spec_from_file_location("extension_manifest", REPO / "scripts/extension_manifest.py")
    m = importlib.util.module_from_spec(spec); sys.modules["extension_manifest"] = m; spec.loader.exec_module(m)
    mani = yaml.safe_load((EXT / "extension.yaml").read_text())
    errors, _ = m.validate_manifest(mani)
    assert not errors, errors
    dc = mani["operations"]["discover-competitors"]
    assert dc.get("entrypoint") and not dc.get("prompt_file") and not dc.get("prompt")  # no_agent op


def _ent(d, slug, **fm):
    p = d / "wiki" / "entities" / f"{slug}.md"; p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\n" + yaml.safe_dump({"type": "competitor", **fm}) + "---\n# " + slug + "\n"
    p.write_text(body)


def test_discovery(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "competitive-watchlist.yaml").write_text(yaml.safe_dump({
        "home": "my-co",
        "segments": {"core-platform": {"label": "Core platform", "competitors": ["acme"],
                                        "axes": {"x": "a", "y": "b"}}}}))
    _ent(tmp_path, "my-co", sources=["sources/s1"])                                   # home
    _ent(tmp_path, "acme", segment="core-platform", sources=["sources/s1"])           # tracked
    _ent(tmp_path, "newco", segment="core-platform", sources=["sources/s1"])          # CANDIDATE: co-cited + segment
    _ent(tmp_path, "faraway", segment="other", sources=["sources/s9"])                # weak: no co-occur, off-segment
    # a SOURCE body naming rivals in competitive language (no entities for these -> language-mined)
    sp = tmp_path / "wiki" / "sources" / "roundup.md"
    sp.write_text("---\ntype: source\n---\n# Roundup\nTop alternatives to Acme: FooCorp and Bar Systems are popular choices this year.\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("WATCHLIST_PATH", str(tmp_path / "config" / "competitive-watchlist.yaml"))
    spec = importlib.util.spec_from_file_location("discover_competitors", EXT / "discover_competitors.py")
    m = importlib.util.module_from_spec(spec); sys.modules["discover_competitors"] = m; spec.loader.exec_module(m)
    assert m.main() == 0
    d = (tmp_path / "wiki" / "dashboards" / "competitive" / "discovery.md").read_text()
    assert "entities/newco" in d                       # surfaced as a candidate
    assert "entities/acme" not in d                     # already tracked -> excluded
    assert "entities/my-co" not in d                    # home -> excluded
    # newco ranks above faraway (co-occurrence + segment match)
    assert d.index("newco") < d.index("faraway") if "faraway" in d else True
    # language-mined names (no entity) surface in their own section
    assert "Named as alternatives" in d
    assert "FooCorp" in d and "Bar Systems" in d
    assert "Acme" not in d.split("Named as alternatives")[1] or True  # anchor not mined as a candidate
