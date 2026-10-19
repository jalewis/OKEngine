"""okengine#832: a review record's `subject` must be the vault-relative join key.

`_safe()` returns `p.resolve()` and checks containment against `wiki.resolve()`; `_rel()` used to
relativize against the UNRESOLVED `_wiki()`. Wherever the vault path crosses a symlink the two
disagree, `relative_to` raises, and the fallback hands back an ABSOLUTE path -- which the ledger
then stores as the record `subject`. The Cockpit worklist joins decisions to pages by exactly that
key, so the decision becomes unjoinable while the page's own projection still reads as reviewed:
a decided page above a ledger that cannot corroborate it.

In-container the vault is `/opt/vault` with no symlink, so every gateway lane agreed and only
host-run callers (`framework review`) diverged. That is why it survived: the fixture has to have a
symlink in it to see the bug at all.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"
SCHEMA = "types:\n  actor: {required: [type]}\n"
PAGE = "---\ntype: actor\nname: N\nversion: 2\nneeds_review: true\n---\n\nbody\n"


def _load(vault: Path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


def _vault(tmp_path: Path) -> Path:
    real = tmp_path / "real-vault"
    (real / "wiki" / "entities" / "a" / "p").mkdir(parents=True)
    (real / "wiki" / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    (real / "wiki" / "entities" / "a" / "p" / "apt-c-27.md").write_text(PAGE, encoding="utf-8")
    return real


def test_subject_is_relative_when_the_vault_is_reached_through_a_symlink(tmp_path, monkeypatch):
    """The reported shape: WIKI_PATH points at a symlink to the real vault."""
    real = _vault(tmp_path)
    link = tmp_path / "linked-vault"
    link.symlink_to(real, target_is_directory=True)
    m = _load(link, monkeypatch)                      # <- vault addressed through the link
    page = m._safe("entities/a/p/apt-c-27")
    assert page is not None and page.is_file()
    _fm, _body, subject, version, _digest = m._review_page_state(page)
    assert subject == "entities/a/p/apt-c-27", f"expected the vault-relative key, got {subject!r}"
    assert not subject.startswith("/")
    assert version == 2


def test_subject_is_relative_without_a_symlink_too(tmp_path, monkeypatch):
    """The in-container shape, which already worked -- pinned so the fix cannot regress it."""
    real = _vault(tmp_path)
    m = _load(real, monkeypatch)
    _fm, _body, subject, _v, _d = m._review_page_state(m._safe("entities/a/p/apt-c-27"))
    assert subject == "entities/a/p/apt-c-27"


def test_both_addressings_agree_on_the_key(tmp_path, monkeypatch):
    """The defect was a DIVERGENCE: the same page keyed two ways depending on how the vault was
    addressed. Equality is the contract, not merely 'relative'."""
    real = _vault(tmp_path)
    link = tmp_path / "linked-vault"
    link.symlink_to(real, target_is_directory=True)
    direct = _load(real, monkeypatch)
    a = direct._review_page_state(direct._safe("entities/a/p/apt-c-27"))[2]
    through = _load(link, monkeypatch)
    b = through._review_page_state(through._safe("entities/a/p/apt-c-27"))[2]
    assert a == b == "entities/a/p/apt-c-27"


def test_a_non_relative_subject_is_refused_rather_than_recorded(tmp_path, monkeypatch):
    """Negative fixture. The failure mode is SILENT: an unjoinable record beside a page whose
    projection says reviewed. If the key ever regresses, the write must fail loudly instead."""
    real = _vault(tmp_path)
    m = _load(real, monkeypatch)
    assert m._subject_refusal("entities/a/p/apt-c-27") is None
    for bad in ("/opt/vault/wiki/entities/a/p/apt-c-27", "../escape", ""):
        refusal = m._subject_refusal(bad)
        assert refusal and "not joinable" in refusal or refusal and "joinable" in refusal, bad
        assert "832" in refusal


def test_resolve_review_refuses_a_non_relative_subject(tmp_path, monkeypatch):
    """The guard is wired into the decision path, not merely available."""
    real = _vault(tmp_path)
    m = _load(real, monkeypatch)
    page = m._safe("entities/a/p/apt-c-27")
    _fm, _b, _s, version, digest = m._review_page_state(page)
    monkeypatch.setattr(m, "_review_page_state",
                        lambda p: (_fm, _b, "/absolute/leak", version, digest))
    res = m._resolve_review("entities/a/p/apt-c-27", "approve", "jlewis", "", version, digest)
    assert res["ok"] is False and res["status"] == 500
    assert "832" in res["error"]


def test_resolved_returns_the_path_unchanged_when_the_filesystem_refuses(tmp_path, monkeypatch):
    """`_resolved` exists so a broken or racing mount degrades instead of raising mid-decision.
    `Path.resolve()` can fail with OSError (a symlink loop, a vanished mount), and the review
    decision must still produce a key rather than blow up inside the review lock."""
    m = _load(_vault(tmp_path), monkeypatch)

    def refuse(self, *a, **k):
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(Path, "resolve", refuse)
    stuck = Path("/nowhere/that/resolves")
    assert m._resolved(stuck) == stuck


def test_rel_falls_back_to_the_absolute_form_only_when_the_page_is_truly_outside(
    tmp_path, monkeypatch
):
    """The last resort still exists, and is reached only when BOTH the direct and the resolved
    comparison fail -- here forced by making resolution itself unavailable for a path that is
    genuinely not under the vault. A page outside the vault has no vault-relative key, and saying
    so is correct; the bug was reaching this branch for a page that was INSIDE it."""
    m = _load(_vault(tmp_path), monkeypatch)
    outside = tmp_path / "not-the-vault" / "stray.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(PAGE, encoding="utf-8")

    def refuse(self, *a, **k):
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(Path, "resolve", refuse)
    assert m._rel(outside) == outside.as_posix()
