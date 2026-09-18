"""A retired page must not compete for a canonical seat (okengine#520).

`build_map` read every page under a namespace, tombstoned or not. That gave a retired
duplicate two ways to cause damage:

  * it tried to MOVE, so a redirect that exists precisely so links to the superseded location
    still resolve would have been relocated away from the path that needs it; and
  * it CLAIMED a seat, so a live page lost its own shard to a page nothing reads.

On a private market-intel pack that was 44 of 80 held-back collisions. Excluding retired pages took
the namespace from 1 movable page / 80 collisions to 21 / 15.

The remaining collisions are deliberately NOT auto-resolved, and the classifier exists so they
stay distinguishable instead of collapsing into one number — see `classify_collisions`. In
particular a live page whose seat is held by a *redundant* tombstone is still held back here:
freeing it means retiring the tombstone, which is the dedup pass's call, and this mover does
not delete.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MIGRATE = REPO / "scripts" / "cron" / "okf_migrate.py"


def _load():
    if str(MIGRATE.parent) not in sys.path:
        sys.path.insert(0, str(MIGRATE.parent))
    spec = importlib.util.spec_from_file_location("okf_migrate_tomb", MIGRATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["okf_migrate_tomb"] = module
    spec.loader.exec_module(module)
    module._SCHEMA_CACHE.clear()
    return module


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types:\n  source: {required: [type]}\n"
        "partitioning:\n  namespaces:\n"
        "    sources: {strategy: by-date, date_field: published}\n", encoding="utf-8")
    return tmp_path


def _page(vault: Path, rel: str, *, pid, published="2026-05-04",
          status=None, superseded_by=None):
    p = vault / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: source", f"id: {pid}", f"published: {published}"]
    if status:
        lines.append(f"status: {status}")
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")
    lines += ["---", "Body.", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_a_tombstoned_page_never_moves(tmp_path):
    """Its path IS the redirect. Moving it takes the redirect off the path that needs it."""
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2020/01/old-story", pid="sources:old", status="tombstoned",
          superseded_by="sources:new")
    move_map, collisions = m.build_map(v, "sources")
    assert move_map == {}, "a retired page must stay where stale links expect it"
    assert collisions == []


def test_a_tombstone_does_not_take_a_seat_from_a_live_page(tmp_path):
    """Two pages, one canonical seat, one of them retired — the live page wins outright
    rather than both being held back."""
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:live")
    _page(v, "sources/2026/07/06/sec/story", pid="sources:dead", status="tombstoned",
          superseded_by="sources:live")
    move_map, collisions = m.build_map(v, "sources")
    assert move_map == {"sources/2026/07/06/market/story": "sources/2026/05/story"}
    assert collisions == [], "a retired page is not a competing claim"


def test_two_live_pages_still_collide(tmp_path):
    """The guard that matters stays: this mover must not silently pick a winner."""
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:a")
    _page(v, "sources/2026/07/06/sec/story", pid="sources:b")
    move_map, collisions = m.build_map(v, "sources")
    assert move_map == {}
    assert len(collisions) == 2
    kinds = m.classify_collisions(v, collisions)
    assert len(kinds["live_conflict"]) == 2


def test_a_live_page_blocked_by_a_redundant_tombstone_is_held_but_named(tmp_path):
    """Held back — freeing it means deleting the tombstone, which is the dedup pass's call.
    But it must be reported as resolvable rather than lumped in with real conflicts."""
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:survivor")
    _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned",
          superseded_by="sources:survivor")
    move_map, collisions = m.build_map(v, "sources")
    assert move_map == {}, "this mover does not delete to make room"
    kinds = m.classify_collisions(v, collisions)
    assert len(kinds["redundant_tombstone"]) == 1
    assert kinds["blocked_by_tombstone"] == [] and kinds["live_conflict"] == []


def test_a_tombstone_pointing_elsewhere_is_not_called_redundant(tmp_path):
    """Only `superseded_by == the mover's own id` proves the redirect is superfluous."""
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:survivor")
    _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned",
          superseded_by="sources:some-third-page")
    _, collisions = m.build_map(v, "sources")
    kinds = m.classify_collisions(v, collisions)
    assert len(kinds["blocked_by_tombstone"]) == 1
    assert kinds["redundant_tombstone"] == []


def test_a_path_designating_a_different_page_is_not_redundant(tmp_path):
    """`superseded_by` holds a PATH by convention — `dedup_sources_by_url` writes `survivor_rel`
    and `write_server._tombstone` sanitises it as a path (15,978 of 16,061 live values). So a
    path is matched, not rejected; what disqualifies this one is that it designates a DIFFERENT
    page. Comparing only against the mover's id — the first version of this — misfiled two
    genuinely redundant seats as unresolvable."""
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:survivor")
    _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned",
          superseded_by="sources/frontier/2026/story")
    _, collisions = m.build_map(v, "sources")
    kinds = m.classify_collisions(v, collisions)
    assert len(kinds["blocked_by_tombstone"]) == 1
    assert kinds["redundant_tombstone"] == []


def test_a_path_designating_the_mover_is_redundant(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:survivor")
    _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned",
          superseded_by="sources/2026/07/06/market/story")
    _, collisions = m.build_map(v, "sources")
    kinds = m.classify_collisions(v, collisions)
    assert len(kinds["redundant_tombstone"]) == 1
    assert kinds["blocked_by_tombstone"] == []


def test_a_mover_with_no_id_is_never_called_redundant(tmp_path):
    """Empty must not match empty: a page with no id and a tombstone with no pointer would
    otherwise compare equal and be reported as safe to resolve."""
    m = _load()
    v = _vault(tmp_path)
    p = _page(v, "sources/2026/07/06/market/story", pid="sources:x")
    p.write_text(p.read_text().replace("id: sources:x\n", ""), encoding="utf-8")
    _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned")
    _, collisions = m.build_map(v, "sources")
    kinds = m.classify_collisions(v, collisions)
    assert kinds["redundant_tombstone"] == []
    assert len(kinds["blocked_by_tombstone"]) == 1


def test_status_spelling_matches_the_rest_of_the_engine(tmp_path):
    m = _load()
    assert m.is_tombstoned({"status": "  TOMBSTONED "}) is True
    assert m.is_tombstoned({"status": "active"}) is False
    assert m.is_tombstoned({}) is False
    assert m.is_tombstoned(None) is False
