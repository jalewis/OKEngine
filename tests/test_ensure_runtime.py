"""Smoke tests for scripts/ensure-runtime.sh — seeds a pack's .hermes-data/
config.yaml (host-owned) before docker compose up, for git-cloned library packs
that have no committed runtime."""
import os
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ensure-runtime.sh"
TEMPLATE = REPO / "config" / "config.yaml.template"

# Deployment-tool orchestration exercises real shell/filesystem/process
# boundaries. Token, ownership, and security-baseline words in individual test
# names describe the data under test; they do not turn these into security
# property tests with the security lane's runtime contract.
pytestmark = pytest.mark.integration

# Run as the current uid so the writability check passes (the gateway-uid mismatch
# path is exercised separately below).
SELF_UID = str(os.getuid())
SELF_GID = str(os.getgid())


def _run(args, **env):
    e = dict(os.environ, HERMES_UID=SELF_UID, HERMES_GID=SELF_GID,
             OKENGINE_CRON_PLUS_SKIP="1")   # hermetic: no plugin clone in tests
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=30, env=e)


def test_script_exists_and_parses():
    assert SCRIPT.is_file()
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_seeds_missing_runtime(tmp_path):
    """A fresh clone (no .hermes-data) gets config.yaml + qmd/ + logs/."""
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = tmp_path / ".hermes-data" / "config.yaml"
    assert cfg.is_file()
    assert (tmp_path / ".hermes-data" / "qmd").is_dir()
    assert (tmp_path / ".hermes-data" / "logs").is_dir()
    assert (tmp_path / ".hermes-data" / ".gitkeep").is_file()
    # seeded verbatim from the template, except ensure-runtime rewrites the MCP Bearer to a
    # generated secret token (okengine#105) — compare modulo that one line.
    import re
    _norm = lambda s: re.sub(r"Bearer \S+", "Bearer X", s)
    assert _norm(cfg.read_text()) == _norm(TEMPLATE.read_text())


def test_existing_config_preserves_operator_values_and_adds_security_baseline(tmp_path):
    """An old config keeps operator values/comments while gaining a newly introduced safe default."""
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text("# my edited config\nterminal:\n  backend: local\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0
    after = cfg.read_text()
    assert after.startswith("# my edited config\nterminal:\n  backend: local\n")
    assert "platform_toolsets:\n  api_server:\n    - okengine\n    - okengine-write-chat\n" in after
    assert "already present" in r.stdout


def test_existing_platform_toolsets_gets_missing_api_server_without_clobber(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text(
        "# keep me\n"
        "platform_toolsets:\n"
        "  cron:\n"
        "    - hermes-cron\n"
        "model:\n"
        "  default: operator/model\n"
    )

    first = _run([str(tmp_path)])
    assert first.returncode == 0, first.stderr
    after = cfg.read_text()
    assert "# keep me" in after and "default: operator/model" in after
    assert "  cron:\n    - hermes-cron" in after
    assert after.count("  api_server:") == 1
    assert "  api_server:\n    - okengine\n    - okengine-write-chat\nmodel:" in after

    second = _run([str(tmp_path)])
    assert second.returncode == 0, second.stderr
    assert cfg.read_text() == after, "reconciliation must be idempotent"


def test_existing_mcp_config_gains_server_bound_source_quality_writer(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text(
        "mcp_servers:\n"
        "  okengine-write:\n"
        "    command: /custom/python\n"
        "platform_toolsets:\n"
        "  cron: [no_mcp]\n"
    )
    first = _run([str(tmp_path)])
    assert first.returncode == 0, first.stderr
    after = cfg.read_text()
    assert after.count("okengine-write-source-quality:") == 1
    assert "OKENGINE_WRITE_ACTOR: cron:source-quality-backfill" in after
    assert after.count("OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml") == 3  # + the Agent Chat writer (okengine#983)
    assert "OKENGINE_CRON_JOBS: /opt/data/cron-plus/jobs.json" in after
    assert "OKENGINE_OUTPUT_CONTRACT_MODE: enforce" in after
    assert "command: /custom/python" in after, "operator configuration must be preserved"
    second = _run([str(tmp_path)])
    assert second.returncode == 0
    assert cfg.read_text() == after


def test_existing_writer_repairs_stale_policy_catalog_path(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text(
        "mcp_servers:\n"
        "  okengine-write:\n"
        "    command: /custom/python\n"
        "    env:\n"
        "      OKENGINE_POLICY_CATALOG: /obsolete/catalog.yaml\n"
        "platform_toolsets:\n"
        "  cron: [no_mcp]\n"
    )

    first = _run([str(tmp_path)])
    assert first.returncode == 0, first.stderr
    after = cfg.read_text()
    assert "/obsolete/catalog.yaml" not in after
    assert after.count(
        "OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml"
    ) == 3  # + the Agent Chat writer (okengine#983)

    second = _run([str(tmp_path)])
    assert second.returncode == 0, second.stderr
    assert cfg.read_text() == after


def test_existing_writer_expands_inline_env_without_losing_operator_values(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text(
        "mcp_servers:\n"
        "  okengine-write-custom:\n"
        "    command: /custom/python\n"
        "    env: {OKENGINE_WRITE_ACTOR: 'cron:custom', CUSTOM_FLAG: keep}\n"
        "platform_toolsets:\n"
        "  cron: [no_mcp]\n"
    )

    result = _run([str(tmp_path)])
    assert result.returncode == 0, result.stderr
    after = cfg.read_text()
    assert after.count("    env:") == 3  # + the Agent Chat writer (okengine#983)
    parsed = yaml.safe_load(after)
    env = parsed["mcp_servers"]["okengine-write-custom"]["env"]
    assert env["OKENGINE_WRITE_ACTOR"] == "cron:custom"
    assert env["CUSTOM_FLAG"] == "keep"
    assert env["OKENGINE_POLICY_CATALOG"] == "/opt/hermes/config/policy/catalog.yaml"


def test_materialized_cron_capabilities_gain_distinct_server_bound_writers(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text(
        "mcp_servers:\n"
        "  okengine-write:\n"
        "    command: /custom/python\n"
        "platform_toolsets:\n"
        "  cron: [no_mcp]\n"
    )
    state = tmp_path / ".okengine"
    state.mkdir()
    (state / "effective-policy.json").write_text(json.dumps({
        "capabilities": {
            "cron:tid-procedure-candidate": {},
            "cron:tid-gap-priority-candidate": {},
            "extension:not-a-cron": {},
        }
    }))
    first = _run([str(tmp_path)])
    assert first.returncode == 0, first.stderr
    after = cfg.read_text()
    assert after.count(f"{_cps().contract_writer_name('tid-procedure-candidate')}:") == 1
    assert after.count("OKENGINE_WRITE_ACTOR: cron:tid-procedure-candidate") == 1
    assert after.count("OKENGINE_CRON_JOBS: /opt/data/cron-plus/jobs.json") >= 2
    assert after.count(f"{_cps().contract_writer_name('tid-gap-priority-candidate')}:") == 1
    assert after.count("OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml") == 5  # + the Agent Chat writer (okengine#983)
    assert "extension:not-a-cron" not in after
    assert "command: /custom/python" in after
    second = _run([str(tmp_path)])
    assert second.returncode == 0, second.stderr
    assert cfg.read_text() == after


_CONTRACT = {
    "api": 1, "allowed_namespaces": ["briefs"], "allowed_types": ["brief"],
    "operations": ["create"], "required_fields": ["type"], "required_relationships": [],
    "body": {"required": False, "min_non_whitespace": 0}, "unknown_fields": "allow",
    "unresolved_links": "review", "placeholder_links": "reject", "completion": "run",
}


def _domain_pack(pack, lanes):
    """A deployment whose OWN crons carry `lanes`. Since okengine#1029 ensure-runtime renders lane
    writers from the pack's own fleet, never from the checkout's shared cron-plus-jobs.json."""
    (pack / "crons").mkdir()
    (pack / "pack.yaml").write_text("name: okpack-er\nversion: 0.1.0\n")
    (pack / "crons" / "domain-crons.json").write_text(json.dumps([
        {"name": name, "schedule": {"kind": "cron", "expr": "0 3 * * *"}, "prompt": "write it",
         "enabled_toolsets": ["okengine-write"], "output_contract": _CONTRACT,
         "max_iterations": 10, "timeout": 600, "expect": {"may_idle": {"max_idle_runs": 10}},
         **extra} for name, extra in lanes]))
    rt = pack / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text("mcp_servers:\n  okengine-write:\n    command: /custom/python\n")
    return cfg


def test_generated_contracted_lane_gains_enforcing_bound_writer(tmp_path):
    cfg = _domain_pack(tmp_path, [("okpack-er-compile", {})])
    first = _run([str(tmp_path)])
    assert first.returncode == 0, first.stderr
    server = yaml.safe_load(cfg.read_text())["mcp_servers"]["okengine-write-okpack-er-compile"]
    assert server["env"]["OKENGINE_WRITE_ACTOR"] == "cron:okpack-er-compile"
    assert server["env"]["OKENGINE_POLICY_CATALOG"] == "/opt/hermes/config/policy/catalog.yaml"
    assert server["env"]["OKENGINE_CRON_JOBS"] == "/opt/data/cron-plus/jobs.json"
    assert server["env"]["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce"


def test_pack_lane_write_tools_are_forwarded_to_its_bound_writer(tmp_path):
    """okengine#664: a pack lane narrows its MCP surface with `write_tools` on its cron def; the
    generated server-bound writer carries it as OKENGINE_WRITE_TOOLS. The engine table in
    write_server.py never learns the lane's name."""
    lane = "okpack-er-vendor-frontmatter-backfill"
    cfg = _domain_pack(tmp_path, [(lane, {"write_tools": ["update_entity", " append_to_section "]})])
    r = _run([str(tmp_path)])
    assert r.returncode == 0, r.stderr
    servers = yaml.safe_load(cfg.read_text())["mcp_servers"]
    assert servers[_cps().contract_writer_name(lane)]["env"]["OKENGINE_WRITE_TOOLS"] == \
        "update_entity,append_to_section"


def test_misplaced_source_quality_writer_is_moved_under_mcp_servers(tmp_path):
    """A legacy reconciliation put the reserved writer under `web:`. Mere text
    presence must not hide that invalid placement from subsequent deploys."""
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text(
        "mcp_servers:\n"
        "  okengine-write:\n"
        "    command: /custom/python\n"
        "web:\n"
        "  backend: rotate\n"
        "  # Engine-managed server-bound identity for source-quality-backfill.\n"
        "  okengine-write-source-quality:\n"
        "    command: /opt/hermes/.venv/bin/python\n"
        "    args:\n"
        "    - /opt/hermes/okengine-mcp/write_server.py\n"
        "    env:\n"
        "      OKENGINE_WRITE_ACTOR: cron:source-quality-backfill\n"
        "platform_toolsets:\n"
        "  cron: [no_mcp]\n"
    )

    first = _run([str(tmp_path)])
    assert first.returncode == 0, first.stderr
    after = cfg.read_text()
    assert after.count("okengine-write-source-quality:") == 1
    mcp, web = after.split("web:\n", 1)
    assert "okengine-write-source-quality:" in mcp
    assert "okengine-write-source-quality:" not in web
    assert "web:\n  backend: rotate\nplatform_toolsets:" in after

    second = _run([str(tmp_path)])
    assert second.returncode == 0, second.stderr
    assert cfg.read_text() == after, "repair must reach an idempotent steady state"


def test_existing_api_server_policy_is_operator_owned(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text("platform_toolsets:\n  api_server: [okengine, web]\n")

    r = _run([str(tmp_path)])

    assert r.returncode == 0, r.stderr
    assert "api_server: [okengine, web]" in cfg.read_text()
    assert cfg.read_text().count("api_server:") == 1


def test_inline_platform_toolsets_is_not_duplicated(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text("platform_toolsets: {}\n")

    r = _run([str(tmp_path)])

    assert r.returncode == 0, r.stderr
    assert cfg.read_text() == "platform_toolsets: {}\n"
    assert "left operator config unchanged" in r.stdout


def test_fails_when_not_writable_by_gateway_uid(tmp_path):
    """If the pack tree isn't writable by HERMES_UID, fail BEFORE compose with an
    actionable message (issue #16) — not a silent broken start."""
    e = dict(os.environ, HERMES_UID="99999", HERMES_GID="99999", OKENGINE_CRON_PLUS_SKIP="1")
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)],
                       capture_output=True, text=True, timeout=30, env=e)
    assert r.returncode == 1
    assert "not writable by 99999" in r.stderr
    assert "--fix-perms" in r.stderr and "chown" in r.stderr


def test_fix_perms_makes_tree_writable(tmp_path):
    """--fix-perms makes the tree world-writable so a non-matching gateway uid can
    write it (the documented local-deploy remedy)."""
    e = dict(os.environ, HERMES_UID="99999", HERMES_GID="99999", OKENGINE_CRON_PLUS_SKIP="1")
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path), "--fix-perms"],
                       capture_output=True, text=True, timeout=30, env=e)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "fix-perms" in r.stdout
    # .hermes-data is now other-writable
    mode = oct((tmp_path / ".hermes-data").stat().st_mode)[-3:]
    assert mode[-1] in ("6", "7")   # other has write


def test_generates_secret_token_on_fresh_deploy(tmp_path):
    """okengine#105: a fresh pack (no .env) must get a GENERATED secret OKENGINE_MCP_TOKEN,
    not the built-in 'okengine-local' (which the read MCP refuses on its 0.0.0.0 bind →
    crash-loop), and the seeded config's Bearer must be rewritten to match."""
    import re
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    env = (tmp_path / ".env").read_text()
    m = re.search(r"(?m)^OKENGINE_MCP_TOKEN=(\S+)", env)
    assert m, f"no OKENGINE_MCP_TOKEN written to .env:\n{env}"
    tok = m.group(1)
    assert tok != "okengine-local" and len(tok) >= 24, f"not a generated secret: {tok!r}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert f"Bearer {tok}" in cfg, "config Bearer not synced to the generated token"
    assert "Bearer okengine-local" not in cfg, "default token still present in config"


def test_seeded_config_passes_validator(tmp_path):
    """The seeded config.yaml satisfies the validator's runtime-config key checks
    (so a clone -> ensure-runtime -> validate flow is clean on that check)."""
    import importlib.util
    import sys
    _run([str(tmp_path)])
    spec = importlib.util.spec_from_file_location(
        "framework_validate", REPO / "scripts" / "framework_validate.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_validate"] = m
    spec.loader.exec_module(m)
    r = m.Report()
    m.check_runtime_config(tmp_path, r)
    assert not any(s == "FAIL" for s, c, d in r.rows), [(c, d) for s, c, d in r.rows]


def test_mcp_token_synced_from_env(tmp_path):
    """When .env sets OKENGINE_MCP_TOKEN, the seeded read-MCP Authorization header
    is rewritten to match (okengine#32) — otherwise the gateway agent 401s."""
    (tmp_path / ".env").write_text('OKENGINE_MCP_TOKEN="s3cr3t-xyz"\n')
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert 'Authorization: "Bearer s3cr3t-xyz"' in cfg
    assert "<OKENGINE_MCP_TOKEN" not in cfg, "template placeholder must be gone"


# (okengine#105) No-token-in-.env no longer leaves the built-in "okengine-local" default —
# ensure-runtime generates a secret; covered by test_generates_secret_token_on_fresh_deploy.


def test_mcp_token_synced_unquoted_header(tmp_path):
    """The header may be unquoted YAML (`Authorization: Bearer x`), as seeded by
    older templates. Sync must handle that form too — not just the quoted
    template — and must run on an already-present config (okengine#32)."""
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    (rt / "config.yaml").write_text(
        "terminal:\n  backend: local\n"
        "mcp_servers:\n  okengine:\n    headers:\n"
        "      Authorization: Bearer <OKENGINE_MCP_TOKEN from pack .env>\n"
        "  okengine-write:\n    command: x\n"
    )
    (tmp_path / ".env").write_text("OKENGINE_MCP_TOKEN=abc123\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (rt / "config.yaml").read_text()
    assert "Authorization: Bearer abc123" in cfg, cfg
    assert "<OKENGINE_MCP_TOKEN" not in cfg


def test_mcp_url_uses_service_name_not_host_port(tmp_path):
    """okengine#138 (supersedes #137): the gateway shares the compose bridge with okengine-mcp and
    reaches the read-MCP by SERVICE NAME on the container port (okengine-mcp:8730) — no host port,
    no port_offset, no cross-pack collision. So an offset pack's MCP url is the service name, NOT a
    rewritten localhost host-port."""
    (tmp_path / "pack.yaml").write_text("name: demo\nport_offset: 100\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert "http://okengine-mcp:8730/mcp" in cfg      # service name on the bridge
    assert "localhost:8830" not in cfg                # not the superseded #137 offset host-port form
    # (the template's explanatory comment legitimately mentions localhost:8730 — don't over-assert)


def test_no_offset_keeps_default_mcp_url(tmp_path):
    """No port_offset -> the template default :8730 is kept (back-compat)."""
    (tmp_path / "pack.yaml").write_text("name: demo\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert "http://localhost:8730/mcp" in cfg


def test_iwe_absent_from_runtime_images_and_staging():
    """A graph lookup must never install or stage the retired whole-corpus IWE runtime."""
    for f in ("okengine-mcp/Dockerfile", "okengine-reader/Dockerfile",
              "okengine-cockpit/Dockerfile", "scripts/ensure-runtime.sh",
              "scripts/deploy-cron-scripts.sh"):
        body = (REPO / f).read_text().lower()
        assert "iwe-org/iwe/releases" not in body
        assert "kb_graph.py" not in body


def test_template_api_server_toolset_is_locked():
    """Secure-by-default: the engine config template's api_server (Agent Chat) toolset must grant
    ONLY vault read + enforced write — never web/terminal/code_execution/file/computer_use — so a
    deployment that enables chat can't inherit a broad toolset by omission (docs/agent-chat.md's
    'do not skip this' footgun, made the default). Add `web` per-deployment, deliberately."""
    import yaml
    cfg = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    api = (cfg.get("platform_toolsets") or {}).get("api_server")
    assert api == ["okengine", "okengine-write-chat"], api
    forbidden = {"terminal", "code_execution", "coding", "file", "computer_use", "web", "shell"}
    assert not (set(api) & forbidden), f"chat toolset leaked a dangerous tool: {set(api) & forbidden}"


def test_pins_hermes_uid_into_env_for_bare_compose(tmp_path):
    """invariant-audit HIGH #1: ensure-runtime seeded the runtime but never pinned HERMES_UID into
    .env, so a bare `docker compose up` (the documented step-by-step path) interpolated
    ${HERMES_UID:-10000} and ran the gateway as a DIFFERENT uid than the tree — a silently dead
    scheduler. It must pin the resolved uid:gid into .env (like deploy.sh) so compose uses the same."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".env").write_text("OKENGINE_MCP_TOKEN=okengine-local\n")
    # NO HERMES_UID in the env -> the resolver must fall to the tree owner (this uid) and pin it
    e = dict(os.environ, OKENGINE_CRON_PLUS_SKIP="1")
    e.pop("HERMES_UID", None); e.pop("HERMES_GID", None)
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True,
                       timeout=30, env=e)
    assert r.returncode == 0, r.stderr
    env_txt = (tmp_path / ".env").read_text()
    assert f"HERMES_UID={SELF_UID}" in env_txt, f"HERMES_UID not pinned:\n{env_txt}"
    assert f"HERMES_GID={SELF_GID}" in env_txt


def test_existing_uid_pin_is_not_clobbered(tmp_path):
    """A deliberate operator pin (portable/shared vault) must survive — pin only when absent."""
    (tmp_path / "wiki").mkdir()
    # pin our OWN uid (so the writability gate passes) — the point is no DUPLICATE line is appended
    (tmp_path / ".env").write_text(f"HERMES_UID={SELF_UID}\nHERMES_GID={SELF_GID}\nOKENGINE_MCP_TOKEN=x\n")
    e = dict(os.environ, OKENGINE_CRON_PLUS_SKIP="1", HERMES_UID=SELF_UID, HERMES_GID=SELF_GID)
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True,
                       timeout=30, env=e)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".env").read_text().count("HERMES_UID=") == 1
    assert (tmp_path / ".env").read_text().count("HERMES_GID=") == 1


def test_cron_plus_install_delegates_to_the_shared_installer():
    """HIGH #6: ensure-runtime must install cron-plus via install-cron-plus.sh — the ONE installer
    that also applies the carried patches (job-env + after-ordering) extension crons require. A
    divergent inline `git clone`/`checkout` here ships an UNPATCHED scheduler. Static guard so the
    clone-only block can't come back."""
    txt = SCRIPT.read_text(encoding="utf-8")
    assert "install-cron-plus.sh" in txt, "ensure-runtime must delegate cron-plus install to the shared, patch-applying installer"
    # the old divergent shape (a bare clone of the plugin into plugins/cron-plus) must be gone
    assert "git clone" not in txt or "cron-plus" not in txt.split("git clone", 1)[1][:200], \
        "ensure-runtime should not inline-clone cron-plus — delegate to install-cron-plus.sh"


def test_install_cron_plus_verifies_the_pinned_policy_modules():
    """Per-job env and after: ordering, which extension crons depend on, ship in the pinned
    hermes-cron-plus since v0.2.0; the shared installer verifies the pin's policy modules against
    the engine's tested copies instead of patching them in."""
    installer = (REPO / "scripts" / "install-cron-plus.sh").read_text(encoding="utf-8")
    assert '"$ENGINE_DIR"/patches/cron-plus/*.py' in installer and "cmp -s" in installer
    assert (REPO / "patches" / "cron-plus" / "after_ordering.py").is_file()


# --- bounded writer names (okengine#840) --------------------------------------------------------
LONG_LANE = "okengine.predictions:prediction-falsification-search"


def _cps():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cps_for_ensure_runtime",
                                                  REPO / "scripts" / "cron_pack_split.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _writer_block(name, actor):
    return (f"  {name}:\n    command: /opt/hermes/.venv/bin/python\n    args:\n"
            f"      - /opt/hermes/okengine-mcp/write_server.py\n    env:\n"
            f"      OKENGINE_WRITE_ACTOR: {actor}\n")


def _with_jobs(jobs, fn, pack):
    """Make each job's lane an actor of `pack` through its deploy-materialized policy (a
    `cron:` capability), the per-deployment input that carries a lane name no domain cron may
    use. The checkout's shared cron-plus-jobs.json is no longer read (okengine#1029)."""
    (pack / ".okengine").mkdir(exist_ok=True)
    (pack / ".okengine" / "effective-policy.json").write_text(json.dumps(
        {"capabilities": {f"cron:{job['name']}": {} for job in jobs}}))
    return fn()


def _pack(tmp_path, mcp_body):
    (tmp_path / "crons").mkdir()
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text("mcp_servers:\n" + mcp_body)
    return cfg


def test_a_long_lane_is_registered_under_its_bounded_writer_name(tmp_path):
    """ensure-runtime must register exactly the name regen stamps onto the job's toolset."""
    cps = _cps()
    cfg = _pack(tmp_path, "  okengine-write:\n    command: /custom/python\n")
    run = _with_jobs([{"name": LONG_LANE, "output_contract": {}}],
                     lambda: _run([str(tmp_path)]), tmp_path)
    assert run.returncode == 0, run.stderr
    after = cfg.read_text()
    assert f"  {cps.contract_writer_name(LONG_LANE)}:" in after
    assert f"  {cps.unbounded_writer_name(LONG_LANE)}:" not in after


def test_the_engines_own_pre_bound_block_is_retired_not_duplicated(tmp_path):
    """Left in place, the old block keeps a second server registered for the lane -- one the
    gateway still spawns, under the name that clamps."""
    cps = _cps()
    stale = cps.unbounded_writer_name(LONG_LANE)
    cfg = _pack(tmp_path, "  okengine-write:\n    command: /custom/python\n"
                + _writer_block(stale, f"cron:{LONG_LANE}"))
    run = _with_jobs([{"name": LONG_LANE, "output_contract": {}}],
                     lambda: _run([str(tmp_path)]), tmp_path)
    assert run.returncode == 0, run.stderr
    after = cfg.read_text()
    assert f"  {stale}:" not in after, "the pre-bound block must be retired"
    assert after.count(f"OKENGINE_WRITE_ACTOR: cron:{LONG_LANE}") == 1, after
    assert "retired 1 pre-bound name" in run.stdout, run.stdout


def test_an_operator_block_under_the_old_name_but_another_actor_is_kept(tmp_path):
    """Retirement keys on the engine's exact old name AND its actor. Same name, different
    identity is somebody else's server and is never removed."""
    cps = _cps()
    stale = cps.unbounded_writer_name(LONG_LANE)
    cfg = _pack(tmp_path, "  okengine-write:\n    command: /custom/python\n"
                + _writer_block(stale, "cron:an-operator-lane"))
    run = _with_jobs([{"name": LONG_LANE, "output_contract": {}}],
                     lambda: _run([str(tmp_path)]), tmp_path)
    assert run.returncode == 0, run.stderr
    after = cfg.read_text()
    assert f"  {stale}:" in after and "OKENGINE_WRITE_ACTOR: cron:an-operator-lane" in after


def test_reconciling_bounded_writers_is_idempotent(tmp_path):
    cps = _cps()
    cfg = _pack(tmp_path, "  okengine-write:\n    command: /custom/python\n"
                + _writer_block(cps.unbounded_writer_name(LONG_LANE), f"cron:{LONG_LANE}"))
    jobs = [{"name": LONG_LANE, "output_contract": {}}]
    assert _with_jobs(jobs, lambda: _run([str(tmp_path)]), tmp_path).returncode == 0
    first = cfg.read_text()
    second = _with_jobs(jobs, lambda: _run([str(tmp_path)]), tmp_path)
    assert second.returncode == 0, second.stderr
    assert cfg.read_text() == first, "a second reconcile must change nothing"


def test_an_unloadable_derivation_fails_the_deploy_rather_than_re_deriving(tmp_path):
    """A silently restated rule is the drift this replaced, so a load failure is fatal."""
    import shutil
    engine = tmp_path / "engine"
    engine.mkdir()
    shutil.copytree(REPO / "config", engine / "config")
    shutil.copytree(REPO / "scripts", engine / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (engine / "scripts" / "cron_pack_split.py").write_text("raise ImportError('broken')\n")
    (engine / "config" / "cron-plus-jobs.json").write_text(
        json.dumps({"jobs": [{"name": LONG_LANE, "output_contract": {}}]}))
    pack = tmp_path / "pack"
    pack.mkdir()
    _pack(pack, "  okengine-write:\n    command: /custom/python\n")
    e = dict(os.environ, HERMES_UID=SELF_UID, HERMES_GID=SELF_GID, OKENGINE_CRON_PLUS_SKIP="1")
    run = subprocess.run(["bash", str(engine / "scripts" / "ensure-runtime.sh"), str(pack)],
                         capture_output=True, text=True, timeout=30, env=e)
    assert run.returncode != 0, run.stdout
    assert "cannot load the writer-name derivation" in run.stderr, run.stderr


# --- okengine#994 [17]: the token is read and rewritten the way compose reads it --------------------
# A grep/cut read took everything after `=`, so an inline comment, quotes around a commented value or
# an `export` line put a different token in the read-MCP header than compose hands the MCP server,
# and every read-MCP call 401'd. The rewrite regex also missed `export` lines, which then won.

def _compose_token(pack):
    import sys
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import dotenv_lib
    return dotenv_lib.read_compose_env(pack / ".env", {})["OKENGINE_MCP_TOKEN"]


def _header_token(pack):
    import re
    cfg = (pack / ".hermes-data" / "config.yaml").read_text()
    match = re.search(r'Authorization:[ \t]*"?Bearer ([^\n"]*)', cfg)
    assert match, cfg
    return match.group(1).strip()


@pytest.mark.parametrize("line", [
    "OKENGINE_MCP_TOKEN=abc123 # rotated 2026-10",
    "OKENGINE_MCP_TOKEN='abc123' # rotated",
    'OKENGINE_MCP_TOKEN="abc123"  # rotated',
    "export OKENGINE_MCP_TOKEN=abc123",
    "OKENGINE_MCP_TOKEN=stale\nOKENGINE_MCP_TOKEN=abc123",
])
def test_the_header_carries_the_token_compose_hands_the_mcp(tmp_path, line):
    (tmp_path / ".env").write_text(line + "\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert _header_token(tmp_path) == "abc123" == _compose_token(tmp_path)
    assert "generated a secret" not in r.stdout, "an operator token must never be replaced"


@pytest.mark.parametrize("text", [
    "export OKENGINE_MCP_TOKEN=okengine-local\n",
    "OKENGINE_MCP_TOKEN=okengine-local\nexport OKENGINE_MCP_TOKEN=okengine-local\n",
    "export OKENGINE_MCP_TOKEN=okengine-local # default\nOKENGINE_MCP_TOKEN = ''\n",
    "OKENGINE_MCP_TOKEN: okengine-local\n",
    "KEEP=1\nOKENGINE_MCP_TOKEN_OLD=okengine-local",       # a lookalike key, no trailing newline
])
def test_a_generated_token_replaces_every_spelling_of_the_default(tmp_path, text):
    (tmp_path / ".env").write_text(text)
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    token = _header_token(tmp_path)
    assert token != "okengine-local" and len(token) >= 24
    assert _compose_token(tmp_path) == token, (tmp_path / ".env").read_text()
    env = (tmp_path / ".env").read_text()
    assert "okengine-local" not in env.replace("OKENGINE_MCP_TOKEN_OLD=okengine-local", "")
    assert "KEEP=1" in env or "KEEP" not in text


def test_a_spelling_the_rewrite_cannot_reach_fails_the_deploy(tmp_path):
    """Negative fixture for the read-back gate: compose accepts a vertical tab before `=`, the
    rewrite does not, and (after a line it does rewrite) that line wins. The deploy must FAIL, not
    sync a header the MCP rejects."""
    (tmp_path / ".env").write_text(
        "OKENGINE_MCP_TOKEN=okengine-local\nOKENGINE_MCP_TOKEN\v= okengine-local\n")
    r = _run([str(tmp_path)])
    assert r.returncode != 0
    assert "compose would not hand the containers that value" in r.stderr


@pytest.mark.parametrize("text", ["OKENGINE_MCP_TOKEN=abc123", "OKENGINE_MCP_TOKEN=abc123\n"])
def test_appended_pins_never_glue_onto_an_unterminated_last_line(tmp_path, text):
    """A .env without a trailing newline had `HERMES_UID=` glued onto its last line
    (the token value gained a `HERMES_UID=<uid>` suffix): the token was corrupted and the pin lost."""
    import sys
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import dotenv_lib
    (tmp_path / ".env").write_text(text)
    (tmp_path / "pack.yaml").write_text("name: p\ntrust: public\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    env = dotenv_lib.read_compose_env(tmp_path / ".env", {})
    assert env["OKENGINE_MCP_TOKEN"] == "abc123" == _header_token(tmp_path)
    assert env["HERMES_UID"] == SELF_UID and env["HERMES_GID"] == SELF_GID
    assert env["OKENGINE_TRUST"] == "public"
    assert "\n\n" not in (tmp_path / ".env").read_text(), "a terminated file gains no blank line"
