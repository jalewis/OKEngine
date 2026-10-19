"""usage_rollup — log → SQLite usage ledger, idempotent + settled (okengine#144)."""
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

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


# --- okengine#991: observable-output contracts (whole-file mutation floor) -------------------------

def _mod_with_local(monkeypatch, models):
    monkeypatch.setenv("HERMES_LOCAL_MODELS", models)
    return _mod()


def _rows(m, data_dir):
    c = m.connect(data_dir)
    rows = sorted(c.execute("SELECT day, model, lane, is_free, calls FROM usage"))
    c.close()
    return rows


def _insert(m, data_dir, rows):
    c = m.connect(data_dir)
    c.executemany("INSERT INTO usage(day, model, lane, is_free, calls) VALUES(?,?,?,?,?)", rows)
    c.commit()
    c.close()


@pytest.mark.parametrize("model, free", [
    ("x:free", True), ("X:FREE", True), ("openrouter/free", True),
    ("zzz-paid", False),          # sorts after "openrouter/free": a > or >= comparison would free it
    ("deepseek-flash", False), ("openrouter/freebie", False),
])
def test_free_classification(model, free):
    assert _mod().is_free_model(model) is free


def test_declared_local_models_count_as_free(monkeypatch):
    m = _mod_with_local(monkeypatch, " qwen3.5:27b , Llama3:8B ,")
    assert m.is_free_model("qwen3.5:27b") and m.is_free_model("llama3:8b")
    assert not m.is_free_model("deepseek-flash")


def test_rollup_records_day_lane_and_free_flag_exactly(tmp_path):
    m = _mod()
    _log(tmp_path, "daily-brief-20260626-130000.log", ["x:free", "deepseek-flash", "x:free"])
    _log(tmp_path, "daily-brief-20260627-130000.log", ["x:free"])
    assert m.rollup(tmp_path) == (2, 4)
    assert _rows(m, tmp_path) == [
        ("2026-06-26", "deepseek-flash", "daily-brief", 0, 1),
        ("2026-06-26", "x:free", "daily-brief", 1, 2),
        ("2026-06-27", "x:free", "daily-brief", 1, 1),
    ]


def test_rollup_keeps_going_past_skipped_logs(tmp_path):
    """Already-counted, unsettled, vanished and unparseable logs are each skipped, never a reason to
    stop: the valid log sorted after all of them is still counted."""
    m = _mod()
    _log(tmp_path, "a-20260601-000000.log", ["x:free"])
    assert m.rollup(tmp_path) == (1, 1)                              # now in `processed`
    _log(tmp_path, "b-20260601-000000.log", ["x:free"], settled=False)
    _log(tmp_path, "c-not-a-run-log.log", ["x:free"])
    _log(tmp_path, "d-20260601-000000.log", ["deepseek-flash", "deepseek-flash"])
    assert m.rollup(tmp_path) == (1, 2)
    assert ("2026-06-01", "deepseek-flash", "d", 0, 2) in _rows(m, tmp_path)


def test_rollup_counts_a_log_once_it_is_exactly_settled(tmp_path):
    m = _mod()
    f = _log(tmp_path, "a-20260601-000000.log", ["x:free"])
    os.utime(f, (100, 100))
    assert m.rollup(tmp_path, now=100 + m.SETTLE_SECONDS - 1) == (0, 0)   # still settling
    assert m.rollup(tmp_path, now=100 + m.SETTLE_SECONDS) == (1, 1)       # boundary counts
    g = _log(tmp_path, "b-20260601-000000.log", ["x:free"])
    os.utime(g, (100, 100))
    assert m.rollup(tmp_path, now=300) == (1, 1)                          # long settled


def test_settle_window_is_two_minutes():
    assert _mod().SETTLE_SECONDS == 120


def test_ledger_is_created_under_a_missing_data_dir(tmp_path):
    m = _mod()
    data = tmp_path / "fresh" / "data"
    _log(data, "a-20260601-000000.log", ["x:free"])
    assert m.rollup(data) == (1, 1)
    assert (data / "metrics" / "usage.db").is_file()
    assert m.rollup(data) == (0, 0)                                   # reconnect to the same ledger


def test_report_prints_exact_daily_and_all_time_offload(tmp_path):
    m = _mod()
    _insert(m, tmp_path, [
        ("2026-06-01", "x:free", "a", 1, 1), ("2026-06-01", "paid", "a", 0, 2),   # 1/3 -> 33%
        ("2026-06-02", "x:free", "a", 1, 7), ("2026-06-02", "paid", "a", 0, 1),   # 7/8 -> 87%
    ])
    lines = m.report(tmp_path).splitlines()
    assert lines[1:5] == [
        f"  {'day':<12}{'calls':>9}{'free%':>8}",
        f"  {'2026-06-02':<12}{8:>9}{87:>7}%",
        f"  {'2026-06-01':<12}{3:>9}{33:>7}%",
        f"  {'ALL TIME':<12}{11:>9}{72:>7}%   (cost offload)",
    ]
    assert lines[5:] == ["  top models:", f"    {8:>8}  [free]  x:free", f"    {3:>8}  [PAID]  paid"]


def test_report_shows_all_time_even_when_nothing_was_free(tmp_path):
    m = _mod()
    _insert(m, tmp_path, [("2026-06-01", "paid", "a", 0, 4)])
    assert f"  {'ALL TIME':<12}{4:>9}{0:>7}%   (cost offload)" in m.report(tmp_path).splitlines()


def test_report_limits_to_the_most_recent_days(tmp_path):
    m = _mod()
    _insert(m, tmp_path, [(f"2026-06-{d:02d}", "paid", "a", 0, 1) for d in range(1, 17)])
    default = m.report(tmp_path)
    assert "2026-06-16" in default and "2026-06-03" in default and "2026-06-02" not in default
    two = m.report(tmp_path, 2)
    assert "2026-06-16" in two and "2026-06-15" in two and "2026-06-14" not in two


def test_report_reclassifies_history_under_the_current_local_models(tmp_path, monkeypatch):
    """A model declared local AFTER its calls were counted is shown free retroactively."""
    _insert(_mod(), tmp_path, [("2026-06-01", "qwen-local", "a", 0, 2), ("2026-06-01", "p", "a", 0, 2)])
    m = _mod_with_local(monkeypatch, "qwen-local")
    out = m.report(tmp_path)
    assert "[free]  qwen-local" in out and f"{50:>7}%" in out
    assert ("2026-06-01", "qwen-local", "a", 1, 2) in _rows(m, tmp_path)


def test_main_routes_report_and_rollup_with_their_arguments(tmp_path, monkeypatch, capsys):
    m = _mod()
    _insert(m, tmp_path, [(f"2026-06-{d:02d}", "paid", "a", 0, 1) for d in range(1, 4)])
    assert m.main(["".join(["re", "port"]), str(tmp_path), "1"]) == 0   # not an interned literal
    out = capsys.readouterr().out
    assert str(tmp_path / "metrics" / "usage.db") in out
    assert "2026-06-03" in out and "2026-06-02" not in out
    assert m.main(["report", str(tmp_path)]) == 0
    assert "2026-06-01" in capsys.readouterr().out
    monkeypatch.setattr(m, "DEFAULT_DATA_DIR", str(tmp_path))
    assert m.main(["report"]) == 0
    assert str(tmp_path / "metrics" / "usage.db") in capsys.readouterr().out

    data = tmp_path / "other"
    _log(data, "a-20260601-000000.log", ["x:free", "x:free"])
    assert m.main([str(data)]) == 0
    assert f"usage-rollup: +1 log(s), +2 call(s) -> {data / 'metrics' / 'usage.db'}" in \
        capsys.readouterr().out


def test_a_data_dir_that_sorts_after_report_is_rolled_up_not_reported(tmp_path, monkeypatch, capsys):
    m = _mod()
    monkeypatch.chdir(tmp_path)
    _log(Path("zdata"), "a-20260601-000000.log", ["x:free"])
    assert m.main(["zdata"]) == 0
    assert "usage-rollup: +1 log(s), +1 call(s)" in capsys.readouterr().out


def test_script_entry_point_runs_main(tmp_path, monkeypatch, capsys):
    import runpy
    monkeypatch.setattr(sys, "argv", ["usage_rollup.py", "report", str(tmp_path), "3"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(REPO / "scripts" / "cron" / "usage_rollup.py"), run_name="__main__")
    assert exit_info.value.code == 0
    assert "Model-usage ledger" in capsys.readouterr().out
