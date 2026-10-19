"""entity-link: sources naming an EXISTING entity join its `sources:` (okengine#888).

The end-to-end tests stub nothing on the write path: the lane imports this repo's write_server,
authenticates as `cron:entity-link`, and is held to that job's output_contract from jobs.json.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"
CONTRACT = next(j for j in json.loads((REPO / "config/engine-crons.json").read_text())
                if j["name"] == "entity-link")["output_contract"]
NOW = "2026-06-26"
_FRESH = ("entity_link", "select_trend_deltas", "okf_migrate", "write_server",
          "output_contract_enforce")


def _page(root: Path, rel: str, fm: dict, body: str = "Body text.\n") -> Path:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body)
    return p


def _src(root: Path, slug: str, day: str, text: str, **fm) -> str:
    seat = f"sources/{day[:4]}/{day[5:7]}/{day[8:]}/{slug}"
    _page(root, seat + ".md", {"type": "source", "title": fm.pop("title", slug),
                               "published": day, **fm}, text + "\n")
    return seat


def _vault(root: Path, contract: bool = True) -> None:
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(yaml.safe_dump({
        "types": {"model": {"required": ["type"], "extensible": True},
                  "source": {"required": ["type", "published"], "extensible": True}},
        "partitioning": {"namespaces": {
            "sources": {"strategy": "by-date", "date_field": "published", "reshard_by": "day"},
            "entities": {"strategy": "by-letter"}}},
        "permissions": {"default": {"create": True, "update": True, "delete": False}},
    }))
    jobs = [{"name": "entity-link", "output_contract": CONTRACT}] if contract else []
    (root / "jobs.json").write_text(json.dumps({"jobs": jobs}))


@pytest.fixture
def lane(tmp_path, monkeypatch):
    saved_modules = {name: sys.modules.get(name) for name in _FRESH}
    saved_path = list(sys.path)
    lane_env = ("OKENGINE_WRITE_ACTOR", "OKENGINE_OUTPUT_CONTRACT_MODE")
    saved_env = {var: os.environ.pop(var, None) for var in lane_env}

    def load(contract: bool = True, **env):
        _vault(tmp_path, contract)
        monkeypatch.setenv("WIKI_PATH", str(tmp_path))
        monkeypatch.setenv("OKENGINE_CRON_JOBS", str(tmp_path / "jobs.json"))
        monkeypatch.setenv("OKENGINE_MCP_DIR", str(REPO / "okengine-mcp"))
        monkeypatch.setenv("ENTITY_LINK_NOW", NOW)
        monkeypatch.setenv("TREND_NOW", NOW)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        sys.path.insert(0, str(CRON))
        for mod in _FRESH:
            sys.modules.pop(mod, None)
        return importlib.import_module("entity_link"), tmp_path

    yield load
    for var, value in saved_env.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    sys.path[:] = saved_path
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _fm(root: Path, rel: str) -> dict:
    return yaml.safe_load((root / "wiki" / rel).read_text().split("---")[1])


def _run(el, capsys, *argv) -> tuple[str, dict]:
    assert el.main(list(argv)) == 0
    out = capsys.readouterr().out
    return out, json.loads(out.strip().splitlines()[-1])


def _corpus(root: Path) -> None:
    _page(root, "entities/r/rlvr.md", {"type": "model", "id": "entities:rlvr",
                                       "title": "Reinforcement learning with verifiable rewards",
                                       "aliases": ["RLVR"],
                                       "sources": ["sources/2026/06/01/seed"]})
    _src(root, "seed", "2026-06-01", "The seed paper.")
    for i in range(3):
        _src(root, f"rl-{i}", "2026-06-2" + str(i), f"We study RLVR on math, run {i}.")
    _src(root, "other", "2026-06-20", "Nothing relevant here at all.")


def test_sources_naming_an_entity_join_its_sources(lane, capsys):
    el, root = lane()
    _corpus(root)
    out, verdict = _run(el, capsys)
    refs = _fm(root, "entities/r/rlvr.md")["sources"]
    assert refs == ["sources/2026/06/01/seed", "sources/2026/06/22/rl-2",
                    "sources/2026/06/21/rl-1", "sources/2026/06/20/rl-0"], \
        "existing ref kept first; new seats appended newest-first, in the vault's ref form"
    assert verdict["linked"] == 3 and verdict["entities-updated"] == 1
    snap = (root / "wiki" / "operational" / "entity-link-snapshots.md").read_text()
    assert f"| {NOW} | 4 | 5 | 80.0 |" in snap, "the ledger row kb-health reads"


def test_a_second_run_adds_nothing(lane, capsys):
    el, root = lane()
    _corpus(root)
    _run(el, capsys)
    before = (root / "wiki" / "entities" / "r" / "rlvr.md").read_text()
    _, verdict = _run(el, capsys)
    assert "linked" not in verdict
    assert (root / "wiki" / "entities" / "r" / "rlvr.md").read_text() == before


def test_linked_entities_become_trend_movers(lane, capsys):
    """REGRESSION (#888): with one citation per entity the trends gate never fired. After
    linking, the entity-frequency signal exists without the tag fallback."""
    el, root = lane()
    _corpus(root)
    _run(el, capsys)
    sys.modules.pop("select_trend_deltas", None)
    trends = importlib.import_module("select_trend_deltas")
    os.environ["TREND_MIN_MOVERS"] = "1"
    os.environ["TREND_TAG_FALLBACK"] = "0"
    try:
        sys.modules.pop("select_trend_deltas", None)
        trends = importlib.import_module("select_trend_deltas")
        assert trends.main() == 0
    finally:
        os.environ.pop("TREND_MIN_MOVERS")
        os.environ.pop("TREND_TAG_FALLBACK")
    assert "entities/r/rlvr" in capsys.readouterr().out


def test_an_ambiguous_name_links_nothing(lane, capsys):
    """NEGATIVE: two entities called the same thing — never guess which one a source means."""
    el, root = lane()
    _page(root, "entities/a/atlas-a.md", {"type": "model", "name": "Atlas"})
    _page(root, "entities/a/atlas-b.md", {"type": "model", "title": "atlas"})
    _src(root, "s", "2026-06-20", "Atlas beats the baseline.")
    out, verdict = _run(el, capsys)
    assert verdict["name-ambiguous"] == 1 and "linked" not in verdict
    assert "sources" not in _fm(root, "entities/a/atlas-a.md")


@pytest.mark.parametrize("name, text, reason", [
    ("GPT", "GPT is mentioned.", "name-too-short"),
    ("τ²-bench", "a bench of tests", "name-lossy-fold"),
    ("One Two Three Four Five Six Seven Eight Nine", "x", "name-too-long"),
])
def test_names_that_are_not_evidence_are_dropped(lane, capsys, name, text, reason):
    el, root = lane()
    _page(root, "entities/x/x.md", {"type": "model", "name": name})
    _src(root, "s", "2026-06-20", text)
    _, verdict = _run(el, capsys)
    assert verdict[reason] == 1 and "linked" not in verdict


def test_the_pack_exclude_pattern_drops_generic_names(lane, capsys):
    el, root = lane(OKENGINE_ENTITY_LINK_EXCLUDE=r"^prompting$")
    _page(root, "entities/p/prompting.md", {"type": "model", "name": "Prompting"})
    _src(root, "s", "2026-06-20", "Prompting helps.")
    _, verdict = _run(el, capsys)
    assert verdict["name-excluded-by-pack"] == 1 and "linked" not in verdict


def test_an_acronym_matches_only_in_its_own_case(lane, capsys):
    """`CLEAR` the method must not link every source that says "clear"."""
    el, root = lane()
    _page(root, "entities/c/clear.md", {"type": "model", "id": "entities:clear", "name": "CLEAR"})
    _src(root, "plain", "2026-06-20", "The results are clear.")
    _src(root, "acro", "2026-06-21", "We extend CLEAR to video.")
    _run(el, capsys)
    assert _fm(root, "entities/c/clear.md")["sources"] == ["sources/2026/06/21/acro"]


def test_the_longest_name_wins_at_a_position(lane, capsys):
    """`GPT-5.5` is the GPT-5.5 entity's mention, not also a mention of `GPT-5`."""
    el, root = lane()
    _page(root, "entities/g/gpt-5.md", {"type": "model", "id": "entities:gpt-5", "name": "GPT-5"})
    _page(root, "entities/g/gpt-5-5.md",
          {"type": "model", "id": "entities:gpt-5-5", "name": "GPT-5.5"})
    _src(root, "new", "2026-06-20", "GPT-5.5 is out.")
    _run(el, capsys)
    assert _fm(root, "entities/g/gpt-5-5.md")["sources"] == ["sources/2026/06/20/new"]
    assert "sources" not in _fm(root, "entities/g/gpt-5.md")


def test_a_name_in_too_many_sources_is_not_evidence(lane, capsys):
    el, root = lane(OKENGINE_ENTITY_LINK_MAX_SHARE="0.5")
    _page(root, "entities/m/model.md", {"type": "model", "name": "Everywhere"})
    for i in range(25):
        _src(root, f"s{i}", "2026-06-20", "Everywhere, everything." if i < 20 else "quiet")
    _, verdict = _run(el, capsys)
    assert verdict["name-too-common"] == 1 and "linked" not in verdict


def test_tombstoned_entities_and_sources_never_link(lane, capsys):
    el, root = lane()
    _page(root, "entities/r/retired.md", {"type": "model", "name": "Retired Model",
                                           "status": "tombstoned"})
    _page(root, "entities/l/live.md", {"type": "model", "name": "Live Model"})
    _src(root, "gone", "2026-06-20", "Live Model and Retired Model.", status="tombstoned")
    _, verdict = _run(el, capsys)
    assert "linked" not in verdict
    assert "sources" not in _fm(root, "entities/l/live.md")


def test_a_moved_source_ref_is_repaired_to_its_one_new_seat(lane, capsys):
    el, root = lane()
    _src(root, "moved", "2026-06-20", "text")
    _page(root, "entities/r/r.md", {"type": "model", "id": "entities:r", "name": "Unmentioned",
                                     "sources": ["sources/moved"]})
    _, verdict = _run(el, capsys)
    assert verdict["repaired"] == 1
    assert _fm(root, "entities/r/r.md")["sources"] == ["sources/2026/06/20/moved"]


def test_an_unrepairable_stale_ref_is_kept_reported_and_grandfathered(lane, capsys):
    """NEGATIVE: two live sources share the stem, so the ref's new seat is ambiguous. The lane
    never drops a curated ref to make a write pass, and reports the ref as unrepairable.

    The ref was already on the page, so the write path grandfathers it (`_missing_source_reject`
    blocks only NEWLY introduced unresolved sources) and the new link lands beside it. Before
    okengine#994 this update was refused, but only because the stored ref was spelled
    `[[sources/twin]]`: the raw stored spelling never matched the canonical item the write would
    store. Spelled `sources/twin`, main accepted the same update. A NEW unresolved source is still
    refused (test_chat_writer_983::test_a_new_dangling_source_is_still_refused)."""
    el, root = lane()
    linked = _src(root, "twin", "2026-06-20", "Linkable Name")
    _src(root, "twin", "2026-06-21", "text")
    _page(root, "entities/r/r.md", {"type": "model", "id": "entities:r", "name": "Linkable Name",
                                     "sources": ["[[sources/twin]]"]})
    _, verdict = _run(el, capsys)
    assert verdict["stale-ref-unrepairable"] == 1 and verdict.get("repaired", 0) == 0
    assert verdict["linked"] == 1 and not any(k.startswith("rejected:") for k in verdict)
    assert _fm(root, "entities/r/r.md")["sources"] == ["sources/twin", linked], \
        "the stale ref is kept (in its canonical spelling) and the new link added"


def test_an_id_form_ref_already_counts_as_linked(lane, capsys):
    el, root = lane()
    _page(root, "sources/2026/06/20/by-id.md", {"type": "source", "id": "sources:by-id",
                                                  "title": "t", "published": "2026-06-20"},
          "Known Model appears.\n")
    _page(root, "entities/k/k.md", {"type": "model", "id": "entities:k", "name": "Known Model",
                                     "sources": ["sources:by-id"]})
    _, verdict = _run(el, capsys, "--dry-run")
    assert "would-link" not in verdict


def test_without_its_declared_contract_the_lane_cannot_write(lane, capsys):
    """NEGATIVE: an authenticated lane, not an admin bypass."""
    el, root = lane(contract=False)
    _corpus(root)
    out, verdict = _run(el, capsys)
    assert any(k.startswith("rejected:") for k in verdict)
    assert _fm(root, "entities/r/rlvr.md")["sources"] == ["sources/2026/06/01/seed"]
    assert not (root / "wiki" / "operational" / "entity-link-snapshots.md").read_text() \
        .count("| 2026-06-26 | 4 |")


def test_backfill_is_a_dry_run_unless_applied(lane, capsys):
    el, root = lane()
    _corpus(root)
    _src(root, "old", "2025-01-10", "Old RLVR work.")
    out, verdict = _run(el, capsys, "--backfill")
    assert verdict["would-link"] == 4 and "most-linked entities" in out
    assert _fm(root, "entities/r/rlvr.md")["sources"] == ["sources/2026/06/01/seed"]
    assert not (root / "wiki" / "operational" / "entity-link-snapshots.md").exists()
    _, verdict = _run(el, capsys, "--backfill", "--apply")
    assert verdict["linked"] == 4, "the backfill reaches sources outside the daily window"


def test_the_daily_window_skips_older_and_undated_sources(lane, capsys):
    el, root = lane()
    _corpus(root)
    _src(root, "old", "2025-01-10", "Old RLVR work.")
    _page(root, "sources/loose/undated.md", {"type": "source", "title": "u"}, "RLVR again\n")
    _, verdict = _run(el, capsys, "--dry-run")
    assert verdict["would-link"] == 3


def test_run_limit_leaves_the_rest_unexamined(lane, capsys):
    el, root = lane()
    _corpus(root)
    _page(root, "entities/s/second.md",
          {"type": "model", "id": "entities:second", "name": "Nothing relevant"})
    _, verdict = _run(el, capsys, "--limit", "1")
    assert verdict["entities-updated"] == 1 and verdict["not-examined:run-limit"] == 1


def test_unreadable_and_malformed_pages_are_skipped(lane, capsys):
    el, root = lane()
    _corpus(root)
    ents = root / "wiki" / "entities" / "z"
    ents.mkdir(parents=True)
    (ents / "no-fm.md").write_text("plain text\n")
    (ents / "bad.md").write_text("---\nname: [unclosed\n---\n")
    (ents / "list.md").write_text("---\n- a\n---\n")
    (ents / "_draft.md").write_text("---\nname: RLVR\n---\n")
    (ents / "INDEX.md").write_text("---\nname: RLVR\n---\n")
    (ents / "gone.md").symlink_to(ents / "missing.md")
    _page(root, "entities/n/numeric.md", {"type": "model", "aliases": [2026, {"x": 1}],
                                          "sources": "sources/2026/06/01/seed"})
    _page(root, "sources/2026/13/01/bad-month.md", {"type": "source", "published": "n/a"},
          "RLVR\n")
    _, verdict = _run(el, capsys)
    assert verdict["linked"] == 3


def test_published_as_a_datetime_or_folder_month(lane, capsys):
    el, root = lane()
    mod = el
    assert mod._date({"published": mod.datetime(2026, 6, 20, 5)}, "") == mod.date(2026, 6, 20)
    assert mod._date({"published": mod.date(2026, 6, 20)}, "") == mod.date(2026, 6, 20)
    assert mod._date({}, "sources/2026/05/x") == mod.date(2026, 5, 15)
    assert mod._date({}, "sources/2026/13/x") is None
    assert mod._norm_ref("[[sources/a|label]]") == "sources/a"
    assert mod._today() == mod.date(2026, 6, 26)
    os.environ.pop("ENTITY_LINK_NOW")
    assert mod._today() == mod.date.today()


def test_no_wiki_is_a_clean_no_op(lane, capsys, tmp_path, monkeypatch):
    el, _root = lane()
    monkeypatch.setattr(el, "WIKI", tmp_path / "missing")
    out, verdict = _run(el, capsys)
    assert verdict == {"wakeAgent": False}


def test_fleet_health_shows_the_link_ratio_and_never_invents_a_zero(lane, capsys, monkeypatch):
    el, root = lane()
    spec = importlib.util.spec_from_file_location("fleet_health_el", CRON / "fleet_health.py")
    fh = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fh
    spec.loader.exec_module(fh)
    try:
        assert fh.entity_linkage()[0] == "unknown", "no snapshot is unknown, not 0%"
        snap = root / "wiki" / "operational" / "entity-link-snapshots.md"
        snap.parent.mkdir(parents=True)
        snap.write_text("| date | a | b | % |\n|---|---|---|---|\n| 2026-06-25 | x | 5 | 1 |\n")
        assert fh.entity_linkage()[0] == "unknown", "a malformed row is no data"
        snap.write_text("| 2026-06-25 | 131 | 3022 | 4.3 |\n")
        state, detail = fh.entity_linkage()
        assert state == "warn" and "4.3% of recent sources" in detail and "131 / 3,022" in detail
        snap.write_text("| 2026-06-25 | 131 | 3022 | 4.3 |\n| 2026-06-26 | 1409 | 3022 | 46.6 |\n")
        assert fh.entity_linkage()[0] == "ok"
        snap.write_text("| 2026-06-26 | 0 | 0 | 0 |\n")
        assert fh.entity_linkage()[0] == "warn"
    finally:
        sys.modules.pop(spec.name, None)


# ── boundary pins (each asserts the exact edge a mutant would move) ──────────────────────────

def test_fold_lossy_and_acronym_edges(lane):
    el, _root = lane()
    assert el.fold("Qwen2.5-VL") == ["qwen2", "5", "vl"]
    assert el.fold("Qwen2.5-VL", lower=False) == ["Qwen2", "5", "VL"]
    assert el._lossy("τ²-bench") and el._lossy("Ω model")
    assert not el._lossy("café latte"), "a combining accent folds cleanly: not lossy"
    assert not el._lossy("plain ascii 42")
    assert el._acronym("CLEAR") and el._acronym("ABCDEFGH")
    assert not el._acronym("ABCDEFGHI"), "more than 8 capitals is not an acronym"
    assert not el._acronym("Clear") and not el._acronym("123")


def test_date_parsing_edges(lane):
    el, _root = lane()
    d = el.date
    assert el._date({"published": "2026-06-20T04:00:00+00:00"}, "") == d(2026, 6, 20)
    assert el._date({"published": ""}, "sources/2026/12/x") == d(2026, 12, 15)
    assert el._date({"published": ""}, "sources/2026/01/x") == d(2026, 1, 15)
    assert el._date({"published": ""}, "sources/2026/00/x") is None
    assert el._date({"published": None}, "sources/loose/x") is None


def test_window_boundary_and_ordering(lane):
    """The window includes its first day; sources sort newest-first, ties by seat."""
    el, root = lane()
    _vault(root)
    _src(root, "edge", "2026-06-12", "x")
    _src(root, "before", "2026-06-11", "x")
    _src(root, "b", "2026-06-20", "x")
    _src(root, "a", "2026-06-20", "x")
    seats = [s["seat"] for s in el.load_sources(el.date(2026, 6, 12))]
    assert seats == ["sources/2026/06/20/b", "sources/2026/06/20/a", "sources/2026/06/12/edge"]
    assert len(el.load_sources(None)) == 4


def test_entity_names_come_from_name_title_and_scalar_aliases(lane):
    el, root = lane()
    _page(root, "entities/m/m.md", {"type": "model", "name": "Alpha Model", "title": "Beta Model",
                                    "aliases": ["Gamma Model", "", 404, 1.5, None, ["nested"]]})
    _page(root, "entities/s/s.md", {"type": "model", "title": "Solo Name", "aliases": "Solo Alias"})
    ents = el.load_entities()
    assert ents["entities/m/m.md"]["names"] == {"Alpha Model", "Beta Model", "Gamma Model",
                                                "404", "1.5"}
    assert ents["entities/s/s.md"]["names"] == {"Solo Name", "Solo Alias"}


@pytest.mark.parametrize("alias, names", [
    (4040, {"Solo Name", "4040"}),           # bare int: a one-item list, per the list contract
    (2.5, {"Solo Name", "2.5"}),             # bare float
    (True, {"Solo Name"}),                   # a YAML boolean is never a name ("True" would link)
    ("2026-01-01", {"Solo Name", "2026-01-01"}),
])
def test_a_bare_scalar_alias_is_a_one_item_list_never_a_crash(lane, alias, names):
    """okengine#994 [20]: `aliases: 4040` iterated an int and crashed the whole lane."""
    el, root = lane()
    _page(root, "entities/s/s.md", {"type": "model", "title": "Solo Name", "aliases": alias})
    assert el.load_entities()["entities/s/s.md"]["names"] == names


def test_a_boolean_in_an_alias_list_is_not_a_name(lane):
    el, root = lane()
    _page(root, "entities/s/s.md", {"type": "model", "title": "Solo Name",
                                    "aliases": [True, False, "Real Alias"]})
    assert el.load_entities()["entities/s/s.md"]["names"] == {"Solo Name", "Real Alias"}


@pytest.mark.parametrize("sources, refs", [
    (12345, [12345]),
    ("sources/2026/06/01/seed", ["sources/2026/06/01/seed"]),
    (None, []),
    ({"bad": "shape"}, [{"bad": "shape"}]),  # kept whole, never flattened to its keys
])
def test_a_bare_scalar_sources_value_is_a_one_item_list(lane, sources, refs):
    el, root = lane()
    _page(root, "entities/s/s.md", {"type": "model", "title": "Solo Name", "sources": sources})
    assert el.load_entities()["entities/s/s.md"]["refs"] == refs


def test_a_lane_run_over_a_scalar_alias_and_source_links_instead_of_crashing(lane, capsys):
    el, root = lane()
    _page(root, "entities/c/cve.md", {"type": "model", "id": "entities:cve", "title": "Kestrel Loader",
                                      "aliases": 90210, "sources": 7})
    _src(root, "k-1", "2026-06-21", "A new Kestrel Loader build, tracked as 90210.")
    _, verdict = _run(el, capsys)
    assert verdict["linked"] == 1
    assert _fm(root, "entities/c/cve.md")["sources"] == [7, "sources/2026/06/21/k-1"]


def test_name_floor_and_length_edges(lane):
    el, _root = lane(OKENGINE_ENTITY_LINK_MIN_CHARS="4")
    eight = "one two three four five six seven eight"
    ents = {"entities/a.md": {"names": {"ABCD", "abc", eight, eight + " nine"}, "refs": []}}
    index, dropped = el.name_index(ents)
    assert index[("abcd",)] == ("entities/a.md", ("ABCD",))
    assert index[tuple(eight.split())] == ("entities/a.md", None)
    assert dict(dropped) == {"name-too-short": 1, "name-too-long": 1}


def test_a_name_owned_by_one_entity_twice_is_not_ambiguous(lane):
    el, _root = lane()
    index, dropped = el.name_index({"entities/a.md": {"names": {"Alpha", "ALPHA"}, "refs": []}})
    assert index[("alpha",)][0] == "entities/a.md" and "name-ambiguous" not in dropped


def test_longest_match_consumes_its_span(lane):
    el, _root = lane()
    index = {("gpt", "5"): ("e/gpt5", None), ("gpt", "5", "5"): ("e/gpt55", None),
             ("5",): ("e/five", None), ("clear",): ("e/clear", ("CLEAR",))}
    assert el._names_in(["GPT", "5", "5", "and", "GPT", "5"], index) == {("gpt", "5", "5"),
                                                                         ("gpt", "5")}
    assert el._names_in(["5"], index) == {("5",)}
    assert el._names_in(["clear", "CLEAR"], index) == {("clear",)}
    assert el._names_in(["clear"], index) == set()
    assert el._names_in([], index) == set()


def test_the_common_name_threshold_edges(lane):
    """Share is judged only from MIN_SOURCES_FOR_SHARE sources up; `>` share, not `>=`."""
    el, _root = lane(OKENGINE_ENTITY_LINK_MAX_SHARE="0.5")
    index = {("hot",): ("e/hot", None)}

    def srcs(n, hot):
        return [{"seat": f"s{i}", "id": "", "date": el.date(2026, 6, 1),
                 "tokens": ["hot"] if i < hot else ["x"]} for i in range(n)]
    hits, dropped = el.match(srcs(19, 19), index)
    assert len(hits["e/hot"]) == 19 and not dropped, "under 20 sources, share is not evidence"
    hits, dropped = el.match(srcs(20, 10), index)
    assert len(hits["e/hot"]) == 10 and not dropped, "exactly half is not MORE than half"
    hits, dropped = el.match(srcs(20, 11), index)
    assert "e/hot" not in hits and dict(dropped) == {"name-too-common": 1}


def test_top_names_ranks_by_count_then_path(lane):
    el, _root = lane()
    index = {("a",): ("e/a", None), ("b",): ("e/b", None), ("bb",): ("e/b", None),
             ("c",): ("e/c", None)}
    hits = {"e/a": [1], "e/b": [1, 2], "e/c": [1]}
    assert el.top_names(index, hits) == ["2 e/b (b, bb)", "1 e/a (a)", "1 e/c (c)"]
    assert el.top_names(index, hits, n=1) == ["2 e/b (b, bb)"]


def test_plan_counts_and_keeps_order(lane):
    el, _root = lane()
    live = {"sources/2026/06/20/new", "sources/2026/06/20/kept"}
    by_stem = {"new": ["sources/2026/06/20/new"], "kept": ["sources/2026/06/20/kept"]}
    found = [{"seat": "sources/2026/06/20/new", "id": ""},
             {"seat": "sources/2026/06/20/kept", "id": ""}]
    entity = {"refs": ["[[sources/2026/06/20/kept]]", "sources/old/new", "sources/gone"]}
    refs, added, repaired, stale = el.plan(entity, found, live, by_stem)
    assert refs == ["[[sources/2026/06/20/kept]]", "sources/2026/06/20/new", "sources/gone"]
    assert (added, repaired, stale) == (0, 1, 1)
    assert el.plan({"refs": []}, [], live, by_stem) == (None, 0, 0, 0)


def test_link_ratio_counts_seat_or_id_citations_in_window(lane):
    el, _root = lane()
    today = el.date(2026, 6, 26)
    live = [{"seat": "s/a", "id": "", "date": today},
            {"seat": "s/b", "id": "sources:b", "date": today - el.timedelta(days=42)},
            {"seat": "s/c", "id": "", "date": today - el.timedelta(days=43)},
            {"seat": "s/d", "id": "", "date": None}]
    ents = {"e": {"refs": ["[[s/a]]", "sources:b", "s/c"]}}
    assert el.link_ratio(ents, live, today) == (2, 2)


def test_snapshot_header_written_once_and_percent_rounded(lane):
    el, root = lane()
    _vault(root)
    el.append_snapshot(el.date(2026, 6, 25), 1, 3)
    el.append_snapshot(el.date(2026, 6, 26), 0, 0)
    text = (root / "wiki" / "operational" / "entity-link-snapshots.md").read_text()
    assert text.count("| date |") == 1
    assert "| 2026-06-25 | 1 | 3 | 33.3 |" in text and "| 2026-06-26 | 0 | 0 | 0.0 |" in text


def test_report_lines_and_json_summary(lane, capsys):
    el, root = lane()
    _corpus(root)
    out, verdict = _run(el, capsys, "--dry-run")
    assert out.startswith("# entity-link — 4 sources (last 14d), 1 entities, 2 usable names "
                          "(dry run)\n")
    assert "- would-link: 3\n" in out and "- entities-would-update: 1\n" in out
    # the dry run projects the ratio AFTER its planned links: 1 cited today + 3 would-link
    assert "- recent sources cited by an entity (42d): 4/5 (80.0%) — projected" in out
    assert verdict["wakeAgent"] is False and verdict["sources"] == 4
    assert verdict["recent_cited"] == 4 and verdict["recent"] == 5
    out, _ = _run(el, capsys, "--backfill")
    assert "sources (backfill)" in out
    out, _ = _run(el, capsys)
    assert "(dry run)" not in out and "projected" not in out


def test_rejections_are_counted_by_their_first_line(lane, capsys, monkeypatch):
    el, root = lane()
    _corpus(root)
    fake = type("W", (), {"_update": staticmethod(lambda rel, patch: "refused: nope\nmore")})
    monkeypatch.setattr(el, "_write_server", lambda: fake)
    _, verdict = _run(el, capsys)
    assert verdict["rejected:refused: nope"] == 1 and "linked" not in verdict


def test_the_run_limit_counts_only_attempted_writes(lane, capsys):
    el, root = lane()
    _corpus(root)
    _page(root, "entities/s/second.md", {"type": "model", "id": "entities:second",
                                         "name": "Nothing relevant"})
    _, verdict = _run(el, capsys, "--limit", "2")
    assert verdict["entities-updated"] == 2 and "not-examined:run-limit" not in verdict
    _page(root, "entities/t/third.md", {"type": "model", "id": "entities:third",
                                        "name": "Third Thing"})
    _src(root, "third", "2026-06-24", "About Third Thing.")
    _, verdict = _run(el, capsys, "--limit", "0", "--dry-run")
    assert verdict["not-examined:run-limit"] == 1
