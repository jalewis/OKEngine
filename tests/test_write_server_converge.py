"""P2 regression: converge_entity upserts by id — explicit identity convergence merges into one
canonical page, create-time weak identity stays non-merging, tombstones refuse, ownership holds.
"""
import importlib.util
import hashlib
import json
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


def test_converge_create_fallback_rejects_generic_actor_for_admin(tmp_path, monkeypatch):
    """Negative fixture: alternate authorized converge cannot bypass admission."""
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    m = _load(tmp_path, "types:\n  actor: {required: [type]}\n")
    out = m._converge("entities/threat-actor.md", "type: actor\ntitle: Threat Actor\n",
                      "Threat actors continue to target companies.")
    assert "generic class labels" in out, out
    assert not list((tmp_path / "wiki" / "entities").rglob("threat-actor.md")), out


def test_converge_create_fallback_accepts_evidenced_named_actor(tmp_path, monkeypatch):
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    m = _load(tmp_path, "types:\n  actor: {required: [type]}\n")
    out = m._converge("entities/comment-crew.md", "type: actor\ntitle: Comment Crew\n",
                      "Comment Crew is a named adversary cluster documented in source reports.")
    assert out.startswith("created"), out
    assert list((tmp_path / "wiki" / "entities").rglob("comment-crew.md"))


def test_entity_backfill_drops_copied_source_identity(tmp_path):
    """Embedded source frontmatter is evidence, not the new entity's identity."""
    m = _load(tmp_path)
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/qwen-control-laboratory",
        "id: sources:qwen-control-d\n"
        "type: source\n"
        "title: Qwen Control Laboratory\n"
    )
    assert error is None
    parsed = __import__("yaml").safe_load(cleaned)
    assert "id" not in parsed
    assert parsed["type"] == "vendor"
    assert parsed["title"] == "Qwen Control Laboratory"
    assert parsed["name"] == "Qwen Control Laboratory"
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/qwen-control-laboratory",
        "id: concepts:qwen-control\n"
        "type: vendor\n"
        "title: Qwen Control Laboratory\n",
    )
    assert error is None
    assert "id" not in __import__("yaml").safe_load(cleaned)
    for impossible_type in ("entity", "concept"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/qwen-control-laboratory",
            f"type: {impossible_type}\ntitle: Qwen Control Laboratory\n",
        )
        assert error is None
        normalized = __import__("yaml").safe_load(cleaned)
        assert normalized["type"] == "vendor"
        assert normalized["name"] == "Qwen Control Laboratory"


def test_entity_backfill_requires_named_classified_threat_actor(tmp_path):
    schema = (
        "types:\n"
        "  actor: {required: [type]}\n"
        "  publisher: {required: [type]}\n"
        "  identity: {required: [type]}\n"
    )
    m = _load(tmp_path, schema)
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/anthropic",
        "type: actor\ntitle: Anthropic\n",
    )
    assert cleaned is None
    assert "requires explicit actor_type" in error

    cleaned, error = m._entity_backfill_frontmatter(
        "entities/threat-actor",
        "type: actor\ntitle: Threat Actor\nactor_type: unknown\n",
    )
    assert cleaned is None
    assert "generic class labels" in error

    cleaned, error = m._entity_backfill_frontmatter(
        "entities/mirage-kitten",
        "type: actor\ntitle: Mirage Kitten\nactor_type: nation-state\n",
        "Mirage Kitten is a named threat actor associated with state-directed operations.",
    )
    assert error is None
    assert __import__("yaml").safe_load(cleaned)["actor_type"] == "nation-state"

    cleaned, error = m._entity_backfill_frontmatter(
        "entities/anthropic",
        "type: publisher\ntitle: Anthropic\n",
    )
    assert error is None
    assert __import__("yaml").safe_load(cleaned)["type"] == "publisher"


def test_entity_backfill_rejects_production_641_regressions(tmp_path):
    m = _load(tmp_path, "types:\n  actor: {required: [type]}\n  malware: {required: [type]}\n")
    assert m._actor_payload_reject("[broken") is None
    assert m._actor_payload_reject("- scalar") is None
    for title in ("Unsafe", "Outsider"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: unknown\n"
        )
        assert cleaned is None
        assert "generic class labels" in error
    for title, body in (
        ("SLEEPWALKER", "SLEEPWALKER is a Windows backdoor."),
        ("Mirage2FA", "Mirage2FA is a phishing-as-a-service toolkit."),
        ("BridgePay Ransomware", "This ransomware affected municipal billing systems."),
    ):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: unknown\n", body
        )
        assert cleaned is None
        assert "not a threat actor" in error
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/x",
        "type: actor\ntitle: SparklingGoblin\nactor_type: nation-state\n",
        "A backdoor, SideWalk, is used by an APT group named SparklingGoblin.",
    )
    assert error is None
    assert cleaned is not None
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/x",
        "type: actor\ntitle: Gunra\nactor_type: cybercriminal\n",
        "Gunra is a ransomware-as-a-service (RaaS) operation targeting government entities.",
    )
    assert error is None
    assert cleaned is not None


def test_actor_admission_rejects_schema_configured_geopolitical_titles_only(tmp_path):
    schema = (
        "types:\n  actor: {required: [type]}\n"
        "identity_admission:\n"
        "  actor:\n"
        "    excluded_exact_titles: {KP: North Korea, IR: Iran, RU: Russia}\n"
    )
    m = _load(tmp_path, schema)
    for title in ("North Korea", "north-korea", "IRAN", "Russia"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: nation-state\n",
            f"{title} is a named threat actor tracked as an intrusion set."
        )
        assert cleaned is None
        assert "geopolitical entities" in error

    for title in ("Lazarus Group", "North Korean Lazarus Group", "Iranian-Aligned Ember Bear"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: nation-state\n",
            f"{title} is a named threat actor tracked as an intrusion set."
        )
        assert error is None
        assert cleaned is not None


def test_actor_reserved_title_schema_edges(tmp_path, monkeypatch):
    m = _load(tmp_path)
    assert m._actor_reserved_titles() == frozenset()

    page = tmp_path / "wiki" / "entities" / "x.md"
    monkeypatch.setattr(
        m,
        "_governing",
        lambda _path: {
            "identity_admission": {"actor": {"excluded_exact_titles": ["Iran", ""]}}
        },
    )
    assert m._actor_reserved_titles(page) == frozenset({"iran"})

    monkeypatch.setattr(
        m,
        "_governing",
        lambda _path: {
            "identity_admission": {"actor": {"excluded_exact_titles": "Iran"}}
        },
    )
    assert m._actor_reserved_titles(page) == frozenset()

    monkeypatch.setattr(m, "_governing", lambda _path: (_ for _ in ()).throw(ValueError("bad")))
    assert m._actor_reserved_titles(page) == frozenset()


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


def test_converge_exact_existing_path_with_weaker_identity_uses_merge(tmp_path):
    """An entity lane that knows the canonical path must be able to enrich an
    authority-backed page even when its evidence only supplies a title-derived
    slug identity.  Returning "use update_entity" is a dead end because the
    actor intentionally exposes only converge_entity."""
    m = _load(tmp_path)
    for slug in ("original", "new"):
        source = tmp_path / "wiki" / "sources" / f"{slug}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"---\ntype: source\ntitle: {slug}\n---\nevidence\n")
    requested = m._safe("entities/apt3.md")
    existing = m._partitioned_create_path(
        requested, {"type": "vendor", "title": "APT3"}
    )
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        "---\ntype: vendor\nid: G0022\ntitle: APT3\n"
        "sources: [sources/original]\n---\nexisting body\n"
    )

    out = m._converge(
        "entities/apt3.md",
        "type: vendor\ntitle: APT3\nsources: [sources/new]",
    )

    assert out.startswith("converged into entities/")
    fm, _ = m._read_page(existing)
    assert fm["id"] == "G0022"
    assert "sources/new" in fm["sources"]


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


def test_scoped_extension_token_is_composed_type_owner_for_removal(tmp_path, monkeypatch):
    """The deployment pack cannot impersonate an extension owner; the scoped
    extension token can perform an exact, preconditioned governed repair."""
    monkeypatch.setenv("OKENGINE_PACK", "okpack-domain-example")
    m = _load(tmp_path, (
        "types:\n"
        "  lacuna: {required: [type]}\n"
        "partitioning:\n"
        "  namespaces: {lacuna: {strategy: flat}}\n"
    ))
    monkeypatch.setattr(m, "_governing", lambda _path: {
        "types": {"lacuna": {"required": ["type"]}},
        "partitioning": {"namespaces": {"lacuna": {"strategy": "flat"}}},
        "owners": {"types": {"lacuna": "ext:okengine.lacuna"}},
    })
    page = tmp_path / "wiki" / "lacuna" / "gap.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: lacuna\nid: lacuna:gap\ntitle: Gap\n"
        "prediction_candidate: predictions/missing\nneeds_review: true\n---\nbody\n",
        encoding="utf-8",
    )

    before = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    denied = m._converge(
        "lacuna/gap.md", {"type": "lacuna", "id": "lacuna:gap"},
        remove="prediction_candidate", expected_sha256=before,
    )
    assert "1 conflict" in denied, denied
    assert _read_id(m, "lacuna/gap.md")["prediction_candidate"] == "predictions/missing"

    token = m._caller_var.set({
        "kind": "extension",
        "actor": "extension:okengine.lacuna",
        "ext_id": "okengine.lacuna",
        "write_scopes": ["lacuna/**"],
    })
    try:
        current = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
        repaired = m._converge(
            "lacuna/gap.md", {"type": "lacuna", "id": "lacuna:gap"},
            remove="prediction_candidate", expected_sha256=current,
        )
    finally:
        m._caller_var.reset(token)

    assert "-1 removed" in repaired, repaired
    fm = _read_id(m, "lacuna/gap.md")
    assert "prediction_candidate" not in fm
    assert "ext:okengine.lacuna" in fm["maintained_by"]
    assert fm["needs_review"] is True

    # The generated in-gateway writer has no network token; its server-bound
    # cron identity must receive the same exact extension-owner authority.
    token = m._caller_var.set({
        "kind": "extension", "actor": "extension:okengine.lacuna",
        "ext_id": "okengine.lacuna", "write_scopes": ["lacuna/**"],
    })
    try:
        current = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
        restored = m._converge(
            "lacuna/gap.md", {
                "type": "lacuna", "id": "lacuna:gap",
                "prediction_candidate": "predictions/still-missing",
            }, expected_sha256=current,
        )
    finally:
        m._caller_var.reset(token)
    assert "+1 added" in restored, restored
    contract = {
        "api": 1,
        "allowed_namespaces": ["lacuna"],
        "allowed_types": ["lacuna"],
        "operations": ["converge"],
        "required_fields": ["type"],
        "required_relationships": [],
        "optional_relationships": ["prediction_candidate"],
        "body": {"required": False},
        "unknown_fields": "preserve",
        "unresolved_links": "review",
        "placeholder_links": "reject",
        "completion": "run",
    }
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{
        "name": "okengine.lacuna",
        "output_contract": contract,
        "output_contract_digest": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
    }]}), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "enforce")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:okengine.lacuna")
    current = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    repaired_by_job = m._converge(
        "lacuna/gap.md", {"type": "lacuna", "id": "lacuna:gap"},
        remove="prediction_candidate", expected_sha256=current,
    )
    assert "-1 removed" in repaired_by_job, repaired_by_job
    assert "prediction_candidate" not in _read_id(m, "lacuna/gap.md")


def test_explicit_converge_resolves_slug_collision_to_canonical(tmp_path):
    m = _load(tmp_path)
    m._converge("entities/one.md", "type: vendor\ntitle: Acme", body="old")
    out = m._converge(
        "entities/two.md", "type: vendor\ntitle: Acme\nsector: finance", body="new")
    assert out.startswith("converged into entities/o/one.md")
    assert not (tmp_path / "wiki" / "entities" / "two.md").exists()
    fm = _read_id(m, "entities/o/one.md")
    assert fm["sector"] == "finance"
    assert (tmp_path / "wiki" / "entities" / "o" / "one.md").read_text().endswith("new")
    queue = tmp_path / "wiki" / "_review-queue.md"
    assert not queue.exists() or "entities/two.md" not in queue.read_text()


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
    for slug in ("a", "b"):
        source = tmp_path / "wiki" / "sources" / "2026" / "07" / f"{slug}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            f"---\ntype: source\nname: {slug}\npublisher: Test\nsource_kind: report\n"
            "published: 2026-07-01\n---\nbody\n",
            encoding="utf-8",
        )
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


def test_converge_rejects_fabricated_source_namespace_on_merge(tmp_path):
    """An existing-page converge must enforce the same fabricated-source guard as create/update.

    Entity backfills use converge, so omitting this check allowed the known singular ``source/``
    hallucination signature onto an otherwise legitimate canonical entity.
    """
    m = _load(tmp_path)
    assert m._converge(
        "entities/acme.md", "type: vendor\ntitle: Acme Corp", "body"
    ).startswith("created")
    before = _read_id(m, "entities/acme.md")

    out = m._converge(
        "entities/acme.md",
        "type: vendor\ntitle: Acme Corp\nsources: [source/cisa/fabricated-report-2026]",
    )

    assert out.startswith("rejected:") and "source/" in out, out
    assert _read_id(m, "entities/acme.md") == before


def test_converge_grandfathers_unchanged_legacy_fabricated_source(tmp_path):
    """An unrelated merge must not jam a legacy-tainted page forever; only a
    newly introduced singular source/ reference is rejected."""
    m = _load(tmp_path)
    page = m._partitioned_create_path(
        m._safe("entities/acme.md"), {"type": "vendor", "title": "Acme Corp"}
    )
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: vendor\nid: entities:acme-corp\ntitle: Acme Corp\n"
        "sources: [source/cisa/legacy-fabricated-ref]\n---\nbody\n",
        encoding="utf-8",
    )

    result = m._converge(
        "entities/acme.md",
        "type: vendor\nid: entities:acme-corp\ntitle: Acme Corp\nactor_type: vendor",
    )

    assert result.startswith("converged into"), result
    fm, _ = m._read_page(page)
    assert fm["actor_type"] == "vendor"
    assert fm["sources"] == ["source/cisa/legacy-fabricated-ref"]


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


def test_converge_capability_uses_target_type_and_includes_removed_fields(tmp_path, monkeypatch):
    m = _load(tmp_path)
    page = tmp_path / "wiki/entities/v/victim.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: vendor\nname: Victim\nsecret_grade: A\nid: entities:victim\n---\nbody\n",
        encoding="utf-8",
    )
    m._registries.clear()
    policy = {
        "rules": [{"id": "narrow-converge", "severity": "reject"}],
        "capabilities": {"cron:lane": {
            "rule_id": "narrow-converge", "operations": ["converge"],
            "paths": ["entities/**"], "types": ["vendor"],
            "update_fields": ["name", "type"], "body": "deny",
        }},
    }
    monkeypatch.setattr(m, "_effective_policy", lambda: policy)
    token = m._caller_var.set({"kind": "job", "actor": "cron:lane"})
    try:
        removed = m._converge(
            "entities/victim", {"type": "vendor", "name": "Victim"}, remove="secret_grade"
        )
        assert removed.startswith("rejected:") and "secret_grade" in removed
        assert "secret_grade: A" in page.read_text()

        page.write_text(page.read_text().replace("type: vendor", "type: attack-pattern"))
        m._registries.clear()
        wrong_type = m._converge(
            "entities/victim", {"type": "vendor", "name": "Changed"}
        )
        assert wrong_type.startswith("rejected:") and "attack-pattern" in wrong_type
        assert "name: Victim" in page.read_text()

        page.write_text(page.read_text().replace("type: attack-pattern", "type: vendor"))
        m._registries.clear()
        resulting_type = m._converge(
            "entities/victim", {"type": "malware", "name": "Changed"}
        )
        assert resulting_type.startswith("rejected:") and "malware" in resulting_type
        assert "type: vendor" in page.read_text()
    finally:
        m._caller_var.reset(token)


def test_converge_allows_authorized_resulting_type_transition(tmp_path, monkeypatch):
    m = _load(tmp_path)
    page = tmp_path / "wiki/entities/v/victim.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: vendor\nname: Victim\nid: entities:victim\n---\nbody\n",
        encoding="utf-8",
    )
    m._registries.clear()
    monkeypatch.setattr(m, "_contract_reject", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(m, "_effective_policy", lambda: {
        "rules": [{"id": "transition", "severity": "reject"}],
        "capabilities": {"cron:lane": {
            "rule_id": "transition", "operations": ["converge"],
            "paths": ["entities/**"], "types": ["vendor", "malware"],
            "update_fields": ["name", "type"], "body": "deny",
        }},
    })
    token = m._caller_var.set({"kind": "job", "actor": "cron:lane"})
    try:
        result = m._converge("entities/victim", {"type": "malware", "name": "Victim"})
    finally:
        m._caller_var.reset(token)
    assert result.startswith("converged into"), result
    assert "type: malware" in page.read_text()


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


def test_raw_backfill_normalizes_storage_directory_as_source_kind(tmp_path):
    m = _load(tmp_path, _TYPENS)
    fm, error = m._raw_backfill_frontmatter(
        "type: qualification\nsource_kind: qualification\npublisher: Lab"
    )
    assert error is None
    assert fm["type"] == "source"
    assert fm["source_kind"] == "report"


def test_raw_backfill_preserves_valid_source_kind(tmp_path):
    m = _load(tmp_path, _TYPENS)
    fm, error = m._raw_backfill_frontmatter(
        "type: source\nsource_kind: vendor-research\npublisher: Lab"
    )
    assert error is None
    assert fm["source_kind"] == "vendor-research"


def test_raw_backfill_accepts_structured_frontmatter_from_local_model(tmp_path):
    m = _load(tmp_path, _TYPENS)
    fm, error = m._raw_backfill_frontmatter({
        "type": "source",
        "source_kind": "report",
        "publisher": "Lab",
    })
    assert error is None
    assert fm["publisher"] == "Lab"


def test_raw_backfill_recovers_url_from_selected_raw_evidence(tmp_path, monkeypatch):
    m = _load(tmp_path, _TYPENS)
    raw = tmp_path / "raw" / "qualification" / "control.md"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "# Control\n\nURL: https://qualification.invalid/raw/control\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    assert m._selected_raw_url(["raw/qualification/control.md"]) == \
        "https://qualification.invalid/raw/control"
    assert m._selected_raw_url(["sources/not-raw.md"]) == ""


def test_concept_backfill_slug_identity_follows_requested_target(tmp_path, monkeypatch):
    m = _load(tmp_path, _TYPENS)
    # Exercise the invariant independently of FastMCP registration: a stale
    # concepts slug copied from evidence is not the requested page's identity.
    rel = "concepts/q/w/qwen-control-g2.md"
    fm = {"type": "concept", "id": "concepts:qwen-control", "title": "Qwen Control G2"}
    requested = f"concepts:{Path(rel).stem}"
    supplied = str(fm["id"])
    if supplied.startswith("sources:") or \
            supplied.startswith("concepts:") and supplied != requested:
        fm.pop("id", None)
    assert "id" not in fm


def test_every_generic_label_family_member_is_refused(tmp_path):
    """The list IS the guard, and a partial list is a guard with holes.

    It previously held only the two examples the entity-backfill PROMPT happens to name ("threat
    actor", "AI agents") plus a couple of neighbours. `Attacker`, `Malware Campaign` and
    `Ransomware Gang` were all created on a live vault AFTER this guard shipped, because nobody had
    listed them — the prompt already forbade them, which is exactly why the prompt is not the
    enforcement.

    Plurals are listed explicitly: the write path normalises punctuation, not grammar, so
    "Attackers" is a different string from "Attacker" and would be the next page created.
    """
    schema = (
        "types:\n  actor: {required: [type]}\n  publisher: {required: [type]}\n"
        "  identity: {required: [type]}\n"
    )
    m = _load(tmp_path, schema)
    for title in ("Attacker", "Attackers", "Intruder", "Malware Campaign", "Ransomware Gang",
                  "Ransomware Gangs", "Threat Actors", "Cybercriminal Group", "Hacker Group",
                  "Unknown", "AI Agents", "Autonomous LLM Agent", "Adversary", "LLM Agents"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: unknown\n")
        assert cleaned is None, f"{title!r} was accepted as a named threat actor"
        assert "generic class labels" in error, f"{title!r}: wrong rejection ({error})"


def test_a_named_actor_containing_a_generic_word_is_still_accepted(tmp_path):
    """The guard matches the WHOLE normalised title, never a substring. Real names are built from
    generic words -- `Comment Crew` is APT1 -- and a substring rule would refuse the corpus."""
    schema = "types:\n  actor: {required: [type]}\n"
    m = _load(tmp_path, schema)
    for title in ("Comment Crew", "Scattered Spider", "Volt Typhoon", "Sandworm Team",
                  "DragonForce ransomware cartel", "Unknown Wolf"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: cybercriminal\n",
            f"{title} is a named cybercriminal group tracked by defenders.")
        assert cleaned is not None, f"{title!r} was wrongly refused: {error}"


def test_terminalfix_variant_is_rejected_as_a_non_actor(tmp_path):
    m = _load(tmp_path, "types:\n  actor: {required: [type]}\n")
    body = (
        "A new ClickFix variant, dubbed TerminalFix, that aims to trick users into running a "
        "malicious command in Windows Terminal or PowerShell."
    )
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/t/terminalfix",
        "type: actor\ntitle: TerminalFix\nactor_type: cybercriminal\nrecent_news: 4\n",
        body,
    )
    assert cleaned is None
    assert "variant, not a threat actor" in error


def test_entity_backfill_requires_positive_actor_identity_evidence(tmp_path):
    m = _load(tmp_path, "types:\n  actor: {required: [type]}\n")
    cleaned, error = m._entity_backfill_frontmatter(
        "entities/s/shinyhunters",
        "type: actor\ntitle: ShinyHunters\nactor_type: cybercriminal\nrecent_news: 16\n",
        "ShinyHunters appeared in sixteen articles.",
    )
    assert cleaned is None
    assert "source-grounded evidence" in error

    cleaned, error = m._entity_backfill_frontmatter(
        "entities/s/shinyhunters",
        "type: actor\ntitle: ShinyHunters\nactor_type: cybercriminal\n",
        "ShinyHunters is a named cybercriminal group associated with data theft and extortion.",
    )
    assert error is None
    assert __import__("yaml").safe_load(cleaned)["actor_identity_validated"] is True


def test_the_lane_reached_a_label_the_list_did_not_have(tmp_path):
    """Live counter-evidence: while this guard was in review, the lane created `Initial Access
    Brokers` on a real vault. A scan of all 1,018 actor titles then surfaced `AI Attackers`,
    `Unnamed Actor`, `Placeholder` and `Threat actor name` as well.

    Enumerating loses to a generative writer, which is the point of okengine#592. These are the
    members it did reach, listed because a guard that is known to be incomplete should at least
    not be incomplete in the ways already observed.
    """
    schema = "types:\n  actor: {required: [type]}\n"
    m = _load(tmp_path, schema)
    for title in ("Initial Access Brokers", "Initial Access Broker", "AI Attackers",
                  "Unnamed Actor", "Placeholder", "Threat actor name", "Some Generic Actor",
                  "[Unnamed group]", "Iranian-Aligned Threat Actor", "Unknown threat actor"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: cybercriminal\n")
        assert cleaned is None, f"{title!r} was accepted as a named threat actor"
        assert "generic class labels" in error, f"{title!r}: wrong rejection ({error})"


def test_the_structural_rules_touch_no_real_name_on_a_live_corpus(tmp_path):
    """The two shapes in `_is_class_description` were measured against every actor title on a live
    vault before being written down: 9 matches, all junk. This pins the other side of that
    measurement -- names that share the shape's vocabulary but are real, including ALL-CAPS and
    hyphenated forms, and a five-word name ending in "Actor" that the four-word bound protects.
    """
    schema = "types:\n  actor: {required: [type]}\n"
    m = _load(tmp_path, schema)
    for title in ("Comment Crew", "The Shadow Brokers", "Yanbian Gang", "INDOHAXSEC TEAM",
                  "SYLHET GANG-SG", "Team-Xecuter", "Hacking Team", "Belsen Group",
                  "313 Team", "Iron Group", "Daixin Team", "Mana Team", "Team Jorge",
                  "Chaos Group", "Muddywater Group", "Keksec Group", "TeamSpy Crew",
                  "Clop Ransom Group", "Threat Actor 888",
                  "Silent Chollima Advanced Persistent Actor"):
        cleaned, error = m._entity_backfill_frontmatter(
            "entities/x", f"type: actor\ntitle: {title}\nactor_type: cybercriminal\n",
            f"{title} is a named cybercriminal group tracked by defenders.")
        assert cleaned is not None, f"{title!r} was wrongly refused: {error}"


def test_a_class_description_is_recognised_by_shape_not_by_membership(tmp_path):
    m = _load(tmp_path, "types:\n  actor: {required: [type]}\n")
    assert m._is_class_description("chinese aligned threat actor")   # ends in the category word
    assert m._is_class_description("several ransomware operators")   # a vagueness word
    assert not m._is_class_description("")                           # nothing to judge
    assert not m._is_class_description("comment crew")
    # bounded at four words, so a longer real name ending in "Actor" survives
    assert not m._is_class_description("silent chollima advanced persistent actor")


def test_unrelated_job_actor_cannot_claim_composed_extension_ownership(tmp_path, monkeypatch):
    m = _load(tmp_path, (
        "types:\n  lacuna: {required: [type]}\n"
        "partitioning:\n  namespaces: {lacuna: {strategy: flat}}\n"
    ))
    monkeypatch.setattr(m, "_governing", lambda _path: {
        "types": {"lacuna": {"required": ["type"]}},
        "partitioning": {"namespaces": {"lacuna": {"strategy": "flat"}}},
        "owners": {"types": {"lacuna": "ext:okengine.lacuna"}},
    })
    page = tmp_path / "wiki/lacuna/gap.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: lacuna\nid: lacuna:gap\nprediction_candidate: predictions/missing\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(m, "_wauth_refusal", lambda _path: None)
    monkeypatch.setattr(m, "_capability_reject", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(m, "_contract_reject", lambda *_args, **_kwargs: None)
    token = m._caller_var.set({"kind": "job", "actor": "cron:other-extension"})
    try:
        result = m._converge(
            "lacuna/gap.md", {"type": "lacuna", "id": "lacuna:gap"},
            remove="prediction_candidate",
        )
    finally:
        m._caller_var.reset(token)
    assert "1 conflict" in result
    assert _read_id(m, "lacuna/gap.md")["prediction_candidate"] == "predictions/missing"
