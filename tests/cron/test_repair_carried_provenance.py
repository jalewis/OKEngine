"""scripts/cron/repair_carried_provenance.py — restore provenance the compile dropped/invented.

Ingest provenance is carried, never re-derived. `source_kind` was missing from the carry list,
so the compile agent invented it; when the compile model changed on 2026-07-26 the invention
became a constant and a downstream lane went blind for three weeks.

The raw pages kept the truth, so this repair is a copy rather than a guess — and these tests
pin the three ways a "repair" like this normally goes wrong: inventing a value the raw does
not have, reformatting fields it was not asked to touch, and folding an unreadable raw file
into a clean pass.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "repair_carried_provenance.py"


def _mod():
    spec = importlib.util.spec_from_file_location("repair_carried_provenance", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def _vault(tmp_path: Path, page_fm: str, raw_fm: str | None, body="body text\n"):
    vault = tmp_path / "v"
    (vault / "wiki" / "sources").mkdir(parents=True)
    (vault / "wiki" / "sources" / "a.md").write_text(
        f"---\n{page_fm}\n---\n\n{body}", encoding="utf-8")
    if raw_fm is not None:
        (vault / "raw").mkdir(parents=True, exist_ok=True)
        (vault / "raw" / "a.md").write_text(f"---\n{raw_fm}\n---\n\nraw body\n", encoding="utf-8")
    return vault


def _page(vault: Path) -> str:
    return (vault / "wiki" / "sources" / "a.md").read_text(encoding="utf-8")


def test_it_copies_the_raw_value_over_an_invented_one(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: cyber-news")
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["divergent_pages"] == 1 and state["repaired_pages"] == 1
    assert "source_kind: cyber-news" in _page(vault)
    assert "source_kind: report" not in _page(vault)


def test_it_adds_a_key_the_page_lost_entirely(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md",
                   "type: source\nsource_feed: Some Feed")
    mod.scan(vault, ("source_feed",), True, None)
    assert "source_feed: Some Feed" in _page(vault)


def test_it_never_invents_a_key_the_raw_does_not_declare(tmp_path):
    """The whole defect was a value supplied by something that did not know it."""
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_feed: Some Feed")
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["divergent_pages"] == 0
    assert "source_kind: report" in _page(vault), "left exactly as it was"


def test_it_touches_nothing_but_the_keys_it_repairs(tmp_path):
    """A yaml round-trip would rewrite every neighbouring field; provenance repair must not."""
    mod = _mod()
    fm = ("type: source\nraw: raw/a.md\nsource_kind: report\n"
          "title: 'Odd: quoted title'\ntags: [a, b]\npublished: 2026-08-01\nversion: 7")
    vault = _vault(tmp_path, fm, "type: source\nsource_kind: news")
    mod.scan(vault, ("source_kind",), True, None)
    text = _page(vault)
    assert "title: 'Odd: quoted title'" in text
    assert "tags: [a, b]" in text
    assert "published: 2026-08-01" in text
    assert "version: 7" in text
    assert text.endswith("body text\n")


def test_the_raw_value_text_is_copied_verbatim_not_reserialised(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_feed: wrong",
                   'type: source\nsource_feed: "The Quoted Feed"')
    mod.scan(vault, ("source_feed",), True, None)
    assert 'source_feed: "The Quoted Feed"' in _page(vault)


def test_a_dry_run_writes_nothing(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    before = _page(vault)
    state = mod.scan(vault, ("source_kind",), False, None)
    assert state["divergent_pages"] == 1 and state["repaired_pages"] == 0
    assert _page(vault) == before


def test_an_unreadable_raw_is_reported_never_counted_clean(tmp_path):
    """'I could not check' is not 'I checked and it was fine'."""
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/gone.md\nsource_kind: report", None)
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["raw_missing"] == 1
    assert state["divergent_pages"] == 0 and state["repaired_pages"] == 0
    assert state["unresolvable_examples"], "an unresolvable raw ref must be named"


def test_a_raw_ref_that_escapes_the_vault_is_refused(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: ../../etc/passwd\nsource_kind: report",
                   "type: source\nsource_kind: news")
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["with_raw"] == 0, "an out-of-vault raw ref must not be followed"


def test_a_page_with_no_raw_ref_is_left_alone(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nsource_kind: report", "type: source\nsource_kind: news")
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["with_raw"] == 0 and state["repaired_pages"] == 0


def test_it_is_idempotent(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    mod.scan(vault, ("source_kind",), True, None)
    once = _page(vault)
    second = mod.scan(vault, ("source_kind",), True, None)
    assert second["divergent_pages"] == 0
    assert _page(vault) == once


def test_the_key_set_is_the_carry_set_and_nothing_else(tmp_path, capsys):
    """ONE definition of what provenance is: a key not carried cannot be 'repaired' here."""
    mod = _mod()
    from select_raw_batch import PROVENANCE_KEYS  # noqa: E402
    assert "source_kind" in PROVENANCE_KEYS
    rc = mod.main(["--vault", str(tmp_path), "--key", "title"])
    assert rc == 2
    assert "not carried provenance" in capsys.readouterr().err


def test_a_missing_wiki_is_undetectable_not_a_clean_run(tmp_path, capsys):
    mod = _mod()
    rc = mod.main(["--vault", str(tmp_path / "nope")])
    assert rc == 1
    assert json.loads(capsys.readouterr().out.strip())["status"] == "undetectable"


def test_a_value_the_write_path_would_refuse_is_not_written(tmp_path):
    """A no_agent lane bypasses the write SERVER, never the corpus INVARIANTS.

    Raw pages on the vault this was built against carry `intrusion-report` and `cyber-news`,
    which no schema declares. They are legal there only because `source_kind` is
    `extensible: true`. Where a field's enum is CLOSED, copying the raw value verbatim would
    inject something the enforced write path rejects, so it is reported and left alone.
    """
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\ntlp: CLEAR",
                   "type: source\ntlp: BANANA")
    (vault / "schema.yaml").write_text(
        "enums:\n  tlp: [CLEAR, GREEN, AMBER, RED]\n"
        "field_enums:\n  tlp: {enum: tlp}\n", encoding="utf-8")
    state = mod.scan(vault, ("tlp",), True, None)
    assert state["schema_refused"] == 1, state
    assert state["repaired_pages"] == 0
    assert "tlp: CLEAR" in _page(vault), "the legal existing value must survive"
    assert "BANANA" not in _page(vault)


def test_an_extensible_enum_still_accepts_a_new_value(tmp_path):
    """`source_kind` is extensible by declaration; a novel kind is vocabulary, not corruption."""
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: intrusion-report")
    (vault / "schema.yaml").write_text(
        "enums:\n  source_kind: [news, report]\n"
        "field_enums:\n  source_kind: {enum: source_kind, extensible: true}\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["schema_refused"] == 0
    assert "source_kind: intrusion-report" in _page(vault)


def test_an_unreadable_page_is_skipped_not_crashed(tmp_path, monkeypatch):
    """A vault holds files the lane cannot read; that is not a reason to abort the sweep."""
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    real = Path.read_text

    def boom(self, *a, **k):
        if self.name == "a.md" and "wiki" in str(self):
            raise OSError("permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    assert mod.frontmatter_text(vault / "wiki" / "sources" / "a.md") is None


def test_non_scalar_frontmatter_lines_are_ignored(tmp_path):
    """Block/list values are not provenance; the scanner must step over them."""
    mod = _mod()
    fm = "type: source\ntags:\n  - one\n  - two\nraw: raw/a.md\nsource_kind: report"
    vault = _vault(tmp_path, fm, "type: source\nsource_kind: news")
    vals = mod.scalar_lines("type: source\ntags:\n  - one\nsource_kind: news\n")
    assert vals == {"type": "source", "source_kind": "news"}
    mod.scan(vault, ("source_kind",), True, None)
    assert "  - one" in _page(vault), "the list block must survive untouched"


def test_a_page_with_no_frontmatter_is_not_counted(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md", "type: source\nsource_kind: news")
    (vault / "wiki" / "sources" / "plain.md").write_text("no frontmatter here\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), False, None)
    assert state["pages"] == 1


def test_generated_index_and_underscore_pages_are_skipped(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md", "type: source\nsource_kind: news")
    for name in ("INDEX.md", "_about.md"):
        (vault / "wiki" / "sources" / name).write_text(
            "---\ntype: source\nraw: raw/a.md\n---\n\nx\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), False, None)
    assert state["pages"] == 1, "generated per-directory artifacts are not content"


def test_a_raw_file_without_frontmatter_is_unverified(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report", None)
    (vault / "raw").mkdir(parents=True, exist_ok=True)
    (vault / "raw" / "a.md").write_text("just prose, no frontmatter\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["raw_unreadable"] == 1 and state["repaired_pages"] == 0
    assert any("no frontmatter" in e for e in state["unresolvable_examples"])


def test_an_unresolvable_schema_is_an_error_not_a_clean_sweep(tmp_path, monkeypatch):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md", "type: source\nsource_kind: news")
    monkeypatch.setattr(mod.schema_lib, "merged_schema",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no schema")))
    state = mod.scan(vault, ("source_kind",), False, None)
    assert "cannot resolve the governing schema" in state["error"]


def test_a_bare_list_field_enum_is_a_closed_enum(tmp_path):
    mod = _mod()
    assert mod.enum_rules({"field_enums": {"tlp": ["CLEAR", "RED"]}}) == {"tlp": {"CLEAR", "RED"}}


def test_an_enum_rule_naming_no_declared_list_constrains_nothing(tmp_path):
    """A dangling enum reference must not silently become an empty allow-list."""
    mod = _mod()
    assert mod.enum_rules({"enums": {}, "field_enums": {"tlp": {"enum": "nope"}}}) == {}


def test_a_write_failure_is_reported_and_does_not_stop_the_sweep(tmp_path, monkeypatch, capsys):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    real = Path.write_text
    monkeypatch.setattr(Path, "write_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only"))
                        if self.suffix == ".md" and "wiki" in str(self) else real(self, *a, **k))
    state = mod.scan(vault, ("source_kind",), True, None)
    assert state["divergent_pages"] == 1 and state["repaired_pages"] == 0
    assert "cannot write" in capsys.readouterr().err


def test_the_limit_stops_the_sweep(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    for i in range(4):
        (vault / "wiki" / "sources" / f"b{i}.md").write_text(
            "---\ntype: source\nraw: raw/a.md\nsource_kind: report\n---\n\nx\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), False, 2)
    assert state["divergent_pages"] == 2


def test_example_lists_cap_while_the_counts_stay_complete(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    for i in range(mod.MAX_EXAMPLES + 3):
        (vault / "wiki" / "sources" / f"d{i}.md").write_text(
            "---\ntype: source\nraw: raw/a.md\nsource_kind: report\n---\n\nx\n", encoding="utf-8")
        (vault / "wiki" / "sources" / f"m{i}.md").write_text(
            "---\ntype: source\nraw: raw/gone.md\n---\n\nx\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), False, None)
    assert state["divergent_pages"] == mod.MAX_EXAMPLES + 4
    assert len(state["examples"]) == mod.MAX_EXAMPLES
    assert state["raw_missing"] == mod.MAX_EXAMPLES + 3
    assert len(state["unresolvable_examples"]) == mod.MAX_EXAMPLES


def test_the_refusal_examples_cap_too(tmp_path):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\ntlp: CLEAR", "type: source\ntlp: NOPE")
    (vault / "schema.yaml").write_text(
        "enums:\n  tlp: [CLEAR, RED]\nfield_enums:\n  tlp: {enum: tlp}\n", encoding="utf-8")
    for i in range(mod.MAX_EXAMPLES + 2):
        (vault / "wiki" / "sources" / f"e{i}.md").write_text(
            "---\ntype: source\nraw: raw/a.md\ntlp: CLEAR\n---\n\nx\n", encoding="utf-8")
    state = mod.scan(vault, ("tlp",), False, None)
    assert state["schema_refused"] == mod.MAX_EXAMPLES + 3
    assert len(state["refused_examples"]) == mod.MAX_EXAMPLES


def test_main_reports_counts_examples_and_the_dry_run_notice(tmp_path, capsys):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    (vault / "wiki" / "sources" / "gone.md").write_text(
        "---\ntype: source\nraw: raw/absent.md\n---\n\nx\n", encoding="utf-8")
    rc = mod.main(["--vault", str(vault), "--key", "source_kind"])
    out = capsys.readouterr()
    assert rc == 0
    assert "would repair 1" in out.out
    assert "source_kind: 1" in out.out
    assert "UNVERIFIED, not clean" in out.err
    assert "dry run — pass --apply to write" in out.err
    payload = json.loads(out.out.strip().splitlines()[-1])
    assert payload["divergent_pages"] == 1 and payload["raw_missing"] == 1
    assert "examples" not in payload, "example lists stay out of the machine payload"


def test_main_apply_reports_repaired_rather_than_would_repair(tmp_path, capsys):
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/a.md\nsource_kind: report",
                   "type: source\nsource_kind: news")
    assert mod.main(["--vault", str(vault), "--apply"]) == 0
    assert "repaired 1" in capsys.readouterr().out


def test_the_unverified_cap_holds_for_frontmatterless_raws_too(tmp_path):
    """Both unverifiable shapes share one capped list; the count stays complete regardless."""
    mod = _mod()
    vault = _vault(tmp_path, "type: source\nraw: raw/bare.md", None)
    (vault / "raw").mkdir(parents=True, exist_ok=True)
    (vault / "raw" / "bare.md").write_text("prose only\n", encoding="utf-8")
    for i in range(mod.MAX_EXAMPLES + 2):
        (vault / "wiki" / "sources" / f"f{i}.md").write_text(
            "---\ntype: source\nraw: raw/bare.md\n---\n\nx\n", encoding="utf-8")
    state = mod.scan(vault, ("source_kind",), False, None)
    assert state["raw_unreadable"] == mod.MAX_EXAMPLES + 3
    assert len(state["unresolvable_examples"]) == mod.MAX_EXAMPLES
