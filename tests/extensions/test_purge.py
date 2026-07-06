"""Regression tests for `extensions purge` (okengine#127).

Purge deletes an extension's produced pages by the extension_id provenance stamp
(#132). It is destructive: disabled-required, dry-run unless --yes, and never touches
pages it didn't stamp.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
COMP = REPO / "scripts" / "extension_compose.py"
DISC = REPO / "scripts" / "extension_discovery.py"
CLI = REPO / "scripts" / "framework_extensions.py"

pytestmark = pytest.mark.skipif(not COMP.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _page(pack, rel, ext_id=None):
    p = pack / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "watchlist", "id": "watchlist:" + p.stem}
    if ext_id:
        fm["extension_id"] = ext_id
    p.write_text("---\n" + yaml.safe_dump(fm) + "---\n# " + p.stem + "\n", encoding="utf-8")
    return p


def _vault(tmp_path):
    pack = tmp_path / "pack"
    _page(pack, "watchlists/w1.md", "demo.x")
    _page(pack, "watchlists/w2.md", "demo.x")
    _page(pack, "predictions/p1.md", "demo.y")     # different extension
    _page(pack, "entities/e1.md", None)            # not extension-owned
    return pack


def test_purge_targets_by_stamp(tmp_path):
    comp = _load("extension_compose", COMP)
    pack = _vault(tmp_path)
    assert comp.purge_targets(pack, "demo.x") == ["watchlists/w1.md", "watchlists/w2.md"]
    assert comp.purge_targets(pack, "demo.y") == ["predictions/p1.md"]
    assert comp.purge_targets(pack, "demo.none") == []


def test_cli_purge_refuses_while_enabled(tmp_path):
    cli = _load("framework_extensions", CLI)
    disc = _load("extension_discovery", DISC)
    pack = _vault(tmp_path)
    disc.set_enabled(pack, "demo.x", True)
    assert cli.main(["purge", str(pack), "demo.x"]) == 1     # enabled -> refused
    assert (pack / "wiki" / "watchlists" / "w1.md").is_file()  # nothing deleted


def test_cli_purge_dry_run_then_yes(tmp_path):
    cli = _load("framework_extensions", CLI)
    pack = _vault(tmp_path)                                  # demo.x not enabled
    # dry-run: lists, deletes nothing
    assert cli.main(["purge", str(pack), "demo.x"]) == 0
    assert (pack / "wiki" / "watchlists" / "w1.md").is_file()
    # --yes: deletes only demo.x's pages
    assert cli.main(["purge", str(pack), "demo.x", "--yes"]) == 0
    assert not (pack / "wiki" / "watchlists" / "w1.md").exists()
    assert not (pack / "wiki" / "watchlists" / "w2.md").exists()
    assert (pack / "wiki" / "predictions" / "p1.md").is_file()   # demo.y untouched
    assert (pack / "wiki" / "entities" / "e1.md").is_file()      # unstamped untouched


def test_cli_purge_nothing_to_do(tmp_path):
    cli = _load("framework_extensions", CLI)
    pack = _vault(tmp_path)
    assert cli.main(["purge", str(pack), "demo.absent", "--yes"]) == 0
