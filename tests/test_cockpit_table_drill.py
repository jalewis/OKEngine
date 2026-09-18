"""Cockpit table/cards drill-through — reaching the denominator a truncated box advertises.

A `view: table` box renders `limit` rows and a meta line naming the total ("showing 8 of 83
actors named in recent news"). Until okengine#564 that total was UNREACHABLE: `/api/drill`
served bars/chips/bignums/coverage and answered `400 box is not drillable` for a table, so 17
boxes on one live vault named a denominator the UI gave no way to open. A widget that states a
total it cannot show is a dead affordance — worse than one that stays quiet, because the number
reads as a promise.

Two contracts pinned here:

1. **The drill returns the box's OWN order.** It re-sorts with the box's `sort` (not by title
   like a group_by drill), so the first row past the fold is the row the table would have shown
   next. A drill that reorders answers a different question than the one the user clicked.
2. **The limit has ONE definition.** `_box_limit` is shared by the renderers, the meta line and
   the truncation flag. Three copies of `or 10` is how "showing 8 of 83" drifts from the count
   the table actually cut, and how the drill offers itself on a box that withheld nothing.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("yaml")

APP = Path(__file__).resolve().parent.parent / "okengine-cockpit" / "app.py"

CFG = """\
cockpit:
  tabs: [adv]
  tab_defs:
    adv:
      label: Adversaries
      boxes:
        - title: Recently active
          span: 7
          view: table
          dataset: {dir: entities, type: actor}
          sort: {field: seen, desc: true, then: news}
          limit: 3
          meta_template: "showing {shown} of {total} actors"
          columns:
            - {field: title, label: Actor, link: true}
        - title: Everything fits
          span: 5
          view: table
          dataset: {dir: entities, type: actor}
          limit: 50
          columns:
            - {field: title, label: Actor}
        - title: Themes
          span: 12
          view: cards
          dataset: {dir: trends}
          limit: 1
"""


def _load(vault, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def _mk(root, rel, fm):
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\nbody\n", encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(CFG, encoding="utf-8")
    # 5 actors, distinct `seen` dates -> a deterministic full ordering, 3 shown, 2 withheld.
    for slug, seen, news in [
        ("alpha", "2026-07-26", 3), ("bravo", "2026-07-25", 7), ("charlie", "2026-07-24", 2),
        ("delta", "2026-07-23", 1), ("echo", "2026-07-22", 9),
    ]:
        _mk(tmp_path, f"entities/{slug}.md",
            f"type: actor\ntitle: {slug.upper()}\nseen: '{seen}'\nnews: {news}\n")
    for n in range(4):
        _mk(tmp_path, f"trends/t{n}.md", f"type: trend\ntitle: T{n}\n")
    return _load(tmp_path, monkeypatch)


def test_truncated_table_offers_its_meta_line_as_the_way_in(vault):
    by = {b["title"]: b for b in vault.api_tab("adv")["boxes"]}
    box = by["Recently active"]
    assert box["meta"] == "showing 3 of 5 actors"          # the box states a denominator...
    assert box["meta_drill"] == {"tab": "adv", "box": 0}   # ...and now carries the way to reach it


def test_box_that_withholds_nothing_offers_no_drill(vault):
    """The affordance appears only when rows were actually cut. A meta line that opens a list
    identical to the table above it is noise dressed as a feature."""
    by = {b["title"]: b for b in vault.api_tab("adv")["boxes"]}
    assert "meta_drill" not in by["Everything fits"]       # limit 50 over 5 rows
    assert by["Everything fits"]["meta"] == "showing 5 of 5 records"


def test_drill_returns_every_row_in_the_boxs_own_order(vault):
    """Not the group_by drill's title sort: the box sorts `seen` desc, so the drill must too —
    otherwise row 4 of the drill is not the row the table would have shown 4th."""
    d = vault.api_drill("adv", 0)
    assert d["count"] == 5                                 # the full denominator, not the limit
    assert [p["title"] for p in d["pages"]] == ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO"]
    assert d["truncated"] is False
    assert d["title"] == "Recently active"                 # heading is the box the user clicked


def test_drill_results_carry_panel_context_not_only_identity(vault):
    """The complete-list overlay must retain the fields that made the table analytically useful."""
    row = {
        "_sub": "entities", "_rel": "zirconium", "type": "actor", "title": "ZIRCONIUM",
        "aliases": ["APT40", "Leviathan"], "news_last_seen": "2026-08-09",
        "recent_news": 7, "total_mentions": 31,
    }
    box = {"columns": [
        {"field": "title", "label": "Actor", "link": True},
        {"field": "aliases", "label": "Aliases", "max": 2},
        {"field": "news_last_seen", "label": "Seen", "date": True},
        {"field": "recent_news", "label": "News"},
        {"field": "total_mentions", "label": "Total"},
    ]}
    page = vault._row_page(row, box)
    assert page["path"] == "entities/zirconium"
    assert page["facts"] == [
        {"label": "Aliases", "value": "APT40, Leviathan"},
        {"label": "Seen", "value": "2026-08-09"},
        {"label": "News", "value": "7"},
        {"label": "Total", "value": "31"},
    ]


def test_aggregate_drill_results_receive_safe_common_fallback_context(vault):
    row = {"_sub": "sources", "_rel": "report", "type": "source", "title": "Report",
           "summary": "A concise finding.", "published": "2026-08-08", "publisher": "Talos"}
    page = vault._row_page(row, {})
    assert page["summary"] == "A concise finding."
    assert page["facts"] == [
        {"label": "Published", "value": "2026-08-08"},
        {"label": "Publisher", "value": "Talos"},
    ]


def test_cards_view_drills_too(vault):
    by = {b["title"]: b for b in vault.api_tab("adv")["boxes"]}
    assert by["Themes"]["meta_drill"] == {"tab": "adv", "box": 2}
    assert vault.api_drill("adv", 2)["count"] == 4


def test_drill_ignores_a_bucket_value_it_has_no_use_for(vault):
    """A table has no group_by bucket. A stray `value` from the shared client handler must not
    filter, and must not 400 — the request is well-formed, the parameter is simply irrelevant."""
    assert vault.api_drill("adv", 0, value="nonsense")["count"] == 5


def test_limit_has_a_single_definition(vault):
    """The renderers, the meta line and the truncation flag must all cut at the same depth. Each
    extra copy of the default is an independent chance for the widget and its own description to
    disagree about what was shown."""
    import re
    src = APP.read_text(encoding="utf-8")
    # the per-view defaults live in exactly one mapping
    assert vault._VIEW_DEFAULT_LIMIT == {"cards": 12, "table": 10, "coverage": 10}
    assert vault._box_limit({}, "table") == 10
    assert vault._box_limit({}, "cards") == 12
    assert vault._box_limit({}, "coverage") == 10
    assert vault._box_limit({}, "bars") == 8               # everything else
    assert vault._box_limit({"limit": 3}, "table") == 3    # explicit config always wins
    assert vault._box_limit({"limit": 0}, "table") == 10   # 0 is not a limit, it's absence
    # and NO renderer re-derives a default inline — this caught coverage cutting at an inline 10
    # while the meta path computed 8 for the same box.
    strays = [ln.strip() for ln in src.splitlines()
              if re.search(r'box\.get\("limit"\)\s*or\s*\d', ln)
              and "_VIEW_DEFAULT_LIMIT" not in ln]
    assert not strays, f"inline limit default bypasses _box_limit: {strays}"


def test_drill_caps_a_huge_list_and_says_so(vault, tmp_path, monkeypatch):
    """A capped list must never read as a complete one — `truncated` is what lets the overlay
    say "showing the first 300" instead of silently implying that is all there is."""
    for n in range(vault._DRILL_CAP + 5):
        _mk(tmp_path, f"entities/bulk{n:04d}.md", f"type: actor\ntitle: B{n:04d}\nseen: '2026-01-01'\n")
    v = _load(tmp_path, monkeypatch)
    d = v.api_drill("adv", 0)
    assert d["count"] == vault._DRILL_CAP + 10             # honest total...
    assert len(d["pages"]) == vault._DRILL_CAP             # ...capped payload...
    assert d["truncated"] is True                          # ...and the gap is declared


# ---------------------------------------------------------------------------
# The plain-text fact renderers behind a drill card.
#
# These decide what a user READS about a row, so the failure mode is not a
# crash — it is a card that states something the vault does not. Each of these
# pins a shape that must render as nothing rather than as a wrong fact.
# ---------------------------------------------------------------------------


def test_drill_text_never_stringifies_a_structured_payload(vault):
    """`str(dict)` on a card renders `{'kind': 'x', ...}` as if it were a finding. A nested payload
    has no honest one-line form, so it renders as nothing and the card omits the row."""
    assert vault._drill_text({"kind": "x", "score": 1}) == ""
    assert vault._drill_text([{"a": 1}, {"b": 2}]) == ""


def test_drill_text_bounds_a_list_and_drops_the_gaps(vault):
    """The bound is on what a card can carry. Empty elements are dropped rather than rendered as
    stray separators — `a, , b` reads as a missing value, not as a two-item list."""
    assert vault._drill_text(["alpha", "", "bravo", "charlie", "delta"]) == "alpha, bravo", (
        "the bound is on elements CONSIDERED, not on elements rendered — a list whose first three "
        "entries include a gap shows two, and does not reach past the bound to backfill")
    assert vault._drill_text(["alpha", {"x": 1}, "bravo"]) == "alpha, bravo"
    assert vault._drill_text([]) == "" and vault._drill_text(None) == ""


def test_an_unassessed_row_says_the_review_did_not_run(vault, monkeypatch):
    """The distinction this lane exists for: "no judgment recorded" is NOT "judged negative". A card
    that renders an absent assessment as a value invents a finding."""
    monkeypatch.setattr(vault, "_assessment_for_row", lambda row, spec: None)
    monkeypatch.setattr(vault, "_assessment_terminal_for_row",
                        lambda row, spec: {"state": "no-association-established"})
    assert vault._drill_assessment_fact({}, {}) == "No association established"

    monkeypatch.setattr(vault, "_assessment_terminal_for_row", lambda row, spec: None)
    assert vault._drill_assessment_fact({}, {}) == "Review not run"


def test_an_assessment_fact_carries_its_status_and_confidence(vault, monkeypatch):
    """A value without its epistemic status reads as settled fact. Both grading shapes — a numeric
    confidence and a qualitative band — have to survive to the card."""
    monkeypatch.setattr(vault, "_assessment_for_row", lambda row, spec: {
        "epistemic_status": "provisionally-assessed", "assessed_value": "cn",
        "confidence": 0.755})
    spec = {"labels": {"cn": "China"}}
    assert vault._drill_assessment_fact({}, spec) == (
        "China · provisionally assessed · 76% confidence")

    monkeypatch.setattr(vault, "_assessment_for_row", lambda row, spec: {
        "assessed_value": "cn", "confidence_band": "moderate"})
    assert vault._drill_assessment_fact({}, spec) == "China · assessed · moderate confidence"

    monkeypatch.setattr(vault, "_assessment_for_row", lambda row, spec: {"assessed_value": None})
    assert vault._drill_assessment_fact({}, spec) == "assessed · assessed", (
        "with no value the status IS the fact")


def test_a_column_fact_applies_the_columns_own_rendering(vault):
    """`_drill_column_fact` is the plain-text counterpart of the HTML cell. If the two disagree, the
    drill card contradicts the table it was opened from."""
    col_pct = {"field": "share", "label": "Share", "pct": True}
    assert vault._drill_column_fact({"share": 0.42}, col_pct) == "42%"
    assert vault._drill_column_fact({"share": 0.073}, col_pct) == "7.3%", "sub-10% keeps a decimal"
    assert vault._drill_column_fact({"share": "unknown"}, col_pct) == "unknown", (
        "a non-numeric value is shown as written, not dropped or crashed on")

    col_date = {"field": "seen", "label": "Seen", "date": True}
    assert vault._drill_column_fact({"seen": "2026-08-09T04:00:00Z"}, col_date) == "2026-08-09"
    assert vault._drill_column_fact({"seen": "sometime"}, col_date) == "", (
        "an unparsable date renders as nothing rather than as prose in a date column")

    assert vault._drill_column_fact({"u": "https://evil.example.com/x"},
                                    {"field": "u", "defang": True}) == "hxxps://evil[.]example[.]com/x"

    assert vault._drill_column_fact({"other": 1}, {"field": "missing"}) == ""


def test_a_column_fact_delegates_an_assessment_column(vault, monkeypatch):
    monkeypatch.setattr(vault, "_assessment_for_row", lambda row, spec: None)
    monkeypatch.setattr(vault, "_assessment_terminal_for_row",
                        lambda row, spec: {"state": "collection-required"})
    assert vault._drill_column_fact({}, {"assessment": {"kind": "actor-country-linkage"}}) == (
        "Collection required")


def test_a_fact_that_only_repeats_the_card_heading_is_not_a_fact(vault):
    """The heading is already on the card. Repeating it costs a fact slot that could have carried
    something the user does not already have."""
    row = {"_sub": "entities", "_rel": "zirconium", "type": "actor", "title": "ZIRCONIUM",
           "name": "zirconium", "recent_news": 7}
    page = vault._row_page(row, {"columns": [
        {"field": "name", "label": "Name"},
        {"field": "recent_news", "label": "News"},
    ]})
    assert page["facts"] == [{"label": "News", "value": "7"}]


def test_fallback_context_is_bounded(vault):
    """A card is a summary. An aggregate row with a dozen usable fields must not turn into a full
    frontmatter dump — the fallback stops at five."""
    row = {"_sub": "sources", "_rel": "r", "type": "source", "title": "R",
           "published": "2026-08-08", "news_last_seen": "2026-08-07", "last_seen": "2026-08-06",
           "first_seen": "2026-08-05", "due_date": "2026-08-04", "as_of": "2026-08-03",
           "status": "open", "severity": "high", "publisher": "Talos", "sector": "energy"}
    page = vault._row_page(row, {})
    assert [f["label"] for f in page["facts"]] == ["Published", "Seen", "Last seen",
                                                   "First seen", "Due"]


def test_ref_title_reads_the_whole_frontmatter_and_never_raises(vault, tmp_path, monkeypatch):
    """A fixed byte window silently missed `scattered-spider`, whose `title:` sits at line 67 behind
    a 60-alias list, and the board rendered the slug beside properly titled peers. Every way of
    failing to find a title has to yield "" — the caller's fallback — and never an exception on a
    render path."""
    wiki = tmp_path / "wiki"

    _mk(tmp_path, "refs/no-fm.md".replace("refs/", "refs/"), "")
    (wiki / "refs" / "no-fm.md").write_text("# heading, no frontmatter\n", encoding="utf-8")
    assert vault._ref_title("refs/no-fm") == ""

    (wiki / "refs" / "untitled.md").write_text("---\ntype: actor\n---\nbody\n", encoding="utf-8")
    assert vault._ref_title("refs/untitled") == ""

    deep = "---\n" + "".join(f"alias_{n}: a{n}\n" for n in range(60)) + "title: Deep Title\n---\n"
    (wiki / "refs" / "deep.md").write_text(deep, encoding="utf-8")
    assert vault._ref_title("refs/deep") == "Deep Title"

    runaway = "---\n" + "".join(f"k{n}: v{n}\n" for n in range(500)) + "title: Too Late\n---\n"
    (wiki / "refs" / "runaway.md").write_text(runaway, encoding="utf-8")
    assert vault._ref_title("refs/runaway") == "", "unbounded frontmatter is not read unbounded"

    real_open = Path.open

    def boom(self, *a, **kw):
        if "unreadable" in self.as_posix():
            raise OSError(5, "EIO")
        return real_open(self, *a, **kw)

    (wiki / "refs" / "unreadable.md").write_text("---\ntitle: X\n---\n", encoding="utf-8")
    monkeypatch.setattr(Path, "open", boom)
    assert vault._ref_title("refs/unreadable") == ""


def test_an_assessment_column_contributes_a_fact_without_owning_a_field(vault, monkeypatch):
    """An assessment column names no `field` — it is resolved from the ledger. It must still be able
    to contribute a fact, and must not mark anything as "used" and shadow a fallback field."""
    monkeypatch.setattr(vault, "_assessment_for_row", lambda row, spec: None)
    monkeypatch.setattr(vault, "_assessment_terminal_for_row",
                        lambda row, spec: {"state": "collection-required"})
    row = {"_sub": "entities", "_rel": "zirconium", "type": "actor", "title": "ZIRCONIUM",
           "recent_news": 7}
    page = vault._row_page(row, {"columns": [
        {"label": "Linkage", "assessment": {"kind": "actor-country-linkage"}},
        {"field": "recent_news", "label": "News"},
    ]})
    assert page["facts"] == [{"label": "Linkage", "value": "Collection required"},
                             {"label": "News", "value": "7"}]


def test_an_unparsable_fallback_date_is_omitted_rather_than_shown_as_prose(vault):
    """A date column that cannot parse its value must not fall back to printing the raw string — a
    card reading "Published: sometime next quarter" is a fact the vault never recorded."""
    row = {"_sub": "sources", "_rel": "r", "type": "source", "title": "R",
           "published": "sometime next quarter", "publisher": "Talos"}
    page = vault._row_page(row, {})
    assert page["facts"] == [{"label": "Publisher", "value": "Talos"}]
