import importlib.util
import io
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout

import yaml


REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_glued_closing_delimiter_is_detected_and_repaired():
    repair = _load("repair_broken_frontmatter", "scripts/cron/repair_broken_frontmatter.py")
    audit = _load("wiki_schema_audit", "scripts/cron/wiki_schema_audit.py")
    broken = "---\ntype: source\ntitle: Report\ntlp: CLEAR---\n# Report\n"

    assert audit.parse_frontmatter(broken) is None
    assert audit.categorize_fm_failure(broken) == "glued-close"

    fixed, label = repair.repair_text(broken)
    assert label == "glued-close"
    assert fixed == "---\ntype: source\ntitle: Report\ntlp: CLEAR\n---\n# Report\n"
    assert yaml.safe_load(audit.parse_frontmatter(fixed))["tlp"] == "CLEAR"


def test_scalar_ending_in_dashes_without_a_type_is_not_rewritten():
    repair = _load("repair_broken_frontmatter_no_type", "scripts/cron/repair_broken_frontmatter.py")
    text = "---\ntitle: prose ending---\n"

    fixed, label = repair.repair_text(text)
    assert fixed is None
    assert label == ""


def test_rss_suffix_echo_is_not_mistaken_for_closing_fence_and_is_repaired():
    repair = _load("repair_broken_frontmatter_rss_echo", "scripts/cron/repair_broken_frontmatter.py")
    audit = _load("wiki_schema_audit_rss_echo", "scripts/cron/wiki_schema_audit.py")
    broken = (
        "---\n"
        "type: source\n"
        "title: Report\n"
        "url: https://example.test/report?source=rss----2983bc435765---4\n"
        "source_feed: Example Feed\n"
        "----2983bc435765---4\n"
        "source_feed: Example Feed\n"
        "---\n"
        "-2983bc435765---4\n"
        "source_feed: Example Feed\n"
        "----2983bc435765---4\n"
        "source_kind: report\n"
        "---\n"
        "# Report\n"
    )

    assert repair._already_valid(broken) is False
    assert audit.yaml_validity(audit.parse_frontmatter(broken)) is not None

    fixed, label = repair.repair_text(broken)
    assert label == "rss-suffix-echo"
    assert fixed is not None
    fm = yaml.safe_load(audit.parse_frontmatter(fixed))
    assert fm["type"] == "source"
    assert fm["source_feed"] == "Example Feed"
    assert fm["source_kind"] == "report"
    assert fm["url"].endswith("?source=rss----2983bc435765---4")
    assert fixed.endswith("---\n# Report\n")


def test_rss_suffix_echo_collapses_conflicting_scalar_to_last_value():
    repair = _load("repair_broken_frontmatter_rss_conflict", "scripts/cron/repair_broken_frontmatter.py")
    broken = (
        "---\n"
        "type: source\n"
        "publisher: First\n"
        "----2983bc435765---4\n"
        "publisher: Second\n"
        "----2983bc435765---4\n"
        "---\n"
        "# Report\n"
    )

    fixed, label = repair.repair_text(broken)
    assert label == "rss-suffix-echo"
    assert fixed is not None
    match = repair._FM_BLOCK_RE.match(fixed)
    assert yaml.safe_load(match.group("frontmatter"))["publisher"] == "Second"
    assert fixed.count("publisher:") == 1


def test_valid_typeless_frontmatter_is_not_a_yaml_repair_candidate():
    repair = _load("repair_broken_frontmatter_typeless", "scripts/cron/repair_broken_frontmatter.py")
    text = "---\ntitle: Structural page\nstatus: active\n---\n# Structural page\n"

    assert repair._already_valid(text) is True
    assert repair.repair_text(text) == (None, "")


def test_common_corruption_classes_are_repaired_and_parseable():
    repair = _load("repair_broken_frontmatter_classes", "scripts/cron/repair_broken_frontmatter.py")
    cases = [
        ("---\ntype: source\ntags: [one\n---\nbody\n", "inline-flow"),
        ("---\ntype: source\ntags: [one]\"]\n---\nbody\n", "trailing-garbage"),
        ("---\ntype: source\nrelated: [[entities/a/apt29]], [[entities/q/qilin]]\n---\nbody\n",
         "wikilink-flow"),
        ("---\ntype: source\ntitle: Report\n|----\n# Report\n", "missing-close"),
        ("---\ntype: source\ntitle: Report\n# Report\nBody text.\n", "body-bleed"),
        ("1|---\n2|type: source\n3|title: Report\n4|---\n5|# Report\n", "catn-prefix"),
    ]

    for broken, expected_label in cases:
        fixed, label = repair.repair_text(broken)
        assert label == expected_label
        assert fixed is not None
        match = repair._FM_BLOCK_RE.match(fixed)
        assert match and repair.parses_with_type(match.group("frontmatter"))

    malformed = "---\ntype: source\ntitle: Report\n----\n# Report\n"
    assert repair.repair_malformed_close(malformed) == (
        "---\ntype: source\ntitle: Report\n---\n# Report\n"
    )


def test_main_repairs_live_files_skips_backups_and_reports_unrepairable(tmp_path):
    repair = _load("repair_broken_frontmatter_main", "scripts/cron/repair_broken_frontmatter.py")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    live = wiki / "live.md"
    live.write_text("---\ntype: source\ntitle: Report---\n# Report\n")
    backup = wiki / "copy.bak.md"
    backup.write_text("---\ntype: source\ntitle: Backup---\n# Backup\n")
    unsafe = wiki / "unsafe.md"
    unsafe.write_text("---\ntype: [\n# Broken\n")
    repair.VAULT = tmp_path
    repair.WIKI = wiki

    output = io.StringIO()
    with redirect_stdout(output):
        assert repair.main() == 0

    assert "repaired (glued-close)" in output.getvalue()
    assert "still broken (left quarantined)" in output.getvalue()
    assert "\ntitle: Report\n---\n" in live.read_text()
    assert "Backup---" in backup.read_text()
    assert unsafe.read_text() == "---\ntype: [\n# Broken\n"


def test_main_missing_wiki_is_a_clean_reported_failure(tmp_path):
    repair = _load("repair_broken_frontmatter_missing", "scripts/cron/repair_broken_frontmatter.py")
    repair.VAULT = tmp_path
    repair.WIKI = tmp_path / "wiki"
    stdout, stderr = io.StringIO(), io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        assert repair.main() == 1

    assert '"wakeAgent": false' in stdout.getvalue()
    assert "does not exist" in stderr.getvalue()


def test_repair_fail_closed_guard_edges():
    repair = _load("repair_broken_frontmatter_guard_edges", "scripts/cron/repair_broken_frontmatter.py")
    assert repair._is_strong_body("ordinary prose sentence")
    assert not repair._is_strong_body("key: value")
    assert repair.repair_body_bleed("body") is None
    assert repair.repair_body_bleed("---\ntype: source\n---\nbody") is None
    assert repair.repair_body_bleed("---\ntype: source\n???\n") is None
    assert repair.repair_body_bleed("---\ntype: source\n") is None
    assert repair.repair_body_bleed("---\ninvalid: [\n# Heading\n") is None
    assert repair.repair_glued_close("body") is None
    assert repair.repair_malformed_close("body") is None
    assert repair.repair_malformed_close("---\ntype: source\n---\nbody") is None
    assert repair.repair_malformed_close("---\ntype: [\n----\nbody") is None
    assert repair.repair_missing_close("body") is None
    assert repair.repair_missing_close("---\ntype: source\n---\nbody") is None
    assert repair.repair_missing_close("---\ntype: source\n") is None
    assert repair.repair_missing_close("---\ninvalid: [\n# body\n") is None
    assert repair.repair_wikilink_flow("body") is None
    assert repair.repair_wikilink_flow("---\ntype: source\n---\n") is None
    assert repair.repair_catn_prefix("ordinary\ntext") is None
    assert repair.repair_catn_prefix("|not-a-fence\ntype: source\n|---") is None
    assert repair.repair_catn_prefix("|---\n|type: source\n") is None
    assert repair.repair_catn_prefix("|---\n|type: [\n|---") is None
    assert repair.repair_catn_prefix("|---\n|type: source\n|---\n|key: orphan") is None


def test_missing_close_blank_and_corrupt_marker_paths():
    repair = _load("repair_broken_frontmatter_missing_edges", "scripts/cron/repair_broken_frontmatter.py")
    fixed = repair.repair_missing_close("---\ntype: source\n\n|----\n# Body\n")
    assert fixed == "---\ntype: source\n---\n\n# Body\n"
    fixed = repair.repair_missing_close("---\ntype: source\n\n# Body\n")
    assert fixed and "---\n\n# Body" in fixed


def test_already_valid_pipe_plain_missing_close_and_bad_yaml_edges():
    repair = _load("repair_broken_frontmatter_valid_edges", "scripts/cron/repair_broken_frontmatter.py")
    assert not repair._already_valid("|---\n|type: source\n|---")
    assert repair._already_valid("plain body")
    assert not repair._already_valid("---\ntype: source\n")
    assert not repair._already_valid("---\ntype: [\n---\n")


def test_main_tolerates_read_races_and_permission_denials(tmp_path, monkeypatch):
    repair = _load("repair_broken_frontmatter_io_edges", "scripts/cron/repair_broken_frontmatter.py")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    raced = wiki / "raced.md"
    raced.write_text("---\ntype: source\ntitle: Raced---\n")
    denied = wiki / "denied.md"
    denied.write_text("---\ntype: source\ntitle: Denied---\n")
    valid = wiki / "valid.md"
    valid.write_text("---\ntype: source\n---\n")
    original_read = repair.Path.read_text
    original_write = repair.Path.write_text
    monkeypatch.setattr(
        repair.Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == raced else original_read(self, *a, **k),
    )
    monkeypatch.setattr(
        repair.Path, "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied"))
        if self == denied else original_write(self, *a, **k),
    )
    repair.VAULT, repair.WIKI = tmp_path, wiki
    output = io.StringIO()
    with redirect_stdout(output):
        assert repair.main() == 0
    assert "PERMISSION DENIED" in output.getvalue()
    assert "Permission-skipped: 1" in output.getvalue()


def test_remaining_parser_candidate_and_rss_fail_closed_edges():
    repair = _load("repair_broken_frontmatter_final_edges", "scripts/cron/repair_broken_frontmatter.py")
    fixed = repair.repair_body_bleed("---\ntype: source\n\n# comment-like\nordinary prose body\n")
    assert fixed and "---\n\n# comment-like" in fixed
    assert repair.repair_body_bleed("---\n\n# body\n") is None

    assert repair.repair_malformed_close("---\ntype: [\n---\nbody") is None
    assert repair.repair_missing_close("---\n- list\n---\nbody") is None
    assert repair.repair_inline_flow("---\ntype: [bad\nother: [still bad\n---\n") is None
    assert repair.repair_wikilink_flow(
        "---\ntype: [bad\nrelated: [[entities/a]]\n---\n") is None

    token = "----2983bc435765---4"
    same = (
        "---\ntype: source\npublisher: Same\n" + token + "\n"
        "publisher: Same\nempty:\n  - value\n" + token + "\n---\nbody\n"
    )
    fixed = repair.repair_rss_suffix_echo(same)
    assert fixed and fixed.count("publisher:") == 1 and "empty:" in fixed

    invalid = "---\ntitle: no type\n" + token + "\n" + token + "\n---\n"
    assert repair.repair_rss_suffix_echo(invalid) is None
    assert repair.repair_text(invalid) == (None, "")
