"""okengine#843 (group B): the human-decision paths of ``framework review``.

``--approve`` and ``--decision`` are the CLI paths that write review records a human decided, so a
wrong state, a wrong exit code, or a write path loaded from the wrong place must fail a test. These
tests drive ``main()`` in process against a real temporary vault and the real write path, so the
exact exit code, stream and record of every branch are observable.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "framework_review.py"
SCHEMA = "okf: {required: [type]}\nstrict_types: false\n"
FLAGGED = ("---\ntype: actor\nid: acme\ntitle: Acme\nversion: 2\nneeds_review: true\n---\n"
           "Claim.\n")


def _load(name: str = "framework_review_mutants_b"):
    spec = importlib.util.spec_from_file_location(name, CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """The CLI module plus an isolated process state: env, sys.path and the write_server import."""
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "write_server", raising=False)
    return _load()


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    page = tmp_path / "wiki" / "entities" / "a" / "acme.md"
    page.parent.mkdir(parents=True)
    page.write_text(FLAGGED, encoding="utf-8")
    return tmp_path


def _records(vault: Path) -> list[dict]:
    store = vault / "wiki" / "operational" / "reviews"
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(store.glob("*.yaml"))]


def _frontmatter(page: Path) -> dict:
    return yaml.safe_load(page.read_text(encoding="utf-8").split("---\n")[1])


# --- --approve / --decision: successful writes -------------------------------------------------

def test_approve_writes_an_approved_record_under_the_relative_subject(cli, vault, capsys):
    rc = cli.main([str(vault), "--approve", "entities/a/acme", "--by", "Ann"])
    err = capsys.readouterr().err
    assert rc == 0, f"--approve on an existing flagged page must exit 0, got {rc}: {err}"
    records = _records(vault)
    assert len(records) == 1, f"expected exactly one review record, got {records}"
    rec = records[0]
    assert rec["state"] == "approved", f"--approve must record state approved, got {rec['state']!r}"
    assert rec["subject"] == "entities/a/acme", (
        f"the subject must be the wiki-relative key (okengine#832), got {rec['subject']!r}")
    assert rec["decision_by"] == "Ann" and rec["decision_service"] == "cli", (
        f"the decision must be attributed to --by via the cli service, got {rec}")
    fm = _frontmatter(vault / "wiki" / "entities" / "a" / "acme.md")
    assert fm.get("reviewed_by") == "Ann", f"the page must carry the sign-off, got {fm}"


def test_decision_records_the_named_decision_not_an_approval(cli, vault, capsys):
    rc = cli.main([str(vault), "--decision", "request-changes", "--page", "entities/a/acme",
                   "--by", "Ann", "--note", "cite a source"])
    err = capsys.readouterr().err
    assert rc == 0, f"a valid --decision must exit 0, got {rc}: {err}"
    rec = _records(vault)[0]
    assert rec["state"] == "changes-requested", (
        f"--decision request-changes must not be recorded as {rec['state']!r}")
    assert rec["decision_note"] == "cite a source", f"the --note was dropped: {rec}"
    assert rec["history"][-1]["decision"] == "request-changes", f"wrong history event: {rec}"
    fm = _frontmatter(vault / "wiki" / "entities" / "a" / "acme.md")
    assert "reviewed_by" not in fm, f"request-changes must not sign the page off: {fm}"


def test_decision_result_is_printed_verdict_first(cli, vault, capsys):
    cli.main([str(vault), "--approve", "entities/a/acme", "--by", "Ann"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["ok: true", "status: 200"], (
        f"the printed result must keep the write path's key order (verdict first), got {lines}")


# --- --approve / --decision: refusals ----------------------------------------------------------

def test_a_refused_decision_exits_1_and_prints_the_refusal(cli, vault, capsys):
    rc = cli.main([str(vault), "--decision", "request-changes", "--page", "entities/a/acme",
                   "--by", "Ann"])
    out = capsys.readouterr().out
    assert rc == 1, f"a decision the write path refuses must exit 1, got {rc}"
    lines = out.splitlines()
    assert lines[:3] == ["ok: false", "status: 400",
                         "error: request-changes requires a decision note"], (
        f"the refusal must be printed verdict first, got {lines}")


def test_decision_without_page_is_a_usage_error(cli, vault, capsys):
    rc = cli.main([str(vault), "--decision", "reject", "--by", "Ann", "--note", "x"])
    captured = capsys.readouterr()
    assert rc == 2, f"--decision without --page must exit 2, got {rc}"
    assert "ERROR: --decision requires --page" in captured.err, f"stderr was {captured.err!r}"
    assert "review-queue" not in captured.out, "a usage error must not fall through to the queue"


def test_page_without_decision_is_a_usage_error(cli, vault, capsys):
    rc = cli.main([str(vault), "--page", "entities/a/acme", "--by", "Ann"])
    captured = capsys.readouterr()
    assert rc == 2, f"--page without --decision must exit 2, got {rc}"
    assert "ERROR: --decision requires --page" in captured.err, f"stderr was {captured.err!r}"
    assert not (vault / "wiki" / "operational").exists(), "a usage error must write nothing"


def test_a_decision_without_a_reviewer_is_refused_before_any_write(cli, vault, capsys):
    rc = cli.main([str(vault), "--approve", "entities/a/acme"])
    captured = capsys.readouterr()
    assert rc == 2, f"a decision without --by must exit 2, got {rc}"
    assert "ERROR: review decisions require --by NAME" in captured.err, f"stderr {captured.err!r}"
    assert not (vault / "wiki" / "operational").exists(), "an unattributed decision wrote a record"


def test_a_missing_page_is_reported_not_found(cli, vault, capsys):
    rc = cli.main([str(vault), "--approve", "entities/a/nope", "--by", "Ann"])
    captured = capsys.readouterr()
    assert rc == 1, f"approving a page that does not exist must exit 1, got {rc}"
    assert "ERROR: page not found: entities/a/nope" in captured.err, f"stderr {captured.err!r}"
    assert not (vault / "wiki" / "operational").exists(), "a missing page produced a record"


def test_a_path_outside_the_wiki_is_reported_not_found(cli, vault, capsys):
    rc = cli.main([str(vault), "--approve", "../../etc/passwd", "--by", "Ann"])
    captured = capsys.readouterr()
    assert rc == 1, f"a path escaping the wiki must exit 1, got {rc}"
    assert "ERROR: page not found: ../../etc/passwd" in captured.err, f"stderr {captured.err!r}"


def test_an_unloadable_write_path_exits_1_with_a_diagnostic(cli, vault, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "write_server", None)  # makes `import write_server` fail
    rc = cli.main([str(vault), "--approve", "entities/a/acme", "--by", "Ann"])
    captured = capsys.readouterr()
    assert rc == 1, f"a write path that cannot be imported must exit 1, got {rc}"
    assert "ERROR: cannot load the write path" in captured.err, f"stderr {captured.err!r}"


def test_the_engine_write_path_wins_over_a_shadowing_write_server(cli, vault, tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "write_server.py").write_text(
        "raise ImportError('decoy write_server was imported instead of the engine one')\n",
        encoding="utf-8")
    sys.path.insert(0, str(decoy))  # restored by the cli fixture
    rc = cli.main([str(vault), "--approve", "entities/a/acme", "--by", "Ann"])
    assert rc == 0, "a write_server earlier on sys.path shadowed the engine's enforced write path"
    assert _records(vault)[0]["state"] == "approved", "the engine write path did not record it"


# --- the queue view (no decision) --------------------------------------------------------------

def test_without_a_decision_the_built_queue_is_printed(cli, vault, capsys):
    dash = vault / "wiki" / "dashboards" / "review-queue.md"
    dash.parent.mkdir(parents=True)
    dash.write_text("# Review queue\n\n- entities/a/acme\n", encoding="utf-8")
    rc = cli.main([str(vault)])
    out = capsys.readouterr().out
    assert rc == 0, f"showing the queue must exit 0, got {rc}"
    assert "# Review queue\n\n- entities/a/acme" in out, f"the queue was not printed: {out!r}"
    assert "no review-queue.md yet" not in out, "an existing queue was reported missing"


def test_without_a_built_queue_the_cron_hint_is_printed(cli, vault, capsys):
    rc = cli.main([str(vault)])
    out = capsys.readouterr().out
    assert rc == 0, f"a missing queue is not an error, got exit {rc}"
    assert "no review-queue.md yet" in out, f"the missing-queue hint was not printed: {out!r}"


# --- --migrate --apply: imported legacy sign-off records ----------------------------------------

def _signed(vault: Path, rel: str, by: str) -> None:
    page = vault / "wiki" / f"{rel}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: actor\ntitle: T\nversion: 3\nreviewed_by: {by}\n"
                    "reviewed_on: 2026-06-01\n---\nClaim.\n", encoding="utf-8")


def _migrated(vault: Path, cli, capsys) -> list[Path]:
    rc = cli.main([str(vault), "--migrate", "--apply"])
    captured = capsys.readouterr()
    assert rc == 0, f"--migrate --apply must exit 0, got {rc}: {captured.err}"
    return sorted((vault / "wiki" / "operational" / "reviews").glob("*.yaml"))


def test_migration_loads_the_engine_write_path_over_a_shadowing_write_server(cli, tmp_path, capsys):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "write_server.py").write_text(
        "raise ImportError('decoy write_server was imported instead of the engine one')\n",
        encoding="utf-8")
    sys.path.insert(0, str(decoy))  # restored by the cli fixture
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    _signed(tmp_path, "entities/a/signed", "Jane")
    rc = cli.main([str(tmp_path), "--migrate", "--apply"])
    err = capsys.readouterr().err
    assert rc == 0, f"a write_server earlier on sys.path shadowed the engine write path: {err}"
    files = sorted((tmp_path / "wiki" / "operational" / "reviews").glob("*.yaml"))
    assert len(files) == 1, f"the engine write path did not import the sign-off, got {files}"


def test_migration_creates_the_ledger_directory_on_a_fresh_vault(cli, tmp_path, capsys):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    _signed(tmp_path, "entities/a/signed", "Jane")
    assert not (tmp_path / "wiki" / "operational").exists()
    files = _migrated(tmp_path, cli, capsys)
    assert len(files) == 1, f"one legacy sign-off must yield one record, got {files}"


def test_migration_imports_several_signoffs_into_one_ledger(cli, tmp_path, capsys):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    _signed(tmp_path, "entities/a/one", "Jane")
    _signed(tmp_path, "entities/b/two", "Joe")
    files = _migrated(tmp_path, cli, capsys)
    assert len(files) == 2, f"two legacy sign-offs must yield two records, got {files}"


def test_imported_records_keep_the_ledger_key_order(cli, tmp_path, capsys):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    _signed(tmp_path, "entities/a/signed", "Jane")
    text = _migrated(tmp_path, cli, capsys)[0].read_text(encoding="utf-8")
    keys = [line.split(":", 1)[0] for line in text.splitlines() if line and line[0].isalpha()]
    assert keys[:4] == ["version", "review_id", "subject", "subject_version"], (
        f"an imported record must keep the ledger's field order, got {keys}")


def test_imported_records_keep_a_non_ascii_reviewer_readable(cli, tmp_path, capsys):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    _signed(tmp_path, "entities/a/signed", "José")
    text = _migrated(tmp_path, cli, capsys)[0].read_text(encoding="utf-8")
    assert "decision_by: José" in text, (
        f"the reviewer's name must be stored verbatim, not escaped: {text!r}")


# --- module entry point --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["framework_review", "Framework_review"])
def test_importing_the_module_never_runs_the_cli(monkeypatch, tmp_path, name):
    monkeypatch.setattr(sys, "argv", ["framework_review.py", str(tmp_path)])
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    try:
        module = _load(name)
    except SystemExit as e:
        pytest.fail(f"importing the CLI as {name!r} ran main() (exit {e.code})")
    assert callable(module.main)


def test_running_the_file_as_a_script_runs_the_cli(tmp_path):
    out = subprocess.run([sys.executable, str(CLI), str(tmp_path)],
                         text=True, capture_output=True, check=False, timeout=120)
    assert out.returncode == 0, f"script run failed: {out.stderr}"
    assert "no review-queue.md yet" in out.stdout, f"the script did not run main(): {out.stdout!r}"
