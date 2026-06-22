"""P3 regression: deploy-time N-way compose (discover packs -> validate -> merge)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron_pack_split.py"


def _load():
    spec = importlib.util.spec_from_file_location("cron_pack_split", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split"] = m
    spec.loader.exec_module(m)
    return m


def _mkpack(root: Path, name: str, owns: str, trust="public", domain=None, prompts=None):
    d = root / name
    (d / "crons").mkdir(parents=True)
    (d / "pack.yaml").write_text(f"name: {name}\nversion: 0.1.0\ntrust: {trust}\nowns: {owns}\n")
    (d / "crons" / "domain-crons.json").write_text(json.dumps(domain or []))
    (d / "crons" / "engine-template-prompts.json").write_text(json.dumps(prompts or {}))
    return d


def test_compose_two_disjoint_packs(tmp_path):
    cps = _load()
    packs = tmp_path / "packs"
    packs.mkdir()
    _mkpack(packs, "packA", "{types: [a], namespaces: [na]}",
            domain=[{"name": "digA", "schedule": "0 9 * * *"}])
    _mkpack(packs, "packB", "{types: [b], namespaces: [nb]}",
            domain=[{"name": "digB", "schedule": "0 10 * * *"}])
    jobs, errors = cps.compose(packs)
    assert errors == []
    names = {j["name"] for j in jobs}
    assert {"packA:digA", "packB:digB"} <= names      # pack-prefixed domain jobs
    assert "reshelve" in names                        # the real engine half composed in


def test_discover_skips_dirs_without_pack_yaml(tmp_path):
    cps = _load()
    packs = tmp_path / "packs"
    packs.mkdir()
    _mkpack(packs, "real", "{types: [a]}")
    (packs / "not-a-pack").mkdir()                     # no pack.yaml -> ignored
    found = {p["name"] for p in cps.discover_packs(packs)}
    assert found == {"real"}


def test_compose_overlap_is_fatal_and_writes_nothing(tmp_path):
    cps = _load()
    packs = tmp_path / "packs"
    packs.mkdir()
    _mkpack(packs, "packA", "{types: [shared]}")
    _mkpack(packs, "packB", "{types: [shared]}")
    jobs, errors = cps.compose(packs)
    assert any("owned by both" in e for e in errors)
    with pytest.raises(SystemExit):                   # regen refuses to write on errors
        cps.regen_composed(packs)
