# okengine.relevance-gate

Consumer-side ingest **scope filtering** (okengine#167): keep off-thesis material from polluting a
vault fed by a broad producer (a feed, a service, an importer) — **without** filtering at the
producer, and **without** deleting anything.

## The principle

Relevance is a **consumer** decision. Filtering at the source bakes one consumer's taste into a
shared producer; the next consumer that wants that signal can't get it. So the mechanism ships
here (generic), and the boundary is **pack/operator config** the gate merely applies:

```yaml
# in the pack's schema.yaml
pack_config:
  scope:
    statement: <one sentence: what this vault tracks>
    in_scope:  [<phrases>]
    out_of_scope: [<phrases>]
    on_uncertain: keep        # the contract — the gate never guess-flags
```

No scope declared → both lanes **no-op loudly**. The gate never invents a boundary.

## The two lanes (both no_agent, both flag-not-delete)

| lane | schedule | does |
|---|---|---|
| `scope-prescore` | `10 5 * * *` | deterministic term pass from the scope lists: flags **clear** out-of-scope sources (`off_scope: true` + which terms matched), leaves the ambiguous middle → `dashboards/scope-audit.md` |
| `scope-classify` | `25 5 * * *` | the ambiguous middle through the **vendored `llm_lib`** (reasoning-off default — a cheap qwen-class model is enough): the model labels in/out/uncertain, **this script holds the pen** (propose/dispose); uncertain → **kept** → `dashboards/scope-classify.md` |

`scope-classify` needs `OKENGINE_LLM_BASE_URL` + `OKENGINE_LLM_MODEL` in the deployment `.env`;
absent, it no-ops loudly and prescore still covers the clear cases. **Also raise the cron script
timeout** — Hermes kills no_agent scripts at 120s by default, and a dozen model calls under load
won't fit: set `HERMES_CRON_SCRIPT_TIMEOUT=600` in `.env` (gateway recreate to apply). The lanes
hand off through `wiki/.scope-queue.json` (prescore writes the ambiguous list; classify pops up to
`SCOPE_CLASSIFY_BATCH` per run and re-queues transient errors) — classify never re-scans the
corpus.

Flags are **reversible frontmatter markers** (`off_scope: true`, `scope_reason:`) — the same
convention the manual scope pass established. Nothing is deleted; downstream selectors that honor
`off_scope` (e.g. question-corpus triggers) deprioritize flagged pages. The dashboards are the
point: the boundary becomes a **visible, tunable dial** — the operator watches what got flagged
and edits the one scope config, instead of fighting pages.

Err-toward-keep is structural: in-terms always beat out-terms, both-sides terms count as in,
model-uncertain is kept, and a backlog pass is explicit (`SCOPE_LOOKBACK_DAYS=0`), never implied.

## Enable

```
framework extensions enable <pack> okengine.relevance-gate
# set OKENGINE_LLM_BASE_URL/OKENGINE_LLM_MODEL in .env for the classify half; redeploy
```
