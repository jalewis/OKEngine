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
    # base-schema injects the core namespaces (entities/sources/…); this test also writes to a
    # type-named namespace, so declare it or the #115 namespace-discipline gate rejects the write.
    "partitioning:\n"
    "  namespaces: {attack-pattern: {strategy: flat}}\n"
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
    # Resolve through _safe so an entity rel lands at the same sharded canonical the write
    # path normalized it to (entities/<slug> -> entities/<l>/<slug>.md); non-entity paths pass through.
    fm, _ = m._read_page(m._safe(rel))
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


def test_converge_normalizes_schema_drift(tmp_path):
    """okengine#46: converge must run the SAME vocab-drift guard as create/update — rename a drifted
    alias key, map an aliased value. Regression: the converge merge path bypassed _normalize_drift, so
    a composed-multipack import landed `country: CN`/`status: active` verbatim (invariant-audit)."""
    schema = (
        "types:\n"
        "  intrusion-set: {required: [type], id_authority: actor, id_field: actor_id, owner: sec}\n"
        "partitioning:\n"
        "  namespaces: {intrusion-set: {strategy: flat}}\n"
        "field_aliases: {country: suspected_origin}\n"
        "value_aliases: {status: {active: live}}\n"
    )
    m = _load(tmp_path, schema)
    assert m._converge("intrusion-set/apt-x.md",
                       "type: intrusion-set\nactor_id: APTX\nname: APT-X", pack="sec").startswith("created")
    out = m._converge("intrusion-set/apt-x.md",
                      "type: intrusion-set\nactor_id: APTX\ncountry: CN\nstatus: active", pack="sec")
    assert out.startswith("converged")
    fm = _read_id(m, "intrusion-set/apt-x.md")
    assert fm.get("suspected_origin") == "CN" and "country" not in fm   # alias KEY renamed (was bypassed)
    assert fm.get("status") == "live"                                    # aliased VALUE mapped


def test_scalar_sources_coerced_to_list(tmp_path):
    """okengine#196's write-path list-coercion forgot `sources` — the very citation field it was for.
    A scalar comma-string must split, not land as one blob, or the grounding/staleness graph sees zero
    primary citations (invariant-audit). Guarded by base-schema field_shapes now including sources."""
    m = _load(tmp_path)   # default schema + the REAL base-schema (field_shapes now has `sources`)
    m._create("entities/acme.md",
              "type: vendor\nname: Acme\nsources: sources/2026/07/a, sources/2026/07/b", "body")
    assert _read_id(m, "entities/acme.md")["sources"] == ["sources/2026/07/a", "sources/2026/07/b"]


def test_converge_briefing_rejects_broken_link(tmp_path):
    """L3: converge must enforce the briefing dead-link guard (like create/update/patch/append) —
    a briefings/ merge whose body carries an unresolvable [[wikilink]] is rejected, file untouched."""
    schema = (
        "types: {briefing: {required: [type]}, source: {required: [type]}}\n"
        "partitioning:\n  namespaces: {briefings: {strategy: flat}}\n"
    )
    m = _load(tmp_path, schema)
    (tmp_path / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "sources" / "s1.md").write_text("---\ntype: source\n---\nx")
    assert m._converge("briefings/b1.md", "type: briefing\ntitle: B1",
                       "Cited [[sources/s1]].").startswith(("created", "converged"))
    out = m._converge("briefings/b1.md", "type: briefing\ntitle: B1",
                      "Now cites [[entities/does-not-exist]].")
    assert out.startswith("rejected") and "resolve" in out.lower()


def _created_rel(out: str) -> str:
    return out.split("created ", 1)[1].split(" v")[0].strip()


def test_write_time_link_guard_flags_curated_not_sources(tmp_path):
    """link-audit: a concepts/entities page that INTRODUCES an unresolvable wikilink is soft-flagged
    needs_review (not rejected — organic growth preserved); a resolvable link is clean; a SOURCE page
    (forward-refs are its nature) is NOT flagged; briefings still HARD-reject."""
    schema = (
        "types: {concept: {required: [type]}, source: {required: [type]}, vendor: {required: [type]}}\n"
        "partitioning:\n"
        "  namespaces: {concepts: {strategy: by-letter}, entities: {strategy: by-letter},\n"
        "               sources: {strategy: flat}}\n"
    )
    m = _load(tmp_path, schema)
    m._create("entities/a/acme.md", "type: vendor\nname: Acme", "body")   # a real link target
    # concept with a broken path link + a bare-name link -> flagged, still created
    out = m._create("concepts/f/foo.md", "type: concept\nname: Foo",
                    "See [[entities/a/acme]] (ok), [[entities/does-not-exist]] (broken), [[BareName]].")
    assert out.startswith("created")
    assert _read_id(m, _created_rel(out)).get("needs_review") is True
    # concept with ONLY a resolvable link -> no flag
    out2 = m._create("concepts/b/bar.md", "type: concept\nname: Bar", "Only [[entities/a/acme]].")
    assert not _read_id(m, _created_rel(out2)).get("needs_review")
    # a SOURCE page with a broken forward-ref -> NOT flagged (excluded namespace)
    out3 = m._create("sources/s1.md", "type: source", "Forward [[concepts/not-yet-created]].")
    assert not _read_id(m, _created_rel(out3)).get("needs_review")


def test_converge_enforces_int_field_guard_on_merge(tmp_path):
    """The merge branch must run the machine-owned int guard (recent_reports/total_mentions) like
    create/update/patch — else it's a hole: _dedup_on_create redirects a create_entity for an
    already-known id INTO converge, so even the create tool bypasses the guard on a live entity
    (invariant-audit M15). recent_reports/total_mentions shapes come from config/base-schema.yaml."""
    m = _load(tmp_path)
    # establish the entity (int count as an int -> fine)
    assert m._converge("entities/acme.md",
                       "type: vendor\ntitle: Acme Corp\nrecent_reports: 3").startswith("created")
    # a SECOND converge into the same page (same minted slug id) with recent_reports as a hand-written
    # LIST (the live incident: agent misread the field) must be REJECTED at the merge, not written.
    out = m._converge("entities/acme.md",
                      "type: vendor\ntitle: Acme Corp\nrecent_reports:\n  - sources/2026/07/x")
    assert out.startswith("rejected:") and "recent_reports" in out, out
    # the stored page is untouched (still the int)
    assert _read_id(m, "entities/acme.md")["recent_reports"] == 3


def test_merge_frontmatter_never_overwrites_server_provenance():
    """converge's _SERVER_KEYS are server-managed — the merge must PRESERVE them, never take an
    incoming payload's value, or a caller forges provenance (created/discovered_by). The old code did
    `merged[key] = new_val` for every server key, the exact opposite of its own docstring
    (invariant-audit M19). Pure-function test — no stamping dependency."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("converge", REPO / "okengine-mcp" / "converge.py")
    cv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cv)
    prev = {"type": "actor", "created": "2026-01-01", "created_by": "atk",
            "discovered_by": "atk", "maintained_by": ["atk"], "id": "mitre:t1"}
    incoming = {"created": "1999-01-01", "created_by": "attacker",
                "discovered_by": "attacker", "name": "X"}
    merged, dec = cv.merge_frontmatter(prev, incoming, owner_pack="atk", caller_pack="atk")
    assert merged["created"] == "2026-01-01", "caller must not forge `created`"
    assert merged["created_by"] == "atk", "caller must not forge `created_by`"
    assert merged["discovered_by"] == "atk", "caller must not forge `discovered_by`"
    assert merged["name"] == "X", "a non-server key is still added normally"
    assert "atk" in merged["maintained_by"], "maintained_by provenance union preserved"
    # forged provenance keys are not counted as legitimate updates
    assert not ({"created", "created_by", "discovered_by"} & set(dec.updated))
    # id/version/updated ARE re-stamped by the write path, so merge pass-through of them is fine
    # (this test targets provenance forgery, not the re-stamped keys).


# ── H4 (okengine#324): alias-dedup consults the id-index, no per-create full scan ──────────────

def _mk_entity_file(root, rel, fm_lines):
    p = root / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + fm_lines + "\n---\n\nbody\n", encoding="utf-8")


def test_dedup_index_incoming_name_matches_existing_alias(tmp_path):
    m = _load(tmp_path)
    m._create("entities/acme", "type: vendor\nname: Acme\naliases: [Acme Corporation]", "body")
    # a second create whose NAME equals the first's ALIAS must converge, not fork
    out = m._create("entities/acme-corporation", "type: vendor\nname: Acme Corporation", "body")
    assert out.startswith("converged into"), out


def test_dedup_index_incoming_alias_matches_existing_name(tmp_path):
    m = _load(tmp_path)
    m._create("entities/beta", "type: vendor\nname: Beta", "body")
    out = m._create("entities/other", "type: vendor\nname: Other\naliases: [Beta]", "body")
    assert out.startswith("converged into"), out


def test_dedup_index_catches_idless_existing_page(tmp_path):
    m = _load(tmp_path)
    # a legacy entity page with NO id (written directly), present before the registry is built.
    # H4's contract is that id-less entity pages are INDEXED and returned as dedup candidates (unlike
    # by_id, which is id-only) — so a duplicate of a legacy id-less page is still caught. (What the
    # converge step then does with an id-less target is separate, pre-#324 behavior.)
    _mk_entity_file(tmp_path, "entities/g/gamma", "type: vendor\nname: Gamma")
    p = m._safe("entities/new")
    hits = m._alias_hits(p, m.id_lib.normalize_key("New"), {m.id_lib.normalize_key("Gamma")})
    assert [h[0].name for h in hits] == ["gamma.md"], hits
    assert hits[0][1].get("name") == "Gamma"


def test_dedup_does_not_full_scan_entities(tmp_path, monkeypatch):
    m = _load(tmp_path)
    # populate many UNRELATED entities so a full scan would be expensive
    for i in range(25):
        m._create(f"entities/unrelated-{i}", f"type: vendor\nname: Unrelated {i}", "body")
    m._create("entities/target", "type: vendor\nname: Target\naliases: [Codename Zed]", "body")
    # count _read_page calls during ONE matching create — index path reads only the hit(s), O(1)
    real = m._read_page
    calls = {"n": 0}
    def counting(pp):
        calls["n"] += 1
        return real(pp)
    monkeypatch.setattr(m, "_read_page", counting)
    out = m._create("entities/zed", "type: vendor\nname: Codename Zed", "body")
    assert out.startswith("converged into"), out
    assert calls["n"] <= 4, f"read {calls['n']} pages — expected O(1), not a full entities/ scan"


def test_dedup_falls_back_to_scan_on_pre_v2_index(tmp_path):
    m = _load(tmp_path)
    m._create("entities/delta", "type: vendor\nname: Delta\naliases: [DeltaCorp]", "body")
    # simulate a pre-v2 persisted artifact: identity maps empty (only by_id present)
    reg = m._registry()
    reg.name_to_rels.clear()
    reg.alias_to_rels.clear()
    out = m._create("entities/deltacorp", "type: vendor\nname: DeltaCorp", "body")
    assert out.startswith("converged into"), out   # fallback scan still catches it — never blind


def test_dedup_same_process_back_to_back(tmp_path):
    m = _load(tmp_path)
    # first create is a genuinely new entity; the SECOND (same process) matches its alias and must
    # dedup against it via the write-synchronous identity claim (not a stale load-time index)
    m._create("entities/epsilon", "type: vendor\nname: Epsilon\naliases: [EPS]", "body")
    out = m._create("entities/eps", "type: vendor\nname: EPS", "body")
    assert out.startswith("converged into"), out


def test_dedup_multiple_alias_hits_refused_for_review(tmp_path):
    m = _load(tmp_path)
    m._create("entities/one", "type: vendor\nname: One\naliases: [Shared]", "body")
    m._create("entities/two", "type: vendor\nname: Two\naliases: [Shared]", "body")
    out = m._create("entities/three", "type: vendor\nname: Shared", "body")
    assert out.startswith("refused") and "multiple canonicals" in out, out


def test_converge_authority_redirect_rechecks_reserved_file(tmp_path):
    """invariant-audit HIGH #5: converge checks _reserved_refuse on the ORIGINAL path, then the
    authority-id redirect points p at an existing canonical — which id_index CAN resolve to a
    pack-reserved page (id_index._skip only knows the engine set, not schema reserved_files). The
    redirect re-checks _wauth but used to skip _reserved, so a converge could land on a reserved
    page. It must be refused on the redirected path, like every other mutating lane."""
    schema = SCHEMA + "reserved_files: [pinned.md]\n"
    m = _load(tmp_path, schema)
    # an authority page living AT a pack-reserved filename, carrying its authority id (written
    # directly — the write path would refuse to CREATE a reserved file, but one can pre-exist)
    (tmp_path / "wiki" / "attack-pattern").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "attack-pattern" / "pinned.md").write_text(
        "---\ntype: attack-pattern\ntechnique_id: T1059\nid: mitre:t1059\nversion: 1\n---\n\nBody.\n",
        encoding="utf-8")
    m._registries.clear()                            # rebuild the id-index so it sees pinned.md
    # a converge from a different path with the SAME authority id -> redirects to the reserved page
    out = m._converge("attack-pattern/incoming.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: execution", pack="atk")
    assert out.startswith("refused") and "reserved" in out.lower(), out
    # the reserved page must be untouched (no tactic merged in)
    assert "tactic" not in (tmp_path / "wiki" / "attack-pattern" / "pinned.md").read_text()


_TYPENS = (
    "types:\n  source: {required: [type]}\n  concept: {required: [type]}\n"
    "type_namespaces: {source: sources, concept: concepts}\n"
    "partitioning:\n  namespaces: {sources: {strategy: flat}, concepts: {strategy: flat}}\n"
)


def test_update_cannot_drift_type_out_of_its_home_namespace(tmp_path):
    """invariant-audit: the type-namespace guard was CREATE-ONLY, so update/patch/converge could
    rewrite a page's type to one whose home is a different namespace, forking the graph. Now the
    mutating lanes reject a type CHANGE that drifts — but grandfather a legacy mismatched page."""
    m = _load(tmp_path, _TYPENS)
    # a compliant page: type concept under concepts/
    assert m._create("concepts/c/idea", "type: concept\ntitle: Idea").startswith("created")
    # changing its type to `source` (home = sources/) while it lives under concepts/ must be REFUSED
    out = m._update("concepts/c/idea", {"type": "source"}, None)
    assert out.startswith("rejected") and "belongs in 'sources/'" in out, out
    assert _read_id(m, "concepts/c/idea")["type"] == "concept"   # untouched
    # grandfather: a legacy page already mismatched (written directly) stays editable when the type
    # is NOT changing
    (tmp_path / "wiki" / "concepts" / "l").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "concepts" / "l" / "legacy.md").write_text(
        "---\ntype: source\ntitle: Legacy\nid: sources:legacy\nversion: 1\n---\n\nbody\n", encoding="utf-8")
    out2 = m._update("concepts/l/legacy", {"note": "edited"}, None)   # type unchanged
    assert not out2.startswith(("refused", "rejected")), out2         # grandfathered
