"""okengine#1066: no document describes IWE as a live engine component.

IWE was retired from the runtime in okengine#651. The backlink graph is built by
`backlink_lib.scan_forward_refs` (okengine#179), published by the `backlinks-refresh` cron as
`wiki/.backlinks.json`, and served by the read MCP, reader and cockpit. Docs kept telling
operators to stage an `iwe` binary and naming it as the engine's graph layer, long after nothing
shipped it.

Every mention of `iwe` (case-insensitive, whole word) in the repository's documents and in the
comments, docstrings and UI text of its code and configuration must sit in an allowlisted context: history, or IWE as an ecosystem tool the engine does not run. A
line-scoped entry allows only lines matching its pattern, so a stale sentence added elsewhere in
the same file still fails. Line-scoped entries must keep matching, so the allowlist only shrinks.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_IWE = re.compile(r"\biwe\b", re.IGNORECASE)

# Directory parts never scanned: VCS/tool state, dependencies, build output, and test fixture data
# (fixture vault pages are inputs to tests, not documentation; test files may name the retired tool
# to pin its absence). Any other hidden directory is local tool state too, except .github.
_SKIP_PARTS = frozenset({"node_modules", "__pycache__", "tests", "build", "dist", "site-packages"})

# Text surfaces scanned: an operator reads comments, docstrings and UI strings as much as markdown,
# and a stale "runs iwe" sentence in a code comment misleads the same way (okengine#1066 follow-up).
_SUFFIXES = frozenset({".md", ".py", ".sh", ".js", ".css", ".html", ".yaml", ".yml", ".toml",
                       ".txt", ".json", ".template", ".example", ".service", ".timer", ".mjs"})
_NAMES = frozenset({"Dockerfile", "Makefile"})

# Whole-file allowances: records of a past state.
_HISTORY = {
    "CHANGELOG.md": "release history; entries describe the engine as it was when released",
    "docs/reviews/": "dated audit records; rewriting a finding would falsify what was reviewed",
}

# Line-scoped allowances: {path: [(line regex, reason)]}.
_RETIRED = "states that the engine no longer stages or runs an iwe binary"
_PARITY = "the in-process scanner's parity with the retired IWE parser it replaced (okengine#179)"
_LINES = {
    "okengine-cockpit/Dockerfile": [(r"no iwe binary needed", _RETIRED)],
    "okengine-reader/Dockerfile": [(r"no iwe binary needed", _RETIRED)],
    "okengine-cockpit/app.py": [
        (r"^# the heavy `iwe find -l 0` full-graph dump", "history: what the fallback scan replaced"),
    ],
    "okengine-mcp/server.py": [
        (r"rebuild the IWE graph live on EVERY", "history: what the backlinks artifact replaced"),
        (r"cheap body parse, no IWE", _RETIRED),
    ],
    "okengine-reader/static/index.html": [
        (r'href="https://github\.com/iwe-org/iwe".*IWE — markdown knowledge graph',
         "About panel references list (ecosystem recommendation)"),
    ],
    "scripts/check-test-skips.py": [
        (r'"Dockerfile does not download IWE"', "quotes a skip reason tests/test_dockerfile_pins.py emits"),
    ],
    "scripts/cron/backlink_lib.py": [
        (r"originally the heavy `iwe find -f json -l 0`", "history: what the scanner replaced"),
        (r"^Titles are curated, not IWE's \(IWE's \"title\"", _PARITY),
        (r"^# Replaces the `iwe find -f json -l 0` full-graph dump", _PARITY),
        (r"at 99\.99% edge parity with iwe; the residual diff", _PARITY),
        (r"^# links iwe's markdown parser drops", _PARITY),
        (r"^# iwe link semantics reproduced", _PARITY),
        (r"an unresolved link stays literal \(dangling, like iwe\)", _PARITY),
        (r"before link extraction \(iwe parses the AST", _PARITY),
        (r"forward references \(iwe-parity\), returning iwe-shaped docs", _PARITY),
        (r"resolve to the same keys iwe produces", _PARITY),
    ],
    "scripts/cron/backlinks_refresh.py": [(r"REPLACES the former `iwe find -f json", _PARITY)],
    "scripts/cron/deployment_checks.py": [
        (r"backlinks-refresh no longer needs iwe", _RETIRED),
        (r"no iwe binary dependency to validate here\. iwe is", _RETIRED),
    ],
    "scripts/cron/okf_migrate.py": [
        (r"^Replicates the semantics of IWE's `iwe rename`", "names the link contract it reproduces"),
        (r"because per-file `iwe rename` rebuilt the", "history: why the mover is a single pass"),
        (r"The engine no longer runs IWE", _RETIRED),
    ],
    "scripts/ensure-runtime.sh": [(r"^# No IWE binary is staged anywhere", _RETIRED)],
    "scripts/import_lib.py": [(r"same `iwe rename` link contract", "names the link contract the reshard reproduces")],
    "scripts/post_deploy_verify.sh": [
        (r"backlinks-refresh no longer needs an iwe binary", _RETIRED),
        (r"no gateway iwe dependency to verify here anymore", _RETIRED),
    ],
    "src/okengine/cockpit_services/navigation.py": [
        (r"replaces iwe's raw first-heading title", _PARITY),
        (r"Forward-reference scan over WIKI \(iwe-parity\)", _PARITY),
        (r"precomputed artifact wins — no iwe here", _RETIRED),
    ],
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


def _scanned(rel: Path) -> bool:
    """True for a text surface outside the skipped directories."""
    dirs = rel.parts[:-1]
    if _SKIP_PARTS.intersection(dirs) or any(d.startswith(".") and d != ".github" for d in dirs):
        return False
    return rel.suffix in _SUFFIXES or rel.name in _NAMES


def _documents() -> list[str]:
    """Repository-relative paths of every scanned text surface."""
    return sorted(p.relative_to(REPO).as_posix() for p in REPO.rglob("*")
                  if p.is_file() and _scanned(p.relative_to(REPO)))


def stale_mentions(rel: str, text: str) -> list[str]:
    """`rel:line: text` for each line mentioning iwe outside the allowlist."""
    if any(rel == h or (h.endswith("/") and rel.startswith(h)) for h in _HISTORY):
        return []
    allowed = [re.compile(rx) for rx, _ in _LINES.get(rel, [])]
    return [f"{rel}:{n}: {line.strip()}"
            for n, line in enumerate(text.splitlines(), 1)
            if _IWE.search(line) and not any(a.search(line) for a in allowed)]


# Reads every scanned file (806 at ae763786, 0.274s CPU), well over the 0.100s unit limit.
@pytest.mark.integration
def test_no_document_describes_iwe_as_a_live_engine_component():
    found = [m for rel in _documents()
             for m in stale_mentions(rel, (REPO / rel).read_text(encoding="utf-8", errors="replace"))]
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
                "docs/importing-an-existing-vault.md", "docs/design/composable-okpacks.md",
                "okengine-reader/Dockerfile", "okengine-reader/static/app.js",
                "okengine-cockpit/static/style.css", "okengine-reader/static/index.html",
                "scripts/cron/backlink_lib.py", "scripts/ensure-runtime.sh",
                "config/config.yaml.template"):
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


def test_rejects_a_stale_code_comment_and_ui_text():
    assert stale_mentions("scripts/cron/backlink_lib.py", "# The UIs keep a live-iwe build.\n") == [
        "scripts/cron/backlink_lib.py:1: # The UIs keep a live-iwe build."]
    assert len(stale_mentions("okengine-reader/static/app.js", "// IWE knowledge-graph\n")) == 1


def test_skips_tool_state_and_tests_but_scans_code_and_github():
    assert _scanned(Path("okengine-reader/Dockerfile"))
    assert _scanned(Path(".github/workflows/ci.yml"))
    assert not _scanned(Path(".venv/lib/x.py"))
    assert not _scanned(Path("tests/test_dockerfile_pins.py"))
    assert not _scanned(Path("okengine-reader/static/logo.png"))


def test_a_history_prefix_does_not_cover_a_sibling_path():
    assert stale_mentions("docs/reviews-new.md", "iwe\n") == ["docs/reviews-new.md:1: iwe"]
