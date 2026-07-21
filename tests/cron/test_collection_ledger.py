import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_append_projection_preserves_zero_yield_failure_and_unknown(tmp_path):
    m = _load("collection_ledger", REPO / "scripts/cron/collection_ledger.py")
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    sources = [
        {"source_id": "official", "connector_id": "test", "label": "Official",
         "source_kind": "primary", "independent_origin": True},
        {"source_id": "never", "connector_id": "test", "label": "Never"},
    ]
    m.register_sources(tmp_path, sources)
    m.append_attempt(tmp_path, {
        "connector_id": "test", "source_id": "official",
        "started_at": now - timedelta(minutes=2), "finished_at": now,
        "outcome": "failure", "error_category": "http-503", "latency_ms": 10,
    })
    attempts = m.load_attempts(tmp_path, now=now)
    rows = m.project_current(m.load_sources(tmp_path), attempts, now=now)
    official = next(row for row in rows if row["source_id"] == "official")
    never = next(row for row in rows if row["source_id"] == "never")
    assert official["status"] == "failing" and official["fetched"] == 0
    assert official["consecutive_failures"] == 1 and official["last_success"] is None
    assert never["status"] == "unknown" and never["fetched"] is None
    line = next(tmp_path.glob("attempts-*.ndjson")).read_text().strip()
    assert json.loads(line)["error_category"] == "http-503"


def test_stale_success_is_not_rendered_healthy(tmp_path):
    m = _load("collection_ledger_stale", REPO / "scripts/cron/collection_ledger.py")
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    sources = [{"source_id": "s", "connector_id": "c", "label": "S"}]
    attempt = {"source_id": "s", "connector_id": "c", "outcome": "success",
               "started_at": now - timedelta(days=3), "finished_at": now - timedelta(days=3)}
    assert m.project_current(sources, [attempt], now=now)[0]["status"] == "stale"


def test_zero_yield_success_is_an_observed_healthy_attempt(tmp_path):
    m = _load("collection_ledger_zero", REPO / "scripts/cron/collection_ledger.py")
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    source = {"source_id": "s", "connector_id": "c", "label": "S"}
    attempt = {"source_id": "s", "connector_id": "c", "outcome": "success",
               "started_at": now, "finished_at": now, "fetched": 0, "accepted": 0}
    row = m.project_current([source], [attempt], now=now)[0]
    assert row["status"] == "healthy" and row["fetched"] == 0


def test_concurrent_appends_remain_complete_json_lines(tmp_path):
    m = _load("collection_ledger_append", REPO / "scripts/cron/collection_ledger.py")
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)

    def write(n):
        m.append_attempt(tmp_path, {"connector_id": "c", "source_id": f"s{n}",
                                    "started_at": now + timedelta(seconds=n),
                                    "finished_at": now + timedelta(seconds=n),
                                    "outcome": "success"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    lines = next(tmp_path.glob("attempts-*.ndjson")).read_text().splitlines()
    assert len(lines) == 40
    assert {json.loads(line)["source_id"] for line in lines} == {f"s{n}" for n in range(40)}


def test_source_registry_reconciles_only_the_calling_connector(tmp_path):
    m = _load("collection_ledger_registry", REPO / "scripts/cron/collection_ledger.py")
    m.register_sources(tmp_path, [{"source_id": "a", "connector_id": "one"},
                                  {"source_id": "b", "connector_id": "two"}])
    m.register_sources(tmp_path, [], connector_id="one")
    assert [row["source_id"] for row in m.load_sources(tmp_path)] == ["b"]


def test_checkpoint_is_opaque_and_monthly_segments_prune(tmp_path):
    m = _load("collection_ledger_privacy", REPO / "scripts/cron/collection_ledger.py")
    digest = m.checkpoint_digest({"authorization": "secret", "page": 2})
    assert digest.startswith("sha256:") and "secret" not in digest
    old = tmp_path / "attempts-2025-01.ndjson"
    old.write_text("{}\n")
    removed = m.prune(tmp_path, now=datetime(2026, 7, 18, tzinfo=timezone.utc), retention_days=90)
    assert old in removed and not old.exists()


def test_dashboard_renders_unknowns_and_ops_artifact(tmp_path, monkeypatch):
    ledger = _load("collection_ledger", REPO / "scripts/cron/collection_ledger.py")
    health = _load("collection_health", REPO / "scripts/cron/collection_health.py")
    ledger.register_sources(tmp_path / "ledger", [{
        "source_id": "never", "connector_id": "demo", "label": "Never run",
    }])
    out = health.render(vault=tmp_path, ledger=tmp_path / "ledger",
                        now=datetime(2026, 7, 18, tzinfo=timezone.utc))
    text = out.read_text()
    assert out == tmp_path / "wiki/operational/collection-health.md"
    assert "| unknown | Never run" in text
    assert "never interpreted as zero or healthy" in text
    assert "recent yield: unknown — no collection attempts recorded" in text
