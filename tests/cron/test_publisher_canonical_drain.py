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


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("publisher: CrowdStrike\n", "CrowdStrike"),
        ("publisher: Acme Research # normalized later\n", "Acme Research"),
        ('publisher: "Quoted Publisher"\n', "Quoted Publisher"),
        ("publisher: ''\n", None),
        ("title: no publisher\n", None),
    ],
)
def test_extract_publisher_handles_yaml_scalar_forms(tmp_path, monkeypatch, line, expected):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    mod = _load(f"select_publisher_extract_{abs(hash(line))}", "scripts/cron/select_publisher_canonical_drain.py")
    assert mod.extract_publisher(f"---\n{line}---\n") == expected


def test_scan_and_main_classify_candidates_variants_flags_and_threshold(tmp_path, monkeypatch):
    sources = tmp_path / "wiki" / "sources" / "2026" / "07"
    sources.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text(
        "**Canonical names**\n\n`Cisco Talos`, `Microsoft`\n", encoding="utf-8")
    publishers = ["Cisco Talos", "Cisco-Talos", "New Research", "Unknown", "One Off"]
    for i, publisher in enumerate(publishers):
        (sources / f"{i}.md").write_text(f"---\npublisher: {publisher}\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("PCD_MIN_SOURCES", "1")
    mod = _load("select_publisher_classify", "scripts/cron/select_publisher_canonical_drain.py")

    counts = mod.scan_publishers()
    assert counts["New Research"] == 1
    assert mod.looks_like_drift_variant("Cisco-Talos", {"Cisco Talos"}) == "Cisco Talos"
    assert mod.looks_like_drift_variant("Entirely New", {"Cisco Talos"}) is None
    rc, out, wake = _run(mod)

    assert rc == 0 and wake is True
    assert "`New Research` (1 sources)" in out
    assert "`Cisco-Talos` (1 sources) → likely canonical: `Cisco Talos`" in out
    assert "`Unknown` (1 sources)" in out
    assert "`One Off` (1 sources)" in out


def test_read_failures_missing_source_tree_substring_variant_and_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    mod = _load("select_publisher_edges", "scripts/cron/select_publisher_canonical_drain.py")
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        (_ for _ in ()).throw(OSError("missing"))
                        if path == mod._CLAUDE_MD else original(path, *args, **kwargs))
    assert mod.load_canonical_list() == set()
    assert mod.scan_publishers() == {}
    assert mod.looks_like_drift_variant("Acme Research", {"Acme / Acme Blog"}) == "Acme / Acme Blog"
    assert mod.looks_like_drift_variant("Acme", {"Acme Corporation International Holdings"}) is None

    sources = tmp_path / "wiki/sources"; sources.mkdir(parents=True)
    bad = sources / "bad.md"; bad.write_text("publisher: Bad")
    (sources / "no-publisher.md").write_text("title: none\n")
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        (_ for _ in ()).throw(OSError("race"))
                        if path == bad else original(path, *args, **kwargs))
    assert mod.scan_publishers() == {}

    monkeypatch.setattr(mod, "load_canonical_list", lambda: {"Canonical"})
    monkeypatch.setattr(mod, "scan_publishers", lambda: __import__("collections").Counter({"Rare": 1}))
    monkeypatch.setattr(mod, "MIN_SOURCES", 2)
    rc, _, wake = _run(mod)
    assert rc == 0 and wake is False
