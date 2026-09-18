"""Disposable v0.18.2 -> v0.21.3 state migration and rollback contract."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


OLD_SHA = "9de9c25f620ff7f1ce0fd5457d596052d5159596"
TARGET_SHA = "345cd2b057a452236de401d3534b8502a7465e8d"

_CHILD = r'''
import json
import sys
from pathlib import Path
from hermes_state import SessionDB

phase, name = sys.argv[1:]
db = SessionDB(Path(name))
try:
    if phase == "seed":
        db.create_session("fixture-cron", "cron")
        db.create_session("fixture-delegate", "subagent")
        db.append_message("fixture-cron", "user", "fixture-old-cron")
        db.append_message("fixture-delegate", "tool", "fixture-old-tool", tool_name="read_file")
    else:
        cron = db.get_messages("fixture-cron")
        delegate = db.get_messages("fixture-delegate")
        assert any(m["content"] == "fixture-old-cron" for m in cron)
        assert any(m["content"] == "fixture-old-tool" for m in delegate)
        if phase == "upgrade":
            db.append_message("fixture-cron", "assistant", "fixture-target-write")
        elif phase == "old_probe":
            assert any(m["content"] == "fixture-target-write" for m in cron)
            db.append_message("fixture-cron", "assistant", "fixture-old-rollback-write")
        elif phase == "target_roundtrip":
            assert any(m["content"] == "fixture-target-write" for m in cron)
            assert any(m["content"] == "fixture-old-rollback-write" for m in cron)
        elif phase == "restore_probe":
            assert not any(m["content"] == "fixture-target-write" for m in cron)
            db.append_message("fixture-cron", "assistant", "fixture-restored-write")
        else:
            raise AssertionError("unknown migration phase")
    print(json.dumps({"phase": phase, "cron_messages": len(db.get_messages("fixture-cron")),
                      "delegate_messages": len(db.get_messages("fixture-delegate"))}))
finally:
    db.close()
'''


def _schema(path: Path) -> tuple[int, str | None]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        version = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0]
        try:
            row = connection.execute(
                "SELECT value FROM state_meta WHERE key='fts_storage_version'").fetchone()
        except sqlite3.OperationalError:
            row = None
    return version, row[0] if row else None


def _backup(source: Path, target: Path) -> None:
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old:
        with sqlite3.connect(target) as copy:
            old.backup(copy)


def _run(source: Path, phase: str, db: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source)
    env["HERMES_HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, phase, str(db)],
        cwd=source, env=env, text=True, capture_output=True, timeout=90, check=False,
    )
    return result


def _require_phase(source: Path, phase: str, db: Path, home: Path) -> dict:
    result = _run(source, phase, db, home)
    assert result.returncode == 0, (
        f"{phase} failed in pinned source {source.name}: "
        f"{result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_v19_to_v30_preserves_sessions_and_snapshot_restore(tmp_path):
    old = Path(os.environ["OKENGINE_HERMES_OLD_SOURCE"]).resolve()
    target = Path.cwd().resolve()
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=old, text=True,
                          capture_output=True, check=True).stdout.strip() == OLD_SHA
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=target, text=True,
                          capture_output=True, check=True).stdout.strip() == TARGET_SHA

    home = tmp_path / "hermes-home"
    seed = tmp_path / "seed-v19.db"
    snapshot = tmp_path / "snapshot-v19.db"
    upgraded = tmp_path / "upgrade-v30.db"
    old_probe = tmp_path / "old-image-probe.db"
    restored = tmp_path / "restored-v19.db"

    seeded = _require_phase(old, "seed", seed, home)
    assert seeded == {"phase": "seed", "cron_messages": 1, "delegate_messages": 1}
    assert _schema(seed)[0] == 19
    _backup(seed, snapshot)  # SQLite backup includes any committed WAL rows.
    assert _schema(snapshot)[0] == 19
    shutil.copy2(snapshot, upgraded)

    migrated = _require_phase(target, "upgrade", upgraded, home)
    assert migrated["cron_messages"] == 2 and migrated["delegate_messages"] == 1
    target_schema, fts_storage = _schema(upgraded)
    assert target_schema == 30
    _backup(upgraded, old_probe)  # old-image trial cannot damage the migrated copy.
    old_result = _run(old, "old_probe", old_probe, home)
    roundtrip_safe = False
    if old_result.returncode == 0:
        roundtrip_safe = _run(target, "target_roundtrip", old_probe, home).returncode == 0

    shutil.copy2(snapshot, restored)
    recovered = _require_phase(old, "restore_probe", restored, home)
    assert recovered["cron_messages"] == 2 and recovered["delegate_messages"] == 1
    assert _schema(restored)[0] == 19

    output = Path(os.environ["OKENGINE_HERMES_MIGRATION_OUTPUT"])
    output.write_text(json.dumps({
        "old_sha": OLD_SHA, "target_sha": TARGET_SHA,
        "old_schema": 19, "target_schema": target_schema,
        "target_fts_storage_version": fts_storage,
        "old_image_writable_reopen": old_result.returncode == 0,
        "old_to_target_roundtrip_safe": roundtrip_safe,
        "snapshot_restore_writable": True,
        "fixture_sessions": ["cron", "subagent"],
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
