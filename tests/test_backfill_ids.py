"""P1 regression: backfill stamps an immutable id (authority or minted slug),
is idempotent, never recomputes, and disambiguates slug collisions.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "backfill_ids.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_ids", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["backfill_ids"] = m
    spec.loader.exec_module(m)
    return m


def _page(vault: Path, rel: str, fm: str) -> Path:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}\n---\nbody here\n", encoding="utf-8")
    return p


def _schema(vault: Path):
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    (vault / "wiki" / "schema.yaml").write_text(
        "types:\n"
        "  attack-pattern: {required: [type], id_authority: mitre, id_field: technique_id}\n"
        "  vendor: {required: [type]}\n"
    )


def _id_of(p: Path) -> str:
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    return fm.get("id")


def test_stamps_authority_and_minted_slug(tmp_path):
    m = _load()
    _schema(tmp_path)
    ap = _page(tmp_path, "attack-pattern/t/t1059.md", "type: attack-pattern\ntechnique_id: T1059")
    ve = _page(tmp_path, "entities/a/acme.md", "type: vendor\ntitle: Acme Corp")
    res = m.run(tmp_path, apply=True)
    assert res["stamped"] == 2
    assert _id_of(ap) == "mitre:t1059"          # authority id
    assert _id_of(ve) == "entities:acme-corp"   # minted slug scoped to namespace
    assert ap.read_text().splitlines()[1].startswith('id:')   # id is the first FM line
    assert "body here" in ap.read_text()        # body preserved


def test_idempotent_and_never_recomputes(tmp_path):
    m = _load()
    _schema(tmp_path)
    ve = _page(tmp_path, "entities/a/acme.md", "type: vendor\ntitle: Acme Corp")
    m.run(tmp_path, apply=True)
    first = _id_of(ve)
    # re-run stamps nothing
    assert m.run(tmp_path, apply=True)["stamped"] == 0
    # renaming the page does NOT change the already-stamped id (immutable)
    ve.write_text(ve.read_text().replace("Acme Corp", "Acme Corporation Renamed"))
    m.run(tmp_path, apply=True)
    assert _id_of(ve) == first


def test_slug_collision_is_disambiguated(tmp_path):
    m = _load()
    _schema(tmp_path)
    a = _page(tmp_path, "entities/a/one.md", "type: vendor\ntitle: Acme")
    b = _page(tmp_path, "entities/a/two.md", "type: vendor\ntitle: Acme")  # same name
    res = m.run(tmp_path, apply=True)
    ids = {_id_of(a), _id_of(b)}
    assert "entities:acme" in ids and len(ids) == 2   # one base, one disambiguated
    assert len(res["slug_collisions"]) == 1


def test_dry_run_writes_nothing(tmp_path):
    m = _load()
    _schema(tmp_path)
    ve = _page(tmp_path, "entities/a/acme.md", "type: vendor\ntitle: Acme Corp")
    res = m.run(tmp_path, apply=False)
    assert res["stamped"] == 1
    assert _id_of(ve) is None                          # nothing written in dry run
