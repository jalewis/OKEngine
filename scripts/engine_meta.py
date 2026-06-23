"""Single source of truth for the engine's own version pins.

Both `framework init` (what version a new pack pins) and `framework validate`
(what version a pack is required to pin) read the engine release + Hermes tag from
`engine-manifest.yaml` here, so the version a scaffold is stamped with and the
version the validator enforces can never drift apart — they're the same value,
from the same file, in the engine checkout you're running.
"""
from __future__ import annotations

import re
from pathlib import Path

_SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a runtime dep, guarded for safety
    yaml = None

# scripts/engine_meta.py -> repo root / engine-manifest.yaml
MANIFEST = Path(__file__).resolve().parent.parent / "engine-manifest.yaml"


def _load() -> dict:
    if yaml is None or not MANIFEST.is_file():
        return {}
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def engine_release() -> str | None:
    """The engine release this checkout is (e.g. 'v0.2.0'), or None if unreadable."""
    v = str(_load().get("engine_release") or "").strip()
    return v or None


def hermes_pin() -> str | None:
    """The pinned Hermes tag this engine targets (e.g. 'v2026.6.5'), or None."""
    rt = _load().get("runtime")
    if not isinstance(rt, dict):
        return None
    v = str(rt.get("pinned_tag") or "").strip()
    return v or None


def _semver(v: str | None) -> tuple[int, int, int] | None:
    """Parse the first 'vX.Y.Z' (also matches 'engine-vX.Y.Z') -> (X, Y, Z), or None."""
    m = _SEMVER_RE.search(v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def satisfies_pin(pin: str | None, engine: str | None) -> bool | None:
    """Does the running `engine` release satisfy a pack pinned to `pin`?

    Caret/compatibility semantics, so a PATCH release never breaks a pin (okengine#104):
      - same major;
      - for 0.x the minor must also match (a 0.x minor bump is the breaking unit);
      - and the engine is the same-or-newer release within that series.
    So a pack pinned to v0.3.0 is satisfied by engine v0.3.2 (compatible) but NOT by
    v0.4.0 (new generation) or v0.2.x (older generation). Returns None if either
    version is unparseable (caller falls back to a format-only check).
    """
    p, e = _semver(pin), _semver(engine)
    if not p or not e:
        return None
    if p[0] != e[0]:
        return False
    if p[0] == 0 and p[1] != e[1]:
        return False
    return e >= p
