import importlib.util
import json
import os
from pathlib import Path

import pytest


MODULE = Path(__file__).parents[2] / "scripts" / "cron" / "observability_validate.py"
REPO = Path(__file__).parents[2]
# The search-layer status line exactly as fleet_health.qmd_search_health renders it.
SEARCH_LINE = ("{state} — search {s_calls} call(s) p95 {s_p95}ms · maintenance {m_calls} p95 "
               "{m_p95}ms · saturated {saturated}, search timeouts 0, maintenance errors 0, "
               "maintenance timeouts 0, concurrency {concurrency}")


def load(name="observability_validate_test"):
    spec = importlib.util.spec_from_file_location(name, MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path, monkeypatch):
    m = load()
    vault, data = tmp_path / "vault", tmp_path / "data"
    dashboards = vault / "wiki" / "dashboards"
    operational = vault / "wiki" / "operational"
    dashboards.mkdir(parents=True)
    operational.mkdir()
    (data / "qmd").mkdir(parents=True)
    (data / "metrics").mkdir()
    monkeypatch.setattr(m, "VAULT", vault)
    monkeypatch.setattr(m, "DATA", data)
    monkeypatch.setattr(m, "WIKI", vault / "wiki")
    monkeypatch.setattr(m, "FLEET", dashboards / "fleet-health.md")
    monkeypatch.setattr(m, "LANES", dashboards / ".fleet-lanes.json")
    monkeypatch.setattr(m, "SEARCH", data / "qmd" / "search-telemetry.json")
    monkeypatch.setattr(m, "DEPLOYMENT", operational / "deployment-validation.md")
    monkeypatch.setattr(m, "METRICS", data / "metrics" / "okengine.prom")
    monkeypatch.setattr(m, "OUTPUT", operational / "observability-validation.md")

    lanes = {key: [] for key in m.LANE_KEYS}
    lanes["ok"] = ["deployment-validate", "fleet-health", "health-export"]
    # Published a minute BEFORE the fleet-health run, so the artifact is still the version that
    # run read and must equal its snapshot exactly.
    search = {
        "updated": "2026-08-27T11:59:00Z", "concurrency_limit": 2, "saturated": 0,
        "search": {"calls": 10, "samples": 10, "p95_ms": 20},
        "maintenance": {"calls": 4, "samples": 4, "p95_ms": 30},
    }
    m.LANES.write_text(json.dumps({
        "updated": "2026-08-27T12:00:00Z", "search_telemetry": search, **lanes}))
    m.FLEET.write_text(
        "- ok: 3 · stale: 0 · critical-stale: 0 · errored: 0 · timeout: 0 · "
        "saturated: 0 · undetectable: 0 · invalid-schedule: 0 · off-model: 0 · never-run: 0\n"
        "## Search layer (qmd)\n\n"
        + SEARCH_LINE.format(state="🟢 **ok**", s_calls=10, s_p95=20, m_calls=4, m_p95=30,
                             saturated=0, concurrency=2)
        + "\n\n| 🟢 ok | deployment-validate | ran 1h ago |\n")
    m.SEARCH.write_text(json.dumps(search))
    m.DEPLOYMENT.write_text("**PASS** — 0 fail · 0 warn\n")
    m.METRICS.write_text(
        "okengine_health_export_timestamp_seconds 100\n"
        "okengine_health_monitor_stale 0\n")
    now = max(path.stat().st_mtime for path in (
        m.FLEET, m.LANES, m.SEARCH, m.DEPLOYMENT, m.METRICS))
    return m, now


def test_fresh_nonempty_consistent_surfaces_pass(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    assert m.validate(now=now) == []


def test_fleet_health_output_is_parseable_by_observability_consumer(tmp_path, monkeypatch):
    """Exercise the real producer and consumer together so their text contract cannot drift."""
    m, now = fixture(tmp_path, monkeypatch)
    spec = importlib.util.spec_from_file_location(
        "fleet_health_observability_contract", REPO / "scripts/cron/fleet_health.py"
    )
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    telemetry = json.loads(m.SEARCH.read_text())
    datetime_module = __import__("datetime")
    status, detail = fleet.qmd_search_health(
        datetime_module.datetime.fromtimestamp(now, tz=datetime_module.timezone.utc), telemetry
    )
    icon = {"ok": "🟢", "warn": "🟡", "unknown": "⚪"}[status]
    _set_search(m, telemetry, f"{icon} **{status}** — {detail}")
    assert m.validate(now=now) == []


def test_stale_and_cross_surface_disagreement_fail_loudly(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["ok"].append("extra")
    m.LANES.write_text(json.dumps(lanes))
    old = now - m.SEARCH_MAX_AGE - 1
    os.utime(m.SEARCH, (old, old))
    errors = m.validate(now=now)
    assert any("disagree for ok" in error for error in errors)
    assert any("search-telemetry.json: stale" in error for error in errors)


def test_deployment_failure_must_be_visible_in_fleet_classification(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    m.DEPLOYMENT.write_text("**FAIL** — 1 fail · 0 warn\n")
    errors = m.validate(now=now)
    assert any("failure is not visible" in error for error in errors)

    lanes = json.loads(m.LANES.read_text())
    lanes["ok"].remove("deployment-validate")
    lanes["errored"].append("deployment-validate")
    m.LANES.write_text(json.dumps(lanes))
    m.FLEET.write_text(m.FLEET.read_text().replace("ok: 3", "ok: 2").replace(
        "errored: 0", "errored: 1"))
    assert m.validate(now=now) == []


def test_missing_or_zero_signals_are_not_a_vacuous_pass(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    m.SEARCH.write_text("{}")
    m.METRICS.write_text("")
    errors = m.validate(now=now)
    assert any("search telemetry 'search' bucket is absent" in error for error in errors)
    assert any("search telemetry 'maintenance' bucket is absent" in error for error in errors)
    assert any("okengine_health_export_timestamp_seconds" in error for error in errors)


def _set_search(m, telemetry, line=None):
    """Publish one telemetry version to the artifact, the sidecar snapshot and the dashboard."""
    m.SEARCH.write_text(json.dumps(telemetry))
    lanes = json.loads(m.LANES.read_text())
    lanes["search_telemetry"] = telemetry
    m.LANES.write_text(json.dumps(lanes))
    if line is not None:
        head, _, _ = m.FLEET.read_text().partition("## Search layer (qmd)")
        m.FLEET.write_text(head + "## Search layer (qmd)\n\n" + line + "\n")


# What the read-MCP publishes before a bucket has any latency sample: no percentile keys at all
# (okengine-mcp/server.py `_percentiles`). The normal state after every read-MCP restart (#757).
UNSAMPLED = {"ok": 0, "timeouts": 0, "errors": 0, "calls": 0}


def test_an_unsampled_bucket_is_valid_and_renders_as_unmeasured(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry["search"] = dict(UNSAMPLED)
    _set_search(m, telemetry, SEARCH_LINE.format(
        state="🟢 **ok** — no searches yet this cycle (maintenance only)", s_calls=0, s_p95="—",
        m_calls=4, m_p95=30, saturated=0, concurrency=2))
    assert m.validate(now=now) == []


def test_an_unsampled_bucket_is_still_cross_checked(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry["search"] = dict(UNSAMPLED)
    _set_search(m, telemetry, SEARCH_LINE.format(
        state="🟢 **ok**", s_calls=1, s_p95="—", m_calls=4, m_p95=30, saturated=0, concurrency=2))
    assert m.validate(now=now) == ["fleet dashboard search telemetry disagrees with its snapshot"]


def test_an_unsampled_bucket_with_invalid_calls_is_rejected_alone(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["search_telemetry"]["search"] = {"ok": 0, "timeouts": 0, "errors": 0, "calls": None}
    m.LANES.write_text(json.dumps(lanes))
    assert m.validate(now=now) == [
        "fleet search snapshot search.calls must be non-negative integer"]


@pytest.mark.parametrize("field", ["saturated", "concurrency_limit"])
def test_one_invalid_root_measurement_invalidates_the_snapshot_alone(tmp_path, monkeypatch, field):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["search_telemetry"][field] = None
    m.LANES.write_text(json.dumps(lanes))
    assert m.validate(now=now) == [f"fleet search snapshot.{field} must be non-negative integer"]


def test_calls_without_samples_are_valid(tmp_path, monkeypatch):
    """A qmd that is not installed counts an error with no latency sample."""
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry["maintenance"] = {"ok": 0, "timeouts": 0, "errors": 3, "calls": 3}
    _set_search(m, telemetry, SEARCH_LINE.format(
        state="🟢 **ok**", s_calls=10, s_p95=20, m_calls=3, m_p95="—", saturated=0,
        concurrency=2))
    assert m.validate(now=now) == []


@pytest.mark.parametrize("field", ["samples", "p95_ms"])
def test_percentiles_are_published_together(tmp_path, monkeypatch, field):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    del telemetry["search"][field]
    m.SEARCH.write_text(json.dumps(telemetry))
    assert "search telemetry search samples and p95_ms must be published together" in m.validate(
        now=now)


@pytest.mark.parametrize("samples,calls,valid", [(0, 10, False), (1, 10, True), (10, 10, True),
                                                 (11, 10, False)])
def test_published_samples_lie_between_one_and_calls(tmp_path, monkeypatch, samples, calls, valid):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry["search"].update(samples=samples, calls=calls)
    _set_search(m, telemetry)
    error = "search telemetry search.samples must be between 1 and calls when p95_ms is published"
    assert (error not in m.validate(now=now)) is valid


@pytest.mark.parametrize("updated", ["2026-08-27T12:00:00Z", "2026-08-27T12:01:00Z"])
def test_an_empty_snapshot_must_render_as_unknown(tmp_path, monkeypatch, updated):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry["updated"] = updated   # published in or after the second the run began
    m.SEARCH.write_text(json.dumps(telemetry))
    lanes = json.loads(m.LANES.read_text())
    lanes["search_telemetry"] = {}
    m.LANES.write_text(json.dumps(lanes))
    head, _, _ = m.FLEET.read_text().partition("## Search layer (qmd)")
    error = "empty fleet search snapshot is not rendered as unknown"

    m.FLEET.write_text(head + "## Search layer (qmd)\n\n⚪ **unknown** — no search telemetry\n")
    assert m.validate(now=now) == []
    for state in ("🟢 **ok**", "🟡 **warn**"):
        m.FLEET.write_text(head + f"## Search layer (qmd)\n\n{state} — no search telemetry\n")
        assert m.validate(now=now) == [error]
    # the legend below every dashboard says `unknown`; only the status line counts
    m.FLEET.write_text(head + "⚪ **unknown** — elsewhere\n## Search layer (qmd)\n\n"
                              "_`unknown` means unmeasured_\n")
    assert error in m.validate(now=now)
    m.FLEET.write_text(head + "⚪ **unknown** — no search section\n")
    assert error in m.validate(now=now)


def test_an_empty_snapshot_of_an_artifact_that_predates_the_run_is_a_misread(
        tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["search_telemetry"] = {}
    m.LANES.write_text(json.dumps(lanes))
    head, _, _ = m.FLEET.read_text().partition("## Search layer (qmd)")
    m.FLEET.write_text(head + "## Search layer (qmd)\n\n⚪ **unknown** — no search telemetry\n")
    assert m.validate(now=now) == [
        "fleet-health recorded no search telemetry although the qmd artifact predates its run"]


def test_main_publishes_machine_owned_verdict(tmp_path, monkeypatch, capsys):
    m, _ = fixture(tmp_path, monkeypatch)
    assert m.main() == 0
    assert "**PASS** — 0 error(s)" in m.OUTPUT.read_text()
    output = capsys.readouterr().out
    assert "observability-validation: PASS" in output
    assert '"wakeAgent": false' in output


def test_live_lane_runs_after_the_producers_it_cross_checks():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    job = next(value for value in jobs if value["name"] == "observability-validation")
    assert job["no_agent"] is True
    assert job["after"] == ["fleet-health", "health-export"]
    assert job["script"].endswith("observability_validate.py")


def test_unavailable_surfaces_report_read_and_freshness_failures(tmp_path, monkeypatch):
    m = load()
    missing = tmp_path / "missing"
    for name in ("FLEET", "LANES", "SEARCH", "DEPLOYMENT", "METRICS"):
        monkeypatch.setattr(m, name, missing / name.lower())
    errors = m.validate(now=1000)
    assert sum("unavailable" in error for error in errors) == 5
    assert not any("freshness unknown" in error for error in errors)
    assert "fleet sidecar has no search telemetry snapshot" in errors
    assert "deployment validation has no parseable verdict" in errors


@pytest.mark.parametrize("payload,expected", [
    ("not JSON", "malformed JSON"),
    (json.dumps(["not", "an", "object"]), "root must be an object"),
])
def test_json_surfaces_reject_malformed_and_nonobject_roots(
        tmp_path, monkeypatch, payload, expected):
    m, now = fixture(tmp_path, monkeypatch)
    m.LANES.write_text(payload)
    assert any(expected in error for error in m.validate(now=now))


def test_freshness_unknown_is_distinct_from_an_unreadable_surface(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    real_age = m._age
    monkeypatch.setattr(
        m, "_age", lambda path, current: None
        if path in {m.FLEET, m.SEARCH} else real_age(path, current))
    errors = m.validate(now=now)
    assert f"{m.FLEET}: freshness unknown" in errors
    assert f"{m.SEARCH}: freshness unknown" in errors


def test_every_freshness_ceiling_is_strict(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    surfaces = (
        (m.FLEET, m.FLEET_MAX_AGE),
        (m.LANES, m.FLEET_MAX_AGE),
        (m.SEARCH, m.SEARCH_MAX_AGE),
        (m.DEPLOYMENT, m.DEPLOYMENT_MAX_AGE),
        (m.METRICS, m.METRICS_MAX_AGE),
    )
    for path, ceiling in surfaces:
        os.utime(path, (now - ceiling, now - ceiling))
    assert m.validate(now=now) == []
    for path, ceiling in surfaces:
        os.utime(path, (now - ceiling - 1, now - ceiling - 1))
        assert any(f"{path}: stale" in error for error in m.validate(now=now))
        os.utime(path, (now, now))


@pytest.mark.parametrize("bad", [None, {}, [1], ["ok", 2]])
def test_lane_buckets_require_string_lists(tmp_path, monkeypatch, bad):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["ok"] = bad
    m.LANES.write_text(json.dumps(lanes))
    errors = m.validate(now=now)
    assert "fleet sidecar 'ok' must be a string list" in errors


def test_dashboard_requires_each_count_and_nonempty_classification(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    m.FLEET.write_text(m.FLEET.read_text().replace("never-run: 0", "missing: 0"))
    assert "fleet dashboard count 'never-run' is absent" in m.validate(now=now)
    lanes = json.loads(m.LANES.read_text())
    lanes["ok"] = []
    m.LANES.write_text(json.dumps(lanes))
    m.FLEET.write_text(m.FLEET.read_text().replace("ok: 3", "ok: 0"))
    assert "fleet sidecar contains zero classified lanes" in m.validate(now=now)


@pytest.mark.parametrize("kind,field,bad", [
    ("search", "calls", True),
    ("search", "samples", -1),
    ("search", "p95_ms", "20"),
    ("maintenance", "calls", None),
])
def test_search_measurements_are_nonnegative_integers(tmp_path, monkeypatch, kind, field, bad):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry[kind][field] = bad
    m.SEARCH.write_text(json.dumps(telemetry))
    assert f"search telemetry {kind}.{field} must be non-negative integer" in m.validate(now=now)


@pytest.mark.parametrize("field,wrong", [
    ("s_calls", 11), ("s_p95", 21), ("s_p95", "—"), ("m_calls", 5), ("m_p95", 31),
    ("saturated", 1), ("concurrency", 3),
])
def test_dashboard_must_render_exactly_its_snapshot(tmp_path, monkeypatch, field, wrong):
    m, now = fixture(tmp_path, monkeypatch)
    values = dict(state="🟢 **ok**", s_calls=10, s_p95=20, m_calls=4, m_p95=30, saturated=0,
                  concurrency=2)
    values[field] = wrong
    _set_search(m, json.loads(m.SEARCH.read_text()), SEARCH_LINE.format(**values))
    assert m.validate(now=now) == ["fleet dashboard search telemetry disagrees with its snapshot"]


@pytest.mark.parametrize("line", [
    "🟢 **ok** — search telemetry garbled",
    "search 10 call(s) p95 20ms · maintenance 4 p95 30ms · saturated 0, search timeouts 0, "
    "concurrency 2",                                   # the numbers, but no status line
])
def test_dashboard_without_a_parseable_status_line_fails(tmp_path, monkeypatch, line):
    m, now = fixture(tmp_path, monkeypatch)
    _set_search(m, json.loads(m.SEARCH.read_text()), line)
    assert m.validate(now=now) == ["fleet dashboard has no parseable search/maintenance telemetry"]


def test_a_warn_status_line_is_parsed_too(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    _set_search(m, json.loads(m.SEARCH.read_text()), SEARCH_LINE.format(
        state="🟡 **warn** — SEARCH p95 over 5000ms", s_calls=10, s_p95=20, m_calls=4, m_p95=30,
        saturated=0, concurrency=2))
    assert m.validate(now=now) == []


@pytest.mark.parametrize("key,value", [
    ("updated", "2026-08-27T11:58:00Z"),
    (("search", "calls"), 11), (("search", "p95_ms"), 21), (("maintenance", "calls"), 5),
    (("maintenance", "p95_ms"), 31), ("saturated", 1), ("concurrency_limit", 3),
])
def test_an_unchanged_artifact_must_equal_the_snapshot_read_from_it(
        tmp_path, monkeypatch, key, value):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    if isinstance(key, tuple):
        telemetry[key[0]][key[1]] = value
    else:
        telemetry[key] = value
    m.SEARCH.write_text(json.dumps(telemetry))
    assert m.validate(now=now) == ["fleet search snapshot disagrees with the qmd artifact it read"]


@pytest.mark.parametrize("updated", ["2026-08-27T12:00:00Z", "2026-08-27T12:04:00Z"])
def test_an_artifact_republished_after_the_run_may_be_ahead(tmp_path, monkeypatch, updated):
    """The live #757 failure: maintenance kept counting after fleet-health took its snapshot."""
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry["updated"] = updated
    telemetry["search"].update(calls=15, samples=15, p95_ms=1146)
    telemetry["maintenance"].update(calls=9, samples=9, p95_ms=90)
    telemetry["saturated"] = 2
    m.SEARCH.write_text(json.dumps(telemetry))
    assert m.validate(now=now) == []


@pytest.mark.parametrize("unchanged", ["search", "maintenance", "saturated"])
def test_an_artifact_ahead_on_some_counters_and_equal_on_others_is_valid(
        tmp_path, monkeypatch, unchanged):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry.update(updated="2026-08-27T12:01:00Z", saturated=1)
    telemetry["search"].update(calls=11, samples=11)
    telemetry["maintenance"].update(calls=5, samples=5)
    if unchanged == "saturated":
        telemetry["saturated"] = 0
    else:
        telemetry[unchanged].update(calls={"search": 10, "maintenance": 4}[unchanged],
                                    samples={"search": 10, "maintenance": 4}[unchanged])
    m.SEARCH.write_text(json.dumps(telemetry))
    assert m.validate(now=now) == []


@pytest.mark.parametrize("location,field", [
    ("search", "calls"), ("maintenance", "calls"), ("root", "saturated"),
])
def test_counters_that_went_backwards_since_the_snapshot_fail(
        tmp_path, monkeypatch, location, field):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["search_telemetry"]["saturated"] = 1
    m.LANES.write_text(json.dumps(lanes))
    m.FLEET.write_text(m.FLEET.read_text().replace("saturated 0,", "saturated 1,"))
    telemetry = json.loads(m.SEARCH.read_text())
    telemetry.update(updated="2026-08-27T12:01:00Z", saturated=1)
    target = telemetry if location == "root" else telemetry[location]
    target[field] -= 1
    if field == "calls":
        target["samples"] -= 1
    m.SEARCH.write_text(json.dumps(telemetry))
    assert m.validate(now=now) == [
        "qmd artifact counters went backwards since the fleet snapshot (read-MCP restart, or "
        "fleet-health read a different file)"]


@pytest.mark.parametrize("surface,label", [
    ("artifact", "search telemetry"), ("snapshot", "fleet search snapshot"),
    ("sidecar", "fleet sidecar"),
])
@pytest.mark.parametrize("updated", [None, "2026-08-27 12:00:00", "yesterday"])
def test_version_stamps_must_parse(tmp_path, monkeypatch, surface, label, updated):
    m, now = fixture(tmp_path, monkeypatch)
    telemetry = json.loads(m.SEARCH.read_text())
    lanes = json.loads(m.LANES.read_text())
    target = {"artifact": telemetry, "snapshot": lanes["search_telemetry"], "sidecar": lanes}[surface]
    target["updated"] = updated
    m.SEARCH.write_text(json.dumps(telemetry))
    m.LANES.write_text(json.dumps(lanes))
    assert m.validate(now=now) == [f"{label}.updated must be a %Y-%m-%dT%H:%M:%SZ timestamp"]


@pytest.mark.parametrize(
    "location,field,bad",
    [
        ("root", "saturated", "bad"),
        ("root", "concurrency_limit", None),
        ("search", "calls", "bad"),
        ("maintenance", "samples", True),
    ],
)
def test_malformed_fleet_snapshot_is_reported_without_crashing(
        tmp_path, monkeypatch, location, field, bad):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    snapshot = lanes["search_telemetry"]
    target = snapshot if location == "root" else snapshot[location]
    target[field] = bad
    m.LANES.write_text(json.dumps(lanes))

    errors = m.validate(now=now)
    assert any(f"fleet search snapshot" in error and field in error for error in errors)


def test_deployment_verdict_and_lane_membership_must_agree(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    lanes = json.loads(m.LANES.read_text())
    lanes["ok"].remove("deployment-validate")
    m.LANES.write_text(json.dumps(lanes))
    m.FLEET.write_text(m.FLEET.read_text().replace("ok: 3", "ok: 2"))
    assert "deployment-validate is absent from fleet-health classifications" in m.validate(now=now)
    lanes["errored"].append("deployment-validate")
    m.LANES.write_text(json.dumps(lanes))
    m.FLEET.write_text(m.FLEET.read_text().replace("errored: 0", "errored: 1"))
    assert "deployment validation is PASS but fleet-health classifies it unhealthy" in m.validate(now=now)


def _move_deployment_lane(m, bucket, label):
    lanes = json.loads(m.LANES.read_text())
    lanes["ok"].remove("deployment-validate")
    lanes[bucket].append("deployment-validate")
    m.LANES.write_text(json.dumps(lanes))
    m.FLEET.write_text(m.FLEET.read_text().replace("ok: 3", "ok: 2").replace(
        f"{label}: 0", f"{label}: 1"))


def test_a_passing_deployment_fleet_health_cannot_see_is_reported(tmp_path, monkeypatch):
    """Live on two packs before #757: a silent PASS was filed as `undetectable`."""
    m, now = fixture(tmp_path, monkeypatch)
    _move_deployment_lane(m, "undetectable", "undetectable")
    assert m.validate(now=now) == [
        "deployment validation is PASS but fleet-health cannot see its verdict"]


def test_an_undetectable_deployment_failure_is_not_visible(tmp_path, monkeypatch):
    m, now = fixture(tmp_path, monkeypatch)
    m.DEPLOYMENT.write_text("**FAIL** — 1 fail · 0 warn\n")
    _move_deployment_lane(m, "undetectable", "undetectable")
    assert m.validate(now=now) == [
        "deployment validation failure is not visible as an unhealthy fleet lane"]


def test_lane_keys_cover_every_bucket_fleet_health_publishes():
    import importlib.util as util
    import sys
    spec = util.spec_from_file_location("fleet_health_parity", REPO / "scripts/cron/fleet_health.py")
    fleet = util.module_from_spec(spec)
    sys.modules[spec.name] = fleet
    spec.loader.exec_module(fleet)
    assert sorted(load().LANE_KEYS) == sorted(fleet.LANE_BUCKETS)


def test_writer_records_each_error_and_creates_parent(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "OUTPUT", tmp_path / "new" / "result.md")
    m._write(["first", "second"])
    text = m.OUTPUT.read_text()
    assert "**FAIL** — 2 error(s)" in text
    assert "- first\n- second\n" in text


def test_main_fails_when_result_cannot_be_published(tmp_path, monkeypatch, capsys):
    m, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "OUTPUT", tmp_path)  # a directory cannot be replaced by the report
    assert m.main() == 2
    assert "cannot publish result" in capsys.readouterr().err


def test_main_prints_each_validation_error(tmp_path, monkeypatch, capsys):
    m, _ = fixture(tmp_path, monkeypatch)
    m.FLEET.write_text(m.FLEET.read_text().replace("ok: 3", "ok: 99"))
    assert m.main() == 1
    output = capsys.readouterr().out
    assert "observability-validation: FAIL" in output
    assert "fleet dashboard/sidecar disagree for ok" in output
