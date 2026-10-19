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

An `identity_edit` grant in `capabilities.write_policy` lets the extension's writes skip the
identity guard's review hold (okengine#1028). A first-party (engine-tier, `okengine.*`) extension is
reviewed with the engine and holds its grant by declaration. A pack- or operator-tier extension holds
it only after the operator accepts it (`extensions enable --accept-identity-edit`), and the acceptance
is bound to a digest of the whole write policy, so a later change to it needs a new acceptance
(okengine#1032 [12]). Without one, the token carries the rest of the write policy and every identity
change the extension makes is held for review.

The store is a COPY of the manifest. `reconcile_store` re-derives every enabled extension's record
at deploy and rewrites a stale one, so a narrowed or revoked grant never outlives the manifest that
dropped it (okengine#1032 [17]). It also revokes an active record whose extension is no longer
enabled (removed, renamed, or dropped from extensions.yaml without `extensions disable`): such a
token has no consumer left and only kept its scopes usable (okengine#1042 [20]).
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


IDENTITY_ACCEPTED_KEY = "identity_edit_accepted"
FIRST_PARTY_TIER = "engine"


def write_policy_digest(ext_id: str, write_policy: dict) -> str:
    """What an identity-edit acceptance is bound to: the extension id and its WHOLE write policy.
    The paths, types and operations decide where the grant reaches, so widening any of them is a new
    grant as surely as adding a field to `identity_edit`."""
    raw = json.dumps({"ext_id": ext_id, "write_policy": write_policy}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + _sha256(raw)


def identity_edit_needs_acceptance(record: dict) -> bool:
    """A pack- or operator-tier extension whose manifest declares an `identity_edit` grant."""
    policy = write_capability_from_manifest(record.get("manifest") or {})
    return "identity_edit" in policy and record.get("tier") != FIRST_PARTY_TIER


def identity_edit_acceptance(record: dict) -> dict:
    """The enabled-state entry that records the operator's acceptance of this grant, as it is now."""
    policy = write_capability_from_manifest(record.get("manifest") or {})
    grant = policy.get("identity_edit")
    return {"digest": write_policy_digest(str(record.get("id")), policy),
            "fields": list(grant.get("fields") or []) if isinstance(grant, dict) else []}


def is_identity_edit_accepted(settings, record: dict) -> bool:
    accepted = settings.get(IDENTITY_ACCEPTED_KEY) if isinstance(settings, dict) else None
    return (isinstance(accepted, dict)
            and accepted.get("digest") == identity_edit_acceptance(record)["digest"])


def identity_accept_command(pack_dir, ext_id: str) -> str:
    return f"framework extensions enable {pack_dir} {ext_id} --accept-identity-edit"


def effective_write_capability(record: dict, settings, pack_dir="<pack>") -> tuple[dict, list[str]]:
    """The write capability the token may carry: the manifest's write policy, less an
    `identity_edit` grant that a pack- or operator-tier extension holds without a current
    acceptance. Returns ``(capability, warnings)``; a dropped grant is a warning, not a refusal,
    because the extension still works and its identity changes go to review (fail closed)."""
    capability = write_capability_from_manifest(record.get("manifest") or {})
    if not identity_edit_needs_acceptance(record) or is_identity_edit_accepted(settings, record):
        return capability, []
    capability.pop("identity_edit", None)
    ext_id = str(record.get("id"))
    reason = ("was accepted for a different write policy"
              if isinstance(settings, dict) and IDENTITY_ACCEPTED_KEY in settings
              else "has no recorded operator acceptance")
    return capability, [
        f"{record.get('tier')}-tier extension '{ext_id}' declares an identity_edit grant that "
        f"{reason} (okengine#1032), so its token does not carry it and its identity changes are "
        f"held for review. To grant it, accept it: `{identity_accept_command(pack_dir, ext_id)}`."]


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


def _record_view(record: dict) -> dict:
    """The authority half of a token record, normalized as the write path reads it (a missing
    `write_capability` is the empty capability: transport.py reads `or {}`)."""
    return {"read_scopes": list(record.get("read_scopes") or []),
            "write_scopes": list(record.get("write_scopes") or []),
            "write_capability": dict(record.get("write_capability") or {})}


def reconcile_store(pack_dir, resolved: dict, enabled: dict, *, write: bool = True
                    ) -> tuple[list[tuple[str, str]], list[str]]:
    """Re-derive every stored token record of an enabled extension from its CURRENT manifest, tier
    and acceptance, and report each drift (okengine#1032 [17]). With ``write`` the stale record is
    rewritten in place; the token hash and status are kept, so nothing is rotated and a running
    sidecar keeps working. An enabled extension with no record is left alone (`extensions enable`
    mints one). An ACTIVE record for an extension that is not enabled is drift too: with ``write``
    it is revoked, record and plaintext, because nothing deploys that extension any more and the
    token only kept its scopes usable (okengine#1042 [20]). An inactive record is left as it is.

    Returns ``(drift, warnings)``: drift as ``(line, what write does about it)`` pairs."""
    pack = Path(pack_dir)
    store_path = pack.joinpath(*STORE_REL)
    store = _load(store_path)
    records = store.get("tokens")
    if not isinstance(records, list):
        return [], []
    drift: list[tuple[str, str]] = []
    warnings: list[str] = []
    updated = []
    orphans = []
    for record in records:
        ext_id = record.get("ext_id") if isinstance(record, dict) else None
        target = resolved.get(ext_id) if isinstance(ext_id, str) else None
        if target is None:
            # okengine.mcp.scope.resolve reads a record with no status as active
            if isinstance(record, dict) and record.get("status", "active") == "active":
                drift.append((f"token record for '{ext_id}', which is not an enabled extension, "
                              "is active", "revoked"))
                orphans.append(ext_id)
                continue
            updated.append(record)
            continue
        read_scopes, write_scopes = scopes_from_manifest(target.get("manifest") or {})
        capability, cap_warnings = effective_write_capability(
            target, enabled.get(ext_id), pack_dir)
        warnings.extend(cap_warnings)
        desired = {"read_scopes": read_scopes, "write_scopes": write_scopes,
                   "write_capability": capability}
        current = _record_view(record)
        stale = sorted(key for key in desired if desired[key] != current[key])
        if stale:
            drift.append((f"token record for '{ext_id}' was stale against its manifest "
                          f"({', '.join(stale)})", "rewritten from the manifest"))
            record = {**record, **desired}
        updated.append(record)
    if drift and write:
        store["tokens"] = updated
        _write_private(store_path, store)
        _drop_secrets(pack, orphans)
    return drift, warnings


def _drop_secrets(pack: Path, ext_ids) -> bool:
    """Remove the plaintext of each id from the secrets file; True when one was there."""
    secrets_path = pack.joinpath(*SECRETS_REL)
    sec = _load(secrets_path)
    gone = [ext_id for ext_id in ext_ids if ext_id in sec]
    for ext_id in gone:
        sec.pop(ext_id)
    if gone:
        _write_private(secrets_path, sec)
    return bool(gone)


def holds_token(pack_dir, ext_id: str) -> bool:
    """Whether the store or the secrets file still holds anything for ``ext_id``."""
    pack = Path(pack_dir)
    records = _load(pack.joinpath(*STORE_REL)).get("tokens")
    in_store = isinstance(records, list) and any(
        isinstance(r, dict) and r.get("ext_id") == ext_id for r in records)
    return in_store or ext_id in _load(pack.joinpath(*SECRETS_REL))


def revoke(pack_dir, ext_id: str) -> None:
    """Revoke ext_id's token: drop it from the store and the secrets file. Both MCP
    servers reject a revoked/unknown token immediately."""
    pack = Path(pack_dir)
    store_path = pack.joinpath(*STORE_REL)

    store = _load(store_path)
    if "tokens" in store:
        store["tokens"] = [r for r in store["tokens"] if r.get("ext_id") != ext_id]
        _write_private(store_path, store)
    _drop_secrets(pack, [ext_id])
