# KB Tooling — qmd search + bounded backlink graph

Local, on-device knowledge-base tooling wired into the agent. qmd runs in the MCP
container; the gateway periodically builds a bounded backlink artifact consumed by
the MCP, reader, and cockpit.

## What's wired

| Tool | What | Wrapper (agent calls via `terminal`) |
|---|---|---|
| **qmd** 2.5.3 (`@tobilu/qmd`) | local hybrid search over `wiki/`: BM25 + vector + LLM rerank, all on-device | `scripts/cron/kb_search.py` |
| **Backlink scanner** | bounded markdown graph over native `[[wikilinks]]` and Markdown links | `scripts/cron/backlink_lib.py` + `backlinks_refresh.py` |

### Usage

```bash
# semantic / hybrid search (needs embeddings; falls back: use --mode search for BM25)
python /opt/data/scripts/kb_search.py "ransomware targeting healthcare"
python /opt/data/scripts/kb_search.py --mode search "CISA ICS advisory"   # BM25, instant

# refresh the static graph artifact (normally scheduled)
python /opt/data/scripts/backlinks_refresh.py
```

## Behavior at scale

**qmd** — `qmd collection add wiki/`:
- BM25 `search` is near-instant and returns `qmd://` URIs + snippets.
- Vector embeddings use a small local model (`embeddinggemma-300M`, Q8) on **CPU**
  (no GPU in the container); the full embed is a long batch job, so BM25 is the
  default fast path and the vector index builds in the background.

**Backlink scanner** over `wiki/`:
- Parses native `[[...]]` and Markdown links and resolves backlinks across the corpus.
- Writes page, target, edge, and hub evidence to `wiki/.backlinks.json`.
- Scans once per refresh rather than rebuilding the corpus on a query. On the measured
  large vault it used about 104 MiB/14 seconds versus IWE's 4.2 GiB/530 seconds.

## Search index — setup, performance & tuning (deployment reality)

> The sections above describe the original terminal-wrapper design. In a deployment that
> exposes the **`okengine` read MCP** (`okengine-mcp/server.py`), the agent reaches qmd and the graph
> through that MCP server, which runs in the **mcp container** (`okpack-cti-*-mcp`) — that is
> where qmd is installed and where `/opt/data/qmd/` lives. The **gateway** runs cron-plus but
> does **not** have qmd installed. This topology has real consequences below.

**The index must be registered, or search returns nothing.** qmd indexes a *collection*; with
none registered (`index.yml` `collections: {}`) every query returns "No results" — silently,
for everything. One-time setup, run **in the container that has qmd** (the mcp container),
with the wrapper's env:

```bash
export XDG_CACHE_HOME=/opt/data/qmd/cache XDG_CONFIG_HOME=/opt/data/qmd/config
qmd collection add /opt/vault/wiki     # registers wiki/**/*.md
qmd update                             # builds the BM25/FTS index → instant lexical search
qmd embed                              # OPTIONAL: vectors for hybrid (long CPU batch)
qmd status                             # docs/vectors indexed
```

**Lexical is the default (for speed).** `okengine-mcp/server.py` `search()` defaults to
`mode="search"` — instant BM25, no model load. `mode="hybrid"` runs local query-expansion
(1.7B) + embed (300M) + rerank (0.6B) GGUF models; on **CPU** that's ~16 s/query — too slow
for interactive chat. Change the default by editing `search()`'s `mode=` default; pass
`mode="hybrid"` per-call when a semantic match is worth the latency.

### GPU acceleration (NOT enabled here — deliberate; how to turn it on)

qmd runs the local query-expansion (1.7B), embed (300M) and rerank (0.6B) GGUF models on
**CPU** — `~16 s/query` for hybrid, and `qmd embed` is a long batch. This is **intentional**:
the wrappers force CPU and the containers aren't given the GPU, so the deployment stays
portable and lexical (BM25) is the fast default. A host GPU (e.g. an NVIDIA RTX A2000) sits
**idle** as a result. If a deployment wants fast hybrid/embeddings, enabling the GPU is the
single biggest win — done by the operator, in this order:

1. **Host**: NVIDIA driver + the **NVIDIA Container Toolkit** (`nvidia-ctk runtime configure`),
   so containers can see the GPU.
2. **Give the mcp container the GPU** in `docker-compose.yml` (the mcp service), e.g.
   `deploy.resources.reservations.devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]`
   (or `gpus: all`). The reader/gateway don't need it.
3. **Stop forcing CPU.** `QMD_FORCE_CPU=1` is currently **hard-coded** in `_QMD_ENV` in both
   `okengine-mcp/server.py` and `scripts/cron/kb_search.py` — make it env-controlled (or
   drop it) so the GPU is actually used. (Small code change; intentionally not done.)
4. **CUDA-capable qmd build.** qmd's GGUF inference (node-llama-cpp) is compiled for CPU in
   `okengine-mcp/Dockerfile`; rebuild it with CUDA support (CUDA toolkit in the build stage +
   the node-llama-cpp CUDA flags) so the models run on the GPU.
5. **Verify**: `qmd doctor` (device diagnostics — should report GPU/CUDA, not "no GPU
   acceleration").

Expected payoff: hybrid query (expansion + rerank) and `qmd embed` drop from ~16 s / long
batch on CPU to sub-second / minutes on the GPU — enough to make **hybrid** a viable default
and the embedding build cheap. **Trade-offs / why it's off now:** the laptop-class A2000 is
modest and shared with other workloads; the CUDA qmd build adds image complexity and breaks
the "portable, GPU-optional" property; and lexical search is already fast enough for the
current demo. Revisit if/when semantic recall (hybrid) becomes a requirement.

**Freshness (self-maintaining).** The mcp server (`okengine-mcp/server.py`) keeps the index
fresh itself, because qmd is only in the mcp container and cron-plus (gateway) has no qmd. On
startup it **registers the `wiki` collection if missing** (so a fresh deploy self-bootstraps
search) and runs an incremental `qmd update`, then repeats every
`OKENGINE_MCP_INDEX_REFRESH_HOURS` (default `6`; set `0` to disable). **Between full refreshes it
also polls the vault every `OKENGINE_MCP_INDEX_POLL_SECONDS` (default `30`) and reindexes
incrementally when a page's mtime changes** — so a page the agent just wrote is
searchable within seconds on a quiet vault, not up to 6h later (the write→recall loop;
okengine#80). Change-triggered updates are **debounced**: after each update the next one waits
`max(OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS (default 60), 3 × the update's own duration)`, so on
a large vault where an incremental `qmd update` takes minutes, a write burst (backfill lanes)
coalesces into one update per cooldown instead of one per write — reindex churn previously
starved MCP tool calls into the client's 300s timeout. Writes landing during the cooldown are
picked up by the next update, never lost. The full
refresh still runs on its timer to catch deletions/orphaned hashes the mtime check can't see.
Lexical/FTS only —
vector `qmd embed` stays manual (heavy; off the default search path). Recreating the mcp
container preserves the index (it lives on the `/opt/data` volume) and the maintainer
re-runs on the next start.

**Load shedding.** Search and index maintenance share
`OKENGINE_MCP_QMD_CONCURRENCY` slots (default `2`). A search waits at most
`OKENGINE_MCP_SEARCH_QUEUE_SECONDS` (default `10`) for capacity, then returns an explicit
`search saturated` result that clients should retry with backoff. Once admitted, a search is
bounded by `OKENGINE_MCP_SEARCH_TIMEOUT_SECONDS` (default `120`). Client cancellation or timeout
kills and reaps the whole helper process group, including qmd descendants; abandoned searches do
not continue consuming CPU or memory. Index refresh requests are coalesced so only one refresh can
run at a time. Fleet health reports saturation separately from execution timeout.

**Chat latency knobs (reader → agent).** A chat answer is a multi-turn agent loop over the
(remote) model — each tool decision is a model round-trip — so latency = turns × (model +
tool). Tune via: the search default (lexical, above); the MCP tool output caps in `server.py`
(`search` 8k, `retrieve_context` 16k — smaller = cheaper prompts); the reader's
`OKENGINE_READER_CHAT_MAX_MSGS` / `_CHAT_MAX_CHARS`; and the model/provider. The reader's
grounding prompt makes the agent **acknowledge before calling tools** so the wait is visible,
not a blank stream.

## Architecture / persistence (code in image, data on the volume)

- **Search binaries ship in the image** (`Dockerfile`): `npm i -g @tobilu/qmd`.
- **Data lives on the `/opt/data` volume** (`~/.hermes`), NOT in the image:
  - qmd index + ~2 GB GGUF models → `/opt/data/qmd/` (`XDG_CACHE_HOME`/`XDG_CONFIG_HOME`).
    Set once; a recreate does not re-download or re-index.
- The search wrapper sets `XDG_*` + `QMD_FORCE_CPU=1`; the graph producer resolves
  `WIKI_PATH` and atomically publishes `wiki/.backlinks.json`.

## Why the graph is an artifact

Graph reads vastly outnumber graph rebuilds. A scheduled, atomically published artifact
makes every lookup O(1), gives all three consumers the same snapshot, and prevents a typo
or stale cache from initiating multi-gigabyte work. Missing or stale evidence is reported
explicitly; it is never repaired synchronously on the request path.

## UI integration — okengine-reader backlinks panel

The reader UI shows a **"↩ Backlinks — what links here"** panel on every wiki
page opened in the overlay (entities/concepts/sources/predictions), powered by the
static backlink graph — each backlink clickable to navigate.

Design (keeps the reader **standalone** — no Hermes coupling):
- `GET /api/backlinks?path=<key>` reads `wiki/.backlinks.json` and serves per-page
  lookups instantly.
- A bounded in-process scan is available to the UI as an asynchronous compatibility
  fallback; request handling never waits for it.
- The vault remains mounted read-only in the UI containers.

The qmd semantic search bar (replacing the ripgrep `/api/search`) is the planned
next step — it needs the warm qmd HTTP daemon (see MCP upgrade path above).

## Maintenance

- Lexical/FTS refresh is **automatic** — the mcp server self-registers the collection and
  runs incremental `qmd update` on a timer (`OKENGINE_MCP_INDEX_REFRESH_HOURS`, default 6).
  Vector `qmd embed` is still manual (heavy) — run it mcp-side off-hours if you want hybrid.
  See [Search index — setup, performance & tuning](#search-index--setup-performance--tuning-deployment-reality).
- The `backlinks-refresh` lane publishes the graph artifact daily; stale/missing
  evidence is a health condition rather than permission to rebuild during a request.
- Bump `npm i -g @tobilu/qmd@<v>` in the Dockerfile to upgrade qmd.
