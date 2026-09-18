"""A source document is identified by its URL, not its title (okengine#515).

A minted slug is `f(title)` — a WEAK key. The same article arriving on a second path
therefore collided instead of converging, and the write was refused. Measured on one live
deployment: of 83 collisions, **74 (89%) agreed on `url`** — one document on two paths,
because the vault carries five date-partition spellings so a path-based existence check
misses across them. Only 9 were genuinely different documents that slugged alike.

`url` is a STRONG key: two source pages claiming the same URL are the same document.

What these tests pin, in both directions — because the value is as much in what must STILL
be refused as in what now converges:

* same URL  -> converge (the 89%);
* different URL -> still refuse and flag (the 11%, using the real `show-hn` pair that
  motivated it — auto-merging those would fuse two unrelated posts, unrecoverably);
* no URL -> still refuse, because absence is undecidable and guessing fuses records;
* a new source with a URL is stamped with the strong `sources:url-<sha>` id;
* a legacy holder still carrying a TITLE slug is found by the two-key lookup, so shipping
  the strong id before the legacy re-id backfill cannot create duplicates.
"""
import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"

SCHEMA = (
    "types:\n"
    "  source:\n"
    "    required: [type]\n"
)


def _load(wiki_path: Path):
    os.environ["WIKI_PATH"] = str(wiki_path)
    os.environ["OKENGINE_MCP_WRITE_DATE"] = "2026-06-16"
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    os.environ.pop("OKENGINE_WRITE_ACTOR", None)
    (wiki_path / "wiki").mkdir(parents=True, exist_ok=True)
    (wiki_path / "wiki" / "schema.yaml").write_text(SCHEMA)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    assert m._CONVERGE_OK, "converge libs should import"
    return m


def _fm(title, url, extra=""):
    return f"type: source\ntitle: {title}\nurl: {url}\npublished: 2026-06-16\n{extra}"


def test_url_id_formula_matches_converge_source(tmp_path):
    """The two writers must agree about a document's identity, byte for byte."""
    m = _load(tmp_path)
    url = "https://example.test/a"
    expected = "sources:url-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    assert m.source_url_id(url) == expected
    assert m.source_url_id("") == "", "no url -> no id, never a hash of the empty string"


def test_url_normalization_is_minimal(tmp_path):
    """One trailing slash, and nothing else — a URL path is case-significant."""
    m = _load(tmp_path)
    assert m.source_url_id("https://example.test/a/") == m.source_url_id("https://example.test/a")
    assert m.source_url_id(" https://example.test/a ") == m.source_url_id("https://example.test/a")
    assert m.source_url_id("https://example.test/A") != m.source_url_id("https://example.test/a"), (
        "case folding would fuse two genuinely different paths on a case-sensitive host")


def test_same_url_on_a_second_path_converges(tmp_path):
    """The 89%: one document, two paths, must not be refused."""
    m = _load(tmp_path)
    url = "https://example.test/adobe-coldfusion"
    first = m._create("sources/2026/06/adobe-coldfusion.md",
                      _fm("Adobe Patches ColdFusion", url), "First capture.\n")
    assert not first.startswith(("refused:", "rejected:")), first

    # Same document, different partition spelling and a drifted basename — exactly the
    # shape seen live (`2026/06` vs `2026-06`, `oldfusion` vs `coldfusion`).
    second = m._create("sources/2026-06/adobe-oldfusion.md",
                       _fm("Adobe Patches ColdFusion", url), "Second capture.\n")
    assert not second.startswith("refused:"), (
        f"same URL must converge, not refuse: {second}")


def test_two_different_documents_sharing_a_title_both_survive(tmp_path):
    """The other 11% — and this direction was a FALSE REFUSAL before, not a save.

    Two unrelated Show HN posts shared one title, so they slugged identically and the
    second was refused: a real, distinct document rejected because of a title collision.
    Under URL identity they get different ids, so both are created and neither is merged.
    """
    m = _load(tmp_path)
    title = '"Show HN: Paste a URL get an honest answer"'   # quoted: the title contains a colon
    first = m._create("sources/2026/07/show-hn-files402.md",
                      _fm(title, "https://example.test/files402"), "A.\n")
    second = m._create("sources/2026/07/show-hn-video-gallery.md",
                       _fm(title, "https://example.test/video-gallery"), "B.\n")
    assert not first.startswith(("refused:", "rejected:")), first
    assert not second.startswith(("refused:", "rejected:")), (
        f"a genuinely different document must not be refused for a title collision: {second}")
    ids = []
    for rel in ("sources/2026/07/show-hn-files402.md",
                "sources/2026/07/show-hn-video-gallery.md"):
        text = (tmp_path / "wiki" / rel).read_text()
        ids += [ln for ln in text.splitlines() if ln.startswith("id:")]
    assert len(set(ids)) == 2, f"different URLs must yield distinct ids, got {ids}"


def test_a_page_with_no_url_is_still_refused(tmp_path):
    """Absence is not equality — undecidable must not become 'merge'."""
    m = _load(tmp_path)
    assert not m._create("sources/2026/07/one.md",
                         "type: source\ntitle: Same Title\npublished: 2026-06-16\n", "A.\n"
                         ).startswith(("refused:", "rejected:"))
    second = m._create("sources/2026-07/two.md",
                       "type: source\ntitle: Same Title\npublished: 2026-06-16\n", "B.\n")
    assert second.startswith("refused:"), "no URL on either side -> refuse, never guess"
    assert "URL identity is required" in second
    queue = tmp_path / "wiki" / "_review-queue.md"
    assert not queue.exists() or "sources/2026-07/two.md" not in queue.read_text(), (
        "an undecidable source collision is not actionable human review work"
    )
    assert "collision-refused sources/2026-07/two.md" in (tmp_path / "wiki" / "log.md").read_text()


def test_a_new_source_is_stamped_with_the_strong_url_id(tmp_path):
    """Item 1: the strong id is no longer limited to the raw-backfill actor."""
    m = _load(tmp_path)
    url = "https://example.test/fresh"
    rel = "sources/2026/06/fresh.md"
    assert not m._create(rel, _fm("Fresh Item", url), "Body.\n").startswith(
        ("refused:", "rejected:"))
    text = (tmp_path / "wiki" / rel).read_text()
    assert m.source_url_id(url) in text, (
        "a source with a url must carry sources:url-<sha>, not a title slug")


def test_legacy_title_slug_holder_is_found_by_the_two_key_lookup(tmp_path):
    """The ordering hazard, pinned.

    Legacy pages carry TITLE-slug ids. If the new strong id were minted and only that id
    checked, the legacy holder would be missed and a DUPLICATE created — strictly worse than
    the refusal it replaced. The lookup therefore resolves both candidate ids.
    """
    m = _load(tmp_path)
    url = "https://example.test/legacy-doc"
    legacy_rel = "sources/2026/05/legacy-doc.md"
    legacy = tmp_path / "wiki" / legacy_rel
    legacy.parent.mkdir(parents=True, exist_ok=True)
    # Hand-written the way an older writer left it: a TITLE-derived id, not a url id.
    legacy.write_text(
        "---\ntype: source\nid: sources:legacy-document-title\n"
        f"title: Legacy Document Title\nurl: {url}\npublished: 2026-05-01\n---\nOld.\n")
    # The index is built by walking the vault, so writing the file is enough — no private
    # index mutation, which also keeps the test honest about how a real legacy page looks.
    assert m._registry().resolve("sources:legacy-document-title") == legacy_rel, (
        "precondition: the legacy title-slug id must be indexed")

    second = m._create("sources/2026-05/legacy-document-title.md",
                       _fm("Legacy Document Title", url), "New capture.\n")
    assert not second.startswith("refused:"), (
        "the legacy title-slug holder must be found and converged into, not duplicated: "
        f"{second}")
