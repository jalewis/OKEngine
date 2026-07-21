#!/usr/bin/env python3
"""extension_tokens — mint/revoke per-extension MCP tokens (okengine#132, host side).

The write half of the scoped-MCP contract. `framework extensions enable` mints a
token for an extension; `disable` revokes it. Two files under `<pack>/.okengine/`:

  - extension-tokens.json   — the STORE the MCP servers read (SHA-256 hashes + scopes,
                              NO plaintext). Safe to mount read-only into the containers.
  - extension-secrets.json  — plaintext tokens (mode 0600), gitignored. Read only by the
                              deploy to inject OKENGINE_*_TOKEN into a sidecar's env
                              (okengine#135). In-gateway extensions don't use it.

Scopes are derived from the manifest `capabilities.read` / `capabilities.write`
(`docs/design/scoped-mcp-spec.md`). The store format matches okengine-mcp/scope.py.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

STORE_REL = (".okengine", "extension-tokens.json")
SECRETS_REL = (".okengine", "extension-secrets.json")


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def scopes_from_manifest(manifest: dict) -> tuple[list, list]:
    caps = manifest.get("capabilities") or {}
    read = list(caps.get("read") or [])
    write = list(caps.get("write") or [])
    return read, write


def write_capability_from_manifest(manifest: dict) -> dict:
    """Return the optional field/body capability bound into the token record.

    Path scopes remain in capabilities.write for compatibility.  The richer
    contract is separately named so existing list-shaped manifests do not change
    meaning during the incremental migration.
    """
    caps = manifest.get("capabilities") or {}
    value = caps.get("write_policy") or {}
    return dict(value) if isinstance(value, dict) else {}


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def mint(pack_dir, ext_id: str, read_scopes, write_scopes,
         write_capability: dict | None = None) -> str:
    """Mint (or rotate) a token for ext_id. Persists the SHA-256 + scopes to the store
    and the plaintext to the secrets file. Returns the plaintext (emitted once)."""
    pack = Path(pack_dir)
    token = secrets.token_hex(32)
    store_path = pack.joinpath(*STORE_REL)
    secrets_path = pack.joinpath(*SECRETS_REL)

    store = _load(store_path)
    tokens = [r for r in store.get("tokens", []) if r.get("ext_id") != ext_id]
    tokens.append({
        "ext_id": ext_id,
        "token_sha256": _sha256(token),
        "read_scopes": list(read_scopes or []),
        "write_scopes": list(write_scopes or []),
        "write_capability": dict(write_capability or {}),
        "status": "active",
    })
    store["tokens"] = sorted(tokens, key=lambda r: r["ext_id"])
    _write_private(store_path, store)

    sec = _load(secrets_path)
    sec[ext_id] = token
    _write_private(secrets_path, sec)
    return token


def reconcile(pack_dir, ext_id: str, read_scopes, write_scopes,
              write_capability: dict | None = None) -> dict:
    """Make an enabled extension's stored scopes match its current manifest.

    Preserve a valid active token so a manifest-only capability change does not break an already
    deployed sidecar. Rotate only when either half of the credential is absent/inconsistent; in
    that case retaining the old hash or plaintext would leave an unusable authorization record.
    Returns ``{"token", "changed", "rotated"}``.
    """
    pack = Path(pack_dir)
    store_path = pack.joinpath(*STORE_REL)
    secrets_path = pack.joinpath(*SECRETS_REL)
    store = _load(store_path)
    sec = _load(secrets_path)
    records = [r for r in store.get("tokens", [])
               if isinstance(r, dict) and r.get("ext_id") == ext_id]
    plaintext = sec.get(ext_id)
    current = records[-1] if records else None
    valid = (
        isinstance(plaintext, str)
        and bool(plaintext)
        and current is not None
        and current.get("status") == "active"
        and current.get("token_sha256") == _sha256(plaintext)
    )
    if not valid:
        token = mint(pack, ext_id, read_scopes, write_scopes, write_capability)
        return {"token": token, "changed": True, "rotated": True}

    desired = dict(current)
    desired["read_scopes"] = list(read_scopes or [])
    desired["write_scopes"] = list(write_scopes or [])
    desired["write_capability"] = dict(write_capability or {})
    desired["status"] = "active"
    others = [r for r in store.get("tokens", [])
              if not isinstance(r, dict) or r.get("ext_id") != ext_id]
    changed = len(records) != 1 or desired != current
    if changed:
        store["tokens"] = sorted(others + [desired],
                                 key=lambda r: str(r.get("ext_id") or ""))
        _write_private(store_path, store)
    return {"token": plaintext, "changed": changed, "rotated": False}


def revoke(pack_dir, ext_id: str) -> None:
    """Revoke ext_id's token: drop it from the store and the secrets file. Both MCP
    servers reject a revoked/unknown token immediately."""
    pack = Path(pack_dir)
    store_path = pack.joinpath(*STORE_REL)
    secrets_path = pack.joinpath(*SECRETS_REL)

    store = _load(store_path)
    if "tokens" in store:
        store["tokens"] = [r for r in store["tokens"] if r.get("ext_id") != ext_id]
        _write_private(store_path, store)
    sec = _load(secrets_path)
    if ext_id in sec:
        sec.pop(ext_id, None)
        _write_private(secrets_path, sec)
