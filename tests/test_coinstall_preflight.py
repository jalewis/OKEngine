"""HIGH #2 regression: install-domain co-install must reject a guest whose pack.yaml trust differs
from the host's — else a private guest's content is served on a public host's unauthenticated reader
(the reader/cockpit serve one global trust, frozen from the HOST at first deploy)."""
import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "coinstall_preflight.py"


def _mod():
    spec = importlib.util.spec_from_file_location("coinstall_preflight", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["coinstall_preflight"] = m
    spec.loader.exec_module(m)
    m.FINDINGS.clear()
    return m


def _pack(d: Path, trust: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(f"name: p\ntrust: {trust}\n")
    (d / "schema.yaml").write_text("types: {}\n")
    return d


def test_check_trust_fails_private_guest_on_public_host(tmp_path):
    m = _mod()
    m.check_trust(_pack(tmp_path / "host", "public"), _pack(tmp_path / "guest", "private"))
    assert any(l == "FAIL" and a == "trust" for l, a, _ in m.FINDINGS), m.FINDINGS


def test_check_trust_passes_when_aligned(tmp_path):
    m = _mod()
    m.check_trust(_pack(tmp_path / "host", "public"), _pack(tmp_path / "guest", "public"))
    assert not any(a == "trust" for _, a, _ in m.FINDINGS), m.FINDINGS


def test_check_trust_defaults_private_and_flags_public_host(tmp_path):
    """A guest with NO trust declared defaults to private (engine default) — still flagged on a public host."""
    m = _mod()
    host = _pack(tmp_path / "host", "public")
    guest = tmp_path / "guest"; guest.mkdir()
    (guest / "pack.yaml").write_text("name: p\n")            # no trust -> defaults private
    (guest / "schema.yaml").write_text("types: {}\n")
    m.check_trust(host, guest)
    assert any(l == "FAIL" and a == "trust" for l, a, _ in m.FINDINGS), m.FINDINGS


def test_check_trust_allows_public_guest_on_private_host(tmp_path):
    """The SAFE direction: a public guest on a private host is over-protected (served privately), not
    leaked — so it must NOT fail (this is what broke the alias-merge tests when the check was symmetric)."""
    m = _mod()
    m.check_trust(_pack(tmp_path / "host", "private"), _pack(tmp_path / "guest", "public"))
    assert not any(a == "trust" for _, a, _ in m.FINDINGS), m.FINDINGS


def test_extension_owned_type_collision_flagged(tmp_path):  # okengine#326 [10]
    """A pack type that collides with an ENABLED EXTENSION's owned type must FAIL — the root-schema
    checks miss it because the extension's ids live in the composed artifact's owners map, not the
    host's schema.yaml."""
    m = _mod()
    host = tmp_path / "host"; (host / ".okengine").mkdir(parents=True)
    (host / "schema.yaml").write_text("types: {}\n")   # host ROOT declares nothing colliding
    (host / ".okengine" / "composed-schema.yaml").write_text(
        "types: {assessment: {}, gadget: {}}\n"
        "owners:\n  types: {assessment: 'ext:okengine.assessments', gadget: 'ext:demo.gadgets'}\n"
        "  namespaces: {gadgets: 'ext:demo.gadgets'}\n")
    pack = tmp_path / "pack"; pack.mkdir()
    (pack / "pack.yaml").write_text(
        "name: p\ntrust: private\nowns:\n  namespaces: [gadgets]\n"
    )
    (pack / "schema.yaml").write_text(
        "types: {assessment: {required: [x]}}\n"          # collides with an ext-owned type
        "partitioning: {namespaces: {gadgets: {}}}\n")     # collides with an ext-owned namespace
    m.check_extension_collisions(host, pack)
    fails = [msg for lvl, area, msg in m.FINDINGS if lvl == "FAIL"]
    assert any("assessment" in f and "okengine.assessments" in f for f in fails), m.FINDINGS
    assert any("gadgets" in f and "demo.gadgets" in f for f in fails), m.FINDINGS
    # subtree shape downgrades to WARN (walk-up separates the contracts), never silent
    m.FINDINGS.clear()
    m.check_extension_collisions(host, pack, subtree=True)
    assert not any(lvl == "FAIL" for lvl, _, _ in m.FINDINGS), m.FINDINGS
    assert any(lvl == "WARN" and "assessment" in msg for lvl, _, msg in m.FINDINGS), m.FINDINGS

    # Taxonomy installs merge host-schema-additions.yaml, not the standalone schema. A collision
    # that exists only in the materialized additions must still block before install.
    additions = pack / "subdomain" / "host-schema-additions.yaml"
    additions.parent.mkdir()
    additions.write_text(
        "types: {gadget: {required: [name]}}\n"
    )
    (pack / "schema.yaml").write_text("types: {}\npartitioning: {namespaces: {}}\n")
    m.FINDINGS.clear()
    m.check_extension_collisions(host, pack, additions)
    assert any(lvl == "FAIL" and "gadgets" in msg for lvl, _, msg in m.FINDINGS)


def test_extension_namespace_collision_matches_exact_installer_landing_set(tmp_path):
    m = _mod()
    host = tmp_path / "host"
    (host / ".okengine").mkdir(parents=True)
    (host / ".okengine/composed-schema.yaml").write_text(
        "owners:\n  types: {}\n  namespaces: {gadgets: 'ext:demo.gadgets'}\n"
    )
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {gadgets: {}, reports: {}}}\n"
    )

    # Taxonomy with no owns block lands no namespace and must not false-fail.
    (pack / "pack.yaml").write_text("name: p\n")
    m.check_extension_collisions(host, pack)
    assert not m.FINDINGS

    # Mapping-form ownership lands its keys just like list-form ownership.
    (pack / "pack.yaml").write_text("name: p\nowns:\n  namespaces: {gadgets: {}}\n")
    m.check_extension_collisions(host, pack)
    assert any(level == "FAIL" and "gadgets" in message
               for level, _, message in m.FINDINGS)

    # Subtree landing is driven by the supplied subdomain schema, not pack ownership.
    m.FINDINGS.clear()
    additions = pack / "subdomain/schema.yaml"
    additions.parent.mkdir()
    additions.write_text("types: {}\npartitioning: {namespaces: {gadgets: {}}}\n")
    (pack / "pack.yaml").write_text("name: p\nowns:\n  namespaces: [reports]\n")
    m.check_extension_collisions(host, pack, additions, subtree=True)
    assert any(level == "WARN" and "gadgets" in message
               for level, _, message in m.FINDINGS)

    # Invalid ownership shapes land no namespaces rather than iterating a string.
    m.FINDINGS.clear()
    (pack / "pack.yaml").write_text("name: p\nowns:\n  namespaces: gadgets\n")
    m.check_extension_collisions(host, pack)
    assert not m.FINDINGS


def test_no_composed_schema_produces_no_false_fail(tmp_path):  # okengine#326 [10] regression
    """An absent .okengine/composed-schema.yaml (no enabled extensions, or a host being composed
    fresh — the pack-parity taxonomy-shape case) must NOT emit a FAIL: the check simply has nothing
    to compare against. (Regression: the first cut called _yaml() unconditionally, which added a
    spurious 'unparseable yaml' FAIL on the missing file and broke the pack-parity gate.)"""
    m = _mod()
    host = tmp_path / "host"; host.mkdir()
    (host / "schema.yaml").write_text("types: {}\n")            # NO composed-schema.yaml
    pack = tmp_path / "pack"; pack.mkdir()
    (pack / "schema.yaml").write_text("types: {gadget: {}}\npartitioning: {namespaces: {gadgets: {}}}\n")
    m.check_extension_collisions(host, pack)
    assert m.FINDINGS == [], m.FINDINGS


def test_full_preflight_surfaces_every_collision_family(tmp_path, capsys):
    m = _mod()
    host = _pack(tmp_path / "host", "public")
    pack = _pack(tmp_path / "okpack-demo", "private")
    (host / "schema.yaml").write_text(
        "types:\n"
        "  same: {required: [x]}\n"
        "  different: {required: [host]}\n"
        "  owned: {required: []}\n"
        "type_aliases: {shadowed: product}\n"
        "partitioning: {namespaces: {custom: {strategy: flat}}}\n"
    )
    (pack / "schema.yaml").write_text(
        "types:\n"
        "  same: {required: [x]}\n"
        "  different: {required: [pack]}\n"
        "  shadowed: {required: []}\n"
        "type_aliases: {owned: replacement}\n"
        "partitioning: {namespaces: {custom: {strategy: by-letter}}}\n"
    )
    domain_schema = host / "wiki" / "domain" / "schema.yaml"
    domain_schema.parent.mkdir(parents=True)
    domain_schema.write_text("types: {same: {}}\n")
    (host / "wiki" / "custom").mkdir()

    for root in (host, pack):
        (root / "crons").mkdir()
        (root / "config").mkdir()
        (root / "feeds").mkdir()
    (host / "crons" / "domain-crons.json").write_text(json.dumps([
        {"id": "same", "name": "installed"},
        {"id": "host-id", "name": "host-name"},
    ]))
    (pack / "crons" / "domain-crons.json").write_text(json.dumps([
        {"id": "same", "name": "installed"},
        {"id": "new-id", "name": "host-name"},
        {"id": "host-id", "name": "new-name"},
    ]))
    (host / "crons" / "engine-template-prompts.json").write_text('{"shared": "h"}')
    (pack / "crons" / "engine-template-prompts.json").write_text('{"shared": "p"}')

    host_rules = {"rules": [{"id": "same", "x": 1}, {"id": "different", "x": 1}]}
    pack_rules = {"rules": [{"id": "same", "x": 1}, {"id": "different", "x": 2}]}
    (host / "config" / "rules.yaml").write_text(__import__("yaml").safe_dump(host_rules))
    (pack / "config" / "rules.yaml").write_text(__import__("yaml").safe_dump(pack_rules))
    (host / "feeds" / "h.opml").write_text('<outline xmlUrl="https://same"/>')
    (pack / "feeds" / "p.opml").write_text('<outline xmlUrl="https://same"/>')

    scripts = pack / "crons" / "scripts"
    scripts.mkdir()
    (scripts / "writer.py").write_text(
        'stream = "raw/shared"\ndash = "dashboards/shared.md"\n'
    )
    (host / "raw" / "shared").mkdir(parents=True)
    dashboard = host / "wiki" / "dashboards" / "shared.md"
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("host")

    assert m.main([str(host), str(pack)]) == 1
    out = capsys.readouterr().out
    for token in ("[trust]", "[types]", "[namespaces]", "[crons]",
                  "[configs]", "[feeds]", "[raw-streams]", "[dashboards]"):
        assert token in out
    assert "already installed" in out
    assert "rule id(s) already merged" in out and "rule id collision" in out


def test_subtree_idempotency_streams_and_namespace_paths(tmp_path):
    m = _mod()
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "okpack-demo", "private")
    (host / "schema.yaml").write_text("types: {x: {required: [a]}}\n")
    (pack / "schema.yaml").write_text("types: {x: {required: [b]}}\n")
    sub = pack / "subdomain" / "schema.yaml"
    sub.parent.mkdir()
    sub.write_text("types: {x: {}}\n")
    installed = host / "wiki" / "demo"
    installed.mkdir(parents=True)
    (installed / "schema.yaml").write_bytes(sub.read_bytes())
    m.check_types(host, pack, None, subtree=True)
    m.check_namespaces(host, pack, subtree=True)
    assert any(level == "WARN" and area == "types" for level, area, _ in m.FINDINGS)
    assert any(level == "INFO" and area == "namespaces" for level, area, _ in m.FINDINGS)

    m.FINDINGS.clear()
    script = pack / "crons" / "scripts" / "writer.py"
    script.parent.mkdir(parents=True)
    script.write_text('raw/owned dashboards/owned.md')
    host_script = host / "crons" / "scripts" / "writer.py"
    host_script.parent.mkdir(parents=True)
    host_script.write_bytes(script.read_bytes())
    (host / "raw" / "owned").mkdir(parents=True)
    dash = host / "wiki" / "dashboards" / "owned.md"
    dash.parent.mkdir(parents=True)
    dash.write_text("x")
    m.check_streams_dashboards(host, pack)
    assert {area for level, area, _ in m.FINDINGS if level == "INFO"} == {
        "raw-streams", "dashboards"
    }


def test_clean_main_usage_parse_fallbacks_and_entrypoint(tmp_path, capsys, monkeypatch):
    m = _mod()
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "pack", "public")
    (pack / "schema.yaml").write_text("types: {unique: {required: []}}\n")
    assert m.main([str(host), str(pack)]) == 0
    assert "clean — no collisions" in capsys.readouterr().out
    assert m.main([str(tmp_path / "missing"), str(pack)]) == 2

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("[")
    m.FINDINGS.clear()
    assert m._yaml(malformed) == {}
    assert any(area == "parse" for _, area, _ in m.FINDINGS)
    (host / "crons").mkdir()
    (pack / "crons").mkdir()
    (host / "crons" / "domain-crons.json").write_text("{")
    (pack / "crons" / "domain-crons.json").write_text("[]")
    m.check_crons(host, pack)
    assert any(area == "crons" for _, area, _ in m.FINDINGS)

    monkeypatch.setattr(sys, "argv", [str(MOD), str(host), str(pack)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(MOD), run_name="__main__")
    assert exc.value.code == 1


def test_preflight_remaining_empty_idempotent_and_parse_edges(tmp_path, monkeypatch):
    m = _mod()
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "okpack-demo", "private")
    (host / "schema.yaml").write_text("types: {}\n")
    (pack / "schema.yaml").write_text("types: {}\n")
    m.check_types(host, pack, None)
    assert any("no types found" in message for _, _, message in m.FINDINGS)

    (pack / "pack.yaml").write_text("domain: /explicit-domain/\ntrust: private\n")
    assert m._domain_slug(pack) == "explicit-domain"
    # Missing subtree target is a clean early return.
    m.check_namespaces(host, pack, subtree=True)
    conflict = host / "wiki" / "explicit-domain"
    conflict.mkdir(parents=True)
    m.check_namespaces(host, pack, subtree=True)
    assert any(level == "FAIL" and area == "namespaces" for level, area, _ in m.FINDINGS)

    # Standalone namespace already occupied by this exact subdomain schema.
    (pack / "schema.yaml").write_text(
        "partitioning: {namespaces: {custom: {strategy: flat}}}\n"
    )
    sub = pack / "subdomain" / "schema.yaml"
    sub.parent.mkdir()
    sub.write_text("types: {}\n")
    custom = host / "wiki" / "custom"
    custom.mkdir()
    (custom / "schema.yaml").write_bytes(sub.read_bytes())
    m.check_namespaces(host, pack)
    assert any(level == "INFO" and "sub-domain" in message for level, _, message in m.FINDINGS)

    # Shape equality alone is not identity: an unrelated occupied namespace must fail.
    (pack / "schema.yaml").write_text(
        "partitioning: {namespaces: {absent: {strategy: flat}, taxo: {strategy: flat}}}\n"
    )
    (host / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {taxo: {strategy: flat}}}\n"
    )
    (host / "wiki/taxo").mkdir()
    m.check_namespaces(host, pack)
    assert any(level == "FAIL" and "wiki/taxo" in message
               for level, _, message in m.FINDINGS)

    # Once an ownership manifest records the namespace for THIS pack, the same contract is an
    # idempotent reinstall rather than a collision.
    owned = host / ".okengine" / "installed-domains" / "okpack-demo.json"
    owned.parent.mkdir(parents=True)
    owned.write_text(json.dumps({"pack": "okpack-demo", "owned_namespaces": {
        "taxo": {"partitioning": {"strategy": "flat"}, "permissions": None, "tier": None}
    }}))
    m.FINDINGS.clear()
    m.check_namespaces(host, pack)
    assert any(level == "INFO" and "host partitioning" in message
               for level, _, message in m.FINDINGS)

    owned.unlink()
    original_read = Path.read_text
    reads = {host / "schema.yaml": 0}
    def unreadable_schema(path, *args, **kwargs):
        if path == host / "schema.yaml":
            reads[path] += 1
            if reads[path] == 2:
                raise OSError("unreadable")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unreadable_schema)
    m.FINDINGS.clear()
    m.check_namespaces(host, pack)
    assert any(level == "FAIL" and "already exists" in message
               for level, _, message in m.FINDINGS)
    monkeypatch.setattr(Path, "read_text", original_read)

    # Prompt parse failures are deliberately ignored.
    for root in (host, pack):
        (root / "crons").mkdir(exist_ok=True)
        (root / "crons/domain-crons.json").write_text("[]")
        (root / "crons/engine-template-prompts.json").write_text("{")
    m.check_crons(host, pack)

    # Config iteration includes directories/non-collisions and malformed colliding rule files.
    hc, pc = host / "config", pack / "config"
    hc.mkdir(); pc.mkdir()
    (pc / "directory").mkdir()
    (pc / "pack-only.yaml").write_text("x: 1\n")
    (hc / "rules.yaml").write_text("{")
    (pc / "rules.yaml").write_text("{")
    (hc / "shared.txt").write_text("host")
    (pc / "shared.txt").write_text("pack")
    m.check_configs(host, pack)
    assert any(level == "WARN" and area == "configs" for level, area, _ in m.FINDINGS)


def test_stream_dashboard_loops_with_absent_and_unowned_targets(tmp_path):
    m = _mod()
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "pack", "private")
    scripts = pack / "crons/scripts"
    scripts.mkdir(parents=True)
    (scripts / "a.py").write_text("raw/absent dashboards/absent.md")
    (scripts / "b.py").write_text("raw/shared dashboards/shared.md")
    (host / "raw/shared").mkdir(parents=True)
    dash = host / "wiki/dashboards/shared.md"
    dash.parent.mkdir(parents=True)
    dash.write_text("host")
    m.check_streams_dashboards(host, pack)
    assert {area for level, area, _ in m.FINDINGS if level == "FAIL"} == {
        "raw-streams", "dashboards",
    }


def test_type_and_extension_loops_include_noncolliding_entries(tmp_path):
    m = _mod()
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "pack", "private")
    (host / "schema.yaml").write_text("types: {owned: {}}\n")
    (pack / "schema.yaml").write_text(
        "types: {guest: {}}\ntype_aliases: {owned: guest, harmless: guest}\n"
    )
    for name, types in (("a", "guest"), ("b", "other")):
        path = host / "wiki" / name / "schema.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(f"types: {{{types}: {{}}}}\n")
    m.check_types(host, pack, None)
    assert any("incoming type_alias" in message for _, _, message in m.FINDINGS)

    composed = host / ".okengine/composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text("owners: {types: {base: engine}, namespaces: {core: pack}}\n")
    m.check_extension_collisions(host, pack)


# --- okengine#812: a namespace the host does not declare, holding the incoming pack's own content,
# is an ADOPTION rather than a collision. Refusing it is what pushes a co-install off the supported
# path (okengine#811: 524 pages in a namespace no schema declared, outside every partition guard).


def _adoption_case(tmp_path, *, host_owns=(), pack_owns=("t-guest",), pages=(("a.md", "t-guest"),),
                   host_declares=False):
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "okpack-demo", "private")
    (host / "pack.yaml").write_text(
        "name: okpack-host\ntrust: private\nowns:\n  types: [%s]\n" % ", ".join(host_owns))
    (pack / "pack.yaml").write_text(
        "name: okpack-demo\ntrust: private\nowns:\n  types: [%s]\n" % ", ".join(pack_owns))
    (pack / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    hyp: {strategy: by-letter}\n")
    (host / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    hyp: {strategy: flat}\n" if host_declares
        else "types: {}\n")
    ns = host / "wiki" / "hyp"
    ns.mkdir(parents=True)
    for name, typ in pages:
        (ns / name).write_text(f"---\ntype: {typ}\nid: x\n---\n\nbody\n")
    return host, pack


def test_undeclared_namespace_holding_only_pack_types_is_adopted(tmp_path):
    m = _mod()
    host, pack = _adoption_case(tmp_path)
    m.check_namespaces(host, pack)
    assert not any(level == "FAIL" for level, _, _ in m.FINDINGS), m.FINDINGS
    warn = [msg for level, area, msg in m.FINDINGS if level == "WARN" and area == "namespaces"]
    assert warn and "ADOPTING" in warn[0], m.FINDINGS
    assert "reshelve drain" in warn[0] and "--refresh is runtime-only" in warn[0], warn


def test_adoption_refused_when_namespace_holds_foreign_types(tmp_path):
    m = _mod()
    host, pack = _adoption_case(tmp_path, pages=(("a.md", "t-guest"), ("b.md", "t-other")))
    m.check_namespaces(host, pack)
    fail = [msg for level, area, msg in m.FINDINGS if level == "FAIL" and area == "namespaces"]
    assert fail and "t-other" in fail[0], m.FINDINGS


def test_adoption_refused_while_both_packs_claim_the_type(tmp_path):
    """The exclusivity requirement: a type both pack.yamls claim cannot attribute the directory, so
    the collision stands until a human reconciles owns.types (the okcti case in okengine#811)."""
    m = _mod()
    host, pack = _adoption_case(tmp_path, host_owns=("t-guest",))
    m.check_namespaces(host, pack)
    fail = [msg for level, area, msg in m.FINDINGS if level == "FAIL" and area == "namespaces"]
    assert fail and "BOTH packs claim" in fail[0], m.FINDINGS


def test_empty_namespace_directory_is_adopted(tmp_path):
    m = _mod()
    host, pack = _adoption_case(tmp_path, pages=())
    m.check_namespaces(host, pack)
    assert not any(level == "FAIL" for level, _, _ in m.FINDINGS), m.FINDINGS
    assert any("empty of pages" in msg for _, _, msg in m.FINDINGS), m.FINDINGS


def test_generated_artifacts_never_veto_an_adoption(tmp_path):
    """INDEX.md and dashboards are engine-written into whatever namespace they describe; counting
    them as foreign content would veto every adoption."""
    m = _mod()
    host, pack = _adoption_case(
        tmp_path, pages=(("a.md", "t-guest"), ("INDEX.md", "index"), ("d.md", "dashboard")))
    m.check_namespaces(host, pack)
    assert not any(level == "FAIL" for level, _, _ in m.FINDINGS), m.FINDINGS
    assert any("1 page(s)" in msg for _, _, msg in m.FINDINGS), m.FINDINGS


def test_namespace_the_host_declares_differently_still_fails(tmp_path):
    """Adoption applies only where the host schema is SILENT. A host that declares the namespace
    with its own partitioning is a real ownership conflict, unchanged by okengine#812."""
    m = _mod()
    host, pack = _adoption_case(tmp_path, host_declares=True)
    m.check_namespaces(host, pack)
    assert any(level == "FAIL" and area == "namespaces" for level, area, _ in m.FINDINGS), m.FINDINGS


# --- okengine#812 (same class, second surface): ownership of a dashboard/stream cannot be proven
# by byte-identity on a REFRESH, whose whole point is that the pack's script has moved ahead.


def _dashboard_case(tmp_path, *, job_name: str):
    host = _pack(tmp_path / "host", "private")
    pack = _pack(tmp_path / "okpack-demo", "private")
    (pack / "pack.yaml").write_text("name: okpack-demo\ntrust: private\n")
    script = pack / "crons" / "scripts" / "writer.py"
    script.parent.mkdir(parents=True)
    script.write_text("# v2 — moved ahead of the deployed copy\ndashboards/board.md\n")
    staged = host / "crons" / "scripts" / "writer.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("# v1 — what the host currently runs\ndashboards/board.md\n")
    (host / "crons" / "domain-crons.json").write_text(json.dumps(
        [{"id": "aa", "name": job_name, "script": "/opt/data/scripts/writer.py"}]))
    board = host / "wiki" / "dashboards" / "board.md"
    board.parent.mkdir(parents=True)
    board.write_text("---\ntype: dashboard\n---\n")
    return host, pack


def test_a_drifted_script_is_still_this_packs_when_a_host_job_names_it(tmp_path):
    m = _mod()
    host, pack = _dashboard_case(tmp_path, job_name="okpack-demo-board-refresh")
    m.check_streams_dashboards(host, pack)
    assert not any(level == "FAIL" for level, _, _ in m.FINDINGS), m.FINDINGS
    assert any(level == "INFO" and area == "dashboards" for level, area, _ in m.FINDINGS), m.FINDINGS


def test_a_dashboard_driven_by_another_packs_job_is_still_a_collision(tmp_path):
    """Attribution is by the host's own job-name prefix. A same-named script driven by somebody
    else's lane proves nothing about this pack."""
    m = _mod()
    host, pack = _dashboard_case(tmp_path, job_name="okpack-somebody-else-board-refresh")
    m.check_streams_dashboards(host, pack)
    assert any(level == "FAIL" and area == "dashboards" for level, area, _ in m.FINDINGS), m.FINDINGS


def test_an_unreadable_entry_does_not_derail_the_type_scan(tmp_path):
    """A namespace can contain something that is not a readable page — here a directory named
    `*.md`, which `rglob` matches and `open()` refuses. One unreadable entry must not decide
    ownership for the 500 pages beside it."""
    m = _mod()
    host, pack = _adoption_case(tmp_path)
    (host / "wiki" / "hyp" / "not-a-page.md").mkdir()
    m.check_namespaces(host, pack)
    assert not any(level == "FAIL" for level, _, _ in m.FINDINGS), m.FINDINGS
    assert any("1 page(s)" in msg for _, _, msg in m.FINDINGS), m.FINDINGS


def test_a_pack_owning_no_types_cannot_claim_a_populated_namespace(tmp_path):
    """Attribution is by owned type. A pack that owns none has nothing to attribute the content
    with, so the collision stands rather than resolving in its favour by default."""
    m = _mod()
    host, pack = _adoption_case(tmp_path, pack_owns=())
    m.check_namespaces(host, pack)
    fail = [msg for level, area, msg in m.FINDINGS if level == "FAIL" and area == "namespaces"]
    assert fail and "declares no owned types" in fail[0], m.FINDINGS
# --- okengine#813: an exposure an operator has considered and accepted is recorded, keyed to the
# exact trust pair, and still reported. A gate that cannot be satisfied gets bypassed instead.


def _exposed(tmp_path):
    host = _pack(tmp_path / "host", "public")
    guest = _pack(tmp_path / "guest", "private")
    (guest / "pack.yaml").write_text("name: okpack-guest\ntrust: private\n")
    return host, guest


def test_recorded_override_downgrades_the_exposure_to_a_reported_warning(tmp_path):
    m = _mod()
    host, guest = _exposed(tmp_path)
    m.record_trust_override(host, "okpack-guest", "private", "public", "trusted-LAN reader only")
    m.check_trust(host, guest)
    assert not any(level == "FAIL" for level, _, _ in m.FINDINGS), m.FINDINGS
    warn = [msg for level, area, msg in m.FINDINGS if level == "WARN" and area == "trust"]
    assert warn and "ACCEPTED" in warn[0] and "trusted-LAN reader only" in warn[0], m.FINDINGS


def test_override_does_not_carry_to_a_different_guest_trust(tmp_path):
    """Consent was to ONE exposure. A guest that later declares a more restrictive trust is a
    different exposure than the one accepted, so the gate blocks again."""
    m = _mod()
    host, guest = _exposed(tmp_path)
    m.record_trust_override(host, "okpack-guest", "private", "public", "trusted-LAN reader only")
    (guest / "pack.yaml").write_text("name: okpack-guest\ntrust: secret\n")
    m.check_trust(host, guest)
    assert any(level == "FAIL" and area == "trust" for level, area, _ in m.FINDINGS), m.FINDINGS


def test_override_recorded_against_a_different_host_trust_does_not_apply(tmp_path):
    """The record is keyed to BOTH sides. An acceptance carried over from when the host was private
    (an exposure that did not then exist) must not satisfy the host being public now."""
    m = _mod()
    host, guest = _exposed(tmp_path)
    m.record_trust_override(host, "okpack-guest", "private", "private", "recorded pre-flip")
    m.check_trust(host, guest)
    assert any(level == "FAIL" and area == "trust" for level, area, _ in m.FINDINGS), m.FINDINGS


def test_hand_written_override_without_a_reason_is_ignored(tmp_path):
    """record_trust_override() cannot produce this; a hand-edited file can. An acceptance with no
    stated reason is not one."""
    m = _mod()
    host, guest = _exposed(tmp_path)
    path = host / m.OVERRIDES_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("trust_exposure:\n  okpack-guest:\n    guest_trust: private\n"
                    "    host_trust: public\n")
    m.check_trust(host, guest)
    assert any(level == "FAIL" and area == "trust" for level, area, _ in m.FINDINGS), m.FINDINGS


def test_override_for_another_pack_does_not_apply(tmp_path):
    m = _mod()
    host, guest = _exposed(tmp_path)
    m.record_trust_override(host, "okpack-somebody-else", "private", "public", "unrelated")
    m.check_trust(host, guest)
    assert any(level == "FAIL" and area == "trust" for level, area, _ in m.FINDINGS), m.FINDINGS


def test_override_without_a_reason_is_refused_and_writes_nothing(tmp_path):
    """An exposure accepted for no stated reason cannot be reviewed later, which is the only thing
    that makes a recorded override better than a bypass."""
    m = _mod()
    host, _ = _exposed(tmp_path)
    with pytest.raises(ValueError):
        m.record_trust_override(host, "okpack-guest", "private", "public", "   ")
    assert not (host / m.OVERRIDES_REL).exists()


def test_recorded_override_round_trips_and_keeps_earlier_entries(tmp_path):
    m = _mod()
    host, _ = _exposed(tmp_path)
    m.record_trust_override(host, "okpack-one", "private", "public", "first")
    m.record_trust_override(host, "okpack-two", "private", "public", "second")
    entries = m.load_overrides(host)["trust_exposure"]
    assert set(entries) == {"okpack-one", "okpack-two"}
    assert entries["okpack-one"]["reason"] == "first"
    assert entries["okpack-one"]["accepted_at"].endswith("Z")
    assert m.trust_override(host, "okpack-two", "private", "public")["reason"] == "second"
