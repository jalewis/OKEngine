"""Contracts of the evidence-graded review lane that the original suite left unpinned.

Every test runs the real lane over a real temporary vault and reads back the page it wrote. The only
monkeypatch is a delegating `Path.rglob` that fixes the filesystem's arbitrary directory order, so a
test that needs one judgment page visited after another is deterministic.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "review_autoverify.py"

SCHEMA = """\
source_registry:
  Microsoft: {reliability: A}
  MITRE ATT&CK: {reliability: A}
  CISA: {reliability: A}
  MISP galaxy: {reliability: B}
  URLhaus: {reliability: B}
  Malpedia: {reliability: B}
types:
  actor: {required: [type, name]}
  source: {required: [type]}
"""
GOVERNED = SCHEMA + "review_autoverify:\n  defer_confidence_to_types: [assessment]\n"


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("review_autoverify_mutation", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    sl = sys.modules.get("schema_lib")
    if sl is not None:
        for cache in ("_SCHEMA_CACHE", "_BASE_CACHE", "_COMPOSED_CACHE"):
            getattr(sl, cache, {}).clear()
    return m


def _vault(tmp_path: Path, schema: str = SCHEMA) -> Path:
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    (tmp_path / "wiki").mkdir(exist_ok=True)
    return tmp_path


def _page(v: Path, rel: str, fm: dict, body: str = "body\n") -> Path:
    p = v / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body,
                 encoding="utf-8")
    return p


def _raw(v: Path, rel: str, text: str) -> Path:
    p = v / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _fm(p: Path) -> dict:
    return yaml.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))


def _run(v: Path, capsys) -> list[str]:
    assert _load(v).main([]) == 0
    return capsys.readouterr().out.splitlines()


def _summary(lines: list[str]) -> str:
    return next(line for line in lines if line.startswith("review-autoverify:"))


# --- the evidence bar -----------------------------------------------------------------------------

@pytest.mark.parametrize("sources, cleared", [
    (["Microsoft"], True),                                 # 1xA is the default bar
    (["MISP galaxy"], False),                              # 1xB is below it
    (["MISP galaxy", "URLhaus"], True),                    # 2xB meets it exactly
    (["MISP galaxy", "URLhaus", "Malpedia"], True),        # 3xB exceeds it
])
def test_the_default_bar_for_a_held_type_is_one_A_or_two_B(tmp_path, sources, cleared):
    v = _vault(tmp_path)
    page = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "Cobalt", "needs_review": True,
                                          "sources": sources})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert ("needs_review" not in fm) is cleared
    assert (fm.get("review_status") == "auto-verified") is cleared


@pytest.mark.parametrize("sources, band", [
    (["Microsoft", "MITRE ATT&CK", "CISA"], "confirmed"),
    (["MISP galaxy", "URLhaus", "Malpedia"], "high"),
])
def test_more_corroboration_than_a_band_needs_still_earns_it(tmp_path, sources, band):
    v = _vault(tmp_path)
    page = _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "needs_review": True,
                                          "sources": sources})
    assert _load(v).main([]) == 0
    assert _fm(page)["attribution_confidence"] == band
    assert _fm(page)["consensus"] == 3


# --- which pages carry the review latch ----------------------------------------------------------

def test_a_latch_yaml_reads_true_but_the_lane_cannot_rewrite_is_left_alone(tmp_path):
    v = _vault(tmp_path)
    page = _raw(v, "tools/y/yes.md",
                "---\ntype: tool\nname: Yes\nneeds_review: yes\nsources: [Microsoft]\n---\n\nb\n")
    before = page.read_text()
    assert _load(v).main([]) == 0
    assert page.read_text() == before


def test_a_latch_the_parser_reads_as_non_boolean_is_left_alone(tmp_path):
    v = _vault(tmp_path)
    page = _raw(v, "tools/d/dup.md", "---\ntype: tool\nname: Dup\nneeds_review: true\n"
                                     "needs_review: 1\nsources: [Microsoft]\n---\n\nb\n")
    before = page.read_text()
    assert _load(v).main([]) == 0
    assert page.read_text() == before


# --- refusals -------------------------------------------------------------------------------------

@pytest.mark.parametrize("ptype", ["tool", "access-broker"])
def test_actor_identity_classification_applies_only_to_actor_pages(tmp_path, ptype):
    v = _vault(tmp_path)
    page = _page(v, f"tools/c/{ptype}.md",
                 {"type": ptype, "name": "Cobalt Strike", "needs_review": True,
                  "sources": ["Microsoft"]},
                 body="Cobalt Strike is a commercial tool sold to penetration testers.\n")
    assert _load(v).main([]) == 0
    assert _fm(page)["review_status"] == "auto-verified"
    actor = _page(v, "entities/c/cobalt-strike.md",
                  {"type": "actor", "name": "Cobalt Strike", "needs_review": True,
                   "sources": ["Microsoft"]},
                  body="Cobalt Strike is a commercial tool sold to penetration testers.\n")
    assert _load(v).main([]) == 0
    assert _fm(actor)["needs_review"] is True          # the same evidence does hold an actor


def test_an_actor_title_is_the_name_checked_against_its_evidence(tmp_path, capsys):
    v = _vault(tmp_path)
    page = _page(v, "entities/t/terminal-fix-page.md",
                 {"type": "actor", "title": "TerminalFix", "name": "Terminal Fix Crew",
                  "needs_review": True, "sources": ["Microsoft"]},
                 body="TerminalFix is a ClickFix variant seen in the wild.\n")
    lines = _run(v, capsys)
    assert _fm(page)["needs_review"] is True
    assert ("  held  entities/t/terminal-fix-page.md: actor evidence defines a clickfix variant, "
            "not an adversarial agent") in lines


def test_an_actor_without_a_title_is_checked_by_its_name(tmp_path):
    v = _vault(tmp_path)
    page = _page(v, "entities/t/terminal-fix-page.md",
                 {"type": "actor", "name": "TerminalFix", "needs_review": True,
                  "sources": ["Microsoft"]},
                 body="TerminalFix is a ClickFix variant seen in the wild.\n")
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True


def test_only_an_explicit_false_marks_actor_identity_unvalidated(tmp_path):
    v = _vault(tmp_path)
    page = _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "needs_review": True,
                                          "actor_identity_validated": 0,
                                          "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["review_status"] == "auto-verified"


# --- source references ----------------------------------------------------------------------------

def _publisher_page(v: Path, rel: str) -> None:
    _page(v, rel, {"type": "source", "publisher": "Microsoft"})


def test_a_reference_through_a_255_byte_directory_resolves(tmp_path):
    v = _vault(tmp_path)
    long_dir = "d" * 255
    _publisher_page(v, f"sources/{long_dir}/ms.md")
    page = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "needs_review": True,
                                          "sources": [f"sources/{long_dir}/ms"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["review_status"] == "auto-verified"


def test_a_reference_with_a_256_byte_part_is_ungraded_prose(tmp_path):
    """An over-long part is refused before the filesystem is consulted; the basename fallback must
    not resolve it to some other page."""
    v = _vault(tmp_path)
    _publisher_page(v, "sources/x/ms-unique.md")
    page = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "needs_review": True,
                                          "sources": [f"sources/{'d' * 256}/ms-unique"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True


def _key_of(nbytes: int) -> str:
    """A `sources/...` reference of exactly `nbytes` bytes whose every part is short."""
    tail = "/ms-unique"
    body = nbytes - len("sources") - len(tail)
    parts = []
    while body > 0:
        take = min(200, body - 1) if body > 1 else 0
        if take <= 0:
            break
        parts.append("p" * take)
        body -= take + 1
    key = "sources/" + "/".join(parts) + tail
    while len(key) < nbytes:                          # pad the first part to the exact size
        key = key.replace("sources/p", "sources/pp", 1)
    assert len(key.encode("utf-8")) == nbytes
    return key


def _is_file_tolerates_overlong_paths() -> bool:
    """Whether this interpreter's `Path.is_file` answers False for a path over PATH_MAX (3.14 does)
    instead of raising ENAMETOOLONG (3.12 does)."""
    try:
        Path("/" + "x" * 5000).is_file()
    except OSError:
        return False
    return True


def test_a_4096_byte_reference_passes_the_length_guard(tmp_path):
    """4096 bytes is inside the guard, so the key reaches the filesystem. The full path is over
    PATH_MAX: where `is_file` tolerates that, the unique-basename fallback resolves it; where it
    raises, the reference is ungraded. Either way the guard itself did not refuse it."""
    v = _vault(tmp_path)
    _publisher_page(v, "sources/x/ms-unique.md")
    page = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "needs_review": True,
                                          "sources": [_key_of(4096)]})
    assert _load(v).main([]) == 0
    if _is_file_tolerates_overlong_paths():
        assert _fm(page)["review_status"] == "auto-verified"
    else:
        assert _fm(page)["needs_review"] is True


def test_a_4097_byte_reference_is_ungraded_prose(tmp_path):
    v = _vault(tmp_path)
    _publisher_page(v, "sources/x/ms-unique.md")
    page = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "needs_review": True,
                                          "sources": [_key_of(4097)]})
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True


# --- the reliability contradiction ----------------------------------------------------------------

def test_only_an_entity_page_loses_a_reliability_its_basis_outranks(tmp_path, capsys):
    v = _vault(tmp_path)
    entity = _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "reliability": "C",
                                            "needs_review": True, "sources": ["Microsoft"]})
    tool = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "reliability": "C",
                                          "needs_review": True, "sources": ["Microsoft"]})
    lines = _run(v, capsys)
    assert "reliability" not in _fm(entity)
    assert _fm(tool)["reliability"] == "C"
    assert ("  drop entities/a/apt.md: page reliability C contradicts the A-grade basis "
            "being stamped") in lines


# --- re-entry of already-stamped pages ------------------------------------------------------------

def _stamped(v: Path, rel: str, *, basis: str, consensus, sources: list[str],
             status: str = "auto-verified", **extra) -> Path:
    fm = {"type": "actor", "name": "APT", "sources": sources, "review_status": status,
          "auto_verified_basis": basis, "attribution_confidence": "confirmed",
          "auto_verified_at": "2026-01-01T00:00:00Z", **extra}
    if consensus is not None:
        fm["consensus"] = consensus
    return _page(v, rel, fm)


@pytest.mark.parametrize("stored, cited, expected", [
    ("1 A-grade source: Microsoft", "MITRE ATT&CK", "1 A-grade source: MITRE ATT&CK"),
    ("1 A-grade source: MITRE ATT&CK", "Microsoft", "1 A-grade source: Microsoft"),
])
def test_a_stale_basis_is_restamped_in_either_lexical_direction(tmp_path, stored, cited, expected):
    v = _vault(tmp_path)
    page = _stamped(v, "entities/a/apt.md", basis=stored, consensus=1, sources=[cited])
    assert _load(v).main([]) == 0
    assert _fm(page)["auto_verified_basis"] == expected


@pytest.mark.parametrize("stored", [5, 0, None])
def test_a_stale_consensus_is_restamped_in_either_direction(tmp_path, stored):
    v = _vault(tmp_path)
    page = _stamped(v, "entities/a/apt.md", basis="1 A-grade source: Microsoft",
                    consensus=stored, sources=["Microsoft"])
    assert _load(v).main([]) == 0
    assert _fm(page)["consensus"] == 1


def test_a_mixed_stamp_is_restamped_to_the_state_its_basis_implies(tmp_path):
    v = _vault(tmp_path)
    page = _stamped(v, "entities/a/apt.md", basis="1 A-grade source: Microsoft", consensus=1,
                    sources=["Microsoft"], status="unverified-no-evidence")
    assert _load(v).main([]) == 0
    assert _fm(page)["review_status"] == "auto-verified"


def test_a_settled_page_with_hundreds_of_publishers_is_not_rewritten(tmp_path, capsys):
    """Equality of counts, not identity: a count above the small-int cache is still settled."""
    publishers = [f"Pub {i:03d}" for i in range(300)]
    registry = "".join(f"  {p}: {{reliability: B}}\n" for p in publishers)
    v = _vault(tmp_path, "source_registry:\n" + registry + "types: {}\n")
    page = _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "needs_review": True,
                                          "sources": publishers})
    assert "  clear entities/a/apt.md: " in "\n".join(_run(v, capsys))
    assert _fm(page)["consensus"] == 300
    second = _run(v, capsys)
    assert not [line for line in second if line.startswith("  clear ")]


def test_a_settled_page_does_not_stop_later_pages_from_clearing(tmp_path):
    v = _vault(tmp_path)
    settled = _stamped(v, "entities/a/aaa.md", basis="1 A-grade source: Microsoft", consensus=1,
                       sources=["Microsoft"])
    before = settled.read_text()
    later = _page(v, "entities/z/zzz.md", {"type": "actor", "name": "Z", "needs_review": True,
                                           "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    assert settled.read_text() == before
    assert _fm(later)["review_status"] == "auto-verified"


def test_a_hollow_claim_that_is_now_refused_is_reported_with_its_reason(tmp_path, capsys):
    v = _vault(tmp_path)
    _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "sources": ["Microsoft"],
                                   "review_status": "auto-verified",
                                   "conflicts": [{"field": "origin"}]})
    lines = _run(v, capsys)
    assert ("  stale-claim entities/a/apt.md: still stamped auto-verified with no basis "
            "(conflicts present)") in lines
    assert "1 stale claim(s) unresolved" in _summary(lines)


def test_a_hollow_claim_without_a_refusal_has_no_reason_suffix(tmp_path, capsys):
    v = _vault(tmp_path)
    _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "sources": ["MISP galaxy"],
                                   "review_status": "auto-verified"})
    lines = _run(v, capsys)
    assert "  stale-claim tools/c/cobalt.md: still stamped auto-verified with no basis" in lines


def test_a_stamp_that_no_longer_clears_says_so(tmp_path, capsys):
    v = _vault(tmp_path)
    _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "sources": ["MISP galaxy"],
                                   "review_status": "auto-verified",
                                   "auto_verified_basis": "1 A-grade source: Microsoft"})
    _page(v, "tools/d/dagger.md", {"type": "tool", "name": "D", "sources": ["Microsoft"],
                                   "review_status": "auto-verified",
                                   "auto_verified_basis": "1 A-grade source: Microsoft",
                                   "conflicts": [{"field": "origin"}]})
    lines = _run(v, capsys)
    assert ("  now-held tools/c/cobalt.md: no longer clears the bar "
            "(existing stamp left in place)") in lines
    assert ("  now-held tools/d/dagger.md: conflicts present "
            "(existing stamp left in place)") in lines


# --- summary counters -----------------------------------------------------------------------------

def test_the_summary_counts_each_outcome_exactly_once(tmp_path, capsys):
    v = _vault(tmp_path, GOVERNED)
    _page(v, "entities/n/no-evidence.md", {"type": "actor", "name": "N", "needs_review": True,
                                           "sources": []})
    _page(v, "entities/g/governed.md", {"type": "actor", "name": "G", "needs_review": True,
                                        "sources": ["Microsoft"]})
    _page(v, "assessments/a/judgment.md", {"type": "assessment",
                                           "subject": "[[entities/g/governed]]"})
    lines = _run(v, capsys)
    assert _summary(lines) == (
        "review-autoverify: 1 cleared on evidence, 1 published unverified (no graded evidence), "
        "1 deferred to an analytic judgment, 0 held for human review, "
        "0 stale claim(s) unresolved (bar: 1xA or 2xB)")


# --- governing judgments --------------------------------------------------------------------------

def test_only_a_governing_type_names_a_governed_subject(tmp_path):
    v = _vault(tmp_path, GOVERNED)
    page = _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "needs_review": True,
                                          "sources": ["Microsoft"]})
    _page(v, "notes/n/note.md", {"type": "note", "subject": "[[entities/a/apt]]"})
    assert _load(v).main([]) == 0
    assert _fm(page)["attribution_confidence"] == "confirmed"


def test_unreadable_and_foreign_pages_do_not_end_the_governing_scan(tmp_path, monkeypatch):
    v = _vault(tmp_path, GOVERNED)
    page = _page(v, "entities/a/apt.md", {"type": "actor", "name": "APT", "needs_review": True,
                                          "sources": ["Microsoft"]})
    wiki = v / "wiki"
    first = [
        _page(v, "notes/_draft.md", {"type": "assessment", "subject": "x"}),
        wiki / "notes" / "folder.md",
        _raw(v, "notes/plain.md", "no frontmatter here\n"),
        _page(v, "notes/n/note.md", {"type": "note", "subject": "y"}),
    ]
    (wiki / "notes" / "folder.md").mkdir()
    governing = _page(v, "zz/judgment.md", {"type": "assessment",
                                            "subject": "[[entities/a/apt]]"})
    real_rglob = Path.rglob

    def ordered(self, pattern, *args, **kwargs):
        found = list(real_rglob(self, pattern, *args, **kwargs))
        rank = {p: i for i, p in enumerate(first)}
        rank[governing] = len(first) + 1
        return iter(sorted(found, key=lambda p: (rank.get(p, len(first)), str(p))))

    monkeypatch.setattr(Path, "rglob", ordered)
    assert _load(v).main([]) == 0
    assert "attribution_confidence" not in _fm(page)


# --- module wiring --------------------------------------------------------------------------------

def test_loading_the_lane_puts_its_own_directory_first_on_the_path(tmp_path, monkeypatch):
    here = str(MOD.resolve().parent)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != here])   # earlier tests add it
    _load(_vault(tmp_path))
    assert sys.path[0] == here


def test_the_lane_runs_as_a_command(tmp_path):
    v = _vault(tmp_path)
    page = _page(v, "tools/c/cobalt.md", {"type": "tool", "name": "C", "needs_review": True,
                                          "sources": ["Microsoft"]})
    proc = subprocess.run([sys.executable, str(MOD)], capture_output=True, text=True,
                          timeout=120, check=False, env={**os.environ, "WIKI_PATH": str(v)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == '{"wakeAgent": false}'
    assert _fm(page)["review_status"] == "auto-verified"
