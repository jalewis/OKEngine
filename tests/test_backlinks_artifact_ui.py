"""Reader + cockpit serve the precomputed wiki/.backlinks.json (okengine#168):
fresh artifact wins with NO iwe subprocess; stale/corrupt/absent falls back to
the live build; mtime change reloads."""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent


def _load(app_dir: str, modname: str, tmp_path, monkeypatch):
    for extra in ("markdown", "nh3") if app_dir == "okengine-reader" else ("markdown",):
        pytest.importorskip(extra)
    app = REPO / app_dir / "app.py"
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    sys.path.insert(0, str(app.parent))
    sys.modules.pop(modname, None)
    spec = importlib.util.spec_from_file_location(modname, app)
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


def _write_artifact(tmp_path, bl, age_s=0):
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    p = wiki / ".backlinks.json"
    p.write_text(json.dumps({"version": 1, "built_at": int(time.time() - age_s),
                             "backlinks": bl}), encoding="utf-8")
    if age_s:
        os.utime(p, (time.time() - age_s, time.time() - age_s))
    return p


@pytest.mark.parametrize("app_dir,modname", [
    ("okengine-reader", "reader_app_bl"),
    ("okengine-cockpit", "cockpit_app_bl"),
])
def test_fresh_artifact_served_without_iwe(app_dir, modname, tmp_path, monkeypatch):
    _write_artifact(tmp_path, {"entities/a/acme": [{"key": "sources/x", "title": "X"}]})
    m = _load(app_dir, modname, tmp_path, monkeypatch)

    def boom():
        raise AssertionError("live iwe build must not run when the artifact is fresh")
    monkeypatch.setattr(m, "_build_backlinks", boom)
    got = m._load_backlinks(blocking=True)
    assert got["entities/a/acme"][0]["key"] == "sources/x"


@pytest.mark.parametrize("app_dir,modname", [
    ("okengine-reader", "reader_app_bl2"),
    ("okengine-cockpit", "cockpit_app_bl2"),
])
def test_stale_artifact_falls_back_to_live_build(app_dir, modname, tmp_path, monkeypatch):
    _write_artifact(tmp_path, {"a": []}, age_s=10 * 86400)   # way past the 48h ceiling
    m = _load(app_dir, modname, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_build_backlinks", lambda: {"live": [{"key": "l", "title": "L"}]})
    got = m._load_backlinks(blocking=True)
    assert "live" in got and "a" not in got


def test_corrupt_artifact_falls_back(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / ".backlinks.json").write_text("{not json", encoding="utf-8")
    m = _load("okengine-reader", "reader_app_bl3", tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_build_backlinks", lambda: {"live": []})
    assert "live" in m._load_backlinks(blocking=True)


def test_mtime_change_reloads(tmp_path, monkeypatch):
    p = _write_artifact(tmp_path, {"v1": []})
    m = _load("okengine-reader", "reader_app_bl4", tmp_path, monkeypatch)
    assert "v1" in m._load_backlinks(blocking=False)
    p.write_text(json.dumps({"version": 1, "backlinks": {"v2": []}}), encoding="utf-8")
    os.utime(p, (time.time() + 2, time.time() + 2))   # ensure a distinct mtime
    got = m._load_backlinks(blocking=False)
    assert "v2" in got and "v1" not in got
