"""Single source of truth for the engine's own version pins.

Both `framework init` (what version a new pack pins) and `framework validate`
(what version a pack is required to pin) read the engine release + Hermes tag from
`engine-manifest.yaml` here, so the version a scaffold is stamped with and the
version the validator enforces can never drift apart — they're the same value,
from the same file, in the engine checkout you're running.
"""
from __future__ import annotations

from pathlib import Path

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
