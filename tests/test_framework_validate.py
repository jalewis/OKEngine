"""Regression: `framework validate` catches deploy-breaking pack defects."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "scripts" / "framework_init.py"
VAL = REPO / "scripts" / "framework_validate.py"
CLI = REPO / "scripts" / "framework.py"

pytestmark = pytest.mark.skipif(not VAL.is_file(), reason="framework_validate absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _scaffold(dest: Path):
    m = _load("framework_init", INIT)
    assert m.main([str(dest), "--domain", "Test Domain", "--no-compose"]) == 0


def _scaffold_with_compose(dest: Path):
    m = _load("framework_init", INIT)
    assert m.main([str(dest), "--domain", "Test Domain"]) == 0


def test_scaffolded_pack_validates_clean(tmp_path):
    """A freshly-scaffolded pack has NO FAILs (warns for unfilled persona / example feeds ok)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"]
    assert fails == [], f"unexpected FAILs: {fails}"
    assert v.main([str(pack), "--quiet"]) == 0


def test_schema_exclude_rejects_ambiguous_subtree_before_deploy(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    schema = yaml.safe_load((pack / "schema.yaml").read_text(encoding="utf-8"))
    schema["exclude"] = ["assessments/staging"]
    (pack / "schema.yaml").write_text(yaml.safe_dump(schema), encoding="utf-8")
    v = _load("framework_validate_exclude_grammar", VAL)

    report = v.validate(pack)

    assert any(severity == "FAIL" and check == "schema.exclude"
               and "not a namespace exclusion" in detail
               for severity, check, detail in report.rows)


def test_runtime_config_rejects_qwen_coder_with_global_fallback(tmp_path):
    """P0: a Qwen failure must be visible, never silently become paid cloud traffic."""
    pack = tmp_path / "pack"
    cfg = pack / ".hermes-data" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(yaml.safe_dump({
        "model": {"default": "qwen3-coder:30b", "provider": "custom"},
        "fallback_providers": [{"provider": "deepseek", "model": "deepseek-flash"}],
        "terminal": {"backend": "local"},
        "mcp_servers": {
            "okengine": {"headers": {"Authorization": "Bearer test"}},
            "okengine-write": {},
            "okengine-write-source-quality": {},
        },
    }))
    v = _load("framework_validate_qwen_fallback", VAL)
    report = v.Report()
    v.check_runtime_config(pack, report)
    assert any(s == "FAIL" and c == "Qwen Coder fallback policy"
               for s, c, _d in report.rows)


def test_runtime_config_accepts_qwen_coder_with_empty_fallback(tmp_path):
    pack = tmp_path / "pack"
    cfg = pack / ".hermes-data" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(yaml.safe_dump({
        "model": {"default": "qwen3-coder:30b-tools", "provider": "custom"},
        "fallback_providers": [],
        "terminal": {"backend": "local"},
        "mcp_servers": {
            "okengine": {"headers": {"Authorization": "Bearer test"}},
            "okengine-write": {},
            "okengine-write-source-quality": {},
        },
    }))
    v = _load("framework_validate_qwen_no_fallback", VAL)
    report = v.Report()
    v.check_runtime_config(pack, report)
    assert not any(s == "FAIL" and c == "Qwen Coder fallback policy"
                   for s, c, _d in report.rows)


def test_model_policy_rejects_legacy_deepseek_selections(tmp_path):
    pack = tmp_path / "pack"
    (pack / ".okengine").mkdir(parents=True)
    (pack / "crons").mkdir()
    (pack / ".hermes-data").mkdir()
    (pack / ".okengine" / "model-profiles.yaml").write_text(
        "profiles:\n  reasoning: {provider: deepseek, model: deepseek-v4-pro}\n",
        encoding="utf-8",
    )
    (pack / "crons" / "domain-crons.json").write_text(
        json.dumps([{"name": "brief", "model": "deepseek-v4-flash"}]), encoding="utf-8"
    )
    (pack / ".hermes-data" / "config.yaml").write_text(
        yaml.safe_dump({"fallback_providers": [
            {"provider": "deepseek", "model": "deepseek-reasoner"}
        ]}), encoding="utf-8",
    )
    (pack / ".okengine" / "cron-models.json").write_text(
        json.dumps({"extract": "deepseek-flash"}), encoding="utf-8"
    )
    (pack / ".okengine" / "extension-models.json").write_text(
        json.dumps({"grade": "deepseek/deepseek-v4-pro"}), encoding="utf-8"
    )
    v = _load("framework_validate_legacy_deepseek", VAL)
    report = v.Report()

    v._model_checks().check_deepseek_model_policy(pack, report)

    failures = [detail for severity, check, detail in report.rows
                if severity == "FAIL" and check == "DeepSeek model policy"]
    assert len(failures) == 4, failures
    assert any("model-profiles.yaml" in detail and "deepseek-v4-pro" in detail
               for detail in failures)
    assert any("domain-crons.json" in detail and "deepseek-v4-flash" in detail
               for detail in failures)
    assert any("config.yaml" in detail and "deepseek-reasoner" in detail
               for detail in failures)
    assert any("extension-models.json" in detail and "deepseek/deepseek-v4-pro" in detail
               for detail in failures)


def test_model_policy_accepts_v41_and_ignores_descriptive_legacy_text(tmp_path):
    pack = tmp_path / "pack"
    (pack / ".okengine").mkdir(parents=True)
    (pack / ".hermes-data").mkdir()
    (pack / ".okengine" / "model-profiles.yaml").write_text(
        "profiles:\n  deepseek-v4-pro:\n    model: deepseek-flash\n"
        "    note: migrated from deepseek-v4-pro\n",
        encoding="utf-8",
    )
    (pack / ".hermes-data" / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "deepseek-flash"},
                        "fallback_providers": [{"provider": "openrouter",
                        "model": "deepseek/deepseek-v4.1-flash"}]}), encoding="utf-8",
    )
    (pack / ".okengine" / "cron-models.json").write_text("{}", encoding="utf-8")
    (pack / ".okengine" / "extension-models.json").write_text("[]", encoding="utf-8")
    v = _load("framework_validate_current_deepseek", VAL)
    report = v.Report()

    v._model_checks().check_deepseek_model_policy(pack, report)

    assert not [row for row in report.rows if row[0] == "FAIL"], report.rows
    assert ("OK", "DeepSeek model policy", "active selections use current model IDs") in report.rows


def test_compose_drift_ignores_commented_base_port(tmp_path):
    """Regression (library deploy-matrix): the port-offset drift check greps the RAW compose
    text, so a commented-out example binding that shows the un-offset base port — e.g. a doc
    line `# ports: [...:8730:8730]` — was flagged as drift. A comment is not a binding: it must
    not trip the check, while an actual (uncommented) base-port binding still must."""
    v = _load("framework_validate", VAL)
    _reader = ("services:\n"
               "  okengine-reader:\n"
               "    image: okengine-reader:latest\n"
               "    environment:\n"
               "      - OKENGINE_TRUST=public\n"
               "      - OKENGINE_BIND=127.0.0.1\n"
               "      - OKENGINE_READER_PASSWORD=changeme\n")

    pack = tmp_path / "offset-pack"
    _scaffold_with_compose(pack)
    (pack / "pack.yaml").write_text((pack / "pack.yaml").read_text().rstrip() + "\nport_offset: 400\n")

    # real binding is OFFSET (9600:9200); the only base-port mention is a COMMENT → no drift
    (pack / "docker-compose.yml").write_text(
        _reader +
        '    ports: ["${OKENGINE_BIND:-127.0.0.1}:9600:9200"]\n'
        '    # ports: ["${OKENGINE_BIND:-127.0.0.1}:8730:8730"]   # doc example, NOT a binding\n')
    drift = [d for s, c, d in v.validate(pack).rows if s == "FAIL" and "port_offset" in d]
    assert not drift, f"commented base-port wrongly flagged as drift: {drift}"

    # sanity: an UNCOMMENTED un-offset base-port binding under an offset pack STILL fails
    (pack / "docker-compose.yml").write_text(
        _reader + '    ports: ["${OKENGINE_BIND:-127.0.0.1}:8730:8730"]\n')
    drift2 = [d for s, c, d in v.validate(pack).rows if s == "FAIL" and "port_offset" in d]
    assert drift2, "an uncommented un-offset base-port binding must still fail compose drift"


def test_broken_schema_is_a_fail(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    (pack / "schema.yaml").write_text("types: [this, is, not, a, mapping\n:::bad yaml")
    v = _load("framework_validate", VAL)
    assert v.main([str(pack), "--quiet"]) == 1


def test_unknown_reshard_strategy_is_a_fail(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    schema = yaml.safe_load((pack / "schema.yaml").read_text())
    namespace = next(iter(schema["partitioning"]["namespaces"]))
    schema["partitioning"]["namespaces"][namespace]["reshard_by"] = "year"
    (pack / "schema.yaml").write_text(yaml.safe_dump(schema, sort_keys=False))

    v = _load("framework_validate_bad_reshard", VAL)
    rows = v.validate(pack).rows
    assert any(s == "FAIL" and c == "schema.yaml reshard_by" for s, c, _d in rows)


def test_scalar_namespace_partition_config_does_not_raise_a_false_reshard_error(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    schema = yaml.safe_load((pack / "schema.yaml").read_text())
    schema["partitioning"]["namespaces"]["broken"] = "scalar"
    (pack / "schema.yaml").write_text(yaml.safe_dump(schema, sort_keys=False))

    v = _load("framework_validate_scalar_namespace", VAL)
    rows = v.validate(pack).rows
    assert not any(
        check == "schema.yaml reshard_by" for _status, check, _detail in rows
    ), "a non-mapping namespace config has no reshard_by value to reject"


def test_feed_validation_rejects_dtd_entity_opml(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    feeds = pack / "feeds" / "feeds.opml"
    feeds.write_text('<!DOCTYPE opml [<!ENTITY x "boom">]><opml><body>&x;</body></opml>')
    v = _load("framework_validate_safe_xml", VAL)
    rows = v.validate(pack).rows
    assert any(s == "FAIL" and "DTD/entity" in d for s, _c, d in rows)


def test_missing_persona_is_a_fail(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    (pack / "CLAUDE.md").unlink()
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert any(s == "FAIL" and "persona" in c.lower() or "CLAUDE.md" in c for s, c, d in r.rows if s == "FAIL")
    assert v.main([str(pack), "--quiet"]) == 1


def test_vault_mount_wiki_path_doubling_is_a_fail(tmp_path):
    """okengine#110: WIKI_PATH ending in 'wiki' (e.g. /opt/wiki) doubles to /opt/wiki/wiki and
    forks the vault into a split-brain — validate must FAIL it; the skeleton (/opt/vault) passes."""
    pack = tmp_path / "pack"
    _scaffold_with_compose(pack)
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert not any(s == "FAIL" and "WIKI_PATH" in c for s, c, d in r.rows), "skeleton (/opt/vault) should pass the guard"
    compose = pack / "docker-compose.yml"
    compose.write_text(compose.read_text().replace("/opt/vault", "/opt/wiki"))   # the okpack-ai-research misconfig
    r2 = v.validate(pack)
    assert any(s == "FAIL" and "WIKI_PATH" in c for s, c, d in r2.rows), "WIKI_PATH=/opt/wiki must FAIL"
    assert v.main([str(pack), "--quiet"]) == 1


def test_runtime_config_context_aware(tmp_path):
    """Missing .hermes-data/config.yaml is context-aware: a definition repo
    (.hermes-data gitignored) WARNs (it's seeded at deploy); a dir that doesn't
    gitignore it FAILs. A present-but-bad config always FAILs."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    cfg = pack / ".hermes-data" / "config.yaml"
    # (a) scaffold seeds it with valid keys -> OK
    assert any(s == "OK" and "config.yaml" in c for s, c, d in v.validate(pack).rows)
    # (b) remove it; the scaffold .gitignore excludes .hermes-data -> INFO, not FAIL
    cfg.unlink()
    rows = v.validate(pack).rows
    assert any(s == "INFO" and "config.yaml" in c for s, c, d in rows)
    assert not any(s == "FAIL" and "config.yaml" in c for s, c, d in rows)
    assert v.main([str(pack), "--quiet"]) == 0   # expected definition-only absence doesn't block
    # (c) if .hermes-data isn't gitignored, a missing config is a real FAIL
    (pack / ".gitignore").write_text("# no runtime ignore\n.env\n")
    assert any(s == "FAIL" and "config.yaml" in c for s, c, d in v.validate(pack).rows)


def test_declared_analysis_overlay_has_no_feed_warning(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    meta = yaml.safe_load((pack / "pack.yaml").read_text())
    meta["collection"] = {"mode": "overlay", "feeds": "none"}
    (pack / "pack.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    # The skeleton already ships an empty active OPML: it is intentional for this mode.
    v = _load("framework_validate_overlay", VAL)
    rows = v.validate(pack).rows
    assert any(s == "OK" and c == "pack.yaml collection" for s, c, _d in rows)
    assert not any(s == "WARN" and c.startswith("feeds/") for s, c, _d in rows)


def test_empty_feeds_still_warn_without_overlay_contract(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate_ingest", VAL)
    assert any(s == "WARN" and c.startswith("feeds/") for s, c, _d in v.validate(pack).rows)


def test_invalid_collection_contract_fails(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    meta = yaml.safe_load((pack / "pack.yaml").read_text())
    meta["collection"] = {"mode": "magic", "feeds": "none"}
    (pack / "pack.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    v = _load("framework_validate_bad_collection", VAL)
    assert any(s == "FAIL" and c == "pack.yaml collection" for s, c, _d in v.validate(pack).rows)


def _mini_exposed_pack(d, trust):
    # minimal pack with an EXPOSED reader + no password, for the trust-aware surface-auth check
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(f"name: demo\ntrust: {trust}\n")
    (d / "docker-compose.yml").write_text(
        'services:\n  okengine-reader:\n    image: okengine-reader\n'
        '    ports: ["${OKENGINE_BIND:-127.0.0.1}:9300:9200"]\n')
    (d / ".env").write_text("OKENGINE_BIND=0.0.0.0\n")   # exposed, no OKENGINE_READER_PASSWORD
    return d


def test_private_pack_exposed_without_password_fails(tmp_path):
    # okengine#90 P4a: a PRIVATE vault exposed beyond loopback with no reader password is a FAIL.
    v = _load("framework_validate", VAL)
    r = v.validate(_mini_exposed_pack(tmp_path / "priv", "private"))
    assert any(s == "FAIL" and c == "reader auth" for s, c, d in r.rows), [x for x in r.rows if "reader" in str(x[1])]


def test_public_pack_exposed_without_password_warns_not_fails(tmp_path):
    # a PUBLIC reference deployment is intentionally open — WARN, never FAIL on reader auth.
    v = _load("framework_validate", VAL)
    r = v.validate(_mini_exposed_pack(tmp_path / "pub", "public"))
    assert not any(s == "FAIL" and c == "reader auth" for s, c, d in r.rows), r.rows
    assert any(s == "WARN" and c == "reader auth" for s, c, d in r.rows), r.rows


def test_private_pack_exposed_cockpit_without_password_fails(tmp_path):
    # okengine#90 P4a extends to the cockpit: it is a SUPERSET of the reader on the same bind, so a
    # cockpit-only deployment exposed with no password is just as much a FAIL. (Regression: the
    # validator originally only checked okengine-reader, silently passing an exposed cockpit.)
    d = tmp_path / "cockpit-only"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text("name: demo\ntrust: private\n")
    (d / "docker-compose.yml").write_text(
        'services:\n  okengine-cockpit:\n    image: okengine-cockpit\n'
        '    ports: ["${OKENGINE_BIND:-127.0.0.1}:9400:9200"]\n')
    (d / ".env").write_text("OKENGINE_BIND=0.0.0.0\n")   # exposed, no OKENGINE_READER_PASSWORD
    v = _load("framework_validate", VAL)
    r = v.validate(d)
    assert any(s == "FAIL" and c == "cockpit auth" for s, c, d in r.rows), \
        [x for x in r.rows if "cockpit" in str(x[1])]
    # a shared password clears it (one credential protects both UIs)
    (d / ".env").write_text("OKENGINE_BIND=0.0.0.0\nOKENGINE_READER_PASSWORD=hunter2\n")
    r = v.validate(d)
    assert not any(s == "FAIL" and c == "cockpit auth" for s, c, d in r.rows), r.rows


def test_bad_runtime_config_keys_is_a_fail(tmp_path):
    """A present config missing the required MCP servers / terminal backend FAILs
    regardless of gitignore (it IS a deploy-ready dir then)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    (pack / ".hermes-data" / "config.yaml").write_text("terminal:\n  backend: docker\n")
    r = v.validate(pack)
    assert any(s == "FAIL" and "config.yaml" in c for s, c, d in r.rows)
    assert v.main([str(pack), "--quiet"]) == 1


def test_bad_cron_json_and_script_syntax_are_fails(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    (pack / "crons" / "domain-crons.json").write_text("{not json")
    (pack / "crons" / "scripts" / "broken.py").write_text("def x(:\n")  # syntax error
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    checks = " ".join(c for s, c, d in r.rows if s == "FAIL")
    assert "domain-crons.json" in checks
    assert "compile" in checks
    assert v.main([str(pack), "--quiet"]) == 1


def test_domain_cron_may_reference_known_engine_script(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    (pack / "crons" / "domain-crons.json").write_text(
        '[{"name":"nvd","schedule":"0 1 * * *","script":"nvd_import.py"}]')
    v = _load("framework_validate_engine_script", VAL)
    rows = v.validate(pack).rows
    assert any(s == "INFO" and "supplied by the engine" in d for s, _c, d in rows)
    assert not any(s == "WARN" and "nvd_import.py" in d for s, _c, d in rows)


def test_engine_input_keys_shape_checked(tmp_path):
    """Optional engine-input keys: absent ⇒ clean (scaffold), bad shape ⇒ FAIL,
    type reference to an undeclared type ⇒ WARN (not FAIL)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    base = ("okf:\n  required: [type]\ntypes:\n  entity: {required: [type]}\n")
    # non-list protected_fields ⇒ FAIL
    (pack / "schema.yaml").write_text(base + "protected_fields: notalist\n")
    r = v.validate(pack)
    assert any(s == "FAIL" and "protected_fields" in c for s, c, d in r.rows)
    # type_aliases pointing at an undeclared type ⇒ WARN, no FAIL on that key
    (pack / "schema.yaml").write_text(base + "type_aliases: {org: nonsuch}\n")
    r = v.validate(pack)
    assert any(s == "WARN" and "type_aliases" in c for s, c, d in r.rows)
    assert not any(s == "FAIL" and "type_aliases" in c for s, c, d in r.rows)


def test_schema_inputs_may_reference_inherited_engine_core_types(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    schema = yaml.safe_load((pack / "schema.yaml").read_text())
    schema["type_aliases"] = {"article": "source", "weekly-note": "briefing"}
    schema["classify_hints"] = {"concept": ["pattern"]}
    (pack / "schema.yaml").write_text(yaml.safe_dump(schema, sort_keys=False))
    v = _load("framework_validate_core_refs", VAL)
    rows = v.validate(pack).rows
    assert not any(s == "WARN" and c.startswith("schema.") for s, c, _d in rows)


def test_bundle_does_not_require_runtime_environment_example(tmp_path):
    pack = tmp_path / "bundle"
    pack.mkdir()
    (pack / "pack.yaml").write_text(
        "name: bundle\nversion: 0.1.0\nkind: bundle\ntrust: public\n"
        "owns: {types: [], namespaces: []}\n"
        "bundle: {host: host-pack, compose: [child-pack]}\n"
        "requires: [host-pack, child-pack]\n")
    manifest = yaml.safe_load((REPO / "engine-manifest.yaml").read_text())
    (pack / "engine.version").write_text(
        f"engine: okengine\nversion: {manifest['engine_release']}\n"
        f"hermes_pin: {manifest['runtime']['pinned_tag']}\n")
    (pack / "README.md").write_text("# Bundle\n\n## Install\n\nUse framework pull.\n")
    (pack / "LICENSE").write_text("test\n")
    v = _load("framework_validate_bundle_env", VAL)
    rows = v.validate(pack).rows
    assert not any(c == ".env.example" for _s, c, _d in rows)


def test_pack_level_strict_types_is_boolean_opt_in(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    assert not any("strict_types" in c for s, c, d in v.validate(pack).rows)  # scaffold clean
    (pack / "schema.yaml").write_text(
        "okf:\n  required: [type]\nstrict_types: true\ntypes:\n  entity: {required: [type]}\n")
    rows = v.validate(pack).rows
    assert not any("strict_types" in c for s, c, d in rows)
    (pack / "schema.yaml").write_text(
        "okf:\n  required: [type]\nstrict_types: closed\ntypes:\n  entity: {required: [type]}\n")
    assert any(s == "FAIL" and "strict_types" in c for s, c, d in v.validate(pack).rows)


def test_local_first_default_passes_clean(tmp_path):
    """Local-first: a fresh scaffold (no .env, host ports bound to loopback) has no
    auth FAIL — the generic default MCP token + open loopback reader are fine."""
    pack = tmp_path / "pack"
    _scaffold_with_compose(pack)
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert not any(s == "FAIL" for s, c, d in r.rows), \
        f"local default should not FAIL: {[(c, d) for s, c, d in r.rows if s == 'FAIL']}"
    assert any(s == "INFO" and "exposure" in c for s, c, d in r.rows)


def test_exposed_without_real_secrets_is_a_fail(tmp_path):
    """Flipping OKENGINE_BIND beyond localhost with the default/empty creds FAILs —
    widening the bind forces real auth (issues #20/#29)."""
    pack = tmp_path / "pack"
    _scaffold_with_compose(pack)
    (pack / ".env").write_text("OKENGINE_BIND=0.0.0.0\nOKENGINE_MCP_TOKEN=okengine-local\n"
                               "OKENGINE_READER_PASSWORD=\n")
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    fails = " ".join(c for s, c, d in r.rows if s == "FAIL")
    assert "MCP auth" in fails       # still the built-in default token
    assert "reader auth" in fails    # no reader password
    assert v.main([str(pack), "--quiet"]) == 1


def test_exposed_with_real_secrets_passes(tmp_path):
    """Exposed but with real secrets set — no auth FAIL."""
    pack = tmp_path / "pack"
    _scaffold_with_compose(pack)
    (pack / ".env").write_text("OKENGINE_BIND=0.0.0.0\nOKENGINE_MCP_TOKEN=s3cret-xyz\n"
                               "OKENGINE_READER_PASSWORD=hunter2\n")
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert not any(s == "FAIL" and "auth" in c for s, c, d in r.rows)


def test_loopback_default_token_warns_mcp_will_crash(tmp_path):  # invariant-audit #208
    """A LOOPBACK deploy whose .env still carries the built-in default MCP token: the containerized
    MCP binds 0.0.0.0 internally, so it FAILS CLOSED at startup (crash-loop) regardless of the
    loopback host-port mapping. framework validate must WARN — not stay silent behind the
    'host ports bind loopback' INFO (the false-confidence trap #208) — and must NOT hard-FAIL a
    loopback deploy."""
    pack = tmp_path / "pack"
    _scaffold_with_compose(pack)
    (pack / ".env").write_text("OKENGINE_BIND=127.0.0.1\nOKENGINE_MCP_TOKEN=okengine-local\n")
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert any(s == "WARN" and "MCP auth" in c and "0.0.0.0" in (d or "") for s, c, d in r.rows), \
        f"expected a fail-closed WARN on the loopback default token: {r.rows}"
    assert not any(s == "FAIL" for s, c, d in r.rows), \
        f"loopback default token is a WARN, not a FAIL: {[(c, d) for s, c, d in r.rows if s == 'FAIL']}"


def test_loopback_default_token_ok_with_allow_or_real_token(tmp_path):  # invariant-audit #208
    """The #208 fail-closed WARN must NOT fire when the operator accepts the default
    (OKENGINE_MCP_ALLOW_DEFAULT_TOKEN=1) or sets a real token — both boot fine — nor on a fresh
    scaffold with no .env (deploy.sh/ensure-runtime will generate one)."""
    v = _load("framework_validate", VAL)
    ok_envs = [
        "OKENGINE_BIND=127.0.0.1\nOKENGINE_MCP_TOKEN=okengine-local\nOKENGINE_MCP_ALLOW_DEFAULT_TOKEN=1\n",
        "OKENGINE_BIND=127.0.0.1\nOKENGINE_MCP_TOKEN=s3cret-xyz\n",
        None,   # no .env at all (fresh scaffold)
    ]
    for i, env in enumerate(ok_envs):
        pack = tmp_path / f"p{i}"
        _scaffold_with_compose(pack)
        if env is not None:
            (pack / ".env").write_text(env)
        r = v.validate(pack)
        assert not any(s == "WARN" and "MCP auth" in c and "0.0.0.0" in (d or "")
                       for s, c, d in r.rows), f"unexpected #208 WARN for env {env!r}: {r.rows}"


def test_scaffold_writes_valid_pack_yaml(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    assert (pack / "pack.yaml").is_file()
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    # the scaffolded pack.yaml is well-formed (OK row, no FAIL on it)
    assert any(s == "OK" and "pack.yaml" in c for s, c, d in r.rows)
    assert not any(s == "FAIL" and "pack.yaml" in c for s, c, d in r.rows)
    # a bad trust value is an invalid enum -> FAIL (strict)
    (pack / "pack.yaml").write_text("name: p\nversion: 0.1.0\ntrust: bogus\nowns: {types: [x]}\n")
    r = v.validate(pack)
    assert any(s == "FAIL" and "trust" in c for s, c, d in r.rows)
    assert v.main([str(pack), "--quiet"]) == 1


def test_unrendered_token_is_a_fail(tmp_path):
    """A surviving {{TOKEN}} in a declarative pack file is a broken deploy -> FAIL."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    # scaffold is fully rendered: clean
    assert not any(s == "FAIL" and "token" in c.lower() for s, c, d in v.validate(pack).rows)
    # inject an unrendered token into schema.yaml
    sp = pack / "schema.yaml"
    sp.write_text(sp.read_text() + '\n# owner: {{PACK}}\n')
    r = v.validate(pack)
    assert any(s == "FAIL" and "token" in c.lower() for s, c, d in r.rows)
    assert v.main([str(pack), "--quiet"]) == 1


def test_cron_without_usable_schedule_is_a_fail(tmp_path):
    """The nested schedule object must actually carry an expr; a cron with no
    usable schedule, or with neither script nor prompt, is a FAIL (not a WARN)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    import json
    dc = pack / "crons" / "domain-crons.json"
    # nested schedule present but expr empty -> previously slipped through (dict is truthy)
    json.loads(dc.read_text())  # sanity: parses
    dc.write_text(json.dumps([
        {"name": "no-expr", "schedule": {"kind": "cron", "expr": ""}, "prompt": "x"},
        {"name": "no-action", "schedule": {"kind": "cron", "expr": "0 0 * * *"}},
    ]))
    r = v.validate(pack)
    fails = " ".join(c for s, c, d in r.rows if s == "FAIL")
    assert "no-expr" in fails        # empty expr caught despite the dict being truthy
    assert "no-action" in fails      # neither script nor prompt
    assert v.main([str(pack), "--quiet"]) == 1


def test_domain_cron_rejects_spring_forward_loss_patterns(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    import json
    dc = pack / "crons/domain-crons.json"
    dc.write_text(json.dumps([
        {"name": "dense-gap", "schedule": {"expr": "0 0-4,8,12,16,20 * * *"},
         "prompt": "x", "enabled_toolsets": []},
        {"name": "single-gap", "schedule": {"expr": "0 2 * * *"},
         "prompt": "x", "enabled_toolsets": []},
    ]))
    failures = [(context, detail) for status, context, detail in v.validate(pack).rows
                if status == "FAIL"]
    assert any("dense-gap" in context and "collapse" in detail for context, detail in failures)
    assert any("single-gap" in context and "skipped" in detail for context, detail in failures)
    assert v._dst_schedule_problem("0 */2 * * *") is None
    assert v._dst_schedule_problem("0 1-23/2 * * *") is None
    assert v._fixed_cron_hours("") == []
    assert v._fixed_cron_hours("0 */0 * * *") == []
    assert v._fixed_cron_hours("0 a-b * * *") == []
    assert v._fixed_cron_hours("0 nope * * *") == []


def test_empty_engine_template_prompt_is_a_fail(tmp_path):
    """An engine-template lane with an empty prompt has no instructions -> FAIL."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    etp = pack / "crons" / "engine-template-prompts.json"
    import json
    data = json.loads(etp.read_text())
    first = next(iter(data))
    data[first] = "   "   # blank it
    etp.write_text(json.dumps(data))
    r = v.validate(pack)
    assert any(s == "FAIL" and "engine-template-prompts" in c for s, c, d in r.rows)
    assert v.main([str(pack), "--quiet"]) == 1


def test_engine_version_required_and_matches_engine(tmp_path):
    """engine.version must exist, carry a vX.Y.Z pin, AND match the engine running
    the validator (single source of truth: engine-manifest.yaml)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    em = _load("engine_meta", REPO / "scripts" / "engine_meta.py")
    target = em.engine_release()
    ev = pack / "engine.version"
    # the scaffold is stamped from the same manifest -> matches
    assert any(s == "OK" and "engine.version" in c for s, c, d in v.validate(pack).rows)
    assert target and target in ev.read_text()
    # missing -> FAIL
    ev.unlink()
    assert any(s == "FAIL" and "engine.version" in c for s, c, d in v.validate(pack).rows)
    assert v.main([str(pack), "--quiet"]) == 1
    # present but no vX.Y.Z pin -> FAIL
    ev.write_text("engine: okengine\nversion: latest\n")
    assert any(s == "FAIL" and "engine.version" in c for s, c, d in v.validate(pack).rows)
    # a valid-but-wrong version (drift) -> FAIL with a "this engine is" message
    ev.write_text("engine: okengine\nversion: v0.0.1\nhermes_pin: v2026.6.19\n")
    r = v.validate(pack)
    assert any(s == "FAIL" and "engine.version" in c and "this engine is" in d for s, c, d in r.rows)


def test_readme_required_and_substantive(tmp_path):
    """A pack must ship a README; missing or a stub FAILs, the detailed scaffold
    README passes."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    rm = pack / "README.md"
    assert rm.is_file()
    assert any(s == "OK" and "README.md" in c for s, c, d in v.validate(pack).rows)  # scaffold detailed
    # missing -> FAIL
    rm.unlink()
    assert any(s == "FAIL" and "README.md" in c for s, c, d in v.validate(pack).rows)
    assert v.main([str(pack), "--quiet"]) == 1
    # stub (title only, no sections) -> FAIL
    rm.write_text("# my pack\n")
    assert any(s == "FAIL" and "README.md" in c for s, c, d in v.validate(pack).rows)


def test_readme_deploy_section_mandatory(tmp_path):
    """A substantive README with sections but NO Deploy/Install heading FAILs."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    # scaffold has '## Deploy (local)' -> no deploy FAIL
    assert not any(s == "FAIL" and "Deploy" in c for s, c, d in v.validate(pack).rows)
    # a real README that omits a deploy section
    (pack / "README.md").write_text(
        "# My Pack\n\n## Overview\n\n" + ("Substantial prose about the domain. " * 12)
        + "\n\n## Schema\n\nThe types this pack declares.\n")
    r = v.validate(pack)
    assert any(s == "FAIL" and "Deploy" in c for s, c, d in r.rows)
    assert v.main([str(pack), "--quiet"]) == 1


def test_license_required(tmp_path):
    """Every pack must ship a license; missing or empty FAILs, a variant name OK."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    assert any(s == "OK" and "LICENSE" in c for s, c, d in v.validate(pack).rows)  # scaffold has one
    # remove it -> FAIL
    (pack / "LICENSE").unlink()
    assert any(s == "FAIL" and "LICENSE" in c for s, c, d in v.validate(pack).rows)
    assert v.main([str(pack), "--quiet"]) == 1
    # a variant filename with content satisfies it
    (pack / "LICENSE.md").write_text("MIT License\n\nCopyright ...\n")
    r = v.validate(pack)
    assert not any(s == "FAIL" and "LICENSE" in c for s, c, d in r.rows)
    # present but empty -> FAIL
    (pack / "LICENSE.md").write_text("   \n")
    assert any(s == "FAIL" and "LICENSE" in c for s, c, d in v.validate(pack).rows)


def test_readme_unrendered_token_is_a_fail(tmp_path):
    """A surviving {{TOKEN}} in the README is also caught (README is in the scan)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    rm = pack / "README.md"
    rm.write_text(rm.read_text() + "\n\nMaintained for {{DOMAIN}}.\n")
    r = v.validate(pack)
    assert any(s == "FAIL" and "token" in c.lower() for s, c, d in r.rows)


def test_inert_feeds_warning_is_file_specific(tmp_path):
    """The empty-feeds WARN names the OPML file and says it's deployable/inert (#11)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    warns = [(c, d) for s, c, d in v.validate(pack).rows if s == "WARN" and "feeds/" in c]
    assert warns, "no file-specific feeds warning"
    _, detail = warns[0]
    assert "deployable" in detail and "feeds/*.example" in detail


def test_gateway_env_passthrough_required(tmp_path):
    """The gateway must receive .env (env_file or an explicit model-key env); a
    compose that passes neither FAILs (#22)."""
    import yaml as y
    pack = tmp_path / "pack"
    _scaffold_with_compose(pack)
    v = _load("framework_validate", VAL)
    compose = pack / "docker-compose.yml"
    # scaffold compose ships env_file -> OK
    assert any(s == "OK" and "gateway .env" in c for s, c, d in v.validate(pack).rows)
    # strip it -> FAIL
    data = y.safe_load(compose.read_text())
    data["services"]["gateway"].pop("env_file", None)
    compose.write_text(y.safe_dump(data))
    assert any(s == "FAIL" and "gateway .env" in c for s, c, d in v.validate(pack).rows)
    assert v.main([str(pack), "--quiet"]) == 1
    # an explicit model-key env entry also satisfies it
    data["services"]["gateway"]["environment"] = ["OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"]
    compose.write_text(y.safe_dump(data))
    assert not any(s == "FAIL" and "gateway .env" in c for s, c, d in v.validate(pack).rows)


def test_cli_dispatches_validate(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    cli = _load("framework", CLI)
    assert cli.main(["validate", str(pack), "--quiet"]) == 0
    assert cli.main(["bogus-cmd"]) == 2


def test_alias_shadowing_declared_type_is_a_fail(tmp_path):
    """okengine v0.9.0 sweep class: an alias key that IS a declared type makes the
    normalization drains silently retype canonical pages. Pack-side this was only a
    WARN (deployment-validate and coinstall_preflight already FAILed it), which let
    the digest-alias collision reach a live deployment — severity now agrees."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    schema = (pack / "schema.yaml").read_text()
    schema += ("\n" if not schema.endswith("\n") else "")
    schema = schema.replace("type_aliases: {}",
                            "type_aliases: {funding-digest: briefing}", 1)
    schema += "\n"
    # declare the same name as a canonical type -> shadow
    schema = schema.replace("types:", "types:\n  funding-digest: {required: [type]}", 1)
    (pack / "schema.yaml").write_text(schema)
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    hits = [(s, d) for s, c, d in r.rows if c == "schema.type_aliases" and s == "FAIL"]
    assert hits and "funding-digest" in hits[0][1], r.rows
    assert v.main([str(pack), "--quiet"]) == 1


def test_subdomain_schema_without_partitioning_warns(tmp_path):
    """First automated subtree install: a subdomain schema with types but no
    partitioning leaves dir creation empty and the subtree namespace guard a no-op."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    import yaml as _yaml
    main_schema = _yaml.safe_load((pack / "schema.yaml").read_text())
    main_schema["types"] = main_schema.get("types") or {}   # scaffold ships a commented-empty block
    main_schema["types"]["assumption"] = {"required": ["type"]}
    (pack / "schema.yaml").write_text(_yaml.safe_dump(main_schema, sort_keys=False))
    (pack / "subdomain").mkdir()
    (pack / "subdomain" / "schema.yaml").write_text(
        "types:\n  assumption: {required: [type]}\n")
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    warns = [d for s, c, d in r.rows if s == "WARN" and c == "subdomain form"
             and "partitioning" in d]
    assert warns, r.rows
    # and the fix silences it
    (pack / "subdomain" / "schema.yaml").write_text(
        "types:\n  assumption: {required: [type]}\n"
        "partitioning:\n  namespaces:\n    assumptions: {strategy: flat}\n")
    r2 = v.validate(pack)
    assert not [d for s, c, d in r2.rows if s == "WARN" and "partitioning" in d], r2.rows


def test_pack_description_mission_warns(tmp_path):
    """description/mission feed About + catalog + framework list (multi-surface):
    missing description WARNs; a scaffold-TODO mission WARNs; filled = silent."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    warns = lambda r: [c for s, c, d in r.rows if s == "WARN" and c.startswith("pack.yaml")]
    # scaffold ships description filled + mission TODO -> exactly the mission warn
    w = warns(v.validate(pack))
    assert "pack.yaml mission" in w, w
    # strip description -> the description warn
    t = (pack / "pack.yaml").read_text()
    import re
    (pack / "pack.yaml").write_text(re.sub(r'^description:.*\n', "", t, flags=re.M))
    w = warns(v.validate(pack))
    assert "pack.yaml description" in w, w
    # fill both -> neither
    t = (pack / "pack.yaml").read_text()
    t = "description: A test domain\n" + re.sub(r'^mission: >-\n(  .*\n)+', "mission: Real mission text.\n", t, flags=re.M)
    (pack / "pack.yaml").write_text(t)
    w = warns(v.validate(pack))
    assert "pack.yaml mission" not in w and "pack.yaml description" not in w, w


# --- okengine#181: kind: bundle validates its recipe, not domain content -----------

def _make_bundle(pack: Path, recipe_yaml: str):
    """Scaffold a clean normal pack (correct engine.version/README/.env), then convert it
    into a kind: bundle: owns nothing, ships a recipe, no schema/contract."""
    _scaffold(pack)
    (pack / "pack.yaml").write_text(recipe_yaml)
    (pack / "schema.yaml").unlink()          # a bundle ships no contract


def test_bundle_validates_without_schema(tmp_path):
    pack = tmp_path / "okpack-cti"
    _make_bundle(pack,
                 "name: okpack-cti\nversion: 0.3.0\nkind: bundle\ntrust: public\n"
                 "description: security bundle\n"
                 "owns: {types: [], namespaces: []}\n"
                 "requires: [okpack-a, okpack-b]\n"
                 "bundle: {host: okpack-a, compose: [okpack-b]}\n")
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"]
    assert fails == [], f"unexpected FAILs for a bundle: {fails}"
    assert any(s == "OK" and c == "bundle recipe" for s, c, d in r.rows)
    assert v.main([str(pack), "--quiet"]) == 0


def test_bundle_malformed_recipe_is_a_fail(tmp_path):
    pack = tmp_path / "okpack-bad"
    _make_bundle(pack,
                 "name: okpack-bad\nkind: bundle\ntrust: public\ndescription: x\n"
                 "owns: {types: [], namespaces: []}\n"
                 "requires: []\n"
                 "bundle: {host: okpack-a, compose: [okpack-a]}\n")  # host in compose + not required
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert any(s == "FAIL" and c == "bundle recipe" for s, c, d in r.rows), r.rows
    assert v.main([str(pack), "--quiet"]) == 1


def test_enabled_but_undiscovered_extension_fails_validate(tmp_path):  # invariant-audit #39
    """An enabled id in .okengine/extensions.yaml that no longer resolves (e.g. an engine upgrade
    renamed a tier-1 extension the operator had enabled) must FAIL `framework validate` — the deploy's
    step-1 fail-fast gate — not first hard-stop at deploy step 5 AFTER every container was recreated.
    check_extension_requirements only covered ids the pack DECLARES; enabled-only ids were unguarded."""
    import yaml as _yaml
    pack = tmp_path / "pack"
    _scaffold(pack)
    okd = pack / ".okengine"
    okd.mkdir(exist_ok=True)
    (okd / "extensions.yaml").write_text(
        _yaml.safe_dump({"enabled": {"acme.ghost": {}}}), encoding="utf-8")
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"
             and ("ghost" in d.lower() or "extension" in c.lower())]
    assert fails, f"an enabled-but-undiscovered extension must FAIL validate; rows={r.rows}"
    assert v.main([str(pack), "--quiet"]) == 1


def test_unknown_partition_strategy_fails_validate(tmp_path):  # invariant-audit #25
    """A partitioned namespace's `strategy` must be one okf_migrate knows — a typo (`by_date`) or
    invented value silently degraded to flat while drains treated it as partitioned. Gate at validate."""
    import yaml as _yaml
    pack = tmp_path / "pack"
    _scaffold(pack)
    sp = pack / "schema.yaml"
    sch = _yaml.safe_load(sp.read_text())
    sch.setdefault("partitioning", {}).setdefault("namespaces", {})["sources"] = {
        "strategy": "by_date", "date_field": "published"}     # underscore typo
    sp.write_text(_yaml.safe_dump(sch))
    v = _load("framework_validate", VAL)
    rows = v.validate(pack).rows
    assert any(s == "FAIL" and "strategy" in c and "by_date" in d for s, c, d in rows), rows


def test_cross_tier_duplicate_extension_fails_validate(tmp_path):  # invariant-audit #351
    """check_enabled_extensions_resolve must surface discover()'s Rule-2 cross-tier duplicate-id
    error (a hard FAIL per the discovery spec). resolve_enabled() indexes discovered records by bare
    id, so a duplicate is silently last-wins with NO res_error — before the fix, disc_errors was
    dropped from `problems` and an ambiguous extension validated CLEAN. Now it must FAIL."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    man = {"id": "demo.dup", "kind": "operation", "version": "0.1.0", "name": "demo.dup",
           "requires": {"engine": ">=0.3.0"}, "trust": "in-gateway",
           "capabilities": {"read": ["wiki/**"], "write": ["dup/**"]}}
    for sub in ("extensions", ".okengine/extensions"):        # same id in pack tier AND operator tier
        d = pack / sub / "demo.dup"
        d.mkdir(parents=True, exist_ok=True)
        (d / "extension.yaml").write_text(yaml.safe_dump(man), encoding="utf-8")
    (pack / ".okengine").mkdir(exist_ok=True)
    (pack / ".okengine" / "extensions.yaml").write_text(
        yaml.safe_dump({"enabled": {"demo.dup": {}}}), encoding="utf-8")
    r = v.Report()
    v.check_enabled_extensions_resolve(pack, r)
    fails = [d for s, c, d in r.rows if s == "FAIL"]
    assert any("demo.dup" in d and "multiple tiers" in d for d in fails), r.rows


def test_owns_must_cover_non_core_schema_types_and_namespaces(tmp_path):  # invariant-audit #351
    """compose-preview builds a pack's schema fragment from pack.yaml owns ONLY, so a non-core type
    or partitioning namespace present in schema.yaml but absent from owns is invisible to the
    co-install collision gate. check_owns_covers_schema WARNs on that divergence (not FAIL — a
    standalone pack is harmless and compose-preview is the real collision gate), and is clean once
    owns covers it. Core (base-schema) types/namespaces need no owns entry."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    # a freshly scaffolded pack (core-only schema, empty owns) has NO owns/schema divergence
    r0 = v.Report()
    v.check_owns_covers_schema(pack, r0)
    assert [d for s, c, d in r0.rows if s in ("FAIL", "WARN")] == [], r0.rows
    # introduce a non-core type + a pack namespace in schema.yaml but NOT in owns -> WARN (never FAIL).
    # (the scaffold's `types:`/`partitioning:` keys can be null, so coerce before mutating)
    sch = yaml.safe_load((pack / "schema.yaml").read_text()) or {}
    types = sch.get("types") if isinstance(sch.get("types"), dict) else {}
    types["gadget"] = {"required": ["name"]}
    sch["types"] = types
    part = sch.get("partitioning") if isinstance(sch.get("partitioning"), dict) else {}
    nss = part.get("namespaces") if isinstance(part.get("namespaces"), dict) else {}
    nss["gadgets"] = {"strategy": "by_letter"}
    part["namespaces"] = nss
    sch["partitioning"] = part
    (pack / "schema.yaml").write_text(yaml.safe_dump(sch), encoding="utf-8")
    r1 = v.Report()
    v.check_owns_covers_schema(pack, r1)
    assert not [d for s, c, d in r1.rows if s == "FAIL"], f"must WARN not FAIL: {r1.rows}"
    warns = " | ".join(d for s, c, d in r1.rows if s == "WARN")
    assert "gadget" in warns and "owns.types" in warns, r1.rows
    assert "gadgets" in warns and "owns.namespaces" in warns, r1.rows
    # declare them in owns -> clean
    pm = yaml.safe_load((pack / "pack.yaml").read_text()) or {}
    owns = pm.get("owns") if isinstance(pm.get("owns"), dict) else {}
    owns["types"] = (owns.get("types") if isinstance(owns.get("types"), list) else []) + ["gadget"]
    owns["namespaces"] = (owns.get("namespaces") if isinstance(owns.get("namespaces"), list) else []) + ["gadgets"]
    pm["owns"] = owns
    (pack / "pack.yaml").write_text(yaml.safe_dump(pm), encoding="utf-8")
    r2 = v.Report()
    v.check_owns_covers_schema(pack, r2)
    assert [d for s, c, d in r2.rows if s in ("FAIL", "WARN")] == [], r2.rows


def test_owns_check_honors_schema_exclude(tmp_path):  # invariant-audit #351 / #359 follow-up
    """A namespace in schema.exclude is intentionally OUTSIDE the pack's OKF scope (a shared render
    tree like dashboards/operational) — it is NOT owned and must not warn. Excluding it clears the
    owns.namespaces warning without adding it to owns (the convention for shared trees)."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    sch = yaml.safe_load((pack / "schema.yaml").read_text()) or {}
    part = sch.get("partitioning") if isinstance(sch.get("partitioning"), dict) else {}
    nss = part.get("namespaces") if isinstance(part.get("namespaces"), dict) else {}
    nss["dashboards"] = {"strategy": "flat"}
    part["namespaces"] = nss
    sch["partitioning"] = part
    sch["exclude"] = (sch.get("exclude") or []) + ["wiki/dashboards/"]   # shared render tree, not owned
    (pack / "schema.yaml").write_text(yaml.safe_dump(sch), encoding="utf-8")
    r = v.Report()
    v.check_owns_covers_schema(pack, r)
    assert not any("dashboards" in d for s, c, d in r.rows), \
        f"an EXCLUDED namespace must not warn: {r.rows}"


def _percheck_cron(pack: Path, *, script_body: str | None, script_name="select_thing.py"):
    """A pack cron declaring completion=per-selected-item, with a selector we control."""
    import json
    crons = pack / "crons"
    crons.mkdir(parents=True, exist_ok=True)
    if script_body is not None:
        sdir = crons / "scripts"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / script_name).write_text(script_body)
    (crons / "domain-crons.json").write_text(json.dumps([{
        "name": "thing-drain",
        "id": "aaaabbbbcccc",
        "script": script_name,
        "schedule": {"kind": "cron", "expr": "0 * * * *"},
        "enabled": True,
        "workdir": "/opt/vault",
        "enabled_toolsets": ["okengine-write", "okengine"],
        "adversarial_fixtures": ["tests/cron/test_model_write_contract_inventory.py"],
        "output_contract": {"api": 1, "allowed_namespaces": ["entities"],
                            "allowed_types": ["*"], "operations": ["update"],
                            "required_fields": ["type"], "required_relationships": [],
                            "body": {"required": False, "min_non_whitespace": 0},
                            "unknown_fields": "reject", "unresolved_links": "review",
                            "placeholder_links": "reject",
                            "completion": "per-selected-item"},
    }], indent=1))


def test_per_selected_item_lane_without_a_manifest_writer_is_a_fail(tmp_path):
    """okengine#478: declaring the contract without writing the manifest fails EVERY run.

    `per-selected-item` makes the runner verify the receipt against the selection manifest.
    A selector that never writes one yields "selection manifest unavailable" forever, and the
    failure reads like a model fault. Measured live: 6 engine lanes in exactly this state,
    171 failed receipts. Two surfaces that must agree, with nothing enforcing the agreement.
    """
    pack = tmp_path / "pack"
    _scaffold(pack)
    _percheck_cron(pack, script_body="print('I select things but write no manifest')\n")
    v = _load("framework_validate", VAL)
    rows = v.validate(pack).rows
    assert any(s == "FAIL" and "never writes a selection manifest" in d
               for s, _c, d in rows), [r for r in rows if r[0] == "FAIL"]


def test_per_selected_item_lane_with_a_manifest_writer_passes(tmp_path):
    """The other direction: a selector that writes the manifest must NOT be flagged."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    _percheck_cron(pack, script_body=(
        "from selection_manifest import write_selection_manifest\n"
        "write_selection_manifest([], 'x.json')\n"))
    v = _load("framework_validate", VAL)
    rows = v.validate(pack).rows
    assert not any("selection manifest" in d for s, _c, d in rows if s == "FAIL"), rows


def test_required_write_lane_rejects_two_iteration_budget(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    _percheck_cron(pack, script_body="print('select')\n")
    path = pack / "crons/domain-crons.json"
    jobs = json.loads(path.read_text())
    jobs[0]["output_contract"]["completion"] = "run"
    jobs[0]["output_contract"]["required_write_path"] = "briefings/daily-{date}.md"
    jobs[0]["max_iterations"] = 2
    path.write_text(json.dumps(jobs))
    rows = _load("framework_validate", VAL).validate(pack).rows
    assert any(s == "FAIL" and c == "cron iteration budget" and "at least 6" in d
               for s, c, d in rows), rows


def test_required_write_lane_accepts_bounded_recovery_budget(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    _percheck_cron(pack, script_body="print('select')\n")
    path = pack / "crons/domain-crons.json"
    jobs = json.loads(path.read_text())
    jobs[0]["output_contract"]["completion"] = "run"
    jobs[0]["output_contract"]["required_write_path"] = "briefings/daily-{date}.md"
    jobs[0]["max_iterations"] = 8
    path.write_text(json.dumps(jobs))
    rows = _load("framework_validate", VAL).validate(pack).rows
    assert not any(s == "FAIL" and c == "cron iteration budget" for s, c, _d in rows), rows


def test_unfindable_selector_warns_rather_than_vacuously_passing(tmp_path):
    """An absent selector is UNDETECTABLE, not clean — it must warn, never silently pass."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    _percheck_cron(pack, script_body=None)          # declared, but no file written
    v = _load("framework_validate", VAL)
    rows = v.validate(pack).rows
    assert any(s == "WARN" and "cannot confirm" in d for s, _c, d in rows), rows


def _partitioned_pack(tmp_path, strategy="by-letter"):
    pack = tmp_path / "pack"
    _scaffold(pack)
    sp = pack / "schema.yaml"
    sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    sch.setdefault("partitioning", {}).setdefault("namespaces", {})["things"] = {"strategy": strategy}
    sp.write_text(yaml.safe_dump(sch, sort_keys=False), encoding="utf-8")
    sdir = pack / "crons" / "scripts"
    sdir.mkdir(parents=True, exist_ok=True)
    return pack, sdir


def _fails(pack):
    v = _load("framework_validate", VAL)
    return [(c, d) for s, c, d in v.validate(pack).rows if s == "FAIL"]


def _resharding_pack(tmp_path, reshard_by="second-letter"):
    pack, sdir = _partitioned_pack(tmp_path)
    sp = pack / "schema.yaml"
    sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    sch["partitioning"]["namespaces"]["things"]["reshard_by"] = reshard_by
    sp.write_text(yaml.safe_dump(sch, sort_keys=False), encoding="utf-8")
    return pack, sdir


def test_a_lane_seating_pages_by_canonical_key_fails(tmp_path):
    """okengine#818. canonical_key() is the drain's BASE bucket; reshard_oversized keeps a split
    bucket one level deeper and sweeps stragglers nightly, so a writer using the base key re-mints
    them one level up all day. On a live vault that left 448 pages loose in 23 already-split
    buckets between sweeps, and every consumer that had stored a page path held a stale one."""
    pack, sdir = _resharding_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "rel = okf_migrate.canonical_key(root, ns, slug, fm)\n", encoding="utf-8")
    hits = [d for c, d in _fails(pack) if c == "cron reshard-aware writes"]
    assert hits and "lane.py" in hits[0] and "write_key()" in hits[0], _fails(pack)


def test_a_bare_canonical_key_call_is_caught_too(tmp_path):
    """The helper is imported both ways: `okf_migrate.canonical_key(...)` and a bare
    `canonical_key(...)` from `from okf_migrate import canonical_key`."""
    pack, sdir = _resharding_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "from okf_migrate import canonical_key\nkey = canonical_key(vault, ns, slug)\n",
        encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron reshard-aware writes"], _fails(pack)


def test_write_key_is_the_contract_and_passes(tmp_path):
    pack, sdir = _resharding_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "rel = okf_migrate.write_key(root, ns, slug, fm)\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron reshard-aware writes"] == [], _fails(pack)


def _no_reshard_pack(tmp_path, reshard_by=None):
    """A pack whose governing schema reshards NOTHING. The scaffold inherits the engine core
    (entities/sources/concepts), which all declare `reshard_by`, so every declaration has to be
    cleared for this case to exist at all — which is itself the point: on a real vault the base
    key is almost never the seat."""
    pack, sdir = _partitioned_pack(tmp_path)
    sp = pack / "schema.yaml"
    sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    for cfg in (sch.get("partitioning") or {}).get("namespaces", {}).values():
        if isinstance(cfg, dict):
            cfg.pop("reshard_by", None)
            if reshard_by:
                cfg["reshard_by"] = reshard_by
    sp.write_text(yaml.safe_dump(sch, sort_keys=False), encoding="utf-8")
    return pack, sdir


def test_canonical_key_is_fine_where_no_namespace_reshards(tmp_path):
    """Without a `reshard_by` directive anywhere, the base key IS the seat, so the call is correct
    and must not be reported."""
    pack, sdir = _no_reshard_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "rel = okf_migrate.canonical_key(root, ns, slug, fm)\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron reshard-aware writes"] == [], _fails(pack)


def test_not_applicable_reshard_by_is_not_a_resharding_namespace(tmp_path):
    pack, sdir = _no_reshard_pack(tmp_path, reshard_by="not-applicable")
    (sdir / "lane.py").write_text(
        "rel = okf_migrate.canonical_key(root, ns, slug, fm)\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron reshard-aware writes"] == [], _fails(pack)


def test_a_syntactically_broken_lane_is_not_a_reshard_finding(tmp_path):
    pack, sdir = _resharding_pack(tmp_path)
    (sdir / "lane.py").write_text("def broken(:\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron reshard-aware writes"] == [], _fails(pack)


def test_hand_built_path_into_a_partitioned_namespace_fails(tmp_path):
    """okengine#54. `canonical_key`/`write_key` exist so an importer and the reshelve drain 'can
    never disagree and re-open the loop'. A lane that hand-builds the path re-creates the page at
    its own spelling each run while the drain files it under the declared strategy — on one live
    vault, 48 duplicated records, NINE of which disagreed about whether the judgment was live."""
    pack, sdir = _partitioned_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "rel = f'things/kind/{rec}.md'\n", encoding="utf-8")
    hits = [d for c, d in _fails(pack) if c == "cron partition-aware writes"]
    assert hits and "things/kind" in hits[0], _fails(pack)


def test_quoted_interpolation_does_not_hide_the_offender(tmp_path):
    """The real lanes read f\"assessments/identity/{rec['id']}.md\" — inner QUOTES that no
    quote-delimited regex can span. A text scan missed exactly the three lanes this check exists
    for, so the check parses the AST and renders interpolations to a shape."""
    pack, sdir = _partitioned_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "rel = f\"things/kind/{record['id'].rsplit(':', 1)[-1]}.md\"\n", encoding="utf-8")
    hits = [d for c, d in _fails(pack) if c == "cron partition-aware writes"]
    assert hits and "things/kind" in hits[0], _fails(pack)


def test_a_computed_shard_segment_is_not_judged(tmp_path):
    """An interpolated segment may compute the correct seat; statically it cannot be called wrong.
    corpus_audit's partition_collisions catches those against the real corpus instead."""
    pack, sdir = _partitioned_pack(tmp_path)
    (sdir / "lane.py").write_text(
        "rel = f\"things/{slug[0].lower()}/{slug}.md\"\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron partition-aware writes"] == []


def test_a_single_letter_segment_is_a_plausible_shard(tmp_path):
    pack, sdir = _partitioned_pack(tmp_path)
    (sdir / "lane.py").write_text("rel = f'things/a/{slug}.md'\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron partition-aware writes"] == []


def test_a_flat_namespace_may_carry_sub_segments(tmp_path):
    """flat namespaces 'may legitimately carry sub-segments' (is_partitioned's own contract), so
    the check must not fire on every correctly-built pack layout."""
    pack, sdir = _partitioned_pack(tmp_path, strategy="flat")
    (sdir / "lane.py").write_text("rel = f'things/kind/{rec}.md'\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron partition-aware writes"] == []


def test_a_syntactically_broken_lane_is_the_compile_checks_verdict(tmp_path):
    """This check must not double-report a syntax error as a placement defect."""
    pack, sdir = _partitioned_pack(tmp_path)
    (sdir / "lane.py").write_text("def broken(:\n", encoding="utf-8")
    assert [d for c, d in _fails(pack) if c == "cron partition-aware writes"] == []


def test_a_pack_script_shadowing_an_engine_cron_script_is_a_fail(tmp_path):
    """`deploy-cron-scripts.sh` stages the engine's `scripts/cron/*.py` first and the pack's
    `crons/scripts/*.py` second, into the SAME `/opt/data/scripts/`. A shared basename means the
    pack copy silently wins -- on every deploy, forever -- and nothing downstream notices: the
    deploy exits 0, the file is present, and the cron runs "successfully" doing whatever the fork
    does.

    Measured: okcti-test carried a pre-#267 fork of `nvd_import.py`. The engine had since taken
    ownership of that lane, and the regenerated cron def already passed `NVD_PAGE_MODEL` -- which
    the fork ignores. So a live deployment ran a three-week-stale lane whose own configuration
    described a different one, and it surfaced only because a hash was compared inside the
    container. This is the gate that makes it visible before the deploy, not after.
    """
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    engine_cron = Path(v.__file__).resolve().parent / "cron"
    victim = sorted(p.name for p in engine_cron.glob("*.py"))[0]

    scripts = pack / "crons" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / victim).write_text("# a pack fork of an engine lane\n", encoding="utf-8")

    r = v.validate(pack)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"]
    assert any("shadow" in d for _c, d in fails), f"shadowing must FAIL, got: {fails}"
    assert any(victim in d for _c, d in fails), f"the offending file must be named: {fails}"
    assert v.main([str(pack), "--quiet"]) != 0, "a shadow must make the whole validate fail"


def test_a_pack_script_with_its_own_name_is_not_a_shadow(tmp_path):
    """The negative half. Packs are SUPPOSED to ship domain cron scripts -- if any pack script
    tripped this, every pack would fail validation and the check would be turned off rather than
    fixed. Only a same-basename collision is refused; a deliberate override renames."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)

    scripts = pack / "crons" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "my_domain_lane.py").write_text("print('domain')\n", encoding="utf-8")

    r = v.validate(pack)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"]
    assert fails == [], f"a distinctly-named pack script is normal, got: {fails}"
    assert any("no pack script shadows" in d for s, _c, d in r.rows if s == "OK"), r.rows


def test_a_shadowing_script_is_not_described_as_engine_supplied(tmp_path):
    """The cron-def line used to say "supplied by the engine" whenever the engine HAD the script —
    including when the pack also had it, which is the one case where the engine's copy is precisely
    what does NOT run. That reassuring sentence is what a reader saw for three weeks while okcti ran
    a fork. It must name the file that actually runs."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    victim = sorted(p.name for p in v.ENGINE_CRON_DIR.glob("*.py"))[0]

    scripts = pack / "crons" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / victim).write_text("# pack fork\n", encoding="utf-8")
    crons = pack / "crons"
    crons.mkdir(parents=True, exist_ok=True)
    (crons / "domain-crons.json").write_text(json.dumps([{
        "name": "forked-lane", "no_agent": True,
        "schedule": {"kind": "cron", "expr": "0 3 * * *"},
        "script": f"/opt/data/scripts/{victim}",
    }]), encoding="utf-8")

    rows = v.validate(pack).rows
    said = " ".join(d for _s, c, d in rows if "forked-lane" in c)
    assert "the PACK copy runs" in said, said
    assert "supplied by the engine" not in said, (
        "an engine copy that is overwritten at deploy must not be reported as the live one")


def test_shadowing_reports_undetectable_when_the_engine_cron_dir_is_missing(tmp_path,
                                                                            monkeypatch):
    """The missing-key rule: with nothing to compare against, the check must say it could not look
    rather than emit a confident "no shadows". A vacuous pass here is exactly the shape of failure
    the detector exists to remove."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    (pack / "crons" / "scripts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(v, "ENGINE_CRON_DIR", tmp_path / "no-such-engine-cron-dir")

    rows = v.validate(pack).rows
    shadow_rows = [(s, d) for s, c, d in rows if "shadow" in c.lower() or "scripts/cron" in c]
    assert any(s == "WARN" and "UNDETECTABLE" in d for s, d in shadow_rows), shadow_rows
    assert not any(s == "OK" and "no pack script shadows" in d for s, d in shadow_rows), (
        "must not claim a clean result it could not measure")
