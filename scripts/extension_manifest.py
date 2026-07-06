#!/usr/bin/env python3
"""extension_manifest — parse + validate an extension's ``extension.yaml``.

The manifest contract is `docs/design/extension-system.md` §6; this module covers
what discovery (#134) needs to key, reserve, and surface an extension: a parseable
file, a valid `id` (load-bearing — it keys discovery and the `okengine.*`
reservation), and the §6 structural floor. Deeper semantic checks are deferred to
their owners and called out inline:
  - capability grants / scopes  -> #132 (scoped MCP)
  - requires.schema_refs existence against the composed schema -> #133
  - entrypoint script-vs-image + sidecar fields -> #135

`validate_manifest` returns ``(errors, warnings)`` (FAIL vs WARN), mirroring the
severity split in framework_validate.
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a deploy dep; absence is a clear error
    yaml = None

MANIFEST_NAME = "extension.yaml"

# §6: lower-case, no underscores, 3-128 chars, dotted/hyphenated; okengine.* reserved.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$")
OKENGINE_PREFIX = "okengine."
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# §8 kinds. MVP ships only `operation`; the rest are reserved (a kind binds to a
# stable contract that must already exist, so a non-MVP kind is not yet shippable).
KNOWN_KINDS = ("operation", "importer", "reader-extension", "validator")
MVP_KINDS = ("operation",)
# §7 execution models.
KNOWN_TRUST = ("declarative", "in-gateway", "sidecar")
# §12 scope; `vault` is MVP, `workspace` is a reserved seam.
KNOWN_SCOPE = ("vault", "workspace")

_TOP_KEYS = {"id", "kind", "scope", "version", "name", "description",
             "requires", "trust", "capabilities", "schema", "operation",
             "operations", "config", "core", "reader_panels"}
# Built-in reader panel KINDS the reader ships (okengine#160). An extension BINDS one to a page
# type declaratively — it ships no renderer code, so there's no third-party-JS surface in the
# reader (sidesteps the #124 sandbox concern for this path). New kinds are added to the reader.
_PANEL_KINDS = {"fields", "two-axis", "timeline"}
_REQUIRED_TOP = ("id", "kind", "version", "requires", "trust", "capabilities")
# §6: unknown keys under these blocks FAIL (vs unknown descriptive top-level = WARN).
_REQUIRES_KEYS = {"engine", "schema_refs", "extensions"}
_CAP_KEYS = {"read", "write", "network", "secrets", "delivery"}
_OPERATION_KEYS = {"schedule", "entrypoint", "timeout", "prompt", "prompt_file",
                   "toolsets", "tier", "model", "after"}


class ManifestError(Exception):
    """Raised when an extension.yaml cannot be read or parsed at all."""


def load_manifest(ext_dir: Path) -> dict | None:
    """Read + parse ``<ext_dir>/extension.yaml``. Returns the dict, ``None`` if the
    file is absent (not an extension dir), or raises ManifestError on a parse fault."""
    path = Path(ext_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    if yaml is None:
        raise ManifestError("PyYAML not available to parse extension manifests")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ManifestError(f"{path}: unparseable YAML: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a YAML mapping")
    return data


def _validate_operation(op: dict, trust, errors: list[str], ctx: str) -> None:
    """Validate one operation block (shared by the singular `operation:` and each
    `operations:` entry). The entrypoint is a discriminated union tied to `trust`
    (okengine#135): in-gateway -> a script (string or {script: ...}); sidecar ->
    a digest-pinned {image: ...}. ``ctx`` labels the block in error messages."""
    for k in set(op) - _OPERATION_KEYS:
        errors.append(f"unknown key under {ctx}: {k}")
    # agent vs no_agent: a `prompt` (inline) or `prompt_file` (bundled) makes it an
    # agent op (the entrypoint, if any, is then the wake-gate selector); without one
    # the entrypoint script is required.
    prompt = op.get("prompt")
    prompt_file = op.get("prompt_file")
    has_prompt = (isinstance(prompt, str) and bool(prompt.strip())) or \
                 (isinstance(prompt_file, str) and bool(prompt_file.strip()))
    if prompt is not None and not isinstance(prompt, str):
        errors.append(f"{ctx}.prompt must be a string")
    if prompt_file is not None and not isinstance(prompt_file, str):
        errors.append(f"{ctx}.prompt_file must be a string (a path under the extension dir)")
    if isinstance(prompt, str) and prompt.strip() and isinstance(prompt_file, str) and prompt_file.strip():
        errors.append(f"{ctx}: set either 'prompt' or 'prompt_file', not both")
    toolsets = op.get("toolsets")
    if toolsets is not None and not (isinstance(toolsets, list)
                                     and all(isinstance(t, str) for t in toolsets)):
        errors.append(f"{ctx}.toolsets must be a list of toolset names")
    tier = op.get("tier")               # okengine#129 hint: slot into a kickstart stage
    if tier is not None and not (isinstance(tier, str) and tier.strip()):
        errors.append(f"{ctx}.tier must be a non-empty stage name (a kickstart-order hint)")
    after = op.get("after")             # okengine#129: HARD cross-job dependency (job names this
    if after is not None and not (isinstance(after, list)   # op must run after — fail-loud on a
                                  and all(isinstance(a, str) and a.strip() for a in after)):  # cycle/missing target at deploy
        errors.append(f"{ctx}.after must be a list of job names this op must run after")
    model = op.get("model")             # per-operation model override (honored by cron scheduler)
    if model is not None and not (isinstance(model, str) and model.strip()):
        errors.append(f"{ctx}.model must be a non-empty model id (e.g. 'openai/gpt-oss-120b:free')")
    ep = op.get("entrypoint")
    if ep is None:
        if not has_prompt:
            errors.append(f"{ctx}: needs an entrypoint script, or a 'prompt' "
                          "(agent operation)")
        return
    is_image = isinstance(ep, dict) and "image" in ep
    if isinstance(ep, dict) and "script" in ep and "image" in ep:
        errors.append(f"{ctx}.entrypoint: exactly one of script | image")
    if trust == "sidecar":
        if not is_image:
            errors.append(f"trust: sidecar requires {ctx}.entrypoint.image "
                          "(a digest-pinned image ref)")
        else:
            img = ep["image"]
            if not isinstance(img, dict) or not img.get("digest"):
                errors.append(f"{ctx}.entrypoint.image must be a mapping with a "
                              "pinned 'digest' (tag-only is refused for sidecars)")
    elif trust in ("in-gateway", "declarative") and is_image:
        errors.append(f"trust: {trust} uses a script entrypoint, not an image "
                      "(image is for trust: sidecar)")


def validate_manifest(manifest: dict) -> tuple[list[str], list[str]]:
    """Validate a parsed manifest against the §6 structural floor.

    Returns ``(errors, warnings)``. Errors are deploy-breaking (FAIL); warnings are
    deployable-but-worth-fixing (WARN). Semantic checks owned by #132/#133/#135 are
    intentionally NOT done here."""
    errors: list[str] = []
    warnings: list[str] = []

    # Required top-level keys.
    for k in _REQUIRED_TOP:
        if k not in manifest:
            errors.append(f"missing required key: {k}")

    # id — load-bearing for discovery; validated strictly.
    ext_id = manifest.get("id")
    if ext_id is not None:
        if not isinstance(ext_id, str) or not ID_RE.match(ext_id):
            errors.append(
                f"invalid id {ext_id!r}: must match {ID_RE.pattern} "
                "(lower-case, no underscores, 3-128 chars)")

    # kind — must be known; known-but-not-MVP is a WARN (reserved, not yet shippable).
    kind = manifest.get("kind")
    if kind is not None:
        if kind not in KNOWN_KINDS:
            errors.append(f"unknown kind {kind!r}: one of {', '.join(KNOWN_KINDS)}")
        elif kind not in MVP_KINDS:
            warnings.append(f"kind {kind!r} is reserved/not-yet-shippable (MVP kind: operation)")

    # version — semver triple.
    ver = manifest.get("version")
    if ver is not None and not (isinstance(ver, str) and _SEMVER_RE.match(ver)):
        errors.append(f"invalid version {ver!r}: expected semver triple x.y.z")

    # trust — known execution model.
    trust = manifest.get("trust")
    if trust is not None and trust not in KNOWN_TRUST:
        errors.append(f"invalid trust {trust!r}: one of {', '.join(KNOWN_TRUST)}")

    # scope — defaults to vault; workspace is a reserved seam (§12), accepted with a WARN.
    scope = manifest.get("scope", "vault")
    if scope not in KNOWN_SCOPE:
        errors.append(f"invalid scope {scope!r}: one of {', '.join(KNOWN_SCOPE)}")
    elif scope == "workspace":
        warnings.append("scope: workspace is a reserved seam (§12), not MVP")

    # requires.engine is required; unknown sub-keys FAIL.
    requires = manifest.get("requires")
    if requires is not None:
        if not isinstance(requires, dict):
            errors.append("requires: must be a mapping")
        else:
            if "engine" not in requires:
                errors.append("requires.engine is required (e.g. \">=0.3.0\")")
            elif not isinstance(requires["engine"], str):
                errors.append("requires.engine must be a version-spec string")
            for k in set(requires) - _REQUIRES_KEYS:
                errors.append(f"unknown key under requires: {k}")

    # capabilities present (required); unknown sub-keys FAIL. Grant semantics -> #132.
    caps = manifest.get("capabilities")
    if caps is not None:
        if not isinstance(caps, dict):
            errors.append("capabilities: must be a mapping")
        else:
            for k in set(caps) - _CAP_KEYS:
                errors.append(f"unknown key under capabilities: {k}")

    # core: default-ON marker (okengine#142). Only honored for the engine tier (a
    # pack/operator extension can't force itself on) — enforced at resolve time.
    core = manifest.get("core")
    if core is not None and not isinstance(core, bool):
        errors.append("core must be a boolean (true marks an engine extension default-on)")

    # operation(s) — optional here; unknown sub-keys FAIL. An extension declares
    # EITHER a singular `operation:` block OR a plural `operations:` map (a
    # multi-operation extension — each entry a job named `<id>:<op>`), not both.
    operation = manifest.get("operation")
    operations = manifest.get("operations")
    if isinstance(operation, dict) and isinstance(operations, dict):
        errors.append("declare either 'operation' or 'operations', not both")
    if isinstance(operation, dict):
        _validate_operation(operation, trust, errors, "operation")
    if isinstance(operations, dict):
        if not operations:
            errors.append("'operations' map is empty")
        for name in sorted(operations):
            if not isinstance(name, str) or not name.strip():
                errors.append(f"operations key {name!r} must be a non-empty string")
                continue
            op = operations[name]
            if not isinstance(op, dict):
                errors.append(f"operations[{name!r}] must be a block")
                continue
            _validate_operation(op, trust, errors, f"operations.{name}")

    # reader_panels (okengine#160): declarative type -> built-in-kind bindings. The reader renders
    # the kind from the page's frontmatter; the extension ships NO renderer code. Unknown sub-keys
    # are allowed (kind-specific, e.g. x/y for two-axis) — only the contract is enforced here.
    rp = manifest.get("reader_panels")
    if rp is not None:
        if not isinstance(rp, list):
            errors.append("reader_panels must be a list of {type, kind, ...} bindings")
        else:
            for i, b in enumerate(rp):
                ctx = f"reader_panels[{i}]"
                if not isinstance(b, dict):
                    errors.append(f"{ctx} must be a mapping")
                    continue
                if not isinstance(b.get("type"), str) or not b["type"].strip():
                    errors.append(f"{ctx}: 'type' (page type to bind) is required")
                if b.get("kind") not in _PANEL_KINDS:
                    errors.append(f"{ctx}: 'kind' must be one of {sorted(_PANEL_KINDS)}")
                if "fields" in b and not (isinstance(b["fields"], list)
                                          and all(isinstance(f, str) for f in b["fields"])):
                    errors.append(f"{ctx}: 'fields' must be a list of frontmatter field names")

    # Unknown descriptive top-level keys -> WARN (§6).
    for k in set(manifest) - _TOP_KEYS:
        warnings.append(f"unknown top-level key (ignored): {k}")

    return errors, warnings


def is_reserved_id(ext_id: str) -> bool:
    """True if the id is in the first-party `okengine.*` namespace (tier-1 only)."""
    return isinstance(ext_id, str) and ext_id.startswith(OKENGINE_PREFIX)
