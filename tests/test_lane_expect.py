"""okengine#890/#893 — every lane declares `expect:` (what "working" means), enforced at validate.

Why: on one live vault lacuna skipped 78 of 81 runs and trends skipped every week for two months
while the fleet-health dashboard judged them by whether they RAN. `output_contract` says what a
lane may write; `expect:` says what it should produce, so a lane that stops producing can be told
apart from one that works (#892 evaluates it).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST = REPO / "config" / "expect-allowlist.yaml"
DOC = REPO / "docs" / "lane-expectations.md"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


LE = _load("lane_expect_under_test", REPO / "scripts" / "cron" / "lane_expect.py")
sys.path.insert(0, str(REPO))
from scripts import framework_validate_expect as FVE  # noqa: E402

CONTRACT = {"allowed_namespaces": ["trends", "dashboards"]}
# Internal files scripts/publish-snapshot.sh excludes from the public tree; the public suite runs
# without them, so a documentation check may only require them where they exist.
INTERNAL_ONLY = {"CLAUDE.md"}


# --- the schema ------------------------------------------------------------------------------

def test_every_shape_validates_and_shapes_combine():
    expect = {"produces": {"namespace": "trends", "at_least": 1, "per": "4w"},
              "drains": {}, "maintains": {"artifact": "wiki/dashboards/x.md", "max_age": "3x"},
              "may_idle": {"max_idle_runs": 8}}
    assert LE.validate(expect, "lane", CONTRACT) == []
    assert LE.validate({"produces": {"type": "trend", "at_least": 2, "per": "12h"}}, "l") == []
    assert LE.validate({"drains": {"max_stalled_runs": 3}}, "l") == []


@pytest.mark.parametrize("expect, fragment", [
    ({}, "non-empty mapping"),
    (["produces"], "non-empty mapping"),
    ({"make_trends": {}}, "unknown shape 'make_trends'"),
    ({"drains": {"backlog": 1}}, "unknown key 'backlog'"),
    ({"drains": "yes"}, "must be a mapping"),
    ({"produces": {"namespace": "trends", "per": "4w"}}, "at_least is required"),
    ({"produces": {"namespace": "trends", "at_least": 0, "per": "4w"}}, "integer >= 1"),
    ({"produces": {"namespace": "trends", "at_least": True, "per": "4w"}}, "integer >= 1"),
    ({"produces": {"namespace": "trends", "at_least": 1, "per": "monthly"}}, "window like"),
    ({"produces": {"namespace": "Trends!", "at_least": 1, "per": "4w"}}, "namespace/type name"),
    ({"produces": {"at_least": 1, "per": "4w"}}, "needs a namespace or a type"),
    ({"maintains": {"artifact": "/abs/path", "max_age": "3x"}}, "vault-relative path"),
    ({"maintains": {"max_age": "3x"}}, "artifact is required"),
    ({"may_idle": {"max_idle_runs": "8"}}, "integer >= 1"),
])
def test_malformed_expectations_fail_with_a_named_reason(expect, fragment):
    errors = LE.validate(expect, "lane", CONTRACT)
    assert any(fragment in e for e in errors), errors


def test_a_produces_namespace_outside_the_contract_can_never_be_met():
    """NEGATIVE: the #1153 class one level up — an expectation the lane cannot legally meet."""
    errors = LE.validate({"produces": {"namespace": "briefings", "at_least": 1, "per": "1w"}},
                         "lane", CONTRACT)
    assert errors and "outside the lane's output_contract" in errors[0]
    ok = {"produces": {"namespace": "briefings", "at_least": 1, "per": "1w"}}
    assert LE.validate(ok, "lane", {"allowed_namespaces": ["*"]}) == []
    assert LE.validate(ok, "lane", None) == []


# --- the teaching failure message ------------------------------------------------------------

def test_the_missing_message_names_the_lane_says_why_links_the_doc_and_prints_the_fix():
    model = {"output_contract": {"allowed_namespaces": ["trends"]}}
    msg = LE.missing_message("trends-refresh", "config/engine-crons.json", model)
    assert "'trends-refresh'" in msg and "config/engine-crons.json" in msg
    assert "stop working unnoticed" in msg and "docs/lane-expectations.md" in msg
    example = json.loads("{" + msg.split("e.g. ", 1)[1] + "}")["expect"]
    assert example["produces"] == {"namespace": "trends", "at_least": 1, "per": "4x"}
    assert LE.validate(example, "copied", model["output_contract"]) == [], "the fix must be valid"


def test_a_script_lane_is_shown_a_maintains_example():
    example = json.loads("{" + LE.example({"no_agent": True}) + "}")["expect"]
    assert set(example) == {"maintains"}
    assert LE.validate(example, "copied") == []


# --- allowlist + per-lane findings -----------------------------------------------------------

def test_allowlist_loading_reports_every_defect(tmp_path):
    good = tmp_path / "a.yaml"
    good.write_text("max_entries: 1\nlanes:\n  x: reason\n")
    assert LE.load_allowlist(good) == ({"x": "reason"}, 1, [])
    for body, fragment in (("lanes: [x]\n", "must be a mapping"),
                           ("max_entries: 1\nlanes:\n  x: ''\n", "needs a non-empty reason"),
                           ("lanes:\n  x: why\n", "max_entries"),
                           ("lanes: {x: [\n", "unreadable")):
        good.write_text(body)
        assert any(fragment in e for e in LE.load_allowlist(good)[2]), body
    assert "unreadable" in LE.load_allowlist(tmp_path / "missing.yaml")[2][0]


def test_lane_findings():
    assert LE.lane_findings("x", "s", {"enabled": False}, {}) == ([], False)
    assert LE.lane_findings("x", "s", "not a lane", {}) == ([], False)
    assert LE.lane_findings("x", "s", {}, {"x": "why"}) == ([], True)
    errors, _ = LE.lane_findings("x", "s", {}, {})
    assert "declares no `expect:`" in errors[0]
    assert LE.lane_findings("x", "s", {"expect": {"drains": {}}}, {}) == ([], False)
    assert LE.lane_findings("x", "s", {"expect": {"nope": {}}}, {})[0]


# --- the engine gate (this repo's own lanes) -------------------------------------------------

def test_the_engine_s_own_lanes_pass_the_gate():
    errors, undeclared = FVE.check_engine()
    assert errors == []
    assert undeclared == len(LE.load_allowlist(ALLOWLIST)[0])


def _fixture_engine(root: Path, crons: list, ext_ops: dict | None = None, allow: dict | None = None,
                    cap: int | None = None) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "engine-crons.json").write_text(json.dumps(crons))
    allow = allow or {}
    (root / "config" / "expect-allowlist.yaml").write_text(yaml.safe_dump(
        {"max_entries": len(allow) if cap is None else cap, "lanes": allow}))
    ext = root / "extensions" / "okengine.demo"
    ext.mkdir(parents=True)
    (ext / "extension.yaml").write_text(yaml.safe_dump(
        {"id": "okengine.demo", "kind": "operation", "operations": ext_ops or {}}))
    (root / "extensions" / "okengine.theme").mkdir()
    (root / "extensions" / "okengine.theme" / "extension.yaml").write_text("id: t\nkind: theme\n")
    (root / "extensions" / "okengine.broken").mkdir()
    (root / "extensions" / "okengine.broken" / "extension.yaml").write_text("id: [\n")
    return root


OP = {"schedule": {"kind": "cron", "expr": "17 * * * *"}, "entrypoint": "x.py"}


def test_a_new_lane_without_expect_fails_and_an_allowlisted_one_passes(tmp_path):
    root = _fixture_engine(tmp_path, [{"name": "old-lane"}, {"name": "new-lane"}],
                           {"op": dict(OP)}, allow={"old-lane": "pre-#890"})
    errors, undeclared = FVE.check_engine(root)
    assert undeclared == 1
    assert [e for e in errors if "'new-lane'" in e and "declares no" in e]
    assert [e for e in errors if "'okengine.demo:op'" in e and "declares no" in e]
    assert not [e for e in errors if "'old-lane'" in e]


def test_the_allowlist_only_shrinks(tmp_path):
    """Entries for lanes that now declare expect:, or no longer exist, must be deleted; the cap
    must equal the count."""
    root = _fixture_engine(tmp_path, [{"name": "fixed", "expect": {"drains": {}}}],
                           allow={"fixed": "why", "gone": "why"}, cap=5)
    errors, _ = FVE.check_engine(root)
    assert any("'fixed' now declares expect:" in e for e in errors)
    assert any("'gone' is not a lane any more" in e for e in errors)
    assert any("max_entries is 5 but 2" in e for e in errors)


def test_a_malformed_extension_expect_fails_even_when_allowlisted(tmp_path):
    root = _fixture_engine(tmp_path, [], {"op": {**OP, "expect": {"sometimes": {}}}},
                           allow={"okengine.demo:op": "why"})
    errors, _ = FVE.check_engine(root)
    assert any("unknown shape 'sometimes'" in e for e in errors)


# --- the ratchet against the merge base ------------------------------------------------------

def _base_allowlist() -> dict | None:
    base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or "origin/main"
    shown = subprocess.run(["git", "show", f"{base}:config/expect-allowlist.yaml"], cwd=REPO,
                           capture_output=True, text=True)
    return yaml.safe_load(shown.stdout) if shown.returncode == 0 else None


def ratchet_errors(current: dict, base: dict | None) -> list[str]:
    errors = []
    lanes = current.get("lanes") or {}
    if current.get("max_entries") != len(lanes):
        errors.append(f"max_entries {current.get('max_entries')} != {len(lanes)} entries")
    if base:
        grown = sorted(set(lanes) - set(base.get("lanes") or {}))
        if grown:
            errors.append(f"allowlist grew: {grown} — declare expect: instead")
        if (current.get("max_entries") or 0) > (base.get("max_entries") or 0):
            errors.append("max_entries rose above the merge base")
    return errors


def test_the_allowlist_never_grows_relative_to_the_merge_base():
    """The ratchet. Without a readable base (first introduction, shallow clone) the count pin
    still holds — the base comparison is additive, never a silent pass of the pin."""
    assert ratchet_errors(yaml.safe_load(ALLOWLIST.read_text()), _base_allowlist()) == []


def test_the_ratchet_rejects_growth():
    base = {"max_entries": 1, "lanes": {"a": "why"}}
    assert ratchet_errors({"max_entries": 1, "lanes": {"a": "why"}}, base) == []
    grown = ratchet_errors({"max_entries": 2, "lanes": {"a": "why", "b": "why"}}, base)
    assert any("grew: ['b']" in e for e in grown) and any("rose" in e for e in grown)
    assert ratchet_errors({"max_entries": 3, "lanes": {"a": "why"}}, None)


# --- framework validate <pack> ---------------------------------------------------------------

class _Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def ok(self, c, d=""):
        self.rows.append(("OK", c, d))

    def warn(self, c, d=""):
        self.rows.append(("WARN", c, d))

    def fail(self, c, d=""):
        self.rows.append(("FAIL", c, d))


def test_pack_lanes_warn_when_undeclared_and_fail_when_malformed(tmp_path):
    pack = tmp_path / "pack"
    ext = pack / "extensions" / "acme.ops"
    ext.mkdir(parents=True)
    (ext / "extension.yaml").write_text(yaml.safe_dump(
        {"id": "acme.ops", "kind": "operation", "operations": {"sweep": dict(OP)}}))
    r = _Report()
    FVE.check_pack(pack, r, [{"name": "pack-lane"}, {"name": "bad", "expect": {"x": {}}}, "junk"])
    rows = {(s, c) for s, c, _d in r.rows}
    assert ("OK", "lane expectations (engine)") in rows
    assert ("FAIL", "lane expectations (pack)") in rows
    warn = [d for s, c, d in r.rows if s == "WARN"][0]
    assert "pack-lane" in warn and "acme.ops:sweep" in warn and "future release" in warn


def test_declared_pack_lanes_pass_and_engine_errors_fail_the_pack(tmp_path, monkeypatch):
    r = _Report()
    FVE.check_pack(tmp_path, r, [{"name": "p", "expect": {"drains": {}}}])
    assert ("OK", "lane expectations (pack)", "1 pack lane(s) declare expect:") in r.rows
    monkeypatch.setattr(FVE, "_ENGINE_RESULT", None)
    monkeypatch.setattr(FVE, "check_engine", lambda: (["broken engine lane"], 0))
    r = _Report()
    FVE.check_pack(tmp_path, r, [])
    assert ("FAIL", "lane expectations (engine)", "broken engine lane") in r.rows


def test_the_scaffolded_pack_declares_its_lanes():
    """framework init's crons ship WITH expect: — the template cannot teach the stale pattern."""
    crons = json.loads((REPO / "templates/pack/skeleton/crons/domain-crons.json").read_text())
    r = _Report()
    FVE.check_pack(REPO / "templates" / "pack" / "skeleton", r, crons)
    assert not [row for row in r.rows if row[0] in ("WARN", "FAIL")], r.rows


# --- carried to the deployed job -------------------------------------------------------------

def test_extension_composition_carries_a_valid_expect_and_rejects_a_bad_one(tmp_path):
    ec = _load("extension_compose_expect", REPO / "scripts" / "extension_compose.py")
    manifest = {"id": "okengine.demo", "kind": "operation", "trust": "in-gateway",
                "operations": {"op": {**OP, "expect": {"maintains": {
                    "artifact": "wiki/dashboards/demo.md", "max_age": "3x"}}}}}
    jobs, errors, _ = ec.synthesize_ops({"id": "okengine.demo", "manifest": manifest,
                                         "dir": str(tmp_path)})
    assert errors == [] and jobs[0]["expect"]["maintains"]["max_age"] == "3x"
    manifest["operations"]["op"]["expect"] = {"sometimes": {}}
    jobs, errors, _ = ec.synthesize_ops({"id": "okengine.demo", "manifest": manifest,
                                         "dir": str(tmp_path)})
    assert jobs == [] and any("unknown shape" in e for e in errors)


def test_cron_pack_split_writes_expect_into_the_deployed_jobs():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    split = _load("cron_pack_split_expect", REPO / "scripts" / "cron_pack_split.py")
    job = {"name": "p", "schedule": {"kind": "cron", "expr": "0 * * * *"},
           "no_agent": True, "expect": {"drains": {}}}
    assert json.loads(split._dump_jobs([job]))["jobs"][0]["expect"] == {"drains": {}}


# --- the docs cannot fall behind the schema --------------------------------------------------

def test_every_shape_and_key_is_documented():
    doc = DOC.read_text(encoding="utf-8")
    for shape, keys in LE.SHAPES.items():
        assert f"`{shape}`" in doc, shape
        for key in keys:
            assert f"`{key}`" in doc, f"{shape}.{key}"
    for page in ("docs/authoring-a-pack.md", "docs/authoring-an-extension.md",
                 "docs/technical-reference.md", "docs/observability.md",
                 "templates/pack/BUILDING-A-PACK.md", "CLAUDE.md"):
        path = REPO / page
        if page in INTERNAL_ONLY and not path.is_file():
            continue  # publish-EXCLUDED: absent from the public snapshot's scrubbed tree
        text = path.read_text(encoding="utf-8")
        assert "expect:" in text and "lane-expectations.md" in text, page


def test_a_pack_whose_only_lane_is_malformed_gets_no_ok_row(tmp_path):
    r = _Report()
    FVE.check_pack(tmp_path, r, [{"name": "bad", "expect": {"x": {}}}])
    pack_rows = [s for s, c, _d in r.rows if c == "lane expectations (pack)"]
    assert pack_rows == ["FAIL"]


def test_framework_validate_checks_expectations_even_without_a_crons_dir(tmp_path):
    """A pack with no crons/ still gets the engine-lane gate (the early-return path)."""
    init = _load("framework_init_expect", REPO / "scripts" / "framework_init.py")
    pack = tmp_path / "pack"
    assert init.main([str(pack), "--domain", "Test Domain", "--no-compose"]) == 0
    import shutil
    shutil.rmtree(pack / "crons")
    v = _load("framework_validate_expect_entry", REPO / "scripts" / "framework_validate.py")
    rows = {(s, c) for s, c, _d in v.validate(pack).rows}
    assert ("OK", "lane expectations (engine)") in rows
    assert ("OK", "lane expectations (pack)") in rows


def test_the_shared_validator_loads_when_scripts_cron_is_not_yet_on_the_path(monkeypatch):
    """`framework validate` runs from a fresh interpreter where scripts/cron is not importable yet;
    the loader must put it on the path itself rather than rely on a caller having done so."""
    cron = str(REPO / "scripts" / "cron")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != cron])
    monkeypatch.delitem(sys.modules, "lane_expect", raising=False)
    module = FVE.lane_expect()
    assert sys.path[0] == cron, "the loader must add scripts/cron to the import path"
    assert callable(module.validate) and callable(module.lane_findings)
