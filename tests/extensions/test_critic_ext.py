"""okengine.critic — subjective QC over a pack flagship (okengine#157). Conditional wake-gate
(cost lever: wakes only on hard flags); drop-in model; derived report, no own type."""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.critic"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
SELECTOR = EXT / "select_critic.py"
pytestmark = pytest.mark.skipif(not EXT.is_dir(), reason="okengine.critic absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m


def _manifest():
    return yaml.safe_load((EXT / "extension.yaml").read_text())


def test_manifest_valid_dropin_no_schema():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.critic" and mod.is_reserved_id(m["id"])
    assert "operation" not in m and "operations" not in m and "schema" not in m
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


def test_dropin_composes_one_agent_lane():
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(
        {"id": "okengine.critic", "tier": "engine", "dir": str(EXT), "manifest": _manifest()})
    assert not errors, errors
    assert [j["name"] for j in jobs] == ["okengine.critic:flagship"]
    assert jobs[0]["no_agent"] is False and jobs[0]["prompt"].strip()


def _run(vault: Path):
    return subprocess.run([sys.executable, str(SELECTOR)], capture_output=True, text=True,
                          env={**os.environ, "WIKI_PATH": str(vault),
                               "OKENGINE_MCP_WRITE_DATE": "2026-06-28"}).stdout


def _brief(vault: Path, body: str, updated="2026-06-28"):
    p = vault / "wiki" / "briefings" / "weekly.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: report\nupdated: {updated}\n---\n# Weekly\n{body}\n")


def test_gate_wakes_on_hard_flags(tmp_path):
    (tmp_path / "schema.yaml").write_text("critic_flagship: 'briefings/**'\n")
    _brief(tmp_path, "Short.")                       # thin + under-cited
    assert '"wakeAgent": true' in _run(tmp_path)


def test_gate_silent_on_healthy_flagship(tmp_path):
    (tmp_path / "schema.yaml").write_text("critic_flagship: 'briefings/**'\n")
    _brief(tmp_path, "\n".join(f"Para {i} with evidence [[sources/s{i%4}]]." for i in range(60)))
    assert '"wakeAgent": false' in _run(tmp_path)     # cost lever: no hard flags -> silent


def test_gate_noop_without_flagship_config(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n")  # no critic_flagship
    assert '"wakeAgent": false' in _run(tmp_path)


def test_selector_self_contained():
    imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", SELECTOR.read_text(), re.M)
    allowed = {"__future__", "json", "os", "re", "datetime", "pathlib", "yaml", "typing", "collections"}
    assert not [i for i in imports if i.split(".")[0] not in allowed]


def test_gate_ignores_generated_index_pages(tmp_path):
    """A generated briefings/INDEX.md is not an authored deliverable — it must not trip the gate
    (the false positive found rolling to sec)."""
    (tmp_path / "schema.yaml").write_text("critic_flagship: 'briefings/**'\n")
    b = tmp_path / "wiki" / "briefings"
    b.mkdir(parents=True)
    (b / "INDEX.md").write_text("---\ntype: dashboard\n---\n# Index\nthin\n")   # generated + thin
    assert '"wakeAgent": false' in _run(tmp_path)


def test_helpers_cover_schema_frontmatter_and_generated_names(tmp_path, monkeypatch):
    mod = _load("select_critic_helpers", SELECTOR)
    assert mod._is_generated("index.md")
    assert mod._is_generated("INDEX-weekly.md")
    assert mod._is_generated("index-weekly.md")
    assert not mod._is_generated("weekly.md")
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    assert mod._schema() == {}
    composed = tmp_path / ".okengine" / "composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text("- not-a-mapping\n")
    (tmp_path / "schema.yaml").write_text("critic_flagship: reports/*.md\n")
    assert mod._schema()["critic_flagship"] == "reports/*.md"
    composed.write_text("[unterminated\n")
    assert mod._schema()["critic_flagship"] == "reports/*.md"
    assert mod._split("plain body") == ({}, "plain body")
    fm, body = mod._split("---\n- list\n---\nbody\n")
    assert fm == {} and body == "body\n"
    fm, body = mod._split("---\nupdated: 2026-01-01\n---\nbody\n")
    assert str(fm["updated"]) == "2026-01-01" and body == "body\n"
    fm, _ = mod._split("---\n[bad\n---\nbody\n")
    assert fm == {}


def test_flags_cover_read_date_and_threshold_edges(tmp_path, monkeypatch):
    mod = _load("select_critic_flags", SELECTOR)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-28")
    monkeypatch.setattr(mod, "STALE_DAYS", 14)
    monkeypatch.setattr(mod, "MIN_WORDS", 3)
    monkeypatch.setattr(mod, "MIN_SOURCES", 1)
    assert mod._today().isoformat() == "2026-06-28"
    assert mod._flags(tmp_path / "missing.md") == []
    invalid = tmp_path / "invalid.md"
    invalid.write_text("---\nupdated: nonsense-2026-99-99\n---\none two three [[sources/a]]\n")
    assert mod._flags(invalid) == []
    recent = tmp_path / "recent.md"
    recent.write_text("---\nupdated: 2026-06-28\n---\none two three [[sources/a]]\n")
    assert mod._flags(recent) == []
    stale = tmp_path / "stale.md"
    stale.write_text("---\ncreated: 2026-01-01\n---\none\n")
    assert mod._flags(stale) == ["stale (178d > 14d)", "thin (1 words < 3)",
                                 "under-cited (0 sources < 1)"]


def test_main_filters_targets_and_reports_flagged_and_clean(tmp_path, monkeypatch, capsys):
    mod = _load("select_critic_main", SELECTOR)
    wiki = tmp_path / "wiki"
    reports = wiki / "reports"
    reports.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("critic_flagship: reports/**\n")
    (reports / "good.md").write_text("one two three [[sources/a]]")
    (reports / "bad.md").write_text("one")
    (reports / "note.txt").write_text("one")
    (reports / "_hidden.md").write_text("one")
    (reports / "INDEX.md").write_text("one")
    (reports / "folder").mkdir()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", wiki)
    monkeypatch.setattr(mod, "MIN_WORDS", 3)
    monkeypatch.setattr(mod, "MIN_SOURCES", 1)
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "matched: 2 page(s)" in out
    assert "1 flagged flagship page(s)" in out
    assert '"wakeAgent": true' in out
    (reports / "bad.md").write_text("one two three [[sources/a]]")
    (tmp_path / "schema.yaml").write_text("critic_flagship: reports/*.md\n")
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "SKIP: no hard flags" in out
    assert '"wakeAgent": false' in out


def test_main_noops_when_vault_missing(tmp_path, monkeypatch, capsys):
    mod = _load("select_critic_no_vault", SELECTOR)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "missing")
    (tmp_path / "schema.yaml").write_text("critic_flagship: reports/*.md\n")
    assert mod.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out
