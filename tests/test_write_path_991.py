"""okengine#991: write-path follow-ups to okengine#983's chat writer and protected-field checks.

[4] patch and converge judge protected fields type-strictly and alias-aware.

okengine#983 moved every write path's capability check onto `_frontmatter_changes`, which compares
type-strictly (`0` over `false` is a change) and alias-renamed (a pack alias whose canonical name is
protected is a change to the protected field). Only the update and create sites were tested, through
the chat actor; the engine catalog grants protected fields to no actor that may patch or converge.
These tests give a pack-declared cron actor patch and converge with a protected field. Reverting
the patch site to a plain `!=` fails here. The converge sites are pinned by behavior: the merge
keeps the stored value for an `==` echo and renames aliases before the comparison, and a converge
create is re-checked by `_create` with the same helper.

[14] a create that dedup resolves onto an existing page names that page when it is refused.
"""
from __future__ import annotations

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
strict_types: false
field_aliases:
  marking: tlp
"""

_POLICY = """\
schema_version: 1
rules:
  - id: probe-protected-fields
    owner: pack
    description: A probe lane may patch and converge actors but never move a protected field.
    severity: reject
    applies_to: {operation: write}
    enforcement: [write]
    evaluator: field-capability
    remediation: Leave the protected fields as stored.
    override: forbidden
    verified_by: [write]
capabilities:
  cron:probe:
    rule_id: probe-protected-fields
    operations: [create, update, patch, converge]
    paths: [entities/**]
    update_fields: ["*"]
    protected_fields: [actor_identity_validated, tlp]
    body: allow
"""

_PAGE = """\
---
type: actor
id: G1017
title: Volt Typhoon
actor_identity_validated: false
---

Body line.
"""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, WRITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities" / "v").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "policy.yaml").write_text(_POLICY, encoding="utf-8")
    (tmp_path / "wiki" / "entities" / "v" / "volt-typhoon.md").write_text(_PAGE, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-10-03")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:probe")
    # The probe lane declares no output contract; report mode keeps that check out of the way
    # so each test reaches the capability gate it is about.
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "report")
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    return _load(f"ws_probe_{id(tmp_path)}"), tmp_path


def _stored(root: Path) -> dict:
    text = (root / "wiki" / "entities" / "v" / "volt-typhoon.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---")[1])


def _refused_as_protected(out: str) -> bool:
    return out.startswith("rejected:") and "protected fields would change" in out


def test_patch_refuses_zero_over_a_stored_false(probe):
    m, root = probe
    out = m._patch("entities/v/volt-typhoon", "actor_identity_validated: false",
                   "actor_identity_validated: 0")
    assert _refused_as_protected(out), out
    assert _stored(root)["actor_identity_validated"] is False


def test_patch_refuses_a_pack_alias_that_lands_on_a_protected_field(probe):
    m, root = probe
    out = m._patch("entities/v/volt-typhoon", "title: Volt Typhoon",
                   "title: Volt Typhoon\nmarking: CLEAR")
    assert _refused_as_protected(out), out
    stored = _stored(root)
    assert "tlp" not in stored and "marking" not in stored


def test_patch_still_lands_an_unprotected_edit(probe):
    m, root = probe
    out = m._patch("entities/v/volt-typhoon", "Body line.", "Body line, revised.")
    assert out.startswith("patched"), out
    assert _stored(root)["actor_identity_validated"] is False


def test_converge_into_an_existing_page_never_stores_zero_over_a_stored_false(probe):
    """The merge keeps the stored value for an `==` echo, so `0` never lands over `false`."""
    m, root = probe
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\nactor_identity_validated: 0\n")
    assert out.startswith(("converged", "rejected:")), out
    assert _stored(root)["actor_identity_validated"] is False


def test_converge_into_an_existing_page_refuses_true_over_a_stored_false(probe):
    m, root = probe
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\nactor_identity_validated: true\n")
    assert _refused_as_protected(out), out
    assert _stored(root)["actor_identity_validated"] is False


def test_converge_refuses_a_stored_alias_the_merge_renames_onto_a_protected_field(probe):
    """A stored `marking` key is renamed onto protected `tlp` by the write: that is a change."""
    m, root = probe
    page = root / "wiki" / "entities" / "v" / "volt-typhoon.md"
    page.write_text(_PAGE.replace("title: Volt Typhoon\n", "title: Volt Typhoon\nmarking: CLEAR\n"),
                    encoding="utf-8")
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\nsummary: unrelated\n")
    assert _refused_as_protected(out), out
    assert "tlp" not in _stored(root)


def test_converge_into_an_existing_page_refuses_a_pack_alias_onto_a_protected_field(probe):
    m, root = probe
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\nmarking: CLEAR\n")
    assert _refused_as_protected(out), out
    assert "tlp" not in _stored(root)


def test_converge_creating_a_page_refuses_a_pack_alias_onto_a_protected_field(probe):
    m, root = probe
    out = m._converge("entities/s/storm-0558",
                      "type: actor\nid: S0558\ntitle: Storm-0558\nmarking: CLEAR\n", "Body.")
    assert _refused_as_protected(out), out
    assert not (root / "wiki" / "entities" / "s" / "storm-0558.md").exists()


# --- [14] a refused create that dedup resolved onto an existing page ------------------------

_SALT = """\
---
type: actor
id: G1045
title: Salt Typhoon
aliases: [GhostEmperor]
---

Curated body.
"""


@pytest.fixture
def chat(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities" / "s").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "wiki" / "entities" / "s" / "salt-typhoon.md").write_text(_SALT, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-10-03")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "chat:api_server")
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    return _load(f"ws_chat991_{id(tmp_path)}"), tmp_path


def test_a_chat_create_redirected_onto_an_existing_page_names_the_page_to_extend(chat):
    """`GhostEmperor` is an alias of Salt Typhoon, so create_entity resolves onto that page and
    the merge needs `converge`, which chat may not do. The refusal must say where to write
    instead, not only repeat "create the page", the call that just failed."""
    m, root = chat
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    out = m._create("entities/g/ghostemperor",
                    "type: actor\ntitle: GhostEmperor\naliases: [Salt Typhoon]\n", "B.")
    assert out.startswith("rejected: policy[chat-write-back-floor]"), out
    assert out.endswith(
        " This create resolved to the existing page entities/s/salt-typhoon.md (an identity or"
        " alias match): extend that page with update_entity or append_to_section instead."), out
    assert page.read_text(encoding="utf-8") == _SALT
    assert not (root / "wiki" / "entities" / "g" / "ghostemperor.md").exists()


def test_a_refused_explicit_converge_gets_no_create_hint(chat):
    """Only a genuine create is redirected; chat's own converge is refused plainly."""
    m, _root = chat
    out = m._converge("entities/s/salt-typhoon", "type: actor\nid: G1045\ntitle: Salt Typhoon\n")
    assert out.startswith("rejected:") and "resolved to the existing page" not in out, out


def test_a_non_policy_refusal_of_a_redirected_create_gets_no_create_hint(chat, monkeypatch):
    m, root = chat
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "")
    m = _load(f"ws_admin991_{id(root)}")
    out = m._create("entities/g/ghostemperor",
                    "type: actor\ntitle: GhostEmperor\naliases: [Salt Typhoon]\n", "B.")
    assert not out.startswith("rejected: policy["), out
    assert "resolved to the existing page" not in out, out


# --- QA of !1252 B1: `_` shard directories are entity storage, not reserved namespaces --------

_TMOBILE = """\
---
type: actor
id: T0001
title: T-Mobile
---

Body.

## Activity

- 2025: breach
"""


def _shard_page(root: Path) -> Path:
    page = root / "wiki" / "entities" / "t" / "_" / "t-mobile.md"
    page.parent.mkdir(parents=True)
    page.write_text(_TMOBILE, encoding="utf-8")
    return page


def test_chat_updates_and_appends_to_a_page_in_a_second_letter_shard(chat):
    m, root = chat
    page = _shard_page(root)
    out = m._update("entities/t/_/t-mobile", "summary: carrier\n")
    assert out.startswith("updated"), out
    out = m._append_section("entities/t/_/t-mobile", "Activity", "- 2026: more")
    assert out.startswith("appended"), out
    assert "- 2026: more" in page.read_text(encoding="utf-8")


def test_chat_creates_a_page_the_partitioner_files_under_a_shard(chat):
    m, root = chat
    (root / "schema.yaml").write_text(_SCHEMA + "partitioning:\n  namespaces:\n    entities: "
                                      "{strategy: by-letter, reshard_by: second-letter}\n",
                                      encoding="utf-8")
    out = m._create("entities/t/t-rex-group",
                    f"type: actor\ntitle: T-Rex Group\nsources: [{_cite(m, 'T-Rex Group')}]\n", "B.")
    assert out.startswith("created"), out
    assert "entities/t/_/t-rex-group.md" in out, out


_ACME_SCHEMA = """\
okf:
  required: [type]
types:
  actor:
    required: [type, title]
  source:
    required: [type]
strict_types: false
"""

_ACME_POLICY = {"schema_version": 1, "rules": [], "capabilities": {"chat:api_server": None}}


@pytest.fixture
def subdomain_chat(chat, monkeypatch):
    """A walk-up host whose pack widened chat to the `acme` sub-domain (`acme/**`)."""
    m, root = chat
    (root / "wiki" / "acme").mkdir(parents=True)
    (root / "wiki" / "acme" / "schema.yaml").write_text(_ACME_SCHEMA, encoding="utf-8")
    floor = yaml.safe_load((REPO / "config" / "policy" / "catalog.yaml").read_text(
        encoding="utf-8"))["capabilities"]["chat:api_server"]
    doc = dict(_ACME_POLICY, capabilities={"chat:api_server": dict(
        floor, paths=["entities/**", "concepts/**", "acme/**"])})
    (root / "policy.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return _load(f"ws_chat991_sub_{id(root)}"), root


def test_chat_writes_a_sub_domain_page_in_a_shard(subdomain_chat):
    m, root = subdomain_chat
    page = root / "wiki" / "acme" / "entities" / "t" / "_" / "t-mobile.md"
    page.parent.mkdir(parents=True)
    page.write_text(_TMOBILE, encoding="utf-8")
    out = m._update("acme/entities/t/_/t-mobile", "summary: carrier\n")
    assert out.startswith("updated"), out


@pytest.mark.parametrize(("path", "namespace"), [
    ("acme/sources/a-post", "sources"), ("acme/_archive/old", "_archive"),
])
def test_chat_may_not_write_a_sub_domain_reserved_namespace(subdomain_chat, path, namespace):
    m, root = subdomain_chat
    out = m._create(path, "type: source\ntitle: X\n", "B.")
    assert out.startswith("rejected:") and f"forbidden namespace '{namespace}'" in out, out
    assert not (root / "wiki" / (path + ".md")).exists()


# --- QA of !1252: the redirect hint is chat advice, so only chat gets it ----------------------


def test_a_cron_create_redirected_and_refused_by_policy_gets_no_chat_hint(probe, monkeypatch):
    """A create-only cron lane whose create dedups onto an existing page is refused for
    `converge` like chat is, but `update_entity`/`append_to_section` are not its tools."""
    m, root = probe
    policy = yaml.safe_load((root / "policy.yaml").read_text(encoding="utf-8"))
    policy["capabilities"]["cron:creator"] = {
        "rule_id": "probe-protected-fields", "operations": ["create"], "paths": ["entities/**"],
        "update_fields": ["*"], "body": "allow"}
    (root / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:creator")
    m = _load(f"ws_creator_{id(root)}")
    out = m._create("entities/s/storm-x", "type: actor\ntitle: Storm X\naliases: [Volt Typhoon]\n",
                    "B.")
    assert out.startswith("rejected: policy[probe-protected-fields] operation 'converge'"), out
    assert "resolved to the existing page" not in out, out
    assert not (root / "wiki" / "entities" / "s" / "storm-x.md").exists()


# --- QA of !1252 S1: the converge merge site's comparison is observable ------------------------


def test_converge_refuses_adding_a_protected_key_as_null(probe):
    """`tlp: null` onto a page without `tlp` adds the key. A `.get()` comparison reads both sides
    as None and let it land; the key comparison refuses it."""
    m, root = probe
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\ntlp: null\n")
    assert _refused_as_protected(out), out
    assert "tlp" not in _stored(root)


_NARROW = {"cron:narrow": {"rule_id": "probe-protected-fields", "operations": ["converge"],
                           "paths": ["entities/**"], "update_fields": ["title", "summary"],
                           "body": "deny"}}


def test_converge_judges_a_float_echo_of_an_int_field_as_a_change(probe, monkeypatch):
    """`version: 1.0` over a stored `1` is `==` in Python; under a narrow update allowlist the
    type-strict comparison refuses it as a field outside the allowlist."""
    m, root = probe
    policy = yaml.safe_load((root / "policy.yaml").read_text(encoding="utf-8"))
    policy["capabilities"].update(_NARROW)
    (root / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    page = root / "wiki" / "entities" / "v" / "volt-typhoon.md"
    page.write_text(_PAGE.replace("title: Volt Typhoon\n", "title: Volt Typhoon\nversion: 1\n"),
                    encoding="utf-8")
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:narrow")
    m = _load(f"ws_narrow_{id(root)}")
    out = m._converge("entities/v/volt-typhoon",
                      "type: actor\nid: G1017\ntitle: Volt Typhoon\nversion: 1.0\n")
    assert out.startswith("rejected:") and "fields exceed the update allowlist" in out, out
    assert _stored(root)["version"] == 1


# --- QA of !1252 S2: a pathological echo string cannot crash the update ----------------------


def test_a_deeply_nested_string_echoed_over_a_stored_date_is_a_change_not_a_crash(chat):
    m, root = chat
    page = root / "wiki" / "entities" / "s" / "salt-typhoon.md"
    page.write_text(_SALT.replace("title: Salt Typhoon\n",
                                  "title: Salt Typhoon\nreviewed_on: 2026-09-01\n"),
                    encoding="utf-8")
    out = m._update("entities/s/salt-typhoon", {"reviewed_on": "[" * 200000})
    assert out.startswith("rejected:") and "protected fields would change" in out, out


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
