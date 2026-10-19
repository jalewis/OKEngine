"""Regression tests for the extension trust gate (okengine#124).

No OS sandboxing/signing yet, so an OPERATOR-tier (third-party/paid drop-in) extension
that runs in-gateway (full access, no isolation) is refused unless --allow-untrusted.
pack/engine-tier in-gateway (author already trusted) and any-tier sidecar (isolated)
are allowed.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
CLI = REPO / "scripts" / "framework_extensions.py"
DISC = REPO / "scripts" / "extension_discovery.py"

pytestmark = pytest.mark.skipif(not CLI.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest(ext_id, trust):
    if trust == "sidecar":
        ep = {"image": {"registry": f"r/{ext_id}", "digest": "sha256:abc"}}
    else:
        ep = {"script": "run.py"}
    return {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": trust,
            "requires": {"engine": ">=0.3.0"},
            "capabilities": {"read": ["wiki/**"], "write": [ext_id.split(".")[-1] + "/**"]},
            "operation": {"schedule": {"kind": "cron", "expr": "@jitter:daily@4"}, "entrypoint": ep}}


def _operator_ext(pack, ext_id, trust="in-gateway"):
    d = pack / ".okengine" / "extensions" / ext_id          # tier-3: operator drop-in
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_manifest(ext_id, trust)), encoding="utf-8")
    if trust != "sidecar":
        (d / "run.py").write_text("print('{}')\n", encoding="utf-8")
    return d


def _pack_ext(pack, ext_id, trust="in-gateway"):
    d = pack / "extensions" / ext_id                        # tier-2: pack-bundled
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_manifest(ext_id, trust)), encoding="utf-8")
    if trust != "sidecar":
        (d / "run.py").write_text("print('{}')\n", encoding="utf-8")
    return d


def test_operator_in_gateway_refused_by_default(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party"]) == 1     # refused
    enabled, _ = disc.load_enabled_state(pack)
    assert "third.party" not in enabled                            # no state change


def test_operator_in_gateway_allowed_with_override(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    assert "third.party" in enabled


def test_operator_sidecar_allowed(tmp_path):
    """A sidecar is isolated (own container + scoped MCP), so an operator-tier sidecar
    needs no override."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.side", trust="sidecar")
    assert cli.main(["enable", str(pack), "third.side"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    assert "third.side" in enabled


def test_pack_tier_in_gateway_allowed(tmp_path):
    """A pack-bundled in-gateway extension is as trusted as the pack's own crons — allowed."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _pack_ext(pack, "demo.bundled")
    assert cli.main(["enable", str(pack), "demo.bundled"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    assert "demo.bundled" in enabled


# ── okengine#994 [14]: the acceptance is RECORDED and re-checked at load time ────────────────────
COMP = REPO / "scripts" / "extension_compose.py"
VALIDATE = REPO / "scripts" / "framework_validate.py"


def _hand_enable(pack, ext_id, settings=None):
    """Enable WITHOUT the CLI, the way an operator edit (or a pre-#994 enable) leaves state."""
    state = pack / ".okengine" / "extensions.yaml"
    state.parent.mkdir(parents=True, exist_ok=True)
    current = yaml.safe_load(state.read_text()) if state.is_file() else {}
    current = current or {}
    current.setdefault("enabled", {})[ext_id] = settings or {}
    state.write_text(yaml.safe_dump(current), encoding="utf-8")


def _staging_and_jobs(pack):
    comp = _load("extension_compose", COMP)
    targets, stage_errors = comp.staging_targets(pack)
    jobs, job_errors = comp.extension_jobs(pack)
    mine = lambda name: not name.startswith("okengine.")     # core default-on engine lanes aside
    return ([t["id"] for t in targets if mine(t["id"])], stage_errors,
            [j["name"] for j in jobs if mine(j["name"])], job_errors)


def test_unaccepted_operator_in_gateway_is_neither_staged_nor_scheduled(tmp_path):
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == [] and jobs == []
    for errors in (stage_errors, job_errors):
        assert len(errors) == 1 and "third.party" in errors[0], errors
        assert f"framework extensions enable {pack} third.party --allow-untrusted" in errors[0]


def test_allow_untrusted_records_acceptance_and_unblocks_load(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    enabled, _ = disc.load_enabled_state(pack)
    accepted = enabled["third.party"][disc.TRUST_ACCEPTED_KEY]
    assert accepted["trust"] == "in-gateway" and accepted["accepted_at"].endswith("Z")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == ["third.party"] and jobs == ["third.party"]
    assert stage_errors == [] and job_errors == []


def test_one_time_accept_of_an_already_enabled_extension_keeps_its_entry(tmp_path, capsys):
    """The live upgrade path: enabled before acceptance was recorded. One `enable --allow-untrusted`
    records it and preserves the entry's existing settings."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party", {"operator_note": "kept"})
    assert _staging_and_jobs(pack)[1], "precondition: blocked before acceptance"
    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    assert "trust acceptance recorded: third.party" in capsys.readouterr().out
    entry = disc.load_enabled_state(pack)[0]["third.party"]
    assert entry["operator_note"] == "kept" and entry[disc.TRUST_ACCEPTED_KEY]["trust"] == "in-gateway"
    assert _staging_and_jobs(pack)[0] == ["third.party"]
    # once recorded, a plain re-enable needs no flag
    assert cli.main(["enable", str(pack), "third.party"]) == 0


def test_already_enabled_without_flag_is_still_refused(tmp_path, capsys):
    cli = _load("framework_extensions", CLI)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party"]) == 1
    assert "allow-untrusted" in capsys.readouterr().err


def test_a_manifest_flipped_to_in_gateway_after_enable_is_blocked(tmp_path):
    """Enabled as a sidecar (no acceptance needed), then repackaged in-gateway: blocked at load."""
    cli = _load("framework_extensions", CLI)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.flip", trust="sidecar")
    assert cli.main(["enable", str(pack), "third.flip"]) == 0
    _operator_ext(pack, "third.flip", trust="in-gateway")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == [] and jobs == [] and stage_errors and job_errors


@pytest.mark.parametrize("record", [
    "in-gateway",                                   # not a mapping
    {"trust": "sidecar"},                           # accepted a different trust level
    {"accepted_at": "2026-10-06T00:00:00Z"},        # no trust named
])
def test_a_malformed_or_mismatched_acceptance_does_not_count(tmp_path, record):
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party", {"trust_accepted": record})
    assert _staging_and_jobs(pack)[0] == []


def test_disable_clears_the_acceptance(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    assert cli.main(["disable", str(pack), "third.party"]) == 0
    assert cli.main(["enable", str(pack), "third.party"]) == 1
    assert "third.party" not in disc.load_enabled_state(pack)[0]


def test_trusted_tiers_and_sidecars_need_no_acceptance(tmp_path):
    """Negative fixture: the load gate must not block what never needed acceptance."""
    pack = tmp_path / "pack"
    _pack_ext(pack, "demo.bundled")
    _operator_ext(pack, "third.side", trust="sidecar")
    _hand_enable(pack, "demo.bundled")
    _hand_enable(pack, "third.side")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == ["demo.bundled"] and "demo.bundled" in jobs
    assert stage_errors == [] and job_errors == []


def test_extensions_validate_fails_an_unaccepted_operator_extension(tmp_path, capsys):
    cli = _load("framework_extensions", CLI)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party")
    assert cli.main(["validate", str(pack)]) == 1
    assert "--allow-untrusted" in capsys.readouterr().out
    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    capsys.readouterr()
    assert cli.main(["validate", str(pack)]) == 0


def test_framework_validate_fails_before_deploy_naming_the_accept_command(tmp_path):
    """Deploy step 1 must FAIL here, before step 4 recreates containers."""
    v = _load("framework_validate", VALIDATE)
    cli = _load("framework_extensions", CLI)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party")
    r = v.Report()
    v._extension_checks().check_operator_trust_acceptance(pack, r)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"]
    assert len(fails) == 1 and fails[0][0] == "operator trust third.party"
    assert f"framework extensions enable {pack} third.party --allow-untrusted" in fails[0][1]
    assert not fails[0][1].startswith("FAIL")

    assert cli.main(["enable", str(pack), "third.party", "--allow-untrusted"]) == 0
    r = v.Report()
    v._extension_checks().check_operator_trust_acceptance(pack, r)
    assert [s for s, _c, _d in r.rows] == ["OK"]


def test_framework_validate_is_silent_without_operator_in_gateway(tmp_path):
    v = _load("framework_validate", VALIDATE)
    pack = tmp_path / "pack"
    _pack_ext(pack, "demo.bundled")
    _hand_enable(pack, "demo.bundled")
    r = v.Report()
    v._extension_checks().check_operator_trust_acceptance(pack, r)
    assert r.rows == []


def test_framework_validate_reports_an_unreadable_trust_check_as_warn(tmp_path, monkeypatch):
    v = _load("framework_validate", VALIDATE)
    checks = v._extension_checks()
    broken = type("Broken", (), {
        "resolve_for_pack": staticmethod(lambda _p: (
            {"x": {"tier": "operator", "manifest": {"trust": "in-gateway"}}}, [])),
        "requires_trust_acceptance": staticmethod(lambda _r: True),
        "unaccepted_trust": staticmethod(lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))})
    checks.discovery = lambda: broken
    r = v.Report()
    checks.check_operator_trust_acceptance(tmp_path, r)
    assert r.rows and r.rows[0][0] == "WARN" and "boom" in r.rows[0][2]


@pytest.mark.parametrize("trust", ["declarative", "sidecar"])
def test_an_operator_extension_that_runs_no_gateway_code_needs_no_acceptance(trust):
    """Negative fixture on the rule: an operator-tier extension with no gateway operation (a
    schema/config-only declarative one, or a sidecar) runs nothing in the gateway. The LABEL is not
    what decides it (okengine#1004): a declarative extension that ships a script is covered by
    the tests below."""
    disc = _load("extension_discovery", DISC)
    assert disc.requires_trust_acceptance({"tier": "operator", "manifest": {"trust": trust}}) is False
    assert disc.requires_trust_acceptance(
        {"tier": "operator", "manifest": {"trust": "in-gateway"}}) is True
    assert disc.requires_trust_acceptance({"tier": "pack", "manifest": {"trust": "in-gateway"}}) is False
    assert disc.requires_trust_acceptance({"tier": "operator"}) is False


def test_allow_untrusted_records_nothing_for_an_extension_that_needs_no_acceptance(tmp_path):
    """The flag is harmless on a pack-tier extension: no acceptance record is invented for it."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _pack_ext(pack, "demo.bundled")
    assert cli.main(["enable", str(pack), "demo.bundled", "--allow-untrusted"]) == 0
    assert cli.main(["enable", str(pack), "demo.bundled", "--allow-untrusted"]) == 0   # already-enabled path
    assert disc.TRUST_ACCEPTED_KEY not in disc.load_enabled_state(pack)[0]["demo.bundled"]


def test_a_recorded_acceptance_is_not_rewritten_by_a_plain_re_enable(tmp_path):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party", {"trust_accepted": {"trust": "in-gateway",
                                                          "accepted_at": "2026-01-01T00:00:00Z"}})
    assert cli.main(["enable", str(pack), "third.party"]) == 0
    accepted = disc.load_enabled_state(pack)[0]["third.party"][disc.TRUST_ACCEPTED_KEY]
    assert accepted["accepted_at"] == "2026-01-01T00:00:00Z"


def test_framework_validate_runs_the_trust_check_on_its_enabled_extension_pass(tmp_path):
    """The wiring: `framework validate` reaches the trust check through the enabled-extension check
    it already runs at deploy step 1."""
    v = _load("framework_validate", VALIDATE)
    pack = tmp_path / "pack"
    _operator_ext(pack, "third.party")
    _hand_enable(pack, "third.party")
    r = v.Report()
    v.check_enabled_extensions_resolve(pack, r)
    assert ("FAIL", "operator trust third.party") in [(s, c) for s, c, _d in r.rows]
    assert ("FAIL", "operator trust third.party") in [(s, c) for s, c, _d in v.validate(pack).rows]


# ── okengine#1004: the trust label cannot lie about what runs ───────────────────────────────────
# `trust: declarative` means "no extension-owned code". An operator-tier extension labelled that way
# that ships a script used to pass enable, validate and the load-time re-check, then be staged into
# /opt/data/scripts and scheduled with write-path toolsets. Both sides are closed: the validator
# rejects a script under declarative (manifest block and crons/*.cron.json drop-in), and the
# acceptance gate keys on what would run, not on the label.
_SCHEDULE = {"kind": "cron", "expr": "@jitter:daily@4"}


def _declarative_manifest(ext_id, operation=None):
    m = _manifest(ext_id, "declarative")
    m.pop("operation")
    if operation is not None:
        m["operation"] = operation
    return m


def _write_ext(d, manifest, *, dropins=None, files=("run.py",)):
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    for name in files:
        (d / name).write_text("print('{}')\n", encoding="utf-8")
    for name, block in (dropins or {}).items():
        (d / "crons").mkdir(exist_ok=True)
        (d / "crons" / f"{name}.cron.json").write_text(json.dumps(block), encoding="utf-8")
    return d


def _declarative_script_ext(pack, ext_id, form):
    """An operator-tier `trust: declarative` extension that ships run.py as a script entrypoint,
    declared either in the manifest or (the issue's repro) as a crons/*.cron.json drop-in."""
    d = pack / ".okengine" / "extensions" / ext_id
    script_op = {"schedule": _SCHEDULE, "entrypoint": "run.py"}
    if form == "manifest":
        return _write_ext(d, _declarative_manifest(ext_id, script_op))
    return _write_ext(d, _declarative_manifest(ext_id), dropins={"x": script_op})


FORMS = ["manifest", "dropin"]


@pytest.mark.parametrize("form", FORMS)
def test_declarative_extension_with_a_script_is_neither_staged_nor_scheduled(tmp_path, form):
    pack = tmp_path / "pack"
    _declarative_script_ext(pack, "acme.decl", form)
    _hand_enable(pack, "acme.decl")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == [], f"a declarative extension with a script was staged: {staged}"
    assert jobs == [], f"a declarative extension with a script was scheduled: {jobs}"
    for errors in (stage_errors, job_errors):
        assert any("acme.decl" in e for e in errors), errors
        assert any("trust: declarative runs no code" in e for e in errors), errors


@pytest.mark.parametrize("form", FORMS)
def test_enable_refuses_a_declarative_extension_with_a_script(tmp_path, form, capsys):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _declarative_script_ext(pack, "acme.decl", form)
    assert cli.main(["enable", str(pack), "acme.decl"]) == 1
    err = capsys.readouterr().err
    assert "trust: declarative runs no code" in err
    assert "runs code in the gateway" in err
    assert "acme.decl" not in disc.load_enabled_state(pack)[0]


@pytest.mark.parametrize("form", FORMS)
def test_enable_allow_untrusted_cannot_launder_the_declarative_label(tmp_path, form):
    """Accepting the risk does not make the label true: the manifest is still invalid."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _declarative_script_ext(pack, "acme.decl", form)
    assert cli.main(["enable", str(pack), "acme.decl", "--allow-untrusted"]) == 1
    assert "acme.decl" not in disc.load_enabled_state(pack)[0]


@pytest.mark.parametrize("form", FORMS)
def test_framework_validate_fails_a_declarative_extension_with_a_script(tmp_path, form):
    v = _load("framework_validate", VALIDATE)
    pack = tmp_path / "pack"
    _declarative_script_ext(pack, "acme.decl", form)
    _hand_enable(pack, "acme.decl")
    r = v.Report()
    v._extension_checks().check_enabled_extensions_resolve(pack, r)
    fails = [(c, d) for s, c, d in r.rows if s == "FAIL"]
    assert any("trust: declarative runs no code" in d for _c, d in fails), r.rows
    assert ("operator trust acme.decl" in [c for c, _d in fails]), r.rows


@pytest.mark.parametrize("form", FORMS)
def test_extensions_validate_fails_a_declarative_extension_with_a_script(tmp_path, form, capsys):
    cli = _load("framework_extensions", CLI)
    pack = tmp_path / "pack"
    _declarative_script_ext(pack, "acme.decl", form)
    _hand_enable(pack, "acme.decl")
    assert cli.main(["validate", str(pack)]) == 1
    out = capsys.readouterr().out
    assert "trust: declarative runs no code" in out
    assert "acme.decl" in out and "--allow-untrusted" in out


@pytest.mark.parametrize("form", FORMS)
def test_acceptance_is_required_from_what_runs_not_from_the_label(tmp_path, form):
    """The load-side half stands on its own: even with the validator out of the picture (a stale
    copy, a future code path), an operator-tier declarative extension that would stage or schedule
    a script needs a recorded acceptance."""
    disc = _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    d = _declarative_script_ext(pack, "acme.decl", form)
    record = {"id": "acme.decl", "tier": "operator", "dir": str(d),
              "manifest": yaml.safe_load((d / "extension.yaml").read_text())}
    assert disc.runs_gateway_code(record) is True
    assert disc.requires_trust_acceptance(record) is True
    blocked = disc.unaccepted_trust(pack, {"acme.decl": record}, enabled={})
    assert "runs code in the gateway (`trust: declarative`)" in blocked["acme.decl"]


def test_a_prompt_operation_that_ships_python_needs_acceptance(tmp_path):
    """Staging copies the extension's top-level *.py for any gateway operation, so code reaches the
    gateway even when no entrypoint names it (an agent with a shell toolset can run it)."""
    disc = _load("extension_discovery", DISC)
    d = _write_ext(tmp_path / "acme.agent", _declarative_manifest(
        "acme.agent", {"schedule": _SCHEDULE, "prompt": "do work", "max_iterations": 3}),
        files=("helper.py",))
    record = {"id": "acme.agent", "tier": "operator", "dir": str(d),
              "manifest": yaml.safe_load((d / "extension.yaml").read_text())}
    assert disc.runs_gateway_code(record) is True
    assert disc.requires_trust_acceptance(record) is True
    (d / "helper.py").unlink()                     # negative: the same agent op with no code to stage
    assert disc.runs_gateway_code(record) is False
    assert disc.requires_trust_acceptance(record) is False


@pytest.mark.parametrize("manifest_extra", [
    {},                                                                   # no operation at all
    {"operation": {"schedule": _SCHEDULE,                                 # an image op
                   "entrypoint": {"image": {"registry": "r/x", "digest": "sha256:a"}}}},
    {"kind": "validator", "operation": {"schedule": _SCHEDULE, "entrypoint": "run.py"}},
])
def test_runs_gateway_code_negatives(tmp_path, manifest_extra):
    """Negative fixtures: no operation, an image-only operation, or a kind that synthesizes no job
    puts nothing into the gateway, even with *.py on disk."""
    disc = _load("extension_discovery", DISC)
    m = {**_declarative_manifest("acme.none"), **manifest_extra}
    d = _write_ext(tmp_path / "acme.none", m)
    record = {"id": "acme.none", "tier": "operator", "dir": str(d), "manifest": m}
    assert disc.runs_gateway_code(record) is False


def test_runs_gateway_code_reads_plural_operations_and_dict_scripts(tmp_path):
    disc = _load("extension_discovery", DISC)
    m = {**_declarative_manifest("acme.multi"),
         "operations": {"a": {"schedule": _SCHEDULE, "prompt": "p", "max_iterations": 1},
                        "b": {"schedule": _SCHEDULE, "entrypoint": {"script": "run.py"}},
                        "c": "not-a-block"}}
    record = {"id": "acme.multi", "tier": "operator", "dir": None, "manifest": m}
    assert disc.runs_gateway_code(record) is True


def test_truly_declarative_operator_extension_enables_end_to_end_without_acceptance(tmp_path):
    """Negative fixture: a declarative operator extension with NO operation (schema/config only)
    is valid, needs no acceptance, enables without a flag, and contributes nothing to staging or
    the cron fleet -- and no error either."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    em = _load("extension_manifest", REPO / "scripts" / "extension_manifest.py")
    pack = tmp_path / "pack"
    m = _declarative_manifest("acme.pure")
    d = _write_ext(pack / ".okengine" / "extensions" / "acme.pure", m, files=())
    record = {"id": "acme.pure", "tier": "operator", "dir": str(d), "manifest": m}
    assert em.validate_manifest(m)[0] == [] and em.validate_dropins(m, d) == []
    assert disc.requires_trust_acceptance(record) is False
    assert cli.main(["enable", str(pack), "acme.pure"]) == 0
    assert disc.TRUST_ACCEPTED_KEY not in disc.load_enabled_state(pack)[0]["acme.pure"]
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == [] and jobs == []
    assert not [e for e in stage_errors + job_errors if "acme.pure" in e], stage_errors + job_errors


# QA B1 (okengine#1004 round 2): a PROMPT-only declarative operation is an agent lane in the gateway;
# with `terminal` (or `file`, or a composite) it runs shell and rewrites /opt/data/scripts. Strict
# rule: declarative declares no operation at all, prompt or script.
PROMPT_TOOLSETS = [["terminal"], ["file", "okengine"], None]
_VALID_OUTPUT_CONTRACT = json.loads(
    (REPO / "extensions" / "okengine.frontier-watch" / "crons" / "frontier-brief.cron.json")
    .read_text(encoding="utf-8"))["output_contract"]


def _declarative_prompt_ext(pack, ext_id, form, toolsets):
    d = pack / ".okengine" / "extensions" / ext_id
    # a COMPLETE agent op (executable output_contract + adversarial fixtures) so nothing but the
    # trust rule can refuse it -- the shape QA's B1 probe enabled with no acceptance
    op = {"schedule": _SCHEDULE, "prompt": "do work", "max_iterations": 3,
          "output_contract": _VALID_OUTPUT_CONTRACT, "adversarial_fixtures": ["tests/x.py"]}
    if toolsets is not None:
        op["toolsets"] = toolsets
    if form == "manifest":
        return _write_ext(d, _declarative_manifest(ext_id, op), files=())
    return _write_ext(d, _declarative_manifest(ext_id), dropins={"x": op}, files=())


@pytest.mark.parametrize("toolsets", PROMPT_TOOLSETS)
@pytest.mark.parametrize("form", FORMS)
def test_declarative_prompt_operation_is_neither_staged_nor_scheduled(tmp_path, form, toolsets):
    pack = tmp_path / "pack"
    _declarative_prompt_ext(pack, "acme.decl", form, toolsets)
    _hand_enable(pack, "acme.decl")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == [] and jobs == [], (staged, jobs)
    for errors in (stage_errors, job_errors):
        assert any("acme.decl" in e and "trust: declarative runs no code" in e for e in errors), errors


@pytest.mark.parametrize("flag", [[], ["--allow-untrusted"]])
@pytest.mark.parametrize("toolsets", PROMPT_TOOLSETS)
@pytest.mark.parametrize("form", FORMS)
def test_enable_refuses_a_declarative_prompt_operation(tmp_path, form, toolsets, flag, capsys):
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    _declarative_prompt_ext(pack, "acme.decl", form, toolsets)
    assert cli.main(["enable", str(pack), "acme.decl", *flag]) == 1
    assert "trust: declarative runs no code" in capsys.readouterr().err
    assert "acme.decl" not in disc.load_enabled_state(pack)[0]


@pytest.mark.parametrize("toolsets", PROMPT_TOOLSETS)
@pytest.mark.parametrize("form", FORMS)
def test_validators_fail_a_declarative_prompt_operation(tmp_path, form, toolsets, capsys):
    v, cli = _load("framework_validate", VALIDATE), _load("framework_extensions", CLI)
    pack = tmp_path / "pack"
    _declarative_prompt_ext(pack, "acme.decl", form, toolsets)
    _hand_enable(pack, "acme.decl")
    r = v.Report()
    v._extension_checks().check_enabled_extensions_resolve(pack, r)
    assert any(s == "FAIL" and "trust: declarative runs no code" in d for s, _c, d in r.rows), r.rows
    assert cli.main(["validate", str(pack)]) == 1
    assert "trust: declarative runs no code" in capsys.readouterr().out


def test_declarative_prompt_operation_message_names_the_block():
    em = _load("extension_manifest", REPO / "scripts" / "extension_manifest.py")
    m = _declarative_manifest("acme.decl")
    m["operations"] = {"grade": {"schedule": _SCHEDULE, "prompt": "p", "max_iterations": 1}}
    assert em.validate_manifest(m)[0] == [
        "trust: declarative runs no code, but operations.grade declares an operation (an agent or "
        "script lane that runs in the gateway); declare trust: in-gateway (operator tier requires "
        "--allow-untrusted) or trust: sidecar"]
    assert em.declarative_script_error is em.declarative_operation_error
    assert em.declarative_operation_error("not-a-block", "declarative", "operation") is None


@pytest.mark.parametrize("toolsets,code", [
    (["terminal"], True), (["file", "okengine"], True), (["code_execution"], True),
    (["hermes-cron", "okengine-write", "okengine"], True), ("terminal", True), ([3], True),
    (None, False), (["okengine", "okengine-write", "okengine-write-acme-x", "web", "todo", "no_mcp",
                    "file_read", "file_read_exact"], False),
])
def test_runs_gateway_code_counts_code_capable_toolsets(tmp_path, toolsets, code):
    """Defense in depth behind the validator: a mislabelled agent operation that lists a toolset
    outside the no-code allowlist runs code in the gateway, so it needs acceptance."""
    disc = _load("extension_discovery", DISC)
    op = {"schedule": _SCHEDULE, "prompt": "p", "max_iterations": 1}
    if toolsets is not None:
        op["toolsets"] = toolsets
    m = _declarative_manifest("acme.agent", op)
    record = {"id": "acme.agent", "tier": "operator", "dir": str(tmp_path), "manifest": m}
    assert disc.runs_gateway_code(record) is code
    assert disc.requires_trust_acceptance(record) is code


def test_composer_rejects_a_declarative_manifest_prompt_operation_on_its_own(tmp_path):
    comp = _load("extension_compose", COMP)
    m = _declarative_manifest("acme.decl", {"schedule": _SCHEDULE, "prompt": "p",
                                            "max_iterations": 1, "toolsets": ["terminal"]})
    jobs, errors, _ = comp.synthesize_ops({"id": "acme.decl", "tier": "operator",
                                          "dir": str(tmp_path), "manifest": m})
    assert jobs == [] and len(errors) == 1 and "operation declares an operation" in errors[0], errors


def test_an_empty_acceptance_does_not_match_a_manifest_without_trust():
    disc = _load("extension_discovery", DISC)
    record = {"tier": "operator", "manifest": {"kind": "operation"}}
    assert disc.is_trust_accepted({disc.TRUST_ACCEPTED_KEY: {}}, record) is False
    assert disc.is_trust_accepted({disc.TRUST_ACCEPTED_KEY: {"trust": None}}, record) is False
    accepted = {disc.TRUST_ACCEPTED_KEY: {"trust": "in-gateway"}}
    assert disc.is_trust_accepted(accepted, {"manifest": {"trust": "in-gateway"}}) is True


def test_sidecar_and_in_gateway_scripts_still_validate(tmp_path):
    """Negative fixtures for the new validator rule: it fires only under `trust: declarative`."""
    em = _load("extension_manifest", REPO / "scripts" / "extension_manifest.py")
    for trust in ("in-gateway", "sidecar"):
        assert em.validate_manifest(_manifest("acme.ok", trust))[0] == []
    d = _write_ext(tmp_path / "acme.ok", _manifest("acme.ok", "in-gateway"),
                   dropins={"x": {"schedule": _SCHEDULE, "entrypoint": {"script": "run.py"}}})
    assert em.validate_dropins(_manifest("acme.ok", "in-gateway"), d) == []


def test_declarative_validator_names_the_script_for_each_entrypoint_shape():
    em = _load("extension_manifest", REPO / "scripts" / "extension_manifest.py")
    for ep in ("run.py", {"script": "run.py"}):
        m = _declarative_manifest("acme.decl", {"schedule": _SCHEDULE, "entrypoint": ep})
        errors, _ = em.validate_manifest(m)
        assert errors == ["trust: declarative runs no code, but operation.entrypoint names the "
                          "script 'run.py'; declare trust: in-gateway (operator tier requires "
                          "--allow-untrusted) or trust: sidecar"], errors
    assert em.script_entrypoint({"entrypoint": "  "}) is None
    assert em.script_entrypoint({"entrypoint": {"script": 3}}) is None
    assert em.script_entrypoint("not-a-block") is None
    assert em.script_entrypoint({"entrypoint": {"image": {"digest": "d"}, "script": "run.py"}}) is None
    assert em.script_entrypoint({"entrypoint": {"script": "run.py"}}) == "run.py"
    # only the exact `declarative` label is held to "no code"; other labels (even ones sorting
    # before it) are judged by their own rules
    for other in ("builtin", "custom", "in-gateway", None):
        assert em.declarative_script_error({"entrypoint": "run.py"}, other, "operation") is None


@pytest.mark.parametrize("name,body,expected", [
    (".cron.json", "{}", "invalid cron drop-in filename"),
    ("x.cron.json", "{not json", "invalid JSON"),
    ("x.cron.json", "[]", "must be a JSON object"),
])
def test_dropin_reader_errors(tmp_path, name, body, expected):
    """The shared drop-in reader keeps the composer's error contract."""
    em = _load("extension_manifest", REPO / "scripts" / "extension_manifest.py")
    (tmp_path / "crons").mkdir()
    (tmp_path / "crons" / name).write_text(body, encoding="utf-8")
    ops, err = em.load_dropin_ops(tmp_path)
    assert ops == [] and expected in err
    # in-gateway: the composer reports the unreadable drop-in, not the trust check
    assert em.validate_dropins(_manifest("acme.x", "in-gateway"), tmp_path) == []
    # declarative: shipping a drop-in at all is the violation, readable or not
    [error] = em.validate_dropins(_declarative_manifest("acme.x"), tmp_path)
    assert "trust: declarative runs no code, but it ships crons/ drop-in(s)" in error and expected in error
    assert em.load_dropin_ops(None) == ([], None)
    assert em.load_dropin_ops(tmp_path / "missing") == ([], None)


def test_trust_and_tier_compare_by_value_not_identity(tmp_path):
    """Manifest and record values come from YAML/JSON parsing, so they are equal to, but not the same
    object as, the literals the predicate compares against. Built at runtime here to prove it."""
    disc = _load("extension_discovery", DISC)
    sidecar, operator = "".join(["side", "car"]), "".join(["oper", "ator"])
    m = {**_declarative_manifest("acme.side"), "trust": sidecar,
         "operation": {"schedule": _SCHEDULE, "entrypoint": "run.py"}}
    d = _write_ext(tmp_path / "acme.side", m)
    assert disc.runs_gateway_code({"tier": operator, "dir": str(d), "manifest": m}) is False
    script = {**_declarative_manifest("acme.decl"),
              "operation": {"schedule": _SCHEDULE, "entrypoint": "run.py"}}
    assert disc.requires_trust_acceptance({"tier": operator, "dir": str(d), "manifest": script}) is True


def test_composer_drop_in_path_rejects_a_declarative_script_on_its_own(tmp_path):
    """The composer's drop-in reader enforces the label itself, so a caller that synthesizes jobs
    from a record without going through discovery's validation still gets no job."""
    comp = _load("extension_compose", COMP)
    m = _declarative_manifest("acme.decl")
    d = _write_ext(tmp_path / "acme.decl", m,
                   dropins={"x": {"schedule": _SCHEDULE, "entrypoint": "run.py"}})
    jobs, errors, _ = comp.synthesize_ops({"id": "acme.decl", "tier": "operator", "dir": str(d),
                                          "manifest": m})
    assert jobs == []
    assert errors == ["acme.decl: trust: declarative runs no code, but crons/x.cron.json.entrypoint "
                      "names the script 'run.py'; declare trust: in-gateway (operator tier requires "
                      "--allow-untrusted) or trust: sidecar"], errors


def test_an_unreadable_drop_in_cannot_mask_a_declarative_script_drop_in(tmp_path, capsys):
    """QA probe P6c: the drop-in reader stops at the first bad file, which hid a readable script
    drop-in beside it -- the declarative extension then enabled with no error. Under declarative any
    drop-in is the violation, and the load-side predicate assumes code when it cannot read them."""
    cli, disc = _load("framework_extensions", CLI), _load("extension_discovery", DISC)
    pack = tmp_path / "pack"
    m = _declarative_manifest("acme.f3")
    d = _write_ext(pack / ".okengine" / "extensions" / "acme.f3", m,
                   dropins={"a": {"schedule": _SCHEDULE, "entrypoint": "run.py"}})
    (d / "crons" / "z.cron.json").write_text("{bad", encoding="utf-8")
    assert cli.main(["enable", str(pack), "acme.f3"]) == 1
    assert "ships crons/ drop-in(s)" in capsys.readouterr().err
    record = {"id": "acme.f3", "tier": "operator", "dir": str(d), "manifest": m}
    assert disc.runs_gateway_code(record) is True
    _hand_enable(pack, "acme.f3")
    staged, stage_errors, jobs, job_errors = _staging_and_jobs(pack)
    assert staged == [] and jobs == []
    assert any("acme.f3" in e and "invalid JSON" in e for e in job_errors), job_errors


@pytest.mark.parametrize("trust", ["builtin", "custom", "in-gateway"])
def test_declarative_only_rules_match_the_exact_label(tmp_path, trust):
    """Only `trust: declarative` gets the no-operation shortcut and the unreadable-drop-in
    violation; any other label (even one sorting before it) keeps the ordinary contract."""
    em = _load("extension_manifest", REPO / "scripts" / "extension_manifest.py")
    comp = _load("extension_compose", COMP)
    (tmp_path / "crons").mkdir()
    (tmp_path / "crons" / "x.cron.json").write_text("{bad", encoding="utf-8")
    m = {**_declarative_manifest("acme.x"), "trust": trust}
    assert em.validate_dropins(m, tmp_path) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    jobs, errors, _ = comp.synthesize_ops({"id": "acme.x", "tier": "operator", "dir": str(empty),
                                          "manifest": m})
    assert jobs == [] and any("requires an 'operation'" in e for e in errors), errors
