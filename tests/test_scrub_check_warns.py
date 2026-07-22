"""okengine#326 [17]: the pre-commit private-token scrub must WARN when .scrub-patterns is absent
(git-ignored → absent on a fresh clone), not silently no-op into a false 'clean' — the same
UNDETECTABLE≠pass posture the CI takes when its SCRUB_PATTERNS variable is missing."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_scrub_check_warns_when_patterns_absent():
    sh = (REPO / "scripts" / "scrub-check.sh").read_text(encoding="utf-8")
    assert "if [ -f .scrub-patterns ]" in sh
    # an else branch that warns loudly (not a silent skip)
    tail = sh.split("if [ -f .scrub-patterns ]", 1)[1]
    assert "else" in tail and "UNDETECTABLE" in tail, \
        "scrub-check.sh silently skips the private-token scrub when .scrub-patterns is absent (no WARN)"
