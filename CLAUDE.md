# OKEngine — project standards

Instructions for anyone (human or agent) working **on** OKEngine. For standing up
a deployment, read [`INSTALL.md`](INSTALL.md) and
[`docs/deploy-a-new-domain.md`](docs/deploy-a-new-domain.md) instead.

## What OKEngine is

OKEngine (Open Knowledge Engine) is the reusable layer that turns a **pinned
Hermes-Agent** into an agent that builds and maintains an **OKF** (Open Knowledge
Format) markdown wiki: conformance/validation, the enforced MCP write path,
index/health/tiering, repair drains, the cron machinery, and the framework CLI.

```
a live deployment  =  OKEngine @ a pinned Hermes  +  ONE domain pack
```

- **Engine** (this repo) — domain-agnostic. Ships no domain knowledge.
- **Hermes** — a **pinned dependency**, NOT vendored here. Cloned at a fixed tag,
  then `patches/` + the overlay are applied (see `INSTALL.md`). We consume Hermes;
  we do not fork it or send changes upstream.
- **Pack** — the domain layer (its own repo/dir): `schema.yaml` + persona
  `CLAUDE.md` + feeds + crons + content. The engine reads these; it never
  hardcodes domain facts. See `docs/deploy-a-new-domain.md`.

## Architecture / boundary

The engine layer is enumerated in [`engine-manifest.yaml`](engine-manifest.yaml).
The engine⇄pack split is documented in
[`docs/engine-domain-boundary.md`](docs/engine-domain-boundary.md). Cron jobs are
classified into three tiers in [`config/cron-tiers.yaml`](config/cron-tiers.yaml):

- `engine` — full def, ships unchanged, runs on any OKF vault.
- `engine-template` — the engine ships the selector/wake-gate SCRIPT; the pack
  supplies the agent PROMPT.
- `domain` — fully pack-supplied.

`scripts/cron_pack_split.py` merges `config/engine-crons.json` + the pack's
`crons/` into the deployed `config/cron-plus-jobs.json` (a **generated** artifact —
never hand-edit it).

## Core rules

- **Hermes is pinned — keep it that way.** Engine changes are additions (overlay)
  or carried patches (`patches/`), never an in-place Hermes fork. To move to a
  new Hermes version: bump the pin, re-run `patches/apply.sh`, rebase any patch
  that fails, re-test. The pin is recorded in `engine-manifest.yaml` (`runtime`)
  and `patches/README.md`.
- **No domain knowledge in the engine.** No vendor/product names, no private
  hostnames/IPs, no deployment-specific paths. Anything domain-specific is a
  pack input (schema field, env var, config file), not a literal. Run the scrub
  check below before any commit that touches engine code.
- **Generated artifacts are not edited by hand.** `config/cron-plus-jobs.json` is
  produced by `cron_pack_split.py`; deployed copies under the runtime data dir are
  produced by the `deploy-*.sh` scripts. Edit the SOURCE
  (`config/engine-crons.json` for engine crons; the pack for domain crons), then
  regenerate.
- **No fabricated facts.** Don't assert versions, file paths, API behaviors, or
  metrics you haven't verified by reading the source or running it. If unsure, say
  so or check.
- **Every fix gets a regression test** (`tests/`), except pure infra/config.
- **The MCP write path is the enforced contract.** Agent writes to the vault go
  through `okengine-mcp/write_server.py` (`okengine-write`), which validates against
  the pack's `schema.yaml` and applies the field-loss / reserved-file / permission
  guards. Don't add a bypass.

## Repo layout

```
tools/schema_validator.py     OKF conformance contract (+ the write-guard hook)
okengine-mcp/                     MCP servers: read query (server.py) + enforced write (write_server.py)
okengine-reader/                  read-only web reader for an OKF vault
scripts/cron/                  engine + engine-template cron scripts + shared libs (kb_*, tier_*, okf_migrate)
scripts/cron_pack_split.py     engine+pack -> cron-plus-jobs.json generator
scripts/framework.py           CLI: `init` (scaffold a pack) + `validate` (pre-deploy check)
scripts/deploy-*.sh            deploy scripts/crons/data into the runtime data dir
config/cron-tiers.yaml         engine/engine-template/domain classification
config/engine-crons.json       the engine half of the cron fleet (source)
config/config.yaml.template    documented Hermes config keys for a deployment
patches/                       the 6 carried Hermes core-file patches + apply.sh
docs/                          INSTALL/deploy guides + the OKF pattern guides (docs/okf/)
tests/                         regressions
```

## Before you commit (engine code)

```bash
# 1. nothing domain-specific leaked in
grep -rinE "192\.168\.|<your-corp-host>|<private-product-names>" \
  --include="*.py" --include="*.md" --include="*.yaml" --include="*.json" --include="*.sh" .
# 2. everything still parses
for f in $(git ls-files '*.py'); do python -c "import ast;ast.parse(open('$f').read())" || echo "FAIL $f"; done
# 3. run the tests that don't need a live Hermes
python -m pytest tests/cron/test_cron_pack_split.py tests/cron/test_tier_lib.py -q
```
