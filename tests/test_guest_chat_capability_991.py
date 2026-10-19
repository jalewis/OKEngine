"""okengine#991 [15]: a co-installed guest pack's chat capability is never composed. Say so.

`policy_plane.discover_documents(vault)` composes `<vault>/policy.yaml` and
`<vault>/.okengine/policy.yaml` for the directory it is given. A pack deployed standalone is that
directory, so its `chat:*` replacement is composed whether or not it also ships a `subdomain/`
co-install form. Installed INTO a host by `framework install-domain`, the pack's policy files never
reach the host: the host composes only its own. That is the inert case, and the only one warned:

- install-domain warns when it installs a guest that declares a `chat:*` capability, and records
  the declaration in the guest's ownership manifest;
- `framework validate` on the host warns for every installed guest whose manifest records one.

Validating a pack directory itself (a standalone deployment, or a pack source) never warns.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from test_framework_install_domain import _host, _subtree_pack, _taxonomy_pack, mod  # noqa: E402


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_991", REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_991"] = module
    spec.loader.exec_module(module)
    return module


STATE = _sibling("composed_pack_state")
VALIDATE = _sibling("framework_validate")

_FLOOR = yaml.safe_load((REPO / "config" / "policy" / "catalog.yaml").read_text(
    encoding="utf-8"))["capabilities"]["chat:api_server"]


def _policy(path: Path, capabilities) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": 1, "rules": [],
                                    "capabilities": capabilities}), encoding="utf-8")


def _chat_rows(report) -> list[tuple[str, str]]:
    return [(sev, detail) for sev, check, detail in report.rows if check == "guest chat capability"]


# --- what a pack declares -------------------------------------------------------------------


def test_chat_capabilities_lists_every_policy_file_the_pack_ships(tmp_path):
    pack = tmp_path / "pack"
    _policy(pack / "policy.yaml", {"chat:api_server": {}, "cron:x": {}})
    _policy(pack / ".okengine" / "policy.yaml", {"chat:telegram": {}})
    _policy(pack / "extensions" / "okpack.x" / "policy.yaml", {"chat:api_server": {}})
    assert STATE.chat_capabilities(pack) == {
        "policy.yaml": ["chat:api_server"],
        ".okengine/policy.yaml": ["chat:telegram"],
        "extensions/okpack.x/policy.yaml": ["chat:api_server"],
    }


@pytest.mark.parametrize("text", [
    yaml.safe_dump({"capabilities": {"cron:x": {}}}),
    yaml.safe_dump({"schema_version": 1}),
    yaml.safe_dump({"capabilities": ["chat:api_server"]}),
    yaml.safe_dump(["not", "a", "mapping"]),
    "capabilities: [unclosed\n",
])
def test_a_policy_without_a_chat_capability_declares_none(tmp_path, text):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "policy.yaml").write_text(text, encoding="utf-8")
    assert STATE.chat_capabilities(pack) == {}


def test_the_manifest_records_a_chat_declaration_only_when_there_is_one(tmp_path):
    pack = _taxonomy_pack(tmp_path)
    assert "chat_capabilities" not in STATE.source_manifest(pack, "taxonomy")
    _policy(pack / "policy.yaml", {"chat:api_server": _FLOOR})
    assert STATE.source_manifest(pack, "taxonomy")["chat_capabilities"] == {
        "policy.yaml": ["chat:api_server"]}


# --- the warning wording is correct per shape -----------------------------------------------


def test_a_taxonomy_guest_is_told_its_types_are_reached_only_through_the_host_capability():
    text = STATE.guest_chat_warning({"pack": "okpack-tax", "shape": "taxonomy",
                                     "chat_capabilities": {"policy.yaml": ["chat:api_server"]}})
    assert text.startswith("okpack-tax: policy.yaml declares chat:api_server, which is never "
                           "composed in this host"), text
    assert "merged into the host schema" in text and "host's own chat capability" in text, text
    assert "wiki/<slug>/" not in text, text


def test_a_subtree_guest_is_told_its_sub_domain_is_outside_the_host_chat_scope():
    text = STATE.guest_chat_warning({"pack": "okpack-doct", "shape": "subtree",
                                     "chat_capabilities": {"policy.yaml": ["chat:api_server"],
                                                           ".okengine/policy.yaml": ["chat:x"]}})
    assert text.startswith("okpack-doct: policy.yaml declares chat:api_server; "
                           ".okengine/policy.yaml declares chat:x, which is never composed"), text
    assert "wiki/<slug>/" in text and "merged into the host schema" not in text, text


def test_a_guest_without_a_chat_declaration_gets_no_warning():
    assert STATE.guest_chat_warning({"pack": "p", "shape": "taxonomy"}) is None
    assert STATE.guest_chat_warning({"pack": "p", "chat_capabilities": []}) is None


# --- install-domain and the host's validate -------------------------------------------------


@pytest.mark.parametrize(("make", "shape_text"), [
    (_taxonomy_pack, "merged into the host schema"),
    (_subtree_pack, "wiki/<slug>/"),
])
def test_installing_a_guest_with_a_chat_capability_warns_and_host_validate_repeats_it(
        tmp_path, capsys, make, shape_text):
    host, pack = _host(tmp_path), make(tmp_path)
    _policy(pack / "policy.yaml", {"chat:api_server": _FLOOR})
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    out = capsys.readouterr().out
    assert f"WARN  {pack.name}: policy.yaml declares chat:api_server" in out, out
    rows = _chat_rows(VALIDATE.validate(host))
    assert len(rows) == 1 and rows[0][0] == "WARN", rows
    assert rows[0][1].startswith(f"{pack.name}: policy.yaml declares chat:api_server"), rows
    assert shape_text in rows[0][1], rows


def test_a_host_whose_guests_declare_no_chat_capability_is_not_warned(tmp_path, capsys):
    host, pack = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    assert "chat" not in capsys.readouterr().out
    assert _chat_rows(VALIDATE.validate(host)) == []


def test_a_standalone_deployment_with_a_co_install_form_and_a_chat_replacement_is_not_warned(
        tmp_path):
    """QA false positive on !1257: a standalone pack keeps its subdomain/ form, and
    discover_documents composes its policy.yaml, so its chat replacement is live, not inert."""
    pack = _taxonomy_pack(tmp_path)
    _policy(pack / "policy.yaml", {"chat:api_server": _FLOOR})
    assert _chat_rows(VALIDATE.validate(pack)) == []


def test_an_unreadable_manifest_is_left_to_the_drift_check(tmp_path):
    host = _host(tmp_path)
    domains = host / ".okengine" / "installed-domains"
    domains.mkdir(parents=True)
    (domains / "broken.json").write_text("{not json", encoding="utf-8")
    (domains / "list.json").write_text(json.dumps(["not", "a", "manifest"]), encoding="utf-8")
    assert STATE.guest_chat_warnings(host) == []


# --- main's mutation-changed run on fe826132: each test below kills a survivor ---------------


@pytest.mark.parametrize("first", ["capabilities: [unclosed\n", "schema_version: 1\n"])
def test_a_policy_file_without_chat_does_not_stop_the_scan_of_later_files(tmp_path, first):
    """An unparseable or capability-less policy.yaml is skipped, not the end of the scan."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "policy.yaml").write_text(first, encoding="utf-8")
    _policy(pack / ".okengine" / "policy.yaml", {"chat:api_server": {}})
    assert STATE.chat_capabilities(pack) == {".okengine/policy.yaml": ["chat:api_server"]}


def test_an_unreadable_manifest_does_not_hide_a_later_guest(tmp_path):
    host = _host(tmp_path)
    domains = host / ".okengine" / "installed-domains"
    domains.mkdir(parents=True)
    (domains / "a-broken.json").write_text("{not json", encoding="utf-8")
    (domains / "b-guest.json").write_text(json.dumps(
        {"pack": "okpack-b", "shape": "taxonomy",
         "chat_capabilities": {"policy.yaml": ["chat:api_server"]}}), encoding="utf-8")
    warnings = STATE.guest_chat_warnings(host)
    assert len(warnings) == 1 and warnings[0].startswith("okpack-b: policy.yaml"), warnings


@pytest.mark.parametrize("declared", [{}, ["policy.yaml"], "policy.yaml"])
def test_an_empty_or_malformed_chat_record_gets_no_warning(declared):
    assert STATE.guest_chat_warning({"pack": "p", "shape": "taxonomy",
                                     "chat_capabilities": declared}) is None


def test_a_manifest_that_records_no_shape_gets_the_taxonomy_wording():
    """install-domain records only `subtree` or `taxonomy` (it refuses a pack shipping both); a
    manifest without a shape is judged as not a subtree rather than crashing."""
    text = STATE.guest_chat_warning({"pack": "p",
                                     "chat_capabilities": {"policy.yaml": ["chat:api_server"]}})
    assert "merged into the host schema" in text and "wiki/<slug>/" not in text, text


def test_the_manifest_records_its_schema_version(tmp_path):
    pack = _taxonomy_pack(tmp_path)
    assert STATE.source_manifest(pack, "taxonomy")["manifest_version"] == 2


# --- okengine#993: a re-install that keeps the accepted runtime hashes still records chat -----


def _add_lane_job(pack: Path) -> None:
    """A pack update that only ADDS a cron job: install-domain appends it (rc 0), but the runtime
    changed, so the manifest keeps its last accepted lane_scripts/cron_jobs hashes."""
    jobs = json.loads((pack / "crons" / "domain-crons.json").read_text(encoding="utf-8"))
    jobs.append({"id": "dd", "name": "okpack-tax-second", "script": "okpack_tax_feed_fetch.py"})
    (pack / "crons" / "domain-crons.json").write_text(json.dumps(jobs), encoding="utf-8")


def test_a_reinstall_that_skips_the_manifest_still_records_a_new_chat_declaration(
        tmp_path, capsys):
    host, pack = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    before = STATE.load(host, "okpack-tax")
    _add_lane_job(pack)
    _policy(pack / "policy.yaml", {"chat:api_server": _FLOOR})
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    after = STATE.load(host, "okpack-tax")
    assert after["chat_capabilities"] == {"policy.yaml": ["chat:api_server"]}
    assert {k: v for k, v in after.items() if k != "chat_capabilities"} == before, \
        "only the chat declaration may change without --refresh"
    rows = _chat_rows(VALIDATE.validate(host))
    assert len(rows) == 1 and rows[0][1].startswith("okpack-tax: policy.yaml"), rows


def test_a_reinstall_that_skips_the_manifest_still_drops_a_withdrawn_chat_declaration(
        tmp_path, capsys):
    host, pack = _host(tmp_path), _taxonomy_pack(tmp_path)
    _policy(pack / "policy.yaml", {"chat:api_server": _FLOOR})
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    before = STATE.load(host, "okpack-tax")
    assert "chat_capabilities" in before
    _add_lane_job(pack)
    (pack / "policy.yaml").unlink()
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    after = STATE.load(host, "okpack-tax")
    assert after == {k: v for k, v in before.items() if k != "chat_capabilities"}
    assert _chat_rows(VALIDATE.validate(host)) == []


def test_a_reinstall_that_changes_nothing_about_chat_leaves_the_manifest_untouched(tmp_path):
    host, pack = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    path = STATE.manifest_path(host, "okpack-tax")
    before = path.read_text(encoding="utf-8")
    _add_lane_job(pack)
    assert mod.main([str(host), str(pack), "--apply"]) == 0
    assert path.read_text(encoding="utf-8") == before


def test_the_chat_record_keeps_every_other_accepted_field_whatever_its_name():
    """Only `chat_capabilities` may change; a field sorting before it (`alpha`) survives too."""
    accepted = {"alpha": 1, "cron_jobs": {"j": "h"}, "chat_capabilities": {"policy.yaml": ["x"]}}
    assert STATE.with_chat_record(accepted, {"cron_jobs": {"j": "new"}}) == {
        "alpha": 1, "cron_jobs": {"j": "h"}}
    assert STATE.with_chat_record({"alpha": 1}, {"chat_capabilities": {"p": ["chat:a"]}}) == {
        "alpha": 1, "chat_capabilities": {"p": ["chat:a"]}}
