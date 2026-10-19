"""Guard: every carried Hermes patch stays registered + well-formed.

The patches in `patches/` are re-applied on each Hermes bump by `patches/apply.sh`, which
`scripts/build-engine-image.sh` runs at image build. apply.sh picks the patch DIRECTORY and its
REGISTRY from the manifest pin (the legacy v2026.7.7.2 set lives at the root, the v2026.9.14
series in `patches/target-v2026.9.14/`). These guards resolve that selection the same way, so
they cover the artifacts the build really applies, not just whichever files sit at the root
(okengine#991 audit 1 [17]: they once pinned only the retired root set while the live series
went unguarded). They don't run a real `git apply` (needs a Hermes checkout); they pin the
bookkeeping, the hunk integrity, and the semantic content of the live artifacts.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
APPLY_SH = PATCHES / "apply.sh"

_CASE_RE = re.compile(
    r'^\s*(?P<pin>v[0-9][0-9.]*)\)\s*PATCHDIR="\$PATCHROOT(?P<dir>[^"]*)";\s*'
    r'REGISTRY="(?P<registry>[^"]+)"', re.M)
_REGISTERED_RE = re.compile(r"\b[0-9]{2}-[a-z0-9][a-z0-9-]*\.patch\b")


def _governed_sets(apply_text: str) -> dict[str, tuple[Path, Path]]:
    """pin -> (patch dir, registry), parsed from apply.sh's own selection table."""
    out = {}
    for m in _CASE_RE.finditer(apply_text):
        directory = PATCHES / m["dir"].lstrip("/")
        registry = m["registry"].replace("$PATCHDIR", str(directory)).replace("$PATCHROOT", str(PATCHES))
        out[m["pin"]] = (directory, Path(registry))
    return out


def _manifest_pin() -> str:
    """The pin apply.sh reads: the first `pinned_tag:` line of engine-manifest.yaml."""
    m = re.search(r"^\s*pinned_tag:\s*(\S+)", (REPO / "engine-manifest.yaml").read_text(), re.M)
    assert m, "engine-manifest.yaml has no pinned_tag"
    return m.group(1)


GOVERNED = _governed_sets(APPLY_SH.read_text(encoding="utf-8"))
LIVE_DIR, LIVE_REGISTRY = GOVERNED.get(_manifest_pin(), (PATCHES / "<unselected>", Path("/dev/null")))
live_patch_files = sorted(LIVE_DIR.glob("[0-9]*.patch"))
# Hunk integrity covers every patch file under patches/: each governed series AND the cron-plus
# patches install-cron-plus.sh applies. A truncation is never benign, wherever it lands.
every_patch_file = sorted(PATCHES.rglob("*.patch"))


def _live(number: str) -> str:
    """The text of the live artifact carrying original patch `number` (09 resolves 09-22-...)."""
    found = sorted(LIVE_DIR.glob(f"{number}-*.patch"))
    assert len(found) == 1, f"expected one live artifact for patch {number} in {LIVE_DIR}: {found}"
    return found[0].read_text(encoding="utf-8")


def _added(text: str, *, tests: bool = True) -> str:
    """The added lines; tests=False skips the files under tests/ (their negative fixtures)."""
    out, in_test = [], False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            in_test = " b/tests/" in line
        elif line.startswith("+") and not line.startswith("+++") and (tests or not in_test):
            out.append(line[1:])
    return "\n".join(out)


def _removed(text: str) -> str:
    return "\n".join(l[1:] for l in text.splitlines() if l.startswith("-") and not l.startswith("---"))


def test_the_build_applies_patches_through_apply_sh():
    build = (REPO / "scripts" / "build-engine-image.sh").read_text(encoding="utf-8")
    assert 'patches/apply.sh" "$WORK"' in build, \
        "build-engine-image.sh no longer applies patches via apply.sh; re-derive LIVE_DIR from it"


def test_the_manifest_pin_selects_a_governed_nonempty_patch_set():
    assert GOVERNED, "could not parse apply.sh's pin -> PATCHDIR/REGISTRY table"
    assert _manifest_pin() in GOVERNED, f"apply.sh governs no patch set for pin {_manifest_pin()}"
    assert LIVE_DIR.is_dir() and live_patch_files, f"no numbered patches in {LIVE_DIR}"


def test_integrity_checks_cover_every_artifact_the_build_applies():
    """The regression for audit 1 [17]: the live series must be inside the integrity set."""
    assert set(live_patch_files) <= set(every_patch_file)
    assert any(p.parent != PATCHES for p in live_patch_files) or LIVE_DIR == PATCHES


def test_patches_dir_present():
    assert PATCHES.is_dir(), "patches/ missing"
    assert every_patch_file, "no patches found"


@pytest.mark.parametrize("pin", sorted(GOVERNED))
def test_every_patch_is_listed_in_its_registry(pin):
    directory, registry = GOVERNED[pin]
    text = registry.read_text(encoding="utf-8")
    missing = [p.name for p in sorted(directory.glob("[0-9]*.patch")) if p.name not in text]
    assert not missing, f"patches not registered in {registry.relative_to(REPO)}: {missing}"


@pytest.mark.parametrize("pin", sorted(GOVERNED))
def test_every_registered_patch_exists_as_a_file(pin):  # invariant-audit #17
    """The DANGEROUS direction: every patch the registry names must exist on disk. Without this,
    a patch dropped by a bad rebase or a digit-dropping rename just makes apply.sh iterate fewer
    times and still exit 0 — a carried guard (e.g. the OKF write-guard) silently missing from the
    built image. (The reverse, extra-file-not-documented, is the harmless direction above.)"""
    directory, registry = GOVERNED[pin]
    registered = sorted(set(_REGISTERED_RE.findall(registry.read_text(encoding="utf-8"))))
    assert registered, f"no patches parsed from {registry} — registry format changed"
    missing = [n for n in registered if not (directory / n).is_file()]
    assert not missing, f"{registry.name} registers patch(es) with NO file on disk: {missing}"


def test_apply_sh_asserts_registered_patches_present():  # invariant-audit #17
    """apply.sh must verify the expected set (registry) before applying, not just glob what exists —
    else a missing patch bakes a partially-patched image with no failure."""
    apply = APPLY_SH.read_text(encoding="utf-8")
    assert "REGISTRY" in apply and "MISSING" in apply, \
        "apply.sh no longer checks that every registered patch is present on disk"


def test_apply_sh_globs_numbered_patches():
    apply = APPLY_SH.read_text(encoding="utf-8")
    assert "[0-9]*.patch" in apply, "apply.sh no longer globs numbered patches"


@pytest.mark.parametrize("path", every_patch_file, ids=lambda p: str(p.relative_to(PATCHES)))
def test_each_patch_is_a_wellformed_unified_diff(path):
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith(("diff --git", "--- ")), f"{path.name}: not a unified diff"
    assert re.search(r"^--- (a/|/dev/null)", text, re.M) and "+++ b/" in text, \
        f"{path.name}: missing diff headers"
    assert re.search(r"^@@ .* @@", text, re.M), f"{path.name}: no hunk header"


def test_ctx_patch_touches_the_three_sites():
    """okengine#151 2b spans run_job, the AIAgent forwarder, and the ctx resolver."""
    text = _live("06")
    for path in ("cron/scheduler.py", "run_agent.py", "agent/agent_init.py"):
        assert f"b/{path}" in text, f"live patch 06 should touch {path}"
    assert text.count("ollama_num_ctx") >= 4   # signature + forward + resolver + run_job call


def test_cron_mcp_patch_admits_only_the_jobs_servers_and_fails_closed():
    text = _live("09")
    added, removed = _added(text), _removed(text)
    assert "discover_mcp_tools()" in removed, "the broad MCP discovery call must be removed"
    assert "discover_mcp_tools(enabled_toolsets)" in added
    assert "_required_cron_mcp_surfaces" in added
    assert "refusing the full default toolset" in added
    assert "falling back to full default toolset" in removed


def test_read_only_file_patch_excludes_mutators():
    added = _added(_live("10"), tests=False)
    assert '"file_read": _ts(' in added and '["read_file", "search_files"]' in added
    assert '"file_read_exact": _ts(' in added
    assert 'tools.intersection_update({"read_file"})' in added
    assert '"write_file"' not in added and '"patch"' not in added


def test_cron_model_patch_bounds_the_per_job_iteration_limit():
    added = _added(_live("18"))
    assert 'job["max_iterations"]' in added
    assert "not 1 <= raw <= safe_default" in added and "safe_default = 90" in added
    assert "isinstance(raw, bool)" in added, "a bool must not pass as an integer cap"


def test_cron_evidence_scan_keeps_strict_rule_but_drops_taxonomy_false_positive():
    added = _added(_live("20"))
    assert 'entry[1] != "sys_prompt_override"' in added
    assert "keep that phrase blocked in raw user-authored prompts" in added.lower()


def test_http_status_policy_patch_is_bounded_and_opt_in():
    added = _added(_live("11"))
    assert "agent._http_status_policy" in added
    assert "_api_retry_loop_ceiling" in added and "_allow_status_fallback" in added
    assert 'getattr(agent, "_http_status_policy", {})' in added


def test_mcp_resource_patch_directs_file_paths_to_file_tools():
    added = _added(_live("12"))
    assert "copy the URI verbatim from this server's list_resources result" in added
    assert "use read_file for an explicit local path" in added and "get_page" in added


def test_responses_requests_are_attributed_by_deployment_and_conversation():
    added = _added(_live("21"))
    assert "X-Client-Id" in added and "OKENGINE_PACK" in added
    assert "X-Conversation-Id" in added and "session_id" in added
    assert "X-Oneshot" in added


def test_cron_plus_run_patch_is_null_safe():
    text = (PATCHES / "cron-plus" / "cli-null-next-run.patch").read_text()
    assert 'j.get("next_run_at")' in text
    assert "pending scheduler reconciliation" in text


def test_local_pool_contract_is_in_seed_template():
    text = (REPO / "config" / "config.yaml.template").read_text()
    for status, attempts in ((404, 1), (500, 2), (503, 6), (504, 1)):
        assert f"{status}: {{max_attempts: {attempts}, fallback: false}}" in text
    assert "request_timeout_seconds: 500" in text


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunk_errors(text: str) -> list[str]:
    """Every hunk whose body disagrees with its header's line counts.

    Reads a hunk the way `git apply` does: exactly the declared number of old/new lines. A body
    that runs out first is truncated (the #552 class). Diff-body lines left over after the counts
    are met are lines `git apply` would silently drop, so they are reported too; a blank line there
    is only the separator between two concatenated file diffs and is allowed.
    """
    errors = []
    lines = text.splitlines()
    i, index = 0, 0
    while i < len(lines):
        m = _HUNK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        index += 1
        want = (int(m.group(2) or 1), int(m.group(4) or 1))
        old = new = 0
        i += 1
        while i < len(lines) and (old, new) != want and (old <= want[0] and new <= want[1]):
            line = lines[i]
            if line.startswith("+"):
                new += 1
            elif line.startswith("-"):
                old += 1
            elif line.startswith(" ") or line == "":
                old, new = old + 1, new + 1
            elif not line.startswith("\\"):  # "\ No newline at end of file" carries no line
                break
            i += 1
        if (old, new) != want:
            errors.append(f"hunk {index}: header declares -{want[0]},+{want[1]} "
                          f"but body supplies -{old},+{new}")
            continue
        while i < len(lines) and not lines[i].startswith(("@@", "diff --git")):
            if lines[i] and not lines[i].startswith("\\"):
                errors.append(f"hunk {index}: line after the declared -{want[0]},+{want[1]} "
                              f"would be dropped by git apply: {lines[i][:60]!r}")
                break
            i += 1
    return errors


_GOOD = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n"


@pytest.mark.parametrize("text,expected", [
    (_GOOD, []),
    (_GOOD + "\n" + _GOOD, []),                                   # blank separator between diffs
    (_GOOD.replace(" ctx\n", ""), ["body supplies -1,+1"]),       # #552: a stripped context line
    (_GOOD + "+dropped\n", ["would be dropped by git apply"]),    # body longer than the header
    (_GOOD.replace("@@ -1,2 +1,2 @@", "@@ -1 +1 @@").replace(" ctx\n", ""), []),
    (_GOOD.replace("+new\n", "+new\n\\ No newline at end of file\n"), []),
])
def test_hunk_integrity_checker_rejects_truncated_and_overlong_hunks(text, expected):
    errors = _hunk_errors(text)
    assert len(errors) == len(expected), errors
    assert all(want in got for want, got in zip(expected, errors)), errors


@pytest.mark.parametrize("path", every_patch_file, ids=lambda p: str(p.relative_to(PATCHES)))
def test_every_hunk_body_matches_its_header_counts(path):
    """A hunk whose body is shorter than its header declares is a CORRUPT patch, not a drifted one.

    Regression for okengine#552. patches/20 lost its final line -- a context line consisting of a
    single space, standing for a blank line in the source -- almost certainly to trailing-whitespace
    stripping. The header still declared `@@ -73,12 +73,14 @@` while the body supplied 11/13, so
    `git apply` failed with "corrupt patch at line 22".

    That reached main because nothing here counted hunk lines, and apply.sh reports such a failure
    as "Hermes drift from <pin>; rebase this patch" -- so a truncated patch is indistinguishable
    from a genuine upstream drift. Trailing whitespace is load-bearing in a patch file; this test
    is what makes stripping it loud, for every artifact the build applies.
    """
    errors = _hunk_errors(path.read_text(encoding="utf-8"))
    assert not errors, f"corrupt patch {path.relative_to(REPO)}:\n  " + "\n  ".join(errors)


@pytest.mark.parametrize("path", every_patch_file, ids=lambda p: str(p.relative_to(PATCHES)))
def test_no_patch_ends_without_a_trailing_newline(path):
    """A patch whose last line lacks its newline is the same truncation class as #552."""
    data = path.read_bytes()
    assert not data or data.endswith(b"\n"), f"{path.relative_to(REPO)} lacks a trailing newline"
