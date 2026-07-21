"""Reader and Cockpit startup work uses FastAPI lifespan, not deprecated events."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("nh3")

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, directory: Path):
    sys.path.insert(0, str(directory))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, directory / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _enter_lifespan(module):
    async def run():
        async with module._lifespan(module.app):
            pass
    asyncio.run(run())


def test_reader_lifespan_starts_both_cache_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    module = _load("reader_lifespan_app", REPO / "okengine-reader")
    calls = []
    monkeypatch.setattr(module, "_start_warmer", lambda: calls.append("warmer"))
    monkeypatch.setattr(module, "_prewarm_backlinks", lambda: calls.append("backlinks"))
    _enter_lifespan(module)
    assert calls == ["warmer", "backlinks"]


def test_cockpit_lifespan_starts_cache_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    module = _load("cockpit_lifespan_app", REPO / "okengine-cockpit")
    calls = []
    monkeypatch.setattr(module, "_warm_initial_tab_datasets", lambda: calls.append("landing-tab"))
    monkeypatch.setattr(module.threading, "Thread", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("lifespan must not start whole-vault background scans")))
    _enter_lifespan(module)
    assert calls == ["landing-tab"]


def test_supported_apps_declare_no_deprecated_event_hooks():
    for app in (REPO / "okengine-reader" / "app.py", REPO / "okengine-cockpit" / "app.py"):
        assert ".on_event(" not in app.read_text(encoding="utf-8")
