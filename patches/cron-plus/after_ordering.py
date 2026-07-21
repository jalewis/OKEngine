"""Runtime policy for cron-plus ``after:`` dependency freshness.

Kept as a small overlay so the behavior is directly unit-testable while the
carried jobs.py patch only wires it into cron-plus's atomic claim boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _completed_token(job: dict) -> str | None:
    """Return a token only for a successfully completed run.

    The ``last_run_at`` fallback upgrades existing live state written before
    ``last_completed_at`` existed.
    """
    if job.get("last_run_success") is not True:
        return None
    return job.get("last_completed_at") or job.get("last_run_at")


def _token_is_newer(candidate: str, consumed: str) -> bool:
    """Compare ISO completion tokens, failing closed on malformed state."""
    try:
        new = datetime.fromisoformat(candidate)
        old = datetime.fromisoformat(consumed)
        if new.tzinfo is None:
            new = new.replace(tzinfo=timezone.utc)
        if old.tzinfo is None:
            old = old.replace(tzinfo=timezone.utc)
        return new > old
    except (TypeError, ValueError):
        return False


def after_ready(job: dict, by_name: dict[str, dict]) -> tuple[bool, dict[str, str], str | None]:
    """Evaluate a downstream job's all-of successful-freshness gate.

    Returns the exact upstream completion tokens being claimed. Runtime shape
    errors fail closed; OKEngine's deploy-time graph validation normally keeps
    malformed or unresolved dependencies from reaching this boundary.
    """
    deps = job.get("after")
    if not deps:
        return True, {}, None
    if not isinstance(deps, list) or any(not isinstance(name, str) or not name for name in deps):
        return False, {}, "malformed after: dependency list"

    consumed = job.get("after_consumed")
    if consumed is None:
        consumed = {}
    elif not isinstance(consumed, dict):
        return False, {}, "malformed after_consumed runtime state"

    claimed: dict[str, str] = {}
    for name in deps:
        upstream = by_name.get(name)
        if upstream is None:
            return False, {}, f"dependency {name!r} is absent"
        token = _completed_token(upstream)
        if not token:
            return False, {}, f"dependency {name!r} has no successful completion"
        prior = consumed.get(name)
        if prior is not None and not _token_is_newer(token, prior):
            return False, {}, f"dependency {name!r} has no fresh completion"
        claimed[name] = token
    return True, claimed, None


def begin_claim(job: dict, after_claim: dict[str, str]) -> None:
    """Mark a claimed run in progress and snapshot its dependency inputs."""
    # Do not pair a newly stamped last_run_at with the previous run's success.
    job["last_run_success"] = None
    if after_claim:
        job["after_claim"] = after_claim


def record_outcome(job: dict, success: bool, completed_at: str) -> None:
    """Commit consumed tokens only when the downstream completes successfully."""
    job["last_run_success"] = success
    claim = job.pop("after_claim", None)
    if success:
        job["last_completed_at"] = completed_at
        if claim:
            job["after_consumed"] = claim
