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
| `0.2.x` / `main` | ✅ |
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

When reporting, please note the deployment mode (local vs exposed) you tested.
