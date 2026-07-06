#!/usr/bin/env python3
"""scope — per-extension MCP token resolution + path-scope matching (okengine#132).

Shared by the read server (server.py) and the write server (write_server.py) so both
enforce the same per-extension scopes. The token STORE is minted host-side by
`framework extensions enable` (scripts/extension_tokens.py) and lives in the vault at
`<vault>/.okengine/extension-tokens.json` — both MCP containers mount the vault, so
both can read it. The store holds only SHA-256 token hashes (never plaintext), so it
is safe to mount read-only into the read container.

Back-compat is the contract: the gateway's configured admin token (OKENGINE_MCP_TOKEN)
keeps FULL access; only a token present in the store resolves to a scoped identity.
With no store and the admin token, behavior is exactly as before #132.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
from pathlib import Path


def store_path() -> Path:
    vault = Path(os.environ.get("WIKI_PATH") or "/opt/vault")
    override = os.environ.get("OKENGINE_EXT_TOKEN_STORE")
    return Path(override) if override else vault / ".okengine" / "extension-tokens.json"


_cache: dict = {"mtime": None, "records": []}


def load_records() -> list[dict]:
    """Load token records, mtime-cached. Each: {ext_id, token_sha256, read_scopes,
    write_scopes, status}. Missing/unparseable store -> []."""
    p = store_path()
    try:
        mt = p.stat().st_mtime
    except OSError:
        _cache["mtime"], _cache["records"] = None, []
        return []
    if _cache["mtime"] != mt:
        recs: list[dict] = []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("tokens", []) if isinstance(data, dict) else data
            if isinstance(raw, list):
                recs = [r for r in raw if isinstance(r, dict)]
        except Exception:
            recs = []
        _cache["records"], _cache["mtime"] = recs, mt
    return _cache["records"]


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve(token: str) -> dict | None:
    """Plaintext token -> its active store record, or None. Constant-time compare."""
    if not token:
        return None
    h = token_sha256(token)
    for r in load_records():
        if r.get("status", "active") != "active":
            continue
        if hmac.compare_digest(str(r.get("token_sha256", "")), h):
            return r
    return None


def _norm(scope: str) -> str:
    """Normalize a manifest scope to a wiki-relative pattern: drop an optional
    `<vault>:` qualifier and a leading `wiki/` (capability paths are wiki-relative)."""
    s = str(scope).split(":", 1)[-1].lstrip("/")
    if s.startswith("wiki/"):
        s = s[len("wiki/"):]
    return s


def path_in_scopes(rel_path: str, scopes) -> bool:
    """Is a wiki-relative path (e.g. 'dashboards/x' or 'entities/a/y') covered by any
    scope? Scopes are the manifest globs ('wiki/**', 'dashboards/**', a namespace). A
    full-vault scope ('wiki/**' / '**' / '*') covers everything."""
    rp = str(rel_path).lstrip("/")
    if rp.endswith(".md"):
        rp = rp[:-3]
    for raw in scopes or []:
        pat = _norm(raw)
        if pat in ("", "**", "*"):
            return True
        prefix = pat.rstrip("*").rstrip("/")
        if prefix and (rp == prefix or rp.startswith(prefix + "/")):
            return True
        if fnmatch.fnmatch(rp, pat):
            return True
    return False


def is_full(scopes) -> bool:
    """True if the scope set grants the whole vault (so a caller is effectively admin
    on that surface)."""
    return any(_norm(s) in ("", "**", "*") for s in (scopes or []))
