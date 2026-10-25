"""okengine#1072: tests never share the write path's persisted id-index with the host or a prior run."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.invariant


def test_the_session_hermes_data_is_private_to_this_process():
    data = Path(os.environ["HERMES_DATA"])
    assert not str(data).startswith("/opt/data"), "tests must never read the deployment default"
    assert data.parent.name.startswith("okengine-test-hermes-data-") and data.is_dir(), (
        f"HERMES_DATA {data} is not a per-test directory under the session directory")


def _assert_fresh_hermes_data_then_write_state():
    data = Path(os.environ["HERMES_DATA"])
    left = sorted(str(p.relative_to(data)) for p in data.rglob("*"))
    assert not left, f"this test inherited another test's HERMES_DATA state: {left}"
    marker = data / "state" / "okengine.events" / "event-scores.jsonl"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}\n", encoding="utf-8")


def test_a_test_starts_with_an_empty_hermes_data_first():
    """Negative fixture, with its twin below: whichever of the two runs second (pytest-randomly may
    order them either way) fails if a lane's persisted state survives into the next test. A shared
    session directory let an event-score sidecar flip numeric_metrics' has_events branch."""
    _assert_fresh_hermes_data_then_write_state()


def test_a_test_starts_with_an_empty_hermes_data_second():
    _assert_fresh_hermes_data_then_write_state()


def test_the_write_path_id_index_artifact_lives_under_the_session_directory():
    spec = importlib.util.spec_from_file_location("id_index_isolation", REPO / "scripts/cron/id_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.INDEX_PATH.is_relative_to(Path(os.environ["HERMES_DATA"]))


def test_a_planted_id_index_outside_the_session_cannot_reach_the_write_path(tmp_path):
    """Negative fixture: an id-index left by another vault (the mutation-shard pollution) sits where a
    pre-set HERMES_DATA points. A test session must ignore it; a URL-identity create still converges."""
    polluted = tmp_path / "polluted"
    other = tmp_path / "other"
    page = other / "wiki" / "entities" / "z" / "zeta.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: actor\nid: entities:zeta\ntitle: Zeta\n---\nz\n", encoding="utf-8")
    env = {**os.environ, "WIKI_PATH": str(other), "HERMES_DATA": str(polluted),
           "PYTHONPATH": os.pathsep.join(["src", "okengine-mcp", "scripts/cron"])}
    built = subprocess.run([sys.executable, "scripts/cron/id_index.py"], cwd=REPO, env=env,
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    assert json.loads((polluted / "state" / "id-index.json").read_text())["by_id"]

    env.pop("WIKI_PATH")
    target = "tests/test_write_server.py::test_separator_hit_with_same_url_converges_legacy_source_id"
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
                         cwd=REPO, env=env, capture_output=True, text=True)
    assert run.returncode == 0, f"a pre-set HERMES_DATA leaked into the session:\n{run.stdout[-2000:]}"
