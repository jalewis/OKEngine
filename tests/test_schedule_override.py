"""extension-schedules.json — per-deployment cron cadence override, by job name."""
import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import extension_compose as ec


def _wl(tmp, obj):
    import json
    (tmp / ".okengine").mkdir(exist_ok=True)
    (tmp / ".okengine" / "extension-schedules.json").write_text(json.dumps(obj))


def test_override_applies(tmp_path):
    _wl(tmp_path, {"okengine.lacuna": "0 6 * * *"})
    jobs = [{"name": "okengine.lacuna", "schedule": {"kind": "cron", "expr": "0 6 * * 1"}}]
    assert ec._apply_schedule_overrides(jobs, tmp_path) == []
    assert jobs[0]["schedule"] == {"kind": "cron", "expr": "0 6 * * *"}


def test_unknown_name_fails_loud(tmp_path):
    _wl(tmp_path, {"okengine.nope": "0 6 * * *"})
    errs = ec._apply_schedule_overrides([{"name": "okengine.lacuna", "schedule": {}}], tmp_path)
    assert errs and "nope" in errs[0]


def test_malformed_expr_fails(tmp_path):
    _wl(tmp_path, {"okengine.lacuna": "0 6 * *"})  # 4 fields
    errs = ec._apply_schedule_overrides([{"name": "okengine.lacuna", "schedule": {}}], tmp_path)
    assert errs and "5-field" in errs[0]


def test_absent_file_is_noop(tmp_path):
    assert ec._apply_schedule_overrides([], tmp_path) == []
