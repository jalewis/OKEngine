# okengine-mcp — OKEngine MCP query surface (engine)

A read-only **MCP server** that exposes the OKF vault (whatever pack is mounted)
as query tools, so the operator's other agents/use-cases can consume the compiled
corpus as a tool — exposing a corpus as a substrate other agents consume rather
than reporting into them.

**Engine, not domain:** it serves the mounted vault generically. Every result
carries its vault **path** — the provenance contract (`discovered_by`) that makes
the knowledge attributable when a consumer ingests it.

## Tools (all read-only)

| Tool | What it does |
|---|---|
| `search(query, mode, limit)` | qmd search — defaults to `search` (instant BM25); `hybrid` (BM25+vector+rerank) on request. Hybrid is CPU-slow without a GPU, hence the lexical default. Requires a registered qmd collection or it returns nothing — setup, performance & GPU tuning in [`docs/kb-tooling.md`](../docs/kb-tooling.md#search-index--setup-performance--tuning-deployment-reality) |
| `get_page(path)` | fetch one wiki page (frontmatter + body); path-sandboxed to the vault |
| `find_references(target)` | IWE knowledge-graph: matching pages + resolved refs/backlinks |
| `list_pages(namespace, type, status, limit)` | list pages in a vault namespace, optionally filtered by frontmatter `type`/`status`, newest first (domain-agnostic) |

Wraps the engine's `kb_search.py` (qmd) and `kb_graph.py` (IWE) plus direct,
sandboxed vault reads.

## Run

```bash
# stdio (default) — local / same-host agent integration, testing
WIKI_PATH=/opt/vault python okengine-mcp/server.py

# networked — for sibling agents on other hosts
OKENGINE_MCP_TRANSPORT=streamable-http WIKI_PATH=/opt/vault python okengine-mcp/server.py
```

Env: `WIKI_PATH`, `OKENGINE_MCP_SCRIPTS` (default `/opt/data/scripts`),
`OKENGINE_MCP_PY`, `OKENGINE_MCP_TRANSPORT`.

## Deployment

- **stdio:** server + read tools run stdio in the existing gateway container (it
  has the venv, mcp SDK, qmd/IWE, scripts, and the vault mount).
- **networked:** a slim dedicated image
  (`Dockerfile`, option A): `python:3.13-slim-trixie` + nodejs/qmd (`@tobilu/qmd`,
  better-sqlite3 compiled via a fixed node-gyp) + IWE binary + the engine `kb_*`
  wrappers + `server.py`. **Not** the hermes image — so no duplicate gateway (no
  gateway/cron/telegram process runs inside the container).
  Compose service `okengine-mcp` on `:8730` (streamable-http), vault mounted `:ro`,
  only `/opt/data/qmd` writable (qmd's SQLite cache), run as `HERMES_UID`,
  `restart: unless-stopped` + 2 CPU / 2.5 GB limits.
  **Auth (local-first):** the server **always** comes up with a bearer token
  (401 without; constant-time compared). `OKENGINE_MCP_TOKEN` from the repo-root
  `.env` (gitignored) sets it; if unset it falls back to the built-in default
  `okengine-local`, so a fresh `docker compose up` just works. That default is
  safe only because the **host port binds `127.0.0.1` by default**
  (`OKENGINE_BIND` in `.env`); the container itself binds `0.0.0.0`
  (`OKENGINE_MCP_HOST`, required for Docker port-forwarding), so LAN exposure is
  gated at the host-port mapping, not in the app. To expose: set
  `OKENGINE_BIND=0.0.0.0` **and** a real `OKENGINE_MCP_TOKEN` — `framework
  validate` FAILs if you widen the bind while still on the default/empty token.
  Binding beyond loopback with the default token logs a startup warning;
  `OKENGINE_MCP_ALLOW_UNAUTHENTICATED=1` is an explicit opt-out to serve with no
  auth at all.

## Connect

A consuming Hermes agent points an MCP client at `http://<host>:8730/mcp` with
`Authorization: Bearer $OKENGINE_MCP_TOKEN`. Rotate the token by editing the repo
`.env` and `docker compose up -d okengine-mcp`.
