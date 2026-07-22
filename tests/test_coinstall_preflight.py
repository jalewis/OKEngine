"""HIGH #2 regression: install-domain co-install must reject a guest whose pack.yaml trust differs
from the host's — else a private guest's content is served on a public host's unauthenticated reader
(the reader/cockpit serve one global trust, frozen from the HOST at first deploy)."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "coinstall_preflight.py"


def _mod():
    spec = importlib.util.spec_from_file_location("coinstall_preflight", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["coinstall_preflight"] = m
    spec.loader.exec_module(m)
    m.FINDINGS.clear()
    return m


def _pack(d: Path, trust: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(f"name: p\ntrust: {trust}\n")
    (d / "schema.yaml").write_text("types: {}\n")
    return d


def test_check_trust_fails_private_guest_on_public_host(tmp_path):
    m = _mod()
    m.check_trust(_pack(tmp_path / "host", "public"), _pack(tmp_path / "guest", "private"))
    assert any(l == "FAIL" and a == "trust" for l, a, _ in m.FINDINGS), m.FINDINGS


def test_check_trust_passes_when_aligned(tmp_path):
    m = _mod()
    m.check_trust(_pack(tmp_path / "host", "public"), _pack(tmp_path / "guest", "public"))
    assert not any(a == "trust" for _, a, _ in m.FINDINGS), m.FINDINGS


def test_check_trust_defaults_private_and_flags_public_host(tmp_path):
    """A guest with NO trust declared defaults to private (engine default) — still flagged on a public host."""
    m = _mod()
    host = _pack(tmp_path / "host", "public")
    guest = tmp_path / "guest"; guest.mkdir()
    (guest / "pack.yaml").write_text("name: p\n")            # no trust -> defaults private
    (guest / "schema.yaml").write_text("types: {}\n")
    m.check_trust(host, guest)
    assert any(l == "FAIL" and a == "trust" for l, a, _ in m.FINDINGS), m.FINDINGS


def test_check_trust_allows_public_guest_on_private_host(tmp_path):
    """The SAFE direction: a public guest on a private host is over-protected (served privately), not
    leaked — so it must NOT fail (this is what broke the alias-merge tests when the check was symmetric)."""
    m = _mod()
    m.check_trust(_pack(tmp_path / "host", "private"), _pack(tmp_path / "guest", "public"))
    assert not any(a == "trust" for _, a, _ in m.FINDINGS), m.FINDINGS


def test_extension_owned_type_collision_flagged(tmp_path):  # okengine#326 [10]
    """A pack type that collides with an ENABLED EXTENSION's owned type must FAIL — the root-schema
    checks miss it because the extension's ids live in the composed artifact's owners map, not the
    host's schema.yaml."""
    m = _mod()
    host = tmp_path / "host"; (host / ".okengine").mkdir(parents=True)
    (host / "schema.yaml").write_text("types: {}\n")   # host ROOT declares nothing colliding
    (host / ".okengine" / "composed-schema.yaml").write_text(
        "types: {assessment: {}, gadget: {}}\n"
        "owners:\n  types: {assessment: 'ext:okengine.assessments', gadget: 'ext:demo.gadgets'}\n"
        "  namespaces: {gadgets: 'ext:demo.gadgets'}\n")
    pack = tmp_path / "pack"; pack.mkdir()
    (pack / "pack.yaml").write_text("name: p\ntrust: private\n")
    (pack / "schema.yaml").write_text(
        "types: {assessment: {required: [x]}}\n"          # collides with an ext-owned type
        "partitioning: {namespaces: {gadgets: {}}}\n")     # collides with an ext-owned namespace
    m.check_extension_collisions(host, pack)
    fails = [msg for lvl, area, msg in m.FINDINGS if lvl == "FAIL"]
    assert any("assessment" in f and "okengine.assessments" in f for f in fails), m.FINDINGS
    assert any("gadgets" in f and "demo.gadgets" in f for f in fails), m.FINDINGS
    # subtree shape downgrades to WARN (walk-up separates the contracts), never silent
    m.FINDINGS.clear()
    m.check_extension_collisions(host, pack, subtree=True)
    assert not any(lvl == "FAIL" for lvl, _, _ in m.FINDINGS), m.FINDINGS
    assert any(lvl == "WARN" and "assessment" in msg for lvl, _, msg in m.FINDINGS), m.FINDINGS


def test_no_composed_schema_produces_no_false_fail(tmp_path):  # okengine#326 [10] regression
    """An absent .okengine/composed-schema.yaml (no enabled extensions, or a host being composed
    fresh — the pack-parity taxonomy-shape case) must NOT emit a FAIL: the check simply has nothing
    to compare against. (Regression: the first cut called _yaml() unconditionally, which added a
    spurious 'unparseable yaml' FAIL on the missing file and broke the pack-parity gate.)"""
    m = _mod()
    host = tmp_path / "host"; host.mkdir()
    (host / "schema.yaml").write_text("types: {}\n")            # NO composed-schema.yaml
    pack = tmp_path / "pack"; pack.mkdir()
    (pack / "schema.yaml").write_text("types: {gadget: {}}\npartitioning: {namespaces: {gadgets: {}}}\n")
    m.check_extension_collisions(host, pack)
    assert m.FINDINGS == [], m.FINDINGS
