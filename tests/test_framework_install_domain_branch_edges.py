"""Defensive and alternate-shape coverage for framework_install_domain."""
from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/framework_install_domain.py"


def _mod(name="framework_install_domain_edges"):
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _base(tmp_path):
    host, pack = tmp_path / "host", tmp_path / "pack"
    (host / "wiki").mkdir(parents=True)
    (pack / "subdomain").mkdir(parents=True)
    (host / "schema.yaml").write_text("types: {}\n")
    (pack / "schema.yaml").write_text("types: {}\n")
    (pack / "pack.yaml").write_text("name: pack\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text("types: {}\n")
    return host, pack


def test_type_rule_coverage_and_enum_warning_edges(tmp_path):
    m = _mod("install_domain_type_edges")
    host, pack = _base(tmp_path)
    (host / "schema.yaml").write_text(
        "types:\n  same: {required: [type]}\n"
        "coverage_fields: [{type: same, field: one}]\n"
        "enums: {status: [open]}\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(
        "types:\n  same: {required: [type, id]}\n"
        "coverage_fields: [{type: new, field: two}]\n"
        "enums: {extra: [x]}\n")
    plan = m.Plan(True)
    assert m.merge_types(host, pack, plan) == {"same"}
    m.merge_coverage_fields(host, pack, plan)
    m.merge_enums(host, pack, plan)
    assert any("different required fields" in x for x in plan.warns)
    assert any("coverage_fields inline" in x for x in plan.warns)
    assert any("enums inline" in x for x in plan.warns)

    cfg = pack / "config"; cfg.mkdir()
    (cfg / "completeness-rules.yaml").write_text(
        "rules:\n- {id: skip, when: {type: foreign}}\n")
    plan = m.Plan(False); m.merge_rules(host, pack, set(), plan)
    assert plan.steps == [] and plan.warns


def test_list_contract_append_and_existing_value_paths(tmp_path):
    m = _mod("install_domain_list_edges")
    host, pack = _base(tmp_path)
    (pack / "subdomain/host-schema-additions.yaml").write_text(
        "operational_types: [one]\ndepth_critical_types: [two]\n")
    (host / "schema.yaml").write_text("types: {}\noperational_types: [host]\n")
    plan = m.Plan(True)
    m.merge_list_contracts(host, pack, plan)
    assert plan.run() == 0
    data = yaml.safe_load((host / "schema.yaml").read_text())
    assert data["operational_types"] == ["host"]
    assert data["depth_critical_types"] == ["two"]


@pytest.mark.parametrize("host_schema", [
    "types: {}\npartitioning:\n  namespaces:\npermissions:\n  namespaces:\ntier:\n  namespaces:\ntail: true\n",
    "types: {}\npartitioning: {}\npermissions: {}\ntier: {}\n",
    "types: {}\n",
])
def test_namespace_merge_layout_variants(tmp_path, host_schema):
    m = _mod("install_domain_namespace_" + str(abs(hash(host_schema))))
    host, pack = _base(tmp_path)
    (host / "schema.yaml").write_text(host_schema)
    (pack / "pack.yaml").write_text(
        "name: pack\nowns:\n  namespaces: [items]\n")
    (pack / "schema.yaml").write_text(
        "partitioning: {namespaces: {items: {strategy: by-letter}}}\n"
        "permissions: {namespaces: {items: {write: agent}}}\n"
        "tier: {namespaces: {items: warm}}\n")
    plan = m.Plan(True); m.merge_namespaces(host, pack, plan)
    assert plan.run() == 0
    assert (host / "wiki/items").is_dir()


def test_namespace_host_wins_and_no_owned_namespace(tmp_path):
    m = _mod("install_domain_namespace_noops")
    host, pack = _base(tmp_path)
    plan = m.Plan(False); m.merge_namespaces(host, pack, plan)
    assert plan.steps == []
    (pack / "pack.yaml").write_text("name: pack\nowns: {namespaces: [items]}\n")
    (host / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {items: {strategy: flat}}}\n")
    plan = m.Plan(False); m.merge_namespaces(host, pack, plan)
    assert plan.steps == [] and plan.fails
    assert "does not prove identity" in plan.fails[0]


def test_namespace_legacy_marker_read_error_and_owned_contract_drift(tmp_path, monkeypatch):
    m = _mod("install_domain_namespace_owner_edges")
    host, pack = _base(tmp_path)
    (pack / "pack.yaml").write_text("name: pack\nowns: {namespaces: [items]}\n")
    (pack / "schema.yaml").write_text(
        "partitioning: {namespaces: {items: {strategy: flat}}}\n"
        "permissions: {namespaces: {items: {write: agent}}}\n")
    (host / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {items: {strategy: flat}}}\n"
        "permissions: {namespaces: {items: {write: human}}}\n")
    original_read = Path.read_text
    reads = {host / "schema.yaml": 0}
    def fail_legacy_read(p, *args, **kwargs):
        if p == host / "schema.yaml":
            reads[p] += 1
            if reads[p] == 2:
                raise OSError("unreadable")
        return original_read(p, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", fail_legacy_read)
    plan = m.Plan(False); m.merge_namespaces(host, pack, plan)
    assert plan.fails and "does not prove identity" in plan.fails[0]

    monkeypatch.setattr(Path, "read_text", original_read)
    state = host / ".okengine/installed-domains/pack.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"owned_namespaces": {"items": {}}}))
    plan = m.Plan(False); m.merge_namespaces(host, pack, plan)
    assert plan.fails and "contract has drifted" in plan.fails[0]


def test_feed_copy_parse_failure_and_cron_prompt_info(tmp_path):
    m = _mod("install_domain_feed_edges")
    host, pack = _base(tmp_path)
    (pack / "feeds").mkdir(); (pack / "feeds/feeds.opml").write_text("<opml><body/></opml>")
    plan = m.Plan(True); m.merge_feeds(host, pack, plan)
    assert plan.run() == 0 and (host / "feeds/feeds.opml").is_file()
    (host / "feeds/feeds.opml").write_text("[bad")
    plan = m.Plan(False); m.merge_feeds(host, pack, plan)
    assert plan.fails

    (pack / "crons").mkdir(); (host / "crons").mkdir()
    (pack / "crons/engine-template-prompts.json").write_text(json.dumps({"new": "prompt"}))
    (host / "crons/engine-template-prompts.json").write_text("{}")
    plan = m.Plan(False); m.merge_crons(host, pack, plan)
    assert plan.infos


def test_lane_scope_persona_legacy_and_mapping_edges(tmp_path):
    m = _mod("install_domain_lane_persona_edges")
    host, pack = _base(tmp_path)
    src = pack / "crons/scripts"; src.mkdir(parents=True)
    (src / "one.py").write_text("one")
    (src / "two.py").write_text("two")
    plan = m.Plan(True)
    m.merge_lane_scripts(host, pack, plan, scope={"one.py"})
    assert plan.run() == 0
    assert (host / "crons/scripts/one.py").is_file()
    assert not (host / "crons/scripts/two.py").exists()

    (host / "CLAUDE.md").write_text(
        f"{m.PERSONA_MARKER} old `wiki/domain/`\n")
    plan = m.Plan(False); m.append_persona(host, pack, "domain", plan)
    assert plan.steps == []
    assert m._mapping_value(yaml.compose("- one\n"), "x") is None


def test_replace_boxes_dump_suffix_and_cockpit_invalid_declarations(tmp_path, monkeypatch):
    m = _mod("install_domain_cockpit_edges")
    original = m.yaml.safe_dump
    monkeypatch.setattr(m.yaml, "safe_dump", lambda *_a, **_k: "[]\n...")
    out = m._replace_cockpit_boxes(
        "cockpit:\n  tab_defs:\n    one:\n      boxes: []\n", "one", [])
    assert "..." not in out
    monkeypatch.setattr(m.yaml, "safe_dump", original)

    host, pack = _base(tmp_path)
    (host / "schema.yaml").write_text(
        "types: {}\ncockpit:\n  tabs: [overview, browse]\n  tab_defs:\n"
        "    overview: {label: Overview, boxes: []}\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(yaml.safe_dump({
        "types": {}, "cockpit": {"tab_contributions": {
            "wrong": {},
            "pack.scalar": "bad",
            "pack.missing": {"target": "absent", "boxes": [{"id": "pack.x"}]},
            "pack.empty": {"target": "overview", "boxes": []},
            "pack.badbox": {"target": "overview", "boxes": [{}]},
        }, "tab_aliases": {"bad": "absent"}}}))
    plan = m.Plan(False); m.merge_cockpit(host, pack, plan)
    assert len(plan.fails) >= 6


def test_legacy_runtime_ownership_parse_errors_and_scalar_rows(tmp_path, monkeypatch):
    m = _mod("install_domain_legacy_edges")
    host, pack = _base(tmp_path)
    (pack / "crons").mkdir(); (host / "crons").mkdir()
    (pack / "crons/domain-crons.json").write_text("{bad")
    (host / "crons/domain-crons.json").write_text("{bad")
    monkeypatch.setattr(m, "_load_mod", lambda _name: SimpleNamespace(
        source_manifest=lambda *_a: {"shared_support_scripts": {"shared.py": "hash"}}))
    result = m._legacy_runtime_ownership(host, pack)
    assert result["cron_jobs"] == {}
    assert "shared.py" in result["shared_support_scripts"]

    (pack / "crons/domain-crons.json").write_text(json.dumps(["scalar", {
        "name": "pack-job", "script": "lane.py"}]))
    (host / "crons/domain-crons.json").write_text(json.dumps([{"name": "pack-job"}]))
    result = m._legacy_runtime_ownership(host, pack)
    assert "pack-job" in result["cron_jobs"]


def test_main_missing_pack_preflight_and_script_entrypoint(tmp_path, monkeypatch, capsys):
    m = _mod("install_domain_main_edges")
    assert m.main([str(tmp_path / "missing-host"), str(tmp_path / "missing-pack")]) == 2
    host, pack = _base(tmp_path)
    preflight = SimpleNamespace(main=lambda _args: 1)
    monkeypatch.setattr(m, "_load_mod", lambda _name: preflight)
    assert m.main([str(host), str(pack)]) == 1
    assert "preflight FAIL" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path / "x"), str(tmp_path / "y")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 2


def test_remaining_merge_layout_and_warning_paths(tmp_path, monkeypatch):
    m = _mod("install_domain_remaining_merges")
    host, pack = _base(tmp_path)
    cfg = pack / "config"; cfg.mkdir()
    (cfg / "completeness-rules.yaml").write_text(
        "rules:\n- {id: add, when: {type: owned}}\n")
    plan = m.Plan(False); m.merge_rules(host, pack, {"owned"}, plan)
    assert plan.steps and not plan.warns

    # Coverage block exists, so insertion takes the existing-section path.
    (host / "schema.yaml").write_text("types: {}\ncoverage_fields:\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(
        "coverage_fields: [{type: one, field: field}]\n")
    plan = m.Plan(True); m.merge_coverage_fields(host, pack, plan)
    assert plan.run() == 0

    # Only field_enums is pending; enums takes the empty-block continue. The existing field entry
    # is block style, so replacement spans through the next top-level entry.
    (host / "schema.yaml").write_text(
        "types: {}\nfield_enums:\n  status:\n    by_type: {host: [a]}\nafter: true\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(yaml.safe_dump({
        "field_enums": {"status": {"by_type": {"guest": ["b"]}}}}))
    plan = m.Plan(True); m.merge_enums(host, pack, plan)
    assert plan.run() == 0

    (host / "schema.yaml").write_text("types: {}\nfield_shapes: {same: scalar}\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(
        "field_shapes: {same: list}\n")
    plan = m.Plan(False); m.merge_field_contracts(host, pack, plan)
    assert any("differs" in warning for warning in plan.warns)


def test_list_dump_suffix_and_nonmapping_document(tmp_path, monkeypatch):
    m = _mod("install_domain_list_dump_edges")
    host, pack = _base(tmp_path)
    (pack / "subdomain/host-schema-additions.yaml").write_text(
        "protected_fields: [one]\n")
    plan = m.Plan(True); m.merge_list_contracts(host, pack, plan)
    original = m.yaml.safe_dump
    original_compose = m.yaml.compose
    monkeypatch.setattr(m.yaml, "safe_dump", lambda *_a, **_k: "[one]\n...")
    monkeypatch.setattr(m.yaml, "compose", lambda _text: original_compose("- scalar\n"))
    assert plan.run() == 0
    monkeypatch.setattr(m.yaml, "safe_dump", original)


def test_namespace_existing_parent_without_namespaces(tmp_path):
    m = _mod("install_domain_namespace_inner_absent")
    host, pack = _base(tmp_path)
    (host / "schema.yaml").write_text(
        "types: {}\npartitioning:\n  other: true\npermissions:\n  other: true\ntier:\n  other: true\n")
    (pack / "pack.yaml").write_text("name: pack\nowns: {namespaces: [items]}\n")
    (pack / "schema.yaml").write_text(
        "partitioning: {namespaces: {items: {strategy: flat}}}\n"
        "permissions: {namespaces: {items: {write: agent}}}\n"
        "tier: {namespaces: {items: warm}}\n")
    plan = m.Plan(True); m.merge_namespaces(host, pack, plan)
    assert plan.run() == 0


def test_persona_missing_shared_subtree_mapping_and_block_boxes(tmp_path):
    m = _mod("install_domain_small_edges")
    host, pack = _base(tmp_path)
    (host / "CLAUDE.md").write_text("host")
    plan = m.Plan(False); m.append_persona(host, pack, "pack", plan)
    assert any("no subdomain/PERSONA" in warning for warning in plan.warns)

    (host / "schema.yaml").write_text("types: {shared: {}}\n")
    (pack / "subdomain/schema.yaml").write_text("types: {shared: {}}\n")
    plan = m.Plan(False); m.install_subtree(host, pack, "sub", plan)
    assert any("shared with the host" in info for info in plan.infos)
    assert m._mapping_value(yaml.compose("a: 1\n"), "missing") is None

    text = ("cockpit:\n  tab_defs:\n    one:\n      boxes:\n"
            "        - {id: old}\n    two: {boxes: []}\n")
    out = m._replace_cockpit_boxes(text, "one", [{"id": "new"}])
    assert "\ntwo:" in out


def test_cockpit_alias_conflict_and_existing_alias_block(tmp_path):
    m = _mod("install_domain_alias_edges")
    host, pack = _base(tmp_path)
    (host / "schema.yaml").write_text(
        "types: {}\ncockpit:\n  tabs: [overview, browse]\n  tab_defs:\n"
        "    overview: {boxes: []}\n    browse: {boxes: []}\n"
        "  tab_aliases:\n    old: overview\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(yaml.safe_dump({
        "cockpit": {"tab_aliases": {"old": "browse", "new": "overview"}}}))
    plan = m.Plan(True); m.merge_cockpit(host, pack, plan)
    assert any("conflicts" in failure for failure in plan.fails)
    # The conflict blocks Plan.run, but executing the alias merge thunk verifies existing-block insert.
    assert plan.steps
    plan.steps[0][1]()
    aliases = yaml.safe_load((host / "schema.yaml").read_text())["cockpit"]["tab_aliases"]
    assert aliases["new"] == "overview"


def test_main_both_shape_requires_explicit_choice(tmp_path, capsys):
    m = _mod("install_domain_both_shape")
    host, pack = _base(tmp_path)
    (pack / "subdomain/schema.yaml").write_text("types: {}\n")
    assert m.main([str(host), str(pack)]) == 2
    assert "ships BOTH" in capsys.readouterr().err


def test_alias_namespace_cron_persona_and_tab_loop_branches(tmp_path):
    m = _mod("install_domain_final_direct_edges")
    host, pack = _base(tmp_path)
    (host / "schema.yaml").write_text("types: {}\ntype_aliases:\nafter: true\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text("type_aliases: {new: target}\n")
    plan = m.Plan(True); m.merge_type_aliases(host, pack, plan)
    assert plan.run() == 0

    (host / "schema.yaml").write_text("types: {}\n")
    (pack / "pack.yaml").write_text("name: pack\nowns: {namespaces: [items]}\n")
    (pack / "schema.yaml").write_text(
        "partitioning: {namespaces: {items: {strategy: flat}}}\n")
    plan = m.Plan(True); m.merge_namespaces(host, pack, plan)
    assert plan.run() == 0

    (host / "crons").mkdir(); (pack / "crons").mkdir()
    (host / "crons/domain-crons.json").write_text(json.dumps([
        {"name": "pack-job", "script": "old.py"}]))
    (pack / "crons/domain-crons.json").write_text(json.dumps([
        {"name": "pack-job", "script": "new.py"},
        {"name": "pack-new", "script": "add.py"}]))
    (host / "crons/engine-template-prompts.json").write_text(json.dumps({"same": "host"}))
    (pack / "crons/engine-template-prompts.json").write_text(json.dumps({"same": "pack"}))
    plan = m.Plan(True); m.merge_crons(host, pack, plan, refresh=True, owned={"pack-job"})
    assert plan.run() == 0

    # No existing CLAUDE.md takes the direct source-presence branch.
    (pack / "subdomain/PERSONA.md").write_text("persona")
    plan = m.Plan(True); m.append_persona(host, pack, "pack", plan)
    assert plan.run() == 0

    (host / "schema.yaml").write_text(
        "types: {}\ncockpit:\n  tabs: [browse]\n  tab_defs:\n"
        "    browse: {boxes: []}\n")
    (pack / "subdomain/host-schema-additions.yaml").write_text(yaml.safe_dump({
        "cockpit": {"tabs": ["one", "one", "two"], "tab_defs": {
            "one": {"boxes": []}, "two": {"boxes": []}}}}))
    plan = m.Plan(True); m.merge_cockpit(host, pack, plan)
    assert plan.run() == 0


def test_legacy_multiple_owned_rows_loop(tmp_path, monkeypatch):
    m = _mod("install_domain_legacy_loop")
    host, pack = _base(tmp_path)
    (host / "crons").mkdir(); (pack / "crons").mkdir()
    rows = [{"name": "pack-one", "script": "one.py"},
            {"name": "pack-two", "script": "two.py"},
            {"name": "pack-three"}]
    (host / "crons/domain-crons.json").write_text(json.dumps(rows))
    (pack / "crons/domain-crons.json").write_text(json.dumps(rows))
    monkeypatch.setattr(m, "_load_mod", lambda _name: SimpleNamespace(
        source_manifest=lambda *_a: {}))
    result = m._legacy_runtime_ownership(host, pack)
    assert set(result["cron_jobs"]) == {"pack-one", "pack-two", "pack-three"}


def test_main_transaction_failures_without_snapshot(tmp_path, monkeypatch):
    m = _mod("install_domain_transaction_edges")
    host, pack = _base(tmp_path)
    state_data = {"prior": None}
    state = SimpleNamespace(
        load=lambda *_a: state_data["prior"],
        source_manifest=lambda *_a: {"pack_version": "2.0.0", "lane_scripts": {"new": "x"}},
        write=lambda *_a: True,
        manifest_path=lambda *_a: Path("manifest.json"),
    )
    fu = SimpleNamespace(
        installed_pack_version=lambda *_a, **_k: "1.0.0",
        snapshot=lambda *_a, **_k: None,
        added_since_snapshot=lambda *_a: set(), changed_since_snapshot=lambda *_a: set(),
        restore=lambda *_a, **_k: 0,
        run_pack_migrations=lambda *_a, **_k: 0,
        record_pack_version=lambda *_a: None,
    )
    compose = SimpleNamespace(write_composed_schema=lambda _h: ["conflict"])
    preflight = SimpleNamespace(main=lambda _a: 0)
    def load(name):
        return {"coinstall_preflight.py": preflight, "composed_pack_state.py": state,
                "framework_upgrade.py": fu, "extension_compose.py": compose}[name]
    monkeypatch.setattr(m, "_load_mod", load)
    monkeypatch.setattr(m, "install_taxonomy", lambda _h, _p, _s, plan, **_k:
                        plan.step("change", lambda: None))
    assert m.main([str(host), str(pack), "--apply"]) == 1

    # Existing member, changed runtime ownership, no planned edits: manifest is deliberately not
    # blessed and a failed pack migration returns without a transaction snapshot.
    state_data["prior"] = {"pack_version": "1.0.0", "lane_scripts": {"old": "x"}}
    monkeypatch.setattr(m, "install_taxonomy", lambda *_a, **_k: None)
    fu.run_pack_migrations = lambda *_a, **_k: 1
    assert m.main([str(host), str(pack), "--apply"]) == 1
