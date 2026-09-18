import importlib.util
import json
import os
import runpy
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    path = REPO / "scripts" / "flash_fallback_audit.py"
    spec = importlib.util.spec_from_file_location("flash_fallback_audit", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _log(pack, name, lines):
    path = pack / ".hermes-data" / "logs" / "cron-plus" / name
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n")


def test_counts_only_flash_activity_after_cutoff(tmp_path):
    pack = tmp_path / "okpack-test"
    _log(pack, "raw-backfill-20260727-010000.log", [
        "2026-07-27 00:59:59,000 INFO API call #1: model=deepseek-flash provider=deepseek",
        "2026-07-27 01:00:01,000 INFO Fallback activated: qwen3-coder:30b → deepseek-flash",
        "2026-07-27 01:00:02,000 INFO API call #2: model=deepseek-flash provider=deepseek",
    ])
    result = _load().audit([pack], datetime.fromisoformat("2026-07-27T01:00:00"))
    assert result["flash_calls"] == 1
    assert result["paid_flash_calls"] == 1
    assert result["local_flash_calls"] == 0
    assert result["flash_fallback_activations"] == 1
    assert result["calls_by_pack_lane"]["okpack-test"]["raw-backfill"] == 1


def test_non_flash_models_are_clean(tmp_path):
    pack = tmp_path / "okpack-test"
    _log(pack, "entity-backfill-20260727-010000.log", [
        "2026-07-27 01:00:01,000 INFO API call #1: model=qwen3-coder:30b",
    ])
    result = _load().audit([pack], datetime.fromisoformat("2026-07-27T01:00:00"))
    assert result["flash_calls"] == 0
    assert result["flash_fallback_activations"] == 0


def test_legacy_flash_logs_remain_auditable(tmp_path):
    pack = tmp_path / "okpack-test"
    _log(pack, "legacy-20260727-010000.log", [
        "2026-07-27 01:00:01,000 INFO Fallback activated: qwen3-coder:30b → deepseek-v4-flash",
        "2026-07-27 01:00:02,000 INFO API call #1: model=deepseek-v4-flash provider=deepseek",
    ])

    result = _load().audit([pack], datetime.fromisoformat("2026-07-27T01:00:00"))

    assert result["paid_flash_calls"] == 1
    assert result["flash_fallback_activations"] == 1


def test_ignores_lifecycle_mentions_and_separates_local_flash(tmp_path):
    pack = tmp_path / "okpack-test"
    _log(pack, "gap-drain-20260727-010000.log", [
        "2026-07-27 01:00:01,000 INFO OpenAI client created provider=deepseek model=deepseek-flash",
        "2026-07-27 01:00:02,000 INFO API call #1: model=deepseek-flash provider=custom in=10 out=2",
        "2026-07-27 01:00:03,000 INFO OpenAI client closed provider=custom model=deepseek-flash",
    ])
    result = _load().audit([pack], datetime.fromisoformat("2026-07-27T01:00:00"))
    assert result["flash_calls"] == 1
    assert result["paid_flash_calls"] == 0
    assert result["local_flash_calls"] == 1


def test_explicit_paid_lane_is_visible_but_not_unexpected(tmp_path):
    pack = tmp_path / "okpack-private-x"
    _log(pack, "trends-refresh-20260727-010000.log", [
        "2026-07-27 01:00:02,000 INFO API call #1: "
        "model=deepseek-flash provider=deepseek",
    ])
    result = _load().audit(
        [pack],
        datetime.fromisoformat("2026-07-27T01:00:00"),
        {("okpack-private-x", "trends-refresh")},
    )
    assert result["paid_flash_calls"] == 1
    assert result["expected_paid_flash_calls"] == 1
    assert result["unexpected_paid_flash_calls"] == 0
    assert result["expected_paid_calls_by_pack_lane"] == {
        "okpack-private-x": {"trends-refresh": 1}
    }


def test_paid_lane_without_allowlist_is_unexpected(tmp_path):
    pack = tmp_path / "okpack-private-x"
    _log(pack, "trends-refresh-20260727-010000.log", [
        "2026-07-27 01:00:02,000 INFO API call #1: "
        "model=deepseek-flash provider=deepseek",
    ])
    result = _load().audit(
        [pack], datetime.fromisoformat("2026-07-27T01:00:00"))
    assert result["expected_paid_flash_calls"] == 0
    assert result["unexpected_paid_flash_calls"] == 1


def test_audit_missing_dirs_unreadable_unstamped_failures_and_default_allowlist(
    tmp_path, monkeypatch
):
    module = _load()
    missing = tmp_path / "missing"
    assert module.audit([missing], datetime(2026, 1, 1))["logs_scanned"] == 0
    pack = tmp_path / "pack"
    _log(pack, "odd.log", [
        "no timestamp",
        "2026-01-01 00:00:00,000 ERROR API call failed x "
        "provider=deepseek y model=deepseek-flash",
    ])
    unreadable = pack / ".hermes-data" / "logs" / "cron-plus" / "bad.log"
    unreadable.write_text("x")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("bad"))
            if path == unreadable else original_read(path, *a, **k)
        ),
    )
    result = module.audit([pack], datetime(2026, 1, 1), None)
    assert result["logs_scanned"] == 1
    assert result["paid_flash_failures"] == 1
    assert result["paid_failures_by_pack_lane"]["pack"]["odd"] == 1


def test_atomic_write_replaces_and_cleans_missing_temp(tmp_path):
    module = _load()
    target = tmp_path / "nested" / "out.json"
    module.atomic_write(target, "first")
    module.atomic_write(target, "second")
    assert target.read_text() == "second"


def test_main_clean_failure_allowlist_output_and_entrypoint(tmp_path, capsys, monkeypatch):
    module = _load()
    clean = tmp_path / "clean"
    clean.mkdir()
    output = tmp_path / "report.json"
    assert module.main([
        "--since", "2026-01-01", "--output", str(output), str(clean),
    ]) == 0
    assert json.loads(output.read_text())["flash_calls"] == 0
    assert json.loads(capsys.readouterr().out)["flash_calls"] == 0

    paid = tmp_path / "paid"
    _log(paid, "lane-20260101-000000.log", [
        "2026-01-01 00:00:01,000 INFO API call #1: "
        "model=deepseek-flash provider=deepseek",
    ])
    assert module.main(["--since", "2026-01-01", str(paid)]) == 1
    assert module.main([
        "--since", "2026-01-01", "--allow-paid", "paid/lane", str(paid),
    ]) == 0
    with pytest.raises(SystemExit):
        module.main(["--since", "2026-01-01", "--allow-paid", "bad", str(paid)])

    monkeypatch.setattr(sys, "argv", [
        str(REPO / "scripts" / "flash_fallback_audit.py"),
        "--since", "2026-01-01", str(clean),
    ])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts" / "flash_fallback_audit.py"),
                       run_name="__main__")
    assert exc.value.code == 0
