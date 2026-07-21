"""okengine.dedupe:same-story — duplicate SOURCE pages (one article, several slugs) merge
into the fullest copy; losers tombstone; inbound refs rewrite.

The real case (okcti 2026-07-14): one Hacker News item fetched by TWO packs' feed lanes
(raw/indicators/... and raw/detections/..., identical raw basename), ingested twice with
different slugs — one with an agent-mangled URL path segment (/2026/05/ vs /2026/07/).
Every per-story count downstream double-counted it.

Exact-match grouping only: shared raw basename, normalized URL, or host+slug+date.
Similar titles alone must NOT merge (different outlets covering one event are distinct
sources)."""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "extensions" / "okengine.dedupe" / "same_story_dedupe.py"


def _load():
    sys.modules.pop("same_story_dedupe", None)
    spec = importlib.util.spec_from_file_location("same_story_dedupe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(wiki: Path, rel: str, fm: dict, body: str = "body") -> None:
    p = wiki / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body + "\n",
                 encoding="utf-8")


def _fm(wiki: Path, rel: str) -> dict:
    t = (wiki / f"{rel}.md").read_text(encoding="utf-8")
    return yaml.safe_load(t[3:t.find("\n---", 3)]) or {}


# ---------------------------------------------------------------- unit


def test_group_keys_raw_url_hpd():
    m = _load()
    # shared raw basename despite different raw/ subtrees
    a = m.group_keys({"raw": "raw/indicators/2026-07-11-hack.md",
                      "url": "https://thehackernews.com/2026/07/hack.html",
                      "published": "2026-07-11"})
    b = m.group_keys({"raw": "raw/detections/2026-07-11-hack.md",
                      "url": "https://www.thehackernews.com/2026/05/hack.html",  # mangled month
                      "published": "2026-07-11"})
    assert set(a) & set(b), "shared raw basename OR host+slug+date must overlap"
    assert any(k.startswith("raw:") for k in a)
    assert any(k.startswith("hpd:") for k in a)          # host|slug|date key present
    # url normalization: www + query + trailing slash stripped
    assert m._norm_url("https://www.x.com/a/?q=1") == m._norm_url("http://x.com/a")


def test_redirect_unwrap_and_stub_path_rejected():
    m = _load()
    # Aggregator google-redirect: the real target is unwrapped from ?url=, so two links to
    # the same article via google redirect group together — and DON'T collapse to google.com/url
    g = "https://www.google.com/url?rct=j&sa=t&url=https://cybersecuritynews.com/rogueplanet-zero-day.html"
    direct = "https://cybersecuritynews.com/rogueplanet-zero-day.html"
    assert m._norm_url(g) == m._norm_url(direct)
    assert "google.com" not in (m._norm_url(g) or "")
    # a redirect whose real target has a STORY slug is usable; a bare redirector is NOT a key
    assert m._norm_url("https://www.google.com/url?sa=t&url=https://x.com/") is None    # target has no slug
    assert m._norm_url("https://example.com/search") is None                           # stub path
    assert m._norm_url("https://example.com/") is None                                 # empty path
    # two UNRELATED google-redirect articles must NOT share a url key (the 2026-07-14 blob bug)
    u1 = m._norm_url("https://www.google.com/url?url=https://a.com/oracle-ebs-flaw")
    u2 = m._norm_url("https://www.google.com/url?url=https://b.com/jenkins-rce")
    assert u1 != u2


def test_arxiv_ids_not_collapsed_by_extension_strip():
    m = _load()
    # two DIFFERENT arXiv papers, same date — the hpd key must keep the full dotted id
    # (2606.21349 vs 2606.22827), not truncate at the first '.' to a shared '2606'
    a = m.group_keys({"url": "https://arxiv.org/abs/2606.21349", "published": "2026-06-23"})
    b = m.group_keys({"url": "https://arxiv.org/abs/2606.22827", "published": "2026-06-23"})
    assert not (set(a) & set(b)), "distinct arXiv papers must not share any key"


def test_two_pages_need_two_signal_types_to_merge():
    """One shared signal is not a merge — agents mis-stamp raw:, and a single fuzzy key
    can collide. Only >=2 distinct types (raw/url/hpd) is a safe automatic merge."""
    m = _load()
    # mis-stamped raw: same raw basename, no url on either -> ONE type -> must NOT merge
    a = {"raw": "detections/2026-05-26-insights.md"}
    b = {"raw": "detections/2026-05-26-insights.md"}
    ta = {k.split(":", 1)[0] for k in set(m.group_keys(a)) & set(m.group_keys(b))}
    assert ta == {"raw"} and len(ta) < 2
    # genuine dup: same raw basename AND same story slug (url/hpd) -> TWO types -> merges
    c = {"raw": "x/2026-07-11-hack.md", "url": "https://thn.com/2026/07/hack.html",
         "published": "2026-07-11"}
    d = {"raw": "y/2026-07-11-hack.md", "url": "https://thn.com/2026/05/hack.html",  # mangled month
         "published": "2026-07-11"}
    tc = {k.split(":", 1)[0] for k in set(m.group_keys(c)) & set(m.group_keys(d))}
    assert len(tc) >= 2, tc          # raw + hpd


def test_merge_unions_lists_and_fills_scalars():
    m = _load()
    win = {"type": "source", "sources": ["A"], "tags": ["x"], "title": "Long"}
    lose = {"type": "source", "sources": ["B"], "tags": ["x", "y"], "vendor": "Acme"}
    out = m.merge_fm(win, lose)
    assert sorted(out["sources"]) == ["A", "B"]
    assert sorted(out["tags"]) == ["x", "y"]             # de-duped union
    assert out["vendor"] == "Acme"                       # missing scalar filled
    assert out["title"] == "Long"                        # present scalar NOT overwritten


# ------------------------------------------------------------- behavior


def _vault_with_pairs(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    # pair 1 — the real shape: same raw BASENAME under two packs' raw trees, mangled URL
    # month on the short copy. Longer body must win.
    _write(wiki, "sources/2026/07/story-long", {
        "type": "source", "source_kind": "news", "published": "2026-07-11",
        "raw": "raw/detections/2026-07-11-hack.md",
        "url": "https://thehackernews.com/2026/07/hack.html",
        "sources": ["The Hacker News"]}, body="full article body, many sentences here.")
    _write(wiki, "sources/2026/07/story-short", {
        "type": "source", "source_kind": "news", "published": "2026-07-11",
        "raw": "raw/indicators/2026-07-11-hack.md",
        "url": "https://thehackernews.com/2026/05/hack.html",   # mangled month
        "sources": ["The Hacker News / Detections.io"]}, body="short.")
    # a DISTINCT story — different article, must be left alone
    _write(wiki, "sources/2026/07/other", {
        "type": "source", "source_kind": "news", "published": "2026-07-11",
        "raw": "raw/feeds/2026-07-11-other.md",
        "url": "https://example.com/2026/07/other.html"}, body="unrelated story body.")
    # an actor citing the LOSER by plain-path ref must be rewritten to the winner
    _write(wiki, "entities/a/apt42", {
        "type": "actor", "title": "APT42",
        "recent_news_refs": ["sources/2026/07/story-short"]})
    return wiki


def test_merges_pair_tombstones_loser_rewrites_refs_and_spares_distinct(tmp_path):
    m = _load()
    wiki = _vault_with_pairs(tmp_path)
    rc = m.main(["--vault", str(tmp_path)])
    assert rc == 0

    long_fm = _fm(wiki, "sources/2026/07/story-long")
    short_fm = _fm(wiki, "sources/2026/07/story-short")
    # winner = the longer-bodied copy; loser tombstoned with provenance
    assert str(long_fm.get("status") or "") != "tombstoned"
    assert short_fm["status"] == "tombstoned"
    assert short_fm["superseded_by"] == "sources/2026/07/story-long"
    assert "duplicate" in short_fm["tombstone_reason"]
    # sources unioned onto the winner
    assert set(long_fm["sources"]) == {"The Hacker News", "The Hacker News / Detections.io"}
    # the citing actor's ref now points at the winner (count no longer double-links)
    assert _fm(wiki, "entities/a/apt42")["recent_news_refs"] == ["sources/2026/07/story-long"]
    # the distinct story is untouched
    assert str(_fm(wiki, "sources/2026/07/other").get("status") or "") != "tombstoned"


def test_idempotent_and_skips_tombstoned(tmp_path):
    m = _load()
    wiki = _vault_with_pairs(tmp_path)
    assert m.main(["--vault", str(tmp_path)]) == 0
    long_body_1 = (wiki / "sources/2026/07/story-long.md").read_text()
    # second run: the loser is already tombstoned (skipped), nothing new to merge
    assert m.main(["--vault", str(tmp_path)]) == 0
    assert (wiki / "sources/2026/07/story-long.md").read_text() == long_body_1


def test_dry_run_writes_nothing(tmp_path):
    m = _load()
    wiki = _vault_with_pairs(tmp_path)
    before = (wiki / "sources/2026/07/story-short.md").read_text()
    assert m.main(["--vault", str(tmp_path), "--dry-run"]) == 0
    assert (wiki / "sources/2026/07/story-short.md").read_text() == before


def test_titles_agree_veto():
    m = _load()
    mod = {"title": "Google and Microsoft Pull ModHeader After Dormant Collector Found"}
    var = {"title": "ModHeader extension removed from Chrome and Edge over dormant collector"}
    npm = {"title": "npm packages student proxy botnet jfrog"}
    assert m.titles_agree(mod, var)          # same story, shared distinctive tokens
    assert not m.titles_agree(mod, npm)      # provenance-matched but topically different -> veto
    assert m.titles_agree({"title": "x"}, {})   # missing title -> no veto (nothing to judge)
    # title taken from the body H1 when the frontmatter has none (the real okcti shape)
    assert not m.titles_agree(mod, {}, "", "# 148 npm Packages Turned Browsers Into a Botnet")
    assert m.titles_agree({}, {}, "# Google Microsoft Pull ModHeader Dormant Collector",
                          "# ModHeader Extension Removed Chrome Edge Dormant Collector")


def test_corrupt_provenance_page_vetoed_by_title(tmp_path, capsys):
    """A page whose raw/url/hpd all match another story (corrupt provenance) but whose TITLE
    is a different topic must NOT be merged — the title veto is the last-line guard."""
    m = _load()
    wiki = tmp_path / "wiki"
    shared = {"source_kind": "news", "published": "2026-07-13",
              "raw": "raw/x/2026-07-13-modheader.md",
              "url": "https://thn.com/2026/07/modheader.html"}
    _write(wiki, "sources/2026/07/modheader-a", {"type": "source",
           "title": "Google and Microsoft pull ModHeader over dormant collector", **shared},
           body="the full modheader story with a long body so this copy wins the group. " * 3)
    _write(wiki, "sources/2026/07/modheader-b", {"type": "source",
           "title": "ModHeader extension removed from Chrome Edge dormant collector", **shared},
           body="short.")
    # corrupt: SAME raw+url as the modheader story, but a different article — AND its title
    # lives only in the body H1 (no frontmatter title), the real okcti shape
    _write(wiki, "sources/2026/07/npm-botnet", {"type": "source", **shared},
           body="# 148 npm Packages Turned Browsers Into a DDoS Botnet\n\nnpm story.")
    rc = m.main(["--vault", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "VETO title-disjoint" in out
    # the modheader pair merged; the npm page survived un-tombstoned
    assert _fm(wiki, "sources/2026/07/npm-botnet").get("status") != "tombstoned"
    tombstoned = [r for r in ("sources/2026/07/modheader-a", "sources/2026/07/modheader-b")
                  if _fm(wiki, r).get("status") == "tombstoned"]
    assert len(tombstoned) == 1               # one modheader copy merged into the other


def test_oversized_group_reported_not_merged(tmp_path, capsys):
    """A low-entropy key that chains many unrelated pages must be SKIPPED, not merged —
    the structural backstop against a false-collision destroying real content."""
    m = _load()
    wiki = tmp_path / "wiki"
    # 8 pages sharing BOTH raw basename AND url (so they pass the 2-type gate and chain into
    # one component) — a story apparently fetched 8 times exceeds the cap: report, never merge.
    for i in range(8):
        _write(wiki, f"sources/2026/07/story{i}", {
            "type": "source", "source_kind": "news", "published": "2026-07-11",
            "raw": "raw/x/collide.md",
            "url": "https://outlet.com/2026/07/one-story.html"},
            body=f"copy number {i} of the article.")
    rc = m.main(["--vault", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP oversized" in out
    # nothing tombstoned — all 8 pages survive intact (cap refused to merge the big group)
    for i in range(8):
        assert str(_fm(wiki, f"sources/2026/07/story{i}").get("status") or "") != "tombstoned"


def test_scan_tolerates_vanished_pages(tmp_path):
    m = _load()
    wiki = _vault_with_pairs(tmp_path)
    (wiki / "sources" / "2026" / "07" / "ghost.md").symlink_to(
        wiki / "sources" / "2026" / "07" / "gone.md")   # dangling: rglob lists, read raises
    assert m.main(["--vault", str(tmp_path)]) == 0        # must not crash
