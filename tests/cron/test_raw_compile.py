"""raw-compile: structured raw captures -> source pages through the REAL write path (okengine#862).

The end-to-end tests stub nothing on the write path: the lane imports this repo's write_server,
authenticates as `cron:raw-compile`, and is held to that job's output_contract from jobs.json.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"
CONTRACT = next(j for j in json.loads((REPO / "config/engine-crons.json").read_text())
                if j["name"] == "raw-compile")["output_contract"]

FEED_ITEM = """---
type: source
source_channel: feed
source_feed: "Lab Feed"
title: "Sparse Attention at Scale"
url: "https://lab.test/sparse-attention"
published: 2026-09-20T10:00:00+00:00
fetched: 2026-09-21T08:00:00+00:00
watch_lane: ai
---

# Sparse Attention at Scale

We study sparse attention for long-context models and report a 3x throughput gain at equal
perplexity across four model sizes, with ablations on block size and routing.
"""
RECORD = {
    "connector_id": "news.search",
    "payload": {
        "article_id": "a1",
        "title": "Evaluating agents on real workflows",
        "source_url": "https://www.news.test/agents-workflows",
        "published_at": "2026-09-19T07:00:00+00:00",
        "matched_snippet": "An open <b>benchmark</b> measures whether AI <b>agents</b> can "
                           "complete real business workflows end to end, not just answer "
                           "isolated questions &amp; puzzles.",
    },
}


def _vault(tmp_path: Path, *, contract: bool = True) -> Path:
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump({
        "types": {"source": {"required": ["type", "published"], "extensible": True}},
        "partitioning": {"namespaces": {"sources": {
            "strategy": "by-date", "date_field": "published", "reshard_by": "day"}}},
        "permissions": {"default": {"create": True, "update": True, "delete": False}},
    }))
    jobs = [{"name": "raw-compile", "output_contract": CONTRACT}] if contract else []
    (tmp_path / "jobs.json").write_text(json.dumps({"jobs": jobs}))
    (tmp_path / "feeds").mkdir()
    (tmp_path / "feeds" / "feeds.opml").write_text(
        '<opml><body><outline text="Lab Feed" xmlUrl="https://lab.test/rss" publisher="Lab"/>'
        "</body></opml>")
    (tmp_path / "raw" / "ai").mkdir(parents=True)
    (tmp_path / "raw" / "ai" / "2026-09-20-sparse-attention-at-scale.md").write_text(FEED_ITEM)
    rec = tmp_path / "raw" / "connectors" / "news" / "records" / "a1"
    rec.mkdir(parents=True)
    (rec / "2026-09-19.json").write_text(json.dumps(RECORD))
    return tmp_path


_FRESH = ("raw_compile", "select_raw_batch", "okf_migrate", "write_server",
          "output_contract_enforce", "feed_fetch", "source_hydrate")


@pytest.fixture
def lane(tmp_path, monkeypatch):
    # The lane binds process state on purpose (actor env, sys.path, a freshly imported
    # write_server). Snapshot and restore all of it so no later test inherits this lane's identity.
    saved_modules = {name: sys.modules.get(name) for name in _FRESH}
    saved_path = list(sys.path)
    # The lane writes these straight into os.environ, and monkeypatch.delenv on an ABSENT var
    # records nothing to undo — so restore them by hand, or later tests run as `cron:raw-compile`.
    lane_env = ("OKENGINE_WRITE_ACTOR", "OKENGINE_OUTPUT_CONTRACT_MODE")
    saved_env = {var: os.environ.pop(var, None) for var in lane_env}

    def load(*, contract: bool = True):
        vault = _vault(tmp_path, contract=contract)
        monkeypatch.setenv("WIKI_PATH", str(vault))
        monkeypatch.setenv("OKENGINE_CRON_JOBS", str(vault / "jobs.json"))
        monkeypatch.setenv("OKENGINE_MCP_DIR", str(REPO / "okengine-mcp"))
        monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(vault / "selection.json"))
        sys.path.insert(0, str(CRON))
        for mod in _FRESH:
            sys.modules.pop(mod, None)
        return importlib.import_module("raw_compile"), vault

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


def _pages(vault: Path) -> dict[str, dict]:
    out = {}
    for p in (vault / "wiki" / "sources").rglob("*.md"):
        if p.name == "INDEX.md":
            continue
        fm = yaml.safe_load(p.read_text().split("---")[1])
        out[p.relative_to(vault / "wiki").as_posix()] = fm
    return out


def test_structured_captures_compile_through_the_write_path(lane, capsys):
    rc, vault = lane()
    assert rc.main([]) == 0
    pages = _pages(vault)
    assert len(pages) == 2, capsys.readouterr().out
    by_raw = {fm["raw"]: (path, fm) for path, fm in pages.items()}
    path, feed = by_raw["raw/ai/2026-09-20-sparse-attention-at-scale.md"]
    assert path.startswith("sources/2026/09/20/sparse-attention-at-scale-"), \
        "seat comes from the by-date partition + the shared URL-hash identity"
    assert feed["publisher"] == "Lab", "backlog item resolves its feed's DECLARED publisher"
    assert feed["source_feed"] == "Lab Feed"
    assert feed["id"].startswith("sources:url-")
    assert feed["producer_lane"] == "raw-compile", "writes are attributed to this lane's actor"
    assert isinstance(feed["published"], str)
    _, record = by_raw["raw/connectors/news/records/a1/2026-09-19.json"]
    assert record["title"] == "Evaluating agents on real workflows"
    assert record["publisher"] == "news.test", "no declared publisher: the publishing site"
    assert record["source_channel"] == "api"
    assert record["content_state"] == "discovery"
    assert feed["content_state"] == "discovery"


def test_a_second_run_finds_nothing_left_and_writes_nothing(lane, capsys):
    rc, vault = lane()
    rc.main([])
    before = _pages(vault)
    capsys.readouterr()
    rc.main([])
    assert '"queue": 0' in capsys.readouterr().out
    assert _pages(vault) == before


def test_the_same_url_from_two_captures_is_one_page(lane):
    """The okengine#54 class: identity is the URL, so a second capture of the same article
    converges onto the existing page instead of minting a second seat."""
    rc, vault = lane()
    dup = vault / "raw" / "ai" / "2026-09-21-sparse-attention-at-scale-repost.md"
    dup.write_text(FEED_ITEM.replace("2026-09-21T08", "2026-09-22T08"))
    rc.main([])
    pages = [fm for fm in _pages(vault).values() if fm["url"] == "https://lab.test/sparse-attention"]
    assert len(pages) == 1
    assert set(pages[0]["raw"]) == {"raw/ai/2026-09-20-sparse-attention-at-scale.md",
                                    "raw/ai/2026-09-21-sparse-attention-at-scale-repost.md"}, \
        "the second capture is recorded on the page, not dropped"


def test_without_its_declared_contract_the_lane_cannot_write(lane, capsys):
    """NEGATIVE: the lane is an authenticated writer, not an admin bypass — with no
    output_contract for `raw-compile` in jobs.json every write is refused."""
    rc, vault = lane(contract=False)
    rc.main([])
    assert _pages(vault) == {}
    assert "rejected" in capsys.readouterr().out


@pytest.mark.parametrize("mutate, reason", [
    (lambda t: t.replace('source_feed: "Lab Feed"', 'source_feed: "Unknown Feed"'),
     "missing-publisher"),
    (lambda t: t.split("# Sparse")[0] + "# Sparse\n\nshort\n", "body-too-short"),
    (lambda t: t.replace("https://lab.test/sparse-attention", "ftp://lab.test/x"),
     "url-not-http"),
    (lambda t: t.replace('title: "Sparse Attention at Scale"\n', ""), "missing-title"),
])
def test_incomplete_captures_are_left_for_the_model_lane(lane, mutate, reason):
    """NEGATIVE: nothing is inferred — a capture missing any identity field is not compiled."""
    rc, _vault = lane()
    parsed = rc.structured("raw/ai/x.md", mutate(FEED_ITEM), rc.declared_publishers(_vault))
    assert parsed == reason


def test_unparseable_and_unsupported_captures_are_classified(lane):
    rc, _vault = lane()
    assert rc.structured("raw/connectors/news/records/a/1.json", "{broken", {}) == "unparseable"
    assert rc.structured("raw/docs/report.pdf.txt", "text", {}) == "shape-unsupported"


def test_dry_run_classifies_without_writing(lane, capsys):
    rc, vault = lane()
    rc.main(["--dry-run"])
    assert _pages(vault) == {}
    assert "compilable: 2" in capsys.readouterr().out


def test_run_limit_leaves_the_rest_unexamined(lane, capsys):
    rc, vault = lane()
    rc.main(["--limit", "1"])
    assert len(_pages(vault)) == 1
    assert "not-examined:run-limit: 1" in capsys.readouterr().out


def test_a_write_server_without_the_shared_identity_fails_loudly(lane, tmp_path, monkeypatch):
    """An un-rolled gateway image lacks _converge_raw_source; the lane must stop, never fall back
    to a second, divergent copy of the identity logic."""
    rc, _vault = lane()
    stale = tmp_path / "stale-mcp"
    stale.mkdir()
    (stale / "write_server.py").write_text("")
    monkeypatch.setenv("OKENGINE_MCP_DIR", str(stale))
    sys.modules.pop("write_server", None)
    with pytest.raises(SystemExit, match="rebuild the gateway image"):
        rc.main([])


def test_outlines_without_a_publisher_declare_nothing(lane):
    rc, vault = lane()
    (vault / "feeds" / "feeds.opml").write_text(
        '<opml><body><outline text="Lab Feed" xmlUrl="https://lab.test/rss" publisher="Lab"/>'
        '<outline text="Other" xmlUrl="https://other.test/rss"/></body></opml>')
    assert rc.declared_publishers(vault) == {"Lab Feed": "Lab"}


def test_non_mapping_frontmatter_and_payload_have_no_identity(lane):
    rc, _vault = lane()
    assert rc.structured("raw/ai/x.md", "---\n- a list\n---\nbody\n", {}) == "missing-url"
    assert rc.structured("raw/connectors/n/records/a/1.json", '{"payload": ["x"]}', {}) == \
        "missing-url"


def test_a_frontmatter_rejection_is_returned_without_a_write(lane):
    rc_mod, _vault = lane()

    class W:
        @staticmethod
        def _raw_backfill_frontmatter(_fm):
            return None, "rejected: invalid frontmatter"

        @staticmethod
        def _converge_raw_source(*_a):
            raise AssertionError("wrote despite a rejected frontmatter")

    assert rc_mod.compile_one(W, "raw/ai/x.md", {"title": "t"}, "b") == \
        "rejected: invalid frontmatter"


def test_a_vault_without_raw_is_a_clean_no_op(lane, capsys):
    rc, vault = lane()
    import shutil
    shutil.rmtree(vault / "raw")
    assert rc.main([]) == 0
    assert capsys.readouterr().out.strip() == '{"wakeAgent": false}'


def test_incomplete_and_vanished_captures_are_counted_not_dropped(lane, capsys, monkeypatch):
    """Every queue item ends in a counted outcome: an incomplete capture is left for the model
    lane by reason, and one that vanished mid-run is reported unreadable."""
    rc, vault = lane()
    (vault / "raw" / "ai" / "2026-09-20-orphan.md").write_text(
        FEED_ITEM.replace('source_feed: "Lab Feed"', 'source_feed: "Unknown"')
                 .replace("sparse-attention", "orphan"))
    real_scan = rc.S.scan_raw

    def scan_with_a_ghost():
        scan = real_scan()
        scan.unprocessed.append(("raw/ai/2026-09-20-vanished.md", 2026, 0.0))
        return scan

    monkeypatch.setattr(rc.S, "scan_raw", scan_with_a_ghost)
    rc.main([])
    out = capsys.readouterr().out
    assert "left-for-model:missing-publisher: 1" in out
    assert "unreadable: 1" in out
    assert "compiled: 2" in out


LEGACY = """---
type: source
id: sources:sparse-attention-at-scale
title: Sparse Attention at Scale
url: https://lab.test/sparse-attention
publisher: Lab
published: 2026-09-20
raw: raw/hf/2026-09-20-sparse-attention.json
{extra}---
An importer's page for the same paper, carrying a title-derived id rather than the URL-derived
one, with a body long enough to count as a compiled source.
"""


def _legacy(vault: Path, extra: str = "") -> Path:
    page = vault / "wiki" / "sources" / "2026" / "08" / "29" / "sparse-attention-2608.md"
    page.parent.mkdir(parents=True)
    page.write_text(LEGACY.format(extra=extra))
    return page


def test_a_url_already_held_under_another_id_is_attached_not_reminted(lane):
    """REGRESSION (first live run, 2026-09-26): identity was only the URL-derived id, so an
    importer's page for the same paper (title-derived id, different seat) was invisible and the
    lane minted a second page for 5 papers. The capture must attach to the existing page."""
    rc, vault = lane()
    legacy = _legacy(vault)
    rc.main([])
    same = [path for path, fm in _pages(vault).items()
            if fm["url"] == "https://lab.test/sparse-attention"]
    assert same == ["sources/2026/08/29/sparse-attention-2608.md"]
    fm = yaml.safe_load(legacy.read_text().split("---")[1])
    assert fm["raw"] == ["raw/hf/2026-09-20-sparse-attention.json",
                         "raw/ai/2026-09-20-sparse-attention-at-scale.md"]
    assert fm["id"] == "sources:sparse-attention-at-scale", "the existing identity is kept"


def test_dry_run_reports_would_attach(lane, capsys):
    rc, vault = lane()
    _legacy(vault)
    rc.main(["--dry-run"])
    out = capsys.readouterr().out
    assert "would-attach: 1" in out and "compilable: 1" in out


def test_a_tombstoned_page_never_absorbs_a_capture(lane):
    """NEGATIVE: a retired page is not a live seat for the URL; the capture compiles fresh."""
    rc, vault = lane()
    legacy = _legacy(vault, "status: tombstoned\n")
    rc.main([])
    live = [fm for fm in _pages(vault).values()
            if fm["url"] == "https://lab.test/sparse-attention"
            and fm.get("status") != "tombstoned"]
    assert len(live) == 1 and live[0]["producer_lane"] == "raw-compile"
    assert "raw/ai/" not in legacy.read_text()


def test_an_attach_the_contract_refuses_is_counted_and_mints_nothing(lane, capsys):
    """NEGATIVE: an existing page of a legacy type (`news`, not `source`) is outside this lane's
    contract, so the update is refused. That is reported as a rejection — and never falls through
    to minting a second page."""
    rc, vault = lane()
    legacy = _legacy(vault)
    legacy.write_text(legacy.read_text().replace("type: source", "type: news"))
    rc.main([])
    assert "rejected:" in capsys.readouterr().out
    assert [fm["url"] for fm in _pages(vault).values()].count(
        "https://lab.test/sparse-attention") == 1

def test_a_capture_already_listed_is_not_written_again(lane, capsys):
    rc, vault = lane()
    legacy = _legacy(vault)
    # listed, but a legacy-typed page never counts as compiled, so the queue still offers it
    legacy.write_text(legacy.read_text().replace("type: source", "type: news").replace(
        "raw: raw/hf/2026-09-20-sparse-attention.json",
        "raw: [raw/ai/2026-09-20-sparse-attention-at-scale.md]"))
    before = legacy.read_text()
    rc.main([])
    assert "already-attached: 1" in capsys.readouterr().out
    assert legacy.read_text() == before


def test_unreadable_and_url_less_pages_are_not_url_seats(lane):
    """NEGATIVE: a source page with broken YAML, or a placeholder url (`UNKNOWN`), is not
    identity for anything — indexing it would attach unrelated captures to it."""
    rc, vault = lane()
    seats = vault / "wiki" / "sources" / "2026" / "01" / "01"
    seats.mkdir(parents=True)
    (seats / "broken.md").write_text("---\ntitle: [unclosed\n---\nbody\n")
    (seats / "placeholder.md").write_text("---\ntype: source\nurl: UNKNOWN\n---\nbody\n")
    (seats / "no-frontmatter.md").write_text("just text\n")
    W = rc._write_server()
    assert rc.url_index(W, vault) == {}


def test_an_attach_fills_a_missing_publisher_from_the_declaration(lane, capsys):
    """REGRESSION: an existing page with no `publisher` never counts as compiled, so its capture
    was re-offered every run (263 such no-ops on one live vault). The attach carries the
    capture's declared publisher, and the capture then leaves the queue."""
    rc, vault = lane()
    legacy = _legacy(vault)
    legacy.write_text(legacy.read_text().replace("publisher: Lab\n", ""))
    rc.main([])
    fm = yaml.safe_load(legacy.read_text().split("---")[1])
    assert fm["publisher"] == "Lab"
    assert "raw/ai/2026-09-20-sparse-attention-at-scale.md" in fm["raw"]
    capsys.readouterr()
    rc.main([])
    assert '"queue": 0' in capsys.readouterr().out


def test_an_attach_never_overwrites_an_existing_publisher(lane):
    rc, vault = lane()
    legacy = _legacy(vault)
    legacy.write_text(legacy.read_text().replace("publisher: Lab\n", "publisher: Other Lab\n"))
    rc.main([])
    assert yaml.safe_load(legacy.read_text().split("---")[1])["publisher"] == "Other Lab"


def test_already_attached_no_ops_do_not_spend_the_run_limit(lane, capsys):
    """REGRESSION: every queue item that reached the write step counted toward --limit, so
    re-offered no-ops starved real work (~263 of 400 slots per live run)."""
    rc, vault = lane()
    legacy = _legacy(vault)
    legacy.write_text(legacy.read_text().replace("type: source", "type: news").replace(
        "raw: raw/hf/2026-09-20-sparse-attention.json",
        "raw: [raw/ai/2026-09-20-sparse-attention-at-scale.md]"))
    noop = vault / "raw" / "ai" / "2026-09-20-sparse-attention-at-scale.md"
    os.utime(noop, (4_000_000_000, 4_000_000_000))  # newest, so the queue offers the no-op first
    rc.main(["--limit", "1"])
    out = capsys.readouterr().out
    assert "already-attached: 1" in out
    assert "compiled: 1" in out, "the one real capture still gets the run's single slot"
    assert "not-examined" not in out


@pytest.mark.parametrize("escaped, expected", [
    ("\\ud83d\\udd25", "\U0001F525"),
    ("\\ud83d alone", "� alone"),
], ids=["paired-escape-is-the-character", "lone-half-is-replaced"])
def test_a_legacy_capture_title_never_carries_a_surrogate_onto_the_page(lane, escaped, expected):
    """okengine#894: captures feed_fetch wrote before the fix hold JSON surrogate escapes in YAML.
    Compiled as read, the page title held lone surrogates and broke the projection rebuild."""
    rc, vault = lane()
    capture = vault / "raw" / "ai" / "2026-09-20-sparse-attention-at-scale.md"
    capture.write_text(FEED_ITEM.replace('title: "Sparse Attention at Scale"',
                                         f'title: "Sparse Attention at Scale {escaped}"\n'
                                         f'authors: ["Ann {escaped}"]'))
    assert rc.main([]) == 0
    feed = {fm["raw"]: fm for fm in _pages(vault).values()}["raw/ai/2026-09-20-sparse-attention-at-scale.md"]
    assert feed["title"] == f"Sparse Attention at Scale {expected}"
    assert feed["authors"] == [f"Ann {expected}"], "list fields are repaired element by element"
    feed["title"].encode("utf-8")


def test_capture_provenance_is_carried_but_owned_fields_never_are(lane):
    """A capture's own provenance (an importer's paper id, an upvote snapshot) reaches the page
    verbatim — the fixed allow-list dropped it. Fields the write path or other lanes own are
    never taken from a capture, whatever it claims."""
    rc, vault = lane()
    owned = {"id": "sources:forged", "status": "tombstoned", "version": 99,
             "producer_lane": "someone-else", "credibility": 1, "reliability": "A",
             "needs_review": True, "created": "2020-01-01", "raw": "raw/elsewhere.md"}
    extra = "".join(f"{k}: {v!r}\n" if not isinstance(v, str) else f"{k}: '{v}'\n"
                    for k, v in owned.items())
    (vault / "raw" / "ai" / "2026-09-20-sparse-attention-at-scale.md").write_text(
        FEED_ITEM.replace("watch_lane: ai\n", "watch_lane: ai\narxiv_id: '2609.00001'\n"
                          "hf_upvotes: 42\n" + extra))
    rc.main([])
    page = next(fm for fm in _pages(vault).values() if fm["url"].endswith("sparse-attention"))
    assert page["arxiv_id"] == "2609.00001" and page["hf_upvotes"] == 42
    assert page["id"].startswith("sources:url-") and page.get("status") != "tombstoned"
    assert page["producer_lane"] == "raw-compile" and page["version"] != 99
    assert "credibility" not in page and "reliability" not in page
    assert page["raw"] == "raw/ai/2026-09-20-sparse-attention-at-scale.md"


def test_an_attach_fills_a_stub_page_from_the_capture(lane, capsys):
    """REGRESSION: a page whose body is under the acceptance floor never counts as compiled, so
    its captures were re-offered every run AND the lane's own contract refused the attach. The
    capture's full text now fills the stub; the capture then leaves the queue."""
    rc, vault = lane()
    legacy = _legacy(vault)
    legacy.write_text(legacy.read_text().split("An importer")[0] + "stub\n")
    rc.main([])
    assert "We study sparse attention" in legacy.read_text()
    capsys.readouterr()
    rc.main([])
    assert '"queue": 0' in capsys.readouterr().out


def test_an_attach_lets_only_the_fuller_capture_replace_a_real_body(lane):
    """User decision (okengine#962): the FULLER capture wins, the same rule as the converge path.
    A shorter capture (snippet, paywall) never overwrites a real body; a fuller re-extraction of the
    same URL corrects it."""
    rc, vault = lane()
    legacy = _legacy(vault)
    W = rc._write_server()
    before_body = legacy.read_text().split("---", 2)[2]
    rc.attach(W, "sources/2026/08/29/sparse-attention-2608.md", "raw/ai/short.md", "", "A short snippet.")
    assert legacy.read_text().split("---", 2)[2] == before_body, "a shorter capture never replaces"
    rc.main([])                                     # the fixture's capture is the fuller text
    assert "We study sparse attention" in legacy.read_text()



def test_a_refused_body_upgrade_still_records_the_backlink(lane):
    """Re-verify round 5 (okengine#962): when the write path refuses the fuller body, the attach
    retries with the backlink alone, so the capture is never orphaned; a capture already recorded
    reports already-attached instead of rewriting."""
    rc, _vault = lane()
    calls = []

    class W:
        converge = rc._write_server().converge

        @staticmethod
        def _read_page(_p):
            return {"type": "source", "raw": ["raw/a.md"], "publisher": "E"}, "Short existing body."

        @staticmethod
        def _update(_page, fm, body):
            calls.append((fm, body is not None))
            return "rejected: contract" if body is not None else "updated x v3"

    body = "A much fuller capture body sentence. " * 20
    assert rc.attach(W, "sources/x.md", "raw/b.md", "", body) == "updated x v3"
    assert [b for _fm, b in calls] == [True, False] and "raw/b.md" in str(calls[-1][0])
    calls.clear()
    assert rc.attach(W, "sources/x.md", "raw/a.md", "", body) == "already-attached"
    assert [b for _fm, b in calls] == [True]


def _attach_body(rc, current_body, capture):
    """What attach() writes as the body (None = backlink only) for one capture."""
    seen = []

    class W:
        converge = rc._write_server().converge

        @staticmethod
        def _read_page(_p):
            return {"type": "source", "raw": [], "publisher": "E"}, current_body

        @staticmethod
        def _update(_page, _fm, body):
            seen.append(body)
            return "updated x v2"

    rc.attach(W, "sources/x.md", "raw/new.md", "", capture)
    return seen[0]


def test_an_attach_body_upgrade_honours_the_acceptance_floor_exactly(lane):
    """The fuller capture must ALSO clear ACCEPT_MIN_CHARS (non-whitespace), inclusive."""
    rc, _vault = lane()
    floor = rc.S.ACCEPT_MIN_CHARS
    at_floor, below = "x" * floor, "x" * (floor - 1)
    assert _attach_body(rc, "y", at_floor) == at_floor
    assert _attach_body(rc, "y", below) is None


def test_an_attach_never_replaces_a_body_with_a_shorter_capture_above_the_floor(lane):
    rc, _vault = lane()
    floor = rc.S.ACCEPT_MIN_CHARS
    assert _attach_body(rc, "z" * (floor * 3), "x" * (floor * 2)) is None

def test_a_short_capture_never_fills_a_stub(lane):
    """NEGATIVE: filling is only ever an upgrade — a capture too short to count as compiled
    itself does not overwrite a stub with another stub."""
    rc, vault = lane()
    legacy = _legacy(vault)
    legacy.write_text(legacy.read_text().split("An importer")[0] + "stub\n")
    body = legacy.read_text().split("---", 2)[2]
    W = rc._write_server()
    rc.attach(W, "sources/2026/08/29/sparse-attention-2608.md", "raw/ai/x.md", "", "tiny")
    assert legacy.read_text().split("---", 2)[2].strip().endswith(body.strip())


def test_literal_brackets_in_captured_text_are_not_links(lane):
    """REGRESSION: captured web text containing `[[ticker]]` (a template shown on the page) was
    rejected every run as an unresolved wikilink."""
    rc, vault = lane()
    item = vault / "raw" / "ai" / "2026-09-20-sparse-attention-at-scale.md"
    item.write_text(FEED_ITEM.replace("with ablations", "with a template like [[ticker]] and "
                                       "[[company]] shown in the text, and ablations"))
    rc.main([])
    page = next(p for p in (vault / "wiki" / "sources").rglob("*.md")
                if "sparse-attention" in p.name)
    text = page.read_text()
    assert "[[ticker]]" not in text and "ticker" in text
    assert rc.literal_text("a [[b]] c") == "a [​[b]​] c"


def test_raw_state_is_derived_not_copied_from_an_untrusted_label(lane):
    rc, vault = lane()
    capture = vault / "raw/ai/2026-09-20-sparse-attention-at-scale.md"
    capture.write_text(FEED_ITEM.replace("watch_lane: ai", "watch_lane: ai\ncontent_state: captured"))
    rc.main([])
    assert all(fm["content_state"] == "discovery" for fm in _pages(vault).values())


def test_full_capture_compiles_with_provenance_and_remains_captured_on_repeat(lane):
    """Real producer → compiler → enforced writer → persisted source, without infrastructure mocks."""
    rc, vault = lane()
    import feed_fetch
    article = "The experiment was independently repeated with the same protocol and instruments. " * 3
    raw = feed_fetch.write_item(vault / "raw/notices", "Official", {
        "title": "Replication notice", "link": "https://lab.test/replication",
        "published": "2026-09-21", "id": "replication",
        "canonical_url": "https://lab.test/replication", "retrieved_url": "https://lab.test/replication",
        "content_hash": "abc", "capture_object": "objects/ab/report.html",
        "capture_revision": "revisions/ab/report.json", "captured_text": article,
    }, "", publisher="Lab")
    rc.main([])
    page, fm = next((p, fm) for p, fm in _pages(vault).items()
                    if fm["url"] == "https://lab.test/replication")
    text = (vault / "wiki" / page).read_text()
    assert article.strip() in text and fm["raw"] == raw.relative_to(vault).as_posix()
    assert fm["content_state"] == "captured" and fm["capture_revision"] == "revisions/ab/report.json"
    assert "review_status" not in fm, "mechanical capture does not confer appraisal"
    assert rc.source_content.source_state(vault, fm, text.split("---", 2)[2]) == "captured"
    before = text
    rc.main([])
    assert (vault / "wiki" / page).read_text() == before


@pytest.fixture
def hydration(lane, monkeypatch):
    rc, vault = lane()
    module = importlib.import_module("source_hydrate")
    page = "sources/2026/09/19/existing-agent-report.md"
    path = vault / "wiki" / page
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump({
        "type": "source", "id": "sources:existing-report", "title": RECORD["payload"]["title"],
        "url": RECORD["payload"]["source_url"], "publisher": "news.test",
        "published": "2026-09-19", "raw": "raw/connectors/news/records/a1/2026-09-19.json",
    }) + "---\n" + "An excerpt from the discovered report. " * 8 + "\n")
    # Only the HTTP boundary is replaced: extraction, archives and enforced writes remain real.
    import io
    from unittest.mock import create_autospec
    from urllib.response import addinfourl
    document = b'<html><main>A short complete primary notice: the trial ended.</main></html>'

    def opened(request, timeout):
        return addinfourl(io.BytesIO(document), {"Content-Type": "text/html"}, request.full_url, 200)

    opener = create_autospec(module.web_capture._default_open, side_effect=opened)
    monkeypatch.setattr(module.web_capture, "_default_open", opener)
    # DNS is an external boundary. Return a globally routable test address; no socket is opened.
    monkeypatch.setattr(module.web_capture.socket, "getaddrinfo", create_autospec(
        module.web_capture.socket.getaddrinfo,
        return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
    return module, rc, vault, page, opener


def test_hydration_preserves_identity_text_and_provenance_on_stable_repeat(hydration):
    h, rc, vault, page, opener = hydration
    old_fm, old_body, _ = h.read_source(vault, page)
    outcome = h.hydrate(rc._write_server(), vault, page, apply=True)
    assert outcome["outcome"] == "captured", outcome
    fm, body, _ = h.read_source(vault, page)
    assert fm["id"] == old_fm["id"] and fm["url"] == old_fm["url"]
    assert old_body.rstrip() in body and "the trial ended" in body
    assert old_fm["raw"] in fm["raw"] and outcome["raw"] in fm["raw"]
    raw_fm, _ = h.content.split((vault / outcome["raw"]).read_text())
    assert raw_fm["requested_url"] == raw_fm["retrieved_url"] == raw_fm["canonical_url"] == fm["url"]
    assert raw_fm["retrieved_at"] and raw_fm["content_hash"] and raw_fm["text_hash"]
    assert (vault / "raw/captures" / raw_fm["capture_object"]).is_file()
    assert (vault / "raw/captures" / raw_fm["capture_revision"]).is_file()
    assert not fm.get("reviewed_by"), "capture never invents an appraisal"
    before = (vault / "wiki" / page).read_bytes()
    assert h.hydrate(rc._write_server(), vault, page, apply=True)["outcome"] == "already-captured"
    assert opener.call_count == 1
    assert (vault / "wiki" / page).read_bytes() == before


def test_hydration_dry_run_never_fetches_or_writes(hydration):
    h, _, vault, page, opener = hydration
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert h.hydrate(None, vault, page)["outcome"] == "would-capture"
    assert not opener.called
    assert before == {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}


def test_hydration_http_failure_retains_excerpt_and_is_not_promoted(hydration, monkeypatch):
    h, rc, vault, page, opener = hydration
    import urllib.error
    opener.side_effect = urllib.error.HTTPError("https://www.news.test/agents-workflows", 403, "Forbidden", {}, None)
    fm, old_body, _ = h.read_source(vault, page)
    result = h.hydrate(rc._write_server(), vault, page, apply=True)
    assert result["outcome"] == "capture-failed" and result["category"] == "http", result
    after_fm, body, _ = h.read_source(vault, page)
    assert body == old_body
    assert h.content.source_state(vault, after_fm, body) == "capture-failed"
    before = (vault / "wiki" / page).read_bytes()
    assert h.hydrate(rc._write_server(), vault, page, apply=True)["outcome"] == "capture-failed"
    assert (vault / "wiki" / page).read_bytes() == before


def test_hydration_respects_the_writer_contract(hydration):
    h, rc, vault, page, _ = hydration
    (vault / "jobs.json").write_text('{"jobs": []}')
    before = (vault / "wiki" / page).read_bytes()
    result = h.hydrate(rc._write_server(), vault, page, apply=True)
    assert result["outcome"] == "write-rejected", result
    assert (vault / "wiki" / page).read_bytes() == before


def test_hydration_defers_when_page_changes_during_fetch(hydration):
    h, rc, vault, page, opener = hydration
    original = opener.side_effect
    def changed(request, timeout):
        p = vault / "wiki" / page
        p.write_text(p.read_text() + "\nConcurrent curator note.\n")
        return original(request, timeout)
    opener.side_effect = changed
    result = h.hydrate(rc._write_server(), vault, page, apply=True)
    assert result["outcome"] == "write-rejected" and "concurrent mutation" in result["detail"]
    assert "Concurrent curator note." in (vault / "wiki" / page).read_text()
    assert "Full-text acquisition" not in (vault / "wiki" / page).read_text()


def test_hydration_cli_reports_limit_and_invalid_sources(hydration, capsys):
    h, _, vault, page, opener = hydration
    capsys.readouterr()
    assert h.main(["--limit", "1", "--source", "../outside.md", "--source", page,
                   "--source", "sources/missing.md"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert [r["outcome"] for r in result["results"]] == ["invalid-source", "would-capture"]
    assert result["not_examined"] == 1 and not opener.called
    with pytest.raises(SystemExit):
        h.main(["--limit", "0"])


@pytest.mark.parametrize("change, message", [
    ({"type": "news"}, "not a live source"),
    ({"type": "z-news"}, "not a live source"),
    ({"status": "tombstoned"}, "not a live source"),
    ({"raw": 12}, "malformed raw references"),
    ({"raw": ["raw/a.md", None]}, "malformed raw references"),
])
def test_hydration_rejects_invalid_source_records(hydration, change, message):
    h, _, vault, page, opener = hydration
    fm, body, _ = h.read_source(vault, page)
    fm.update(change)
    (vault / "wiki" / page).write_text("---\n" + yaml.safe_dump(fm) + "---\n" + body)
    with pytest.raises(ValueError, match=message):
        h.hydrate(None, vault, page, apply=True)
    assert not opener.called


@pytest.mark.parametrize("url", [None, "", "   ", ["https://example.test"]])
def test_hydration_missing_url_is_visible_without_fetch(hydration, url):
    h, _, vault, page, opener = hydration
    fm, body, _ = h.read_source(vault, page)
    fm["url"] = url
    (vault / "wiki" / page).write_text("---\n" + yaml.safe_dump(fm) + "---\n" + body)
    assert h.hydrate(None, vault, page, apply=True)["outcome"] == "missing-url"
    assert not opener.called


def test_hydration_reads_exact_size_boundary_and_refuses_oversize(hydration):
    h, _, vault, page, _ = hydration
    path = vault / "wiki" / page
    start = path.read_bytes()
    path.write_bytes(start + b" " * (h.content.MAX_RAW_BYTES - len(start)))
    assert h.read_source(vault, page)[0]["type"] == "source"
    with path.open("ab") as handle:
        handle.write(b"x")
    with pytest.raises(ValueError, match="bounded read"):
        h.read_source(vault, page)


def test_hydration_empty_plaintext_is_a_failure(hydration):
    h, rc, vault, page, opener = hydration
    import io
    from urllib.response import addinfourl
    opener.side_effect = lambda request, timeout: addinfourl(
        io.BytesIO(b"   \n"), {"Content-Type": "text/plain"}, request.full_url, 200)
    result = h.hydrate(rc._write_server(), vault, page, apply=True)
    assert result["outcome"] == "capture-failed" and result["category"] == "extraction"


def test_hydration_cli_apply_and_default_inventory(hydration, capsys):
    h, _, vault, page, _ = hydration
    capsys.readouterr()
    assert h.main(["--source", page, "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["apply"] and result["results"][0]["outcome"] == "captured"
    assert h.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {x["outcome"] for x in result["results"]} == {"already-captured"}


def test_hydration_rejects_a_symlink_outside_the_source_namespace(hydration):
    h, _, vault, page, opener = hydration
    outside = vault / "outside.md"
    outside.write_bytes((vault / "wiki" / page).read_bytes())
    (vault / "wiki/sources/escape.md").symlink_to(outside)
    with pytest.raises(ValueError):
        h.hydrate(None, vault, "sources/escape.md", apply=True)
    assert not opener.called


def test_hydration_does_not_trust_a_writer_success_without_persisted_content(hydration):
    h, rc, vault, page, _ = hydration
    from types import SimpleNamespace
    from unittest.mock import create_autospec
    real_writer = rc._write_server()
    # Fault injection at the writer boundary: a success acknowledgment without a durable write.
    writer = SimpleNamespace(_update=create_autospec(real_writer._update, return_value="updated " + page))
    before = (vault / "wiki" / page).read_bytes()
    assert h.hydrate(writer, vault, page, apply=True)["outcome"] == "verification-failed"
    assert (vault / "wiki" / page).read_bytes() == before


def test_capture_repair_reaches_the_reader_and_stays_visible_on_repeat(hydration, monkeypatch):
    h, rc, vault, page, _ = hydration
    import importlib.util
    monkeypatch.setenv("VAULT_DIR", str(vault))
    monkeypatch.syspath_prepend(str(REPO / "okengine-reader"))
    spec = importlib.util.spec_from_file_location("capture_repair_reader", REPO / "okengine-reader/app.py")
    reader = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, reader)
    spec.loader.exec_module(reader)
    before = reader.api_page(page.removesuffix(".md"))
    assert "the trial ended" not in before["html"]
    W = rc._write_server()
    assert h.hydrate(W, vault, page, apply=True)["outcome"] == "captured"
    first = reader.api_page(page.removesuffix(".md"))
    assert "the trial ended" in first["html"]
    assert "An excerpt from the discovered report." in first["html"]
    assert h.hydrate(W, vault, page, apply=True)["outcome"] == "already-captured"
    repeat = reader.api_page(page.removesuffix(".md"))
    assert repeat["html"] == first["html"] and repeat["path"] == before["path"]
