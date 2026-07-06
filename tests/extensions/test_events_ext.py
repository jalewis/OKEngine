"""okengine.events — deterministic domain event ledger (okengine#155). Built on the #63 drop-in
model (no_agent lane in crons/*.cron.json); derived L1 dashboard, no own type."""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.events"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
LEDGER = EXT / "build_event_ledger.py"
pytestmark = pytest.mark.skipif(not EXT.is_dir(), reason="okengine.events absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m


def _manifest():
    return yaml.safe_load((EXT / "extension.yaml").read_text())


def test_manifest_valid_dropin_no_schema():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.events" and mod.is_reserved_id(m["id"])
    assert "operation" not in m and "operations" not in m and "schema" not in m  # drop-in, derived
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


def test_dropin_composes_one_no_agent_lane():
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(
        {"id": "okengine.events", "tier": "engine", "dir": str(EXT), "manifest": _manifest()})
    assert not errors, errors
    assert [j["name"] for j in jobs] == ["okengine.events:event-ledger"]
    assert jobs[0]["no_agent"] is True


def _run(vault: Path):
    return subprocess.run([sys.executable, str(LEDGER)], capture_output=True, text=True,
                          env={**os.environ, "WIKI_PATH": str(vault),
                               "OKENGINE_MCP_WRITE_DATE": "2026-06-28"}).stdout


def test_ledger_compiles_scored_events(tmp_path):
    w = tmp_path / "wiki"
    (w / "deals").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "event_types: [deal, incident]\nevent_score_weights: {deal: 2, incident: 1}\n")
    (w / "deals" / "a.md").write_text("---\ntype: deal\ntitle: A\ndate: 2026-06-20\n---\nx\n")
    (w / "deals" / "b.md").write_text("---\ntype: incident\ntitle: B\ndate: 2026-06-25\n---\nx\n")
    (w / "deals" / "c.md").write_text("---\ntype: concept\ntitle: C\n---\nx\n")  # not an event
    _run(tmp_path)
    led = (w / "dashboards" / "event-ledger.md").read_text()
    assert "**2 events**" in led
    assert "| 2026-06-25 | incident | 1 |" in led and "| 2026-06-20 | deal | 2 |" in led
    assert "C |" not in led            # the concept page is excluded


def test_no_event_types_is_a_clean_noop(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n")     # no event_types
    out = _run(tmp_path)
    assert "nothing to compile" in out
    assert not (tmp_path / "wiki" / "dashboards" / "event-ledger.md").exists()


def test_ledger_self_contained():
    imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", LEDGER.read_text(), re.M)
    allowed = {"__future__", "json", "os", "re", "datetime", "pathlib", "yaml", "typing", "collections"}
    assert not [i for i in imports if i.split(".")[0] not in allowed]


def test_ledger_parses_year_month_dates(tmp_path):
    """Partial dates are common (campaign first_seen: 2025-10) — they must parse to a real date,
    not silently fall back to `updated` (the bug found rolling to sec)."""
    w = tmp_path / "wiki"
    (w / "e").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("event_types: [campaign]\nevent_date_field: first_seen\n")
    (w / "e" / "camp.md").write_text(
        "---\ntype: campaign\ntitle: Camp\nfirst_seen: 2025-10\nupdated: 2026-06-27\n---\nx\n")
    _run(tmp_path)
    led = (w / "dashboards" / "event-ledger.md").read_text()
    assert "2025-10-01" in led and "2026-06-27" not in led   # year-month padded, not the fallback
