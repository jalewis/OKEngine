"""okengine#90 P1 — multi-pack compose-preview safety gate.

Compatible packs preview clean; ownership/trust/cron collisions BLOCK (non-zero), so the preview
can gate a deploy before composition is attempted.
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import framework_compose_preview as cp  # noqa: E402


def _mkpack(d: Path, name, trust, types, namespaces, crons=None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(yaml.safe_dump(
        {"name": name, "trust": trust, "owns": {"types": list(types), "namespaces": list(namespaces)}}))
    (d / "schema.yaml").write_text(yaml.safe_dump({
        "types": {t: {"required": ["type"]} for t in types},
        "partitioning": {"namespaces": {n: {"strategy": "flat"} for n in namespaces}}}))
    if crons is not None:
        (d / "crons").mkdir(exist_ok=True)
        (d / "crons" / "domain-crons.json").write_text(json.dumps(crons))
    return d


def test_compatible_packs_safe(tmp_path):
    a = _mkpack(tmp_path / "a", "okpack-a", "public", ["alpha"], ["as"])
    b = _mkpack(tmp_path / "b", "okpack-b", "public", ["beta"], ["bs"])
    r = cp.analyze([str(a), str(b)])
    assert not r["hard"], r["hard"]
    assert "alpha" in r["merged_types"] and "beta" in r["merged_types"]
    assert cp.main([str(a), str(b)]) == 0


def test_type_ownership_conflict_blocks(tmp_path):
    a = _mkpack(tmp_path / "a", "okpack-a", "public", ["shared"], ["as"])
    b = _mkpack(tmp_path / "b", "okpack-b", "public", ["shared"], ["bs"])
    r = cp.analyze([str(a), str(b)])
    assert any("SCHEMA" in h and "shared" in h for h in r["hard"]), r["hard"]
    assert cp.main([str(a), str(b)]) == 1


def test_tightening_a_core_type_blocks(tmp_path):
    # a pack that re-declares a core type (`source`) with an EXTRA required field is flagged —
    # it would reject another pack's source pages under composition (okengine#90 P2).
    a = _mkpack(tmp_path / "a", "okpack-a", "public", ["alpha"], ["as"])
    b = tmp_path / "b"
    b.mkdir()
    (b / "pack.yaml").write_text(yaml.safe_dump(
        {"name": "okpack-b", "trust": "public", "owns": {"types": ["beta"], "namespaces": ["bs"]}}))
    (b / "schema.yaml").write_text(yaml.safe_dump({
        "types": {"beta": {"required": ["type"]},
                  "source": {"required": ["type", "published", "reliability"]}},   # tightens core source
        "partitioning": {"namespaces": {"bs": {"strategy": "flat"}}}}))
    r = cp.analyze([str(a), str(b)])
    assert any("TIGHTEN" in h and "source" in h for h in r["hard"]), r["hard"]


def test_trust_mismatch_blocks(tmp_path):
    a = _mkpack(tmp_path / "a", "okpack-a", "public", ["alpha"], ["as"])
    b = _mkpack(tmp_path / "b", "okpack-b", "private", ["beta"], ["bs"])
    r = cp.analyze([str(a), str(b)])
    assert any("TRUST" in h for h in r["hard"])


def test_cron_name_collision_blocks(tmp_path):
    job = [{"name": "dup-job", "schedule": {"kind": "cron", "expr": "0 5 * * 1"}}]
    a = _mkpack(tmp_path / "a", "okpack-a", "public", ["alpha"], ["as"], crons=job)
    b = _mkpack(tmp_path / "b", "okpack-b", "public", ["beta"], ["bs"], crons=job)
    r = cp.analyze([str(a), str(b)])
    assert any("name collision" in h for h in r["hard"])
