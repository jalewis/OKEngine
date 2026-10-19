"""okengine#1032 [6]: a networked extension's `identity_edit` grant passes the real capability gate.

The token's `write_capability` is the extension's whole `capabilities.write_policy`, `identity_edit`
included, and `_capability_reject` hands it to `policy_plane.evaluate_capability`. Before the fix
`validate_capability` called `identity_edit` an unknown key, so EVERY write by a granted extension was
rejected as an invalid capability, not just its identity edits. Every identity-guard test stubbed the
gate out, so nothing noticed. These tests run the real gate (`capability_gate=True`)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "tests")]
import test_identity_guard_1028 as T  # noqa: E402

_CAP = {"rule_id": "engine-authenticated-writer", "operations": ["update"],
        "paths": ["entities/**"], "update_fields": ["*"], "body": "allow"}


def _as_extension(m, capability):
    return m._caller_var.set({"kind": "extension", "ext_id": "acme.curate",
                              "actor": "extension:acme.curate", "write_scopes": ["entities/"],
                              "write_capability": capability})


def _real_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_POLICY_CATALOG", str(REPO / "config" / "policy" / "catalog.yaml"))
    m, vault = T._load(tmp_path, monkeypatch, "okpack-threat-actors", actor=None,
                       capability_gate=True)
    assert getattr(m._capability_reject, "__name__", "") == "_capability_reject"   # not stubbed
    return m, vault, T._seed(m, "entities/hafnium.md", dict(T._HAFNIUM))


def test_a_granted_extension_write_passes_the_real_capability_gate(tmp_path, monkeypatch):
    m, vault, page = _real_gate(tmp_path, monkeypatch)
    token = _as_extension(m, {**_CAP, "identity_edit": {"fields": ["title"]}})
    try:
        out = m._update("entities/hafnium.md", {"title": "Silk Typhoon", "type": "identity"})
    finally:
        m._caller_var.reset(token)
    assert out.startswith("updated"), out
    fm = T._fm(page)
    # the granted field applies directly; the ungranted retype is still held for review
    assert fm["title"] == "Silk Typhoon" and fm["type"] == "actor", fm
    assert fm["needs_review"] is True


def test_an_ungranted_extension_rename_is_held_under_the_real_gate(tmp_path, monkeypatch):
    m, vault, page = _real_gate(tmp_path, monkeypatch)
    token = _as_extension(m, dict(_CAP))
    try:
        out = m._update("entities/hafnium.md", {"title": "Silk Typhoon"})
    finally:
        m._caller_var.reset(token)
    assert out.startswith("updated"), out
    assert T._fm(page)["title"] == "HAFNIUM"


def test_a_malformed_grant_is_refused_by_the_real_gate(tmp_path, monkeypatch):
    m, vault, page = _real_gate(tmp_path, monkeypatch)
    token = _as_extension(m, {**_CAP, "identity_edit": {"fields": "title"}})
    try:
        out = m._update("entities/hafnium.md", {"description": "Enriched."})
    finally:
        m._caller_var.reset(token)
    assert "caller capability is invalid" in out, out
    assert T._fm(page)["description"] == T._HAFNIUM["description"]
