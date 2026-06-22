# Install okpack-sec

`okpack-sec` is the security/threat-intel pack. It builds a security wiki with
typed pages for sources, vulnerabilities, threat actors, malware, campaigns,
indicators, ATT&CK techniques, detections, findings, predictions, and briefings.

> **Cost warning:** the pack ships with no active feeds, so feed-driven LLM spend
> is off by default. Once you populate `feeds/feeds.opml`, ingest can run
> continuously. Start with a few feeds and set hard budget caps with your model
> provider before enabling the full feed list.

## Prerequisites

- OKEngine installed per [`../INSTALL.md`](../INSTALL.md).
- Docker Compose.
- A model provider key in the pack `.env` file, unless you are using a local/free
  provider.
- The `cron-plus` scheduler installed with the engine runtime.

Keep the engine checkout and vault directory separate:

```text
Source/
  okengine/       # engine code
  sec-brain/      # okpack-sec vault
```

## Pull the pack

From the OKEngine checkout:

```bash
python scripts/framework.py list
python scripts/framework.py pull okpack-sec ../sec-brain
cd ../sec-brain
```

If the public catalog is not reachable yet, point the CLI at a local catalog:

```bash
python scripts/framework.py pull okpack-sec ../sec-brain \
  --catalog /path/to/okpacks-library/catalog.json
```

Or pull directly from the library repo once it is public:

```bash
python scripts/framework.py pull jalewis/okpacks-library:packs/okpack-sec ../sec-brain
```

`okpack-sec` declares `port_offset: 200`, so the reader runs on
`http://localhost:9400` and the MCP service on `http://localhost:8930`.

## Configure

```bash
cp .env.example .env
```

Edit `.env` and set your model provider key and delivery settings. The exact
provider variables depend on your model choice; the pack README and `.env.example`
are the source of truth.

The pack is useful before feed ingest because deterministic, token-free importers
seed ATT&CK, CISA KEV, and related security reference data. Feed-derived news and
analysis stay empty until you opt in.

To enable feed ingest, copy selected entries from `feeds/feeds.opml.example` into
`feeds/feeds.opml`, then probe before going live:

```bash
python validate.py --probe
```

## Validate

Run both the pack validator and engine-aware validator:

```bash
python validate.py
python ../okengine/scripts/framework.py validate .
```

Expected result for a fresh pulled pack is `PASS-with-warnings`: empty active feeds
and absent runtime state are normal before you enable ingest and deploy.

## Deploy

From the pack directory:

```bash
export ENGINE_DIR="$(cd ../okengine && pwd)"
export HERMES_UID=10000
export HERMES_GID=10000

bash "$ENGINE_DIR/scripts/deploy.sh"
```

Open the reader:

```text
http://localhost:9400
```

## Update Later

Use `--update` from inside the deployed vault so local configuration and generated
content are not overwritten:

```bash
python ../okengine/scripts/framework.py pull okpack-sec . --update
python ../okengine/scripts/framework.py validate .
```
