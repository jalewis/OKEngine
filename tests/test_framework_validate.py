"""Regression: `framework validate` catches deploy-breaking pack defects."""
import importlib.util
import sys
from pathlib import Path

import pytest

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


def test_broken_schema_is_a_fail(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    (pack / "schema.yaml").write_text("types: [this, is, not, a, mapping\n:::bad yaml")
    v = _load("framework_validate", VAL)
    assert v.main([str(pack), "--quiet"]) == 1


def test_missing_persona_is_a_fail(tmp_path):
    pack = tmp_path / "pack"
    _scaffold(pack)
    (pack / "CLAUDE.md").unlink()
    v = _load("framework_validate", VAL)
    r = v.validate(pack)
    assert any(s == "FAIL" and "persona" in c.lower() or "CLAUDE.md" in c for s, c, d in r.rows if s == "FAIL")
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
    # (b) remove it; the scaffold .gitignore excludes .hermes-data -> WARN, not FAIL
    cfg.unlink()
    rows = v.validate(pack).rows
    assert any(s == "WARN" and "config.yaml" in c for s, c, d in rows)
    assert not any(s == "FAIL" and "config.yaml" in c for s, c, d in rows)
    assert v.main([str(pack), "--quiet"]) == 0   # WARN doesn't block deploy
    # (c) if .hermes-data isn't gitignored, a missing config is a real FAIL
    (pack / ".gitignore").write_text("# no runtime ignore\n.env\n")
    assert any(s == "FAIL" and "config.yaml" in c for s, c, d in v.validate(pack).rows)


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


def test_pack_level_strict_types_warns_engine_owned(tmp_path):
    """#23: strict_types is engine-owned — a pack declaring it is WARNed (ignored,
    not a FAIL). A scaffold (which doesn't declare it) does not warn."""
    pack = tmp_path / "pack"
    _scaffold(pack)
    v = _load("framework_validate", VAL)
    assert not any("strict_types" in c for s, c, d in v.validate(pack).rows)  # scaffold clean
    (pack / "schema.yaml").write_text(
        "okf:\n  required: [type]\nstrict_types: true\ntypes:\n  entity: {required: [type]}\n")
    rows = v.validate(pack).rows
    assert any(s == "WARN" and "strict_types" in c for s, c, d in rows)
    assert not any(s == "FAIL" and "strict_types" in c for s, c, d in rows)


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
    ev.write_text("engine: okengine\nversion: v0.0.1\nhermes_pin: v2026.6.5\n")
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
