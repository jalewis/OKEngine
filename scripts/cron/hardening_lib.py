#!/usr/bin/env python3
"""hardening_lib.py — the OKENGINE_HARDENED posture contract (okengine#78).

A single opt-in profile so an operator doesn't have to discover and set a dozen
independent safety flags. `OKENGINE_HARDENED=1` is an *assertion of intent*:
"this deployment must be safe to expose." It is **fail-closed** — the profile
never mints secrets or silently flips values; it names every unsafe setting and
the deployment-validation gate FAILs until the operator fixes each one.

`hardened_posture_violations(env)` is the single source of truth for what the
profile requires. It is PURE (no I/O, no os.environ) so both the runtime gate
(deployment_validate.check_auth, in-gateway) and any pre-deploy caller can share
one definition and it is unit-testable without a live stack.

The posture (each unsafe setting is one FAIL string):
  1. real MCP token       OKENGINE_MCP_TOKEN set and != the built-in loopback default
  2. reader auth OR public OKENGINE_READER_PASSWORD set, or OKENGINE_TRUST=public / a
                          deliberate OKENGINE_READER_PUBLIC=1 declaration
  3. rate limiting on      OKENGINE_READER_RATE not explicitly 0 (disabled)
  4. exports off if public a PUBLIC reader must not also enable OKENGINE_READER_EXPORTS
  5. UI editing off        OKENGINE_EDITING=0 — the reader Chat's write-back (okengine-write)
                          must not be exposed on a hardened deployment (okengine#257)

Not re-checked here (already enforced UNCONDITIONALLY by deployment_validate.check_auth,
so hardened mode doesn't need to duplicate it): the Agent Chat (api_server) toolset
lockdown. That FAIL fires whether or not hardened mode is on.
"""
from __future__ import annotations

from typing import Mapping

# Keep in sync with okengine-mcp/server.py DEFAULT_LOCAL_TOKEN — the built-in token a
# fresh deployment uses out of the box, safe ONLY because the host port binds to loopback.
DEFAULT_LOCAL_TOKEN = "okengine-local"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in _TRUTHY


def is_editing(env: Mapping) -> bool:
    """Whether UI editing is enabled — the reader Chat's write-back to the vault (via the
    okengine-write MCP in the api_server toolset). DEFAULT ON for back-compat: editing is on
    unless OKENGINE_EDITING is an explicit falsey value (0/false/no/off). This is the operator
    switch; the REAL enforcement is ensure-runtime dropping okengine-write from the api_server
    toolset when it's off (okengine#257)."""
    return str(env.get("OKENGINE_EDITING") or "").strip().lower() not in _FALSEY


def is_hardened(env: Mapping) -> bool:
    """Whether the deployment opted into the hardened posture (OKENGINE_HARDENED truthy)."""
    return _truthy(env.get("OKENGINE_HARDENED"))


def is_public(env: Mapping) -> bool:
    """A reader is 'public' if the pack trust is public OR the operator flipped the
    reader's public-mode flag. Public is a legitimate, deliberate posture — hardened
    mode allows it (no reader password required) but then requires exports stay off."""
    trust = str(env.get("OKENGINE_TRUST") or "private").strip().lower()
    return trust == "public" or _truthy(env.get("OKENGINE_READER_PUBLIC"))


def hardened_posture_violations(env: Mapping) -> list[str]:
    """Return one human-readable FAIL string per unsafe setting under the hardened
    posture. Empty list means either the profile is off (OKENGINE_HARDENED unset) or
    the posture is fully safe. Pure — pass a mapping (os.environ, a dict, a parsed
    .env); never reads the environment itself."""
    if not is_hardened(env):
        return []

    out: list[str] = []

    # 1. real MCP token — the built-in default is loopback-only.
    token = str(env.get("OKENGINE_MCP_TOKEN") or "").strip()
    if not token:
        out.append(
            "OKENGINE_MCP_TOKEN is unset — hardened mode requires a real MCP token "
            f"(the built-in '{DEFAULT_LOCAL_TOKEN}' is safe only on a loopback bind). "
            "Set OKENGINE_MCP_TOKEN to a generated secret.")
    elif token == DEFAULT_LOCAL_TOKEN:
        out.append(
            f"OKENGINE_MCP_TOKEN is still the built-in default '{DEFAULT_LOCAL_TOKEN}' — "
            "set a real generated token for a hardened deployment.")

    # 2. reader auth, OR a deliberate public declaration.
    public = is_public(env)
    password = str(env.get("OKENGINE_READER_PASSWORD") or "").strip()
    if not public and not password:
        out.append(
            "the reader has no auth and the vault is not declared public — hardened mode "
            "requires OKENGINE_READER_PASSWORD to be set, or OKENGINE_TRUST=public "
            "(an explicit, deliberate public declaration).")

    # 3. rate limiting must not be explicitly disabled.
    if str(env.get("OKENGINE_READER_RATE") or "").strip() == "0":
        out.append(
            "OKENGINE_READER_RATE=0 disables reader rate limiting — hardened mode requires "
            "it on. Unset it for the built-in default, or set a positive per-minute cap.")

    # 4. a public reader must not also enable exports (unbounded pandoc/IWE work).
    if public and _truthy(env.get("OKENGINE_READER_EXPORTS")):
        out.append(
            "OKENGINE_READER_EXPORTS is enabled on a PUBLIC reader — hardened mode requires "
            "exports off when public (the reader defaults them off for public; unset the flag).")

    # 5. UI editing (reader Chat write-back) must be OFF — a hardened/exposed deployment must
    # not let anonymous UI sessions write to the vault (okengine#257).
    if is_editing(env):
        out.append(
            "OKENGINE_EDITING is on — hardened mode requires UI editing OFF (the reader Chat "
            "writes back to the vault via the okengine-write MCP). Set OKENGINE_EDITING=0; "
            "ensure-runtime then drops okengine-write from the api_server toolset.")

    return out
