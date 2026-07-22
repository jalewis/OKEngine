"""Guard: every carried Hermes patch stays registered + well-formed.

The patches in `patches/` are re-applied on each Hermes bump by `patches/apply.sh`. A patch
added to the dir but not documented (README) — or vice versa — is a silent drift hazard. These
checks don't run a real `git apply` (needs a Hermes checkout), they pin the bookkeeping:
the file set, the README table, and that each patch is a syntactically plausible unified diff.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"

patch_files = sorted(p.name for p in PATCHES.glob("[0-9]*.patch")) if PATCHES.is_dir() else []


def test_patches_dir_present():
    assert PATCHES.is_dir(), "patches/ missing"
    assert patch_files, "no numbered patches found"


def test_every_patch_is_listed_in_readme():
    readme = (PATCHES / "README.md").read_text(encoding="utf-8")
    missing = [n for n in patch_files if n not in readme]
    assert not missing, f"patches not documented in patches/README.md: {missing}"


def test_every_readme_patch_exists_as_a_file():  # invariant-audit #17
    """The DANGEROUS direction: every patch the README registers must exist on disk. Without this,
    a patch dropped by a bad rebase or a digit-dropping rename just makes apply.sh iterate fewer
    times and still exit 0 — a carried guard (e.g. the OKF write-guard) silently missing from the
    built image. (The reverse, extra-file-not-documented, is the harmless direction above.)"""
    readme = (PATCHES / "README.md").read_text(encoding="utf-8")
    registered = sorted(set(re.findall(r"\b[0-9]{2}-[a-z0-9][a-z0-9-]*\.patch\b", readme)))
    assert registered, "no patches parsed from patches/README.md — registry format changed"
    missing = [n for n in registered if not (PATCHES / n).is_file()]
    assert not missing, f"README registers patch(es) with NO file on disk (dropped/renamed?): {missing}"


def test_apply_sh_asserts_registered_patches_present():  # invariant-audit #17
    """apply.sh must verify the expected set (README) before applying, not just glob what exists —
    else a missing patch bakes a partially-patched image with no failure."""
    apply = (PATCHES / "apply.sh").read_text(encoding="utf-8")
    assert "README.md" in apply and "MISSING" in apply, \
        "apply.sh no longer checks that every README-registered patch is present on disk"


def test_apply_sh_globs_numbered_patches():
    apply = (PATCHES / "apply.sh").read_text(encoding="utf-8")
    assert "[0-9]*.patch" in apply, "apply.sh no longer globs numbered patches"


def test_each_patch_is_a_wellformed_unified_diff():
    for n in patch_files:
        text = (PATCHES / n).read_text(encoding="utf-8")
        assert text.lstrip().startswith("diff --git"), f"{n}: not a git diff"
        assert "--- a/" in text and "+++ b/" in text, f"{n}: missing diff headers"
        assert re.search(r"^@@ .* @@", text, re.M), f"{n}: no hunk header"


def test_ctx_patch_touches_the_three_sites():
    """okengine#151 2b spans run_job, the AIAgent forwarder, and the ctx resolver.

    The ctx patch was RENUMBERED 07 -> 06 (commit e998bf1); this test still named the old file and
    `return`ed early when it was absent, so it silently asserted nothing (invariant-audit #17). Now
    it resolves the ctx patch from the registry (by content, rename-proof) and REQUIRES it."""
    ctx = [p for p in PATCHES.glob("[0-9]*-cron-per-job-ollama-num-ctx.patch")]
    assert ctx, "the per-job ollama_num_ctx patch (okengine#151) is missing from patches/"
    text = ctx[0].read_text(encoding="utf-8")
    for path in ("cron/scheduler.py", "run_agent.py", "agent/agent_init.py"):
        assert f"b/{path}" in text, f"{ctx[0].name} should touch {path}"
    assert text.count("ollama_num_ctx") >= 4   # signature + forward + resolver + run_job call


def test_cron_mcp_patch_filters_before_connecting():
    text = (PATCHES / "09-cron-scoped-mcp-init.patch").read_text()
    assert "_discover_cron_mcp_tools" in text
    assert "if name in allowed" in text
    assert "discover_mcp_tools()" in text  # removed broad call is visible in the diff


def test_read_only_file_patch_excludes_mutators():
    text = (PATCHES / "10-read-only-file-toolset.patch").read_text()
    assert '"file_read"' in text
    assert '"tools": ["read_file", "search_files"]' in text
    added = "\n".join(line for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    assert '"write_file"' not in added and '"patch"' not in added


def test_http_status_policy_patch_is_bounded_and_opt_in():
    text = (PATCHES / "11-http-status-retry-policy.patch").read_text()
    assert 'http_status_policy' in text
    assert 'max_attempts' in text and '_api_retry_loop_ceiling' in text
    assert '_allow_status_fallback' in text
    assert 'getattr(agent, "_http_status_policy", {})' in text


def test_mcp_resource_patch_directs_file_paths_to_file_tools():
    text = (PATCHES / "12-mcp-resource-uri-guidance.patch").read_text()
    assert "returned verbatim by list_resources" in text
    assert "Do not retry read_resource with this file URI" in text
    assert "use read_file" in text and "get_page" in text


def test_cron_plus_run_patch_is_null_safe():
    text = (PATCHES / "cron-plus" / "cli-null-next-run.patch").read_text()
    assert 'j.get("next_run_at")' in text
    assert "pending scheduler reconciliation" in text


def test_local_pool_contract_is_in_seed_template():
    text = (REPO / "config" / "config.yaml.template").read_text()
    for status, attempts in ((404, 1), (500, 2), (503, 6), (504, 1)):
        assert f"{status}: {{max_attempts: {attempts}, fallback: false}}" in text
    assert "request_timeout_seconds: 350" in text
