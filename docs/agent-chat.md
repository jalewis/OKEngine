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
3. **Toolset lockdown** (`config.yaml`). The engine template and `ensure-runtime` set this for
   you; add `web` per deployment if Chat should research the internet:
   ```yaml
   platform_toolsets:
     api_server:
     - web
     - okengine             # read query surface
     - okengine-write-chat  # Chat's own actor-bound writer (chat:api_server)
   ```
   This allowlist **excludes** `terminal`, `code_execution`, `coding`, `file`, and
   `computer_use` — so Chat can read the vault, write through the MCP guard, and search the web,
   but **cannot run shell or code**. Without it, the api_server inherits a broad default toolset.
   Never list the generic `okengine-write` here: it has no `OKENGINE_WRITE_ACTOR`, so the write
   path treats it as admin. `framework validate` and the in-gateway deployment check FAIL it, as
   they FAIL any list that names no MCP server at all (Hermes then binds every server).

## What Chat may write (okengine#983)

Chat writes as `chat:api_server`, held to the engine floor in `config/policy/catalog.yaml`:

- **create** a page in `entities/` or `concepts/`, with a body;
- **update** an existing page's **frontmatter only** (a body replacement is refused);
- **append** to a section of an existing page (`append_to_section`).

It may not tombstone, converge, patch, flag or record a review, and it may not set or change
provenance (`maintained_by`, `discovered_by`, …), handling (`tlp`, `sensitivity`), review or
authority fields, the curated-authority attestation fields, or the keys that make a page a
redirect, a stub or retired (`redirect_to`, `link_stub`, `tombstoned`, `superseded_by`,
`canonical`). It may set an ordinary `status` (`active`, `draft`, `monitoring-active`, …) but
never a value the engine treats as retired: `tombstoned`, `superseded` or `retired`, compared case-
and whitespace-insensitively and after the write's own key and value aliases, on create and update
alike (okengine#993). An update that leaves `status` alone is refused too when the write would map
the stored value onto a retirement value (a legacy `status: gone` under a `gone → retired` alias). That rule is the capability's `forbidden_values: {status: [...]}`; the
retirement set is `policy_plane.RETIREMENT_STATUSES` (`deprecated` is not in it: packs use it as an
ordinary status). Retiring a page is `tombstone_entity`'s job and the dedup lanes'. An update that echoes a stored value back (a date sent as a JSON string, a legacy
scalar list field) is not a change. A create that dedup resolves onto an existing page is refused
with that page's path: extend it with `update_entity` or `append_to_section`. **Every chat write
is queued for human review** (`needs_review: true` plus a review-queue entry) on create, update
and append alike. Only those three tools are exposed to it (`OKENGINE_WRITE_TOOLS`).

A pack whose knowledge lives elsewhere (for example `cves/`) may **replace** the capability in its
`policy.yaml` under the same actor, inside the engine ceiling: operations from create/update/append
only, no body replacement on update, every floor-protected field still protected, every
floor-forbidden value still forbidden (at least the retirement statuses on `status`), and no
`sources/`, `raw/`, `assessments/`, `dashboards/`, `operational/`, `system/`, `reviews/`, `_`/`.`
or glob-namespace paths. A reserved namespace name is refused anywhere in the pattern
(`<sub-domain>/sources/**` too). Composition (and so `framework validate`) FAILs anything wider.
Because a sub-domain scope such as `okcti/**` also reaches that sub-domain's lane-owned
namespaces, the write path checks the page's resolved namespace as well, sub-domain prefix
included (`okcti/sources`, `okcti/_archive`), whatever scope the capability declares. Only the
namespace position counts: the `_` shard directory that the by-letter layout uses for a
non-alphanumeric (second) letter, as in `entities/t/_/t-mobile`, is ordinary entity storage, not a
reserved namespace. Reference the engine rule `chat-write-back-floor`.

The replacement takes effect where the pack's own `policy.yaml` is composed: when the pack is
the deployment (standalone), whether or not it also ships a `subdomain/` form. Installed into a
host with `framework install-domain`, a pack's policy files are never composed; the host composes
only its own. For a taxonomy guest (`host-schema-additions.yaml`) the guest's types merge into the
host schema, so Chat reaches them only where the host's own chat capability allows. For a subtree
guest (`subdomain/schema.yaml`) the pages live under `wiki/<slug>/`, outside the default chat scope.
`install-domain` WARNs when it installs a guest that declares a `chat:*` capability, and
`framework validate` on the host repeats the WARN for every such installed guest (okengine#991).
The host's validate reads the declaration from the guest's ownership manifest
(`.okengine/installed-domains/<pack>.json`), not from the guest's source, which the manifest does
not locate. A manifest written before v0.15.4 has no `chat_capabilities` field, so validate stays
silent for that guest until `install-domain --apply` is re-run for it. Any re-run records the
declaration, including one that holds the accepted runtime hashes back until `--refresh`
(okengine#993).

**Decided behavior (okengine#993): Chat write-back into a walk-up sub-domain stays refused.** The
engine floor does not widen to `*/entities/**` or `*/concepts/**`, and a guest pack's `policy.yaml`
is not composed under its sub-domain. A write into `wiki/<slug>/…` fails closed unless the HOST's
own `policy.yaml` replaces the chat capability with a scope that names that sub-domain (inside the
ceiling above); the WARN is the operator's signal that a guest wanted more than that.

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
