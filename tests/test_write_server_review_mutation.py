"""Observable contracts of the version-locked review services (okengine#612).

These drive `_assign_review`, `_resolve_review` and `_record_machine_review` through the
enforced write path (`okengine-mcp/write_server.py`) on a real temp vault and assert what a
caller, the Cockpit worklist or the next reviewer can observe: the exact structured result,
the stored review record (its version counter, history and raw YAML form), the subject
page's frontmatter after a decision, file modes, and rollback after a failed publish.

Extension callers run with a real policy-plane capability; the only substitution is
`_effective_policy`, the composed policy document a deployment would discover on disk.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  vendor: {required: [type]}
  entity: {required: [type, name]}
strict_types: false
"""

PAGE = "vendors/acme.md"


def _load(root: Path, monkeypatch):
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    name = f"write_server_review_mutation_{id(root)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch), tmp_path


def _write_page(root: Path, rel: str, fm: dict, body: str = "body under review\n") -> Path:
    """A page placed on disk by a no_agent lane, not through the write server."""
    page = root / "wiki" / rel
    fm = {"id": f"{page.parent.name}:{page.stem}", **fm}
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body,
                    encoding="utf-8")
    return page


def _fm(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{page} has no frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1]) or {}


def _lock(m, page: Path):
    """The (version, hash) pair a reviewer must present for the page as it is now."""
    _, _, _, version, digest = m._review_page_state(page)
    return version, digest


def _record(m, review_id: str) -> tuple[Path, dict, str]:
    rp = m._review_record_path(review_id)
    raw = rp.read_text(encoding="utf-8")
    return rp, yaml.safe_load(raw), raw


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _set_record_version(m, review_id: str, version) -> None:
    rp, rec, _ = _record(m, review_id)
    if version is None:
        rec.pop("version", None)
    else:
        rec["version"] = version
    rp.write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8")


class _Caller:
    """Run as a networked extension with explicit path scopes and one capability."""

    def __init__(self, m, monkeypatch, *, scopes=("**",), **capability):
        self.m = m
        monkeypatch.setattr(m, "_effective_policy", lambda: {
            "rules": [{"id": "ext-write", "remediation": "narrow the write"}],
            "capabilities": {},
        })
        capability.setdefault("rule_id", "ext-write")
        capability.setdefault("paths", ["**"])
        self.caller = {"kind": "extension", "actor": "extension:probe", "ext_id": "probe",
                       "write_scopes": list(scopes), "write_capability": capability}

    def __enter__(self):
        self.token = self.m._caller_var.set(self.caller)
        return self

    def __exit__(self, *exc):
        self.m._caller_var.reset(self.token)


# --- page state and review identity ------------------------------------------------------


@pytest.mark.parametrize("stored, expected", [(None, 1), (0, 1), (7, 7), ("x", 1)])
def test_review_page_state_defaults_a_missing_or_falsy_version_to_one(vault, stored, expected):
    m, root = vault
    fm = {"type": "vendor"}
    if stored is not None:
        fm["version"] = stored
    page = _write_page(root, PAGE, fm)

    _, _, subject, version, digest = m._review_page_state(page)

    assert (subject, version) == ("vendors/acme", expected)
    assert digest == hashlib.sha256(page.read_bytes()).hexdigest()


@pytest.mark.parametrize("subject", ["", "/abs/vendors/acme", "../outside/acme"])
def test_subject_refusal_rejects_every_non_vault_relative_key(vault, subject):
    m, _ = vault
    refusal = m._subject_refusal(subject)
    assert refusal and "non-vault-relative" in refusal and repr(subject) in refusal


@pytest.mark.parametrize("subject", ["vendors/acme", "a/../b", "x/abs"])
def test_subject_refusal_admits_vault_relative_keys(vault, subject):
    m, _ = vault
    assert m._subject_refusal(subject) is None


def test_review_id_hashes_sorted_unescaped_reasons_with_fixed_prefix_widths(vault):
    m, root = vault
    page = _write_page(root, PAGE, {
        "type": "vendor", "version": 3,
        "conflicts": [{"field": "headquarters"}],
    })
    flags = ["needs a résumé check"]

    rec = m._ensure_review_request(page, flags)

    _, _, subject, version, digest = m._review_page_state(page)
    reasons = m._structured_review_reasons(_fm(page), "body under review\n", flags)
    assert reasons[1] == {"code": "conflict", "field": "headquarters",
                          "detail": "sources disagree on this field"}
    reason_key = json.dumps(reasons, sort_keys=True, ensure_ascii=False)
    expected = (f"review:vendors/acme:3:{digest[:16]}:"
                f"{hashlib.sha256(reason_key.encode('utf-8')).hexdigest()[:12]}")
    assert rec["review_id"] == expected
    assert rec["version"] == 1
    assert rec["subject_version"] == 3
    assert rec["state"] == "open"


def test_ensure_review_request_returns_the_stored_record_on_repeat(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    first = m._ensure_review_request(page)
    _set_record_version(m, first["review_id"], 5)

    again = m._ensure_review_request(page)

    assert again["review_id"] == first["review_id"]
    assert again["version"] == 5, "an existing record must be loaded, not re-minted"


def test_new_review_record_is_unicode_yaml_in_declared_key_order(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor", "sources": "https://example.test/ä"})

    rec = m._ensure_review_request(page)

    _, stored, raw = _record(m, rec["review_id"])
    assert raw.splitlines()[0] == "version: 1"
    assert "https://example.test/ä" in raw, "the record must be written with allow_unicode"
    assert list(stored)[:3] == ["version", "review_id", "subject"]
    assert stored["evidence"] == ["https://example.test/ä"]
    assert stored["requested_by"] == "admin"


def test_requested_by_prefers_the_extension_id(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    with _Caller(m, monkeypatch, operations=["review"], types=["*"]):
        rec = m._ensure_review_request(page)
    assert rec["requested_by"] == "probe"


def test_requested_by_falls_back_to_the_caller_kind(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    token = m._caller_var.set({"kind": "job", "actor": "lane", "ext_id": None})
    try:
        rec = m._ensure_review_request(page)
    finally:
        m._caller_var.reset(token)
    assert rec["requested_by"] == "job"


def test_requested_by_is_unknown_without_identity(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    token = m._caller_var.set({"kind": "", "ext_id": None})
    try:
        rec = m._ensure_review_request(page)
    finally:
        m._caller_var.reset(token)
    assert rec["requested_by"] == "unknown"


def test_load_review_record_round_trips_and_misses_cleanly(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    rec = m._ensure_review_request(page)

    rp, loaded = m._load_review_record(rec["review_id"])
    assert rp == m._review_record_path(rec["review_id"])
    assert loaded["review_id"] == rec["review_id"]
    assert m._load_review_record("review:missing") == (None, None)


# --- assign ---------------------------------------------------------------------------------


def test_assign_claims_the_request_and_publishes_a_shared_record(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)

    result = m._assign_review(PAGE, "Zoë", version, digest, service="cockpit")

    rec = m._ensure_review_request(page)
    assert result == {"ok": True, "status": 200, "review_id": rec["review_id"],
                      "state": "in-review", "assigned_to": "Zoë"}
    rp, stored, raw = _record(m, rec["review_id"])
    assert stored["version"] == 2
    assert stored["state"] == "in-review" and stored["assigned_to"] == "Zoë"
    assert stored["history"][-1]["service"] == "cockpit"
    assert raw.splitlines()[0] == "version: 2"
    assert "Zoë" in raw, "the record must be written with allow_unicode"
    assert _mode(rp) == 0o644
    assert not list(rp.parent.glob(".review-record-*"))
    assert "review assign vendors/acme v1 to Zoë via cockpit" in (
        root / "wiki" / "log.md").read_text(encoding="utf-8")


def test_assign_increments_the_stored_record_version_by_one(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    _set_record_version(m, rec["review_id"], 3)

    assert m._assign_review(PAGE, "rev", version, digest)["ok"] is True

    assert _record(m, rec["review_id"])[1]["version"] == 4


def test_assign_treats_a_missing_record_version_as_one(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    _set_record_version(m, rec["review_id"], None)

    assert m._assign_review(PAGE, "rev", version, digest)["ok"] is True

    assert _record(m, rec["review_id"])[1]["version"] == 2


def test_assign_twice_reuses_the_lock_directory(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    assert m._assign_review(PAGE, "first", version, digest)["ok"] is True
    second = m._assign_review(PAGE, "second", version, digest)
    assert second["ok"] is True and second["assigned_to"] == "second"


def test_assign_with_the_current_review_id_proceeds(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    review_id = "".join(list(m._ensure_review_request(page)["review_id"]))

    result = m._assign_review(PAGE, "rev", version, digest, review_id=review_id)

    assert result["ok"] is True


def test_assign_refuses_a_stale_review_id_that_sorts_before_the_current(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)

    result = m._assign_review(PAGE, "rev", version, digest, review_id="review:a")

    assert result == {"ok": False, "status": 409, "error": "review request is stale"}


@pytest.mark.parametrize("delta", [-1, 1])
def test_assign_refuses_any_other_page_version(vault, delta):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor", "version": 5})
    version, digest = _lock(m, page)

    result = m._assign_review(PAGE, "rev", version + delta, digest)

    assert result == {"ok": False, "status": 409,
                      "error": "subject changed; refresh before assigning"}


def test_assign_compares_large_versions_by_value(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor", "version": 100000})
    _, digest = _lock(m, page)

    assert m._assign_review(PAGE, "rev", int("100000"), digest)["ok"] is True


def test_assign_refuses_a_closed_review(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    rp, stored, _ = _record(m, rec["review_id"])
    stored["state"] = "rejected"
    rp.write_text(yaml.safe_dump(stored), encoding="utf-8")

    result = m._assign_review(PAGE, "rev", version, digest)

    assert result == {"ok": False, "status": 409, "error": "closed review cannot be assigned"}


def test_assign_reports_a_missing_subject(vault):
    m, _ = vault
    assert m._assign_review("vendors/ghost.md", "rev", 1, "0" * 64) == {
        "ok": False, "status": 404, "error": "subject page not found"}


def test_assign_refuses_a_page_with_corrupt_bytes(vault):
    m, root = vault
    page = root / "wiki" / PAGE
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\ntype: vendor\n---\nbad \xff byte\n")

    result = m._assign_review(PAGE, "rev", 1, "0" * 64)

    assert result["ok"] is False and result["status"] == 422
    assert "invalid UTF-8" in result["error"]


def test_assign_refuses_outside_the_extension_scope(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    with _Caller(m, monkeypatch, scopes=("topics/**",), operations=["review"], types=["*"]):
        result = m._assign_review(PAGE, "rev", version, digest)
    assert result["ok"] is False and result["status"] == 403
    assert "write scope" in result["error"]


def test_assign_refuses_without_review_capability(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    with _Caller(m, monkeypatch, operations=["update"], types=["*"]):
        result = m._assign_review(PAGE, "rev", version, digest)
    assert result["ok"] is False and result["status"] == 403
    assert result["error"]


# --- resolve ---------------------------------------------------------------------------------


def test_approve_stamps_the_page_and_closes_the_record(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor", "version": 3, "needs_review": True})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    _set_record_version(m, rec["review_id"], 3)

    result = m._resolve_review(PAGE, "approve", "Zoë", "", version, digest, service="cockpit")

    assert result == {"ok": True, "status": 200, "review_id": rec["review_id"],
                      "state": "approved", "subject": "vendors/acme",
                      "reviewed_version": 3, "page_version": 4}
    fm = _fm(page)
    assert fm["version"] == 4
    assert fm["needs_review"] is False
    assert fm["review_state"] == "approved"
    assert fm["reviewed_version"] == 3
    assert fm["reviewed_by"] == "Zoë" and fm["reviewed_on"] == "2026-06-15"
    assert fm["reviewed_at"]
    rp, stored, raw = _record(m, rec["review_id"])
    assert stored["version"] == 4
    assert stored["decision_note"] is None
    assert stored["history"][-1]["decision_note"] is None
    assert stored["decision_service"] == "cockpit"
    assert raw.splitlines()[0] == "version: 4"
    assert "Zoë" in raw
    assert _mode(page) == 0o644 and _mode(rp) == 0o644
    assert not list(page.parent.glob(".review-page-*"))
    assert not list(rp.parent.glob(".review-record-*"))


def test_resolve_treats_a_missing_record_version_as_one(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    _set_record_version(m, rec["review_id"], None)

    assert m._resolve_review(PAGE, "approve", "rev", "", version, digest)["ok"] is True

    assert _record(m, rec["review_id"])[1]["version"] == 2


@pytest.mark.parametrize("decision, state, flagged", [
    ("reject", "rejected", True),
    ("request-changes", "changes-requested", True),
    ("dismiss", "dismissed", False),
    ("defer", "open", True),
])
def test_non_approval_withdraws_a_prior_approval(vault, decision, state, flagged):
    m, root = vault
    page = _write_page(root, PAGE, {
        "type": "vendor", "version": 3, "needs_review": True,
        "reviewed_by": "old", "reviewed_on": "2026-01-01", "reviewed_at": "2026-01-01T00:00:00Z",
    })
    version, digest = _lock(m, page)

    result = m._resolve_review(PAGE, decision, "rev", "  why not  ", version, digest)

    assert result["ok"] is True and result["state"] == state
    fm = _fm(page)
    assert fm["review_state"] == state and fm["needs_review"] is flagged
    for key in ("reviewed_by", "reviewed_on", "reviewed_at"):
        assert key not in fm, f"{key} must not survive a {decision}"
    stored = _record(m, result["review_id"])[1]
    assert stored["decision_note"] == "why not"
    assert stored["history"][-1]["decision_note"] == "why not"


def test_resolve_with_the_current_review_id_proceeds(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    review_id = "".join(list(m._ensure_review_request(page)["review_id"]))

    assert m._resolve_review(PAGE, "approve", "rev", "", version, digest,
                             review_id=review_id)["ok"] is True


def test_resolve_refuses_a_stale_review_id_that_sorts_before_the_current(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)

    assert m._resolve_review(PAGE, "approve", "rev", "", version, digest,
                             review_id="review:a") == {
        "ok": False, "status": 409, "error": "review request is stale"}


@pytest.mark.parametrize("delta", [-1, 1])
def test_resolve_refuses_any_other_page_version(vault, delta):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor", "version": 5})
    version, digest = _lock(m, page)

    result = m._resolve_review(PAGE, "approve", "rev", "", version + delta, digest)

    assert result == {"ok": False, "status": 409,
                      "error": "subject changed; refresh before deciding",
                      "current_version": 5, "current_hash": digest}


def test_resolve_compares_large_versions_by_value(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor", "version": 100000})
    _, digest = _lock(m, page)

    assert m._resolve_review(PAGE, "approve", "rev", "", int("100000"), digest)["ok"] is True


def test_resolve_refuses_an_already_closed_review(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    rp, stored, _ = _record(m, rec["review_id"])
    stored["state"] = "dismissed"
    rp.write_text(yaml.safe_dump(stored), encoding="utf-8")

    assert m._resolve_review(PAGE, "approve", "rev", "", version, digest) == {
        "ok": False, "status": 409, "error": "review request is already closed"}


def test_resolve_input_refusals_are_exact(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    assert m._resolve_review(PAGE, "approve", "rev", "", "x", digest) == {
        "ok": False, "status": 400, "error": "expected page version is required"}
    assert m._resolve_review(PAGE, "reject", "rev", "", version, digest) == {
        "ok": False, "status": 400, "error": "reject requires a decision note"}
    assert m._resolve_review("vendors/ghost.md", "approve", "rev", "", 1, "0" * 64) == {
        "ok": False, "status": 404, "error": "subject page not found"}


def test_resolve_refuses_a_page_with_corrupt_bytes(vault):
    m, root = vault
    page = root / "wiki" / PAGE
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\ntype: vendor\n---\nbad \xff byte\n")

    result = m._resolve_review(PAGE, "approve", "rev", "", 1, "0" * 64)

    assert result["ok"] is False and result["status"] == 422
    assert "invalid UTF-8" in result["error"]


def test_resolve_refuses_outside_the_extension_scope(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    with _Caller(m, monkeypatch, scopes=("topics/**",), operations=["review"], types=["*"]):
        result = m._resolve_review(PAGE, "approve", "rev", "", version, digest)
    assert result["ok"] is False and result["status"] == 403
    assert "write scope" in result["error"]


def test_resolve_refuses_without_review_capability(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    with _Caller(m, monkeypatch, operations=["update"], types=["*"]):
        result = m._resolve_review(PAGE, "approve", "rev", "", version, digest)
    assert result["ok"] is False and result["status"] == 403
    assert result["error"]


def test_resolve_refuses_a_non_vault_relative_subject(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    real_rel = m._rel
    fake_rel = mock.create_autospec(real_rel, side_effect=lambda p: "/" + real_rel(p))
    monkeypatch.setattr(m, "_rel", fake_rel)

    result = m._resolve_review(PAGE, "approve", "rev", "", version, digest)

    assert result["ok"] is False and result["status"] == 500
    assert "non-vault-relative" in result["error"]


def test_resolve_refuses_a_decision_the_schema_rejects(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "entity"})  # entity requires `name`
    version, digest = _lock(m, page)
    before = page.read_text(encoding="utf-8")

    result = m._resolve_review(PAGE, "approve", "rev", "", version, digest)

    assert result["ok"] is False and result["status"] == 422
    assert result["error"].startswith("review update violates schema: ")
    assert page.read_text(encoding="utf-8") == before


def test_failed_record_publish_restores_the_already_published_page(vault, monkeypatch):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    version, digest = _lock(m, page)
    rec = m._ensure_review_request(page)
    rp = m._review_record_path(rec["review_id"])
    before_page = page.read_text(encoding="utf-8")
    before_record = rp.read_text(encoding="utf-8")
    real_replace = os.replace

    def replace(src, dst, *a, **kw):
        if Path(dst) == rp:
            raise OSError("disk full")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", mock.create_autospec(real_replace, side_effect=replace))

    result = m._resolve_review(PAGE, "approve", "rev", "", version, digest)

    assert result == {"ok": False, "status": 500,
                      "error": "atomic review write failed: disk full"}
    assert page.read_text(encoding="utf-8") == before_page
    assert rp.read_text(encoding="utf-8") == before_record
    assert not list(page.parent.glob(".review-page-*"))
    assert not list(rp.parent.glob(".review-record-*"))


def test_restore_removes_a_record_that_did_not_exist_before(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    record = root / "wiki" / "operational" / "reviews" / "new.yaml"
    record.parent.mkdir(parents=True)
    record.write_text("state: approved\n", encoding="utf-8")

    m._restore_review_outputs(page, "old page", record, None, False, True)
    assert not record.exists()
    # Restoring an already-absent record is idempotent.
    m._restore_review_outputs(page, "old page", record, None, False, True)
    assert page.read_text(encoding="utf-8") != "old page"


def test_restore_rewrites_a_published_page_and_prior_record(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    record = root / "wiki" / "operational" / "reviews" / "r.yaml"
    record.parent.mkdir(parents=True)
    record.write_text("state: approved\n", encoding="utf-8")

    m._restore_review_outputs(page, "old page", record, "state: open\n", True, True)

    assert page.read_text(encoding="utf-8") == "old page"
    assert record.read_text(encoding="utf-8") == "state: open\n"


# --- machine review --------------------------------------------------------------------------


def test_machine_review_appends_a_check_without_deciding(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    rec = m._ensure_review_request(page)
    _set_record_version(m, rec["review_id"], 3)

    result = m._record_machine_review(PAGE, "grounder", "supported", "all claims sourced ä")

    rp, stored, raw = _record(m, rec["review_id"])
    check = stored["machine_checks"][-1]
    assert result == {"ok": True, "status": 200, "review_id": rec["review_id"],
                      "state": "open", "machine_check": check}
    assert check["evaluator"] == "grounder" and check["note"] == "all claims sourced ä"
    assert stored["version"] == 4
    assert raw.splitlines()[0] == "version: 4"
    assert "all claims sourced ä" in raw
    assert _mode(rp) == 0o644
    # A second check reuses the lock directory and counts on.
    assert m._record_machine_review(PAGE, "", "unresolved")["ok"] is True
    stored = _record(m, rec["review_id"])[1]
    assert stored["version"] == 5
    assert stored["machine_checks"][-1]["evaluator"] == "machine"
    assert stored["machine_checks"][-1]["note"] == ""


def test_machine_review_treats_a_missing_record_version_as_one(vault):
    m, root = vault
    page = _write_page(root, PAGE, {"type": "vendor"})
    rec = m._ensure_review_request(page)
    _set_record_version(m, rec["review_id"], None)

    assert m._record_machine_review(PAGE, "grounder", "supported")["ok"] is True

    assert _record(m, rec["review_id"])[1]["version"] == 2


def test_machine_review_refusals_are_exact(vault, monkeypatch):
    m, root = vault
    assert m._record_machine_review(PAGE, "g", "maybe") == {
        "ok": False, "status": 400, "error": "invalid machine review outcome"}
    assert m._record_machine_review("vendors/ghost.md", "g", "supported") == {
        "ok": False, "status": 404, "error": "subject page not found"}
    page = root / "wiki" / PAGE
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\ntype: vendor\n---\nbad \xff byte\n")
    result = m._record_machine_review(PAGE, "g", "supported")
    assert result["ok"] is False and result["status"] == 422


def test_machine_review_refuses_outside_scope_or_capability(vault, monkeypatch):
    m, root = vault
    _write_page(root, PAGE, {"type": "vendor"})
    with _Caller(m, monkeypatch, scopes=("topics/**",), operations=["review"], types=["*"]):
        scoped = m._record_machine_review(PAGE, "g", "supported")
    with _Caller(m, monkeypatch, operations=["update"], types=["*"]):
        capped = m._record_machine_review(PAGE, "g", "supported")
    assert scoped["ok"] is False and scoped["status"] == 403 and "write scope" in scoped["error"]
    assert capped["ok"] is False and capped["status"] == 403 and capped["error"]
