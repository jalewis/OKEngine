"""Regression: ensure_readable restores the reader-needed read bits on pages a
writer (e.g. the Hermes file tool for the daily brief) left owner-only 0600."""
import importlib.util
import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "ensure_readable.py"


def _load():
    spec = importlib.util.spec_from_file_location("ensure_readable", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_restores_other_read_on_owner_only_page(tmp_path):
    m = _load()
    wiki = tmp_path / "wiki"
    (wiki / "briefings").mkdir(parents=True)
    (wiki / "entities" / "a").mkdir(parents=True)
    brief = wiki / "briefings" / "2026-06-22.md"
    brief.write_text("---\ntype: dashboard\n---\nbrief\n")
    os.chmod(brief, 0o600)                                 # file-tool default — reader can't read
    ok = wiki / "entities" / "a" / "acme.md"
    ok.write_text("---\ntype: entity\nname: Acme\n---\nx\n")
    os.chmod(ok, 0o644)                                    # already fine

    res = m.run(wiki)

    assert res["fixed"] == 1 and res["pages"] == ["briefings/2026-06-22.md"]
    assert (brief.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH)) == (stat.S_IRGRP | stat.S_IROTH)
    # additive only: the owner-write bit and the already-good page are untouched
    assert brief.stat().st_mode & stat.S_IWUSR
    assert (ok.stat().st_mode & 0o777) == 0o644

    # idempotent: a second run fixes nothing
    assert m.run(wiki)["fixed"] == 0
