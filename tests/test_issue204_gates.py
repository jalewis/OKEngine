"""okengine#204 P0 — release gates must not silent-skip / must have conventional exit codes.

Offline contract tests (no live stack, no docker):
- The smoke harness FAILS the rendered-DOM layer in RELEASE mode instead of skipping it, so
  `make smoke-e2e` can't be green with the DOM assertions silently omitted (gap 1).
- The domain-leak gate has conventional exit semantics (0=clean, 1=leak) via `scripts/scrub-check.sh`
  + `make scrub`, wired into `make check` (gap 6)."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SMOKE_SH = REPO / "tests" / "e2e" / "smoke" / "smoke-e2e.sh"
RENDER = REPO / "tests" / "e2e" / "smoke" / "test_smoke_render.py"
SCRUB = REPO / "scripts" / "scrub-check.sh"
MAKEFILE = REPO / "Makefile"


# ── gap 1: the rendered-DOM smoke layer is MANDATORY in release mode ──────────────────────────────

def test_render_layer_is_mandatory_in_release_mode():
    src = RENDER.read_text()
    assert "SMOKE_REQUIRE_DOM" in src, "render layer no longer honors the release-mode flag"
    # release mode does a HARD import (fails on absence), NOT importorskip
    assert "if _REQUIRE_DOM:" in src and "import playwright.sync_api" in src, \
        "release mode must hard-import playwright, not importorskip it"
    # unavailability (no chrome / unreachable cockpit) FAILS in release mode, skips only in dev
    assert "def _unavailable" in src and "pytest.fail(" in src, \
        "an unavailable DOM layer must FAIL (not skip) in release mode"


def test_smoke_script_preflights_and_gates_both_layers():
    src = SMOKE_SH.read_text()
    assert "SMOKE_REQUIRE_DOM" in src
    assert "import playwright.sync_api" in src and "exit 3" in src, \
        "release mode must preflight playwright and hard-fail before building the stack"
    # both layers run and BOTH gate the exit code (reported separately)
    assert "test_smoke_curl.py" in src and "test_smoke_render.py" in src
    assert "http_rc" in src and "dom_rc" in src, "the two layers must report/gate separately"
    # dev mode: a DOM layer that collected NOTHING (pytest exit 5, playwright absent) is a SKIP, not
    # a failure — the HTTP layer still gates. Release mode already hard-fails at the preflight above,
    # so exit 5 is only reachable in dev. (Guards the fix for the spurious dev-mode smoke failure.)
    assert "dom_fatal" in src and '"$dom_rc" = 5' in src, \
        "dev-mode DOM skip (pytest exit 5) must be tolerated, not treated as a smoke failure"


# ── gap 6: the domain-leak gate has conventional exit codes ───────────────────────────────────────

def test_scrub_check_exits_zero_on_the_clean_tree():
    """git grep returns 1 on NO match (clean) — the wrapper must invert that to a conventional exit 0,
    proving it won't abort a `set -e` script on success."""
    r = subprocess.run(["bash", str(SCRUB)], capture_output=True, text=True)
    assert r.returncode == 0, f"scrub-check should exit 0 on a clean tree:\n{r.stdout}\n{r.stderr}"
    assert "clean" in r.stdout


def test_make_check_includes_the_scrub_gate():
    mk = MAKEFILE.read_text()
    assert "\nscrub:" in mk, "Makefile lost the scrub target"
    m = re.search(r"^check:\s*(.*)$", mk, re.M)
    assert m and "scrub" in m.group(1).split("#")[0], "make check must run the scrub gate"


def test_scrub_check_is_executable_and_not_set_e():
    assert SCRUB.stat().st_mode & 0o111, "scrub-check.sh should be executable"
    # must NOT use `set -e` (a clean `git grep` exit 1 would abort it) — this is the whole bug
    body = SCRUB.read_text()
    assert "set -uo pipefail" in body and "set -euo pipefail" not in body, \
        "scrub-check must not use `set -e` — a clean git grep returns 1 and would abort the gate"


# ── P1: preflight + enforced allowed-skip policy ──────────────────────────────────────────────────

def test_preflight_and_test_release_targets_exist():
    mk = MAKEFILE.read_text()
    assert "\npreflight:" in mk and "\ntest-release:" in mk, "Makefile lost the preflight/test-release gates"
    assert (REPO / "scripts" / "preflight.sh").exists()
    assert (REPO / "scripts" / "check-test-skips.py").exists()


def test_skip_policy_forbids_missing_dep_allows_environmental():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cts", REPO / "scripts" / "check-test-skips.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    fb = m.FORBIDDEN_RE
    # missing-dependency skips -> FORBIDDEN (release env incomplete)
    assert fb.search("could not import 'mcp': No module named 'mcp'")
    assert fb.search("No module named 'croniter'")
    assert fb.search("requires the fastapi package")
    # genuinely-environmental skips -> ALLOWED (not a missing dependency)
    assert not fb.search("okengine-cockpit/Dockerfile does not download IWE")
    assert not fb.search("smoke cockpit not reachable at http://… — run smoke-e2e.sh")
    assert not fb.search("cron-plus-jobs.json not present")
    assert not fb.search("bash not available")


def test_preflight_is_valid_bash_and_executable():
    pf = REPO / "scripts" / "preflight.sh"
    assert pf.stat().st_mode & 0o111
    import shutil
    import subprocess
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", str(pf)]).returncode == 0
