"""okengine#842: close ledger records no surface can reach -- and never claim anyone reviewed them.

Measured on a live vault: 3,891 open records, of which 3,440 pointed at a page that no longer
requires review, 80 at a page that no longer exists, and 224 were duplicate opens on a page that
already had one. The worklist lists only pages that currently require review, so none of those
could ever be closed. Reconciliation dismisses them -- `dismiss` judges the REQUEST, the honest
verb when nobody examined the content -- and appends history rather than replacing it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "framework_review.py"


def _run(pack: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), str(pack), "--reconcile", *args],
                          capture_output=True, text=True, timeout=120)


def _page(pack: Path, rel: str, *, needs_review: bool):
    p = pack / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    flag = "needs_review: true\n" if needs_review else ""
    p.write_text(f"---\ntype: actor\nname: N\n{flag}---\n\nbody\n", encoding="utf-8")
    return p


def _record(pack: Path, name: str, subject: str, *, when: str = "2026-09-01T00:00:00Z"):
    store = pack / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    f = store / f"{name}.yaml"
    f.write_text(yaml.safe_dump({
        "version": 1, "review_id": f"review:{subject}:1:d:{name}", "subject": subject,
        "subject_version": 1, "state": "open", "requested_by": "job", "requested_at": when,
        "reasons": [{"code": "manual", "detail": "d"}], "history": [],
    }, sort_keys=False), encoding="utf-8")
    return f


def _load(f: Path) -> dict:
    return yaml.safe_load(f.read_text(encoding="utf-8"))


def test_dry_run_writes_nothing(tmp_path):
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    rec = _record(tmp_path, "r1", "entities/a/cleared")
    before = rec.read_text()
    out = _run(tmp_path)
    assert out.returncode == 0, out.stderr
    assert "DRY RUN" in out.stdout and "nothing written" in out.stdout
    assert rec.read_text() == before, "a dry run modified the ledger"


def test_a_cleared_page_is_dismissed_never_approved(tmp_path):
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    rec = _record(tmp_path, "r1", "entities/a/cleared")
    assert _run(tmp_path, "--apply").returncode == 0
    d = _load(rec)
    assert d["state"] == "dismissed"
    assert d["state"] != "approved", "reconciliation must never claim a review happened"
    assert d["history"][-1]["reconcile_reason"] == "cleared-elsewhere"
    assert "Not a review of any content" in d["decision_note"]


def test_an_absent_page_is_dismissed_as_subject_absent(tmp_path):
    rec = _record(tmp_path, "r1", "entities/a/gone")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["history"][-1]["reconcile_reason"] == "subject-absent"


def test_duplicate_opens_keep_the_newest_and_supersede_the_rest(tmp_path):
    _page(tmp_path, "entities/a/live", needs_review=True)
    old = _record(tmp_path, "old", "entities/a/live", when="2026-09-01T00:00:00Z")
    new = _record(tmp_path, "new", "entities/a/live", when="2026-09-05T00:00:00Z")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(new)["state"] == "open", "the newest request must stay open"
    d = _load(old)
    assert d["state"] == "dismissed" and d["history"][-1]["reconcile_reason"] == "superseded"
    assert d["history"][-1]["superseded_by"] == _load(new)["review_id"]


def test_a_page_that_still_requires_review_stays_open(tmp_path):
    """Negative guard: reconciliation must not close real work."""
    _page(tmp_path, "entities/a/live", needs_review=True)
    rec = _record(tmp_path, "r1", "entities/a/live")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["state"] == "open"


def test_a_resharded_live_page_is_not_mistaken_for_absent(tmp_path):
    """Negative guard: a record whose page merely moved shards is live work, not an orphan.
    Closing it would silently discard 741 recoverable requests on the vault that motivated this."""
    _page(tmp_path, "entities/q/a/qakbot", needs_review=True)
    rec = _record(tmp_path, "r1", "entities/q/qakbot")        # pre-reshard path
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["state"] == "open", "a resharded live page was closed as absent"


def test_history_is_appended_not_replaced(tmp_path):
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    rec = _record(tmp_path, "r1", "entities/a/cleared")
    d = _load(rec); d["history"] = [{"action": "assign", "assigned_to": "someone"}]
    rec.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    assert _run(tmp_path, "--apply").returncode == 0
    h = _load(rec)["history"]
    assert h[0] == {"action": "assign", "assigned_to": "someone"}, "prior history was lost"
    assert h[-1]["decision"] == "dismiss"


def test_reconcile_is_idempotent(tmp_path):
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    _record(tmp_path, "r1", "entities/a/cleared")
    assert _run(tmp_path, "--apply").returncode == 0
    second = _run(tmp_path, "--apply")
    assert second.returncode == 0
    assert "0 open record(s)" in second.stdout, "a second pass found something to re-close"


def test_an_unreadable_page_keeps_its_request_open(tmp_path):
    """Never close on doubt: if the page cannot be read, its request stays."""
    p = tmp_path / "wiki" / "entities" / "a" / "broken.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: [unclosed\n---\nbody\n", encoding="utf-8")
    rec = _record(tmp_path, "r1", "entities/a/broken")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["state"] == "open"


def test_no_ledger_is_a_clean_no_op(tmp_path):
    (tmp_path / "wiki").mkdir()
    out = _run(tmp_path)
    assert out.returncode == 0 and "nothing to do" in out.stdout


# --- Behaviours a diff-scoped mutation campaign found unpinned in the reconciler (okengine#842). The
# gate passed at 83.25% in diff mode, which does not demand survivor dispositions -- these were real
# gaps anyway, several of them in exactly the places a wrong closure would be irreversible.

def test_a_record_in_any_terminal_or_active_state_is_never_touched(tmp_path):
    """Only `open` is reconciled. The state comparison must be equality: "rejected" and
    "request-changes" both sort AFTER "open", so an ordering comparison would sweep decided records
    -- including human rejections -- into a bulk dismissal."""
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    for state in ("rejected", "request-changes", "in-review", "approved", "dismissed"):
        rec = _record(tmp_path, state, "entities/a/cleared")
        d = _load(rec); d["state"] = state
        rec.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    assert _run(tmp_path, "--apply").returncode == 0
    store = tmp_path / "wiki" / "operational" / "reviews"
    for state in ("rejected", "request-changes", "in-review", "approved", "dismissed"):
        assert _load(store / f"{state}.yaml")["state"] == state, f"a `{state}` record was reconciled"


def test_a_two_part_subject_resolves_by_key(tmp_path):
    """The reshard-proof key must handle a flat namespace's two-segment subject, not only sharded
    three-plus-segment ones."""
    _page(tmp_path, "briefings/weekly-2026-09-01", needs_review=True)
    rec = _record(tmp_path, "r1", "briefings/weekly-2026-09-01")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["state"] == "open"


def test_the_key_uses_the_last_segment_not_the_second(tmp_path):
    """`entities/q/qakbot` must key on `qakbot`. Keying on the shard letter would match an unrelated
    page in the same shard and keep a dead request open against the wrong page."""
    _page(tmp_path, "entities/q/a/quasar", needs_review=True)          # same shard, other page
    rec = _record(tmp_path, "r1", "entities/q/qakbot")                  # no qakbot page anywhere
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["history"][-1]["reconcile_reason"] == "subject-absent"


def test_a_page_live_only_through_a_failed_grounding_section_stays_open(tmp_path):
    """No flag, but the body records a failed grounding check -- that page still requires review."""
    p = tmp_path / "wiki" / "entities" / "a" / "grounded.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: actor\nname: N\n---\n\n## Grounding check\n\nclaim X: unsupported\n",
                 encoding="utf-8")
    rec = _record(tmp_path, "r1", "entities/a/grounded")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["state"] == "open"


def test_a_corrupt_record_is_skipped_and_the_rest_still_reconcile(tmp_path):
    """One unparseable ledger file must neither crash the pass nor end it early -- a `break` there
    would leave every later record unreconciled while reporting success."""
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    store = tmp_path / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / "aaa-corrupt.yaml").write_text("state: [unclosed\n", encoding="utf-8")
    good = _record(tmp_path, "zzz-good", "entities/a/cleared")
    out = _run(tmp_path, "--apply")
    assert out.returncode == 0, out.stderr
    assert _load(good)["state"] == "dismissed", "a record after the corrupt one was never reached"


def test_the_dry_run_report_counts_each_disposition(tmp_path):
    """The operator decides on --apply from this report, so its numbers are part of the contract."""
    _page(tmp_path, "entities/a/live", needs_review=True)
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    _record(tmp_path, "keep", "entities/a/live", when="2026-09-05T00:00:00Z")
    _record(tmp_path, "dup", "entities/a/live", when="2026-09-01T00:00:00Z")
    _record(tmp_path, "clr", "entities/a/cleared")
    _record(tmp_path, "gone", "entities/a/absent")
    out = _run(tmp_path).stdout
    assert "4 open record(s); 1 stay open" in out, out
    assert "3 to close" in out, out
    for reason, n in (("cleared-elsewhere", 1), ("superseded", 1), ("subject-absent", 1)):
        assert any(line.split() == [reason, str(n)] for line in out.splitlines()), (reason, out)


def test_supersession_is_named_only_where_it_happened(tmp_path):
    """A superseded record's note and example name the request that replaced it; a merely cleared
    one must not claim a supersession it never had."""
    _page(tmp_path, "entities/a/live", needs_review=True)
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    keep = _record(tmp_path, "keep", "entities/a/live", when="2026-09-05T00:00:00Z")
    dup = _record(tmp_path, "dup", "entities/a/live", when="2026-09-01T00:00:00Z")
    clr = _record(tmp_path, "clr", "entities/a/cleared")
    dry = _run(tmp_path).stdout
    assert f"(by {_load(keep)['review_id']})" in dry, dry
    assert _run(tmp_path, "--apply").returncode == 0
    assert "Superseded by" in _load(dup)["decision_note"]
    assert "Superseded by" not in _load(clr)["decision_note"]


def test_a_closed_record_keeps_its_field_order_and_its_unicode(tmp_path):
    """Ledger files are read and diffed by people. A closure must not reorder the record (burying
    review_id and subject) or escape non-ASCII text into \\u sequences."""
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    rec = _record(tmp_path, "r1", "entities/a/cleared")
    d = _load(rec); d["reasons"] = [{"code": "manual", "detail": "Überprüfung — naïve café"}]
    rec.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
    assert _run(tmp_path, "--apply").returncode == 0
    text = rec.read_text(encoding="utf-8")
    assert text.index("version:") < text.index("review_id:") < text.index("subject:"), "fields reordered"
    assert "Überprüfung — naïve café" in text, "non-ASCII text was escaped"


def test_the_same_slug_in_two_namespaces_does_not_cross_match(tmp_path):
    """The key is (NAMESPACE, slug). `entities/q/qakbot` and `concepts/q/qakbot` are different pages;
    keying on the shard letter instead of the namespace would let a live concept keep a dead entity
    request open against the wrong page."""
    _page(tmp_path, "concepts/q/qakbot", needs_review=True)      # a live page -- other namespace
    rec = _record(tmp_path, "r1", "entities/q/qakbot")           # no entity page exists
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["history"][-1]["reconcile_reason"] == "subject-absent"


def test_a_record_with_no_subject_does_not_crash_the_pass(tmp_path):
    """Ledger YAML comes from several producers; an empty or missing subject must be reconciled as
    absent, not raise mid-pass and leave the remainder of the ledger untouched."""
    store = tmp_path / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True)
    for name, subject in (("empty", ""), ("missing", None)):
        rec = {"version": 1, "review_id": f"review:x:{name}", "state": "open",
               "requested_at": "2026-09-01T00:00:00Z", "reasons": [], "history": []}
        if subject is not None:
            rec["subject"] = subject
        (store / f"{name}.yaml").write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8")
    out = _run(tmp_path, "--apply")
    assert out.returncode == 0, out.stderr
    for name in ("empty", "missing"):
        assert _load(store / f"{name}.yaml")["history"][-1]["reconcile_reason"] == "subject-absent"


def test_a_disposition_with_no_records_is_reported_as_zero(tmp_path):
    """A reason with nothing to close still appears, as 0 -- a missing line and a zero are not the
    same statement to an operator deciding whether to apply."""
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    _record(tmp_path, "r1", "entities/a/cleared")
    out = _run(tmp_path).stdout
    for reason, n in (("cleared-elsewhere", 1), ("superseded", 0), ("subject-absent", 0)):
        assert any(line.split() == [reason, str(n)] for line in out.splitlines()), (reason, out)


def test_a_one_segment_subject_does_not_match_a_nested_page_of_the_same_name(tmp_path):
    """A root-level subject `foo` and a page `foo/foo` are different. Widening the two-segment rule
    to one segment would key both as (foo, foo) and keep a dead request open against the wrong page
    -- verified to change the match relation on 516 of 2.4M generated path pairs."""
    _page(tmp_path, "foo/foo", needs_review=True)
    rec = _record(tmp_path, "r1", "foo")
    assert _run(tmp_path, "--apply").returncode == 0
    assert _load(rec)["history"][-1]["reconcile_reason"] == "subject-absent"


def test_the_cli_loads_the_source_tree_rule_not_a_stale_installed_copy(tmp_path):
    """The reconciler puts the source tree FIRST on sys.path. A stale `okengine` earlier on the path
    must not win -- that is the baked-vs-source divergence of okengine#832, where the code being run
    is not the code being reviewed. The shadowing copy here would crash if it were imported."""
    shadow = tmp_path / "shadow" / "okengine"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "review_policy.py").write_text(
        "raise RuntimeError('a stale installed okengine was loaded instead of the source tree')\n",
        encoding="utf-8")
    _page(tmp_path, "entities/a/cleared", needs_review=False)
    _record(tmp_path, "r1", "entities/a/cleared")
    import os
    env = dict(os.environ, PYTHONPATH=str(tmp_path / "shadow"))
    out = subprocess.run([sys.executable, str(CLI), str(tmp_path), "--reconcile"],
                         capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    assert "stale installed okengine" not in out.stderr


def test_the_dry_run_lists_a_bounded_sample_not_every_record(tmp_path):
    """On a live ledger this pass closes thousands of records. The report shows exactly five examples
    -- enough to sanity-check what --apply would do, bounded so it cannot flood the terminal."""
    for i in range(8):
        _page(tmp_path, f"entities/a/c{i}", needs_review=False)
        _record(tmp_path, f"r{i}", f"entities/a/c{i}")
    out = _run(tmp_path).stdout
    assert sum(1 for line in out.splitlines() if line.strip().startswith("e.g.")) == 5, out
