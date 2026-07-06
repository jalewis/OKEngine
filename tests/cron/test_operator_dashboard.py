"""operator_dashboard (okengine#60): rolls up the per-area dashboards into one home with overall
status, drill-down links, and a stale-dashboard warning."""
import importlib.util, sys
from pathlib import Path
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
