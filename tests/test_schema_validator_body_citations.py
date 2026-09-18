"""okengine#563 — the write path must refuse an entity page whose evidence lives only in body prose.

`sources:` frontmatter is the single location every consumer reads: the grading lane
(review_autoverify._grade_evidence) and both render surfaces. A page citing `[[sources/...]]` under
a `## Sources` heading with no `sources:` key carries real, checkable evidence that nothing can see
— on one live vault, 40 entity pages graded as unsourced and rendered quarantined while citing
named publishers.

This is the graduation-rule detector for that class: enforced at the write path rather than asked
for in a prompt, because prompts guide and the write path enforces.
"""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "tools" / "schema_validator.py"

SCHEMA = """\
apply_under: [wiki/]
types:
  actor: {required: [type, name]}
  source: {required: [type]}
  dashboard: {required: [type]}
  briefing: {required: [type]}
  trend: {required: [type]}
  concept: {required: [type]}
"""


def _load():
    spec = importlib.util.spec_from_file_location("schema_validator", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    return tmp_path


def _src(vault, rel):
    """A real source page, so the guard's resolution check has something to find."""
    p = vault / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\nid: sources:x\n---\n\nx\n", encoding="utf-8")


def _page(vault, rel, fm_lines, body):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    content = "---\n" + fm_lines + "---\n\n" + body
    p.write_text(content, encoding="utf-8")
    return str(p), content


def _reject(vault, rel, fm_lines, body):
    m = _load()
    # clear the schema cache so each test's fixture is re-read
    for c in ("_SCHEMA_CACHE", "_BASE_CACHE"):
        getattr(m, c, {}).clear()
    path, content = _page(vault, rel, fm_lines, body)
    return m.conformance_reject_reason(path, content)


def test_entity_citing_sources_only_in_body_is_rejected(vault):
    _src(vault, "sources/2026/07/07/register-story")
    r = _reject(vault, "entities/z/z-pentest.md", "type: actor\nname: Z\nid: entities:z\n",
                "## Sources\n- [[sources/2026/07/07/register-story]] — The Register\n")
    assert r and "body prose" in r, r
    assert "sources:" in r, "the message must name the field to fix"


def test_the_same_page_passes_once_the_field_exists(vault):
    """The positive half — otherwise the rule above could be satisfied by rejecting everything."""
    _src(vault, "sources/2026/07/07/register-story")
    r = _reject(vault, "entities/z/z-pentest.md",
                "type: actor\nname: Z\nid: entities:z\nsources:\n- sources/2026/07/07/register-story\n",
                "## Sources\n- [[sources/2026/07/07/register-story]] — The Register\n")
    assert r is None, r


def test_entity_with_no_citations_at_all_is_not_rejected_by_this_rule(vault):
    """This guard is about citation LOCATION, not about requiring citations. Making `sources:`
    mandatory is a separate product decision and would reject legitimately unsourced stubs."""
    assert _reject(vault, "entities/k/kongtuke.md", "type: actor\nname: K\nid: entities:k\n",
                   "Some prose with no citations.\n") is None


def test_dashboards_are_exempt(vault):
    """A dashboard is a GENERATED aggregate view: its body list of source records is a rendering,
    not a citation, and the next regeneration would overwrite a lifted field anyway."""
    _src(vault, "sources/a")
    assert _reject(vault, "dashboards/overview.md", "type: dashboard\nid: dashboards:o\n",
                   "## Sources\n- [[sources/a]]\n") is None


def test_non_entity_pages_are_NOT_rejected_by_the_guard(vault):
    """The guard is deliberately narrower than the repair. It REJECTS a write, and outside
    entities/ body links are an established, tested contract: a briefing narrates and cites inline,
    and okengine-mcp already enforces those links resolve (the briefing dead-link guard). Widening
    the guard here broke four write_server briefing tests — i.e. it would have broken a working
    lane in production.

    The repair (scripts/cron/source_normalize) still normalises these pages; the asymmetry is safe
    in that direction, because fixing more than is enforced can never make a page unwritable."""
    _src(vault, "sources/a")
    for rel, ty in (("briefings/weekly.md", "briefing"), ("trends/theme-x.md", "trend"),
                    ("concepts/c.md", "concept")):
        assert _reject(vault, rel, f"type: {ty}\nid: x:y\n",
                       "## Sources\n- [[sources/a]]\n") is None, rel


def test_source_record_referencing_another_source_is_not_rejected(vault):
    """A source page IS the primary document; body links on it are references, not its provenance.
    It also lives outside entities/, so the scope rule covers it."""
    assert _reject(vault, "sources/story.md", "type: source\nid: sources:s\n",
                   "See also [[sources/other]]\n") is None


def test_non_source_wikilinks_do_not_trigger_the_rule(vault):
    assert _reject(vault, "entities/e/ent.md", "type: actor\nname: E\nid: entities:e\n",
                   "related: [[entities/a/other]] and [[concepts/x]]\n") is None


def test_rejection_message_lists_the_offending_refs(vault):
    for s in ("sources/one", "sources/two", "sources/three", "sources/four"):
        _src(vault, s)
    r = _reject(vault, "entities/m/multi.md", "type: actor\nname: M\nid: entities:m\n",
                "[[sources/one]] [[sources/two]] [[sources/three]] [[sources/four]]\n")
    assert r and "4 source(s)" in r, r
    assert "sources/one" in r and "…" in r, "shows the first few then truncates"


def test_runtime_gate_also_rejects_so_the_write_path_actually_enforces(vault):
    """conformance_reject_reason is the strict gate; schema_reject_reason is what the enforced MCP
    write path calls. A guard that only fires in the release gate would let the corpus keep
    accumulating the defect between releases."""
    m = _load()
    for c in ("_SCHEMA_CACHE", "_BASE_CACHE"):
        getattr(m, c, {}).clear()
    _src(vault, "sources/a")
    path, content = _page(vault, "entities/z/z2.md", "type: actor\nname: Z\nid: entities:z\n",
                          "## Sources\n- [[sources/a]]\n")
    assert m.schema_reject_reason(path, content), "the runtime write gate must reject it too"


def test_dangling_body_ref_does_not_trip_the_guard(vault):
    """The guard and its repair must agree on scope. source_normalize refuses to promote a link to
    a page that does not exist — that would manufacture a citation to nothing — so if the guard
    fired on dangling refs those pages could never reach a clean state: rejected forever with no
    legal fix. A dangling ref is a separate data-quality problem with its own detector."""
    assert _reject(vault, "entities/d/dangling.md", "type: actor\nname: D\nid: entities:d\n",
                   "## Sources\n- [[sources/does/not/exist]]\n") is None


def test_guard_fires_only_on_the_resolving_subset(vault):
    """Mixed page: one dangling ref, one real one. The real one still makes it rejectable, so
    narrowing to resolving refs must not become a way to smuggle body-only citations past."""
    _src(vault, "sources/real")
    r = _reject(vault, "entities/x/mixed.md", "type: actor\nname: X\nid: entities:x\n",
                "[[sources/does/not/exist]] and [[sources/real]]\n")
    assert r and "1 source(s)" in r, r
    assert "sources/real" in r and "does/not/exist" not in r


# --- okengine#563: a page-level grade may not contradict its own evidence -------------------------
def test_reliability_worse_than_its_own_basis_is_rejected(vault):
    """21 pages on one live vault asserted `F` ("unreliable") while citing MITRE ATT&CK and
    Microsoft. Admiralty reliability grades a SOURCE; on a knowledge page it is a snapshot that
    never moves as evidence accumulates, so the trust strip shows the stale, weaker claim."""
    r = _reject(vault, "entities/a/apt.md",
                "type: actor\nname: A\nid: entities:a\nreliability: F\n"
                'auto_verified_basis: "2 A-grade sources: MITRE ATT&CK, Microsoft"\n', "prose\n")
    assert r and "contradicts its own" in r, r
    assert "remove `reliability`" in r, "the message must state the fix"


def test_a_grade_agreeing_with_the_evidence_passes(vault):
    """Only self-contradiction is rejected — a grade consistent with the evidence is untouched, so
    a pack that deliberately grades entities is not broken."""
    assert _reject(vault, "entities/a/ok.md",
                   "type: actor\nname: A\nid: entities:ok\nreliability: A\n"
                   'auto_verified_basis: "1 A-grade source: Microsoft"\n', "prose\n") is None


def test_a_grade_better_than_the_evidence_is_not_rejected(vault):
    """Better-than-evidence is a different (weaker) claim and not the measured defect; rejecting it
    would block pages this guard has no evidence about."""
    assert _reject(vault, "entities/a/better.md",
                   "type: actor\nname: A\nid: entities:bt\nreliability: A\n"
                   'auto_verified_basis: "2 B-grade sources: X, Y"\n', "prose\n") is None


def test_reliability_without_a_basis_is_not_rejected(vault):
    """No basis means nothing to contradict — otherwise the guard would fire on every page that
    carries a grade before the evidence lane has ever run."""
    assert _reject(vault, "entities/a/nobasis.md",
                   "type: actor\nname: A\nid: entities:nb\nreliability: F\n", "prose\n") is None


def test_non_entity_pages_are_out_of_scope(vault):
    """A `source` record's reliability IS its own outlet grade — a different meaning entirely."""
    assert _reject(vault, "sources/s.md",
                   "type: source\nid: sources:s\nreliability: C\n"
                   'auto_verified_basis: "A-grade publisher: Microsoft"\n', "prose\n") is None


def test_runtime_write_gate_enforces_the_grade_rule_too(vault):
    """schema_reject_reason is what the enforced MCP write path calls; a guard that only fires in
    the release gate would let the corpus keep accumulating the defect between releases."""
    m = _load()
    for c in ("_SCHEMA_CACHE", "_BASE_CACHE"):
        getattr(m, c, {}).clear()
    path, content = _page(vault, "entities/a/rt.md",
                          "type: actor\nname: A\nid: entities:rt\nreliability: C\n"
                          'auto_verified_basis: "1 A-grade source: Microsoft"\n', "prose\n")
    assert m.schema_reject_reason(path, content), "the runtime write gate must reject it too"


def test_a_body_ref_that_is_not_a_safe_path_is_never_walked(vault):
    """A citation is not a filesystem instruction. Prose inside `[[...]]` can carry `..`, an absolute
    path or an overlong component; stat-ing those on a WRITE-PATH check is how a guard turns into a
    traversal, and an overlong component raises rather than returning False, which would abort the
    write of an otherwise valid page."""
    _src(vault, "sources/2026/07/07/register-story")
    body = ("## Sources\n"
            "- [[sources/../../../etc/passwd]]\n"
            "- [[/sources/absolute]]\n"
            "- [[sources/./relative]]\n"
            f"- [[sources/{'n' * 300}]]\n")
    r = _reject(vault, "entities/z/unsafe.md", "type: actor\nname: Z\nid: entities:z\n", body)
    assert r is None, ("no unsafe ref resolves, so the page cites nothing this rule can lift: %r" % r)


def test_a_body_ref_that_cannot_be_stat_ed_is_not_counted(vault, monkeypatch):
    """An I/O failure is not evidence that a page exists. Counting it would reject a write on the
    strength of an error — and the repair lane could never clear it, because it refuses to promote
    a ref it cannot resolve either."""
    m = _load()
    for c in ("_SCHEMA_CACHE", "_BASE_CACHE"):
        getattr(m, c, {}).clear()
    _src(vault, "sources/2026/07/07/register-story")
    real = m.Path.is_file

    def is_file(self):
        if "register-story" in self.as_posix():
            raise OSError(5, "EIO")
        return real(self)

    monkeypatch.setattr(m.Path, "is_file", is_file)
    path, content = _page(vault, "entities/z/eio.md", "type: actor\nname: Z\nid: entities:z\n",
                          "## Sources\n- [[sources/2026/07/07/register-story]]\n")
    assert m.conformance_reject_reason(path, content) is None
