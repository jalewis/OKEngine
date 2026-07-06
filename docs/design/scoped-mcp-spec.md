# Scoped MCP — implementation spec

**Issue:** okengine#132 · **Gate:** okengine#131 · **Parent design:**
[`extension-system.md`](extension-system.md) §4, §7
**Status:** design — **implemented (MVP)**
**Blocks:** #135 (sidecar write path), the enforced-`sidecar` isolation boundary,
running untrusted third-party extensions (#124)
**Consumed by:** #133 (the write-path provenance stamp this spec builds is the key
the composed-schema orphan check reads)

**Implemented:** shared token store + resolution (`okengine-mcp/scope.py`), host-side
mint/revoke (`scripts/extension_tokens.py`, wired into `framework extensions
enable/disable`), read-MCP scoped auth (admin = full; extension = scoped on
`get_page`/`retrieve_context`/`list_pages`), networked write transport
(`OKENGINE_WRITE_TRANSPORT=streamable-http`) with scoped-bearer auth + write-scope
enforcement on every write helper, and the server-side `extension_id` provenance stamp.
**Back-compat by construction:** the admin token (`OKENGINE_MCP_TOKEN`) keeps full access
and stdio write stays local-trusted, so existing deploys are unchanged.
**Deferred:** read-scope filtering of the text/graph tools (`search`/`find_references`/
`graph_stats`) — full-vault for any authenticated caller in v1 (lower-risk discovery
surfaces); the sidecar that consumes the networked write surface is #135.

## 1. Current state

**Read MCP — `okengine-mcp/server.py`.** FastMCP server `okengine` (`server.py:51`),
read-only tools (`search`, `get_page`, `find_references`, `retrieve_context`,
`graph_stats`, `list_pages`). Transport mode-switched at `server.py:350-374`: default
`stdio`; `OKENGINE_MCP_TRANSPORT=streamable-http` serves `mcp.streamable_http_app()`
on `OKENGINE_MCP_HOST` (default `127.0.0.1`) : `PORT` (default `8730`). Auth is a
**single coarse bearer**: `_resolve_http_auth(env, host)` (`server.py:213-243`) resolves
one token (`OKENGINE_MCP_TOKEN` or the built-in `DEFAULT_LOCAL_TOKEN`), enforced by
`_BearerAuth` ASGI middleware (`server.py:246-265`) with one `hmac.compare_digest`.
**One token = full read of the whole vault.** No per-caller identity, no path/namespace
scoping in any tool.

**Write MCP — `okengine-mcp/write_server.py`.** FastMCP server exposing `create_entity`/
`update_entity`/`tombstone_entity`/`patch_entity`/`append_to_section`/`converge_entity`
(logic in plain `_create`/`_update`/… helpers, testable without `mcp`). **Transport:
stdio only** — `mcp.run(transport="stdio")` (`write_server.py:972`); the module docstring
says "STDIO-ONLY — no HTTP/bearer". **No authentication of any kind.** Spawned by the
Hermes gateway as a subprocess via `config.yaml` `mcp_servers.okengine-write`
(`config/config.yaml.template:62-65`); the file is baked into the gateway image
(`scripts/build-engine-image.sh`), not the read-only `okengine-mcp` compose service —
so **a separate sidecar container has no path to reach it.** Guards
(`_reserved_refuse` :232, `_policy_reject` :279, `_namespace_reject` :294, `_field_loss`
:625) all run in-process against **pack** policy, with no caller identity.
**`_stamp` (:636) bumps only `version`/`last_updated` — there is no owning-extension
stamp today**, despite `extension-system.md` §4 implying it is "free".

**Token plumbing.** `scripts/ensure-runtime.sh:74-129` reads `OKENGINE_MCP_TOKEN` from
`<pack>/.env`; if unset/default it mints one `secrets.token_hex(24)`, persists it, and
rewrites the gateway `config.yaml` `Authorization:` header. **One token, shared by every
gateway MCP call.** `scripts/post_deploy_verify.sh:70-95` checks the read MCP rejects
unauthenticated requests and accepts the token; there is no write-auth check (nothing to
check — stdio).

## 2. Gap

1. Read MCP has one all-or-nothing token; an extension declaring `read: [wiki/x/**]`
   (§6) would get full read.
2. Write MCP is unreachable by sidecars and unauthenticated; `write:` namespaces (§6)
   are unenforced.
3. No per-extension provenance stamp → disable/orphan/purge (§9, #127, #133) has no key.
4. No token store / lifecycle — nothing maps extension → token → scopes; enable/disable
   don't mint/revoke.

## 3. Design

### 3.1 Token store + lifecycle

- **Store:** `<pack>/.okengine/extension-tokens.json`, mode `0600`, sibling to the
  enabled-state (`<pack>/.okengine/extensions.yaml`, §9) and the composed schema (#133).
  Records: `{ext_id, token_sha256, read_scopes, write_scopes, issued_at, status}`.
  **Store only the SHA-256** — the plaintext is emitted once into the extension's
  injected env and never logged or persisted.
- **Lifecycle, driven by `framework.py extensions`** (§9):
  - `enable <id>` → mint `secrets.token_hex(32)`; derive scopes from the manifest
    `capabilities.read`/`write` (§6), **validated against the composed schema (#133) so
    `write:` ⊆ the extension's owned/extended namespaces** (§7); persist hash + scopes;
    inject the plaintext into that extension's container/subprocess env
    (`OKENGINE_READ_TOKEN`, `OKENGINE_WRITE_TOKEN`).
  - `disable <id>` → set `status: revoked` (or delete). Both MCPs reject a revoked/unknown
    token immediately. Produced pages are preserved (§9); purge is #127.
  - Regeneration is idempotent / from-source (§9 generated-from-source).
- **Scope grammar:** reuse the manifest `[<vault>:]<path>` glob grammar (§6). Read scopes
  are path globs over `wiki/**`; write scopes are namespace globs that must be a subset of
  the extension's schema-declared namespaces.
- **Token model:** store-bound opaque handle (scopes looked up server-side), not a signed
  self-describing token. Matches the existing `secrets.token_hex` + `.env` model and gives
  instant revoke. (JWT/macaroon is the §6 open question, deferred.)

### 3.2 Read-MCP authorization (`server.py`)

- Replace single-token `_BearerAuth` with a **token-resolving middleware**: map the
  presented bearer → an identity record by SHA-256 against the store (gateway-admin =
  full scope for back-compat; extension = scoped). Keep `compare_digest` per candidate.
- Stash resolved `(ext_id, read_scopes)` on the ASGI scope / a contextvar.
- **Per-tool scope gate.** Add `_authorize_read(path)` consulted in `get_page` /
  `retrieve_context` / `find_references`; add a result filter in `search` / `list_pages` /
  `graph_stats` that drops any row whose vault path isn't covered by `read_scopes` (the
  tools already compute the vault path per result, e.g. `list_pages` at `server.py:189`).
  `_safe(path)` stays the path-escape guard; this is the *scope* layer on top.
- The existing `OKENGINE_MCP_TOKEN` keeps full read (cron jobs + reader Chat relay).

### 3.3 Network-reachable write transport + auth (`write_server.py`)

- **Transport.** Mirror the read server's proven pattern (`mcp.streamable_http_app()` +
  uvicorn + bearer middleware, `server.py:352-372`) behind a new `OKENGINE_WRITE_TRANSPORT`:
  default stays `stdio` (back-compat with the gateway `config.yaml` command form); set
  `streamable-http` to serve on `OKENGINE_WRITE_HOST`/`OKENGINE_WRITE_PORT` wrapped in the
  same scoped-bearer middleware. This adds the second compose surface a sidecar can dial
  (vault mounted **rw**, unlike the read service's `:ro`).
- **Write-scope check.** Resolve bearer → `(ext_id, write_scopes)`. Add
  `_authorize_write(path, ext_id, write_scopes)` at the top of the `_create`/`_update`/
  `_converge`/`_tombstone`/`_patch`/`_append_section` helpers (testable without `mcp`),
  after `_safe`/`_reserved_refuse`, before `_policy_reject`. Refuse any path whose namespace
  isn't in `write_scopes`. The existing `_policy_reject`/`_namespace_reject` remain as the
  deeper pack-policy guards; this is the per-extension layer.
- **Provenance stamp (built here).** In `_create`/`_stamp`, set
  `fm["extension_id"] = ext_id` (sentinel/omit for gateway-admin) so disable/orphan/purge
  (§9, #133, #127) has its key. Add `extension_id` to a stamp-exempt set analogous to
  `_STAMP_KEYS` (`write_server.py:623`) so `_field_loss` doesn't fire on it. **Server-side
  stamp** (derived from the scoped token, not a client-supplied env) so it can't be spoofed.

### 3.4 Failure behavior

- Unknown/revoked token → `401` at the middleware (same shape as `server.py:260-264`).
- Authenticated but out-of-scope **read** → refusal string for direct `get_page`; **silent
  filter** of out-of-scope rows from list/search (don't leak existence).
- Out-of-scope **write** → helper returns
  `"refused: namespace '<ns>/' not in this extension's write scope (declared: …)"`,
  matching the existing refusal idiom. Fail-closed; never a partial write.

### 3.5 `post_deploy_verify` additions

- Extend step [4] (`post_deploy_verify.sh:87-95`): if `OKENGINE_WRITE_TRANSPORT=
  streamable-http`, the write port returns 401 without a token and accepts the extension
  token (mirror of step [3]).
- New step: for each enabled extension, assert a token record exists; negative probe — the
  extension token is **rejected** for a read/write outside its declared scope.
- Update `tests/test_post_deploy_verify.py:40-52` `required` map.

## 4. Test plan

- **`tests/test_mcp_auth.py`** — token→scope resolution (admin / extension / unknown /
  revoked); read tool returns/filters by scope; `search`/`list_pages` drop out-of-scope rows.
- **`tests/test_write_server.py`** — `_authorize_write` allows in-scope / refuses
  out-of-scope; `extension_id` stamped on create and exempt from `_field_loss`; revoked
  token refused. Hits `_create`/`_update`/`_converge` directly.
- **New `tests/test_extension_tokens.py`** — mint-on-enable writes hashed record + scopes;
  disable revokes; store is `0600`; plaintext never appears in store/logs; scope derivation
  rejects a `write:` not covered by the composed schema (§7).
- **`tests/test_post_deploy_verify.py`** — extend for the new write-auth / scope checks.
- **`tests/test_ensure_runtime.py`** / **`tests/test_framework_validate.py`** — per-extension
  provisioning + validation that enabled extensions have token records and `write:` ⊆ schema.

## 5. Cross-cutting decisions

- **The write-path provenance stamp is built in #132** (per-extension identity arrives
  here) and consumed by #133 (orphan detection) and #135 (attribution). Server-side,
  token-derived, spoof-resistant. This closes the §4 over-claim by making the stamp real.
- **Gateway-admin identity** — the existing `OKENGINE_MCP_TOKEN` stays an implicit
  full-scope admin (cron + reader Chat relay must retain full access). Optionally minted an
  explicit `gateway` store record for uniformity.

## 6. Open questions

1. Scope encoding — store-bound (recommended v1) vs signed JWT/macaroon (no store read per
   call, needs signing key + revocation list).
2. One networked write surface with per-token scope checks (recommended — token is the wall)
   vs a write server per sidecar with a pre-scoped mount.
3. Read filtering for graph tools (`retrieve_context`/`graph_stats`/`find_references`) — do
   traversals stop at scope boundaries, or just omit out-of-scope nodes from output?
4. Token rotation on re-enable — does it rotate (forcing a sidecar restart to pick up the new
   env) or preserve?

**Anchors:** read auth `okengine-mcp/server.py:213-265,350-374`; write transport/guards
`okengine-mcp/write_server.py:232-317,623-641,972`; wiring `config/config.yaml.template:49-65`,
`scripts/ensure-runtime.sh:74-129`, `scripts/post_deploy_verify.sh:70-95`.
