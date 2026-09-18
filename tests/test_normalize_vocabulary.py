"""Normalising an ingest-minted vocabulary in `raw/`, not in the carry path.

Provenance is carried from the raw capture to the source page verbatim; mapping a value while
copying would put a value on the page that its raw record never said, which is the defect
`repair_carried_provenance` exists to repair. So the raw record is corrected instead — under a
caller-supplied map, and only ever onto a value the vault's schema declares.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MOD = REPO / "scripts" / "normalize_vocabulary.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="normalize_vocabulary absent")

SCHEMA = """\
version: 1
types:
  source: {required: [type]}
enums:
  source_kind: [news, report, incident-report]
field_enums:
  source_kind: {enum: source_kind}
"""


def _mod():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("normalize_vocabulary", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["normalize_vocabulary"] = mod
    spec.loader.exec_module(mod)
    return mod


def _vault(tmp_path: Path, schema: str = SCHEMA) -> Path:
    (tmp_path / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    return tmp_path


def _capture(vault: Path, name: str, body: str) -> Path:
    path = vault / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: source\n{body}---\n\nBody text.\n", encoding="utf-8")
    return path


# --- parse_map -----------------------------------------------------------------------------------

def test_pairs_parse_into_a_mapping():
    assert _mod().parse_map(["a=b", " c = d "]) == {"a": "b", "c": "d"}


@pytest.mark.parametrize("pair", ["nosep", "=b", "a=", " = "])
def test_a_malformed_pair_is_rejected_not_dropped(pair):
    """A silently-dropped pair means the run reports success having normalised less than asked."""
    with pytest.raises(ValueError):
        _mod().parse_map([pair])


# --- the schema guard ----------------------------------------------------------------------------

def test_a_target_the_schema_does_not_declare_is_refused_before_anything_is_written(tmp_path):
    """Mapping one undeclared value onto another leaves the corpus exactly as unconformant."""
    mod = _mod()
    vault = _vault(tmp_path)
    original = _capture(vault, "a.md", "source_kind: cyber-news\n").read_text()

    state = mod.scan(vault, "source_kind", {"cyber-news": "breaking-news"}, True, None)

    assert "not a declared source_kind value" in state["error"]
    assert (vault / "raw" / "a.md").read_text() == original, "refusal must precede every write"
    assert "ERROR" in mod.report(state)


def test_an_unconstrained_field_accepts_any_target(tmp_path):
    """Where the schema declares no vocabulary there is nothing for the guard to enforce."""
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "watch_lane: old-name\n")

    state = mod.scan(vault, "watch_lane", {"old-name": "whatever"}, False, None)

    assert not state.get("error") and state["changed"] == 1


def test_an_unresolvable_schema_is_an_error_not_a_clean_run(tmp_path, monkeypatch):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: cyber-news\n")
    monkeypatch.setattr(mod.schema_lib, "merged_schema",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no schema")))

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, False, None)

    assert "cannot resolve the governing schema" in state["error"]


def test_a_vault_with_no_raw_tree_reports_it(tmp_path):
    mod = _mod()
    (tmp_path / "wiki").mkdir(parents=True)
    assert "no raw/" in mod.scan(tmp_path, "source_kind", {"a": "news"}, False, None)["error"]


# --- the rewrite ---------------------------------------------------------------------------------

def test_only_the_mapped_field_changes(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    path = _capture(vault, "a.md", "source_feed: Some Feed\nsource_kind: cyber-news\nurl: x\n")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)

    text = path.read_text()
    assert state["changed"] == 1 and state["by_value"] == {"cyber-news": 1}
    assert 'source_kind: "news"' in text
    assert "source_feed: Some Feed" in text and "url: x" in text


def test_a_dry_run_writes_nothing_and_says_so(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    path = _capture(vault, "a.md", "source_kind: cyber-news\n")
    original = path.read_text()

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, False, None)

    assert state["changed"] == 1 and path.read_text() == original
    assert "dry run" in mod.report(state)


def test_a_quoted_value_is_matched_the_same_as_a_bare_one(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: 'cyber-news'\n")

    assert mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)["changed"] == 1


def test_a_declared_value_is_left_alone(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: news\n")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)
    assert state["with_field"] == 1 and state["changed"] == 0 and state["unmapped"] == {}


def test_an_undeclared_value_with_no_mapping_is_reported_never_buried(tmp_path):
    """The value the caller did not think to map is the one this run must not hide."""
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: cyber-news\n")
    _capture(vault, "b.md", "source_kind: vendor-analysis\n")
    _capture(vault, "c.md", "source_kind: vendor-analysis\n")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, False, None)

    assert state["unmapped"] == {"vendor-analysis": 2}
    assert "UNMAPPED" in mod.report(state) and "vendor-analysis" in mod.report(state)


def test_an_unmapped_value_on_an_unconstrained_field_is_not_a_finding(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "watch_lane: other\n")

    assert mod.scan(vault, "watch_lane", {"old": "new"}, False, None)["unmapped"] == {}


def test_a_capture_without_the_field_is_counted_but_not_touched(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "url: x\n")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)
    assert state["captures"] == 1 and state["with_field"] == 0


def test_a_file_without_frontmatter_is_skipped(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    (vault / "raw" / "plain.md").write_text("no frontmatter here\n", encoding="utf-8")

    assert mod.scan(vault, "source_kind", {"a": "news"}, True, None)["captures"] == 0


def test_limit_bounds_the_number_of_changes(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    for i in range(3):
        _capture(vault, f"{i}.md", "source_kind: cyber-news\n")

    assert mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, 2)["changed"] == 2


def test_an_unreadable_capture_does_not_stop_the_sweep(tmp_path, monkeypatch):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: cyber-news\n")
    _capture(vault, "b.md", "source_kind: cyber-news\n")
    real = Path.read_text

    def flaky(self, *a, **k):
        if self.name == "a.md":
            raise OSError("gone")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert mod.scan(vault, "source_kind", {"cyber-news": "news"}, False, None)["changed"] == 1


def test_a_failed_write_is_reported_and_does_not_inflate_the_count(tmp_path, monkeypatch):
    """A write that raised must not leave the run claiming it normalised the capture."""
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: cyber-news\n")
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)

    assert state["changed"] == 0 and state["by_value"]["cyber-news"] == 0
    assert "UNWRITABLE" in mod.report(state)


def test_a_capture_whose_edit_would_break_it_is_named_not_silently_skipped(tmp_path):
    """The field is there and mapped, but the block will not survive the edit — say which file."""
    mod = _mod()
    vault = _vault(tmp_path)
    path = vault / "raw" / "broken.md"
    path.write_text("---\nsource_kind: cyber-news\nbad: 'unclosed\n---\n\nB.\n", encoding="utf-8")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)

    assert state["changed"] == 0 and state["unwritable"] == ["raw/broken.md"]
    assert "UNWRITABLE" in mod.report(state)


def test_the_unsafe_edit_list_is_capped_too(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    for i in range(mod.MAX_EXAMPLES + 3):
        (vault / "raw" / f"{i:02d}.md").write_text(
            "---\nsource_kind: cyber-news\nbad: 'unclosed\n---\n\nB.\n", encoding="utf-8")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)

    assert state["changed"] == 0
    assert len(state["unwritable"]) == mod.MAX_EXAMPLES


def test_examples_are_capped_while_the_count_stays_complete(tmp_path):
    """A capped example list must never cap the number it is illustrating."""
    mod = _mod()
    vault = _vault(tmp_path)
    for i in range(mod.MAX_EXAMPLES + 3):
        _capture(vault, f"{i:02d}.md", "source_kind: cyber-news\n")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, False, None)

    assert state["changed"] == mod.MAX_EXAMPLES + 3
    assert len(state["examples"]) == mod.MAX_EXAMPLES


def test_the_unwritable_list_is_capped_but_every_failure_is_still_excluded(tmp_path, monkeypatch):
    mod = _mod()
    vault = _vault(tmp_path)
    for i in range(mod.MAX_EXAMPLES + 3):
        _capture(vault, f"{i:02d}.md", "source_kind: cyber-news\n")
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None)

    assert state["changed"] == 0, "not one of them was written"
    assert len(state["unwritable"]) == mod.MAX_EXAMPLES


# --- rewrite() edges -----------------------------------------------------------------------------

def test_rewrite_replaces_a_folded_scalar_whole(tmp_path):
    """`^field:.*$` matches one PHYSICAL line; a folded value's tail would be stranded inside
    the frontmatter and the capture would stop parsing at all."""
    mod = _mod()
    text = "---\nsource_kind: 'cyber\n  -news'\nurl: x\n---\n\nBody.\n"
    out = mod.rewrite(text, "source_kind", "news")
    assert out is not None and 'source_kind: "news"' in out and "url: x" in out
    assert "-news'" not in out


def test_rewrite_refuses_text_without_frontmatter():
    assert _mod().rewrite("no frontmatter\n", "source_kind", "news") is None


def test_rewrite_refuses_when_the_field_is_absent():
    assert _mod().rewrite("---\nurl: x\n---\n\nB.\n", "other", "news") is None


def test_rewrite_refuses_an_edit_that_would_not_reparse(tmp_path, monkeypatch):
    """The result is re-parsed before it is returned; a surgical edit that breaks the block is
    abandoned rather than written."""
    mod = _mod()
    monkeypatch.setattr(mod.yaml, "safe_load",
                        lambda *a, **k: (_ for _ in ()).throw(mod.yaml.YAMLError("bad")))
    assert mod.rewrite("---\nsource_kind: x\n---\n\nB.\n", "source_kind", "news") is None


def test_rewrite_refuses_when_the_reparse_does_not_show_the_new_value(monkeypatch):
    mod = _mod()
    monkeypatch.setattr(mod.yaml, "safe_load", lambda *a, **k: "not a mapping")
    assert mod.rewrite("---\nsource_kind: x\n---\n\nB.\n", "source_kind", "news") is None


# --- CLI -----------------------------------------------------------------------------------------

def test_no_map_is_a_usage_error_not_a_silent_success(capsys):
    assert _mod().main(["--vault", "/nonexistent"]) == 2
    assert "no --map or --map-file given" in capsys.readouterr().err


def test_a_malformed_map_exits_before_scanning(capsys):
    assert _mod().main(["--vault", "/nonexistent", "--map", "nosep"]) == 2
    assert "OLD=NEW" in capsys.readouterr().err


def test_the_cli_writes_only_with_apply(tmp_path, capsys):
    mod = _mod()
    vault = _vault(tmp_path)
    path = _capture(vault, "a.md", "source_kind: cyber-news\n")

    assert mod.main(["--vault", str(vault), "--map", "cyber-news=news"]) == 0
    assert "cyber-news" in path.read_text(), "dry run must not write"

    assert mod.main(["--vault", str(vault), "--map", "cyber-news=news", "--apply"]) == 0
    assert 'source_kind: "news"' in path.read_text()
    assert "rewrote 1" in capsys.readouterr().out


def test_the_cli_exits_nonzero_on_a_refusal(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: cyber-news\n")
    assert mod.main(["--vault", str(vault), "--map", "cyber-news=invented"]) == 1


def test_the_cli_accepts_a_limit(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    for i in range(2):
        _capture(vault, f"{i}.md", "source_kind: cyber-news\n")
    assert mod.main(["--vault", str(vault), "--map", "cyber-news=news", "--limit", "1"]) == 0


# --- okengine#595: the wiki tree, where the compile agent authored the value directly ------------

def _page(vault: Path, rel: str, body: str) -> Path:
    path = vault / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: source\n{body}---\n\nBody text.\n", encoding="utf-8")
    return path


def test_the_wiki_tree_is_normalised_the_same_way(tmp_path):
    """A page the compile agent authored has no raw record to be corrected from, so the page
    itself is the thing to correct."""
    mod = _mod()
    vault = _vault(tmp_path)
    path = _page(vault, "sources/2026/a.md", "source_kind: cyber-news\ntitle: T\n")

    state = mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None, "wiki")

    assert state["tree"] == "wiki" and state["changed"] == 1
    text = path.read_text()
    assert 'source_kind: "news"' in text and "title: T" in text


def test_the_two_trees_are_scanned_independently(tmp_path):
    """Correcting pages must not touch the raw records, and the reverse."""
    mod = _mod()
    vault = _vault(tmp_path)
    raw = _capture(vault, "a.md", "source_kind: cyber-news\n")
    page = _page(vault, "sources/a.md", "source_kind: cyber-news\n")

    mod.scan(vault, "source_kind", {"cyber-news": "news"}, True, None, "wiki")

    assert "cyber-news" in raw.read_text(), "the raw record is a different tree"
    assert 'source_kind: "news"' in page.read_text()


def test_a_vault_with_no_wiki_tree_reports_it(tmp_path):
    mod = _mod()
    (tmp_path / "raw").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    assert "no wiki/" in mod.scan(tmp_path, "source_kind", {"a": "news"}, False, None,
                                  "wiki")["error"]


def test_the_cli_takes_a_tree(tmp_path, capsys):
    mod = _mod()
    vault = _vault(tmp_path)
    page = _page(vault, "sources/a.md", "source_kind: cyber-news\n")

    assert mod.main(["--vault", str(vault), "--tree", "wiki",
                     "--map", "cyber-news=news", "--apply"]) == 0

    assert 'source_kind: "news"' in page.read_text()
    assert "normalize-vocabulary [wiki/]" in capsys.readouterr().out


# --- --map-file: the form that scales, and the only one a shell cannot mangle ---------------------

def test_a_map_file_is_read_and_applied(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path)
    path = _capture(vault, "a.md", "source_kind: cyber-news\n")
    mapfile = tmp_path / "map.json"
    mapfile.write_text(json.dumps({"cyber-news": "news"}), encoding="utf-8")

    assert mod.main(["--vault", str(vault), "--map-file", str(mapfile), "--apply"]) == 0
    assert 'source_kind: "news"' in path.read_text()


def test_a_map_file_carries_values_a_command_line_cannot(tmp_path):
    """`report (38-minute read)` and `blog-post ---` are real live values; a shell splits both."""
    mod = _mod()
    vault = _vault(tmp_path)
    _capture(vault, "a.md", "source_kind: report (38-minute read)\n")
    _capture(vault, "b.md", "source_kind: blog-post ---\n")
    mapfile = tmp_path / "map.json"
    mapfile.write_text(json.dumps({"report (38-minute read)": "report",
                                   "blog-post ---": "report"}), encoding="utf-8")

    assert mod.main(["--vault", str(vault), "--map-file", str(mapfile), "--apply"]) == 0
    assert 'source_kind: "report"' in (vault / "raw" / "a.md").read_text()
    assert 'source_kind: "report"' in (vault / "raw" / "b.md").read_text()


def test_inline_maps_override_the_file(tmp_path):
    mod = _mod()
    mapfile = tmp_path / "map.json"
    mapfile.write_text(json.dumps({"a": "news", "b": "report"}), encoding="utf-8")
    assert mod.load_map_file(mapfile) == {"a": "news", "b": "report"}


@pytest.mark.parametrize("payload", ["[]", "{}", '{"a": 3}', '{"": "news"}', '{"a": " "}',
                                     "not json"])
def test_a_malformed_map_file_is_refused_not_partially_applied(tmp_path, payload):
    mod = _mod()
    mapfile = tmp_path / "map.json"
    mapfile.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        mod.load_map_file(mapfile)


def test_a_missing_map_file_is_a_usage_error(tmp_path, capsys):
    mod = _mod()
    assert mod.main(["--vault", str(tmp_path), "--map-file", str(tmp_path / "nope.json")]) == 2
    assert "cannot read --map-file" in capsys.readouterr().err


def test_neither_map_nor_map_file_is_a_usage_error(capsys):
    assert _mod().main(["--vault", "/nonexistent"]) == 2
    assert "no --map or --map-file given" in capsys.readouterr().err


def test_declared_alias_migration_is_case_insensitive_dry_run_and_reports_ambiguity(tmp_path):
    schema = SCHEMA + "value_aliases:\n  source_kind: {article: news}\n"
    mod = _mod()
    vault = _vault(tmp_path, schema)
    alias = _capture(vault, "alias.md", "source_kind: Article\n")
    _capture(vault, "ambiguous.md", "source_kind: possible\n")
    before = alias.read_text(encoding="utf-8")

    state = mod.scan(vault, "source_kind", {}, False, None, "raw", True)

    assert state["schema_aliases"] is True and state["changed"] == 1
    assert state["by_value"] == {"Article": 1}
    assert state["unmapped"] == {"possible": 1}
    assert alias.read_text(encoding="utf-8") == before
    rendered = mod.report(state)
    assert "UNPARSEABLE / AMBIGUOUS" in rendered and "no value was guessed" in rendered


def test_schema_alias_mode_refuses_a_malformed_alias_block(tmp_path, monkeypatch):
    mod = _mod()
    vault = _vault(tmp_path)
    malformed = {
        "enums": {"source_kind": ["news", "report", "incident-report"]},
        "field_enums": {"source_kind": {"enum": "source_kind"}},
        "value_aliases": {"source_kind": "broken"},
    }
    monkeypatch.setattr(mod.schema_lib, "merged_schema", lambda *_: malformed)
    state = mod.scan(vault, "source_kind", {}, False, None, "raw", True)
    assert state["error"] == "value_aliases.source_kind is not a mapping"
