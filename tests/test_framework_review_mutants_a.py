"""okengine#843 (group A): pin the observable contract of `framework review --migrate`.

The legacy-flag inventory and the sign-off import were never under mutation before the whole file
became a mutation target. These tests fix what an operator and the review ledger rely on: the
summary counts, the reason/type/age tallies, which pages the walk skips (and that it keeps walking
after one), and, above all, the exact `approved` record written for an imported legacy sign-off --
the one place the CLI creates an approval without a human deciding one.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "framework_review.py"
SCHEMA = "okf: {required: [type]}\nstrict_types: false\n"
_SUMMARY = re.compile(
    r"review migration (?P<mode>APPLY|DRY RUN): (?P<pages>-?\d+) flagged page\(s\); "
    r"(?P<created>-?\d+) open record\(s\) created; (?P<historical>-?\d+) legacy sign-off\(s\); "
    r"(?P<historical_created>-?\d+) historical record\(s\) created")


def _run(vault: Path, *args: str, pythonpath_prefix: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if pythonpath_prefix is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(pythonpath_prefix), env.get("PYTHONPATH", "")) if p)
    return subprocess.run([sys.executable, str(CLI), str(vault), "--migrate", *args],
                          text=True, capture_output=True, check=False, timeout=120, env=env)


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    return tmp_path


def _page(vault: Path, rel: str, frontmatter: str, body: str = "Claim.\n") -> Path:
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return p


def _ok(out: subprocess.CompletedProcess) -> str:
    assert out.returncode == 0, f"--migrate exited {out.returncode}; stderr:\n{out.stderr}"
    assert "Traceback" not in out.stderr, f"--migrate raised:\n{out.stderr}"
    return out.stdout


def _summary(stdout: str) -> dict:
    m = _SUMMARY.search(stdout)
    assert m, f"no migration summary line in:\n{stdout}"
    return {k: (v if k == "mode" else int(v)) for k, v in m.groupdict().items()}


def _tally(stdout: str, label: str) -> dict[str, int]:
    line = next((ln for ln in stdout.splitlines() if ln.startswith(f"{label}: ")), None)
    assert line is not None, f"no {label!r} line in:\n{stdout}"
    rest = line[len(label) + 2:]
    return {k: int(v) for k, v in (item.rsplit("=", 1) for item in rest.split(", "))} if rest else {}


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def _records(vault: Path) -> list[dict]:
    store = vault / "wiki" / "operational" / "reviews"
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(store.glob("*.yaml"))]


def _spread(vault: Path, poison: str) -> None:
    """Six counted flagged pages laid out so a skipped page always precedes some of them.

    Each `dN/` holds a counted page, a poison page one level down and a counted page two levels
    down, so whether the walk is depth- or breadth-first, a poison page is reached before at least
    one counted page; a walk that stops at the first poison page undercounts.
    """
    for n in range(3):
        _page(vault, f"d{n}/page", "type: actor\nneeds_review: true\n")
        _page(vault, f"d{n}/deep/er/page", "type: actor\nneeds_review: true\n")
        p = vault / "wiki" / f"d{n}" / "skipme" / "poison.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(poison, encoding="utf-8")


# ---- the walk ---------------------------------------------------------------------------------

def test_operational_pages_are_skipped_and_the_walk_continues_past_them(tmp_path):
    vault = _vault(tmp_path)
    for n in range(3):
        _page(vault, f"d{n}/page", "type: actor\nneeds_review: true\n")
        _page(vault, f"d{n}/deep/er/page", "type: actor\nneeds_review: true\n")
        _page(vault, f"d{n}/operational/ledger", "type: actor\nneeds_review: true\n")
    pages = _summary(_ok(_run(vault)))["pages"]
    assert pages == 6, f"expected the 6 non-operational flagged pages, counted {pages}"


def test_unparseable_frontmatter_is_skipped_and_the_walk_continues_past_it(tmp_path):
    vault = _vault(tmp_path)
    _spread(vault, "---\nneeds_review: true\ntitle: [unclosed\n---\nBody.\n")
    pages = _summary(_ok(_run(vault)))["pages"]
    assert pages == 6, f"a malformed-YAML page must be skipped, not end the walk; counted {pages}"


def test_non_mapping_frontmatter_is_skipped_and_the_walk_continues_past_it(tmp_path):
    vault = _vault(tmp_path)
    _spread(vault, "---\n- needs_review\n- true\n---\nBody.\n")
    pages = _summary(_ok(_run(vault)))["pages"]
    assert pages == 6, f"a list-valued frontmatter page must be skipped, not end the walk; counted {pages}"


def test_only_a_literal_true_needs_review_flag_is_counted(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/flagged", "type: actor\nneeds_review: true\n")
    _page(vault, "a/one", "type: actor\nneeds_review: 1\n")
    _page(vault, "a/string", "type: actor\nneeds_review: 'true'\n")
    _page(vault, "a/cleared", "type: actor\nneeds_review: false\n")
    _page(vault, "a/plain", "type: actor\n")
    pages = _summary(_ok(_run(vault)))["pages"]
    assert pages == 1, f"only `needs_review: true` (the boolean) is a legacy flag; counted {pages}"


# ---- summary, reasons, types ------------------------------------------------------------------

def test_dry_run_summary_counts_start_from_zero_and_write_nothing(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/flagged", "type: actor\nneeds_review: true\n")
    _page(vault, "a/signed", "type: actor\nreviewed_by: Jane\nreviewed_on: 2026-06-01\n")
    got = _summary(_ok(_run(vault)))
    assert got == {"mode": "DRY RUN", "pages": 1, "created": 0, "historical": 1,
                   "historical_created": 0}, f"wrong dry-run summary: {got}"
    assert not (vault / "wiki" / "operational" / "reviews").exists(), "a dry run wrote records"


def test_reasons_and_types_are_tallied_per_flagged_page(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/conflicted", "type: actor\nneeds_review: true\n"
          "conflicts: [{field: country}]\n")
    _page(vault, "a/ungrounded", "type: actor\nneeds_review: true\n",
          body="Claim.\n\n## Grounding check\n\n- claim X: not found in source\n")
    _page(vault, "a/untyped", "needs_review: true\n")
    out = _ok(_run(vault))
    reasons, types = _tally(out, "reasons"), _tally(out, "types")
    assert reasons == {"conflict": 1, "grounding": 1, "legacy-unspecified": 1}, \
        f"each derived reason code (from frontmatter AND body) counts once: {reasons}"
    assert types == {"actor": 2, "unknown": 1}, f"type tally wrong (untyped -> unknown): {types}"


# ---- ages ---------------------------------------------------------------------------------------

def test_age_buckets_include_their_upper_bound(tmp_path):
    vault = _vault(tmp_path)
    for days in (0, 30, 31, 90, 91, 365, 366, 400):
        _page(vault, f"a/age{days}", f"type: actor\nneeds_review: true\ncreated: {_days_ago(days)}\n")
    ages = _tally(_ok(_run(vault)), "ages")
    assert ages == {"0-30d": 2, "31-90d": 2, "91-365d": 2, ">365d": 2}, \
        f"ages 0,30 | 31,90 | 91,365 | 366,400 must bucket inclusively at 30/90/365: {ages}"


def test_age_stamp_prefers_last_updated_then_updated_then_created(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/lu", f"type: actor\nneeds_review: true\nlast_updated: {_days_ago(10)}\n")
    _page(vault, "a/up", f"type: actor\nneeds_review: true\nupdated: {_days_ago(60)}\n")
    _page(vault, "a/cr", f"type: actor\nneeds_review: true\ncreated: {_days_ago(200)}\n")
    _page(vault, "a/both", f"type: actor\nneeds_review: true\nlast_updated: {_days_ago(5)}\n"
          f"created: {_days_ago(800)}\n")
    ages = _tally(_ok(_run(vault)), "ages")
    assert ages == {"0-30d": 2, "31-90d": 1, "91-365d": 1}, \
        f"each stamp field alone must date the page, last_updated winning over created: {ages}"


def test_a_timestamp_stamp_is_dated_by_its_first_ten_characters(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/ts", f"type: actor\nneeds_review: true\nlast_updated: {_days_ago(3)}T12:00:00Z\n")
    ages = _tally(_ok(_run(vault)), "ages")
    assert ages == {"0-30d": 1}, f"a full timestamp must be dated by its YYYY-MM-DD prefix: {ages}"


def test_an_unparseable_or_missing_stamp_is_aged_unknown(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/garbled", "type: actor\nneeds_review: true\ncreated: last spring\n")
    _page(vault, "a/undated", "type: actor\nneeds_review: true\n")
    ages = _tally(_ok(_run(vault)), "ages")
    assert ages == {"unknown": 2}, f"unparseable and missing stamps are each one 'unknown': {ages}"


# ---- the imported legacy sign-off (writes an `approved` record) ---------------------------------

def test_imported_signoff_writes_one_exact_approved_record(tmp_path):
    vault = _vault(tmp_path)
    page = _page(vault, "entities/a/signed",
                 "type: actor\ntitle: Signed\nversion: 3\nreviewed_by: Jane\n"
                 "reviewed_on: 2026-06-01T09:30:00Z\nreviewed_at: '2026-06-01T10:00:00Z'\n"
                 "sources: [https://example.org/a, '  ', https://example.org/b]\n")
    digest = hashlib.sha256(page.read_bytes()).hexdigest()
    got = _summary(_ok(_run(vault, "--apply")))
    assert (got["historical"], got["historical_created"]) == (1, 1), f"wrong summary: {got}"
    review_id = "review:entities/a/signed:legacy:2026-06-01"
    path = vault / "wiki" / "operational" / "reviews" / (
        hashlib.sha256(review_id.encode("utf-8")).hexdigest() + ".yaml")
    assert path.is_file(), f"record not stored under the digest of {review_id!r}: {_records(vault)}"
    stamp = "2026-06-01T10:00:00Z"
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "version": 1, "review_id": review_id, "subject": "entities/a/signed",
        "subject_version": 3, "subject_hash": digest, "state": "approved",
        "reasons": [{"code": "legacy-signoff-import",
                     "detail": "imported sign-off metadata; evidence examination is not asserted"}],
        "evidence": ["https://example.org/a", "https://example.org/b"],
        "requested_by": "migration", "requested_at": stamp, "assigned_to": "Jane",
        "decision_by": "Jane", "decision_at": stamp,
        "decision_note": "Imported from legacy reviewed_* metadata; scope unknown.",
        "decision_service": "migration", "machine_checks": [],
        "history": [{"decision": "approve", "state": "approved", "decision_by": "Jane",
                     "decision_at": stamp,
                     "decision_note": "Imported legacy sign-off; evidence scope unknown.",
                     "service": "migration"}],
    }, "the imported sign-off record differs from the legacy-import contract"


def test_imported_signoff_wraps_a_scalar_source_as_one_evidence_item(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "entities/a/signed", "type: actor\nreviewed_by: Jane\nreviewed_on: 2026-06-01\n"
          "source: https://example.org/report\n")
    _ok(_run(vault, "--apply"))
    records = _records(vault)
    assert [r["evidence"] for r in records] == [["https://example.org/report"]], \
        f"a scalar `source` is one evidence item, not a list of characters: {records}"


def test_imported_signoff_without_reviewed_at_is_stamped_reviewed_on(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "entities/a/signed", "type: actor\nreviewed_by: Jane\nreviewed_on: 2026-06-01\n")
    _ok(_run(vault, "--apply"))
    records = _records(vault)
    assert [(r["requested_at"], r["decision_at"], r["state"]) for r in records] == [
        ("2026-06-01", "2026-06-01", "approved")], f"stamp must fall back to reviewed_on: {records}"


def test_a_partial_signoff_is_not_imported(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/by_only", "type: actor\nreviewed_by: Jane\n")
    _page(vault, "a/on_only", "type: actor\nreviewed_on: 2026-06-01\n")
    got = _summary(_ok(_run(vault, "--apply")))
    assert (got["historical"], got["historical_created"]) == (0, 0), \
        f"a sign-off needs both reviewed_by and reviewed_on: {got}"
    assert not (vault / "wiki" / "operational" / "reviews").exists(), "a partial sign-off was imported"


# ---- loading the write path -------------------------------------------------------------------

def test_an_unloadable_write_path_is_reported_with_exit_1(tmp_path):
    vault = _vault(tmp_path)
    decoy = tmp_path / "decoy"
    (decoy / "starlette").mkdir(parents=True)
    (decoy / "starlette" / "__init__.py").write_text(
        "raise ImportError('decoy dependency is unavailable')\n", encoding="utf-8")
    out = _run(vault, pythonpath_prefix=decoy)
    assert out.returncode == 1, f"expected exit 1, got {out.returncode}: {out.stderr}"
    assert "ERROR: cannot load the write path: decoy dependency is unavailable" in out.stderr, \
        f"the load failure must be reported, not raised:\n{out.stderr}"
    assert "Traceback" not in out.stderr, out.stderr


def test_migrate_loads_the_engine_write_server_ahead_of_a_same_named_module(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "a/flagged", "type: actor\nneeds_review: true\n")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "write_server.py").write_text(
        "raise ImportError('a shadowing write_server was imported')\n", encoding="utf-8")
    got = _summary(_ok(_run(vault, pythonpath_prefix=decoy)))
    assert got["pages"] == 1, f"the engine's own write path must be the one loaded: {got}"
