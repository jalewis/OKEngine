"""invariant-audit #44 — framework_init must never stamp a rotting hardcoded pin/version.

engine_meta._load() returns {} (no exception) when PyYAML is missing, which used to drop
hermes_pin()/engine_version() to a HARDCODED literal (v2026.6.19 / v0.3.3) that went stale across
Hermes/engine bumps. The fallback now parses engine-manifest.yaml directly (yaml-free), so a
scaffold on a yaml-less python still stamps the REAL pin.
"""
import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "scripts" / "framework_init.py"
MANIFEST = REPO / "engine-manifest.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("framework_init", INIT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _manifest_value(key):
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"\s*{re.escape(key)}:\s*(\S+)", line)
        if m:
            return m.group(1).strip()
    return None


def test_hermes_pin_matches_manifest_not_a_literal():
    m = _load()
    assert m.hermes_pin() == _manifest_value("pinned_tag")


def test_engine_version_matches_manifest():
    m = _load()
    assert m.engine_version() == _manifest_value("engine_release")


def test_manifest_scalar_reads_without_yaml():
    """The yaml-free fallback used when PyYAML is unavailable reads the real manifest values."""
    m = _load()
    assert m._manifest_scalar("pinned_tag") == _manifest_value("pinned_tag")
    assert m._manifest_scalar("engine_release") == _manifest_value("engine_release")


def test_no_stale_hardcoded_pin_literals_remain():
    """The rotting literals (v2026.6.19 / v0.3.3) must be gone from the source — no hidden fallback."""
    src = INIT.read_text(encoding="utf-8")
    assert "v2026.6.19" not in src, "stale hardcoded Hermes pin literal still present"
    assert "v0.3.3" not in src, "stale hardcoded engine version literal still present"
