"""Schema-drift queue must use the governing composed taxonomy and explain real drift (#200)."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.path.insert(0, str(CRON))
    for name in ("lint_watcher", "schema_lib"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("lint_watcher", CRON / "lint_watcher.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lint_watcher"] = module
    spec.loader.exec_module(module)
    return module


def _page(vault: Path, rel: str, typ: str):
    path = vault / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: {typ}\n---\nbody\n", encoding="utf-8")


def test_composed_and_walkup_types_are_not_drift_but_aliases_remain_drainable(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "types:\n  root-item: {required: [type]}\n", encoding="utf-8")
    composed = tmp_path / ".okengine" / "composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text(
        "types:\n"
        "  root-item: {required: [type]}\n"
        "  extension-item: {required: [type]}\n"
        "type_aliases:\n"
        "  old-item: root-item\n",
        encoding="utf-8",
    )
    sub = tmp_path / "wiki" / "guest"
    sub.mkdir(parents=True)
    (sub / "schema.yaml").write_text(
        "types:\n  guest-item: {required: [type]}\n", encoding="utf-8")
    _page(tmp_path, "items/root.md", "root-item")
    _page(tmp_path, "items/ext.md", "extension-item")
    _page(tmp_path, "items/alias.md", "old-item")
    _page(tmp_path, "guest/items/guest.md", "guest-item")
    _page(tmp_path, "items/bad.md", "invented-item")

    details = {}
    queues = _load(tmp_path).scan_queues(details)

    assert queues["schema-drift"] == 2
    assert details["schema-drift"]["by_type"] == {
        "invented-item": 1,
        "old-item": 1,
    }
    assert details["schema-drift"]["alias_targets"] == {"old-item": ["root-item"]}
    assert details["schema-drift"]["examples"] == {
        "invented-item": ["items/bad.md"],
        "old-item": ["items/alias.md"],
    }


def test_report_renders_unknown_type_counts_and_samples(tmp_path):
    module = _load(tmp_path)
    report = module.write_today_report(
        "2026-07-16",
        {"schema-drift": 2},
        {"schema-drift": 1},
        ["`schema-drift` grew"],
        {"schema-drift": {
            "by_type": {"invented": 2},
            "examples": {"invented": ["entities/a.md", "entities/b.md"]},
            "alias_targets": {},
        }},
    )
    text = report.read_text(encoding="utf-8")
    assert "Schema drift detail" in text
    assert (
        "| `invented` | 2 | unknown — classify or add an explicit alias | "
        "`entities/a.md`, `entities/b.md` |"
    ) in text
    assert "governing composed schema" in text
