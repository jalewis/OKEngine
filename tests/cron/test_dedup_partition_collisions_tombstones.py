"""okengine#663: the dedup drain was tombstone-blind and could retire a live page.

`dedup_namespace` grouped every same-slug copy with no `status` filter; `_merge_fm` took the
first non-empty scalar from the DEEPEST path; `needs_review` fired only on body differences. So a
live flat page plus a deeper tombstone merged into one page with `status: tombstoned`, the
tombstone's `id`, a leaked `superseded_by`, and no review flag -- and two live copies that
disagreed on `status: live` vs `retracted` with identical bodies were resolved by path depth,
silently (the assessments incident CLAUDE.md cites). `find_page`/`write_key` also routed an
importer's update INTO a deeper tombstone while `build_map` skips tombstones.

Contract pinned here (matching okf_migrate's classify_collisions vocabulary):
  * a tombstone never occupies a seat and never wins a merge;
  * a REDUNDANT tombstone (superseded_by names the live survivor) is a loser: retired, links
    rewritten to the survivor -- this is exactly the call build_map defers to the dedup pass;
  * a tombstone pointing ELSEWHERE is load-bearing: left untouched, never overwritten, reported;
  * live copies that disagree on a non-volatile scalar are merged with `needs_review: true` and
    the conflicting fields named in the report, never resolved by depth alone.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts" / "cron"


def _load():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "dedup_partition_collisions_tomb", SCRIPTS / "dedup_partition_collisions.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["dedup_partition_collisions_tomb"] = m
    spec.loader.exec_module(m)
    m.okf_migrate._SCHEMA_CACHE.clear()
    return m


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n  assessment: {required: [type]}\n"
        "partitioning:\n  namespaces:\n"
        "    entities: {strategy: by-letter}\n"
        "    assessments: {strategy: by-letter}\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    return tmp_path


def _page(vault: Path, rel: str, fm: str, body: str = "Body.") -> Path:
    p = vault / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm.rstrip()}\n---\n\n{body}\n", encoding="utf-8")
    return p


def _fm(p: Path) -> dict:
    import yaml
    text = p.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n", 2)[1])


def _run(m, vault: Path, *args) -> str:
    with redirect_stdout(io.StringIO()) as out:
        assert m.main(["--root", str(vault), *args]) == 0
    return out.getvalue()


def test_redundant_tombstone_is_retired_and_the_live_page_takes_the_seat(tmp_path):
    v = _vault(tmp_path)
    live = _page(v, "entities/acme", "type: actor\nid: pack:acme\nstatus: active\naliases: [ACME]")
    tomb = _page(v, "entities/a/acme",
                 "type: actor\nid: pack:acme-old\nstatus: tombstoned\nsuperseded_by: pack:acme")
    ref = _page(v, "note", "type: concept", "see [[entities/a/acme]] and [[entities/acme]]")
    m = _load()
    out = _run(m, v, "--apply")
    survivor = v / "wiki" / "entities" / "a" / "acme.md"
    assert survivor.is_file() and not live.exists()
    fm = _fm(survivor)
    assert fm["status"] == "active" and fm["id"] == "pack:acme", fm
    assert "superseded_by" not in fm and "needs_review" not in fm
    assert fm["aliases"] == ["ACME"]
    text = ref.read_text()
    assert text.count("[[entities/a/acme]]") == 2, text
    assert "1 removed" in out or "removed" in out


def test_load_bearing_tombstone_is_never_overwritten_or_merged(tmp_path):
    v = _vault(tmp_path)
    live = _page(v, "entities/acme", "type: actor\nid: pack:acme\nstatus: active")
    tomb = _page(v, "entities/a/acme",
                 "type: actor\nid: pack:old\nstatus: tombstoned\nsuperseded_by: entities/a/acme-corp")
    before_tomb, before_live = tomb.read_text(), live.read_text()
    m = _load()
    out = _run(m, v, "--apply")
    assert tomb.read_text() == before_tomb, "a redirect that points elsewhere is load-bearing"
    assert live.read_text() == before_live, "the live page is not merged into a tombstone"
    assert "blocked" in out.lower() or "tombstone" in out.lower()


def test_live_copies_that_disagree_on_status_are_flagged_not_resolved_by_depth(tmp_path):
    v = _vault(tmp_path)
    _page(v, "assessments/x", "type: assessment\nid: a:1\nstatus: live", "Same body.")
    _page(v, "assessments/a/x", "type: assessment\nid: a:2\nstatus: retracted", "Same body.")
    m = _load()
    out = _run(m, v, "--apply")
    survivor = v / "wiki" / "assessments" / "a" / "x.md"
    fm = _fm(survivor)
    assert fm.get("needs_review") is True, fm
    assert "status" in out and "id" in out, out       # the conflicting fields are named
    assert not (v / "wiki" / "assessments" / "x.md").exists()


def test_volatile_fields_do_not_trigger_review(tmp_path):
    v = _vault(tmp_path)
    _page(v, "entities/beta", "type: actor\nid: pack:beta\nupdated: 2026-01-01", "Same.")
    _page(v, "entities/b/beta", "type: actor\nid: pack:beta\nupdated: 2026-02-02", "Same.")
    m = _load()
    _run(m, v, "--apply")
    fm = _fm(v / "wiki" / "entities" / "b" / "beta.md")
    assert "needs_review" not in fm, fm


def test_only_tombstones_are_left_alone(tmp_path):
    v = _vault(tmp_path)
    a = _page(v, "entities/gone", "type: actor\nstatus: tombstoned\nsuperseded_by: entities/g/x")
    b = _page(v, "entities/g/gone", "type: actor\nstatus: tombstoned\nsuperseded_by: entities/g/x")
    m = _load()
    _run(m, v, "--apply")
    assert a.exists() and b.exists()


def test_dry_run_reports_and_touches_nothing(tmp_path):
    v = _vault(tmp_path)
    live = _page(v, "entities/acme", "type: actor\nid: pack:acme\nstatus: active")
    tomb = _page(v, "entities/a/acme",
                 "type: actor\nid: pack:acme-old\nstatus: tombstoned\nsuperseded_by: pack:acme")
    m = _load()
    out = _run(m, v)
    assert "DRY-RUN" in out and live.exists() and tomb.exists()


# ── okf_migrate: seat resolution must agree with the drain ────────────────────

def _migrate():
    spec = importlib.util.spec_from_file_location("okf_migrate_seat", SCRIPTS / "okf_migrate.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["okf_migrate_seat"] = m
    spec.loader.exec_module(m)
    m._SCHEMA_CACHE.clear()
    return m


def test_find_page_prefers_a_live_copy_over_a_deeper_tombstone(tmp_path):
    v = _vault(tmp_path)
    live = _page(v, "entities/a/acme", "type: actor\nid: pack:acme\nstatus: active")
    _page(v, "entities/a/c/acme", "type: actor\nstatus: tombstoned\nsuperseded_by: pack:acme")
    m = _migrate()
    assert m.find_page(v, "entities", "acme") == live


def test_find_page_still_returns_a_lone_tombstone(tmp_path):
    """Callers must still be able to SEE that a slug is retired (the write path refuses to
    resurrect it); only the choice between live and retired changes."""
    v = _vault(tmp_path)
    tomb = _page(v, "entities/a/acme", "type: actor\nstatus: tombstoned\nsuperseded_by: x")
    m = _migrate()
    assert m.find_page(v, "entities", "acme") == tomb


def test_write_key_routes_to_the_live_copy_when_the_seat_holds_a_tombstone(tmp_path):
    v = _vault(tmp_path)
    _page(v, "entities/a/acme", "type: actor\nstatus: tombstoned\nsuperseded_by: entities/x")
    _page(v, "entities/acme", "type: actor\nid: pack:acme\nstatus: active")
    m = _migrate()
    assert m.write_key(v, "entities", "acme", {"type": "actor"}) == "entities/acme"
