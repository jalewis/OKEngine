# Install a published pack (operator quickstart)

The happy path when you want an **existing, curated** pack from the catalog — no
authoring. To build a NEW pack from scratch instead, use `framework init` and the
[authoring guide](authoring-a-pack.md).

Pack-specific walkthroughs:

- [`install-okpack-sec.md`](install-okpack-sec.md) — security/threat-intel pack.
- [`install-okpack-ai-research.md`](install-okpack-ai-research.md) — AI/LLM
  research-watch pack.

| You want… | Command |
|---|---|
| an existing catalog pack | `framework pull <name> ../<vault-dir>` |
| a brand-new pack from scratch | `framework init ../<vault-dir> --domain "…"` |

## Directory layout — the engine and your vault are SEPARATE

The OKEngine checkout (code) and your pack/vault (content + runtime) are two
different directories. Keep them side by side:

```
Source/
  okengine/             # the engine checkout — code only; you never edit it
  my-research-brain/    # your pack/vault — the deployment (created by `pull`)
```

`pull` writes the vault as a **sibling** directory (the path you pass), not into
the engine checkout. Two foot-guns to avoid:

- **Don't `pull` into the engine checkout** (`.` while inside `okengine/`).
- **Don't name the engine clone after the brain.** Cloning the engine as
  `research-brain` and then pulling `research-brain` next to it is needlessly
  confusing — keep the engine called `okengine`.

## Steps

```bash
# 1. clone the engine (once) — code only
git clone <okengine-url> okengine
cd okengine

# 2. browse the catalog and pick a pack
python scripts/framework.py list                 # NAME / ENGINE / TRUST / STATUS / DOMAIN
#   --json for tooling

# 3. pull it into a SIBLING vault dir (NOT the engine checkout)
python scripts/framework.py pull <pack-name> ../my-research-brain
#   fetches the definition, strips runtime, seeds .hermes-data/config.yaml,
#   validates, and leaves the pack on safe defaults (no feeds active; schedules
#   are disabled or wake-gated depending on the pack).
#   Applies the pack's declared port_offset (pack.yaml) automatically; pass
#   --port-offset N to override if the reader port 9200 (+offset) still collides on this host.

# 4. configure the vault
cd ../my-research-brain
cp .env.example .env                             # fill: OPENROUTER_API_KEY (default model), delivery
#   review schema.yaml / CLAUDE.md / feeds — see this pack's README "Customizing your vault"

# 5. deploy (one command: validate -> seed -> build -> compose up -> crons)
bash ../okengine/scripts/deploy.sh               # add --fix-perms if the tree isn't writable by uid 10000
```

Pulled packs ship on **safe defaults**: active feeds are empty, and schedules are
either disabled or wake-gated depending on the pack. Nothing fetches upstream
content until you opt in by adding feeds to `feeds/feeds.opml`; some packs also
require enabling schedules in `crons/domain-crons.json`. The pulled pack's own
`README.md` lists the customization levers.

## Updating a deployed pack (keep your config)

A plain re-`pull` is a *fresh fetch* and would clobber your config. To pull a newer
version of the pack's definition **without losing your setup**, use `--update`:

```bash
cd my-research-brain
python ../okengine/scripts/framework.py pull <pack-name> . --update
```

`--update` is non-destructive:

- **Untouched:** `.env`, `.hermes-data/` (your secrets + model/delivery config),
  `raw/`, and `wiki/` (your content).
- **Added:** brand-new upstream files (e.g. a new cron script) copied straight in.
- **Flagged, not overwritten:** a changed definition file (`schema.yaml`,
  `CLAUDE.md`, crons, …) is written next to yours as `<file>.upstream`. Diff it,
  merge what you want, then delete the `.upstream` copy.

It prints a summary (`+ new · ~ changed · = unchanged`) and re-validates. After
merging, bump `engine.version` if the update targets a newer engine and re-run
`framework validate`.

## Prerequisites

Docker, git, a host user, and the engine installed per [`INSTALL.md`](../INSTALL.md)
(the gateway image is built by `deploy.sh` on first run). The default model is
`gpt-oss-120b` on OpenRouter — set `OPENROUTER_API_KEY` in `.env`.
