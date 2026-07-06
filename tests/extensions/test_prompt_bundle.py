"""Extension-bundled prompts, pack-overridable (step 3).

An extension ships generic default prompts as bundled files (``prompt_file``), and a
deployment overrides them by job name via ``<pack>/.okengine/extension-prompts.json`` —
the engine-template pattern for extensions. So okengine.predictions can ship generic
grading prompts while the ai-research pack keeps its tuned ones.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _ext_dir(tmp_path, ext_id, manifest, files=None):
    d = tmp_path / "extensions" / ext_id
    d.mkdir(parents=True, exist_ok=True)
    import yaml
    (d / "extension.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    for rel, content in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(content, encoding="utf-8")
    return d


def _manifest(ext_id, op):
    return {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
            "requires": {"engine": ">=0.4.0"},
            "capabilities": {"read": ["wiki/**"], "write": ["predictions/**"]},
            "operation": op}


# --- bundled prompt_file ---------------------------------------------------

def test_prompt_file_is_loaded_as_the_agent_prompt(tmp_path):
    c = _load("extension_compose", COMPOSE)
    d = _ext_dir(tmp_path, "okengine.predictions",
                 _manifest("okengine.predictions",
                           {"schedule": {"kind": "cron", "expr": "23 6 * * *"},
                            "entrypoint": "gate.py", "prompt_file": "prompts/grade.md"}),
                 files={"prompts/grade.md": "Generic grading instructions.\n"})
    rec = {"id": "okengine.predictions", "tier": "engine", "dir": str(d),
           "manifest": _manifest("okengine.predictions",
                                 {"schedule": {"kind": "cron", "expr": "23 6 * * *"},
                                  "entrypoint": "gate.py", "prompt_file": "prompts/grade.md"})}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    assert jobs[0]["no_agent"] is False
    assert jobs[0]["prompt"] == "Generic grading instructions.\n"


def test_missing_prompt_file_is_an_error(tmp_path):
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "x.y", "tier": "engine", "dir": str(tmp_path),
           "manifest": _manifest("x.y", {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                                         "prompt_file": "nope.md"})}
    _, errors, _ = c.synthesize_ops(rec)
    assert any("prompt_file not found" in e for e in errors), errors


def test_inline_prompt_beats_default_path():
    # inline prompt wins when both present? validator forbids both; composer prefers inline.
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "x.y", "tier": "engine", "dir": "/nonexistent",
           "manifest": _manifest("x.y", {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                                         "prompt": "inline wins"})}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors and jobs[0]["prompt"] == "inline wins"


# --- pack override via .okengine/extension-prompts.json --------------------

def test_pack_override_replaces_bundled_prompt(tmp_path):
    c = _load("extension_compose", COMPOSE)
    disc = _load("extension_discovery", REPO / "scripts" / "extension_discovery.py")
    pack = tmp_path
    man = _manifest("demo.predictions",
                    {"schedule": {"kind": "cron", "expr": "23 6 * * *"},
                     "entrypoint": "gate.py", "prompt_file": "prompts/grade.md"})
    _ext_dir(pack, "demo.predictions", man, files={"prompts/grade.md": "GENERIC default."})
    disc.set_enabled(pack, "demo.predictions", True)
    # pack supplies a tuned prompt keyed by the namespaced job name
    okd = pack / ".okengine"
    okd.mkdir(parents=True, exist_ok=True)
    (okd / "extension-prompts.json").write_text(
        json.dumps({"demo.predictions": "TUNED ai-research grading."}), encoding="utf-8")
    jobs, errors = c.extension_jobs(pack)
    assert not errors, errors
    assert jobs[0]["prompt"] == "TUNED ai-research grading."     # override won


def test_pack_override_unknown_job_is_an_error(tmp_path):
    c = _load("extension_compose", COMPOSE)
    disc = _load("extension_discovery", REPO / "scripts" / "extension_discovery.py")
    pack = tmp_path
    man = _manifest("demo.predictions",
                    {"schedule": {"kind": "cron", "expr": "23 6 * * *"}, "entrypoint": "gate.py"})
    _ext_dir(pack, "demo.predictions", man)
    (pack / "extensions" / "demo.predictions" / "gate.py").write_text("print('{}')\n")
    disc.set_enabled(pack, "demo.predictions", True)
    okd = pack / ".okengine"; okd.mkdir(parents=True, exist_ok=True)
    (okd / "extension-prompts.json").write_text(
        json.dumps({"demo.predictions:ghost": "x"}), encoding="utf-8")
    jobs, errors = c.extension_jobs(pack)
    assert any("no extension job named" in e for e in errors), errors


# --- manifest validation ----------------------------------------------------

def test_manifest_rejects_both_prompt_and_prompt_file():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest("x.y", {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                          "prompt": "a", "prompt_file": "b.md"})
    errors, _ = mod.validate_manifest(m)
    assert any("either 'prompt' or 'prompt_file'" in e for e in errors), errors


def test_manifest_accepts_prompt_file_without_entrypoint():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest("okengine.brief", {"schedule": {"kind": "cron", "expr": "0 8 * * *"},
                                     "prompt_file": "prompts/brief.md"})
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors
