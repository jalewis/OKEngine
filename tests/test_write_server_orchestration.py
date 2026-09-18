"""Write-path orchestration against a real vault (okengine#462, tranche T1).

The guards were unit-covered in !722; this drives the operations that CALL them —
create / update / tombstone / flag / patch / append — through a real vault fixture,
because their interesting behaviour is in the ORDERING and the refusals, not in any
single helper:

  * a refusal must happen BEFORE anything is written (no partial page left behind)
  * create must refuse an existing page, update must refuse a missing one
  * reserved files are refused on every operation, not just create
  * malformed existing frontmatter must be REFUSED, never silently overwritten
    (invariant-audit M18 — a wipe would destroy the only copy of the data)
  * optimistic concurrency (expected_sha256) must reject a stale write

Deliberately uses monkeypatch for the environment rather than assigning
os.environ directly: three existing modules do the latter and leak a frozen
_today() into the whole session (#468).
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"

SCHEMA = (
    "types:\n"
    "  actor: {required: [type]}\n"
    "  source: {required: [type]}\n"
    "  vendor: {required: [type]}\n"
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m, tmp_path


def page_text(ws_mod, rel):
    return ws_mod._safe(rel).read_text(encoding="utf-8")


# ── _create ───────────────────────────────────────────────────────────────────

def test_create_writes_a_page_with_frontmatter_and_body(ws):
    m, vault = ws
    out = m._create("entities/acme.md", "type: vendor\ntitle: Acme", "Some body prose.")
    assert out.startswith("created"), out

    text = page_text(m, "entities/acme.md")
    assert "type: vendor" in text and "Some body prose." in text


def test_create_refuses_an_existing_page(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "b")
    again = m._create("entities/acme.md", "type: vendor\ntitle: Other", "b2")
    assert "refused" in again or "exists" in again

    assert "Acme" in page_text(m, "entities/acme.md"), "the original must be untouched"


def test_create_refuses_a_path_outside_the_vault(ws):
    m, vault = ws
    out = m._create("../../escape.md", "type: vendor", "b")
    assert out.startswith("refused")
    assert not (vault.parent / "escape.md").exists()


def test_create_refuses_reserved_basenames(ws):
    m, _ = ws
    for reserved in ("INDEX.md", "log.md", "HEALTH.md"):
        out = m._create(f"entities/{reserved}", "type: vendor", "b")
        assert "refused" in out.lower() or "reserved" in out.lower(), reserved


def test_create_refuses_malformed_frontmatter_without_writing(ws):
    m, vault = ws
    out = m._create("entities/broken.md", "type: [unclosed\n", "b")
    assert "refus" in out.lower() or "reject" in out.lower(), out
    assert not (vault / "wiki" / "entities" / "b" / "broken.md").exists(), \
        "a rejected create must leave nothing behind"


# ── _update ───────────────────────────────────────────────────────────────────

def test_update_merges_frontmatter_and_replaces_body(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme\nstatus: active", "old body")
    out = m._update("entities/acme.md", "title: Acme Corporation", "new body")
    assert not out.startswith("refused"), out

    text = page_text(m, "entities/acme.md")
    assert "Acme Corporation" in text
    assert "type: vendor" in text, "unmentioned keys survive the merge"
    assert "new body" in text and "old body" not in text


def test_update_refuses_a_missing_page(ws):
    m, _ = ws
    out = m._update("entities/never-created.md", "title: X", "b")
    assert "does not exist" in out and "create_entity" in out


def test_stale_write_is_DEFERRED_not_refused(ws):
    """The concurrency conflict is reported as `deferred:`, distinct from `refused:`/
    `rejected:`. That wording is the contract a lane depends on: a deferral is
    RETRYABLE (re-read and try again), whereas a refusal means stop. Collapsing the
    two would make lanes either give up on transient races or retry real rejections
    forever."""
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body one")
    out = m._update("entities/acme.md", None, "body two",
                    expected_sha256="sha256:" + "0" * 64)
    assert out.startswith("deferred:"), out
    assert "concurrent mutation" in out
    assert "body one" in page_text(m, "entities/acme.md"), "the stale write must not land"


def test_update_accepts_a_matching_precondition(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body one")
    current = "sha256:" + hashlib.sha256(
        m._safe("entities/acme.md").read_bytes()).hexdigest()

    out = m._update("entities/acme.md", None, "body two", expected_sha256=current)
    assert not out.startswith(("refused", "rejected", "deferred")), out
    assert "body two" in page_text(m, "entities/acme.md")


def test_update_rejects_a_page_whose_existing_frontmatter_is_malformed(ws):
    """invariant-audit M18: reject rather than wipe -- the broken page may hold the
    only copy of that data."""
    m, vault = ws
    broken = vault / "wiki" / "entities" / "b" / "broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("---\ntype: [unclosed\n---\n# Broken\n", encoding="utf-8")

    out = m._update("entities/b/broken.md", "title: Fixed", "new")
    assert out.startswith("rejected:"), out
    assert "invalid frontmatter" in out and "repair the frontmatter" in out
    assert "unclosed" in broken.read_text(), "the malformed original must survive"


# ── _tombstone ────────────────────────────────────────────────────────────────

def test_tombstone_marks_status_without_deleting_the_page(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body")
    out = m._tombstone("entities/acme.md", "superseded by a merge")
    assert not out.startswith("refused"), out

    text = page_text(m, "entities/acme.md")
    assert "tombstoned" in text, "tombstone is a status change, never a delete"
    assert "superseded by a merge" in text


def test_tombstone_refuses_a_missing_page(ws):
    m, _ = ws
    out = m._tombstone("entities/nope.md", "reason")
    assert "does not exist" in out


# ── _flag ─────────────────────────────────────────────────────────────────────

def test_flag_queues_a_page_for_review(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body")
    out = m._flag("entities/acme.md", "needs a second look")
    assert not out.startswith("refused"), out


def test_flag_refuses_a_path_outside_the_vault(ws):
    m, _ = ws
    assert m._flag("../../outside.md", "note").startswith("refused")


# ── _patch ────────────────────────────────────────────────────────────────────

def test_patch_replaces_an_exact_match(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "the quick brown fox")
    out = m._patch("entities/acme.md", "quick brown", "slow purple")
    assert not out.startswith("refused"), out
    assert "slow purple fox" in page_text(m, "entities/acme.md")


def test_patch_refuses_a_missing_page_and_an_empty_needle(ws):
    m, _ = ws
    assert "does not exist" in m._patch("entities/nope.md", "a", "b")

    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body")
    out = m._patch("entities/acme.md", "", "x")
    assert "refus" in out.lower() or "empty" in out.lower(), out


def test_patch_refuses_a_needle_that_is_absent(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body text")
    out = m._patch("entities/acme.md", "not present anywhere", "x")
    assert "refus" in out.lower() or "not found" in out.lower() or "no match" in out.lower(), out
    assert "body text" in page_text(m, "entities/acme.md")


# ── _append_section ───────────────────────────────────────────────────────────

def test_append_adds_to_an_existing_section_and_creates_a_missing_one(ws):
    m, _ = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme",
              "# Acme\n\n## Analysis\n\nfirst point\n")

    out = m._append_section("entities/acme.md", "Analysis", "second point")
    assert not out.startswith("refused"), out
    text = page_text(m, "entities/acme.md")
    assert "first point" in text and "second point" in text

    out = m._append_section("entities/acme.md", "New Section", "fresh content")
    assert not out.startswith("refused"), out
    assert "New Section" in page_text(m, "entities/acme.md")
