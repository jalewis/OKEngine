"""Observable contracts of the write path's deduplication service (okengine#612).

Each test loads the enforced write server (`okengine-mcp/write_server.py`) against a real temp
vault and asserts what a caller, a reviewer, or the next writer observes: the review-queue
file and its ordering, the log ledger, which existing page an alias-bearing create converges
into or is refused against, and how source URL identity decides a collision.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  entity: {}
  concept: {}
  source: {}
strict_types: false
partitioning:
  namespaces:
    entities: {strategy: flat}
    concepts: {strategy: flat}
    sources: {strategy: flat}
    topics: {strategy: flat}
"""


def _load(root: Path, monkeypatch, schema: str = _SCHEMA):
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(schema, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    name = f"write_server_dedup_mutation_{id(root)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch), tmp_path


def _fm(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n", 2)[1]) or {}


def _write_page(page: Path, fm: dict, body: str = "body\n") -> None:
    """A page placed on disk by a no_agent lane, not through the write server."""
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body,
                    encoding="utf-8")


def _log(root: Path) -> list[str]:
    log = root / "wiki" / "log.md"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --- review queue ordering -------------------------------------------------------------------


def test_a_new_row_goes_above_existing_rows_below_the_preamble(vault):
    m, _ = vault
    text = "---\ntitle: Queue\n---\n# Queue\n\n- old row\n- older row\n"
    assert m._prepend_queue_row(text, "- new row\n") == (
        "---\ntitle: Queue\n---\n# Queue\n\n- new row\n- old row\n- older row\n"
    )


def test_a_first_row_follows_the_preamble_after_one_blank_line(vault):
    m, _ = vault
    assert m._prepend_queue_row("# Queue", "- row\n") == "# Queue\n\n- row\n"
    assert m._prepend_queue_row("# Queue\n", "- row\n") == "# Queue\n\n- row\n"
    assert m._prepend_queue_row("# Queue\n\n", "- row\n") == "# Queue\n\n- row\n"


def test_a_row_in_an_empty_queue_has_no_leading_blank_lines(vault):
    m, _ = vault
    assert m._prepend_queue_row("", "- row\n") == "- row\n"


def test_queueing_creates_a_missing_vault_tree(tmp_path, monkeypatch):
    m = _load(tmp_path / "boot", monkeypatch)
    fresh = tmp_path / "new" / "vault"
    monkeypatch.setenv("WIKI_PATH", str(fresh))

    assert m._append_review_queue_once(fresh / "wiki" / "concepts" / "x.md", "why") is True

    assert "**concepts/x.md** — why" in (fresh / "wiki" / "_review-queue.md").read_text("utf-8")


def test_queue_review_logs_a_new_and_an_already_queued_flag_distinctly(vault):
    m, root = vault
    assert m._create("concepts/x", {"type": "concept", "name": "X"}, "b") == "created concepts/x.md v1"
    page = root / "wiki" / "concepts" / "x.md"

    first = m._queue_review(page, ["reason one"])
    second = m._queue_review(page, ["reason two", "reason three"])

    assert first == " — flagged for review (1 reason(s))"
    assert second == " — already queued for review (2 reason(s))"
    flags = [line for line in _log(root) if "review-flag" in line]
    assert flags == [
        "- 2026-06-15 mcp-write review-flag concepts/x.md — reason one",
        "- 2026-06-15 mcp-write review-flag already-queued concepts/x.md — reason two; reason three",
    ]


# --- alias matching --------------------------------------------------------------------------


@pytest.mark.parametrize("cur,expected", [
    ({"name": "Acme Corp", "title": "Other"}, "acme-corp"),
    ({"title": "Acme Corp"}, "acme-corp"),
    ({}, "stem-only"),
])
def test_existing_page_name_falls_back_to_title_then_stem(vault, cur, expected):
    m, _ = vault
    stem = "stem-only"
    norm = m.id_lib.normalize_key
    target = norm({"acme-corp": "Acme Corp", "stem-only": "stem-only"}[expected])
    assert m._alias_matches(cur, stem, "nobody", {target}) is True
    others = {"acme-corp", "stem-only", norm("Other")} - {target}
    assert m._alias_matches(cur, stem, "nobody", {norm(o) for o in others}) is False


# --- alias hits: index and disk-scan candidates ---------------------------------------------


def _entity(root: Path, rel: str, name: str, aliases=None, **extra) -> Path:
    fm = {"type": "entity", "name": name, "id": "entities:" + Path(rel).stem}
    if aliases:
        fm["aliases"] = aliases
    fm.update(extra)
    page = root / "wiki" / rel
    _write_page(page, fm)
    return page


def _hits(m, page: Path, name: str, aliases=()) -> list[str]:
    norm = m.id_lib.normalize_key
    found = m._alias_hits(page, norm(name), {norm(a) for a in aliases})
    return [m._rel(p) for p, _ in found]


def test_index_hits_skip_a_stale_entry_and_keep_scanning(vault):
    m, root = vault
    first = _entity(root, "entities/a-first.md", "First", ["Shared Name"])
    _entity(root, "entities/b-second.md", "Second", ["Shared Name"])
    m._registry()  # built from disk: both pages carry the alias
    first.unlink()

    hits = _hits(m, root / "wiki/entities/newco.md", "Shared Name")

    assert hits == ["entities/b-second.md"]


def test_index_hits_skip_a_tombstoned_page_and_keep_a_live_one(vault):
    m, root = vault
    _entity(root, "entities/a-first.md", "First", ["Shared Name"], status="tombstoned")
    _entity(root, "entities/b-second.md", "Second", ["Shared Name"], status="verified")
    m._registry()

    hits = _hits(m, root / "wiki/entities/newco.md", "Shared Name")

    assert hits == ["entities/b-second.md"]


def test_index_hits_never_cross_into_a_sub_domain(vault):
    m, root = vault
    (root / "wiki" / "acme").mkdir(parents=True)
    (root / "wiki" / "acme" / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    _entity(root, "acme/entities/acme-ent.md", "Acme Ent", ["Shared Name"])
    _entity(root, "entities/root-ent.md", "Root Ent", ["Shared Name"])
    m._registry()

    assert _hits(m, root / "wiki/entities/newco.md", "Shared Name") == ["entities/root-ent.md"]
    assert _hits(m, root / "wiki/acme/entities/newco.md", "Shared Name") == [
        "acme/entities/acme-ent.md"
    ]


def test_an_index_with_names_but_no_aliases_is_consulted_not_bypassed(vault):
    m, root = vault
    _entity(root, "entities/known.md", "Known")
    reg = m._registry()
    assert reg.name_to_rels and not reg.alias_to_rels, "fixture needs a names-only index"
    # A lane writes an alias-bearing page after the index was built; the index does not hold it.
    _entity(root, "entities/later.md", "Later", ["NewCo"])

    assert _hits(m, root / "wiki/entities/newco.md", "NewCo") == []


def test_disk_scan_fallback_stays_inside_the_incoming_sub_domain(vault):
    m, root = vault
    reg = m._registry()  # built over an empty vault: no identity maps, so dedup scans disk
    assert not reg.name_to_rels and not reg.alias_to_rels
    (root / "wiki" / "acme").mkdir(parents=True)
    (root / "wiki" / "acme" / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    _entity(root, "acme/entities/acme-ent.md", "Acme Ent", ["NewCo"])
    _entity(root, "entities/root-ent.md", "Root Ent", ["NewCo"])

    assert _hits(m, root / "wiki/acme/entities/newco.md", "NewCo") == ["acme/entities/acme-ent.md"]
    assert _hits(m, root / "wiki/entities/newco.md", "NewCo") == ["entities/root-ent.md"]


# --- create-time dedup decisions -------------------------------------------------------------


def test_a_url_derived_source_id_is_recognized_by_shape(vault):
    m, _ = vault
    url_id = m.source_url_id("https://news.site.io/a")
    assert url_id.startswith("sources:url-")
    assert m._is_url_derived_source_id(url_id) is True
    assert m._is_url_derived_source_id(None) is False


@pytest.mark.parametrize("existing,incoming", [
    ("https://news.site.io/a", "https://news.site.io/b"),
    ("https://news.site.io/b", "https://news.site.io/a"),
])
def test_different_source_urls_are_not_the_same_document(vault, existing, incoming):
    m, root = vault
    page = root / "wiki/sources/held.md"
    _write_page(page, {"type": "source", "url": existing})
    assert m._same_source_url({"url": incoming}, page) is False


def test_equal_source_urls_are_the_same_document(vault):
    m, root = vault
    page = root / "wiki/sources/held.md"
    _write_page(page, {"type": "source", "url": "https://news.site.io/a/"})
    assert m._same_source_url({"url": "".join(list("https://news.site.io/a"))}, page) is True


@pytest.mark.parametrize("namespace,page_type", [("concepts", "concept"), ("sources", "source")])
def test_alias_dedup_runs_only_for_entities(vault, namespace, page_type):
    m, root = vault
    _entity(root, "entities/existing-corp.md", "Existing Corp")
    fields = {"type": page_type, "name": "Other", "aliases": ["Existing Corp"]}
    if page_type == "source":
        fields["url"] = "https://news.site.io/other"
    result = m._create(f"{namespace}/other", fields, "body")
    assert result == f"created {namespace}/other.md v1", result


def test_an_incoming_title_is_its_name_for_alias_dedup(vault):
    m, root = vault
    _entity(root, "entities/existing-corp.md", "Existing Corp", ["Titled Thing"])
    result = m._create("entities/newco", {"type": "entity", "title": "Titled Thing"}, "body")
    assert not (root / "wiki/entities/newco.md").exists(), result
    assert "existing-corp" in result


def test_an_ambiguous_alias_refusal_lists_at_most_five_canonicals(vault):
    m, root = vault
    for n in range(6):
        _entity(root, f"entities/holder-{n}.md", f"Holder {n}", ["Shared Name"])
    result = m._create("entities/newco", {"type": "entity", "name": "Shared Name"}, "body")
    listed = ", ".join(f"entities/holder-{n}.md" for n in range(5))
    assert result == (
        f"refused: entity alias matches multiple canonicals ({listed}) — flagged for review"
    )


@pytest.mark.parametrize("namespace,page_type", [("concepts", "concept"), ("topics", "topic")])
def test_only_source_pages_take_a_url_derived_id(vault, namespace, page_type):
    m, root = vault
    result = m._create(f"{namespace}/doc", {"type": page_type, "name": "Doc",
                                            "url": "https://news.site.io/doc"}, "body")
    assert result == f"created {namespace}/doc.md v1", result
    assert _fm(root / f"wiki/{namespace}/doc.md")["id"] == f"{namespace}:doc"


def _source(root: Path, rel: str, **fm) -> Path:
    page = root / "wiki" / rel
    _write_page(page, {"type": "source", **fm})
    return page


def test_an_unresolved_url_id_still_checks_the_title_slug_holder(vault):
    m, root = vault
    _source(root, "sources/a/foo.md", id="sources:foo", title="Foo")
    result = m._create("sources/b/foo", {"type": "source", "title": "Foo",
                                         "url": "https://news.site.io/new"}, "body")
    assert result.startswith("refused: weak source slug id sources:foo already used by "), result
    assert not (root / "wiki/sources/b/foo.md").exists()


def test_a_stale_url_index_entry_still_checks_the_title_slug_holder(vault):
    m, root = vault
    url = "https://news.site.io/moved"
    stale = _source(root, "sources/c/old.md", id=m.source_url_id(url), url=url, title="Old")
    _source(root, "sources/a/foo.md", id="sources:foo", title="Foo")
    m._registry()  # the index now maps the url id to sources/c/old.md
    stale.unlink()
    result = m._create("sources/b/foo", {"type": "source", "title": "Foo", "url": url}, "body")
    assert result.startswith("refused: weak source slug id sources:foo already used by "), result


def test_a_url_identity_match_wins_over_a_later_title_slug_match(vault):
    m, root = vault
    url = "https://news.site.io/shared"
    _source(root, "sources/c/by-url.md", id=m.source_url_id(url), url=url, title="By Url")
    _source(root, "sources/a/foo.md", id="sources:foo", title="Foo")
    result = m._create("sources/b/foo", {"type": "source", "title": "Foo", "url": url}, "body")
    assert "sources/c/by-url.md" in result, result
    assert not (root / "wiki/sources/b/foo.md").exists()


def test_disk_scan_skips_a_tombstoned_page_and_keeps_scanning(vault):
    m, root = vault
    m._registry()  # empty vault: no identity maps, so dedup scans the entities tree on disk
    # rglob yields a directory's own files before descending, so the tombstone is seen first.
    _entity(root, "entities/a-gone.md", "Gone", ["Shared Name"], status="Tombstoned")
    _entity(root, "entities/b/b-live.md", "Live", ["Shared Name"], status="verified")

    assert _hits(m, root / "wiki/entities/newco.md", "Shared Name") == ["entities/b/b-live.md"]
