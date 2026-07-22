"""Per-model inference slots for cron-plus agent runners."""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import yaml

logger = logging.getLogger("cron-plus.model-slots")


def _runtime_config() -> dict:
    path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def model_identity(job: dict) -> str | None:
    """Resolve the concrete inference endpoint shared by this agent job."""
    if job.get("no_agent") is True:
        return None
    configured = _runtime_config().get("model") or {}
    configured = configured if isinstance(configured, dict) else {}
    provider = str(job.get("provider") or configured.get("provider") or "default")
    base_url = str(job.get("base_url") or configured.get("base_url") or "default")
    model = str(job.get("model") or configured.get("default") or "default")
    return f"{provider}|{base_url}|{model}"


def model_concurrency(job: dict) -> int:
    """Return the explicit per-model slot count, conservatively defaulting to one."""
    raw = job.get("model_concurrency", 1)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


@contextmanager
def model_slot(job: dict):
    """Serialize agent jobs sharing one provider/endpoint/model identity."""
    identity = model_identity(job)
    if identity is None:
        with nullcontext():
            yield
        return
    root = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "cron-plus" / "model-slots"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    limit = model_concurrency(job)
    started = time.monotonic()
    logger.info("waiting for model slot: identity=%s limit=%d", identity, limit)
    handle = None
    slot = None
    while handle is None:
        for index in range(limit):
            path = root / f"{digest}.{index}.lock"
            candidate = path.open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                candidate.close()
                continue
            handle, slot = candidate, index + 1
            break
        if handle is None:
            time.sleep(0.1)
    waited = time.monotonic() - started
    logger.info("acquired model slot: identity=%s slot=%d/%d waited=%.1fs",
                identity, slot, limit, waited)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        logger.info("released model slot: identity=%s slot=%d/%d", identity, slot, limit)
