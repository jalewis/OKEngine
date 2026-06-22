"""P1 regression (okengine#39): cross-source canonical resolution must not OVER-MERGE on
a single shared alias token. The motivating case: ThaiCERT's Iranian "Iridium" must not
fold into Sandworm just because Microsoft calls Sandworm "IRIDIUM".
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "entity_resolve.py"


def _load():
    spec = importlib.util.spec_from_file_location("entity_resolve", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["entity_resolve"] = m
    spec.loader.exec_module(m)
    return m


def _index(m):
    # Existing canonicals, including Sandworm carrying Microsoft's "IRIDIUM" alias.
    return m.build_index([
        ("sandworm-team", "Sandworm Team",
         ["IRIDIUM", "Voodoo Bear", "TeleBots", "BlackEnergy Group"]),
        ("apt29", "APT29", ["Cozy Bear", "The Dukes"]),
    ])


def test_single_shared_alias_does_not_over_merge(tmp_path=None):
    """The bug: ThaiCERT 'Iridium' (Iran) shares only the 'iridium' token with Sandworm."""
    m = _load()
    res = m.resolve(_index(m), "Iridium", [])
    assert res.slug is None, "must NOT merge into Sandworm on a lone shared alias"
    assert res.merged is False
    assert res.evidence == "single-alias"
    assert res.ambiguous is not None
    assert res.ambiguous.candidate == "sandworm-team"
    assert "iridium" in res.ambiguous.shared   # the situation is surfaced, not silent


def test_primary_name_match_merges():
    """The common case still merges on a single match when it's the PRIMARY name."""
    m = _load()
    res = m.resolve(_index(m), "APT29", [])
    assert res.slug == "apt29"
    assert res.merged is True
    assert res.evidence == "primary-name"


def test_two_shared_aliases_merge():
    """>= 2 distinct shared keys is strong enough even without a primary-name match."""
    m = _load()
    res = m.resolve(_index(m), "Some Feed Name", ["Voodoo Bear", "TeleBots"])
    assert res.slug == "sandworm-team"
    assert res.merged is True
    assert res.evidence == "multi-alias"


def test_no_overlap_mints_new():
    m = _load()
    res = m.resolve(_index(m), "Brand New Actor", ["Totally Unique Alias"])
    assert res.slug is None
    assert res.merged is False
    assert res.evidence == "none"


def test_accepted_under_merge_is_flagged_not_silent():
    """'structural now, seed later': when a source's PRIMARY name is only an ALIAS of an
    existing canonical (primary names differ), we decline + flag rather than over-merge.
    This is the accepted duplicate-vs-contamination trade-off until the co-reference seed."""
    m = _load()
    idx = m.build_index([("cozy-bear", "Cozy Bear", ["APT29", "The Dukes"])])
    res = m.resolve(idx, "APT29", [])
    assert res.slug is None
    assert res.evidence == "single-alias"
    assert res.ambiguous.candidate == "cozy-bear"


def test_trusted_coref_relaxes_single_alias():
    """The seed hook: a curated co-reference pair lets a single shared alias merge."""
    m = _load()
    idx = m.build_index([("cozy-bear", "Cozy Bear", ["APT29", "The Dukes"])])
    res = m.resolve(idx, "APT29", [], trusted={("apt29", "cozy-bear")})
    assert res.slug == "cozy-bear"
    assert res.merged is True


def test_normalize_ignores_spacing_and_case():
    m = _load()
    assert m.normalize("APT 28") == m.normalize("apt28") == "apt28"
