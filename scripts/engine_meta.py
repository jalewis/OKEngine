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
# A release key that orders prereleases BEFORE their final (okengine#1032 [25], #1042 [5]/[27]).
# _semver keeps only X.Y.Z, so v0.15.12-rc1 and v0.15.12 compare equal. PEP 440 stage order
# (dev < a < b < rc) with the semver/PEP 440 spellings the tags use ("-rc1", "rc.1", "-beta.2",
# "a1"); a suffix that is not a prerelease stage keeps today's meaning (the final). Every ORDERING
# comparison of two versions uses release_key; _semver stays for parse-ability and X.Y.Z access.
_RELEASE_RE = re.compile(
    r"v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(dev|alpha|beta|pre|rc|a|b|c)[.-]?(\d*)(?![0-9A-Za-z]))?",
    re.IGNORECASE)
_PRERELEASE_STAGE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "pre": 3, "rc": 3}
_FINAL_STAGE = 9

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
    """The pinned Hermes tag this engine targets (e.g. 'v2026.6.19'), or None."""
    rt = _load().get("runtime")
    if not isinstance(rt, dict):
        return None
    v = str(rt.get("pinned_tag") or "").strip()
    return v or None


def _semver(v: str | None) -> tuple[int, int, int] | None:
    """Parse the first 'vX.Y.Z' (also matches 'engine-vX.Y.Z') -> (X, Y, Z), or None."""
    m = _SEMVER_RE.search(v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def release_key(version: str | None) -> tuple[int, int, int, int, int] | None:
    """(X, Y, Z, stage, n) for the first release in `version`, or None when there is none."""
    m = _RELEASE_RE.search(version or "")
    if not m:
        return None
    base = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    stage = m.group(4)
    if stage is None:
        return (*base, _FINAL_STAGE, 0)
    return (*base, _PRERELEASE_STAGE[stage.lower()], int(m.group(5) or 0))


def satisfies_pin(pin: str | None, engine: str | None) -> bool | None:
    """Does the running `engine` release satisfy a pack pinned to `pin`?

    Caret/compatibility semantics, so a PATCH release never breaks a pin (okengine#104):
      - same major;
      - for 0.x the minor must also match (a 0.x minor bump is the breaking unit);
      - and the engine is the same-or-newer release within that series.
    So a pack pinned to v0.3.0 is satisfied by engine v0.3.2 (compatible) but NOT by
    v0.4.0 (new generation) or v0.2.x (older generation). A prerelease orders before its
    final, so an rc engine does NOT satisfy a pin on the final it precedes (okengine#1042 [5]).
    Returns None if either version is unparseable (caller falls back to a format-only check).
    """
    p, e = release_key(pin), release_key(engine)
    if not p or not e:
        return None
    if p[0] != e[0]:
        return False
    if p[0] == 0 and p[1] != e[1]:
        return False
    return e >= p
