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
    assert "REGISTRY" in apply and "MISSING" in apply, \
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
    text = ((PATCHES / "09-cron-scoped-mcp-init.patch").read_text()
            + (PATCHES / "22-cron-mcp-surface-enforcement.patch").read_text())
    assert "_discover_cron_mcp_tools" in text
    assert "if name in allowed" in text
    assert "discover_mcp_tools()" in text  # removed broad call is visible in the diff
    assert "_require_explicit_cron_tool_surface" in text
    assert "enabled_toolsets=null" in text
    assert "_validate_cron_mcp_surface" in text
    assert "zero canonical MCP tools were offered" in text
    assert "tool surface" in text


def test_read_only_file_patch_excludes_mutators():
    text = (PATCHES / "10-read-only-file-toolset.patch").read_text()
    assert '"file_read"' in text
    assert '"tools": ["read_file", "search_files"]' in text
    assert '"file_read_exact"' in text
    assert '"tools": ["read_file"]' in text
    added = "\n".join(line for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    assert '"write_file"' not in added and '"patch"' not in added


def test_cron_model_patch_forwards_per_job_iteration_limit():
    text = (PATCHES / "18-cron-max-iterations.patch").read_text()
    assert 'job.get("max_iterations")' in text


def test_cron_evidence_scan_keeps_strict_rule_but_drops_taxonomy_false_positive():
    text = (PATCHES / "20-cron-evidence-scan-context.patch").read_text()
    removed = "\n".join(
        line[1:] for line in text.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    added = "\n".join(
        line[1:] for line in text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    assert "sys_prompt_override" in removed
    assert "sys_prompt_override" not in added
    assert "same phrase remains" in added.lower()


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
    assert "request_timeout_seconds: 500" in text


def test_qwen_pool_requests_are_attributed_by_deployment_and_conversation():
    text = (PATCHES / "21-qwen-pool-attribution.patch").read_text()
    assert "X-Client-Id" in text and "OKENGINE_PACK" in text
    assert "X-Conversation-Id" in text and "session_id" in text
    assert "X-Oneshot" in text
    assert 'api_kwargs["extra_headers"]' in text


def _hunks(text):
    """Yield (header_old, header_new, body_old, body_new) per hunk of a unified diff."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
        if not m:
            i += 1
            continue
        header_old, header_new = int(m.group(2) or 1), int(m.group(4) or 1)
        i += 1
        context = removed = added = 0
        while i < len(lines) and not lines[i].startswith(("@@", "diff --git")):
            line = lines[i]
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
            elif line.startswith(" ") or line == "":
                context += 1
            elif line.startswith("\\"):  # "\ No newline at end of file"
                pass
            else:
                break
            i += 1
        yield header_old, header_new, context + removed, context + added


def test_every_hunk_body_matches_its_header_counts():
    """A hunk whose body is shorter than its header declares is a CORRUPT patch, not a drifted one.

    Regression for okengine#552. patches/20 lost its final line -- a context line consisting of a
    single space, standing for a blank line in the source -- almost certainly to trailing-whitespace
    stripping. The header still declared `@@ -73,12 +73,14 @@` while the body supplied 11/13, so
    `git apply` failed with "corrupt patch at line 22".

    That reached main because nothing here counts hunk lines, and apply.sh reports EVERY failure as
    "Hermes drift from <pin>; rebase this patch" -- so a truncated patch is indistinguishable from a
    genuine upstream drift, and the fleet could not be rebuilt at all until someone read the bytes.
    Trailing whitespace is load-bearing in a patch file; this test is what makes stripping it loud.
    """
    broken = []
    for name in patch_files:
        text = (PATCHES / name).read_text(encoding="utf-8")
        for index, (h_old, h_new, b_old, b_new) in enumerate(_hunks(text), 1):
            if (h_old, h_new) != (b_old, b_new):
                broken.append(
                    f"{name} hunk {index}: header declares -{h_old},+{h_new} "
                    f"but body supplies -{b_old},+{b_new}"
                )
    assert not broken, "corrupt patch hunk(s):\n  " + "\n  ".join(broken)


def test_no_patch_ends_without_a_trailing_newline():
    """A patch whose last line lacks its newline is the same truncation class as #552."""
    naked = [n for n in patch_files
             if (PATCHES / n).read_bytes() and not (PATCHES / n).read_bytes().endswith(b"\n")]
    assert not naked, f"patch files missing a trailing newline (truncation risk): {naked}"
