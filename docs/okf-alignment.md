# OKF Alignment — OKEngine

**Status:** reference notes

This captures OKEngine's relationship to the **Open Knowledge Format (OKF)**,
its conformance status, and the retrieval/graph tooling it uses. It also makes
the origin story explicit: OKEngine was sparked by Karpathy's LLM-wiki idea; OKF
is useful validation and compatibility, not the central catalyst.

## Origin story

OKEngine was sparked by Andrej Karpathy's "LLM wiki" idea: give an agent a
durable markdown wiki so knowledge accumulates instead of being rediscovered on
every query. The project goal is an engine that can create and maintain those LLM
wikis for swappable topics.

The first concrete domain was security: a security-focused wiki format/pack.
Google's OKF arrived as useful external validation of the same general direction
and as a very small, high-level interoperability spec. Because OKF requires so
little, OKEngine can support it cheaply. OKF is therefore a compatibility layer
and validation reference, not the center of OKEngine.

---

## What OKF is

The **[Google Open Knowledge Format](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing) (OKF)**
is Google's minimal markdown + YAML convention for sharing structured context
with AI agents. It overlaps naturally with OKEngine because both use markdown
pages, YAML frontmatter, and graph links:

- **Lineage:** Andrej Karpathy's "LLM wiki" note ([gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), 2026-04-04) is the *pattern* that sparked OKEngine. **Google's Open Knowledge Format is a named, minimal *format*** that later validated the direction and gives OKEngine a cheap compatibility floor. OKEngine is an LLM-wiki engine with OKF-compatible validation; okpack-cti is the first domain profile.
- **Form:** a directory of markdown files with YAML frontmatter, linked into a graph; reserved `index.md` + `log.md`; an `AGENTS.md`-style agent contract.
- **Required field:** `type` (the *only* mandatory field).
- **Conformance unit:** per-page conformance to the nearest `schema.yaml`.
- **Common optional fields:** `title, description, tags, timestamp, resource`.

OKEngine builds on a pinned Hermes-Agent (consumed as a dependency, not forked)
and maintains typed markdown+YAML wikis. That makes it naturally OKF-compatible,
but the security wiki format is an OKEngine domain profile first; OKF support is
deliberately small and mostly a matter of preserving the `type` floor and portable
projection.

---

## Tools — in use / evaluated

| Tool | What | Stars / pushed | Relevance |
|---|---|---|---|
| **qmd** (`tobi/qmd`) | local hybrid BM25 + vector markdown search, CLI + MCP, all on-device | — | Ranked-search layer (concept queries over narratives) |
| **IWE** (`iwe-org/iwe`) | markdown knowledge-graph; LSP + CLI + MCP; backlinks, graph export, bulk ops | — | Agent-accessible graph layer (alt to bespoke reader tooling) |
| **Obsidian** | the vault's editor; Dataview + graph view | — | Current human browse/review layer |
| **okengine-reader** (this repo) | standalone read-only web reader over the vault | — | Current custom reader; domain-neutral |

**Both qmd and IWE are wired in** — agent-accessible via the
`kb_search.py` (qmd hybrid search) and `kb_graph.py` (IWE wikilink graph) terminal
wrappers; binaries baked into the image, models/index on the `/opt/data` volume.
See [`kb-tooling.md`](kb-tooling.md) for the design.

---

## A note on the "OKF" acronym

"OKF" here means **Google's [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog)**
(the agent-wiki format, 2026). A bare search for "okf" is otherwise dominated by unrelated
projects that share the acronym — most prominently the **Open Knowledge *Foundation*** (`okfn`),
an open-data nonprofit, and `helpfulengineering/OKF-Schema` (Open *Know-how* Framework, for
hardware docs) — neither related to Google's format. When searching for prior art on the
underlying pattern, search **"LLM wiki" / "agent-maintained wiki"**, not the bare "okf" acronym.

---

## Canonical references

| Resource | URL | Verified |
|---|---|---|
| **Google Open Knowledge Format** — announcement (the named format) | https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing | ✅ real (2026-06-12) |
| OKF v0.1 spec (`GoogleCloudPlatform/knowledge-catalog`) | https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md | ✅ real |
| Karpathy "LLM wiki" gist (the origin pattern) | https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f | ✅ real (2026-04-04) |
| LlamaIndex — "Is Grep All You Need?" | https://www.llamaindex.ai/blog/is-grep-all-you-need-lexical-vs-sematic-search-for-agents | ✅ real (2026-05-26) — **argues grep degrades at enterprise scale; advocates layered semantic+lexical** (i.e. a counterpoint to pure-grep, not support) |
| qmd | https://github.com/tobi/qmd | ✅ |
| IWE | https://github.com/iwe-org/iwe | ✅ |

---

## Caveats

- OKEngine uses `[[wikilinks]]` and Dataview `index.md` — Obsidian-native, **not** portable plain-markdown OKF. Conformance for *consumption by other tools* needs an export projection; it is not required for the system to run.
