import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "local_evidence", ROOT / "extensions/okengine.assessments/local_evidence.py")
resolver = importlib.util.module_from_spec(spec); spec.loader.exec_module(resolver)


def page(vault, rel, fm, body=""):
    path = vault / "wiki" / f"{rel}.md"; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False).rstrip() +
                    "\n---\n\n" + body + "\n", encoding="utf-8")


def build(vault):
    page(vault, "entities/f/fsb-center-16", {"type": "actor", "title": "FSB Center 16",
         "aliases": ["Static Tundra"], "sources": [
             "https://cisa.gov/advisory", "sources/vendor", "sources/missing"],
         "repository_recent_articles": [{"id": "article-1", "title": "FSB Center 16 update",
                                         "url": "https://news.example/fsb"}]})
    page(vault, "sources/cisa", {"type": "source", "id": "source:cisa",
         "url": "https://cisa.gov/advisory", "publisher": "CISA",
         "authority_id": "agency:cisa", "source_channel": "government-advisory"},
         "Russian Federal Security Service Center 16 conducted the activity.")
    page(vault, "sources/vendor", {"type": "source", "id": "source:vendor",
         "url": "https://vendor.example/report", "publisher": "Vendor"},
         "Static Tundra targets edge devices.")
    page(vault, "sources/mitre", {"type": "source", "id": "source:mitre",
         "url": "https://attack.mitre.org/groups/G0007/", "attack_id": "G0007",
         "publisher": "MITRE ATT&CK"}, "APT28 is associated with Russia.")


def test_local_resolution_provenance_alias_hold_and_unresolved(tmp_path):
    build(tmp_path)
    result = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "actor-country-linkage")
    cisa = next(x for x in result["resolved"] if x["artifact"] == "sources/cisa")
    assert cisa["authority_ids"] == ["agency:cisa"]
    assert cisa["ingestion_provenance"] == "government-advisory"
    vendor = next(x for x in result["held_alias_matches"] if x["artifact"] == "sources/vendor")
    assert vendor["observed_subject_label"] == "Static Tundra"
    embedded = next(x for x in result["resolved"] if "#article:" in x["artifact"])
    assert embedded["source_identity"] is None  # repository provenance is not publisher
    assert embedded["ingestion_provenance"] == "article-repository"
    assert any(x["reason"] == "local-page-missing" for x in result["unresolved"])


def test_authority_lookup_and_snapshot_are_deterministic(tmp_path):
    build(tmp_path)
    one = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "country",
                           authority_ids=["G0007"])
    two = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "country",
                           authority_ids=["G0007"])
    assert any(x["artifact"] == "sources/mitre" for x in one["resolved"])
    assert one["snapshot_digest"] == two["snapshot_digest"]


def test_pack_can_name_an_embedded_article_field_without_engine_vocabulary(tmp_path):
    build(tmp_path)
    entity = tmp_path / "wiki/entities/f/fsb-center-16.md"
    text = entity.read_text().replace("repository_recent_articles:", "pack_article_records:")
    entity.write_text(text, encoding="utf-8")
    result = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "country",
                              embedded_article_fields=("pack_article_records",))
    embedded = next(x for x in result["resolved"] if "#article:" in x["artifact"])
    assert embedded["reference_origin"].endswith(":pack_article_records")


def test_missing_subject_and_malformed_page_are_explicit(tmp_path):
    build(tmp_path)
    bad = tmp_path / "wiki/sources/bad.md"; bad.write_text("bad", encoding="utf-8")
    good = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "country")
    assert any(x["reason"] == "malformed-local-page" for x in good["malformed"])
    missing = resolver.resolve(tmp_path, "entities/x/missing", "country")
    assert missing["status"] == "failed"
    assert missing["unresolved"][0]["reason"] == "subject-not-local"


def test_batch_can_reuse_one_index(tmp_path, monkeypatch):
    build(tmp_path)
    index = resolver.build_index(tmp_path)
    monkeypatch.setattr(resolver, "build_index", lambda _vault: (_ for _ in ()).throw(
        AssertionError("resolve rebuilt the supplied batch index")))
    result = resolver.resolve(
        tmp_path, "entities/f/fsb-center-16", "country", index=index)
    assert result["status"] == "resolved"


def test_yaml_dates_have_canonical_snapshot_serialization(tmp_path):
    build(tmp_path)
    source = tmp_path / "wiki/sources/cisa.md"
    source.write_text(source.read_text().replace(
        "publisher: CISA\n", "publisher: CISA\npublished: 2026-07-19\n"), encoding="utf-8")
    one = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "country")
    two = resolver.resolve(tmp_path, "entities/f/fsb-center-16", "country")
    assert one["snapshot_digest"] == two["snapshot_digest"]


def test_generic_operator_evidence_carries_fail_closed_attestation(tmp_path):
    build(tmp_path)
    entity = tmp_path / "wiki/entities/f/fsb-center-16.md"
    text = entity.read_text().replace("sources:\n", "operator_evidence_refs: [sources/operator]\nsources:\n")
    entity.write_text(text, encoding="utf-8")
    page(tmp_path, "sources/operator", {"type": "source", "publisher": "Malpedia",
         "dataset": "malpedia_actor_data", "retrieved_via": "local-repository",
         "local_only": True, "export_policy": "deny", "record_checksum": "sha256:abc",
         "bounded_auto_accept": True, "evidence_role": "curated-authority"},
         "FSB Center 16 is associated with Russia.")
    item = next(x for x in resolver.resolve(
        tmp_path, "entities/f/fsb-center-16", "country")["resolved"]
        if x["artifact"] == "sources/operator")
    assert item["authority_record_attested"] is True
    assert item["dataset"] == "malpedia_actor_data"
    assert item["bounded_auto_accept"] is True

    source = tmp_path / "wiki/sources/operator.md"
    source.write_text(source.read_text().replace("export_policy: deny", "export_policy: allow"))
    item = next(x for x in resolver.resolve(
        tmp_path, "entities/f/fsb-center-16", "country")["resolved"]
        if x["artifact"] == "sources/operator")
    assert item["authority_record_attested"] is False
