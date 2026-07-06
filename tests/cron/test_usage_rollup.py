"""usage_rollup — log → SQLite usage ledger, idempotent + settled (okengine#144)."""
import importlib.util
import os
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "usage_rollup", REPO / "scripts" / "cron" / "usage_rollup.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _log(data_dir, name, models, *, settled=True):
    d = Path(data_dir) / "logs" / "cron-plus"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(f"x model={m} y" for m in models) + "\n", encoding="utf-8")
    if settled:
        old = time.time() - 1000
        os.utime(f, (old, old))
    return f


def test_parse_log(tmp_path):
    m = _mod()
    f = _log(tmp_path, "daily-brief-20260626-130000.log",
             ["nemotron-3-super:free", "deepseek-v4-pro", "model"])
    day, lane, counts = m.parse_log(f)
    assert day == "2026-06-26" and lane == "daily-brief"
    assert counts == {"nemotron-3-super:free": 1, "deepseek-v4-pro": 1}   # 'model=model' skipped


def test_rollup_counts_and_classifies(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free", "x:free", "deepseek-v4-pro"])
    logs, calls = m.rollup(tmp_path)
    assert logs == 1 and calls == 3
    c = m.connect(tmp_path)
    rows = {mdl: (n, isf) for mdl, isf, n in c.execute("SELECT model, is_free, calls FROM usage")}
    assert rows["x:free"] == (2, 1) and rows["deepseek-v4-pro"] == (1, 0)


def test_rollup_is_idempotent(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free"])
    m.rollup(tmp_path); m.rollup(tmp_path)                  # twice
    c = m.connect(tmp_path)
    assert c.execute("SELECT calls FROM usage").fetchone()[0] == 1   # not double-counted


def test_rollup_skips_unsettled_log(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free"], settled=False)   # fresh mtime
    logs, calls = m.rollup(tmp_path)
    assert logs == 0 and calls == 0                        # still being written; deferred


def test_report_offload_pct(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free", "x:free", "deepseek-v4-pro"])
    m.rollup(tmp_path)
    r = m.report(tmp_path)
    assert "66%" in r and "cost offload" in r              # 2 of 3 calls free
    assert "[PAID]  deepseek-v4-pro" in r


def test_report_empty(tmp_path):
    m = _mod()
    assert "empty" in m.report(tmp_path)
