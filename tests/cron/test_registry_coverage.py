"""registry_coverage (okengine#541) — rank what it would cost to unstarve review-autoverify.

The contract under test: the census measures the bar the LANE applies (not a copy of it), never
assigns a grade itself, separates a missing grade from a defect that grading cannot fix, and states
its projections as the non-additive upper bounds they are.
"""
import importlib.util
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "registry_coverage.py"
AV = REPO / "scripts" / "cron" / "review_autoverify.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="registry_coverage absent")

SCHEMA = """\
source_registry:
  Microsoft: {reliability: A}
  MISP galaxy: {reliability: B}
  URLhaus: {reliability: B}
# okengine#554 makes `actor` always-published, which would remove every fixture page below from
# the census. These tests are about the census/lane agreement on the EVIDENCE BAR, so switch the
# policy off here and cover the always-publish behaviour in its own test instead.
review_autoverify:
  always_publish_types: []
types:
  actor: {required: [type, name]}
  malware: {required: [type, name]}
  source: {required: [type]}
  prediction: {required: [type]}
"""


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("registry_coverage", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    import sys
    sl = sys.modules.get("schema_lib")
    if sl is not None and hasattr(sl, "_SCHEMA_CACHE"):
        sl._SCHEMA_CACHE.clear()
    return m


def _load_av(vault: Path):
    spec = importlib.util.spec_from_file_location("review_autoverify_probe", AV)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    import sys
    sl = sys.modules.get("schema_lib")
    if sl is not None and hasattr(sl, "_SCHEMA_CACHE"):
        sl._SCHEMA_CACHE.clear()
    return m


def _vault(tmp_path: Path, schema: str = SCHEMA) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    return tmp_path


def _source(vault: Path, rel: str, publisher: str):
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: source\npublisher: {publisher}\n---\n\nbody\n", encoding="utf-8")


def _held(vault: Path, rel: str, sources, *, ptype="actor", extra=""):
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    # Quoted: an unquoted `- [[sources/x]]` is a nested YAML flow sequence, not the string a live
    # vault stores. Leaving it bare made the fixture a list-of-lists and silently skipped the ref.
    src = "".join(f"  - '{s}'\n" for s in sources)
    p.write_text(f"---\ntype: {ptype}\nname: N\nneeds_review: true\nsources:\n{src}{extra}---\n\nbody\n",
                 encoding="utf-8")


def _report(vault: Path) -> str:
    return (vault / "wiki" / "operational" / "registry-coverage.md").read_text()


# --- the drift pin --------------------------------------------------------

def test_census_held_total_matches_what_autoverify_refuses(tmp_path, capsys):
    """The census's whole value is that it measures the bar the lane actually applies. If the two
    disagree about which pages are held, the ranking is against a policy nobody enforces."""
    v = _vault(tmp_path)
    _source(v, "sources/ms", "Microsoft")
    _source(v, "sources/misp", "MISP galaxy")
    # `malware`, not `actor`: actors are always-published now (okengine#554) and so are not census
    # subjects at all. The contract under test is that the census and the lane agree about the
    # EVIDENCE BAR, which only applies to types that can still be held.
    _held(v, "entities/clears", ["sources/ms"])                    # 1xA -> autoverify clears
    _held(v, "entities/blocked", ["sources/unknown-pub"])          # ungraded -> held
    _held(v, "entities/onlyb", ["sources/misp"])                   # 1xB, under the 2xB bar
    _source(v, "sources/unknown-pub", "Acme Threat Labs")

    m = _load(v)
    result = m.census(v)
    av = _load_av(v)
    av.main(["--dry-run"])
    out = capsys.readouterr().out
    # Parse the tally by NAME, not by position between adjacent words: the previous
    # split("cleared,")[1] form broke the moment the summary gained a counter (okengine#563), which
    # made a contract test fail for a wording change rather than a behaviour change.
    import re as _re
    _m = _re.search(r"(\d+) held for human review", out)
    assert _m, f"could not find the held tally in the lane summary: {out!r}"
    av_held = int(_m.group(1))

    assert result["held_total"] == av_held + 1        # census counts the cleared page as held too...
    assert result["buckets"].get("clearable now (lane has not run)") == 1   # ...in this bucket


def test_policy_is_read_from_the_lane_not_hardcoded(tmp_path):
    """A pack that lowers the bar to 1xB must change the census's answer with no edit here."""
    v = _vault(tmp_path, SCHEMA.replace("  always_publish_types: []\n",
                                    "  always_publish_types: []\n  b_sources: 1\n"))
    _source(v, "sources/misp", "MISP galaxy")
    _held(v, "entities/onlyb", ["sources/misp"])
    result = _load(v).census(v)
    assert result["policy"]["b_sources"] == 1
    assert result["buckets"].get("clearable now (lane has not run)") == 1


# --- ranking --------------------------------------------------------------

def test_ranks_publishers_by_pages_they_would_release(tmp_path):
    v = _vault(tmp_path)
    for i in range(3):
        _source(v, f"sources/big{i}", "Big Vendor")
        _held(v, f"entities/b{i}", [f"sources/big{i}"])
    _source(v, "sources/small", "Small Blog")
    _held(v, "entities/s0", ["sources/small"])

    ranked = _load(v).census(v)["ranked"]
    assert [r["publisher"] for r in ranked] == ["Big Vendor", "Small Blog"]
    assert ranked[0]["pages_blocked"] == 3 and ranked[0]["clears_if_graded_a"] == 3
    assert ranked[1]["clears_if_graded_a"] == 1


def test_b_projection_needs_a_partner_when_the_bar_is_two(tmp_path):
    """A page citing one ungraded publisher and nothing else clears at A but NOT at B — 2xB needs
    two distinct B publishers, so promising a B-grade clear there would be a lie."""
    v = _vault(tmp_path)
    _source(v, "sources/solo", "Solo Outlet")
    _held(v, "entities/solo", ["sources/solo"])
    r = _load(v).census(v)["ranked"][0]
    assert r["clears_if_graded_a"] == 1
    assert r["clears_if_graded_b"] == 0

    v2 = _vault(tmp_path / "two")
    _source(v2, "sources/misp", "MISP galaxy")          # already B
    _source(v2, "sources/partner", "Partner Outlet")    # ungraded
    _held(v2, "entities/pair", ["sources/misp", "sources/partner"])
    r2 = _load(v2).census(v2)["ranked"][0]
    assert r2["clears_if_graded_b"] == 1                # 1 existing B + this one = 2xB


def test_report_states_projections_are_not_additive(tmp_path):
    """Two publishers can each be the sole blocker of the same page. Summing the column overcounts,
    and a reader who does not know that will promise a clear rate the lane cannot deliver."""
    v = _vault(tmp_path)
    _source(v, "sources/a", "Alpha")
    _source(v, "sources/b", "Beta")
    _held(v, "entities/both", ["sources/a", "sources/b"])
    m = _load(v)
    m.main([])
    text = _report(v)
    assert "not additive" in text
    assert "upper bound" in text.lower()


# --- never assigns a grade ------------------------------------------------

def test_proposed_schema_leaves_every_grade_blank(tmp_path):
    """#313's audit property depends on a human assigning the grade. A census that guessed would
    hand the model the laundering path the no_agent split was built to close."""
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/x", ["sources/x"])
    m = _load(v)
    m.main([])
    block = _report(v).split("```yaml")[1].split("```")[0]
    assert "Ungraded Outlet: {reliability: }" in block
    for line in block.splitlines():
        if "reliability" in line:
            assert line.split("reliability:")[1].split("}")[0].strip() == ""


# --- defects grading cannot fix -------------------------------------------

def test_vault_artifacts_are_reported_separately_from_missing_grades(tmp_path):
    """`log.md` in a sources list is a writer defect. Ranking it as an ungraded publisher would
    propose grading the changelog."""
    v = _vault(tmp_path)
    _held(v, "entities/artifact", ["log.md", "[[wiki/log.md]]"])
    result = _load(v).census(v)
    assert result["artifact_pages"] == 1
    assert not [r for r in result["ranked"] if "log" in r["publisher"].lower()]


def test_wikilink_form_refs_are_not_a_grading_gap(tmp_path):
    """`[[sources/…]]` is a real source path in brackets; _source_page requires a bare `sources/`
    prefix, so the evidence is invisible. Grading will not help these pages — say so instead of
    ranking the bracketed string as a publisher."""
    v = _vault(tmp_path)
    _source(v, "sources/real", "Microsoft")
    _held(v, "entities/wl", ["[[sources/real]]"])
    m = _load(v)
    result = m.census(v)
    assert result["wikilink_pages"] == 1
    assert result["ranked"] == []
    m.main([])
    assert "invisible evidence" in _report(v)


def test_artifact_and_wikilink_are_distinguished(tmp_path):
    m = _load(_vault(tmp_path))
    assert m._ref_class("[[wiki/log.md]]") == "artifact"
    assert m._ref_class("log.md") == "artifact"
    assert m._ref_class("[[sources/2017/04/thing]]") == "wikilink"
    assert m._ref_class("CrowdStrike") == "publisher"


# --- refusal buckets ------------------------------------------------------

def test_judgment_types_are_bucketed_not_ranked(tmp_path):
    """A prediction's needs_review guards an analytic judgment; no amount of registry work clears
    it, so counting it as a grading gap would inflate the reachable number."""
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/p", ["sources/x"], ptype="prediction")
    result = _load(v).census(v)
    assert result["buckets"].get("judgment type 'prediction'") == 1
    assert result["ranked"] == []


@pytest.mark.parametrize("extra,bucket", [
    ("status: tombstoned\n", "tombstoned"),
    ("conflicts:\n  - a dispute\n", "conflicts present"),
])
def test_other_refusals_are_bucketed(tmp_path, extra, bucket):
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/r", ["sources/x"], extra=extra)
    assert _load(v).census(v)["buckets"].get(bucket) == 1


def test_missing_required_field_is_bucketed(tmp_path):
    v = _vault(tmp_path)
    p = v / "wiki" / "entities" / "nofield.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: actor\nneeds_review: true\nsources:\n  - sources/x\n---\n\nb\n",
                 encoding="utf-8")
    _source(v, "sources/x", "Ungraded Outlet")
    assert _load(v).census(v)["buckets"].get("missing required") == 1


def test_grounding_failure_is_bucketed(tmp_path):
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    p = v / "wiki" / "entities" / "g.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: actor\nname: N\nneeds_review: true\nsources:\n  - sources/x\n---\n\n"
                 "## Grounding check\n\nThe claim is unsupported by the cited source.\n",
                 encoding="utf-8")
    assert _load(v).census(v)["buckets"].get("grounding-check failure") == 1


def test_page_with_no_sources_is_bucketed_not_ranked(tmp_path):
    v = _vault(tmp_path)
    _held(v, "entities/bare", [])
    result = _load(v).census(v)
    assert result["buckets"].get("no sources cited") == 1
    assert result["ranked"] == []


def test_all_cited_publishers_graded_but_under_the_bar(tmp_path):
    v = _vault(tmp_path)
    _source(v, "sources/misp", "MISP galaxy")
    _held(v, "entities/onlyb", ["sources/misp"])
    result = _load(v).census(v)
    assert result["buckets"].get("cited, all graded, still under the bar") == 1


# --- the empty-registry case ----------------------------------------------

def test_no_registry_is_reported_as_undetectable_not_a_pass(tmp_path, capsys):
    """Two live packs ship no source_registry, where the lane is a silent no-op. A census that
    printed a clean zero would confirm the false impression that nothing is wrong."""
    v = _vault(tmp_path, "types:\n  actor: {required: [type, name]}\n")
    _source(v, "sources/x", "Some Outlet")
    _held(v, "entities/x", ["sources/x"])
    m = _load(v)
    assert m.main([]) == 0
    err = capsys.readouterr().err
    assert "no source_registry" in err and "not a pass" in err
    assert "No `source_registry`" in _report(v)


# --- CLI / output contract ------------------------------------------------

def test_json_goes_to_a_file_so_stdout_stays_the_cron_contract(tmp_path, capsys):
    """stdout ends with the runner's {"wakeAgent": ...} line. Dumping the census there too makes
    the stream parseable as neither."""
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/x", ["sources/x"])
    dest = tmp_path / "out.json"
    m = _load(v)
    assert m.main(["--json", str(dest)]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith('{"wakeAgent": false}')
    assert json.loads(dest.read_text())["ranked"][0]["publisher"] == "Ungraded Outlet"


def test_report_is_written_and_summary_names_the_top_blockers(tmp_path, capsys):
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/x", ["sources/x"])
    m = _load(v)
    assert m.main([]) == 0
    assert "Ungraded Outlet (1)" in capsys.readouterr().out
    assert "Registry coverage" in _report(v)


def test_long_tail_is_truncated_but_the_remainder_is_declared(tmp_path, monkeypatch, capsys):
    """Silent truncation reads as 'this is all of them'. Live market-intel has 397 ungraded
    publishers against a top-25 table."""
    v = _vault(tmp_path)
    for i in range(4):
        _source(v, f"sources/p{i}", f"Outlet {i}")
        _held(v, f"entities/e{i}", [f"sources/p{i}"])
    m = _load(v)
    monkeypatch.setattr(m, "TOP", 2)
    m.main([])
    assert "and 2 more ungraded publisher(s)" in _report(v)


def test_missing_wiki_is_an_error(tmp_path, capsys):
    m = _load(tmp_path / "absent")
    assert m.main([]) == 1
    assert "wiki not found" in capsys.readouterr().err


def test_unreadable_page_is_skipped(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/x", ["sources/x"])
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "x.md" and "entities" in self.parts:
            raise OSError("vanished")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert _load(v).census(v)["held_total"] == 0


def test_non_string_and_unresolvable_refs_do_not_crash(tmp_path):
    v = _vault(tmp_path)
    p = v / "wiki" / "entities" / "odd.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: actor\nname: N\nneeds_review: true\nsources:\n  - 42\n"
                 "  - Plain Prose Citation\n---\n\nb\n", encoding="utf-8")
    result = _load(v).census(v)
    assert [r["publisher"] for r in result["ranked"]] == ["Plain Prose Citation"]


def test_sources_not_a_list_is_treated_as_no_sources(tmp_path):
    v = _vault(tmp_path)
    p = v / "wiki" / "entities" / "scalar.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: actor\nname: N\nneeds_review: true\nsources: just-a-string\n---\n\nb\n",
                 encoding="utf-8")
    assert _load(v).census(v)["buckets"].get("no sources cited") == 1


def test_source_page_without_a_publisher_is_an_unresolvable_label(tmp_path):
    v = _vault(tmp_path)
    p = v / "wiki" / "sources" / "nopub.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\n---\n\nb\n", encoding="utf-8")
    _held(v, "entities/np", ["sources/nopub"])
    result = _load(v).census(v)
    assert result["buckets"].get("no sources cited") == 1     # no label resolved at all


def test_structural_files_are_not_censused(tmp_path):
    v = _vault(tmp_path)
    for name in ("_scratch.md", "INDEX.md", "page.bak.md"):
        p = v / "wiki" / name
        p.write_text("---\ntype: actor\nname: N\nneeds_review: true\n---\n\nb\n", encoding="utf-8")
    assert _load(v).census(v)["held_total"] == 0


def test_artifact_section_and_warning_are_emitted(tmp_path, capsys):
    v = _vault(tmp_path)
    _held(v, "entities/artifact", ["log.md"])
    m = _load(v)
    assert m.main([]) == 0
    assert "cite a vault artifact as a source" in capsys.readouterr().err
    text = _report(v)
    assert "Vault artifacts cited as sources" in text and "`log.md`" in text


def test_source_page_that_vanishes_while_labelling_is_skipped(tmp_path, monkeypatch):
    """The label pass re-reads each cited source page; a reshelve can remove one between the
    resolve and the read. One lost race must not abort the census for every other page."""
    v = _vault(tmp_path)
    _source(v, "sources/gone", "Some Outlet")
    _held(v, "entities/x", ["sources/gone"])
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "gone.md":
            raise OSError("vanished")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    result = _load(v).census(v)
    assert result["held_total"] == 1
    assert result["buckets"].get("no sources cited") == 1


def test_regex_match_but_parsed_flag_not_true_is_skipped(tmp_path):
    """The cheap regex prefilter can match a `needs_review: true` line that YAML then overrides
    (duplicate key — last wins). The parsed value is the authority."""
    v = _vault(tmp_path)
    p = v / "wiki" / "entities" / "dup.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: actor\nname: N\nneeds_review: true\nneeds_review: false\n---\n\nb\n",
                 encoding="utf-8")
    assert _load(v).census(v)["held_total"] == 0


def test_a_projection_respects_a_raised_bar(tmp_path):
    """A pack demanding 2xA gets no single-publisher A-clear promise from a page citing one."""
    v = _vault(tmp_path, SCHEMA.replace("  always_publish_types: []\n",
                                    "  always_publish_types: []\n  a_sources: 2\n"))
    _source(v, "sources/x", "Ungraded Outlet")
    _held(v, "entities/x", ["sources/x"])
    r = _load(v).census(v)["ranked"][0]
    assert r["pages_blocked"] == 1
    assert r["clears_if_graded_a"] == 0


def test_emits_wake_agent_false(tmp_path, capsys):
    v = _vault(tmp_path)
    m = _load(v)
    assert m.main([]) == 0
    assert '{"wakeAgent": false}' in capsys.readouterr().out


def test_always_published_types_are_not_census_subjects(tmp_path):
    """An actor page is never held, so it must not appear in the census or skew the publisher
    ranking. Before this, the census counted 857 pages as "blocked by an ungraded publisher" that
    the lane publishes regardless -- ranking publishers by a bar nobody applies to them (#554)."""
    on = SCHEMA.replace("  always_publish_types: []\n", "  always_publish_types: [actor]\n")
    v = _vault(tmp_path, on)
    _source(v, "sources/unknown-pub", "Acme Threat Labs")
    _held(v, "entities/an-actor", ["sources/unknown-pub"], ptype="actor")
    _held(v, "entities/a-malware", ["sources/unknown-pub"], ptype="malware")
    result = _load(v).census(v)
    assert result["held_total"] == 1, "only the still-holdable type counts"
    blocked = sum(e["pages_blocked"] for e in result["ranked"])
    assert blocked == 1, "the actor page must not inflate the publisher ranking"
