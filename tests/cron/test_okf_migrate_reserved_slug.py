"""okengine#997: no writer may MINT a page under a reserved or dot-leading basename.

Two live pages sat under names the OKF contract reserves: a `type: publisher` page minted as
`entities/h/e/health.md` by a no_agent importer (through `okf_migrate.write_key`), and a concept
page at `concepts/_/.net-framework-code-injection.md`. The write path refuses both names and the
validator, id-index and INDEX builders skip them, so neither page could be maintained or listed.

`write_key` is the chokepoint every partition-aware direct writer (and the write path's own
create placement) crosses, so it is where a reserved slug is disambiguated. The reserved set is
read from tools/schema_validator — the source the write-path refusal is already bound to — never
copied. `reserved_name_pages` is the shared detector the deployment check and the repair lane use.
"""
import importlib
import pathlib
import sys

import pytest

yaml = pytest.importorskip("yaml")
REPO = pathlib.Path(__file__).resolve().parents[2]


def _mod():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import okf_migrate
    return importlib.reload(okf_migrate)


def _sv():
    sys.path.insert(0, str(REPO))
    from tools import schema_validator
    return schema_validator


SCHEMA = (
    "partitioning:\n  namespaces:\n"
    "    entities: {strategy: by-letter, reshard_by: second-letter}\n"
    "    concepts: {strategy: by-letter}\n"
    "    notes: {strategy: flat}\n"
    "types:\n  publisher: {}\n  concept: {}\n"
)


def _vault(tmp_path, schema=SCHEMA):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(schema)
    (tmp_path / "wiki").mkdir()
    return tmp_path


def _page(root, rel, fm="type: concept\n", body="body\n"):
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n{body}" if fm is not None else body)
    return p


# ── the minting guard ────────────────────────────────────────────────────────────────────────────

def test_reserved_slug_is_disambiguated_by_type(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    assert m.write_key(root, "entities", "health", {"type": "publisher"}) == \
        "entities/h/e/health-publisher"


def test_reserved_slug_is_case_insensitive(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    assert m.write_key(root, "entities", "HEALTH", {"type": "publisher"}) == \
        "entities/h/e/HEALTH-publisher"
    assert m.write_key(root, "entities", "Readme", {"type": "publisher"}) == \
        "entities/r/e/Readme-publisher"


def test_every_engine_reserved_basename_is_never_minted(tmp_path):
    """Derived from the validator's own default set, so a name added there is covered here."""
    m = _mod()
    root = _vault(tmp_path)
    for name in _sv()._OKF_RESERVED_DEFAULT:
        stem = name[:-3]
        key = m.write_key(root, "concepts", stem, {"type": "concept"})
        # `index-concept` is itself reserved (every `index-*` is), so `index` takes the prefix form
        want = "concept-index" if stem == "index" else f"{stem}-concept"
        assert pathlib.PurePosixPath(key).name == want, key
        assert not m.reserved_basename(root / "wiki" / f"{key}.md"), key


def test_leading_dot_slug_becomes_dot_word(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    assert m.write_key(root, "concepts", ".net-framework-code-injection", {"type": "concept"}) == \
        "concepts/d/dotnet-framework-code-injection"
    assert m.mintable_slug(root, "concepts", "..x", {}) == "dotdotx"


def test_untyped_reserved_slug_falls_back_to_page(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    assert m.write_key(root, "notes", "log") == "notes/log-page"
    # `index-page` is itself reserved (every `index-*` is), so the kind moves to the front
    assert m.mintable_slug(root, "notes", "index", {"type": "  "}) == "page-index"


def test_flat_namespace_is_guarded_too(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    assert m.write_key(root, "notes", "agents", {"type": "Threat Note"}) == \
        "notes/agents-threat-note"


def test_generated_structural_shapes_are_never_minted(tmp_path):
    """The validator also exempts the INDEX family and `_` scaffolding from conformance; a page
    minted under one of those shapes would be just as invisible. A type suffix cannot rescue a
    prefix rule, so the type moves to the front."""
    m = _mod()
    root = _vault(tmp_path)
    assert m.mintable_slug(root, "concepts", "index-p01", {"type": "concept"}) == \
        "concept-index-p01"
    assert m.mintable_slug(root, "concepts", "Index-P7", {"type": "concept"}) == \
        "concept-Index-P7"
    assert m.mintable_slug(root, "concepts", "index-funds", {"type": "concept"}) == \
        "concept-index-funds"
    assert m.mintable_slug(root, "concepts", "_scratch", {"type": "concept"}) == "concept-_scratch"
    assert m.mintable_slug(root, "concepts", "INDEX-glossary", {"type": "concept"}) == \
        "concept-INDEX-glossary"


@pytest.mark.parametrize("slug", ["healthcare", "net-framework", "health-isac",
                                  "dotnet", "logistics", "hot-wallet", "readme-generator",
                                  "bundle-pricing", "agents-of-chaos", "a.health",
                                  "indexing", "reindex-funds", "pricing-index"])
def test_near_misses_pass_unchanged(tmp_path, slug):
    m = _mod()
    root = _vault(tmp_path)
    assert m.mintable_slug(root, "concepts", slug, {"type": "concept"}) == slug
    assert not m.reserved_basename(root / "wiki" / "concepts" / f"{slug}.md")


def test_pack_reserved_files_are_honored_at_any_depth(tmp_path):
    m = _mod()
    root = _vault(tmp_path, SCHEMA + "reserved_files: [Overview.md]\n")
    assert m.reserved_basename(root / "wiki" / "concepts" / "o" / "v" / "overview.md")
    assert m.write_key(root, "concepts", "overview", {"type": "concept"}) == \
        "concepts/o/overview-concept"


def test_sub_domain_reserved_files_are_honored(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    sub = root / "wiki" / "market"
    sub.mkdir()
    (sub / "schema.yaml").write_text(SCHEMA + "reserved_files: [briefing.md]\n")
    assert m.write_key(root, "market/concepts", "briefing", {"type": "concept"}) == \
        "market/concepts/b/briefing-concept"
    # the sibling root namespace is governed by the root schema, which reserves nothing extra
    assert m.write_key(root, "concepts", "briefing", {"type": "concept"}) == "concepts/b/briefing"


def test_suffix_then_prefix_then_refuse(tmp_path):
    m = _mod()
    root = _vault(tmp_path, SCHEMA + "reserved_files: [health-publisher.md]\n")
    assert m.mintable_slug(root, "entities", "health", {"type": "publisher"}) == "publisher-health"
    root2 = _vault(tmp_path / "v2", SCHEMA
                   + "reserved_files: [health-publisher.md, publisher-health.md]\n")
    with pytest.raises(ValueError, match="no mintable basename"):
        m.mintable_slug(root2, "entities", "health", {"type": "publisher"})


def test_existing_reserved_page_is_updated_in_place_not_forked(tmp_path):
    """A page minted before the guard keeps receiving its importer's updates where it sits —
    forking a second copy beside it is the okengine#54 loop. The detector reports it and the
    repair lane renames it (links rewritten) — never an importer."""
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"type": "publisher"}) == "entities/h/e/health"


def test_existing_prefix_shaped_page_is_updated_in_place(tmp_path):
    """A stand-in can sort BEFORE the legacy slug (`concept-index-pricing` < `index-pricing`); the
    legacy page must still be found."""
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "concepts/i/index-pricing.md")
    assert m.write_key(root, "concepts", "index-pricing", {"type": "concept"}) == \
        "concepts/i/index-pricing"


def test_once_renamed_the_safe_page_wins(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health-publisher.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"type": "publisher"}) == \
        "entities/h/e/health-publisher"
    # even if a stale reserved copy is still lying around, it never wins over the safe page
    _page(root, "entities/h/e/health.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"type": "publisher"}) == \
        "entities/h/e/health-publisher"


def test_non_reserved_slug_path_is_unchanged(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    assert m.write_key(root, "entities", "acme", {"type": "publisher"}) == "entities/a/c/acme"


# ── the contract import (single source) ──────────────────────────────────────────────────────────

def test_reserved_set_is_the_validators_not_a_copy(tmp_path):
    m = _mod()
    assert m._reserved_contract() is _sv()


def test_contract_import_bootstraps_paths_when_not_importable(tmp_path, monkeypatch):
    """Loaded by path (a pack's cron env, an external consumer) `tools` may not be importable until
    the engine root is on sys.path; the lazy import adds it and retries rather than failing open."""
    m = _mod()
    sv = _sv()
    calls = []
    real = m._import_paths

    def unblock():
        calls.append(True)
        del sys.modules["tools"]
        real()

    monkeypatch.setitem(sys.modules, "tools", None)          # first import -> ModuleNotFoundError
    monkeypatch.setattr(m, "_import_paths", unblock)
    monkeypatch.setattr(m, "_RESERVED_CONTRACT", None)
    assert m._reserved_contract() is sv
    assert calls == [True]
    assert m._reserved_contract() is sv and calls == [True]  # cached: bootstraps once


def test_import_paths_appends_the_engine_root(monkeypatch):
    m = _mod()
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in (str(REPO), "/opt/hermes")])
    m._import_paths()
    assert sys.path[-2:] == [str(REPO), "/opt/hermes"]      # appended: an installed copy wins


def test_contract_bootstrap_tolerates_a_wheel_without_engine_package(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setitem(sys.modules, "engine_package", None)   # import -> ModuleNotFoundError
    m._import_paths()                                          # must not raise
    assert str(REPO) in sys.path


# ── the shared detector ──────────────────────────────────────────────────────────────────────────

def test_detector_finds_reserved_and_dot_content_pages(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health.md", "type: publisher\nid: health\n")
    _page(root, "concepts/_/.net-framework-code-injection.md",
          "type: concept\nstatus: tombstoned\n")
    _page(root, "entities/i/n/Index.md", "type: dashboard\n")
    # skipped files that sort BEFORE the offenders: a skip must never end the scan
    _page(root, "HEALTH.md", "type: dashboard\n")                  # domain root
    _page(root, "aaa/a/acme.md")                                    # not a reserved name
    assert m.reserved_name_pages(root) == [
        ("concepts/_/.net-framework-code-injection.md", True),
        ("entities/h/e/health.md", False),
        ("entities/i/n/Index.md", False),
    ]


def test_detector_negative_fixture_generated_and_structural_files_pass(tmp_path):
    """Everything an engine lane legitimately writes under a reserved name — root dashboards, a
    sub-domain's own root files, the per-directory INDEX tree, derived namespaces, hidden dirs —
    and structural files with no `type`, plus the near-miss names, must NOT be flagged."""
    m = _mod()
    root = _vault(tmp_path)
    for name in ("HEALTH.md", "HOT.md", "BUNDLE.md", "index.md", "log.md", "AGENTS.md"):
        _page(root, name, "type: dashboard\n")
    sub = root / "wiki" / "market"
    sub.mkdir()
    (sub / "schema.yaml").write_text(SCHEMA)
    _page(root, "market/HEALTH.md", "type: dashboard\n")
    _page(root, "entities/h/e/INDEX.md", "type: dashboard\n")
    _page(root, "dashboards/HOT.md", "type: dashboard\n")
    _page(root, "operational/health.md", "type: dashboard\n")
    _page(root, "market/dashboards/hot.md", "type: dashboard\n")
    _page(root, "concepts/.hidden/health.md", "type: concept\n")
    _page(root, "sources/index.md", None, "# legacy index, no frontmatter\n")
    _page(root, "concepts/README.md", "title: about\n")
    _page(root, "concepts/r/README.md", "type: ''\n")
    for slug in ("healthcare", "net-framework", "indexing"):
        _page(root, f"concepts/{slug[0]}/{slug}.md")
    assert m.reserved_name_pages(root) == []


def test_detector_reads_frontmatter_robustly(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "concepts/h/health.md", "type: [unclosed\n")      # unparseable -> not a typed page
    _page(root, "concepts/l/log.md", "- a list\n")                 # non-mapping -> not a typed page
    assert m.reserved_name_pages(root) == []


def test_detector_without_wiki_is_empty(tmp_path):
    m = _mod()
    assert m.reserved_name_pages(tmp_path) == []


# ── QA round 1 (okengine#997 !1277): B1, S1, S2, S3 ──────────────────────────────────────────────

def test_index_slug_never_resolves_to_a_generated_INDEX(tmp_path):
    """B1: case-insensitive lookup made `index` resolve to the deepest generated INDEX.md."""
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/a/c/acme.md", "type: dashboard\n")
    (root / "wiki/entities/a/INDEX.md").write_text("# generated index\n")
    (root / "wiki/entities/a/c/INDEX-p02.md").write_text("# generated page 2\n")
    assert m.find_page(root, "entities", "index") is None
    assert m.write_key(root, "entities", "index", {"type": "dashboard"}) == \
        "entities/d/a/dashboard-index"


def test_find_page_never_returns_generated_files_and_reserved_stems_match_exact_case(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/HEALTH.md", "type: dashboard\n")
    _page(root, "entities/_/_review-queue.md", "type: dashboard\n")
    _page(root, "entities/c/v/CVE-1.md", "type: concept\n")
    assert m.find_page(root, "entities", "health") is None          # reserved: exact case only
    assert m.find_page(root, "entities", "HEALTH") == root / "wiki/entities/h/e/HEALTH.md"
    assert m.find_page(root, "entities", "_review-queue") is None   # generated scaffolding
    # an ordinary slug keeps its case-insensitive match (okengine#883)
    assert m.find_page(root, "entities", "cve-1") == root / "wiki/entities/c/v/CVE-1.md"


def test_canonical_assemble_never_writes_into_a_generated_INDEX(tmp_path):
    """B1, second caller: canonical_assemble resolves its seat with find_page directly."""
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import canonical_assemble as ca
    importlib.reload(ca)
    root = _vault(tmp_path)
    index = root / "wiki/entities/i/n/INDEX.md"
    index.parent.mkdir(parents=True)
    index.write_text("---\ntype: dashboard\n---\n# generated\n")
    ca.write_canonical(root, "index", "organization", {"type": "organization", "name": "Index"},
                       [], [], {}, "2026-10-07")
    assert index.read_text() == "---\ntype: dashboard\n---\n# generated\n"
    assert (root / "wiki/entities/o/r/organization-index.md").is_file()


def test_post_repair_other_type_writer_converges(tmp_path):
    """S1: after the repair renamed health -> health-publisher, an organization writer must not
    fork health-organization."""
    m = _mod()
    root = _vault(tmp_path, SCHEMA + "  organization: {}\n")
    _page(root, "entities/h/e/health-publisher.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"type": "organization"}) == \
        "entities/h/e/health-publisher"


def test_post_repair_untyped_writer_converges(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health-publisher.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", None) == "entities/h/e/health-publisher"
    _page(root, "concepts/c/concept-index-p01.md")                 # prefix form, too
    assert m.write_key(root, "concepts", "index-p01", None) == "concepts/c/concept-index-p01"


def test_standin_with_the_writers_id_wins_and_retired_or_generated_ones_never_do(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health-concept.md", "type: concept\nid: other\n")
    _page(root, "entities/h/e/health-publisher.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"type": "page", "id": "health"}) == \
        "entities/h/e/health-publisher"
    assert m.write_key(root, "entities", "health", {"type": "page"}) == \
        "entities/h/e/health-concept"                               # no id: first by path
    root2 = _vault(tmp_path / "v2")
    _page(root2, "entities/h/e/health-publisher.md", "type: publisher\nstatus: tombstoned\n")
    _page(root2, "concepts/i/n/INDEX-concept.md", "type: dashboard\n")
    assert m.write_key(root2, "entities", "health", {"type": "concept"}) == \
        "entities/h/e/health-concept"
    assert m.write_key(root2, "concepts", "INDEX", {"type": "concept"}) == "concepts/c/concept-INDEX"


def test_a_tombstone_at_the_legacy_name_holds_no_seat(tmp_path):
    """S2 (#663): a retired `.net-x` must not swallow the write; the stand-in is minted."""
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "concepts/_/.net-x.md",
          "type: concept\nstatus: tombstoned\nsuperseded_by: concepts/n/net-x\n")
    assert m.write_key(root, "concepts", ".net-x", {"type": "concept"}) == "concepts/d/dotnet-x"
    _page(root, "concepts/_/.live.md")                              # a LIVE legacy page still wins
    assert m.write_key(root, "concepts", ".live", {"type": "concept"}) == "concepts/_/.live"


def test_detector_flags_every_shape_the_guard_blocks(tmp_path):
    """S3: the detector shares the predicate, so `_x` and `index-pNN` CONTENT pages are flagged;
    the engine's own generated INDEX tree and scaffolding (dashboards) are not."""
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "concepts/_/_board-q.md", "type: board-question\n")
    _page(root, "concepts/i/index-p02.md", "type: concept\n")
    _page(root, "concepts/i/INDEX-glossary.md", "type: concept\n")
    _page(root, "concepts/i/INDEX-p03.md", "type: dashboard\n")
    _page(root, "concepts/_/_review-queue.md", "type: dashboard\n")
    _page(root, "concepts/i/index-funds.md", "type: concept\n")
    _page(root, "concepts/i/indexing.md", "type: concept\n")
    assert [rel for rel, _ in m.reserved_name_pages(root)] == [
        "concepts/_/_board-q.md", "concepts/i/INDEX-glossary.md", "concepts/i/index-funds.md",
        "concepts/i/index-p02.md"]


def test_contract_prefers_the_sibling_validator_over_an_installed_one(monkeypatch):
    """A host venv carries a stale installed `tools`; in a checkout the sibling validator is the
    matching version, so it goes first on sys.path when nothing has imported `tools` yet."""
    m = _mod()
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(REPO)])
    for name in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(m, "_RESERVED_CONTRACT", None)
    sv = m._reserved_contract()
    assert sys.path[0] == str(REPO)
    assert pathlib.Path(sv.__file__).resolve() == (REPO / "tools" / "schema_validator.py").resolve()


def test_contract_refuses_a_validator_that_predates_the_shared_predicate(monkeypatch):
    import types
    m = _mod()
    stale = types.ModuleType("tools.schema_validator")
    stale.__file__ = "/opt/hermes/tools/schema_validator.py"
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    pkg.schema_validator = stale
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.schema_validator", stale)
    monkeypatch.setattr(m, "_RESERVED_CONTRACT", None)
    with pytest.raises(ImportError, match="predates okengine#997"):
        m._reserved_contract()


# ── mutation-driven boundaries ────────────────────────────────────────────────────────────────────

def test_a_stray_legacy_copy_never_outranks_a_stand_in(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "aaa/x.md")                                         # unrelated page sorts first
    _page(root, "entities/a/health.md", "type: publisher\n")        # stray legacy copy sorts first
    _page(root, "entities/a/acme.md", "type: publisher\n")          # non-matching page sorts first
    _page(root, "entities/h/e/health-publisher.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"type": "publisher"}) == \
        "entities/h/e/health-publisher"


def test_only_an_exact_id_match_is_owned(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health-concept.md", "type: concept\nid: aaa\n")   # sorts before
    _page(root, "entities/h/e/health-publisher.md", "type: publisher\nid: health\n")
    assert m.write_key(root, "entities", "health", {"id": "health"}) == \
        "entities/h/e/health-publisher"


def test_reserved_upper_case_slug_never_matches_a_lower_case_page(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "entities/h/e/health.md", "type: publisher\n")
    assert m.find_page(root, "entities", "HEALTH") is None


def test_a_scaffold_shaped_legacy_page_is_never_a_writers_seat(tmp_path):
    """find_page never returns `_` scaffolding (a pack template), so a writer minting `_x` gets
    its stand-in instead of writing into the scaffold."""
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "concepts/_/_x.md", "type: '2024'\n")
    assert m.write_key(root, "concepts", "_x", {"type": "2024"}) == "concepts/0-9/2024-_x"


def test_contract_leaves_sys_path_alone_once_tools_is_imported(monkeypatch):
    m = _mod()
    _sv()                                                           # `tools` imported
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(REPO)])
    monkeypatch.setattr(m, "_RESERVED_CONTRACT", None)
    m._reserved_contract()
    assert str(REPO) not in sys.path


def test_detector_flags_a_typed_scaffold_whatever_its_type_sorts_after(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    _page(root, "predictions/_template.md", "type: prediction\n")   # "prediction" > "dashboard"
    assert m.reserved_name_pages(root) == [("predictions/_template.md", False)]
