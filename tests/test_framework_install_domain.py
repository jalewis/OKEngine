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
        # the guest contributes its cockpit tab so a composed vault surfaces it (okengine#<n>)
        "cockpit": {"tabs": ["taxtab"], "tab_defs": {
            "taxtab": {"label": "Tax", "boxes": [
                {"title": "Events", "view": "table", "dataset": {"dir": "tax-events", "type": "intrusion-set"}}]}}},
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
