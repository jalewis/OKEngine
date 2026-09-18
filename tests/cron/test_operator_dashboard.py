"""operator_dashboard (okengine#60): rolls up the per-area dashboards into one home with overall
status, drill-down links, and a stale-dashboard warning."""
import importlib.util, runpy, sys
from datetime import datetime, timezone
from pathlib import Path
import pytest
REPO = Path(__file__).resolve().parent.parent.parent


def test_rollup(tmp_path, monkeypatch):
    dd = tmp_path / "wiki" / "dashboards"; dd.mkdir(parents=True)
    (dd / "fleet-health.md").write_text("---\ntype: dashboard\ntitle: Fleet health\nupdated: 2026-06-28T20:00:00Z\n---\n"
        "- 🟢 ok: **51**  ·  🔴 stale: **0**  ·  🔴 errored: **1**  ·  🔴 off-model: **0**\n")
    (dd / "source-grounding.md").write_text("---\ntype: dashboard\ntitle: Source grounding\nupdated: 2026-06-28T20:00:00Z\n---\n"
        "- in scope: **106**  ·  🟢 grounded: **43** (41%)  ·  🔴 ungrounded: **58**\n")
    (dd / "review-queue.md").write_text("---\ntype: dashboard\ntitle: Review queue\nupdated: 2026-06-28T20:00:00Z\n---\n"
        "**8 item(s) awaiting a human** · GROUNDING: **4**\n")
    (dd / "old.md").write_text("---\ntype: dashboard\ntitle: Old thing\nupdated: 2020-01-01T00:00:00Z\n---\n# old\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location("operator_dashboard", REPO / "scripts/cron/operator_dashboard.py")
    m = importlib.util.module_from_spec(spec); sys.modules["operator_dashboard"] = m; spec.loader.exec_module(m)
    assert m.main() == 0
    o = (dd / "operator.md").read_text()
    assert "## Overall: 🔴" in o                         # fleet has 1 errored -> red
    assert "Fleet (cron lanes)" in o and "1 need attention" in o
    assert "41% of claims cite a real source" in o
    assert "8 awaiting a human" in o
    assert "Stale dashboards" in o and "Old thing" in o   # old.md is stale
    assert "[fleet-health](fleet-health.md)" in o          # drill-down link


def test_stale_only_rolls_up_yellow(tmp_path, monkeypatch):
    dd = tmp_path / "wiki" / "dashboards"; dd.mkdir(parents=True)
    (dd / "fleet-health.md").write_text(
        "---\ntype: dashboard\ntitle: Fleet health\nupdated: 2026-06-28T20:00:00Z\n---\n"
        "- 🟢 ok: 51 · 🟡 stale: 1 · 🟠 critical-stale: 0 "
        "· 🔴 errored: 0 · 🔴 off-model: 0 · 🟡 never-run: 0\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "operator_dashboard_stale", REPO / "scripts" / "cron" / "operator_dashboard.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["operator_dashboard_stale"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    out = (dd / "operator.md").read_text()
    assert "| Fleet (cron lanes) | 🟡 |" in out


def _load(tmp_path, monkeypatch, name="operator_edges"):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / "cron" / "operator_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_helpers_missing_plain_dates_numbers_and_read_error(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "page.md"
    page.write_text("plain")
    assert m._read(page) == ({}, "plain")
    page.write_text("---\ntitle: \"Quoted\"\nupdated: 2026-01-01\n---\nbody")
    fm, body = m._read(page)
    assert fm["title"] == "Quoted" and body == "\nbody"
    page.write_text("---\n# comment only\ntitle: Valid\n---\nbody")
    assert m._read(page)[0] == {"title": "Valid"}
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m._read(page) == ({}, "")
    monkeypatch.setattr(Path, "read_text", original_read)

    assert m._age_h("bad") is None
    assert m._age_h("2026-99-99T00:00:00") is None
    assert isinstance(m._age_h("2026-01-01"), float)
    assert m._num("x(\\d+)", "none", "d") == "d"
    assert m._num("x(\\d+)", "x3") == "3"


def test_no_dashboard_orange_green_and_optional_rollups(tmp_path, monkeypatch, capsys):
    m = _load(tmp_path, monkeypatch, "operator_no_dirs")
    assert m.main() == 0
    assert "no dashboards" in capsys.readouterr().out

    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (dd / "fleet-health.md").write_text(
        f"---\ntitle: Fleet\nupdated: {now}\n---\n"
        "ok: 10 critical-stale: 1 errored: 0 off-model: 0 stale: 0 never-run: 0"
    )
    (dd / "source-grounding.md").write_text(
        f"---\nupdated: {now}\n---\ngrounded: **8** (80%)"
    )
    (dd / "review-queue.md").write_text(
        f"---\nupdated: {now}\n---\n**0 item(s) awaiting"
    )
    (dd / "conformance.md").write_text(
        f"---\nupdated: {now}\n---\nsource-refs-are-pages: **2**"
    )
    (dd / "kb-health.md").write_text(f"---\nupdated: {now}\n---\n")
    (dd / "_skip.md").write_text("x")
    (dd / "INDEX.md").write_text("x")
    m = _load(tmp_path, monkeypatch, "operator_orange")
    assert m.main() == 0
    out = m.OUT.read_text()
    assert "## Overall: 🟠" in out
    assert "Conformance" in out and "KB health" in out
    assert "(0h ago)" in out

    (dd / "fleet-health.md").write_text(
        f"---\nupdated: {now}\n---\nok: 10 critical-stale: 0 "
        "errored: 0 off-model: 0 stale: 0 never-run: 0"
    )
    m = _load(tmp_path, monkeypatch, "operator_green")
    assert m.main() == 0
    assert "## Overall: 🟡" in m.OUT.read_text()  # conformance still warns


def test_entrypoint(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "dashboards").mkdir(parents=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [str(REPO / "scripts/cron/operator_dashboard.py")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts/cron/operator_dashboard.py"),
                       run_name="__main__")
    assert exc.value.code == 0
