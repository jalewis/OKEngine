"""wiki_change_check: generated INDEX / reserved files must NOT count as wiki changes (they churn on
every index rebuild and overran the lint agent -> truncation)."""
import importlib.util, sys, io, contextlib, json
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent.parent


def test_excludes_generated(tmp_path, monkeypatch):
    w = tmp_path / "wiki"; (w / "sources" / "x").mkdir(parents=True); (w / "entities").mkdir()
    (w / "entities" / "real-page.md").write_text("---\ntype: entity\n---\n# r\n")
    # generated/reserved churn — must be ignored
    for n in ("sources/INDEX.md", "sources/x/INDEX-p01.md", "_reserved.md", "entities/.hidden.md"):
        (w / n).write_text("# generated\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path)); monkeypatch.setenv("HERMES_HOME", str(tmp_path / "data"))
    spec = importlib.util.spec_from_file_location("wcc", REPO / "scripts/cron/wiki_change_check.py")
    m = importlib.util.module_from_spec(spec); sys.modules["wcc"] = m; spec.loader.exec_module(m)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.main()
    out = buf.getvalue()
    assert "**Pages modified since last lint:** 1" in out      # only real-page.md
    assert "real-page.md" in out
    assert "INDEX" not in out and "_reserved" not in out
