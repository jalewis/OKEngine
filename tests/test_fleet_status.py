"""Fleet health view (okengine#64) — the pure analysis functions over a fixture data dir."""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "fleet_status.py"


def _mod():
    spec = importlib.util.spec_from_file_location("fleet_status", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --- classify_log -----------------------------------------------------------

def test_classify_outcomes():
    m = _mod()
    assert m.classify_log("... Job 'x' completed successfully\n") == "ok"
    assert m.classify_log("... agent returned [SILENT] — skipping delivery") == "silent"
    assert m.classify_log("Job 'x' (no_agent): wakeAgent=false gate — silent run") == "silent"
    assert m.classify_log("started... then nothing") == "incomplete"   # no completion logged


# --- scan_signals -----------------------------------------------------------

def test_scan_signals_counts_the_silent_failures():
    m = _mod()
    text = (
        "Write denied: '/opt/vault/x.md' is a protected system/credential file.\n"
        "Auxiliary: marking nous unhealthy for 60s (payment / credit error).\n"
        "cron-plus: job 'raw-backfill' skipped — previous run still active\n"
    )
    s = m.scan_signals(text)
    assert s["vault write denied (#140)"] == 1
    assert s["provider payment/credit error"] == 1
    assert s["job-overlap skip (runs > interval)"] == 1
    assert s["agent tool error"] == 0


# --- build_report -----------------------------------------------------------

def _seed(tmp_path, *, ticking=True, jobs=None, logs=None, stalled=None):
    cp = tmp_path / "cron-plus"; cp.mkdir(parents=True)
    (cp / "jobs.json").write_text(json.dumps({"jobs": jobs or []}), encoding="utf-8")
    if ticking:
        (cp / ".tick.lock").write_text("")
    if stalled is not None:
        (cp / ".scheduler-stalled").write_text(json.dumps({"error": stalled}), encoding="utf-8")
    ld = tmp_path / "logs" / "cron-plus"; ld.mkdir(parents=True)
    for name, text in (logs or {}).items():
        (ld / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_stall_sentinel_flagged_even_when_tick_lock_is_fresh(tmp_path):
    """HIGH #2: tick() refreshes .tick.lock BEFORE load_jobs(), so a ticking-but-not-loading
    scheduler keeps a FRESH lock while firing no lanes. The .scheduler-stalled sentinel is the
    machine alarm for that, but its only other reader is a cron LANE the stalled scheduler never
    runs. fleet_status must surface it and exit non-zero — even with a fresh .tick.lock present."""
    m = _mod()
    _seed(tmp_path, ticking=True,             # lock present + fresh (would otherwise read healthy)
          jobs=[{"name": "feed-fetch", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"feed-fetch-20260101-000000.log": "Job 'feed-fetch' completed successfully\n"},
          stalled="jobs.json: unexpected end of JSON")
    report, code = m.build_report(str(tmp_path))
    assert code == 1, "a stalled scheduler must fail the fleet status"
    assert "SCHEDULER STALLED" in report
    assert "unexpected end of JSON" in report
    # and with NO sentinel the same fixture is healthy (proves the sentinel is what flips it)
    (tmp_path / "cron-plus" / ".scheduler-stalled").unlink()
    report2, code2 = m.build_report(str(tmp_path))
    assert code2 == 0 and "SCHEDULER STALLED" not in report2


def test_report_clean_fleet_exits_zero(tmp_path):
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "feed-fetch", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"feed-fetch-20260101-000000.log": "Job 'feed-fetch' completed successfully\n"})
    report, code = m.build_report(str(tmp_path))
    assert code == 0
    assert "✓ none" in report
    assert "1 ok" in report and "healthy" in report.lower()


def test_report_flags_critical_signals_and_exits_one(tmp_path):
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "g", "enabled": True, "extension": "okengine.g",
                 "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"g-20260101-000000.log":
                "Write denied: '/opt/vault/x' is a protected system/credential file.\n"
                "payment / credit error\n"})
    report, code = m.build_report(str(tmp_path))
    assert code == 1                                   # critical -> non-zero
    assert "vault write denied" in report
    assert "provider payment/credit error" in report
    assert "ATTENTION" in report
    assert "1 extension" in report


def test_report_warns_when_scheduler_not_ticking(tmp_path):
    m = _mod()
    _seed(tmp_path, ticking=False,
          jobs=[{"name": "x", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={})
    report, _ = m.build_report(str(tmp_path))
    assert "scheduler not running" in report


def test_report_marks_overdue_lane(tmp_path):
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "stuck", "enabled": True, "next_run_at": "2000-01-01T00:00:00+00:00"}],
          logs={})
    report, _ = m.build_report(str(tmp_path))
    assert "overdue" in report and "stuck" in report


def test_is_free_model():
    m = _mod()
    assert m.is_free_model("nvidia/nemotron-3-super-120b-a12b:free")
    assert m.is_free_model("openrouter/free")
    assert not m.is_free_model("deepseek-v4-pro")
    assert not m.is_free_model("openai/gpt-oss-120b")


def test_count_models_skips_placeholder():
    m = _mod()
    text = "x model=openai/gpt-oss-120b:free y\nz model=deepseek-v4-pro\nq model=model\n"
    c = m.count_models(text)
    assert c == {"openai/gpt-oss-120b:free": 1, "deepseek-v4-pro": 1}   # 'model=model' skipped


def test_report_shows_model_usage_and_free_offload(tmp_path):
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "x", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"x-20260101-000000.log":
                "Job 'x' completed successfully\n"
                "model=nemotron-3-super:free\nmodel=nemotron-3-super:free\nmodel=deepseek-v4-pro\n"})
    report, _ = m.build_report(str(tmp_path))
    assert "Model usage" in report and "cost offload" in report
    assert "66% on free tiers" in report          # 2 of 3 calls free
    assert "[PAID]  deepseek-v4-pro" in report


def test_report_exposes_verified_receipt_counts(tmp_path):
    m = _mod()
    _seed(tmp_path, jobs=[])
    receipt = tmp_path / "cron-plus" / "receipts" / "lane" / "run.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"counts": {
        "selected": 30, "accepted": 3, "rejected": 2, "deferred": 4, "undisposed": 21,
    }}))
    report, _ = m.build_report(str(tmp_path))
    assert "30 selected" in report and "3 accepted" in report and "21 undisposed" in report
