"""okengine#933: a regenerated dashboard/report keeps its identity.

Generated pages are rewritten whole on every run. `generated_page` must (a) preserve an id already
on the page, (b) otherwise write exactly the id `backfill_ids` would stamp, so a later backfill is
a no-op, and (c) leave structural and non-page files alone. The negative fixture shows the
pre-fix behavior (a plain rewrite) drops the id the backfill stamped.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import generated_page as G  # noqa: E402
import id_lib  # noqa: E402


def _load_backfill():
    spec = importlib.util.spec_from_file_location("backfill_ids_933", REPO / "scripts" / "backfill_ids.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKFILL = _load_backfill()


def _vault(tmp_path: Path, schema: str = "types:\n  vendor: {required: [type]}\n") -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "schema.yaml").write_text(schema, encoding="utf-8")
    return tmp_path


def _fm(path: Path) -> dict:
    fm, _ = G.parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm


def _page(title: str | None = "Fleet health", extra: str = "") -> str:
    head = "---\ntype: dashboard\n" + (f'title: "{title}"\n' if title else "") + extra + "---\n"
    return head + "\n# body\n"


# ── parity with backfill_ids ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel, text", [
    ("dashboards/fleet-health.md", _page("Fleet health")),
    ("operational/observability-validation.md", _page("Observability validation")),
    ("operational/source-backlink-drain-2026-09-27.md", _page(None)),          # stem fallback
    ("dashboards/competitive/discovery.md", _page("Competitor discovery")),    # nested dir
    ("dashboards/named.md", "---\ntype: dashboard\nname: Named Only\n---\nbody\n"),
])
def test_written_id_is_what_backfill_would_stamp(tmp_path, rel, text):
    vault = _vault(tmp_path)
    path = vault / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    G.write(path, text)
    fm = _fm(path)
    expected, kind = BACKFILL.derive_for_page(vault, rel, {k: v for k, v in fm.items() if k != "id"})
    assert kind == "slug"
    assert fm["id"] == expected, f"{rel}: writer stamped {fm['id']!r}, backfill derives {expected!r}"
    result = BACKFILL.run(vault, apply=False)
    assert result["stamped"] == 0, "a later backfill must be a no-op on a routed page"


def test_authority_type_gets_the_authority_id(tmp_path):
    vault = _vault(tmp_path, "types:\n  report-card: {required: [type], id_authority: acme, id_field: ref}\n")
    path = vault / "wiki" / "dashboards" / "card.md"
    path.parent.mkdir(parents=True)
    G.write(path, "---\ntype: report-card\nref: RC-7\ntitle: Card\n---\nbody\n")
    assert _fm(path)["id"] == "acme:rc-7"
    assert BACKFILL.run(vault, apply=False)["stamped"] == 0


def test_id_is_the_first_frontmatter_line_and_body_is_preserved(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "fleet-health.md"
    path.parent.mkdir(parents=True)
    written = G.write(path, _page("Fleet health"))
    assert written.splitlines()[1] == 'id: "dashboards:fleet-health"'
    assert written.endswith("\n# body\n")
    assert path.read_text(encoding="utf-8") == written


# ── preservation ──────────────────────────────────────────────────────────────────────────

def test_existing_id_survives_a_whole_file_rewrite(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "fleet-health.md"
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: "legacy:kept-forever"\ntype: dashboard\n---\nold\n', encoding="utf-8")
    G.write(path, _page("A different title now"))
    assert _fm(path)["id"] == "legacy:kept-forever"
    assert "# body" in path.read_text(encoding="utf-8")


def test_repeated_rewrites_are_stable(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "operational" / "tier-distribution.md"
    path.parent.mkdir(parents=True)
    first = G.write(path, _page("Tier distribution"))
    second = G.write(path, _page("Tier distribution"))
    assert first == second


def test_writer_pinned_id_is_kept_verbatim(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "x.md"
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: "on:disk"\ntype: dashboard\n---\n', encoding="utf-8")
    text = "---\ntype: dashboard\nid: dashboard:pinned\ntitle: X\n---\nbody\n"
    assert G.with_id(path, text) == text


def test_blank_or_non_string_disk_id_is_not_preserved(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "fleet-health.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: 42\ntype: dashboard\n---\n", encoding="utf-8")
    assert _fm(Path(path))["id"] == 42
    G.write(path, _page("Fleet health"))
    assert _fm(path)["id"] == "dashboards:fleet-health"


# ── dated siblings: the backfill's collision disambiguation ─────────────────────────────────

def test_a_sibling_holding_the_derived_slug_forces_backfill_disambiguation(tmp_path):
    vault = _vault(tmp_path)
    ops = vault / "wiki" / "operational"
    ops.mkdir(parents=True)
    (ops / "field-loss-2026-09-26.md").write_text(
        '---\nid: "operational:curated-field-loss-review"\ntype: dashboard\n'
        "title: Curated field-loss review\n---\n", encoding="utf-8")
    fresh = ops / "field-loss-2026-09-27.md"
    # what backfill_ids stamps on an id-less twin
    fresh.write_text(_page("Curated field-loss review"), encoding="utf-8")
    BACKFILL.run(vault, apply=True)
    backfilled = _fm(fresh)["id"]
    fresh.unlink()
    G.write(fresh, _page("Curated field-loss review"))
    assert _fm(fresh)["id"] == backfilled
    rel = "operational/field-loss-2026-09-27.md"
    assert backfilled == id_lib.make_id("operational", f"field-loss-2026-09-27-{id_lib._short_hash(rel, 6)}")


def test_a_later_named_sibling_also_forces_disambiguation(tmp_path):
    vault = _vault(tmp_path)
    ops = vault / "wiki" / "operational"
    ops.mkdir(parents=True)
    (ops / "zz-later.md").write_text('---\nid: "operational:shared-title"\ntype: dashboard\n---\n',
                                     encoding="utf-8")
    G.write(ops / "aa-first.md", _page("Shared title"))
    assert _fm(ops / "aa-first.md")["id"].startswith("operational:aa-first-")


@pytest.mark.parametrize("other", ["dashboards:a", "dashboards:zzz", "zzz:fleet-health"])
def test_siblings_with_other_ids_do_not_force_disambiguation(tmp_path, other):
    vault = _vault(tmp_path)
    dash = vault / "wiki" / "dashboards"
    dash.mkdir(parents=True)
    (dash / "other.md").write_text(f'---\nid: "{other}"\ntype: dashboard\n---\n', encoding="utf-8")
    G.write(dash / "fleet-health.md", _page("Fleet health"))
    assert _fm(dash / "fleet-health.md")["id"] == "dashboards:fleet-health"


def test_sibling_scan_ignores_exempt_files_and_the_page_itself(tmp_path):
    vault = _vault(tmp_path)
    dash = vault / "wiki" / "dashboards"
    dash.mkdir(parents=True)
    (dash / "_draft.md").write_text('---\nid: "dashboards:fleet-health"\n---\n', encoding="utf-8")
    G.write(dash / "fleet-health.md", _page("Fleet health"))
    assert _fm(dash / "fleet-health.md")["id"] == "dashboards:fleet-health"


def test_authority_ids_are_never_disambiguated(tmp_path):
    vault = _vault(tmp_path, "types:\n  card: {required: [type], id_authority: acme, id_field: ref}\n")
    dash = vault / "wiki" / "dashboards"
    dash.mkdir(parents=True)
    (dash / "a.md").write_text('---\nid: "acme:rc-7"\ntype: card\n---\n', encoding="utf-8")
    G.write(dash / "b.md", "---\ntype: card\nref: RC-7\n---\n")
    assert _fm(dash / "b.md")["id"] == "acme:rc-7"


# ── exemptions and non-pages ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["INDEX.md", "INDEX-p02.md", "index-p10.md", "HEALTH.md",
                                  "BUNDLE.md", "HOT.md", "log.md", "_about.md", ".hidden.md",
                                  "page.bak.md", "schema.yaml"])
def test_structural_files_are_exempt(tmp_path, name):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _page("Index")
    assert G.with_id(path, text) == text
    assert G.is_exempt(path)


@pytest.mark.parametrize("name", ["INDEX-page.md", "fleet-health.md", "xINDEX-p02.md"])
def test_ordinary_pages_are_not_exempt(name):
    assert not G.is_exempt(Path(name))


@pytest.mark.parametrize("text", ["no frontmatter at all\n", "---\n[unclosed\n---\nbody\n",
                                  "---\ncreated: 2026-13-45\n---\nbody\n"])
def test_unparseable_or_missing_frontmatter_is_left_alone(tmp_path, text):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "x.md"
    path.parent.mkdir(parents=True)
    assert G.with_id(path, text) == text


def test_non_mapping_frontmatter_parses_as_empty_mapping():
    fm, match = G.parse_frontmatter("---\n- one\n---\n")
    assert fm == {} and match is not None


def test_page_id_accepts_only_non_blank_strings():
    assert G.page_id({"id": "  a:b  "}) == "a:b"
    assert G.page_id({"id": "   "}) == ""
    assert G.page_id({"id": 7}) == ""
    assert G.page_id(None) == ""


def test_a_page_outside_any_wiki_dir_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not inside a vault wiki/"):
        G.with_id(tmp_path / "loose" / "x.md", _page("X"))


def test_explicit_wiki_overrides_the_ancestor_lookup(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "wiki" / "nested.md"
    path.parent.mkdir(parents=True)
    out = G.with_id(path, _page("Nested"), wiki=vault / "wiki")
    assert 'id: "wiki:nested"' in out


@pytest.mark.parametrize("subdir", ["dashboards", "zz-late-sorting", "wiki-ish"])
def test_wiki_root_is_the_nearest_wiki_ancestor(tmp_path, subdir):
    path = tmp_path / "wiki" / subdir / "x.md"
    assert G.wiki_root(path) == (tmp_path / "wiki").resolve()


def test_wiki_and_encoding_are_keyword_only(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "x.md"
    path.parent.mkdir(parents=True)
    with pytest.raises(TypeError):
        G.with_id(path, _page("X"), vault / "wiki")
    with pytest.raises(TypeError):
        G.write(path, _page("X"), "utf-8")


def test_an_id_behind_a_long_frontmatter_is_preserved(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "long.md"
    path.parent.mkdir(parents=True)
    filler = "".join(f"field_{i}: {'x' * 60}\n" for i in range(200))       # ~14 KB of frontmatter
    path.write_text(f"---\n{filler}id: \"keep:long\"\n---\nbody\n---\nid: decoy\n---\n",
                    encoding="utf-8")
    G.write(path, _page("Long"))
    assert _fm(path)["id"] == "keep:long"


def test_write_honors_encoding(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "u.md"
    path.parent.mkdir(parents=True)
    G.write(path, _page("Übersicht — ✓"), encoding="utf-8")
    assert "✓" in path.read_text(encoding="utf-8")


# ── negative fixture: the pre-fix writer ────────────────────────────────────────────────────

def _legacy_write(path: Path, text: str) -> None:
    """What every generated-page writer did before okengine#933: rewrite the file whole."""
    path.write_text(text, encoding="utf-8")


def test_pre_fix_rewrite_drops_the_backfilled_id(tmp_path):
    vault = _vault(tmp_path)
    path = vault / "wiki" / "dashboards" / "fleet-health.md"
    path.parent.mkdir(parents=True)
    _legacy_write(path, _page("Fleet health"))
    assert BACKFILL.run(vault, apply=True)["stamped"] == 1
    assert _fm(path)["id"] == "dashboards:fleet-health"
    _legacy_write(path, _page("Fleet health"))              # the next scheduled run
    assert "id" not in _fm(path), "the legacy writer is expected to drop the id (the #933 defect)"
    assert BACKFILL.run(vault, apply=False)["stamped"] == 1  # backfill never converges
    G.write(path, _page("Fleet health"))
    assert _fm(path)["id"] == "dashboards:fleet-health"
    assert BACKFILL.run(vault, apply=False)["stamped"] == 0


def test_backfill_reuses_the_shared_derivation():
    assert BACKFILL.derive_for_page is G.derive_for_page
    assert BACKFILL._skip is G.is_exempt
    assert yaml.safe_load(G.insert_id("---\na: 1\n---\n", "x:y", G.FM_RE.match("---\na: 1\n---\n"))
                          .split("---")[1]) == {"id": "x:y", "a": 1}
