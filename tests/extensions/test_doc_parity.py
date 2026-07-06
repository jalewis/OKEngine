"""Doc/code parity guard for the extension manifest grammar.

The manifest grammar grew faster than the docs this session (multi-op, agent-op, core,
tier landed in code+tests but not the reference) — a silent drift. This test makes the
authoring docs self-maintaining: every author-facing manifest key the validator accepts
MUST be documented, so adding a field without documenting it fails CI.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO / "scripts" / "extension_manifest.py"
DOCS = [REPO / "docs" / "design" / "extension-system.md",
        REPO / "docs" / "authoring-an-extension.md"]

pytestmark = pytest.mark.skipif(not MANIFEST.is_file(), reason="extension modules absent")


def _manifest_mod():
    spec = importlib.util.spec_from_file_location("extension_manifest", MANIFEST)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _doc_text() -> str:
    return "\n".join(d.read_text(encoding="utf-8") for d in DOCS if d.is_file())


def _author_facing_keys(m) -> set[str]:
    # every key an author writes in extension.yaml — top-level, operation, capabilities,
    # and requires sub-keys. (Internal-only helpers, if any, would be excluded here.)
    return set(m._TOP_KEYS) | set(m._OPERATION_KEYS) | set(m._CAP_KEYS) | set(m._REQUIRES_KEYS)


def test_every_manifest_key_is_documented():
    m = _manifest_mod()
    text = _doc_text()
    missing = sorted(k for k in _author_facing_keys(m) if k not in text)
    assert not missing, (
        f"manifest keys accepted by the validator but undocumented in the authoring docs: "
        f"{missing} — add them to extension-system.md §6 / authoring-an-extension.md")


def test_authoring_guide_exists_and_links_the_reference():
    guide = REPO / "docs" / "authoring-an-extension.md"
    assert guide.is_file(), "the step-by-step extension authoring guide is missing"
    body = guide.read_text(encoding="utf-8")
    assert "extension-system.md" in body          # links the field reference
    assert "extension.yaml" in body


def test_known_trust_values_are_documented():
    m = _manifest_mod()
    text = _doc_text()
    missing = [t for t in m.KNOWN_KINDS if t not in text]
    assert not missing, f"manifest kinds undocumented: {missing}"
