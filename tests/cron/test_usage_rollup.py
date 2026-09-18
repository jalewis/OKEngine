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
             ["nemotron-3-super:free", "deepseek-flash", "model"])
    day, lane, counts = m.parse_log(f)
    assert day == "2026-06-26" and lane == "daily-brief"
    assert counts == {"nemotron-3-super:free": 1, "deepseek-flash": 1}   # 'model=model' skipped


def test_rollup_counts_and_classifies(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free", "x:free", "deepseek-flash"])
    logs, calls = m.rollup(tmp_path)
    assert logs == 1 and calls == 3
    c = m.connect(tmp_path)
    rows = {mdl: (n, isf) for mdl, isf, n in c.execute("SELECT model, is_free, calls FROM usage")}
    assert rows["x:free"] == (2, 1) and rows["deepseek-flash"] == (1, 0)
    c.close()


def test_rollup_is_idempotent(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free"])
    m.rollup(tmp_path); m.rollup(tmp_path)                  # twice
    c = m.connect(tmp_path)
    assert c.execute("SELECT calls FROM usage").fetchone()[0] == 1   # not double-counted
    c.close()


def test_rollup_skips_unsettled_log(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free"], settled=False)   # fresh mtime
    logs, calls = m.rollup(tmp_path)
    assert logs == 0 and calls == 0                        # still being written; deferred


def test_report_offload_pct(tmp_path):
    m = _mod()
    _log(tmp_path, "a-20260626-130000.log", ["x:free", "x:free", "deepseek-flash"])
    m.rollup(tmp_path)
    r = m.report(tmp_path)
    assert "66%" in r and "cost offload" in r              # 2 of 3 calls free
    assert "[PAID]  deepseek-flash" in r


def test_report_empty(tmp_path):
    m = _mod()
    assert "empty" in m.report(tmp_path)


def test_parse_and_rollup_filesystem_edges(tmp_path, monkeypatch):
    m = _mod()
    invalid = _log(tmp_path, "not-a-run.log", ["x:free"])
    assert m.parse_log(invalid) is None
    valid = _log(tmp_path, "lane-20260731-120000.log", ["x:free"])
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == valid else original_read(self, *a, **k),
    )
    assert m.parse_log(valid) is None
    monkeypatch.setattr(Path, "read_text", original_read)

    original_stat = Path.stat
    monkeypatch.setattr(
        Path, "stat",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("vanished"))
        if self == valid else original_stat(self, *a, **k),
    )
    logs, calls = m.rollup(tmp_path, now=time.time())
    assert (logs, calls) == (0, 0)


def test_report_zero_total_and_main_routes(tmp_path, monkeypatch, capsys):
    m = _mod()
    c = m.connect(tmp_path)
    c.execute(
        "INSERT INTO usage(day, model, lane, is_free, calls) VALUES(?,?,?,?,?)",
        ("2026-07-31", "zero-model", "lane", 0, 0),
    )
    c.commit()
    c.close()
    rendered = m.report(tmp_path)
    assert "0%" in rendered and "ALL TIME" not in rendered

    assert m.main(["report", str(tmp_path), "3"]) == 0
    assert "Model-usage ledger" in capsys.readouterr().out
    assert m.main(["report", str(tmp_path)]) == 0
    monkeypatch.setattr(m, "rollup", lambda _data: (2, 5))
    assert m.main([str(tmp_path)]) == 0
    assert "+2 log(s), +5 call(s)" in capsys.readouterr().out
    monkeypatch.setattr(m, "DEFAULT_DATA_DIR", str(tmp_path))
    assert m.main([]) == 0
