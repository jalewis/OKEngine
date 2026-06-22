"""Regression: framework pull source resolution + catalog read (offline)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FP = REPO / "scripts" / "framework_pull.py"

_CAT = {"catalog": "okpacks-library", "packs": [
    {"name": "okpack-foo", "repo": "jalewis/okpacks-library",
     "subdir": "packs/okpack-foo", "ref": "main", "engine_version": "v0.2.0"}]}


def _load():
    spec = importlib.util.spec_from_file_location("framework_pull", FP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_pull"] = m
    spec.loader.exec_module(m)
    return m


def test_resolve_explicit_url():
    m = _load()
    spec, curated = m.resolve("https://github.com/o/r.git", None)
    assert spec["giturl"] == "https://github.com/o/r.git"
    assert spec["subdir"] == "" and spec["name"] == "r" and curated is False


def test_engine_version_falls_back_to_manifest_when_no_tag(monkeypatch):
    """A no-history public snapshot has no git tag, so `git describe` yields nothing — the version
    must then come from engine-manifest.yaml, not the v0.0.0 placeholder (okengine#96)."""
    m = _load()
    monkeypatch.setattr(m.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())   # simulate no tag
    v = m.engine_version()
    assert v != "v0.0.0", "must fall back to the manifest, not the placeholder"
    assert v == m._engine_release_from_manifest() and v.startswith("v")


def test_resolve_library_shorthand():
    m = _load()
    spec, curated = m.resolve("okpacks-library:okpack-bar", None)
    assert spec["subdir"] == "packs/okpack-bar" and spec["name"] == "okpack-bar"
    assert "okpacks-library" in spec["giturl"] and curated is False


def test_resolve_owner_repo():
    m = _load()
    spec, curated = m.resolve("acme/okpack-x", None)
    assert "acme/okpack-x.git" in spec["giturl"] and spec["subdir"] == "" and curated is False


def test_resolve_owner_repo_subdir():
    m = _load()
    spec, curated = m.resolve("acme/mono:packs/okpack-y", None)
    assert spec["subdir"] == "packs/okpack-y" and spec["name"] == "okpack-y"
    assert "acme/mono.git" in spec["giturl"]


def test_resolve_catalog_name_is_curated():
    m = _load()
    spec, curated = m.resolve("okpack-foo", _CAT)
    assert curated is True and spec["subdir"] == "packs/okpack-foo" and spec["ref"] == "main"
    assert "jalewis/okpacks-library.git" in spec["giturl"]


def test_resolve_unknown_name_errors():
    m = _load()
    with pytest.raises(SystemExit):
        m.resolve("nonsuch-pack", _CAT)


def test_read_catalog_local(tmp_path):
    m = _load()
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(_CAT))
    cat, err = m.read_catalog(str(p))
    assert err is None and cat["catalog"] == "okpacks-library"


def test_read_catalog_diagnostics(tmp_path):
    """A failure names what went wrong (issue #8) — missing file, bad JSON, shape."""
    m = _load()
    cat, err = m.read_catalog(str(tmp_path / "missing.json"))
    assert cat is None and "not found" in err
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    cat, err = m.read_catalog(str(bad))
    assert cat is None and "not valid JSON" in err
    wrong = tmp_path / "wrong.json"; wrong.write_text('{"no":"packs"}')
    cat, err = m.read_catalog(str(wrong))
    assert cat is None and "wrong shape" in err


def test_unknown_name_error_mentions_catalog_failure():
    """A bare name that can't resolve because the catalog is unreadable explains it
    and points at the fallbacks."""
    m = _load()
    with pytest.raises(SystemExit) as e:
        m.resolve("okpack-foo", None, "HTTP 404 reading … (not found — private repo)")
    msg = str(e.value)
    assert "could not be read" in msg and "owner/repo" in msg


def test_apply_port_offset_rewrites_compose_and_config(tmp_path):
    """--port-offset shifts the published host ports and the gateway's MCP url."""
    m = _load()
    (tmp_path / ".hermes-data").mkdir()
    (tmp_path / "docker-compose.yml").write_text(
        'reader:\n  ports: ["${OKENGINE_BIND:-127.0.0.1}:9200:9200"]\n'
        'mcp:\n  ports: ["${OKENGINE_BIND:-127.0.0.1}:8730:8730"]\n'
        '  environment: [PORT=8730]\n')
    (tmp_path / ".hermes-data" / "config.yaml").write_text(
        "mcp_servers:\n  okengine:\n    url: http://localhost:8730/mcp\n")
    m._apply_port_offset(tmp_path, 100)
    compose = (tmp_path / "docker-compose.yml").read_text()
    assert ":9300:9200" in compose and ":8830:8730" in compose
    assert "PORT=8730" in compose                      # internal port untouched
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert "localhost:8830/mcp" in cfg


def test_apply_port_offset_zero_is_noop(tmp_path):
    m = _load()
    (tmp_path / "docker-compose.yml").write_text('ports: ["127.0.0.1:9200:9200"]\n')
    m._apply_port_offset(tmp_path, 0)
    assert ":9200:9200" in (tmp_path / "docker-compose.yml").read_text()


def test_resolve_offset_pack_declared_vs_flag(tmp_path):
    """Effective port offset: --port-offset wins; else the pack's declared
    pack.yaml port_offset; else 0 (#30)."""
    m = _load()
    (tmp_path / "pack.yaml").write_text("name: p\nversion: 1.0.0\ntrust: public\n"
                                        "owns: {types: [t]}\nport_offset: 100\n")
    assert m._resolve_offset(None, tmp_path) == (100, "pack.yaml")   # declared default
    assert m._resolve_offset(50, tmp_path) == (50, "--port-offset")  # flag overrides
    assert m._resolve_offset(0, tmp_path) == (0, "--port-offset")    # explicit 0 forces none
    # no pack.yaml / no declaration -> 0
    assert m._resolve_offset(None, tmp_path / "missing") == (0, "")


def _w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_update_in_place_preserves_config_and_flags_changes(tmp_path):
    """--update: new files copied in, changed definition files -> .upstream (operator's
    untouched), and runtime/content (.env, .hermes-data, raw, wiki) never touched."""
    m = _load()
    up, dest = tmp_path / "up", tmp_path / "dest"
    # upstream definition
    _w(up / "schema.yaml", "types: {a: {}, b: {}}\n")            # changed vs operator
    _w(up / "CLAUDE.md", "upstream persona v2\n")                # changed
    _w(up / "README.md", "same readme\n")                       # identical -> unchanged
    _w(up / "crons" / "scripts" / "new.py", "print('new')\n")    # new file
    _w(up / "wiki" / "index.md", "UPSTREAM EMPTY SCAFFOLD\n")     # preserved tree -> ignored
    # operator's deployed pack
    _w(dest / "pack.yaml", "name: p\n")
    _w(dest / "schema.yaml", "types: {a: {}}\n")                 # operator edit
    _w(dest / "CLAUDE.md", "MY persona edits\n")
    _w(dest / "README.md", "same readme\n")
    _w(dest / ".env", "OPENROUTER_API_KEY=secret\n")
    _w(dest / ".hermes-data" / "config.yaml", "model: {default: mine}\n")
    _w(dest / "wiki" / "my-page.md", "MY CONTENT\n")
    _w(dest / "feeds.opml.upstream", "stale\n")                 # stale from a prior run

    s = m._update_in_place(up, dest)

    # new file added
    assert (dest / "crons" / "scripts" / "new.py").is_file()
    assert "crons/scripts/new.py" in s["added"]
    # changed dual-owned -> .upstream, operator's file NOT overwritten
    assert (dest / "schema.yaml.upstream").read_text() == "types: {a: {}, b: {}}\n"
    assert (dest / "schema.yaml").read_text() == "types: {a: {}}\n"
    assert set(s["changed"]) == {"schema.yaml", "CLAUDE.md"}
    # runtime + content preserved; upstream wiki NOT copied
    assert (dest / ".env").read_text() == "OPENROUTER_API_KEY=secret\n"
    assert (dest / ".hermes-data" / "config.yaml").read_text() == "model: {default: mine}\n"
    assert (dest / "wiki" / "my-page.md").read_text() == "MY CONTENT\n"
    assert not (dest / "wiki" / "index.md").exists()
    # stale .upstream cleared (README unchanged so no new one written)
    assert not (dest / "feeds.opml.upstream").exists()
    assert not (dest / "README.md.upstream").exists()
    assert s["unchanged"] >= 1
