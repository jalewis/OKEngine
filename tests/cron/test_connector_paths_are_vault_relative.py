"""Connector output paths must resolve against the VAULT, never the cwd (okengine#509).

`source_connector.py` defaulted `--state-root`, `--archive-root` and `--health-root` to bare
relative paths, so whatever directory the process was started in became the vault. The
authority-enrich lane does not forward a health root, so running its test suite from a
checkout wrote `.okengine/connectors/health/reference.ror-organizations.json` into the REPO
and left a tracked file modified after every run.

That is not merely untidy. It is why `git add -A` was unsafe in this repo, and a working tree
that is never clean is precisely the condition that let three days of uncommitted work go
unnoticed (#493). Same class as the cwd-based vault resolution fixed in !364.

Two guards:
  * the shipped defaults are anchored to WIKI_PATH;
  * running the authority-enrich lane leaves the repository's own connector tree untouched
    — the direct regression, which fails on the pre-fix code.
"""
import importlib.util
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONNECTOR = REPO / "scripts" / "cron" / "source_connector.py"
ENRICH = REPO / "scripts" / "cron" / "authority_enrich.py"
MANIFEST = REPO / "examples" / "source-connectors" / "ror-organizations.yaml"
FIXTURE = REPO / "examples" / "source-connectors" / "fixtures" / "ror-organizations.fixture.json"


def test_defaults_are_anchored_to_wiki_path_not_cwd():
    text = CONNECTOR.read_text(encoding="utf-8")
    assert 'os.environ.get("WIKI_PATH")' in text, (
        "connector output roots must be anchored to WIKI_PATH; a bare relative default "
        "silently makes the process's cwd the vault")
    for bare in ('default=Path(".okengine/connectors/state")',
                 'default=Path("raw/connectors")',
                 'default=Path(".okengine/connectors/health")'):
        assert bare not in text, f"cwd-relative default still present: {bare}"


def test_audit_history_path_is_resolved_at_call_time():
    """A repo path bound as a DEFAULT ARGUMENT cannot be redirected (okengine#509).

    `record_run(run_id, path: Path = HISTORY)` captured the module-level repo path when the
    function was defined, so the tests' `m.HISTORY = tmp_path / ...` had no effect and the
    run wrote scripts/audit/findings-history.jsonl into the checkout after every suite run.
    The test looked like it redirected the write. It did not.

    Checked on the real signature rather than the source text, so the guard survives
    reformatting.
    """
    audit = REPO / "scripts" / "audit" / "deterministic_audit.py"
    if not audit.is_file():
        pytest.skip("scripts/audit is publish-excluded and absent from this snapshot")
    spec = importlib.util.spec_from_file_location("deterministic_audit_sigcheck", audit)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    default = inspect.signature(module.record_run).parameters["path"].default
    assert default is None, (
        "record_run's `path` must default to None and resolve HISTORY at call time; a Path "
        f"default ({default!r}) is early-bound and cannot be redirected by a caller")


def test_authority_enrich_forwards_the_health_root():
    text = ENRICH.read_text(encoding="utf-8")
    assert '"--health-root"' in text, (
        "authority_enrich must forward --health-root, or source_connector falls back to "
        "its own default and the caller cannot control where the record lands")


def _snapshot_repo_connectors() -> dict:
    root = REPO / ".okengine" / "connectors"
    if not root.is_dir():
        return {}
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.skipif(not MANIFEST.is_file() or not FIXTURE.is_file(),
                    reason="reference connector manifest/fixture not present")
def test_running_the_lane_does_not_touch_the_repository(tmp_path):
    """The regression: a lane run must not write into the checkout it is run from.

    This is the shape that actually bit — MANIFEST and FIXTURE legitimately point at the
    real repo, and it was the OUTPUT root that leaked, not the inputs.
    """
    before = _snapshot_repo_connectors()

    spec = importlib.util.spec_from_file_location("authority_enrich_leakcheck", ENRICH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if str(ENRICH.parent) not in sys.path:
        sys.path.insert(0, str(ENRICH.parent))
    spec.loader.exec_module(module)

    vault = tmp_path / "vault"
    module.VAULT = vault
    module.WIKI = vault / "wiki"
    page = vault / "wiki" / "entities" / "h" / "hkust.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: lab\nname: Hong Kong University of Science and Technology\n---\nBody.\n",
        encoding="utf-8")

    rc = module.main([
        "--manifest", str(MANIFEST), "--fixture", str(FIXTURE),
        "--state-root", str(tmp_path / "state"),
        "--health-root", str(tmp_path / "health"),
        "--ledger-root", str(tmp_path / "ledger.jsonl"),
    ])
    assert rc == 0, "the lane should complete against the fixture"

    assert _snapshot_repo_connectors() == before, (
        "the lane modified the repository's own .okengine/connectors tree; output roots "
        "must land under the supplied vault/tmp paths, never the checkout")
    assert (tmp_path / "health").exists(), (
        "the health record should have been written to the supplied --health-root, "
        "proving the assertion above is not vacuous")


@pytest.mark.skipif(not MANIFEST.is_file() or not FIXTURE.is_file(),
                    reason="reference connector manifest/fixture not present")
def test_default_roots_follow_wiki_path_and_not_the_cwd(tmp_path):
    """The anchoring itself: with WIKI_PATH set, output must land there, not in the cwd.

    Run with NO --state-root/--health-root at all, from a cwd that is deliberately not the
    vault, so only the defaults decide. Pre-fix this wrote into the cwd.
    """
    vault = tmp_path / "vault"
    elsewhere = tmp_path / "elsewhere"
    vault.mkdir()
    elsewhere.mkdir()
    env = dict(os.environ, WIKI_PATH=str(vault))
    env.pop("COLLECTION_LEDGER_DIR", None)

    done = subprocess.run(
        [sys.executable, str(CONNECTOR), "--manifest", str(MANIFEST),
         "--fixture", str(FIXTURE), "--observed-at", "2026-01-01T00:00:00Z",
         "--param", "entity_name=Hong Kong University of Science and Technology"],
        capture_output=True, text=True, env=env, cwd=str(elsewhere))
    assert done.returncode == 0, done.stderr

    assert not (elsewhere / ".okengine").exists(), (
        f"the connector wrote into its cwd instead of WIKI_PATH: "
        f"{sorted(p.name for p in elsewhere.iterdir())}")
    assert (vault / ".okengine" / "connectors").is_dir(), (
        "with WIKI_PATH set, the default output roots must resolve inside the vault")
