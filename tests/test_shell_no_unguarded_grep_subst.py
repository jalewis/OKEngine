"""Class gate: no unguarded $(grep …) command substitution in a `set -e` shell script.

Under `set -euo pipefail`, a command substitution whose pipeline ends in a no-match grep exits 1
and kills the script INSTANTLY and SILENTLY at the assignment — rc masked by any downstream pipe.
This class has now bitten five times (ensure-runtime manifest parse, vault-exec v1, a find/ls glob
in deploy-cron-scripts, and deploy.sh's HERMES_UID resolution — the last one made the documented
paste-block install die instantly for any fresh .env with no pinned uid, while every internal
deploy sailed through because THEIR .envs pinned one). The standing rule (also in the deploy-script
comments): a substitution whose match may legitimately be absent carries `|| true`.

Scans every tracked scripts/**.sh that sets -e for `=$(grep …)` / `="$(grep …)` substitutions
lacking an `|| true` / `|| echo` / `|| :` guard on the same line. Conservative on purpose: bare
`if grep …` conditions and guarded substitutions don't match.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_SET_E = re.compile(r"^\s*set\s+-[a-z]*e", re.M)
_SUBST = re.compile(r'=\"?\$\(\s*grep\b')
_GUARD = re.compile(r"\|\|\s*(true|echo|:)")


def test_no_unguarded_grep_substitution_in_set_e_scripts():
    files = subprocess.run(["git", "ls-files", "scripts/*.sh", "scripts/**/*.sh"],
                           cwd=REPO, capture_output=True, text=True).stdout.split()
    assert files, "git ls-files returned no shell scripts — scanner is broken, not clean"
    offenders = []
    for rel in files:
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        if not _SET_E.search(text):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _SUBST.search(line) and not _GUARD.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "unguarded $(grep …) substitution(s) in set -e scripts — a no-match kills the script "
        "silently; append `|| true` inside the substitution:\n  " + "\n  ".join(offenders))
