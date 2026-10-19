"""Fail deterministic cron runs that create a separator-equivalent live page."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import yaml


LOGGER = logging.getLogger(__name__)
ARTIFACT_PREFIX = "OKENGINE_ARTIFACT:"
# Attributions that do not fail the job: the page is quarantined and reported, not blamed.
_NOT_BLAMED = frozenset({"unattributed"})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# File metadata may use a coarser clock than snapshot's wall clock (#946). Treat up to
# two seconds before the window as uncertain, including quantization across a second boundary.
_MTIME_UNCERTAINTY_NS = 2_000_000_000
_RESERVED = {"index.md", "log.md", "agents.md", "hot.md", "bundle.md", "health.md"}
_ID_LIB: ModuleType | None = None


class SlugIdentityCollision(RuntimeError):
    """A deterministic writer introduced a weak-identity collision."""


class Snapshot(NamedTuple):
    paths: frozenset[str]
    # When the window opened. Attribution asks which OTHER runs overlapped it (okengine#883).
    taken_at: datetime | None = None


def _id_lib() -> ModuleType:
    global _ID_LIB
    if _ID_LIB is not None:
        return _ID_LIB
    scripts = Path(os.environ.get("OKENGINE_CRON_SCRIPTS", "/opt/data/scripts"))
    target = scripts / "id_lib.py"
    spec = importlib.util.spec_from_file_location("okengine_cron_id_lib", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load governed identity library at {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _ID_LIB = module
    return module


def _is_generated_structural(name: str) -> bool:
    """An engine-GENERATED structural artifact, not an authored page (okengine#836).

    Mirrors `schema_validator._is_generated_structural` -- the canonical rule, which write_server's
    reserve check and `conformance_audit._is_structural` also follow. Kept in sync by
    `test_generated_structural_rule_matches_the_validator`.

    These are PER-DIRECTORY artifacts: `INDEX-p02.md` is supposed to exist in every shard, and
    those files are different pages that happen to share a basename, not spellings of one page.
    Identity here is (namespace, slug_identity), and `qualified_namespace` returns `entities` for
    every shard beneath it -- so without this exemption all 687 shard directories' `INDEX-p02.md`
    collapse onto one key and every index rebuild "introduces" a collision. Measured on a live
    vault: 1,216 of 1,420 quarantined pages were this, against 204 real ones, which buried the
    genuine signal under ~180/day of noise. Bare `INDEX.md` was already exempt via `_RESERVED`
    (compared lowercased); only the paginated family was missed.
    """
    return (
        name.startswith(("_", "."))
        or name == "INDEX.md"
        or name.startswith("INDEX-")
        or name.lower().startswith("index-p")
    )


def _eligible_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.endswith(".md")
        and lowered not in _RESERVED
        and not _is_generated_structural(name)
        and ".bak." not in name
    )


def _iter_paths(wiki: Path):
    if not wiki.is_dir():
        return
    for root, directories, files in os.walk(wiki):
        directories[:] = [
            name for name in directories if not name.startswith(("_", "."))
        ]
        root_path = Path(root)
        relative_root = root_path.relative_to(wiki)
        for name in files:
            if not _eligible_name(name):
                continue
            path = root_path / name
            rel = (relative_root / name).as_posix()
            yield rel, path


def snapshot(wiki: Path) -> Snapshot:
    """Capture names only; deterministic lanes must not parse the corpus twice."""
    taken_at = datetime.now(timezone.utc)
    return Snapshot(frozenset(rel for rel, _path in _iter_paths(wiki)), taken_at)


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    try:
        value = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _live(path: Path) -> bool:
    return str(_frontmatter(path).get("status") or "").strip().lower() != "tombstoned"


def _declared_paths(wiki: Path, response: str | None) -> set[Path]:
    """Pages this run named as its own outputs (`OKENGINE_ARTIFACT:` lines, the contract
    artifact_records.py validates later). Relative paths are vault-relative, as there. A line that
    does not parse names nothing; artifact_records rejects it for opted-in jobs."""
    declared: set[Path] = set()
    for line in (response or "").splitlines():
        if not line.startswith(ARTIFACT_PREFIX):
            continue
        try:
            item = json.loads(line[len(ARTIFACT_PREFIX):])
        except ValueError:
            continue
        path = item.get("path") if isinstance(item, dict) else None
        if isinstance(path, str) and path:
            declared.add((wiki.parent / path).resolve())
    return declared


def _when(value: object) -> datetime | None:
    """An aware timestamp from a run record, or None when it cannot be read as one."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _overlaps(path: Path, start: datetime, now: datetime) -> bool:
    """Did the run this record describes overlap [start, now]?

    A record that cannot be interpreted is UNKNOWN, not absent: it counts when the file itself was
    written inside the window or its bounded mtime uncertainty interval, and is ignored when it
    predates that interval. Readable run timestamps retain full precision. A `running` record whose
    worker is gone ended at an unknown time, so only its start is known: it overlaps iff it started in the window,
    or one orphaned record would disable attribution forever. Every record began before `now`
    (it was written before this scan), so a run overlaps exactly when it ended at or after
    `start`."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    begun = _when(record.get("started_at")) if isinstance(record, dict) else None
    ended_raw = record.get("ended_at") if isinstance(record, dict) else None
    ended = _when(ended_raw) if ended_raw is not None else None
    if begun is None or (ended_raw is not None and ended is None):
        return _recent(path, start)
    if ended is None:
        ended = now if _alive(record.get("pid")) else begun
    return ended >= start


def _recent(path: Path, start: datetime) -> bool:
    """Could this mtime describe a write in the window, given two-second clock uncertainty?

    Kernel/file timestamps can round a write after snapshot() to a time before it. Unreadable
    records in that boundary interval remain unknown candidates, not proof that no writer ran.
    Compare integer nanoseconds so a stale record one nanosecond outside the interval stays
    stale. A record that vanished mid-scan does not describe a writer.
    """
    elapsed = start - _EPOCH
    start_ns = ((elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000
                + elapsed.microseconds * 1000)
    try:
        return path.stat().st_mtime_ns >= start_ns - _MTIME_UNCERTAINTY_NS
    except OSError:
        return False


def _lane_overlapped(lane: Path, start: datetime, now: datetime) -> bool:
    """Did any run of this lane overlap [start, now]?

    A live vault holds tens of thousands of records, so most are ruled out by a stat: a record
    last written before the window opened is a run that finished before it (the finish rewrites
    the record) or a `running` marker. Only the lane's NEWEST record can be a run still going --
    the PID lock keeps a lane from overlapping itself -- so it is always read; older ones are read
    only when written inside the window."""
    records = sorted(lane.glob("*.json"))  # glob-ok: one lane's flat record directory
    if not records:
        return False
    *older, newest = records
    return _overlaps(newest, start, now) or any(
        _overlaps(path, start, now) for path in older if _recent(path, start)
    )


def _concurrent_jobs(
    runs_root: Path, start: datetime, now: datetime, job_id: str
) -> list[str] | None:
    """Other cron-plus lanes whose runs overlapped this run's window, from the runner-owned
    records (run_records.py: `<runs_root>/<job_id>/<stamp>.json`). Read only when an introduction
    was found, so a clean run never pays for the scan. This job's own records are excluded: the
    scheduler's PID lock keeps a lane from overlapping itself.

    None when the records directory is absent: this run's own start record should be in it, so
    its absence means the records are not being written (run_records logs that), and "no other
    run" would be read off missing data. The caller then falls back to the legacy contract."""
    if not runs_root.is_dir():
        return None
    return sorted(
        lane.name
        for lane in runs_root.iterdir()
        if lane.is_dir() and lane.name != job_id and _lane_overlapped(lane, start, now)
    )


def _attribute(
    path: Path, declared: set[Path], concurrent: list[str] | None
) -> str:
    """Who wrote `path`, as far as the runner can tell (okengine#883).

    The guard sees a whole-wiki difference, not a writer. It names this job only on evidence:
    the run declared the file as its own artifact, or no other cron-plus lane ran inside the
    window. Otherwise another lane may have written it, and failing this one blames a bystander
    (budget-guard was failed for nvd-import's page). Writers outside cron-plus (an operator, a
    host cron) leave no run record, so `sole-runner` is the runner's best evidence, not proof.
    `concurrent` is None when there are no run records to consult (none supplied, or the
    directory is missing): the legacy contract, which attributes to this job, labeled
    `unassessed` in the receipt."""
    if path.resolve() in declared:
        return "declared-artifact"
    if concurrent is None:
        return "unassessed"
    return "unattributed" if concurrent else "sole-runner"


def enforce(
    wiki: Path,
    before: Snapshot,
    quarantine_root: Path,
    *,
    job_id: str,
    runs_root: Path | None = None,
    response: str | None = None,
) -> list[dict]:
    """Quarantine collisions introduced during this invocation; fail it only when it wrote one.

    Every introduced collision is moved out of the wiki regardless of attribution: the page is a
    duplicate whoever wrote it. The job fails when at least one is attributed to it; an
    unattributed one is logged and recorded in the receipt with the lanes that overlapped, and
    the job is returned its records instead of failing."""
    identity = _id_lib()
    created_paths = {
        rel: path for rel, path in _iter_paths(wiki) if rel not in before.paths
    }
    created = sorted(created_paths)
    if not created:
        return []

    new_keys = {
        (
            identity.qualified_namespace(wiki, created_paths[rel]),
            identity.slug_identity(created_paths[rel].stem),
        )
        for rel in created
    }
    new_keys.discard(("", ""))
    groups: dict[tuple[str, str], list[str]] = {key: [] for key in new_keys if all(key)}
    for rel, path in _iter_paths(wiki):
        key = (
            identity.qualified_namespace(wiki, path),
            identity.slug_identity(path.stem),
        )
        if key in groups and _live(path):
            groups[key].append(rel)

    introduced: list[tuple[str, list[str]]] = []
    for rel in created:
        path = created_paths[rel]
        key = (identity.qualified_namespace(wiki, path), identity.slug_identity(path.stem))
        matches = sorted(groups.get(key, []))
        if _live(path) and len(matches) > 1:
            introduced.append((rel, matches))
    if not introduced:
        return []

    now = datetime.now(timezone.utc)
    start = before.taken_at or datetime.min.replace(tzinfo=timezone.utc)
    concurrent = (
        _concurrent_jobs(runs_root, start, now, job_id) if runs_root is not None else None
    )
    declared = _declared_paths(wiki, response)
    run_root = quarantine_root / now.strftime("%Y%m%dT%H%M%S.%fZ")
    records: list[dict] = []
    for rel, matches in introduced:
        source = created_paths[rel]
        attribution = _attribute(source, declared, concurrent)
        target = run_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        record = {
            "job_id": job_id,
            "path": rel,
            "matches": matches,
            "quarantined_to": target.as_posix(),
            "attribution": attribution,
            "concurrent_jobs": list(concurrent or []),
        }
        if attribution in _NOT_BLAMED:
            record["note"] = (
                "concurrent writer, unattributed: introduced while "
                f"{', '.join(concurrent or [])} also ran; not failing {job_id}"
            )
        records.append(record)
    (run_root / "receipt.json").write_text(
        json.dumps({"api": 1, "collisions": records}, indent=2) + "\n",
        encoding="utf-8",
    )
    blamed = [item for item in records if "note" not in item]
    for item in records:
        if "note" in item:
            LOGGER.warning("slug-identity collision quarantined: %s -> %s (%s)",
                           item["path"], item["matches"], item["note"])
    if not blamed:
        return records
    summary = "; ".join(f"{item['path']} -> {item['matches']}" for item in blamed)
    raise SlugIdentityCollision(
        "deterministic writer introduced separator-equivalent page(s); "
        f"quarantined outside wiki: {summary}"
    )
