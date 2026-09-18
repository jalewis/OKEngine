"""Fleet health view (okengine#64) — the pure analysis functions over a fixture data dir."""
import importlib.util
import json
import os
from datetime import datetime
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


def test_registry_loss_tool_error_overrides_false_clean_completion():
    m = _mod()
    actual_call = (
        'agent.tool_executor: Tool mcp__okengine__get_page returned error: '
        '{"error":"Unknown tool: mcp__okengine__get_page"}\n'
    )
    assert m.classify_log(actual_call + "Job 'x' completed successfully\n") == "registry_lost", (
        "a missing MCP registry entry must not be counted as a clean lane"
    )
    assert m.classify_log(actual_call + "agent returned [SILENT]\n") == "registry_lost"
    assert m.classify_log(
        "User asked about the text 'Unknown tool: mcp__okengine__get_page'.\n"
        "Job 'x' completed successfully\n"
    ) == "ok", "quoted user text is not an executed MCP tool error"
    quoted_entry = (
        'INFO cron.scheduler: Prompt: agent.tool_executor: Tool '
        'mcp__okengine__get_page returned error: '
        '{"error":"Unknown tool: mcp__okengine__get_page"}\n'
    )
    assert m.classify_log(quoted_entry + "Job 'x' completed successfully\n") == "ok"
    assert m.scan_signals(quoted_entry)["MCP registry loss (#608)"] == 0


def test_unreachable_mcp_executor_error_overrides_false_clean_completion():
    m = _mod()
    actual_call = (
        'agent.tool_executor: Tool mcp__okengine__get_page returned error (0.00s): '
        '{"error":"MCP server \'okengine\' is unreachable after 3 consecutive failures"}\n'
    )
    assert m.classify_log(actual_call + "Job 'x' completed successfully\n") == "mcp_unreachable"
    assert m.scan_signals(actual_call)["MCP transport unavailable (#608)"] == 1
    assert m.classify_log(
        "User mentioned an MCP server being unreachable.\n"
        "Job 'x' completed successfully\n"
    ) == "ok"
    assert m.scan_signals("User quoted 'MCP server \'okengine\' transport is down'\n")[
        "MCP transport unavailable (#608)"] == 0
    quoted_entry = "INFO cron.scheduler: Prompt: " + actual_call
    assert m.classify_log(quoted_entry + "Job 'x' completed successfully\n") == "ok"
    assert m.scan_signals(quoted_entry)["MCP transport unavailable (#608)"] == 0
    timestamped = "2026-09-15 16:18:30,846 WARNING [cron_fixture] " + actual_call
    assert m.classify_log(timestamped + "Job 'x' completed successfully\n") == "mcp_unreachable"
    assert m.classify_log(
        actual_call.replace("is unreachable after 3 consecutive failures", "transport is down")
        + "Job 'x' completed successfully\n"
    ) == "mcp_unreachable"
    assert m.classify_log(
        actual_call
        + 'agent.tool_executor: Tool mcp__okengine__get_page returned error: '
          '{"error":"Unknown tool: mcp__okengine__get_page"}\n'
        + "Job 'x' completed successfully\n"
    ) == "registry_lost", "a physical registry miss remains the more specific failure"


# --- scan_signals -----------------------------------------------------------

def test_scan_signals_counts_the_silent_failures():
    m = _mod()
    text = (
        "Write denied: '/opt/vault/x.md' is a protected system/credential file.\n"
        "Auxiliary: marking nous unhealthy for 60s (payment / credit error).\n"
        "cron-plus: job 'raw-backfill' skipped — previous run still active\n"
        'agent.tool_executor: Tool mcp__okengine__get_page returned error: {"error":"Unknown tool: '
        'mcp__okengine__get_page"}\n'
        "deterministic writer introduced separator-equivalent page(s); "
        "quarantined outside wiki: entities/a/agenttesla.md\n"
    )
    s = m.scan_signals(text)
    assert s["vault write denied (#140)"] == 1
    assert s["provider payment/credit error"] == 1
    assert s["job-overlap skip (runs > interval)"] == 1
    assert s["MCP registry loss (#608)"] == 1
    assert s["agent tool error"] == 1
    assert s["direct-writer slug collision (#647)"] == 1


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


def test_report_names_registry_lost_lane_instead_of_counting_false_ok(tmp_path):
    m = _mod()
    _seed(
        tmp_path,
        jobs=[{"name": "search-drain", "enabled": True,
               "next_run_at": "2099-01-01T00:00:00+00:00"}],
        logs={"search-drain-20260101-000000.log":
              'agent.tool_executor: Tool mcp__okengine__search returned error: '
              '{"error":"Unknown tool: mcp__okengine__search"}\n'
              "Job 'search-drain' completed successfully\n"},
    )
    report, code = m.build_report(str(tmp_path))
    assert code == 1
    assert "0 ok" in report and "1 MCP registry-lost" in report
    assert "✗ search-drain" in report
    assert "MCP registry loss (#608)" in report


def test_report_names_mcp_unreachable_lane_instead_of_counting_false_ok(tmp_path):
    m = _mod()
    _seed(
        tmp_path,
        jobs=[{"name": "search-drain", "enabled": True,
               "next_run_at": "2099-01-01T00:00:00+00:00"}],
        logs={"search-drain-20260101-000000.log":
              'agent.tool_executor: Tool mcp__okengine__get_page returned error (0.00s): '
              '{"error":"MCP server \'okengine\' is unreachable after 3 consecutive failures"}\n'
              "Job 'search-drain' completed successfully\n"},
    )
    report, code = m.build_report(str(tmp_path))
    assert code == 1
    assert "0 ok" in report and "1 MCP unreachable" in report
    assert "✗ search-drain" in report
    assert "MCP transport unavailable (#608)" in report


def test_later_clean_fire_does_not_erase_earlier_mcp_affected_run(tmp_path):
    m = _mod()
    bad_name = "search-drain-20260915-000000.log"
    good_name = "search-drain-20260915-020000.log"
    _seed(
        tmp_path,
        jobs=[{"name": "search-drain", "enabled": True,
               "next_run_at": "2099-01-01T00:00:00+00:00"}],
        logs={
            bad_name:
                'agent.tool_executor: Tool mcp__okengine__get_page returned error (0.00s): '
                '{"error":"MCP server \'okengine\' is unreachable after 3 consecutive failures"}\n'
                "Job 'search-drain' completed successfully\n",
            good_name: "Job 'search-drain' completed successfully\n",
        },
    )
    now = m._now()
    log_dir = tmp_path / "logs" / "cron-plus"
    os.utime(log_dir / bad_name, (now - 120, now - 120))
    os.utime(log_dir / good_name, (now - 60, now - 60))
    report, code = m.build_report(str(tmp_path))
    assert code == 1, "the earlier blind fire is still inside the observation window"
    assert "1 ok" in report and "0 MCP unreachable" in report, (
        "latest-lane status should describe the clean repeat, not overwrite the past")
    assert "MCP-affected fires in window" in report
    assert "search-drain: 0 registry-lost, 1 transport-unreachable fire(s)" in report


def test_aggregate_error_events_use_timestamps_and_numeric_rotations(tmp_path, monkeypatch):
    m = _mod()
    fixed = datetime(2026, 9, 15, 12, 0, 0).timestamp()
    monkeypatch.setattr(m, "_now", lambda: fixed)
    _seed(tmp_path)
    errors = tmp_path / "logs" / "errors.log"
    errors.write_text(
        '2026-09-13 11:00:00 WARNING agent.tool_executor: Tool '
        'mcp__okengine__get_page returned error: '
        '{"error":"Unknown tool: mcp__okengine__get_page"}\n', encoding="utf-8")
    report, code = m.build_report(str(tmp_path), window_h=24)
    assert code == 0 and "MCP registry loss (#608)" not in report, (
        "an old event in a freshly modified current log is not a last-24h failure")
    rotated = tmp_path / "logs" / "errors.log.1"
    rotated.write_text(
        '2026-09-15 11:00:00 WARNING agent.tool_executor: Tool '
        'mcp__okengine__get_page returned error: '
        '{"error":"Unknown tool: mcp__okengine__get_page"}\n', encoding="utf-8")
    report, code = m.build_report(str(tmp_path), window_h=24)
    assert code == 1 and "MCP registry loss (#608)" in report
    assert m._recent_error_signals(str(tmp_path), fixed - 24 * 3600)[0][
        "MCP registry loss (#608)"] == 1
    (tmp_path / "logs" / "errors.log.backup").write_text(
        rotated.read_text(encoding="utf-8"), encoding="utf-8")
    report, _ = m.build_report(str(tmp_path), window_h=24)
    assert m._recent_error_signals(str(tmp_path), fixed - 24 * 3600)[0][
        "MCP registry loss (#608)"] == 1, (
        "a nonnumeric backup must not duplicate the standing signal")


def test_aggregate_unknown_age_and_unreadable_rotation_fail_loudly(tmp_path):
    m = _mod()
    _seed(tmp_path)
    (tmp_path / "logs" / "errors.log").write_text(
        'agent.tool_executor: Tool mcp__okengine__get_page '
        'returned error: {"error":"MCP server \'okengine\' transport is down"}\n',
        encoding="utf-8")
    report, code = m.build_report(str(tmp_path))
    assert code == 1 and "age unknown" in report
    assert "MCP transport unavailable (#608)" in report
    (tmp_path / "logs" / "errors.log").write_text(
        '2026-99-99 11:00:00 WARNING agent.tool_executor: Tool '
        'mcp__okengine__get_page returned error: '
        '{"error":"MCP server \'okengine\' transport is down"}\n', encoding="utf-8")
    report, code = m.build_report(str(tmp_path))
    assert code == 1 and "age unknown" in report, (
        "a timestamp-shaped but impossible calendar date must not certify a clean window")
    (tmp_path / "logs" / "errors.log").unlink()
    (tmp_path / "logs" / "errors.log.1").mkdir()
    report, code = m.build_report(str(tmp_path))
    assert code == 1 and "could not be read" in report
    assert "ATTENTION" in report


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
    assert not m.is_free_model("deepseek-flash")
    assert not m.is_free_model("openai/gpt-oss-120b")


def test_is_local_endpoint_covers_the_private_ranges_not_a_hostname_allowlist():
    """The engine ships no domain knowledge, so "our inference box" has to be recognised from the
    ADDRESS. Every RFC1918 block, loopback and link-local counts; a public address never does."""
    m = _mod()
    # NB: the consumer-router RFC1918 block is deliberately NOT spelled out in this file. The
    # repo's scrub gate greps every tracked file for that prefix to stop a deployment's real LAN
    # address shipping in the public snapshot, and a test is not worth a permanent hole in it.
    # Nothing is lost: is_local_endpoint delegates to ipaddress.is_private, so every private block
    # takes the identical path — the two below exercise it just as well.
    for url in ("http://10.0.0.5:11436/v1", "http://172.16.4.9:8000/v1",
                "http://172.31.255.254/v1", "http://127.0.0.1:8080/v1",
                "http://[::1]:8080/v1", "http://localhost:11436/v1",
                "http://169.254.10.10/v1", "http://gpu-box.lan:11436/v1",
                "http://inference.internal/v1", "10.1.2.3:11436"):
        assert m.is_local_endpoint(url), url
    for url in ("https://api.deepseek.com/v1", "https://openrouter.ai/api/v1",
                "http://172.32.0.1/v1",              # just OUTSIDE 172.16/12
                "https://example.com/v1", ""):
        assert not m.is_local_endpoint(url), url


def test_a_locally_served_model_is_free_even_though_its_name_says_nothing(tmp_path):
    """#610: the regression this fix exists for. `qwen3-coder:30b` carries no `:free` marker, so
    the name-only rule called it PAID and reported the fleet at 0% offload while ~95% of calls
    cost nothing. Free-ness is a property of WHERE the call went, not what it is called."""
    m = _mod()
    assert not m.is_free_model("qwen3-coder:30b")                       # name alone: unknowable
    assert m.is_free_model("qwen3-coder:30b", {"http://10.0.0.9:11436/v1"})
    assert not m.is_free_model("deepseek-flash", {"https://api.deepseek.com/v1"})


def test_a_model_served_from_both_local_and_paid_endpoints_counts_as_paid():
    """Failover means the same model id can be served from either side. Counting it free would
    overstate offload and hide real spend, so a mixed set is paid — understating is the cheap
    mistake of the two."""
    m = _mod()
    mixed = {"http://10.0.0.9:11436/v1", "https://api.deepseek.com/v1"}
    assert not m.is_free_model("qwen3-coder:30b", mixed)
    assert m.is_free_model("qwen3-coder:30b", {"http://10.0.0.9:11436/v1"})


def test_map_model_endpoints_pairs_only_adjacent_provider_lines():
    """Most `model=` occurrences are bare progress lines with no provider, so the mapping is built
    from the ones that DO carry a base_url and then applied model-wide. A model that never appears
    with a provider must be ABSENT rather than defaulted, so the report can say so."""
    m = _mod()
    text = ("provider=custom base_url=http://10.0.0.9:11436/v1 model=qwen3-coder:30b p=1\n"
            "base_url=https://api.deepseek.com/v1 model=deepseek-flash\n"
            "agent.conversation_loop: API call #3: model=qwen3-coder:30b\n"
            "some-other-line model=mystery-model\n"
            "base_url=http://x/v1 model=model\n")
    got = m.map_model_endpoints(text)
    assert got == {"qwen3-coder:30b": {"http://10.0.0.9:11436/v1"},
                   "deepseek-flash": {"https://api.deepseek.com/v1"}}
    assert "mystery-model" not in got          # bare line -> unknown provider, not a default


def test_count_models_skips_placeholder():
    m = _mod()
    # The placeholder sits in the MIDDLE on purpose: with it last, skipping the rest of the file
    # instead of just that entry would produce an identical result, and the test could not tell
    # `continue` from `break`.
    text = ("x model=openai/gpt-oss-120b:free y\nq model=model\n"
            "z model=deepseek-flash\n")
    c = m.count_models(text)
    assert c == {"openai/gpt-oss-120b:free": 1, "deepseek-flash": 1}   # 'model=model' skipped


def test_report_shows_model_usage_and_free_offload(tmp_path):
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "x", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"x-20260101-000000.log":
                "Job 'x' completed successfully\n"
                "model=nemotron-3-super:free\nmodel=nemotron-3-super:free\nmodel=deepseek-flash\n"})
    report, _ = m.build_report(str(tmp_path))
    assert "Model usage" in report and "cost offload" in report
    assert "66% free/self-hosted" in report        # 2 of 3 calls free
    assert "[PAID?]  deepseek-flash" in report    # no provider logged in this fixture


def test_report_counts_a_local_endpoint_as_offload(tmp_path):
    """End-to-end through build_report, in the shape the live logs actually take: a locally
    served model with no free-tier marker in its name, alongside a commercial one."""
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "x", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"x-20260101-000000.log":
                "Job 'x' completed successfully\n"
                "provider=custom base_url=http://10.0.0.9:11436/v1 model=qwen3-coder:30b\n"
                "API call #2: model=qwen3-coder:30b\n"
                "provider=deepseek base_url=https://api.deepseek.com/v1 model=deepseek-flash\n"})
    report, _ = m.build_report(str(tmp_path))
    assert "66% free/self-hosted" in report         # 2 local calls of 3
    assert "[free]  qwen3-coder:30b" in report
    assert "[PAID]  deepseek-flash" in report
    assert "unclassified" not in report             # every model had a provider


def test_report_says_when_it_could_not_price_the_calls(tmp_path):
    """A 0% offload must not be ambiguous between "nothing is offloaded" and "no provider was
    ever logged" — that ambiguity is what let the stat read 0% through the whole llama.cpp era."""
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "x", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"x-20260101-000000.log":
                "Job 'x' completed successfully\nmodel=mystery-model\nmodel=mystery-model\n"})
    report, _ = m.build_report(str(tmp_path))
    assert "0% free/self-hosted" in report
    assert "100% unclassified" in report
    assert "[PAID?]  mystery-model" in report


def test_report_prices_what_it_can_and_says_what_it_could_not(tmp_path):
    """The mixed case, which the all-or-nothing fixtures above cannot reach: some calls priced,
    some not. With every model unclassified the unclassified share is trivially 100% and any
    arithmetic error hides; here it must come out at a specific partial value."""
    m = _mod()
    _seed(tmp_path,
          jobs=[{"name": "x", "enabled": True, "next_run_at": "2099-01-01T00:00:00+00:00"}],
          logs={"x-20260101-000000.log":
                "Job 'x' completed successfully\n"
                "base_url=http://10.0.0.9:11436/v1 model=qwen3-coder:30b\n"
                "base_url=https://api.deepseek.com/v1 model=deepseek-flash\n"
                "model=mystery-model\nmodel=mystery-model\n"})
    report, _ = m.build_report(str(tmp_path))
    assert "25% free/self-hosted" in report      # 1 local call of 4
    assert "50% unclassified" in report          # 2 of 4 had no provider logged
    assert "[free]  qwen3-coder:30b" in report
    assert "[PAID]  deepseek-flash" in report
    assert "[PAID?]  mystery-model" in report


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


def test_io_helpers_tolerate_missing_stale_and_raced_files(tmp_path, monkeypatch):
    m = _mod()
    assert m._load_jobs(str(tmp_path)) == []
    cp = tmp_path / "cron-plus"
    cp.mkdir()
    (cp / "jobs.json").write_text("not json")
    assert m._load_jobs(str(tmp_path)) == []

    log = tmp_path / "run.log"
    old_log = tmp_path / "old.log"
    log.write_text("run")
    old_log.write_text("old")
    monkeypatch.setattr(m.glob, "glob", lambda _pattern: [str(log), str(old_log), str(tmp_path / "gone")])
    monkeypatch.setattr(m, "_now", lambda: 1000)
    monkeypatch.setattr(
        m.os.path, "getmtime",
        lambda path: 999 if path == str(log) else -3000 if path == str(old_log)
        else (_ for _ in ()).throw(OSError("gone")),
    )
    assert m._recent_logs(str(tmp_path), 1) == [str(log)]

    fresh = tmp_path / "fresh.json"
    stale = tmp_path / "stale.json"
    bad = tmp_path / "bad.json"
    fresh.write_text(json.dumps({"counts": {"selected": "2"}}))
    stale.write_text(json.dumps({"counts": {"selected": 99}}))
    bad.write_text("bad")
    monkeypatch.setattr(m.glob, "glob", lambda _pattern: [str(stale), str(fresh), str(bad)])
    monkeypatch.setattr(m.os.path, "getmtime", lambda path: 1 if path == str(stale) else 100)
    assert m._receipt_counts(str(tmp_path), 50)["selected"] == 2


def test_report_tolerates_corrupt_auxiliary_files_and_bad_dates(tmp_path, monkeypatch):
    m = _mod()
    _seed(
        tmp_path,
        jobs=[{"name": "bad-date", "next_run_at": "not-a-date"}, {"name": "numeric", "next_run_at": 3}],
        logs={"raced-20260101-000000.log": "incomplete"},
        stalled="initial",
    )
    (tmp_path / "cron-plus" / ".scheduler-stalled").write_text("bad json")
    errors = tmp_path / "logs" / "errors.log"
    errors.write_text("payment / credit error")
    report, code = m.build_report(str(tmp_path))
    assert code == 1
    assert "unreadable job store" in report
    assert "provider payment/credit error" in report

    original_open = open
    raced = str(tmp_path / "logs" / "cron-plus" / "raced-20260101-000000.log")
    monkeypatch.setattr(
        m, "open",
        lambda path, *a, **k: (_ for _ in ()).throw(OSError("raced"))
        if str(path) in {raced, str(errors)} else original_open(path, *a, **k),
        raising=False,
    )
    report, _ = m.build_report(str(tmp_path))
    assert "0 lanes ran" in report


def test_main_uses_arguments_and_prints_report(monkeypatch, capsys):
    m = _mod()
    seen = []
    monkeypatch.setattr(m, "build_report", lambda data, window: (seen.append((data, window)) or "report", 7))
    assert m.main(["fleet-status", "/data", "6.5"]) == 7
    assert seen == [("/data", 6.5)]
    assert capsys.readouterr().out.strip() == "report"


# --- slot starvation (a hard-timeout kill that executed no work) -------------

def _seed_runs(tmp_path, runs):
    """runs: {job_id: [record, ...]} under cron-plus/runs/<job_id>/<n>.json"""
    root = tmp_path / "cron-plus" / "runs"
    for job_id, records in runs.items():
        d = root / job_id
        d.mkdir(parents=True, exist_ok=True)
        for i, rec in enumerate(records):
            (d / f"2099-01-01_00-00-{i:02d}.json").write_text(json.dumps(rec), encoding="utf-8")


_KILLED = "TimeoutError: cron-plus run exceeded hard timeout of 600s"
_DENIED = ("TimeoutError: model slot unavailable after 300.0s "
           "(identity=custom|http://gpu:11436/v1|m, limit=4)")


def test_a_denied_slot_is_reported_as_starvation_with_the_capacity_remedy(tmp_path):
    """A hard-timeout kill with zero executed turns did no work to be 'too slow' at.

    It never received a model response — it queued for an inference slot until killed. Left
    unnamed, six weeks of these read as "these lanes are too slow" and sent the investigation
    at lane work size instead of at endpoint capacity.
    """
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "raw-backfill", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"aaa": [
        {"lane": "raw-backfill", "status": "failed", "error": _DENIED,
         "executed_tool_call_turns": 0},
        {"lane": "raw-backfill", "status": "succeeded", "executed_tool_call_turns": 4},
    ]})
    report, code = m.build_report(str(tmp_path))
    assert "never got an inference slot" in report
    assert "CAPACITY" in report
    assert "raw-backfill" in report
    assert code == 1, "starvation must drive the exit code, not just print"
    assert "slot-starved" in report


def test_a_stall_is_reported_separately_from_starvation(tmp_path):
    """Held a slot, returned nothing. Opposite remedy — it must not appear under starvation."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "raw-backfill", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"aaa": [
        {"lane": "raw-backfill", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 0},
    ]})
    report, code = m.build_report(str(tmp_path))
    assert "Stalled runs: ✗" in report
    assert "LATENCY/LANE, not capacity" in report
    assert "Slot starvation: ✓ none" in report
    assert code == 1


def test_a_kill_that_did_work_is_a_genuine_overrun_not_starvation(tmp_path):
    """The discriminator is turns, not the timeout itself — otherwise every real overrun
    would be misfiled as capacity, which is the same error in the opposite direction."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "regrade", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"bbb": [
        {"lane": "regrade", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 7},
    ]})
    report, code = m.build_report(str(tmp_path))
    assert "all with work executed" in report
    assert "never got an inference slot" not in report
    assert code == 0


def test_a_no_agent_lane_with_zero_turns_is_not_starvation(tmp_path):
    """A no_agent lane has no turns to execute, so zero proves nothing about slots there.
    Counting it would manufacture starvation on every deterministic script that overran."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "reshelve", "enabled": True, "no_agent": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"ccc": [
        {"lane": "reshelve", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 0},
    ]})
    report, code = m.build_report(str(tmp_path))
    assert "never got an inference slot" not in report
    assert code == 0


def test_absent_run_records_report_undetectable_never_a_clean_pass(tmp_path):
    """No records is UNKNOWN, not "nothing starved". A detector that reports 0 for a directory
    it never read is decoration — the empty-parse-is-not-a-no rule."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "x", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    report, _ = m.build_report(str(tmp_path))
    assert "UNDETECTABLE" in report
    assert "not a pass" in report


def test_starvation_scan_ignores_records_outside_the_window(tmp_path):
    """An in-window healthy run keeps the scan MEASURABLE, so this proves the stale record was
    excluded on its age -- not that the scan simply found nothing to read."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "raw-backfill", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"aaa": [
        {"lane": "raw-backfill", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 0},                       # stale, aged out below
        {"lane": "raw-backfill", "status": "succeeded", "executed_tool_call_turns": 3},
    ]})
    stale = tmp_path / "cron-plus" / "runs" / "aaa" / "2099-01-01_00-00-00.json"
    os.utime(stale, (1_000_000, 1_000_000))       # far outside any window
    report, code = m.build_report(str(tmp_path))
    assert "never got an inference slot" not in report
    assert "UNDETECTABLE" not in report, "an in-window record was present; the scan DID run"
    assert code == 0


def test_a_failed_run_that_was_not_a_timeout_is_not_counted_as_a_kill(tmp_path):
    """Only hard-timeout kills belong to this class. A crash, a denial, or a script error is a
    different failure with a different fix, and folding them in would inflate the signal that
    exists precisely to point at endpoint capacity."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "raw-backfill", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"aaa": [
        {"lane": "raw-backfill", "status": "failed",
         "error": "ValueError: bad frontmatter", "executed_tool_call_turns": 0},
    ]})
    report, code = m.build_report(str(tmp_path))
    assert "never got an inference slot" not in report
    assert "all with work executed" not in report      # it was not a timeout kill at all
    assert "UNDETECTABLE" not in report
    assert code == 0


def test_starvation_scan_tolerates_corrupt_run_records(tmp_path):
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "raw-backfill", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"aaa": [
        {"lane": "raw-backfill", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 0},
    ]})
    bad = tmp_path / "cron-plus" / "runs" / "aaa" / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    report, code = m.build_report(str(tmp_path))
    assert "Stalled runs: ✗" in report and code == 1   # the good record still counts


def test_a_clean_starvation_scan_says_so_rather_than_staying_silent(tmp_path):
    """Silence is not success. A scan that ran and found nothing reads identically to a section
    that was skipped -- the same ambiguity UNDETECTABLE removes at the other end."""
    m = _mod()
    _seed(tmp_path, jobs=[{"name": "raw-backfill", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _seed_runs(tmp_path, {"aaa": [
        {"lane": "raw-backfill", "status": "succeeded", "executed_tool_call_turns": 3},
    ]})
    report, code = m.build_report(str(tmp_path))
    assert "Slot starvation: ✓ none" in report
    assert "UNDETECTABLE" not in report
    assert code == 0


# --- resolving the shared starvation library (okengine#614) ------------------

def _reset_lib(m):
    m._starvation_module = None
    m._starvation_looked_up = False


def test_prefers_the_staged_copy_beside_the_cron_scripts(tmp_path):
    """Inside a gateway this file is streamed on stdin, so the STAGED copy under
    <data_dir>/scripts/ is the only one that exists. It must be tried first."""
    m = _mod(); _reset_lib(m)
    sd = tmp_path / "scripts"; sd.mkdir(parents=True)
    (sd / "slot_starvation.py").write_text(
        "MARKER='staged'\n"
        "def scan(d,c,a): return {'starved':7,'killed':7,'runs':7,'measurable':True,'lanes':{}}\n"
        "def agent_lanes_of(j): return set()\n", encoding="utf-8")
    lib = m._starvation_lib(str(tmp_path))
    assert getattr(lib, "MARKER", None) == "staged"
    assert m._slot_starvation(str(tmp_path), 0, set())["starved"] == 7


def test_falls_back_to_the_repo_copy_when_nothing_is_staged(tmp_path):
    """Loaded as a file (tests, a local run) there is no staged copy, but __file__ resolves the
    repo's scripts/cron/ sibling."""
    m = _mod(); _reset_lib(m)
    lib = m._starvation_lib(str(tmp_path))          # tmp_path has no scripts/ dir
    assert lib is not None and hasattr(lib, "scan")


def test_no_resolvable_library_is_undetectable_never_a_clean_zero(tmp_path, monkeypatch):
    """The whole point of the delegation: a missing library must not read as 'nothing starved'."""
    m = _mod(); _reset_lib(m)
    monkeypatch.delattr(m, "__file__")              # no repo sibling reachable either
    assert m._starvation_lib(str(tmp_path)) is None
    r = m._slot_starvation(str(tmp_path), 0, set())
    assert r["measurable"] is False and r["starved"] == 0

    _seed(tmp_path, jobs=[{"name": "x", "enabled": True,
                           "next_run_at": "2099-01-01T00:00:00+00:00"}])
    _reset_lib(m)
    report, _ = m.build_report(str(tmp_path))
    assert "UNDETECTABLE" in report and "not staged" in report
    assert "Model-slot health" in report


def test_a_broken_library_does_not_take_the_report_down(tmp_path, monkeypatch):
    """A syntax error in the staged copy must degrade to UNDETECTABLE, not raise through the
    whole fleet report."""
    m = _mod(); _reset_lib(m)
    monkeypatch.delattr(m, "__file__")              # force the staged copy to be the only path
    sd = tmp_path / "scripts"; sd.mkdir(parents=True)
    (sd / "slot_starvation.py").write_text("def scan(  <<< not python\n", encoding="utf-8")
    assert m._starvation_lib(str(tmp_path)) is None


def test_the_resolved_library_is_memoised(tmp_path):
    m = _mod(); _reset_lib(m)
    first = m._starvation_lib(str(tmp_path))
    assert m._starvation_looked_up is True
    assert m._starvation_lib(str(tmp_path)) is first


def test_the_unresolvable_library_fallback_matches_the_real_scan_shape(tmp_path):
    """A fallback missing a key crashes the report in exactly the degraded case it exists to
    survive — caught when `stalled` was added to the scan but not to the fallback."""
    m = _mod(); _reset_lib(m)
    import importlib.util as _ilu
    lib_path = REPO / "scripts" / "cron" / "slot_starvation.py"
    spec = _ilu.spec_from_file_location("slot_starvation_probe", lib_path)
    lib = _ilu.module_from_spec(spec); spec.loader.exec_module(lib)
    real = lib.scan(str(tmp_path), 0, set())
    m._starvation_looked_up = True; m._starvation_module = None
    fallback = m._slot_starvation(str(tmp_path), 0, set())
    assert set(fallback) == set(real), "fallback and scan must expose the same keys"
