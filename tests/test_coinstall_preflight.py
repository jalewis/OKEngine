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
