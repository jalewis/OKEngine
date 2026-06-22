"""P3 regression: pack metadata loading + composition validation (disjoint
ownership, requires, single trust)."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "pack_meta.py"


def _load():
    spec = importlib.util.spec_from_file_location("pack_meta", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["pack_meta"] = m
    spec.loader.exec_module(m)
    return m


pm = _load()


def _pack(tmp: Path, name: str, yaml_text: str) -> Path:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(yaml_text)
    return d


def test_load_normalizes(tmp_path):
    d = _pack(tmp_path, "okpack-attack",
              "name: okpack-attack\nversion: 0.1.0\ntrust: public\n"
              "owns: {types: [attack-pattern], namespaces: [attack-pattern]}\n"
              "requires: [okpack-base, okpack-foo@>=0.2.0]\n")
    m = pm.load_pack_meta(d)
    assert m["name"] == "okpack-attack" and m["trust"] == "public"
    assert m["owns_types"] == {"attack-pattern"}
    assert m["requires"] == ["okpack-base", "okpack-foo@>=0.2.0"]
    assert m["port_offset"] == 0                      # absent -> default 0
    assert pm.load_pack_meta(tmp_path / "nope") is None


def test_port_offset_parsed_and_coerced(tmp_path):
    base = "name: p\nversion: 1.0.0\ntrust: public\nowns: {types: [t]}\n"
    assert pm.load_pack_meta(_pack(tmp_path, "a", base + "port_offset: 100\n"))["port_offset"] == 100
    assert pm.load_pack_meta(_pack(tmp_path, "b", base + "port_offset: -5\n"))["port_offset"] == 0   # clamped
    assert pm.load_pack_meta(_pack(tmp_path, "c", base + "port_offset: nope\n"))["port_offset"] == 0  # bad -> 0


def _meta(name, version="1.0.0", trust="public", types=(), namespaces=(), requires=()):
    return {"name": name, "version": version, "trust": trust,
            "owns_types": set(types), "owns_namespaces": set(namespaces),
            "requires": list(requires), "dir": name}


def test_disjoint_packs_validate_clean():
    metas = [_meta("a", types=["t1"], namespaces=["n1"]),
             _meta("b", types=["t2"], namespaces=["n2"])]
    assert pm.validate_composition(metas) == []


def test_overlapping_type_and_namespace_ownership_fail():
    metas = [_meta("a", types=["shared"], namespaces=["nsx"]),
             _meta("b", types=["shared"], namespaces=["nsx"])]
    errs = " ".join(pm.validate_composition(metas))
    assert "type 'shared' is owned by both" in errs
    assert "namespace 'nsx' is owned by both" in errs


def test_requires_presence_and_version():
    # missing dependency
    assert any("requires 'okpack-base'" in e
               for e in pm.validate_composition([_meta("a", requires=["okpack-base"])]))
    # present + version satisfied
    metas = [_meta("a", requires=["base@>=0.2.0"]), _meta("base", version="0.3.0")]
    assert pm.validate_composition(metas) == []
    # present but version too low
    metas = [_meta("a", requires=["base@>=0.5.0"]), _meta("base", version="0.3.0")]
    assert any("requires base@>=0.5.0" in e for e in pm.validate_composition(metas))
    # caret: same-major floor
    assert pm._satisfies("1.4.0", "^1.2.0") and not pm._satisfies("2.0.0", "^1.2.0")


def test_mixed_trust_fails():
    metas = [_meta("a", trust="public"), _meta("b", trust="private")]
    assert any("mixed trust levels" in e for e in pm.validate_composition(metas))
