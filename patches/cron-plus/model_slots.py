"""Per-model inference slots for cron-plus agent runners."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
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


# The run's own hard ceiling bounds this wait, and it is not a preference -- it is arithmetic.
# cron-plus arms the deadline BEFORE entering the slot wait:
#
#     with run_timeout.run_deadline(job):     # SIGALRM armed here
#         with model_slots.model_slot(job):   # ...then we queue for a slot
#
# so a wait limit at or above that ceiling can NEVER expire. SIGALRM fires first and raises the
# generic "cron-plus run exceeded hard timeout of Ns", which discards the identity/limit
# diagnostic below and reports a capacity shortfall as a lane whose work is too big. The shipped
# defaults did exactly that -- wait 1800s against a 1200s ceiling -- making that diagnostic
# unreachable dead code. Measured consequence on one deployment: 1,558 runs across 42 lanes
# recorded as hard-timeout failures having executed ZERO tool-call turns, with a near-uniform
# 19-20% kill rate across unrelated scripts -- the signature of a shared endpoint, not of
# per-lane work. Six weeks of that read as "these lanes are too slow".
#
# A FRACTION, not a fixed margin: acquiring a slot with no budget left to use it produces the
# same empty run as never acquiring one, so the wait has to end early enough to leave working time.
SLOT_WAIT_CEILING_FRACTION = 0.5

_run_timeout_module = None
_run_timeout_looked_up = False


def _run_timeout_mod():
    """The sibling run_timeout module, or None where it cannot be resolved.

    Resolved BY PATH rather than by plain import so the ceiling is honoured in every layout this
    file is loaded in -- flat sys.path inside the deployed plugin, and importlib-from-disk in the
    engine's tests. An import-only lookup silently returns None under the test layout, which would
    leave the bound below untested and therefore unenforced.
    """
    global _run_timeout_module, _run_timeout_looked_up
    if _run_timeout_looked_up:
        return _run_timeout_module
    _run_timeout_looked_up = True
    try:
        import run_timeout  # type: ignore[import]
        _run_timeout_module = run_timeout
        return _run_timeout_module
    except ImportError:
        pass
    path = Path(__file__).with_name("run_timeout.py")
    if path.is_file():
        try:
            spec = importlib.util.spec_from_file_location("_okengine_run_timeout", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _run_timeout_module = module
        except Exception:            # a broken sibling must not take the slot path down
            _run_timeout_module = None
    return _run_timeout_module


def slot_wait_ceiling(job: dict) -> float | None:
    """Longest wait that still leaves the run time to USE the slot, or None when unknown.

    None means "no ceiling discoverable", never "a ceiling of zero" -- an unknown bound that
    silenced every wait would be a worse failure than the one this exists to prevent.
    """
    module = _run_timeout_mod()
    if module is None:
        return None
    try:
        total = float(module.run_timeout_seconds(job))
    except (AttributeError, TypeError, ValueError):
        return None
    return max(0.0, total * SLOT_WAIT_CEILING_FRACTION)


def model_slot_wait_seconds(job: dict) -> float:
    """Maximum queue delay before the run fails visibly instead of piling up."""
    raw = job.get("model_slot_wait_seconds",
                  os.environ.get("OKENGINE_MODEL_SLOT_WAIT_SECONDS", "1800"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 1800.0
    value = max(0.0, value)
    ceiling = slot_wait_ceiling(job)
    if ceiling is not None and value > ceiling:
        logger.info(
            "slot wait %.1fs exceeds this run's usable ceiling; capping to %.1fs "
            "so an unavailable slot reports itself instead of being killed as a timeout",
            value, ceiling)
        return ceiling
    return value


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
    wait_limit = model_slot_wait_seconds(job)
    started = time.monotonic()
    logger.info("waiting for model slot: identity=%s limit=%d timeout=%.1fs",
                identity, limit, wait_limit)
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
            waited = time.monotonic() - started
            if waited >= wait_limit:
                raise TimeoutError(
                    f"model slot unavailable after {waited:.1f}s "
                    f"(identity={identity}, limit={limit})")
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
