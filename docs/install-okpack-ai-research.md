# Install okpack-ai-research

`okpack-ai-research` is an AI/LLM research-watch pack. It builds a research wiki
with typed pages for sources, models, labs, researchers, benchmarks, datasets,
techniques, concepts, predictions, and dashboards.

> **Cost warning:** the pack ships inert: active feeds are empty and crons are
> disabled. Once you copy feeds into `feeds/feeds.opml` and enable schedules, the
> agent can make continuous LLM calls. Start small and set hard spend caps with
> your provider before enabling regular ingest.

## Prerequisites

- OKEngine installed per [`../INSTALL.md`](../INSTALL.md).
- Docker Compose.
- A model provider key in the pack `.env` file, unless you are using a local/free
  provider.
- The `cron-plus` scheduler installed with the engine runtime.

Keep the engine checkout and vault directory separate:

```text
Source/
  okengine/            # engine code
  ai-research-brain/   # okpack-ai-research vault
```

## Pull the pack

From the OKEngine checkout:

```bash
python scripts/framework.py list
python scripts/framework.py pull okpack-ai-research ../ai-research-brain
cd ../ai-research-brain
```

If the public catalog is not reachable yet, point the CLI at a local catalog:

```bash
python scripts/framework.py pull okpack-ai-research ../ai-research-brain \
  --catalog /path/to/okpacks-library/catalog.json
```

Or pull directly from the library repo once it is public:

```bash
python scripts/framework.py pull jalewis/okpacks-library:packs/okpack-ai-research ../ai-research-brain
```

The catalog pack declares `port_offset: 100`, so the reader runs on
`http://localhost:9300` and the MCP service on `http://localhost:8830`.

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set your model provider key and delivery settings. The exact
provider variables depend on your model choice; the pack README and `.env.example`
are the source of truth.

To enable feed ingest, review `feeds/feeds.opml.example`, copy selected entries
into `feeds/feeds.opml`, then probe before going live:

```bash
python validate.py --probe
```

Enable crons only after you have selected feeds, reviewed schedules, and set
provider budget caps.

## Validate

Run both the pack validator and engine-aware validator:

```bash
python validate.py
python ../okengine/scripts/framework.py validate .
```

Expected result for a fresh pulled pack is `PASS-with-warnings`: empty active
feeds and absent runtime state are normal before you enable ingest and deploy.

## Deploy

Deploy only after validation passes. From the pack directory:

```bash
export ENGINE_DIR="$(cd ../okengine && pwd)"
# HERMES_UID/HERMES_GID default to your uid (you own the clone) — nothing to export.
# Only for a portable/shared vault: export a fixed uid AND `sudo chown -R <uid> .`.

bash "$ENGINE_DIR/scripts/deploy.sh"
```

Open the reader:

```text
http://localhost:9300
```

## Update Later

Use `--update` from inside the deployed vault so local configuration and generated
content are not overwritten:

```bash
python ../okengine/scripts/framework.py pull okpack-ai-research . --update
python ../okengine/scripts/framework.py validate .
```
