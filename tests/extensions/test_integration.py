"""Cross-component integration tests for the extension system.

The bugs live verification caught (Dockerfile, validator root, schema_lib composed-
awareness) were all CROSS-component — they slipped past per-feature unit tests. These
exercise multiple components together: two extensions (in-gateway + sidecar) enabled at
once, cron + schema composition + staging + sidecar generation staying mutually
consistent, cross-extension conflicts, and the disable/teardown flow.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
PATHS = {n: REPO / "scripts" / f"{n}.py" for n in
         ("extension_discovery", "extension_compose", "extension_tokens", "framework_extensions")}
SL = REPO / "scripts" / "cron" / "schema_lib.py"

pytestmark = pytest.mark.skipif(not all(p.is_file() for p in PATHS.values()),
                                reason="extension modules absent")


@pytest.fixture(autouse=True)
def _isolate_engine_tier(monkeypatch, tmp_path):
    """Synthetic-extension integration tests — keep shipped engine extensions (the core
    okengine.contradictions, #142) out of the tier-1 scan so exact-set assertions hold."""
    empty = tmp_path / "_no_engine_exts"
    empty.mkdir()
    monkeypatch.setenv("OKENGINE_ENGINE_ROOT", str(empty))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _disc():
    return _load("extension_discovery", PATHS["extension_discovery"])


def _comp():
    return _load("extension_compose", PATHS["extension_compose"])


def _cli():
    return _load("framework_extensions", PATHS["framework_extensions"])


def _pack(tmp_path):
    pack = tmp_path / "pack"
    (pack / "wiki").mkdir(parents=True)
    (pack / "schema.yaml").write_text(yaml.safe_dump({
        "apply_under": ["wiki/"],
        "partitioning": {"namespaces": {"entities": {}}},
        "types": {"entity": {"required": ["type"]}},
    }), encoding="utf-8")
    return pack


def _ingateway_ext(pack, ext_id, namespace, type_name):
    d = pack / "extensions" / ext_id
    (d / "schema").mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump({
        "id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
        "requires": {"engine": ">=0.3.0"},
        "capabilities": {"read": ["wiki/**"], "write": [f"{namespace}/**"]},
        "schema": ["schema/frag.yaml"],
        "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                      "entrypoint": {"script": "run.py"}},
    }), encoding="utf-8")
    (d / "schema" / "frag.yaml").write_text(yaml.safe_dump({
        "owns": {"namespaces": [namespace], "types": {type_name: {"required": ["type", "id"]}}}}),
        encoding="utf-8")
    (d / "run.py").write_text("print('{}')\n", encoding="utf-8")
    return d


def _sidecar_ext(pack, ext_id, namespace):
    d = pack / "extensions" / ext_id
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump({
        "id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "sidecar",
        "requires": {"engine": ">=0.3.0"},
        "capabilities": {"read": ["wiki/**"], "write": [f"{namespace}/**"]},
        "operation": {"schedule": {"kind": "cron", "expr": "0 5 * * *"},
                      "entrypoint": {"image": {"registry": f"r/{ext_id}", "digest": "sha256:abc"}}},
    }), encoding="utf-8")
    return d


def _enable(cli, pack, ext_id):
    assert cli.main(["enable", str(pack), ext_id]) == 0


def test_two_extensions_compose_consistently(tmp_path):
    disc, comp, cli = _disc(), _comp(), _cli()
    pack = _pack(tmp_path)
    _ingateway_ext(pack, "demo.alpha", "alphas", "alpha")
    _sidecar_ext(pack, "demo.side", "sides")
    _enable(cli, pack, "demo.alpha")
    _enable(cli, pack, "demo.side")

    # cron fleet: both jobs, namespaced by id, no collision
    exts, derr = disc.discover(pack)
    assert derr == []
    jobs, jerr = comp.extension_jobs(pack, existing_names={"build-hot-set"})
    assert jerr == []
    names = {j["name"] for j in jobs}
    assert names == {"demo.alpha", "demo.side"}
    side_job = next(j for j in jobs if j["name"] == "demo.side")
    assert side_job["script"].endswith("demo.side/trigger.sh")     # sidecar -> trigger wrapper

    # schema: only the in-gateway one brings schema here; composed has its type
    assert comp.write_composed_schema(pack) == []
    composed = yaml.safe_load((pack / ".okengine" / "composed-schema.yaml").read_text())
    assert "alpha" in composed["types"]
    assert composed["owners"]["types"]["alpha"] == "ext:demo.alpha"

    # staging: only the in-gateway extension stages a script
    targets, terr = comp.staging_targets(pack)
    assert terr == [] and {t["id"] for t in targets} == {"demo.alpha"}

    # sidecar-generate: only the sidecar yields a service + token
    override, wrappers, oerr = comp.sidecar_compose_override(pack)
    assert oerr == []
    assert set(override["services"]) == {"demo.side-sidecar"}
    assert "demo.side" in wrappers


def test_cross_extension_type_conflict_blocks_second_enable(tmp_path):
    cli = _cli()
    pack = _pack(tmp_path)
    _ingateway_ext(pack, "demo.alpha", "shared", "widget")
    _ingateway_ext(pack, "demo.beta", "shared2", "widget")     # same TYPE id 'widget'
    _enable(cli, pack, "demo.alpha")
    # enabling beta must fail the schema dry-run (widget already owned by alpha)
    assert cli.main(["enable", str(pack), "demo.beta"]) == 1
    enabled, _ = _disc().load_enabled_state(pack)
    assert "demo.beta" not in enabled                          # no state change on conflict


def test_disable_one_keeps_the_other(tmp_path):
    disc, comp, cli = _disc(), _comp(), _cli()
    pack = _pack(tmp_path)
    _ingateway_ext(pack, "demo.alpha", "alphas", "alpha")
    _ingateway_ext(pack, "demo.gamma", "gammas", "gamma")
    _enable(cli, pack, "demo.alpha")
    _enable(cli, pack, "demo.gamma")
    assert cli.main(["disable", str(pack), "demo.alpha"]) == 0

    jobs, _ = comp.extension_jobs(pack)
    assert {j["name"] for j in jobs} == {"demo.gamma"}         # alpha's job dropped
    composed = yaml.safe_load((pack / ".okengine" / "composed-schema.yaml").read_text())
    assert "gamma" in composed["types"] and "alpha" not in composed["types"]
    # alpha's token revoked, gamma's kept
    import json
    store = json.loads((pack / ".okengine" / "extension-tokens.json").read_text())
    assert {r["ext_id"] for r in store["tokens"]} == {"demo.gamma"}


def test_full_teardown_to_no_artifacts(tmp_path):
    disc, comp, cli = _disc(), _comp(), _cli()
    pack = _pack(tmp_path)
    _ingateway_ext(pack, "demo.alpha", "alphas", "alpha")
    _enable(cli, pack, "demo.alpha")
    assert (pack / ".okengine" / "composed-schema.yaml").is_file()
    assert cli.main(["disable", str(pack), "demo.alpha"]) == 0
    # no extension brings schema -> composed artifact removed; no jobs; no tokens
    assert not (pack / ".okengine" / "composed-schema.yaml").exists()
    assert comp.extension_jobs(pack) == ([], [])
    import json
    store = json.loads((pack / ".okengine" / "extension-tokens.json").read_text())
    assert store.get("tokens") == []
