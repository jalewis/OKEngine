"""okengine#994 [19]: the legacy-shape repair lane (scripts/cron/repair_legacy_shapes.py).

A `no_agent` maintainer that rewrites legacy frontmatter shapes to the write path's canonical shape.
It bypasses the MCP server, never its invariants: reshape only, attestation and operator fields
byte for byte, immutable fields only to the same value, idempotent, refused on a pre-fix runtime.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "repair_legacy_shapes.py"
FRAMEWORK = REPO / "scripts" / "framework_audit_repair.py"

_SCHEMA = "okf:\n  required: [type]\nstrict_types: false\n"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load(SCRIPT, "repair_legacy_shapes")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "dep"
    (root / "wiki").mkdir(parents=True)
    (root / ".hermes-data").mkdir()
    (root / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (root / ".hermes-data" / "engine-runtime.yaml").write_text("engine_release: v0.15.7\n",
                                                               encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-gateway-home"))
    monkeypatch.setenv("WIKI_PATH", str(root))
    return root


def _page(root: Path, rel: str, frontmatter: str, body: str = "Body.\n") -> Path:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return path


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def _plan(root: Path, *classes: str) -> dict:
    code, out, err = _run(["--vault", str(root), "--json",
                           *(["--classes", ",".join(classes)] if classes else [])])
    assert code == 0, err
    return json.loads(out)


_LEGACY = """\
type: actor
id: G1
title: "X"   # kept as written
discovered_by: pack-a
tags: a, b
successor_hint: '[[entities/y/y]]'
related:
- '[[entities/z/z]]'
- plain
"""


# --- the two classes ------------------------------------------------------------------------

def test_a_legacy_scalar_list_field_is_rewritten_to_the_write_paths_list(vault):
    page = _page(vault, "entities/x/x.md", _LEGACY)
    assert _plan(vault, "scalars")["by_field"] == {"scalars": {"discovered_by": 1, "tags": 1}}
    code, _out, err = _run(["--vault", str(vault), "--classes", "scalars", "--apply"])
    assert code == 0, err
    fm = _fm(page)
    assert fm["discovered_by"] == ["pack-a"] and fm["tags"] == ["a", "b"]
    text = page.read_text(encoding="utf-8")
    assert 'title: "X"   # kept as written\n' in text, "an untouched line must keep its bytes"
    assert "successor_hint: '[[entities/y/y]]'\n" in text, "the other class is not touched"


#: Live deployment `basis` values (a predictions list field), verbatim (okengine#1005).
_LIVE_BASIS = {
    "jadepuffer": "JadePuffer (first fully autonomous AI ransomware, week of 2026-06-30) follows "
                  "historical malware commoditization patterns. Chinese open-weight LLMs (Seed2.0, "
                  "GLM 5.2) erase model-capability moat. Tooling commoditization typically follows "
                  "initial disclosure within weeks.",
    "nspm-12": "White House NSPM-12 signed June 29, 2026 mandating NSS cybersecurity governance "
               "reform; structural fragmentation across NSS agencies; personal-accountability "
               "incentives for compliance; historical NSPM governance precedents",
    "oracle": "CISA KEV catalog + public breach disclosures; Oracle PeopleSoft/EBS active "
              "exploitation pattern observed June 2026 (Nissan, Oracle Health); structural factors "
              "favor attacker replication",
    "swiss": "Swiss media report June 30, 2026: federal cybersecurity positions at 120K+ CHF "
             "receiving zero applicants; structural barriers (language, citizenship, cost of "
             "living); NIS2-equivalent regulatory demand shock; AI skills premium widening gap",
}


@pytest.mark.parametrize("name", sorted(_LIVE_BASIS))
def test_a_prose_scalar_is_wrapped_whole_never_split_on_its_commas(vault, name):
    """okengine#1005: the plan must never shred a prose list value into comma fragments."""
    (vault / "schema.yaml").write_text(_SCHEMA + "field_shapes:\n  basis: list\n", encoding="utf-8")
    basis = _LIVE_BASIS[name]
    page = _page(vault, f"predictions/{name}.md", f"type: prediction\nbasis: {json.dumps(basis)}\n")
    write_path = mod.load_write_path(vault)
    planned = mod.plan_page(write_path, vault / "wiki", page, mod.CLASSES)
    assert planned["repairs"] == {"basis": (basis, [basis], "scalars")}
    assert _run(["--vault", str(vault), "--classes", "scalars", "--apply"])[0] == 0
    assert _fm(page)["basis"] == [basis]


def test_a_comma_joined_token_list_is_still_split(vault):
    tags = "vulnerability, rce, kemp-loadmaster, uninitialized heap, pre-authentication exploit"
    page = _page(vault, "sources/s.md", f"type: source\ntags: '{tags}'\n")
    assert _run(["--vault", str(vault), "--classes", "scalars", "--apply"])[0] == 0
    assert _fm(page)["tags"] == ["vulnerability", "rce", "kemp-loadmaster", "uninitialized heap",
                                 "pre-authentication exploit"]


def test_a_comma_split_of_prose_is_a_data_change(vault):
    """The reshape-only proof reads a scalar's items with the shared rule: a canonical form that
    cuts an item inside brackets (a stale write path) is skipped as a data change, never applied.
    The whole string as one item (a `prose-list` field) is the same data."""
    old = "a (b, c) d"
    assert not mod.reshape_only(old, ["a (b", "c) d"])
    assert mod.reshape_only(old, [old])
    assert mod.reshape_only("x, y", ["x", "y"])
    assert mod.reshape_only("x, y", ["x, y"])
    assert not mod.reshape_only("x, y", ["x"])
    assert not mod.reshape_only("x, y", "x")


def test_a_declared_prose_list_scalar_is_wrapped_whole(vault):
    """A `prose-list` field's scalar, short clauses and all, is repaired to one item."""
    (vault / "schema.yaml").write_text(_SCHEMA + "field_shapes:\n  assumptions: prose-list\n",
                                       encoding="utf-8")
    value = "the vendor patches, and attackers move on"
    page = _page(vault, "assessments/a.md", f"type: assessment\nassumptions: '{value}'\n")
    assert _run(["--vault", str(vault), "--classes", "scalars", "--apply"])[0] == 0
    assert _fm(page)["assumptions"] == [value]


def test_qa_reference_and_alias_lists_still_split(vault):
    paths = ["sources/2026/07/06/skillcloak-evasion-of-llm-agent-skill-scanners-via-encoded-"
             "payloads-hkust",
             "sources/2026/07/06/trojpix-pixel-level-trojan-triggers-in-multimodal-models-"
             "shandong-university"]
    page = _page(vault, "entities/a/a.md",
                 f"type: actor\nsources: '{', '.join(paths)}'\naliases: 'Volexity, Volexity Inc.'\n")
    assert _run(["--vault", str(vault), "--classes", "scalars", "--apply"])[0] == 0
    assert _fm(page)["sources"] == paths
    assert _fm(page)["aliases"] == ["Volexity", "Volexity Inc."]


def test_a_wikilink_value_is_rewritten_to_the_plain_path(vault):
    page = _page(vault, "entities/x/x.md", _LEGACY + "mapped: [[entities/w/w]]\n")
    assert _plan(vault, "wikilinks")["by_field"] == {
        "wikilinks": {"mapped": 1, "related": 1, "successor_hint": 1}}
    assert _run(["--vault", str(vault), "--classes", "wikilinks", "--apply"])[0] == 0
    fm = _fm(page)
    assert fm["successor_hint"] == "entities/y/y"
    assert fm["related"] == ["entities/z/z", "plain"]
    assert fm["mapped"] == ["entities/w/w"], "a YAML-mangled nested list is flattened"
    assert fm["tags"] == "a, b", "the scalar class is not touched"


def test_the_new_value_is_the_write_paths_own_canonical_form(vault):
    """Round trip: `_coerce_fm` of the stored value IS the repaired value, and is stable."""
    page = _page(vault, "entities/x/x.md", _LEGACY)
    before = _fm(page)
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    after = _fm(page)
    write_path = mod.load_write_path(vault)
    for key in ("discovered_by", "tags", "successor_hint", "related"):
        assert mod.same(write_path._coerce_fm({key: before[key]}, page)[key], after[key]), key
        assert mod.same(write_path._coerce_fm({key: after[key]}, page)[key], after[key]), key


def test_a_second_plan_is_empty_and_verify_tracks_the_remaining_work(vault):
    _page(vault, "entities/x/x.md", _LEGACY)
    assert _run(["--vault", str(vault), "--verify"])[0] == 1, "verify fails while work remains"
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert _plan(vault)["repair_pages"] == 0
    assert _run(["--vault", str(vault), "--verify"])[0] == 0


def test_the_plan_counts_per_class_page_and_field(vault):
    _page(vault, "entities/x/x.md", _LEGACY)
    _page(vault, "concepts/c.md", "type: concept\ntags: p, q\n")
    summary = _plan(vault)
    assert summary["pages"] == {"scalars": 2, "wikilinks": 1}
    assert summary["fields"] == {"scalars": 3, "wikilinks": 2}
    assert summary["by_field"]["scalars"] == {"discovered_by": 1, "tags": 2}
    assert summary["deployment"] == str(vault.resolve()) and summary["mode"] == "plan"
    assert _plan(vault, "scalars")["deferred"] == {"wikilinks": 2}


# --- negative fixtures: what the lane must never rewrite --------------------------------------

def test_an_attested_page_is_skipped_whole_and_left_byte_for_byte(vault):
    page = _page(vault, "sources/s.md", "type: source\nlocal_only: true\ntags: a, b\n"
                 "publisher: '[[entities/p/p]]'\n")
    before = page.read_bytes()
    assert _plan(vault)["skipped"] == {"attested-page": 2}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == before


def test_an_operator_owned_field_is_skipped_and_its_bytes_kept(vault):
    page = _page(vault, "entities/x/x.md",
                 "type: actor\noperator_evidence_refs: '[[sources/s]]'\ntags: a, b\n")
    assert _plan(vault)["skipped"] == {"operator-owned": 1}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert "operator_evidence_refs: '[[sources/s]]'\n" in page.read_text(encoding="utf-8")
    assert _fm(page)["tags"] == ["a", "b"]


@pytest.mark.parametrize("frontmatter", [
    "type: actor\ncreated_by: '[[pack-a|Pack A]]'\n",   # immutable; display text would be lost
    "type: actor\nid: '[[G1#current]]'\n",              # immutable; anchor would be lost
    "type: actor\nrelated: ['[[a]]', a]\n",             # a duplicate item would collapse
    "type: actor\naliases: ['[[a|Alpha]]']\n",          # display text would be lost
])
def test_a_canonical_form_that_differs_in_data_is_skipped(vault, frontmatter):
    page = _page(vault, "entities/x/x.md", frontmatter)
    before = page.read_bytes()
    assert _plan(vault)["skipped"] == {"data-change": 1}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == before


def test_an_immutable_field_gets_only_the_canonical_shape_of_the_same_value(vault):
    page = _page(vault, "entities/x/x.md", "type: actor\nid: G1\ndiscovered_by: pack-a\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert _fm(page) == {"type": "actor", "id": "G1", "discovered_by": ["pack-a"]}


@pytest.mark.parametrize("rel,frontmatter", [
    ("entities/x/x.md", "type: actor\nstatus: tombstoned\ntags: a, b\n"),
    ("entities/INDEX.md", "type: index\ntags: a, b\n"),
])
def test_tombstoned_and_reserved_pages_are_not_touched(vault, rel, frontmatter):
    page = _page(vault, rel, frontmatter)
    before = page.read_bytes()
    assert _plan(vault)["repair_pages"] == 0
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == before


def test_a_comment_inside_the_value_is_not_dropped(vault):
    page = _page(vault, "entities/x/x.md", "type: actor\nrelated:\n- '[[a]]'  # why\n- b\n")
    before = page.read_bytes()
    assert _plan(vault)["skipped"] == {"comment": 1}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == before


def test_a_value_inherited_through_a_merge_key_is_not_located_and_skipped(vault):
    page = _page(vault, "entities/x/x.md", "type: actor\nbase: &b\n  tags: a, b\n<<: *b\n")
    before = page.read_bytes()
    assert _plan(vault)["skipped"] == {"unlocated": 1}
    _run(["--vault", str(vault), "--apply"])
    assert page.read_bytes() == before


def test_a_rewrite_that_would_break_an_alias_fails_verification_and_is_skipped(vault):
    page = _page(vault, "entities/x/x.md",
                 "type: actor\nsuccessor_hint: &v '[[entities/p/p]]'\nmeta: {ref: *v}\n")
    before = page.read_bytes()
    assert _plan(vault)["skipped"] == {"verification": 1}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == before


def test_a_page_with_no_frontmatter_or_bad_yaml_is_ignored(vault):
    _page(vault, "entities/x/bad.md", "type: [unclosed\n")
    (vault / "wiki" / "entities" / "x" / "plain.md").write_text("no frontmatter\n")
    (vault / "wiki" / "entities" / "x" / "bin.md").write_bytes(b"---\ntags: \xff\n---\n")
    assert _plan(vault)["repair_pages"] == 0
    write_path = mod.load_write_path(vault)
    page = vault / "wiki" / "entities" / "x" / "bin.md"
    assert mod.plan_page(write_path, vault / "wiki", page, mod.CLASSES)["page_skip"] == "unreadable"


# --- apply guards -----------------------------------------------------------------------------

@pytest.mark.parametrize("stamp", [None, "engine_release: v0.15.6\n", "engine_release: soon\n",
                                   "- not a mapping\n", "engine_release: [\n"])
def test_apply_is_refused_unless_the_runtime_runs_the_fix(vault, stamp):
    page = _page(vault, "entities/x/x.md", _LEGACY)
    before = page.read_bytes()
    runtime = vault / ".hermes-data" / "engine-runtime.yaml"
    if stamp is None:
        runtime.unlink()
    else:
        runtime.write_text(stamp, encoding="utf-8")
    code, _out, err = _run(["--vault", str(vault), "--apply"])
    assert code == 1 and "refusing --apply" in err
    assert page.read_bytes() == before
    assert _run(["--vault", str(vault)])[0] == 0, "a dry run needs no stamp"


def test_the_gateway_home_stamp_is_read_inside_a_container(vault, tmp_path, monkeypatch):
    (vault / ".hermes-data" / "engine-runtime.yaml").unlink()
    home = tmp_path / "opt-data"
    home.mkdir()
    (home / "engine-runtime.yaml").write_text("engine_release: v0.16.0-rc1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert mod.runtime_release(vault) == (0, 16, 0)


def test_apply_journals_the_pages_it_writes_in_one_corpus_transaction(vault):
    page = _page(vault, "entities/x/x.md", _LEGACY)
    _page(vault, "entities/y/y.md", "type: actor\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    journal = (vault / ".okengine" / "corpus" / "journal.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in journal.splitlines()]
    assert [(r["writer"], r["status"]) for r in records] == [("repair_legacy_shapes", "committed")]
    changed = {c["path"] for c in records[0]["affected_paths"]}
    assert changed == {page.relative_to(vault).as_posix()}


def test_a_held_corpus_lock_is_reported_not_raised(vault, monkeypatch):
    _page(vault, "entities/x/x.md", _LEGACY)

    def held(*_a, **_k):
        raise mod.corpus_transaction.CorpusLockTimeout("held by a gateway write")

    monkeypatch.setattr(mod.corpus_transaction, "mutation", held)
    code, _out, err = _run(["--vault", str(vault), "--apply"])
    assert code == 1 and "held by a gateway write" in err


def test_a_write_path_that_predates_the_fix_or_is_missing_is_refused(vault, tmp_path, monkeypatch):
    stale = tmp_path / "stale-mcp"
    stale.mkdir()
    (stale / "write_server.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_engine_dirs", lambda: [tmp_path / "absent", stale])
    code, _out, err = _run(["--vault", str(vault)])
    assert code == 1 and "predates okengine#994" in err
    monkeypatch.setattr(mod, "_engine_dirs", lambda: [tmp_path / "absent"])
    code, _out, err = _run(["--vault", str(vault)])
    assert code == 1 and "cannot locate" in err


# --- helpers and CLI ----------------------------------------------------------------------------

class _FakeWritePath:
    def __init__(self, results):
        self.results = list(results)

    def _coerce_fm(self, fm, _page):
        return self.results.pop(0)


def test_canonical_is_none_when_the_write_path_refuses_or_never_settles(tmp_path):
    assert mod.canonical(_FakeWritePath([None]), tmp_path, "k", "v") is None
    flip = _FakeWritePath([{"k": [1]}, {"k": [2]}, {"k": [3]}, {"k": [4]}])
    assert mod.canonical(flip, tmp_path, "k", "v") is None
    assert mod.canonical(_FakeWritePath([{"k": ["v"]}, {"k": ["v"]}]), tmp_path, "k", "v") == ["v"]


def test_an_unstable_canonical_form_is_skipped(vault, monkeypatch):
    _page(vault, "entities/x/x.md", "type: actor\ntags: a, b\n")
    monkeypatch.setattr(mod, "canonical",
                        lambda _w, _p, key, value: None if key == "tags" else value)
    assert _plan(vault)["skipped"] == {"unstable-canonical": 1}


@pytest.mark.parametrize(("old", "new", "expected"), [
    ("a, b", ["a", "b"], True),
    ("a,, b ,", ["a", "b"], True),
    ("[[x]], y", ["x", "y"], True),
    ("[[x]]", "x", True),
    ("a, b", ["a"], False),
    ("a", "b", False),
    (["[[x]]"], "x", False),
    ("a, b", "a, b", True),
    ([1, "[[x]]"], [1, "x"], True),
    ([True], [1], False),
])
def test_reshape_only_accepts_exactly_the_same_items(old, new, expected):
    assert mod.reshape_only(old, new) is expected


def test_same_is_type_strict_and_ignores_yaml_sharing():
    shared = {"k": 1}
    assert mod.same([shared, shared], [{"k": 1}, {"k": 1}])
    assert not mod.same(0, False) and not mod.same("1", 1)
    assert mod.same({"b": 1, "a": 2}, {"a": 2, "b": 1})
    assert mod.same({None: 1, "a": [2]}, {"a": [2], None: 1}), "mixed key types still compare"
    assert not mod.same({1: "x"}, {"1": "x"})
    assert not mod.same({"!map": []}, {}), "a key named like the tag is not the empty mapping"
    import datetime
    assert mod.same(datetime.date(2026, 1, 1), datetime.date(2026, 1, 1))


def test_a_non_mapping_frontmatter_has_no_spans():
    assert mod._spans("- a\n- b\n") == {}


def test_list_mode_prints_every_planned_and_skipped_field(vault):
    _page(vault, "entities/x/x.md", _LEGACY + "aliases: ['[[a|A]]']\n")
    code, out, _err = _run(["--vault", str(vault), "--list"])
    assert code == 0
    assert "WOULD REPAIR [scalars] entities/x/x.md tags: 'a, b' -> ['a', 'b']" in out
    assert "SKIP [data-change] entities/x/x.md aliases" in out
    assert "legacy-shape-repair (plan, scalars,wikilinks): 1 page(s)" in out
    code, out, _err = _run(["--vault", str(vault), "--list", "--apply"])
    assert "REPAIRED [scalars]" in out


@pytest.mark.parametrize("bad", ["", "scalars,bogus", " , "])
def test_an_unknown_class_is_a_usage_error(vault, bad):
    with pytest.raises(SystemExit):
        _run(["--vault", str(vault), "--classes", bad])


def test_apply_and_verify_are_exclusive(vault):
    with pytest.raises(SystemExit):
        _run(["--vault", str(vault), "--apply", "--verify"])


# --- through `framework repair plan|apply|verify` ------------------------------------------------

def _framework():
    return _load(FRAMEWORK, "framework_audit_repair_994")


def _call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def _audit(fw, root: Path) -> str:
    fw._atomic_new(root / ".okengine/operations/runs/audit-fixture.json",
                   {"api": 1, "kind": "audit", "run_id": "audit-fixture"})
    return "audit-fixture"


def test_framework_plan_records_the_dry_run_and_apply_verify_repair_the_class(vault):
    fw = _framework()
    page = _page(vault, "entities/x/x.md", _LEGACY)
    code, out, err = _call(fw.repair, ["plan", str(vault), "--from-audit", _audit(fw, vault),
                                      "--repairs", "legacy-scalars", "--json"])
    assert code == 0, err
    plan = json.loads(out)
    assert plan["dry_runs"][0]["repair"] == "legacy-scalars"
    assert "legacy-shape-repair (plan, scalars): 1 page(s)" in plan["dry_runs"][0]["stdout"]
    assert _fm(page)["tags"] == "a, b", "planning writes nothing"
    assert _call(fw.repair, ["apply", str(vault), "--plan", plan["plan_id"]])[0] == 0
    assert _fm(page)["tags"] == ["a", "b"]
    assert _fm(page)["successor_hint"] == "[[entities/y/y]]", "only the planned class"
    assert _call(fw.repair, ["verify", str(vault), "--plan", plan["plan_id"]])[0] == 0


def test_framework_apply_refuses_when_the_wiki_changed_since_the_plan(vault):
    fw = _framework()
    page = _page(vault, "entities/x/x.md", _LEGACY)
    _, out, _ = _call(fw.repair, ["plan", str(vault), "--from-audit", _audit(fw, vault),
                                 "--repairs", "legacy-wikilinks", "--json"])
    plan = json.loads(out)
    page.write_text(page.read_text(encoding="utf-8") + "More.\n", encoding="utf-8")
    code, _, err = _call(fw.repair, ["apply", str(vault), "--plan", plan["plan_id"]])
    assert code == 1 and "changed since plan" in err
    assert _fm(page)["successor_hint"] == "[[entities/y/y]]"


def test_framework_verify_fails_while_a_planned_repair_remains(vault, monkeypatch):
    fw = _framework()
    page = _page(vault, "entities/x/x.md", _LEGACY)
    _, out, _ = _call(fw.repair, ["plan", str(vault), "--from-audit", _audit(fw, vault),
                                 "--repairs", "legacy-scalars", "--json"])
    plan = json.loads(out)
    assert _call(fw.repair, ["apply", str(vault), "--plan", plan["plan_id"]])[0] == 0
    page.write_text(page.read_text(encoding="utf-8").replace("- pack-a", "") .replace(
        "discovered_by:\n", "discovered_by: pack-a\n"), encoding="utf-8")
    assert _call(fw.repair, ["verify", str(vault), "--plan", plan["plan_id"]])[0] == 1


def test_framework_plan_is_not_written_when_a_dry_run_fails(vault):
    fw = _framework()
    (vault / ".hermes-data" / "engine-runtime.yaml").unlink()
    fw.REPAIRS["broken"] = ("repair_legacy_shapes.py", "--vault", ("--classes", "bogus"), None)
    try:
        code, _, err = _call(fw.repair, ["plan", str(vault), "--from-audit", _audit(fw, vault),
                                        "--repairs", "broken"])
    finally:
        del fw.REPAIRS["broken"]
    assert code == 1 and "dry run failed for: broken" in err
    assert not (vault / ".okengine/operations/plans").exists()


def test_framework_plan_refuses_when_the_wiki_moves_during_the_dry_run(vault, monkeypatch):
    fw = _framework()
    page = _page(vault, "entities/x/x.md", _LEGACY)

    def moving(script, deployment, extra=None):
        page.write_text("---\ntype: actor\n---\nchanged\n", encoding="utf-8")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(fw, "_run", moving)
    code, _, err = _call(fw.repair, ["plan", str(vault), "--from-audit", _audit(fw, vault),
                                    "--repairs", "legacy-scalars"])
    assert code == 1 and "changed during the dry run" in err


def test_framework_argv_carries_the_class_and_the_phase_flag(vault):
    fw = _framework()
    assert fw._args("legacy-wikilinks", vault) == [
        "--vault", str(vault), "--classes", "wikilinks"]
    assert fw._args("legacy-wikilinks", vault, "apply")[-1] == "--apply"
    assert fw._args("legacy-wikilinks", vault, "verify")[-1] == "--verify"
    assert fw._args("malformed-slugs", vault, "verify") == ["--vault", str(vault)]


def test_non_string_keys_and_md_directories_are_ignored(vault):
    page = _page(vault, "entities/x/x.md", "type: actor\n1: '[[entities/q/q]]'\ntags: a, b\n")
    (vault / "wiki" / "entities" / "odd.md").mkdir()
    assert _plan(vault)["by_field"] == {"scalars": {"tags": 1}}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert "1: '[[entities/q/q]]'\n" in page.read_text(encoding="utf-8")


_STORED_TEXT = "---\ntype: actor\ntags: a\n---\nbody\n"
_EDIT = [(len("---\ntype: actor\n"), len("---\ntype: actor\ntags: a"), "tags:\n- a")]


@pytest.mark.parametrize("text", [
    "---\n- a\n---\nbody\n",             # no longer a mapping
    "---\ntags: [a]\ntype: actor\n---\n",  # keys reordered
    "---\ntype: actor\n---\n",            # a key lost
    "---\ntype: other\ntags: [a]\n---\n",  # an untouched key changed
    "---\ntype: actor\ntags: [b]\n---\n",  # the repaired key holds another value
    "no frontmatter\n",
])
def test_verification_rejects_any_rewrite_but_the_planned_one(text):
    stored = {"type": "actor", "tags": "a"}
    repairs = {"tags": ("a", ["a"], "scalars")}
    assert mod._verified(stored, _STORED_TEXT, "---\ntype: actor\ntags:\n- a\n---\nbody\n",
                         repairs, _EDIT)
    assert not mod._verified(stored, text, text, repairs, [])


@pytest.mark.parametrize("new_text", [
    "---\ntype: actor\ntags:\n- a\n---\nbody changed\n",       # a body byte after the edit
    "---\ntype:  actor\ntags:\n- a\n---\nbody\n",              # a byte before the edit
    "---\r\ntype: actor\r\ntags:\n- a\r\n---\r\nbody\r\n",  # line endings translated
    "---\ntype: actor\ntags:\n- a\n---\nbody\n\n",            # a byte appended
])
def test_verification_rejects_a_byte_changed_outside_the_edits(new_text):
    """The CRLF bug's class: the frontmatter still parses to the planned values, but bytes outside
    the repaired keys changed. Verification compares those bytes, not just the parsed values."""
    assert not mod._verified({"type": "actor", "tags": "a"}, _STORED_TEXT, new_text,
                             {"tags": ("a", ["a"], "scalars")}, _EDIT)


def test_a_page_the_write_path_refuses_is_skipped(vault):
    """An unpaired surrogate is not text; the write path refuses the whole frontmatter."""
    page = _page(vault, "entities/x/x.md", 'type: actor\ntitle: "\\ud800"\ntags: a, b\n')
    before = page.read_bytes()
    write_path = mod.load_write_path(vault)
    planned = mod.plan_page(write_path, vault / "wiki", page, mod.CLASSES)
    assert planned["page_skip"] == "refused-by-write-path" and planned["repairs"] == {}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == before


# --- after apply, the write path sees no change (QA of !1278) -------------------------------------

_EVERY_SHAPE = """\
type: actor
id: G3000
title: Probe
aliases: APT1, Comment Crew
sources: sources/a, sources/missing
discovered_by: pack-a, pack-b
successor_hint: '[[entities/x/y]]'
related: ['[[entities/z/z]]', entities/w/w]
"""


@pytest.mark.parametrize("spelling", ["legacy", "repaired"])
def test_after_apply_an_update_echo_through_the_write_path_has_no_side_effect(vault, spelling):
    """The lane leaves pages the enforced write path agrees with: an echo of the page, in either
    its old legacy spelling or its repaired one, changes nothing else and is not refused."""
    (vault / "wiki" / "sources").mkdir()
    (vault / "wiki" / "sources" / "a.md").write_text("---\ntype: source\n---\n", encoding="utf-8")
    page = _page(vault, "entities/p/probe.md", _EVERY_SHAPE)
    legacy = _fm(page)
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    repaired = _fm(page)
    assert "rejected_aliases" not in repaired and "needs_review" not in repaired
    write_path = mod.load_write_path(vault)
    echo = legacy if spelling == "legacy" else repaired
    out = write_path._update("entities/p/probe", {**echo, "summary": "s"})
    assert out.startswith("updated") and "flagged for review" not in out, out
    after = _fm(page)
    assert "rejected_aliases" not in after and "needs_review" not in after, after
    assert {k: after[k] for k in repaired} == repaired, "the echo changed a repaired field"


# --- edges the mutation campaign measured -------------------------------------------------------

def test_a_non_mapping_frontmatter_is_ignored(vault):
    page = _page(vault, "entities/x/x.md", "- tags\n- a, b\n")
    write_path = mod.load_write_path(vault)
    planned = mod.plan_page(write_path, vault / "wiki", page, mod.CLASSES)
    assert planned["repairs"] == {} and planned["page_skip"] is None


@pytest.mark.parametrize("status", ["unknown", "verified", "active"])
def test_only_a_tombstone_status_skips_the_page(vault, status):
    page = _page(vault, "entities/x/x.md", f"type: actor\nstatus: {status}\ntags: a, b\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert _fm(page)["tags"] == ["a", "b"]


def test_a_heading_in_the_body_is_not_read_as_a_comment_in_the_frontmatter(vault):
    page = _page(vault, "entities/x/x.md", "type: actor\ntags: a, b\n", body="# H\n" * 60)
    assert _plan(vault)["skipped"] == {}
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert _fm(page)["tags"] == ["a", "b"]


def test_verification_rejects_an_added_key():
    text = "---\ntype: actor\ntags: [a]\nextra: 1\n---\n"
    assert not mod._verified({"type": "actor", "tags": "a"}, text, text,
                             {"tags": ("a", ["a"], "scalars")}, [])


def test_a_later_release_may_apply(vault):
    (vault / ".hermes-data" / "engine-runtime.yaml").write_text("engine_release: v0.16.0\n")
    page = _page(vault, "entities/x/x.md", "type: actor\ntags: a, b\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert _fm(page)["tags"] == ["a", "b"]


def test_a_page_with_only_skips_does_not_stop_the_pages_after_it(vault):
    first = _page(vault, "entities/a/a.md", "type: actor\naliases: ['[[a|A]]']\n")
    later = _page(vault, "entities/b/b.md", "type: actor\ntags: a, b\n")
    before = first.read_bytes()
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert first.read_bytes() == before and _fm(later)["tags"] == ["a", "b"]


def test_the_vault_defaults_to_wiki_path(vault, monkeypatch):
    page = _page(vault, "entities/x/x.md", "type: actor\ntags: a, b\n")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    assert mod.main(["--apply"]) == 0
    assert _fm(page)["tags"] == ["a", "b"]


def test_json_output_is_indented_and_key_sorted(vault):
    _page(vault, "entities/x/x.md", "type: actor\ntags: a, b\n")
    _code, out, _err = _run(["--vault", str(vault), "--json"])
    assert out.startswith('{\n  "at": ')
    assert list(json.loads(out)) == sorted(json.loads(out))


def test_non_ascii_items_are_written_as_text(vault):
    page = _page(vault, "entities/x/x.md", "type: actor\ntags: café, naïve\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert "- café\n- naïve\n" in page.read_text(encoding="utf-8")


def test_two_repairs_on_one_page_are_spliced_at_their_own_offsets(vault):
    page = _page(vault, "entities/x/x.md",
                 "type: actor\ntags: a, b\ntitle: T\nsuccessor_hint: '[[e/x/y]]'\nz: 1\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_text(encoding="utf-8").startswith(
        "---\ntype: actor\ntags:\n- a\n- b\ntitle: T\nsuccessor_hint: e/x/y\nz: 1\n---\n")


# --- line endings are kept byte for byte -------------------------------------------------------

def _raw_page(root: Path, rel: str, data: bytes) -> Path:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_crlf_page_keeps_every_byte_but_the_repaired_key(vault):
    """A CRLF file used to be rewritten whole as LF (read_text/write_text translate newlines)."""
    page = _raw_page(vault, "entities/x/x.md",
                     b"---\r\ntype: actor\r\ntags: a, b\r\ntitle: T\r\n---\r\n\r\nBody.\r\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == (
        b"---\r\ntype: actor\r\ntags:\r\n- a\r\n- b\r\ntitle: T\r\n---\r\n\r\nBody.\r\n")
    assert _plan(vault)["repair_pages"] == 0, "a second plan of the CRLF page is empty"


def test_a_mixed_ending_page_keeps_each_lines_own_ending(vault):
    page = _raw_page(vault, "entities/x/x.md",
                     b"---\ntype: actor\r\ntags: a, b\r\nhint: '[[e/x/y]]'\n---\nBody.\r\nMore.\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == (
        b"---\ntype: actor\r\ntags:\r\n- a\r\n- b\r\nhint: e/x/y\n---\nBody.\r\nMore.\n")


def test_a_crlf_block_list_repair_keeps_crlf(vault):
    page = _raw_page(vault, "entities/x/x.md",
                     b"---\r\ntype: actor\r\nrelated:\r\n- '[[e/a/a]]'\r\n- b\r\nz: 1\r\n---\r\nB\r\n")
    assert _run(["--vault", str(vault), "--apply"])[0] == 0
    assert page.read_bytes() == (
        b"---\r\ntype: actor\r\nrelated:\r\n- e/a/a\r\n- b\r\nz: 1\r\n---\r\nB\r\n")


def test_the_line_ending_is_read_from_the_keys_own_line():
    assert mod._line_ending("a: 1\r\nb: 2\n", 0) == "\r\n"
    assert mod._line_ending("a: 1\r\nb: 2\n", 6) == "\n"
    assert mod._line_ending("\na", 0) == "\n"
    assert mod._line_ending("no newline", 0) == "\n"


@pytest.mark.parametrize(("text", "start", "ending"), [
    ("\nb\r", 0, "\n"),     # the newline is the first character: there is no line before it
    ("\rx", 0, "\n"),       # no newline at all; a stray `\r` is not an ending
    ("a\t\n", 0, "\n"),     # the character before the newline is not `\r`
])
def test_the_line_ending_is_crlf_only_for_a_real_cr_lf_pair(text, start, ending):
    assert mod._line_ending(text, start) == ending


@pytest.mark.parametrize("new_text", [
    "---\ntype: actor\ntags:\n- a\n---\nbody",        # the last byte dropped
    "---\ntype: actor\ntags:\n- a\n---\nbodx\n",      # a later byte lowered
    "---\ntype: actoq\ntags:\n- a\n---\nbody\n",      # a kept byte lowered before the edit
    "---\ntype: actos\ntags:\n- a\n---\nbody\n",      # a kept byte raised before the edit
])
def test_any_kept_byte_that_differs_fails_the_byte_check(new_text):
    assert not mod._outside_edits_unchanged(_STORED_TEXT, new_text, _EDIT)
    assert mod._outside_edits_unchanged(_STORED_TEXT, "---\ntype: actor\ntags:\n- a\n---\nbody\n",
                                        _EDIT)


def test_the_lane_and_the_write_path_agree_on_every_scalar(vault):
    """okengine#1005: for `list` and `prose-list` fields alike, the lane's canonical value is the
    write path's, and it is always a reshape of the stored scalar (a seeded fuzz, deterministic)."""
    import random
    (vault / "schema.yaml").write_text(_SCHEMA + "field_shapes:\n  assumptions: prose-list\n",
                                       encoding="utf-8")
    page = _page(vault, "assessments/a.md", "type: assessment\n")
    write_path = mod.load_write_path(vault)
    rng = random.Random(1005)
    alphabet = ["a", "Bc", " ", ",", ", ", "(", ")", "[", "]", "June 29", "2026", "1", ".", ";",
                "[[x]]", "sources/2026/07/06/long-path-segment"]
    for _ in range(500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
        for key in ("tags", "assumptions"):
            stored = write_path._coerce_fm({key: text}, page)[key]
            lane = mod.canonical(write_path, page, key, text)
            assert lane is not None and mod.same(
                mod.canonical(write_path, page, key, stored), lane), (key, text)
            assert mod.reshape_only(text, lane), (key, text, lane)
