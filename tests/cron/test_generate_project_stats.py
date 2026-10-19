"""generate_project_stats: the project-stats dashboard's collectors and renderer.

Each collector runs in-process against a disposable vault (and a disposable Hermes data dir holding
a real cron-plus ``jobs.json`` and a real SQLite ``state.db``), with the module's path constants
pointed at them. The renderer is exercised on collector output, so what is asserted is the page an
operator reads.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "generate_project_stats.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_project_stats_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


G = _load_module()
TODAY = date(2026, 9, 27)


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    data = tmp_path / "hermes-data"
    data.mkdir()
    # A tmp_path inside a Git checkout -- the mutation gate puts TMPDIR in its worktree -- would let
    # `git -C vault` find that checkout's history and report it as the vault's (okengine#1046).
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(G, "VAULT", vault)
    monkeypatch.setattr(G, "HERMES_DATA", data)
    monkeypatch.setattr(G, "OUT", vault / "wiki" / "dashboards" / "project-stats.md")
    monkeypatch.setattr(G, "TODAY", TODAY)
    return vault, data


def _write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- namespaces, frontmatter, file listing ------------------------------------------------------

def test_declared_namespaces_exclude_operational_dashboards_and_excluded_dirs(env):
    vault, _ = env
    _write(vault / "schema.yaml", "partitioning:\n  namespaces:\n    entities: {strategy: flat}\n"
                                  "    dashboards: {strategy: flat}\n    scratch: {strategy: flat}\n"
                                  "exclude: [wiki/scratch/]\n")

    assert G.knowledge_namespaces() == ["entities"]


def test_namespaces_fall_back_to_visible_on_disk_dirs(env):
    vault, _ = env
    for d in ("widgets", "gadgets", "_drafts", ".git", "operational"):
        (vault / "wiki" / d).mkdir()
    _write(vault / "wiki" / "README.md")

    assert G.knowledge_namespaces() == ["gadgets", "widgets"]


def test_no_namespaces_without_a_wiki_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "VAULT", tmp_path / "empty")

    assert G.knowledge_namespaces() == []


def test_parse_frontmatter_reads_a_mapping(tmp_path):
    page = _write(tmp_path / "p.md", "---\ntype: entity\ntitle: A\n---\nbody\n")

    assert G.parse_frontmatter(page) == {"type": "entity", "title": "A"}


@pytest.mark.parametrize("text", ["no frontmatter\n", "---\ntype: [unclosed\n---\n", "---\n- a\n---\n"])
def test_parse_frontmatter_is_empty_for_absent_broken_or_non_mapping_frontmatter(tmp_path, text):
    assert G.parse_frontmatter(_write(tmp_path / "p.md", text)) == {}


def test_parse_frontmatter_is_empty_for_an_unreadable_page(tmp_path):
    assert G.parse_frontmatter(tmp_path / "vanished.md") == {}


def test_md_files_is_empty_for_a_missing_namespace(env):
    assert G.md_files("nope") == []


def test_md_files_skips_index_and_scaffold_pages_across_shards(env):
    vault, _ = env
    for rel in ("a.md", "b/c.md", "_draft.md", "INDEX.md", "b/INDEX-b.md", "notes.txt"):
        _write(vault / "wiki" / "widgets" / rel)
    (vault / "wiki" / "widgets" / "dir.md").mkdir()

    names = sorted(p.name for p in G.md_files("widgets"))

    assert names == ["a.md", "c.md"]


# --- corpus -------------------------------------------------------------------------------------

def test_corpus_counts_raw_files_bytes_and_top_level_subdirs(env):
    vault, _ = env
    _write(vault / "raw" / "feeds" / "a.txt", "12345")
    _write(vault / "raw" / "feeds" / "deep" / "b.txt", "123")
    _write(vault / "raw" / "top.txt", "1")
    (vault / "raw" / "feeds" / "dangling.txt").symlink_to(vault / "raw" / "gone.txt")
    _write(vault / "wiki" / "entities" / "a.md")
    _write(vault / "wiki" / "_scaffold.md")

    corpus = G.corpus_stats()

    assert (corpus["raw_files"], corpus["raw_bytes"]) == (3, 9)
    assert corpus["raw_subdirs"] == Counter({"feeds": 2, "(root)": 1})
    assert corpus["wiki_md"] == 1


def test_corpus_without_raw_or_git_reports_nothing_for_them(env):
    corpus = G.corpus_stats()

    assert (corpus["raw_files"], corpus["raw_bytes"], corpus["git"]) == (0, 0, {})


def test_corpus_reads_vault_git_history(env):
    vault, _ = env
    git = ["git", "-C", str(vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "first | snapshot"], check=True)

    info = G.corpus_stats()["git"]

    assert info["commits"] == 1
    assert info["last_commit_subject"] == "first | snapshot"
    assert info["last_commit_at"][:4].isdigit()


# --- namespace stats ----------------------------------------------------------------------------

def test_namespace_stats_bucket_by_type_then_category_then_untyped(env):
    vault, _ = env
    _write(vault / "wiki" / "widgets" / "a.md", "---\ntype: gear\n---\n")
    _write(vault / "wiki" / "widgets" / "b.md", "---\ncategory: legacy\n---\n")
    _write(vault / "wiki" / "widgets" / "c.md", "no frontmatter\n")

    stats = G.namespace_stats(["widgets", "absent"])

    assert stats == {"widgets": Counter({"gear": 1, "legacy": 1, "untyped": 1}), "absent": Counter()}


# --- sources ------------------------------------------------------------------------------------

def test_read_publisher_strips_quotes(tmp_path):
    page = _write(tmp_path / "s.md", "---\ntype: source\npublisher: \"Example Labs\"\n---\n")

    assert G._read_publisher(page) == "Example Labs"


@pytest.mark.parametrize("text", ["---\ntype: source\n---\n", "---\npublisher: ''\n---\n"])
def test_read_publisher_is_none_when_absent_or_empty(tmp_path, text):
    assert G._read_publisher(_write(tmp_path / "s.md", text)) is None


def test_read_publisher_is_none_for_an_unreadable_page(tmp_path):
    assert G._read_publisher(tmp_path / "vanished.md") is None


def test_source_stats_bucket_by_filename_date_and_publisher(env):
    vault, _ = env
    src = vault / "wiki" / "sources"
    for days, pub in ((1, "A"), (20, "A"), (60, "B"), (200, None)):
        day = TODAY - timedelta(days=days)
        _write(src / f"{day:%Y/%m}" / f"{day.isoformat()}-item.md",
               f"---\npublisher: {pub}\n---\n" if pub else "---\ntype: source\n---\n")
    _write(src / "2026-02-31-impossible.md", "---\npublisher: C\n---\n")
    _write(src / "undated.md", "---\npublisher: C\n---\n")

    stats = G.source_stats()

    assert stats["total"] == 6
    assert (stats["last_7"], stats["last_30"], stats["last_90"]) == (1, 2, 3)
    assert stats["by_year"] == Counter({"2026": 5})
    assert dict(stats["top_publishers"]) == {"A": 2, "C": 2, "B": 1}
    assert stats["top_publishers"][-1] == ("B", 1)
    assert stats["publishers_total"] == 3


# --- cron-plus health ---------------------------------------------------------------------------

def _jobs(data: Path, payload) -> None:
    _write(data / "cron-plus" / "jobs.json", payload if isinstance(payload, str) else json.dumps(payload))


def test_cron_health_is_unavailable_without_jobs_json(env):
    assert G.cron_health() == {"available": False}


@pytest.mark.parametrize("payload", ["{not json", {"jobs": {"a": 1}}, "42"])
def test_cron_health_is_unavailable_for_unusable_jobs_json(env, payload):
    _jobs(env[1], payload)

    assert G.cron_health() == {"available": False}


def test_cron_health_classifies_each_job_state(env):
    _jobs(env[1], {"jobs": [
        {"name": "off", "enabled": False},
        {"name": "fresh"},
        {"name": "ok", "last_run_at": "2026-09-27T01:00:00Z", "last_run_success": True},
        {"name": "bad", "last_run_at": "2026-09-27T01:00:00Z", "last_run_success": False,
         "last_error": "  boom " + "x" * 300},
        {"id": "undelivered", "last_run_at": "2026-09-27T01:00:00Z", "last_delivery_error": "smtp"},
        {"last_run_at": "2026-09-27T01:00:00Z"},
    ]})

    health = G.cron_health()

    assert {k: health[k] for k in ("total", "success", "failure", "never", "disabled")} == {
        "total": 6, "success": 1, "failure": 3, "never": 1, "disabled": 1}
    assert [name for name, _ in health["failures"]] == ["bad", "undelivered", "?"]
    assert health["failures"][0][1].startswith("boom") and len(health["failures"][0][1]) == 200
    assert health["failures"][2][1] == ""


def test_cron_health_accepts_a_bare_job_list(env):
    _jobs(env[1], [{"name": "ok", "last_run_at": "t", "last_run_success": True}])

    assert G.cron_health()["success"] == 1


# --- Hermes sessions ----------------------------------------------------------------------------

def _state_db(data: Path, rows) -> Path:
    db = data / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (source TEXT, model TEXT, started_at REAL, input_tokens INT,"
                 " output_tokens INT, cache_read_tokens INT, actual_cost_usd REAL, estimated_cost_usd REAL)")
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_sessions_are_unavailable_without_a_state_db(env):
    assert G.hermes_session_stats() == {"available": False}


def test_sessions_are_unavailable_when_the_db_cannot_be_opened(env):
    db = _write(env[1] / "state.db", "")
    db.chmod(0)
    try:
        assert not os.access(db, os.R_OK), "run as a non-root user: root can open a mode-000 file"
        stats = G.hermes_session_stats()
    finally:
        db.chmod(0o600)

    assert stats == {"available": False}


def test_sessions_report_the_query_error_for_a_non_hermes_db(env):
    _write(env[1] / "state.db", "this is not a sqlite database, just text" * 10)

    stats = G.hermes_session_stats()

    assert stats["available"] is False
    assert stats["error"]


def test_sessions_are_summed_per_window_source_and_model(env):
    now = G.NOW.timestamp()
    day = 86400
    _state_db(env[1], [
        ("cron", "m1", now - 3600, 10, 5, 2, 0.5, 0.25),
        ("cron", "m1", now - 3 * day, 20, 10, 0, 0.0, 0.50),
        ("cli", None, now - 20 * day, 1, 1, 0, 0.0, 1.00),
        ("cli", "m2", now - 60 * day, 100, 100, 0, 9.0, 9.00),
    ])

    stats = G.hermes_session_stats()

    assert stats["available"] is True
    assert stats["windows"]["24h"] == {"sessions": 1, "input_tokens": 10, "output_tokens": 5,
                                       "cache_read": 2, "actual_cost": 0.5, "estimated_cost": 0.25}
    assert stats["windows"]["7d"]["sessions"] == 2
    assert stats["windows"]["30d"]["estimated_cost"] == pytest.approx(1.75)
    assert stats["sources_30d"] == [("cron", 2), ("cli", 1)]
    assert stats["models_30d"] == [("m1", 2, 0.75), ("?", 1, 1.0)]


# --- freshness ----------------------------------------------------------------------------------

def test_freshness_counts_recent_and_stale_pages_by_mtime(env):
    vault, _ = env
    now = G.NOW.timestamp()
    for name, age_days in (("recent", 1), ("middle", 30), ("stale", 120)):
        page = _write(vault / "wiki" / "widgets" / f"{name}.md")
        os.utime(page, (now - age_days * 86400,) * 2)

    assert G.freshness_stats(["widgets"]) == {"widgets": {"recent_7d": 1, "stale_90d": 1}}


def test_freshness_skips_a_page_that_vanishes_after_listing(env, monkeypatch):
    # A live vault changes under the scan: a page listed by md_files can be gone before its stat.
    vault, _ = env
    kept = _write(vault / "wiki" / "widgets" / "kept.md")
    monkeypatch.setattr(G, "md_files", lambda ns: [vault / "wiki" / ns / "vanished.md", kept])

    assert G.freshness_stats(["widgets"]) == {"widgets": {"recent_7d": 1, "stale_90d": 0}}


# --- formatting and rendering -------------------------------------------------------------------

@pytest.mark.parametrize(("n", "text"), [(0, "0.0 B"), (1536, "1.5 KB"), (3 * 1024 ** 5, "3.0 PB")])
def test_fmt_bytes_scales_units(n, text):
    assert G.fmt_bytes(n) == text


def _corpus(**over):
    base = {"raw_files": 0, "raw_bytes": 0, "raw_subdirs": Counter(), "wiki_md": 0, "git": {}}
    return {**base, **over}


def _sources(**over):
    base = {"total": 0, "by_year": Counter(), "last_7": 0, "last_30": 0, "last_90": 0,
            "top_publishers": [], "publishers_total": 0}
    return {**base, **over}


def test_render_minimal_page_notes_every_unavailable_source():
    page = G.render(_corpus(), {"widgets": Counter()}, _sources(), {"available": False},
                    {"available": False}, {}, ["widgets"])

    assert page.startswith("---\ntype: dashboard\ntitle: Project Stats\n")
    assert "| widgets | 0 |" in page
    assert "Vault git commits" not in page
    assert "Top 15 raw/ subdirs" not in page
    assert "## widgets (0)\n\n\n" in page
    assert "## Source analytics" not in page
    assert "_cron-plus jobs.json not readable" in page
    assert "_state.db not readable" in page
    assert "| widgets | " not in page.split("## Freshness", 1)[1]


def test_render_full_page_shows_every_section():
    corpus = _corpus(raw_files=2, raw_bytes=2048, raw_subdirs=Counter({"feeds": 2}), wiki_md=5,
                     git={"commits": 12, "last_commit_at": "2026-09-27T00:00:00Z", "last_commit_subject": "s"})
    ns = {"widgets": Counter({"gear": 2, "untyped": 1})}
    sources = _sources(total=3, by_year=Counter({str(TODAY.year): 2, "2001": 1, "misc": 4}),
                       last_7=1, last_30=2, last_90=3, top_publishers=[("A", 2)], publishers_total=1)
    cron = {"available": True, "total": 3, "success": 1, "failure": 1, "never": 1, "disabled": 0,
            "failures": [("bad", "line1\nline2"), ("quiet", "")]}
    window = {"sessions": 2, "input_tokens": 1000, "output_tokens": 10, "cache_read": 0,
              "actual_cost": 0.0, "estimated_cost": 1.5}
    hermes = {"available": True, "windows": {"24h": window, "7d": window, "30d": window},
              "sources_30d": [("cron", 2), (None, 1)], "models_30d": [("m1", 2, 1.5)]}
    freshness = {"widgets": {"recent_7d": 1, "stale_90d": 2}}

    page = G.render(corpus, ns, sources, cron, hermes, freshness, ["widgets"])

    assert "| Vault git commits | 12 |" in page
    assert "| `feeds` | 2 |" in page
    assert "| gear | 2 |" in page and "| untyped ⚠ untyped | 1 |" in page
    assert "| Last 90 days | 3 |" in page
    assert f"| {TODAY.year} | 2 |" in page and "| earlier | 1 |" in page
    assert "| A | 2 |" in page
    assert "- `bad` — line1 line2" in page and "- `quiet` — (no error message)" in page
    assert "| 24h | 2 | 1,000 | 10 | 0 | $1.50 |" in page
    assert "| cron | 2 |" in page and "| ? | 1 |" in page
    assert "| m1 | 2 | $1.50 |" in page
    assert "| widgets | 1 | 2 |" in page


def test_render_omits_empty_failure_source_and_model_lists():
    window = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
              "actual_cost": 0.0, "estimated_cost": 0.0}
    cron = {"available": True, "total": 0, "success": 0, "failure": 0, "never": 0, "disabled": 0,
            "failures": []}
    hermes = {"available": True, "windows": {"24h": window, "7d": window, "30d": window},
              "sources_30d": [], "models_30d": []}

    page = G.render(_corpus(), {}, _sources(), cron, hermes, {}, [])

    assert "Currently in failure state" not in page
    assert "Sessions by source" not in page
    assert "Top models" not in page


# --- main ---------------------------------------------------------------------------------------

def test_main_without_a_vault_is_an_error_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(G, "VAULT", tmp_path / "missing")
    monkeypatch.setattr(sys, "argv", ["generate_project_stats.py"])

    assert G.main() == 1
    assert "vault not found" in capsys.readouterr().err


def test_main_dry_run_prints_without_writing(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["generate_project_stats.py", "--dry-run"])

    rc = G.main()

    assert rc == 0
    assert "# Project Stats" in capsys.readouterr().out
    assert not G.OUT.exists()


def test_main_writes_the_dashboard_and_keeps_one_backup(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["generate_project_stats.py"])

    assert G.main() == 0
    first = G.OUT.read_text()
    assert G.main() == 0

    assert capsys.readouterr().out.strip().splitlines()[-1] == '{"wakeAgent": false}'
    assert (G.OUT.parent / "project-stats.md.bak").read_text() == first
