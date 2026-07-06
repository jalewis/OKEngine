"""okengine.relevance-gate: err-toward-keep scope filtering — flag-not-delete, no-scope no-op,
prescore only flags CLEAR out, classify defers on uncertain (okengine#167)."""
import importlib.util
import io
import contextlib
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.relevance-gate"
COMPOSE = REPO / "scripts" / "extension_compose.py"


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or EXT / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def _vault(tmp, scope=True):
    (tmp / "wiki" / "sources" / "2026" / "07").mkdir(parents=True)
    (tmp / "wiki" / "dashboards").mkdir(parents=True)
    if scope:
        (tmp / "schema.yaml").write_text(yaml.safe_dump({"pack_config": {"scope": {
            "statement": "cyber-security market strategy",
            "in_scope": ["cyber-security vendors", "ransomware breach threat"],
            "out_of_scope": ["generic gardening", "cooking recipes baking"],
            "on_uncertain": "keep"}}}))
    else:
        (tmp / "schema.yaml").write_text("types: {}\n")


def _src(tmp, slug, title, created="2099-01-01"):
    p = tmp / "wiki" / "sources" / "2026" / "07" / f"{slug}.md"
    p.write_text(f"---\ntype: source\ntitle: \"{title}\"\ncreated: {created}\n---\n# s\nbody\n")
    return p


def _run(mod, tmp, monkeypatch, **env):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _load(mod).main()
    return buf.getvalue()


def test_composes_into_two_no_agent_jobs():
    c = _load("extension_compose", COMPOSE)
    manifest = yaml.safe_load((EXT / "extension.yaml").read_text())
    jobs, errors, _ = c.synthesize_ops(
        {"id": "okengine.relevance-gate", "tier": "engine", "dir": str(EXT), "manifest": manifest})
    assert not errors, errors
    assert sorted(j["name"] for j in jobs) == [
        "okengine.relevance-gate:scope-classify", "okengine.relevance-gate:scope-prescore"]
    assert all(j["no_agent"] for j in jobs)


def test_no_scope_declared_noops_loudly(tmp_path, monkeypatch):
    _vault(tmp_path, scope=False)
    _src(tmp_path, "x", "anything")
    out = _run("scope_prescore", tmp_path, monkeypatch)
    assert "will not invent a boundary" in out.lower() or "no-op" in out.lower()
    assert "off_scope" not in _src(tmp_path, "x2", "y").read_text()   # nothing got flagged


def test_prescore_flags_clear_out_keeps_in_and_ambiguous(tmp_path, monkeypatch):
    _vault(tmp_path)
    out_p = _src(tmp_path, "sourdough-tips", "Cooking recipes for sourdough baking")
    in_p = _src(tmp_path, "acme-breach", "Ransomware breach at a cyber-security vendor")
    both = _src(tmp_path, "baking-vendors", "baking industry cyber-security vendors")  # in wins
    amb = _src(tmp_path, "quarterly-note", "A quarterly note about weather")
    _run("scope_prescore", tmp_path, monkeypatch)
    assert "off_scope: true" in out_p.read_text()          # clear out -> flagged
    assert "off_scope" not in in_p.read_text()             # in -> untouched
    assert "off_scope" not in both.read_text()             # both sides -> in wins (err-keep)
    assert "off_scope" not in amb.read_text()              # ambiguous -> left for classify
    dash = (tmp_path / "wiki" / "dashboards" / "scope-audit.md").read_text()
    assert "sourdough-tips" in dash and "quarterly-note" in dash
    q = json.loads((tmp_path / "wiki" / ".scope-queue.json").read_text())
    assert any("quarterly-note" in r for r in q["ambiguous"])   # the classify handoff queue


def test_classify_needs_llm_env_else_noop(tmp_path, monkeypatch):
    _vault(tmp_path)
    _src(tmp_path, "quarterly-note", "A quarterly note about weather")
    (tmp_path / "wiki" / ".scope-queue.json").write_text(json.dumps(
        {"ambiguous": ["sources/2026/07/quarterly-note"]}))
    monkeypatch.delenv("OKENGINE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OKENGINE_LLM_MODEL", raising=False)
    out = _run("scope_classify", tmp_path, monkeypatch)
    assert "no-op" in out.lower()
    assert "off_scope" not in _src(tmp_path, "q2", "y").read_text()


def test_classify_flags_out_keeps_uncertain(tmp_path, monkeypatch):
    _vault(tmp_path)
    outp = _src(tmp_path, "weather-note", "A note about weather patterns")
    unc = _src(tmp_path, "mystery-item", "An item of unclear nature")
    (tmp_path / "wiki" / ".scope-queue.json").write_text(json.dumps(
        {"ambiguous": ["sources/2026/07/weather-note", "sources/2026/07/mystery-item"]}))
    monkeypatch.setenv("OKENGINE_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("OKENGINE_LLM_MODEL", "m")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))   # BEFORE load — VAULT binds at import
    sc = _load("scope_classify")
    verdicts = {"weather-note": "out-of-scope", "mystery-item": "uncertain"}
    def fake_classify(prompt, labels, **kw):
        for slug, v in verdicts.items():
            if slug in prompt:
                return v
        return "uncertain"
    monkeypatch.setattr(sc.llm_lib, "classify", fake_classify)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sc.main()
    assert "off_scope: true" in outp.read_text()           # model out -> script flags
    assert "off_scope" not in unc.read_text()              # uncertain -> KEPT
    dash = (tmp_path / "wiki" / "dashboards" / "scope-classify.md").read_text()
    assert "mystery-item" in dash                           # ...and surfaced for review


def test_flag_is_reversible_marker_and_idempotent(tmp_path, monkeypatch):
    _vault(tmp_path)
    p = _src(tmp_path, "sourdough-tips", "Cooking recipes for sourdough baking")
    _run("scope_prescore", tmp_path, monkeypatch)
    once = p.read_text()
    _run("scope_prescore", tmp_path, monkeypatch)          # second pass: no double-flag
    assert p.read_text() == once
    assert once.count("off_scope: true") == 1
