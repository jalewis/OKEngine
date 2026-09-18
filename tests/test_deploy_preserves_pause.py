"""A cron deploy must not re-arm an operator-paused lane (okengine#517).

Pause is RUNTIME state — it lives in the deployed `jobs.json` as `enabled: false` + `paused_at`,
while the generated source always declares `enabled: true`. Regeneration therefore overwrote
runtime state with declared state and silently re-enabled a held lane. On one pack it happened
twice, the second time ~40 minutes before a reshelve that would have moved 988 pages unattended.

The failure shape is why this needs a test rather than a note: the deploy reports success, the
post-deploy verification passes (an enabled job is not an error), and nothing observable happens
until the lane's next scheduled fire.

The merge itself is a heredoc inside `deploy-cron-plus-jobs.sh`, so these tests extract and run
that exact block rather than a reimplementation of it — a copy would drift from the shipped code
and pass while the deploy stayed broken.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "scripts" / "deploy-cron-plus-jobs.sh"


def _merge_block() -> str:
    """The pause-preservation python heredoc, lifted verbatim from the deploy script."""
    text = DEPLOY.read_text(encoding="utf-8")
    marker = 'RESUME_PAUSED="${RESUME_PAUSED:-0}" "$HOST_PYTHON" - "$DEPLOY_JOBS" "$LIVE_JOBS_TMP" <<\'PY\'\n'
    start = text.find(marker)
    assert start >= 0, "pause-preservation block not found — did the deploy script change shape?"
    start += len(marker)
    end = text.find("\nPY\n", start)
    assert end > start
    return text[start:end]


def _run(tmp_path: Path, new_jobs: dict, live_jobs, *, resume=False):
    new_p = tmp_path / "deploy.json"
    live_p = tmp_path / "live.json"
    new_p.write_text(json.dumps(new_jobs), encoding="utf-8")
    live_p.write_text(json.dumps(live_jobs), encoding="utf-8")
    env = dict(os.environ, RESUME_PAUSED="1" if resume else "0")
    proc = subprocess.run([sys.executable, "-c", _merge_block(), str(new_p), str(live_p)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(new_p.read_text(encoding="utf-8")), proc.stdout


def _src(**over):
    job = {"id": "aaa111", "name": "reshelve", "enabled": True,
           "schedule": {"kind": "cron", "expr": "35 */2 * * *"}}
    job.update(over)
    return {"jobs": [job, {"id": "bbb222", "name": "other", "enabled": True}]}


def test_a_paused_lane_stays_paused(tmp_path):
    """The regression: regeneration re-armed it because the source says enabled: true."""
    live = {"jobs": [{"id": "aaa111", "name": "reshelve", "enabled": False,
                      "paused_at": "2026-07-30T14:38:42+00:00"}]}
    out, log = _run(tmp_path, _src(), live)
    job = next(j for j in out["jobs"] if j["id"] == "aaa111")
    assert job["enabled"] is False, "a deploy must not re-arm a lane the operator held"
    assert job["paused_at"] == "2026-07-30T14:38:42+00:00", "the pause timestamp is evidence"
    assert "reshelve" in log, "silently preserving it would be its own foot-gun — say so"


def test_unpaused_lanes_are_untouched(tmp_path):
    live = {"jobs": [{"id": "aaa111", "enabled": True}, {"id": "bbb222", "enabled": True}]}
    out, _ = _run(tmp_path, _src(), live)
    assert all(j["enabled"] is True for j in out["jobs"])


def test_pause_is_matched_on_id_not_name(tmp_path):
    """A renamed lane keeps its pause; a re-used name does not inherit one."""
    live = {"jobs": [{"id": "aaa111", "name": "reshelve-OLD-NAME", "enabled": False}]}
    out, _ = _run(tmp_path, _src(), live)
    assert next(j for j in out["jobs"] if j["id"] == "aaa111")["enabled"] is False

    live2 = {"jobs": [{"id": "zzz999", "name": "reshelve", "enabled": False}]}
    out2, _ = _run(tmp_path, _src(), live2)
    assert all(j["enabled"] is True for j in out2["jobs"]), (
        "a different lane that happens to share a name must not import its pause")


def test_resume_paused_re_arms_and_says_so(tmp_path):
    live = {"jobs": [{"id": "aaa111", "name": "reshelve", "enabled": False}]}
    out, log = _run(tmp_path, _src(), live, resume=True)
    assert all(j["enabled"] is True for j in out["jobs"])
    assert "RESUME_PAUSED" in log and "reshelve" in log


def test_a_declared_disable_survives(tmp_path):
    """`enabled: false` in the SOURCE is a declaration, not a pause, and still applies."""
    out, _ = _run(tmp_path, _src(enabled=False), {"jobs": [{"id": "aaa111", "enabled": True}]})
    assert next(j for j in out["jobs"] if j["id"] == "aaa111")["enabled"] is False


def test_a_paused_lane_dropped_from_source_is_reported(tmp_path):
    live = {"jobs": [{"id": "gone999", "name": "retired-lane", "enabled": False}]}
    _out, log = _run(tmp_path, _src(), live)
    assert "no longer in the source" in log


def test_a_bare_list_live_store_is_handled(tmp_path):
    """cron-plus has shipped jobs.json as both a bare list and {"jobs": [...]}."""
    live = [{"id": "aaa111", "name": "reshelve", "enabled": False}]
    out, _ = _run(tmp_path, _src(), live)
    assert next(j for j in out["jobs"] if j["id"] == "aaa111")["enabled"] is False


def test_an_unreadable_live_store_deploys_as_generated(tmp_path):
    """First deploy into a pack with no jobs.json must not fail."""
    new_p = tmp_path / "deploy.json"
    live_p = tmp_path / "live.json"
    new_p.write_text(json.dumps(_src()), encoding="utf-8")
    live_p.write_text("not json at all", encoding="utf-8")
    proc = subprocess.run([sys.executable, "-c", _merge_block(), str(new_p), str(live_p)],
                          capture_output=True, text=True,
                          env=dict(os.environ, RESUME_PAUSED="0"))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(new_p.read_text())["jobs"][0]["enabled"] is True


def test_the_deploy_script_documents_the_override():
    """
    CANNOT DETECT: whether the documented override is implemented, or still spelled the same way
    in the code as in the comment. Documentation and behaviour drift independently.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert "RESUME_PAUSED=1" in text.split("set -euo pipefail")[0], (
        "the opt-out belongs in the usage header, not only in the code")
    assert re.search(r"okengine#517", text), "keep the incident reference next to the fix"
