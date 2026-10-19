"""No tracked non-test Python file compiles with a SyntaxWarning.

An invalid escape such as "\\`" in a normal string is a SyntaxWarning on Python 3.12+ and is slated to
become a SyntaxError, at which point the file stops importing. ci/runners/mint_runner.py shipped one
(in a docstring) and it surfaced only as noise in a full-suite warnings summary. Tests are exempt:
three deliberately compare with `is not` against a str literal to prove the code under test uses
`==`, and say so with a noqa.
"""
from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_tracked_source_file_compiles_with_a_syntax_warning():
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, check=True,
                           capture_output=True, text=True).stdout.split()
    assert files, "git ls-files listed no Python files: the scan would pass vacuously"
    offenders = []
    for path in files:
        if path.startswith("tests/"):
            continue
        source = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(source, path, "exec")
        offenders += [f"{path}:{w.lineno}: {w.message}" for w in caught
                      if issubclass(w.category, SyntaxWarning)]
    assert offenders == [], "fix these before they become SyntaxErrors:\n" + "\n".join(offenders)
