"""Regression: the G1 enforced MCP write path.

Verifies write_server's plain logic helpers validate against the walk-up
schema.yaml BEFORE writing, append to wiki/log.md on success, dedupe-refuse on
re-create, version-bump on update, retain-and-mark on tombstone, and queue
flags without mutating the target.

Tests call the plain `_create`/`_update`/`_tombstone`/`_flag` helpers directly
(the @mcp.tool() wrappers merely delegate, and `mcp` may be absent in the host
test env). The module is loaded by file path so no package install is needed.
"""
import importlib.util
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  source:
    required: [type, source_kind, publisher, published]
  entity:
    required: [type, name]
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
  namespaces:
    findings:
      create: false
      update: false
review:
  confidence_field: confidence
  confidence_review_values: [confirmed, false-positive, refuted]
  review_on_change_fields: [verified_by]
"""


def _load():
    spec = importlib.util.spec_from_file_location("write_server", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A temp vault with wiki/ and a governing schema.yaml so the validator fires.

    schema.yaml sits at the vault root (above wiki/), so the walk-up validator
    finds it for any wiki/**.md page. Date is pinned for deterministic logs.
    """
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    # Fresh import so module-level VAULT/WIKI pick up the env (helpers also
    # re-read WIKI_PATH at call time, but be explicit).
    sys.modules.pop("write_server", None)
    m = _load()
    return m, tmp_path


def _log_text(root: Path) -> str:
    log = root / "wiki" / "log.md"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _enable_source_contract(root: Path, monkeypatch, *, mode="enforce", operations=None):
    contract = {
        "api": 1, "allowed_namespaces": ["sources"], "allowed_types": ["source"],
        "operations": operations or ["create", "update", "patch", "append", "converge"],
        "required_fields": ["type", "raw"], "required_relationships": [],
        "body": {"required": True, "min_non_whitespace": 40},
        "unknown_fields": "reject", "unresolved_links": "reject",
        "placeholder_links": "reject", "completion": "per-selected-item",
    }
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    jobs = root / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"name": "raw-backfill", "output_contract": contract,
                                          "output_contract_digest": digest}]}))
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:raw-backfill")
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", mode)


def test_output_contract_rejects_qilin_class_atomically(vault, monkeypatch):
    m, root = vault
    _enable_source_contract(root, monkeypatch)
    monkeypatch.setattr(m, "_capability_reject", lambda *args, **kwargs: None)
    target = root / "wiki" / "sources" / "2026" / "07" / "bad.md"
    result = m._create("sources/2026/07/bad", {
        "type": "source", "source_kind": "vendor-blog", "publisher": "Example",
        "published": "2026-06-14",
    }, "[Read more](#)")
    assert "output_contract.required_field_missing" in result
    assert "body_too_short" in result and "placeholder_link" in result
    assert not target.exists()


def test_contract_only_lane_does_not_require_duplicate_policy_capability(vault, monkeypatch):
    m, root = vault
    _enable_source_contract(root, monkeypatch)
    monkeypatch.setattr(m, "_effective_policy", lambda: {"capabilities": {}})
    assert m._capability_reject(root / "wiki" / "sources" / "x.md", "create",
                                page_type="source") is None


def test_output_contract_accepts_valid_source_and_protects_updates(vault, monkeypatch):
    m, root = vault
    _enable_source_contract(root, monkeypatch)
    monkeypatch.setattr(m, "_capability_reject", lambda *args, **kwargs: None)
    body = "# Complete source\n\n" + ("Grounded captured content. " * 5)
    result = m._create("sources/2026/07/good", {
        "type": "source", "source_kind": "vendor-blog", "publisher": "Example",
        "published": "2026-06-14", "raw": "raw/clippings/good.md",
    }, body)
    assert result.startswith("created"), result
    target = root / "wiki" / "sources" / "2026" / "07" / "good.md"
    before = target.read_bytes()
    result = m._update("sources/2026/07/good", {}, "[placeholder](#)")
    assert "output_contract" in result and "placeholder_link" in result
    assert target.read_bytes() == before


def test_output_contract_report_mode_records_without_blocking(vault, monkeypatch):
    m, root = vault
    _enable_source_contract(root, monkeypatch, mode="report")
    monkeypatch.setattr(m, "_capability_reject", lambda *args, **kwargs: None)
    result = m._create("sources/2026/07/report-only", {
        "type": "source", "source_kind": "vendor-blog", "publisher": "Example",
        "published": "2026-06-14",
    }, "short")
    assert result.startswith("created"), result
    assert "output-contract report" in _log_text(root)


def test_output_contract_denies_tombstone_and_flag_unless_declared(vault, monkeypatch):
    m, root = vault
    _enable_source_contract(root, monkeypatch)
    monkeypatch.setattr(m, "_capability_reject", lambda *args, **kwargs: None)
    body = "# Complete source\n\n" + ("Grounded captured content. " * 5)
    assert m._create("sources/2026/07/protected", {
        "type": "source", "source_kind": "vendor-blog", "publisher": "Example",
        "published": "2026-06-14", "raw": "raw/clippings/protected.md",
    }, body).startswith("created")
    target = root / "wiki" / "sources" / "2026" / "07" / "protected.md"
    before = target.read_bytes()
    assert "operation_not_allowed" in m._tombstone("sources/2026/07/protected", "duplicate")
    assert "operation_not_allowed" in m._flag("sources/2026/07/protected", "inspect")
    assert target.read_bytes() == before
    assert not (root / "wiki" / "_review-queue.md").exists()


def test_output_contract_allows_declared_tombstone_and_flag(vault, monkeypatch):
    m, root = vault
    operations = ["create", "update", "patch", "append", "converge", "tombstone", "flag"]
    _enable_source_contract(root, monkeypatch, operations=operations)
    monkeypatch.setattr(m, "_capability_reject", lambda *args, **kwargs: None)
    body = "# Complete source\n\n" + ("Grounded captured content. " * 5)
    assert m._create("sources/2026/07/reviewed", {
        "type": "source", "source_kind": "vendor-blog", "publisher": "Example",
        "published": "2026-06-14", "raw": "raw/clippings/reviewed.md",
    }, body).startswith("created")
    assert m._flag("sources/2026/07/reviewed", "inspect").startswith("flagged")
    assert m._tombstone("sources/2026/07/reviewed", "duplicate").startswith("tombstoned")


# ── degeneration guard (soft flag at the enforced write boundary) ─────────────

def test_degeneration_flags_word_salad():
    m = _load()
    salad = " ".join(f"term{i}" for i in range(400))       # 400-word unpunctuated run (repetition loop)
    flags = m._degeneration_flags("# X\n\n" + salad + ".\n")
    assert any("unpunctuated run" in f for f in flags), flags


def test_degeneration_flags_legitimate_content_clean():
    m = _load()
    assert m._degeneration_flags("# X\n\nA coherent page. It has sentences. They end.\n") == []
    # a long comma-separated LIST (MITRE techniques / killed services) is legitimate, not filler
    assert m._degeneration_flags("Applies to: " + ", ".join(f"Svc {i}" for i in range(300)) + ".\n") == []
    # a long wikilink list is not prose
    assert m._degeneration_flags(" ".join(f"[[t/x-{i}|X{i}]]" for i in range(300)) + "\n") == []
    # Chinese CTI content is NOT degeneration — the dropped CJK-fusion signal used to flag it
    assert m._degeneration_flags("Aliases: XY助手, 熊猫Stealer. 东南亚新APT组织持续活跃。\n") == []
    assert m._degeneration_flags(None) == [] and m._degeneration_flags("") == []


def test_degeneration_guard_agrees_with_content_lint():
    """Cross-surface contract: the write-path guard and scripts/cron/content_lint.py must agree on the
    same samples, or a fix to one silently drifts from the other (the multi-surface-contract rule)."""
    m = _load()
    cl_spec = importlib.util.spec_from_file_location("content_lint", REPO / "scripts" / "cron" / "content_lint.py")
    cl = importlib.util.module_from_spec(cl_spec)
    cl_spec.loader.exec_module(cl)
    salad = " ".join(f"t{i}" for i in range(400))
    biglist = ", ".join(f"Svc {i}" for i in range(300))
    for body in (salad, biglist, "Clean prose. It ends.", "Aliases: XY助手, 熊猫Stealer."):
        guard = bool(m._degeneration_flags(body))
        lint = bool(cl.lint_text("x", body))
        assert guard == lint, f"guard/lint disagree on {body[:40]!r}: guard={guard} lint={lint}"


def test_create_degenerate_page_is_flagged_needs_review(vault):
    m, root = vault
    salad = " ".join(f"term{i}" for i in range(400))
    res = m._create("concepts/x/degen", {"type": "concept", "name": "Degen"}, "# Degen\n\n" + salad + ".\n")
    assert "review" in res.lower()
    page = (root / "wiki" / "concepts" / "x" / "degen.md").read_text(encoding="utf-8")
    assert "needs_review: true" in page


def test_entity_path_designation_conflict_is_flagged_for_quarantine(vault):
    """The Gentlemen regression: Storm-2698 in the path vs Storm-2697 in aliases."""
    m, root = vault
    res = m._create("entities/gentlemen-ransomware-group-storm-2698",
                    "type: actor\nname: The Gentlemen\naliases: [Storm-2697]",
                    "A substantive profile body with enough detail for review.")
    assert res.startswith("created") and "flagged for review" in res
    page = next((root / "wiki" / "entities").rglob("gentlemen-ransomware-group-storm-2698.md"))
    assert "needs_review: true" in page.read_text()
    assert "identity contradiction" in (root / "wiki" / "_review-queue.md").read_text()


def test_matching_entity_designation_is_not_flagged(vault):
    m, root = vault
    res = m._create("entities/storm-2697", "type: actor\nname: Storm-2697\naliases: [The Gentlemen]",
                    "A substantive profile body with enough detail for review.")
    assert res.startswith("created") and "flagged for review" not in res
    page = next((root / "wiki" / "entities").rglob("storm-2697.md"))
    assert "needs_review" not in page.read_text()


# ── _safe path normalization — dotted slugs must not be truncated ─────────────────────────────────

def test_dotted_slug_not_truncated(vault):
    """_safe forces the .md extension. It must APPEND, not with_suffix(), which strips everything
    after the LAST dot: 'sources/2026/07/openssl-3.0.7-advisory' would misfile to '...openssl-3.0.md',
    colliding distinct dotted prefixes onto one file and dead-linking every wikilink (invariant-audit
    M16)."""
    m, root = vault
    fm = {"type": "source", "source_kind": "advisory", "publisher": "OpenSSL", "published": "2026-06-01"}
    res = m._create("sources/2026/07/openssl-3.0.7-advisory", fm, "# Advisory\n\nBody.\n")
    assert res.startswith("created"), res
    good = root / "wiki" / "sources" / "2026" / "07" / "openssl-3.0.7-advisory.md"
    bad = root / "wiki" / "sources" / "2026" / "07" / "openssl-3.0.md"
    assert good.is_file(), "dotted slug must keep its full stem + .md"
    assert not bad.exists(), "must NOT truncate at the last dot"


def test_create_rejects_whitespace_and_run_on_basenames(vault):
    m, root = vault
    spaced = m._create("entities/a/prose sentence", {"type": "entity", "name": "X"}, "body")
    assert spaced.startswith("refused: unsafe wiki path")
    long_slug = "x" * 81
    run_on = m._create(f"entities/x/{long_slug}", {"type": "entity", "name": "X"}, "body")
    assert run_on.startswith("refused: unsafe wiki path")
    assert not list((root / "wiki" / "entities").rglob(f"{long_slug}.md"))


# ── engine-generated root dashboards are write-refused (paired with the validator exemption) ───────

def test_engine_dashboards_health_bundle_refused(vault):
    """schema_validator exempts HEALTH.md/BUNDLE.md from conformance (876fceb); if the write path does
    NOT also refuse them the two guards compose into ZERO protection — an agent overwrites the
    engine-generated dashboard with fabricated content and nothing objects (invariant-audit M17)."""
    m, root = vault
    for name in ("HEALTH", "BUNDLE"):
        assert m._create(name, "type: dashboard\ntitle: X", "# fabricated").startswith("refused:")
        assert m._update(name, {"type": "dashboard", "title": "Y"}, None).startswith("refused:")


def test_write_refusal_covers_validator_conformance_exemption(vault):
    """The true cross-surface contract (M17): EVERY basename the validator exempts from conformance
    (schema_validator._OKF_RESERVED_DEFAULT ∪ _is_generated_structural) MUST be write-refused, or the
    two guards compose into zero protection — an agent forges a conformance-invisible page. The
    round-2 re-verify showed the narrow health/bundle fix left the INDEX-<nonp> and dotfile families
    exposed, and that a test iterating only _OKF_RESERVED_DEFAULT never bound them. Assert the
    implication over a battery spanning every exempt family (the write path MAY refuse a superset)."""
    m, _root = vault
    sv_spec = importlib.util.spec_from_file_location("schema_validator", REPO / "tools" / "schema_validator.py")
    sv = importlib.util.module_from_spec(sv_spec)
    sv_spec.loader.exec_module(sv)

    def validator_exempt(bn):
        return bn.lower() in sv._OKF_RESERVED_DEFAULT or sv._is_generated_structural(bn)

    def write_refused(bn):
        return m._reserved_refuse(Path("wiki") / bn) is not None

    exempt = ["HEALTH.md", "BUNDLE.md", "HOT.md", "index.md", "INDEX.md", "INDEX-p02.md",
              "INDEX-glossary.md", "INDEX-summary.md", "_review-queue.md", ".backlinks.md",
              "log.md", "AGENTS.md", "README.md"]
    for bn in exempt:
        assert validator_exempt(bn), f"test battery wrong: {bn} should be validator-exempt"
        assert write_refused(bn), (
            f"{bn} is conformance-EXEMPT but NOT write-refused — the two guards compose to zero "
            f"protection (an agent forges a page nothing conformance-checks)")
    # and a clearly-normal knowledge page is NOT refused (no runaway over-refusal)
    for bn in ("apt-29.md", "openssl-advisory.md", "coverage-gap.md"):
        assert not write_refused(bn), f"{bn} is a normal page and must remain writable"


# ── tombstone invariant: no lane may resurrect a tombstoned page ─────────────────────────────────

def test_tombstoned_page_not_resurrected_by_update_patch_append(vault):
    """The never-resurrect guard lived ONLY on the converge lane (id-based); update/patch/append
    operate by PATH on the retained tombstone file, so they could silently un-tombstone it
    (invariant-audit M18). All three must refuse; tombstone_entity is the only lane that touches a
    tombstone."""
    m, root = vault
    rel = "entities/g/ghost"                        # canonical shard form (create/tombstone/update all agree)
    m._create(rel, {"type": "entity", "name": "Ghost"}, "# Ghost\n\nBody line.\n")
    assert m._tombstone(rel, "merged into successor").startswith("tombstoned")
    page = root / "wiki" / "entities" / "g" / "ghost.md"
    before = page.read_text(encoding="utf-8")
    assert m._update(rel, {"type": "entity", "name": "Ghost", "status": "active"}, None).startswith("refused")
    assert "tombstoned" in m._patch(rel, "Body line.", "Alive again.")
    assert m._append_section(rel, "Notes", "- resurrected").startswith("refused")
    assert page.read_text(encoding="utf-8") == before, "tombstoned page must be untouched by all three"


# ── int-shaped fields (machine-owned counts) — the recent_reports live incident ──────────────────

def test_create_rejects_list_in_int_field(vault):
    """An agent misreading `recent_reports` as \"list the recent reports\" hand-wrote a list of
    source paths; the malformed value then TOPPED the cockpit's Most-active sort. The write path
    must reject it, naming the field, before anything lands."""
    m, root = vault
    res = m._create("entities/b/badcount",
                    {"type": "entity", "name": "BadCount",
                     "recent_reports": ["sources/2026/07/some-report"]},
                    "# BadCount\n\nBody.\n")
    assert res.startswith("rejected:") and "recent_reports" in res, res
    assert not (root / "wiki" / "entities" / "b" / "badcount.md").exists()


def test_create_coerces_digit_string_count(vault):
    """`recent_reports: \"15\"` is unambiguous intent — coerce, don't churn the lane."""
    m, root = vault
    res = m._create("entities/g/goodcount",
                    {"type": "entity", "name": "GoodCount", "recent_reports": "15"},
                    "# GoodCount\n\nBody.\n")
    assert res.startswith("created"), res
    page = (root / "wiki" / "entities" / "g" / "goodcount.md").read_text(encoding="utf-8")
    assert "recent_reports: 15" in page


def test_update_rejects_garbage_int_field_and_leaves_file(vault):
    m, root = vault
    m._create("entities/o/okactor", {"type": "entity", "name": "OkActor", "total_mentions": 7},
              "# OkActor\n\nBody.\n")
    before = (root / "wiki" / "entities" / "o" / "okactor.md").read_text(encoding="utf-8")
    res = m._update("entities/o/okactor", {"type": "entity", "name": "OkActor",
                                           "total_mentions": "about a dozen"}, None)
    assert res.startswith("rejected:") and "total_mentions" in res, res
    assert (root / "wiki" / "entities" / "o" / "okactor.md").read_text(encoding="utf-8") == before


_DRIFT_SCHEMA = """\
okf:
  required: [type]
types:
  entity:
    required: [type, name]
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
field_aliases:
  country: suspected_origin
value_aliases:
  suspected_origin: {CN: China, US: United States}
  status: {active: live}
allowed:
  intrusion-set: [suspected_origin, motivation]
"""


@pytest.fixture
def drift_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_DRIFT_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    return _load(), tmp_path


def test_drift_field_and_value_aliases_normalized(drift_vault):
    """okengine#46: agent drift converges on the schema vocab BEFORE write —
    country->suspected_origin (key), CN->China + active->live (values)."""
    import yaml
    m, root = drift_vault
    res = m._create("entities/a/apt-x",
                    "type: intrusion-set\nname: APT-X\ncountry: CN\nstatus: active\nmotivation: espionage",
                    "body")
    assert res.startswith("created"), res
    fm = yaml.safe_load((root / "wiki" / "entities" / "a" / "apt-x.md").read_text().split("---")[1])
    assert fm.get("suspected_origin") == "China" and "country" not in fm
    assert fm.get("status") == "live"
    assert "unknown" not in res.lower()                  # every field known -> not flagged


def test_drift_unknown_field_flagged_not_rejected(drift_vault):
    """okengine#46: a field outside the type's allowed set is flagged for review (G3), kept, not
    rejected or dropped."""
    import yaml
    m, root = drift_vault
    res = m._create("entities/a/apt-y",
                    "type: intrusion-set\nname: APT-Y\nbogus_field: x\nmotivation: espionage", "body")
    assert res.startswith("created"), res                # flagged, NOT rejected
    txt = (root / "wiki" / "entities" / "a" / "apt-y.md").read_text()
    assert "needs_review: true" in txt
    fm = yaml.safe_load(txt.split("---")[1])
    assert fm.get("bogus_field") == "x"                  # preserved, just surfaced


def test_valid_create(vault):
    m, root = vault
    res = m._create(
        "entities/vendor/acme",
        "type: entity\nname: Acme Corp",
        "Acme is a vendor.",
    )
    assert res.startswith("created"), res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert p.is_file()
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["version"] == 1
    assert fm["last_updated"] == "2026-06-15"
    assert "create" in _log_text(root)
    assert "entities/vendor/acme.md v1" in _log_text(root)


def test_strict_pack_normalizes_type_alias_and_rejects_unknown(vault):
    m, root = vault
    (root / "schema.yaml").write_text(
        _SCHEMA.replace("strict_types: false", "strict_types: true")
        + "\ntype_aliases: {thing: entity}\n", encoding="utf-8")
    m.schema_lib._SCHEMA_CACHE.clear()
    m.schema_reject_reason.__globals__["_schema_cache"].clear()
    m.schema_reject_reason.__globals__["_dir_to_schema"].clear()
    created = m._create("entities/a/aliased", {"type": "thing", "name": "Aliased"}, "body")
    assert created.startswith("created"), created
    fm, _ = m._read_page(root / "wiki" / "entities" / "a" / "aliased.md")
    assert fm["type"] == "entity"
    rejected = m._create("entities/a/unknown", {"type": "wildcat", "name": "Unknown"}, "body")
    assert "unknown type 'wildcat'" in rejected
    assert not (root / "wiki" / "entities" / "a" / "unknown.md").exists()


def test_entity_shard_normalized_to_one_level(vault):
    """okengine#48: a two-level entity shard collapses to the one-level canonical path, so the
    agent can't create a stale duplicate of entities/<c>/<slug>.md. Non-shard segments and
    other namespaces are untouched."""
    m, root = vault
    assert m._normalize_entity_shard("entities/c/v/cve-2021-44228.md") == "entities/c/cve-2021-44228.md"
    assert m._normalize_entity_shard("entities/c/cve-2021-44228.md") == "entities/c/cve-2021-44228.md"
    assert m._normalize_entity_shard("entities/vendor/acme.md") == "entities/vendor/acme.md"   # multi-char kept
    assert m._normalize_entity_shard("sources/2026/06/x.md") == "sources/2026/06/x.md"          # other ns kept
    # end-to-end: a create at a two-level shard lands at the one-level path
    res = m._create("entities/a/p/apt-test", "type: entity\nname: APT-Test", "body")
    assert res.startswith("created"), res
    assert (root / "wiki" / "entities" / "a" / "apt-test.md").is_file()
    assert not (root / "wiki" / "entities" / "a" / "p" / "apt-test.md").exists()


def test_frontmatter_parser_is_line_anchored_not_substring_split(tmp_path):
    """okengine#349 root-1: the DragonForce page carried a frontmatter URL value whose query suffix
    held repeated hyphens (`?source=rss-1535934fa2f2------2`). The old `text.split('---', 2)` mistook
    that INLINE `---` for the closing delimiter — truncating the frontmatter and spilling `---2\n...`
    into the body. The line-anchored `_FM` parser only treats a `---` preceded by a newline as the
    close, so an inline `---` inside a value (or a YAML comment) can no longer terminate front matter.
    Pins the exact fixtures #349 asks for: a URL with `---`, a YAML comment containing `---`, and a
    body horizontal-rule `---`."""
    m = _load()
    page = tmp_path / "dragonforce.md"
    page.write_text(
        "---\n"
        "type: entity\n"
        "name: DragonForce\n"
        "source_url: https://ex.test/feed?source=rss-1535934fa2f2------2\n"
        "# a yaml comment containing --- inside it\n"
        "origin_country: null\n"
        "---\n"
        "# DragonForce\n\nCurated cartel body.\n\n---\n\nMore body.\n",
        encoding="utf-8")
    fm, body = m._read_page(page)
    # Frontmatter parsed in full — the inline hyphen-URL did NOT terminate it early.
    assert fm.get("name") == "DragonForce"
    assert str(fm.get("source_url", "")).endswith("------2")   # URL preserved verbatim
    assert "origin_country" in fm                              # field AFTER the hyphen-URL survived
    # Body is the real content, NOT a leaked `---2...` YAML fragment; the body's own `---` rule stays.
    assert body.startswith("# DragonForce")
    assert not body.lstrip().startswith("---2")
    assert "Curated cartel body." in body
    # A correctly-parsed hyphen-URL page must NOT be falsely refused as malformed.
    assert m._frontmatter_error(page) is None


def test_frontmatter_error_refuses_unparseable_existing_page(tmp_path):
    """okengine#349: a page that starts with `---` but has no line-anchored closing delimiter cannot
    be parsed unambiguously — the write path must REFUSE it rather than rewrite from a partial parse."""
    m = _load()
    bad = tmp_path / "truncated.md"
    bad.write_text("---\ntype: entity\nname: X\nno closing delimiter here\n", encoding="utf-8")
    err = m._frontmatter_error(bad)
    assert err and "frontmatter" in err.lower(), err


def test_entity_shard_preserves_resharded_two_level(vault):
    """okengine invariant-audit: once a hot first-letter leaf is resharded to two levels
    (entities/<l>/<2nd>/<slug>.md), the enforced write path must NOT collapse the resharded canonical
    back to one level — that refuses/duplicates writes on a mature vault. Shard letters recomputed
    from the slug either way, so an arbitrary/old path still resolves to the real canonical."""
    m, root = vault
    resh = root / "wiki" / "entities" / "c" / "v"
    resh.mkdir(parents=True)
    (resh / "cve-2021-44228.md").write_text("---\ntype: entity\nname: x\n---\nbody\n")
    # the resharded canonical is preserved (NOT collapsed to the nonexistent one-level path)
    assert m._normalize_entity_shard("entities/c/v/cve-2021-44228.md") == "entities/c/v/cve-2021-44228.md"
    # an agent passing the OLD one-level path is redirected UP to the resharded canonical
    assert m._normalize_entity_shard("entities/c/cve-2021-44228.md") == "entities/c/v/cve-2021-44228.md"
    # an arbitrary wrong second-shard is corrected to the real resharded location
    assert m._normalize_entity_shard("entities/c/z/cve-2021-44228.md") == "entities/c/v/cve-2021-44228.md"
    # a DIFFERENT slug in the same resharded leaf lands two-level (leaf has been resharded)
    assert m._normalize_entity_shard("entities/c/cobalt-strike.md") == "entities/c/o/cobalt-strike.md"


def test_malformed_create_rejected(vault):
    m, root = vault
    # type: source but missing publisher/published required fields.
    res = m._create("sources/bad", "type: source\nsource_kind: blog", "body")
    assert res.startswith("rejected:"), res
    assert "publisher" in res and "published" in res
    p = root / "wiki" / "sources" / "bad.md"
    assert not p.exists(), "rejected create must write nothing"
    assert _log_text(root) == "", "rejected create must not append a log line"


def test_path_escape_prefix_sibling_refused(vault):
    """A sibling like <vault>/wiki_evil must not pass a string-prefix check for
    <vault>/wiki. This locks the guard to Path.relative_to semantics."""
    m, root = vault
    res = m._create("../wiki_evil/pwn", "type: entity\nname: Pwn", "body")
    assert res.startswith("refused:"), res
    assert not (root / "wiki_evil" / "pwn.md").exists()


def test_flag_path_escape_refused(vault):
    m, root = vault
    res = m._flag("../wiki_evil/pwn", "bad\nnote")
    assert res.startswith("refused:"), res
    assert not (root / "wiki" / "_review-queue.md").exists()


def test_dedupe_refuse(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "v1 body")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    res = m._create("entities/vendor/acme", "type: entity\nname: Acme2", "v2 body")
    assert res.startswith("refused:") and "already exists" in res
    assert p.read_text() == before, "dedupe-refuse must leave the file unchanged"


def test_update_bumps_version(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "v1 body")
    res = m._update("entities/vendor/acme", "name: Acme Corp", "v2 body")
    assert res.startswith("updated") and "v2" in res, res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["version"] == 2
    assert fm["name"] == "Acme Corp"
    assert fm["last_updated"] == "2026-06-15"
    assert "v2 body" in p.read_text()
    assert "update" in _log_text(root)


def test_update_invalid_rejected_original_untouched(vault):
    m, root = vault
    m._create(
        "sources/acme-blog",
        "type: source\nsource_kind: blog\npublisher: Acme\npublished: 2026-01-01",
        "body",
    )
    p = root / "wiki" / "sources" / "acme-blog.md"
    before = p.read_text()
    # Strip a required field -> must be rejected, original (v1) preserved.
    res = m._update("sources/acme-blog", "publisher: null", None)
    assert res.startswith("rejected:"), res
    assert p.read_text() == before, "rejected update must not alter the file"
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["version"] == 1


def test_update_malformed_existing_frontmatter_refused(vault):
    m, root = vault
    # existing pages live at the sharded canonical (entities/<l>/<slug>.md); _update
    # normalizes the flat arg to the same shard, so plant the fixture there too.
    p = root / "wiki" / "entities" / "b" / "bad.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: [unterminated\n---\nbody")
    before = p.read_text()

    res = m._update("entities/bad", "type: entity\nname: Fixed", "new body")
    assert res.startswith("rejected:"), res
    assert "invalid frontmatter" in res
    assert p.read_text() == before


def test_tombstone_retains_file(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    res = m._tombstone("entities/vendor/acme", "merged into acme-inc",
                       superseded_by="entities/vendor/acme-inc")
    assert res.startswith("tombstoned"), res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert p.is_file(), "tombstone must NOT delete the file"
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["status"] == "tombstoned"
    assert fm["tombstone_reason"] == "merged into acme-inc"
    assert fm["superseded_by"] == "entities/vendor/acme-inc"
    assert fm["version"] == 2
    assert "tombstone" in _log_text(root)


def test_flag_for_review(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    res = m._flag("entities/vendor/acme", "publisher attribution looks wrong")
    assert res.startswith("flagged"), res
    queue = root / "wiki" / "_review-queue.md"
    assert queue.is_file()
    qtext = queue.read_text()
    assert "publisher attribution looks wrong" in qtext
    assert "entities/vendor/acme" in qtext
    assert p.read_text() == before, "flag must not mutate the target page"
    assert "flag" in _log_text(root)


def test_flag_for_review_replay_is_idempotent_by_page(vault):
    """A successful flag can be replayed when receipt persistence fails (#397)."""
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    first = m._flag("entities/vendor/acme", "first model wording")
    second = m._flag("entities/vendor/acme", "retry paraphrased the reason")

    queue = (root / "wiki" / "_review-queue.md").read_text()
    assert first.startswith("flagged")
    assert second.startswith("already flagged")
    assert queue.count("**entities/vendor/acme.md**") == 1
    assert "first model wording" in queue
    assert "retry paraphrased the reason" not in queue
    assert "flag already-queued entities/vendor/acme.md" in _log_text(root)
    assert "retry paraphrased the reason" in _log_text(root)


# --- G2 structural permissions + G3 review FLAGS (not gates) --------------

def test_tombstone_respects_update_denied_namespace(vault):
    """G2 via tombstone (okengine#166): a tombstone IS an update — a namespace the
    agent may not update (e.g. a human-authored or federated read-only lookup tree)
    must reject it through the same permission matrix as every other mutation.
    This path bypassed _policy_reject before the fix."""
    m, root = vault
    p = root / "wiki" / "findings" / "seeded.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: entity\nname: Seeded\nversion: 1\n---\nbody\n")
    res = m._tombstone("findings/seeded", "should not land")
    assert res.startswith("rejected:") and "not agent-writable" in res, res
    assert "tombstoned" not in p.read_text(), "file must be untouched"


def test_create_denied_namespace_refused(vault):
    """G2: a namespace with create:false is a STRUCTURAL boundary (human-authored)
    — the one hard reject. (delete:false is exercised via tombstone above.)"""
    m, root = vault
    res = m._create("findings/intrusion-x", "type: entity\nname: X", "body")
    assert res.startswith("rejected:") and "not agent-writable" in res, res
    assert not (root / "wiki" / "findings" / "intrusion-x.md").exists()


def _fm(p):
    import yaml
    return yaml.safe_load(p.read_text().split("---")[1])


def test_confidence_verdict_flags_not_blocks_on_create(vault):
    """G3: an agent CAN assert a categorical verdict — the write LANDS, but the
    page is flagged (needs_review + review queue), never blocked."""
    m, root = vault
    res = m._create("entities/vendor/acme",
                    "type: entity\nname: Acme\nconfidence: confirmed", "body")
    assert res.startswith("created") and "flagged for review" in res, res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert p.is_file()
    assert _fm(p)["needs_review"] is True
    q = (root / "wiki" / "_review-queue.md").read_text()
    assert "entities/vendor/acme.md" in q and "confidence" in q
    assert "review-flag" in _log_text(root)


def test_numeric_and_categorical_levels_do_not_flag(vault):
    """G3: numeric scores + low/medium/high are free — no flag, no needs_review."""
    m, root = vault
    assert m._create("entities/vendor/num", "type: entity\nname: N\nconfidence: 0.7", "b") \
        .startswith("created")
    res = m._create("entities/vendor/med", "type: entity\nname: M\nconfidence: medium", "b")
    assert res.startswith("created") and "flagged" not in res, res
    assert "needs_review" not in _fm(root / "wiki" / "entities" / "vendor" / "med.md")


def test_escalation_flags_but_preserve_does_not_refire(vault):
    """G3: escalating TO confirmed on update flags (write lands); a later update
    that PRESERVES the existing confirmed value does not re-flag."""
    m, root = vault
    m._create("entities/vendor/esc", "type: entity\nname: E\nconfidence: 0.6", "body")
    p = root / "wiki" / "entities" / "vendor" / "esc.md"
    res = m._update("entities/vendor/esc", "confidence: confirmed", None)
    assert res.startswith("updated") and "flagged for review" in res, res
    assert _fm(p)["confidence"] == "confirmed" and _fm(p)["needs_review"] is True
    q_lines_before = (root / "wiki" / "_review-queue.md").read_text().count("esc.md")
    # preserve confirmed (only touch name) -> no new flag line
    res = m._update("entities/vendor/esc", "name: E2", None)
    assert res.startswith("updated") and "flagged" not in res, res
    q_lines_after = (root / "wiki" / "_review-queue.md").read_text().count("esc.md")
    assert q_lines_after == q_lines_before, "preserve must not re-flag"


def test_review_on_change_field_flags(vault):
    """G3: setting a configured review_on_change_field flags the page (not blocks)."""
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    res = m._update("entities/vendor/acme", "verified_by: analyst", None)
    assert res.startswith("updated") and "flagged for review" in res, res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert _fm(p)["needs_review"] is True and _fm(p)["verified_by"] == "analyst"


# --- G1.1 surgical edits: patch_entity + append_to_section ----------------

def _mk(m, path, fm, body):
    assert m._create(path, fm, body).startswith("created")
    return None


def test_patch_replaces_one_place_body_preserved(vault):
    m, root = vault
    _mk(m, "concepts/c/ransomware", "type: concept\nname: R\nsources: [x]",
        "Intro.\n\nSee [[concepts/old-link]] for more.\n\nOutro stays.")
    res = m._patch("concepts/c/ransomware",
                   "[[concepts/old-link]]", "[[concepts/r/ransomware-ops]]")
    assert res.startswith("patched") and "v2" in res, res
    p = root / "wiki" / "concepts" / "c" / "ransomware.md"
    txt = p.read_text()
    assert "[[concepts/r/ransomware-ops]]" in txt
    assert "[[concepts/old-link]]" not in txt
    assert "Intro." in txt and "Outro stays." in txt   # body preserved
    assert _fm(p)["name"] == "R" and _fm(p)["version"] == 2
    assert "patch" in _log_text(root)


def test_patch_rejects_not_found_and_ambiguous(vault):
    m, root = vault
    _mk(m, "entities/vendor/acme", "type: entity\nname: Acme", "foo bar foo")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    assert m._patch("entities/vendor/acme", "nope", "x").startswith("rejected:")
    assert p.read_text() == before
    r = m._patch("entities/vendor/acme", "foo", "baz")  # 2 matches
    assert r.startswith("rejected:") and "matches 2" in r
    assert p.read_text() == before


def test_patch_field_loss_guard(vault):
    """An edit that drops an existing frontmatter key is rejected."""
    m, root = vault
    _mk(m, "entities/vendor/acme",
        "type: entity\nname: Acme\npriority_tier: direct", "body")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    # try to patch out the priority_tier line entirely
    res = m._patch("entities/vendor/acme", "priority_tier: direct\n", "")
    assert res.startswith("rejected:") and "priority_tier" in res, res
    assert p.read_text() == before, "field-loss reject must leave the file untouched"


def test_patch_insert_section_before_heading(vault):
    m, root = vault
    _mk(m, "predictions/p1",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Reasoning\n\nBecause.\n\n## What would refute this\n\nIf not.\n")
    anchor = "## What would refute this"
    new = "## Structural analysis\n\nPaths.\n\n" + anchor
    res = m._patch("predictions/p1", anchor, new)
    assert res.startswith("patched"), res
    txt = (root / "wiki" / "predictions" / "p1.md").read_text()
    assert txt.index("## Structural analysis") < txt.index("## What would refute this")
    assert "## Reasoning" in txt


def test_append_to_existing_section(vault):
    m, root = vault
    _mk(m, "predictions/p2",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Claim\n\nC.\n\n## Evidence log\n\n- old entry\n")
    res = m._append_section("predictions/p2", "Evidence log", "- new entry 2026-06-15")
    assert res.startswith("appended") and "existing section" in res, res
    txt = (root / "wiki" / "predictions" / "p2.md").read_text()
    assert "- old entry" in txt and "- new entry 2026-06-15" in txt
    # new entry sits inside the Evidence log section (after old entry)
    assert txt.index("- old entry") < txt.index("- new entry")
    assert _fm(root / "wiki" / "predictions" / "p2.md")["version"] == 2


def test_append_creates_missing_section(vault):
    m, root = vault
    _mk(m, "predictions/p3",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Claim\n\nC.\n")
    res = m._append_section("predictions/p3", "Postmortem", "Resolved confirmed.")
    assert res.startswith("appended") and "section created" in res, res
    txt = (root / "wiki" / "predictions" / "p3.md").read_text()
    assert "## Postmortem" in txt and "Resolved confirmed." in txt
    assert "## Claim" in txt


def test_append_section_normalizes_prefixed_heading(vault):
    """okengine#242: an agent may pass an already-`##`-prefixed section name. The create
    path must NOT double-prefix (`## ## Recent activity` — the fleet-wide corruption class).
    Both create and re-append on the same page must produce exactly ONE `## Recent activity`."""
    m, root = vault
    _mk(m, "predictions/p242",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Claim\n\nC.\n")
    # 1. create the section with a '## '-prefixed heading arg
    m._append_section("predictions/p242", "## Recent activity", "- entry one")
    txt = (root / "wiki" / "predictions" / "p242.md").read_text()
    assert "## ## Recent activity" not in txt, "double-prefixed heading (the #242 bug)"
    assert txt.count("## Recent activity") == 1
    # 2. re-append with the SAME prefixed arg must match the existing section, not make a 2nd
    m._append_section("predictions/p242", "## Recent activity", "- entry two")
    txt = (root / "wiki" / "predictions" / "p242.md").read_text()
    assert txt.count("## Recent activity") == 1, "second append created a duplicate section"
    assert "- entry one" in txt and "- entry two" in txt


def test_write_path_rejects_introduced_reader_derived_panels(vault):
    m, root = vault
    created = m._create(
        "entities/a/acme",
        "type: entity\nname: Acme",
        "# Acme\n\n## Incoming backlinks\n\n- frozen graph\n",
    )
    assert created.startswith("rejected:") and "reader-derived panel" in created

    _mk(m, "entities/b/beta", "type: entity\nname: Beta", "# Beta\n\n## Notes\n\nClean.\n")
    before = (root / "wiki" / "entities" / "b" / "beta.md").read_text()
    appended = m._append_section("entities/b/beta", "Outbound references", "- computed")
    assert appended.startswith("rejected:") and "reader-derived panel" in appended
    assert (root / "wiki" / "entities" / "b" / "beta.md").read_text() == before


def test_body_integrity_guard_ignores_fenced_markdown_examples(vault):
    m, root = vault
    body = (
        "# Acme\n\n## Notes\n\nExample:\n\n"
        "```markdown\n## ## Recent activity\n## Incoming backlinks\n```\n"
    )

    created = m._create("entities/a/acme", "type: entity\nname: Acme", body)

    assert created.startswith("created"), created
    assert "## Incoming backlinks" in (
        root / "wiki" / "entities" / "a" / "acme.md"
    ).read_text()


def test_legacy_derived_panel_can_be_removed(vault):
    m, root = vault
    path = root / "wiki" / "entities" / "a" / "acme.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntype: entity\nname: Acme\nid: entities:a:acme\nversion: 1\n---\n"
        "# Acme\n\n## Incoming backlinks\n\n- stale\n\n## Notes\n\nKeep.\n"
    )

    removed = m._patch(
        "entities/a/acme",
        "## Incoming backlinks\n\n- stale\n\n",
        "",
    )
    assert removed.startswith("patched"), removed
    assert "Incoming backlinks" not in path.read_text()


def test_reserved_files_refused(vault):
    """Engine-managed structural files (log.md/index.md/INDEX.md/AGENTS.md/HOT.md/
    _review-queue.md) are NOT agent-writable — append/patch/update/create on them
    must refuse, never inject frontmatter into a plain changelog."""
    m, root = vault
    log = root / "wiki" / "log.md"
    log.write_text("# Wiki Log\n\n- existing entry\n")
    before = log.read_text()
    assert m._append_section("log", "Whatever", "- new").startswith("refused:")
    assert m._patch("log", "existing", "changed").startswith("refused:")
    assert m._update("log", "version: 9", None).startswith("refused:")
    assert m._create("index", "type: dashboard\ntitle: X", "b").startswith("refused:")
    assert m._create("_review-queue", "type: x", "b").startswith("refused:")
    assert log.read_text() == before, "reserved file must be untouched (no frontmatter injected)"
    assert not log.read_text().startswith("---")


def test_leading_wiki_prefix_not_doubled(vault):
    """A path that already carries a leading wiki/ must land at wiki/<rel>, not
    wiki/wiki/<rel>. The doubled path stays *inside* wiki/ so the escape guard
    never catches it — it silently misfiles every page and breaks raw-drain
    dedup (okengine#31)."""
    m, root = vault
    res = m._create("wiki/entities/vendor/acme", "type: entity\nname: Acme", "body")
    assert res.startswith("created"), res
    good = root / "wiki" / "entities" / "vendor" / "acme.md"
    doubled = root / "wiki" / "wiki" / "entities" / "vendor" / "acme.md"
    assert good.is_file(), "page must land at wiki/entities/..."
    assert not doubled.exists(), "must NOT double into wiki/wiki/..."
    # The plain and wiki/-prefixed forms normalize to the same resolved path.
    assert m._safe("entities/vendor/acme") == m._safe("wiki/entities/vendor/acme")


def test_over_qualified_vault_path_not_shadowed(vault):
    """Over-qualified-path variant of okengine#31/#34: an agent that follows the persona's 'prefer the absolute form'
    guidance (correct for file_read) may pass a write tool the FULL vault path —
    `<vault>/wiki/sources/...` or the vault-relative `<...>/wiki/sources/...`.
    Those stay *inside* wiki/, so the escape guard misses them and the write lands
    in a shadow `wiki/<vault>/wiki/...` tree — a duplicate canonical. _safe must
    collapse the over-qualified prefix to the wiki-relative tail."""
    m, root = vault
    rel = "sources/2026/06/09/unit42-cloud-logging"
    body = "type: source\nsource_kind: vendor-research\npublisher: Unit 42\npublished: 2026-06-09"
    # 1) absolute vault/wiki form (the exact shape seen in production)
    abs_form = str(root / "wiki" / rel)
    res = m._create(abs_form, body, "x")
    assert res.startswith("created"), res
    good = root / "wiki" / "sources" / "2026" / "06" / "09" / "unit42-cloud-logging.md"
    assert good.is_file(), "page must land at the canonical wiki/sources/... path"
    # no shadow tree anywhere under wiki/ (e.g. wiki/.../wiki/sources/...)
    shadows = [p for p in (root / "wiki").rglob("unit42-cloud-logging.md")
               if p != good]
    assert not shadows, f"over-qualified path created a shadow page: {shadows}"
    # 2) absolute & vault-relative over-qualified forms normalize to the canonical path
    assert m._safe(abs_form) == m._safe(rel)
    assert m._safe(abs_form.lstrip("/")) == m._safe(rel)


def test_create_slug_variant_does_not_duplicate(vault):
    """okengine#98/#99/#100: create_entity keys on IDENTITY, not the filename. The
    same entity written a second time under a cosmetically different path/slug (here
    `Akira` vs `akira`, different shard dir) derives the same minted slug id and is
    refused as a slug collision + flagged — so exactly ONE canonical exists instead
    of a stale duplicate the assembler never reconciles."""
    m, root = vault
    r1 = m._create("entities/a/akira", "type: entity\nname: Akira", "first")
    assert r1.startswith("created"), r1
    # same identity (name), cosmetically different filename + shard dir
    r2 = m._create("entities/vendor/Akira", "type: entity\nname: akira", "second")
    assert r2.startswith("refused:") and "slug id" in r2 and "akira" in r2, r2
    # only the first canonical exists; the duplicate path was never written
    assert (root / "wiki" / "entities" / "a" / "akira.md").is_file()
    assert not (root / "wiki" / "entities" / "vendor" / "Akira.md").exists()
    assert "slug id collision" in (root / "wiki" / "_review-queue.md").read_text()
    # the stamped id is the content-derived identity, independent of the path
    assert _fm(root / "wiki" / "entities" / "a" / "akira.md")["id"] == "entities:akira"


def test_create_name_matching_existing_alias_converges(vault):
    m, root = vault
    first = m._create(
        "entities/s/shinyhunters",
        {"type": "entity", "name": "ShinyHunters", "aliases": ["UNC6240"]},
        "canonical body",
    )
    assert first.startswith("created"), first
    second = m._create(
        "entities/u/unc6240",
        {"type": "entity", "name": "UNC6240", "aliases": ["ShinyHunters crew"]},
        "new observation",
    )
    assert second.startswith("converged"), second
    assert not (root / "wiki" / "entities" / "u" / "unc6240.md").exists()
    canonical = _fm(root / "wiki" / "entities" / "s" / "shinyhunters.md")
    assert "ShinyHunters crew" in canonical["aliases"]


def test_invalid_type_rejects_before_alias_dedup(vault):
    m, root = vault
    assert m._create(
        "entities/s/shinyhunters",
        {"type": "entity", "name": "ShinyHunters", "aliases": ["UNC6240"]},
        "canonical body",
    ).startswith("created")
    out = m._create(
        "entities/u/unc6240",
        {"type": "threat_actor", "name": "UNC6240"},
        "bad type",
    )
    assert out.startswith("rejected:") and "type" in out
    assert "bad type" not in (root / "wiki" / "entities" / "s" / "shinyhunters.md").read_text()


def test_create_stamps_name_from_h1_when_absent(vault):
    """Source ingest (agent -> okengine-write) often puts the article title in the
    body's `# H1` but sets no `name`/`title`, leaving the page nameless. The write
    path derives `name` from the true H1 when both are absent — never overriding a
    curated name, and never picking up a `## Summary` section heading."""
    m, root = vault
    body = "## Summary\nblah\n\n# Crypto Clipper uses Tor for propagation\nbody\n"
    res = m._create("sources/2026/06/crypto-clipper", "type: source\npublisher: MS\n"
                    "source_kind: vendor-research\npublished: 2026-06-14", body)
    assert res.startswith("created"), res
    p = root / "wiki" / "sources" / "2026" / "06" / "crypto-clipper.md"
    assert _fm(p)["name"] == "Crypto Clipper uses Tor for propagation"   # from the # H1, not ## Summary

    # a curated name is never overridden
    m._create("entities/a/acme", "type: entity\nname: Acme Corp", "# Something Else\nx")
    assert _fm(root / "wiki" / "entities" / "a" / "acme.md")["name"] == "Acme Corp"

    # no name and no true H1 -> left nameless (no spurious stamp; source has no
    # required `name`, so it still creates)
    m._create("sources/2026/06/nohdr", "type: source\npublisher: MS\n"
              "source_kind: vendor-research\npublished: 2026-06-14", "## Summary only\nno h1 here\n")
    assert "name" not in _fm(root / "wiki" / "sources" / "2026" / "06" / "nohdr.md")


def test_blank_wiki_path_falls_back_to_default(monkeypatch):
    """A set-but-blank WIKI_PATH must fall back to the /opt/vault default, not
    resolve to a *relative* wiki/ under CWD (okengine#34)."""
    monkeypatch.setenv("WIKI_PATH", "")
    sys.modules.pop("write_server", None)
    m = _load()
    assert m._wiki().is_absolute(), "blank WIKI_PATH must not yield a relative path"
    assert m._wiki() == Path("/opt/vault") / "wiki"


def test_create_stamps_immutable_created(vault, monkeypatch):
    """A create stamps an immutable `created` (ingest date); a later update preserves it while
    last_updated moves — so age/recent-ingest reporting is accurate (not last_updated drift)."""
    import yaml
    m, root = vault
    m._create("entities/f/foo", "type: entity\nname: Foo", "body")
    p = root / "wiki" / "entities" / "f" / "foo.md"
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["created"] == "2026-06-15" and fm["version"] == 1
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-20")
    m._update("entities/f/foo", "type: entity\nname: Foo 2", None)
    fm2 = yaml.safe_load(p.read_text().split("---")[1])
    assert fm2["created"] == "2026-06-15"        # immutable across updates
    assert fm2["last_updated"] == "2026-06-20"    # moves
    assert fm2["version"] == 2


def test_update_clears_body_on_empty_keeps_on_none(vault):
    """okengine#52: body='' intentionally CLEARS the page body; body=None KEEPS it — so the
    update_entity wrapper (now passing body through) can clear a body, not silently keep it."""
    m, root = vault
    m._create("entities/f/foo", "type: entity\nname: Foo", "original body text")
    p = root / "wiki" / "entities" / "f" / "foo.md"
    m._update("entities/f/foo", None, None)                  # body=None -> keep
    assert "original body text" in p.read_text()
    m._update("entities/f/foo", None, "")                    # body="" -> clear
    txt = p.read_text()
    assert "original body text" not in txt
    assert txt.split("---", 2)[2].strip() == ""              # body emptied


_NS_SCHEMA = """\
okf:
  required: [type]
types:
  source: {required: [type, source_kind, published]}
  entity: {required: [type, name]}
strict_types: false
partitioning:
  namespaces:
    sources:  {strategy: by-date, date_field: published}
    entities: {strategy: by-letter}
exclude:
  - wiki/operational/
permissions:
  default: {create: true, update: true, delete: false}
"""


@pytest.fixture
def ns_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_NS_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    return _load(), tmp_path


def test_create_rejects_stray_namespace(ns_vault):
    """okengine#115: a type:source page must land in the schema's `sources/`; a stray
    `source/` (singular) is refused with a hint, so content can't fork into a tree the
    dashboards/index never see (the duplicate-namespace bug found dogfooding)."""
    m, root = ns_vault
    res = m._create("source/some-paper",
                    "type: source\nsource_kind: paper\npublished: 2026-06-01", "# Some Paper")
    assert res.startswith("rejected"), res
    assert "not declared" in res and "sources" in res    # names the issue + the right namespace
    assert not (root / "wiki" / "source").exists()        # nothing written to the stray namespace


def test_create_allows_declared_namespace(ns_vault):
    """The canonical `sources/` namespace is accepted — the guard only blocks strays."""
    m, root = ns_vault
    res = m._create("sources/2026-06-01-some-paper",
                    "type: source\nsource_kind: paper\npublished: 2026-06-01", "# Some Paper")
    assert res.startswith("created"), res


def test_create_allows_excluded_namespace(ns_vault):
    """Engine-internal excluded dirs (operational/) are not knowledge strays — not blocked."""
    m, root = ns_vault
    res = m._create("operational/health", "type: dashboard\ntitle: Health", "# Health")
    assert "not declared" not in res, res


# ── type<->namespace consistency guard (okengine#276) ─────────────────────────
_TNS_SCHEMA = """\
okf:
  required: [type]
types:
  source: {required: [type, source_kind, published]}
  concept: {required: [type]}
  cve: {required: [type]}
  entity: {required: [type, name]}
strict_types: false
partitioning:
  namespaces:
    sources:  {strategy: by-date, date_field: published}
    concepts: {strategy: by-letter}
    entities: {strategy: by-letter}
    cves:     {strategy: by-date, date_field: date_added}
# domain type home (core types use the built-in convention; cve is declared here)
type_namespaces:
  cve: cves
exclude:
  - wiki/operational/
permissions:
  default: {create: true, update: true, delete: false}
"""


@pytest.fixture
def tns_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_TNS_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    return _load(), tmp_path


def test_type_home_namespace_helper():
    """schema_lib.type_home_namespace: `type_namespaces` wins, else the core convention, else None."""
    import importlib.util
    sl_spec = importlib.util.spec_from_file_location("schema_lib", REPO / "scripts" / "cron" / "schema_lib.py")
    sl = importlib.util.module_from_spec(sl_spec)
    sl_spec.loader.exec_module(sl)
    sch = {"type_namespaces": {"cve": "cves"}}
    assert sl.type_home_namespace(sch, "cve") == "cves"        # declared override
    assert sl.type_home_namespace(sch, "source") == "sources"  # core convention
    assert sl.type_home_namespace(sch, "entity") is None       # no rule -> guard no-ops


def test_create_rejects_type_in_wrong_declared_namespace(tns_vault):
    """okengine#276: a `type: source` page written under `concepts/` (both declared, but source
    belongs in sources/) is refused — the exact misfiling the compile lane produced."""
    m, root = tns_vault
    res = m._create("concepts/c/some-paper",
                    "type: source\nsource_kind: paper\npublished: 2026-06-01", "# Paper")
    assert res.startswith("rejected"), res
    assert "belongs in 'sources/'" in res and "concepts/" in res
    assert not (root / "wiki" / "concepts" / "c" / "some-paper.md").exists()   # nothing written


def test_create_allows_type_in_home_namespace(tns_vault):
    """The canonical home is accepted — core convention (source->sources) + declared (cve->cves)."""
    m, root = tns_vault
    assert m._create("sources/2026-06-01-paper",
                     "type: source\nsource_kind: paper\npublished: 2026-06-01", "# P").startswith("created")
    assert m._create("cves/2026/CVE-2026-1",
                     "type: cve\ndate_added: 2026-06-01", "# CVE").startswith("created")


def test_create_rejects_domain_type_via_type_namespaces(tns_vault):
    """A schema-declared domain home is enforced too: a `type: cve` under `entities/` is refused."""
    m, root = tns_vault
    res = m._create("entities/c/CVE-2026-9", "type: cve\ndate_added: 2026-06-01", "# CVE")
    assert res.startswith("rejected") and "belongs in 'cves/'" in res, res


def test_create_no_op_for_type_without_a_home_rule(tns_vault):
    """A type with no home rule (not core, not in type_namespaces) is NOT blocked by this guard —
    no vacuous reject; `entity` can live in the declared `entities/`."""
    m, root = tns_vault
    res = m._create("entities/g/ghost", "type: entity\nname: Ghost", "# Ghost\n\nBody.\n")
    assert res.startswith("created"), res


# --- frontmatter reference normalization (wikilink -> plain path) -----------------
# Agents write [[wikilinks]]; in YAML `[[x]]` is a nested flow sequence, so a bare wikilink in a
# frontmatter value mangles. _coerce_fm canonicalizes refs to plain paths at the enforced-write
# chokepoint, fixing every extension at once. (okengine#145 follow-up)

def test_normalize_refs_strips_bare_wikilink_string():
    m = _load()
    fm = m._normalize_refs({"field_mapped": "[[concepts/x]]", "title": "Plain Title"})
    assert fm["field_mapped"] == "concepts/x"
    assert fm["title"] == "Plain Title"            # non-wikilink string untouched


def test_normalize_refs_flattens_yaml_mangled_nested_lists():
    m = _load()
    fm = m._normalize_refs({
        "field_mapped": [["concepts/supply-chain-compromise"]],   # bare [[x]] -> [["x"]]
        "see_also": [[["concepts/x"]], [["entities/s/y"]]],       # list of bare [[..]] items
    })
    assert fm["field_mapped"] == ["concepts/supply-chain-compromise"]
    assert fm["see_also"] == ["concepts/x", "entities/s/y"]


def test_normalize_refs_strips_wikilinks_in_flat_list():
    m = _load()
    fm = m._normalize_refs({"see_also": ["[[concepts/a]]", "[[entities/b]]"]})
    assert fm["see_also"] == ["concepts/a", "entities/b"]


def test_normalize_refs_leaves_plain_values_untouched():
    m = _load()
    fm = m._normalize_refs({"aliases": ["foo", "bar"], "sources": [], "name": "Acme"})
    assert fm["aliases"] == ["foo", "bar"]
    assert fm["sources"] == []
    assert fm["name"] == "Acme"


def test_coerce_fm_normalizes_bare_wikilinks_from_yaml_string():
    m = _load()
    # the REAL path: agent writes bare [[..]] in the YAML string -> safe_load mangles to nested
    # lists -> _coerce_fm must return canonical plain paths.
    yaml_text = ("type: lacuna\nfield_mapped: [[concepts/x]]\n"
                 "see_also:\n- [[concepts/a]]\n- [[entities/s/b]]\n")
    fm = m._coerce_fm(yaml_text)
    assert fm["field_mapped"] == ["concepts/x"]
    assert fm["see_also"] == ["concepts/a", "entities/s/b"]


# --- ISO-8601 timestamps for last_updated/created (OKF envelope; UI tracks WHEN, not just day) ---

def test_now_helper_timestamp_and_overrides(monkeypatch):
    m = _load()
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "2026-06-28T14:30:00Z")
    assert m._now() == "2026-06-28T14:30:00Z"
    monkeypatch.delenv("OKENGINE_MCP_WRITE_NOW", raising=False)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-28")     # date override still honored
    assert m._now() == "2026-06-28"
    monkeypatch.delenv("OKENGINE_MCP_WRITE_DATE", raising=False)
    real = m._now()                                                  # real UTC timestamp
    assert real.endswith("Z") and "T" in real and len(real) == 20


def test_create_stamps_iso_timestamp(vault, monkeypatch):
    m, root = vault
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "2026-06-28T14:30:00Z")
    res = m._create("entities/q/qilin", "type: entity\nname: Qilin", "body")
    assert res.startswith("created"), res
    import yaml
    fm = yaml.safe_load((root / "wiki" / "entities" / "q" / "qilin.md").read_text().split("---")[1])
    assert fm["last_updated"] == "2026-06-28T14:30:00Z"   # a timestamp, not just a date
    assert fm["created"] == "2026-06-28T14:30:00Z"


# --- future-date guard (record-keeping fields only) ---------------------------------------
# Live incident: a weekly-brief lane hallucinated `published: <next Sunday>` onto an empty
# stub (briefings/weekly-2026-07-12.md, written 2026-07-05) even though its prompt explicitly
# said "use TODAY's actual date, not a guessed or future one" — the prompt is the unenforced
# half. These pin the enforced half at the boundary every writer crosses.
# The vault fixture pins today = 2026-06-15.

def test_create_rejects_future_published(vault):
    m, root = vault
    res = m._create("entities/w/weekly-x",
                    "type: entity\nname: Weekly X\npublished: 2026-06-21\nupdated: 2026-06-21",
                    "body")
    assert res.startswith("rejected:") and "future" in res, res
    assert not (root / "wiki" / "entities" / "w" / "weekly-x.md").exists()


def test_create_rejects_future_yaml_bare_date(vault):
    """YAML parses a bare `published: 2026-07-12` into a datetime.date — the exact shape the
    incident wrote. The guard must handle the parsed-date type, not only strings."""
    m, root = vault
    res = m._create("entities/w/weekly-y",
                    {"type": "entity", "name": "Weekly Y",
                     "published": __import__("datetime").date(2026, 7, 12)},
                    "body")
    assert res.startswith("rejected:") and "future" in res, res


def test_create_allows_today_and_one_day_skew(vault):
    m, root = vault
    assert m._create("entities/t/today-page",
                     "type: entity\nname: T\npublished: 2026-06-15", "b").startswith("created")
    # +1 day = TZ-skew tolerance (UTC-thinking model past midnight UTC on a US-eastern host)
    assert m._create("entities/t/skew-page",
                     "type: entity\nname: S\npublished: 2026-06-16", "b").startswith("created")


def test_create_ignores_future_domain_dates(vault):
    """NARROW guard: domain dates are legitimately future (a KEV due_date, an event date,
    a contract end) — only the record-keeping envelope fields are checked."""
    m, root = vault
    res = m._create("entities/k/kev-cve",
                    "type: entity\nname: KEV\ndue_date: 2027-01-01\nevent_date: 2026-12-31",
                    "b")
    assert res.startswith("created"), res


def test_update_rejects_future_date_in_patch_only(vault):
    """The guard checks ONLY the fields the patch supplies — a legacy page already carrying a
    bad future date must stay fixable by an update that doesn't touch dates."""
    m, root = vault
    # a legacy page with a future `published`, written OUTSIDE the write path
    legacy = root / "wiki" / "entities" / "l" / "legacy.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ntype: entity\nname: Legacy\nid: entities:legacy\npublished: 2026-07-12\n---\nold\n",
                      encoding="utf-8")
    # patching a future date -> rejected, file untouched
    res = m._update("entities/l/legacy", "updated: 2026-06-30")
    assert res.startswith("rejected:") and "future" in res, res
    assert "old" in legacy.read_text()
    # an update NOT touching record dates goes through (and can fix the page)
    res = m._update("entities/l/legacy", "published: 2026-06-15", "fixed body")
    assert res.startswith("updated"), res
    assert "fixed body" in legacy.read_text()


# --- briefing wikilink guard --------------------------------------------------------------
# Live incident (okcti daily brief 2026-07-06): the lane invented slugs from memory —
# [[entities/q/quimarat]] for the real entities/q/quimat-rat page — shipping 4 dead links on
# the one page a human reads daily. The broken-wikilinks drain's >=3-inbound wake gate treats
# single-ref brief links as orphan noise, so the boundary must reject them with suggestions.

def _seed(root, rel, fm="type: entity\nname: X\n"):
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\nbody\n", encoding="utf-8")


def test_briefing_with_broken_link_rejected_with_suggestion(vault):
    m, root = vault
    _seed(root, "entities/q/quimat-rat.md")
    res = m._create("briefings/daily-2026-06-15",
                    "type: entity\nname: Daily\npublished: 2026-06-15",
                    "New RAT [[entities/q/quimarat]] spotted.")
    assert res.startswith("rejected:") and "quimat-rat" in res, res   # did-you-mean carries the real slug
    assert not (root / "wiki" / "briefings" / "daily-2026-06-15.md").exists()


def test_briefing_wrong_shard_suggests_exact_page(vault):
    """Right basename, wrong dir (the [[entities/p/oauth-...]] shape) -> exact suggestion."""
    m, root = vault
    _seed(root, "entities/s/storm-2372.md")
    res = m._create("briefings/daily-x", "type: entity\nname: D\n",
                    "Actor [[entities/p/storm-2372]] is active.")
    assert res.startswith("rejected:") and "[[entities/s/storm-2372]]" in res, res


def test_briefing_with_resolving_links_created(vault):
    m, root = vault
    _seed(root, "entities/q/quimat-rat.md")
    _seed(root, "entities/s/skillcloak.md")
    _seed(root, "sources/2026/06/rep.md", "type: source\npublisher: X\n"
          "source_kind: vendor-research\npublished: 2026-06-14\n")
    res = m._create("briefings/daily-good", "type: entity\nname: D\n",
                    "See [[entities/q/quimat-rat]] and bare [[skillcloak]] and "
                    "[[entities/s/skillcloak|labeled]].\nSource: [[sources/2026/06/rep]]")
    assert res.startswith("created"), res


def test_source_forward_reference_still_allowed(vault):
    """Scope: sources legitimately link entities that DON'T exist yet (the stub-creation
    drain depends on that declared-but-uncreated state) — the guard is briefings-only."""
    m, root = vault
    res = m._create("sources/2026/06/new-report",
                    "type: source\npublisher: X\nsource_kind: vendor-research\npublished: 2026-06-14",
                    "Updates [[entities/not-yet-created]] with new capability.")
    assert res.startswith("created"), res


def test_briefing_update_body_rechecked(vault):
    m, root = vault
    _seed(root, "entities/q/quimat-rat.md")
    _seed(root, "sources/2026/06/rep.md", "type: source\npublisher: X\n"
          "source_kind: vendor-research\npublished: 2026-06-14\n")
    assert m._create("briefings/daily-upd", "type: entity\nname: D\n",
                     "ok [[entities/q/quimat-rat]]\nSource: [[sources/2026/06/rep]]").startswith("created")
    # body rewrite introducing a broken link -> rejected, page untouched
    res = m._update("briefings/daily-upd", None,
                    "now broken [[entities/z/zzz-nope]]\nSource: [[sources/2026/06/rep]]")
    assert res.startswith("rejected:"), res
    assert "quimat-rat" in (root / "wiki" / "briefings" / "daily-upd.md").read_text()
    # fm-only update (no body) never walks the vault
    assert m._update("briefings/daily-upd", "tags: [x]").startswith("updated")


# --- briefing source-citation enforcement -------------------------------------------------
# Live (okcti weekly brief, 3x): analytically-sound briefs cited sources only as [^1] footnotes
# / code-span paths — zero clickable [[sources/...]] links — so an analyst couldn't reach any
# source. The grounded PROMPT didn't stop it; the write path must.

def test_briefing_with_entity_claims_needs_a_source(vault):
    m, root = vault
    _seed(root, "entities/i/inc-ransom.md")
    res = m._create("briefings/weekly-2026-06-15", "type: entity\nname: W\n",
                    "INC Ransom [[entities/i/inc-ransom]] deployed Lynx this week.[^1]")
    assert res.startswith("rejected:") and "no source" in res, res
    assert not (root / "wiki" / "briefings" / "weekly-2026-06-15.md").exists()


def test_briefing_with_a_source_link_passes(vault):
    m, root = vault
    _seed(root, "entities/i/inc-ransom.md")
    _seed(root, "sources/2026/06/report.md", "type: source\npublisher: X\n"
          "source_kind: vendor-research\npublished: 2026-06-14\n")
    res = m._create("briefings/weekly-good", "type: entity\nname: W\n",
                    "INC Ransom [[entities/i/inc-ransom]] deployed Lynx.\n"
                    "Source: [[sources/2026/06/report]]")
    assert res.startswith("created"), res


def test_empty_nothing_happened_briefing_is_exempt(vault):
    """A briefing with no entity claims (a one-line 'quiet week') needs no source."""
    m, root = vault
    res = m._create("briefings/daily-quiet", "type: entity\nname: Q\n",
                    "Quiet week — nothing of analyst significance landed.")
    assert res.startswith("created"), res


def test_briefing_linking_only_a_dashboard_still_needs_a_source(vault):
    """A nav link to a dashboard is not a claim's citation — if the brief makes an entity
    claim it still needs a source; a dashboard-only link doesn't satisfy it."""
    m, root = vault
    _seed(root, "entities/i/inc-ransom.md")
    _seed(root, "dashboards/top-actors.md", "type: dashboard\nname: Top\n")
    res = m._create("briefings/daily-dash", "type: entity\nname: D\n",
                    "See [[dashboards/top-actors]] — [[entities/i/inc-ransom]] active.")
    assert res.startswith("rejected:") and "no source" in res, res


# ── invariant-audit fixes (pre-release): three write-path-contract gaps ───────

def test_tombstone_marks_registry_so_converge_cannot_resurrect_in_process(vault):
    """invariant-audit HIGH: within ONE server process the id registry is cached; _tombstone wrote
    status:tombstoned to disk but never updated reg.tombstoned, so a same-process converge saw a
    stale set and RESURRECTED the tombstoned page. The tombstone must be registry-write-synchronous
    (like create/converge)."""
    m, root = vault
    assert m._converge("entities/acme.md", "type: entity\nname: Acme\nid: authority:acme\n").startswith("created")
    m._registry()                                  # registry now cached with the page ACTIVE
    assert "tombstoned" in m._tombstone("entities/acme.md", "merged away")
    out = m._converge("entities/acme.md", "type: entity\nname: Acme Reborn\nid: authority:acme\n")
    assert "tombstoned" in out and "resurrect" in out, out          # refused, not resurrected
    assert "status: tombstoned" in (root / "wiki" / "entities" / "a" / "acme.md").read_text()


def test_schema_reserved_files_refuses_all_write_tools(tmp_path, monkeypatch):
    """invariant-audit: `reserved_files` in schema.yaml is documented as a WRITE-PATH refusal and the
    validator exempts those files from conformance — but the write path ignored the key, so declaring
    a file reserved made it MORE writable. Every write tool must refuse a pack-reserved file."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA + "reserved_files: [SUMMARY.md]\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    m = _load()
    (tmp_path / "wiki" / "SUMMARY.md").write_text("---\ntype: source\n---\nbody\n", encoding="utf-8")
    for out in (
        m._update("SUMMARY.md", "type: source\nsource_kind: x\npublisher: y\npublished: 2026-06-01\n", "hacked"),
        m._append_section("SUMMARY.md", "H", "x"),
        m._patch("SUMMARY.md", "body", "hacked"),
    ):
        assert "refused" in out.lower() and "reserved" in out.lower(), out
    assert "hacked" not in (tmp_path / "wiki" / "SUMMARY.md").read_text()


def test_future_date_rejected_on_converge_patch_append(vault):
    """invariant-audit: the future-record-date guard ('the boundary every writer crosses') was only on
    create/update — converge, patch, and append skipped it, so a fabricated future published/updated
    persisted through those tools. All three must reject a future record-keeping date."""
    m, root = vault
    assert m._converge("entities/x.md", "type: entity\nname: X\nid: authority:x\n").startswith("created")
    # converge with a future `published` (survives _stamp, which only sets last_updated)
    c = m._converge("entities/x.md", "type: entity\nname: X\nid: authority:x\npublished: 2099-01-01\n")
    assert "future" in c.lower() and "reject" in c.lower(), c
    # patch injecting a future `published`
    pt = m._patch("entities/x.md", "name: X", "name: X\npublished: 2099-01-01")
    assert "future" in pt.lower() and "reject" in pt.lower(), pt
    # append to a page that already carries a future date (planted directly, bypassing the guard).
    # Plant at the sharded canonical so _append_section's normalized lookup finds it.
    yp = root / "wiki" / "entities" / "y" / "y.md"
    yp.parent.mkdir(parents=True, exist_ok=True)
    yp.write_text(
        "---\ntype: entity\nname: Y\npublished: 2099-01-01\n---\nbody\n", encoding="utf-8")
    ap = m._append_section("entities/y.md", "H", "note")
    assert "future" in ap.lower() and "reject" in ap.lower(), ap


def test_scalar_list_field_coerced_via_base_schema():
    """okengine#196 (generalized): the write path coerces a scalar string -> list for the fields
    base-schema DECLARES as `list` shape (aliases/tags/maintained_by/discovered_by) — driven off the
    schema, not a hardcoded set. Non-list scalars and correct lists are left untouched."""
    m = _load()
    out = m._coerce_fm({
        "name": "StealC",
        "aliases": "StealC, StealC info-stealer",   # scalar comma-string -> list
        "tags": "malware",                           # scalar single value -> single-element list
        "description": "an info-stealer",            # NON-list scalar field stays a string
    })
    assert out["aliases"] == ["StealC", "StealC info-stealer"]
    assert out["tags"] == ["malware"]
    assert out["description"] == "an info-stealer"
    assert m._coerce_fm({"aliases": ["A", "B"]})["aliases"] == ["A", "B"]     # correct list untouched
    assert m._coerce_fm({"aliases": " , "})["aliases"] == []                  # blank scalar -> []
    # the base list fields come from the schema declaration, not a literal in the code
    assert "aliases" in m._base_list_fields() and "tags" in m._base_list_fields()


def test_pack_declared_list_field_is_coerced(vault):
    """Proves it is SCHEMA-driven, not hardcoded: a PACK that declares its own `field_shapes` list
    field has a scalar for it coerced too. The `vault` fixture's schema.yaml is extended with a
    domain list field, and _coerce_fm is given the page path so the governing (composed) schema
    supplies the field set."""
    m, root = vault
    sy = (root / "schema.yaml").read_text(encoding="utf-8")
    (root / "schema.yaml").write_text(sy + "\nfield_shapes:\n  refs: list\n", encoding="utf-8")
    p = root / "wiki" / "entities" / "x.md"
    out = m._coerce_fm({"type": "entity", "name": "X", "refs": "a, b, c"}, p)
    assert out["refs"] == ["a", "b", "c"], "a pack-declared list field must be coerced from a scalar"


def test_coerce_fm_always_yields_valid_shapes_on_adversarial_input():
    """Property/fuzz: whatever shape an agent authors for a declared list field, _coerce_fm produces
    a dict where that field is a list (or absent) — never a bare string a consumer can crash on.
    Guards the whole okengine#196 class, not one field at a time."""
    m = _load()
    adversarial = [
        {"aliases": "one, two"},                      # scalar comma-string
        {"aliases": "solo"},                          # scalar single
        {"aliases": ["ok", "list"]},                  # already a list
        {"aliases": ""},                              # empty scalar
        {"tags": "a,b,,c"},                           # empties dropped
        {"name": "X", "aliases": "a", "tags": "b"},   # multiple list fields at once
    ]
    for fm in adversarial:
        out = m._coerce_fm(dict(fm))
        for f in ("aliases", "tags"):
            if f in out:
                assert isinstance(out[f], list), f"{f} must be a list for {fm!r}, got {out[f]!r}"
                assert all(isinstance(x, str) for x in out[f]), f"{f} elements must be strings"


# ── invariant-audit (v0.10.8): patch/append are full write chokepoints too ──────────────────

def test_patch_coerces_scalar_list_field(vault):
    """#6: patch_entity applies the #196 shape guard like create/update/converge — a list field
    edited to a scalar string is coerced to a list, so it can't poison id_index / entity_resolve
    (which iterate the value and do NOT defensively split a scalar)."""
    import yaml
    m, root = vault
    m._create("entities/s/stealc", "type: entity\nname: StealC\naliases: [StealC]", "body")
    fpath = root / "wiki" / "entities" / "s" / "stealc.md"
    old = "aliases:\n- StealC"                          # block form as _compose serialises the list
    assert old in fpath.read_text(), fpath.read_text()
    res = m._patch("entities/s/stealc", old, "aliases: StealC, StealV")
    assert res.startswith("patched"), res
    fm = yaml.safe_load(fpath.read_text().split("---")[1])
    assert fm["aliases"] == ["StealC", "StealV"]        # scalar coerced to a list, not left a string


def test_briefing_dead_link_guard_applies_to_append_and_patch(vault):
    """#7: append_to_section and patch_entity enforce the briefings dead-link guard that
    create/update enforce — the two body-mutating primitives used to grow a briefing incrementally
    cannot write an unresolvable [[wikilink]] into the flagship daily page."""
    m, root = vault
    m._create("sources/2026/s0",
              "type: source\nsource_kind: report\npublisher: X\npublished: 2026-06-01", "s")
    ok = m._create("briefings/2026-06-14", "type: briefing\ntitle: Daily\npublished: 2026-06-14",
                   "## Recent activity\n\n- context [[sources/2026/s0]]\n")
    assert ok.startswith("created"), ok
    bpath = root / "wiki" / "briefings" / "2026-06-14.md"
    before = bpath.read_text()
    res = m._append_section("briefings/2026-06-14", "Recent activity",
                            "- [[entities/q/ghost]] resurfaced")           # dead link
    assert res.startswith("rejected"), res
    assert bpath.read_text() == before                                     # file left untouched
    res2 = m._patch("briefings/2026-06-14", "context [[sources/2026/s0]]",
                    "context [[entities/q/ghost]]")                        # dead link via patch
    assert res2.startswith("rejected"), res2


def test_drift_check_does_not_flag_base_universals(drift_vault):
    """#14: the drift 'always allowed' set is _OKF_ALWAYS ∪ base-schema common_optional, so the
    check treats engine-stamped provenance (maintained_by/discovered_by) and base universals
    (confidence, …) as KNOWN scaffolding — not domain drift flagged on every update_entity."""
    m, root = drift_vault
    p = root / "wiki" / "entities" / "i" / "apt-x.md"
    fm = {"type": "intrusion-set", "name": "APT X", "maintained_by": ["mypack"],
          "discovered_by": "mypack", "confidence": 0.7, "suspected_origin": "China"}
    _out, flags = m._normalize_drift(fm, p)
    assert flags == [], f"base universals flagged as domain drift: {flags}"


def test_okf_always_covers_base_common_optional(drift_vault):
    """Binding contract: the drift-check 'always allowed' set ⊇ base-schema common_optional, so the
    two hand-maintained lists can't silently drift apart again (the maintained_by/discovered_by
    omission that flagged the engine's own provenance)."""
    import yaml
    m, _root = drift_vault
    base = yaml.safe_load((REPO / "config" / "base-schema.yaml").read_text(encoding="utf-8"))
    missing = set(base.get("common_optional") or []) - m._okf_always()
    assert not missing, f"_okf_always() omits base common_optional field(s): {sorted(missing)}"


# ── invariant-audit v0.11.5 batch-2 (write-path contract) ─────────────────────────────────────

def test_flat_entity_path_normalizes_to_shard(vault):
    """M17: the flat `entities/<slug>` (2-part) form is the MOST COMMON wrong shape — it fell
    through _normalize_entity_shard untouched and created an unsharded duplicate canonical."""
    m, root = vault
    assert m._normalize_entity_shard("entities/qilin") == "entities/q/qilin.md"
    assert m._normalize_entity_shard("entities/qilin.md") == "entities/q/qilin.md"
    # a create at the flat path lands at the sharded canonical, not entities/qilin.md
    m._create("entities/qilin", "type: entity\nname: Qilin", "body")
    assert (root / "wiki" / "entities" / "q" / "qilin.md").is_file()
    assert not (root / "wiki" / "entities" / "qilin.md").exists()


def test_create_routes_flat_concept_through_schema_partition(vault):
    """#262: the enforced writer must not mint a flat concept beside its
    by-letter canonical, even when the caller supplies the logical flat key."""
    m, root = vault
    schema = _SCHEMA.replace(
        "  entity:\n    required: [type, name]\n",
        "  entity:\n    required: [type, name]\n  concept:\n    required: [type, name]\n",
    ) + """partitioning:
  namespaces:
    concepts:
      strategy: by-letter
"""
    (root / "schema.yaml").write_text(schema, encoding="utf-8")

    out = m._create("concepts/quantum-risk", {"type": "concept", "name": "Quantum risk"}, "body")

    assert out.startswith("created"), out
    assert (root / "wiki" / "concepts" / "q" / "quantum-risk.md").is_file()
    assert not (root / "wiki" / "concepts" / "quantum-risk.md").exists()


def test_create_routes_year_flat_source_through_date_partition(vault):
    """#262: a caller-supplied year directory cannot bypass the month shard
    derived from the source's governed ``published`` field."""
    m, root = vault
    (root / "schema.yaml").write_text(_SCHEMA + """partitioning:
  namespaces:
    sources:
      strategy: by-date
      date_field: published
""", encoding="utf-8")
    fm = {"type": "source", "source_kind": "article", "publisher": "Example",
          "published": "2026-06-14"}

    out = m._create("sources/2026/example-report", fm, "body")

    assert out.startswith("created"), out
    assert (root / "wiki" / "sources" / "2026" / "06" / "example-report.md").is_file()
    assert not (root / "wiki" / "sources" / "2026" / "example-report.md").exists()


def test_create_refuses_when_partitioned_canonical_already_exists(vault):
    """A wrong physical spelling resolves to the canonical owner before the
    existence gate; it never creates a second page."""
    m, root = vault
    schema = _SCHEMA.replace(
        "  entity:\n    required: [type, name]\n",
        "  entity:\n    required: [type, name]\n  concept:\n    required: [type, name]\n",
    ) + """partitioning:
  namespaces:
    concepts:
      strategy: by-letter
"""
    (root / "schema.yaml").write_text(schema, encoding="utf-8")
    canonical = root / "wiki" / "concepts" / "q" / "quantum-risk.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("---\ntype: concept\nname: Quantum risk\n---\nbody\n")

    out = m._create("concepts/quantum-risk", {"type": "concept", "name": "Quantum risk"}, "new")

    assert out.startswith("refused: concepts/q/quantum-risk.md already exists"), out
    assert not (root / "wiki" / "concepts" / "quantum-risk.md").exists()


def test_unknown_permission_key_fails_closed(vault):
    """L58: a typo'd namespace permission key (`creat: false`) was silently dropped and the
    namespace defaulted OPEN — a human-authored ns could go agent-writable with no error."""
    m, root = vault
    import pytest
    (root / "schema.yaml").write_text(_SCHEMA.replace(
        "    findings:\n      create: false\n      update: false",
        "    findings:\n      creat: false\n      update: false"), encoding="utf-8")
    with pytest.raises(ValueError):
        m._ns_perm(m.governing_policy(str(root / "wiki" / "findings" / "x.md")), "findings")


def test_tombstone_refuses_malformed_frontmatter_instead_of_wiping(vault):
    """M18: a page with malformed YAML frontmatter parsed to {} and the tombstone/append silently
    WIPED it. Now refused, page untouched."""
    m, root = vault
    p = root / "wiki" / "entities" / "a" / "acme.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: Acme\n\tbad: tab\n---\nbody\n", encoding="utf-8")  # tab -> invalid YAML
    before = p.read_text()
    out = m._tombstone("entities/a/acme", "dup")
    assert out.startswith("refused") and "frontmatter" in out
    assert p.read_text() == before                       # untouched
    out2 = m._append_section("entities/a/acme", "Notes", "x")
    assert out2.startswith("refused")
    assert p.read_text() == before


def test_converge_refuses_on_disk_tombstone(vault):
    """M14: a page hand-tombstoned on disk since the last (6h) id-index rebuild slipped past the
    registry check and got resurrected by converge. The on-disk status is now trusted."""
    m, root = vault
    m._create("entities/a/akira", "type: entity\nname: Akira\nid: akira", "body")
    p = root / "wiki" / "entities" / "a" / "akira.md"
    txt = p.read_text().replace("name: Akira", "name: Akira\nstatus: tombstoned")
    p.write_text(txt, encoding="utf-8")
    out = m._converge("entities/a/akira", "type: entity\nname: Akira\nid: akira", "resurrected")
    assert "tombstoned" in out and out.startswith("refused")
    assert "resurrected" not in p.read_text()


def test_patch_converges_drift_and_flags_degeneration(drift_vault):
    """M15: patch_entity bypassed drift normalization AND the review flags. A surgical edit that
    introduces an aliased field (country->suspected_origin) / value (CN->China) landed RAW — the
    identical change via update converged — and a degenerate body arrived with no needs_review."""
    import yaml
    m, root = drift_vault
    m._create("entities/a/apt-z", "type: intrusion-set\nname: APT-Z\nmotivation: espionage", "seed")
    p = root / "wiki" / "entities" / "a" / "apt-z.md"
    out = m._patch("entities/a/apt-z", "name: APT-Z", "name: APT-Z\ncountry: CN")
    assert out.startswith("patched"), out
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm.get("suspected_origin") == "China" and "country" not in fm   # converged, not raw
    # a degenerate body introduced by patch is now flagged for review (was silent before)
    salad = " ".join(f"z{i}" for i in range(400))
    out2 = m._patch("entities/a/apt-z", "seed", salad + ".")
    assert out2.startswith("patched"), out2
    assert "needs_review: true" in p.read_text()


def test_append_flags_degenerate_content(vault):
    """M15: append_to_section is the documented hot path for a briefing's `## Recent activity`,
    yet it skipped the degeneration flag entirely — a word-salad append landed unattributed."""
    m, root = vault
    m._create("entities/b/brief", "type: entity\nname: Brief", "# Brief\n\nClean prose. It ends.\n")
    p = root / "wiki" / "entities" / "b" / "brief.md"
    salad = " ".join(f"term{i}" for i in range(400))
    out = m._append_section("entities/b/brief", "Recent activity", salad + ".")
    assert out.startswith("appended"), out
    assert "needs_review: true" in p.read_text()


def test_extension_id_is_not_client_forgeable(vault):
    """M14: extension_id is SERVER-DERIVED provenance that `extensions purge --yes` HARD-deletes by —
    the one non-tombstone delete. It was stamped only on create-by-extension, so update/patch/converge
    (and admin create) let ANY caller set or reassign it: forge another extension's id onto a curated
    page and purge unlink()s a page it never wrote. Every write must strip a supplied value and keep
    the immutable create-time stamp."""
    import yaml
    m, root = vault

    def as_ext(ext_id):
        m._caller_var.set({"kind": "extension", "ext_id": ext_id, "write_scopes": ["entities/**"]})

    def fm_of(rel):
        return yaml.safe_load((root / "wiki" / rel).read_text().split("---")[1])

    try:
        # create by ext-a stamps ext-a; a client-supplied extension_id in the same call is ignored
        as_ext("ext-a")
        m._create("entities/x/xr", "type: entity\nname: Xr\nextension_id: ext-evil", "b")
        assert fm_of("entities/x/xr.md")["extension_id"] == "ext-a"
        # a DIFFERENT extension updating cannot reassign it (forge attempt via frontmatter)
        as_ext("ext-b")
        m._update("entities/x/xr", "extension_id: ext-b")
        assert fm_of("entities/x/xr.md")["extension_id"] == "ext-a"    # immutable, preserved
        # patch cannot reassign it either
        m._patch("entities/x/xr", "name: Xr", "name: Xr\nextension_id: ext-c")
        assert fm_of("entities/x/xr.md")["extension_id"] == "ext-a"
        # converge cannot reassign it
        m._converge("entities/x/xr", "type: entity\nname: Xr\nid: authority:xr\nextension_id: ext-d", "b2")
        assert fm_of("entities/x/xr.md")["extension_id"] == "ext-a"
    finally:
        m._caller_var.set(None)

    # an ADMIN (stdio) create supplying extension_id gets no stamp — it is not forgeable into existence
    m._create("entities/y/yr", "type: entity\nname: Yr\nextension_id: ext-x", "b")
    assert "extension_id" not in fm_of("entities/y/yr.md")


def test_update_entity_wrapper_passes_body_and_frontmatter_through(vault):
    """M22: the @mcp.tool() wrappers carry real falsy-mapping logic (update_entity maps
    frontmatter_yaml='' -> None and passes body='' -> clear / None -> keep), but the okengine#52
    regression pins m._update, not the wrapper — so a 'normalize falsy' cleanup to the wrapper (body
    or None) could reintroduce #52 with the suite green. Exercise the wrapper itself."""
    m, root = vault
    if m.mcp is None:
        pytest.skip("mcp not installed — wrapper not defined")
    m.create_entity("entities/f/foo", "type: entity\nname: Foo", "original body text")
    p = root / "wiki" / "entities" / "f" / "foo.md"
    m.update_entity("entities/f/foo")                        # body omitted (None) -> keep
    assert "original body text" in p.read_text()
    m.update_entity("entities/f/foo", frontmatter_yaml="")   # "" -> None -> keep fm, no reject
    assert "original body text" in p.read_text()
    out = m.update_entity("entities/f/foo", body="")         # "" -> CLEAR body (the #52 contract)
    assert out.startswith("updated"), out
    assert "original body text" not in p.read_text()
    assert p.read_text().split("---", 2)[2].strip() == ""


# ── H3: identity + provenance immutability on update / patch (invariant-audit) ─────────────────

def _read_fm(root: Path, rel: str) -> dict:
    import yaml, re as _re
    text = (root / "wiki" / (rel + ".md")).read_text(encoding="utf-8")
    m = _re.match(r"\A---\n(.*?\n)---", text, _re.S)
    return yaml.safe_load(m.group(1)) if m else {}


def test_update_reverts_forged_id_and_provenance(vault):
    m, root = vault
    rel = "entities/a/acme"
    m._create(rel, {"type": "entity", "name": "Acme",
                    "id": "entities:acme", "created": "2026-01-01",
                    "created_by": "cron:import", "discovered_by": "feed:rss"},
              "# Acme\n\nBody.\n")
    before = _read_fm(root, rel)
    # a caller tries to rewrite id + every provenance field
    res = m._update(rel, {"id": "entities:HIJACKED", "created": "2000-01-01",
                          "created_by": "attacker", "discovered_by": "attacker",
                          "note": "legit field change"}, None)
    assert not res.startswith(("refused", "rejected")), res
    after = _read_fm(root, rel)
    for k in ("id", "created", "created_by", "discovered_by"):
        assert after[k] == before[k], f"{k} was mutated: {before.get(k)!r} -> {after.get(k)!r}"
    assert after.get("note") == "legit field change", "a normal field update must still apply"
    assert after.get("needs_review") is True, "an attempted immutable-field change must be flagged"


def test_update_may_backfill_a_missing_id_but_not_change_an_existing_one(vault):
    m, root = vault
    # a legacy page WITHOUT an id (written directly, bypassing _create's stamping)
    (root / "wiki" / "entities" / "n").mkdir(parents=True)
    (root / "wiki" / "entities" / "n" / "noid.md").write_text(
        "---\ntype: entity\nname: NoId\nversion: 1\n---\n\n# NoId\n\nBody.\n", encoding="utf-8")
    # backfilling an id onto an id-less page is legitimate (bringing it into compliance) — allowed
    res = m._update("entities/n/noid", {"id": "entities:noid-backfilled", "note": "x"}, None)
    assert not res.startswith(("refused", "rejected")), res
    assert _read_fm(root, "entities/n/noid")["id"] == "entities:noid-backfilled"
    # but once it HAS an id, a later update cannot change it
    res2 = m._update("entities/n/noid", {"id": "entities:HIJACKED"}, None)
    assert not res2.startswith(("refused", "rejected")), res2
    after = _read_fm(root, "entities/n/noid")
    assert after["id"] == "entities:noid-backfilled", "an existing id must be immutable"
    assert after.get("needs_review") is True


def test_patch_reverts_forged_provenance(vault):
    m, root = vault
    rel = "entities/b/beta"
    m._create(rel, {"type": "entity", "name": "Beta", "created": "2026-02-02",
                    "discovered_by": "feed:x"}, "# Beta\n\nBody.\n")
    before = _read_fm(root, rel)
    # a surgical patch that rewrites the created date in the frontmatter (dates serialize quoted)
    res = m._patch(rel, "created: '2026-02-02'", "created: '1999-09-09'")
    assert not res.startswith(("refused", "rejected")), res
    after = _read_fm(root, rel)
    assert str(after["created"]) == str(before["created"]), "patch must not rewrite the immutable created date"
    assert after.get("needs_review") is True


def test_update_normal_change_is_not_flagged_as_immutable(vault):
    m, root = vault
    rel = "entities/c/clean"
    m._create(rel, {"type": "entity", "name": "Clean", "created": "2026-03-03"},
              "# Clean\n\nBody.\n")
    # echo the same immutable values (read-modify-write) + change a normal field: no revert, no flag
    res = m._update(rel, {"created": "2026-03-03", "summary": "updated"}, None)
    assert not res.startswith(("refused", "rejected")), res
    after = _read_fm(root, rel)
    assert after.get("summary") == "updated"
    # echoing the unchanged immutable value must NOT trip the immutable-change flag
    # (needs_review may be set by other governance, but not for a reverted immutable field)
    assert after["created"] == "2026-03-03"


# ── fabricated-source guard (okengine#348): a CITED source must exist ─────────

def _mk_source(root, rel):
    p = root / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\nsource_kind: report\npublisher: X\npublished: '2026-06-01'\n---\n\nreal.\n",
                 encoding="utf-8")


def test_create_entity_with_fabricated_source_is_rejected(vault):
    m, root = vault
    res = m._create("entities/a/p/apt35",
                    {"type": "entity", "name": "APT35", "sources": ["source/mandiant/q1-apt-report"]},
                    "# APT35\n\nx.\n")
    assert res.startswith("rejected:") and "singular" in res, res
    assert not (root / "wiki" / "entities" / "a" / "p" / "apt35.md").exists(), "rejected write must not land"


def test_create_entity_with_real_source_and_provenance_label_allowed(vault):
    m, root = vault
    _mk_source(root, "sources/2026/06/real-report")
    res = m._create("entities/a/real-actor",
                    {"type": "entity", "name": "Real", "sources": ["sources/2026/06/real-report", "MITRE ATT&CK"]},
                    "# Real\n\nx.\n")
    assert not res.startswith("rejected:"), res     # resolvable page-ref + provenance label both fine


def test_update_grandfathers_legacy_bad_ref_but_blocks_new_one(vault):
    m, root = vault
    pg = root / "wiki" / "entities" / "l" / "legacy.md"      # canonical first-letter shard for 'legacy'
    pg.parent.mkdir(parents=True, exist_ok=True)
    pg.write_text("---\ntype: entity\nname: Legacy\nid: entities:legacy\nsources:\n- source/old/fake\n---\n\nx.\n",
                  encoding="utf-8")
    # resending the legacy fabricated ref while touching another field is grandfathered (not rejected)
    ok = m._update("entities/l/legacy",
                   {"sources": ["source/old/fake"], "confidence": "medium"})
    assert not ok.startswith("rejected:"), ok
    # but ADDING a new fabricated ref is rejected
    bad = m._update("entities/l/legacy",
                    {"sources": ["source/old/fake", "source/new/alsofake"]})
    assert bad.startswith("rejected:") and "alsofake" in bad, bad
