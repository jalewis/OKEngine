# Migrating a local-model stack to llama.cpp — findings for other teams

Written after moving an agentic workload off ollama. Everything here was **measured**, not
inferred, and most of it cost real debugging time to establish. It is deliberately
project-agnostic: if you run agents against a local model, this applies to you whether or not
you use OKEngine.

The OKEngine-specific wiring lives in [local-model-serving.md](local-model-serving.md). This
document is the part worth sending to another team.

---

## 1. If your tool calls arrive as prose, suspect the server before the model

**Symptom.** The model "answers" instead of calling a tool. You see the call as text in
`message.content` with `tool_calls: null` — bare `{"name": ..., "arguments": {...}}`, or wrapped
in a `<tools>` tag, or `<function=...><parameter=...>` XML.

**Cause.** Some GGUF builds ship **no chat template**. ollama substitutes a generic one; if the
model was trained to emit a different tool-call format, ollama's parser does not recognise what
comes back and drops it.

**Measurement.** Same GGUF blob, same GPU, same host, same request body — only the serving
process differing:

| cell | ollama | llama.cpp `--jinja` |
|---|---|---|
| captured production payload (31 real tools, ~25k tokens) | **0/8** | **7/8** |
| trivial messages + the same 31 tools (~8k tokens) | **1/6** | **6/6** |
| same payload, streaming | — | **5/6** |

`--jinja` is not optional. Without it you get the same class of failure you are migrating away
from.

**Do not conclude "the model can't tool-call."** We nearly swapped models over this. The model
was fine.

## 2. Capture the real wire payload — reconstructions lie

This is the single most expensive lesson. A synthetic harness with short tool names and trivial
schemas ran **~1,100 consecutive clean tool calls** on the same model that failed 1-in-6 in
production. Three separate conclusions were drawn from reconstructions and all three had to be
withdrawn:

- "prompt size drives it" — falsified by a 7.3k-token payload that failed while 25k succeeded
- "tool count is eliminated as a factor" — the padding used short synthetic names
- "the failure rate is 71%" — an artefact of one probe shape, projecting onto nothing

Put a forwarding proxy between your client and the inference host, record the exact JSON body of
a failing request, and replay **that**. Redact message *text* with same-length filler if you must;
leave the `tools` array byte-identical, because names, descriptions and schemas are part of what
is being tested.

## 3. Do not "fix" the capability-probe 404s by rewriting the paths

Clients that support several local servers probe a **capability ladder** on startup:

```
/api/v1/models   → LM Studio's native API
/api/tags        → ollama  (must return {"models": [...]})
/v1/props        → llama.cpp  (fall back to /props on older builds)
/version         → vLLM
```

Against any one server, every rung but the matching one **necessarily 404s**. That is the ladder
working, not a bug, and the 404s are how it discriminates. We were sent a table of "correct"
paths that pointed all of them at `/v1/models`; applying it would have made ollama and llama.cpp
indistinguishable — and on a load balancer that routes `/api/tags` to the ollama pool, it would
have broken routing outright.

**The real defect is the rate, not the paths.** If the probe is uncached, every client/session
init re-walks the ladder. One fleet generated ~150 failed probes/hour, arriving as bursts of 4
within ~40 ms per lane start. Server type cannot change without a restart — **memoise it per
process, keyed on the normalised base URL, and cache the negative result too.**

## 4. `max_tokens` equal to the context window is a trap

> **Update after acting on this.** We capped it at 8,192 (a turn emits 30–1,200 output tokens in
> practice) and the failure class disappeared. Before the cap, raising a lane's batch size killed a
> run outright — `Response truncated due to output length limit`, **after 19 successful writes**,
> all discarded. If your client derives `max_tokens` from the context window, this is not a
> theoretical tidy-up: it is the ceiling on how much work one turn can do.

A client that sends `max_tokens: 65536` is authorising a single generation of tens of thousands
of tokens. On a server that honours it, one turn ran **~42,000 output tokens over 14 minutes** and
was then truncated mid-tool-call, corrupting the call's arguments and losing the work.

ollama masked this by stopping early (badly, but early). Cap `max_tokens` to what a turn actually
needs. A value that was harmless on one server can run away on another.

## 5. Over-length prompts: a loud 400 replaces silent truncation

**ollama silently truncated.** The model saw less than you sent, nothing recorded it, and the
output was subtly wrong. A workload can sit in that state for weeks. The tell in old logs is a
run reporting a suspiciously round *halved* input size.

**llama.cpp returns a structured 400 instead:**

```json
{"error": {"code": 400, "type": "exceed_context_size_error",
           "message": "request (72016 tokens) exceeds the available context size (65536 tokens)",
           "n_prompt_tokens": 72016, "n_ctx": 65536}}
```

Two consequences:

- **These 400s are usually not breakage.** A client with context-overflow handling classifies
  them, compresses the conversation and retries. Observed end to end: a 400 at 65,650 tokens,
  compression from 117→113 messages, then the run completing 46 tool turns. Anyone grepping logs
  for `400` after the migration will otherwise raise a false alarm.
- **Reserve headroom.** Most overflows we saw were *just* over the line. Setting the client's
  advertised context ~2–3k below the server's real `-c` value converts a 400 plus a compression
  round-trip into just a compression.

Also note compression is bounded, and it can **no-op**: we observed 5 of 7 compression passes
returning the same message count (tokens re-estimated, nothing shed) at ~51s each. The failure to
alert on is "max compression attempts reached", not the 400.

## 6. Server flags that change your capacity math

- **Context is fixed at launch.** `-c` at startup is the real setting; per-request `num_ctx`
  (an ollama-ism) is ignored.
- **`--parallel N` divides `-c` across slots.** `-c 131072 --parallel 2` is **65,536 per slot**,
  not 131,072.
- **`--parallel 1` serialises your whole workload.** One long generation blocks every other
  client behind it. We saw an ordinary job wait 40s for a slot.
- **Check `kv_unified` on the machine you will actually run on.** It is reported at startup and
  differs by configuration; a CPU-run observation did not carry over to GPU for us.
- **Fit:** a 30B-class model at `-c 65536` with a q8_0 KV cache sat around 21 GB — comfortable on
  a 24 GB card at `--parallel 1`, not with more slots.
- llama.cpp can read ollama's blob store directly (`-m /blobs/sha256-<digest>`), so you can serve
  the exact weights ollama already downloaded without a second copy on disk.

## 7. llama.cpp serves `/v1/*` — not ollama's native API

`/api/generate` and `/api/chat` are not served. Any other consumer on those endpoints must stay on
ollama or migrate too. That is the main argument for **splitting by GPU** rather than replacing
outright.

> **Update — the alias is transitional, not permanent.** It exists to let two serving stacks
> coexist behind one URL. Once the old stack is retired and every card runs llama.cpp, the LB can
> serve the plain model name to it and the suffixed alias becomes dead weight — we retired ours
> and reverted to the plain name. Treat the alias as a **migration tool with an exit**, and plan
> to remove it, or you leave a naming quirk in every consumer's config forever.

The cleanest wiring, if you already front the host with a proxy, is a **model-name alias** rather
than a second endpoint:

```
<model>          -> ollama     (consumers that need /api/*)
<model>-tools    -> llama.cpp  (agentic lanes, /v1/* + tools)
```

Both names serve the same weights; the suffix is purely a routing key. Selecting a stack becomes a
model-name change, which every client already supports — and it gives you a clean A/B where the
endpoint is held constant and only the name varies. Route on the model header and **refuse
unlisted names** rather than silently serving something else.

## 8. Quantised KV cache is not free — measure it, and measure it *before*

Dropping the KV cache from q8_0 to q4_0 buys concurrency. It is usually described as
near-lossless; treat that as a hypothesis about *your* workload.

The failure mode to look for is **corrupted verbatim recall of high-entropy tokens deep in a long
context** — hashes, CVE ids, version strings, proper nouns — because KV precision is bought
against exactly the long-range attention that verbatim recall uses. One wrong character there is a
silent data error, not a visible failure.

We measured no regression (60/60 exact across ~2k/16k/48k contexts, all depths) — but with
important limits: small n, single-turn, and **no baseline from the same harness on the previous
configuration**, because that configuration no longer existed by the time we thought to measure.

**Take the baseline before the change.** Afterwards it cannot be recovered. If both stacks can run
side by side — two cards, two ports, one setting different — that A/B is worth more than either
number alone.

## 9. Things that will look like model failures and are not

Ranked by how much time they cost us:

1. **A receipt/verification layer that reads a path the writer rewrote.** Our write path shards a
   created page to a canonical location; the model reported the path it *requested*; the verifier
   read that literally, found nothing, and failed the run. **14 correct writes were discarded as a
   failure.** If you have any post-write path normalisation, make the verifier resolve through the
   same function — or better, take the path from the writer's own return value.
2. **A contract declared but never implemented.** Six of our lanes demanded a per-item receipt
   against a manifest their selectors never wrote — every run failed, permanently, with an error
   that reads like the model misbehaving. Validate that two-sided contracts are actually
   two-sided.
3. **Feeding the model unwritable work and grading it on the output.** Hand an extraction agent
   three vendor blog posts and zero writes is the *correct* answer. Measure "inputs correctly
   dispositioned", not "writes produced".
4. **Instructions in the wrong position.** Rules injected into the *user* message compete with the
   payload for attention and cannot reliably enforce tool calls; system-prompt placement carries
   authority that user-message injection does not. We wrote two prompt directives before finding
   this documented upstream. Search the issue tracker of whatever agent framework you use before
   writing the third.

## 10. MCP gotchas — where several of these failures actually lived

If your agents reach tools over MCP, most of the surprises above have an MCP-shaped cause.

### Tool results are wrapped — never parse them with an anchored pattern

An MCP result comes back as text **parts** (`{"type": "text", "text": "..."}`), so the payload you
care about is rarely at offset 0. We shipped a telemetry parser using an anchored `re.match`; it
matched nothing, produced an empty result set, and a downstream consumer read "empty" as "the run
wrote nothing" and rejected **28 genuinely-written pages**. Use `search`, and treat an empty parse
as *unknown*, never as *nothing happened*.

### A rejected write is still a "completed" tool call

The write server returns refusals as ordinary tool results. Counting completed calls therefore
**overstates** success — `tool ...create_entity completed (0.01s, 223 chars)` is equally a creation
and a `refused: ... already exists`. If you need a success count, parse the result payload; if you
need a durable record, see the next item.

### Make write tools return the CANONICAL path, and have consumers use it

Our write path re-shards a created page to its schema-canonical location, so
`entities/acme` becomes `entities/a/c/acme`. The model naturally reports the path it *requested*.
Any verifier comparing the requested path against disk finds nothing and declares failure — we
discarded 14 correct writes that way.

The tools already returned `created <canonical-path> v<n>`; nothing consumed it. **Return the final
path from the tool, and make every consumer trust the return value over the request.** This is the
single highest-value MCP design note here.

### Tool NAME shape matters, not just tool count

Real MCP names are long and structured — `mcp__<server>_<scope>__<verb>`, 45–58 characters, double
underscores. A reproduction harness padded to the same tool *count* using short synthetic names and
trivial schemas **failed to reproduce** a bug that the real tool array triggered 5 times in 6. If
you are bisecting a tool-related failure, vary the real array, not a same-sized stand-in.

### Tool-count degradation is real, and scoping is usually per-server

Multiple agent frameworks report the same model degrading as the tool surface grows (see
[goose#6883](https://github.com/aaif-goose/goose/issues/6883) for a Qwen3-Coder instance). Our
agentic lane was handed **24 MCP tools and used about 6**.

The practical trap: most frameworks scope tools **per MCP server**, not per tool. You can drop a
whole server from a lane; you usually cannot drop nine of its fourteen verbs. Budget for that when
you design server boundaries — a server is the unit of least privilege you actually get.

### Initialise only the servers a task needs

Connecting every configured MCP server on every task start is a silent tax; ours fanned out to 29
servers and 402 tools before we scoped initialisation to the servers a lane's toolset admits. It
also enlarges the tool array for models that are sensitive to it, per the point above.

### Models invent resource URIs

Given `read_resource`, models will fabricate `file://…` and `<scheme>://…` URIs that were never
advertised, then loop on the failures. The fix that worked was explicit contract guidance: only
pass URIs returned verbatim by `list_resources`; use a plain file-read tool for local paths. We saw
this across more than one model family, so treat it as a protocol-shaped problem rather than a
model quirk.

### Tool-result messages carry `name`

Useful when you want telemetry: the tool-result message carries the tool `name` alongside
`tool_call_id` and `content`, so you can attribute results without correlating ids yourself.

## 11. Instrument the pipeline before you need it — we did not, and it cost the most

Read this one first if you are standing an agent pipeline up. Everything above was *findable*
because the data existed; almost none of it was *visible*, and that gap cost more than any single
bug in this document.

When we finally looked, on one deployment:

```
receipts stored: 1037   invalid: 866 (84%)
   495  the model omitted the completion receipt
   173  lanes structurally unable to produce one
    18  wrong receipt schema version

run logs on disk: 14,098   no index, no aggregation, grep-only
```

**84% of runs had been failing for weeks and nothing said so.** Two entire failure classes
accumulated silently. They surfaced only because someone went looking by hand.

### The gap caused wrong conclusions, it did not merely slow us down

- *"Zero writes"* was read as a model failure for a day. A write-yield-per-input panel would have
  shown at a glance that runs handed vendor commentary correctly wrote **nothing**, while the run
  handed a real threat report wrote **14**. We were grading the model on unwritable inputs.
- A **42,000-token runaway turn** was noticed only because someone happened to be tailing the
  *inference server's* log. Nothing on our side reported turn size.
- The 84% number **reframed the entire investigation** the moment it existed — and it had never
  existed.
- A fleet-wide deploy silently staged nothing on all five hosts and was caught only by manually
  grepping inside containers.

### Minimum viable telemetry for an agent lane

Per run, and none of this needs new instrumentation if your runner already logs turns:

- tool calls executed, and **writes executed counted from tool telemetry — not by grepping logs**.
  A *refused* write is still a "completed" tool call, so log-grepping overstates success. We
  reported two different write counts from `grep` in one morning and neither was the real number.
- terminal outcome and, when it failed, the **error class** — aggregated per lane, not per run
- turn count, **max output tokens in any turn**, calls-used vs budget
- context compressions, and whether any were **no-ops** (message count unchanged — we saw 5 of 7)
- which model and endpoint actually served the run

Rolled up per lane: write yield per selected input, success rate, median and max turn size,
runs-to-first-success.

### The part that actually pays

**Standing thresholds**, not dashboards. A dashboard tells you what is happening when you already
suspect something; a threshold tells you *when it started*. The three we most needed:

- success rate for a lane drops below N%
- a lane produces **zero writes across K consecutive runs**
- any turn exceeds M output tokens

That is the difference between *"84% failing for weeks"* and *"this started failing on Tuesday."*

## 12. The measurement discipline that actually worked

- One variable at a time. We changed an engine directive and a prompt together and could not
  attribute the result; the run had to be repeated after a revert.
- Grade by the outcome you care about, not the nearest counter. "Zero writes" looked like failure
  for a full day before we checked whether writing was the right answer.
- Verify end to end, not by exit code. A fleet-wide deploy returned success on all five hosts and
  had silently done nothing — the pipeline's exit status was `tail`'s, not the script's.
- Check timezones before declaring a defect. A "no files written" finding was a `find -newermt`
  comparing local time against UTC container mtimes.
