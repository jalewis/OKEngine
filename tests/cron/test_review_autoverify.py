"""review_autoverify (okengine#313) — deterministic evidence-graded clearing of needs_review.

The contract under test: the lane clears the flag ONLY by registry arithmetic (1×A or 2×B by
default), refuses whenever anything else is wrong with the page, stamps an auditable basis, and
never lets an ungraded citation count.
"""
import importlib.util
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "review_autoverify.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="review_autoverify absent")

SCHEMA = """\
source_registry:
  Microsoft: {reliability: A}
  MITRE ATT&CK: {reliability: A}
  MISP galaxy: {reliability: B}
  URLhaus: {reliability: B}
types:
  actor: {required: [type, name]}
  source: {required: [type]}
"""


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("review_autoverify", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    # schema_lib is a sys.modules singleton whose _SCHEMA_CACHE keys by path WITHOUT mtime —
    # a rewritten fixture schema.yaml would be served stale across loads; clear per load.
    import sys
    sl = sys.modules.get("schema_lib")
    if sl is not None:
        for cache in ("_SCHEMA_CACHE", "_BASE_CACHE", "_COMPOSED_CACHE"):
            getattr(sl, cache, {}).clear()
    return m


def _page(p: Path, fm: dict, body: str = "body\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body, encoding="utf-8")


def _vault(tmp_path: Path, schema: str = SCHEMA) -> Path:
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    (tmp_path / "wiki").mkdir(exist_ok=True)
    return tmp_path


def _fm(p: Path) -> dict:
    import re
    return yaml.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))


def test_single_A_prose_source_clears(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-x.md"
    _page(page, {"type": "actor", "name": "APT X", "needs_review": True,
                 "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "needs_review" not in fm
    assert fm["review_status"] == "auto-verified"
    assert "Microsoft" in fm["auto_verified_basis"] and "A-grade" in fm["auto_verified_basis"]
    assert fm["auto_verified_at"]


def test_single_B_holds_but_two_distinct_B_clear(tmp_path):
    v = _vault(tmp_path)
    one_b = v / "wiki" / "entities" / "m" / "misp-only.md"
    two_b = v / "wiki" / "entities" / "c" / "corroborated.md"
    _page(one_b, {"type": "actor", "name": "MispOnly", "needs_review": True,
                  "sources": ["MISP galaxy"]})
    _page(two_b, {"type": "actor", "name": "Corr", "needs_review": True,
                  "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    assert _fm(one_b)["needs_review"] is True          # 1×B is not enough
    assert _fm(two_b)["review_status"] == "auto-verified"


def test_linked_source_page_grades_by_publisher(tmp_path):
    v = _vault(tmp_path)
    _page(v / "wiki" / "sources" / "2026" / "07" / "ms-report.md",
          {"type": "source", "publisher": "Microsoft", "url": "https://example.com/x"})
    page = v / "wiki" / "entities" / "s" / "spider.md"
    _page(page, {"type": "actor", "name": "Spider", "needs_review": True,
                 "sources": ["sources/2026/07/ms-report"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "auto-verified"
    assert "Microsoft" in fm["auto_verified_basis"]


def test_refusals_hold_even_with_A_evidence(tmp_path):
    v = _vault(tmp_path)
    conflicted = v / "wiki" / "entities" / "c" / "conflicted.md"
    missing = v / "wiki" / "entities" / "m" / "nameless.md"
    grounding = v / "wiki" / "entities" / "g" / "grounded-bad.md"
    _page(conflicted, {"type": "actor", "name": "C", "needs_review": True,
                       "sources": ["Microsoft"],
                       "conflicts": [{"field": "origin"}]})
    _page(missing, {"type": "actor", "needs_review": True, "sources": ["Microsoft"]})  # no name
    _page(grounding, {"type": "actor", "name": "G", "needs_review": True,
                      "sources": ["Microsoft"]},
          body="## Grounding check\n\n2 unsupported claims found\n")
    assert _load(v).main([]) == 0
    for p in (conflicted, missing, grounding):
        assert _fm(p)["needs_review"] is True, p.name
        assert "review_status" not in _fm(p)


def test_unregistered_prose_source_never_counts(tmp_path):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "f" / "forged.md"
    _page(page, {"type": "actor", "name": "F", "needs_review": True,
                 "sources": ["Totally Real Vendor", "some blog"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True


def test_idempotent_and_dry_run(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-y.md"
    _page(page, {"type": "actor", "name": "APT Y", "needs_review": True,
                 "sources": ["MITRE ATT&CK"]})
    m = _load(v)
    # dry-run: reports the clear but writes nothing
    assert m.main(["--dry-run"]) == 0
    assert _fm(page)["needs_review"] is True
    # real run clears; a second run is a no-op on the same page
    assert m.main([]) == 0
    first = page.read_text()
    assert m.main([]) == 0
    assert page.read_text() == first
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_pack_can_disable_and_tune(tmp_path):
    off = SCHEMA + "review_autoverify: {enabled: false}\n"
    v = _vault(tmp_path, off)
    page = v / "wiki" / "entities" / "a" / "apt-z.md"
    _page(page, {"type": "actor", "name": "APT Z", "needs_review": True, "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True           # disabled -> untouched
    # tuned: require 2×A — one A no longer clears
    tuned = SCHEMA + "review_autoverify: {a_sources: 2}\n"
    (v / "schema.yaml").write_text(tuned, encoding="utf-8")
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True

def test_two_pages_same_publisher_are_one_voice_not_corroboration(tmp_path):
    """Two B-grade articles from the SAME outlet must not clear as 2xB — corroboration means two
    DIFFERENT publishers (caught live: two Akamai pages auto-clearing a lacuna page)."""
    v = _vault(tmp_path)
    for i in (1, 2):
        _page(v / "wiki" / "sources" / "2026" / "07" / f"urlhaus-{i}.md",
              {"type": "source", "publisher": "URLhaus", "url": f"https://example.com/{i}"})
    same = v / "wiki" / "entities" / "s" / "same-voice.md"
    _page(same, {"type": "actor", "name": "SameVoice", "needs_review": True,
                 "sources": ["sources/2026/07/urlhaus-1", "sources/2026/07/urlhaus-2"]})
    mixed = v / "wiki" / "entities" / "m" / "mixed-voices.md"
    _page(mixed, {"type": "actor", "name": "Mixed", "needs_review": True,
                  "sources": ["sources/2026/07/urlhaus-1", "MISP galaxy"]})
    assert _load(v).main([]) == 0
    assert _fm(same)["needs_review"] is True            # 2 pages, 1 publisher -> one voice
    assert _fm(mixed)["review_status"] == "auto-verified"   # URLhaus + MISP galaxy = 2 distinct B

def test_judgment_types_are_never_evidence_cleared(tmp_path):
    """An assessment's needs_review guards the JUDGMENT, not the citations — A-grade sources must
    not clear it (raised by the okcti question: verified pages still carry open CHE assessments)."""
    v = _vault(tmp_path, SCHEMA + "types:\n  assessment: {required: [type]}\n")
    judgment = v / "wiki" / "assessments" / "actor-x-association.md"
    _page(judgment, {"type": "assessment", "needs_review": True,
                     "sources": ["Microsoft", "MITRE ATT&CK"]})     # 2xA — would clear an entity
    assert _load(v).main([]) == 0
    fm = _fm(judgment)
    assert fm["needs_review"] is True and "review_status" not in fm
    # and the exemption is schema-tunable: an empty exempt list restores evidence-clearing
    (v / "schema.yaml").write_text(
        SCHEMA + "types:\n  assessment: {required: [type]}\nreview_autoverify: {exempt_types: []}\n",
        encoding="utf-8")
    import os, time
    future = time.time() + 5
    os.utime(v / "schema.yaml", (future, future))   # defeat schema_lib's mtime cache (same-second rewrite)
    assert _load(v).main([]) == 0
    assert _fm(judgment)["review_status"] == "auto-verified"

