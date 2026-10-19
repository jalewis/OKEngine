"""The engine repo never tracks a vault's runtime directory (okengine#847).

`.okengine/` is where a VAULT keeps its runtime state: effective policy, connector checkpoints,
connector health. The engine repo is not a vault, but a connector run from a checkout with
WIKI_PATH unset resolves the vault to "." and writes there. One such file,
`.okengine/connectors/health/reference.ror-organizations.json`, was committed by accident in
#314 and swept into another commit in #351. After that, every run re-dirtied a tracked file,
and a dirty tree is where unrelated work hides (#493).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def test_nothing_under_the_vault_runtime_directory_is_tracked():
    listing = _git("ls-files", "--", ".okengine")
    assert listing.returncode == 0, listing.stderr
    tracked = listing.stdout.split()
    assert tracked == [], (
        f"runtime state is tracked in the engine repo: {tracked}. `.okengine/` belongs to a "
        f"vault; remove it with `git rm --cached` and make sure .gitignore covers it")


def test_connector_runtime_output_is_ignored():
    """A connector run from a checkout writes these; they must never show up as changes."""
    for path in (".okengine/connectors/health/reference.ror-organizations.json",
                 ".okengine/connectors/state/any-connector.json"):
        assert _git("check-ignore", "-q", path).returncode == 0, (
            f"{path} is not ignored, so a local connector run would dirty the tree")
