"""Standing detector: no engine cache may be keyed on a file's mtime (okengine#1017).

An mtime-only cache key serves stale data when a file is rewritten within one filesystem timestamp
tick, which on a fast disk is the common case: the enforced write path validated against an
outdated schema, and the index lane counted a just-retired page as live. Every file-backed cache now
keys on ``okengine.file_fingerprint.fingerprint()``.

This test pins every remaining ``st_mtime`` / ``st_mtime_ns`` / ``getmtime`` read in engine code to
a reviewed use that is NOT a cache key: an age or freshness check, a recency sort, a time-window
filter, a change tripwire, or archive metadata. A new read fails here until it is reviewed. If it
keys a cache, use ``fingerprint()`` instead; otherwise add it below with its reason. The counts are
exact, so a removed read must also be removed here: the list only shrinks.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = (
    "applications", "bin", "ci", "extensions", "okengine-cockpit", "okengine-mcp",
    "okengine-operations", "okengine-projection", "okengine-reader", "overlays", "patches",
    "plugins", "scripts", "src", "tools",
)
MTIME_READ = re.compile(r"\bst_mtime(?:_ns)?\b|\bgetmtime\(")

# path -> (exact count of mtime reads, why none of them keys a cache)
REVIEWED = {
    "src/okengine/file_fingerprint.py": (4, "the fingerprint helper itself"),
    "okengine-mcp/server.py": (3, "backlink artifact max-age; vault high-water mark for reindex"),
    "okengine-reader/backlinks.py": (1, "backlink artifact max-age"),
    "src/okengine/cockpit_services/navigation.py": (1, "backlink artifact max-age"),
    "okengine-reader/directory_index.py": (1, "human-readable mtime prefix of the revision token"),
    "okengine-projection/service.py": (1, "newer-than-indexed check, then a content digest"),
    "src/okengine/projection/projector.py": (2, "file_mtime column value"),
    "src/okengine/operations/run.py": (1, "plan snapshot digest"),
    "scripts/framework_ingest_rebuild.py": (1, "canonical-page mutation tripwire"),
    "patches/cron-plus/slug_identity_guard.py": (1, "run-window overlap with an uncertainty margin"),
    "extensions/okengine.actor-risk-ranking/actor_risk_rank.py": (1, "input age"),
    "extensions/okengine.lacuna/select_lacuna_field.py": (1, "display date"),
    "extensions/okengine.messaging-synthesis/select_messaging_synthesis.py": (1, "recency sort"),
    "extensions/okengine.messaging-synthesis/select_positioning_battle_cards.py": (
        2, "input-newer-than-output rebuild check"),
    "extensions/okengine.predictions/select_regrade_batch.py": (1, "changed-since watermark"),
    "scripts/backfill_qualification.py": (2, "observed timestamp fallback"),
    "scripts/cron/dedupe_raw_captures.py": (1, "keep the oldest capture"),
    "scripts/cron/fleet_health.py": (3, "report and lane-quiet age"),
    "scripts/cron/generate_project_stats.py": (1, "recent/stale bucket counts"),
    "scripts/cron/health_export.py": (3, "artifact age and ordering"),
    "scripts/cron/observability_validate.py": (1, "artifact age"),
    "scripts/cron/rebuild_index.py": (1, "recency sort"),
    "scripts/cron/select_entity_candidates.py": (1, "recency sort"),
    "scripts/cron/select_raw_batch.py": (1, "year derivation fallback and ordering"),
    "scripts/cron/select_unscored_sources.py": (1, "recency sort"),
    "scripts/cron/slot_starvation.py": (2, "time-window filter"),
    "scripts/cron/usage_rollup.py": (1, "settle delay before a log is rolled up"),
    "scripts/cron/wiki_change_check.py": (4, "changed-since-baseline report"),
    "scripts/cron/wiki_health_audit.py": (5, "changed-since-baseline report and ordering"),
    "scripts/cron/year_derivation_audit.py": (1, "year derivation fallback"),
    "scripts/extract-docs.py": (2, "output-newer-than-input rebuild check"),
    "scripts/extract-html.py": (2, "output-newer-than-input rebuild check"),
    "scripts/fleet_status.py": (3, "time-window filter and recency sort"),
    "scripts/framework_backup.py": (6, "archive member mtime; newest-backup sort; snapshot settle "
                                    "and hot-file checks and the in-place-rewrite re-check, which "
                                    "copy any file "
                                    "unsettled at the fence (SETTLE_NS) so a same-tick rewrite "
                                    "cannot pass unseen (okengine#1032)"),
    "scripts/framework_lanes.py": (1, "written-during-run window"),
    "scripts/repair_daily_brief_freshness.py": (2, "date fallback"),
    "scripts/scheduler_watchdog.py": (1, "tick age"),
}


def scan(repo: Path, roots=ROOTS) -> dict[str, int]:
    """Count mtime reads per Python file under ``roots``."""
    counts: dict[str, int] = {}
    for root in roots:
        base = repo / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            hits = len(MTIME_READ.findall(path.read_text(encoding="utf-8", errors="replace")))
            if hits:
                counts[path.relative_to(repo).as_posix()] = hits
    return counts


def unreviewed(counts: dict[str, int], reviewed=REVIEWED) -> list[str]:
    """Every file whose mtime reads differ from the reviewed count, with a fix hint."""
    problems = []
    for rel in sorted(set(counts) | set(reviewed)):
        have, want = counts.get(rel, 0), reviewed.get(rel, (0, ""))[0]
        if have != want:
            problems.append(f"{rel}: {have} mtime read(s), {want} reviewed")
    return problems


def test_every_engine_mtime_read_is_a_reviewed_non_cache_use():
    problems = unreviewed(scan(REPO))
    assert not problems, (
        "mtime reads changed in engine code. A cache must key on okengine.file_fingerprint."
        "fingerprint(), never on mtime (okengine#1017); a non-cache use goes into REVIEWED with "
        "its reason:\n  " + "\n  ".join(problems))


def test_the_detector_flags_a_new_mtime_keyed_cache(tmp_path):
    """Negative fixture: a planted mtime-keyed cache in an engine root is reported."""
    lib = tmp_path / "scripts" / "cron" / "planted.py"
    lib.parent.mkdir(parents=True)
    lib.write_text("def load(p, cache):\n    k = p.stat().st_mtime_ns\n"
                   "    return cache.get((p, k))\n", encoding="utf-8")
    assert unreviewed(scan(tmp_path), reviewed={}) == [
        "scripts/cron/planted.py: 1 mtime read(s), 0 reviewed"]


def test_the_detector_flags_a_stale_reviewed_entry(tmp_path):
    """A reviewed read that was removed must leave the list, so the list only shrinks."""
    assert unreviewed(scan(tmp_path), reviewed={"scripts/gone.py": (1, "age")}) == [
        "scripts/gone.py: 0 mtime read(s), 1 reviewed"]


def test_the_detector_sees_getmtime_and_skips_bytecode_caches(tmp_path):
    src = tmp_path / "src" / "mod.py"
    src.parent.mkdir()
    src.write_text("import os\nos.path.getmtime('x')\n", encoding="utf-8")
    cached = tmp_path / "src" / "__pycache__" / "mod.py"
    cached.parent.mkdir()
    cached.write_text("x.st_mtime\n", encoding="utf-8")
    assert scan(tmp_path) == {"src/mod.py": 1}
