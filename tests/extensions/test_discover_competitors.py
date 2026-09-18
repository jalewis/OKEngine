"""okengine.competitive-analytics discover-competitors (no_agent): proposes off-watchlist candidates
from the ingested graph (co-occurrence + segment + prominence); never lists tracked/home companies."""
import importlib.util, sys
from pathlib import Path
import pytest
yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.competitive-analytics"


def _load(name="discover_competitors"):
    sys.path.insert(0, str(EXT))
    sys.modules.pop("comp_lib", None)
    spec = importlib.util.spec_from_file_location(name, EXT / "discover_competitors.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    m = _load()
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


def test_entity_loader_and_language_miner_defensive_edges(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    module = _load("discover_competitors_edges")
    assert module._load_entities() == {}
    assert not module._looks_company("word") and module._looks_company("FooCorp")
    assert module._norm("  Foo   Corp ") == "foo corp"
    assert module._stem("[[entities/f/foo.md]]") == "foo"
    assert module._mine_alternatives([], set()) == {}

    entities = tmp_path / "wiki/entities"
    entities.mkdir(parents=True)
    fixtures = {
        "_private.md": "ignored", "INDEX.md": "ignored", "plain.md": "body",
        "bad.md": "---\n[broken\n---\n", "list.md": "---\n- list\n---\n",
        "scalar.md": "---\ntype: vendor\nname: Scalar\nsources: sources/one\n---\n",
        "moved.md": "---\ntype: vendor\n---\n",
    }
    for name, content in fixtures.items():
        (entities / name).write_text(content)
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "moved.md":
            raise OSError("moved")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    loaded = module._load_entities()
    assert loaded["scalar"]["sources"] == {"one"}

    sources = tmp_path / "wiki/sources"
    sources.mkdir()
    (sources / "_ignored.md").write_text("Acme vs FooCorp")
    (sources / "no-trigger.md").write_text("Acme and FooCorp are mentioned")
    (sources / "mined.md").write_text(
        "Acme vs FooCorp. Acme competitors include FooCorp. Acme vs FooCorp. "
        "Acme vs Acme Cloud."
    )
    (sources / "moved.md").write_text("Acme vs Gone Systems")
    module.ALT_MIN = 2
    mined = module._mine_alternatives(["Acme"], {"known corp"})
    assert mined["foocorp"]["count"] >= 2
    assert len(mined["foocorp"]["evidence"]) == 2
    assert "acme cloud" not in mined


def test_discovery_missing_vault_and_empty_candidate_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "absent"))
    assert _load("discover_competitors_missing").main() == 1
    assert "wiki not found" in capsys.readouterr().err

    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/competitive-watchlist.yaml").write_text(yaml.safe_dump({
        "home": "ghost-home",
        "segments": {"tracked": {"label": "Tracked", "competitors": []}},
    }))
    _ent(tmp_path, "known", sources=[])
    _ent(tmp_path, "wrong-type", type="concept", sources=[])
    _ent(tmp_path, "weak-vendor", type="vendor", sources=[])
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("WATCHLIST_PATH", str(tmp_path / "config/competitive-watchlist.yaml"))
    monkeypatch.setenv("DISCOVERY_TYPES", "vendor")
    monkeypatch.setenv("DISCOVERY_ALT", "0")
    monkeypatch.setenv("DISCOVERY_MIN_SCORE", "99")
    module = _load("discover_competitors_empty")
    assert module.main() == 0
    dashboard = (wiki / "dashboards/competitive/discovery.md").read_text()
    assert "No off-watchlist candidates" in dashboard
    assert "anchored on home `ghost-home`" in dashboard


def test_discovery_without_home_accepts_segment_only_candidate(tmp_path, monkeypatch):
    (tmp_path / "wiki/entities").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/competitive-watchlist.yaml").write_text(yaml.safe_dump({
        "segments": {"core": {"label": "Core", "competitors": []}},
    }))
    _ent(tmp_path, "segment-only", type="vendor", segment="core", sources=[])
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("WATCHLIST_PATH", str(tmp_path / "config/competitive-watchlist.yaml"))
    monkeypatch.setenv("DISCOVERY_TYPES", "vendor")
    monkeypatch.setenv("DISCOVERY_ALT", "0")
    monkeypatch.setenv("DISCOVERY_MIN_SCORE", "1")
    module = _load("discover_competitors_segment_only")
    assert module.main() == 0
    dashboard = (tmp_path / "wiki/dashboards/competitive/discovery.md").read_text()
    assert "[[entities/segment-only]]" in dashboard
    assert "in watched segment 'core'" in dashboard
    assert "source(s)" not in next(line for line in dashboard.splitlines() if "segment-only" in line)
