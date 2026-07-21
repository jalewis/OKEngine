"""Regression: enum case-canonicalization at the write path (okengine#226).

The D7 class: 10,378 pages carried `tlp: clear` against the uppercase base enum — written
before enum enforcement / via direct-write importers. A case-insensitive match to exactly
one allowed value is unambiguous intent: coerce at the boundary; genuinely unknown values
still reject. Write-path tests call the plain helpers (mcp may be absent on system python).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"
SV_MOD = REPO / "tools" / "schema_validator.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  source:
    required: [type]
  prediction:
    required: [type]
strict_types: false
field_items:
  evidence:
    direction: {enum: [reinforces, contradicts, partial, neutral]}
"""


def _load_ws():
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


def _load_sv():
    spec = importlib.util.spec_from_file_location("schema_validator_cc", SV_MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    sys.modules.pop("write_server", None)
    m = _load_ws()
    return m, tmp_path


# ── the unit surface: canonicalize_enum_case ──────────────────────────────────────────

def test_canonicalize_scalar_list_and_ambiguity():
    sv = _load_sv()
    schema = {
        "enums": {"tlp": ["CLEAR", "GREEN", "AMBER"], "twin": ["Dup", "dup"]},
        "field_enums": {"tlp": {"enum": "tlp"}, "twin": {"enum": "twin"}},
    }
    fm = {"tlp": "clear"}
    changes = sv.canonicalize_enum_case(schema, "source", fm)
    assert fm["tlp"] == "CLEAR" and changes == ["tlp: 'clear' -> 'CLEAR'"]
    # list-valued field: each element coerces independently
    fm = {"tlp": ["green", "AMBER", "chartreuse"]}
    sv.canonicalize_enum_case(schema, "source", fm)
    assert fm["tlp"] == ["GREEN", "AMBER", "chartreuse"]   # unknown left for the reject
    # ambiguous case-collision (Dup vs dup): left alone
    fm = {"twin": "DUP"}
    assert sv.canonicalize_enum_case(schema, "source", fm) == []
    assert fm["twin"] == "DUP"
    # exact match: untouched, no change recorded
    fm = {"tlp": "CLEAR"}
    assert sv.canonicalize_enum_case(schema, "source", fm) == []


# ── the write path: tlp rides the base-schema enum ────────────────────────────────────

def test_create_lowercase_tlp_lands_canonical(vault):
    m, root = vault
    res = m._create("sources/s-case", {"type": "source", "tlp": "clear"}, "# S\n\nBody.\n")
    assert not res.startswith("rejected:"), res
    page = (root / "wiki" / "sources" / "s-case.md").read_text(encoding="utf-8")
    assert "tlp: CLEAR" in page and "tlp: clear" not in page


def test_create_unknown_tlp_still_rejects(vault):
    m, root = vault
    res = m._create("sources/s-bad", {"type": "source", "tlp": "chartreuse"}, "# S\n\nBody.\n")
    assert res.startswith("rejected:"), res
    assert "chartreuse" in res and "tlp" in res
    assert not (root / "wiki" / "sources" / "s-bad.md").exists()


def test_update_coerces_case_too(vault):
    m, root = vault
    m._create("sources/s-up", {"type": "source", "tlp": "GREEN"}, "# S\n\nBody.\n")
    res = m._update("sources/s-up", {"tlp": "amber"})
    assert not res.startswith("rejected:"), res
    page = (root / "wiki" / "sources" / "s-up.md").read_text(encoding="utf-8")
    assert "tlp: AMBER" in page


def test_extensible_enum_coerces_known_case_passes_novel(vault):
    """severity is extensible in base: `HIGH` case-coerces to the existing `high`;
    a genuinely novel value still passes (extensible enums are legal to extend)."""
    m, root = vault
    res = m._create("sources/s-sev", {"type": "source", "severity": "HIGH"}, "# S\n\nBody.\n")
    assert not res.startswith("rejected:"), res
    page = (root / "wiki" / "sources" / "s-sev.md").read_text(encoding="utf-8")
    assert "severity: high" in page
    res = m._create("sources/s-sev2", {"type": "source", "severity": "catastrophic"},
                    "# S\n\nBody.\n")
    assert not res.startswith("rejected:"), res


# ── item-level: evidence[].direction case-coerces (#211 guard + #226 coercion) ────────

def test_item_direction_case_coerces_and_junk_still_rejects(vault):
    m, root = vault
    fm = {"type": "prediction", "evidence": [{"direction": "Reinforces"}]}
    res = m._create("predictions/p-case", fm, "# P\n\nBody.\n")
    assert not res.startswith("rejected:"), res
    page = (root / "wiki" / "predictions" / "p-case.md").read_text(encoding="utf-8")
    assert "direction: reinforces" in page and "Reinforces" not in page
    fm = {"type": "prediction", "evidence": [{"direction": "confirms"}]}
    res = m._create("predictions/p-case-bad", fm, "# P\n\nBody.\n")
    assert res.startswith("rejected:") and "confirms" in res
