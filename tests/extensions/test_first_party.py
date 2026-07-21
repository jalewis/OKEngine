"""Guards for the shipped first-party extension okengine.contradictions (#131 §11).

The first slice was migrated out of the engine cron fleet into a tier-1 extension.
These assert it ships well-formed (discovers, validates, synthesizes a runnable job)
and that the migration is complete (no dangling engine-cron references).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
DISC_PATH = REPO / "scripts" / "extension_discovery.py"

pytestmark = pytest.mark.skipif(not DISC_PATH.is_file(),
                                reason="extension modules not present")


def _load(name):
    p = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_contradictions_extension_ships_valid():
    disc = _load("extension_discovery")
    em = _load("extension_manifest")
    comp = _load("extension_compose")

    exts, errors = disc.discover(None, engine_root=REPO)
    assert errors == [], errors
    by_id = {e["id"]: e for e in exts}
    assert "okengine.contradictions" in by_id
    rec = by_id["okengine.contradictions"]
    assert rec["tier"] == "engine"                     # okengine.* must be tier-1

    m_errors, _ = em.validate_manifest(rec["manifest"])
    assert m_errors == [], m_errors

    job, errs, _ = comp.synthesize_job(rec)
    assert errs == []
    assert job["name"] == "okengine.contradictions"
    assert job["schedule"]["expr"] == "0 4 * * *"
    assert job["script"] == \
        "/opt/data/scripts/okengine.contradictions/select_contradictions.py"
    assert job["no_agent"] is True


def test_every_first_party_manifest_is_warning_free():
    """First-party manifests define the reference contract; ignored fields are release failures."""
    disc = _load("extension_discovery")
    em = _load("extension_manifest")
    exts, errors = disc.discover(None, engine_root=REPO)
    assert errors == [], errors
    findings = {}
    for rec in exts:
        manifest_errors, warnings = em.validate_manifest(rec["manifest"])
        if manifest_errors or warnings:
            findings[rec["id"]] = {"errors": manifest_errors, "warnings": warnings}
    assert findings == {}


def test_contradictions_fully_migrated_out_of_engine_fleet():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    assert "contradictions-refresh" not in {j.get("name") for j in jobs}
    assert "select_contradictions.py" not in {j.get("script") for j in jobs}

    tiers = yaml.safe_load((REPO / "config" / "cron-tiers.yaml").read_text())
    flat = [n for t in ("engine", "engine-template", "domain") for n in (tiers.get(t) or [])]
    assert "contradictions-refresh" not in flat

    # the script moved from the engine cron dir into the extension dir
    assert not (REPO / "scripts" / "cron" / "select_contradictions.py").exists()
    assert (REPO / "extensions" / "okengine.contradictions"
            / "select_contradictions.py").exists()


def test_contradictions_script_is_self_contained():
    """An isolated extension script must not import sibling engine cron libs (it runs
    from its own /opt/data/scripts/<id>/ dir, not the flat engine scripts dir)."""
    src = (REPO / "extensions" / "okengine.contradictions"
           / "select_contradictions.py").read_text()
    import re
    imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", src, re.M)
    allowed = {"__future__", "json", "os", "re", "sys", "collections", "dataclasses",
               "datetime", "pathlib", "yaml", "typing", "itertools", "math", "functools"}
    foreign = [i for i in imports if i.split(".")[0] not in allowed]
    assert not foreign, f"extension script imports non-stdlib siblings: {foreign}"
