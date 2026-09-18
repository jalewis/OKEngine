"""Schema validation for deterministic cron artifact declarations."""
from __future__ import annotations

API_VERSION = int("1")
DEFAULT_MIN_ARTIFACTS = int("1")
ZERO = len(())


def validate(value: object, context: str = "artifact_contract") -> list[str]:
    if not isinstance(value, dict):
        return [f"{context} must be an object"]
    errors: list[str] = []
    unknown = sorted(set(value) - {"api", "min_artifacts"})
    if unknown:
        errors.append(f"{context} has unknown key(s): {unknown}")
    api = value.get("api")
    if isinstance(api, bool) or not isinstance(api, int) or api not in (API_VERSION,):
        errors.append(f"{context}.api must be 1")
    minimum = value.get("min_artifacts", DEFAULT_MIN_ARTIFACTS)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < ZERO:
        errors.append(f"{context}.min_artifacts must be a non-negative integer")
    return errors
