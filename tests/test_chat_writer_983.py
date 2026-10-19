"""okengine#983: the Chat agent (Hermes api_server) writes through its own actor-bound writer.

The reader/cockpit Chat tab used to write through the generic `okengine-write` server, which has
no OKENGINE_WRITE_ACTOR, so the write path treated it as admin: no capability, no namespace limit,
no review. It now writes as `chat:api_server`, held to the engine floor in the policy catalog
(create/update/append in entities/ and concepts/, frontmatter-only updates, no provenance,
handling, review or attestation fields), and every chat write is queued for human review.

These tests drive the real write path with the real engine catalog, through the env-bound actor
exactly as the gateway launches the `okengine-write-chat` process.
"""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  actor:
    required: [type, title]
  concept:
    required: [type]
strict_types: false
"""

_SALT = """\
---
type: actor
id: G1045
title: Salt Typhoon
aliases: [GhostEmperor]
tlp: AMBER
---

Curated body written by the actor lanes.

## Activity

- 2025: telecom intrusions
"""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, WRITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities" / "s").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "wiki" / "entities" / "s" / "salt-typhoon.md").write_text(_SALT, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-10-03")
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    return tmp_path


@pytest.fixture
def chat(vault, monkeypatch):
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "chat:api_server")
    return _load(f"ws_chat_{id(vault)}"), vault


@pytest.fixture
def admin(vault, monkeypatch):
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    return _load(f"ws_admin_{id(vault)}"), vault


def _fm(root: Path, rel: str) -> dict:
    return yaml.safe_load((root / "wiki" / rel).read_text(encoding="utf-8").split("---")[1])


def _queue(root: Path) -> str:
    q = root / "wiki" / "_review-queue.md"
    return q.read_text(encoding="utf-8") if q.exists() else ""


# --- the issue reproduction -----------------------------------------------------------------


def test_salt_typhoon_style_frontmatter_update_lands_queued_for_review(chat):
    """The one live chat write (okcti-test, 2026-07-07) echoed the whole frontmatter and added
    related actors to Salt Typhoon. It still lands, but now needs a human disposition."""
    m, root = chat
    out = m._update(
        "entities/s/salt-typhoon",
        "type: actor\nid: G1045\ntitle: Salt Typhoon\naliases: [GhostEmperor]\ntlp: AMBER\n"
        "related_actors: [FIN7, OilRig]\n",
    )
    assert out.startswith("updated"), out
    assert "flagged for review" in out, out
    fm = _fm(root, "entities/s/salt-typhoon.md")
    assert fm["related_actors"] == ["FIN7", "OilRig"]
    assert fm["needs_review"] is True
    assert "salt-typhoon" in _queue(root)
    assert "chat:api_server requires human disposition" in _queue(root)


def test_the_chat_writer_exposes_only_its_three_tools(chat):
    m, _root = chat
    if m.mcp is None:  # pragma: no cover - mcp is a CI dependency
        pytest.skip("mcp not installed")
    names = {t.name for t in asyncio.run(m.mcp.list_tools())}
    assert names == {"create_entity", "update_entity", "append_to_section"}, names


# --- what chat may still do -----------------------------------------------------------------


def test_chat_creates_an_entity_with_a_body_and_it_is_queued(chat):
    m, root = chat
    out = m._create("entities/v/volt-typhoon",
                    f"type: actor\ntitle: Volt Typhoon\nsources: [{_cite(m, 'Volt Typhoon')}]\n",
                    "Grounded body.")
    assert out.startswith("created"), out
    assert _fm(root, "entities/v/volt-typhoon.md")["needs_review"] is True
    assert "volt-typhoon" in _queue(root)


def test_chat_creates_a_concept(chat):
    m, _root = chat
    out = m._create("concepts/e/edge-device-exploitation", "type: concept\n", "A pattern.")
    assert out.startswith("created"), out


def test_chat_appends_to_a_section_and_it_is_queued(chat):
    m, root = chat
    out = m._append_section("entities/s/salt-typhoon", "Activity", "- 2026: new reporting")
    assert out.startswith("appended"), out
    body = (root / "wiki" / "entities" / "s" / "salt-typhoon.md").read_text()
    assert "Curated body written by the actor lanes." in body
    assert "- 2026: new reporting" in body
    assert _fm(root, "entities/s/salt-typhoon.md")["needs_review"] is True


def test_an_unchanged_echo_of_a_protected_field_is_not_a_change(chat):
    """`tlp: AMBER` is echoed unchanged in a read-modify-write: no protected field moves."""
    m, _root = chat
    out = m._update("entities/s/salt-typhoon", "tlp: AMBER\ntitle: Salt Typhoon\n")
    assert out.startswith("updated"), out


# --- what chat may not do -------------------------------------------------------------------


def test_chat_may_not_replace_an_existing_body(chat):
    m, root = chat
    out = m._update("entities/s/salt-typhoon", None, "A rewritten body.")
    assert out.startswith("rejected:") and "body mutation is denied" in out, out
    assert "Curated body" in (root / "wiki" / "entities" / "s" / "salt-typhoon.md").read_text()


@pytest.mark.parametrize(
    "patch",
    [
        "tlp: CLEAR\n",
        "maintained_by: [some-pack]\n",
        "actor_identity_validated: true\n",
        "reviewed_by: someone\n",
        "rejected_aliases: [x]\n",
        "superseded_by: entities/x/other\n",
        "redirect_to: entities/x/other\n",
        "link_stub: true\n",
        "tombstoned: true\n",
        "canonical: other\n",
    ],
)
def test_chat_may_not_change_a_protected_field(chat, patch):
    m, root = chat
    out = m._update("entities/s/salt-typhoon", patch)
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    assert _fm(root, "entities/s/salt-typhoon.md")["tlp"] == "AMBER"


def test_chat_may_not_set_attestation_fields(chat):
    m, _root = chat
    out = m._update("entities/s/salt-typhoon", "local_only: true\n")
    assert out.startswith("rejected:"), out


def test_chat_may_not_retire_a_page_through_update(chat):
    m, root = chat
    out = m._update("entities/s/salt-typhoon", "status: tombstoned\n")
    assert out.startswith(("rejected:", "refused:")), out
    assert _fm(root, "entities/s/salt-typhoon.md").get("status") != "tombstoned"


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("_tombstone", ("entities/s/salt-typhoon", "chat says so")),
        ("_flag", ("entities/s/salt-typhoon", "chat note")),
        ("_patch", ("entities/s/salt-typhoon", "telecom intrusions", "something else")),
    ],
)
def test_chat_operations_outside_the_floor_are_refused(chat, call, args):
    m, root = chat
    out = getattr(m, call)(*args)
    assert out.startswith("rejected:") or "not allowed" in out, out
    text = (root / "wiki" / "entities" / "s" / "salt-typhoon.md").read_text()
    assert "status: tombstoned" not in text and "telecom intrusions" in text


@pytest.mark.parametrize(
    ("path", "fm"),
    [
        ("sources/2026/10/a-post", "type: source\ntitle: A post\npublished: 2026-10-01\n"),
        ("assessments/a/claim", "type: assessment\ntitle: Claim\n"),
        ("dashboards/x", "type: dashboard\ntitle: X\n"),
    ],
)
def test_chat_may_not_create_outside_entities_and_concepts(chat, path, fm):
    m, root = chat
    out = m._create(path, fm, "Body.")
    assert out.startswith("rejected:") and "outside allowed scopes" in out, out
    assert not (root / "wiki" / (path + ".md")).exists()


def test_chat_may_not_create_with_a_protected_field(chat):
    m, root = chat
    out = m._create("entities/n/forged", "type: actor\ntitle: Forged\nreviewed_by: alice\n", "B.")
    assert out.startswith("rejected:") and "protected fields" in out, out
    assert not (root / "wiki" / "entities" / "n" / "forged.md").exists()


# --- QA of !1241: type-strict and alias-aware change detection ------------------------------

_VALIDATED = """\
---
type: actor
id: G1017
title: Volt Typhoon
actor_identity_validated: {value}
---

Body.
"""


def _validated_page(root: Path, value: str) -> None:
    (root / "wiki" / "entities" / "v").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "entities" / "v" / "volt-typhoon.md").write_text(
        _VALIDATED.format(value=value), encoding="utf-8")


@pytest.mark.parametrize(("stored", "sent"),
                         [("false", "0"), ("true", "1"), ("true", "'true'"), ("false", "''")])
def test_a_type_change_of_a_protected_field_is_a_change(chat, stored, sent):
    """`0 == False` in Python; a guard that compared with `!=` let chat turn `false` into `0`."""
    m, root = chat
    _validated_page(root, stored)
    out = m._update("entities/v/volt-typhoon", f"actor_identity_validated: {sent}\n")
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    text = (root / "wiki" / "entities" / "v" / "volt-typhoon.md").read_text()
    assert f"actor_identity_validated: {stored}" in text


def test_a_type_identical_echo_of_a_protected_field_is_still_accepted(chat):
    m, root = chat
    _validated_page(root, "false")
    out = m._update("entities/v/volt-typhoon",
                    "actor_identity_validated: false\ntitle: Volt Typhoon\nsummary: x\n")
    assert out.startswith("updated"), out


def _aliased(root: Path) -> None:
    (root / "schema.yaml").write_text(
        _SCHEMA + "field_aliases:\n  marking: tlp\n  handling: sensitivity\n", encoding="utf-8")


@pytest.mark.parametrize("patch", ["marking: CLEAR\n", "handling: secret\n"])
def test_a_pack_field_alias_does_not_carry_a_protected_field_past_chat(chat, patch):
    m, root = chat
    _aliased(root)
    _validated_page(root, "false")
    out = m._update("entities/v/volt-typhoon", patch)
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    text = (root / "wiki" / "entities" / "v" / "volt-typhoon.md").read_text()
    assert "tlp:" not in text and "sensitivity:" not in text


def test_a_pack_field_alias_does_not_carry_a_protected_field_into_a_create(chat):
    m, root = chat
    _aliased(root)
    out = m._create("entities/s/storm-0558", "type: actor\ntitle: Storm-0558\nmarking: CLEAR\n",
                    "Body.")
    assert out.startswith("rejected:") and "protected fields" in out, out


def _chained(root: Path, aliases: str) -> None:
    (root / "schema.yaml").write_text(_SCHEMA + "field_aliases:\n" + aliases, encoding="utf-8")


def test_a_two_hop_alias_chain_does_not_carry_a_protected_field_into_a_create(chat):
    """QA round 2: `{hx: hy, hy: tlp}` renames hx to hy to tlp in one declaration-order pass."""
    m, root = chat
    _chained(root, "  hx: hy\n  hy: tlp\n")
    out = m._create("entities/s/storm-0558", "type: actor\ntitle: Storm-0558\nhx: CLEAR\n", "B.")
    assert out.startswith("rejected:") and "protected fields" in out, out
    assert not (root / "wiki" / "entities" / "s" / "storm-0558.md").exists()


def test_a_three_hop_alias_chain_does_not_carry_a_protected_field_into_an_update(chat):
    m, root = chat
    _chained(root, "  ha: hb\n  hb: hc\n  hc: sensitivity\n")
    out = m._update("entities/s/salt-typhoon", "ha: secret\n")
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    assert "sensitivity" not in (root / "wiki" / "entities" / "s" / "salt-typhoon.md").read_text()


def test_a_chain_declared_out_of_order_is_judged_where_the_write_lands_it(chat):
    """`{hy: tlp, hx: hy}`: the write's single pass stops hx at hy, so hy, not tlp, is set."""
    m, root = chat
    _chained(root, "  hy: tlp\n  hx: hy\n")
    out = m._update("entities/s/salt-typhoon", "hx: harmless\n")
    assert out.startswith("updated"), out
    fm = _fm(root, "entities/s/salt-typhoon.md")
    assert fm["hy"] == "harmless" and fm["tlp"] == "AMBER"


def test_a_stored_alias_the_write_renames_onto_a_protected_field_is_a_change(chat):
    """QA round 3: `{hy: tlp, hx: hy}`. Step 1 stores `hy` (the single pass stops hx at hy).
    Step 2 changes an unrelated field, but the write renames the STORED `hy` onto `tlp`: that
    materializes a protected field, so step 2 is refused and `tlp` never lands."""
    m, root = chat
    _chained(root, "  hy: tlp\n  hx: hy\n")
    _validated_page(root, "false")
    first = m._update("entities/v/volt-typhoon", "hx: CLEAR\n")
    assert first.startswith("updated"), first
    assert _fm(root, "entities/v/volt-typhoon.md")["hy"] == "CLEAR"
    second = m._update("entities/v/volt-typhoon", "related: [Storm-0558]\n")
    assert second.startswith("rejected:") and "protected fields would change" in second, second
    assert "tlp" not in _fm(root, "entities/v/volt-typhoon.md")


def test_a_canonically_keyed_page_echo_is_unchanged_under_aliases(chat):
    m, root = chat
    _chained(root, "  hy: tlp\n  hx: hy\n  marking: tlp\n")
    out = m._update("entities/s/salt-typhoon",
                    "type: actor\nid: G1045\ntitle: Salt Typhoon\naliases: [GhostEmperor]\n"
                    "tlp: AMBER\n")
    assert out.startswith("updated"), out


@pytest.mark.parametrize(("aliases", "fm", "expected"), [
    ({"marking": "tlp"}, {"marking": 1}, {"tlp": 1}),
    ({"marking": ""}, {"marking": 1}, {"marking": 1}),
    ({"marking": ["tlp"]}, {"marking": 1}, {"marking": 1}),
    ({"marking": None}, {"marking": 1}, {"marking": 1}),
    ({"marking": "tlp"}, {"marking": 1, "tlp": 2}, {"tlp": 2}),
    ({"marking": "tlp"}, {"marking": 1, "tlp": ""}, {"tlp": 1}),
    ({"a": "b", "b": "a"}, {"a": 1}, {"a": 1}),
])
def test_alias_renaming_matches_the_write(chat, aliases, fm, expected):
    m, _root = chat
    assert m._alias_renamed(fm, aliases) == expected


def test_frontmatter_changes_are_type_strict_and_two_sided(chat):
    m, root = chat
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    assert m._frontmatter_changes(page, {"a": False, "b": 1, 3: "x"}, {"a": 0, "c": 1, 3: "x"}) \
        == ["a", "b", "c"]
    assert m._frontmatter_changes(page, {"a": [1]}, {"a": [1]}) == []


def test_an_aliased_echo_of_the_stored_value_is_not_a_change(chat):
    m, root = chat
    _aliased(root)
    out = m._update("entities/s/salt-typhoon", "marking: AMBER\n")
    assert out.startswith("updated"), out


# --- operator writes are unchanged ----------------------------------------------------------


def test_operator_in_process_writes_stay_admin_and_unqueued(admin):
    m, root = admin
    assert m._caller()["kind"] == "admin"
    out = m._update("entities/s/salt-typhoon", None, "Operator rewrite.")
    assert out.startswith("updated") and "flagged" not in out, out
    out = m._create("sources/2026/10/op", "type: source\ntitle: Op\npublished: 2026-10-01\n", "Operator body.")
    assert out.startswith("created"), out
    assert "requires human disposition" not in _queue(root)


def test_a_stored_alias_the_write_renames_onto_attestation_is_refused_for_admin(admin):
    """#980 guard, same rule: the stored page is compared raw, so a stored `lo` that the write
    would rename onto `local_only` is an attestation change, even for an operator write."""
    m, root = admin
    (root / "schema.yaml").write_text(_SCHEMA + "field_aliases:\n  lo: local_only\n",
                                      encoding="utf-8")
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    page.write_text(_SALT.replace("tlp: AMBER\n", "tlp: AMBER\nlo: true\n"), encoding="utf-8")
    out = m._update("entities/s/salt-typhoon", "summary: unrelated\n")
    assert out.startswith("refused: local_only"), out
    assert "local_only" not in page.read_text()


def test_a_cron_actor_gets_no_chat_review_flag(vault, monkeypatch):
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:source-quality-backfill")
    m = _load(f"ws_cron_{id(vault)}")
    assert m._chat_review_flags() == []


# --- okengine#991 [13]: a channel echo of the STORED value is not a change -----------------

_REVIEWED = """\
---
type: actor
id: G1046
title: Storm-0558
reviewed_on: 2026-09-01
rejected_aliases: Storm-0559
actor_identity_validated: false
---

Body.
"""


def _reviewed_page(root: Path) -> Path:
    page = root / "wiki" / "entities" / "s" / "storm-0558.md"
    page.write_text(_REVIEWED, encoding="utf-8")
    return page


def _echo(**changes) -> dict:
    """The whole stored frontmatter as a tool sends it through JSON, where a date is a string."""
    fm = {"type": "actor", "id": "G1046", "title": "Storm-0558", "reviewed_on": "2026-09-01",
          "rejected_aliases": "Storm-0559", "actor_identity_validated": False}
    fm.update(changes)
    return fm


def test_a_json_echo_of_a_stored_date_in_a_protected_field_is_not_a_change(chat):
    m, root = chat
    _reviewed_page(root)
    out = m._update("entities/s/storm-0558", _echo(summary="New context."))
    assert out.startswith("updated"), out
    assert _fm(root, "entities/s/storm-0558.md")["summary"] == "New context."


@pytest.mark.parametrize(("key", "stored", "sent"), [
    ("reviewed_on", datetime.date(2026, 9, 1), "2026-09-01"),
    ("reviewed_at", datetime.datetime(2026, 9, 1, 10, 30), "2026-09-01 10:30:00"),
    ("rejected_aliases", "Storm-0559", ["Storm-0559"]),
    ("successor_hint", "[[entities/x/y]]", "entities/x/y"),
])
def test_an_echo_keeps_the_stored_value_and_type(chat, key, stored, sent):
    m, root = chat
    page = _reviewed_page(root)
    assert m._stored_echoes(page, {key: stored}, {key: sent}) == {key: stored}


@pytest.mark.parametrize(("key", "stored", "sent"), [
    ("reviewed_on", datetime.date(2026, 9, 1), "2026-09-02"),
    ("reviewed_on", datetime.date(2026, 9, 1), "a: b: c"),
    ("reviewed_on", datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)),
    ("actor_identity_validated", False, 0),
    ("actor_identity_validated", False, "false"),
    ("rejected_aliases", "Storm-0559", ["Storm-0560"]),
])
def test_a_real_change_is_not_taken_for_an_echo(chat, key, stored, sent):
    m, root = chat
    page = _reviewed_page(root)
    assert m._stored_echoes(page, {key: stored}, {key: sent, "new": 1}) == {key: sent, "new": 1}


def test_an_echo_of_a_legacy_scalar_list_field_is_not_a_change(chat):
    """`rejected_aliases: Storm-0559` is stored as a scalar; the write path coerces the echo to a
    list, which is what it would make of the stored value too. Not a change, so a protected field
    passes; and the write stores the canonical shape it was sent (okengine#994 [19]), where #991
    kept the legacy scalar."""
    m, root = chat
    _reviewed_page(root)
    out = m._update("entities/s/storm-0558", "rejected_aliases: Storm-0559\nsummary: x\n")
    assert out.startswith("updated"), out
    assert _fm(root, "entities/s/storm-0558.md")["rejected_aliases"] == ["Storm-0559"]


@pytest.mark.parametrize("changes", [
    {"reviewed_on": "2026-09-02"},
    {"reviewed_on": "not a date"},
    {"reviewed_on": "2026-09-01: x"},
    {"rejected_aliases": "Storm-0560"},
    {"actor_identity_validated": 0},
    {"actor_identity_validated": "false"},
])
def test_a_json_write_that_really_changes_a_protected_field_is_still_refused(chat, changes):
    m, root = chat
    _reviewed_page(root)
    out = m._update("entities/s/storm-0558", _echo(**changes))
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    assert _fm(root, "entities/s/storm-0558.md")["actor_identity_validated"] is False


# --- okengine#994 [19]: a correct repair of a legacy shape is stored, not discarded -----------

_LEGACY = """\
---
type: actor
id: G1047
title: Storm-0600
rejected_aliases: Storm-0601, Storm-0602
successor_hint: "[[entities/x/y]]"
---

Body.
"""


def _legacy_page(root: Path, text: str = _LEGACY) -> Path:
    page = root / "wiki" / "entities" / "s" / "storm-0600.md"
    page.write_text(text, encoding="utf-8")
    return page


def test_a_legacy_scalar_repaired_to_the_canonical_list_is_stored_canonical(chat):
    """The repair a lane sends for a legacy scalar list field is the canonical list of the same
    names. It changes no value, so even a protected field accepts it, and the page ends canonical."""
    m, root = chat
    _legacy_page(root)
    out = m._update("entities/s/storm-0600",
                    {"rejected_aliases": ["Storm-0601", "Storm-0602"], "summary": "x"})
    assert out.startswith("updated"), out
    fm = _fm(root, "entities/s/storm-0600.md")
    assert fm["rejected_aliases"] == ["Storm-0601", "Storm-0602"], fm
    assert fm["summary"] == "x"


def test_a_legacy_wikilink_value_repaired_to_the_plain_path_is_stored_canonical(admin):
    m, root = admin
    _legacy_page(root)
    out = m._update("entities/s/storm-0600", {"successor_hint": "entities/x/y"})
    assert out.startswith("updated"), out
    assert _fm(root, "entities/s/storm-0600.md")["successor_hint"] == "entities/x/y"


@pytest.mark.parametrize("sent", [
    ["Storm-0601"],                          # drops a stored name
    [],                                      # drops every stored name
    ["Storm-0601", "Storm-0602", "Storm-0603"],  # adds a name
])
def test_a_repair_that_changes_the_data_is_still_a_protected_field_change(chat, sent):
    """Negative fixture: only the canonical shape of the SAME data is a repair. A list that loses
    or adds a name is a change to a protected field, refused, and the page is left as stored."""
    m, root = chat
    page = _legacy_page(root)
    out = m._update("entities/s/storm-0600", {"rejected_aliases": sent, "summary": "x"})
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    assert page.read_text(encoding="utf-8") == _LEGACY


def test_a_stale_echo_of_an_older_value_is_still_a_change(chat):
    """Negative fixture: a read-modify-write that echoes what the field held BEFORE its last write
    is not an echo of the stored value. Judged as a change, a protected field refuses it."""
    m, root = chat
    page = _legacy_page(root)
    out = m._update("entities/s/storm-0600",
                    {"rejected_aliases": ["Storm-0599"], "summary": "x"})
    assert out.startswith("rejected:") and "protected fields would change" in out, out
    assert page.read_text(encoding="utf-8") == _LEGACY


@pytest.mark.parametrize(("key", "stored", "sent", "kept"), [
    ("reviewed_on", datetime.date(2026, 9, 1), "2026-09-01", datetime.date(2026, 9, 1)),
    ("rejected_aliases", "Storm-0559", ["Storm-0559"], ["Storm-0559"]),
    ("successor_hint", "[[entities/x/y]]", "entities/x/y", "entities/x/y"),
    ("rejected_aliases", "Storm-0559", ["Storm-0560"], ["Storm-0560"]),
    # operator-owned attestation (okengine#980): an echo keeps the stored value byte for byte
    ("operator_evidence_refs", "[[sources/a]]", "sources/a", "[[sources/a]]"),
    ("dataset", "[[d/x]]", "d/x", "[[d/x]]"),
])
def test_the_stored_patch_keeps_a_date_and_attestation_but_stores_a_repair(
        chat, key, stored, sent, kept):
    m, root = chat
    page = _reviewed_page(root)
    assert m._stored_repairs(page, {key: stored}, {key: sent, "new": 1}) == {key: kept, "new": 1}


def test_the_record_identity_of_an_attested_page_is_kept_exactly(chat):
    m, root = chat
    page = _reviewed_page(root)
    stored = {"local_only": True, "publisher": "[[entities/p/q]]"}
    assert m._stored_repairs(page, stored, {"publisher": "entities/p/q"}) == {
        "publisher": "[[entities/p/q]]"}
    # the same field on a page with no attestation is ordinary data, repaired
    assert m._stored_repairs(page, {"publisher": "[[entities/p/q]]"},
                             {"publisher": "entities/p/q"}) == {"publisher": "entities/p/q"}


def test_an_alias_spelling_of_an_attestation_field_is_kept_exactly(chat):
    m, root = chat
    (root / "schema.yaml").write_text(_SCHEMA + "field_aliases:\n  oer: operator_evidence_refs\n",
                                      encoding="utf-8")
    page = _reviewed_page(root)
    assert m._stored_repairs(page, {"oer": "[[sources/a]]"}, {"oer": "sources/a"}) == {
        "oer": "[[sources/a]]"}


def test_an_alias_named_like_an_attestation_field_stays_frozen(chat):
    """An alias key that is itself an attestation field, renamed onto another, is still frozen."""
    m, root = chat
    (root / "schema.yaml").write_text(_SCHEMA + "field_aliases:\n  dataset: evidence_role\n",
                                      encoding="utf-8")
    page = _reviewed_page(root)
    assert m._stored_repairs(page, {"dataset": "[[d/x]]"}, {"dataset": "d/x"}) == {
        "dataset": "[[d/x]]"}


def test_an_admin_echo_of_a_legacy_attestation_ref_is_not_refused_and_kept(admin):
    """A whole-frontmatter echo through update must not trip #980 on a legacy-shaped operator
    field: it is judged no change and stored exactly as it was."""
    m, root = admin
    text = _LEGACY.replace("successor_hint", "operator_evidence_refs: \"[[sources/a]]\"\nx_hint")
    _legacy_page(root, text)
    out = m._update("entities/s/storm-0600",
                    {"operator_evidence_refs": "sources/a", "summary": "z"})
    assert out.startswith("updated"), out
    assert _fm(root, "entities/s/storm-0600.md")["operator_evidence_refs"] == "[[sources/a]]"


_DISCOVERED = """\
---
type: actor
id: G1048
title: Storm-0700
discovered_by: pack-a
---

Body.
"""


def _discovered_page(root: Path) -> Path:
    page = root / "wiki" / "entities" / "s" / "storm-0700.md"
    page.write_text(_DISCOVERED, encoding="utf-8")
    return page


def test_an_immutable_legacy_scalar_repaired_to_its_list_is_stored_and_unflagged(admin):
    """`discovered_by` is immutable, but `[pack-a]` is the same value as `pack-a`: the repair is
    stored and is not reported as a reverted immutable change (no needs_review)."""
    m, root = admin
    _discovered_page(root)
    out = m._update("entities/s/storm-0700", {"discovered_by": ["pack-a"], "summary": "s"})
    assert out.startswith("updated"), out
    fm = _fm(root, "entities/s/storm-0700.md")
    assert fm["discovered_by"] == ["pack-a"], fm
    assert "needs_review" not in fm, fm


def test_a_patch_elsewhere_on_a_legacy_immutable_page_is_not_flagged(admin):
    """patch_entity coerces the whole frontmatter, so it canonicalizes the untouched legacy
    `discovered_by`; that is not a forged immutable change."""
    m, root = admin
    _discovered_page(root)
    out = m._patch("entities/s/storm-0700", "title: Storm-0700", "title: Storm 0700")
    assert out.startswith("patched"), out
    fm = _fm(root, "entities/s/storm-0700.md")
    assert fm["discovered_by"] == ["pack-a"], fm
    assert "needs_review" not in fm, fm


def test_a_reshaped_immutable_key_does_not_stop_the_later_ones_being_guarded(admin):
    """A repaired `created_by` is kept; a forged `discovered_by` after it is still reverted."""
    m, root = admin
    page = _discovered_page(root)
    page.write_text(_DISCOVERED.replace("discovered_by", 'created_by: "[[pack-a]]"\ndiscovered_by'),
                    encoding="utf-8")
    out = m._update("entities/s/storm-0700",
                    {"created_by": "pack-a", "discovered_by": ["pack-z"]})
    assert out.startswith("updated"), out
    fm = _fm(root, "entities/s/storm-0700.md")
    assert fm["created_by"] == "pack-a" and fm["discovered_by"] == "pack-a", fm


@pytest.mark.parametrize("sent", [["pack-b"], ["pack-a", "pack-b"], []])
def test_a_real_change_to_an_immutable_legacy_scalar_is_still_reverted(admin, sent):
    """Negative fixture: a different value is a forged change, reverted to the stored value and
    flagged for review, exactly as before."""
    m, root = admin
    _discovered_page(root)
    out = m._update("entities/s/storm-0700", {"discovered_by": sent})
    assert out.startswith("updated") and "flagged for review" in out, out
    assert "immutable field change reverted: discovered_by" in _queue(root)
    fm = _fm(root, "entities/s/storm-0700.md")
    assert fm["discovered_by"] == "pack-a" and fm["needs_review"] is True, fm


# --- okengine#994 [19] QA: checks after the write compare against the CANONICAL stored page ----

_EVERY_LEGACY_SHAPE = """\
---
type: actor
id: G3000
title: Probe
aliases: APT1, Comment Crew
sources: sources/a, sources/missing
basis: sources/a, sources/b
discovered_by: pack-a, pack-b
successor_hint: "[[entities/x/y]]"
related: ['[[entities/z/z]]', entities/w/w]
---

Body.
"""
_LEGACY_SCALAR_ECHO = yaml.safe_load(_EVERY_LEGACY_SHAPE.split("---")[1])
_CANONICAL_ECHO = {**_LEGACY_SCALAR_ECHO,
                   "aliases": ["APT1", "Comment Crew"],
                   "sources": ["sources/a", "sources/missing"],
                   "basis": ["sources/a", "sources/b"],
                   "discovered_by": ["pack-a", "pack-b"],
                   "successor_hint": "entities/x/y",
                   "related": ["entities/z/z", "entities/w/w"]}


def _every_legacy_shape(root: Path) -> Path:
    (root / "schema.yaml").write_text(_SCHEMA + "field_shapes:\n  basis: list\n", encoding="utf-8")
    (root / "wiki" / "sources").mkdir(exist_ok=True)
    (root / "wiki" / "sources" / "a.md").write_text("---\ntype: source\n---\n", encoding="utf-8")
    (root / "wiki" / "sources" / "b.md").write_text("---\ntype: source\n---\n", encoding="utf-8")
    (root / "wiki" / "entities" / "p").mkdir(exist_ok=True)
    page = root / "wiki" / "entities" / "p" / "probe.md"
    page.write_text(_EVERY_LEGACY_SHAPE, encoding="utf-8")
    return page


def _no_side_effects(root: Path, out: str, verb: str) -> dict:
    assert out.startswith(verb), out
    assert "flagged for review" not in out, out
    fm = _fm(root, "entities/p/probe.md")
    assert "rejected_aliases" not in fm, f"an echo recorded a removed alias: {fm}"
    assert "needs_review" not in fm, fm
    assert _queue(root) == "", _queue(root)
    return fm


@pytest.mark.parametrize("echo", [_LEGACY_SCALAR_ECHO, _CANONICAL_ECHO],
                         ids=["legacy-spelling", "canonical-spelling"])
def test_an_update_echo_of_every_legacy_shape_has_no_side_effect(admin, echo):
    """B1/B2 (QA of !1278): the stored scalar `aliases` must not read as a removed alias, and a
    stored dangling source must not read as a NEW unresolved one, once the write stores the
    canonical list. Every legacy shape lands canonical, and nothing else happens."""
    m, root = admin
    _every_legacy_shape(root)
    fm = _no_side_effects(root, m._update("entities/p/probe", {**echo, "summary": "s"}), "updated")
    for key in ("aliases", "sources", "basis", "discovered_by", "successor_hint", "related"):
        assert fm[key] == _CANONICAL_ECHO[key], (key, fm[key])


def test_a_chat_update_echo_of_every_legacy_shape_has_no_side_effect(chat):
    m, root = chat
    _every_legacy_shape(root)
    out = m._update("entities/p/probe", {**_LEGACY_SCALAR_ECHO, "summary": "s"})
    assert out.startswith("updated"), out
    fm = _fm(root, "entities/p/probe.md")
    assert "rejected_aliases" not in fm, fm


def test_a_patch_elsewhere_on_a_page_of_every_legacy_shape_has_no_side_effect(admin):
    """patch_entity canonicalizes the whole frontmatter, so the same comparisons apply."""
    m, root = admin
    _every_legacy_shape(root)
    fm = _no_side_effects(root, m._patch("entities/p/probe", "Body.", "Body two."), "patched")
    assert fm["aliases"] == ["APT1", "Comment Crew"] and fm["sources"][1] == "sources/missing"


def test_an_alias_repair_records_no_removed_alias(admin):
    m, root = admin
    _every_legacy_shape(root)
    _no_side_effects(root, m._update("entities/p/probe", {"aliases": ["APT1", "Comment Crew"]}),
                     "updated")


def test_dropping_a_canonical_alias_still_records_it_as_removed(admin):
    """Negative fixture: comparing against the canonical stored page still sees a real removal."""
    m, root = admin
    _every_legacy_shape(root)
    out = m._update("entities/p/probe", {"aliases": ["APT1"]})
    assert out.startswith("updated"), out
    assert _fm(root, "entities/p/probe.md")["rejected_aliases"] == ["Comment Crew"]


def test_a_new_dangling_source_is_still_refused(admin):
    """Negative fixture: the stored dangling ref is grandfathered; a NEW one is still refused."""
    m, root = admin
    page = _every_legacy_shape(root)
    out = m._update("entities/p/probe",
                    {"sources": ["sources/a", "sources/missing", "sources/also-missing"]})
    assert out.startswith("rejected") and "sources/also-missing" in out, out
    assert page.read_text(encoding="utf-8") == _EVERY_LEGACY_SHAPE


def test_the_canonical_view_falls_back_to_raw_when_the_write_path_refuses_it(admin):
    m, root = admin
    page = _every_legacy_shape(root)
    stored = {"title": "\ud800", "aliases": "a, b"}
    assert m._canonical_view(page, stored) is stored
    assert m._canonical_view(page, {"aliases": "a, b"}) == {"aliases": ["a", "b"]}


def test_an_immutable_value_of_another_type_is_a_change(admin):
    """`_preserve_immutable` compares type-strictly (#983): `1` over `true` is reverted."""
    m, root = admin
    page = root / "wiki" / "entities" / "s" / "storm-0700.md"
    page.write_text(_DISCOVERED.replace("id: G1048", "id: 1"), encoding="utf-8")
    m._update("entities/s/storm-0700", {"id": True})
    fm = _fm(root, "entities/s/storm-0700.md")
    assert fm["id"] == 1 and fm["id"] is not True and fm["needs_review"] is True, fm


# --- okengine#991 [11]: the floor protects the keys that decide what a page IS ----------------

# `status` is also a content field (`active`, `draft`, `monitoring`, ...) that a chat create
# legitimately sets, so the floor cannot protect it wholesale; a value-level rule for retirement
# statuses is an open decision on okengine#991. `tombstoned` is refused by the write path's own
# tombstone guard regardless.
_PAGE_IDENTITY_KEYS_LEFT_TO_STATUS_RULES = {"status"}


def test_the_chat_floor_protects_every_key_that_decides_what_a_page_is(chat):
    """converge's `_NO_ADD_KEYS` are the keys that make a page a redirect, a stub, retired or
    canonical for something else. A chat write may set none of them (the floor: chat never
    retires a page). A key added to `_NO_ADD_KEYS` without protecting it here fails this test."""
    m, _root = chat
    floor = yaml.safe_load((REPO / "config" / "policy" / "catalog.yaml").read_text(
        encoding="utf-8"))["capabilities"]["chat:api_server"]
    unprotected = (set(m.converge._NO_ADD_KEYS) - set(floor["protected_fields"])
                   - _PAGE_IDENTITY_KEYS_LEFT_TO_STATUS_RULES)
    assert unprotected == set(), f"chat may set page-identity keys {sorted(unprotected)}"


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
