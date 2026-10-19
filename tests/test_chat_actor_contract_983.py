"""okengine#983: a `chat:` actor is not a cron lane, and its writer name is reserved.

`output_contract_enforce.resolve()` used to strip `cron:` from any server-bound actor and look the
remainder up as a cron job. For `chat:api_server` that found nothing and returned
`contract_not_resolved`, which the write path enforces for every job actor, so every chat write
would have been rejected. A non-cron actor now resolves no lane contract and is governed by its
catalog capability alone; one with no capability still fails closed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "oce_983", REPO / "okengine-mcp" / "output_contract_enforce.py")
oce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oce)

from tools import policy_plane as pp  # noqa: E402
import cron_pack_split as cps  # noqa: E402


def test_a_chat_actor_resolves_no_cron_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(tmp_path / "jobs.json"))
    caller = {"kind": "job", "actor": "chat:api_server"}
    assert oce.resolve(caller) == (None, None)
    assert oce.capability(caller) is None
    assert oce.evaluate(caller, operation="update", namespace="entities", page_type="actor",
                        frontmatter={}, body="", unknown_fields=[], wiki=tmp_path) == []


def test_a_cron_actor_with_no_job_still_fails_its_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(tmp_path / "jobs.json"))
    assert oce.resolve({"kind": "job", "actor": "cron:ghost"}) == ({"_missing": True}, "ghost")


def test_an_unknown_non_cron_actor_still_fails_closed_on_capability():
    policy = pp.compose_documents([pp.engine_catalog_path()])
    finding = pp.evaluate_capability(policy, "chat:telegram", "create", "entities/x/y.md")
    assert finding["rule_id"] == "engine-authenticated-writer"
    assert finding["message"] == "caller has no declared write capability"


def test_a_pack_job_named_chat_may_not_take_the_chat_writer():
    """contract_writer_name('chat') is `okengine-write-chat`: the Agent Chat's identity."""
    assert cps.contract_writer_name("chat") == "okengine-write-chat"
    job = {"name": "chat", "pack": "p", "enabled_toolsets": ["okengine-write-chat"]}
    errors = cps.writer_identity_errors([job], cps.reserved_writers(extension_lanes=[]))
    assert errors and "chat:api_server" in errors[0], errors
