"""Engine shell scripts must not depend on non-POSIX tools for control flow (#493).

`run_backfill_qualification_matrix.sh` decided whether a lane had produced a valid
receipt with `! rg -q <pattern> "$log"`. On the operator host `rg` is an interactive
shell FUNCTION, not a binary, and a script run as `bash script.sh` inherits no
interactive functions -- so every call exited "command not found", matched nothing,
and the negated test read that empty result as proof of a bad receipt.

29 lanes that had each logged `{'selected': 1, 'accepted': 1, 'undisposed': 0}` were
reported as 29 failures. The runs were fine; the verifier could not read them.

That is the same trap CLAUDE.md records under "an empty parse is `unknown`, never
`nothing happened`" -- a telemetry parser matching nothing, read by a consumer as
proof that nothing happened, rejecting real writes.

Two rules, because the fix has two halves:
  * do not invoke a tool that may not exist (`rg`, `fd`, `sd`, `jq` ...) -- prefer the
    POSIX equivalent, which cannot be missing;
  * naming such a tool in a required-tools preflight list is FINE, because that
    reports absence loudly instead of silently mis-deciding.
"""
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent

# Tools that are commonly absent, or shell functions/aliases rather than binaries.
# `rg` is the one that actually bit; the rest are the same hazard class.
FRAGILE_TOOLS = ("rg", "fd", "sd", "exa", "bat", "ag")

# `tool` in COMMAND position: start of line, after a pipe/semicolon/&&/||/(, or
# after a negation. Deliberately does not match a bare mention such as the
# `for t in git rg docker gitleaks` presence check in scripts/preflight.sh.
_COMMAND_POSITION = r"(?:^|[|;&(]|\|\||&&|!)\s*{tool}\s"


def _scripts() -> list[Path]:
    return sorted(p for p in (REPO / "scripts").rglob("*.sh") if p.is_file())


def test_there_are_scripts_to_check():
    """Guard against a vacuous pass if the layout moves."""
    assert _scripts(), "no shell scripts found under scripts/ — this test would pass vacuously"


@pytest.mark.parametrize("tool", FRAGILE_TOOLS)
def test_no_script_invokes_a_fragile_tool(tool):
    pattern = re.compile(_COMMAND_POSITION.format(tool=re.escape(tool)), re.MULTILINE)
    offenders = []
    for path in _scripts():
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, (
        f"{tool!r} is invoked as a command; it may be absent or be a shell "
        "function that a non-interactive script cannot see. Use the POSIX "
        "equivalent (grep -E / find). Offenders:\n  " + "\n  ".join(offenders))


def test_qualification_matrix_verdict_uses_a_posix_matcher():
    """The specific regression: the receipt verdict must not ride on `rg`."""
    text = (REPO / "scripts" / "run_backfill_qualification_matrix.sh").read_text(
        encoding="utf-8")
    assert "grep -Eq" in text, "the receipt verdict must use a POSIX matcher"
    verdict = [ln for ln in text.splitlines()
               if "verified completion receipt:" in ln and "selected" in ln]
    assert verdict, "could not locate the receipt verdict line"


def test_qualification_matrix_separates_unverifiable_from_failed():
    """An unreadable log must be UNKNOWN, not silently a failed lane."""
    text = (REPO / "scripts" / "run_backfill_qualification_matrix.sh").read_text(
        encoding="utf-8")
    assert "UNVERIFIABLE" in text, (
        "a lane whose log cannot be read must report UNVERIFIABLE rather than "
        "reusing the did-not-prove failure path — 'no data' is not 'data says no'")
    assert "-s \"$log\"" in text or '! -s "$log"' in text, (
        "the script must test that the log is non-empty before judging its contents")


def test_error_printf_emits_a_real_newline():
    r"""The failure line used '\\n', which prints a literal \n and glues output together."""
    text = (REPO / "scripts" / "run_backfill_qualification_matrix.sh").read_text(
        encoding="utf-8")
    assert "did not prove one accepted, fully disposed receipt\\\\n" not in text, (
        r"printf format uses \\n, which emits a literal backslash-n and runs the "
        "next lane header onto the same line")
