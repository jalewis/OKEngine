# Serving local models for tool-calling lanes

Every OKEngine agent lane works by **calling tools** — `create_entity`, `search`, `read_file`.
A lane whose tool calls are not parsed back into structured `tool_calls` does nothing at all,
and (before the guards in [model-run-receipts.md](model-run-receipts.md)) could still report
success. If you run a local model, **the server you run it on decides whether that works** —
often more than the model does.

This guide is about that choice. For *which* model a lane should use, see
[model-selection.md](model-selection.md); this is the layer underneath it.

> **The short version.** Some GGUF builds ship **no chat template of their own**. ollama
> substitutes a generic one, and if the model was trained to emit a different tool-call
> format, ollama's parser does not recognise what comes back: the call arrives as prose in
> `message.content` with `tool_calls: null`. llama.cpp with `--jinja` handles the same weights
> correctly. **Same model, same GPU, same request — the serving process is the variable.**

> **Migrating another project?** [llamacpp-migration-findings.md](llamacpp-migration-findings.md)
> is the project-agnostic version of this — everything we measured, written to hand to another
> team. This document is the OKEngine-specific wiring.

## Symptoms

You have this problem if a lane shows any of these:

- the lane log records reads but **zero write-tool calls**, and the final message ends
  mid-intent (*"Let me read the second source…"*)
- the model's response contains a tool call **as text**: `{"name": "...", "arguments": {...}}`,
  a `<tools>{...}` wrapper (the *input* tag echoed back), an orphaned `</tool_call>`, or
  `<function=name><parameter=x>` XML
- the lane "completes successfully" while producing no pages
- it is **intermittent and gets worse as the run progresses** — the first one or two calls
  succeed, then the model drops out of tool-calling mode and narrates the rest

The engine now refuses receipts from runs in these shapes (`detect_narrated_tool_calls` and the
executed-call check in `run_receipts`), so the failure is loud and the lane's selection survives
— but the lane still does no work until the serving layer is fixed.

## Confirm it before you migrate

Do not infer the cause from a reconstruction of your payload. Reconstructions mislead: a
synthetic tool array with short names and trivial schemas can pass cleanly on a stack where
your real one fails 1-in-6. Capture the real request and replay **that**.

1. **Capture the wire payload.** Put a transparent forwarding proxy between the gateway and
   the inference host, and record the JSON body of a failing run — `tools` in full, `messages`
   including the system prompt, and every top-level parameter. Redact message *text* with
   same-length filler if you need to; leave the `tools` array byte-identical, since tool names,
   descriptions and schemas are part of what is being tested.

2. **Replay it against both servers**, changing nothing but the URL:

```python
import copy, json, urllib.request
cap = json.load(open("captured-payload.json"))          # the verbatim body

body = copy.deepcopy(cap)
body["stream"] = False                                   # easier to read; test streaming too
body["max_tokens"] = 400

for label, url in (("ollama",   "http://<inference-host>:11434/v1/chat/completions"),
                   ("llamacpp", "http://<inference-host>:8080/v1/chat/completions")):
    ok = 0
    for _ in range(8):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            msg = (json.load(r).get("choices") or [{}])[0].get("message") or {}
        ok += bool(msg.get("tool_calls"))
        if not msg.get("tool_calls"):
            print(f"  {label} MISS: {(msg.get('content') or '')[:80]!r}")
    print(f"{label}: {ok}/8")
```

A clear split between the two columns is your answer. If both columns are healthy, the
serving stack is *not* your problem and you should look at the model or the lane prompt
instead.

3. **Repeat with `stream: true`.** The gateway streams, so a server that returns `tool_calls`
   only in non-streaming mode is not usable. Accumulate `delta.tool_calls[].function.arguments`
   across chunks and confirm the concatenation parses as JSON and that `finish_reason` is
   `tool_calls`.

### A worked example

Measured on one deployment against a 30B-class coding model whose GGUF carries no template,
replaying its own captured `entity-backfill` payload (31 real MCP tools, ~25k tokens):

| cell | ollama | llama.cpp `--jinja` |
|---|---|---|
| captured payload, verbatim | **0/8** | **7/8** |
| trivial messages + the same 31 captured tools (~8k tokens) | **1/6** | **6/6** |
| captured payload, **streaming** | — | **5/6** |

Same GGUF blob, same GPU, same host. The ollama misses returned exactly the prose shapes above.

Repeating it through a load balancer that routes on the model name (see
[below](#preferred-a-model-name-alias-at-the-load-balancer)) makes the control tighter still —
one URL, one body, **only the model name differing**:

| model, via the same `/v1/chat/completions` | result |
|---|---|
| `<model>-tools` → llama.cpp | **6/6** |
| `<model>` → ollama | **0/6** |

Two things this example also disproves, both of which cost real investigation time before the
replay was run: it is **not** prompt size (the ~8k cell fails on ollama while much larger
requests succeed on llama.cpp), and it is **not** tool count as a model property (the same 31
tools are fine on the other server).

## Running llama.cpp alongside ollama

You do not have to migrate everything. The two servers coexist on one host, one per GPU, and
you move only the lanes that need tool calls.

```bash
docker run -d --name llamacpp --gpus '"device=1"' -p 8080:8080 \
  -v /path/to/models:/models \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/<model>.gguf \
  --jinja \                      # REQUIRED — this is what enables template-driven tool calls
  -c 65536 \                     # context is fixed HERE, not per request
  --n-gpu-layers 99 \
  --parallel 1 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --host 0.0.0.0 --port 8080
```

llama.cpp can read ollama's blob store directly (`-m /blobs/sha256-<digest>`), so you can
serve the exact weights ollama already downloaded without a second copy on disk. Confirm the
digest with `ollama show --modelfile <model>` and mount the blobs directory read-only.

**`--jinja` is not optional.** Without it llama.cpp does not apply the model's chat template
and you get the same class of failure you are migrating away from.

## Pointing OKEngine at it

### Preferred: a model-name alias at the load balancer

If the inference host already fronts its servers with a proxy or load balancer, the cleanest
wiring is not to move endpoints at all — publish a **second model name** that routes to
llama.cpp, and keep one URL:

```
<model>          -> ollama    (other workloads, /api/* and /v1/*)
<model>-tools    -> llama.cpp (tool-calling lanes, /v1/* only)
```

Both names serve the **same weights**; the suffix is purely a routing key. Retire the alias
once the old stack is gone — see the note in
[llamacpp-migration-findings.md](llamacpp-migration-findings.md#7-llamacpp-serves-v1--not-ollamas-native-api). llama.cpp ignores the
`model` field entirely, and the ollama backend must never receive the alias or it would try to
load a model that does not exist — so the LB routes on the name and refuses anything unlisted.

OKEngine then needs no new endpoint concept: selecting a serving stack is just picking a model
name, which every existing layer already supports — `model.default`, a per-lane `model:` on a
cron/operation, and `.okengine/extension-models.json`.

```yaml
# config.yaml — force-recreate the gateway afterwards (config is read at container start;
# a restart is not enough)
model:
  default: <model>-tools
  provider: custom
  base_url: http://<inference-host>:11434/v1    # unchanged
  context_length: 65536
```

Per lane, to move only the lanes that call tools and leave bulk traffic on ollama:

```json
// <pack>/.okengine/cron-models.json
{ "entity-backfill": "<model>-tools", "concept-backfill": "<model>-tools" }
```

This is worth setting up even if you control both stacks: it makes the serving choice a
one-token change, it is trivially reversible, and it gives you a clean A/B — same URL, same
body, only the model name differs, which is exactly the control the diagnosis above needs.

### Fallback: point the deployment at llama.cpp directly

Without an LB in front, set the endpoint per deployment and force-recreate:

```yaml
model:
  default: <model>
  provider: custom
  base_url: http://<inference-host>:8080/v1     # was :11434/v1
  context_length: 65536
```

To move only some lanes this way you need
[model profiles](model-selection.md#named-profiles--switch-host--ctx-per-lane-okengine151),
since a bare per-lane `model:` string resolves against the default provider's host and cannot
carry its own `base_url`:

```yaml
# <pack>/.okengine/model-profiles.yaml
profiles:
  agentic: {provider: custom, base_url: http://<inference-host>:8080/v1, model: <model>, ollama_num_ctx: 65536}
  bulk:    {provider: custom, base_url: http://<inference-host>:11434/v1, model: <model>, ollama_num_ctx: 65536}
```

Then `model: "@agentic"` on the lanes that write.

## Re-measure quality after ANY serving change

Tool-call parsing is the loudest property of a serving stack, but it is not the most fragile.
Emission is decided in the first few output tokens, so a config that scores perfectly on the
replay above can still have degraded what OKEngine actually depends on: **verbatim recall of
high-entropy tokens deep in a long context.** The completion-receipt contract requires the model
to reproduce 64-char `sha256` digests exactly, and the corpus is full of CVE ids and version
strings where one wrong character is a silent data error rather than a visible failure.

KV-cache precision (`--cache-type-k` / `--cache-type-v`) is the setting most likely to move this,
because it is bought against exactly the long-range attention that verbatim recall uses. It is
usually described as near-lossless; treat that as a hypothesis about your workload, not a result.

```bash
scripts/serving_recall_eval.py --base-url http://<host>:<port>/v1 \
    --model <model> --sizes 2000,16000,48000 -n 4 --label <config-name>
```

It calls through `llm_lib`, so it measures what the lanes actually experience — including the
reasoning-off policy every production call carries — rather than a raw endpoint the lanes never use.

It plants labelled `sha256` / CVE / version values at fixed depths in a filler context, asks for
them back verbatim, and scores exact string match by depth, size and identifier kind. The fact
corpus is seeded, so runs are comparable across configurations.

**Run it before the change, not only after.** An absolute score answers "does it work"; only a
before/after pair answers "did this cost us anything", and once the old configuration is gone the
baseline cannot be recovered. If the stacks can run side by side — two cards, two ports, one
setting different — that A/B is worth more than either number alone.

## An over-long prompt now fails loudly instead of being truncated in silence

This is the most consequential behavioural difference after tool calling, and it is an
improvement — but it changes what a too-large prompt looks like, so expect it.

**ollama silently truncated.** A request over the context limit was quietly cut down and answered
anyway, so the model saw less than the lane sent and nothing recorded that it had happened. The
output looked normal and was subtly wrong — a lane could run in that state for weeks. (The symptom
to recognise in old logs: a run reporting a suspiciously round *halved* input size.)

**llama.cpp returns a structured 400 instead:**

```json
{"error": {"code": 400, "type": "exceed_context_size_error",
           "message": "request (72016 tokens) exceeds the available context size (65536 tokens)",
           "n_prompt_tokens": 72016, "n_ctx": 65536}}
```

Hermes recognises this. It classifies as `context_overflow` →
`should_compress=True, should_fallback=False`, so the agent **compresses the conversation and
retries on the same endpoint** rather than failing the lane or escalating to a paid fallback
provider. Observed end to end on a live drain:

```
400 exceed_context_size_error       (65,650 vs 65,536 — over by 114 tokens)
context compression started         117 messages, ~52,092 tokens
context compression done            117 -> 113 messages, ~40,798 tokens
Turn ended  finish_reason=stop  api_calls=48/90  tool_turns=46
```

The lane recovered and completed 46 tool turns. Two things follow:

- **Don't treat these 400s as breakage.** In the logs they are `INFO`-level and followed by a
  compression event. They are the system working.
- **Compression is bounded.** If it cannot get the conversation under the limit, the turn ends
  with `Context length exceeded: max compression attempts (N) reached`. That is the real failure
  to alert on — not the 400.

The recovery does not remove the need to bound context growth within a run; it means the cost of
exceeding it is a compression round-trip rather than silent corruption.

## Gotchas

- **`max_tokens` is a real budget here.** A gateway that sends `max_tokens` equal to the full
  context window (e.g. `65536`) is asking for a single generation that can run for tens of
  thousands of tokens — minutes of GPU time, and a lane that appears hung. Cap it to what a
  turn actually needs. Different servers differ in how aggressively they stop, so a value that
  was harmless on one can run away on another.
- **Context is set at launch, not per request.** `options.num_ctx` (an ollama-ism) is ignored;
  `-c` at startup is the real setting, and it drives your VRAM budget.
- **ollama-specific request fields are accepted and ignored**, so the gateway's body usually
  needs no changes: `think`, `reasoning_effort`, `options`, `stream_options` all pass through.
  The `model` field is likewise not validated against a name — llama.cpp serves whatever it was
  launched with.
- **llama.cpp serves `/v1/*` but not ollama's native `/api/generate` or `/api/chat`.** Any
  other consumer on those endpoints must stay on ollama or be migrated too. This is the main
  reason to split by GPU rather than replace outright.
- **Check `kv_unified` on the machine you will actually run on.** It is reported at startup and
  differs by configuration; do not carry a CPU-run observation over to a GPU deployment when
  sizing concurrency.
- **Measure fit before you cut over.** A 30B-class model at `-c 65536` with a q8_0 KV cache sits
  around 21 GB — comfortable on a 24 GB card at `--parallel 1`, not comfortable with more slots.

## Verify the cutover

1. The lane log names the new endpoint: `grep base_url <lane-log>` →
   `provider=custom base_url=http://<inference-host>:8080/v1`.
2. The lane executes tool calls: `grep -cE 'tool .* completed' <lane-log>` → non-zero.
3. Pages actually appear. As
   [model-selection.md](model-selection.md#watch-out-a-too-weak-model-completes-but-writes-nothing)
   puts it — judge a write lane by *"did the page appear"*, not by *"did it complete"*.
4. Clear stale cron pidfiles after the recreate (`<data>/cron-plus/pids/`) — jobs killed
   mid-run leave a pidfile that blocks the next fire, and the lane will silently skip itself
   with `previous run still active`.
