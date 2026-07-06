# Agent Chat (reader → api_server)

The `okengine-reader` has an optional **Chat** tab that lets a human ask the agent questions
answered **from the vault it maintains** (wiki-first — it reads whole pages and follows
`[[links]]`, not RAG chunks). It is **off by default** and opt-in per deployment.

## How it works

The reader runs **no model of its own**. The Chat tab relays to the gateway's
**OpenAI-compatible api_server** (`:8642`), which is the agent. The reader reveals the tab only
when `_chat_enabled()` is true — i.e. both `OKENGINE_AGENT_API` and `OKENGINE_AGENT_KEY` are set.

```
browser → okengine-reader  ──relay──▶  gateway api_server (:8642)  →  agent → vault
                (Chat tab)              OpenAI /v1/chat/completions
```

## Enabling it (three coupled pieces)

1. **Gateway** — the api_server is enabled purely by env (no config section, no flag):
   ```yaml
   - API_SERVER_ENABLED=true
   - API_SERVER_KEY=${API_SERVER_KEY:-}     # a bearer key; set it in .env
   - API_SERVER_HOST=0.0.0.0                 # on the bridge = the gateway's bridge iface only (private)
   - API_SERVER_MODEL_NAME=OKEngine Agent    # the label the reader shows
   ```
2. **Reader** — point it at the api_server by **service name** (`_chat_enabled()` needs both):
   ```yaml
   environment:
     - OKENGINE_AGENT_API=http://gateway:8642/v1          # service name on the per-pack bridge
     - OKENGINE_AGENT_KEY=${API_SERVER_KEY:-}             # same key
     - OKENGINE_AGENT_MODEL=OKEngine Agent
   ```
3. **Toolset lockdown** (`config.yaml`) — **do not skip this.** Restrict what Chat can do:
   ```yaml
   platform_toolsets:
     api_server:
     - web
     - okengine          # read query surface
     - okengine-write    # the enforced write path (validates against schema.yaml)
   ```
   This whitelist **excludes** `terminal`, `code_execution`, `coding`, `file`, and
   `computer_use` — so Chat can read the vault, write through the MCP guard, and search the web,
   but **cannot run shell or code**. Without it, the api_server inherits a broad default toolset.

Recreate the gateway + reader. Verify: api_server `:8642` open, the reader's `/api/about` reports
`chat_enabled: true`, and `_get_platform_tools(cfg, "api_server")` resolves to the locked set.

## Exposure (private by construction since okengine#138)

The gateway is on the **per-pack bridge** (not host-net), so the reader reaches `:8642` by
**service name** (`http://gateway:8642/v1`). `API_SERVER_HOST=0.0.0.0` binds `0.0.0.0` *inside the
gateway container* — on a bridge that is the container's bridge interface only; **no host port is
published for the gateway**, so the api_server is **unreachable from the host or the LAN**. It is
private by construction: stable (no IP to hardcode, survives recreates) and unexposed.

The one remaining surface is the **reader** itself — the api_server key authenticates only the
reader→gateway hop, not the reader's own Chat UI. So **if you expose the reader** (bind it to
`0.0.0.0` / publish it on the LAN) **with no `OKENGINE_READER_PASSWORD`, anyone who can reach the
reader can chat with — and write to — the agent** (within the locked toolset). Mitigations:

1. **Set `OKENGINE_READER_PASSWORD`** whenever the reader is reachable beyond a trusted network.
2. **Keep the reader on a trusted overlay only** (e.g. Tailscale), not the public LAN.

The toolset lockdown (above) bounds the *blast radius* either way — Chat can't run code — but it
does not stop an unauthenticated reader from reading the vault and issuing schema-valid writes.

> **Legacy host-net deployments:** if a gateway is still `network_mode: host`, the api_server has
> no private+stable bind — `0.0.0.0` exposes it on every host interface, and a docker-bridge IP
> churns on recreate. Drop `network_mode: host` (okengine#138) to get the private posture above.

## Multi-vault note

`OKENGINE_AGENT_MODEL` is just a display label; each deployment's Chat talks only to **its own**
gateway/vault. There is no cross-vault leakage through Chat.
