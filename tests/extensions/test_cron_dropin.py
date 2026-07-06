"""#63 P1 — the cron drop-in model: an extension supplies operations as `crons/*.cron.json`
files (one op per file, filename = op name) instead of (or alongside) a manifest `operations:`
block. The composer collects them forward-only and synthesizes the same namespaced jobs.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "scripts" / "extension_compose.py"

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="extension modules absent")


def _load():
    spec = importlib.util.spec_from_file_location("extension_compose", COMPOSE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["extension_compose"] = m
    spec.loader.exec_module(m)
    return m


def _manifest(ext_id, with_ops=False):
    m = {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
         "requires": {"engine": ">=0.6.0"},
         "capabilities": {"read": ["wiki/**"], "write": ["x/**"]}}
    if with_ops:
        m["operations"] = {"manual": {"schedule": {"kind": "cron", "expr": "0 5 * * *"},
                                      "entrypoint": "manual.py"}}
    return m


def _drop(d: Path, op: str, expr: str, entry: str):
    (d / "crons").mkdir(parents=True, exist_ok=True)
    (d / "crons" / f"{op}.cron.json").write_text(
        json.dumps({"schedule": {"kind": "cron", "expr": expr}, "entrypoint": entry}))


def _rec(ext_id, d, with_ops=False):
    return {"id": ext_id, "tier": "engine", "dir": str(d), "manifest": _manifest(ext_id, with_ops)}


def test_dropin_crons_are_collected(tmp_path):
    c = _load()
    _drop(tmp_path, "watch", "17 6 * * *", "select_watch.py")
    _drop(tmp_path, "grade", "23 6 * * *", "grade.py")
    jobs, errors, _ = c.synthesize_ops(_rec("okengine.ex", tmp_path))
    assert not errors, errors
    assert sorted(j["name"] for j in jobs) == ["okengine.ex:grade", "okengine.ex:watch"]
    by = {j["name"]: j for j in jobs}
    assert by["okengine.ex:grade"]["script"] == "/opt/data/scripts/okengine.ex/grade.py"
    assert by["okengine.ex:grade"]["schedule"]["expr"] == "23 6 * * *"
    assert by["okengine.ex:grade"]["extension"] == "okengine.ex"   # provenance marker


def test_dropin_and_manifest_ops_merge(tmp_path):
    c = _load()
    _drop(tmp_path, "watch", "17 6 * * *", "watch.py")
    jobs, errors, _ = c.synthesize_ops(_rec("okengine.ex", tmp_path, with_ops=True))
    assert not errors, errors
    assert sorted(j["name"] for j in jobs) == ["okengine.ex:manual", "okengine.ex:watch"]


def test_dropin_name_colliding_with_manifest_op_is_error(tmp_path):
    c = _load()
    _drop(tmp_path, "manual", "17 6 * * *", "watch.py")          # collides with manifest op `manual`
    _, errors, _ = c.synthesize_ops(_rec("okengine.ex", tmp_path, with_ops=True))
    assert any("collision" in e or "duplicate" in e for e in errors), errors


def test_no_ops_in_manifest_or_dropins_is_error(tmp_path):
    c = _load()
    _, errors, _ = c.synthesize_ops(_rec("okengine.ex", tmp_path))
    assert any("crons/*.cron.json" in e for e in errors), errors


def test_malformed_dropin_json_fails_loud(tmp_path):
    c = _load()
    (tmp_path / "crons").mkdir()
    (tmp_path / "crons" / "bad.cron.json").write_text("{not json")
    _, errors, _ = c.synthesize_ops(_rec("okengine.ex", tmp_path))
    assert any("bad.cron.json" in e for e in errors), errors


def test_dropin_after_dependency_passes_through(tmp_path):
    """#129: an op's `after:` hard dependency is carried onto the synthesized job."""
    c = _load()
    (tmp_path / "crons").mkdir()
    (tmp_path / "crons" / "score.cron.json").write_text(json.dumps(
        {"schedule": {"kind": "cron", "expr": "0 6 * * *"}, "entrypoint": "score.py",
         "after": ["okengine.ex:ledger"]}))
    jobs, errors, _ = c.synthesize_ops(_rec("okengine.ex", tmp_path))
    assert not errors, errors
    assert jobs[0].get("after") == ["okengine.ex:ledger"]
