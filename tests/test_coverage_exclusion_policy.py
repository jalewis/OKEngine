"""A 100% floor with an unbounded exemption is a 100% floor with an unbounded exemption.

The repo holds 100% statement and branch coverage across 222 measured files, enforced by
`fail_under = 100` plus a per-file branch ratchet. Every `# pragma: no cover` is a line the floor
does not apply to, so the pragma count is the size of the hole in the number — and nothing was
bounding it or asking why. okengine#466's last unmet acceptance item: *"`exclude_lines` defined,
documented, and pragma-growth guarded"*.

Two rules, and the second is the one that matters:

1. **The count may fall, never rise** without a deliberate edit here. A ratchet, like the coverage
   floor it protects.
2. **Every pragma states its reason inline.** A count alone lets 35 unexplained exemptions sit at
   35 forever; a required justification makes the author answer "why can this not be tested?" at
   the moment they exempt it, which is the only moment anyone knows the answer.

Every one of the 35 today is an optional-import or C-extension fallback — `libyaml` absent from a
minimal PyYAML build, a runtime dep missing in a host test env. That is a legitimate class. A
pragma on a business branch is not, and this file is where that argument has to be had.
"""
import re
from pathlib import Path

import pytest
import tomllib

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

# The ratchet. Lower it when pragmas are removed; raising it is a deliberate act that shows up in
# review as a change to this line, with the reason in the commit message.
PRAGMA_BUDGET = 35

_PRAGMA = re.compile(r"#\s*pragma:\s*no cover(?P<tail>[^\n]*)")
_REASON = re.compile(r"^\s*[-—]\s*\S")


def measured_roots() -> list[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["coverage"]["run"]["source"]


def pragmas() -> list[tuple[Path, int, str]]:
    """Every `# pragma: no cover` inside a tree the coverage floor actually measures."""
    found: list[tuple[Path, int, str]] = []
    for root in measured_roots():
        base = REPO / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = _PRAGMA.search(line)
                if match:
                    found.append((path.relative_to(REPO), number, match.group("tail")))
    return found


def test_the_pragma_count_does_not_grow_without_a_deliberate_edit():
    """Each one is a line the 100% floor does not apply to."""
    found = pragmas()
    assert len(found) <= PRAGMA_BUDGET, (
        f"{len(found)} `pragma: no cover` in measured code, budget {PRAGMA_BUDGET}. Each is an "
        f"exemption from the coverage floor. Test the line, or raise PRAGMA_BUDGET here and say "
        f"why in the commit: " + ", ".join(f"{p}:{n}" for p, n, _ in found[PRAGMA_BUDGET:])
    )


def test_the_budget_tracks_reality_rather_than_drifting_above_it():
    """A budget far above the count silently permits growth it was never asked about."""
    count = len(pragmas())
    assert count >= PRAGMA_BUDGET - 5, (
        f"only {count} pragmas remain against a budget of {PRAGMA_BUDGET} — lower the budget to "
        f"{count} so the ratchet keeps biting"
    )


def test_every_pragma_states_why_the_line_cannot_be_covered():
    """A count alone lets 35 unexplained exemptions sit at 35 forever. The reason has to be
    written when the author still knows it."""
    unexplained = [(p, n) for p, n, tail in pragmas() if not _REASON.match(tail)]
    assert not unexplained, (
        "these exempt a line from the coverage floor without saying why — write the reason after "
        "the pragma (`# pragma: no cover - libyaml absent in a minimal PyYAML build`): "
        + ", ".join(f"{p}:{n}" for p, n in unexplained)
    )


def test_the_reason_check_accepts_a_real_one_and_rejects_a_bare_pragma():
    """A green red-check is the tell."""
    assert _REASON.match(" - yaml is a runtime dep")
    assert _REASON.match(" — libyaml absent")
    assert not _REASON.match("")
    assert not _REASON.match("  ")
    assert not _REASON.match(" - ")


def test_the_configured_exclusions_are_documented():
    """`exclude_also` is the other half of the hole, and it is a much bigger lever than a pragma:
    one pattern silently exempts every matching line in the repo."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    patterns = config["tool"]["coverage"]["report"].get("exclude_also", [])
    assert patterns, "exclude_also must exist so this check is not vacuous"
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text.split("exclude_also")[0]
    assert "#" in block.rsplit("\n\n", 1)[-1], (
        "exclude_also must carry a comment explaining each pattern — an undocumented global "
        "exclusion is the largest untracked exemption in the config"
    )


def test_the_floor_itself_has_not_been_lowered():
    """The thing all of the above protects."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert config["tool"]["coverage"]["report"]["fail_under"] == 100
    assert config["tool"]["okengine"]["coverage"]["branch_fail_under"] == 100
