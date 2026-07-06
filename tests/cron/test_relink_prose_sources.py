"""relink_prose_sources (okengine#158 P2): prose source entries -> page-refs ONLY on a unique,
confident slug-token match; vague/ambiguous prose is left flagged (no fabrication)."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


_load("schema_lib", "scripts/cron/schema_lib.py")
R = _load("relink_prose_sources", "scripts/cron/relink_prose_sources.py")

IDX = {
    "sources/2026/06/eset-gamaredon-russia-aligned-threat-actor": {"eset", "gamaredon", "russia", "aligned", "threat", "actor"},
    "sources/2026/06/fortinet-mirai-nexcorium-botnet": {"fortinet", "mirai", "nexcorium", "botnet"},
    "sources/2026/06/eset-apt-activity-report": {"eset", "apt", "activity", "report"},
}


def test_unique_match_relinks():
    assert R._match("Gamaredon ESET writeup", IDX) == "sources/2026/06/eset-gamaredon-russia-aligned-threat-actor"
    assert R._match("Nexcorium Mirai", IDX) == "sources/2026/06/fortinet-mirai-nexcorium-botnet"


def test_ambiguous_or_vague_left_alone():
    assert R._match("Vendor advisory", IDX) is None          # only stopwords -> no tokens
    assert R._match("ESET report", IDX) is None               # "eset" matches 2 sources -> ambiguous
    assert R._match("Cisco Talos disclosure", IDX) is None    # no source slug has these tokens


def test_relink_text_rewrites_only_confident_prose():
    text = ("---\ntype: entity\nname: Gamaredon\nsources:\n"
            "- Gamaredon ESET writeup\n"
            "- Vendor advisory\n"
            "- sources/2026/06/already-a-page\n"
            "---\n# body\n")
    new, n = R.relink_text(text, IDX)
    assert n == 1
    assert "- sources/2026/06/eset-gamaredon-russia-aligned-threat-actor" in new   # relinked
    assert "- Vendor advisory" in new                                              # vague kept
    assert "- sources/2026/06/already-a-page" in new                              # page-ref untouched
    assert "Gamaredon ESET writeup" not in new                                     # prose replaced
