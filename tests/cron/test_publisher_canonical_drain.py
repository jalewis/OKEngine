"""publisher-canonical-drain wake-gate must SKIP cleanly when no canonical list is configured.

Regression: a vault whose CLAUDE.md has no `**Canonical names**` block (e.g. a persona with no
publisher taxonomy — okcti) made the wake-gate `exit 1` with an ERROR every run. That reads as a
fleet failure and feeds a spurious `## Script Error` into the agent. A missing OPTIONAL list is a
clean skip, not a failure.
"""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _run(mod):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    out = buf.getvalue()
    return rc, out, json.loads(out.strip().splitlines()[-1])["wakeAgent"]


def test_skips_cleanly_without_canonical_block(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    # a persona CLAUDE.md that never declares a **Canonical names** block
    (tmp_path / "CLAUDE.md").write_text("# Persona\n\nNo publisher taxonomy here.\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    mod = _load("select_publisher_canonical_drain", "scripts/cron/select_publisher_canonical_drain.py")
    rc, out, wake = _run(mod)
    assert rc == 0                       # clean exit, NOT 1
    assert wake is False                 # nothing to drain -> no wake
    assert "SKIP" in out and "ERROR" not in out


def test_parses_canonical_block_when_present(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text(
        "# Persona\n\n**Canonical names**\n\n`Microsoft`, `Cisco Talos`, `Mandiant`\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    mod = _load("select_publisher_canonical_drain", "scripts/cron/select_publisher_canonical_drain.py")
    assert mod.load_canonical_list() == {"Microsoft", "Cisco Talos", "Mandiant"}
    rc, out, wake = _run(mod)
    assert rc == 0 and "canonical entries: 3" in out and wake is False   # no source publishers -> no candidates
