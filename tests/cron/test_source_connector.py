from __future__ import annotations

import copy
import importlib.util
import json
import socket
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.contract
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


def test_validator_reports_every_malformed_contract_shape():
    m = _load()
    manifest = {
        "connector_version": 99,
        "id": "X",
        "mode": "bad",
        "trust": {
            "permission": "bad",
            "data_sensitivity": "bad",
            "source_authority": "",
            "extra": True,
        },
        "permissions": {
            "network": False,
            "allowed_hosts": ["UPPER.EXAMPLE", ""],
            "write_raw": "yes",
            "allow_private_network": "yes",
        },
        "auth": {
            "type": "bad",
            "secret_refs": {"token": "not env"},
        },
        "inputs": "bad",
        "request": {
            "method": "POST",
            "url": "ftp://user:pass@example.test/path",
            "timeout_seconds": 0,
            "max_bytes": m.MAX_BYTES + 1,
            "headers": [],
            "query": [],
        },
        "response": {
            "format": "xml",
            "records_path": 3,
            "stable_id_path": "",
            "revision_path": 3,
            "deleted_path": [],
        },
        "pagination": {
            "type": "bad",
            "max_pages": 0,
            "start": -1,
        },
        "checkpoint": {"path": ""},
        "conditional_requests": {"enabled": "yes"},
        "rate_limit": {"max_requests": 0, "per_seconds": -1},
        "archive": {
            "enabled": "yes",
            "raw_responses": "yes",
            "path": "/absolute",
            "retention_days": -1,
        },
        "license": {
            "name": "",
            "url": 3,
            "redistribution": "bad",
            "max_retention_days": -1,
        },
        "health": {"path": "../escape"},
        "extra": True,
    }
    errors = m.validate_manifest(manifest)
    joined = "\n".join(errors)
    for expected in (
        "unknown top-level",
        "connector_version",
        "id must",
        "mode must",
        "unknown key under trust",
        "trust.permission",
        "data_sensitivity",
        "source_authority",
        "permissions.network",
        "allowed_hosts",
        "write_raw",
        "allow_private_network",
        "auth.type",
        "secret_refs",
        "inputs must",
        "request.method",
        "http(s)",
        "timeout_seconds",
        "max_bytes",
        "headers",
        "query",
        "response.format",
        "records_path",
        "stable_id_path",
        "revision_path",
        "deleted_path",
        "pagination.type",
        "max_pages",
        "pagination.start",
        "checkpoint.path",
        "conditional_requests.enabled",
        "rate_limit.max_requests",
        "rate_limit.per_seconds",
        "archive.enabled",
        "archive.path",
        "retention_days",
        "license.name",
        "license.url",
        "license.redistribution",
        "max_retention_days",
        "health.path",
    ):
        assert expected in joined


def test_validator_enrichment_pagination_auth_and_template_edges():
    m = _load()
    base = _manifest(m, "query")
    base["enrich"] = "bad"
    assert {"enrich: only valid for mode: enrichment", "enrich: must be a mapping"} <= set(
        m.validate_manifest(base)
    )

    manifest = _manifest(m, "enrichment")
    manifest["enrich"] = {
        "authority": "BAD!",
        "id_path": "",
        "match": {
            "query_input": "undeclared",
            "page_field": "",
            "candidate_paths": [],
        },
        "targets": {},
        "extra": True,
    }
    errors = "\n".join(m.validate_manifest(manifest))
    for expected in (
        "unknown key",
        "enrich.authority",
        "enrich.id_path",
        "page_field",
        "candidate_paths",
        "not a required manifest input",
        "targets.types",
    ):
        assert expected in errors

    manifest = _manifest(m, "query")
    manifest["auth"] = {"type": "none", "secret_refs": {"token": "TOKEN"}}
    manifest["request"]["url"] = "https://query.example/${input.missing}/${runtime.bad}"
    manifest["request"]["headers"] = {
        "X-Test": "${secret.missing}",
        "X-Bad": "${unsupported.value}",
    }
    errors = "\n".join(m.validate_manifest(manifest))
    assert "auth.type none cannot declare" in errors
    assert "input not declared" in errors
    assert "unsupported runtime" in errors
    assert "undeclared secret" in errors
    assert "unsupported or malformed template" in errors

    for kind, block, expected in (
        ("page", {"type": "page", "max_pages": 1}, "page pagination requires"),
        ("cursor", {"type": "cursor", "max_pages": 1}, "cursor pagination requires"),
    ):
        manifest = _manifest(m, "bundle")
        manifest["pagination"] = block
        assert expected in "\n".join(m.validate_manifest(manifest))


def test_validator_remaining_cross_field_edges():
    m = _load()
    manifest = _manifest(m, "enrichment")
    manifest["enrich"] = {
        "authority": "authority",
        "id_path": "id",
        "match": "bad",
        "targets": {"types": ["entity"]},
    }
    assert "enrich.match: required mapping" in m.validate_manifest(manifest)

    manifest = _manifest(m, "bundle")
    del manifest["health"]
    assert "missing required key: health" in m.validate_manifest(manifest)

    manifest = _manifest(m, "query")
    manifest["inputs"] = {"required": [3]}
    assert "inputs.required must be a list of names" in m.validate_manifest(manifest)

    manifest = _manifest(m, "query")
    manifest["request"]["url"] = "https://user:pass@query.example/x"
    assert "request.url must not contain credentials" in m.validate_manifest(manifest)

    manifest = _manifest(m, "bundle")
    manifest["archive"]["enabled"] = True
    manifest["permissions"]["write_raw"] = True
    manifest["license"]["redistribution"] = "prohibited"
    manifest["trust"]["data_sensitivity"] = "clear"
    assert "prohibited redistribution cannot use clear" in "\n".join(
        m.validate_manifest(manifest)
    )


def test_private_network_permission_is_explicit_and_internal_only(monkeypatch):
    m = _load()
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (m.socket.AF_INET, m.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(m.ConnectorError, match="non-public"):
        m._validate_network_url("https://internal.example/api", ["internal.example"])
    m._validate_network_url("https://internal.example/api", ["internal.example"], True)


def test_yaml_lookup_render_state_and_decode_failure_edges(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "yaml", None)
    with pytest.raises(m.ConnectorError, match="PyYAML"):
        m.load_yaml(tmp_path / "x.yaml")
    monkeypatch.setattr(m, "yaml", yaml)
    bad = tmp_path / "bad.yaml"
    bad.write_text("invalid: [")
    with pytest.raises(m.ConnectorError, match="cannot read"):
        m.load_yaml(bad)
    bad.write_text("- item\n")
    with pytest.raises(m.ConnectorError, match="expected a YAML mapping"):
        m.load_yaml(bad)

    errors = []
    assert m._mapping([], "request", errors) == {}
    assert "must be a mapping" in errors[0]
    errors.clear()
    m._relative_path(None, "optional", errors, required=False)
    assert errors == []
    assert m._url_host(3) == ""
    assert m._url_host("https://EXAMPLE.test/x") == "example.test"

    value = {"rows": [{"name": "first"}]}
    assert m._lookup(value, "rows.0.name") == "first"
    assert m._lookup(value, "rows.9.name", "fallback") == "fallback"
    with pytest.raises(m.ConnectorError, match="unresolved template"):
        m._render("${input.missing}", {}, {}, {})
    assert m._render("${input.term}", {"term": "a/b"}, {}, {}, url_component=True) == "a%2Fb"

    state = tmp_path / "state.json"
    state.write_text("{bad")
    with pytest.raises(m.ConnectorError, match="invalid checkpoint"):
        m._load_state(state)
    state.write_text("[]")
    with pytest.raises(m.ConnectorError, match="expected an object"):
        m._load_state(state)

    invalid_fixture = tmp_path / "fixture.json"
    invalid_fixture.write_text("{bad")
    with pytest.raises(m.ConnectorError, match="invalid fixture"):
        m._load_fixture(invalid_fixture)
    invalid_fixture.write_text('{"fixture_version":2,"pages":[]}')
    with pytest.raises(m.ConnectorError, match="fixture requires"):
        m._load_fixture(invalid_fixture)
    invalid_fixture.write_text('{"fixture_version":1,"pages":[{"body":3}]}')
    with pytest.raises(m.ConnectorError, match=r"pages\[0\]"):
        m._load_fixture(invalid_fixture)

    response = {"format": "json", "records_path": ""}
    with pytest.raises(m.ConnectorError, match="not valid json"):
        m._decode_records(m.ResponsePage(200, {}, b"{bad", "x"), response)
    with pytest.raises(m.ConnectorError, match="did not resolve"):
        m._decode_records(m.ResponsePage(200, {}, b"3", "x"), response)
    decoded, records = m._decode_records(
        m.ResponsePage(200, {}, b'{"id":"one"}', "x"), response
    )
    assert records == [{"id": "one"}] and decoded == {"id": "one"}
    ndjson = {"format": "jsonl", "records_path": ""}
    assert len(m._decode_records(
        m.ResponsePage(200, {}, b'{"id":1}\n\n{"id":2}\n', "x"), ndjson
    )[1]) == 2


def test_network_and_http_transport_edges(monkeypatch):
    m = _load()
    for url in ("ftp://example.test", "https://other.test"):
        with pytest.raises(m.ConnectorError, match="network permissions"):
            m._validate_network_url(url, ["example.test"])
    with pytest.raises(m.ConnectorError, match="credentials"):
        m._validate_network_url(
            "https://user:pass@example.test", ["example.test"], True
        )
    monkeypatch.setattr(
        m.socket,
        "getaddrinfo",
        lambda *_a, **_kw: (_ for _ in ()).throw(socket.gaierror("dns")),
    )
    with pytest.raises(m.ConnectorError, match="cannot resolve"):
        m._validate_network_url("https://example.test", ["example.test"])
    monkeypatch.setattr(
        m.socket,
        "getaddrinfo",
        lambda *_a, **_kw: [
            (m.socket.AF_INET, m.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    m._validate_network_url("https://example.test", ["example.test"])

    redirect = m._SafeRedirect(["example.test"], True)
    monkeypatch.setattr(
        m.urllib.request.HTTPRedirectHandler,
        "redirect_request",
        lambda self, req, fp, code, msg, headers, newurl: newurl,
    )
    assert redirect.redirect_request(None, None, 302, "", {}, "https://example.test/x") \
        == "https://example.test/x"

    manifest = _manifest(m, "bundle")
    manifest["permissions"]["allow_private_network"] = True
    manifest["request"]["max_bytes"] = 4

    class Response:
        status = 201
        headers = {"Content-Length": "4", "X-Test": "yes"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://sources.example/final"

        def read(self, _limit):
            return b"data"

    opener = SimpleNamespace(open=lambda *_a, **_kw: Response())
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_a: opener)
    page = m._open_http("https://sources.example/start", {}, manifest)
    assert page.status == 201 and page.headers["x-test"] == "yes"

    Response.headers = {"Content-Length": "5"}
    with pytest.raises(m.ConnectorError, match="Content-Length"):
        m._open_http("https://sources.example/start", {}, manifest)
    Response.headers = {}
    Response.read = lambda self, _limit: b"12345"
    with pytest.raises(m.ConnectorError, match="response exceeds"):
        m._open_http("https://sources.example/start", {}, manifest)

    for error, expected in (
        (urllib.error.HTTPError("x", 304, "", {}, None), m.NotModified),
        (urllib.error.HTTPError("x", 500, "", {}, None), m.ConnectorError),
        (urllib.error.URLError("down"), m.ConnectorError),
    ):
        opener.open = lambda *_a, _error=error, **_kw: (_ for _ in ()).throw(_error)
        with pytest.raises(expected):
            m._open_http("https://sources.example/start", {}, manifest)


def test_normalize_request_and_parameter_edges():
    m = _load()
    manifest = _manifest(m, "bundle")
    for invalid in (None, [], {}, ""):
        record = {"id": invalid}
        with pytest.raises(m.ConnectorError, match="stable ID"):
            m._normalize(manifest, record, "now")
    envelope = m._normalize(manifest, {"id": "x", "modified": []}, "now")
    assert len(envelope["source_revision"]) == 64
    assert len(m._safe_component("!!!")) == 16

    request_manifest = _manifest(m, "query")
    request_manifest["request"].setdefault("headers", {})["X-Bad"] = "${input.term}"
    with pytest.raises(m.ConnectorError, match="newline"):
        m._request_parts(
            request_manifest, {"term": "a\nb"}, {}, {"page": 1, "cursor": ""}, {}
        )
    with pytest.raises(m.ConnectorError, match="invalid --param"):
        m._params(["bad"])
    with pytest.raises(m.ConnectorError, match="invalid --param"):
        m._params(["=value"])
    assert m._params(["a=1", "a=2"]) == {"a": "2"}


def test_execute_network_paging_fixture_exhaustion_policy_and_error_ledger(
    tmp_path, monkeypatch
):
    m = _load()
    invalid = _manifest(m, "bundle")
    invalid["mode"] = "bad"
    with pytest.raises(m.ConnectorError, match="manifest invalid"):
        m.execute(
            invalid,
            state_root=tmp_path / "state-invalid",
            archive_root=tmp_path / "archive-invalid",
            health_root=tmp_path / "health-invalid",
        )

    manifest = _manifest(m, "bundle")
    manifest["pagination"] = {
        "type": "page",
        "request_param": "page",
        "start": 1,
        "max_pages": 2,
    }
    manifest["rate_limit"] = {"max_requests": 2, "per_seconds": 2}
    pages = iter(
        [
            m.ResponsePage(
                200, {}, b'{"objects":[{"id":"one","modified":"v1"}]}', "fixture"
            ),
            m.ResponsePage(200, {}, b'{"objects":[]}', "fixture"),
        ]
    )
    monkeypatch.setattr(m, "_open_http", lambda *_a, **_kw: next(pages))
    sleeps = []
    result = m.execute(
        manifest,
        state_root=tmp_path / "state",
        archive_root=tmp_path / "archive",
        health_root=tmp_path / "health",
        sleep=sleeps.append,
    )
    assert result["requests"] == 2 and sleeps == [1.0]

    manifest["rate_limit"]["per_seconds"] = 0
    pages = iter(
        [
            m.ResponsePage(
                200, {}, b'{"objects":[{"id":"two","modified":"v1"}]}', "fixture"
            ),
            m.ResponsePage(200, {}, b'{"objects":[]}', "fixture"),
        ]
    )
    monkeypatch.setattr(m, "_open_http", lambda *_a, **_kw: next(pages))
    assert m.execute(
        manifest,
        state_root=tmp_path / "state-zero",
        archive_root=tmp_path / "archive-zero",
        health_root=tmp_path / "health-zero",
        sleep=lambda _delay: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )["requests"] == 2

    fixture = tmp_path / "one-page.json"
    fixture.write_text(json.dumps({
        "fixture_version": 1,
        "pages": [{"body": {"objects": [{"id": "three", "modified": "v1"}]}}],
    }))
    assert m.execute(
        manifest,
        state_root=tmp_path / "state-fixture",
        archive_root=tmp_path / "archive-fixture",
        health_root=tmp_path / "health-fixture",
        fixture=fixture,
    )["requests"] == 1

    fixture.write_text(json.dumps({
        "fixture_version": 1,
        "pages": [{"status": 500, "body": {"objects": []}}],
    }))
    with pytest.raises(m.ConnectorError, match="fixture HTTP 500"):
        m.execute(
            manifest,
            state_root=tmp_path / "state-http",
            archive_root=tmp_path / "archive-http",
            health_root=tmp_path / "health-http",
            fixture=fixture,
        )

    fixture.write_text(json.dumps({
        "fixture_version": 1,
        "pages": [{"body": {"objects": [{"id": "four", "modified": "v1"}]}}],
    }))
    monkeypatch.setattr(
        m,
        "policy_plane",
        SimpleNamespace(
            validate_importer_envelope=lambda *_a, **_kw: {"blocked": True},
            finding_message=lambda _result: "policy blocked",
        ),
    )
    monkeypatch.setattr(
        m.collection_ledger,
        "append_attempt",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("ledger down")),
    )
    with pytest.raises(m.ConnectorError, match="policy blocked"):
        m.execute(
            manifest,
            state_root=tmp_path / "state-policy",
            archive_root=tmp_path / "archive-policy",
            health_root=tmp_path / "health-policy",
            ledger_root=tmp_path / "ledger-policy",
            fixture=fixture,
        )


def test_main_connector_error_summary_and_wake_modes(tmp_path, monkeypatch, capsys):
    m = _load()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("invalid: [")
    assert m.main(["--manifest", str(manifest)]) == 2
    assert "ERROR:" in capsys.readouterr().err

    monkeypatch.setattr(m, "load_yaml", lambda _path: {})
    monkeypatch.setattr(
        m,
        "execute",
        lambda *_a, **_kw: {"items": [1], "new_revisions": 1, "ok": True},
    )
    assert m.main([
        "--manifest", str(manifest), "--wake-on-new", "--summary-only",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["wakeAgent"] is True and "items" not in output
    assert m.main([
        "--manifest", str(manifest), "--wake-on-new", "--dry-run",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["wakeAgent"] is False


def test_execute_zero_request_guard_and_no_policy_adapter(tmp_path, monkeypatch):
    m = _load()
    manifest = _manifest(m, "bundle")
    monkeypatch.setattr(m, "validate_manifest", lambda _manifest: [])
    manifest["rate_limit"]["max_requests"] = 0
    result = m.execute(
        manifest,
        state_root=tmp_path / "state-zero",
        archive_root=tmp_path / "archive-zero",
        health_root=tmp_path / "health-zero",
    )
    assert result["requests"] == 0

    manifest = _manifest(m, "bundle")
    monkeypatch.setattr(m, "policy_plane", None)
    result = m.execute(
        manifest,
        state_root=tmp_path / "state-no-policy",
        archive_root=tmp_path / "archive-no-policy",
        health_root=tmp_path / "health-no-policy",
        fixture=FIXTURES / "bundle.fixture.json",
    )
    assert result["records"] == 2


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
