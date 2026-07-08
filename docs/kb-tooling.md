# KB Tooling — qmd (search) + IWE (graph)

Local, on-device knowledge-base tooling wired into the agent. Both run **inside
the gateway container** (where the vault is mounted at `/opt/vault`) and are exposed
to the agent via thin terminal-tool wrappers.

## What's wired

| Tool | What | Wrapper (agent calls via `terminal`) |
|---|---|---|
| **qmd** 2.5.3 (`@tobilu/qmd`) | local hybrid search over `wiki/`: BM25 + vector + LLM rerank, all on-device | `scripts/cron/kb_search.py` |
| **IWE** 0.3.2 (`iwe-org/iwe`) | markdown knowledge-graph; parses the vault's native `[[wikilinks]]` | `scripts/cron/kb_graph.py` |

### Usage

```bash
# semantic / hybrid search (needs embeddings; falls back: use --mode search for BM25)
python /opt/data/scripts/kb_search.py "ransomware targeting healthcare"
python /opt/data/scripts/kb_search.py --mode search "CISA ICS advisory"   # BM25, instant

# graph navigation (READ-ONLY — wrapper refuses normalize/squash/extract/rename/delete/update)
python /opt/data/scripts/kb_graph.py stats
python /opt/data/scripts/kb_graph.py find "ransomware"
python /opt/data/scripts/kb_graph.py retrieve concepts/ransomware
```

## Behavior at scale

**qmd** — `qmd collection add wiki/`:
- BM25 `search` is near-instant and returns `qmd://` URIs + snippets.
- Vector embeddings use a small local model (`embeddinggemma-300M`, Q8) on **CPU**
  (no GPU in the container); the full embed is a long batch job, so BM25 is the
  default fast path and the vector index builds in the background.

**IWE** — `iwe stats` over `wiki/`:
- Parses the vault's native `[[...]]` wikilink graph and resolves backlinks across
  the corpus.
- Reports orphaned-document counts — a useful KB-health signal.
- The CLI rebuilds the graph per call; the `iwes`/`iwec` server keeps it warm for
  repeated queries.

## Search index — setup, performance & tuning (deployment reality)

> The sections above describe the original terminal-wrapper design. In a deployment that
> exposes the **`okengine` read MCP** (`okengine-mcp/server.py`), the agent reaches qmd/IWE
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

**Chat latency knobs (reader → agent).** A chat answer is a multi-turn agent loop over the
(remote) model — each tool decision is a model round-trip — so latency = turns × (model +
tool). Tune via: the search default (lexical, above); the MCP tool output caps in `server.py`
(`search` 8k, `retrieve_context` 16k — smaller = cheaper prompts); the reader's
`OKENGINE_READER_CHAT_MAX_MSGS` / `_CHAT_MAX_CHARS`; and the model/provider. The reader's
grounding prompt makes the agent **acknowledge before calling tools** so the wait is visible,
not a blank stream.

## Architecture / persistence (code in image, data on the volume)

- **Binaries ship in the image** (`Dockerfile`): `npm i -g @tobilu/qmd` + the IWE
  release binaries (`iwe`/`iwes`/`iwec`) to `/usr/local/bin`. Survive rebuild +
  recreate; reproducible on a fresh deploy.
- **Data lives on the `/opt/data` volume** (`~/.hermes`), NOT in the image:
  - qmd index + ~2 GB GGUF models → `/opt/data/qmd/` (`XDG_CACHE_HOME`/`XDG_CONFIG_HOME`).
    Set once; a recreate does not re-download or re-index.
  - IWE config → `wiki/.iwe/config.toml` (`wiki_link_path = "preserve"`; tracked).
- The wrappers set the env (qmd: `XDG_*` + `QMD_FORCE_CPU=1`; IWE: project root =
  `wiki/`) so callers don't have to.

## Why wrappers, not MCP (for now)

Both tools ship MCP servers (`qmd mcp`, `iwec`), but wrappers were chosen first:
1. **`iwec` is fragile on this corpus** — it panics (Rust) parsing markdown with
   embedded HTML when run from the wrong root (it scanned the Hermes install's
   `node_modules/react-colorful`). The **CLI is stable** when scoped to `wiki/`.
2. **qmd-MCP cold-start** loads ~2 GB of models per stdio spawn; cron is
   subprocess-per-job, so every job would pay it. The wrapper uses the warm index.
3. **Pattern match** — the project already exposes tools to the agent via terminal
   wrappers (`np_intelligence_query.py`).

MCP is a viable later enhancement: run `qmd mcp --http --daemon` (warm) + a
`wiki/`-scoped `iwec` as long-lived servers and register them in `config.yaml`
`mcp_servers`. Deferred until the warm-daemon + iwec-robustness story is worth it.

## UI integration — okengine-reader backlinks panel (IWE)

The reader UI shows a **"↩ Backlinks — what links here"** panel on every wiki
page opened in the overlay (entities/concepts/sources/predictions), powered by the
IWE graph — each backlink clickable to navigate.

Design (keeps the reader **standalone** — no hermes coupling):
- The reader ships its own `iwe` binary (`okengine-reader/Dockerfile`). Base bumped
  to `python:3.13-slim-trixie` because the IWE binary needs GLIBC ≥ 2.39
  (bookworm-slim has 2.36). wkhtmltopdf isn't in trixie, so PDF export moved to
  **weasyprint** (md/docx via pandoc unchanged).
- `GET /api/backlinks?path=<key>` builds the full backlink map ONCE per hour
  (`iwe find -f json -l 0` → invert), caches it, and serves per-page lookups
  instantly. A **startup prewarm thread** builds it off the request path; a
  single-flight lock prevents concurrent builds.
- Reader resource limits are sized to host the periodic build off the request path.
- Read-only safe: IWE writes nothing to `.iwe/`, so it works on the `/vault:ro`
  mount. Requires `wiki/.iwe/config.toml` (tracked in the vault repo).

The qmd semantic search bar (replacing the ripgrep `/api/search`) is the planned
next step — it needs the warm qmd HTTP daemon (see MCP upgrade path above).

## Maintenance

- Lexical/FTS refresh is **automatic** — the mcp server self-registers the collection and
  runs incremental `qmd update` on a timer (`OKENGINE_MCP_INDEX_REFRESH_HOURS`, default 6).
  Vector `qmd embed` is still manual (heavy) — run it mcp-side off-hours if you want hybrid.
  See [Search index — setup, performance & tuning](#search-index--setup-performance--tuning-deployment-reality).
- IWE is stateless (rebuilds the graph per call) — no refresh needed.
- Bump `IWE_VERSION` in the `Dockerfile` to upgrade IWE; `npm i -g @tobilu/qmd@<v>`
  for qmd.
