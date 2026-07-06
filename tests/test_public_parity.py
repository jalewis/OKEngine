"""check-public-parity.sh — the publish-pair consistency gate (GitHub deploy class).

Found live: the public catalog sat six releases behind the working repos with nothing
watching. Red/green over local fixture files (the env overrides are the test seam)."""
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SH = REPO / "scripts" / "check-public-parity.sh"
LOCAL_REL = [ln.split(":")[1].split("#")[0].strip()
             for ln in (REPO / "engine-manifest.yaml").read_text().splitlines()
             if ln.startswith("engine_release:")][0]


def _run(tmp_path, pub_rel, cat_vers):
    m = tmp_path / "pub-manifest.yaml"
    m.write_text(f"version: 1\nengine_release: {pub_rel}\n")
    c = tmp_path / "pub-catalog.json"
    c.write_text(json.dumps({"packs": [{"name": f"p{i}", "engine_version": v}
                                       for i, v in enumerate(cat_vers)]}))
    return subprocess.run(["bash", str(SH)], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin",
                               "PUBLIC_ENGINE_MANIFEST": str(m),
                               "PUBLIC_CATALOG": str(c)})


def test_consistent_and_current_passes(tmp_path):
    r = _run(tmp_path, LOCAL_REL, [LOCAL_REL, LOCAL_REL])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK: public snapshot pair" in r.stdout


def test_stale_public_engine_fails(tmp_path):
    r = _run(tmp_path, "v0.3.5", ["v0.3.5"])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STALE" in r.stdout


def test_catalog_engine_skew_fails(tmp_path):
    """The pair external deployers consume together must agree even when the engine
    snapshot is current — a lagging catalog alone still breaks their pin check."""
    r = _run(tmp_path, LOCAL_REL, ["v0.3.5"])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "SKEW" in r.stdout


def test_unfetchable_is_distinct_exit(tmp_path):
    r = subprocess.run(["bash", str(SH)], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin",
                            "PUBLIC_ENGINE_MANIFEST": str(tmp_path / "missing.yaml"),
                            "PUBLIC_CATALOG": str(tmp_path / "missing.json")})
    assert r.returncode == 2, r.stdout + r.stderr
