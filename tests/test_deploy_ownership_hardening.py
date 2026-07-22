"""okengine#351 — deploy/ownership fail-loud hardening (invariant-audit B2/B7/B8/B9).

Four deploy/ownership shell scripts silently guessed a uid/gid or hardcoded a name where a wrong
value stalls the fleet or mints foreign-owned strays. These tests pin the fail-loud contract by
reading each script's TEXT and asserting the guard is present — the same offline, live-stack-free
style as test_post_deploy_verify.py. They fail loudly if a future edit drops a guard.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "hermes_uid.sh"
FIXOWN = REPO / "scripts" / "fix-vault-ownership.sh"
VAULTEXEC = REPO / "scripts" / "vault-exec.sh"
DUMP = REPO / "scripts" / "dump-cron-plus-jobs.sh"
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"

# The libs write_server imports from the BAKED tree (must match deployment_validate._WRITE_PATH_LIBS)
WRITE_PATH_LIBS = ("schema_lib.py", "id_lib.py", "id_index.py", "okf_migrate.py")


def _func_body(text: str, name: str) -> str:
    """Return the body of a shell function `name() { ... }` up to its closing brace at col 0."""
    m = re.search(rf"^{re.escape(name)}\(\)\s*\{{", text, re.M)
    assert m, f"function {name}() not found"
    rest = text[m.end():]
    end = re.search(r"^\}", rest, re.M)
    assert end, f"no closing brace for {name}()"
    return rest[: end.start()]


# --- B7: resolve_hermes_gid must warn before its 10000 fallback (mirrors resolve_hermes_uid) -------
def test_resolve_hermes_gid_warns_before_fallback():
    body = _func_body(LIB.read_text(), "resolve_hermes_gid")
    assert "HERMES_GID unresolved" in body, "resolve_hermes_gid falls back to 10000 SILENTLY (B7)"
    assert ">&2" in body, "the gid fallback warning must go to stderr like the uid twin"
    # the uid twin still has its warning too (guard against a copy/paste that removed it)
    assert "HERMES_UID unresolved" in _func_body(LIB.read_text(), "resolve_hermes_uid")


# --- B8: fix-vault-ownership + vault-exec must resolve a SEPARATE gid, not reuse the uid ----------
def test_fix_vault_ownership_resolves_separate_gid():
    t = FIXOWN.read_text()
    assert "HERMES_GID" in t, "fix-vault-ownership.sh must resolve HERMES_GID separately (B8)"
    assert "GIDG" in t, "expected a distinct GIDG variable for the group"
    assert "chown $UIDG:$GIDG" in t, "chown must use uid:gid, not uid:uid"
    assert "chown $UIDG:$UIDG" not in t, "chown still reuses the uid as the group (B8 not fixed)"


def test_vault_exec_resolves_separate_gid():
    t = VAULTEXEC.read_text()
    assert "HERMES_GID" in t, "vault-exec.sh must resolve HERMES_GID separately (B8)"
    assert 'docker exec -u "$UIDG:$GIDG"' in t, "docker exec must run as uid:gid, not uid:uid"
    assert 'docker exec -u "$UIDG:$UIDG"' not in t, "docker exec still reuses uid as group (B8)"


# --- B9: dump-cron-plus-jobs must resolve the gateway uid, not hardcode `--user hermes` -----------
def test_dump_cron_plus_jobs_resolves_uid():
    t = DUMP.read_text()
    assert "--user hermes" not in t, "dump-cron-plus-jobs.sh still hardcodes --user hermes (B9)"
    assert "resolve_hermes_uid" in t, "must resolve the gateway uid via the shared resolver"
    assert 'lib/hermes_uid.sh' in t, "must source scripts/lib/hermes_uid.sh"
    assert '--user "$HERMES_UID"' in t, "exec must run as the resolved uid"


# --- B2: post_deploy_verify must check baked-vs-staged drift of the write-path libs ----------------
def test_post_deploy_verify_checks_write_path_lib_drift():
    t = VERIFY.read_text()
    assert "/opt/hermes/scripts/cron" in t, (
        "post_deploy_verify.sh must compare the BAKED write-path libs at /opt/hermes/scripts/cron (B2)"
    )
    assert "write-path" in t, "expected a write-path drift section marker"
    # the section must cover every _WRITE_PATH_LIBS name
    for lib in WRITE_PATH_LIBS:
        assert lib in t, f"write-path drift check omits {lib} (must mirror _WRITE_PATH_LIBS)"
    # it must be a real sha256 comparison surfaced via bad() (mirroring the 3a read-MCP check)
    assert "sha256sum /opt/hermes/scripts/cron/$lib" in t, "must sha256 the baked lib"
    assert "sha256sum /opt/data/scripts/$lib" in t, "must sha256 the staged lib"
    assert "STALE (baked vs staged)" in t, "a drift must be reported as a FAIL via bad()"
