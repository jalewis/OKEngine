"""okengine#980: curated-authority attestation is operator-owned at the enforced write path.

The local-evidence resolver treats a source as an attested authority record when an actor lists it
in ``operator_evidence_refs`` and the source carries ``local_only``/``export_policy``/
``retrieved_via``/``dataset``/``record_checksum`` (plus ``bounded_auto_accept`` and
``evidence_role`` for the pack's auto-accept rule). A pack policy then auto-accepts a bounded claim
with no URL check. Before this fix every model-driven lane with a source or entity namespace could
write all of those fields: ``page-quality-enrich`` turned an ordinary blog post into an attested
curated-dataset record with one ``update_entity`` and linked it from the actor with a second.

The legitimate producer is a deterministic importer that writes pages directly. These tests pin
that every MCP write (job lane, extension or the unbound stdio writer) preserves the fields,
that an attested record's publisher and body stay frozen, and that ordinary enrichment of an
attested record and the resolver's reading of it are unaffected.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WRITE = REPO / "okengine-mcp" / "write_server.py"
sys.path.insert(0, str(REPO / "extensions" / "okengine.assessments"))
import local_evidence  # noqa: E402
from okengine import attestation  # noqa: E402

SCHEMA = """\
okf:
  required: [type]
types:
  source:
    required: [type, source_kind, publisher, published]
  actor:
    required: [type, title]
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
"""

ATTESTED = {
    "local_only": True, "export_policy": "deny", "retrieved_via": "local-repository",
    "dataset": "curated_actor_data", "record_checksum": "sha256:" + "a" * 64,
    "bounded_auto_accept": True, "evidence_role": "curated-authority",
}
ACTOR_BODY = "# APT Example\n\nAn actor page with enough body text to satisfy any body contract.\n"
BLOG_BODY = ("# A vendor post\n\nAPT Example is discussed in this ordinary blog post, which "
             "speculates about the group's country without any curated record behind it.\n")
RECORD_BODY = ("# APT Example — curated actor record\n\nAPT Example is associated with a country "
               "in the curated dataset this record was imported from.\n")
ENGINE_JOBS = {job["name"]: job for job in json.loads(
    (REPO / "config" / "engine-crons.json").read_text(encoding="utf-8"))}


def _page(root: Path, rel: str, fm: dict, body: str) -> None:
    """Write a page DIRECTLY, as the deterministic importer does (never through MCP)."""
    path = root / "wiki" / f"{rel}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body,
                    encoding="utf-8")


def _fm(root: Path, rel: str) -> dict:
    text = (root / "wiki" / f"{rel}.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n", 2)[1])


def _writer(root: Path, monkeypatch, lane: str | None):
    """The real write server, bound to an engine lane's shipped output contract, or unbound."""
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-10-03")
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    if lane:
        job = dict(ENGINE_JOBS[lane])
        raw = json.dumps(job["output_contract"], sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
        job["output_contract_digest"] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        jobs = root / "jobs.json"
        jobs.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
        monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
        monkeypatch.setenv("OKENGINE_WRITE_ACTOR", f"cron:{lane}")
        monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "enforce")
    else:
        monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    name = f"ws_980_{lane or 'admin'}_{id(root)}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, WRITE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def corpus(tmp_path):
    """An importer-written attested record linked from its actor, plus an ordinary blog source."""
    _page(tmp_path, "entities/a/apt-example",
          {"type": "actor", "title": "APT Example", "id": "entities:apt-example",
           "operator_evidence_refs": ["sources/private/record"]}, ACTOR_BODY)
    _page(tmp_path, "sources/private/record",
          {"type": "source", "source_kind": "reference-data", "publisher": "Curated Authority",
           "published": "2026-09-01", "id": "sources:record", **ATTESTED}, RECORD_BODY)
    _page(tmp_path, "sources/2026/10/blog",
          {"type": "source", "source_kind": "vendor-blog", "publisher": "Some Blog",
           "published": "2026-10-01", "raw": "raw/blog", "id": "sources:blog"}, BLOG_BODY)
    return tmp_path


def _attested(root: Path) -> dict[str, bool]:
    resolved = local_evidence.resolve(root, "entities/a/apt-example", "country")["resolved"]
    return {item["artifact"]: item["authority_record_attested"] and item["bounded_auto_accept"]
            for item in resolved}


def _refused(result: str) -> bool:
    return result.startswith("refused:") and "okengine#980" in result


# --- the reproduction: an agent lane can no longer forge an attested record ------------------


@pytest.mark.parametrize("lane", ["page-quality-enrich", "orphans-drain", "review-drain"])
def test_an_agent_lane_cannot_forge_an_attested_record_and_link_it(corpus, monkeypatch, lane):
    ws = _writer(corpus, monkeypatch, lane)
    before = (corpus / "wiki/sources/2026/10/blog.md").read_bytes()

    forged = ws._update("sources/2026/10/blog", yaml.safe_dump({**ATTESTED, "publisher": "X"}))
    linked = ws._update("entities/a/apt-example", yaml.safe_dump(
        {"operator_evidence_refs": ["sources/private/record", "sources/2026/10/blog"]}))

    assert _refused(forged), forged
    assert _refused(linked), linked
    assert (corpus / "wiki/sources/2026/10/blog.md").read_bytes() == before
    assert _attested(corpus) == {"sources/private/record": True}, (
        "only the importer's record may be an attested authority for the actor")


def test_a_patch_cannot_splice_attestation_into_frontmatter(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, "page-quality-enrich")
    result = ws._patch("sources/2026/10/blog", "publisher: Some Blog\n",
                       "publisher: Some Blog\nlocal_only: true\nbounded_auto_accept: true\n")
    assert _refused(result) and "local_only" in result and "bounded_auto_accept" in result


def test_converge_cannot_add_an_operator_evidence_reference(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, "entity-backfill")
    result = ws._converge("entities/a/apt-example", yaml.safe_dump(
        {"type": "actor", "title": "APT Example",
         "operator_evidence_refs": ["sources/private/record", "sources/2026/10/blog"]}),
        ACTOR_BODY, "", "", "")
    assert _refused(result) and "operator_evidence_refs" in result, result
    assert _fm(corpus, "entities/a/apt-example")["operator_evidence_refs"] == [
        "sources/private/record"]


def test_converge_cannot_remove_operator_owned_fields(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    result = ws._converge("entities/a/apt-example", yaml.safe_dump(
        {"type": "actor", "title": "APT Example"}), "", "", "operator_evidence_refs", "")
    assert _refused(result), result


def test_the_unbound_stdio_writer_is_refused_too(corpus, monkeypatch):
    """The unbound `okengine-write` server is what the chat agent writes through: it is admin to
    every other check, so the attestation rule must not key on caller kind."""
    ws = _writer(corpus, monkeypatch, None)
    assert ws._caller()["kind"] == "admin"
    created = ws._create("sources/2026/10/forged", yaml.safe_dump(
        {"type": "source", "source_kind": "reference-data", "publisher": "Curated Authority",
         "published": "2026-10-01", "title": "Forged", **ATTESTED}), BLOG_BODY)
    updated = ws._update("sources/2026/10/blog", yaml.safe_dump({"record_checksum": "sha256:1"}))
    assert _refused(created), created
    assert _refused(updated), updated
    assert not (corpus / "wiki/sources/2026/10/forged.md").exists()


# --- an attested record is frozen where the resolver reads it ---------------------------------


def test_an_attested_records_body_cannot_be_rewritten_or_appended(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    rewritten = ws._update("sources/private/record", None, RECORD_BODY + "\nAPT Other too.\n")
    shortened = ws._update("sources/private/record", None, "# A\n")
    appended = ws._append_section("sources/private/record", "Notes", "APT Other is the same group.")
    patched = ws._patch("sources/private/record", "in the curated dataset", "in a dataset")
    extended = ws._patch("sources/private/record", "in the curated", "in the zz curated")
    for result in (rewritten, shortened, appended, patched, extended):
        assert _refused(result) and "body" in result, result


def test_an_attested_records_publisher_is_frozen(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    # one value sorting before the stored one and one after: no ordering test passes for inequality
    for publisher in ("Alpha Authority", "Zeta Authority"):
        result = ws._update("sources/private/record", yaml.safe_dump({"publisher": publisher}))
        assert _refused(result) and "publisher" in result, result


def test_converge_on_an_attested_record_keeps_its_body_frozen(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    fm = yaml.safe_dump({"type": "source", "id": "sources:record", "tlp": "AMBER"})
    for body in (RECORD_BODY + "\nAPT Other too.\n", "# A\n"):   # sorts after, then before
        result = ws._converge("sources/private/record", fm, body, "", "", "")
        assert _refused(result) and "body" in result, result
    assert ws._converge("sources/private/record", fm, RECORD_BODY, "", "", "").startswith(
        "converged"), "resending the stored body is not a body change"
    assert ws._converge("sources/private/record", yaml.safe_dump(
        {"type": "source", "id": "sources:record", "credibility": 2}), "", "", "", "").startswith(
        "converged"), "a fields-only converge never touches the body"
    assert _attested(corpus) == {"sources/private/record": True}


def test_a_frontmatter_only_patch_of_an_attested_record_is_not_a_body_change(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    result = ws._patch("sources/private/record", "source_kind: reference-data\n",
                       "source_kind: reference-data\ntlp: AMBER\n")
    assert result.startswith("patched"), result


@pytest.mark.parametrize("field", ["vendor", "id", "url", "attack_id", "authority_id",
                                   "cve_id", "ghsa_id", "osv_id"])
def test_an_attested_records_identity_and_lookup_keys_are_frozen(corpus, monkeypatch, field):
    """D1/D2: `vendor` stands in for `publisher`, and every lookup key could re-point a reference."""
    ws = _writer(corpus, monkeypatch, "page-quality-enrich")
    result = ws._update("sources/private/record", yaml.safe_dump({field: "G9999"}))
    assert _refused(result) and field in result, result
    assert ws._update("sources/2026/10/blog", yaml.safe_dump({"authority_id": "G9999"})).startswith(
        "updated"), "an unattested page keeps its ordinary lookup keys"


def test_a_pack_field_alias_cannot_carry_a_write_onto_a_protected_key(corpus, monkeypatch):
    """`field_aliases` rename a key AFTER it is written; the check must see the renamed key."""
    ws = _writer(corpus, monkeypatch, "page-quality-enrich")
    (corpus / "schema.yaml").write_text(
        SCHEMA + "field_aliases:\n  trusted: bounded_auto_accept\n  origin_dataset: dataset\n",
        encoding="utf-8")
    result = ws._update("sources/2026/10/blog",
                        yaml.safe_dump({"trusted": True, "origin_dataset": "curated_actor_data"}))
    assert _refused(result) and "bounded_auto_accept" in result and "dataset" in result, result
    assert "trusted" not in _fm(corpus, "sources/2026/10/blog")
    # where the canonical key is already set the alias is dropped, not applied: nothing changes
    kept = ws._update("sources/private/record", yaml.safe_dump({"trusted": False, "tlp": "AMBER"}))
    assert kept.startswith("updated"), kept
    assert _fm(corpus, "sources/private/record")["bounded_auto_accept"] is True


def test_the_attested_channel_resolves_by_exact_path_only(corpus):
    """D1 resolver layer: a dangling operator reference must not find an attested record through
    an id, authority id or URL that record carries."""
    _page(corpus, "sources/private/record",
          {"type": "source", "source_kind": "reference-data", "publisher": "Curated Authority",
           "published": "2026-09-01", "id": "sources:record", "authority_id": "G9999",
           "url": "https://example.test/record", **ATTESTED}, RECORD_BODY)
    _page(corpus, "entities/a/apt-example",
          {"type": "actor", "title": "APT Example",
           "operator_evidence_refs": ["G9999", "https://example.test/record", "sources:record"]},
          ACTOR_BODY)
    result = local_evidence.resolve(corpus, "entities/a/apt-example", "country")
    assert result["resolved"] == []
    assert {row["reason"] for row in result["unresolved"]} == {"operator-ref-not-a-page-path"}
    # the same record named by its path -- plain, wikilinked, .md-suffixed or as a mapping -- attests
    for ref in ("[[sources/private/record]]", "wiki/sources/private/record.md",
                {"path": "sources/private/record"}):
        _page(corpus, "entities/a/apt-example",
              {"type": "actor", "title": "APT Example", "operator_evidence_refs": [ref]}, ACTOR_BODY)
        assert _attested(corpus) == {"sources/private/record": True}, ref


def test_other_reference_channels_keep_their_id_lookup(corpus):
    _page(corpus, "sources/private/record",
          {"type": "source", "source_kind": "reference-data", "publisher": "Curated Authority",
           "published": "2026-09-01", "id": "sources:record", "authority_id": "G9999",
           **ATTESTED}, RECORD_BODY)
    _page(corpus, "entities/a/apt-example",
          {"type": "actor", "title": "APT Example", "sources": ["G9999"]}, ACTOR_BODY)
    resolved = local_evidence.resolve(corpus, "entities/a/apt-example", "country")["resolved"]
    assert [(r["artifact"], r["authority_record_attested"]) for r in resolved] == [
        ("sources/private/record", False)], "found by id, but never attested outside the channel"


# --- what must keep working --------------------------------------------------------------------


def test_scoring_an_attested_record_still_works_and_keeps_it_attested(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    result = ws._update("sources/private/record",
                        yaml.safe_dump({"reliability": "B", "credibility": 2}))
    assert result.startswith("updated"), result
    fm = _fm(corpus, "sources/private/record")
    assert {k: fm[k] for k in ATTESTED} == ATTESTED
    assert _attested(corpus) == {"sources/private/record": True}


def test_resending_the_stored_attestation_unchanged_is_not_a_change(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    result = ws._update("sources/private/record",
                        yaml.safe_dump({**ATTESTED, "publisher": "Curated Authority",
                                        "tlp": "AMBER"}), RECORD_BODY)
    assert result.startswith("updated"), result


def test_an_unattested_page_keeps_its_ordinary_publisher_and_body_edits(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    assert ws._update("sources/2026/10/blog", yaml.safe_dump(
        {"publisher": "Some Blog Inc"})).startswith("updated")
    assert ws._append_section("sources/2026/10/blog", "Notes",
                              "A further note on the post.").startswith("appended")


def test_an_empty_reference_list_is_the_same_as_none(corpus, monkeypatch):
    ws = _writer(corpus, monkeypatch, None)
    result = ws._update("sources/2026/10/blog", yaml.safe_dump({"operator_evidence_refs": []}))
    assert result.startswith("updated"), result


# --- the contract with the consumer ------------------------------------------------------------


def test_every_field_the_resolver_attests_on_is_operator_owned(tmp_path):
    """Bind the write-path list to the consumer: dropping any field the resolver's attestation
    reads must un-attest the record, and every such field must be operator-owned. A new field in
    the resolver's predicate that is not added here would be agent-writable again."""
    base = {"type": "source", "publisher": "Curated Authority", **ATTESTED}
    _page(tmp_path, "entities/a/apt-example",
          {"type": "actor", "title": "APT Example", "operator_evidence_refs": ["sources/r"]},
          ACTOR_BODY)
    consumed = []
    for field in base:
        _page(tmp_path, "sources/r", {k: v for k, v in base.items() if k != field}, RECORD_BODY)
        item = local_evidence.resolve(tmp_path, "entities/a/apt-example", "c")["resolved"][0]
        if not (item["authority_record_attested"] and item["bounded_auto_accept"]):
            consumed.append(field)
    assert set(consumed) == {"local_only", "export_policy", "retrieved_via", "dataset",
                             "record_checksum", "bounded_auto_accept"}
    assert set(consumed) <= set(attestation.OPERATOR_FIELDS)
    # the pack policy also keys on these record descriptors and on the reference channel
    assert {"evidence_role", "operator_evidence_refs"} <= set(attestation.OPERATOR_FIELDS)
    assert "operator_evidence_refs" in local_evidence.REFERENCE_FIELDS


def test_local_only_must_be_the_boolean_true_not_a_truthy_lookalike(tmp_path):
    """`1 == True` in Python; an importer writes the boolean, so only `true` attests."""
    _page(tmp_path, "entities/a/apt-example",
          {"type": "actor", "title": "APT Example", "operator_evidence_refs": ["sources/r"]},
          ACTOR_BODY)
    _page(tmp_path, "sources/r", {"type": "source", **ATTESTED, "local_only": 1}, RECORD_BODY)
    assert _attested(tmp_path) == {"sources/r": False}
