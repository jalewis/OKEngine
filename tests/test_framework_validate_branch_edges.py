"""Branch-focused coverage for framework_validate's defensive pack checks."""
from __future__ import annotations

import importlib
import importlib.util
import builtins
import json
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "framework_validate.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _messages(report):
    return "\n".join(f"{s} {c} {d}" for s, c, d in report.rows)


def test_schema_shape_and_optional_engine_input_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_schema")
    r = v.Report()
    v.check_schema(tmp_path, r)
    assert "missing" in _messages(r)

    schema = tmp_path / "schema.yaml"
    schema.write_text("- scalar\n")
    r = v.Report()
    v.check_schema(tmp_path, r)
    assert "top level is not a mapping" in _messages(r)

    schema.write_text(
        "okf: bad\n"
        "types:\n  broken: scalar\n"
        "partitioning: []\n"
        "hot_set: []\n"
        "permissions: {}\nreview: {}\ntier: {}\n"
        "type_aliases: []\nclassify_hints: []\n"
        "operational_types: bad\nclassify_catchall: [missing]\n"
        "depth_critical_types: [missing]\nprotected_fields: [field]\n")
    r = v.Report()
    v.check_schema(tmp_path, r)
    text = _messages(r)
    assert "schema.types" in text
    assert "schema.type_aliases" in text
    assert "schema.classify_hints" in text
    assert "schema.operational_types" in text

    old_yaml = v.yaml
    monkeypatch.setattr(v, "yaml", None)
    r = v.Report()
    v.check_schema(tmp_path, r)
    assert "PyYAML unavailable" in _messages(r)
    monkeypatch.setattr(v, "yaml", old_yaml)


def test_prompt_vintage_subdomain_and_persona_branches(tmp_path):
    v = _load("framework_validate_branch_content")
    (tmp_path / "schema.yaml").write_text("types: {}\npartitioning: {namespaces: {}}\n")
    crons = tmp_path / "crons"
    crons.mkdir()
    (crons / "domain-crons.json").write_text(
        '{"prompt":"type: alien and [[unknown-space/page]]"}')
    r = v.Report()
    v.check_prompt_residue(tmp_path, r)
    assert _messages(r).count("prompt residue") >= 2

    (tmp_path / "validate.py").write_text("# old validator\n")
    r = v.Report()
    v.check_validator_vintage(tmp_path, r)
    assert "no VALIDATE_VERSION" in _messages(r)
    (tmp_path / "validate.py").write_text('VALIDATE_VERSION = "old"\n')
    r = v.Report()
    v.check_validator_vintage(tmp_path, r)
    assert "vs skeleton" in _messages(r)

    sub = tmp_path / "subdomain"
    sub.mkdir()
    (sub / "bad.yaml").write_text("[broken\n")
    (sub / "schema.yaml").write_text(
        "types:\n  absent: {required: [type]}\n"
        "partitioning: {namespaces: {absent: {strategy: flat}}}\n")
    r = v.Report()
    v.check_subdomain_form(tmp_path, r)
    text = _messages(r)
    assert "unparseable" in text
    assert "not in the main schema" in text
    assert "ships no INSTALL doc" in text

    persona = tmp_path / "CLAUDE.md"
    persona.write_text("short")
    r = v.Report()
    v.check_persona(tmp_path, r)
    assert "effectively empty" in _messages(r)
    persona.write_text("x" * 100 + " Replace the placeholders")
    r = v.Report()
    v.check_persona(tmp_path, r)
    assert "placeholder" in _messages(r)


def test_engine_version_fallback_compatibility_and_hermes_warning(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_engine_version")
    ev = tmp_path / "engine.version"
    ev.write_text("unparseable: [\nrelease v1.2.3\n")

    monkeypatch.setattr(v, "_engine_meta_mod",
                        lambda: (_ for _ in ()).throw(RuntimeError("no manifest")))
    r = v.Report()
    v.check_engine_version(tmp_path, r)
    assert any(s == "OK" for s, _c, _d in r.rows)

    ev.write_text("version: v1.2.3\nhermes_pin: old\n")
    meta = SimpleNamespace(
        engine_release=lambda: "v1.2.4",
        hermes_pin=lambda: "new",
        satisfies_pin=lambda pin, target: True,
    )
    monkeypatch.setattr(v, "_engine_meta_mod", lambda: meta)
    r = v.Report()
    v.check_engine_version(tmp_path, r)
    text = _messages(r)
    assert "compatible" in text
    assert "hermes_pin" in text


def test_pack_meta_bundle_and_owns_early_return_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_pack_meta")
    r = v.Report()
    v.check_pack_meta(tmp_path, r)
    assert "absent" in _messages(r)
    assert not v._is_bundle(tmp_path)

    (tmp_path / "pack.yaml").write_text("name: pack\n")
    fake = SimpleNamespace(
        load_pack_meta=lambda _pack: None,
        validate_bundle_recipe=lambda _meta: ["bad recipe"],
    )
    monkeypatch.setattr(v, "_pack_meta_mod", lambda: fake)
    r = v.Report()
    v.check_pack_meta(tmp_path, r)
    assert "unparseable" in _messages(r)

    fake.load_pack_meta = lambda _pack: (_ for _ in ()).throw(RuntimeError("boom"))
    r = v.Report()
    v.check_pack_meta(tmp_path, r)
    assert "could not load" in _messages(r)
    assert not v._is_bundle(tmp_path)

    fake.load_pack_meta = lambda _pack: {"kind": "bundle"}
    assert v._is_bundle(tmp_path)
    r = v.Report()
    v.check_bundle(tmp_path, r)
    assert "bad recipe" in _messages(r)

    r = v.Report()
    v.check_owns_covers_schema(tmp_path, r)
    assert r.rows == []


def test_extension_requirement_and_enabled_resolution_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_extensions")
    (tmp_path / "pack.yaml").write_text("name: pack\n")
    (tmp_path / "schema.yaml").write_text(
        "owners:\n  types: {thing: 'ext:owner'}\n")
    pm = SimpleNamespace(
        load_pack_meta=lambda _pack: {"ok": True},
        extension_requires=lambda _meta: [
            ("missing", None), ("old", ">=2.0.0"), ("good", None)],
        satisfies=lambda actual, spec: False,
    )
    monkeypatch.setattr(v, "_pack_meta_mod", lambda: pm)
    disc = SimpleNamespace(resolve_for_pack=lambda _pack: ({
        "old": {"manifest": {"version": "1.0.0"}},
        "good": {"manifest": {"version": "1.0.0"}},
    }, ["resolution warning"]))
    monkeypatch.setattr(v, "_discovery_mod", lambda: disc)
    r = v.Report()
    v.check_extension_requirements(tmp_path, r)
    text = _messages(r)
    assert "not enabled" in text
    assert "version floor not met" in text
    assert "requires ext:good" in text
    assert "schema owner ext:owner" in text

    state = tmp_path / ".okengine/extensions.yaml"
    state.parent.mkdir()
    state.write_text("enabled: {}\n")
    monkeypatch.setattr(v, "_discovery_mod",
                        lambda: SimpleNamespace(discover=lambda _p: (_ for _ in ()).throw(
                            RuntimeError("discovery crash"))))
    r = v.Report()
    v.check_enabled_extensions_resolve(tmp_path, r)
    assert "could not resolve" in _messages(r)

    good_disc = SimpleNamespace(
        discover=lambda _p: ([{"id": "x"}], []),
        load_enabled_state=lambda _p: ({"x": {}}, []),
        resolve_enabled=lambda enabled, discovered: ({"x": {}}, []),
        stale_disabled_errors=lambda _p, _d: [],
    )
    monkeypatch.setattr(v, "_discovery_mod", lambda: good_disc)
    r = v.Report()
    v.check_enabled_extensions_resolve(tmp_path, r)
    assert "all discovered" in _messages(r)

    stale_disabled_disc = SimpleNamespace(
        discover=lambda _p: ([{"id": "okengine.current"}], []),
        load_enabled_state=lambda _p: ({}, []),
        resolve_enabled=lambda enabled, discovered: ({}, []),
        stale_disabled_errors=lambda _p, _d: [
            "FAIL: disabled extension 'okengine.renamed' is not discovered in any tier"],
    )
    monkeypatch.setattr(v, "_discovery_mod", lambda: stale_disabled_disc)
    r = v.Report()
    v.check_enabled_extensions_resolve(tmp_path, r)
    assert "disabled extension 'okengine.renamed' is not discovered" in _messages(r)


def test_application_adapter_and_main_missing_pack(tmp_path, monkeypatch, capsys):
    v = _load("framework_validate_branch_adapter")
    assert v.main([str(tmp_path / "missing")]) == 2
    assert "pack dir not found" in capsys.readouterr().err

    declaration = tmp_path / ".okengine/application.yaml"
    declaration.parent.mkdir()
    declaration.write_text("profile: bad\n")
    # A malformed declaration drives the adapter's error-reporting loop.
    r = v.Report()
    v.check_application_profile(tmp_path, r)
    assert any(s == "FAIL" for s, _c, _d in r.rows)


def test_feed_empty_overlay_probe_and_oversize_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_feeds")
    (tmp_path / "pack.yaml").write_text(
        "collection: {mode: overlay, feeds: none}\n")
    r = v.Report()
    v.check_feeds(tmp_path, r, False)
    assert "declared collection contract" in _messages(r)

    feeds = tmp_path / "feeds"
    feeds.mkdir()
    empty = feeds / "empty.opml"
    empty.write_text("<opml><body/></opml>")
    r = v.Report()
    v.check_feeds(tmp_path, r, False)
    assert "0 active feed URLs" in _messages(r)

    empty.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    r = v.Report()
    v.check_feeds(tmp_path, r, False)
    assert "safety limit" in _messages(r)

    empty.write_text(
        '<opml><body><outline xmlUrl="ftp://bad/path"/>'
        '<outline xmlUrl="https://example.test/feed"/></body></opml>')

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: Response())
    r = v.Report()
    v.check_feeds(tmp_path, r, True)
    assert "1/2 unreachable" in _messages(r)

    empty.write_text(
        '<opml><body><outline xmlUrl="https://example.test/feed"/></body></opml>')
    r = v.Report()
    v.check_feeds(tmp_path, r, True)
    assert "1/1 live" in _messages(r)


def test_cron_shapes_contracts_manifest_and_compile_branches(tmp_path):
    v = _load("framework_validate_branch_crons")
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "absent" in _messages(r)

    crons = tmp_path / "crons"
    scripts = crons / "scripts"
    scripts.mkdir(parents=True)
    (crons / "engine-template-prompts.json").write_text("[]")
    (crons / "domain-crons.json").write_text(json.dumps([
        "scalar",
        {"name": "empty"},
        {
            "name": "writer", "schedule": {"expr": "0 * * * *"},
            "prompt": "work", "enabled_toolsets": ["okengine-write"],
        },
        {
            "name": "contract", "schedule": "0 * * * *",
            "script": "missing.py", "output_contract": {},
            "adversarial_fixtures": [],
        },
        {
            "name": "receipt", "schedule": {"expr": "0 * * * *"},
            "prompt": "work",
            "output_contract": {"completion": "per-selected-item"},
            "adversarial_fixtures": ["test.py"],
        },
        {
            "name": "artifact-good", "schedule": {"expr": "0 * * * *"},
            "script": "missing.py", "no_agent": True,
            "artifact_contract": {"api": 1, "min_artifacts": 0},
        },
        {
            "name": "artifact-not-boolean", "schedule": {"expr": "0 * * * *"},
            "script": "missing.py", "no_agent": 1,
            "artifact_contract": {"api": 1},
        },
        {
            "name": "artifact-invalid", "schedule": {"expr": "0 * * * *"},
            "script": "missing.py", "no_agent": True,
            "artifact_contract": {"api": 2, "surprise": True},
        },
    ]))
    (scripts / "broken.py").write_text("def x(:\n")
    r = v.Report()
    v.check_crons(tmp_path, r)
    text = _messages(r)
    assert "must be a JSON object" in text
    assert "entry missing name" in text
    assert "no usable schedule expr" in text
    assert "must declare output_contract" in text
    assert "adversarial_fixtures" in text
    assert "no selector script" in text
    assert "syntax errors" in text
    assert "artifact-not-boolean' artifact_contract is only valid for no_agent" in text
    assert text.count("artifact_contract is only valid for no_agent") == 1
    assert "artifact-invalid' artifact_contract.api must be 1" in text
    assert "artifact-invalid' artifact_contract has unknown key" in text
    assert "artifact-contract validator unavailable" not in text

    (crons / "engine-template-prompts.json").write_text(json.dumps({
        "empty": {"prompt": "", "unknown": True, "output_contract": {}},
    }))
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "empty prompt" in _messages(r)
    assert "unknown key" in _messages(r)


def test_env_dotenv_vault_and_runtime_shape_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_runtime")
    example = tmp_path / ".env.example"
    example.write_text("OTHER=value\n")
    r = v.Report()
    v.check_env(tmp_path, r)
    assert "no model-provider key" in _messages(r)

    env = tmp_path / ".env"
    env.write_text("# c\nbad\nA='one'\nB=\"two\"\n")
    assert v._read_dotenv(tmp_path) == {"A": "one", "B": "two"}
    # okengine#993 R1: compose resolves `${X}` from the process, then the whole .env.
    env.write_text("E=${PROC_ONLY_993}\nF=${LATER}\nLATER=1\n")
    monkeypatch.setenv("PROC_ONLY_993", "0")
    assert v._read_dotenv(tmp_path) == {"E": "0", "F": "1", "LATER": "1"}
    env.write_text("# c\nbad\nA='one'\nB=\"two\"\n")
    monkeypatch.setattr(v.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    r = v.Report()
    v.check_env(tmp_path, r)
    assert "git-TRACKED" in _messages(r)

    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  one: {environment: {WIKI_PATH: /opt/vault}}\n"
        "  scalar: nope\n"
        "  two: {environment: ['WIKI_PATH=/srv/vault']}\n")
    r = v.Report()
    v.check_vault_mount(tmp_path, r)
    assert "services disagree" in _messages(r)
    compose.write_text("services:\n  one: {environment: []}\n")
    r = v.Report()
    v.check_vault_mount(tmp_path, r)
    assert "defaults to /opt/vault" in _messages(r)

    cfg = tmp_path / ".hermes-data/config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("- scalar\n")
    r = v.Report()
    v.check_runtime_config(tmp_path, r)
    assert "top level is not a mapping" in _messages(r)
    cfg.write_text("[broken\n")
    r = v.Report()
    v.check_runtime_config(tmp_path, r)
    assert "YAML error" in _messages(r)
    cfg.write_text(
        "terminal: {backend: local}\n"
        "mcp_servers:\n"
        "  okengine: {headers: {Authorization: Basic-token}}\n"
        "  okengine-write: {}\n"
        "  okengine-write-source-quality: {}\n")
    monkeypatch.setattr(v, "_model_profiles_mod",
                        lambda: SimpleNamespace(validate_qwen_no_fallback=lambda _d: []))
    r = v.Report()
    v.check_runtime_config(tmp_path, r)
    assert "not a `Bearer" in _messages(r)


def test_source_connector_adapter_all_outcomes(tmp_path):
    v = _load("framework_validate_branch_connectors")
    connectors = tmp_path / "connectors"
    connectors.mkdir()
    r = v.Report()
    v.check_source_connectors(tmp_path, r)
    assert "contains no" in _messages(r)

    (connectors / "scalar.yaml").write_text("- nope\n")
    (connectors / "invalid.yml").write_text("id: x\n")
    shutil.copy(REPO / "tests/fixtures/source_connectors/poll.yaml",
                connectors / "valid.yaml")
    r = v.Report()
    v.check_source_connectors(tmp_path, r)
    text = _messages(r)
    assert "expected a YAML mapping" in text
    assert "missing required key" in text
    assert "poll connector" in text


def test_misc_validator_defensive_and_success_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_misc")

    (tmp_path / "schema.yaml").write_text(
        "types: [bad]\npartitioning:\n  namespaces:\n    x: {strategy: invented}\n")
    r = v.Report()
    v.check_schema(tmp_path, r)
    assert "unknown strategy" in _messages(r)

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("[broken\n")
    r = v.Report()
    v.check_compose_drift(tmp_path, r)
    assert "unparseable" in _messages(r)
    r = v.Report()
    v.check_gateway_env(tmp_path, r)
    v.check_vault_mount(tmp_path, r)
    assert r.rows == []

    crons = tmp_path / "crons"
    crons.mkdir()
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "absent (no domain crons)" in _messages(r)
    (crons / "engine-template-prompts.json").write_text("{broken")
    (crons / "domain-crons.json").write_text("{}")
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "must be a JSON array" in _messages(r)
    assert "JSON error" in _messages(r)

    installed = tmp_path / ".okengine/installed-domains"
    installed.mkdir(parents=True)
    monkeypatch.setattr(importlib.util, "spec_from_file_location",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("detector")))
    r = v.Report()
    v.check_installed_domain_drift(tmp_path, r)
    assert "detector failed" in _messages(r)


def test_model_profile_collection_and_load_errors(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_profiles")
    state = tmp_path / ".okengine"
    state.mkdir()
    (state / "model-profiles.yaml").write_text("bad")
    monkeypatch.setattr(v, "_model_profiles_mod", lambda: SimpleNamespace(
        load_profiles=lambda _p: (_ for _ in ()).throw(RuntimeError("bad profiles"))))
    r = v.Report()
    v.check_model_profiles(tmp_path, r)
    assert "bad profiles" in _messages(r)

    crons = tmp_path / "crons"
    crons.mkdir()
    (crons / "domain-crons.json").write_text("{bad")
    (state / "extension-models.json").write_text("{bad")
    mp = SimpleNamespace(is_ref=lambda value: str(value).startswith("@"),
                         ref_name=lambda value: str(value)[1:])
    assert v._collect_model_refs(tmp_path, mp) == set()


def test_model_profile_resolution_outcomes(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_profile_outcomes")
    state = tmp_path / ".okengine"
    crons = tmp_path / "crons"
    state.mkdir()
    crons.mkdir()
    (crons / "domain-crons.json").write_text(json.dumps([
        {"model": "@known"}, {"model": "literal"}, "scalar"]))
    (state / "extension-models.json").write_text(json.dumps(
        {"one": "@missing", "two": "literal"}))
    mp = SimpleNamespace(
        load_profiles=lambda _p: {"known": {}},
        validate_profiles=lambda _p: [],
        is_ref=lambda value: isinstance(value, str) and value.startswith("@"),
        ref_name=lambda value: value[1:],
    )
    monkeypatch.setattr(v, "_model_profiles_mod", lambda: mp)
    r = v.Report()
    v.check_model_profiles(tmp_path, r)
    assert "referenced but" in _messages(r)

    (state / "model-profiles.yaml").write_text("known: {}\n")
    mp.validate_profiles = lambda _p: ["shape one", "shape two"]
    r = v.Report()
    v.check_model_profiles(tmp_path, r)
    assert _messages(r).count("shape") == 2

    mp.validate_profiles = lambda _p: []
    r = v.Report()
    v.check_model_profiles(tmp_path, r)
    assert "undefined profile" in _messages(r)
    mp.load_profiles = lambda _p: {"known": {}, "missing": {}}
    r = v.Report()
    v.check_model_profiles(tmp_path, r)
    assert "reference(s) resolve" in _messages(r)


def test_docs_pack_bundle_and_schema_owner_success_branches(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_metadata")
    (tmp_path / "README.md").write_text("# Pack\n\n## Deploy\n" + "details " * 40)
    (tmp_path / "LICENSE").write_text("license")
    r = v.Report()
    v.check_docs(tmp_path, r)
    assert "no layout/structure" in _messages(r)

    (tmp_path / "pack.yaml").write_text("collection: bad\n")
    meta = {"name": "p", "version": "1", "trust": "public", "kind": "pack",
            "owns_types": [], "owns_namespaces": [], "port_offset": 2}
    pm = SimpleNamespace(load_pack_meta=lambda _p: meta,
                         validate_bundle_recipe=lambda _m: [])
    monkeypatch.setattr(v, "_pack_meta_mod", lambda: pm)
    r = v.Report()
    v.check_pack_meta(tmp_path, r)
    assert "must be a mapping" in _messages(r)

    pm.load_pack_meta = lambda _p: (_ for _ in ()).throw(RuntimeError("bundle load"))
    r = v.Report()
    v.check_bundle(tmp_path, r)
    assert "bundle load" in _messages(r)

    (tmp_path / "schema.yaml").write_text("types: {}\n")
    r = v.Report()
    v.check_owns_covers_schema(tmp_path, r)
    assert r.rows == []


def test_application_and_policy_adapter_exceptions(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_adapter_exceptions")
    declaration = tmp_path / ".okengine/application.yaml"
    declaration.parent.mkdir()
    declaration.write_text("profile: test\nprofile_version: 1\n")

    monkeypatch.setattr(importlib.util, "spec_from_file_location",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("adapter load")))
    r = v.Report()
    v.check_application_profile(tmp_path, r)
    assert "adapter load" in _messages(r)
    r = v.Report()
    v.check_policy_plane(tmp_path, r)
    assert "adapter load" in _messages(r)


def test_subdomain_drift_feed_no_probe_and_runtime_placeholder(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_small_outcomes")
    (tmp_path / "schema.yaml").write_text(
        "types:\n  same: {required: [type, name]}\n")
    sub = tmp_path / "subdomain"
    sub.mkdir()
    (sub / "schema.yaml").write_text(
        "types:\n  same: {required: [type]}\npartitioning: {namespaces: {}}\n")
    r = v.Report()
    v.check_subdomain_form(tmp_path, r)
    assert "required-fields drift" in _messages(r)

    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "one.opml").write_text(
        '<opml><body><outline xmlUrl="https://example.test/feed"/></body></opml>')
    r = v.Report()
    v.check_feeds(tmp_path, r, False)
    assert "not probed" in _messages(r)

    cfg = tmp_path / ".hermes-data/config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "terminal: {backend: local}\n"
        "mcp_servers:\n"
        "  okengine: {headers: {Authorization: '<token from pack .env>'}}\n"
        "  okengine-write: {}\n"
        "  okengine-write-source-quality: {}\n")
    monkeypatch.setattr(v, "_model_profiles_mod", lambda: SimpleNamespace(
        validate_qwen_no_fallback=lambda _d: []))
    r = v.Report()
    v.check_runtime_config(tmp_path, r)
    assert "template placeholder" in _messages(r)


def test_installed_drift_clean_and_nonempty(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_drift_outcomes")
    base = tmp_path / ".okengine/installed-domains"
    base.mkdir(parents=True)
    (base / "one.json").write_text("{}")

    original = importlib.util.module_from_spec
    def module_from_spec(spec):
        if spec.name == "composed_pack_state_validate":
            return SimpleNamespace(all_installed_drift=lambda _p: [])
        return original(spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", module_from_spec)
    # The synthetic module needs a no-op loader because only its adapter contract matters here.
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(
                            exec_module=lambda _module: None)))
    r = v.Report()
    v.check_installed_domain_drift(tmp_path, r)
    assert "1 ownership manifest" in _messages(r)

    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _spec:
                        SimpleNamespace(all_installed_drift=lambda _p: ["changed"]))
    r = v.Report()
    v.check_installed_domain_drift(tmp_path, r)
    assert "changed" in _messages(r)


def test_remaining_noop_and_alternate_outcomes(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_remaining_noops")
    (tmp_path / "schema.yaml").write_text("partitioning: {namespaces: []}\n")
    r = v.Report()
    v.check_schema(tmp_path, r)

    sub = tmp_path / "subdomain"
    sub.mkdir()
    (sub / "schema.yaml").write_text("types: {}\n")
    (sub / "README.md").write_text("install")
    r = v.Report()
    v.check_subdomain_form(tmp_path, r)
    assert "ships no INSTALL doc" not in _messages(r)

    ev = tmp_path / "engine.version"
    ev.write_text("version: v1.2.3\n")
    monkeypatch.setattr(v, "yaml", None)
    monkeypatch.setattr(v, "_engine_meta_mod", lambda: SimpleNamespace(
        engine_release=lambda: "v1.2.3", hermes_pin=lambda: "",
        satisfies_pin=lambda *_a: False))
    r = v.Report()
    v.check_engine_version(tmp_path, r)
    assert any(s == "OK" for s, _c, _d in r.rows)

    cfg = tmp_path / ".hermes-data/config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("anything")
    r = v.Report()
    v.check_runtime_config(tmp_path, r)
    assert "PyYAML unavailable" in _messages(r)


def test_feed_http_error_and_surface_without_pack_metadata(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_feed_status")
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    (feeds / "one.opml").write_text(
        '<opml><body><outline xmlUrl="https://example.test/feed"/></body></opml>')
    class Response:
        status = 503
        def __enter__(self): return self
        def __exit__(self, *_args): return False
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: Response())
    r = v.Report()
    v.check_feeds(tmp_path, r, True)
    assert "503" in _messages(r)

    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  okengine-reader: {ports: ['0.0.0.0:9200:9200']}\n")
    (tmp_path / ".env").write_text("OKENGINE_BIND=0.0.0.0\n")
    r = v.Report()
    v.check_surface_auth(tmp_path, r)
    assert "PRIVATE pack exposed" in _messages(r)


def test_metadata_early_returns_and_enabled_owner_success(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_meta_returns")
    (tmp_path / "pack.yaml").write_text("name: p\n")
    (tmp_path / "schema.yaml").write_text(
        "owners:\n  fields: {field: 'ext:owner'}\n")
    pm = SimpleNamespace(
        load_pack_meta=lambda _p: {"kind": "bundle"},
        extension_requires=lambda _m: [], satisfies=lambda *_a: True)
    monkeypatch.setattr(v, "_pack_meta_mod", lambda: pm)
    r = v.Report()
    v.check_owns_covers_schema(tmp_path, r)
    assert r.rows == []

    pm.load_pack_meta = lambda _p: (_ for _ in ()).throw(RuntimeError("bad meta"))
    r = v.Report()
    v.check_extension_requirements(tmp_path, r)
    assert r.rows == []

    pm.load_pack_meta = lambda _p: {"kind": "pack"}
    disc = SimpleNamespace(resolve_for_pack=lambda _p: (
        {"owner": {"manifest": {"version": "1"}}}, []))
    monkeypatch.setattr(v, "_discovery_mod", lambda: disc)
    r = v.Report()
    v.check_extension_requirements(tmp_path, r)
    assert "schema owner ext:owner" in _messages(r)

    state = tmp_path / ".okengine/extensions.yaml"
    state.parent.mkdir(exist_ok=True)
    state.write_text("enabled: {}\n")
    monkeypatch.setattr(v, "_discovery_mod", lambda: SimpleNamespace(
        discover=lambda _p: ({}, []), load_enabled_state=lambda _p: ({}, []),
        resolve_enabled=lambda *_a: ({}, []), stale_disabled_errors=lambda *_a: []))
    r = v.Report()
    v.check_enabled_extensions_resolve(tmp_path, r)
    assert r.rows == []


def test_dynamic_adapter_success_and_policy_prompt_outcomes(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_dynamic_success")
    declaration = tmp_path / ".okengine/application.yaml"
    declaration.parent.mkdir()
    declaration.write_text("profile: p\nprofile_version: v1\n")
    prompts = tmp_path / "crons/engine-template-prompts.json"
    prompts.parent.mkdir()
    prompts.write_text(json.dumps({"source-quality-backfill": {"prompt": "work"}}))

    application = SimpleNamespace(validate=lambda *_a: [])
    policy_module = SimpleNamespace(
        effective_policy=lambda _p: {"rules": [], "digest": "abcdef123456789"},
        check_prompt=lambda *_a: ["denied"],
    )
    original = importlib.util.module_from_spec
    def module_from_spec(spec):
        if spec.name == "application_profiles": return application
        if spec.name == "okengine_policy_plane": return policy_module
        return original(spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", module_from_spec)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(
                            exec_module=lambda _module: None)))
    r = v.Report()
    v.check_application_profile(tmp_path, r)
    assert "p v1" in _messages(r)
    r = v.Report()
    v.check_policy_plane(tmp_path, r)
    assert "denied" in _messages(r)

    policy_module.check_prompt = lambda *_a: []
    r = v.Report()
    v.check_policy_plane(tmp_path, r)
    assert "prompt conforms" in _messages(r)
    prompts.write_text("not-json")
    r = v.Report()
    v.check_policy_plane(tmp_path, r)
    assert any(c == "source-quality capability/prompt" and s == "FAIL"
               for s, c, _d in r.rows)


def test_remaining_collection_and_runtime_shapes(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_remaining_shapes")
    (tmp_path / "schema.yaml").write_text(
        "partitioning: {namespaces: [truthy]}\n"
        "owners:\n  types: {one: 4, two: 'ext:one'}\n"
        "  fields: {three: 'ext:two'}\n")
    r = v.Report()
    v.check_schema(tmp_path, r)
    assert v._schema_ext_owners(tmp_path) == {"one", "two"}

    cfg = tmp_path / ".hermes-data/config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("terminal: {}\nmcp_servers: [bad]\n")
    monkeypatch.setattr(v, "_model_profiles_mod", lambda: SimpleNamespace(
        validate_qwen_no_fallback=lambda _d: []))
    r = v.Report()
    v.check_runtime_config(tmp_path, r)
    assert "required keys" in _messages(r)

    pm = SimpleNamespace(extension_requires=lambda _m: [])
    monkeypatch.setattr(v, "_pack_meta_mod", lambda: pm)
    monkeypatch.setattr(v, "_discovery_mod", lambda: SimpleNamespace(
        resolve_for_pack=lambda _p: ({
            "one": {"manifest": {"version": "1"}},
            "two": {"manifest": {"version": "1"}}}, [])))
    r = v.Report()
    v.check_extension_requirements(tmp_path, r)
    assert _messages(r).count("schema owner ext:") == 2


def test_dynamic_connector_and_cron_validator_failures(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_dynamic_failures")
    connectors = tmp_path / "connectors"
    connectors.mkdir()
    (connectors / "one.yaml").write_text("id: one\n")
    crons = tmp_path / "crons"
    crons.mkdir()

    monkeypatch.setattr(importlib.util, "spec_from_file_location",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("load failure")))
    r = v.Report()
    v.check_source_connectors(tmp_path, r)
    assert "could not load engine validator" in _messages(r)
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "cron output contracts" in _messages(r)


def test_cron_contract_validator_exception_and_compile_oserror(tmp_path, monkeypatch):
    v = _load("framework_validate_branch_contract_exception")
    crons = tmp_path / "crons"
    scripts = crons / "scripts"
    scripts.mkdir(parents=True)
    prompt_file = crons / "engine-template-prompts.json"
    prompt_file.write_text(json.dumps({
        "one": {"prompt": "x", "output_contract": {}},
        "two": {"prompt": "plain"},
    }))
    target = scripts / "unreadable.py"
    target.write_text("pass\n")

    fake_oc = SimpleNamespace(validate=lambda *_a: (_ for _ in ()).throw(RuntimeError("validate boom")))
    original_module_from_spec = importlib.util.module_from_spec
    original_read_text = Path.read_text
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda spec:
                        fake_oc if spec.name == "okengine_output_contract" else original_module_from_spec(spec))
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=lambda _m: None)))
    def read_text(path, *args, **kwargs):
        if path == target:
            raise OSError("unreadable")
        return original_read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "validate boom" in _messages(r)
    fake_oc.validate = lambda *_a: []
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert "contract shapes valid" in _messages(r)


def test_pack_prompt_value_shapes_and_file_errors_are_reported(tmp_path):
    v = _load("framework_validate_prompt_file_edges")
    assert v._pack_prompt_text(tmp_path, 7) == ""
    assert v._pack_prompt_text(tmp_path, {}) == ""
    assert v._pack_prompt_text(tmp_path, {"prompt": "inline"}) == "inline"
    with pytest.raises(ValueError, match="escapes pack root"):
        v._pack_prompt_text(tmp_path, {"prompt_file": "../outside.md"})

    crons = tmp_path / "crons"
    crons.mkdir()
    (crons / "engine-template-prompts.json").write_text(json.dumps({
        "missing": {"prompt_file": "prompts/missing.md"},
    }))
    r = v.Report()
    v.check_crons(tmp_path, r)
    assert any(status == "FAIL" and "missing.md" in detail
               for status, _check, detail in r.rows)


def test_script_entrypoint_and_yaml_import_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path / "missing")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 2
    assert "pack dir not found" in capsys.readouterr().err

    original_import = builtins.__import__
    def without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("simulated missing optional dependency")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_yaml)
    namespace = runpy.run_path(str(SCRIPT), run_name="framework_validate_without_yaml")
    assert namespace["yaml"] is None


def test_engine_input_facade_delegates(monkeypatch):
    v = _load("framework_validate_engine_inputs_facade")
    seen = []
    monkeypatch.setattr(
        v,
        "_pack_checks",
        lambda: SimpleNamespace(
            _check_engine_inputs=lambda schema, names, report: seen.append(
                (schema, names, report)
            )
        ),
    )
    report = v.Report()
    v._check_engine_inputs({"types": {}}, {"entity"}, report)
    assert seen == [({"types": {}}, {"entity"}, report)]


# --- okengine#813: an accepted trust exposure stays on the report. The guest's content is served
# at the host's trust whether or not the exposure was accepted, so the decision belongs on every
# validate run, not only in the install log that recorded it.


RUNTIME = REPO / "scripts" / "framework_validate_runtime.py"


def _load_runtime(name: str):
    """The override reporter lives in the runtime module; framework_validate calls straight through
    to it (that entrypoint is under a shrinking line ratchet, so it carries no wrapper)."""
    spec = importlib.util.spec_from_file_location(name, RUNTIME)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _with_override(tmp_path, body: str, *, installed: str | None = None) -> Path:
    (tmp_path / ".okengine").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".okengine" / "coinstall-overrides.yaml").write_text(body)
    if installed:
        domains = tmp_path / ".okengine" / "installed-domains"
        domains.mkdir(exist_ok=True)
        (domains / f"{installed}.json").write_text(json.dumps({"pack": installed}))
    return tmp_path


_ENTRY = ("trust_exposure:\n  okpack-guest:\n    guest_trust: private\n"
          "    host_trust: public\n    accepted_at: 2026-09-20T18:00:00Z\n"
          "    reason: trusted-LAN reader only\n")


def test_standing_trust_exposure_override_is_reported(tmp_path):
    v = _load("framework_validate_override_standing")
    rt = _load_runtime("framework_validate_override_standing_rt")
    r = v.Report()
    rt.check_trust_exposure_overrides(_with_override(tmp_path, _ENTRY, installed="okpack-guest"), r)
    messages = _messages(r)
    assert "okpack-guest" in messages and "trusted-LAN reader only" in messages
    assert "STALE" not in messages


def test_override_for_an_uninstalled_pack_is_reported_stale(tmp_path):
    v = _load("framework_validate_override_stale")
    rt = _load_runtime("framework_validate_override_stale_rt")
    r = v.Report()
    rt.check_trust_exposure_overrides(_with_override(tmp_path, _ENTRY, installed="okpack-other"), r)
    assert "STALE" in _messages(r)


def test_absent_or_empty_override_file_reports_nothing(tmp_path):
    v = _load("framework_validate_override_absent")
    rt = _load_runtime("framework_validate_override_absent_rt")
    r = v.Report()
    rt.check_trust_exposure_overrides(tmp_path, r)
    assert r.rows == []
    r = v.Report()
    rt.check_trust_exposure_overrides(_with_override(tmp_path, "trust_exposure: {}\n"), r)
    assert r.rows == []


def test_unreadable_override_file_warns_rather_than_passing(tmp_path):
    """A file that cannot be parsed is not an absence of overrides."""
    v = _load("framework_validate_override_unreadable")
    rt = _load_runtime("framework_validate_override_unreadable_rt")
    r = v.Report()
    rt.check_trust_exposure_overrides(_with_override(tmp_path, "trust_exposure: [unclosed\n"), r)
    assert "unreadable" in _messages(r)


def test_an_unloadable_writer_name_rule_reports_undetectable_not_clean(monkeypatch):
    """A rule that cannot be loaded must never read as a passing lane (okengine#840).

    The write-server length check borrows its limit from cron_pack_split by path. If that load
    fails the honest answer is "undetectable" -- returning no errors would certify a lane the
    gate never actually inspected.
    """
    import importlib.util as _iu
    spec = _iu.spec_from_file_location(
        "okengine_fvc_undetectable", REPO / "scripts" / "framework_validate_crons.py")
    fvc = _iu.module_from_spec(spec)
    spec.loader.exec_module(fvc)

    # an explicitly named writer bypasses the (bounded) derivation, so it can still be over
    over = [{"name": "x", "enabled_toolsets": ["okengine-write-" + "x" * 60]}]
    assert fvc._writer_name_errors(over), "fixture must be over the limit when the rule loads"

    def boom(*a, **k):
        raise ImportError("cron_pack_split is unavailable")
    monkeypatch.setattr(_iu, "spec_from_file_location", boom)

    errors = fvc._writer_name_errors(over)
    assert len(errors) == 1, errors
    assert "UNDETECTABLE" in errors[0], errors[0]
    assert errors[0] != [], "an unloadable rule must not report the lane clean"


def test_an_unknown_type_role_fails_and_the_actor_role_does_not(tmp_path):
    """okengine#1020, okengine#1032 [24]: a misspelled role silently leaves a type out of every
    actor guard, and a WARN let it ship, so it FAILs."""
    v = _load("framework_validate_branch_role")
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\ntypes:\n"
        "  intrusion-set: {required: [type], role: Actor}\n"
        "  threat-actor: {required: [type], role: actors}\n"
        "  malware: {required: [type]}\n  broken: scalar\n")
    r = v.Report()
    v.check_schema(tmp_path, r)
    rows = [row for row in r.rows if row[1] == "schema.types.role"]
    assert len(rows) == 1 and rows[0][0] == "FAIL", rows
    assert "threat-actor: 'actors'" in rows[0][2] and "intrusion-set" not in rows[0][2]
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\ntypes:\n  intrusion-set: {required: [type], role: actor}\n")
    r = v.Report()
    v.check_schema(tmp_path, r)
    assert not [row for row in r.rows if row[1] == "schema.types.role"]


def _fragment_ext(pack: Path, ext_id: str, role) -> None:
    ext = pack / "extensions" / ext_id
    (ext / "schema").mkdir(parents=True)
    (ext / "extension.yaml").write_text(
        f"id: {ext_id}\nname: {ext_id}\nversion: 0.1.0\nschema: [schema/f.schema.yaml]\n")
    spec = {"required": ["type"]} | ({"role": role} if role else {})
    import yaml as _yaml
    (ext / "schema" / "f.schema.yaml").write_text(
        _yaml.safe_dump({"owns": {"types": {"intrusion-set": spec}}}))


def test_an_unknown_role_on_an_extension_owned_type_fails(tmp_path):
    """okengine#1032 [24]: compose copies a fragment's owned type verbatim, so its role reaches the
    same actor resolver and must meet the same vocabulary."""
    v = _load("framework_validate_fragment_role")
    (tmp_path / "schema.yaml").write_text("okf: {required: [type]}\ntypes: {}\n")
    _fragment_ext(tmp_path, "acme.bad", "threat-actor")
    _fragment_ext(tmp_path, "acme.good", "actor")
    r = v.Report()
    v.check_enabled_extensions_resolve(tmp_path, r)
    rows = [row for row in r.rows if "owns.types.role" in row[1]]
    assert [(row[0], row[1].split()[0]) for row in rows] == [("FAIL", "ext:acme.bad")], rows
    assert "intrusion-set: 'threat-actor'" in rows[0][2]


def test_fragment_roles_skip_shapes_that_declare_no_types():
    from scripts import framework_validate_pack as fvp
    frags = {Path("d/a.yaml"): None, Path("d/b.yaml"): {"owns": "x"}, Path("d/c.yaml"): {"owns": {}}}
    r = fvp.Report()
    fvp.check_fragment_type_roles(
        [None, {"manifest": None}, {"id": "e", "dir": "d", "manifest": {"schema": None}},
         {"id": "e", "dir": "d", "manifest": {"schema": ["a.yaml", "b.yaml", "c.yaml"]}}],
        frags.get, r)
    assert r.rows == []
    # a malformed record does not end the scan: a bad fragment after it still FAILs
    frags[Path("d/bad.yaml")] = {"owns": {"types": {"t": {"role": "actors"}}}}
    fvp.check_fragment_type_roles(
        [{"manifest": None}, {"id": "e", "dir": "d", "manifest": {"schema": ["bad.yaml"]}}],
        frags.get, r)
    assert [row[0] for row in r.rows] == ["FAIL"], r.rows


def _owned_pack(tmp_path: Path, name: str | None, owners: dict, guests=()) -> Path:
    import json as _json
    import yaml as _yaml
    (tmp_path / "pack.yaml").write_text(f"name: {name}\n" if name else "version: 1.0.0\n")
    for guest in guests:
        d = tmp_path / ".okengine" / "installed-domains"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{guest}.json").write_text(_json.dumps({"pack": guest, "cron_jobs": []}))
    types = {t: {"required": ["type"]} | ({"owner": o} if o else {}) for t, o in owners.items()}
    (tmp_path / "schema.yaml").write_text(
        _yaml.safe_dump({"okf": {"required": ["type"]}, "types": types}))
    return tmp_path


@pytest.mark.parametrize("owners, guests, failing", [
    # a pack folded into this one (owner names it, but no installed-domains manifest exists)
    # ("aaa", owner-less, sorts first: a type with no owner must not end the scan)
    ({"aaa": None, "actor": "okpack-mine", "intrusion-set": "okpack-folded",
      "software": "okpack-folded"}, (), ["okpack-folded"]),
    # a renamed pack / an author typo
    ({"actor": "my-pack"}, (), ["my-pack"]),
    # a bundle whose re-stamped name hid its host (okengine#1032 [11])
    ({"actor": "okpack-host"}, ("okpack-guest",), ["okpack-host"]),
])
def test_a_type_owner_no_lane_can_act_for_fails(tmp_path, owners, guests, failing):
    """okengine#1032 [11], [15]: converge compares the owner with the write principal by equality,
    so an owner that is neither the pack.yaml name nor a co-installed pack can never be met."""
    v = _load("framework_validate_owner_bad")
    name = "okpack-cti" if "okpack-host" in owners.values() else "okpack-mine"
    pack = _owned_pack(tmp_path, name, owners, guests)
    r = v.Report()
    v.check_schema(pack, r)
    rows = [row for row in r.rows if row[1] == "schema.types.owner"]
    assert [row[0] for row in rows] == ["FAIL"] * len(failing), rows
    for owner, row in zip(failing, rows):
        assert repr(owner) in row[2] and f"pack.yaml name {name!r}" in row[2], row
        assert f"co-installed {sorted(guests)}):" in row[2], row


def test_every_reachable_type_owner_passes(tmp_path):
    v = _load("framework_validate_owner_ok")
    pack = _owned_pack(tmp_path, "okpack-mine", {
        "actor": "okpack-mine", "cve": "okpack-guest", "x-lacuna": "ext:okengine.lacuna",
        "core": "engine", "free": None}, guests=("okpack-guest",))
    r = v.Report()
    v.check_schema(pack, r)
    rows = [row for row in r.rows if row[1] == "schema.types.owner"]
    assert [row[0] for row in rows] == ["OK"], rows


def test_a_deployment_that_also_lists_itself_as_co_installed_still_owns_its_types(tmp_path):
    """The principals are a union: a manifest naming the deployment pack (or `engine`) must not
    cancel it out."""
    from scripts import framework_validate_pack as fvp
    pack = _owned_pack(tmp_path, "okpack-mine", {"actor": "okpack-mine", "core": "engine"},
                       guests=("okpack-mine", "engine"))
    r = fvp.Report()
    fvp.check_type_owners(pack, {"actor": {"owner": "okpack-mine"}, "core": {"owner": "engine"}}, r)
    assert [row[0] for row in r.rows] == ["OK"], r.rows


def test_type_owners_need_a_pack_name_and_a_declared_owner(tmp_path):
    from scripts import framework_validate_pack as fvp
    r = fvp.Report()
    fvp.check_type_owners(_owned_pack(tmp_path, None, {"actor": "x"}), {"actor": {"owner": "x"}}, r)
    assert r.rows == [], "a nameless pack.yaml is its own FAIL; nothing to compare here"
    fvp.check_type_owners(_owned_pack(tmp_path, "p", {"actor": None}),
                          {"actor": {"required": ["type"]}, "y": "scalar"}, r)
    assert r.rows == [], "no declared owner: nothing to report"


@pytest.mark.parametrize(("text", "fails"), [
    ("version: 1.0.0\ntrust: public\n", True),              # nameless: the basename default hid it
    ("name: '  '\nversion: 1.0.0\ntrust: public\n", True),  # blank
    ("name: okpack-x\nversion: 1.0.0\ntrust: public\n", False),
])
def test_a_pack_yaml_without_a_literal_name_fails_validate(tmp_path, text, fails):
    """okengine#1027 (v0.15.11 invariant audit #2/#8): load_pack_meta defaults `name` to the
    directory basename, so a nameless pack.yaml validated clean while every write server refused
    to start after the gateway recreate. The literal name is what the write servers read."""
    v = _load("framework_validate_branch_pack_name")
    (tmp_path / "pack.yaml").write_text(text)
    r = v.Report()
    v.check_pack_meta(tmp_path, r)
    failed = [detail for sev, check, detail in r.rows if sev == "FAIL" and check == "pack.yaml name"]
    assert bool(failed) is fails, r.rows
    assert all("okengine#1027" in detail for detail in failed)
