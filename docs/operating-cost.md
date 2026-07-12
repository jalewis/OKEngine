# Operating cost — what OKEngine spends and when

> **⚠️ This is a 24/7 autonomous agent. Once you give it feeds, it makes LLM API
> calls continuously, on its own, forever — that can cost real money.** Read this
> before you populate `feeds.opml`. The numbers below are **order-of-magnitude
> estimates**, not a quote; your bill = (LLM calls) × (your model's per-token
> price), and both vary a lot.

## TL;DR

- **Out of the box (empty `feeds.opml`) it is ~free.** Every ingest cron is
  *wake-gated*: with no feeds there's nothing to compile, so the agent almost never
  fires. The only guaranteed LLM work is one daily brief (trivial on an empty vault).
- **Cost is driven by feed VOLUME, not the cron schedule.** The `*/5` schedules you
  see are cheap *gate scripts* that check for work; the expensive part (the agent)
  only runs when there's something to ingest.
- **The model is yours to choose, and free/local options exist.** `config.yaml`
  ships choices from `gpt-oss-120b:free` (free, rate-limited) and local Ollama
  (`$0` + electricity) up to paid APIs. Your model choice dominates the bill.
- **There is a one-time spike when you first add feeds** — the initial backfill
  ingests each feed's available history at once (one deployment pulled ~280 items
  on day one).

## What costs money (and what doesn't)

| Vector | Cost? | Notes |
|---|---|---|
| **Model / LLM API** | **YES — the only real recurring cost** | Operator-configured. `$0` on local Ollama or a free tier; scales with feed volume on a paid API. |
| Retrieval / search (`qmd`) | No (local) | BM25 + **local** vector + rerank models. No embedding API. Uses CPU/RAM. |
| Web search/fetch | No | `web_search`/`web_extract` are **disabled** in every ingest prompt (`LOCAL-ONLY`) — no paid web-API spend from crons. |
| Feed fetching | No | `feed-fetch` is a pure script (`no_agent`) — plain HTTP GETs of RSS. |
| Delivery (Telegram) | No | Free API. |
| The other 17 maintenance crons | No | Index/health/tier/repair/reshelve are `no_agent` scripts. |
| Hosting | Indirect | The gateway + reader + mcp containers run 24/7 (server/electricity), and any local model inference uses your CPU/GPU. |
| **Interactive agent (Telegram chat)** | **YES, if used** | Every message you send the gateway agent is an LLM session, and it *can* use paid web tools if you enable them. Separate from the cron fleet below. |

## The cron fleet, by cost

Counts below are the ENGINE-ONLY baseline (`config/engine-crons.json` at engine v0.11.x) — NOT an
engine invariant: a composed deployment ADDS pack + extension jobs, so a live fleet is larger (e.g.
the okcti bundle runs ~90+). Re-derive per deployment from its `cron-plus-jobs.json`; treat this as
the engine floor, not the total.

**53 engine jobs. 34 are free `no_agent` scripts** (feed-fetch, reshelve, index/hot-set/tier
builders, YAML/schema/frontmatter repair drains, health refreshers) — they never call the LLM.
**19 invoke the agent**, and most are wake-gated (a cheap script decides each tick whether there's
work; the agent fires only if so). Only the **daily brief** fires unconditionally (once/day).

So the schedule frequency is an **upper bound on gate checks**, not on LLM calls.
`raw-backfill` ticks every 5 min but only wakes the agent when raw items are waiting,
and then it processes up to **30 items per session**.

## Estimation model (state your own assumptions)

The honest driver is **new items ingested per day**. Per item, the agent compiles a
source page + extracts/links a few entities — roughly **~8 LLM calls/item** (range
5–12; each tool round-trip is a call). Downstream passes (source scoring, concept
synthesis, thin-page enrichment, type classification) add **~40%**. A fixed daily
floor — the brief plus gated maintenance that wakes on drift — is **~50 calls/day**.

> Assumptions: ~8 calls/item, +40% downstream, ~50/day floor, ~8–15k tokens/call
> (persona + schema + item context is input-heavy). **Change these for your model
> and feeds — they swing the result several-fold.**

### Estimated LLM calls

| Scenario | New items/day | **Calls/day** | Calls/week | Calls/month |
|---|---|---|---|---|
| **Default — feeds empty** | 0 | **~5** (brief only) | ~35 | ~150 |
| **Light** — a few feeds | ~15 | **~250** | ~1,700 | ~7,500 |
| **Active** — full 19-feed sec list | ~60 | **~750** | ~5,300 | ~23,000 |
| **Heavy** — busy news week | ~150 | **~1,800** | ~12,500 | ~54,000 |
| *One-time: initial backfill* | ~280 at once | *~2,200 calls over day 1–2* | — | — |

### Turning calls into a bill

At ~10k tokens/call, **Active** ≈ ~23k calls/month ≈ **~230M tokens/month**. Then:

- **Local Ollama / free tier:** ~**$0** (you pay in CPU/GPU + electricity, and free
  tiers are rate-limited so ingest just runs slower).
- **Cheap hosted model** (~$0.30–0.60 / M blended): Active ≈ **~$70–140/month**.
- **Mid-tier API** (~$2–5 / M): Active ≈ **~$500–1,100/month**.
- **Frontier model:** materially more — not recommended for the bulk ingest lane.

These are deliberately coarse. **Run light first, watch your provider dashboard for a
few days, then scale feeds.**

## Controlling cost

- **Stay on empty feeds** until you're ready — the engine maintains itself for ~free.
- **Add feeds gradually**; cost tracks volume linearly.
- **Pick a cheap or local model** for the ingest lane (`config.yaml`). Local Ollama or
  a free tier makes the whole fleet `$0`.
- **Thin the fleet** if you don't need everything: the enrichment/scoring/concept
  crons (`page-quality-enrich`, `source-quality-backfill`, `concept-backfill`) are the
  optional downstream passes — disable them in `crons/` to cut the +40%.
- **Use the built-in spend cap (`budget-guard`).** OKEngine ships a `no_agent`
  cron that reads the runtime's own token tally and **auto-pauses the cost-bearing
  (agent) crons when usage over a rolling window crosses your budget** — the free
  maintenance scripts keep running, so the vault stays healthy while ingest is
  throttled. It's **opt-in / off by default**; enable it in `.env`:

  ```sh
  OKENGINE_BUDGET_TOKENS=50000000        # pause when >50M tokens in the window
  OKENGINE_BUDGET_WINDOW=day             # day | week | month (rolling)
  OKENGINE_BUDGET_RESUME=auto            # auto (resume as usage ages out) | manual
  # optional USD estimate (you supply the blended price; we don't guess):
  # OKENGINE_BUDGET_USD=20  OKENGINE_BUDGET_PRICE_PER_MTOK=0.40
  ```

  It is a backstop, not a substitute for a hard cap **at your provider** — still set
  one there. Note: the token *count* is dominated by cache-read tokens (the agent
  re-reads context each tool call); those are usually cheaper per token, so weight
  your `PRICE_PER_MTOK` accordingly. After a day of ingest, run the guard once to see
  your real number — it prints `usage … tok/<window>`.
