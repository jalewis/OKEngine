# Deploy a new domain on the engine — spec + quickstart

The framework = **engine** (this repo: a pinned Hermes-Agent + OKF KB machinery) + **one
domain pack**. To stand up a new domain-specific second brain (threat-history,
research corpus, a different market…), you supply a pack — no engine code changes.

```
engine @ vX.Y.Z   +   <your pack>   →   a live second brain
```

This is the operator-facing guide (the pack **spec** + the deploy **quickstart**).
For the step-by-step **how to build a pack from scratch** walkthrough, see
[`authoring-a-pack.md`](authoring-a-pack.md). The internal boundary is in
[`engine-domain-boundary.md`](engine-domain-boundary.md); the engine layer is
enumerated in [`../engine-manifest.yaml`](../engine-manifest.yaml).

## 1. What a domain pack IS (the spec)

A pack is one directory (version-controllable; the reference pack lives in its own
vault repo). Layout:

```
<pack>/
├── schema.yaml              # THE contract: types + partitioning + hot_set
├── CLAUDE.md                # persona / curation rules (domain voice + workflow)
├── engine.version           # the engine release this pack is pinned to
├── feeds/*.opml             # feed sources (read by the generic feed_fetch.py)
├── data/*                   # domain data tables (consumed by engine-template crons)
├── crons/
│   ├── domain-crons.json            # your domain-tier cron definitions (full)
│   ├── engine-template-prompts.json # prompts merged onto engine selector scripts
│   └── scripts/*.py                 # your domain-specific cron scripts
└── .env                     # secrets + delivery targets (NEVER committed)
```

Plus `wiki/` (the content the engine compiles + maintains) alongside.

**`schema.yaml`** is the heart — three blocks the engine reads (never hardcodes):
- `types:` — the domain's page types + required fields. The universal OKF core
  (`source`/`concept`/`prediction`/`finding`/`dashboard`/`briefing`/`trend` + the core
  namespaces) is **engine-owned** (`config/base-schema.yaml`) and merged *under* the
  pack, so a pack declares **only its domain types** and uses `extends:` to add optional
  fields to a core type — never re-declares or tightens one (okengine#90 P2; see
  [`core-types-and-extensions.md`](core-types-and-extensions.md)).
- `partitioning:` — how each namespace buckets on disk (`by-type` / `by-letter` /
  `by-date` / `flat`, + `reshard_over`/`reshard_by`). Read by `okf_migrate` /
  `reshelve` / `reshard`.
- `hot_set:` — which namespaces/fields feed the agent's load-first working set.
A sub-domain can drop its own `schema.yaml` (the validator walks up to it) — that's
how a `wiki/<subdomain>/` tree becomes a second domain inside the same vault.

> **Same instance or its own?** A pack can run as an extra **domain in an existing
> vault** (related domains, same trust boundary) *or* as **its own instance**
> (own vault + stack + crons). A **public** pack is always its own instance —
> never co-mingle public and private content (one shared search index / reader /
> MCP / cron fleet per instance). Full model + decision rule:
> [`okf/deployment-topology.md`](okf/deployment-topology.md).

## 2. Quickstart — stand up a new domain

Prereqs: Docker, a host user, the engine repo. **`HERMES_UID`/`HERMES_GID` default to
your own uid/gid** — you clone the pack as yourself, so you own the vault tree and the
gateway remaps to it; the deploy just works, no `chown` (okengine#102). Export a uid
only for the **portable/shared** model: a vault you'll move between hosts or operate as
several users wants a **fixed** uid so ownership doesn't depend on who deployed — pin it
and `chown` the tree to match:

```bash
# self-hosted, single operator (the common case): export NOTHING — the deploy uses your uid.
# portable / shared vault instead — pin a fixed uid and chown the tree to it:
export HERMES_UID=10000 HERMES_GID=10000 && sudo chown -R 10000:10000 <pack>
```

1. **Get the engine** at a pinned release:
   ```bash
   git clone <engine-repo> && cd <engine-repo> && git checkout v0.2.0
   ```
2. **Scaffold the pack** — one command generates the skeleton (schema + persona
   + feeds + crons + wiki + `.env.example` + a starting `docker-compose.yml`,
   pinned to the engine version):
   ```bash
   python scripts/framework.py init ../my-brain --domain "Threat History"
   #   or, with prompts:           python scripts/framework.py init --interactive
   #   2nd pack on this host:      ... --port-offset 100   (reader 9300, mcp 8830)
   ```
   Then fill in:
   - `schema.yaml` — your types + partitioning + hot_set (starts as a valid
     generic OKF contract).
   - `CLAUDE.md` — your domain persona + curation rules (this is what the
     cron agents read at `$WIKI_PATH/CLAUDE.md`).
   - `feeds/*.opml` — your sources (probe them; only live RSS/Atom).
   - `cp .env.example .env` then fill `TELEGRAM_BOT_TOKEN` + model keys. The stack
     is local-first: host ports bind `127.0.0.1` and the MCP uses a generic default
     token, so it runs as-is. To expose on the LAN, set `OKENGINE_BIND=0.0.0.0` and
     real `OKENGINE_MCP_TOKEN` / `OKENGINE_READER_PASSWORD` (validate FAILs otherwise).
   - **`config.yaml`** (the runtime config — REQUIRED): `framework init` copies
     the engine's `config/config.yaml.template` to `<pack>/.hermes-data/config.yaml`;
     fill it before deploy.
     Load-bearing keys: `model.default` (your primary model — make it your **economical
     workhorse**; it carries every cron lane that doesn't override it) +
     `terminal.backend: local` (without it the agent can't see the vault mount);
     `mcp_servers.okengine` (read) **and**
     `mcp_servers.okengine-write` (the enforced G1 write path — stdio, no token);
     and `fallback_providers`. The template documents every key. **Which model for which
     lane** — and how to point a cheap lane (glossary) or a reasoning lane (predictions
     grading) at a different model via the per-lane `model:` override — is in
     [`docs/model-selection.md`](model-selection.md).
   (Or start from the reference pack — okpack-cti, a separate repo — instead of `init`.)
   Then **validate before you go further** (catches a bad schema/cron JSON/script
   syntax/unfilled persona/committed `.env` up front):
   ```bash
   python scripts/framework.py validate ../my-brain            # offline, fast
   python scripts/framework.py validate ../my-brain --probe-feeds   # + HTTP-probe feeds
   ```
   Fix any **FAIL** (deploy-breaking); **WARN**s are should-fix. Re-run until clean.
3. **Define crons** — engine crons ship with the engine (`config/engine-crons.json`,
   tier `engine`/`engine-template`); add your `domain` crons + the
   `engine-template` prompts to `<pack>/crons/`. Generate the deployed file:
   ```bash
   CRON_PACK_DIR=<pack> bash scripts/regen-cron-plus-jobs.sh   # -> config/cron-plus-jobs.json
   ```
4. **Build the gateway image, then bring it up** — the gateway image
   (`hermes-agent` = pinned Hermes + patches + engine overlay) is built once per
   engine version; the scaffold's `docker-compose.yml` then wires all three
   services (gateway consumes that image; reader + mcp build standalone):
   The one-command path (`deploy.sh` runs all of this in order, so the
   seed-before-compose step can't be skipped):
   ```bash
   cd ../my-brain
   bash <engine-checkout>/scripts/deploy.sh   # validate -> seed -> build (if needed) -> compose up -> crons -> verify
   #   flags: --rebuild  --skip-build  --skip-validate  --no-crons
   ```
   The final step runs `post_deploy_verify.sh`, which actually exercises the live stack (reader
   `/healthz`, MCP read auth, the enforced write path, cron-plus job registration, qmd index
   readiness) and prints remediation for anything down. Re-run it any time:
   ```bash
   cd ../my-brain && bash <engine-checkout>/scripts/post_deploy_verify.sh
   ```
   Equivalent manual steps (steps 4–5):
   ```bash
   cd ../my-brain
   # HERMES_UID/HERMES_GID default to your uid (you own the clone) — nothing to export.
   # Only for a portable/shared vault: export a fixed uid AND `sudo chown -R <uid> <pack>`.
   bash <engine-checkout>/scripts/build-engine-image.sh    # -> hermes-agent:latest (once)
   bash <engine-checkout>/scripts/ensure-runtime.sh        # seed .hermes-data/config.yaml + install the PINNED cron-plus scheduler plugin (REQUIRED — without it nothing schedules) — MUST precede compose
   ENGINE_DIR=<engine-checkout> docker compose up -d        # builds reader+mcp, runs all three
   CRON_PACK_DIR=$(pwd) bash $ENGINE_DIR/scripts/deploy-cron-scripts.sh       # engine + pack scripts -> /opt/data/scripts; pack data/feeds -> /opt/data/config
   CRON_PACK_DIR=$(pwd) bash $ENGINE_DIR/scripts/deploy-cron-plus-jobs.sh     # regenerated jobs.json -> live (self-heals next_run_at in ~60s)
   ```
6. **Smoke** (the gauntlet): a no_agent cron succeeds, an LLM agent cron succeeds
   (API calls + tools + prompt-cache), a delivery lands, `okengine-mcp` answers
   (`curl :8730/mcp` → 401 without token), and the conformance gate is green
   (`schema-drift-lint`). Then feeds → ingest → first compiled pages → first digest.

## 3. Engine upgrades (without clobbering the pack)

The pack pins `engine.version`. To upgrade: `git fetch upstream`, merge the new
release on a branch, build, smoke-gauntlet, cut over (keep the old image tagged
for rollback), bump the pack's `engine.version`. The pack is never rewritten by
an engine upgrade. Watch the **HERMES_UID** cutover gotcha — keep the same uid across
the upgrade (the default is your uid; if you pinned a fixed one, keep pinning it) so
vault file ownership doesn't shift.

**After every cutover, in order** (each step verified by the next):

1. `bash scripts/ensure-runtime.sh <deployment>` — besides the runtime seeds, this
   **re-stamps `/opt/data/engine-runtime.yaml`** with the release/Hermes actually
   deployed. A stale stamp makes the weekly pin-drift check compare against the OLD
   engine (found live on the v0.9.0 sweep: a deployment still stamped two releases back).
2. `deploy-cron-scripts.sh` + `deploy-cron-plus-jobs.sh` (with `HERMES_UID` exported —
   the scripts default to 10000 and fail with `mkdir /opt/data: Permission denied`
   otherwise).
3. Recreate the gateway, then **sweep `/opt/data/cron-plus/pids/`** for dead-pid
   orphans (a recreate strands in-flight lanes' pidfiles; a stranded pidfile blocks
   that lane's every future run as "still active").
4. If the fleet includes `backlinks-refresh` (okengine#168): the gateway image ships
   no iwe — stage the binary at `/opt/data/iwe/bin/iwe` (the reader image carries the
   pinned build). The weekly validation lane FAILs when the job is enabled without it.
5. Run the validation lane once by hand and require **PASS**:
   `scripts/vault-exec.sh <deployment> sh -c 'cd /opt/vault && python3 /opt/data/scripts/deployment_validate.py'`

## 3a. Updating a deployed pack (pack updates, not engine upgrades)

The flip side of §3: the **pack definition** changed upstream (library or private repo)
and a live deployment should pick it up. The engine stays put; only pack files move.
The mechanism is `framework pull --update` — it never touches runtime or content
(`.env`, `.hermes-data/`, `raw/`, `wiki/`, the active `feeds.opml`), adds new upstream
files, and writes each changed definition file as `<file>.upstream` next to yours.

**In order** (each step verified by the next):

1. **Pull from the source that actually has the change.** The catalog resolves to the
   *published* snapshot — if the publish lags the working repo, point at a checkout
   directly (a local path is a first-class source):

   ```bash
   OKENGINE_LIBRARY=/path/to/okpacks-library \
     python3 scripts/framework.py pull okpacks-library:<pack> <deployment-dir> --update
   # private/monorepo packs: framework.py pull /path/to/repo:packs/<pack> <deployment-dir> --update
   ```

2. **Reconcile every `.upstream` file — never bulk-adopt.** Diff each one. Rules of thumb:
   - **Keep the deployment's copy** for anything carrying deployment state:
     `pack.yaml` (a deliberate `trust:` flip), the active `feeds/feeds.opml`,
     `crons/domain-crons.json` (install-time jittered minutes + locally added jobs),
     a port-offset `docker-compose.yml`.
   - **Co-installed vaults: `schema.yaml` and `CLAUDE.md` are merge targets, not files
     to replace.** `install-domain` writes the co-installed taxonomy into the host's
     schema and appends `## Installed domain:` persona sections — adopting the upstream
     copy wholesale **wipes the composition** (and can re-shadow a co-installed type
     via `type_aliases`). Merge the upstream delta by hand, or skip it if the change
     doesn't apply.
   - **Adopt upstream** for everything else (validators, conformance suites, docs, CI).
   - Delete each `.upstream` as you settle it; finish with none left.
3. **Validate the pack offline:** `python3 validate.py` in the deployment dir
   (plus its conformance suite, if it ships one).
4. **Redeploy crons only if cron files changed** (`domain-crons.json`,
   `engine-template-prompts.json`, `crons/scripts/`): `deploy-cron-scripts.sh` +
   `deploy-cron-plus-jobs.sh` with `HERMES_UID` exported (§3 step 2). Definition-only
   changes (schema, persona, docs) need **no redeploy and no restart** — the vault is
   bind-mounted and read at use time. Restart only if `docker-compose.yml`/`.env` changed.
5. **Verify live:** `bash <engine>/scripts/post_deploy_verify.sh` from the deployment
   dir — require 0 FAIL.
6. **Run the validation lane once by hand** and require **PASS** (§3 step 5):
   `scripts/vault-exec.sh <deployment> sh -c 'cd /opt/vault && python3 /opt/data/scripts/deployment_validate.py'`

## 4. Reference pack

- **okpack-cti** — a security-focused LLM-wiki pack, published in the
  **pack catalog**: <https://github.com/jalewis/okpacks-library/tree/main/packs/okpack-cti>.
  This engine ships no pack content itself. Use it as a worked example of
  a complete pack — schema, persona `CLAUDE.md`, feeds, domain data, and
  `domain`/`engine-template` crons — when authoring your own.
- An instance can also host **multiple domains in one vault**: drop a
  `wiki/<subdomain>/schema.yaml` and the walk-up validator governs each subtree by
  its nearest schema, proving domain-agnosticism within a single instance.

## 5. What's automated vs manual

The `framework` CLI (`scripts/framework.py`) covers both **`init`** (scaffold a
pack skeleton + `docker-compose.yml`; `--interactive`, `--port-offset`) and
**`validate`** (`scripts/framework_validate.py` — the pre-deploy pack check, strict
about real requirements: schema parses + has `types`, persona present, runtime
`config.yaml` keys, a pinned `engine.version`, a substantive `README.md` with a
Deploy section, a `LICENSE`, no unrendered `{{tokens}}`, well-shaped crons with a
real schedule + action and non-empty engine-template prompts, valid `pack.yaml`
enums, scripts parse, no committed `.env` — see authoring-a-pack §6 for the full
FAIL/WARN list). The per-domain *content*
fill-in (schema types, persona, feeds, cron prompts) is inherently manual — the
engine carries no domain knowledge, so each pack supplies its own.

## 6. Agent chat (optional)

The reader can show a **Chat** tab that relays to *the* agent (Hermes' OpenAI-compatible
`api_server`), which answers by navigating the vault as memory and writing new findings back
(the wiki-as-memory demo, not RAG — see [`kb-tooling.md`](kb-tooling.md) and
[`okengine-reader/README.md`](../okengine-reader/README.md)). The static config is templated
(pack `docker-compose.yml` + `.env.example`); the runtime setup is **manual and required** —
without the last two steps the chat is either insecure or can't find anything.

1. **Enable the api_server** — in the deployment `.env` (keys in the pack `.env.example`):
   `API_SERVER_ENABLED=true`, `API_SERVER_KEY=$(openssl rand -hex 24)`, `API_SERVER_HOST=0.0.0.0`,
   `API_SERVER_PORT=8642`, and point the reader: `OKENGINE_AGENT_API=http://host.docker.internal:8642/v1`.
2. **Recreate** the gateway (picks up the api_server env) and the reader (picks up the agent
   env → the Chat tab appears).
3. **Lock down the chat platform's tools (security — do NOT skip).** The `api_server` platform
   defaults to the full toolset; an untrusted visitor would get shell + code-exec inside the
   gateway. Restrict it to read + wiki-write + web, run **in the gateway** then restart it:
   ```bash
   hermes tools disable terminal code_execution file browser computer_use delegation \
     cronjob messaging memory session_search --platform api_server
   hermes tools enable web --platform api_server          # okengine read+write MCP stay enabled
   ```
   (Drop `okengine-write` too if you want read-only chat with no write-back.)

   > **Enabling `web` is necessary but NOT sufficient — you MUST also configure a search
   > backend key, or the web tools are SILENTLY filtered out** and the agent reports it has no
   > web access (every "enabled" view — `hermes tools list`, `GET /v1/toolsets`, `config.yaml
   > platform_toolsets` — will still show web ✓; the real gate is a working backend). Set ONE of
   > these in the deployment `.env` and recreate the gateway:
   > `TAVILY_API_KEY` (free tier, reliable — recommended) · `EXA_API_KEY` · `BRAVE_SEARCH_API_KEY`
   > · `SEARXNG_URL` (self-hosted) · or install the `ddgs` package for keyless DuckDuckGo.
   > (Hermes' web tool does NOT support Serper.) Verify it's actually live:
   > ```bash
   > # backend reachable?
   > docker compose exec gateway python3 -c "import tools.web_tools as w; print(w.check_web_api_key())"   # must print True
   > # web_search actually in the agent's toolset?
   > curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://localhost:8642/v1/chat/completions \
   >   -d '{"model":"OKEngine Agent","messages":[{"role":"user","content":"list your tool names"}],"stream":false}' | grep -o web_search
   > ```
   > Note: `.env` changes need a `docker compose up -d` **recreate** (not `restart`) to reach the
   > gateway's environment.
4. **Search index — automatic (lexical).** The mcp server self-registers the `wiki` collection
   on startup and keeps it fresh (`OKENGINE_MCP_INDEX_REFRESH_HOURS`, default 6), so lexical
   search just works after the mcp container is up — no manual step. For **hybrid** (semantic)
   search, build vectors mcp-side off-hours: `qmd embed` (heavy; lexical is the default). Full
   setup + speed/GPU tuning in [`kb-tooling.md`](kb-tooling.md#search-index--setup-performance--tuning-deployment-reality).
5. **How the agent behaves (grounding prompt).** The reader injects a server-side system prompt
   (`_AGENT_SYSTEM`, override with `OKENGINE_AGENT_SYSTEM`) that drives the loop: acknowledge →
   **search the vault first** → answer from the vault if it's covered, otherwise **research with
   the web tools and write the findings back** (mirroring an existing same-type page's fields).
   The prompt deliberately NAMES the web capability: without that, the agent assumes it has only
   vault tools and refuses ("I can't research externally"), which defeats the demo on anything
   the vault doesn't already cover. So if you trim the toolset in step 3, keep `web` enabled, and
   keep the prompt's web instruction in sync with the tools you actually leave on.
