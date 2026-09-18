"""Behavioral coverage for competitive-analytics' pack-neutral data adapter."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "extensions/okengine.competitive-analytics/comp_lib.py"


def _load(tmp_path, monkeypatch, name="competitive_comp_lib"):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location(name, MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_watchlist_precedence_parse_and_defensive_fallback(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    default = tmp_path / "config/competitive-watchlist.yaml"
    assert module.watchlist_path() == default
    assert module.read_watchlist() == {}
    default.parent.mkdir()
    default.write_text("segments: [payments]\n")
    assert module.read_watchlist() == {"segments": ["payments"]}
    default.write_text("[broken")
    assert module.read_watchlist() == {}

    legacy = tmp_path / "legacy.yaml"
    current = tmp_path / "current.yaml"
    monkeypatch.setenv("OKENGINE_COMPETITIVE_ANALYTICS_WATCHLIST_PATH", str(legacy))
    assert module.watchlist_path() == legacy
    monkeypatch.setenv("WATCHLIST_PATH", f"  {current}  ")
    assert module.watchlist_path() == current


def test_entity_lookup_direct_shard_recursive_and_missing(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch, "competitive_comp_lib_lookup")
    entities = tmp_path / "wiki/entities"
    entities.mkdir(parents=True)
    direct = entities / "vendor.md"
    direct.write_text("---\ntype: vendor\ntitle: Vendor\nupdated: 2026-08-01\n---\n- one\n")
    assert module._entity_file("vendor") == direct
    assert module.entity_summary("vendor") == {
        "slug": "vendor", "found": True, "type": "vendor", "title": "Vendor",
        "updated": "2026-08-01", "activity": ["one"],
    }

    direct.unlink()
    shard = entities / "v/vendor.md"
    shard.parent.mkdir()
    shard.write_text("---\ntype: vendor\nlast_updated: 2026-07-01\n---\n* two\n")
    assert module._entity_file("vendor") == shard
    summary = module.entity_summary("vendor")
    assert summary["slug"] == "v/vendor" and summary["title"] == "vendor"
    assert summary["updated"] == "2026-07-01" and summary["activity"] == ["two"]

    shard.unlink()
    nested = entities / "x/y/vendor.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("no frontmatter\n- " + "x" * 150 + "\n- b\n")
    assert module._entity_file("aliases/vendor") == nested
    summary = module.entity_summary("aliases/vendor", max_activity=1)
    assert summary["type"] is None and len(summary["activity"][0]) == 120
    nested.write_text("---\n[invalid\n---\nbody\n")
    assert module.entity_summary("vendor")["type"] is None

    nested.unlink()
    assert module._entity_file("vendor") is None
    assert module.entity_summary("vendor") == {"slug": "vendor", "found": False}
    empty = _load(tmp_path / "empty", monkeypatch, "competitive_comp_lib_no_entity_dir")
    assert empty._entity_file("vendor") is None
