"""Validate deterministic lane artifact declarations from structured stdout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PREFIX = "OKENGINE_ARTIFACT:"
OPERATIONS = {"append", "create", "replace", "update", "verify"}
API_VERSION = int("1")
DEFAULT_MIN_ARTIFACTS = int("1")
ZERO = len(())


class ArtifactError(ValueError):
    """A deterministic lane did not satisfy its declared artifact contract."""


def validate_contract(value: object, context: str = "artifact_contract") -> list[str]:
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


def _roots(home: Path) -> list[Path]:
    values = [
        Path(os.environ.get("WIKI_PATH") or "/opt/vault"),
        Path(home),
        Path(home).parent,
    ]
    if os.environ.get("HERMES_HOME"):
        values.append(Path(os.environ["HERMES_HOME"]))
    roots: list[Path] = []
    for value in values:
        resolved = value.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    return f"sha256:{digest.hexdigest()}"


def _validate_artifact(raw: object, roots: list[Path]) -> dict:
    if not isinstance(raw, dict):
        raise ArtifactError("artifact declaration must be an object")
    unknown = sorted(set(raw) - {"path", "operation", "count", "sha256"})
    if unknown:
        raise ArtifactError(f"artifact declaration has unknown key(s): {unknown}")
    path_text = raw.get("path")
    if not isinstance(path_text, str) or not path_text.strip():
        raise ArtifactError("artifact path must be a non-empty string")
    operation = raw.get("operation")
    if operation not in OPERATIONS:
        raise ArtifactError(f"artifact operation must be one of {sorted(OPERATIONS)}")
    candidate = Path(path_text)
    relative = not candidate.is_absolute()
    vault_root, *_ = roots
    if relative:
        candidate = vault_root / candidate
    resolved = candidate.resolve()
    if (relative and not _inside(resolved, [vault_root])) or not _inside(resolved, roots):
        raise ArtifactError(f"artifact path escapes mounted deployment scope: {path_text}")
    if not resolved.is_file():
        raise ArtifactError(f"artifact is missing or is not a file: {path_text}")

    record: dict[str, object] = {
        "path": resolved.as_posix(),
        "operation": operation,
        "sha256": _sha256(resolved),
    }
    count = raw.get("count")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or count < ZERO:
            raise ArtifactError("artifact count must be a non-negative integer")
        record["count"] = count
    expected = raw.get("sha256")
    if expected is not None:
        if not isinstance(expected, str) or not expected.startswith("sha256:"):
            raise ArtifactError("artifact sha256 must use the sha256:<hex> form")
        if expected != record["sha256"]:
            raise ArtifactError(f"artifact sha256 does not match readback: {path_text}")
    return record


def collect(job: dict, response: str, home: Path) -> tuple[str, list[dict]]:
    """Strip and validate artifact control lines for an opted-in job."""
    contract = job.get("artifact_contract")
    if contract is None:
        return response, []
    if job.get("no_agent") is not True:
        raise ArtifactError("artifact_contract is only valid for no_agent jobs")
    errors = validate_contract(contract)
    if errors:
        raise ArtifactError("; ".join(errors))

    declarations: list[object] = []
    visible: list[str] = []
    for line in (response or "").splitlines():
        if not line.startswith(PREFIX):
            visible.append(line)
            continue
        try:
            declarations.append(json.loads(line[len(PREFIX):].strip()))
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"malformed artifact declaration: {exc.msg}") from exc

    minimum = int(contract.get("min_artifacts", DEFAULT_MIN_ARTIFACTS))
    if len(declarations) < minimum:
        raise ArtifactError(
            f"artifact contract requires at least {minimum} declaration(s); "
            f"received {len(declarations)}")
    roots = _roots(Path(home))
    return "\n".join(visible), [_validate_artifact(item, roots) for item in declarations]
