"""P2 regression: converge_entity upserts by id — authority ids merge into one
canonical page, slug collisions and tombstoned ids are refused, ownership holds.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"

SCHEMA = (
    "types:\n"
    "  attack-pattern:\n"
    "    required: [type]\n"
    "    id_authority: mitre\n"
    "    id_field: technique_id\n"
    "    owner: atk\n"
    "    field_owners: {detection: hunt}\n"
    "  vendor: {required: [type]}\n"
)


def _load(wiki_path: Path, schema: str = SCHEMA):
    os.environ["WIKI_PATH"] = str(wiki_path)
    os.environ["OKENGINE_MCP_WRITE_DATE"] = "2026-06-16"
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    (wiki_path / "wiki").mkdir(parents=True, exist_ok=True)
    (wiki_path / "wiki" / "schema.yaml").write_text(schema)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    assert m._CONVERGE_OK, "converge libs should import"
    return m


def _read_id(m, rel):
    import yaml
    fm, _ = m._read_page(Path(os.environ["WIKI_PATH"]) / "wiki" / rel)
    return fm


def test_converge_creates_new_with_minted_slug(tmp_path):
    m = _load(tmp_path)
    out = m._converge("entities/acme.md", "type: vendor\ntitle: Acme Corp", "body")
    assert out.startswith("created")
    assert _read_id(m, "entities/acme.md")["id"] == "entities:acme-corp"


def test_authority_id_merges_into_one_canonical_page(tmp_path):
    m = _load(tmp_path)
    # attack pack creates the technique
    a = m._converge("attack-pattern/t1059.md",
                    "type: attack-pattern\ntechnique_id: T1059\ntactic: execution", pack="atk")
    assert a.startswith("created")
    assert _read_id(m, "attack-pattern/t1059.md")["id"] == "mitre:t1059"
    # hunt pack writes the SAME technique at a different path, adding `detection`
    b = m._converge("attack-pattern/dup.md",
                    "type: attack-pattern\ntechnique_id: T1059\ndetection: sigma-rule", pack="hunt")
    assert b.startswith("converged into attack-pattern/t1059.md")
    assert not (tmp_path / "wiki" / "attack-pattern" / "dup.md").exists()   # no duplicate
    fm = _read_id(m, "attack-pattern/t1059.md")
    assert fm["tactic"] == "execution" and fm["detection"] == "sigma-rule"  # both coexist
    assert set(fm["maintained_by"]) == {"atk", "hunt"}                      # provenance union


def test_nonowner_field_conflict_is_flagged_not_clobbered(tmp_path):
    m = _load(tmp_path)
    m._converge("attack-pattern/t1059.md",
                "type: attack-pattern\ntechnique_id: T1059\ntactic: execution", pack="atk")
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: HIJACK", pack="hunt")
    assert "1 conflict" in out and "flagged for review" in out
    assert _read_id(m, "attack-pattern/t1059.md")["tactic"] == "execution"   # not clobbered


def test_owner_authorized_removal_via_converge(tmp_path):
    m = _load(tmp_path)
    m._converge("attack-pattern/t1059.md",
                "type: attack-pattern\ntechnique_id: T1059\ntactic: execution\nstale: x", pack="atk")
    # owner drops `stale`
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059", pack="atk", remove="stale")
    assert "-1 removed" in out
    assert "stale" not in _read_id(m, "attack-pattern/t1059.md")
    # a non-owner cannot remove an unowned field -> flagged, kept
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059", pack="hunt", remove="tactic")
    assert "conflict" in out and "flagged for review" in out
    assert _read_id(m, "attack-pattern/t1059.md")["tactic"] == "execution"


def test_slug_collision_refused(tmp_path):
    m = _load(tmp_path)
    m._converge("entities/one.md", "type: vendor\ntitle: Acme")
    out = m._converge("entities/two.md", "type: vendor\ntitle: Acme")    # same minted slug
    assert out.startswith("refused: slug id entities:acme already used")
    assert not (tmp_path / "wiki" / "entities" / "two.md").exists()


def test_tombstoned_id_refused(tmp_path):
    m = _load(tmp_path)
    m._converge("entities/acme.md", "type: vendor\ntitle: Acme Corp")
    m._tombstone("entities/acme.md", "merged elsewhere")
    m._registries.clear()                                                # re-scan: sees tombstone
    out = m._converge("entities/acme.md", "type: vendor\ntitle: Acme Corp")
    assert "tombstoned" in out and out.startswith("refused")


def test_create_authority_variant_converges_not_duplicates(tmp_path):
    """okengine#99/#100 via the id-aware create path: a create_entity for an
    AUTHORITY-bound entity that already exists at another path (different filename
    / `<type>--` mint prefix / wrong namespace) resolves to the same authority id
    and CONVERGES into the canonical instead of forking a second canonical."""
    m = _load(tmp_path)
    a = m._create("attack-pattern/t1059.md",
                  "type: attack-pattern\ntechnique_id: T1059\ntactic: execution")
    assert a.startswith("created"), a
    assert _read_id(m, "attack-pattern/t1059.md")["id"] == "mitre:t1059"
    # same technique, different namespace + `<type>--` minted filename -> mitre:t1059
    b = m._create("entities/a/attack-pattern--t1059.md",
                  "type: attack-pattern\ntechnique_id: T1059\ndetection: sigma")
    assert b.startswith("converged into attack-pattern/t1059.md"), b
    assert not (tmp_path / "wiki" / "entities" / "a" / "attack-pattern--t1059.md").exists()
    fm = _read_id(m, "attack-pattern/t1059.md")
    assert fm["tactic"] == "execution" and fm["detection"] == "sigma"   # merged, one canonical


# --- #21: converge must not bypass write-governance on existing pages ---

_PERM_SCHEMA = (
    "types:\n  finding: {required: [type]}\n"
    "permissions:\n"
    "  default: {create: true, update: true, delete: false}\n"
    "  namespaces:\n"
    "    findings: {update: false}\n"          # create ok, update human-only
)

_REVIEW_SCHEMA = (
    "types:\n  vendor: {required: [type]}\n"
    "review:\n"
    "  confidence_field: confidence\n"
    "  confidence_review_values: [confirmed, refuted]\n"
)


def test_converge_respects_human_only_namespace(tmp_path):
    """Existing-page converge into an update-denied namespace is REFUSED (was a
    governance bypass), leaving the page untouched."""
    m = _load(tmp_path, _PERM_SCHEMA)
    assert m._converge("findings/f1.md", "type: finding\ntitle: One\nseverity: low").startswith("created")
    out = m._converge("findings/f1.md", "type: finding\ntitle: One\nseverity: HIGH")
    assert out.startswith("rejected") and "update denied" in out
    assert _read_id(m, "findings/f1.md")["severity"] == "low"     # untouched


def test_converge_applies_review_flags(tmp_path):
    """A categorical confidence verdict via converge flags the page (needs_review +
    queue), same as update_entity — the write lands but is not silent."""
    m = _load(tmp_path, _REVIEW_SCHEMA)
    m._converge("entities/acme.md", "type: vendor\ntitle: Acme Corp")
    out = m._converge("entities/acme.md", "type: vendor\ntitle: Acme Corp\nconfidence: confirmed")
    assert "flagged for review" in out
    fm = _read_id(m, "entities/acme.md")
    assert fm.get("needs_review") is True and fm.get("confidence") == "confirmed"
