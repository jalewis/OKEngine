"""Grounding / anti-fabrication guards on the enforced write path (okengine#462, T1).

These are the guards that kept fabricated pages out of the vault during the
2026-07-24 local-model investigation: across many failing runs the models
confabulated receipts and invented citations, and **zero** bad pages landed.
That defence was, however, the least-covered part of the write path.

Covered here:

  _entity_sources()           the `sources:`/`source:` accessor the guards read
  _fabricated_source_reject() HARD-rejects the singular `source/` namespace --
                              the observed entity-backfill hallucination signature
  _missing_source_reject()    rejects NEW citations to non-existent source pages
  _future_date_reject()       rejects guessed/future record-keeping dates
  _degeneration_flags()       SOFT-flags repetition-loop word salad

Each is asserted in **both** directions: the rejection fires on the bad case, and
legitimate content is *not* blocked -- an over-eager guard here would brick real
importer writes, which is why the grandfathering rules exist.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"


# Pinned so the date guard is deterministic. Necessary, not merely tidy: several existing
# test modules set OKENGINE_MCP_WRITE_DATE through os.environ DIRECTLY rather than via
# monkeypatch, so the override leaks into every later test in the session and a guard test
# comparing against the real date.today() passes alone and fails in the full suite.
PINNED_TODAY = datetime.date(2026, 7, 15)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "sources" / "2026").mkdir(parents=True)
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\ntypes:\n  actor:\n    required: [type]\nstrict_types: false\n",
        encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", PINNED_TODAY.isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def entity_path(vault):
    return vault / "wiki" / "entities" / "acme.md"


# ── _entity_sources: the accessor both guards read ────────────────────────────

def test_entity_sources_accepts_list_scalar_and_the_singular_key(ws):
    mod, _ = ws
    assert mod._entity_sources({"sources": ["a", "b"]}) == ["a", "b"]
    assert mod._entity_sources({"sources": "a"}) == ["a"], "scalar promotes to a list"
    assert mod._entity_sources({"source": "a"}) == ["a"], "legacy singular key is read"
    assert mod._entity_sources({}) == []
    assert mod._entity_sources({"sources": None, "source": None}) == []
    assert mod._entity_sources({"sources": 7}) == [], "non-str scalars are ignored, not crashed on"


# ── _fabricated_source_reject: the hallucination signature ────────────────────

def test_singular_source_namespace_is_rejected_as_fabrication(ws):
    """Every observed entity-backfill hallucination cited `source/<vendor>/<slug>`.
    The schema's namespace is plural; singular is never valid."""
    mod, vault = ws
    reason = mod._fabricated_source_reject(
        entity_path(vault), {"sources": ["source/mandiant/apt35-report"]})
    assert reason and "singular `source/` namespace" in reason
    assert "fabrication signature" in reason


def test_plural_sources_are_never_touched_by_the_fabrication_guard(ws):
    """Plural forward-refs are legitimate (an importer may write the entity before
    its source in the same batch); corpus-audit catches plural dangling refs later."""
    mod, vault = ws
    assert mod._fabricated_source_reject(
        entity_path(vault), {"sources": ["sources/2026/not-yet-written"]}) is None


def test_provenance_labels_are_not_mistaken_for_page_refs(ws):
    mod, vault = ws
    for label in ("MITRE ATT&CK", "Unit 42", "internal telemetry"):
        assert mod._fabricated_source_reject(entity_path(vault), {"sources": [label]}) is None


def test_fabrication_guard_grandfathers_existing_bad_refs_on_update(ws):
    """The lane resends the full list, so only NEWLY-introduced fabrication is blocked --
    otherwise a page carrying a legacy bad ref would be frozen forever."""
    mod, vault = ws
    prev = {"sources": ["source/cisa/legacy-advisory"]}
    assert mod._fabricated_source_reject(entity_path(vault), prev, prev) is None

    added = {"sources": ["source/cisa/legacy-advisory", "source/mandiant/invented"]}
    reason = mod._fabricated_source_reject(entity_path(vault), added, prev)
    assert reason and "source/mandiant/invented" in reason
    assert "legacy-advisory" not in reason, "the grandfathered ref must not be re-reported"


def test_fabrication_guard_only_applies_to_entities(ws):
    mod, vault = ws
    source_page = vault / "wiki" / "sources" / "2026" / "x.md"
    assert mod._fabricated_source_reject(source_page, {"sources": ["source/bad/ref"]}) is None


def test_fabrication_guard_normalises_wrappers_before_matching(ws):
    mod, vault = ws
    for spelling in ("[[source/mandiant/x]]", "wiki/source/mandiant/x", "source/mandiant/x.md"):
        assert mod._fabricated_source_reject(entity_path(vault), {"sources": [spelling]}), spelling


# ── _missing_source_reject: citations must resolve ────────────────────────────

def test_new_citation_to_a_nonexistent_source_page_is_rejected(ws):
    mod, vault = ws
    reason = mod._missing_source_reject(
        entity_path(vault), {"sources": ["sources/2026/never-compiled"]})
    assert reason and "must cite existing canonical source pages" in reason
    assert "never invent or forward-reference evidence" in reason


def test_citation_to_an_existing_source_page_passes(ws):
    mod, vault = ws
    (vault / "wiki" / "sources" / "2026" / "real.md").write_text("# Real", encoding="utf-8")
    assert mod._missing_source_reject(
        entity_path(vault), {"sources": ["sources/2026/real"]}) is None


def test_missing_source_guard_grandfathers_and_ignores_non_source_refs(ws):
    mod, vault = ws
    prev = {"sources": ["sources/2026/broken"]}
    assert mod._missing_source_reject(entity_path(vault), prev, prev) is None, "grandfathered"

    # a provenance label is not a `sources/` path and must not be resolved
    assert mod._missing_source_reject(entity_path(vault), {"sources": ["MITRE ATT&CK"]}) is None


def test_patch_entity_enforces_source_provenance_guards(ws):
    """Surgical edits are a write chokepoint, not an escape hatch around citation validation."""
    mod, vault = ws
    page = mod._safe("entities/acme.md")
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: actor\nid: acme\nsources: []\nversion: 1\n---\nBody.\n", encoding="utf-8")

    singular = mod._patch(
        "entities/acme.md", "sources: []", "sources: [source/mandiant/invented]")
    assert "fabrication signature" in singular
    assert "source/mandiant/invented" not in page.read_text(encoding="utf-8")

    missing = mod._patch(
        "entities/acme.md", "sources: []", "sources: [sources/2026/not-created]")
    assert "must cite existing canonical source pages" in missing
    assert "sources/2026/not-created" not in page.read_text(encoding="utf-8")

    (vault / "wiki" / "sources" / "2026" / "real.md").write_text("# Real\n", encoding="utf-8")
    accepted = mod._patch(
        "entities/acme.md", "sources: []", "sources: [sources/2026/real]")
    assert accepted.startswith("patched entities/a/acme.md")


def test_patch_entity_rejects_operational_receipt_as_source_status(ws):
    mod, vault = ws
    page = vault / "wiki" / "sources" / "2026" / "receipt.md"
    page.write_text(
        "---\ntype: source\nstatus: draft\nversion: 1\n---\nBody.\n", encoding="utf-8")
    result = mod._patch(
        "sources/2026/receipt.md", "status: draft", "status: duplicate-receipt")
    assert "cannot store status duplicate-receipt as a source page" in result
    assert "status: draft" in page.read_text(encoding="utf-8")


# ── _future_date_reject: record-keeping dates ─────────────────────────────────

def test_future_record_dates_are_rejected(ws):
    mod, _ = ws
    future = (PINNED_TODAY + datetime.timedelta(days=30)).isoformat()
    reason = mod._future_date_reject({"published": future})
    assert reason and "is in the future" in reason
    assert "never a guessed or future one" in reason


def test_today_and_past_dates_pass_and_tomorrow_is_tolerated(ws):
    mod, _ = ws
    assert mod._future_date_reject({"published": PINNED_TODAY.isoformat()}) is None
    assert mod._future_date_reject({"created": "2020-01-01"}) is None
    # a one-day grace absorbs timezone skew between the agent and the vault host
    assert mod._future_date_reject(
        {"updated": (PINNED_TODAY + datetime.timedelta(days=1)).isoformat()}) is None


def test_future_date_guard_reads_date_datetime_and_timestamped_strings(ws):
    mod, _ = ws
    future_date = PINNED_TODAY + datetime.timedelta(days=10)
    assert mod._future_date_reject({"published": future_date})
    assert mod._future_date_reject(
        {"published": datetime.datetime.combine(future_date, datetime.time(12, 0))})
    assert mod._future_date_reject({"published": f"{future_date.isoformat()}T09:30:00Z"})


def test_future_date_guard_is_inert_when_the_date_override_is_unparseable(ws, monkeypatch):
    """Guard plumbing must never block a write: an unusable override disables the check
    rather than rejecting everything."""
    mod, _ = ws
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "not-a-date")
    assert mod._future_date_reject({"published": "2099-01-01"}) is None


def test_future_date_guard_ignores_unparseable_and_absent_values(ws):
    mod, _ = ws
    assert mod._future_date_reject({}) is None
    assert mod._future_date_reject({"published": "sometime in spring"}) is None
    assert mod._future_date_reject({"published": "not-a-date"}) is None
    assert mod._future_date_reject({"published": None}) is None


# ── _degeneration_flags: SOFT flag, never a reject ────────────────────────────

def test_repetition_loop_word_salad_is_flagged(ws):
    mod, _ = ws
    salad = " ".join(["ransomware actor targets"] * 120)   # >250 unpunctuated words
    flags = mod._degeneration_flags(salad)
    assert flags and "degenerate" in flags[0] and "repetition loop" in flags[0]


def test_normal_prose_and_empty_bodies_are_not_flagged(ws):
    mod, _ = ws
    assert mod._degeneration_flags("A short, punctuated sentence. And another one.") == []
    assert mod._degeneration_flags("") == []
    assert mod._degeneration_flags(None) == []


def test_wikilink_lists_and_code_fences_are_not_mistaken_for_degeneration(ws):
    """A long list of wikilinks or a code block is legitimate content, not word salad."""
    mod, _ = ws
    links = " ".join(f"[[entities/a/actor-{i}]]" for i in range(400))
    assert mod._degeneration_flags(links) == []

    fenced = "```\n" + " ".join(["x"] * 400) + "\n```\n"
    assert mod._degeneration_flags(fenced) == []
