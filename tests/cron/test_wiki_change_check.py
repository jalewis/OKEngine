"""wiki_change_check: generated files are ignored and baselines advance only after success."""
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    spec = importlib.util.spec_from_file_location(
        "wcc", REPO / "scripts/cron/wiki_change_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcc"] = module
    spec.loader.exec_module(module)
    return module


def _run(module, args=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = module.main(args)
    return result, buf.getvalue()


def test_excludes_generated(tmp_path, monkeypatch):
    w = tmp_path / "wiki"
    (w / "sources" / "x").mkdir(parents=True)
    (w / "entities").mkdir()
    (w / "entities" / "real-page.md").write_text("---\ntype: entity\n---\n# r\n")
    # generated/reserved churn — must be ignored
    for n in ("sources/INDEX.md", "sources/x/INDEX-p01.md", "_reserved.md", "entities/.hidden.md"):
        (w / n).write_text("# generated\n")
    m = _load(tmp_path, monkeypatch)
    _, out = _run(m)
    assert "**Pages modified since last lint:** 1" in out      # only real-page.md
    assert "real-page.md" in out
    assert "INDEX" not in out and "_reserved" not in out


def test_failed_or_unacknowledged_lint_retries_same_changes(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("# page\n")
    m = _load(tmp_path, monkeypatch)

    result, first = _run(m)
    assert result == 0
    state = json.loads(m.STATE_PATH.read_text())
    candidate = state["pending_baseline_mtime"]
    assert state["last_baseline_mtime"] == 0.0
    assert "--commit-baseline" in first

    # A failed agent never runs the acknowledgement command, so the next tick retries.
    _, retry = _run(m)
    assert "**Pages modified since last lint:** 1" in retry

    result, committed = _run(m, ["--commit-baseline", str(candidate)])
    assert result == 0
    assert "committed successful lint baseline" in committed
    state = json.loads(m.STATE_PATH.read_text())
    assert state["last_baseline_mtime"] == candidate
    assert "pending_baseline_mtime" not in state

    _, clean = _run(m)
    assert "**Pages modified since last lint:** 0" in clean
    assert '{"wakeAgent": false}' in clean


def test_baseline_commit_refuses_non_pending_candidate(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("# page\n")
    m = _load(tmp_path, monkeypatch)
    _run(m)

    result, _ = _run(m, ["--commit-baseline", "1"])
    assert result == 1
    state = json.loads(m.STATE_PATH.read_text())
    assert state["last_baseline_mtime"] == 0.0
