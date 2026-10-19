"""install-domain --refresh carries an owned namespace's partitioning change — but only when it moves
no existing page (okengine#915).

Found on a live composed deployment: the library dropped `reshard_by` from okpack-vuln's `cves`
namespace, and every later refresh of the pack failed preflight with "namespace already exists in
host", because ownership was inferred from partitioning EQUALITY and refresh merges no schema. Any
composed pack whose partitioning changed upstream could never be refreshed again.

Disposable fixture vaults only (tmp_path).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str, path: Path):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = _load("framework_install_domain_partition", SCRIPTS / "framework_install_domain.py")
refresh = _load("partition_refresh_under_test", SCRIPTS / "partition_refresh.py")
okf = _load("okf_migrate_partition_refresh", SCRIPTS / "cron" / "okf_migrate.py")

OLD = {"strategy": "by-date", "date_field": "date_added", "reshard_by": "day"}
NEW = {"strategy": "by-date", "date_field": "date_added"}


def _host(tmp_path: Path) -> Path:
    host = tmp_path / "host"
    for sub in ("wiki", "config", "crons"):
        (host / sub).mkdir(parents=True)
    (host / "schema.yaml").write_text(
        "# host schema -- comments must survive a partitioning rewrite\n"
        "name: okpack-host\n"
        "types:\n  entity: {required: [type, id]}\n"
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}  # keep entities\n")
    (host / "pack.yaml").write_text("name: okpack-host\nowns:\n  types: [entity]\n")
    (host / "crons" / "domain-crons.json").write_text("[]")
    return host


def _pack(tmp_path: Path, partitioning: dict, name: str = "okpack-vulnx") -> Path:
    pack = tmp_path / name
    (pack / "subdomain").mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        f"name: {name}\nowns:\n  types: [cve]\n  namespaces: [cves]\n")
    (pack / "schema.yaml").write_text(yaml.safe_dump({
        "name": name, "types": {"cve": {"required": ["type", "id"]}},
        "partitioning": {"namespaces": {"cves": partitioning}}}, sort_keys=False))
    (pack / "subdomain" / "host-schema-additions.yaml").write_text(
        yaml.safe_dump({"types": {"cve": {"required": ["type", "id"]}}}))
    (pack / "subdomain" / "PERSONA.md").write_text("cve curation rules\n")
    return pack


def _set_pack_partitioning(pack: Path, partitioning: dict) -> None:
    schema = yaml.safe_load((pack / "schema.yaml").read_text())
    schema["partitioning"]["namespaces"]["cves"] = partitioning
    (pack / "schema.yaml").write_text(yaml.safe_dump(schema, sort_keys=False))


def _page(host: Path, key: str, **fm) -> Path:
    path = host / "wiki" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"type": "cve", "id": Path(key).name, **fm}
    path.write_text("---\n" + yaml.safe_dump(body, sort_keys=False) + "---\nbody\n")
    return path


def _installed(tmp_path: Path) -> tuple[Path, Path]:
    """A host with the pack installed under the OLD partitioning (reshard_by: day)."""
    host, pack = _host(tmp_path), _pack(tmp_path, OLD)
    assert install.main([str(host), str(pack), "--apply"]) == 0
    assert yaml.safe_load((host / "schema.yaml").read_text())["partitioning"]["namespaces"]["cves"] == OLD
    return host, pack


def _host_cves(host: Path) -> dict:
    return yaml.safe_load((host / "schema.yaml").read_text())["partitioning"]["namespaces"]["cves"]


# -- the refresh that must now work -------------------------------------------------------------
def test_refresh_dry_run_plans_a_no_move_partitioning_change_and_writes_nothing(tmp_path, capsys):
    host, pack = _installed(tmp_path)
    _page(host, "cves/2024/05/cve-2024-0001", date_added="2024-05-03")   # canonical under both
    _set_pack_partitioning(pack, NEW)
    before = (host / "schema.yaml").read_bytes()

    assert install.main([str(host), str(pack), "--refresh"]) == 0

    out = capsys.readouterr().out
    assert "FAIL" not in out.split("install-domain:")[0], out
    assert "partitioning change for owned namespace 'cves'" in out, out
    assert "proof: 1 page(s) checked, 0 would move" in out, out
    assert (host / "schema.yaml").read_bytes() == before, "a dry run must not write"


def test_refresh_apply_writes_the_partitioning_change_in_place(tmp_path):
    host, pack = _installed(tmp_path)
    _page(host, "cves/2024/05/cve-2024-0001", date_added="2024-05-03")
    _set_pack_partitioning(pack, NEW)

    assert install.main([str(host), str(pack), "--refresh", "--apply"]) == 0

    text = (host / "schema.yaml").read_text()
    assert _host_cves(host) == NEW
    assert "# keep entities" in text and text.startswith("# host schema"), "comments survive"
    recorded = json.loads((host / ".okengine" / "installed-domains" / "okpack-vulnx.json").read_text())
    assert recorded["owned_namespaces"]["cves"]["partitioning"] == NEW
    # the page is where it was, and the change is now idempotent
    assert (host / "wiki" / "cves" / "2024" / "05" / "cve-2024-0001.md").is_file()
    assert install.main([str(host), str(pack), "--refresh"]) == 0


# -- negative fixtures ---------------------------------------------------------------------------
def test_refresh_refuses_a_partitioning_change_that_would_move_pages(tmp_path, capsys):
    host, pack = _installed(tmp_path)
    _page(host, "cves/2024/05/cve-2024-0001", date_added="2024-05-03")
    # a day-resharded page: valid under reshard_by: day, reverted to 2024/05/ without it
    _page(host, "cves/2024/05/03/cve-2024-0002", date_added="2024-05-03", published="2024-05-03")
    _set_pack_partitioning(pack, NEW)
    before = (host / "schema.yaml").read_bytes()

    assert install.main([str(host), str(pack), "--refresh", "--apply"]) == 1

    out = capsys.readouterr().out
    assert "would MOVE 1 of 2 page(s)" in out, out
    assert "cves/2024/05/03/cve-2024-0002 -> cves/2024/05/cve-2024-0002" in out, out
    assert (host / "schema.yaml").read_bytes() == before, "nothing written on refusal"
    assert (host / "wiki" / "cves" / "2024" / "05" / "03" / "cve-2024-0002.md").is_file()


def test_foreign_namespace_with_different_partitioning_still_fails_preflight(tmp_path, capsys):
    host = _host(tmp_path)
    (host / "schema.yaml").write_text((host / "schema.yaml").read_text()
                                      + "    cves: {strategy: by-letter}\n")
    (host / "wiki" / "cves").mkdir()
    _page(host, "cves/c/cve-2024-0003")
    pack = _pack(tmp_path, NEW)              # never installed: no manifest, no co-install marker
    before = (host / "schema.yaml").read_bytes()

    assert install.main([str(host), str(pack), "--refresh", "--apply"]) == 1

    out = capsys.readouterr().out
    assert "FAIL [namespaces] pack namespace 'wiki/cves/' already exists in host" in out, out
    assert (host / "schema.yaml").read_bytes() == before


def test_owned_by_another_pack_is_not_owned_by_this_one(tmp_path, capsys):
    """Another pack's manifest recording `cves` does not make it this pack's."""
    host, _ = _installed(tmp_path)
    other = _pack(tmp_path, NEW, name="okpack-other")
    assert install.main([str(host), str(other), "--refresh"]) == 1
    assert "already exists in host" in capsys.readouterr().out


def test_plain_reinstall_still_refuses_partitioning_drift(tmp_path, capsys):
    """Only --refresh carries the change; a non-refresh re-run keeps the explicit drift refusal."""
    host, pack = _installed(tmp_path)
    _set_pack_partitioning(pack, NEW)
    assert install.main([str(host), str(pack), "--apply"]) == 1
    assert "contract has drifted" in capsys.readouterr().out
    assert _host_cves(host) == OLD


# -- the proof and the rewrite in isolation ------------------------------------------------------
def test_proof_reports_unplaceable_new_strategy_as_a_failure(tmp_path, capsys):
    host, pack = _installed(tmp_path)
    _page(host, "cves/2024/05/cve-2024-0001", date_added="2024-05-03")
    _set_pack_partitioning(pack, {"strategy": "by-year"})
    assert install.main([str(host), str(pack), "--refresh"]) == 1
    assert "is not placeable" in capsys.readouterr().out


def test_unknown_strategy_is_refused_even_for_an_empty_namespace(tmp_path, capsys):
    host, pack = _installed(tmp_path)
    _set_pack_partitioning(pack, {"strategy": "by_date"})
    before = (host / "schema.yaml").read_bytes()
    assert install.main([str(host), str(pack), "--refresh", "--apply"]) == 1
    assert "unknown partition strategy 'by_date'" in capsys.readouterr().out
    assert (host / "schema.yaml").read_bytes() == before


def test_proof_ignores_tombstones_and_pages_without_frontmatter(tmp_path):
    host, _ = _installed(tmp_path)
    _page(host, "cves/2024/05/03/cve-old", date_added="2024-05-03", published="2024-05-03",
          status="tombstoned")
    (host / "wiki" / "cves" / "2024" / "05" / "03" / "notes.md").write_text("no frontmatter\n")
    checked, moved = refresh.moving_pages(host, "cves", NEW)
    assert (checked, moved) == (0, [])


def test_proof_names_the_old_and_new_seats(tmp_path):
    host, _ = _installed(tmp_path)
    _page(host, "cves/2024/05/03/cve-2024-0002", date_added="2024-05-03", published="2024-05-03")
    checked, moved = refresh.moving_pages(host, "cves", NEW)
    assert checked == 1
    assert moved == [("cves/2024/05/03/cve-2024-0002", "cves/2024/05/03/cve-2024-0002",
                      "cves/2024/05/cve-2024-0002")]


@pytest.mark.parametrize("layout", [
    "partitioning:\n  namespaces:\n    cves:\n      strategy: by-date\n      date_field: date_added\n"
    "      reshard_by: day\n    other: {strategy: flat}  # keep other\n",
    "partitioning:\n  namespaces:\n    cves: {strategy: by-date, date_field: date_added, reshard_by: day}"
    "  # keep cves\n    other: {strategy: flat}\n",
    "partitioning:\n  namespaces: {cves: {strategy: by-date, reshard_by: day}, other: {strategy: flat}}\n",
    "partitioning:\n  namespaces:\n    cves:\n      strategy: by-type\n      sharded_types:\n"
    "      - cve\n    other: {strategy: flat}\n",
])
def test_rewrite_replaces_only_the_named_entry(layout):
    text = "# head\n" + layout + "tail: 1  # keep tail\n"
    new_text = refresh.replace_partition_entry(text, "cves", NEW)
    parsed = yaml.safe_load(new_text)
    assert parsed["partitioning"]["namespaces"]["cves"] == NEW
    assert parsed["partitioning"]["namespaces"]["other"] == {"strategy": "flat"}
    assert parsed["tail"] == 1 and "# keep tail" in new_text and new_text.startswith("# head")


def test_rewrite_that_fails_to_round_trip_raises(monkeypatch):
    text = "partitioning:\n  namespaces:\n    cves: {strategy: flat}\n"
    real = yaml.safe_load
    calls = []

    def corrupt(value):
        calls.append(value)
        parsed = real(value)
        if len(calls) == 2:
            parsed["partitioning"]["namespaces"]["cves"] = {"strategy": "wrong"}
        return parsed

    monkeypatch.setattr(refresh.yaml, "safe_load", corrupt)
    with pytest.raises(ValueError, match="did not round-trip"):
        refresh.replace_partition_entry(text, "cves", NEW)


def test_drain_target_is_the_build_map_rule(tmp_path):
    """build_map and the refresh proof share one placement rule: a page the mover would move is
    exactly a page whose drain_target is set."""
    host, _ = _installed(tmp_path)
    _page(host, "cves/cve-2024-0009", date_added="2024-06-01")             # flat -> moves
    _page(host, "cves/2024/05/cve-2024-0001", date_added="2024-05-03")     # canonical
    _page(host, "cves/2024/05/03/cve-2024-0002", date_added="2024-05-03", published="2024-05-03")
    move_map, _ = okf.build_map(host, "cves")
    assert move_map == {"cves/cve-2024-0009": "cves/2024/06/cve-2024-0009"}
    cfg = okf._partition_cfg(okf._governing_schema(host, "cves"), "cves")
    targets = {cur: okf.drain_target("cves", cur, slug, fm, cfg, set())
               for cur, slug, fm in okf.placeable_pages(host, "cves")}
    assert {k: v for k, v in targets.items() if v} == move_map


# -- mutation-hardening: the planner's branches, the rewrite's exact output, the enumeration -------
class _Plan:
    def __init__(self):
        self.steps, self.fails = [], []

    def step(self, desc, thunk=None):
        self.steps.append(desc)

    def fail(self, msg):
        self.fails.append(msg)


def _planner_case(tmp_path, host_part: dict, pack_part: dict, owns: list[str]):
    host, pack = tmp_path / "h", tmp_path / "p"
    (host / "wiki").mkdir(parents=True)
    pack.mkdir()
    (host / "schema.yaml").write_text(yaml.safe_dump({"partitioning": {"namespaces": host_part}}))
    (pack / "schema.yaml").write_text(yaml.safe_dump({"partitioning": {"namespaces": pack_part}}))
    (pack / "pack.yaml").write_text(yaml.safe_dump({"name": "okpack-x", "owns": {"namespaces": owns}}))
    return host, pack


def _plan(host, pack, owned=(), legacy=False):
    plan = _Plan()
    refresh.plan_owned_partitioning(host, pack, plan, owned=set(owned), legacy_owned=legacy,
                                    pack_label="okpack-x")
    return plan


def test_planner_skips_undeclared_equal_and_pack_silent_namespaces(tmp_path):
    host, pack = _planner_case(
        tmp_path,
        {"same": {"strategy": "flat"}, "hostonly": {"strategy": "flat"}, "drift": {"strategy": "flat"}},
        {"same": {"strategy": "flat"}, "packonly": {"strategy": "flat"}, "drift": {"strategy": "by-letter"}},
        ["same", "hostonly", "packonly", "drift"])
    plan = _plan(host, pack, owned=["same", "hostonly", "packonly", "drift"])
    assert plan.fails == []
    assert [s.split("'")[1] for s in plan.steps] == ["drift"], plan.steps


def test_planner_ownership_is_manifest_or_legacy_marker(tmp_path):
    host, pack = _planner_case(tmp_path, {"a": {"strategy": "flat"}, "b": {"strategy": "flat"}},
                               {"a": {"strategy": "by-letter"}, "b": {"strategy": "by-letter"}},
                               ["a", "b"])
    unowned = _plan(host, pack)
    assert len(unowned.fails) == 2 and unowned.steps == []
    assert "'a' partitioning differs from okpack-x's but the namespace is not recorded" in unowned.fails[0]
    manifest = _plan(host, pack, owned=["b"])
    assert len(manifest.fails) == 1 and "'a'" in manifest.fails[0]
    assert len(manifest.steps) == 1 and "'b'" in manifest.steps[0]
    legacy = _plan(host, pack, legacy=True)
    assert legacy.fails == [] and len(legacy.steps) == 2


def test_planner_keeps_going_after_each_refusal(tmp_path):
    """Every refusal is per-namespace: a later namespace is still evaluated and planned."""
    host, pack = _planner_case(
        tmp_path,
        {"a1": {"strategy": "flat"}, "a2": {"strategy": "flat"}, "a3": {"strategy": "flat"},
         "z": {"strategy": "flat"}},
        {"a1": {"strategy": "by-letter"}, "a2": {"strategy": "bogus"}, "a3": {"strategy": "by-letter"},
         "z": {"strategy": "by-letter"}},
        ["a1", "a2", "a3", "z"])
    page = host / "wiki" / "a3" / "moved.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: x\nid: moved\n---\n")
    plan = _plan(host, pack, owned=["a2", "a3", "z"])
    assert [f.split("'")[1] for f in plan.fails] == ["a1", "a2", "a3"], plan.fails
    assert [s.split("'")[1] for s in plan.steps] == ["z"], plan.steps


def test_planner_tolerates_malformed_pack_yaml(tmp_path):
    host, pack = _planner_case(tmp_path, {"a": {"strategy": "flat"}}, {"a": {"strategy": "by-letter"}},
                               ["a"])
    (pack / "pack.yaml").write_text("owns: [unclosed\n")
    plan = _plan(host, pack, owned=["a"])
    assert plan.steps == [] and plan.fails == []


def test_proof_detects_a_move_whichever_way_the_paths_sort(tmp_path):
    """flat -> by-letter moves `a/cve-x` DOWN a level; the new seat sorts before the old one."""
    host, _ = _planner_case(tmp_path, {"cves": {"strategy": "flat"}}, {}, [])
    _page(host, "cves/cve-x")
    checked, moved = refresh.moving_pages(host, "cves", {"strategy": "by-letter"})
    assert (checked, moved) == (1, [("cves/cve-x", "cves/cve-x", "cves/c/cve-x")])


def test_proof_equal_but_separately_computed_seats_are_not_a_move(tmp_path):
    """A misplaced page the drain files to the same seat under both configs does not move BECAUSE
    of the change -- its seats are equal strings built independently, not the same object."""
    host, _ = _planner_case(tmp_path, {"cves": OLD}, {}, [])
    _page(host, "cves/cve-2024-0009", date_added="2024-06-01")
    assert refresh.moving_pages(host, "cves", NEW) == (1, [])


def test_proof_works_when_the_governing_schema_declares_no_types(tmp_path):
    host, _ = _planner_case(tmp_path, {"cves": OLD}, {}, [])
    assert "types" not in yaml.safe_load((host / "schema.yaml").read_text())
    assert refresh.moving_pages(host, "cves", NEW) == (0, [])


def test_refusal_names_at_most_five_examples(tmp_path, capsys):
    host, pack = _installed(tmp_path)
    for n in range(7):
        _page(host, f"cves/2024/05/03/cve-2024-100{n}", date_added="2024-05-03", published="2024-05-03")
    _set_pack_partitioning(pack, NEW)
    assert install.main([str(host), str(pack), "--refresh"]) == 1
    out = capsys.readouterr().out
    assert "would MOVE 7 of 7 page(s)" in out
    assert "cve-2024-1004 -> " in out and "cve-2024-1005 -> " not in out, out


def test_rewrite_is_one_line_in_declared_key_order_at_the_original_indent():
    value = {"strategy": "by-type", "sharded_types": ["alpha-type", "beta-type", "gamma-type", "delta"],
             "date_field": "date_added"}
    text = ("partitioning:\n  alpha: 1\n  reshard_over: 500\n  namespaces:\n"
            "    zzz: {strategy: flat}\n    aaa: {strategy: flat}\n"
            "    cves:\n      strategy: by-date\n      sharded_types:\n      - one\n      - two\n"
            "    other: {strategy: flat}\n")
    new_text = refresh.replace_partition_entry(text, "cves", value)
    assert ("    cves: {strategy: by-type, sharded_types: [alpha-type, beta-type, gamma-type, delta], "
            "date_field: date_added}\n    other:") in new_text, new_text
    assert new_text.startswith("partitioning:\n  alpha: 1\n  reshard_over: 500\n  namespaces:\n"
                               "    zzz: {strategy: flat}\n    aaa: {strategy: flat}\n"), new_text


def test_rewrite_in_a_flow_namespaces_mapping_picks_the_named_key():
    text = ("partitioning:\n  alpha: 1\n  reshard_over: 500\n"
            "  namespaces: {zzz: {strategy: flat}, aaa: {strategy: flat}, cves: {strategy: flat}}\n")
    new_text = refresh.replace_partition_entry(text, "cves", NEW)
    assert new_text.endswith(
        "{zzz: {strategy: flat}, aaa: {strategy: flat}, cves: {strategy: by-date, date_field: date_added}}\n")


def test_placeable_pages_skips_reserved_unreadable_and_retired_but_keeps_going(tmp_path):
    root = tmp_path
    ns = root / "wiki" / "cves"
    ns.mkdir(parents=True)
    live = "---\ntype: cve\nid: x\n---\n"
    for name in ("_draft.md", "index.md", "INDEX.md", "log.md", "README.md", "INDEX-p2.md"):
        (ns / name).write_text(live)
    (ns / "a0-dir.md").mkdir()                                         # read_text -> OSError
    (ns / "a1-nofm.md").write_text("no frontmatter\n")
    (ns / "a2-badyaml.md").write_text("---\n: [unclosed\n---\n")
    (ns / "a3-list.md").write_text("---\n- a\n- b\n---\n")
    (ns / "a4-tomb.md").write_text("---\ntype: cve\nid: t\nstatus: tombstoned\n---\n")
    (ns / "z-live.md").write_text(live)
    assert [cur for cur, _, _ in okf.placeable_pages(root, "cves")] == ["cves/z-live"]


def test_refresh_without_a_readable_host_schema_plans_no_partitioning_change(tmp_path):
    """install-domain's preflight refuses an unreadable host schema long before this point, so the
    refresh corridor's own guard is defensive: with no schema to read it must treat the pack as
    not legacy-owned and plan no partitioning step, never raise."""
    host, pack = _installed(tmp_path)
    _set_pack_partitioning(pack, NEW)
    (host / "schema.yaml").unlink()
    plan = _Plan()

    install.install_taxonomy(host, pack, "vulnx", plan, refresh=True, manifest={})

    assert not [s for s in plan.steps if "partitioning" in str(s)], plan.steps
    assert not plan.fails, plan.fails
