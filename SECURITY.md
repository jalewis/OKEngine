# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

Use GitHub's private vulnerability reporting at
[github.com/jalewis/okengine](https://github.com/jalewis/okengine): the repository's
**Security** tab → **Report a vulnerability**. Include a description, affected version/commit, and
reproduction steps. We aim to acknowledge within a few days and will coordinate a
fix and disclosure timeline with you.

## Supported versions

OKEngine is pre-1.0. Security fixes target the latest `0.x` release and `main`.

| Version | Supported |
|---|---|
| `0.15.x` / `main` | ✅ |
| older | ❌ |

## Security model (what to keep in mind)

OKEngine is **local-first by design**, and the security posture depends on how a
deployment is configured:

- **Bind boundary.** Out of the box the stack binds host ports to `127.0.0.1`
  (`OKENGINE_BIND`) — reachable only from the host. Exposing reader/MCP on a
  network is a deliberate change (`OKENGINE_BIND=0.0.0.0`).
- **MCP auth.** The MCP server always comes up with a bearer token; if
  `OKENGINE_MCP_TOKEN` is unset it falls back to a **built-in default**, which is
  safe *only* while bound to loopback. Set a real token before exposing it.
  `framework validate` FAILs if a deployment is exposed beyond localhost while
  still on the default/empty token (and likewise for a passwordless reader).
- **Write path.** Agent writes go through the MCP write server, which validates
  against the governing `schema.yaml` and enforces namespace permissions; the
  file-tool write-guard is the backstop.
- **A public reader deployment** must mount only public content and should disable
  or rate-limit expensive endpoints (export/graph rebuild).
- **UI editing.** The reader's Chat can *write back* to the vault, through its own actor-bound
  `okengine-write-chat` MCP (actor `chat:api_server`) in the api_server toolset, never the generic
  `okengine-write` (which has no actor, so the write path treats it as admin). The `chat:api_server`
  capability in `config/policy/catalog.yaml` limits it to creating pages and updating frontmatter or
  appending to sections in `entities/` and `concepts/`: no body replacement, no tombstone, converge,
  patch, flag or review, and no provenance, handling, review or attestation fields. Every chat write
  is queued for human review. A pack may replace that capability only inside the engine ceiling
  (`framework validate` FAILs anything wider), and `framework validate` plus the in-gateway
  deployment check FAIL any model-facing platform that can reach the actor-less writer
  (okengine#983), including a messaging platform Hermes enables from `.env` credentials alone
  (such as `TELEGRAM_BOT_TOKEN` in `.env` or `.hermes-data/.env`) with no `platform_toolsets` entry
  (okengine#991). On an
  externally-exposed deployment set **`OKENGINE_EDITING=0`** in the pack `.env` to make chat
  read-only — `ensure-runtime` reads it from there, parsing the file the way compose does, and
  drops `okengine-write-chat` from the api_server toolset on the next gateway recreate, so chat
  still answers from the vault but cannot edit it. The gateway gets the flag from `.env` through compose
  `env_file`; the reader has no `env_file` and gets it only from an explicit
  `OKENGINE_EDITING=${OKENGINE_EDITING:-}` entry (in the skeleton compose since okengine#991; a
  hand-maintained pack compose needs it added, or the reader's read-only indicator stays off). Default is on for
  back-compat; the reader shows a read-only indicator when off.
- **The Hermes CLI is an operator surface.** An interactive `hermes` session inside the gateway
  has no `platform_toolsets` entry, so Hermes gives it every MCP server, including the actor-less
  admin writer. It is reachable only via `docker exec` into the gateway, by someone who already
  holds the container, so it is admin-equivalent by construction. The binding check exempts it by
  an explicit, named rule (`hardening_lib.OPERATOR_SURFACES`).
- **Hardened profile (one switch).** Rather than discover and set each flag above,
  set `OKENGINE_HARDENED=1` to assert "this deployment must be safe to expose." It
  is **fail-closed**: the daily in-gateway `deployment_validate` lane FAILs (marking
  itself ERRORED in fleet health) on any unsafe setting and names it — a missing or
  default `OKENGINE_MCP_TOKEN`, a private reader with neither `OKENGINE_READER_PASSWORD`
  nor an explicit `OKENGINE_TRUST=public`, rate limiting disabled
  (`OKENGINE_READER_RATE=0`), exports left on for a public reader, or **UI editing left on**
  (requires `OKENGINE_EDITING=0`). The profile
  never mints secrets or flips values silently — the operator supplies them, so what
  changed is always visible.

When reporting, please note the deployment mode (local vs exposed) you tested.
