"""An actor page must name the actor (okengine#846).

Every identity guard on the write path keys on the title: generic labels, geopolitical names,
software defined as an actor. A page with no title passes all of them. On one live vault 45 live
actor pages had no title or name -- `autonomous-ai-attacks`, `auto-color`, a publication.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\ntypes:\n  actor:\n    required: [type]\n  malware:\n"
        "    required: [type]\nstrict_types: false\n"
        "type_aliases: {threat-actor: actor, threat_actor: actor}\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 7, 15).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


@pytest.mark.parametrize("fm", [{"type": "actor"}, {"type": "actor", "title": ""},
                                {"type": "actor", "title": "  "}, {"type": "actor", "title": "---"}])
def test_an_actor_without_a_name_is_refused(ws, fm):
    mod, _vault = ws
    assert "must name the actor" in (mod._actor_admission_reject(fm, "", None) or "")


@pytest.mark.parametrize("fm", [{"type": "actor", "title": "Sandworm Team"},
                                {"type": "actor", "name": "Named By Name"},
                                {"type": "malware"}])
def test_a_named_actor_or_another_type_passes_this_check(ws, fm):
    mod, _vault = ws
    assert "must name the actor" not in (mod._actor_admission_reject(fm, "", None) or "")


def test_create_refuses_a_nameless_actor(ws):
    mod, vault = ws
    out = mod._create("entities/auto-color", {"type": "actor", "id": "entities:auto-color"}, "# x\n")
    assert "must name the actor" in out, out
    assert not list((vault / "wiki" / "entities").rglob("auto-color.md"))


_VULNERABILITY_BODY = (
    "A critical Windows driver vulnerability (CVE-2026-68820) that allows privilege "
    "escalation to SYSTEM has been patched by Microsoft. This vulnerability is "
    "actively being exploited in the wild."
)


@pytest.mark.parametrize("operation", ["create", "update", "converge", "patch"])
def test_actor_writes_refuse_opening_vulnerability_and_preserve_stored_bytes(ws, operation):
    mod, vault = ws
    page = mod._safe("entities/critical-threat")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "actor", "title": "Critical Threat", "id": "entities:critical-threat"}
    if operation != "create":
        page.write_text(mod._compose(fm, _VULNERABILITY_BODY), encoding="utf-8")
    before = page.read_bytes() if page.exists() else None
    if operation != "create":
        assert mod._safe("entities/critical-threat") == page
        assert mod._registry().resolve(fm["id"]) == page.relative_to(vault / "wiki").as_posix()

    if operation == "create":
        result = mod._create("entities/critical-threat", fm, _VULNERABILITY_BODY)
    elif operation == "update":
        result = mod._update("entities/critical-threat", {"recent_news": 2})
    elif operation == "converge":
        result = mod._converge("entities/critical-threat", fm)
    else:
        result = mod._patch("entities/critical-threat", "patched by Microsoft", "patched by the vendor")

    assert "not a threat actor" in result, result
    assert (page.read_bytes() if page.exists() else None) == before


@pytest.mark.parametrize("operation", ["update", "converge", "patch"])
def test_existing_actor_body_cannot_be_replaced_with_vulnerability_description(ws, operation):
    mod, vault = ws
    page = mod._safe("entities/critical-threat")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "actor", "title": "Critical Threat", "id": "entities:critical-threat"}
    actor_body = "Critical Threat is a threat group."
    page.write_text(mod._compose(fm, actor_body), encoding="utf-8")
    before = page.read_bytes()
    assert mod._safe("entities/critical-threat") == page
    assert mod._registry().resolve(fm["id"]) == page.relative_to(vault / "wiki").as_posix()

    if operation == "update":
        result = mod._update("entities/critical-threat", body=_VULNERABILITY_BODY)
    elif operation == "converge":
        result = mod._converge("entities/critical-threat", fm, _VULNERABILITY_BODY)
    else:
        result = mod._patch("entities/critical-threat", actor_body, _VULNERABILITY_BODY)

    assert "not a threat actor" in result, result
    assert page.read_bytes() == before


@pytest.mark.parametrize("operation", ["update", "converge", "patch"])
def test_retyping_nonactor_vulnerability_into_actor_is_refused(ws, operation):
    mod, vault = ws
    page = mod._safe("entities/critical-threat")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "malware", "title": "Critical Threat", "id": "entities:critical-threat"}
    page.write_text(mod._compose(fm, _VULNERABILITY_BODY), encoding="utf-8")
    before = page.read_bytes()
    assert mod._safe("entities/critical-threat") == page
    assert mod._registry().resolve(fm["id"]) == page.relative_to(vault / "wiki").as_posix()

    if operation == "update":
        result = mod._update("entities/critical-threat", {"type": "actor"})
    elif operation == "converge":
        result = mod._converge("entities/critical-threat", {**fm, "type": "actor"})
    else:
        result = mod._patch("entities/critical-threat", "type: malware", "type: actor")

    assert "not a threat actor" in result, result
    assert page.read_bytes() == before


@pytest.mark.parametrize("title,body", [
    ("Critical Threat", "Critical Threat is a threat group. " + _VULNERABILITY_BODY),
    ("XY", "XY exploited a critical Windows vulnerability."),
    ("Silence", "Silence is a crew targeting mailboxes."),
    ("Silence", "A critical Windows vulnerability was exploited in the wild. Silence is a threat group targeting mailboxes."),
    ("Imported One", "Imported identity; subtype unknown."),
])
def test_named_and_unknown_actor_admission_remains_compatible(ws, title, body):
    mod, _vault = ws
    assert mod._actor_admission_reject(
        {"type": "actor", "title": title, "actor_type": "unknown"}, body
    ) is None


def test_vulnerability_can_be_corrected_out_of_actor_type(ws):
    mod, vault = ws
    page = mod._safe("entities/critical-threat")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "actor", "title": "Critical Threat", "id": "entities:critical-threat"}
    page.write_text(mod._compose(fm, _VULNERABILITY_BODY), encoding="utf-8")

    result = mod._update("entities/critical-threat", {"type": "malware"})

    assert result.startswith("updated"), result
    corrected, body = mod._read_page(page)
    assert corrected["type"] == "malware"
    assert body.strip() == _VULNERABILITY_BODY



def test_valid_existing_actor_converges_into_seeded_canonical_page(ws):
    mod, vault = ws
    page = mod._safe("entities/critical-threat")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    fm = {"type": "actor", "title": "Critical Threat", "id": "entities:critical-threat"}
    page.write_text(mod._compose(fm, "Critical Threat is a threat group."), encoding="utf-8")
    assert mod._registry().resolve(fm["id"]) == page.relative_to(vault / "wiki").as_posix()

    result = mod._converge("entities/critical-threat", fm)

    assert result.startswith("converged into"), result
    assert list((vault / "wiki/entities").rglob("critical-threat.md")) == [page]



@pytest.mark.parametrize("alias", ["threat-actor", "threat_actor"])
def test_create_rejects_normalized_actor_alias_vulnerability(ws, alias):
    mod, vault = ws
    result = mod._create(
        "entities/critical-threat",
        {"type": alias, "title": "Critical Threat", "id": "entities:critical-threat"},
        _VULNERABILITY_BODY,
    )
    assert "not a threat actor" in result, result
    assert not list((vault / "wiki/entities").rglob("critical-threat.md"))


@pytest.mark.parametrize("alias", ["threat-actor", "threat_actor"])
def test_create_stores_supported_actor_alias_as_canonical_actor(ws, alias):
    mod, _vault = ws
    result = mod._create(
        "entities/critical-threat",
        {"type": alias, "title": "Critical Threat", "id": "entities:critical-threat",
         "sources": [_cite(mod, "Critical Threat")]},
        "Critical Threat is a threat group.",
    )
    assert result.startswith("created"), result
    page = mod._safe("entities/critical-threat")
    assert page is not None
    fm, _body = mod._read_page(page)
    assert fm["type"] == "actor"



def test_create_matching_actor_heading_cannot_hide_vulnerability(ws):
    mod, vault = ws
    result = mod._create(
        "entities/critical-threat",
        {"type": "actor", "title": "Critical Threat", "id": "entities:critical-threat"},
        "# Critical Threat\n\n" + _VULNERABILITY_BODY,
    )
    assert "not a threat actor" in result, result
    assert not list((vault / "wiki/entities").rglob("critical-threat.md"))


def _source_page(mod, ref, body):
    page = mod._safe(ref)
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(mod._compose({"type": "source", "title": "Original report"}, body), encoding="utf-8")


@pytest.mark.parametrize("operation", ["create", "update", "converge", "patch"])
def test_fabricated_actor_profile_cannot_override_resolved_vulnerability_source(ws, operation):
    mod, vault = ws
    ref = "sources/2026/08/11/driver-report"
    _source_page(mod, ref, "# Original report\n\n" + _VULNERABILITY_BODY + "\n\n## Actors named in this report\n- [[entities/critical-threat]]\n")
    fm = {"type": "actor", "title": "Critical Threat", "id": "entities:critical-threat", "sources": [ref]}
    body = "Critical Threat is a criminal group."
    page = mod._safe("entities/critical-threat")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    if operation != "create":
        page.write_text(mod._compose(fm, body), encoding="utf-8")
        assert mod._registry().resolve(fm["id"]) == page.relative_to(vault / "wiki").as_posix()
    before = page.read_bytes() if page.exists() else None
    if operation == "create":
        result = mod._create("entities/critical-threat", fm, body)
    elif operation == "update":
        result = mod._update("entities/critical-threat", {"recent_news": 2})
    elif operation == "converge":
        result = mod._converge("entities/critical-threat", fm)
    else:
        result = mod._patch("entities/critical-threat", "criminal group", "threat group")
    assert "not a threat actor" in result, result
    assert (page.read_bytes() if page.exists() else None) == before


@pytest.mark.parametrize("title,source_body,aliases", [
    ("Critical Threat", "Critical Threat is a criminal group.", []),
    ("Critical Threat", "The report provides no identity information.", []),
    ("Silence", _VULNERABILITY_BODY, ["Critical Threat"]),
    ("Imported One", _VULNERABILITY_BODY, []),
])
def test_source_check_preserves_actual_named_agents_unknowns_and_canonical_names(ws, title, source_body, aliases):
    mod, _vault = ws
    ref = "sources/2026/08/11/context-report"
    _source_page(mod, ref, source_body)
    fm = {"type": "actor", "title": title, "sources": [ref], "aliases": aliases}
    assert mod._actor_admission_reject(fm, title + " is a criminal group.") is None


@pytest.mark.parametrize("positive_first", [False, True])
def test_actual_named_source_agent_overrides_other_ambiguous_citations(ws, positive_first):
    mod, _vault = ws
    negative = "sources/2026/08/11/vulnerability"
    positive = "sources/2026/08/11/actor"
    _source_page(mod, negative, _VULNERABILITY_BODY)
    _source_page(mod, positive, "Critical Threat is a threat group targeting mailboxes.")
    refs = [positive, negative] if positive_first else [negative, positive]
    assert mod._actor_admission_reject(
        {"type": "actor", "title": "Critical Threat", "sources": refs},
        "Critical Threat is a criminal group.",
    ) is None


@pytest.mark.parametrize("ref", [None, 42, "sources/missing", "entities/other", "../../outside", "/etc/passwd"])
def test_unresolvable_or_non_source_reference_is_not_contrary_evidence(ws, ref):
    mod, _vault = ws
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": [ref]}) is None


@pytest.mark.parametrize("contents", [b"\xff", b"---\ntitle: [broken\n---\ntext\n"])
def test_unreadable_source_is_not_negative_identity_proof(ws, contents):
    mod, _vault = ws
    page = mod._safe("sources/unreadable")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(contents)
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": ["sources/unreadable"]}) is None


def test_qualified_canonical_source_namespace_is_inspected(ws):
    mod, vault = ws
    container = vault / "wiki" / "research"
    container.mkdir()
    (container / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    ref = "research/sources/report"
    _source_page(mod, ref, "The vulnerability poses a critical threat to organizations.")
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": [ref]}) == "vulnerability"


def test_ordinary_actor_unreadable_source_is_not_negative_identity_proof(ws):
    mod, _vault = ws
    # Unreadable original evidence remains unknown for ordinary names too.
    page = mod._safe("sources/broken")
    assert page is not None
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"\xff")
    assert mod._actor_source_identity_error({"title": "Silence", "sources": ["sources/broken"]}) is None


@pytest.mark.parametrize("ref", ["entities/not-a-source", "zzz/not-a-source"])
def test_existing_non_source_prose_cannot_supply_negative_source_identity(ws, ref):
    mod, _vault = ws
    _source_page(mod, ref, _VULNERABILITY_BODY)
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": [ref]}) is None


def test_source_unmatched_display_heading_is_not_discarded(ws):
    mod, _vault = ws
    ref = "sources/other-heading"
    _source_page(mod, ref, "# Unrelated heading\n\n" + _VULNERABILITY_BODY)
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": [ref]}) is None


def test_source_identity_uses_supported_actor_name_fallback(ws):
    mod, _vault = ws
    ref = "sources/name-only"
    _source_page(mod, ref, _VULNERABILITY_BODY)
    assert mod._actor_source_identity_error({"name": "Critical Threat", "sources": [ref]}) == "vulnerability"


def test_invalid_source_entry_does_not_hide_later_contrary_source(ws):
    mod, _vault = ws
    ref = "sources/valid-after-invalid"
    _source_page(mod, ref, _VULNERABILITY_BODY)
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": [None, ref]}) == "vulnerability"


def test_unreadable_source_does_not_hide_later_contrary_source(ws):
    mod, _vault = ws
    invalid = mod._safe("sources/invalid-encoding")
    assert invalid is not None
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(b"\xff")
    valid = "sources/valid-after-unreadable"
    _source_page(mod, valid, _VULNERABILITY_BODY)
    assert mod._actor_source_identity_error({"title": "Critical Threat", "sources": ["sources/invalid-encoding", valid]}) == "vulnerability"


@pytest.mark.parametrize("suffix", [
    "Morgana exploited it to target banks.",
    "The group named Morgana targeted banks.",
])
@pytest.mark.parametrize("heading", ["", "# Morgana\n\n"])
def test_create_and_update_preserve_vulnerability_led_named_actor_activity(ws, suffix, heading):
    mod, _vault = ws
    body = heading + "A critical Windows driver vulnerability was patched. " + suffix
    fm = {"type": "actor", "title": "Morgana", "actor_type": "unknown", "id": "entities:morgana",
          "sources": [_cite(mod, "Morgana")]}
    created = mod._create("entities/morgana", fm, body)
    assert created.startswith("created"), created
    page = mod._safe("entities/morgana")
    stored, stored_body = mod._read_page(page)
    assert stored["type"] == "actor"
    assert stored["actor_type"] == "unknown"
    assert stored_body.strip() == body
    updated = mod._update("entities/morgana", {"recent_news": 2})
    assert updated.startswith("updated"), updated
    stored, stored_body = mod._read_page(page)
    assert stored["recent_news"] == 2
    assert stored_body.strip() == body


@pytest.mark.parametrize("operation", ["create", "update", "converge", "patch", "append"])
@pytest.mark.parametrize("kind", ["actor", "threat-actor", "threat_actor"])
def test_original_victim_source_refuses_fabricated_actor_on_every_write(ws, operation, kind):
    mod, vault = ws
    ref = "sources/hospitality-report"
    _source_page(mod, ref, "# Original report\nHarbor Hotel Group suffered a data breach. Nightlock is a ransomware group.")
    fm = {"type": kind, "title": "Harbor Hotel Group", "id": "entities:harbor-hotel-group",
          "sources": [ref], "actor_type": "cybercriminal", "actor_identity_validated": True,
          "recent_news": 9}
    body = "Harbor Hotel Group is a cybercriminal intrusion set.\n\n## Recent activity\n"
    page = mod._safe("entities/harbor-hotel-group")
    page.parent.mkdir(parents=True, exist_ok=True)
    if operation != "create":
        # Exercise legacy persisted aliases against the actual governing schema.
        page.write_text(mod._compose(fm, body))
        assert mod._registry().resolve(fm["id"]) == page.relative_to(vault / "wiki").as_posix()
    before = page.read_bytes() if page.exists() else None
    if operation == "create":
        result = mod._create("entities/harbor-hotel-group", fm, body)
    elif operation == "update":
        result = mod._update("entities/harbor-hotel-group", {"type": kind, "recent_news": 10})
    elif operation == "converge":
        result = mod._converge("entities/harbor-hotel-group", fm)
    elif operation == "patch":
        result = mod._patch("entities/harbor-hotel-group", "cybercriminal intrusion set", "criminal group")
    else:
        result = mod._append_section("entities/harbor-hotel-group", "Recent activity", "Claimed an attack.")
    assert "victim organization" in result, result
    assert (page.read_bytes() if page.exists() else None) == before


def test_victim_source_does_not_hide_real_attacker_or_supported_canonical_actor(ws):
    mod, _vault = ws
    _source_page(mod, "sources/incident", "Harbor Hotel Group suffered a breach. Nightlock is a ransomware group.")
    for title, aliases in [("Nightlock", ["Harbor Hotel Group"]), ("Unfamiliar Cluster", [])]:
        assert mod._actor_admission_reject({"type": "actor", "title": title, "sources": ["sources/incident"],
                                          "aliases": aliases}, title + " is a criminal group.") is None
    _source_page(mod, "sources/independent", "Harbor Hotel Group is a cybercriminal group.")
    assert mod._actor_admission_reject({"type": "actor", "title": "Harbor Hotel Group",
        "sources": ["sources/missing", "sources/incident", "sources/independent"]},
        "Harbor Hotel Group is a criminal group.") is None


def test_ordinary_victim_source_singular_qualified_and_missing_inputs(ws):
    mod, vault = ws
    container = vault / "wiki/research"
    container.mkdir()
    (container / "schema.yaml").write_text("types: {}\n")
    _source_page(mod, "research/sources/report", "Harbor Hotel Group suffered a breach.")
    assert mod._actor_source_identity_error({"title": "Harbor Hotel Group", "source": "research/sources/report.md"}) == "victim organization"
    assert mod._actor_source_identity_error({"title": "Harbor Hotel Group", "sources": ["sources/missing", "research/sources/report"]}) == "victim organization"
    assert mod._actor_source_identity_error({"title": "Harbor Hotel Group", "sources": [], "source": "research/sources/report"}) is None


@pytest.mark.parametrize('operation', ['update', 'append'])
@pytest.mark.parametrize('title,description', [
    ('Lazarus', 'North Korean state-sponsored hacking group'),
    ('LockBit', 'Russian-speaking ransomware group'),
    ('Unfamiliar Cluster', 'APT group'),
    ('Harbor Hotel Group', 'extortion gang'),
])
def test_source_described_dual_role_actor_can_be_maintained(ws, operation, title, description):
    mod, _vault = ws
    _source_page(mod, 'sources/identity', f'{title} is an {description}.')
    _source_page(mod, 'sources/victim', f'{title} was attacked by a rival.')
    fm = {'type': 'actor', 'title': title, 'id': 'entities:described-actor',
          'sources': ['sources/victim', 'sources/identity'], 'actor_type': 'unknown'}
    body = title + ' is a criminal group.\n\n## Recent activity\nPrior report.\n'
    page = mod._safe('entities/described-actor')
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(mod._compose(fm, body))
    if operation == 'update':
        result = mod._update('entities/described-actor', {'recent_news': 2})
        assert result.startswith('updated'), result
        stored, stored_body = mod._read_page(page)
        assert stored['recent_news'] == 2
        assert stored_body.strip() == body.strip()
    else:
        result = mod._append_section('entities/described-actor', 'Recent activity', 'New sourced report.')
        assert not result.startswith('rejected'), result
        stored, stored_body = mod._read_page(page)
        assert 'New sourced report.' in stored_body
        assert 'Prior report.' in stored_body
    assert stored['type'] == 'actor'
    assert stored['actor_type'] == 'unknown'
    assert stored['sources'] == fm['sources']


def _cite(m, name):
    """A canonical source that names `name` as a threat group, so creating it is grounded in
    evidence rather than in the page's own prose (okengine#1012)."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    ref = f"sources/2026/01/01/{slug}-report"
    page = m._wiki() / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: source\ntitle: {name} report\n---\n\n"
                    f"{name} is a threat group that targeted banks.\n", encoding="utf-8")
    return ref
