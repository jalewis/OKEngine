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

import json
import re
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a deploy dep; absence is a clear error
    yaml = None

MANIFEST_NAME = "extension.yaml"

# §6: lower-case, no underscores, 3-128 chars, dotted/hyphenated; okengine.* reserved.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]\Z")
OKENGINE_PREFIX = "okengine."
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+\Z")

# §8 kinds. MVP ships only `operation`; the rest are reserved (a kind binds to a
# stable contract that must already exist, so a non-MVP kind is not yet shippable).
KNOWN_KINDS = ("operation", "importer", "reader-extension", "validator")


def _identity_edit_errors(write_policy: dict) -> list[str]:
    """okengine#1028: an extension's `identity_edit` grant follows the cron-lane rule exactly
    (scripts/cron/output_contract.py): the same fields, and never on an extension that can create
    pages. The write path enforces the creating-lane rule too; this is the earliest gate."""
    import importlib.util

    path = Path(__file__).resolve().parent / "cron" / "output_contract.py"
    spec = importlib.util.spec_from_file_location("okengine_output_contract_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._identity_edit_errors(write_policy, "capabilities.write_policy")
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
# The reader_panels BINDING allowlist must match what the reader renders FROM A TYPE BINDING.
# app.py `_panel_for` builds a type-bound panel only for `kind: fields` (pulling declared frontmatter
# fields); `two-axis` needs per-page nodes/coords that exist ONLY when a page SELF-DECLARES `panel:`
# (the viz generated-page path) — a type binding can't synthesize them, so a `kind: two-axis` binding
# validated GREEN yet rendered `panel: null` on every bound page (same failure mode as `timeline`,
# #26, and now two-axis — invariant-audit #351). `timeline` has no renderer at all. Bindable = fields
# ONLY; two-axis stays valid for self-declared page panels (not governed by this manifest check).
# Add a kind here only when the type-binding path in _panel_for renders it.
_BINDABLE_PANEL_KINDS = {"fields"}
_REQUIRED_TOP = ("id", "kind", "version", "requires", "trust", "capabilities")
# §6: unknown keys under these blocks FAIL (vs unknown descriptive top-level = WARN).
_REQUIRES_KEYS = {"engine", "schema_refs", "extensions"}
_CAP_KEYS = {"read", "write", "write_policy", "network", "secrets", "delivery"}
_OPERATION_KEYS = {"schedule", "entrypoint", "timeout", "prompt", "prompt_file",
                   "toolsets", "tier", "model", "max_iterations", "after", "cost_bearing",
                   "output_contract", "adversarial_fixtures"}
# cost_bearing: a no_agent op that STILL spends model budget (a deterministic script calling
# llm_lib directly, e.g. concept-enrich / scope-classify). budget_guard pauses it when over budget —
# without the marker it looked "free" and burned paid tokens unpausably (invariant-audit #36).


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
    declarative = declarative_operation_error(op, trust, ctx)   # okengine#1004, before any return
    if declarative:
        errors.append(declarative)
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
    fixtures = op.get("adversarial_fixtures")
    if fixtures is not None and not (isinstance(fixtures, list) and fixtures and
                                     all(isinstance(item, str) and item.strip()
                                         for item in fixtures)):
        errors.append(f"{ctx}.adversarial_fixtures must be a non-empty list of paths")
    toolsets = op.get("toolsets")
    if toolsets is not None and not (isinstance(toolsets, list)
                                     and all(isinstance(t, str) for t in toolsets)):
        errors.append(f"{ctx}.toolsets must be a list of toolset names")
    elif toolsets is not None and "okengine-write" in toolsets and "file" in toolsets:
        errors.append(
            f"{ctx}.toolsets cannot combine okengine-write with the mutable file toolset; "
            "raw file writes bypass policy, review, output-contract, and receipt enforcement")
    max_iterations = op.get("max_iterations")
    if has_prompt and max_iterations is None:
        errors.append(f"{ctx}.max_iterations is required for every agent operation")
    elif max_iterations is not None and (
            not isinstance(max_iterations, int)
            or isinstance(max_iterations, bool)
            or max_iterations <= 0):
        errors.append(f"{ctx}.max_iterations must be a positive integer")
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


def script_entrypoint(op) -> str | None:
    """The gateway script an operation block names, or None (no entrypoint, an image, a malformed
    value). A string entrypoint or ``{script: ...}`` is a script the deploy stages and cron runs."""
    ep = op.get("entrypoint") if isinstance(op, dict) else None
    if isinstance(ep, dict):
        if "image" in ep:
            return None
        ep = ep.get("script")
    return ep if isinstance(ep, str) and ep.strip() else None


def declarative_operation_error(op, trust, ctx: str) -> str | None:
    """okengine#1004: `trust: declarative` runs no extension-owned code — "schema + reader nav +
    config only" (extension-system.md §7) — so ANY operation under it is a label that lies about
    what runs: a script is staged and executed, and an agent lane runs a model in the gateway with
    whatever toolsets it lists (terminal, file, ...). One rule for the manifest `operation` /
    `operations` blocks and the `crons/*.cron.json` drop-ins; operation-bearing extensions are
    `in-gateway` (operator tier requires acceptance) or `sidecar`."""
    if trust != "declarative" or not isinstance(op, dict):
        return None
    script = script_entrypoint(op)
    what = (f"{ctx}.entrypoint names the script {script!r}" if script
            else f"{ctx} declares an operation (an agent or script lane that runs in the gateway)")
    return (f"trust: declarative runs no code, but {what}; declare trust: in-gateway (operator "
            "tier requires --allow-untrusted) or trust: sidecar")


# First-cut name (okengine#1004 round 1), kept so an older caller still resolves it.
declarative_script_error = declarative_operation_error


def load_dropin_ops(ext_dir) -> tuple[list[tuple[str, dict]], str | None]:
    """`crons/*.cron.json` drop-in op files -> ([(op_name, block)], error) (#63 P1, the drop-in model).

    Each file is ONE operation block (same shape as an `operations:` entry — schedule /
    entrypoint / prompt_file / toolsets); the op name is the filename stem minus `.cron.json`
    (so `crons/grade.cron.json` -> op `grade` -> job `<id>:grade`). Collected forward-only;
    an extension drops a file in rather than editing a central manifest block."""
    if not ext_dir:
        return [], None
    d = Path(ext_dir) / "crons"
    if not d.is_dir():
        return [], None
    out: list[tuple[str, dict]] = []
    for f in sorted(d.glob("*.cron.json")):  # glob-ok: crons/ is a flat per-extension dir, not a sharded namespace
        op_name = f.name[: -len(".cron.json")]
        if not op_name:
            return [], f"invalid cron drop-in filename: crons/{f.name}"
        try:
            block = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                                  # noqa: BLE001
            return [], f"crons/{f.name}: invalid JSON ({e})"
        if not isinstance(block, dict):
            return [], f"crons/{f.name}: must be a JSON object (an operation block)"
        out.append((op_name, block))
    return out, None


def validate_dropins(manifest: dict, ext_dir) -> list[str]:
    """Errors for the `crons/*.cron.json` drop-ins of an extension (okengine#1004).

    Deliberately narrow: only the trust contract. The drop-ins' other fields are checked where the
    job is synthesized, and the full manifest-block validation would newly reject keys (`expect`)
    the drop-in form carries. An unreadable drop-in is reported by the composer, except under
    `trust: declarative`, where shipping a drop-in at all is the violation."""
    ops, err = load_dropin_ops(ext_dir)
    trust = manifest.get("trust") if isinstance(manifest, dict) else None
    if err and trust == "declarative":
        # an unreadable drop-in can hide a readable one (the reader stops at the first fault); under
        # declarative, ANY drop-in is an operation, so do not let the fault mask the rest
        return [f"trust: declarative runs no code, but it ships crons/ drop-in(s) ({err}); declare "
                "trust: in-gateway (operator tier requires --allow-untrusted) or trust: sidecar"]
    errors = []
    for name, op in ops:
        error = declarative_operation_error(op, trust, f"crons/{name}.cron.json")
        if error:
            errors.append(error)
    return errors


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
            wp = caps.get("write_policy")
            if wp is not None:
                if not isinstance(wp, dict):
                    errors.append("capabilities.write_policy must be a mapping")
                else:
                    allowed = {"rule_id", "operations", "paths", "types", "update_fields",
                               "protected_fields", "body", "identity_edit"}
                    unknown = sorted(set(wp) - allowed)
                    if unknown:
                        errors.append(f"capabilities.write_policy has unknown keys: {unknown}")
                    for key in ("operations", "paths", "types", "update_fields", "protected_fields"):
                        if key in wp and not (isinstance(wp[key], list) and
                                              all(isinstance(v, str) and v for v in wp[key])):
                            errors.append(f"capabilities.write_policy.{key} must be a string list")
                    if not isinstance(wp.get("rule_id"), str) or not wp.get("rule_id", "").strip():
                        errors.append("capabilities.write_policy.rule_id is required")
                    known_operations = {"create", "update", "patch", "append", "tombstone",
                                        "converge", "flag", "review", "import"}
                    unknown_operations = sorted(set(wp.get("operations") or []) - known_operations)
                    if unknown_operations:
                        errors.append("capabilities.write_policy.operations has unknown values: "
                                      f"{unknown_operations}")
                    if wp.get("body", "deny") not in {"allow", "append-only", "deny"}:
                        errors.append("capabilities.write_policy.body must be allow, append-only, or deny")
                    errors.extend(_identity_edit_errors(wp))

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
                if b.get("kind") not in _BINDABLE_PANEL_KINDS:
                    errors.append(
                        f"{ctx}: 'kind' must be one of {sorted(_BINDABLE_PANEL_KINDS)} for a type "
                        f"binding (two-axis renders only from a self-declared page panel, not a binding)")
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
