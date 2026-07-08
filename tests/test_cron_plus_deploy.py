"""Regression guards for the cron-plus pack-deploy path (#17-#21).

These are static/offline guards — the live path needs a Docker host. They lock in
that the deploy targets the PACK RUNTIME (the gateway container / .hermes-data),
not the old host ~/.hermes or engine-repo compose, so a refactor can't silently
revert the fixes.
"""
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
S = REPO / "scripts"


def test_gitignore_ignores_generated_jobs():  # #21
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "config/cron-plus-jobs.json"],
                       capture_output=True, text=True)
    assert r.returncode == 0, "config/cron-plus-jobs.json is not gitignored (inline-comment bug?)"


def test_deploy_cron_scripts_creates_runtime_dir():  # #17 + usage.db readonly fix
    t = (S / "deploy-cron-scripts.sh").read_text()
    # metrics too: pre-create as the cron uid so usage.db isn't first made root-owned
    # (which fails usage-rollup with "attempt to write a readonly database")
    assert "mkdir -p /opt/data/scripts /opt/data/config /opt/data/metrics" in t


def test_deploy_stages_base_schema_to_config():  # okengine#90 P2
    """The deploy must stage config/base-schema.yaml to /opt/data/config so the staged cron
    schema_lib resolves the engine-owned core (../config from /opt/data/scripts). Without it, cron
    lanes see only the pack's domain types and miss the core source/concept/trend/… ."""
    t = (S / "deploy-cron-scripts.sh").read_text()
    assert "mkdir -p /opt/data/scripts /opt/data/config" in t      # both runtime dirs created
    assert "base-schema.yaml" in t                                 # the engine core schema is staged
    assert "-C /opt/data/config/" in t                             # …into the config dir (not scripts/)
    assert "base-schema deployed" in t                             # the staging echo is present


def test_deploy_jobs_targets_pack_runtime_not_host_hermes():  # #18
    t = (S / "deploy-cron-plus-jobs.sh").read_text()
    assert "/opt/data/cron-plus/jobs.json" in t
    assert ".hermes/cron-plus/jobs.json" not in t        # no host ~/.hermes target
    assert "ps -q gateway" in t                          # finds the pack container (compose-scoped, #108)


def test_deploy_jobs_expands_jitter_sentinels():  # #107
    """Engine crons ship @jitter:* sentinels; the deploy must expand them to concrete schedules
    (cron-plus can't parse a raw sentinel — it errors every tick) before streaming to the container."""
    t = (S / "deploy-cron-plus-jobs.sh").read_text()
    assert "cron_jitter" in t and "expand_jobs" in t, "deploy no longer expands @jitter sentinels"
    assert 'DEPLOY_JOBS' in t and '< "$DEPLOY_JOBS"' in t, "deploy must stream the expanded copy, not raw $SRC"


def test_deploy_scripts_scope_gateway_to_pack_project():  # #108
    """On a multi-pack host the deploy must target THIS pack's gateway (its compose project),
    not the first 'gateway'-labeled container globally."""
    for name in ("deploy-cron-plus-jobs.sh", "deploy-cron-scripts.sh"):
        t = (S / name).read_text()
        assert 'docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway' in t, f"{name} not project-scoped"
    c = (S / "cron-plus.sh").read_text()   # scope via CRON_PACK_DIR; refuse ambiguity vs pick head -1
    assert "CRON_PACK_DIR" in c and "set CRON_PACK_DIR" in c, "cron-plus.sh lacks the disambiguation guard"


def test_cron_plus_helper_uses_pack_container():  # #19
    t = (S / "cron-plus.sh").read_text()
    assert "docker exec" in t and "docker compose exec" not in t
    assert "com.docker.compose.service=gateway" in t


def test_install_cron_plus_targets_runtime_and_reads_pin():  # #20
    assert (S / "install-cron-plus.sh").is_file()
    t = (S / "install-cron-plus.sh").read_text()
    assert ".hermes-data/plugins/cron-plus" in t
    assert "pinned_sha" in t and "dependencies.cron-plus" not in t.split("\n")[0]  # reads the manifest pin
    r = subprocess.run(["bash", "-n", str(S / "install-cron-plus.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cron_plus_pin_agrees_across_manifest_and_docs():  # #177 follow-up
    """The cron-plus commit pin lives in THREE places that must agree — the manifest (what
    ensure-runtime clones), INSTALL.md (the manual clone), and docs/supply-chain.md (the SBOM
    row). A bump in one without the others reintroduces exactly the stale-pin regression this
    guards: the manifest pointed one commit BEFORE TZ-aware scheduling, so fresh installs cloned
    a UTC-naive scheduler that silently ignored TZ. Verify all three cite the same SHA."""
    import re
    man = (REPO / "engine-manifest.yaml").read_text()
    m = re.search(r"cron-plus:.*?pinned_sha:\s*([0-9a-f]{40})", man, re.DOTALL)
    assert m, "engine-manifest.yaml: cron-plus pinned_sha (40-hex) not found"
    sha = m.group(1)
    install = (REPO / "INSTALL.md").read_text()
    assert sha in install, \
        f"INSTALL.md does not cite the manifest cron-plus pin {sha[:12]} — the clone command drifted"
    sc = (REPO / "docs" / "supply-chain.md").read_text()
    # supply-chain.md abbreviates as `<first8>…<last5>`; accept either the full or that form
    assert sha in sc or (sha[:8] in sc and sha[-5:] in sc), \
        f"docs/supply-chain.md does not cite the manifest cron-plus pin {sha[:8]}…{sha[-5:]}"


def test_deploy_persists_hermes_uid_to_env():  # uid-muddle prevention
    """deploy.sh must PIN HERMES_UID/GID into the pack's .env, else a later bare `docker compose
    up`/recreate falls back to the image default 10000, desyncs ownership from the mounted tree,
    and the cron-plus ticker dies on a .tick.lock PermissionError. Guard both the append and the
    idempotence (only when not already pinned)."""
    t = (S / "deploy.sh").read_text()
    assert 'grep -qE \'^HERMES_UID=\' "$PACK/.env"' in t, "deploy.sh no longer guards on an existing pin"
    assert "HERMES_UID=%s" in t and '>> "$PACK/.env"' in t, "deploy.sh no longer persists HERMES_UID to .env"
    # #7 regression: the persist ran only when .env ALREADY existed, but ensure-runtime creates
    # .env afterward (via >>) without the pin — so a clean deploy left .env with no HERMES_UID.
    # The fix creates .env first; guard that it's still there.
    assert '[ -f "$PACK/.env" ] || : > "$PACK/.env"' in t, \
        "deploy.sh must create .env before pinning, else a clean deploy (no .env yet) leaves no uid pin"


def test_post_deploy_verify_checks_runtime_ownership():  # uid-muddle detection (deploy-time)
    """post_deploy_verify runs at every deploy/recreate regardless of scheduler health (a dead
    ticker never runs the weekly validate lane), so the uid-desync catch must live here too:
    compare /opt/data owner to the gateway's HERMES_UID and FAIL on a mismatch."""
    t = (S / "post_deploy_verify.sh").read_text()
    assert "stat -c '%u' /opt/data/cron-plus" in t, "post_deploy_verify no longer reads the runtime-dir owner"
    assert 'echo ${HERMES_UID:-10000}' in t and 'owned by uid' in t, \
        "post_deploy_verify no longer compares runtime owner to the gateway uid"


def test_hermes_pin_agrees_across_manifest_and_supply_chain():  # audit follow-up (drift class)
    """The Hermes runtime pin (tag + sha) lives in engine-manifest.yaml and is re-stated in the SBOM
    (docs/supply-chain.md). They must agree — the invariant audit found supply-chain.md still
    documenting the PRE-bump pin (v2026.6.19) after the manifest moved to v2026.7.1. Same drift
    class as the cron-plus pin; guard both the tag and the sha."""
    import re
    man = (REPO / "engine-manifest.yaml").read_text()
    rt = re.search(r"runtime:.*?pinned_tag:\s*(\S+).*?pinned_sha:\s*([0-9a-f]{40})", man, re.DOTALL)
    assert rt, "engine-manifest.yaml runtime pinned_tag/pinned_sha not found"
    tag, sha = rt.group(1), rt.group(2)
    sc = (REPO / "docs" / "supply-chain.md").read_text()
    assert tag in sc, f"docs/supply-chain.md does not cite the manifest Hermes tag {tag} — SBOM drifted"
    assert sha[:7] in sc, f"docs/supply-chain.md does not cite the manifest Hermes sha {sha[:7]} — SBOM drifted"


def test_vault_scripts_have_no_hardcoded_operator_uid():  # invariant-audit #5/#6
    """vault-exec.sh / fix-vault-ownership.sh must resolve the vault uid from the pack .env or
    the RUNNING gateway, never a hardcoded operator uid. A fixed `:-1003` fallback runs/chowns as
    the wrong user on any other host (fix-vault-ownership would chown the whole vault to the wrong
    owner and kill every gateway write) — and bakes a personal value into the public engine."""
    for name in ("vault-exec.sh", "fix-vault-ownership.sh"):
        t = (S / name).read_text()
        assert "1003" not in t, f"{name} hardcodes the operator uid 1003"
        assert "HERMES_UID:-10000" in t, \
            f"{name} must fall back to the gateway/image default uid (10000), not a personal one"


def test_install_cron_plus_force_recovers_a_dirty_managed_clone(tmp_path):  # invariant-audit redeploy trap
    """The plugin dir is an engine-MANAGED clone; a plain `git checkout` ABORTS on any local edit —
    a hand-patched jobs.py did exactly this and killed a live redeploy at step 2. install-cron-plus
    must FORCE the tree to the pin (recover) and surface what it discarded. Functional: a local
    upstream + a dirty clone at an OLD commit + a fake manifest -> recovers to the pin, no abort."""
    import os
    import shutil
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a, cwd):
        return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True, text=True, env=genv)

    def rev(cwd):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True).stdout.strip()

    up = tmp_path / "upstream"; up.mkdir(); git("init", "-q", cwd=up)
    (up / "cli.py").write_text("# cli\n"); (up / "jobs.py").write_text("OLD\n")
    git("add", "-A", cwd=up); git("commit", "-qm", "A", cwd=up)
    old = rev(up)
    (up / "jobs.py").write_text("NEW-PINNED\n")
    git("add", "-A", cwd=up); git("commit", "-qm", "B", cwd=up)
    new = rev(up)

    eng = tmp_path / "fake-engine"; (eng / "scripts").mkdir(parents=True)
    (eng / "engine-manifest.yaml").write_text(
        f"dependencies:\n  cron-plus:\n    upstream: file://{up}\n    pinned_sha: {new}\n")
    shutil.copy(S / "install-cron-plus.sh", eng / "scripts" / "install-cron-plus.sh")

    pack = tmp_path / "pack"; dest = pack / ".hermes-data" / "plugins" / "cron-plus"
    dest.parent.mkdir(parents=True)
    git("clone", "-q", str(up), str(dest), cwd=tmp_path); git("checkout", "-q", old, cwd=dest)
    (dest / "jobs.py").write_text("HAND-PATCHED\n")           # local mod -> plain checkout would abort

    r = subprocess.run(["bash", str(eng / "scripts" / "install-cron-plus.sh"), str(pack)],
                       capture_output=True, text=True, env=genv)
    assert r.returncode == 0, f"aborted on a dirty managed clone:\n{r.stderr}"   # the bug
    assert rev(dest) == new, "did not recover to the pin"
    assert (dest / "jobs.py").read_text() == "NEW-PINNED\n", "local edit not discarded"
    assert "discarding LOCAL" in r.stderr, "must surface the discarded change, not silently drop it"


def test_cron_plus_logs_reads_container_not_host_hermes():  # invariant-audit #15
    """cron-plus-logs.sh must read the log stream from INSIDE the gateway container
    (/opt/data/logs via `docker exec`), not the host ~/.hermes/logs. okengine deployments are
    containerized (#138); the old default `${HERMES_HOME:-$HOME/.hermes}/logs` resolved to a
    nonexistent host dir and — because every tail/ls/grep was 2>/dev/null — silently emitted
    nothing, so a dead scheduler read the same as a healthy-quiet one."""
    t = (S / "cron-plus-logs.sh").read_text()
    assert "/opt/data/logs" in t, "cron-plus-logs.sh no longer reads the in-container log dir"
    assert "$HOME/.hermes" not in t and "HERMES_HOME" not in t, \
        "cron-plus-logs.sh still resolves logs on the host (~/.hermes) instead of the container"
    assert "docker exec" in t, "cron-plus-logs.sh must shell into the gateway to read the logs"
    # scoped to THIS pack's gateway on a multi-pack host, same idiom as cron-plus.sh (#108)
    assert "CRON_PACK_DIR" in t and "com.docker.compose.service=gateway" in t, \
        "cron-plus-logs.sh must scope to the pack gateway (CRON_PACK_DIR / compose label)"
    r = subprocess.run(["bash", "-n", str(S / "cron-plus-logs.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cron_plus_logs_fails_loud_on_missing_log_dir():  # invariant-audit #15
    """A missing log dir must be surfaced with an ERROR + nonzero exit, never swallowed. Point the
    helper at a bogus pack (no gateway container) — it must fail LOUDLY, not exit 0 with empty
    output (the silent-default the audit flagged)."""
    import os
    r = subprocess.run(
        ["bash", str(S / "cron-plus-logs.sh"), "tail"],
        capture_output=True, text=True,
        env={**os.environ, "CRON_PACK_DIR": "/nonexistent/pack-with-no-gateway"},
    )
    assert r.returncode != 0, "cron-plus-logs.sh exited 0 despite no gateway/log dir (silent default)"
    assert "ERROR" in r.stderr, f"no loud error on missing gateway:\nstdout={r.stdout!r}\nstderr={r.stderr!r}"


def test_dead_cron_plus_plugin_deploy_script_removed():  # invariant-audit #16
    """scripts/deploy-cron-plus-plugin.sh was a broken, wrong-surface DEAD script: it read from
    a never-vendored plugins/cron-plus and deployed to host ~/.hermes/plugins, a location the
    runtime abandoned post-#138. It must stay removed, and CLAUDE.md's deploy-surface table must
    no longer point operators at it — the real cron-plus plugin deploy is install-cron-plus.sh
    (clones the pinned external dep into <pack>/.hermes-data/plugins/cron-plus)."""
    assert not (S / "deploy-cron-plus-plugin.sh").exists(), \
        "the dead deploy-cron-plus-plugin.sh is back — it targets an unvendored source + host ~/.hermes"
    # CLAUDE.md is engine-internal and EXCLUDED from the public snapshot, so only assert its
    # deploy-surface table when it is present (the GitLab tree / a dev checkout) — never require it
    # (public CI runs on the scrubbed snapshot where CLAUDE.md does not exist).
    claude = REPO / "CLAUDE.md"
    if claude.is_file():
        claude_md = claude.read_text()
        assert "deploy-cron-plus-plugin.sh" not in claude_md, \
            "CLAUDE.md still references the removed deploy-cron-plus-plugin.sh"
        assert "install-cron-plus.sh" in claude_md, \
            "CLAUDE.md deploy surfaces must name install-cron-plus.sh as the cron-plus plugin deploy path"
    assert (S / "install-cron-plus.sh").is_file(), "the real cron-plus plugin installer is missing"


def test_config_template_enables_cron_plus():  # #20
    c = yaml.safe_load((REPO / "config" / "config.yaml.template").read_text())
    assert "cron-plus" in (c.get("plugins") or {}).get("enabled", [])


def test_deploy_installs_cron_plus_before_compose():  # #20 integration
    t = (S / "deploy.sh").read_text()
    assert "install-cron-plus.sh" in t
    # the install call must run before the compose-up STEP (the "[4/" marker —
    # an unambiguous anchor, unlike "docker compose up" which is also in the header;
    # count-agnostic so a step renumber doesn't break it)
    assert t.index("install-cron-plus.sh") < t.index("[4/")
