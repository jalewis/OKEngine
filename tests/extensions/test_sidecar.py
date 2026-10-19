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
    assert (
        "docker compose -f docker-compose.yml "
        "-f .okengine/generated/sidecars.compose.yml "
        "-p okproj run --rm -T demo.sc-sidecar"
    ) in w


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
    override, wrappers, errors = c.sidecar_compose_override(pack, project="okpack-alpha")
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
    override, wrappers, errors = c.sidecar_compose_override(pack, project="okpack-alpha")
    assert errors == ["demo.sc: no minted token (re-run `extensions enable`)"]


# --- okengine#1042 [33]: a sidecar extension with several operations -------------------------------
# Each operation's spec id was `<ext>:<op>`, so the token lookup (keyed by extension, one token per
# extension) missed, and sidecar-generate failed "no minted token (re-run `extensions enable`)",
# which re-running enable can never fix: every deploy of the pack aborted at step 1d. The same id
# also made the compose service and container name `<ext>:<op>-sidecar`, which Compose rejects.

COMPOSE_NAME = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$"     # Compose service / Docker container names


def _multi_op_sidecar(ext_id="acme.side", ops=("one", "two")):
    man = _valid_sidecar(ext_id)
    op = man.pop("operation")
    man["operations"] = {name: dict(op) for name in ops}
    return man


def _pack_with(tmp_path, manifest, mint=True):
    disc = _load("extension_discovery", DISC_PATH)
    tok = _load("extension_tokens", TOK_PATH)
    pack = tmp_path / "pack"
    d = pack / "extensions" / manifest["id"]
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    disc.set_enabled(pack, manifest["id"], True)
    if mint:
        tok.mint(pack, manifest["id"], ["wiki/**"], ["dashboards/**"])
    return pack


def test_a_multi_operation_sidecar_gets_one_service_per_operation_with_the_extension_token(
        tmp_path):
    import re
    c = _comp()
    pack = _pack_with(tmp_path, _multi_op_sidecar())
    secret = json.loads((pack / ".okengine" / "extension-secrets.json").read_text())["acme.side"]
    override, wrappers, errors = c.sidecar_compose_override(pack, project="okpack-alpha")
    assert errors == [], errors
    services = override["services"]
    assert set(services) == {"acme.side--one-sidecar", "acme.side--two-sidecar"}
    for op in ("one", "two"):
        svc = services[f"acme.side--{op}-sidecar"]
        env = dict(e.split("=", 1) for e in svc["environment"])
        assert env["OKENGINE_WRITE_TOKEN"] == secret and env["OKENGINE_READ_TOKEN"] == secret
        assert env["OKENGINE_EXTENSION_ID"] == "acme.side", "the identity is the extension's"
        assert env["OKENGINE_OPERATION"] == op
        assert re.match(COMPOSE_NAME, svc["container_name"]), svc["container_name"]
    assert all(re.match(COMPOSE_NAME, name) for name in services)
    assert set(wrappers) == {"acme.side--one", "acme.side--two"}
    assert "run --rm -T acme.side--one-sidecar" in wrappers["acme.side--one"]


def test_a_single_operation_sidecar_keeps_its_names_and_env(tmp_path):
    c = _comp()
    pack = _pack_with_enabled_sidecar(tmp_path)
    override, wrappers, errors = c.sidecar_compose_override(pack, project="okpack-alpha")
    assert errors == [] and set(override["services"]) == {"demo.sc-sidecar"}
    env = dict(e.split("=", 1) for e in override["services"]["demo.sc-sidecar"]["environment"])
    assert "OKENGINE_OPERATION" not in env and env["OKENGINE_EXTENSION_ID"] == "demo.sc"
    assert set(wrappers) == {"demo.sc"}


def test_a_multi_operation_sidecar_without_a_token_names_the_extension_once_per_op(tmp_path):
    c = _comp()
    pack = _pack_with(tmp_path, _multi_op_sidecar(), mint=False)
    _, _, errors = c.sidecar_compose_override(pack, project="okpack-alpha")
    assert errors == ["acme.side:one: no minted token for acme.side (re-run `extensions enable`)",
                      "acme.side:two: no minted token for acme.side (re-run `extensions enable`)"]


@pytest.mark.parametrize("op", ["bad name", "-lead", "a:b"])
def test_an_operation_name_compose_cannot_carry_is_refused(tmp_path, op):
    c = _comp()
    pack = _pack_with(tmp_path, _multi_op_sidecar(ops=("ok", op)))
    specs, errors = c.sidecar_specs(pack)
    assert [s["name"] for s in specs] == ["acme.side--ok"]
    assert errors == [f"acme.side:{op}: a sidecar operation name must match "
                      "[A-Za-z0-9][A-Za-z0-9_.-]* (it names a Compose service)"]


def test_sidecar_generate_runs_for_a_multi_operation_sidecar_as_deploy_sh_runs_it(tmp_path):
    """Real invocation of the finding's reproduction: `extensions enable`, then deploy step 1d's
    `framework.py extensions sidecar-generate`, with no test PYTHONPATH."""
    import os
    import subprocess
    pack = tmp_path / "pack"
    d = pack / "extensions" / "acme.side"
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump(_multi_op_sidecar()), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    cli = [sys.executable, str(REPO / "scripts" / "framework.py"), "extensions"]
    r = subprocess.run(cli + ["enable", str(pack), "acme.side"], capture_output=True, text=True,
                       timeout=180, env=env, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(cli + ["sidecar-generate", str(pack)], capture_output=True, text=True,
                       timeout=180, env=env, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    compose = yaml.safe_load((pack / ".okengine" / "generated" / "sidecars.compose.yml")
                             .read_text())
    assert set(compose["services"]) == {"acme.side--one-sidecar", "acme.side--two-sidecar"}
    assert (pack / ".okengine" / "generated" / "acme.side--one" / "trigger.sh").is_file()


# --- okengine#1044 [6]/[13]/[14]/[27]: a derived sidecar name is unique across extensions ---------
# `<ext>--<op>` names a multi-operation sidecar's Compose service and wrapper. An extension id may
# itself contain `--`, so extension `acme.side` operation `one` and a separate extension
# `acme.side--one` derived the same name, and the later spec silently replaced the earlier service.

def _pack_with_many(tmp_path, manifests):
    disc = _load("extension_discovery", DISC_PATH)
    tok = _load("extension_tokens", TOK_PATH)
    pack = tmp_path / "pack"
    for manifest in manifests:
        d = pack / "extensions" / manifest["id"]
        d.mkdir(parents=True)
        (d / "extension.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        disc.set_enabled(pack, manifest["id"], True)
        tok.mint(pack, manifest["id"], ["wiki/**"], ["dashboards/**"])
    return pack


def _other_image(manifest, digest="sha256:cafef00d"):
    manifest["operation"]["entrypoint"]["image"]["digest"] = digest
    return manifest


def test_an_extension_id_that_spells_another_sidecars_operation_name_is_refused(tmp_path):
    """The finding's reproduction: three specs, two services, `acme.side:one` silently gone."""
    c = _comp()
    pack = _pack_with_many(tmp_path, [_multi_op_sidecar(),
                                      _other_image(_valid_sidecar("acme.side--one"))])
    specs, errors = c.sidecar_specs(pack)
    assert [s["name"] for s in specs] == ["acme.side--two"], specs
    assert errors == ["sidecar name 'acme.side--one' is derived by both acme.side:one and "
                      "acme.side--one; one would silently replace the other's Compose service "
                      "and trigger wrapper (rename one extension or operation)"], errors
    override, wrappers, oerrors = c.sidecar_compose_override(pack, project="okpack-alpha")
    assert oerrors == errors and set(wrappers) == {"acme.side--two"}


def test_sidecar_generate_fails_on_a_derived_name_collision(tmp_path):
    """Deploy step 1d runs sidecar-generate; a collision must fail the deploy, not ship one image."""
    import os
    import subprocess
    pack = _pack_with_many(tmp_path, [_multi_op_sidecar(),
                                      _other_image(_valid_sidecar("acme.side--one"))])
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "COMPOSE_PROJECT_NAME")}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "framework.py"), "extensions",
                        "sidecar-generate", str(pack)], capture_output=True, text=True,
                       timeout=180, env=env, cwd=REPO)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "sidecar name 'acme.side--one' is derived by both" in r.stderr
    assert not (pack / ".okengine" / "generated" / "sidecars.compose.yml").exists()


def test_similar_but_distinct_sidecar_names_are_accepted(tmp_path):
    """Negative fixture: near-misses of the collision are distinct names and all ship."""
    c = _comp()
    pack = _pack_with_many(tmp_path, [_multi_op_sidecar(),
                                      _other_image(_valid_sidecar("acme.side--three")),
                                      _other_image(_valid_sidecar("acme.side-one"))])
    specs, errors = c.sidecar_specs(pack)
    assert errors == []
    assert sorted(s["name"] for s in specs) == ["acme.side--one", "acme.side--three",
                                                "acme.side--two", "acme.side-one"]


# --- okengine#1044 [5]: a sidecar runs in its own deployment's compose project ---------------------
# trigger.sh hardcoded `-p okengine`, so every pack on a host ran its sidecars in one shared
# project (on a network none of them deploys), and the read MCP URL was `localhost:8830`, which
# inside a bridge container is the sidecar itself.

def _skeleton_services():
    text = (REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml").read_text()
    import re
    return yaml.safe_load(re.sub(r"\{\{[A-Z_]+\}\}", "x", text))["services"]


def test_the_sidecar_read_mcp_url_names_the_skeleton_read_mcp_service():
    """Agreement detector: the URL a sidecar is handed is the skeleton's read MCP service name and
    the port that service listens on, which a bridge container can reach."""
    from urllib.parse import urlsplit
    c = _comp()
    url = urlsplit(c.SIDECAR_READ_MCP_URL)
    services = _skeleton_services()
    assert url.hostname in services, f"{url.hostname!r} is not a skeleton service"
    env = services[url.hostname].get("environment") or []
    assert f"PORT={url.port}" in env, (url.port, env)
    assert url.hostname not in ("localhost", "127.0.0.1")


def test_sidecar_compose_override_has_no_shared_project_default():
    import inspect
    c = _comp()
    param = inspect.signature(c.sidecar_compose_override).parameters["project"]
    assert param.default is inspect.Parameter.empty, "a default project is shared by every pack"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "a positional project is easy to misplace"


def _generate(pack, extra_env=None):
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "COMPOSE_PROJECT_NAME")}
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "framework.py"), "extensions",
                        "sidecar-generate", str(pack)], capture_output=True, text=True,
                       timeout=180, env=env, cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    gen = pack / ".okengine" / "generated"
    compose = yaml.safe_load((gen / "sidecars.compose.yml").read_text())
    return (gen / "demo.sc" / "trigger.sh").read_text(), compose["services"]["demo.sc-sidecar"]


def test_two_packs_on_one_host_run_their_sidecars_in_their_own_projects(tmp_path):
    """Real invocation, as deploy step 1d runs it: each pack's wrapper names the project its own
    `docker compose up` uses (the directory name), and its sidecar reaches the read MCP by service
    name on that project's network."""
    alpha = _pack_with_enabled_sidecar(tmp_path / "a").rename(tmp_path / "okpack-alpha")
    beta = _pack_with_enabled_sidecar(tmp_path / "b").rename(tmp_path / "OKPack.Beta")
    wrapper_a, service_a = _generate(alpha)
    wrapper_b, _ = _generate(beta)
    assert "-p okpack-alpha run --rm -T demo.sc-sidecar" in wrapper_a, wrapper_a
    assert "-p okpackbeta run --rm -T demo.sc-sidecar" in wrapper_b, wrapper_b   # normalized
    env = dict(e.split("=", 1) for e in service_a["environment"])
    assert env["OKENGINE_MCP_URL"] == "http://okengine-mcp:8730/mcp"


def test_the_wrapper_follows_compose_project_name_from_the_pack_env(tmp_path):
    pack = _pack_with_enabled_sidecar(tmp_path)
    (pack / ".env").write_text("COMPOSE_PROJECT_NAME=okalpha\n", encoding="utf-8")
    wrapper, _ = _generate(pack)
    assert "-p okalpha run --rm -T demo.sc-sidecar" in wrapper, wrapper
    wrapper, _ = _generate(pack, {"COMPOSE_PROJECT_NAME": "okfromshell"})
    assert "-p okfromshell run --rm -T demo.sc-sidecar" in wrapper, wrapper


def test_sidecar_generate_fails_when_no_compose_project_can_be_derived(tmp_path):
    """A directory name Compose would normalize to nothing, and no COMPOSE_PROJECT_NAME: fail,
    never fall back to a project another pack could share."""
    import os
    import subprocess
    pack = _pack_with_enabled_sidecar(tmp_path).rename(tmp_path / "___")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "COMPOSE_PROJECT_NAME")}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "framework.py"), "extensions",
                        "sidecar-generate", str(pack)], capture_output=True, text=True,
                       timeout=180, env=env, cwd=REPO)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL: cannot derive a compose project name" in r.stderr
    assert not (pack / ".okengine" / "generated" / "sidecars.compose.yml").exists()
