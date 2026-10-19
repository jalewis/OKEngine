"""okengine#993 [A1]: a value-level capability constraint, `forbidden_values`.

A `chat:*` writer may set an ordinary `status` (`active`, `draft`, `monitoring-active`, ...), but it
may never move `status` to a value the engine treats as RETIRED: `tombstoned`
(`okf_migrate.is_tombstoned`), `superseded` and `retired` (corpus_audit's retracted-judgment set and
the cockpit's current-evidence filter). The tombstone transition was already refused separately
(`_tombstone_transition_refuse`), but `status: retired` or `status: superseded` landed, and every
consumer that hides retired records then hid the page.

The constraint is a general capability key, so any lane may declare one; the chat ceiling requires
every chat capability (the engine floor and any pack replacement) to forbid at least the
retirement set. Values compare case- and whitespace-insensitively, like `is_tombstoned`, and are
judged after the write's own key and value aliasing, so an alias cannot carry a retirement value in.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  actor:
    required: [type, title]
strict_types: false
field_aliases:
  state: status
value_aliases:
  status:
    gone: retired
"""

_PAGE = """\
---
type: actor
id: G1045
title: Salt Typhoon
status: active
---

Body.
"""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, WRITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities" / "s").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "wiki" / "entities" / "s" / "salt-typhoon.md").write_text(_PAGE, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-10-05")
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    return tmp_path


@pytest.fixture
def chat(vault, monkeypatch):
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "chat:api_server")
    return _load(f"ws_chat993_{id(vault)}"), vault


def _status(root: Path) -> object:
    text = (root / "wiki" / "entities" / "s" / "salt-typhoon.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---")[1]).get("status")


def _refused(out: str) -> bool:
    return out.startswith("rejected:") and "field 'status' may not be set to" in out


@pytest.mark.parametrize("value", ["retired", "Superseded", " RETIRED "])
def test_chat_update_may_not_retire_a_page(chat, value):
    m, root = chat
    out = m._update("entities/s/salt-typhoon", {"status": value})
    assert _refused(out), out
    assert _status(root) == "active"


def test_chat_update_may_not_retire_a_page_through_a_key_alias(chat):
    """The write renames `state` onto an absent `status`; the rename is judged, not the alias."""
    m, root = chat
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    page.write_text(_PAGE.replace("status: active\n", ""), encoding="utf-8")
    out = m._update("entities/s/salt-typhoon", "state: superseded\n")
    assert _refused(out), out
    assert _status(root) is None


def test_chat_update_may_not_retire_a_page_through_a_value_alias(chat):
    m, root = chat
    out = m._update("entities/s/salt-typhoon", "status: gone\n")
    assert _refused(out), out
    assert _status(root) == "active"


@pytest.mark.parametrize("value", ["draft", "monitoring-active", "deprecated"])
def test_chat_update_may_set_an_ordinary_status(chat, value):
    m, root = chat
    out = m._update("entities/s/salt-typhoon", {"status": value})
    assert out.startswith("updated"), out
    assert _status(root) == value


def test_chat_may_not_create_a_page_born_retired(chat):
    m, root = chat
    out = m._create("entities/v/volt-typhoon", "type: actor\ntitle: Volt Typhoon\nstatus: retired\n",
                    "Body.")
    assert _refused(out), out
    assert not (root / "wiki" / "entities" / "v" / "volt-typhoon.md").exists()


def test_chat_may_create_an_active_page(chat):
    m, _root = chat
    out = m._create("entities/v/volt-typhoon", "type: actor\ntitle: Volt Typhoon\nstatus: active\n"
                    f"sources: [{_cite(m, 'Volt Typhoon')}]\n",
                    "Body.")
    assert out.startswith("created"), out


_PROBE_POLICY = {
    "schema_version": 1,
    "rules": [{
        "id": "probe-status", "owner": "pack", "description": "A probe lane never retires a page.",
        "severity": "reject", "applies_to": {"operation": "write"}, "enforcement": ["write"],
        "evaluator": "field-capability", "remediation": "Leave status alone.",
        "override": "forbidden", "verified_by": ["write"]}],
    "capabilities": {"cron:probe": {
        "rule_id": "probe-status", "operations": ["create", "update", "patch", "converge"],
        "paths": ["entities/**"], "update_fields": ["*"], "body": "allow",
        "forbidden_values": {"status": ["retired", "superseded"]}}},
}


@pytest.fixture
def probe(vault, monkeypatch):
    (vault / "policy.yaml").write_text(yaml.safe_dump(_PROBE_POLICY), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:probe")
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "report")
    return _load(f"ws_probe993_{id(vault)}"), vault


def test_patch_may_not_set_a_forbidden_value(probe):
    m, root = probe
    out = m._patch("entities/s/salt-typhoon", "status: active", "status: Retired")
    assert _refused(out), out
    assert _status(root) == "active"


def test_converge_into_an_existing_page_may_not_set_a_forbidden_value(probe):
    """Converge never ADDS status (`_NO_ADD_KEYS`) but it does update a stored one."""
    m, root = probe
    out = m._converge("entities/s/salt-typhoon",
                      "type: actor\nid: G1045\ntitle: Salt Typhoon\nstatus: superseded\n")
    assert _refused(out), out
    assert _status(root) == "active"


def test_converge_creating_a_page_may_not_set_a_forbidden_value(probe):
    m, root = probe
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\nstatus: retired\n", "Body.")
    assert _refused(out), out
    assert not (root / "wiki" / "entities" / "v" / "volt-typhoon.md").exists()


def test_converge_may_set_an_ordinary_status(probe):
    m, root = probe
    out = m._converge("entities/s/salt-typhoon",
                      "type: actor\nid: G1045\ntitle: Salt Typhoon\nstatus: dormant\n")
    assert out.startswith("converged"), out
    assert _status(root) == "dormant"


# --- QA of !1264 N1: a stored legacy alias the write would map onto a retirement value ----------


def test_an_unrelated_chat_update_may_not_retire_a_page_through_a_stored_value_alias(chat):
    """`status: gone` is stored raw (consumers read it as live); the write's value alias maps it
    to `retired`. The status key looks unchanged, but the write would store a retirement value."""
    m, root = chat
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    page.write_text(_PAGE.replace("status: active", "status: gone"), encoding="utf-8")
    out = m._update("entities/s/salt-typhoon", {"summary": "telecom intrusions"})
    assert _refused(out), out
    assert _status(root) == "gone"


def test_an_echo_of_a_stored_retirement_value_is_not_a_move(chat):
    """A page already carrying `retired` (any spelling) is not moved by an unrelated update."""
    m, root = chat
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    page.write_text(_PAGE.replace("status: active", "status: Retired"), encoding="utf-8")
    out = m._update("entities/s/salt-typhoon", {"summary": "telecom intrusions"})
    assert out.startswith("updated"), out


# --- QA of !1264 B1 + the converge decision (user: a new id needs `create`) -------------------

_LANES = {
    "cron:mixed": {"rule_id": "probe-status", "operations": ["create", "update", "converge"],
                   "paths": ["entities/**"], "update_fields": ["*"], "body": "deny"},
    "cron:merger": {"rule_id": "probe-status", "operations": ["update", "converge"],
                    "paths": ["entities/**"], "update_fields": ["*"], "body": "allow"},
}


def _lane(vault, monkeypatch, actor):
    policy = dict(_PROBE_POLICY, capabilities={**_PROBE_POLICY["capabilities"], **_LANES})
    (vault / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "report")
    return _load(f"ws_{actor.split(':')[1]}993_{id(vault)}")


def test_a_body_denied_lane_converges_a_new_id_without_a_body(vault, monkeypatch):
    """No body is no body change: a body-denied lane may still create a page by converge."""
    m = _lane(vault, monkeypatch, "cron:mixed")
    out = m._converge("entities/v/volt-typhoon", "type: actor\nid: G1017\ntitle: Volt Typhoon\n"
                      f"sources: [{_cite(m, 'Volt Typhoon')}]\n")
    assert out.startswith("created"), out


def test_a_body_denied_lane_may_not_converge_a_new_id_with_a_body(vault, monkeypatch):
    m = _lane(vault, monkeypatch, "cron:mixed")
    out = m._converge("entities/v/volt-typhoon", "type: actor\nid: G1017\ntitle: Volt Typhoon\n",
                      "Body.")
    assert out.startswith("rejected:") and "body mutation is denied" in out, out


def test_a_converge_lane_without_create_converges_an_existing_page(vault, monkeypatch):
    m = _lane(vault, monkeypatch, "cron:merger")
    out = m._converge("entities/s/salt-typhoon",
                      "type: actor\nid: G1045\ntitle: Salt Typhoon\nsummary: telecom\n")
    assert out.startswith("converged"), out


def test_converging_a_new_id_is_a_create(vault, monkeypatch):
    """User decision (okengine#993): `converge` alone never creates a page."""
    m = _lane(vault, monkeypatch, "cron:merger")
    out = m._converge("entities/v/volt-typhoon", "type: actor\nid: G1017\ntitle: Volt Typhoon\n",
                      "Body.")
    assert out.startswith("rejected:"), out
    assert "operation 'create' is not allowed (converging a new id is a create)" in out, out
    assert not (vault / "wiki" / "entities" / "v" / "volt-typhoon.md").exists()


def test_a_plain_create_refusal_does_not_mention_converge(vault, monkeypatch):
    m = _lane(vault, monkeypatch, "cron:merger")
    out = m._create("entities/v/volt-typhoon", "type: actor\ntitle: Volt Typhoon\n", "Body.")
    assert "operation 'create' is not allowed" in out, out
    assert "converging" not in out, out


def _cite(m, name):
    """A canonical source that names `name` as a threat group, so creating it is grounded in
    evidence rather than in the page's own prose (okengine#1012)."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    ref = f"sources/2026/01/01/{slug}-report"
    page = m._wiki() / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: source\ntitle: {name} report\n---\n\n"
                    f"{name} is a threat group that targeted banks.\n", encoding="utf-8")
    return ref
