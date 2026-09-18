"""okengine#90 P1 — multi-pack compose-preview safety gate.

Compatible packs preview clean; ownership/trust/cron collisions BLOCK (non-zero), so the preview
can gate a deploy before composition is attempted.
"""
import json
import runpy
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
                  "source": {"required": ["type", "published", "reliability"]},
                  "concept": {"required": ["type"]}},   # core type without tightening
        "partitioning": {"namespaces": {"bs": {"strategy": "flat"}}}},
        sort_keys=False))
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


def test_pack_meta_handles_malformed_inputs_and_env_secrets(tmp_path):
    pack = tmp_path / "broken"
    pack.mkdir()
    (pack / "pack.yaml").write_text("[broken")
    (pack / "schema.yaml").write_text("[broken")
    (pack / "crons").mkdir()
    (pack / "crons" / "domain-crons.json").write_text("{broken")
    (pack / ".env.example").write_text(
        "# comment\nAPI_KEY=\n INVALID \nOTHER=value=with-equals\n")
    meta = cp._pack_meta(pack)
    assert meta["name"] == "broken"
    assert meta["trust"] == "?"
    assert meta["crons"] == []
    assert meta["secrets"] == ["API_KEY", "OTHER"]
    assert cp._load_yaml(pack / "missing.yaml") == {}


def test_schedule_contention_authority_overlap_and_secrets(tmp_path):
    job_a = [{"name": "a", "schedule": {"expr": "0 5 * * *"}}]
    job_b = [{"name": "b", "schedule": {"expr": "0 5 * * *"}},
             {"name": "", "schedule": {"expr": "@jitter 5m"}}]
    a = _mkpack(tmp_path / "a", "a", "public", ["alpha"], ["as"], job_a)
    b = _mkpack(tmp_path / "b", "b", "public", ["beta"], ["bs"], job_b)
    for path in (a, b):
        schema = yaml.safe_load((path / "schema.yaml").read_text())
        typename = next(iter(schema["types"]))
        schema["types"][typename]["id_authority"] = "shared"
        schema["types"][typename + "-second"] = {
            "required": ["type"], "id_authority": "shared-second"}
        (path / "schema.yaml").write_text(yaml.safe_dump(schema))
    schema = yaml.safe_load((a / "schema.yaml").read_text())
    schema["types"]["a-only"] = {"required": ["type"], "id_authority": "a-only"}
    (a / "schema.yaml").write_text(yaml.safe_dump(schema))
    (a / ".env.example").write_text("TOKEN=\n")
    (b / ".env.example").write_text("TOKEN=\nSECOND=\n")

    r = cp.analyze([str(a), str(b)])
    assert any("schedule" in w for w in r["warn"])
    assert any("ID-authority" in w for w in r["warn"])
    assert r["secrets"] == {"TOKEN": ["a", "b"], "SECOND": ["b"]}


def test_json_cli_safe_and_unsafe_and_too_few_packs(tmp_path, capsys):
    a = _mkpack(tmp_path / "a", "a", "public", ["alpha"], ["as"])
    b = _mkpack(tmp_path / "b", "b", "public", ["beta"], ["bs"])
    assert cp.main([str(a)]) == 2
    assert "needs >= 2" in capsys.readouterr().err

    assert cp.main([str(a), str(b), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hard"] == []

    (b / "pack.yaml").write_text(yaml.safe_dump({
        "name": "b", "trust": "private",
        "owns": {"types": ["beta"], "namespaces": ["bs"]},
    }))
    assert cp.main([str(a), str(b), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["trust"] == "MIXED"


def test_human_output_renders_warning_and_blocking_sections(tmp_path, capsys):
    job_a = [{"name": "a", "schedule": {"expr": "0 5 * * *"}}]
    job_b = [{"name": "b", "schedule": {"expr": "0 5 * * *"}}]
    a = _mkpack(tmp_path / "a", "a", "public", ["alpha"], ["as"], job_a)
    b = _mkpack(tmp_path / "b", "b", "private", ["beta"], ["bs"], job_b)
    assert cp.main([str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "human review" in out
    assert "BLOCKING conflicts" in out
    assert "UNSAFE" in out


def test_compose_preview_entrypoint(tmp_path, monkeypatch):
    pack = _mkpack(tmp_path / "a", "a", "public", ["alpha"], ["as"])
    monkeypatch.setattr(sys, "argv", [str(ROOT / "scripts/framework_compose_preview.py"),
                                     str(pack)])
    try:
        runpy.run_path(str(ROOT / "scripts/framework_compose_preview.py"), run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 2
