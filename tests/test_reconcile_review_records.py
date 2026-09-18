"""Behavioral coverage for version-locked review-record reconciliation."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest
import yaml


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "reconcile_review_records", REPO / "scripts" / "reconcile_review_records.py")
R = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(R)


def _page(wiki: Path, subject: str, *, version=1, review=True) -> tuple[Path, str]:
    path = wiki / f"{subject}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\nversion: {version}\nneeds_review: {str(review).lower()}\n---\n# Page\n"
    path.write_text(text, encoding="utf-8")
    return path, hashlib.sha256(text.encode()).hexdigest()


def _record(store: Path, name: str, subject: str, version: int, digest: str,
            **extra) -> Path:
    path = store / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"state": "open", "subject": subject, "subject_version": version,
              "subject_hash": digest, "version": 1, **extra}
    path.write_text(yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(("detail", "code"), [
    ("Unresolvable wikilink found", "broken-link"),
    ("Degenerate draft", "agent-draft"),
    ("Repetition detected", "agent-draft"),
    ("Slug ID collision", "id-collision"),
    ("Immutable field change reverted", "rejected-write"),
    ("Field `type` conflicts", "owner-conflict"),
    ("Needs judgment", "manual"),
])
def test_reason_codes(detail, code):
    assert R._reason_code(detail) == code


def test_page_state_defensive_frontmatter_paths(tmp_path):
    plain = tmp_path / "plain.md"
    plain.write_text("body", encoding="utf-8")
    assert R._page_state(plain)[::2] == (1, False)

    malformed = tmp_path / "malformed.md"
    malformed.write_text("---\n[bad\n---\nbody", encoding="utf-8")
    assert R._page_state(malformed)[::2] == (1, False)

    invalid_version = tmp_path / "invalid.md"
    invalid_version.write_text("---\nversion: {bad: value}\nneeds_review: true\n---\n", encoding="utf-8")
    assert R._page_state(invalid_version)[::2] == (1, True)


def test_queue_reasons_missing_and_newest_wins(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    assert R._queue_reasons(wiki) == {}
    (wiki / "_review-queue.md").write_text(
        "not a queue row\n"
        "- 2026-08-02 **entities/a.md** — newest\n"
        "- 2026-08-01 **entities/a.md** — older\n", encoding="utf-8")
    assert R._queue_reasons(wiki) == {"entities/a": "newest"}


def test_atomic_write_cleans_temp_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "record.yaml"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(R.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        R._write(path, {"state": "open"})
    assert path.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".review-reconcile-*"))


def test_main_dry_run_and_apply_reconcile_every_disposition(tmp_path, monkeypatch, capsys):
    pack = tmp_path / "pack"
    wiki = pack / "wiki"
    store = wiki / "operational" / "reviews"
    store.mkdir(parents=True)

    _, current_hash = _page(wiki, "entities/current", version=2, review=True)
    _, hydrate_hash = _page(wiki, "entities/hydrate", version=1, review=True)
    _, unflagged_hash = _page(wiki, "entities/unflagged", version=1, review=False)
    _, superseded_hash = _page(wiki, "entities/superseded", version=2, review=True)

    keep = _record(store, "keep", "entities/current", 2, current_hash,
                   requested_at="2026-08-02", reasons=[{"code": "manual"}])
    duplicate = _record(store, "duplicate", "entities/current", 2, current_hash,
                        requested_at="2026-08-01", reasons=[{"code": "legacy-unspecified"}])
    hydrate = _record(store, "hydrate", "entities/hydrate", 1, hydrate_hash,
                      reasons=["legacy"])
    missing = _record(store, "missing", "entities/missing", 1, "absent")
    superseded = _record(store, "superseded", "entities/superseded", 1, superseded_hash)
    unflagged = _record(store, "unflagged", "entities/unflagged", 1, unflagged_hash)
    empty_subject = _record(store, "empty-subject", "", 1, "none")
    (store / "closed.yaml").write_text("state: approved\n", encoding="utf-8")
    (store / "scalar.yaml").write_text("- scalar\n", encoding="utf-8")
    (store / "bad.yaml").write_text("[bad", encoding="utf-8")
    (wiki / "_review-queue.md").write_text(
        "- 2026-08-02 **entities/hydrate.md** — field `type` conflicts with owner\n",
        encoding="utf-8")

    original = {path: path.read_text(encoding="utf-8")
                for path in (keep, duplicate, hydrate, missing, superseded, unflagged)}
    monkeypatch.setattr(sys, "argv", ["reconcile", str(pack)])
    assert R.main() == 0
    dry = capsys.readouterr().out
    assert "DRY RUN" in dry
    assert "duplicate request for the current subject version=1" in dry
    assert "reasons-hydrated=1" in dry
    assert all(path.read_text(encoding="utf-8") == text for path, text in original.items())

    monkeypatch.setattr(sys, "argv", ["reconcile", "--apply", str(pack)])
    assert R.main() == 0
    applied = capsys.readouterr().out
    assert "APPLY" in applied and "kept-open=2" in applied

    assert yaml.safe_load(keep.read_text())["state"] == "open"
    hydrated = yaml.safe_load(hydrate.read_text())
    assert hydrated["reasons"] == [{"code": "owner-conflict",
                                     "detail": "field `type` conflicts with owner"}]
    assert hydrated["version"] == 2
    assert hydrated["history"][-1]["action"] == "hydrate-reason"
    for path, reason in (
        (duplicate, "duplicate request for the current subject version"),
        (missing, "subject page no longer exists"),
        (superseded, "superseded by a newer subject version"),
        (unflagged, "subject no longer requests review"),
        (empty_subject, "subject page no longer exists"),
    ):
        record = yaml.safe_load(path.read_text())
        assert record["state"] == "dismissed"
        assert record["decision_note"] == reason
        assert record["decision_service"] == "maintenance"
        assert record["history"][-1]["decision"] == "dismiss"


def test_main_skips_unreadable_record(tmp_path, monkeypatch, capsys):
    pack = tmp_path / "pack"
    record = pack / "wiki" / "operational" / "reviews" / "record.yaml"
    record.parent.mkdir(parents=True)
    record.write_text("state: open\n", encoding="utf-8")
    original = R.Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == record:
            raise OSError("gone")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(R.Path, "read_text", unreadable)
    monkeypatch.setattr(sys, "argv", ["reconcile", str(pack)])
    assert R.main() == 0
    assert "DRY RUN" in capsys.readouterr().out
