"""log.md is an audit trail with no ending; rotation gives it one (okengine, #605 follow-on).

The single-file design is deliberate and stays: sixteen lanes and the enforced write path all
append to `wiki/log.md`, and ten reserved-file guards match it by name. Changing that would be a
contract change across the write path, the migrator and every corpus walker.

What it lacked was a bound. Measured on a live vault: 40,575 lines over 39 days, 5.0 MB, ~1,000
lines a day — about 47 MB a year, for a page the reader is expected to open.

The rule under test: entries dated before today move to `wiki/_logs/<date>.md`; today's stay.
Nothing may be lost, duplicated, or reordered, and an undated continuation must travel with the
entry it belongs to.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("rotate_wiki_log",
                                              REPO / "scripts/cron/rotate_wiki_log.py")
R = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = R
SPEC.loader.exec_module(R)


def vault_with(tmp_path: Path, body: str) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "log.md").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("line, expected", [
    ("- 2026-08-18 mcp-write update x.md v2", "2026-08-18"),
    ("* 2026-08-18 lane did a thing", "2026-08-18"),
    ("## [2026-07-10] source-quality-backfill | 20 sources scored", "2026-07-10"),
    ("plain continuation text", None),
    ("- not-a-date mcp-write update", None),
    ("", None),
])
def test_both_line_shapes_declare_their_date(line, expected):
    """The ledger carries two formats and a rotator that knew only one would strand the other."""
    assert R.line_date(line) == expected


def test_entries_before_today_rotate_and_todays_stay():
    lines = ["- 2026-08-16 a\n", "- 2026-08-17 b\n", "- 2026-08-18 c\n"]
    archived, kept = R.partition(lines, "2026-08-18")
    assert archived == {"2026-08-16": ["- 2026-08-16 a\n"], "2026-08-17": ["- 2026-08-17 b\n"]}
    assert kept == ["- 2026-08-18 c\n"]


def test_an_undated_line_travels_with_the_entry_above_it():
    """A wrapped summary separated from its heading is worse than not rotating at all."""
    lines = ["## [2026-08-16] lane | summary\n", "  continued on the next line\n",
             "- 2026-08-18 today\n"]
    archived, kept = R.partition(lines, "2026-08-18")
    assert archived["2026-08-16"] == ["## [2026-08-16] lane | summary\n",
                                      "  continued on the next line\n"]
    assert kept == ["- 2026-08-18 today\n"]


def test_undated_lines_before_any_entry_stay_put():
    """They have no date to inherit, and inventing one would file them under a day the file never
    claimed."""
    lines = ["# Vault log\n", "\n", "- 2026-08-16 a\n"]
    archived, kept = R.partition(lines, "2026-08-18")
    assert kept == ["# Vault log\n", "\n"]
    assert archived == {"2026-08-16": ["- 2026-08-16 a\n"]}


def test_a_future_dated_entry_is_kept_not_archived():
    archived, kept = R.partition(["- 2026-09-01 later\n"], "2026-08-18")
    assert archived == {} and kept == ["- 2026-09-01 later\n"]


def test_no_line_is_lost_or_duplicated(tmp_path):
    """The property that matters most: rotation moves lines, it does not edit them."""
    lines = [f"- 2026-08-{day:02d} entry {n}\n" for day in (16, 17, 18) for n in range(4)]
    archived, kept = R.partition(lines, "2026-08-18")
    moved = [line for rows in archived.values() for line in rows]
    assert sorted(moved + kept) == sorted(lines)
    assert len(moved) + len(kept) == len(lines)


def test_a_dry_run_reports_the_move_and_changes_nothing(tmp_path):
    vault = vault_with(tmp_path, "- 2026-08-16 a\n- 2026-08-18 b\n")
    report = R.rotate(vault, "2026-08-18")
    assert report["rotated"] == 1 and report["kept"] == 1 and report["applied"] is False
    assert (vault / "wiki/log.md").read_text() == "- 2026-08-16 a\n- 2026-08-18 b\n"
    assert not (vault / "wiki/_logs").exists()


def test_applying_writes_the_archive_and_shortens_the_log(tmp_path):
    vault = vault_with(tmp_path, "- 2026-08-16 a\n- 2026-08-17 b\n- 2026-08-18 c\n")
    report = R.rotate(vault, "2026-08-18", apply=True)
    assert report["applied"] is True and report["dates"] == ["2026-08-16", "2026-08-17"]
    assert (vault / "wiki/_logs/2026-08-16.md").read_text() == "- 2026-08-16 a\n"
    assert (vault / "wiki/_logs/2026-08-17.md").read_text() == "- 2026-08-17 b\n"
    assert (vault / "wiki/log.md").read_text() == "- 2026-08-18 c\n"


def test_the_archive_lives_under_an_underscore_directory(tmp_path):
    """`_logs/` is skipped by the projection scanner, the index builders, the corpus indexer and
    the write path, all by the existing `_` convention. An archive must never become a page."""
    vault = vault_with(tmp_path, "- 2026-08-16 a\n")
    R.rotate(vault, "2026-08-18", apply=True)
    assert (vault / "wiki/_logs").is_dir()
    assert R.ARCHIVE_DIR.startswith("_")


def test_rotating_twice_is_a_no_op_the_second_time(tmp_path):
    vault = vault_with(tmp_path, "- 2026-08-16 a\n- 2026-08-18 b\n")
    R.rotate(vault, "2026-08-18", apply=True)
    again = R.rotate(vault, "2026-08-18", apply=True)
    assert again["rotated"] == 0 and again["applied"] is False
    assert (vault / "wiki/_logs/2026-08-16.md").read_text() == "- 2026-08-16 a\n"


def test_a_second_rotation_of_the_same_date_appends_rather_than_discards(tmp_path):
    """If a stray old-dated entry appears after its day was filed, adding it beats replacing the
    archive with just the newcomer."""
    vault = vault_with(tmp_path, "- 2026-08-16 first\n")
    R.rotate(vault, "2026-08-18", apply=True)
    (vault / "wiki/log.md").write_text("- 2026-08-16 late arrival\n", encoding="utf-8")
    R.rotate(vault, "2026-08-18", apply=True)
    assert (vault / "wiki/_logs/2026-08-16.md").read_text() == \
        "- 2026-08-16 first\n- 2026-08-16 late arrival\n"


def test_a_vault_with_no_log_is_not_an_error(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    report = R.rotate(tmp_path, "2026-08-18")
    assert report["rotated"] == 0 and report["reason"] == "no log.md"


def test_no_temp_file_survives_a_successful_rotation(tmp_path):
    """The rewrite goes through a temp file so a crash cannot truncate the ledger; the temp must
    not then be left behind for a corpus walker to find."""
    vault = vault_with(tmp_path, "- 2026-08-16 a\n- 2026-08-18 b\n")
    R.rotate(vault, "2026-08-18", apply=True)
    assert [p.name for p in (vault / "wiki").iterdir() if p.is_file()] == ["log.md"]


def test_cli_previews_with_dry_run_and_changes_nothing(tmp_path, capsys):
    vault = vault_with(tmp_path, "- 2026-08-16 a\n")
    assert R.main(["--root", str(vault), "--today", "2026-08-18", "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rotated"] == 1 and report["applied"] is False
    assert (vault / "wiki/log.md").read_text() == "- 2026-08-16 a\n"


def test_cli_applies_by_default_because_cron_cannot_pass_a_flag(tmp_path, capsys):
    """cron-plus builds a job from a fixed key set with no `args`, so a lane that needed
    `--apply` to do its work would never do it — it would report success forever having moved
    nothing. The default must be the behaviour the scheduled run needs."""
    vault = vault_with(tmp_path, "- 2026-08-16 a\n- 2026-08-18 b\n")
    assert R.main(["--root", str(vault), "--today", "2026-08-18"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert (vault / "wiki/log.md").read_text() == "- 2026-08-18 b\n"


def test_the_cron_entry_does_not_depend_on_an_unsupported_args_key():
    """The regression guard for the mistake this nearly shipped: the first version of this lane
    was registered with `"args": ["--apply"]`. It was the only job in 68 to use that key, and
    cron_pack_split never reads it — the flag would have been dropped silently and the lane would
    have dry-run forever."""
    fleet = json.loads((REPO / "config/engine-crons.json").read_text(encoding="utf-8"))
    jobs = fleet["jobs"] if isinstance(fleet, dict) else fleet
    with_args = [j["name"] for j in jobs if "args" in j]
    assert with_args == [], (
        f"{with_args} declare an `args` key that the scheduler does not read; put the behaviour "
        f"in the script's default or give it a dedicated apply_*.py entry point"
    )
    lane = next(j for j in jobs if j["name"] == "rotate-wiki-log")
    assert lane["script"] == "rotate_wiki_log.py" and lane.get("no_agent") is True


def test_cli_defaults_today_to_the_real_date(tmp_path, capsys, monkeypatch):
    """Without --today the lane must use the actual date, or a cron run would rotate nothing."""
    class Fixed:
        @staticmethod
        def today():
            class D:
                @staticmethod
                def isoformat():
                    return "2026-08-18"
            return D()
    monkeypatch.setattr(R, "date", Fixed)
    vault = vault_with(tmp_path, "- 2026-08-16 a\n- 2026-08-18 b\n")
    R.main(["--root", str(vault)])
    assert json.loads(capsys.readouterr().out)["rotated"] == 1


# --- the archives must be invisible to the corpus walkers (the reason `_logs/` is underscored) ---

def load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / f"scripts/cron/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_backlinks_ignore_a_page_inside_an_underscore_directory():
    """skip_name checked only the FILENAME, so `_logs/2026-08-17.md` — whose name has no
    underscore — seeded backlink edges, while every other walker skipped it. Measured on live
    vaults: 119 such files on one, 68 on another."""
    backlink_lib = load("backlink_lib")
    excluded = frozenset()
    assert backlink_lib.skip_source("_logs/2026-08-17", excluded) is True
    assert backlink_lib.skip_source("predictions/_archive/old-call", excluded) is True
    assert backlink_lib.skip_source("entities/_/x-force", excluded) is False
    assert backlink_lib.skip_source("entities/a/acme", excluded) is False


def test_the_schema_audit_skips_underscore_directories(tmp_path):
    """It already excluded `.bak.` and `.`-prefixed dirs for exactly this reason — an in-tree
    archive polluting a live audit. `_logs/` is that same class."""
    audit = load("wiki_schema_audit")
    source = (REPO / "scripts/cron/wiki_schema_audit.py").read_text(encoding="utf-8")
    assert 'part.startswith((".", "_"))' in source, (
        "the audit walk no longer skips `_`-prefixed archive directories, so rotated log files "
        "will be audited as corpus pages"
    )
    assert audit is not None
