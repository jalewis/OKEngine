"""okengine#1027: the pack principal of an enforced write comes from the deployment's definition.

`okengine.pack_principal` is the one rule the write server (`_prov_pack`, its startup guard) and
the `write-principal` deployment check share."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okengine import pack_principal as pp


def _vault(tmp_path: Path, pack_yaml: str | None = "name: host-pack\n") -> Path:
    if pack_yaml is not None:
        (tmp_path / "pack.yaml").write_text(pack_yaml, encoding="utf-8")
    return tmp_path


def _install(vault: Path, pack: str, jobs, filename: str | None = None) -> None:
    folder = vault / ".okengine" / "installed-domains"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (filename or f"{pack}.json")).write_text(
        json.dumps({"pack": pack, "cron_jobs": jobs}), encoding="utf-8")


# --- declared_pack ------------------------------------------------------------------------------


def test_the_declared_pack_is_the_pack_yaml_name(tmp_path):
    assert pp.declared_pack(_vault(tmp_path, "name: '  okpack-a  '\nversion: 1\n")) == "okpack-a"


@pytest.mark.parametrize("text", [None, "version: 1\n", "name: 7\n", "- a\n", "name: [\n",
                                  "name:\n"])
def test_a_missing_or_unusable_name_declares_no_pack(tmp_path, text):
    assert pp.declared_pack(_vault(tmp_path, text)) == ""


def test_an_undecodable_pack_yaml_declares_no_pack(tmp_path):
    (tmp_path / "pack.yaml").write_bytes(b"name: \xff\xfe\n")
    assert pp.declared_pack(tmp_path) == ""


# --- deployment_pack / startup_refusal -------------------------------------------------------


def test_pack_yaml_wins_over_an_inherited_environment_value(tmp_path):
    vault = _vault(tmp_path)
    assert pp.deployment_pack(vault, {"OKENGINE_PACK": "other"}) == "host-pack"


def test_the_environment_is_used_only_where_pack_yaml_declares_none(tmp_path):
    assert pp.deployment_pack(_vault(tmp_path, None), {"OKENGINE_PACK": " legacy "}) == "legacy"
    assert pp.deployment_pack(_vault(tmp_path, None), {}) == ""


@pytest.mark.parametrize(("pack_yaml", "env"), [
    ("name: host-pack\n", {}),
    ("name: host-pack\n", {"OKENGINE_PACK": "host-pack"}),
    (None, {"OKENGINE_PACK": "legacy"}),
])
def test_a_writer_with_one_unambiguous_principal_may_start(tmp_path, pack_yaml, env):
    assert pp.startup_refusal(_vault(tmp_path, pack_yaml), env) is None


def test_a_writer_with_no_principal_refuses_to_start(tmp_path):
    why = pp.startup_refusal(_vault(tmp_path, "version: 1\n"), {"OKENGINE_PACK": "  "})
    assert why and "no pack principal" in why and "pack.yaml" in why


@pytest.mark.parametrize("pinned", ["renamed", "aaa-earlier"])   # either side of the name
def test_a_writer_whose_environment_disagrees_with_pack_yaml_refuses_to_start(tmp_path, pinned):
    why = pp.startup_refusal(_vault(tmp_path), {"OKENGINE_PACK": pinned})
    assert why and f"OKENGINE_PACK={pinned}" in why and "'host-pack'" in why


# --- installed_domain_jobs ------------------------------------------------------------------


def test_installed_domains_map_each_pack_to_its_jobs(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-guest", ["okpack-guest-a", "okpack-guest-b"])
    _install(vault, "okpack-dict", {"okpack-dict-x": "sha"})
    assert pp.installed_domain_jobs(vault) == {
        "okpack-guest": {"okpack-guest-a", "okpack-guest-b"},
        "okpack-dict": {"okpack-dict-x"},
    }


def test_unusable_manifests_are_skipped_and_a_pack_without_jobs_is_kept(tmp_path):
    vault = _vault(tmp_path)
    folder = vault / ".okengine" / "installed-domains"
    folder.mkdir(parents=True)
    (folder / "broken.json").write_text("{", encoding="utf-8")
    (folder / "binary.json").write_bytes(b"\xff\xfe")
    (folder / "list.json").write_text("[]", encoding="utf-8")
    (folder / "nameless.json").write_text(json.dumps({"pack": " ", "cron_jobs": []}))
    (folder / "notes.txt").write_text(json.dumps({"pack": "ignored"}))
    _install(vault, " okpack-bare ", None, filename="zz-bare.json")   # after every unusable one
    assert pp.installed_domain_jobs(vault) == {"okpack-bare": set()}


def test_no_installed_domains_dir_means_no_co_installed_packs(tmp_path):
    assert pp.installed_domain_jobs(_vault(tmp_path)) == {}


# --- lane_pack -------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["", "admin", "chat:api_server", "ext:okengine.lacuna",
                                   "cron:entity-backfill", "cron:okengine.predictions:grade",
                                   "cron:okpack-guestx-lane", "cron:entity-backfill@stranger",
                                   "cron:stranger:lane", "chat:okpack-guest-lane",
                                   "cron:okpack-guest"])
def test_every_other_caller_acts_for_the_deployment_pack(tmp_path, actor):
    vault = _vault(tmp_path)
    _install(vault, "okpack-guest", ["okpack-guest-lane"])
    assert pp.lane_pack(vault, actor, "host-pack") == "host-pack"


def test_a_co_installed_lane_acts_for_the_pack_that_recorded_it(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-guest", ["renamed-lane"])
    assert pp.lane_pack(vault, "cron:renamed-lane", "host-pack") == "okpack-guest"


def test_a_co_installed_prefix_names_the_longest_matching_pack(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-a", [])
    _install(vault, "okpack-a-b", [])
    assert pp.lane_pack(vault, "cron:okpack-a-b-weekly", "host-pack") == "okpack-a-b"
    assert pp.lane_pack(vault, "cron:okpack-a-weekly", "host-pack") == "okpack-a"


def test_a_shorter_prefix_seen_later_does_not_displace_a_longer_one(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-a-b", [], filename="a.json")
    _install(vault, "okpack-a", [], filename="b.json")
    assert pp.lane_pack(vault, "cron:okpack-a-b-weekly", "host-pack") == "okpack-a-b"


def test_a_composed_engine_template_instance_acts_for_its_driving_pack(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-guest", [])
    assert pp.lane_pack(vault, "cron:entity-backfill@okpack-guest", "host-pack") == "okpack-guest"
    assert pp.lane_pack(vault, "cron:entity-backfill@host-pack", "host-pack") == "host-pack"


def test_a_composed_domain_job_acts_for_its_named_pack(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-guest", [])
    assert pp.lane_pack(vault, "cron:okpack-guest:weekly", "host-pack") == "okpack-guest"


def test_without_a_deployment_pack_no_lane_is_refined(tmp_path):
    vault = _vault(tmp_path)
    _install(vault, "okpack-guest", ["okpack-guest-lane"])
    assert pp.lane_pack(vault, "cron:okpack-guest-lane", "") == ""
