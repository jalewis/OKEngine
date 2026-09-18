"""The image overlay must never bake a gitignored artifact (okengine#511).

`build-engine-image.sh` copies the WORKING TREE into the Hermes build context, while the
provenance label's dirty suffix comes from `git status --porcelain` -- which does NOT report
ignored files. So a checkout holding generated artifacts built as "clean at commit X" while
shipping content present in no commit.

Observed: `config/cron-plus-jobs.json` (a cron_pack_split.py output, git-ignored) baked with
132 jobs, 79 of them pack-specific, into a domain-agnostic engine image; a build of the SAME
commit from another checkout baked a 53-job version of the same path. Two images, identical
clean provenance, different contents.

Tracked-but-modified files are still baked on purpose -- that is the local iteration path and
the label already reports it as `X-dirty`. Only ignored paths are dropped.

The guard is extracted from the shipped script and executed, so this exercises the real bytes
rather than a paraphrase (same approach as tests/cron/test_receipt_nowork_exemption.py).
"""
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "build-engine-image.sh"


BEGIN = "# --- BEGIN ignored-artifact sweep"
END = "# --- END ignored-artifact sweep ---"


def _guard_snippet() -> str:
    """The ignored-artifact sweep, lifted verbatim from the build script.

    Delimited by sentinels rather than inferred: an earlier draft looked for the
    closing `fi` and stopped inside the `case`, producing a snippet that would not
    parse -- a test harness bug that looked exactly like a bug in the guard.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert BEGIN in text and END in text, (
        "the sweep sentinels are missing from build-engine-image.sh; this test extracts "
        "the shipped bytes and cannot run without them")
    body = text.split(BEGIN, 1)[1].split(END, 1)[0]
    # drop the remainder of the BEGIN comment line
    return body.split("\n", 1)[1]


def test_the_script_still_contains_the_guard():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ls-files --others --ignored --exclude-standard" in text, (
        "the overlay must enumerate gitignored paths in order to drop them")
    assert "overlaid_prefixes=" in text, "the sweep must be scoped to the overlaid trees"


def test_guard_is_scoped_to_overlaid_trees_only():
    """Unscoped removal could delete an unrelated Hermes file of the same name."""
    snippet = _guard_snippet()
    for prefix in ("okengine-mcp/", "okengine-reader/", "scripts/", "config/"):
        assert prefix in snippet, f"{prefix} is overlaid but not swept"


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_ignored_artifact_is_dropped_and_tracked_edit_is_kept(tmp_path):
    engine = tmp_path / "engine"
    work = tmp_path / "work"
    for d in ((engine / "scripts"), (engine / "config"), (work / "scripts"), (work / "config")):
        d.mkdir(parents=True)

    (engine / ".gitignore").write_text("config/generated.json\n")
    (engine / "scripts" / "real.sh").write_text("echo tracked\n")
    (engine / "config" / "base.yaml").write_text("k: v\n")
    subprocess.run(["git", "init", "-q"], cwd=engine, check=True)
    subprocess.run(["git", "add", "-A"], cwd=engine, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=engine, check=True)

    # a gitignored generated artifact, and a tracked file edited but not committed
    (engine / "config" / "generated.json").write_text('{"jobs": 132}')
    (engine / "scripts" / "real.sh").write_text("echo tracked but MODIFIED\n")

    # emulate the overlay: the whole working tree is copied
    for rel in ("scripts/real.sh", "config/base.yaml", "config/generated.json"):
        (work / rel).write_bytes((engine / rel).read_bytes())
    assert (work / "config" / "generated.json").is_file(), "precondition: artifact was copied"

    program = f'set -uo pipefail\nENGINE_DIR="{engine}"\nWORK="{work}"\n' + _guard_snippet()
    done = subprocess.run(["bash", "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    assert not (work / "config" / "generated.json").exists(), (
        "the gitignored artifact must not survive into the build context")
    assert (work / "config" / "base.yaml").is_file(), "a tracked file must be kept"
    kept = (work / "scripts" / "real.sh").read_text()
    assert "MODIFIED" in kept, (
        "a tracked-but-modified file must still be baked — that is the iteration path, and "
        "the provenance label already reports it as X-dirty")
    assert "generated.json" in done.stdout, "the drop should be reported, not silent"


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_guard_does_not_touch_paths_outside_the_overlaid_trees(tmp_path):
    """$WORK is a Hermes checkout; an engine-ignored name must not be removed from it."""
    engine = tmp_path / "engine"
    work = tmp_path / "work"
    (engine / "extensions" / "x").mkdir(parents=True)
    (work / "extensions" / "x").mkdir(parents=True)
    (engine / ".gitignore").write_text("extensions/x/cache.bin\n")
    (engine / "keep.txt").write_text("x\n")
    subprocess.run(["git", "init", "-q"], cwd=engine, check=True)
    subprocess.run(["git", "add", "-A"], cwd=engine, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=engine, check=True)
    (engine / "extensions" / "x" / "cache.bin").write_bytes(b"engine cache")
    hermes_own = work / "extensions" / "x" / "cache.bin"
    hermes_own.write_bytes(b"HERMES OWN FILE")

    program = f'set -uo pipefail\nENGINE_DIR="{engine}"\nWORK="{work}"\n' + _guard_snippet()
    done = subprocess.run(["bash", "-c", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert hermes_own.read_bytes() == b"HERMES OWN FILE", (
        "extensions/ is not overlaid by build-engine-image.sh, so the sweep must leave the "
        "Hermes tree's same-named file alone")
