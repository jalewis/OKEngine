"""Operator-facing contracts of the source re-identification lane the original suite left unpinned.

Real temporary vaults run through the real planner and CLI; the dry-run report is what an operator
reads before `--apply`, so its truncation and counts are asserted exactly. The only monkeypatch is a
delegating `Path.rglob` that fixes the filesystem's arbitrary directory order, so a test that needs
one page visited before another is deterministic.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
REID = REPO / "scripts" / "reid_sources_by_url.py"

if str(REID.parent) not in sys.path:
    sys.path.insert(0, str(REID.parent))
_SPEC = importlib.util.spec_from_file_location("reid_sources_by_url_mutation", REID)
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


def _page(vault: Path, rel: str, *, url=None, pid=None, ptype="source", status=None) -> Path:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"type: {ptype}", "title: A Title"]
    if pid:
        lines.append(f"id: {pid}")
    if url:
        lines.append(f"url: {url}")
    if status:
        lines.append(f"status: {status}")
    lines += ["---", "Body.", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _corpus(vault: Path, *, dups: int, rewrites: int, first_without_id: bool = True) -> None:
    """`dups` strong holders each shadowed by a legacy duplicate, plus `rewrites` unique pages."""
    for i in range(dups):
        url = f"https://example.org/dup/{i}"
        _page(vault, f"sources/strong-{i:02d}.md", url=url, pid=m.source_url_id(url))
        _page(vault, f"sources/zdup-{i:02d}.md", url=url, pid=f"sources:legacy-dup-{i:02d}")
    for i in range(rewrites):
        pid = None if (i == 0 and first_without_id) else f"sources:legacy-rw-{i:02d}"
        _page(vault, f"sources/rw-{i:02d}.md", url=f"https://example.org/rw/{i}", pid=pid)


def _run(vault: Path, capsys, *args: str) -> list[str]:
    assert m.main(["--vault", str(vault), *args]) == 0
    return capsys.readouterr().out.splitlines()


def _listed_pairs(lines):
    return [line for line in lines if line.startswith("      sources:url-")]


def _listed_rewrites(lines):
    return [line for line in lines if line.startswith("   sources/rw-")]


def _more(lines):
    return [line for line in lines if "… and" in line]


# --- the dry-run report -------------------------------------------------------------------------

def test_a_long_report_lists_ten_pairs_and_eight_rewrites_then_counts_the_rest(tmp_path, capsys):
    _corpus(tmp_path, dups=23, rewrites=19)
    lines = _run(tmp_path, capsys)
    assert lines[0] == "=== reid-sources-by-url (DRY-RUN) ==="
    assert "  source pages     : 65" in lines
    assert "  to re-identify   : 19" in lines
    assert "  already strong   : 23" in lines
    assert "  duplicate pairs  : 23  (reported, NOT re-identified)" in lines
    pairs = _listed_pairs(lines)
    assert len(pairs) == 10
    assert pairs[0] == "      " + m.source_url_id("https://example.org/dup/0")
    listed = _listed_rewrites(lines)
    assert listed == [f"   sources/rw-{i:02d}.md" for i in range(8)]
    assert _more(lines) == ["      … and 13 more", "   … and 11 more"]
    first = lines.index("   sources/rw-00.md")
    assert lines[first + 1] == (
        "      (no id)  ->  " + m.source_url_id("https://example.org/rw/0"))
    second = lines.index("   sources/rw-01.md")
    assert lines[second + 1] == (
        "      sources:legacy-rw-01  ->  " + m.source_url_id("https://example.org/rw/1"))
    assert lines[-1] == "  (dry run — nothing written; pass --apply)"


def test_exactly_ten_pairs_and_eight_rewrites_need_no_remainder_line(tmp_path, capsys):
    _corpus(tmp_path, dups=10, rewrites=8)
    lines = _run(tmp_path, capsys)
    assert len(_listed_pairs(lines)) == 10
    assert len(_listed_rewrites(lines)) == 8
    assert _more(lines) == []


def test_one_over_each_limit_reports_a_remainder_of_one(tmp_path, capsys):
    _corpus(tmp_path, dups=11, rewrites=9)
    lines = _run(tmp_path, capsys)
    assert len(_listed_pairs(lines)) == 10
    assert len(_listed_rewrites(lines)) == 8
    assert _more(lines) == ["      … and 1 more", "   … and 1 more"]


def test_a_short_report_lists_everything_and_needs_no_remainder_line(tmp_path, capsys):
    _corpus(tmp_path, dups=3, rewrites=2, first_without_id=False)
    lines = _run(tmp_path, capsys)
    assert len(_listed_pairs(lines)) == 3
    assert _listed_rewrites(lines) == ["   sources/rw-00.md", "   sources/rw-01.md"]
    assert _more(lines) == []


def test_limit_is_reported_against_the_full_plan(tmp_path, capsys):
    _corpus(tmp_path, dups=0, rewrites=19)
    lines = _run(tmp_path, capsys, "--limit", "5")
    assert "  to re-identify   : 5 (of 19, --limit)" in lines
    assert len(_listed_rewrites(lines)) == 5
    assert _more(lines) == []


def test_apply_announces_itself(tmp_path, capsys):
    _corpus(tmp_path, dups=0, rewrites=1)
    lines = _run(tmp_path, capsys, "--apply")
    assert lines[0] == "=== reid-sources-by-url (APPLY) ==="
    assert "  to re-identify   : 1" in lines


def test_the_vault_argument_is_required():
    with pytest.raises(SystemExit) as exc:
        m.main([])
    assert exc.value.code == 2


def test_the_script_runs_as_a_command(tmp_path):
    _corpus(tmp_path, dups=0, rewrites=1)
    proc = subprocess.run([sys.executable, str(REID), "--vault", str(tmp_path)],
                          capture_output=True, text=True, timeout=120, check=False,
                          env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "=== reid-sources-by-url (DRY-RUN) ==="
    assert "  to re-identify   : 1" in proc.stdout


# --- the planner skips a page without abandoning the rest -----------------------------------------

def test_each_skip_reason_is_counted_once_and_later_pages_are_still_planned(tmp_path):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "wiki" / "sources" / "a-nofm.md").write_text("no frontmatter\n", encoding="utf-8")
    _page(tmp_path, "sources/b-topic.md", url="https://example.org/topic", ptype="topic")
    strong_url = "https://example.org/strong"
    _page(tmp_path, "sources/c-strong.md", url=strong_url, pid=m.source_url_id(strong_url))
    _page(tmp_path, "sources/d-nourl.md", pid="sources:legacy-d")
    _page(tmp_path, "sources/e-tomb.md", url="https://example.org/e", status="tombstoned")
    _page(tmp_path, "sources/f-dup.md", url=strong_url, pid="sources:legacy-f")
    verified = _page(tmp_path, "sources/g-verified.md", url="https://example.org/g",
                     pid="sources:legacy-g", status="verified")
    last = _page(tmp_path, "sources/z-last.md", url="https://example.org/z", pid="sources:legacy-z")

    result = m.plan(tmp_path)

    assert result["skipped"] == {"already_strong": 1, "no_url": 1, "not_source": 1,
                                 "no_frontmatter": 1, "tombstoned": 1}
    assert result["dup_pairs"] == [
        (m.source_url_id(strong_url), "sources/c-strong.md", "sources/f-dup.md")]
    assert [(p, old) for p, old, _new in result["rewrite"]] == [
        (verified, "sources:legacy-g"), (last, "sources:legacy-z")]
    assert result["total"] == 8


def test_two_non_source_pages_count_two(tmp_path):
    _page(tmp_path, "sources/a.md", url="https://example.org/a", ptype="topic")
    _page(tmp_path, "sources/b.md", url="https://example.org/b", ptype="entity")
    assert m.plan(tmp_path)["skipped"]["not_source"] == 2


# --- the reference scan skips logs and unparsed pages without stopping ---------------------------

def test_the_reference_scan_continues_past_logs_and_frontmatterless_pages(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    log = wiki / "log.md"
    log.write_text("---\nsubject: sources:legacy-a\n---\n", encoding="utf-8")
    bare = wiki / "bare.md"
    bare.write_text("subject: sources:legacy-a\n", encoding="utf-8")
    ref = wiki / "ref.md"
    ref.write_text("---\ntype: note\nsubject: sources:legacy-a\n---\n", encoding="utf-8")

    real_rglob = Path.rglob

    def ordered(self, pattern, *args, **kwargs):
        found = list(real_rglob(self, pattern, *args, **kwargs))
        rank = {log: 0, bare: 1}
        return iter(sorted(found, key=lambda p: (rank.get(p, 2), str(p))))

    monkeypatch.setattr(Path, "rglob", ordered)
    assert m.structured_reference_rewrites(
        tmp_path, {"sources:legacy-a": "sources:url-aaaaaaaaaaaaaaaaaaaa"}) == [
        (ref, {"sources:legacy-a": "sources:url-aaaaaaaaaaaaaaaaaaaa"})]
