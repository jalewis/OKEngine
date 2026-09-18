import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, app_path: Path, vault: Path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(app_path.parent))
    spec = importlib.util.spec_from_file_location(name, app_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("surface", ["okengine-reader", "okengine-cockpit"])
def test_surface_serves_linked_favicon(surface, tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("{}\n", encoding="utf-8")
    app_path = REPO / surface / "app.py"
    module = _load(f"favicon_{surface.replace('-', '_')}", app_path, tmp_path, monkeypatch)

    with TestClient(module.app) as client:
        icon = client.get("/favicon.ico")
        shell = client.get("/")

    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.headers["cache-control"] == "public, max-age=86400"
    assert b"<svg" in icon.content
    assert '<link rel="icon" href="/favicon.ico" type="image/svg+xml">' in shell.text


def test_reader_and_cockpit_use_same_brand_mark():
    reader = (REPO / "okengine-reader/static/favicon.svg").read_bytes()
    cockpit = (REPO / "okengine-cockpit/static/favicon.svg").read_bytes()
    assert reader == cockpit
