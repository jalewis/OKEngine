"""A bare service endpoint is not a document identity, and converge never swaps a body across
identities (okengine#901).

Live incident, synthetic values here: two parcel reads for two different properties both
carried the same bare ArcGIS layer `…/MapServer/0/query` URL. `_dedup_on_create` minted
`source_url_id(url)` for the second, found the first, and converged into it. The frontmatter
conflicts (`title`, `property`, …) kept the owner's values, but the body was REPLACED: one
property's title and `property` over another property's parcel write-up.

Two defects, pinned in both directions:

* URL identity — a bare endpoint (no query string) carries NO identity, exactly like the
  `UNKNOWN` placeholder: no converge on it. A record-specific URL (the same path WITH a query
  string) and ordinary document URLs that merely contain those words keep their identity.
* converge — a conflict on an identity-defining field keeps the existing body byte-identical;
  a conflict on any other field keeps the old behavior (the body is replaced).

Everything runs the real write path against a temp-dir vault; nothing under test is mocked.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"

ENDPOINT = "https://gis.example.test/arcgis/rest/services/Parcels/MapServer/0/query"

PLAIN_SCHEMA = "types:\n  source:\n    required: [type]\n"

# A composed deployment: `house` owns source pages, so any other principal's differing value is a
# CONFLICT (kept), which is the condition of the incident. `property` is a pack-declared identity
# field; the engine cannot name it.
OWNED_SCHEMA = (
    "types:\n"
    "  source:\n"
    "    required: [type]\n"
    "    owner: house\n"
    "    identity_fields: [property]\n"
)


def _load(root: Path, schema: str = PLAIN_SCHEMA):
    os.environ["WIKI_PATH"] = str(root)
    os.environ["OKENGINE_MCP_WRITE_DATE"] = "2026-09-26"
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    os.environ.pop("OKENGINE_WRITE_ACTOR", None)
    os.environ.pop("OKENGINE_PACK", None)
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "schema.yaml").write_text(schema)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    assert m._CONVERGE_OK, "converge libs should import"
    return m


def _fm(title: str, url: str, prop: str, extra: str = "") -> str:
    return (
        f"type: source\ntitle: {title}\nurl: \"{url}\"\nproperty: {prop}\n"
        f"published: 2026-09-20\n{extra}"
    )


def _body_bytes(page: Path) -> str:
    """Everything after the closing frontmatter fence, exactly as stored."""
    text = page.read_text(encoding="utf-8")
    assert text.startswith("---\n"), text[:40]
    end = text.index("\n---", 4)
    return text[end + len("\n---"):]


def _ok(result: str) -> bool:
    return not result.startswith(("refused", "rejected", "error"))


NEIGHBORHOOD_BODY = "## Neighborhood\n\n38 parcels surveyed on Synthetic Court.\n"


# --- classification: conservative, explicit, and negative space ---------------------------


@pytest.mark.parametrize("url", [
    ENDPOINT,
    ENDPOINT + "/",
    "https://gis.example.test/arcgis/rest/services/Parcels/MapServer/0",
    "https://gis.example.test/arcgis/rest/services/Parcels/MapServer",
    "https://gis.example.test/arcgis/rest/services/Parcels/FeatureServer/12",
    "https://gis.example.test/arcgis/rest/services/Parcels/featureserver/3/QUERY",
    "https://api.example.test/v2/query",
    ENDPOINT + "?",  # an empty query string names no record either
    ENDPOINT + "#results",
    "https://data.example.test/resource/ab12-cd34.json",
    "https://data.example.test/resource/ab12-cd34.csv",
    "  https://data.example.test/resource/ab12-cd34.geojson  ",
])
def test_bare_service_endpoints_carry_no_identity(tmp_path, url):
    m = _load(tmp_path)
    assert m._is_service_endpoint_url(url) is True
    assert m._norm_url(url) == ""
    assert m.source_url_id(url) == "", f"a bare endpoint must mint no identity: {url}"


@pytest.mark.parametrize("url", [
    # the same endpoints WITH a record-specific query string name one record
    ENDPOINT + "?where=ACCTID%3D%27SYN0001%27&outFields=*&f=json",
    "https://gis.example.test/arcgis/rest/services/Parcels/FeatureServer/12?objectIds=7",
    "https://data.example.test/resource/ab12-cd34.json?parcel_id=SYN-0001",
    # ordinary documents whose paths merely contain the words
    "https://blog.example.test/2026/09/query-planning-in-postgres",
    "https://docs.example.test/api/query/overview",
    "https://docs.example.test/guides/queryable-fields",
    "https://news.example.test/mapserver-migration-guide",
    "https://gis.example.test/arcgis/rest/services/Parcels/MapServer/0/legend",
    "https://example.test/resources/annual-report.pdf",
    "https://example.test/resource/guide.html",
    "https://data.example.test/resource/ab12-cd34",
    "https://data.example.test/resource/not-a-4x4-id.json",
    "https://example.test/search/query.html",
])
def test_document_urls_keep_their_identity(tmp_path, url):
    m = _load(tmp_path)
    assert m._is_service_endpoint_url(url) is False
    assert m.source_url_id(url).startswith("sources:url-"), url


@pytest.mark.parametrize(("raw", "identity"), [
    ("https://x.test/a/", "https://x.test/a"),
    ("  https://x.test/a  ", "https://x.test/a"),
    ("https://x.test/a//", "https://x.test/a/"),  # ONE trailing slash, never more
    ("https://x.test/", "https://x.test"),
    ("https://x.test/A", "https://x.test/A"),  # no case folding
    ("https://x.test/query?id=1/", "https://x.test/query?id=1"),
])
def test_identity_normalization_is_minimal(tmp_path, raw, identity):
    """The shared rule keeps the pre-#901 normalization exactly: whitespace and one slash."""
    m = _load(tmp_path)
    assert m._source_identity_url(raw) == identity
    assert m._norm_url(raw) == identity


@pytest.mark.parametrize("value", [None, "", "UNKNOWN", "null", "ftp://x.test/query",
                                   "http://[unbalanced/query"])
def test_non_addresses_are_not_classified_as_endpoints(tmp_path, value):
    m = _load(tmp_path)
    assert m._is_service_endpoint_url(value) is False


# --- (a) the incident: one bare endpoint, two records ----------------------------------------


def test_two_records_from_one_bare_query_endpoint_do_not_converge(tmp_path):
    m = _load(tmp_path)
    first_rel = "sources/2026/09/24/parcel-neighborhood-synthetic-ct.md"
    first = m._create(first_rel, _fm("Parcel neighborhood, Synthetic Ct", ENDPOINT,
                                     "properties/synthetic-ct-10"), NEIGHBORHOOD_BODY)
    assert _ok(first), first
    before = (tmp_path / "wiki" / first_rel).read_bytes()

    second_rel = "sources/2026/09/26/parcel-record-example-rd-101.md"
    second = m._create(second_rel, _fm("Parcel record, Example Rd 101", ENDPOINT,
                                       "properties/example-rd-101"), "One parcel, 2.1 acres.\n")

    assert _ok(second) and not second.startswith("converged"), (
        f"a second record read from the same bare endpoint must not converge: {second}")
    assert (tmp_path / "wiki" / first_rel).read_bytes() == before, (
        "the first record's page must be untouched")
    fm2, body2 = m._read_page(tmp_path / "wiki" / second_rel)
    assert fm2["property"] == "properties/example-rd-101"
    assert body2.strip() == "One parcel, 2.1 acres."
    assert not str(fm2["id"]).startswith("sources:url-"), (
        "a page whose url is a bare endpoint must not be stamped with a URL identity")


def test_same_title_records_from_a_bare_endpoint_are_refused_not_fused(tmp_path):
    """Without URL identity a shared title is only a weak slug: refused, exactly as a url-less
    source is — never a converge."""
    m = _load(tmp_path)
    first_rel = "sources/2026/09/24/parcel-read.md"
    assert _ok(m._create(first_rel, _fm("Parcel read", ENDPOINT, "properties/a"),
                         NEIGHBORHOOD_BODY))
    before = (tmp_path / "wiki" / first_rel).read_bytes()
    second = m._create("sources/2026-09/parcel-read.md",
                       _fm("Parcel read", ENDPOINT, "properties/b"), "Other parcel.\n")
    assert second.startswith("refused:") and "URL identity is required" in second, second
    assert (tmp_path / "wiki" / first_rel).read_bytes() == before


def test_record_specific_query_urls_still_dedup(tmp_path):
    """NEGATIVE space: the `/query` path WITH a record query string is a real identity."""
    m = _load(tmp_path)
    record = ENDPOINT + "?where=ACCTID%3D%27SYN0001%27&f=json"
    assert _ok(m._create("sources/2026/09/24/acct-syn0001.md",
                         _fm("Account SYN0001", record, "properties/a"), "First read.\n"))
    again = m._create("sources/2026-09/acct-syn0001-reread.md",
                      _fm("Account SYN0001", record, "properties/a"), "Second read.\n")
    assert again.startswith("converged into sources/2026/09/24/acct-syn0001.md"), again

    other = m._create("sources/2026/09/24/acct-syn0002.md",
                      _fm("Account SYN0002", ENDPOINT + "?where=ACCTID%3D%27SYN0002%27&f=json",
                          "properties/b"), "Different record.\n")
    assert other.startswith("created"), f"a different record query is a new page: {other}"


def test_placeholders_behave_as_before(tmp_path):
    m = _load(tmp_path)
    assert m.source_url_id("UNKNOWN") == ""
    assert _ok(m._create("sources/2026/09/one.md", _fm("Same Title", "UNKNOWN", "p/a"), "A.\n"))
    second = m._create("sources/2026-09/two.md", _fm("Same Title", "UNKNOWN", "p/b"), "B.\n")
    assert second.startswith("refused:") and "URL identity is required" in second, second
    pending = m._create("sources/2026/09/three.md",
                        _fm("Pending", "https://arxiv.example.test/abs/[pending]", "p/c"), "C.\n")
    assert pending.startswith("rejected:") and "placeholder" in pending, pending


def test_raw_compile_identity_rejects_a_bare_endpoint(tmp_path):
    """The raw writers mint a URL identity too; a bare endpoint there is a missing identity."""
    m = _load(tmp_path)
    out = m._converge_raw_source("raw/connectors/gis/a.json", {"type": "source", "url": ENDPOINT},
                                 "sources/2026/09/26/parcel.md", "Body.\n")
    assert out.startswith("rejected:") and "bare service endpoint" in out, out
    assert not list((tmp_path / "wiki").rglob("parcel*.md"))


# --- (b) converge never swaps a body across identities ------------------------------------


def _owned_page(tmp_path):
    m = _load(tmp_path, OWNED_SCHEMA)
    rel = "sources/2026/09/24/synthetic-ct.md"
    created = m._converge(rel, _fm("Parcel neighborhood, Synthetic Ct", "https://x.test/a",
                                   "properties/synthetic-ct-10", "publisher: County Office\n"),
                         NEIGHBORHOOD_BODY, pack="house")
    assert created.startswith("created"), created
    page = tmp_path / "wiki" / rel
    return m, rel, page


def test_a_conflicting_pack_identity_field_leaves_the_body_byte_identical(tmp_path):
    m, rel, page = _owned_page(tmp_path)
    before = _body_bytes(page)
    out = m._converge(rel, _fm("Parcel neighborhood, Synthetic Ct", "https://x.test/a",
                               "properties/example-rd-101"),
                      "One unrelated parcel.\n", pack="operator")
    assert out.startswith("converged into"), out
    assert "body kept (identity conflict on `property`)" in out, out
    assert _body_bytes(page) == before, "the owner's body must stay byte-identical"
    fm, _ = m._read_page(page)
    assert fm["property"] == "properties/synthetic-ct-10"
    queue = (tmp_path / "wiki" / "_review-queue.md").read_text()
    assert "identity field conflict (`property`)" in queue


def test_a_conflicting_title_leaves_the_body_byte_identical(tmp_path):
    """`title` is engine-generic identity (the minted slug is derived from it)."""
    m, rel, page = _owned_page(tmp_path)
    before = _body_bytes(page)
    out = m._converge(rel, _fm("Parcel record, Example Rd 101", "https://x.test/a",
                               "properties/synthetic-ct-10"),
                      "One unrelated parcel.\n", pack="operator")
    assert "body kept (identity conflict on `title`)" in out, out
    assert _body_bytes(page) == before


def test_a_non_identity_conflict_still_replaces_the_body(tmp_path):
    """NEGATIVE: only identity fields withhold the body; the pre-#901 behavior is otherwise kept."""
    m, rel, page = _owned_page(tmp_path)
    out = m._converge(rel, _fm("Parcel neighborhood, Synthetic Ct", "https://x.test/a",
                               "properties/synthetic-ct-10", "publisher: Other Office\n"),
                      "A revised neighborhood survey.\n", pack="operator")
    assert "1 conflict" in out and "body kept" not in out, out
    assert m._read_page(page)[1].strip() == "A revised neighborhood survey."


def test_an_identity_conflict_without_a_body_adds_no_body_note(tmp_path):
    m, rel, page = _owned_page(tmp_path)
    before = _body_bytes(page)
    out = m._converge(rel, _fm("Parcel neighborhood, Synthetic Ct", "https://x.test/a",
                               "properties/other"), "", pack="operator")
    assert "1 conflict" in out and "body kept" not in out, out
    assert _body_bytes(page) == before


@pytest.mark.parametrize("declared", ["property", "[7, '', property2]"])
def test_a_malformed_identity_fields_declaration_is_ignored(tmp_path, declared):
    """A non-list, or non-string/blank entries, declare nothing; the engine fields still apply."""
    schema = OWNED_SCHEMA.replace("[property]", declared)
    m = _load(tmp_path, schema)
    rel = "sources/2026/09/24/synthetic-ct.md"
    assert m._converge(rel, _fm("T", "https://x.test/a", "properties/a"),
                       NEIGHBORHOOD_BODY, pack="house").startswith("created")
    out = m._converge(rel, _fm("T", "https://x.test/a", "properties/b"),
                      "Replacement.\n", pack="operator")
    assert "body kept" not in out, f"`property` is not declared here: {out}"
    out = m._converge(rel, _fm("T2", "https://x.test/a", "properties/a"),
                      "Replacement two.\n", pack="operator")
    assert "body kept (identity conflict on `title`)" in out, out


def test_identity_field_conflicts_is_order_preserving_and_type_scoped(tmp_path):
    m = _load(tmp_path)
    schema = {"types": {"source": {"identity_fields": ["property"]}}}
    conflicts = [("publisher", "a", "b"), ("property", "x", "y"), ("title", "t", "u")]
    assert m._identity_field_conflicts(schema, "source", conflicts) == ["property", "title"]
    assert m._identity_field_conflicts(schema, "vendor", conflicts) == ["title"]
    assert m._identity_field_conflicts({}, "source", conflicts) == ["title"]
    assert m._identity_field_conflicts({"types": {"source": "x"}}, "source",
                                       [("subject", 1, 2), ("name", 1, 2), ("url", 1, 2)]) == [
        "subject", "name", "url"]


def test_the_same_url_dedup_lane_never_groups_a_bare_endpoint(tmp_path, monkeypatch):
    """dedup-same-url groups by the write path's `_norm_url`, so records sharing a bare endpoint
    are never planned as one document to merge; a shared record URL still is."""
    m = _load(tmp_path)
    monkeypatch.syspath_prepend(str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("dedup_same_url_901",
                                                  REPO / "scripts" / "cron" / "dedup_same_url.py")
    lane = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lane)
    record = ENDPOINT + "?where=ACCTID%3D1"
    pages = {
        "sources/a": ({"title": "Parcel read", "url": ENDPOINT}, "A"),
        "sources/b": ({"title": "Parcel read", "url": ENDPOINT}, "B"),
        "sources/c": ({"title": "Account one", "url": record}, "C"),
        "sources/d": ({"title": "Account one", "url": record}, "D"),
    }
    groups, placeholders = lane.plan(m, pages)
    grouped = {r for g in groups for r in [g["survivor"], *g["losers"]]}
    assert grouped == {"sources/c", "sources/d"}, groups
    assert placeholders == []


def test_an_applied_converge_body_is_still_checked_for_degeneration(tmp_path):
    """The body gates follow the body that is actually written: an APPLIED body still gets the
    repetition-loop review flag (M15); a withheld one is never judged in its place."""
    m, rel, page = _owned_page(tmp_path)
    loop = " ".join(["parcel"] * 300) + "\n"
    out = m._converge(rel, _fm("Parcel neighborhood, Synthetic Ct", "https://x.test/a",
                               "properties/synthetic-ct-10"), loop, pack="house")
    assert "flagged for review" in out, out
    assert "degenerate: 300-word unpunctuated run" in (
        tmp_path / "wiki" / "_review-queue.md").read_text()
