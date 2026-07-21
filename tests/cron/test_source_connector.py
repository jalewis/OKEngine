from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts/cron/source_connector.py"
FIXTURES = REPO / "tests/fixtures/source_connectors"


def _load():
    sys.modules.pop("source_connector", None)
    spec = importlib.util.spec_from_file_location("source_connector", MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["source_connector"] = module
    spec.loader.exec_module(module)
    return module


def _manifest(module, mode: str) -> dict:
    return module.load_yaml(FIXTURES / f"{mode}.yaml")


@pytest.mark.parametrize("mode", ["bundle", "query", "enrichment", "stream", "poll"])
def test_reference_manifest_for_every_mode_is_conformant(mode):
    m = _load()
    assert m.validate_manifest(_manifest(m, mode)) == []


def test_machine_schema_and_runtime_agree_on_modes_and_required_blocks():
    m = _load()
    schema = yaml.safe_load((REPO / "config/source-connector.schema.yaml").read_text())
    assert set(schema["properties"]["mode"]["enum"]) == m.MODES
    assert set(schema["required"]) == {
        "connector_version", "id", "mode", "trust", "permissions", "auth", "request",
        "response", "pagination", "checkpoint", "conditional_requests", "rate_limit",
        "archive", "license", "health",
    }


@pytest.mark.parametrize(("mode", "params", "env", "expected"), [
    ("bundle", {}, {}, 2),
    ("query", {"term": "needle"}, {}, 1),
    ("enrichment", {"entity_id": "entity-7"}, {"FIXTURE_ENRICH_TOKEN": "test"}, 1),
    ("stream", {}, {"FIXTURE_STREAM_KEY": "test"}, 2),
    ("poll", {}, {}, 2),
])
def test_every_mode_runs_deterministically_from_fixture(tmp_path, mode, params, env, expected):
    m = _load()
    kwargs = {"inputs": params, "env": env, "state_root": tmp_path / "state",
              "archive_root": tmp_path / "archive", "health_root": tmp_path / "health",
              "ledger_root": tmp_path / "ledger",
              "fixture": FIXTURES / f"{mode}.fixture.json",
              "observed_at": "2026-07-18T12:00:00Z"}
    result = m.execute(_manifest(m, mode), **kwargs)
    assert result["ok"] and result["records"] == expected
    assert all(item["mode"] == mode and item["observed_at"] == "2026-07-18T12:00:00Z"
               for item in result["items"])
    health = json.loads((tmp_path / "health" / f"fixture.{mode}.json").read_text())
    assert health["ok"] and "items" not in health
    attempts = list((tmp_path / "ledger").glob("attempts-*.ndjson"))
    assert len(attempts) == 1
    attempt = json.loads(attempts[0].read_text().strip())
    assert attempt["outcome"] == "success" and attempt["fetched"] == expected


def test_poll_checkpoints_cursor_deletion_etag_and_immutable_revisions(tmp_path):
    m = _load()
    manifest = _manifest(m, "poll")
    kwargs = {"state_root": tmp_path / "state", "archive_root": tmp_path / "archive",
              "health_root": tmp_path / "health", "fixture": FIXTURES / "poll.fixture.json",
              "observed_at": "2026-07-18T12:00:00Z"}
    first = m.execute(manifest, **kwargs)
    second = m.execute(manifest, **{**kwargs, "observed_at": "2026-07-19T12:00:00Z"})
    assert first["deletions"] == 1 and first["new_revisions"] == 2
    assert second["new_revisions"] == 0
    state = json.loads((tmp_path / "state/fixture.poll.json").read_text())
    assert state["cursor"] == "c2" and state["etag"] == "poll-v2"
    records = list((tmp_path / "archive/fixture.poll/records").glob("*/*.json"))
    assert len(records) == 2


def test_bundle_raw_response_is_content_addressed_and_idempotent(tmp_path):
    m = _load()
    manifest = _manifest(m, "bundle")
    kwargs = {"state_root": tmp_path / "state", "archive_root": tmp_path / "archive",
              "health_root": tmp_path / "health", "fixture": FIXTURES / "bundle.fixture.json",
              "observed_at": "2026-07-18T12:00:00Z"}
    m.execute(manifest, **kwargs)
    m.execute(manifest, **kwargs)
    assert len(list((tmp_path / "archive/fixture.bundle/raw").glob("*.json"))) == 1
    assert len(list((tmp_path / "archive/fixture.bundle/records").glob("*/*.json"))) == 2


def test_jsonl_raw_archive_preserves_fixture_response_bytes_exactly(tmp_path):
    m = _load()
    fixture = FIXTURES / "stream.fixture.json"
    expected = json.loads(fixture.read_text())["pages"][0]["body"].encode()
    m.execute(_manifest(m, "stream"), env={"FIXTURE_STREAM_KEY": "test"},
              state_root=tmp_path / "state", archive_root=tmp_path / "archive",
              health_root=tmp_path / "health", fixture=fixture,
              observed_at="2026-07-18T12:00:00Z")
    raw = next((tmp_path / "archive/fixture.stream/raw").glob("*.jsonl"))
    assert raw.read_bytes() == expected


def test_dry_run_does_not_resolve_secret_or_write_state(tmp_path):
    m = _load()
    manifest = _manifest(m, "enrichment")
    plan = m.execute(manifest, inputs={"entity_id": "entity-7"}, env={},
                     state_root=tmp_path / "state", archive_root=tmp_path / "archive",
                     health_root=tmp_path / "health", dry_run=True)
    assert plan["dry_run"]
    assert "<secret:FIXTURE_ENRICH_TOKEN>" in plan["request"]["headers"]["Authorization"]
    assert not list(tmp_path.rglob("*"))


def test_query_requires_declared_runtime_input(tmp_path):
    m = _load()
    with pytest.raises(m.ConnectorError, match="missing required inputs: term"):
        m.execute(_manifest(m, "query"), state_root=tmp_path, archive_root=tmp_path,
                  health_root=tmp_path, fixture=FIXTURES / "query.fixture.json")


def test_authenticated_run_requires_referenced_environment_secret(tmp_path):
    m = _load()
    with pytest.raises(m.ConnectorError, match="FIXTURE_ENRICH_TOKEN"):
        m.execute(_manifest(m, "enrichment"), inputs={"entity_id": "e"}, env={},
                  state_root=tmp_path, archive_root=tmp_path, health_root=tmp_path,
                  fixture=FIXTURES / "enrichment.fixture.json")
    health = json.loads((tmp_path / "fixture.enrichment.json").read_text())
    assert not health["ok"] and "FIXTURE_ENRICH_TOKEN" in health["error"]


def test_failed_connector_attempt_is_recorded_without_exception_text(tmp_path):
    m = _load()
    ledger = tmp_path / "ledger"
    with pytest.raises(m.ConnectorError, match="FIXTURE_ENRICH_TOKEN"):
        m.execute(_manifest(m, "enrichment"), inputs={"entity_id": "e"}, env={},
                  state_root=tmp_path / "state", archive_root=tmp_path / "archive",
                  health_root=tmp_path / "health", ledger_root=ledger,
                  fixture=FIXTURES / "enrichment.fixture.json")
    text = next(ledger.glob("attempts-*.ndjson")).read_text()
    attempt = json.loads(text)
    assert attempt["outcome"] == "failure"
    assert attempt["error_category"] == "connector-error"
    assert "FIXTURE_ENRICH_TOKEN" not in text


def test_validator_rejects_inline_secret_and_undeclared_host():
    m = _load()
    manifest = _manifest(m, "enrichment")
    manifest["request"]["headers"]["Authorization"] = "Bearer actual-secret"
    manifest["request"]["url"] = "https://escape.example/entity/x"
    errors = m.validate_manifest(manifest)
    assert any("reference a secret" in error for error in errors)
    assert any("allowed_hosts" in error for error in errors)


def test_validator_rejects_secret_templates_in_url_or_query():
    m = _load()
    manifest = _manifest(m, "enrichment")
    manifest["request"]["url"] = "https://enrich.example/${secret.token}"
    manifest["request"]["query"] = {"token": "${secret.token}"}
    errors = m.validate_manifest(manifest)
    assert "request.url must not contain secret templates" in errors
    assert "request.query values must not contain secret templates" in errors


def test_validator_rejects_retention_beyond_license():
    m = _load()
    manifest = _manifest(m, "bundle")
    manifest["archive"]["retention_days"] = 91
    manifest["license"]["max_retention_days"] = 90
    assert "archive.retention_days exceeds license.max_retention_days" in m.validate_manifest(manifest)


def test_validator_rejects_parent_paths_and_unknown_contract_keys():
    m = _load()
    manifest = _manifest(m, "bundle")
    manifest["checkpoint"]["path"] = "../escape.json"
    manifest["surprise"] = True
    errors = m.validate_manifest(manifest)
    assert any("runtime root" in error for error in errors)
    assert "unknown top-level key: surprise" in errors


def test_validator_rejects_unknown_nested_key_and_unprivileged_private_network():
    m = _load()
    manifest = _manifest(m, "bundle")
    manifest["request"]["surprise"] = True
    manifest["permissions"]["allow_private_network"] = True
    errors = m.validate_manifest(manifest)
    assert "unknown key under request: surprise" in errors
    assert "allow_private_network requires trust.permission: internal" in errors


def test_runtime_validator_enforces_schema_required_nested_keys():
    m = _load()
    manifest = _manifest(m, "query")
    del manifest["auth"]["secret_refs"]
    del manifest["request"]["method"]
    errors = m.validate_manifest(manifest)
    assert "missing required key: auth.secret_refs" in errors
    assert "missing required key: request.method" in errors


def test_private_network_permission_is_explicit_and_internal_only(monkeypatch):
    m = _load()
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (m.socket.AF_INET, m.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(m.ConnectorError, match="non-public"):
        m._validate_network_url("https://internal.example/api", ["internal.example"])
    m._validate_network_url("https://internal.example/api", ["internal.example"], True)


def test_runtime_refuses_symlink_escape_from_state_root(tmp_path):
    m = _load()
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    (state / "escape").symlink_to(outside, target_is_directory=True)
    manifest = _manifest(m, "bundle")
    manifest["checkpoint"]["path"] = "escape/state.json"
    with pytest.raises(m.ConnectorError, match="escapes configured root"):
        m.execute(manifest, state_root=state, archive_root=tmp_path / "archive",
                  health_root=tmp_path / "health", fixture=FIXTURES / "bundle.fixture.json")


def test_deletion_with_unchanged_source_revision_gets_distinct_archive_observation(tmp_path):
    m = _load()
    manifest = _manifest(m, "bundle")
    fixture = tmp_path / "fixture.json"
    kwargs = {"state_root": tmp_path / "state", "archive_root": tmp_path / "archive",
              "health_root": tmp_path / "health", "fixture": fixture}
    fixture.write_text(json.dumps({"fixture_version": 1, "pages": [{"body": {
        "objects": [{"id": "same", "modified": "v1", "deleted": False}]}}]}))
    m.execute(manifest, observed_at="one", **kwargs)
    fixture.write_text(json.dumps({"fixture_version": 1, "pages": [{"body": {
        "objects": [{"id": "same", "modified": "v1", "deleted": True}]}}]}))
    m.execute(manifest, observed_at="two", **kwargs)
    records = list((tmp_path / "archive/fixture.bundle/records/same").glob("*.json"))
    assert len(records) == 2
    assert {json.loads(path.read_text())["deleted"] for path in records} == {False, True}


def test_not_modified_fixture_records_health_without_items(tmp_path):
    m = _load()
    fixture = tmp_path / "not-modified.json"
    fixture.write_text(json.dumps({"fixture_version": 1, "pages": [{"status": 304, "body": {}}]}))
    result = m.execute(_manifest(m, "bundle"), state_root=tmp_path / "state",
                       archive_root=tmp_path / "archive", health_root=tmp_path / "health",
                       fixture=fixture, observed_at="2026-07-18T12:00:00Z")
    assert result["not_modified"] and result["requests"] == 1 and result["records"] == 0
    assert json.loads((tmp_path / "health/fixture.bundle.json").read_text())["not_modified"]


def test_cursor_and_conditional_values_are_rendered_into_next_request():
    m = _load()
    manifest = _manifest(m, "poll")
    url, headers = m._request_parts(manifest, {}, {}, {"page": 1, "cursor": "next value"},
                                    {"etag": '"v1"', "last_modified": "yesterday"})
    assert "cursor=next+value" in url
    assert headers["If-None-Match"] == '"v1"'
    assert headers["If-Modified-Since"] == "yesterday"


def test_url_path_input_is_encoded_as_one_component():
    m = _load()
    manifest = _manifest(m, "enrichment")
    url, _headers = m._request_parts(
        manifest, {"entity_id": "../../admin?x=1"}, {"token": "test"},
        {"page": 1, "cursor": ""}, {})
    assert url == "https://enrich.example/entity/..%2F..%2Fadmin%3Fx%3D1"


def test_record_without_explicit_revision_gets_stable_content_hash():
    m = _load()
    manifest = _manifest(m, "query")
    first = m._normalize(manifest, {"id": "x", "value": 1}, "one")
    second = m._normalize(manifest, copy.deepcopy({"value": 1, "id": "x"}), "two")
    assert first["source_revision"] == second["source_revision"]


def test_cli_fixture_mode_prints_normalized_result(tmp_path, capsys):
    m = _load()
    rc = m.main(["--manifest", str(FIXTURES / "query.yaml"),
                 "--fixture", str(FIXTURES / "query.fixture.json"),
                 "--param", "term=needle", "--state-root", str(tmp_path / "state"),
                 "--archive-root", str(tmp_path / "archive"),
                 "--health-root", str(tmp_path / "health"),
                 "--observed-at", "2026-07-18T12:00:00Z"])
    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["items"][0]["source_native_id"] == "result-1"
    assert output["wakeAgent"] is False


def test_cli_cron_mode_suppresses_payload_and_wakes_only_for_new_revision(tmp_path, capsys):
    m = _load()
    args = ["--manifest", str(FIXTURES / "bundle.yaml"),
            "--fixture", str(FIXTURES / "bundle.fixture.json"),
            "--state-root", str(tmp_path / "state"),
            "--archive-root", str(tmp_path / "archive"),
            "--health-root", str(tmp_path / "health"), "--summary-only", "--wake-on-new",
            "--observed-at", "2026-07-18T12:00:00Z"]
    assert m.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["wakeAgent"] is True and "items" not in first
    assert m.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["wakeAgent"] is False and second["new_revisions"] == 0
