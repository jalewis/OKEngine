#!/usr/bin/env python3
"""Append-safe collection telemetry and deterministic source-state projection.

Connectors record attempts here even when they fail or yield zero records.  The
ledger deliberately stores identifiers, counters and opaque checkpoint digests;
it must never contain credentials, response bodies, or private query text.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
COUNT_FIELDS = ("fetched", "extracted", "accepted", "rejected", "deduped", "dead_letter")
OUTCOMES = {"success", "partial", "failure"}


def _utc(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value=None) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def source_id(connector_id: str, native: str, label: str = "source") -> str:
    """Stable non-secret source identity; URLs/query text never enter the ledger."""
    token = hashlib.sha256(f"{connector_id}\0{native}".encode()).hexdigest()[:12]
    safe = "-".join("".join(c if c.isalnum() else " " for c in label.lower()).split())[:48]
    return f"{safe or 'source'}-{token}"


def checkpoint_digest(value) -> str | None:
    """Opaque checkpoint fingerprint suitable for operational telemetry."""
    if value in (None, "", {}, []):
        return None
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _locked(root: Path, name: str):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / name).open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def register_sources(root: Path, sources: list[dict], *, connector_id: str | None = None) -> None:
    """Reconcile declarations for one connector without touching other connectors."""
    root = Path(root)
    with _locked(root, ".sources.lock"):
        path = root / "sources.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {"schema_version": SCHEMA_VERSION, "sources": {}}
        rows = current.get("sources") if isinstance(current, dict) else {}
        rows = rows if isinstance(rows, dict) else {}
        now = _iso()
        owned = {str(item.get("source_id") or "").strip() for item in sources}
        if connector_id:
            rows = {sid: row for sid, row in rows.items()
                    if row.get("connector_id") != connector_id or sid in owned}
        for item in sources:
            sid = str(item.get("source_id") or "").strip()
            connector = str(item.get("connector_id") or "").strip()
            if not sid or not connector:
                raise ValueError("configured source requires source_id and connector_id")
            kind = item.get("source_kind") if item.get("source_kind") in {
                "primary", "secondary", "unknown"
            } else "unknown"
            independent = item.get("independent_origin")
            if independent not in (True, False):
                independent = None
            rows[sid] = {
                "source_id": sid,
                "connector_id": connector,
                "label": str(item.get("label") or sid),
                "source_kind": kind,
                "independent_origin": independent,
                "configured_at": str((rows.get(sid) or {}).get("configured_at") or now),
                "observed_configured_at": now,
            }
        doc = {"schema_version": SCHEMA_VERSION, "sources": dict(sorted(rows.items()))}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def append_attempt(root: Path, record: dict) -> dict:
    """Validate and append one complete attempt as a single locked NDJSON write."""
    connector = str(record.get("connector_id") or "").strip()
    sid = str(record.get("source_id") or "").strip()
    if not connector or not sid:
        raise ValueError("attempt requires connector_id and source_id")
    started = _iso(record.get("started_at"))
    finished = _iso(record.get("finished_at"))
    outcome = str(record.get("outcome") or "failure")
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid collection outcome: {outcome}")
    counts = {}
    for field in COUNT_FIELDS:
        value = int(record.get(field, 0))
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
        counts[field] = value
    clean = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": hashlib.sha256(f"{connector}\0{sid}\0{started}".encode()).hexdigest()[:24],
        "connector_id": connector,
        "source_id": sid,
        "started_at": started,
        "finished_at": finished,
        "outcome": outcome,
        **counts,
        "latency_ms": max(0, int(record.get("latency_ms", 0))),
        "error_category": str(record.get("error_category") or "") or None,
        "checkpoint_in": str(record.get("checkpoint_in") or "") or None,
        "checkpoint_out": str(record.get("checkpoint_out") or "") or None,
        "newest_published_at": str(record.get("newest_published_at") or "") or None,
        "publication_to_ingest_ms": (
            max(0, int(record["publication_to_ingest_ms"]))
            if record.get("publication_to_ingest_ms") is not None else None
        ),
    }
    root = Path(root)
    month = _utc(finished).strftime("%Y-%m")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"attempts-{month}.ndjson"
    line = json.dumps(clean, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return clean


def load_sources(root: Path) -> list[dict]:
    try:
        doc = json.loads((Path(root) / "sources.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = doc.get("sources") if isinstance(doc, dict) else {}
    return list(rows.values()) if isinstance(rows, dict) else []


def load_attempts(root: Path, *, now=None, retention_days: int = 90) -> list[dict]:
    cutoff = _utc(now) - timedelta(days=retention_days)
    out = []
    # glob-ok: collection ledger is an intentionally flat monthly segment directory
    for path in sorted(Path(root).glob("attempts-????-??.ndjson")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                if _utc(row.get("finished_at")) >= cutoff:
                    out.append(row)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return sorted(out, key=lambda row: (str(row.get("finished_at")), str(row.get("attempt_id"))))


def prune(root: Path, *, now=None, retention_days: int = 90) -> list[Path]:
    """Remove whole monthly segments whose final possible day is outside retention."""
    cutoff_month = (_utc(now) - timedelta(days=retention_days)).strftime("%Y-%m")
    removed = []
    # glob-ok: collection ledger is an intentionally flat monthly segment directory
    for path in Path(root).glob("attempts-????-??.ndjson"):
        if path.stem.rsplit("-", 2)[-2] + "-" + path.stem.rsplit("-", 1)[-1] < cutoff_month:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def project_current(sources: list[dict], attempts: list[dict], *, now=None,
                    stale_after_hours: float = 26.0) -> list[dict]:
    now_dt = _utc(now)
    by_source: dict[str, list[dict]] = {}
    for row in attempts:
        by_source.setdefault(str(row.get("source_id") or ""), []).append(row)
    out = []
    for source in sorted(sources, key=lambda row: (str(row.get("label", "")).casefold(),
                                                    str(row.get("source_id", "")))):
        history = sorted(by_source.get(str(source.get("source_id")), []),
                         key=lambda row: str(row.get("finished_at") or ""))
        latest = history[-1] if history else None
        successes = [row for row in history if row.get("outcome") == "success"]
        last_success = successes[-1] if successes else None
        failures = 0
        for row in reversed(history):
            if row.get("outcome") == "failure":
                failures += 1
            else:
                break
        age_hours = None
        if latest:
            age_hours = max(0.0, (now_dt - _utc(latest.get("finished_at"))).total_seconds() / 3600)
        if latest is None:
            status = "unknown"
        elif age_hours is None or age_hours > stale_after_hours:
            status = "stale"
        elif failures:
            status = "failing"
        elif latest.get("outcome") == "partial":
            status = "partial"
        else:
            status = "healthy"
        out.append({
            **source,
            "status": status,
            "last_attempt": latest.get("finished_at") if latest else None,
            "last_success": last_success.get("finished_at") if last_success else None,
            "consecutive_failures": failures if latest else None,
            "age_hours": age_hours,
            "fetched": latest.get("fetched") if latest else None,
            "accepted": latest.get("accepted") if latest else None,
            "dead_letter": latest.get("dead_letter") if latest else None,
            "error_category": latest.get("error_category") if latest else None,
            "publication_to_ingest_ms": latest.get("publication_to_ingest_ms") if latest else None,
        })
    return out
