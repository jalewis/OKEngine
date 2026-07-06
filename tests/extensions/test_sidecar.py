"""Regression tests for the sidecar contract (okengine#135).

Covers the deterministic, testable core: manifest image-entrypoint validation, the
image ref, sidecar spec extraction, the compose-service + trigger-wrapper render, and
the token-injected override generation. Live container execution (docker-socket
trigger) needs a real sidecar image + operator opt-in and is out of unit scope.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
COMP_PATH = REPO / "scripts" / "extension_compose.py"
MAN_PATH = REPO / "scripts" / "extension_manifest.py"
DISC_PATH = REPO / "scripts" / "extension_discovery.py"
TOK_PATH = REPO / "scripts" / "extension_tokens.py"

pytestmark = pytest.mark.skipif(not COMP_PATH.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _comp():
    return _load("extension_compose", COMP_PATH)


def _man():
    return _load("extension_manifest", MAN_PATH)


def _valid_sidecar(ext_id="demo.sc", **over):
    man = {"id": ext_id, "kind": "operation", "version": "0.1.0", "name": ext_id,
           "trust": "sidecar", "requires": {"engine": ">=0.3.0"},
           "capabilities": {"read": ["wiki/**"], "write": ["dashboards/**"]},
           "operation": {"schedule": {"kind": "cron", "expr": "17 5 * * *"},
                         "entrypoint": {"image": {"registry": "reg.example.com/demo.sc",
                                                  "tag": "0.1.0", "digest": "sha256:deadbeef"}},
                         "timeout": 1800}}
    man.update(over)
    return man


# --- manifest validation --------------------------------------------------

def test_sidecar_requires_digest_pinned_image():
    m = _man()
    assert m.validate_manifest(_valid_sidecar())[0] == []
    # tag-only (no digest) -> FAIL
    bad = _valid_sidecar()
    bad["operation"]["entrypoint"] = {"image": {"registry": "r", "tag": "1.0"}}
    assert any("digest" in e for e in m.validate_manifest(bad)[0])
    # sidecar with a script entrypoint -> FAIL
    scr = _valid_sidecar()
    scr["operation"]["entrypoint"] = {"script": "run.py"}
    assert any("image" in e for e in m.validate_manifest(scr)[0])


def test_in_gateway_with_image_is_error():
    m = _man()
    man = _valid_sidecar()
    man["trust"] = "in-gateway"
    assert any("image" in e for e in m.validate_manifest(man)[0])


def test_script_and_image_both_is_error():
    m = _man()
    man = _valid_sidecar()
    man["operation"]["entrypoint"] = {"script": "run.py", "image": {"digest": "sha256:x"}}
    assert any("exactly one" in e for e in m.validate_manifest(man)[0])


# --- image ref + render ---------------------------------------------------

def test_image_ref_is_digest_pinned():
    c = _comp()
    assert c.image_ref({"registry": "r/x", "tag": "1.0", "digest": "sha256:abc"}) \
        == "r/x:1.0@sha256:abc"
    assert c.image_ref({"registry": "r/x", "digest": "sha256:abc"}) == "r/x@sha256:abc"


def test_render_sidecar_service_injects_scoped_env():
    c = _comp()
    spec = {"id": "demo.sc", "image": "r/x@sha256:abc", "command": None,
            "config": {"horizon_days": 90}}
    svc = c.render_sidecar_service(spec, "http://localhost:8830/mcp",
                                   "http://localhost:8731/mcp", "RTOK", "WTOK")
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert svc["image"] == "r/x@sha256:abc"
    assert svc["container_name"] == "demo.sc-sidecar"
    assert env["OKENGINE_EXTENSION_ID"] == "demo.sc"
    assert env["OKENGINE_WRITE_MCP_URL"] == "http://localhost:8731/mcp"
    assert env["OKENGINE_READ_TOKEN"] == "RTOK" and env["OKENGINE_WRITE_TOKEN"] == "WTOK"
    assert env["OKENGINE_CONFIG_HORIZON_DAYS"] == "90"
    assert svc["restart"] == "no"


def test_render_sidecar_service_is_os_hardened():
    """okengine#124: a sidecar (untrusted-code boundary) runs confined — no host net, no caps,
    no privilege escalation, read-only rootfs, resource-capped."""
    c = _comp()
    spec = {"id": "demo.sc", "image": "r/x@sha256:abc", "command": None, "config": {}}
    svc = c.render_sidecar_service(spec, "u", "u", "R", "W")
    assert "network_mode" not in svc                     # NOT host net (bridge by service name)
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["read_only"] is True and svc["tmpfs"] == ["/tmp"]
    assert svc["pids_limit"] >= 1 and svc["cpus"] > 0 and svc["mem_limit"]


def test_render_sidecar_service_limits_overridable():
    c = _comp()
    spec = {"id": "demo.sc", "image": "r/x@sha256:abc", "command": None, "config": {},
            "limits": {"pids": 64, "memory": "512m", "cpus": 0.5}}
    svc = c.render_sidecar_service(spec, "u", "u", "R", "W")
    assert svc["pids_limit"] == 64 and svc["mem_limit"] == "512m" and svc["cpus"] == 0.5


def test_render_trigger_wrapper():
    c = _comp()
    w = c.render_trigger_wrapper("demo.sc", "docker-compose.yml", "okproj")
    assert w.startswith("#!/usr/bin/env bash")
    assert "docker compose -f docker-compose.yml -p okproj run --rm -T demo.sc-sidecar" in w


# --- end-to-end: enabled sidecar -> specs + override ----------------------

def _pack_with_enabled_sidecar(tmp_path):
    disc = _load("extension_discovery", DISC_PATH)
    tok = _load("extension_tokens", TOK_PATH)
    pack = tmp_path / "pack"
    d = pack / "extensions" / "demo.sc"
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_valid_sidecar()), encoding="utf-8")
    disc.set_enabled(pack, "demo.sc", True)
    tok.mint(pack, "demo.sc", ["wiki/**"], ["dashboards/**"])   # what `enable` does
    return pack


def test_sidecar_specs_from_enabled(tmp_path):
    c = _comp()
    pack = _pack_with_enabled_sidecar(tmp_path)
    specs, errors = c.sidecar_specs(pack)
    assert errors == []
    assert len(specs) == 1
    assert specs[0]["id"] == "demo.sc"
    assert specs[0]["image"] == "reg.example.com/demo.sc:0.1.0@sha256:deadbeef"


def test_compose_override_injects_minted_token(tmp_path):
    c = _comp()
    pack = _pack_with_enabled_sidecar(tmp_path)
    secret = json.loads((pack / ".okengine" / "extension-secrets.json").read_text())["demo.sc"]
    override, wrappers, errors = c.sidecar_compose_override(pack)
    assert errors == []
    svc = override["services"]["demo.sc-sidecar"]
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert env["OKENGINE_WRITE_TOKEN"] == secret    # the actual minted token, injected
    assert "demo.sc" in wrappers


def test_compose_override_errors_without_token(tmp_path):
    c = _comp()
    disc = _load("extension_discovery", DISC_PATH)
    pack = tmp_path / "pack"
    d = pack / "extensions" / "demo.sc"
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_valid_sidecar()), encoding="utf-8")
    disc.set_enabled(pack, "demo.sc", True)         # enabled but NO token minted
    override, wrappers, errors = c.sidecar_compose_override(pack)
    assert any("no minted token" in e for e in errors)
