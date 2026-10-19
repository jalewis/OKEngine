"""okengine#1028 QA round 2, kept as regressions: the dangling-relationship repair must never clear
a link to a page that EXISTS (N2: an id-form value, a bare wikilink and an ambiguous or case-variant
stem were cleared at 5c5cd613), and free text a contract names a relationship is never dangling."""
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path
import pytest, yaml

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "tests"), str(REPO / "scripts/cron")]
import test_identity_guard_1028 as T  # noqa: E402

ACTOR = "cron:okengine.lacuna"


def _env(tmp_path, monkeypatch, rel_field="prediction_candidate"):
    m, vault = T._load(tmp_path, monkeypatch, "okpack-competitive", "main", actor=ACTOR, grant={"fields": []})
    contract = {"api": 1, "allowed_namespaces": ["*"], "allowed_types": ["*"],
                "operations": ["update", "patch", "converge"], "required_fields": ["type"],
                "required_relationships": [], "optional_relationships": [rel_field],
                "body": {"required": False}, "unknown_fields": "allow", "unresolved_links": "allow",
                "placeholder_links": "allow", "completion": "run"}
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    (vault / "jobs.json").write_text(json.dumps({"jobs": [{
        "name": ACTOR.removeprefix("cron:"), "output_contract": contract,
        "output_contract_digest": "sha256:" + hashlib.sha256(raw.encode()).hexdigest()}]}))
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(vault / "jobs.json"))
    return m, vault


def _target(vault, rel, fm=None):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm or {"type": "prediction", "title": "P"}) + "---\nx\n")
    return p


def _clear(m, vault, value, field="prediction_candidate"):
    page = T._seed(m, "entities/gap.md", {"type": "product", "name": "Gap", "id": "entities:gap", field: value})
    out = m._update("entities/gap.md", {"type": "product", "name": "Gap", "id": "entities:gap", field: None}, None)
    return out, T._fm(page).get(field)


def test_truly_dangling_is_cleared(tmp_path, monkeypatch):           # positive control (lacuna)
    m, vault = _env(tmp_path, monkeypatch)
    out, v = _clear(m, vault, "predictions/nope")
    assert not v, out


@pytest.mark.parametrize("setup,value", [
    ("exact", "predictions/p1"),
    ("exact", "[[predictions/p1]]"),
    ("exact", "[[predictions/p1|Label]]"),
    ("tombstoned", "predictions/p1"),
    ("sharded", "predictions/p1"),            # page moved into a shard dir
    ("bare-wikilink", "[[p1]]"),              # standard OKF bare wikilink
    ("id-form", "predictions:p1"),            # id, not path
    ("case", "predictions/P1"),               # case variant of an existing slug
    ("duplicate", "predictions/p1"),          # partition dup: same stem twice in namespace
    ("cross-namespace", "predictions/p1"),    # page now lives in another namespace
])
def test_existing_target_is_never_cleared(tmp_path, monkeypatch, setup, value):
    m, vault = _env(tmp_path, monkeypatch)
    if setup in ("exact", "bare-wikilink", "id-form", "case"):
        _target(vault, "predictions/p1.md", {"type": "prediction", "title": "P", "id": "predictions:p1"})
    elif setup == "tombstoned":
        _target(vault, "predictions/p1.md", {"type": "prediction", "status": "tombstoned", "id": "predictions:p1"})
    elif setup == "sharded":
        _target(vault, "predictions/p/1/p1.md", {"type": "prediction", "id": "predictions:p1"})
    elif setup == "duplicate":
        _target(vault, "predictions/a/p1.md", {"type": "prediction", "id": "predictions:p1"})
        _target(vault, "predictions/b/p1.md", {"type": "prediction", "id": "predictions:p1"})
    elif setup == "cross-namespace":
        _target(vault, "assessments/p1.md", {"type": "prediction", "id": "predictions:p1"})
    out, v = _clear(m, vault, value)
    assert v, (setup, out)


def test_free_text_field_declared_as_relationship_is_not_clearable(tmp_path, monkeypatch):
    """A contract may name ANY field a relationship; a non-link value always 'fails to resolve'."""
    m, vault = _env(tmp_path, monkeypatch, rel_field="country")
    out, v = _clear(m, vault, "CN", field="country")
    assert v == "CN", out


@pytest.mark.parametrize("mode", ["report", "enforce"])
def test_required_relationship_id_form_not_cleared(tmp_path, monkeypatch, mode):
    """okcti stores 485 subject_ref values in id form (`entities:qtfy`) for EXISTING pages; the
    resolver calls them dangling. inquiry-analyze declares subject_ref a REQUIRED relationship."""
    m, vault = _env(tmp_path, monkeypatch, rel_field="subject_ref")
    c = json.loads((vault / "jobs.json").read_text())
    con = c["jobs"][0]["output_contract"]; con["optional_relationships"] = []; con["required_relationships"] = ["subject_ref"]
    raw = json.dumps(con, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    c["jobs"][0]["output_contract_digest"] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    (vault / "jobs.json").write_text(json.dumps(c))
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", mode)
    _target(vault, "entities/qtfy.md", {"type": "product", "name": "QTFY", "id": "entities:qtfy"})
    out, v = _clear(m, vault, "entities:qtfy", field="subject_ref")
    assert v == "entities:qtfy", (mode, out)
