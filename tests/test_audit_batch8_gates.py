"""invariant-audit v0.11.5 batch-8 — static gates for the CI / publish / scrub findings."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_ci_hard_fails_on_mcp_install_and_verifies_import():  # invariant-audit #23
    """A failed mcp install must fail the CI job, not `|| echo` into green — else the 5 mcp-gated
    modules (incl. the Bearer-token 401 boundary) silently skip. And the import must be verified."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert 'okengine-mcp/requirements.txt || echo' not in ci, "mcp install still soft-fails with || echo"
    assert 'python -c "import mcp"' in ci, "CI must verify mcp actually imported"


def test_scrub_check_scans_whole_tracked_tree_not_a_glob_subset():  # invariant-audit #55
    """The pre-commit scrub must scan the whole tracked tree (like the publish scrub), or shipped
    non-glob files (static/*.js, Dockerfiles, patches/*.patch) go unscanned at commit time."""
    sh = (REPO / "scripts" / "scrub-check.sh").read_text()
    assert "GLOBS=(" not in sh, "scrub-check still restricts to a glob subset"
    # excludes the internal-only docs publish also excludes (they reference the dev remote by design)
    assert "':!docs/release-checklist.md'" in sh and "':!CLAUDE.md'" in sh


def test_publish_test_gate_matches_public_ci_no_ignore():  # invariant-audit #58
    """Publish step 6b must run the full suite exactly as public CI (no --ignore), or a test added to
    the ignored file that reads a publish-EXCLUDED file passes the gate and only reds in CI."""
    import pytest
    sh_path = REPO / "scripts" / "publish-snapshot.sh"
    if not sh_path.is_file():
        pytest.skip("publish-snapshot.sh is excluded from the public snapshot")  # runs in the source repo
    sh = sh_path.read_text()
    assert "--ignore=tests/test_post_deploy_verify.py" not in sh, \
        "publish 6b still diverges from public CI via --ignore"


def test_makefile_publish_snapshot_target_guards_missing_script():  # invariant-audit #59
    """The Makefile ships public but publish-snapshot.sh does not — the target must guard so a public
    clone gets an intentional message, not a bash file-not-found (exit 127)."""
    mk = (REPO / "Makefile").read_text()
    seg = mk[mk.index("publish-snapshot:"):]
    seg = seg[:seg.index("\n\n")] if "\n\n" in seg else seg
    assert "-f scripts/publish-snapshot.sh" in seg, "publish-snapshot target does not guard the missing script"
