"""okengine#185: deploy scripts must resolve HERMES_UID from env -> pack .env pin -> tree owner,
never silently defaulting to 10000 when the pack pins a different owner. A wrong-uid write to
/opt/data stalls the whole cron fleet (cron-plus can't read a mis-owned jobs.json), so this guards
the resolver AND that both jobs/scripts deploy scripts actually use it."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "hermes_uid.sh"


def _resolve(pack_dir, fn="resolve_hermes_uid", env=None):
    e = dict(os.environ)
    # start from a clean slate so the harness's own HERMES_UID doesn't leak into step 1
    e.pop("HERMES_UID", None)
    e.pop("HERMES_GID", None)
    e.update(env or {})
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {fn} "{pack_dir}"'],
        capture_output=True, text=True, env=e,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _env_file_val(pack_dir, key):
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; _okengine_env_file_val "$1" "$2"', "bash",
         str(pack_dir), key],
        capture_output=True,
        text=True,
    )
    return r


def test_lib_exists_and_sources_clean():
    assert LIB.is_file(), "scripts/lib/hermes_uid.sh missing"
    r = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_explicit_env_wins(tmp_path):
    (tmp_path / ".env").write_text("HERMES_UID=1003\n")
    assert _resolve(tmp_path, env={"HERMES_UID": "4242"}) == "4242"


def test_pack_env_pin_used_over_default(tmp_path):
    """The exact bug: no HERMES_UID exported, but the pack .env pins 1003 — must NOT fall to 10000."""
    (tmp_path / ".env").write_text("SOMETHING=x\nHERMES_UID=1003\nHERMES_GID=1003\n")
    assert _resolve(tmp_path) == "1003"
    assert _resolve(tmp_path, fn="resolve_hermes_gid") == "1003"


def test_env_parser_accepts_export_quotes_whitespace_and_comments(tmp_path):
    (tmp_path / ".env").write_text(
        " export HERMES_UID = '1003' # pack owner\n"
        'OKENGINE_BRIEF_HOUR = "09" # local morning\n'
    )
    uid = _env_file_val(tmp_path, "HERMES_UID")
    hour = _env_file_val(tmp_path, "OKENGINE_BRIEF_HOUR")
    assert uid.returncode == 0 and uid.stdout == "1003"
    assert hour.returncode == 0 and hour.stdout == "09"


def test_env_parser_rejects_empty_or_malformed_values(tmp_path):
    (tmp_path / ".env").write_text("HERMES_UID='unterminated\n")
    malformed = _env_file_val(tmp_path, "HERMES_UID")
    missing = _env_file_val(tmp_path, "HERMES_GID")
    assert malformed.returncode != 0
    assert missing.returncode != 0


def test_tree_owner_used_when_no_env_pin(tmp_path):
    """No env, no .env pin -> derive from the runtime tree owner (the vault is chowned to the uid)."""
    owner = str(os.stat(tmp_path).st_uid)
    got = _resolve(tmp_path)
    # current uid is non-root in the test env; resolver returns the tree owner, not 10000
    assert got == owner, f"expected tree owner {owner}, got {got}"


def test_root_uid_is_rejected_from_env_and_dotenv(tmp_path):  # okengine#326 [9]
    """HERMES_UID/GID=0 must never be honoured — remapping the gateway to root writes a root-owned
    jobs.json the pack-uid cron runner can't read (the fleet goes dark, #185). The tree-owner tier
    already excludes root; the explicit env AND .env tiers must too, or a stray 0 short-circuits it."""
    owner, group = str(os.stat(tmp_path).st_uid), str(os.stat(tmp_path).st_gid)
    # env 0 -> rejected, falls through to the (non-root) tree owner
    assert _resolve(tmp_path, env={"HERMES_UID": "0"}) == owner
    assert _resolve(tmp_path, fn="resolve_hermes_gid", env={"HERMES_GID": "0"}) == group
    # .env 0 -> also rejected
    (tmp_path / ".env").write_text("HERMES_UID=0\nHERMES_GID=0\n")
    assert _resolve(tmp_path) == owner
    assert _resolve(tmp_path, fn="resolve_hermes_gid") == group
    # a real non-root value still wins even with a root .env present
    assert _resolve(tmp_path, env={"HERMES_UID": "1003"}) == "1003"


def test_falls_back_to_10000_only_as_last_resort(tmp_path):
    """With no env, no .env, and a root-owned-looking path we can't stat as non-root, the resolver
    still yields a value (10000) rather than empty — the warning path. We assert it never returns
    empty, which would break `docker exec -u`."""
    got = _resolve(tmp_path / "does-not-exist")
    assert got.isdigit() and got, "resolver must always yield a numeric uid"


def test_both_deploy_scripts_use_the_resolver():
    for name in ("deploy-cron-scripts.sh", "deploy-cron-plus-jobs.sh"):
        t = (REPO / "scripts" / name).read_text()
        assert "lib/hermes_uid.sh" in t, f"{name} must source the resolver"
        assert "resolve_hermes_uid" in t, f"{name} must call resolve_hermes_uid"
        assert 'HERMES_UID="${HERMES_UID:-10000}"' not in t, \
            f"{name} must not keep the silent 10000 default"


def test_cron_jobs_deploy_uses_dotenv_parser_for_brief_hour():
    text = (REPO / "scripts" / "deploy-cron-plus-jobs.sh").read_text()
    assert '_okengine_env_file_val "$PACK_DIR" OKENGINE_BRIEF_HOUR' in text
    assert "OKENGINE_BRIEF_HOUR must be" in text
    assert "grep -oE '^OKENGINE_BRIEF_HOUR=" not in text
