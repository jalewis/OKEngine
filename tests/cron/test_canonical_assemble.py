"""Tests for the deterministic canonical fusion core (multi-source MDM; okengine#38).

Covers the union / consensus / latest merge policies, Admiralty-weighted conflict
resolution (highest reliability wins, recency tiebreak), conflict surfacing, and
shape-based defaulting. Pure-function tests — no vault I/O.
"""
import importlib.util
import runpy
import sys
import types
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"


def _load(name):
    sys.path.insert(0, str(CRON))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, CRON / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


POLICY = {"union": {"aliases", "refs"}, "consensus": {"suspected_origin", "category"},
          "latest": {"last_seen"}}


def _obs(source, reliability, observed, **fields):
    return {"source": source, "reliability": reliability, "observed": observed, "fields": fields}


def test_union_dedupes_and_preserves_order():
    m = _load("canonical_assemble")
    obs = [_obs("mitre", "A", "2026-06-01", aliases=["APT29", "Cozy Bear"]),
           _obs("thaicert", "B", "2026-06-02", aliases=["Cozy Bear", "The Dukes"])]
    out = m.fuse(obs, POLICY)["fields"]
    assert out["aliases"] == ["APT29", "Cozy Bear", "The Dukes"]   # union, deduped, stable


def test_consensus_highest_reliability_wins_no_conflict_when_agree():
    m = _load("canonical_assemble")
    obs = [_obs("mitre", "A", "2026-06-01", suspected_origin="Russia"),
           _obs("thaicert", "B", "2026-06-02", suspected_origin="Russia")]
    r = m.fuse(obs, POLICY)
    assert r["fields"]["suspected_origin"] == "Russia"
    assert r["conflicts"] == []                                    # agreement -> no flag


def test_consensus_conflict_picks_reliability_and_flags():
    m = _load("canonical_assemble")
    obs = [_obs("vendorx", "D", "2026-06-05", suspected_origin="Iran"),
           _obs("mitre", "A", "2026-06-01", suspected_origin="Russia")]
    r = m.fuse(obs, POLICY)
    assert r["fields"]["suspected_origin"] == "Russia"            # A (MITRE) beats D (VendorX)
    c = [c for c in r["conflicts"] if c["field"] == "suspected_origin"][0]
    assert c["headline"] == "Russia"
    assert {v["value"] for v in c["values"]} == {"Russia", "Iran"}   # both preserved
    assert c["values"][0]["sources"] == ["mitre"]                 # headline source first


def test_consensus_recency_breaks_tie_at_equal_reliability():
    m = _load("canonical_assemble")
    obs = [_obs("a", "A", "2026-01-01", category="ransomware"),
           _obs("b", "A", "2026-06-01", category="loader")]
    assert m.fuse(obs, POLICY)["fields"]["category"] == "loader"  # same rank -> newer wins


def test_latest_wins_by_observed_date():
    m = _load("canonical_assemble")
    obs = [_obs("a", "A", "2026-06-10", last_seen="2026-06-10"),
           _obs("b", "C", "2026-06-20", last_seen="2026-06-20")]
    assert m.fuse(obs, POLICY)["fields"]["last_seen"] == "2026-06-20"


def test_unlisted_field_infers_by_shape():
    m = _load("canonical_assemble")
    obs = [_obs("a", "A", "2026-06-01", tags=["x"], note="hi"),
           _obs("b", "B", "2026-06-02", tags=["y"], note="ho")]
    out = m.fuse(obs, POLICY)["fields"]
    assert sorted(out["tags"]) == ["x", "y"]      # list -> union (inferred)
    assert out["note"] == "hi"                    # scalar -> consensus (A wins, inferred)


def test_union_of_dicts_refs():
    m = _load("canonical_assemble")
    obs = [_obs("mitre", "A", "2026-06-01", refs=[{"std": "mitre-attack", "id": "G0016"}]),
           _obs("thaicert", "B", "2026-06-02", refs=[{"std": "mitre-attack", "id": "G0016"},
                                                      {"std": "misp", "id": "apt29"}])]
    refs = m.fuse(obs, POLICY)["fields"]["refs"]
    assert len(refs) == 2 and {r["std"] for r in refs} == {"mitre-attack", "misp"}  # dedup by content


def test_empty_values_ignored():
    m = _load("canonical_assemble")
    obs = [_obs("a", "A", "2026-06-01", suspected_origin=""),
           _obs("b", "B", "2026-06-02", suspected_origin="China")]
    assert m.fuse(obs, POLICY)["fields"]["suspected_origin"] == "China"   # empty skipped


def test_fuse_multisource_vulnerability_conflict():
    """okengine#40: a CVE seen by two sources with conflicting severity + CVSS fuses into one
    canonical — higher-reliability/most-recent headline, BOTH values preserved with
    attribution, both conflicting fields flagged. cvss is consensus (not latest), so the older
    vector is not silently dropped."""
    m = _load("canonical_assemble")
    vuln_policy = {"union": {"refs"}, "consensus": {"severity", "cvss"}, "latest": set()}
    obs = [
        _obs("nvd", "A", "2026-06-01", severity="high", refs=[{"std": "nvd", "id": "CVE-2026-9999"}],
             cvss={"version": "3.1", "severity": "high", "score": 7.5}),
        _obs("cisa-kev", "A", "2026-06-10", severity="critical", refs=[{"std": "kev", "id": "CVE-2026-9999"}],
             cvss={"version": "3.1", "severity": "critical", "score": 9.8}),
    ]
    r = m.fuse(obs, vuln_policy)
    # equal reliability → recency breaks the tie → cisa-kev (2026-06-10) is the headline
    assert r["fields"]["severity"] == "critical"
    assert r["fields"]["cvss"]["score"] == 9.8
    conf = {c["field"]: c for c in r["conflicts"]}
    assert set(conf) == {"severity", "cvss"}                       # both conflicts surfaced
    assert {v["value"] for v in conf["severity"]["values"]} == {"high", "critical"}
    assert {tuple(v["sources"]) for v in conf["severity"]["values"]} == {("nvd",), ("cisa-kev",)}
    assert len(conf["cvss"]["values"]) == 2                        # BOTH CVSS vectors preserved
    assert len(r["fields"]["refs"]) == 2                           # refs unioned across sources


def test_collect_observations_strips_admiralty_envelope(tmp_path):
    """okengine#40 regression: per-source Admiralty metadata (credibility AND reliability) is
    weighting input, not a claim about the entity — it must be stripped before fusion. Otherwise
    every multi-source page false-conflicts on credibility (kev=1 vs nvd=2), flagging needs_review
    on all of them. KEV + NVD contribute disjoint content, so a clean fuse has NO conflict."""
    m = _load("canonical_assemble")
    for src, cred in (("cisa-kev", "1"), ("nvd", "2")):
        p = tmp_path / "wiki" / "observations" / src / "c" / "cve-2021-44228.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: vulnerability\nsource: {src}\nreliability: A\n"
                     f"credibility: '{cred}'\ncanonical: cve-2021-44228\ntlp: clear\n---\nbody\n")
    obs = m.collect_observations(tmp_path, {})["cve-2021-44228"]
    assert all("credibility" not in o["fields"] and "reliability" not in o["fields"] for o in obs)
    assert not m.fuse(obs, {"union": set(), "consensus": set(), "latest": set()})["conflicts"]


def test_write_canonical_drops_leaked_envelope(tmp_path):
    """okengine#40: a per-source envelope field (credibility/reliability/source) that leaked onto
    a canonical during the buggy window is stripped on re-assembly — self-healing, not preserved
    as curated fm. Real content + the agent body survive."""
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "cve-2021-44228",
               "type: vulnerability\nname: CVE-2021-44228\ncredibility: '1'\nreliability: A\n"
               "source: cisa-kev\nseverity: critical\n", "Agent notes.\n")
    m.write_canonical(tmp_path, "cve-2021-44228", "vulnerability", {"severity": "critical"}, [],
                      ["cisa-kev", "nvd"], POLICY, "2026-06-21")
    fm, body = m.read_fm(p)
    assert not ({"credibility", "reliability", "source"} & set(fm))   # envelope stripped
    assert fm["severity"] == "critical" and "Agent notes." in body    # content + body preserved
    assert fm["assembled_from"] == ["cisa-kev", "nvd"]


# ── write_canonical: preserve body + curated fm, union with existing, flag conflicts ──
def _canon(tmp, slug, fm, body):
    p = tmp / "wiki" / "entities" / slug[0] / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + fm + "---\n" + body)
    return p


def test_write_canonical_preserves_body_and_curated_fields(tmp_path):
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "apt29",
               "type: intrusion-set\nname: APT29\nmitre_id: G0016\naliases: [Cozy Bear]\n",
               "Agent synthesis prose.\n\n## Sightings\n- [[sources/x]]\n")
    fused = {"aliases": ["Midnight Blizzard"], "suspected_origin": "Russia"}
    m.write_canonical(tmp_path, "apt29", "intrusion-set", fused, [], ["mitre-attack", "thaicert"],
                      POLICY, "2026-06-20")
    txt = p.read_text()
    assert "## Sightings" in txt and "Agent synthesis prose." in txt   # body preserved
    assert "mitre_id: G0016" in txt                                    # curated non-owned fm preserved
    fm, _ = m.read_fm(p)
    assert set(fm["aliases"]) == {"Cozy Bear", "Midnight Blizzard"}    # union with existing
    assert fm["suspected_origin"] == "Russia"
    assert fm["assembled_from"] == ["mitre-attack", "thaicert"]
    assert fm["version"] == 1                                          # 0 -> 1


def test_write_canonical_flags_conflicts(tmp_path):
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "x", "type: intrusion-set\nname: X\n", "body\n")
    conflicts = [{"field": "suspected_origin", "headline": "Russia",
                  "values": [{"value": "Russia", "sources": ["mitre-attack"]},
                             {"value": "Iran", "sources": ["vendorx"]}]}]
    m.write_canonical(tmp_path, "x", "intrusion-set", {"suspected_origin": "Russia"},
                      conflicts, ["mitre-attack", "vendorx"], POLICY, "2026-06-20")
    fm, _ = m.read_fm(p)
    assert fm["needs_review"] is True and fm["conflicts"][0]["field"] == "suspected_origin"


def test_write_canonical_conflict_does_not_regress_existing(tmp_path):
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "sw", "type: intrusion-set\nname: Sandworm\nsuspected_origin: Russia\n", "b\n")
    conflicts = [{"field": "suspected_origin", "headline": "Iran",
                  "values": [{"value": "Iran", "sources": ["thaicert"]},
                             {"value": "Russia", "sources": ["thaicert"]}]}]
    m.write_canonical(tmp_path, "sw", "intrusion-set", {"suspected_origin": "Iran"},
                      conflicts, ["mitre-attack", "thaicert"], POLICY, "2026-06-20")
    fm, _ = m.read_fm(p)
    assert fm["suspected_origin"] == "Russia"     # existing curated value kept, NOT overwritten
    assert fm["needs_review"] is True             # but flagged for analyst arbitration


def test_observation_slug_resolves_to_existing_canonical_on_two_identity_keys(tmp_path):
    """#246: a source-native Storm typo enriches the established canonical, not a duplicate."""
    m = _load("canonical_assemble")
    canonical = tmp_path / "wiki" / "entities" / "g" / "gentlemen-ransomware-group.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "---\ntype: actor\ntitle: The Gentlemen ransomware group\n"
        "aliases: [The Gentlemen, Storm-2697]\n---\nGrounded canonical.\n"
    )
    groups = {
        "gentlemen-ransomware-group-storm-2698": [{
            "source": "unit42", "type": "actor", "reliability": "A", "observed": "2026-07-10",
            "fields": {"name": "Gentlemen", "aliases": ["The Gentlemen", "Storm-2697"]},
        }]
    }
    routed, decisions = m.resolve_observation_groups(tmp_path, groups)
    assert list(routed) == ["gentlemen-ransomware-group"]
    assert decisions[0]["evidence"] == "multi-alias"


def test_observation_single_alias_remains_separate(tmp_path):
    """The #39 over-merge safety rule survives the #246 importer integration."""
    m = _load("canonical_assemble")
    canonical = tmp_path / "wiki" / "entities" / "s" / "sandworm.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "---\ntype: actor\nname: Sandworm\naliases: [IRIDIUM, Voodoo Bear]\n---\nbody\n"
    )
    groups = {"iranian-iridium": [{"source": "feed", "type": "actor", "reliability": "B",
                                    "observed": "2026-07-10",
                                    "fields": {"name": "Iridium", "aliases": []}}]}
    routed, decisions = m.resolve_observation_groups(tmp_path, groups)
    assert list(routed) == ["iranian-iridium"]
    assert decisions[0]["ambiguous"] == "sandworm"


def test_write_canonical_renders_relationships_as_wikilinks(tmp_path):
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "apt29", "type: intrusion-set\nname: APT29\n", "Agent prose.\n")
    rels = [{"p": "uses-technique", "t": "phishing-t1566", "n": "Phishing"},
            {"p": "uses-malware", "t": "sunburst", "n": "SUNBURST"}]
    m.write_canonical(tmp_path, "apt29", "intrusion-set", {"mitre_rels": rels}, [],
                      ["mitre-attack"], POLICY, "2026-06-20")
    txt = p.read_text()
    assert "Agent prose." in txt                                  # agent body preserved
    assert "## Associated (MITRE ATT&CK)" in txt
    assert "[[entities/phishing-t1566|Phishing]]" in txt          # internal technique link
    assert "[[entities/sunburst|SUNBURST]]" in txt                # internal malware link
    assert "mitre_rels" not in txt.split("---")[1]                # NOT a frontmatter field
    # idempotent: re-assembling replaces the section, doesn't duplicate it
    m.write_canonical(tmp_path, "apt29", "intrusion-set", {"mitre_rels": rels}, [],
                      ["mitre-attack"], POLICY, "2026-06-20")
    assert p.read_text().count("## Associated (MITRE ATT&CK)") == 1


def test_write_canonical_migration_preserves_unobserved_fields(tmp_path):
    """okengine#41 (split-forward migration safety): when the FIRST observation arrives for a
    pre-existing merge-in-place entity, fields the observation does NOT cover — plus curated fm
    and the agent body — are preserved (never dropped); only covered fields update to the
    source value. So existing entities migrate into the two-layer model with no field loss."""
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "cve-2026-1",
               "type: vulnerability\nname: CVE-2026-1\nseverity: high\n"
               "cvss:\n  version: '3.1'\n  score: 7.5\nkev: true\n",
               "Agent notes.\n\n## Sightings\n- [[sources/x]]\n")
    # KEV is the first source to arrive in observation mode; it provides severity only
    m.write_canonical(tmp_path, "cve-2026-1", "vulnerability", {"severity": "critical"}, [],
                      ["cisa-kev"], POLICY, "2026-06-21")
    fm, body = m.read_fm(p)
    assert fm["severity"] == "critical"                       # covered field → source value
    assert fm["cvss"] == {"version": "3.1", "score": 7.5}     # un-observed field PRESERVED
    assert fm["kev"] is True                                  # un-observed field PRESERVED
    assert "Agent notes." in body and "## Sightings" in body  # agent body preserved
    assert fm["assembled_from"] == ["cisa-kev"]


def _shard_schema(tmp):
    """Governing schema.yaml so `entities` shards by-letter (matches config/base-schema.yaml),
    like a real deployment — a NEW canonical lands at its sharded seat via canonical_key, not the
    flat root. The pre-existing-page tests don't need this: find_page locates the page wherever
    _canon already placed it."""
    (tmp / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n")


def test_write_canonical_creates_when_absent(tmp_path):
    m = _load("canonical_assemble")
    _shard_schema(tmp_path)
    m.write_canonical(tmp_path, "newactor", "intrusion-set", {"aliases": ["NA"]}, [],
                      ["thaicert"], POLICY, "2026-06-20")
    p = tmp_path / "wiki" / "entities" / "n" / "newactor.md"
    assert p.is_file() and "type: intrusion-set" in p.read_text()


def test_write_canonical_idempotent_skips_unchanged(tmp_path):
    """okengine#43: a re-run over unchanged owned content writes nothing and does NOT bump
    version; a genuine change still rewrites + bumps."""
    m = _load("canonical_assemble")
    _shard_schema(tmp_path)
    args = (tmp_path, "apt29", "intrusion-set",
            {"aliases": ["Cozy Bear"], "suspected_origin": "Russia"},
            [], ["mitre-attack"], POLICY, "2026-06-20")
    _, w1 = m.write_canonical(*args)
    p = tmp_path / "wiki" / "entities" / "a" / "apt29.md"
    v1 = p.read_text()
    assert w1 is True and "version: 1" in v1

    # identical re-run → skipped: no write, no version bump, byte-identical file
    _, w2 = m.write_canonical(*args)
    assert w2 is False
    assert p.read_text() == v1
    assert "version: 2" not in v1

    # a real change (new alias) → rewrite + version bump
    _, w3 = m.write_canonical(tmp_path, "apt29", "intrusion-set",
                              {"aliases": ["Cozy Bear", "The Dukes"], "suspected_origin": "Russia"},
                              [], ["mitre-attack"], POLICY, "2026-06-21")
    assert w3 is True
    fm, _ = m.read_fm(p)
    assert fm["version"] == 2 and "The Dukes" in fm["aliases"]


def test_key_serializes_date_and_set_dict_fields_deterministically():  # invariant-audit completeness
    """A union-mode field can be a list of dicts whose values include a datetime.date (yaml.safe_load
    of a bare ISO date). _key(dict) json.dumps'd it WITHOUT default=str, so a TypeError propagated out
    of the unguarded fuse() and aborted the ENTIRE canonical-assembly run (zero canonicals). It must
    serialize, and a !!set field must key DETERMINISTICALLY (sorted, not PYTHONHASHSEED-order str(set))."""
    import datetime
    m = _load("canonical_assemble")
    d = datetime.date(2026, 7, 10)
    obs = [_obs("mitre", "A", "2026-06-01", refs=[{"url": "https://example.com", "seen": d}]),
           _obs("thaicert", "B", "2026-06-02", refs=[{"url": "https://example.com", "seen": d}])]
    out = m.fuse(obs, POLICY)["fields"]                     # must NOT raise on the date-bearing dict
    assert out["refs"] == [{"url": "https://example.com", "seen": d}]   # identical dicts deduped to one
    # a set inside a dict field keys deterministically regardless of insertion order
    assert m._key({"tags": {"b", "a"}}) == m._key({"tags": {"a", "b"}})


def test_non_int_version_does_not_crash_assembler(tmp_path):  # invariant-audit #30
    """An agent authored a semver `version: 3.0.14` (str) on the existing page — the write path
    accepts it (version is not int-shaped). The assembler must not ValueError on int(version)."""
    m = _load("canonical_assemble")
    p = _canon(tmp_path, "openssl", "type: entity\nname: OpenSSL\nversion: 3.0.14\n", "Agent notes.\n")
    m.write_canonical(tmp_path, "openssl", "entity", {"category": "library"}, [],
                      ["nvd"], POLICY, "2026-06-21")
    fm, _ = m.read_fm(p)
    assert isinstance(fm["version"], int) and fm["version"] == 1   # reset from non-int, then bumped


def test_helper_empty_schema_and_frontmatter_edges(tmp_path, monkeypatch):
    m = _load("canonical_assemble")
    assert m._as_list(None) == [] and m._as_list("x") == ["x"]
    assert m.fuse([{"fields": {"empty": None}}], {}) == {"fields": {}, "conflicts": []}
    assert m.load_schema(tmp_path) == {}
    (tmp_path / "schema.yaml").write_text(
        "merge_policy:\n  union: [aliases]\nsource_registry:\n  feed:\n    reliability: B\n"
    )
    schema = m.load_schema(tmp_path)
    assert m.merge_policy(schema)["union"] == {"aliases"}
    assert m.source_reliability(schema) == {"feed": "B"}
    (tmp_path / "schema.yaml").write_text("[")
    assert m.load_schema(tmp_path) == {}

    page = tmp_path / "page.md"
    page.write_text("plain")
    assert m.read_fm(page) == ({}, "")
    page.write_text("---\n[\n---\nbody")
    assert m.read_fm(page) == ({}, "body")
    page.write_text("---\n- one\n---\nbody")
    assert m.read_fm(page) == ({}, "body")
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m.read_fm(page) == ({}, "")


def test_collect_observation_scan_filters_and_registry_fallback(tmp_path):
    m = _load("canonical_assemble")
    assert m.collect_observations(tmp_path, {}) == {}
    root = tmp_path / "wiki" / "observations" / "feed"
    root.mkdir(parents=True)
    (root / "_skip.md").write_text("---\ntype: actor\n---\n")
    (root / ".skip.md").write_text("---\ntype: actor\n---\n")
    (root / "blank.md").write_text("---\ncanonical: ''\nsource: ''\n---\n")
    (root / "actor.md").write_text(
        "---\ntype: actor\ncanonical: Actor-X\nname: Actor X\nupdated: 2026-01-01\n---\n"
    )
    obs = m.collect_observations(tmp_path, {"feed": "C"})
    assert set(obs) == {"actor-x", "blank"}
    assert obs["actor-x"][0]["source"] == "feed"
    assert obs["actor-x"][0]["reliability"] == "C"
    assert obs["actor-x"][0]["observed"] == "2026-01-01"


def test_collect_observations_rejects_record_without_source(tmp_path, monkeypatch):
    m = _load("canonical_assemble")
    root = tmp_path / "wiki" / "observations"
    root.mkdir(parents=True)

    class SourceLess:
        name = "record.md"
        stem = "record"
        def relative_to(self, _root):
            return Path(".")

    monkeypatch.setattr(Path, "rglob", lambda *_a, **_k: [SourceLess()])
    monkeypatch.setattr(m, "read_fm", lambda *_a: ({"type": "actor"}, ""))
    assert m.collect_observations(tmp_path, {}) == {}


def test_canonical_index_filters_and_alias_shapes(tmp_path):
    m = _load("canonical_assemble")
    empty = m._canonical_index(tmp_path)
    assert empty is not None
    root = tmp_path / "wiki" / "entities"
    root.mkdir(parents=True)
    (root / "_meta.md").write_text("x")
    (root / "INDEX.md").write_text("x")
    (root / "dead.md").write_text("---\nstatus: tombstoned\n---\n")
    (root / "string.md").write_text("---\nname: String\naliases: 'A, B, '\n---\n")
    (root / "odd.md").write_text("---\ntitle: Odd\naliases: {x: y}\n---\n")
    idx = m._canonical_index(tmp_path)
    assert idx is not None


def test_resolution_and_misc_rendering_edge_routes(tmp_path, monkeypatch):
    m = _load("canonical_assemble")
    fused = m.fuse([
        _obs("", "A", "2026-01-01", category="same"),
        _obs("a", "B", "2026-01-02", category="same"),
        _obs("a", "C", "2026-01-03", category="same"),
    ], {"consensus": {"category"}})
    assert fused["fields"]["category"] == "same"
    groups = {
        "empty": [{"source": "x", "type": "", "reliability": "", "fields": {}}],
        "scalar-alias": [{"source": "x", "type": "actor", "reliability": "A",
                          "fields": {"title": "Nobody", "aliases": "Alias"}}],
    }
    routed, decisions = m.resolve_observation_groups(tmp_path, groups)
    assert set(routed) == set(groups)
    assert all(d["target"] == d["proposed"] for d in decisions)
    assert m._canonical_type(groups["empty"]) == "entity"
    assert m._canonical_type(groups["scalar-alias"]) == "actor"
    assert m._assoc_section([]) == ""
    assert m._assoc_section(["bad", {"p": "unknown"}]) == ""
    section = m._assoc_section([{"p": "unknown", "t": "x"}])
    assert "**Related**" in section and "|x]]" in section
    assert m._set_managed_section("body\n\n## Old\nx", "## Old", "") == "body\n"

    # Existing-slug bypasses resolver entirely.
    existing = tmp_path / "wiki" / "entities" / "e" / "existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("---\nname: Existing\n---\n")
    monkeypatch.setattr(
        m.entity_resolve, "resolve",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must not resolve existing slug")),
    )
    routed, decisions = m.resolve_observation_groups(
        tmp_path, {"existing": [{"source": "x", "fields": {}}]}
    )
    assert decisions[0]["evidence"] == "existing-slug"


def test_write_canonical_dry_run_empty_fields_and_empty_relationships(tmp_path):
    m = _load("canonical_assemble")
    _shard_schema(tmp_path)
    text, wrote = m.write_canonical(
        tmp_path, "dry", "", {"name": "", "empty": None, "mitre_rels": []},
        [], [], {"union": {"empty"}}, "2026-01-01", dry_run=True,
    )
    assert wrote is True and "type: entity" in text
    assert not (tmp_path / "wiki" / "entities").exists()


def test_main_reports_all_routes_and_entrypoint(tmp_path, monkeypatch, capsys):
    m = _load("canonical_assemble")
    groups = {
        "multi": [
            _obs("a", "A", "2026-01-01", aliases=["x"], category="one"),
            _obs("b", "B", "2026-01-02", aliases=["y"], category="two"),
        ],
        "single": [_obs("a", "A", "2026-01-01", name="Single")],
        "error": [_obs("a", "A", "2026-01-01", name="Error")],
    }
    for values in groups.values():
        for value in values:
            value["type"] = "actor"
    decisions = [
        {"proposed": "old", "target": "multi", "evidence": "exact-name", "ambiguous": None},
        {"proposed": "maybe", "target": "maybe", "evidence": "single-alias",
         "ambiguous": "known"},
        {"proposed": "same", "target": "same", "evidence": "none", "ambiguous": None},
    ]
    monkeypatch.setattr(m, "collect_observations", lambda *_a: groups)
    monkeypatch.setattr(m, "resolve_observation_groups", lambda *_a: (groups, decisions))
    writes = iter([(None, True), (None, False)])

    def write(_vault, slug, *_a, **_k):
        if slug == "error":
            raise OSError("race")
        return next(writes)

    monkeypatch.setattr(m, "write_canonical", write)
    assert m.main(["--vault", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "CONFLICTS" in out and "old -> multi" in out and "lone-alias" in out
    assert "would assemble 1 canonical" in out

    monkeypatch.setattr(m, "collect_observations", lambda *_a: {})
    monkeypatch.setattr(m, "resolve_observation_groups", lambda *_a: ({}, []))
    assert m.main(["--vault", str(tmp_path), "--only", " NONE "]) == 0
    assert "no observations" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [str(CRON / "canonical_assemble.py"),
                                      "--vault", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(CRON / "canonical_assemble.py"), run_name="__main__")
    assert exc.value.code == 0
