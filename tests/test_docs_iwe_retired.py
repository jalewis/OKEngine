"""okengine#1066: no document describes IWE as a live engine component.

IWE was retired from the runtime in okengine#651. The backlink graph is built by
`backlink_lib.scan_forward_refs` (okengine#179), published by the `backlinks-refresh` cron as
`wiki/.backlinks.json`, and served by the read MCP, reader and cockpit. Docs kept telling
operators to stage an `iwe` binary and naming it as the engine's graph layer, long after nothing
shipped it.

Every mention of `iwe` (case-insensitive, whole word) in the repository's markdown documents must
sit in an allowlisted context: history, or IWE as an ecosystem tool the engine does not run. A
line-scoped entry allows only lines matching its pattern, so a stale sentence added elsewhere in
the same file still fails. Line-scoped entries must keep matching, so the allowlist only shrinks.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_IWE = re.compile(r"\biwe\b", re.IGNORECASE)

# Directory parts never scanned: VCS/tool state, dependencies, and test fixture data (fixture
# vault pages are inputs to tests, not documentation).
_SKIP_PARTS = frozenset({".git", "node_modules", "__pycache__", "tests"})

# Whole-file allowances: records of a past state.
_HISTORY = {
    "CHANGELOG.md": "release history; entries describe the engine as it was when released",
    "docs/reviews/": "dated audit records; rewriting a finding would falsify what was reviewed",
}

# Line-scoped allowances: {path: [(line regex, reason)]}.
_LINES = {
    "docs/kb-tooling.md": [
        (r"versus IWE's 4\.2 GiB/530 seconds",
         "measured comparison that justified retiring IWE for the in-process scanner"),
    ],
    "docs/okf-alignment.md": [
        (r"^\| \*\*IWE\*\* \(`iwe-org/iwe`\) \|.*Evaluated, not run",
         "ecosystem tools table; the row states the engine does not run it"),
        (r"^\| IWE \(ecosystem tool; the engine does not run it\) \| https://github\.com/iwe-org/iwe \|",
         "canonical references table link"),
    ],
    "docs/okf/guide-2-building-an-agent-vault.md": [
        (r"^\| IWE — markdown knowledge graph \| https://github\.com/iwe-org/iwe \|$",
         "OKF guide references table link (ecosystem recommendation)"),
    ],
    "docs/okf/guide-4-scaling-to-100k.md": [
        (r"^\| IWE — markdown knowledge graph \| https://github\.com/iwe-org/iwe \|$",
         "OKF guide references table link (ecosystem recommendation)"),
    ],
}


def _documents() -> list[str]:
    """Repository-relative paths of every markdown document outside the skipped directories."""
    rels = []
    for p in REPO.rglob("*.md"):
        rel = p.relative_to(REPO)
        if _SKIP_PARTS.intersection(rel.parts[:-1]) or not p.is_file():
            continue
        rels.append(rel.as_posix())
    return sorted(rels)


def stale_mentions(rel: str, text: str) -> list[str]:
    """`rel:line: text` for each line mentioning iwe outside the allowlist."""
    if any(rel == h or (h.endswith("/") and rel.startswith(h)) for h in _HISTORY):
        return []
    allowed = [re.compile(rx) for rx, _ in _LINES.get(rel, [])]
    return [f"{rel}:{n}: {line.strip()}"
            for n, line in enumerate(text.splitlines(), 1)
            if _IWE.search(line) and not any(a.search(line) for a in allowed)]


def test_no_document_describes_iwe_as_a_live_engine_component():
    found = [m for rel in _documents()
             for m in stale_mentions(rel, (REPO / rel).read_text(encoding="utf-8"))]
    assert not found, (
        "IWE is retired (okengine#651); backlinks come from backlink_lib + wiki/.backlinks.json. "
        "Describe the backlink artifact instead, or, for an ecosystem/history mention, add a "
        "justified line-scoped entry to _LINES:\n" + "\n".join(found))


# Walks the whole repository, so it is not a unit test even though its own source does no I/O: it
# used 0.148s CPU against the 0.100s unit limit on main pipeline 13969.
@pytest.mark.integration
def test_the_scan_covers_the_operator_documents():
    docs = set(_documents())
    for rel in ("docs/cold-start-checklist.md", "docs/deploy-a-new-domain.md",
                "docs/technical-reference.md", "docs/overview.md",
                "docs/importing-an-existing-vault.md", "docs/design/composable-okpacks.md"):
        assert rel in docs, f"{rel} is not scanned, so a stale IWE step there would pass"


def test_every_line_allowance_still_matches_a_line():
    """A line-scoped allowance whose line is gone must be deleted, not kept as dormant permission."""
    for rel, entries in _LINES.items():
        path = REPO / rel
        assert path.is_file(), f"allowlisted {rel} no longer exists; drop its entry"
        lines = path.read_text(encoding="utf-8").splitlines()
        for rx, _ in entries:
            assert any(re.search(rx, line) and _IWE.search(line) for line in lines), (
                f"{rel}: allowance {rx!r} matches no iwe line; drop it")


# --- negative fixtures: the detector rejects what it exists to prevent ---------------------------

_STALE = "`ensure-runtime` stages the pinned, sha-verified `iwe` binary at /opt/data/iwe/bin/iwe."


def test_rejects_a_stale_staging_step_in_an_operator_doc():
    assert stale_mentions("docs/cold-start-checklist.md", f"4. Deploy.\n{_STALE}\n") == [
        f"docs/cold-start-checklist.md:2: {_STALE}"]


def test_rejects_a_stale_sentence_in_a_file_that_has_line_allowances():
    text = ("large vault it used about 104 MiB/14 seconds versus IWE's 4.2 GiB/530 seconds.\n"
            "The engine runs IWE for the markdown knowledge graph.\n")
    assert stale_mentions("docs/kb-tooling.md", text) == [
        "docs/kb-tooling.md:2: The engine runs IWE for the markdown knowledge graph."]


def test_rejects_any_case_and_a_new_document():
    assert len(stale_mentions("docs/new-guide.md", "Iwe graph\nIWE stats\n`iwe find`\n")) == 3


def test_accepts_words_that_merely_contain_the_letters():
    assert stale_mentions("docs/overview.md", "pairwise kiwi iwed twelve\n") == []


def test_accepts_history_and_allowlisted_ecosystem_rows():
    assert stale_mentions("CHANGELOG.md", "- Staged iwe 0.3.2.\n") == []
    assert stale_mentions("docs/reviews/2026-09-04-critical-review.md", "IWE documented as live\n") == []
    row = "| IWE — markdown knowledge graph | https://github.com/iwe-org/iwe |"
    assert stale_mentions("docs/okf/guide-4-scaling-to-100k.md", row + "\n") == []


def test_a_history_prefix_does_not_cover_a_sibling_path():
    assert stale_mentions("docs/reviews-new.md", "iwe\n") == ["docs/reviews-new.md:1: iwe"]
