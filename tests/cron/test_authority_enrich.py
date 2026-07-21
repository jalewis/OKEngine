"""authority_enrich (okengine#314) — deterministic identity-authority stamping.

Contract under test: exactly-one exact match stamps additively with attribution; ambiguity,
disagreement, and duplicate authority IDs go to review (never merged/overwritten); curated fields
and the body are untouched; runs are idempotent; dry-run writes nothing. End-to-end through the
REAL source_connector runtime in fixture mode (no mocked connector).
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "authority_enrich.py"
MANIFEST = REPO / "examples" / "source-connectors" / "ror-organizations.yaml"
FIXTURE = REPO / "examples" / "source-connectors" / "fixtures" / "ror-organizations.fixture.json"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="authority_enrich absent")


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("authority_enrich", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    return m


def _page(p: Path, fm: dict, body: str = "Curated body stays.\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body, encoding="utf-8")


def _fm(p: Path) -> dict:
    return yaml.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))


def _run(m, vault, tmp_path, *extra):
    return m.main(["--manifest", str(MANIFEST), "--fixture", str(FIXTURE),
                   "--state-root", str(tmp_path / "state"),
                   "--ledger-root", str(tmp_path / "ledger.jsonl"), *extra])


def test_exact_match_stamps_additively_with_attribution(tmp_path):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust.md"
    _page(page, {"type": "lab", "name": "Hong Kong University of Science and Technology",
                 "curated_note": "hand-written", "tags": ["research"]})
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    fm = _fm(page)
    assert fm["authority_ids"] == {"ror": "https://ror.org/00q4vv597"}
    obs = fm["authority_observations"]
    assert obs[0]["authority"] == "ror" and obs[0]["source"] == "reference.ror-organizations"
    assert "exact name match" in obs[0]["basis"]
    # additive only: curated fields + body untouched, no review flags
    assert fm["curated_note"] == "hand-written" and fm["tags"] == ["research"]
    assert "needs_review" not in fm and "conflicts" not in fm
    assert "Curated body stays." in page.read_text()
    # coverage artifact observable
    cov = json.loads((v / ".okengine" / "connectors" / "authority" / "ror.json").read_text())
    assert cov["stamped"] == 1 and cov["authority"] == "ror"


def test_acronym_alias_also_matches_via_candidate_paths(tmp_path):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust-short.md"
    _page(page, {"type": "lab", "name": "HKUST"})   # matches names[].value acronym entry
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    assert _fm(page)["authority_ids"]["ror"] == "https://ror.org/00q4vv597"


def test_unmatched_and_wrong_type_are_left_alone(tmp_path):
    v = tmp_path
    nomatch = v / "wiki" / "entities" / "n" / "nomatch.md"
    wrongtype = v / "wiki" / "entities" / "w" / "wrong.md"
    _page(nomatch, {"type": "lab", "name": "Totally Unknown Institute"})
    _page(wrongtype, {"type": "actor", "name": "HKUST"})   # not in targets.types
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    assert "authority_ids" not in _fm(nomatch) and "needs_review" not in _fm(nomatch)
    assert "authority_ids" not in _fm(wrongtype)


def test_existing_disagreeing_id_is_kept_and_review_flagged(tmp_path):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust.md"
    _page(page, {"type": "lab", "name": "Hong Kong University of Science and Technology",
                 "authority_ids": {"ror": "https://ror.org/DIFFERENT"}})
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    fm = _fm(page)
    # NEVER overwritten — and pages already stamped are skipped as eligible, so no conflict churn
    assert fm["authority_ids"]["ror"] == "https://ror.org/DIFFERENT"


def test_duplicate_authority_id_across_pages_goes_to_review(tmp_path):
    v = tmp_path
    a = v / "wiki" / "entities" / "a" / "a.md"
    b = v / "wiki" / "entities" / "b" / "b.md"
    _page(a, {"type": "lab", "name": "A Lab",
              "authority_ids": {"ror": "https://ror.org/00q4vv597"}})
    _page(b, {"type": "lab", "name": "Hong Kong University of Science and Technology"})
    m = _load(v)
    assert _run(m, v, tmp_path) == 0
    fmb = _fm(b)
    # b matched the same ROR id already stamped on a -> review, NOT auto-merged, NOT stamped
    assert fmb.get("needs_review") is True
    assert any("duplicate identity" in c["detail"] for c in fmb["conflicts"])
    assert "authority_ids" not in fmb or "ror" not in (fmb.get("authority_ids") or {})


def test_idempotent_and_dry_run(tmp_path, capsys):
    v = tmp_path
    page = v / "wiki" / "entities" / "h" / "hkust.md"
    _page(page, {"type": "lab", "name": "Hong Kong University of Science and Technology"})
    m = _load(v)
    assert _run(m, v, tmp_path, "--dry-run") == 0
    assert "authority_ids" not in _fm(page)                 # dry-run wrote nothing
    assert not (v / ".okengine" / "connectors" / "authority" / "ror.json").exists()
    assert _run(m, v, tmp_path) == 0
    first = page.read_text()
    assert _run(m, v, tmp_path) == 0                        # second run: already stamped -> skipped
    assert page.read_text() == first
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_manifest_validation_rejects_bad_enrich_blocks():
    import importlib.util as iu
    import sys
    spec = iu.spec_from_file_location("source_connector", REPO / "scripts" / "cron" / "source_connector.py")
    sc = iu.module_from_spec(spec)
    sys.modules["source_connector"] = sc     # dataclasses resolve InitVar via sys.modules[__module__]
    spec.loader.exec_module(sc)
    good = yaml.safe_load(MANIFEST.read_text())
    assert sc.validate_manifest(good) == []
    bad = yaml.safe_load(MANIFEST.read_text())
    bad["mode"] = "poll"                                    # enrich only valid for enrichment mode
    assert any("enrich" in e for e in sc.validate_manifest(bad))
    bad2 = yaml.safe_load(MANIFEST.read_text())
    bad2["enrich"]["match"]["query_input"] = "not_an_input"  # must be a declared required input
    assert any("query_input" in e for e in sc.validate_manifest(bad2))
    bad3 = yaml.safe_load(MANIFEST.read_text())
    del bad3["enrich"]["targets"]
    assert any("targets" in e for e in sc.validate_manifest(bad3))
