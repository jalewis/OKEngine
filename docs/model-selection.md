# Choosing a model per lane

OKEngine runs many agent lanes (ingest/compile, entity extraction, prediction grading,
briefs, plus any enabled extensions). They have very different needs, and you can point each
at a different model — see [the two-layer chain](#setting-it). This is the guidance for
*which* model, by **task profile** rather than by brand (names churn — flash / lite / mini /
pro / vendor versions — but the profiles are stable).

## Match the model to the work

| Profile | What the lane needs | Model class | OKEngine lanes (examples) |
|---|---|---|---|
| **Deterministic** | nothing — it's a script | **no model at all** (`no_agent`) | `contradictions`, the `*-drain` / `*-backfill` selectors, lint/repair scripts |
| **Lightweight + tools** | short context, simple judgment, must call write tools | **small / fast / cheap** — a flash/lite/mini, a `:free` tier, or a local model | `glossary` (define a term), frontmatter repairs, classify drains |
| **High-volume bulk** | moderate context + judgment, runs over **many** items | **economical mid** (flash / standard) — cost-per-token dominates because volume is high | compile / ingest (raw → source pages), entity extraction + backfill |
| **Reasoning ("thinking")** | careful judgment, few items, high stakes | a **reasoning / "pro" / thinking** model — worth the cost | `predictions:grade` / `regrade`, page-quality + wiki-health audits, canonical-fusion conflict resolution |
| **Large context** | reads many pages and synthesizes | **large-context + capable** | `brief` / `digest` / `weekly-brief`, wide-vault audits |

## Where the money goes (so you optimize the right thing)

- **Bulk ingest/compile is your volume** — most tokens flow through it. Optimize it for
  **cost** (a fast/economical model, or a local one if you have the hardware), not peak
  capability. A small quality gain here costs a lot at scale.
- **Reasoning lanes are low-volume, high-value** — a wrong prediction grade or a missed
  contradiction is worse than a slow one. **Spend here**; they run on few items.
- **Deterministic lanes cost nothing** — keep heavy mechanical work in `no_agent` scripts
  (selection, dedup, dashboards) and only wake the agent for genuine judgment.
- **Lightweight lanes** (glossary, repairs) → a free/cheap model; a rate-limited `:free`
  tier is fine for a daily, low-volume lane.

A reasonable starting allocation: **default = your economical workhorse** — it covers the
bulk *and* lightweight lanes (a lightweight lane like glossary is perfectly happy on it, no
override needed) — and **override the few reasoning lanes *up*** (predictions grading,
audits). If your default is instead a premium model, also override the bulk/lightweight lanes
*down*. Either way you annotate only the exceptions; the default carries the majority.

## Watch out: a too-weak model "completes" but writes nothing

The most confusing failure mode in practice is **silent**. A synthesis-and-write lane — a brief, a
competitor quadrant, any lane that reads context and then must **write a page** — runs on an
under-powered model; the model reads and reasons correctly, often even narrates *"now I'll write the
brief…"*, and then the turn ends **without emitting the write tool call**. The scheduler logs
`completed successfully` (the agent finished its turn), but **no page is produced and no error is
raised**. (Observed with a local `qwen3.5:27b` on the daily-brief and competitor-quadrant lanes: the
brief silently went missing for days; the quadrant once wrote to the wrong namespace.)

Symptoms:
- A daily/weekly artifact (brief, dashboard) is simply **missing for the day**, with nothing failing
  in the logs.
- The lane log shows reads (`read_file` / `get_page` / `search`) but **zero write-tool calls** —
  `grep -cE 'tool (create_entity|update_entity|file_write|append)' <lane-log>` → `0` — and a short
  final message ending mid-intent (*"Let me create it…"*).

Cause: smaller / local models are reliable at *reading and reasoning* but unreliable at the multi-step
**"read N things, then emit a structured tool call to write"** flow — they regress to answering in
prose instead of calling the tool.

Fix: route **synthesis-and-write** lanes to a **capable, reliable-tool-calling** model (a
reasoning/"pro" tier) via a per-lane override, even when `model.default` is a cheaper local model.
These lanes are low-volume, so the extra cost is small. The rule of thumb: **don't judge a write lane
by "did it complete" — judge it by "did the page appear."**

```json
// <pack>/.okengine/cron-models.json  — route the write-heavy synthesis lanes up
{ "okpack-<x>-daily-brief": "@deepseek", "okpack-<x>-weekly-brief": "@deepseek" }
```

## Setting it

Two layers — finest wins (see [docs/authoring-an-extension.md](authoring-an-extension.md) §6
and the per-op `model:` key in [extension-system.md §6](design/extension-system.md)):

1. **Per lane** — `model:` on the operation (extension) or the job def (engine/pack cron).
   Multi-op extensions can set it per lane.
2. **Deployment default + fallback** — `config.yaml` `model.default` + `fallback_providers:`.
   Every lane that doesn't override inherits this.

```yaml
# extension operation (or an engine/pack cron job def)
operation:
  prompt_file: prompts/grade.md
  model: <your reasoning-tier model>   # a reasoning lane -> spend; omit to inherit the default
```

## Named profiles — switch host + ctx per lane (okengine#151)

A per-lane `model:` is just a model *name*, which resolves against the default provider's host —
so it can't point one lane at a different ollama host, and an extension lane (which carries only
a model string) can't carry a `base_url`/`ctx` at all. **Model profiles** close that: name a full
endpoint once, reference it everywhere with an `@`-sigil.

```yaml
# <pack>/.okengine/model-profiles.yaml   (operator tier, like extension-prompts.json)
profiles:
  reasoning: {provider: custom, base_url: http://host-a:11436/v1, model: qwen3.5:27b, ollama_num_ctx: 65536}
  bulk:      {provider: custom, base_url: http://host-b:11436/v1, model: qwen3.5:9b,  ollama_num_ctx: 65536}
```

Any lane then references the profile by name — `model: "@reasoning"` — in an extension manifest,
an engine/pack cron def, or the operator override map
`<pack>/.okengine/extension-models.json` (`{job_name: "@profile" | "literal-model"}`, which routes
**extension** lanes without forking the manifest). At deploy, `@`-refs are expanded into the
concrete `model`/`provider`/`base_url`(/`ollama_num_ctx`) fields the scheduler forwards to the
agent — the same deploy-only transform as `@jitter`, so the committed `cron-plus-jobs.json` keeps
the `@`-refs and stays round-trippable.

- A **bare** model string (`qwen3.5:9b`, `openai/gpt-oss-120b:free`) is a literal — passed through
  unchanged. The `@` sigil is what marks a profile reference.
- An `@`-ref to an undefined profile **fails the deploy** (and `framework validate`) — a typo'd
  reference never silently falls back to the default model.
- `ollama_num_ctx` in a profile is honored per-lane only with the companion Hermes patch
  (okengine#151 Suggestion 2b); without it, a lane on a local-endpoint profile still gets ctx via
  the agent's auto-detect (capped to `context_length`), just not an explicit per-lane cap.

## On specific models

**Pick by the profile, not the brand** — model names and tiers churn, and we don't want this
doc to rot. So this guide names *capabilities*, never products. Concrete model ids live in
exactly one place: the deployment's `config.yaml` (`model.default` + `fallback_providers:`),
which you update when your providers change — the profiles above don't move.

Most vendors offer the tiers these profiles map to, under whatever names are current:

- a **small/fast** tier — often branded "flash", "lite", "mini", or a free/rate-limited tier →
  *lightweight + bulk* lanes;
- a stronger **reasoning/"pro"** tier → *reasoning* lanes;
- **large-context** variants → *brief/digest* lanes.

A **local** OpenAI-compatible model (e.g. via Ollama) is usually cheapest for high-volume bulk
if you have the hardware. Whatever you pick must be reachable by a configured provider (its key
in the deployment `.env`); the `fallback_providers:` chain catches a provider that errors or
rate-limits.

## Running on free models

Free/`:free` tiers cost nothing but are **rate-limited** (requests-per-minute, tokens-per-day).
A 40-lane fleet that lets coincident lanes fire at once will stampede a free tier into `429`s.
The engine ships **free-first** (a free Nemotron default + an all-`:free` fallback chain), which
is only usable with the survival levers below — the trade is **throughput for completion**:
lanes run more serially and slower, but finish instead of dying in a rate-limit storm.

- **Cap concurrency** — `HERMES_CRON_MAX_PARALLEL` (gateway env; skeleton ships `2`). Set `1`
  for the most rate-limit-tolerant (fully serial). This is the biggest lever.
- **Keep the *whole* chain free** — `model.default` AND every `fallback_providers` entry on a
  `:free` tier (or `openrouter/free`, the **Free Models Router**, which routes to any available
  free model — the free counterpart of `openrouter/auto`; never use `openrouter/auto` itself, it
  can route to a paid model). For a *paid escape hatch* when every free tier is rate-limited, end
  the chain with **one explicit paid model** (the engine default uses `deepseek-v4-pro`). Drop
  the paid entry entirely for a strictly-free deployment.
- **Spread the schedules** — pack crons use `@jitter:` sentinels so they don't all coincide.
- **Cut the call volume** — prefer `no_agent` deterministic lanes, batch work, and keep the
  auxiliary side-tasks on a cheap funded model (see above) so compression isn't on the hot path.
- **Busy fleet? Go local.** Free *tiers* can carry *light* lanes but not the bulk of a large
  vault. A local OpenAI-compatible model (e.g. Ollama) has **no rate limits** — slower per call,
  but unlimited — making it the real free workhorse for `model.default` if you have the hardware.

**Verify it's working:** `scripts/fleet-status.sh` surfaces rate-limit hits and the *live* model
distribution — if the free primary's share is low and 429s are high, lower `MAX_PARALLEL`,
confirm the chain is all-`:free`, or move the workhorse to a local model.

**Track it over time:** the `usage-rollup` cron (hourly, no_agent) persists per-model call counts
+ free/paid into a SQLite ledger (`<data>/metrics/usage.db`) from the same `model=` logs — the
long-term record fleet-status can't keep once logs rotate. It's idempotent and per-vault by
construction (correct even with a shared API key). Read the trend with
`python scripts/cron/usage_rollup.py report <data-dir> [days]` — daily call volume + the
**`free%` (cost offload)** + top models. Captures *counts*, not tokens/$ (the agent logs no token
counts); for per-vault `$` attribution, give each vault its own key (okengine#144).
