# okengine-reader

A standalone, **read-only** web reader for an OKEngine/OKF vault. It is
**domain-agnostic**: it discovers the vault's structure at runtime (the
directories under `wiki/` and each page's `type`/`title` frontmatter) and ships
no knowledge of any particular pack. Point it at any OKF vault and browse.

Deliberately **separate from Hermes**: it imports no hermes modules, makes no
gateway/dashboard calls, and serves only from a read-only mount of the vault, so
it keeps working even if the rest of the stack is down.

## What it does

- **Browse** — a rail of the vault's top-level directories (with page counts);
  click one to list its pages (title · type · updated).
- **Render** — any page as HTML, with Obsidian `![[embeds]]` inlined and
  `[[wikilinks]]` turned into click-through navigation.
- **Backlinks** — a "↩ what links here" panel per page, from the IWE wikilink
  graph (built once at startup, cached).
- **Search** — ripgrep across the whole vault.
- **Export** — download any page as `md` / `docx` / `pdf` (pandoc + weasyprint).
- **Chat** *(optional tab)* — talk to the vault's **agent**, which answers by
  navigating the wiki as its memory. See [Agent chat](#agent-chat-optional).

## Agent chat (optional)

A **Chat** tab appears **only when an agent endpoint is configured** (see the env
below); otherwise the reader stays pure browse. It is the deliberate
counter-demo to RAG (`docs/okf/guide-1` §3.1, "Why a Wiki, Not a Vector
Database"): you chat with **the** agent that maintains the vault, and it answers
by **navigating the wiki as its memory** — not by chunk-embed-retrieve, and not a
second agent bolted onto the reader.

**How it works.** `POST /api/chat` relays the conversation to an
**OpenAI-compatible agent endpoint** (Hermes' built-in `api_server`,
`/v1/chat/completions`) and streams the reply back as SSE. The reader runs **no
model of its own** and holds the agent key server-side; the browser only ever
sees the token stream. Replies render as markdown and the agent's
`path`/`[[wikilink]]` citations become click-through links into the reader.

**Grounding + write-back contract.** The reader injects a server-controlled
system prompt (`OKENGINE_AGENT_SYSTEM`, the browser cannot override it) that makes
the agent:
1. **search the vault first** and answer only from vault pages, citing them;
2. when the vault is thin, **research, then write what it learns back** into the
   vault (via the agent's write tools) so the corpus compounds — mirroring an
   existing same-type page's frontmatter rather than inventing fields;
3. never fabricate — say so when it can't ground a claim.

**Agent capability is the operator's call, enforced on the agent — not here.** The
reader is just a relay; what the chat agent can *do* (read-only vs research +
write-back vs full tools) is set on the agent (e.g. Hermes per-platform
`hermes tools`), not in the reader. For a public demo, scope the agent's chat
platform to **read + the wiki-write MCP + web research only**, and disable
**terminal / code-execution / host-file** tools — otherwise an untrusted visitor
is driving an agent that can run code and read secrets inside its container. The
write-back path itself is schema-validated and reserved-file-guarded by the
agent's write server; note that chat-driven writes are still agent-authored
content (review-flagged, not human-verified) landing in the agent's memory.

## Security

- **Output is sanitized.** Vault content is partly agent- and feed-derived, so
  rendered markdown→HTML is scrubbed with [`nh3`](https://pypi.org/project/nh3/)
  (allowlisted tags/attrs) before it reaches the browser — inline `<script>`,
  event handlers, and `javascript:` URLs are stripped.
- **Read-only reader.** The vault is mounted `:ro`; the reader itself has no
  write path. The optional [Agent chat](#agent-chat-optional) *relays* to a
  separate agent that may write to the vault through its own guarded MCP —
  that capability is configured on the agent, not in the reader.
- **Path-confined.** Every file read is resolved and checked to be inside the
  vault root; `..` and absolute paths are refused.
- **Optional auth.** Public reference deployments run open. For a private vault,
  set `OKENGINE_READER_PASSWORD` to require HTTP Basic auth on everything except
  `/healthz` (the credential is constant-time compared).
- **Bounded expensive work.** The endpoints that spawn subprocesses
  (`/api/download` docx/pdf → pandoc/WeasyPrint, `/api/search` → ripgrep) are
  concurrency-capped and per-IP rate-limited, and `/api/backlinks` never blocks a
  request on the heavy IWE graph build (it serves the cached map and refreshes in
  the background). `OKENGINE_READER_PUBLIC=1` turns on safe defaults for an
  internet-facing reader — see [Public deployments](#public-deployments).

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `VAULT_DIR` | `/vault` | read-only vault root (expects a `wiki/` subdir) |
| `PORT` | `9200` | listen port |
| `OKENGINE_READER_PASSWORD` | _(unset → open)_ | if set, require HTTP Basic auth |
| `OKENGINE_READER_USER` | `okengine` | Basic-auth username |
| `IWE_BIN` | `iwe` | path to the IWE binary (backlinks) |
| `OKENGINE_READER_PUBLIC` | `0` | `1` = internet-facing defaults (exports off, rate limit on) |
| `OKENGINE_READER_EXPORTS` | `1` (local) / `0` (public) | allow docx/pdf export (pandoc/WeasyPrint); `md` is always allowed |
| `OKENGINE_READER_MAX_EXPORT` | `2` | max concurrent docx/pdf conversions (over → `503`) |
| `OKENGINE_READER_MAX_SEARCH` | `4` | max concurrent ripgrep searches (over → `503`) |
| `OKENGINE_READER_RATE` | `0` (local) / `60` (public) | per-IP req/min on the expensive endpoints (incl. `/api/chat`); `0` disables (over → `429`) |
| `OKENGINE_AGENT_API` | _(unset → no Chat tab)_ | OpenAI-compatible agent endpoint (e.g. `http://host.docker.internal:8642/v1`); set with `_KEY` to enable the Chat tab |
| `OKENGINE_AGENT_KEY` | _(unset)_ | bearer key for the agent endpoint (held server-side; never sent to the browser) |
| `OKENGINE_AGENT_MODEL` | `OKEngine Agent` | model name sent to the agent endpoint |
| `OKENGINE_AGENT_SYSTEM` | _(built-in)_ | override the wiki-first + write-back grounding prompt |
| `OKENGINE_READER_CHAT_MAX_MSGS` | `24` | max conversation turns relayed per request |
| `OKENGINE_READER_CHAT_MAX_CHARS` | `8000` | max characters per message relayed |

## HTTP API

| Endpoint | Returns |
|---|---|
| `GET /` | the single-page UI shell |
| `GET /api/tree` | top-level directories + page counts |
| `GET /api/pages?dir=<dir>` | pages under a directory (path · title · type · updated) |
| `GET /api/page?path=<key>` | one page rendered to sanitized HTML |
| `GET /api/backlinks?path=<key>` | pages that link to `<key>` (IWE graph) |
| `GET /api/search?q=<query>` | ripgrep matches across the vault |
| `GET /api/download?fmt=md\|docx\|pdf&path=<key>` | export a page |
| `POST /api/chat` | relay a chat turn to the agent, streaming SSE — only when an agent endpoint is configured (see [Agent chat](#agent-chat-optional)) |
| `GET /api/about` | vault/engine/Hermes version line + `chat_enabled` flag |
| `GET /healthz` | liveness (always open) |

`<key>` is a wiki-relative path without the `.md` extension (e.g.
`entities/acme`).

## Public deployments

For an internet-facing reader, set `OKENGINE_READER_PUBLIC=1`. That flips on safe
defaults: docx/pdf exports **off** (the cheap `md` export still works), a per-IP
rate limit (`60`/min) on the expensive endpoints, and the concurrency caps. Each
knob in the table above is independently overridable (e.g. re-enable exports with
`OKENGINE_READER_EXPORTS=1` while keeping the rate limit).

The in-app rate limit is a backstop, not a substitute for an edge proxy. Front the
reader with a reverse proxy that rate-limits and caps body sizes — e.g. nginx:

```nginx
limit_req_zone $binary_remote_addr zone=reader:10m rate=120r/m;
server {
    location /api/ {
        limit_req zone=reader burst=20 nodelay;
        proxy_pass http://127.0.0.1:9200;
    }
    location / { proxy_pass http://127.0.0.1:9200; }
}
```

Mount **only public content** in a public deployment (the reader serves the whole
vault it is pointed at).

## Run

```bash
# Local (needs pandoc, ripgrep, and optionally the IWE binary on PATH):
VAULT_DIR=/path/to/vault uvicorn app:app --port 9200

# Container (bundles pandoc + ripgrep + IWE):
docker build -t okengine-reader .
docker run --rm -p 9200:9200 -v /path/to/vault:/vault:ro okengine-reader
```
