# Installing OKEngine on Hermes

OKEngine = **a pinned Hermes** + **6 carried patches** + an **overlay** (new
files) + **plugins** + **config**, then **one domain pack**. This is the procedure
to take a stock Hermes install and bring it up to OKEngine — i.e. the exact
stock→OKEngine delta.

**Pinned dependency:** Hermes **v0.18.2** = upstream git tag **`v2026.7.7.2`**
(`github.com/NousResearch/hermes-agent`). The engine is cut against this version;
a different Hermes version may require rebasing the patches.

Prereqs: Docker, git, a host user. `HERMES_UID`/`HERMES_GID` **default to your own uid**
(`$(id -u)`), so a pack you cloned as yourself just works — nothing to export. Pin a
**fixed** uid (and `chown` the tree to it) only for a vault you'll move between hosts or
operate as several users:
```bash
export HERMES_UID=10000 HERMES_GID=10000 && sudo chown -R 10000:10000 <pack>   # portable/shared only
```

## Run it with Docker (start here)

Most users never touch §1–§8 below — **`deploy.sh` runs the whole stock→engine delta** (clone
Hermes → apply `patches/` → overlay → `docker build`) on first run, then brings the stack up:

```bash
git clone <engine-repo> okengine                              # 1. the engine (overlay + build source)
python okengine/scripts/framework.py list                     #    browse the pack catalog
python okengine/scripts/framework.py pull <pack> my-brain     # 2. a domain pack -> SIBLING vault dir
cd my-brain && bash ../okengine/scripts/deploy.sh             # 3. build image (once) + up gateway/reader/mcp + crons + verify
```

Then add a model key to `my-brain/.env` to enable the LLM crons (the stack comes up without one).

> **First deploy?** [`docs/cold-start-checklist.md`](docs/cold-start-checklist.md) lists the rough edges a from-scratch deploy hits and how to clear them.

> **Where is `docker-compose.yml`?** Not in this engine repo — the engine is an *overlay*, not a
> deployable stack. The compose file (wiring **gateway + `okengine-reader` + `okengine-mcp`**) ships
> with the **pack**: `framework pull`/`init` lands it at `<pack>/docker-compose.yml`, and `deploy.sh`
> runs `docker compose up` from the pack dir. Engine (`okengine/`) and vault (`my-brain/`) are
> separate sibling directories. Full deploy guide: [`docs/deploy-a-new-domain.md`](docs/deploy-a-new-domain.md).

**§1–§8 below are the by-hand internals** that `build-engine-image.sh` + `deploy.sh` automate —
read them to build manually, debug a build, or rebase patches on a Hermes bump.

## Fast path — build the gateway image (automates §1–§3)

The gateway container image (`hermes-agent`) = pinned Hermes + the patches +
the engine overlay, baked to `/opt/hermes` (where `config.yaml` points the
`okengine-write` MCP server). OKEngine is an *overlay*, not a Hermes fork, so it
has no root Dockerfile — one script assembles the tree and builds the image:

```bash
bash scripts/build-engine-image.sh          # clone Hermes@pin -> patch -> overlay -> docker build hermes-agent
#   HERMES_SRC=/path/to/hermes  bash scripts/build-engine-image.sh   # reuse a checkout
#   SKIP_BUILD=1                bash scripts/build-engine-image.sh   # assemble tree only (inspect/CI)
#   TAG_LATEST=0                bash scripts/build-engine-image.sh   # build the version tag only — DON'T move a :latest other stacks share
```

> **Overlay code changes only go live after an image rebuild.** The overlay files
> (`tools/schema_validator.py`, `okengine-mcp/`, `config/`, `plugins/`) are BAKED into
> `hermes-agent` at build time — the gateway runs them from `/opt/hermes`, not from a mount.
> Editing the engine repo (or `docker cp`-ing a hotfix into a running gateway) does NOT persist:
> a `docker compose up` recreate restores the image's copy. Rebuild the image to make a change
> durable. If `hermes-agent:latest` is shared by other stacks on the host, build with
> `TAG_LATEST=0` (version tag only) or, for a single-file hotfix without re-cloning Hermes, build
> a thin derived image and point only your pack at it:
> ```dockerfile
> FROM hermes-agent:okengine-vX.Y.Z
> COPY tools/schema_validator.py /opt/hermes/tools/schema_validator.py
> COPY okengine-mcp/write_server.py /opt/hermes/okengine-mcp/write_server.py
> ```
> then set that tag as the gateway `image:` in the pack's `docker-compose.yml` and recreate only
> the gateway. (This is how okengine#46/#48's write-path guards were baked in ahead of a full rebuild.)

That single command is §1–§3 below + the `docker build`. The `okengine-reader` and
`okengine-mcp` images are separate slim images built by the pack's `docker compose`
(they're standalone — they don't need the gateway image). §1–§3 document what the
script does, if you prefer to do it by hand.

## 1. Pin Hermes
```bash
git clone https://github.com/NousResearch/hermes-agent.git hermes
cd hermes && git checkout v2026.7.7.2         # == Hermes v0.18.2
```

## 2. Apply the carried patches (6 core-file patches)
```bash
<OKEngine>/patches/apply.sh "$PWD"           # idempotent; fails loudly on drift
```
What each patch is and why: `patches/README.md`. (The schema write-guard is the
only OKF-specific one; the rest are generic hardening/pricing.)

## 3. Install the overlay (the engine's new files — no patching)
Copy the overlay paths from the OKEngine repo onto the Hermes tree. The
**authoritative list is `engine-manifest.yaml`** (`okf_contract`, `cron_machinery`,
`ops_tooling`, `framework_cli`, `mcp_query_surface`, `reader`, plus `docs/`,
`config/`, `tools/schema_validator.py`). High level:
- `tools/schema_validator.py` — the OKF conformance contract (validator + the hook patch 01 calls).
- `okengine-mcp/` — read-only query server (`server.py`) **+** the enforced write server (`write_server.py` → `okengine-write`, G1).
- `okengine-reader/` — the human web reader.
- `scripts/` — OKF cron wake-gates, `framework.py`/`framework_validate.py`, `cron_pack_split.py`, `tier_lib.py`/`tier_refresh.py`, `kb_*`, `deploy-*`.
- `config/` — `cron-tiers.yaml`, `engine-crons.json`, `config.yaml.template`.
- `docs/okf/` — the pattern guides.

## 4. Install the plugins
- **cron-plus** — the **required** subprocess-per-job cron scheduler the engine's
  cron fleet runs on. It is a *separate Hermes plugin*, cloned by you (not vendored
  here) and **pinned** in `engine-manifest.yaml` (`dependencies.cron-plus`). Without
  it the deployed `config/cron-plus-jobs.json` (the engine + pack cron fleet) has
  nothing to schedule it. Clone it at the pin and enable it:
  ```bash
  # containerized deployment (the normal case): ensure-runtime.sh installs it automatically at
  # <pack>/.hermes-data/plugins/cron-plus (= /opt/data/plugins/cron-plus in the gateway), pinned.
  # Manual form (or for a HOST-run hermes, at ~/.hermes/plugins/cron-plus instead):
  git clone https://github.com/jalewis/hermes-cron-plus <pack>/.hermes-data/plugins/cron-plus
  git -C <pack>/.hermes-data/plugins/cron-plus checkout 6b230dc89171b0e21e89b7856e7a1a57628ca83c
  # `cron-plus` under plugins.enabled in config.yaml (the seeded template already lists it)
  ```
- **model-provider plugins** ship in the overlay (`plugins/model-providers/custom` — the local-Ollama `reasoning_effort:none` lever; `openrouter`).

## 5. Configure
```bash
cp config/config.yaml.template ~/.hermes/config.yaml   # (or the pack's .hermes-data/config.yaml)
```
Fill the load-bearing keys (template documents all):
- `model.default` — your primary model (make it your economical workhorse; it carries every
  cron lane that doesn't override it). Which model for which lane: `docs/model-selection.md`.
- `terminal.backend: local` — **required**, or the agent can't see the vault mount.
- `mcp_servers.okengine` (read, HTTP :8730) **and** `mcp_servers.okengine-write`
  (the enforced G1 write path, stdio, no token).
- `fallback_providers` — the failover chain.
- `~/.hermes/.env` — `TELEGRAM_BOT_TOKEN`, model keys, `OKENGINE_MCP_TOKEN` (mode 600).

## 6. Add a domain pack (the "task")
The engine carries no domain knowledge — a pack supplies `schema.yaml` + persona
`CLAUDE.md` + feeds + crons + `wiki/`. The pack/vault is a directory **separate
from** (sibling to) this engine checkout. Two paths:

**Use an existing catalog pack** (operator happy path — `docs/install-selected-pack.md`):
```bash
python scripts/framework.py list                         # browse the catalog
python scripts/framework.py pull <pack> ../my-brain      # fetch into a SIBLING vault dir
```
**Author a new pack from scratch:**
```bash
python scripts/framework.py init ../my-brain --domain "..."   # then fill it in
python scripts/framework.py validate ../my-brain              # pre-deploy check (FAIL = deploy-breaking)
```
Pack spec + quickstart: `docs/deploy-a-new-domain.md`. Don't `pull`/`init` into
this engine checkout — keep `okengine/` (code) and `my-brain/` (vault) side by side.

## 7. Deploy
Run from the **pack** dir (its `docker-compose.yml` wires all three services; `ENGINE_DIR` points
at this engine checkout). **One command** — `deploy.sh` does validate → seed runtime → install
cron-plus → build the image (if missing) → `docker compose up` → deploy crons → verify:
```bash
# HERMES_UID/HERMES_GID default to your uid (you own the clone) — export a fixed uid only for a portable/shared vault.
cd <pack> && bash $ENGINE_DIR/scripts/deploy.sh
```
Or step by step (exactly what `deploy.sh` runs in order):
```bash
bash $ENGINE_DIR/scripts/ensure-runtime.sh "$(pwd)"                    # seed .hermes-data/config.yaml + cron-plus + MCP token
bash $ENGINE_DIR/scripts/build-engine-image.sh                        # build hermes-agent — once per engine version
ENGINE_DIR=$ENGINE_DIR docker compose up -d                           # builds okengine-reader + okengine-mcp, runs gateway + both
CRON_PACK_DIR=$(pwd) bash $ENGINE_DIR/scripts/deploy-cron-scripts.sh   # engine + pack scripts/data -> /opt/data
CRON_PACK_DIR=$(pwd) bash $ENGINE_DIR/scripts/deploy-cron-plus-jobs.sh # cron defs -> live (self-heals next_run_at)
```
The `gateway` service consumes the prebuilt `hermes-agent` image; `compose` builds only the
standalone `okengine-reader`/`okengine-mcp` images.

## 8. Smoke (the gauntlet)
A no_agent cron succeeds; an LLM agent cron succeeds (API + tools + prompt-cache);
a delivery lands; `curl :8730/mcp` → 401 without token (read MCP up); the
`okengine-write` stdio server registers its tools; `schema-drift-lint` is green.
Then feeds → ingest → first compiled pages.

## Upgrading Hermes
Bump the pin: `git checkout <new-tag>` in the Hermes checkout, re-run
`patches/apply.sh` (rebase any patch that fails — `patches/README.md`), rebuild,
smoke-gauntlet, cut over. The overlay + plugins + pack are unaffected.
