"""framework install-domain (okengine#173) — both co-install shapes, automated.

Pins: shape detection, the walk-up subtree install (schema copy + ns dirs + rules
scoped to the SUBTREE's types + persona marker), the taxonomy install (host-wins
type merge, xmlUrl-deduped feeds, prefix-required cron append, host-wins prompt
keys), dry-run writes nothing, and full idempotency (second --apply = no steps).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "framework_install_domain.py"


def _load():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("framework_install_domain", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_install_domain"] = m
    spec.loader.exec_module(m)
    return m


mod = _load()


def _host(tmp_path) -> Path:
    h = tmp_path / "host"
    (h / "wiki").mkdir(parents=True)
    (h / "config").mkdir()
    (h / "crons").mkdir()
    (h / "feeds").mkdir()
    (h / "schema.yaml").write_text(yaml.safe_dump({
        "name": "okpack-host",
        "types": {"entity": {"required": ["type", "id"]},
                  "vendor": {"required": ["type", "id", "name"]}},
    }) + "cockpit:\n  tabs: [overview, browse]\n  tab_defs:\n"
        "    overview: {label: Overview, boxes: []}\n")
    (h / "pack.yaml").write_text("name: okpack-host\nowns:\n  types: [vendor]\n")
    (h / "config" / "completeness-rules.yaml").write_text(yaml.safe_dump(
        {"rules": [{"id": "host-rule", "when": {"type": "vendor"}, "expect": "field"}]}))
    (h / "crons" / "domain-crons.json").write_text(json.dumps(
        [{"id": "aa", "name": "okpack-host-feed-fetch"}]))
    (h / "crons" / "engine-template-prompts.json").write_text(json.dumps(
        {"daily-brief": "HOST BRIEF PROMPT"}))
    (h / "feeds" / "feeds.opml").write_text(
        '<?xml version="1.0"?><opml><body>'
        '<outline text="A" xmlUrl="https://a.example/rss"/></body></opml>')
    (h / "wiki" / "journal").mkdir()          # collides with the subtree pack's STANDALONE ns
    (h / "CLAUDE.md").write_text("# host persona\n")
    return h


def _subtree_pack(tmp_path) -> Path:
    p = tmp_path / "okpack-doct"
    (p / "subdomain").mkdir(parents=True)
    (p / "config").mkdir()
    (p / "pack.yaml").write_text("name: okpack-doct\n")
    # standalone partitioning deliberately declares a namespace that ALSO exists at
    # the host root ('journal') — in the subtree shape those namespaces nest under
    # wiki/<slug>/ and must NOT trip the root-collision check (real-install regression)
    (p / "schema.yaml").write_text(yaml.safe_dump({
        "name": "okpack-doct",
        "types": {"assumption": {"required": ["type", "id", "statement"]},
                  "vendor": {"required": ["type", "id"]}},
        "partitioning": {"namespaces": {"journal": {"strategy": "flat"},
                                        "assumptions": {"strategy": "flat"}}},
    }))
    (p / "subdomain" / "schema.yaml").write_text(yaml.safe_dump({
        "types": {"assumption": {"required": ["type", "id", "statement"]}},
        "partitioning": {"namespaces": ["assumptions", "decisions"]},
    }))
    (p / "subdomain" / "PERSONA.md").write_text(
        "## Installed domain: doct (`wiki/doct/` — okpack-doct sub-domain)\n\nrules...\n")
    (p / "config" / "completeness-rules.yaml").write_text(yaml.safe_dump({"rules": [
        {"id": "assumption-needs-statement", "when": {"type": "assumption"},
         "expect": "field", "field": "statement"},
        {"id": "vendor-needs-risk", "when": {"type": "vendor"}, "expect": "link"},
    ]}))
    return p


def _taxonomy_pack(tmp_path) -> Path:
    p = tmp_path / "okpack-tax"
    (p / "subdomain").mkdir(parents=True)
    (p / "crons").mkdir()
    (p / "feeds").mkdir()
    (p / "schema.yaml").write_text(yaml.safe_dump({
        "name": "okpack-tax",
        "types": {"intrusion-set": {"required": ["type", "id"]},
                  "vendor": {"required": ["type", "id", "name"]}},
        "partitioning": {"namespaces": {"tax-events": {"strategy": "by-date",
                                                       "date_field": "date"}}},
        "permissions": {"namespaces": {"tax-register": {"create": False, "update": False}}},
    }))
    (p / "pack.yaml").write_text(
        "name: okpack-tax\nowns:\n  types: [intrusion-set]\n"
        "  namespaces: [tax-events, tax-register]\n")
    # vendor matches the host contract exactly -> preflight WARN (host wins), not FAIL
    (p / "subdomain" / "host-schema-additions.yaml").write_text(yaml.safe_dump({
        "types": {"intrusion-set": {"required": ["type", "id"]},
                  "vendor": {"required": ["type", "id", "name"]}},
        # the guest contributes field-coverage rows so a composed vault tracks them (okengine#264):
        # one with a min floor, one without — both must travel through install-domain.
        "coverage_fields": [{"type": "intrusion-set", "field": "score", "min": 0.8},
                            {"type": "vendor", "field": "name"}],
        # the guest contributes its field vocabularies so corpus_audit's enum-drift metric can see
        # them in the composed vault (okengine#259 rec 12) — one plain enum, one by_type-scoped.
        "enums": {"severity": ["critical", "high", "medium", "low"],
                  "tax_kind": ["alpha", "beta"]},
        "field_enums": {"severity": {"enum": "severity", "extensible": True},
                        "tax_kind": {"by_type": {"intrusion-set": "tax_kind"}}},
        # the guest contributes its cockpit tab so a composed vault surfaces it (okengine#<n>)
        "cockpit": {"tabs": ["taxtab"], "tab_defs": {
            "taxtab": {"label": "Tax", "boxes": [
                {"title": "Events", "view": "table",
                 "dataset": {"dir": "tax-events", "type": "intrusion-set"},
                 "empty": "No tax events yet."}]}}},
    }))
    (p / "subdomain" / "PERSONA.md").write_text("curation rules for tax\n")
    (p / "crons" / "domain-crons.json").write_text(json.dumps([
        {"id": "bb", "name": "okpack-tax-feed-fetch", "script": "okpack_tax_feed_fetch.py"},
        {"id": "cc", "name": "unprefixed-job"},
    ]))
    (p / "crons" / "scripts").mkdir()
    (p / "crons" / "scripts" / "okpack_tax_feed_fetch.py").write_text("# tax lane\n")
    (p / "crons" / "engine-template-prompts.json").write_text(json.dumps(
        {"daily-brief": "TAX PROMPT", "trends-refresh": "TAX TRENDS"}))
    (p / "feeds" / "feeds.opml").write_text(
        '<?xml version="1.0"?><opml><body>'
        '<outline text="A dup" xmlUrl="https://a.example/rss"/>'
        '<outline text="B" xmlUrl="https://b.example/rss"/></body></opml>')
    return p


# ── shape / slug ─────────────────────────────────────────────────────────────
def test_detect_shape_and_slug(tmp_path):
    st, tx = _subtree_pack(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.detect_shape(st, None) == "subtree"
    assert mod.detect_shape(tx, None) == "taxonomy"
    assert mod.domain_slug(st, None) == "doct"          # okpack- prefix stripped
    assert mod.domain_slug(st, "wiki/other/") == "other"


# ── subtree shape ────────────────────────────────────────────────────────────
def test_subtree_dry_run_writes_nothing(tmp_path):
    h, p = _host(tmp_path), _subtree_pack(tmp_path)
    assert mod.main([str(h), str(p)]) == 0
    assert not (h / "wiki" / "doct").exists()
    assert "doct" not in (h / "CLAUDE.md").read_text()


def test_subtree_apply(tmp_path):
    h, p = _host(tmp_path), _subtree_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    assert (h / "wiki" / "doct" / "schema.yaml").is_file()
    assert (h / "wiki" / "doct" / "assumptions").is_dir()
    assert (h / "wiki" / "doct" / "decisions").is_dir()
    rules = yaml.safe_load((h / "config" / "completeness-rules.yaml").read_text())["rules"]
    ids = {r["id"] for r in rules}
    assert "assumption-needs-statement" in ids     # subtree-type rule merged
    assert "vendor-needs-risk" not in ids          # host-entity-world rule NOT merged
    assert "host-rule" in ids                      # host rules untouched
    assert "## Installed domain: doct" in (h / "CLAUDE.md").read_text()


def test_subtree_idempotent(tmp_path):
    h, p = _host(tmp_path), _subtree_pack(tmp_path)
    mod.main([str(h), str(p), "--apply"])
    before = (h / "CLAUDE.md").read_text()
    plan = mod.Plan(apply=True)
    mod.install_subtree(h, p, "doct", plan)
    assert plan.steps == [], [s for s, _ in plan.steps]
    assert (h / "CLAUDE.md").read_text() == before


# ── taxonomy shape ───────────────────────────────────────────────────────────
def test_taxonomy_apply(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    schema = yaml.safe_load((h / "schema.yaml").read_text())
    assert "intrusion-set" in schema["types"]                       # added
    assert schema["types"]["vendor"]["required"] == ["type", "id", "name"]  # host wins
    feeds = (h / "feeds" / "feeds.opml").read_text()
    assert feeds.count("a.example/rss") == 1                        # deduped
    assert "b.example/rss" in feeds                                 # merged
    jobs = json.loads((h / "crons" / "domain-crons.json").read_text())
    names = [j["name"] for j in jobs]
    assert "okpack-tax-feed-fetch" in names
    assert "unprefixed-job" not in names                            # prefix required
    # engine-template prompts NEVER auto-merge (shared lanes are the host's decision
    # — a merged prompt would silently ACTIVATE a stub the host left promptless)
    prompts = json.loads((h / "crons" / "engine-template-prompts.json").read_text())
    assert prompts == {"daily-brief": "HOST BRIEF PROMPT"}, prompts
    # owned namespaces land with partitioning + permissions, and the wiki dirs exist
    schema2 = yaml.safe_load((h / "schema.yaml").read_text())
    assert schema2["partitioning"]["namespaces"]["tax-events"]["strategy"] == "by-date"
    assert schema2["permissions"]["namespaces"]["tax-register"]["create"] is False
    assert (h / "wiki" / "tax-events").is_dir()
    # the merged job's lane script is copied into the HOST's staging source — a job
    # whose script never stages fails at deploy (first real install caught this)
    assert (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").is_file()
    assert "okpack-tax" in (h / "CLAUDE.md").read_text()


def test_taxonomy_composed_schema_failure_rolls_back_atomically(tmp_path, monkeypatch):  # okengine#326 [11]
    """If composed-schema regeneration fails AFTER the schema.yaml merge, the whole install must roll
    back — leaving a half-merged schema.yaml (and telling the operator to wait for 'the next deploy')
    was a permanent structural break with no recovery. The transaction snapshot restores pre-install
    state."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    before = (h / "schema.yaml").read_text()
    real_load = mod._load_mod

    def _fake_load(filename):
        m2 = real_load(filename)
        if filename == "extension_compose.py":            # force the post-merge regen to fail
            m2.write_composed_schema = lambda host: ["forced composed-schema conflict"]
        return m2

    monkeypatch.setattr(mod, "_load_mod", _fake_load)
    rc = mod.main([str(h), str(p), "--apply"])
    assert rc == 1
    # atomic: schema.yaml is byte-for-byte restored — no 'intrusion-set' half-merge left behind
    assert (h / "schema.yaml").read_text() == before
    assert "intrusion-set" not in (h / "schema.yaml").read_text()


def test_taxonomy_merges_guest_coverage_fields(tmp_path):
    """okengine#264: a guest's `coverage_fields` (field-population ratios corpus_audit tracks) must
    fold into the composed host schema — else a bundle member's coverage declaration (okpack-vuln:
    cve.cvss_base) is silently dropped and the composed vault shows UNDETECTABLE despite owning the
    enriched type. Both the min-floored and floor-less rows must travel; the merge is idempotent."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    cov = yaml.safe_load((h / "schema.yaml").read_text()).get("coverage_fields") or []
    pairs = {(c["type"], c["field"]): c.get("min") for c in cov if isinstance(c, dict)}
    assert pairs.get(("intrusion-set", "score")) == 0.8       # min floor carried through
    assert ("vendor", "name") in pairs and pairs[("vendor", "name")] is None  # floor-less carried
    # idempotent: a second apply must not duplicate the rows
    mod.main([str(h), str(p), "--apply"])
    cov2 = yaml.safe_load((h / "schema.yaml").read_text()).get("coverage_fields") or []
    assert len([c for c in cov2 if isinstance(c, dict) and c.get("field") == "score"]) == 1


def test_taxonomy_merges_guest_enums(tmp_path):
    """okengine#259 rec 12: a guest's `enums` + `field_enums` (its field vocabularies) must fold into
    the composed host schema — else corpus_audit's enum-drift metric reports UNDETECTABLE for the
    guest's fields in the bundle deploy though the A-class vocab drift is real. Both a plain `enum:`
    rule and a `by_type:`-scoped rule travel; a host key WINS; the merge is idempotent."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    # host already owns `severity` with a NARROWER vocabulary — host must win, guest is skipped.
    hs = (h / "schema.yaml").read_text() + \
        "enums:\n  severity: [crit, hi]\nfield_enums:\n  severity: {enum: severity}\n"
    (h / "schema.yaml").write_text(hs)

    assert mod.main([str(h), str(p), "--apply"]) == 0
    schema = yaml.safe_load((h / "schema.yaml").read_text())
    # host-owned `severity` preserved (guest's 4-value list did NOT overwrite it)
    assert schema["enums"]["severity"] == ["crit", "hi"]
    # guest-unique vocabulary + rules travelled
    assert schema["enums"]["tax_kind"] == ["alpha", "beta"]
    assert schema["field_enums"]["tax_kind"] == {"by_type": {"intrusion-set": "tax_kind"}}
    # idempotent: a second apply doesn't duplicate the key or corrupt the block
    assert mod.main([str(h), str(p), "--apply"]) == 0
    schema2 = yaml.safe_load((h / "schema.yaml").read_text())
    assert schema2["enums"]["tax_kind"] == ["alpha", "beta"]
    assert schema2["field_enums"]["severity"] == {"enum": "severity"}   # still the host's rule


def test_taxonomy_extends_inline_by_type_and_carries_field_item_contracts(tmp_path):
    """A co-installed type must not lose status enum dispatch or nested-list validation merely
    because the host already owns the common `status` field and declares it inline."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    host = (h / "schema.yaml").read_text() + (
        "enums:\n  host_status: [active]\n"
        "field_enums:\n  status: {by_type: {vendor: host_status}}\n"
        "field_shapes:\n  aliases: list\n"
        "field_items:\n  aliases: {_item: {shape: str}}\n"
        "protected_fields: [aliases]\n"
        "depth_critical_types: [vendor]\n"
    )
    (h / "schema.yaml").write_text(host)
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["enums"]["tax_status"] = ["candidate", "accepted"]
    additions["field_enums"]["status"] = {"by_type": {"intrusion-set": "tax_status"}}
    additions["field_shapes"] = {"signals": "list", "aliases": "list"}
    additions["field_items"] = {"signals": {"_item": {"shape": "dict", "required": ["id"]}}}
    additions["protected_fields"] = ["signals", "aliases"]
    additions["depth_critical_types"] = ["intrusion-set"]
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))

    assert mod.main([str(h), str(p), "--apply"]) == 0
    schema = yaml.safe_load((h / "schema.yaml").read_text())
    self_status = schema["field_enums"]["status"]["by_type"]
    assert self_status == {"vendor": "host_status", "intrusion-set": "tax_status"}
    assert schema["field_shapes"]["aliases"] == "list"       # identical host value wins
    assert schema["field_shapes"]["signals"] == "list"
    assert schema["field_items"]["signals"]["_item"]["required"] == ["id"]
    assert schema["protected_fields"] == ["aliases", "signals"]
    assert schema["depth_critical_types"] == ["vendor", "intrusion-set"]
    assert mod.main([str(h), str(p), "--apply"]) == 0          # idempotent


def test_taxonomy_does_not_redeclare_extension_owned_field_contract(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["field_shapes"] = {"alternatives": "list", "signals": "list"}
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))
    composed = yaml.safe_load((h / "schema.yaml").read_text())
    composed["field_shapes"] = {"alternatives": "list"}
    (h / ".okengine").mkdir()
    (h / ".okengine" / "composed-schema.yaml").write_text(
        yaml.safe_dump(composed, sort_keys=False))

    assert mod.main([str(h), str(p), "--apply"]) == 0
    root = yaml.safe_load((h / "schema.yaml").read_text())
    assert root["field_shapes"] == {"signals": "list"}


def test_taxonomy_merges_guest_cockpit_tab(tmp_path):
    """A guest's cockpit tab_def + tab name fold into the host's cockpit block, so a composed vault
    surfaces the guest domain's tab (the canonical home a hand-added tab lacked). Host wins on a tab
    it already declares; the guest tab is added BEFORE `browse` so browse stays last."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    ck = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]
    assert "taxtab" in ck["tab_defs"]                                  # tab_def merged
    assert ck["tab_defs"]["taxtab"]["boxes"][0]["title"] == "Events"   # ...with its boxes intact
    assert "overview" in ck["tab_defs"]                                # host's own tab preserved
    assert "taxtab" in ck["tabs"] and ck["tabs"].index("taxtab") < ck["tabs"].index("browse")
    # idempotent: a second apply doesn't duplicate the tab
    assert mod.main([str(h), str(p), "--apply"]) == 0
    ck2 = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]
    assert ck2["tabs"].count("taxtab") == 1


def test_merged_guest_tab_is_consumed_by_cockpit_api(tmp_path):
    """Bind install-domain's producer to the cockpit consumer: the merged schema must make the
    guest key a real /api/tab/<key> response, not merely leave parseable but unused YAML."""
    pytest.importorskip("fastapi")
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0

    app_path = REPO / "okengine-cockpit" / "app.py"
    spec = importlib.util.spec_from_file_location("cockpit_app_issue186", app_path)
    cockpit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cockpit)
    cockpit.VAULT = h
    cockpit.WIKI = h / "wiki"
    cockpit._CFG_CACHE = (float("-inf"), None)

    response = cockpit.api_tab("taxtab")
    assert response["key"] == "taxtab"
    assert response["label"] == "Tax"
    assert response["boxes"][0]["title"] == "Events"


def test_pack_contributes_sections_to_existing_tab_without_adding_navigation(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["cockpit"] = {
        "tab_contributions": {
            "okpack-tax.coverage": {
                "target": "overview",
                "boxes": [{
                    "id": "okpack-tax.coverage-summary",
                    "section": "Threat coverage",
                    "title": "Coverage summary",
                    "view": "application-help",
                    "summary": "How this workspace works",
                }],
            },
        },
        "tab_aliases": {"legacy-tax": "overview"},
    }
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))

    assert mod.main([str(h), str(p), "--apply"]) == 0
    schema = yaml.safe_load((h / "schema.yaml").read_text())
    cockpit = schema["cockpit"]
    assert cockpit["tabs"] == ["overview", "browse"]
    contributed = cockpit["tab_defs"]["overview"]["boxes"][0]
    assert contributed["id"] == "okpack-tax.coverage-summary"
    assert contributed["contribution"] == "okpack-tax.coverage"
    assert contributed["section"] == "Threat coverage"
    assert cockpit["tab_aliases"] == {"legacy-tax": "overview"}

    # Replay is exactly idempotent; an alias resolves to the canonical tab and retains route identity.
    before = (h / "schema.yaml").read_text()
    assert mod.main([str(h), str(p), "--apply"]) == 0
    assert (h / "schema.yaml").read_text() == before

    app_path = REPO / "okengine-cockpit" / "app.py"
    spec = importlib.util.spec_from_file_location("cockpit_app_issue315", app_path)
    cockpit_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cockpit_app)
    cockpit_app.VAULT = h
    cockpit_app.WIKI = h / "wiki"
    cockpit_app._CFG_CACHE = (float("-inf"), None)
    response = cockpit_app.api_tab("legacy-tax")
    assert response["key"] == "legacy-tax"
    assert response["canonical_key"] == "overview"
    assert response["boxes"][0]["section"] == "Threat coverage"
    assert "<details" in response["boxes"][0]["html"]


def test_pack_cockpit_contribution_refresh_replaces_only_owned_boxes(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["cockpit"] = {"tab_contributions": {"okpack-tax.coverage": {
        "target": "overview",
        "boxes": [{"id": "okpack-tax.coverage-summary", "title": "First", "view": "table"}],
    }}}
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))
    assert mod.main([str(h), str(p), "--apply"]) == 0

    additions["cockpit"]["tab_contributions"]["okpack-tax.coverage"]["boxes"][0]["title"] = "Updated"
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))
    # Simulate the effective artifact Cockpit prefers when extensions are enabled. Refresh must
    # never leave this older presentation contract in front of the newly updated root schema.
    stale_artifact = h / ".okengine" / "composed-schema.yaml"
    stale_artifact.write_text(yaml.safe_dump({"cockpit": {"tabs": ["legacy"]}}))
    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 0
    boxes = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]["tab_defs"]["overview"]["boxes"]
    assert [b["title"] for b in boxes] == ["Updated"]
    assert not stale_artifact.exists(), "extensionless compose removes the stale effective artifact"


def test_pack_cockpit_contribution_preserves_block_tab_boundary(tmp_path):
    """Live schemas use block tab definitions; the following key must not join a flow box list."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    schema = yaml.safe_load((h / "schema.yaml").read_text())
    schema["cockpit"]["tab_defs"]["after"] = {"label": "After", "boxes": []}
    (h / "schema.yaml").write_text(yaml.safe_dump(schema, sort_keys=False))
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["cockpit"] = {"tab_contributions": {"okpack-tax.coverage": {
        "target": "overview",
        "boxes": [{"id": "okpack-tax.coverage-summary", "title": "Coverage", "view": "table"}],
    }}}
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))

    assert mod.main([str(h), str(p), "--apply"]) == 0
    parsed = yaml.safe_load((h / "schema.yaml").read_text())
    assert parsed["cockpit"]["tab_defs"]["overview"]["boxes"][0]["title"] == "Coverage"
    assert parsed["cockpit"]["tab_defs"]["after"]["label"] == "After"


def test_failed_pack_migration_rolls_back_install_domain_edits_too(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    pack_manifest = p / "pack.yaml"
    pack_manifest.write_text(pack_manifest.read_text() + "version: 0.1.0\n")
    assert mod.main([str(h), str(p), "--apply"]) == 0
    schema_before = (h / "schema.yaml").read_bytes()
    ownership_path = h / ".okengine" / "installed-domains" / "okpack-tax.json"
    ownership_before = ownership_path.read_bytes()

    pack_manifest.write_text(pack_manifest.read_text().replace("0.1.0", "0.2.0"))
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["cockpit"] = {"tab_contributions": {"okpack-tax.coverage": {
        "target": "overview",
        "boxes": [{"id": "okpack-tax.coverage-summary", "title": "Coverage", "view": "table"}],
    }}}
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))
    (p / "migrations").mkdir()
    (p / "migrations" / "m_0_1_0_0_2_0_fail.py").write_text(
        'ID="fail-update"\nFROM="0.1.0"\nTO="0.2.0"\nDESCRIPTION="fail"\n'
        'def apply(pack, dry_run):\n    raise RuntimeError("expected failure")\n')
    (p / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.2.0 — 2026-07-19\n- Migration impact: test failure.\n")

    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 1
    assert (h / "schema.yaml").read_bytes() == schema_before
    assert ownership_path.read_bytes() == ownership_before


def test_pack_cockpit_contribution_requires_owned_stable_ids(tmp_path, capsys):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    additions_path = p / "subdomain" / "host-schema-additions.yaml"
    additions = yaml.safe_load(additions_path.read_text())
    additions["cockpit"] = {"tab_contributions": {"coverage": {
        "target": "overview", "boxes": [{"title": "No stable id", "view": "table"}],
    }}}
    additions_path.write_text(yaml.safe_dump(additions, sort_keys=False))
    assert mod.main([str(h), str(p), "--apply"]) == 1
    assert "must be prefixed" in capsys.readouterr().out


def test_taxonomy_cockpit_tab_collision_is_host_wins_with_notice(tmp_path, capsys):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    schema = yaml.safe_load((h / "schema.yaml").read_text())
    schema["cockpit"]["tabs"].insert(-1, "taxtab")
    schema["cockpit"]["tab_defs"]["taxtab"] = {
        "label": "Host Tax",
        "boxes": [{"title": "Host-owned", "view": "table",
                   "dataset": {"dir": "entities", "type": "entity"}}],
    }
    (h / "schema.yaml").write_text(yaml.safe_dump(schema, sort_keys=False))

    assert mod.main([str(h), str(p), "--apply"]) == 0

    merged = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]
    assert merged["tab_defs"]["taxtab"]["label"] == "Host Tax"
    assert merged["tab_defs"]["taxtab"]["boxes"][0]["title"] == "Host-owned"
    assert merged["tabs"].count("taxtab") == 1
    assert "host already declares (host wins)" in capsys.readouterr().out


def test_taxonomy_seeds_cockpit_on_a_bare_host(tmp_path):
    """A cockpit-bearing guest onto a host with NO cockpit block must SEED one, not crash. A bare
    `framework init` scaffold ships no cockpit config, and merge_cockpit used to assert on the
    missing `tab_defs:` anchor — which failed every coinstall of vuln/indicators/detections/
    incidents onto a scaffold host in the library's deploy-matrix gate."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    # strip the host's cockpit block entirely (the scaffold-host shape)
    schema = (h / "schema.yaml").read_text()
    (h / "schema.yaml").write_text(schema[:schema.index("cockpit:\n")])
    assert "cockpit" not in yaml.safe_load((h / "schema.yaml").read_text())

    assert mod.main([str(h), str(p), "--apply"]) == 0
    ck = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]
    assert "taxtab" in ck["tab_defs"], ck                               # seeded + merged
    assert ck["tab_defs"]["taxtab"]["boxes"][0]["title"] == "Events"
    assert ck["tabs"].index("taxtab") < ck["tabs"].index("browse"), ck  # browse stays last
    # idempotent: a second apply neither duplicates nor re-seeds
    assert mod.main([str(h), str(p), "--apply"]) == 0
    ck2 = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]
    assert ck2["tabs"].count("taxtab") == 1 and ck2["tabs"].count("browse") == 1


def test_taxonomy_opens_tab_defs_in_an_existing_cockpit(tmp_path):
    """Host has a cockpit: block (tabs only) but no tab_defs: line — the merge must open one inside
    the existing block rather than assert."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    schema = (h / "schema.yaml").read_text()
    (h / "schema.yaml").write_text(schema[:schema.index("cockpit:\n")]
                                   + "cockpit:\n  tabs: [overview, browse]\n")
    assert mod.main([str(h), str(p), "--apply"]) == 0
    ck = yaml.safe_load((h / "schema.yaml").read_text())["cockpit"]
    assert "taxtab" in ck["tab_defs"], ck
    assert "overview" in ck["tabs"] and ck["tabs"].index("taxtab") < ck["tabs"].index("browse")


def test_taxonomy_idempotent(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    mod.main([str(h), str(p), "--apply"])
    snap = {f: (h / f).read_text() for f in
            ("schema.yaml", "feeds/feeds.opml", "crons/domain-crons.json",
             "crons/engine-template-prompts.json", "CLAUDE.md")}
    assert mod.main([str(h), str(p), "--apply"]) == 0
    for f, txt in snap.items():
        assert (h / f).read_text() == txt, f


def test_taxonomy_refresh_updates_owned_runtime_assets_and_records_manifest(tmp_path, capsys):
    """#270: a merged guest update must have a safe path into the composed deployment."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    state = _sibling("composed_pack_state")
    manifest_path = state.manifest_path(h, "okpack-tax")
    assert manifest_path.is_file()
    before_manifest = json.loads(manifest_path.read_text())
    assert "okpack_tax_feed_fetch.py" in before_manifest["lane_scripts"]
    assert "okpack-tax-feed-fetch" in before_manifest["cron_jobs"]
    host_jobs = json.loads((h / "crons" / "domain-crons.json").read_text())
    next(j for j in host_jobs if j["name"] == "okpack-tax-feed-fetch")["enabled"] = False
    (h / "crons" / "domain-crons.json").write_text(json.dumps(host_jobs))
    assert state.installed_drift(h, before_manifest) == []  # operator enablement is not drift

    # Simulate the next pack release changing both its lane and its scheduled definition.
    (p / "crons" / "scripts" / "okpack_tax_feed_fetch.py").write_text("# tax lane v2\n")
    jobs = json.loads((p / "crons" / "domain-crons.json").read_text())
    jobs[0]["schedule"] = {"kind": "cron", "expr": "17 */2 * * *"}
    (p / "crons" / "domain-crons.json").write_text(json.dumps(jobs))

    # A normal re-install diagnoses the stale copy and cannot silently bless/overwrite it.
    assert mod.main([str(h), str(p), "--apply"]) == 1
    out = capsys.readouterr().out
    assert "--refresh" in out
    assert (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").read_text() == "# tax lane\n"
    assert json.loads(manifest_path.read_text()) == before_manifest

    # Explicit refresh replaces only manifest-owned assets and advances their accepted hashes.
    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 0
    assert (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").read_text() == "# tax lane v2\n"
    installed = json.loads((h / "crons" / "domain-crons.json").read_text())
    refreshed = next(j for j in installed if j["name"] == "okpack-tax-feed-fetch")
    assert refreshed["schedule"]["expr"] == "17 */2 * * *"
    assert refreshed["enabled"] is False                    # refresh cannot activate a lane
    manifest = json.loads(manifest_path.read_text())
    assert manifest != before_manifest
    assert state.installed_drift(h, manifest) == []
    capsys.readouterr()
    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 0
    assert "refresh 1 owned domain cron" not in capsys.readouterr().out


def test_taxonomy_refresh_adopts_only_provable_legacy_assets(tmp_path):
    """A pre-#270 deployment can refresh its job script, but --refresh is not a force flag."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    state = _sibling("composed_pack_state")
    state.manifest_path(h, "okpack-tax").unlink()       # legacy deployment: no ownership manifest
    (p / "crons" / "scripts" / "okpack_tax_feed_fetch.py").write_text("# adopted v2\n")
    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 0
    assert (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").read_text() == "# adopted v2\n"

    # An unreferenced same-name file has no ownership proof and remains fail-closed.
    (p / "crons" / "scripts" / "foreign_helper.py").write_text("# guest\n")
    (h / "crons" / "scripts" / "foreign_helper.py").write_text("# host\n")
    jobs = json.loads((p / "crons" / "domain-crons.json").read_text())
    jobs.append({"id": "dd", "name": "okpack-tax-foreign", "script": "foreign_helper.py"})
    (p / "crons" / "domain-crons.json").write_text(json.dumps(jobs))
    state.manifest_path(h, "okpack-tax").unlink()
    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 1
    assert (h / "crons" / "scripts" / "foreign_helper.py").read_text() == "# host\n"


def test_taxonomy_refresh_preserves_shared_support_module(tmp_path, capsys):
    """A flat helper imported by a lane is host-controlled; refreshing one pack cannot regress it."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    entry = p / "crons" / "scripts" / "okpack_tax_feed_fetch.py"
    entry.write_text("from _shared_writer import write\n")
    (p / "crons" / "scripts" / "_shared_writer.py").write_text("write = 'v1'\n")
    assert mod.main([str(h), str(p), "--apply"]) == 0
    state = _sibling("composed_pack_state")
    manifest = json.loads(state.manifest_path(h, "okpack-tax").read_text())
    assert "okpack_tax_feed_fetch.py" in manifest["lane_scripts"]
    assert "_shared_writer.py" in manifest["shared_support_scripts"]
    assert "_shared_writer.py" not in manifest["lane_scripts"]

    # The host helper has moved ahead independently. A pack entrypoint update may land, but the
    # helper remains untouched and the operator gets a visible warning.
    (h / "crons" / "scripts" / "_shared_writer.py").write_text("write = 'host-v2'\n")
    entry.write_text("from _shared_writer import write\n# entry v2\n")
    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 0
    assert (h / "crons" / "scripts" / "_shared_writer.py").read_text() == "write = 'host-v2'\n"
    assert "entry v2" in (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").read_text()
    assert "remain host-controlled" in capsys.readouterr().out


def test_taxonomy_refresh_does_not_touch_schema_cockpit_or_persona(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    before = {name: (h / name).read_bytes() for name in ("schema.yaml", "CLAUDE.md", "feeds/feeds.opml")}
    additions = yaml.safe_load((p / "subdomain" / "host-schema-additions.yaml").read_text())
    additions["types"]["new-in-release"] = {"required": ["type", "id"]}
    (p / "subdomain" / "host-schema-additions.yaml").write_text(yaml.safe_dump(additions))
    (p / "crons" / "scripts" / "okpack_tax_feed_fetch.py").write_text("# runtime v2\n")

    assert mod.main([str(h), str(p), "--refresh", "--apply"]) == 0
    assert "runtime v2" in (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").read_text()
    assert {name: (h / name).read_bytes() for name in before} == before
    assert "new-in-release" not in yaml.safe_load((h / "schema.yaml").read_text())["types"]


def test_composed_manifest_drift_is_reported_by_framework_validate(tmp_path):
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    assert mod.main([str(h), str(p), "--apply"]) == 0
    (h / "crons" / "scripts" / "okpack_tax_feed_fetch.py").write_text("# hand edit\n")
    validator = _sibling("framework_validate")
    report = validator.validate(h)
    drift = [(sev, detail) for sev, check, detail in report.rows
             if check == "composed pack drift"]
    assert any(sev == "WARN" and "modified crons/scripts/okpack_tax_feed_fetch.py" in detail
               for sev, detail in drift)


def test_taxonomy_idempotent_with_friendly_persona_marker(tmp_path):
    """Regression (library deploy-matrix): a pack whose PERSONA.md FIRST LINE is a friendly
    '## Installed domain: <title>' heading — carrying neither the slug nor the pack-name nor a
    `wiki/<slug>/` token — slipped past the marker heuristic, so re-apply double-appended the
    persona block. The wording-independent provenance marker must make re-install idempotent
    regardless of how the pack titles its section."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    # first line is the marker, but contains NO identity token the legacy heuristic recognizes
    (p / "subdomain" / "PERSONA.md").write_text(
        "## Installed domain: detections & mitigations (the defensive-response layer)\n\n"
        "curation rules go here\n")
    assert mod.main([str(h), str(p), "--apply"]) == 0
    after_first = (h / "CLAUDE.md").read_text()
    assert after_first.count("## Installed domain:") == 1
    assert "okengine:installed-domain okpack-tax" in after_first   # provenance stamp present
    # re-apply: no new step, CLAUDE.md byte-identical, still exactly one block
    assert mod.main([str(h), str(p), "--apply"]) == 0
    after_second = (h / "CLAUDE.md").read_text()
    assert after_second == after_first
    assert after_second.count("## Installed domain:") == 1


def test_host_schema_comments_survive_the_merge(tmp_path):
    """The installer must only ADD lines — a parse+safe_dump round-trip strips a
    live deployment schema's comments (caught heading into the first real install)."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    schema = (h / "schema.yaml").read_text()
    (h / "schema.yaml").write_text(
        "# HOST HEADER COMMENT — must survive\n" + schema +
        "\n# HOST TRAILING COMMENT — must survive\n")
    assert mod.main([str(h), str(p), "--apply"]) == 0
    out = (h / "schema.yaml").read_text()
    assert "# HOST HEADER COMMENT — must survive" in out
    assert "# HOST TRAILING COMMENT — must survive" in out
    assert "intrusion-set" in yaml.safe_load(out)["types"]


def test_no_coinstall_form_errors(tmp_path):
    h = _host(tmp_path)
    bare = tmp_path / "okpack-bare"
    bare.mkdir()
    (bare / "schema.yaml").write_text("types: {}\n")
    assert mod.main([str(h), str(bare)]) == 2


# ── okengine#181: type_aliases carried through composition ───────────────────
def _alias_pack(tmp_path, aliases) -> Path:
    p = tmp_path / "okpack-alias"
    (p / "subdomain").mkdir(parents=True)
    (p / "schema.yaml").write_text(yaml.safe_dump(
        {"name": "okpack-alias", "types": {"intrusion-set": {"required": ["type", "id"]}}}))
    (p / "pack.yaml").write_text(
        "name: okpack-alias\ntrust: public\nowns: {types: [intrusion-set]}\n")
    add = {"types": {"intrusion-set": {"required": ["type", "id"]}}}
    if aliases:
        add["type_aliases"] = aliases
    (p / "subdomain" / "host-schema-additions.yaml").write_text(yaml.safe_dump(add))
    (p / "subdomain" / "PERSONA.md").write_text("rules\n")
    return p


def test_taxonomy_merges_type_aliases(tmp_path):
    """A guest's host-schema-additions type_aliases land in the composed host schema so old/
    variant names resolve to canonical types (the STIX-reconciliation path)."""
    h = _host(tmp_path)                                   # host has NO type_aliases block yet
    p = _alias_pack(tmp_path, {"threat-actor": "intrusion-set", "apt": "intrusion-set"})
    assert mod.main([str(h), str(p), "--apply"]) == 0
    sch = yaml.safe_load((h / "schema.yaml").read_text())
    assert sch["type_aliases"]["threat-actor"] == "intrusion-set"
    assert sch["type_aliases"]["apt"] == "intrusion-set"


def test_type_alias_merge_host_wins(tmp_path):
    h = _host(tmp_path)
    (h / "schema.yaml").write_text(
        (h / "schema.yaml").read_text() + "\ntype_aliases:\n  threat-actor: existing\n")
    p = _alias_pack(tmp_path, {"threat-actor": "intrusion-set"})
    assert mod.main([str(h), str(p), "--apply"]) == 0
    sch = yaml.safe_load((h / "schema.yaml").read_text())
    assert sch["type_aliases"]["threat-actor"] == "existing"     # host wins, not overwritten


def test_incoming_alias_shadowing_host_type_is_skipped(tmp_path):
    h = _host(tmp_path)                                   # host OWNS a `vendor` type
    p = _alias_pack(tmp_path, {"vendor": "identity"})    # alias key == host-owned type
    assert mod.main([str(h), str(p), "--apply"]) == 0    # WARN, not FAIL
    sch = yaml.safe_load((h / "schema.yaml").read_text())
    assert "vendor" not in (sch.get("type_aliases") or {})       # skipped (would shadow a type)


def test_type_alias_merge_idempotent(tmp_path):
    h = _host(tmp_path)
    p = _alias_pack(tmp_path, {"threat-actor": "intrusion-set"})
    mod.main([str(h), str(p), "--apply"])
    snap = (h / "schema.yaml").read_text()
    assert mod.main([str(h), str(p), "--apply"]) == 0
    assert (h / "schema.yaml").read_text() == snap               # no re-merge


def test_type_alias_merge_into_inline_flow_map_host(tmp_path):
    """A host whose type_aliases is an INLINE flow map (`type_aliases: {a: b, ...}`) must have the
    guest's aliases INJECTED into that map — not appended as a second `type_aliases:` key, which
    YAML duplicate-key resolution would let clobber the host's own aliases (okengine#181)."""
    h = _host(tmp_path)
    # give the host an inline, multi-line flow-style type_aliases (like okpack-threat-actors)
    (h / "schema.yaml").write_text(
        (h / "schema.yaml").read_text()
        + "\ntype_aliases: {group: actor, adversary: actor,\n"
          "               threat-actor: actor, attack-pattern: technique}\n")
    p = _alias_pack(tmp_path, {"mitigation": "course-of-action", "ioc": "indicator"})
    assert mod.main([str(h), str(p), "--apply"]) == 0
    al = yaml.safe_load((h / "schema.yaml").read_text())["type_aliases"]
    # host's OWN aliases survive
    assert al["threat-actor"] == "actor" and al["attack-pattern"] == "technique"
    assert al["group"] == "actor"
    # guest aliases are merged in
    assert al["mitigation"] == "course-of-action" and al["ioc"] == "indicator"


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_taxonomy_apply_regenerates_composed_schema_artifact(tmp_path):
    """invariant-audit #3: on an extension-enabled host the RUNTIME write path PREFERS
    <host>/.okengine/composed-schema.yaml. The taxonomy merge edits schema.yaml only, so a stale
    artifact predating the merge would silently reject every write to the newly co-installed
    namespace ('namespace not declared'). After --apply the artifact must be regenerated to carry
    the merged namespace — while keeping the extension's own owned type."""
    h, p = _host(tmp_path), _taxonomy_pack(tmp_path)
    # live state: an enabled schema-bringing extension (like messaging-synthesis/predictions on the
    # real deployments) made the composed artifact exist.
    d = h / "extensions" / "demo.pred"
    (d / "schema").mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump({
        "id": "demo.pred", "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
        "requires": {"engine": ">=0.3.0"},
        "capabilities": {"read": ["wiki/**"], "write": ["forecasts/**"]},
        "schema": ["schema/forecasts.schema.yaml"],
        "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                      "entrypoint": {"script": "run.py"}}}))
    (d / "schema" / "forecasts.schema.yaml").write_text(yaml.safe_dump({
        "owns": {"namespaces": ["forecasts"], "types": {"forecast": {"required": ["claim"]}}}}))
    disc, comp = _sibling("extension_discovery"), _sibling("extension_compose")
    disc.set_enabled(h, "demo.pred", True)
    assert comp.write_composed_schema(h) == []
    art = h / ".okengine" / "composed-schema.yaml"
    before = yaml.safe_load(art.read_text())
    assert "tax-events" not in before.get("partitioning", {}).get("namespaces", {})   # stale: pre-merge

    assert mod.main([str(h), str(p), "--apply"]) == 0

    after = yaml.safe_load(art.read_text())
    assert "tax-events" in after.get("partitioning", {}).get("namespaces", {}), \
        "composed artifact must be regenerated with the merged namespace (else writes are rejected)"
    assert "forecast" in after.get("types", {}), "extension-owned type must survive the regeneration"
