# Install okpack-cti (the CTI bundle)

> **Renamed:** this bundle was `okpack-sec` through v0.10.8. Use `okpack-cti` — the old name no
> longer resolves. See the [library catalog](https://github.com/jalewis/okpacks-library#composition--bundles).

`okpack-cti` is a **pack bundle** (okengine#181): it owns no types and declares a recipe that
composes six focused security packs into one STIX-aligned vault. Pulling it recomposes the full
security KB — actors, campaigns, malware, tools, ATT&CK techniques, exploited CVEs, landscape
metrics/publishers, indicators (IOCs), infrastructure, detections, mitigations, incidents, and
identities — with STIX/legacy type names resolving to the friendly canonical types via `type_aliases`.

The recipe (see [`pack.yaml`](../../okpacks-library/packs/okpack-cti/pack.yaml)):

| Pack | Owns | Seed |
|------|------|------|
| **okpack-threat-actors** (host) | actor, campaign, malware, tool, technique | ATT&CK + MISP galaxy + APTnotes + annual reports; ships the STIX 2.1 / OCSF projectors |
| **okpack-vuln** | cve | CISA KEV + NVD |
| **okpack-threat-landscape** | metric, publisher | annual-report intelligence |
| **okpack-indicators** | indicator, infrastructure | abuse.ch URLhaus |
| **okpack-detections** | detection, course-of-action | SigmaHQ + ATT&CK mitigations |
| **okpack-incidents** | incident, identity | VERIS Community Database |

> **Cost warning:** the composed vault ships with no active feeds, so feed-driven LLM spend is off by
> default. The token-free (`no_agent`) importers seed the reference data; feed-derived analysis stays
> empty until you opt in. Set hard budget caps with your model provider before enabling feeds.

## Prerequisites

- OKEngine **v0.10.0+** installed per [`../INSTALL.md`](../INSTALL.md) (bundles need the
  `framework pull` recipe resolver added in v0.10.0).
- Docker Compose.
- A model provider key in the composed `.env` (unless you use a local/free provider).
- The `cron-plus` scheduler installed with the engine runtime.

## Pull the bundle

`framework pull` resolves the recipe automatically — it fetches the host as the base vault, then
`framework install-domain --apply`s each compose pack onto it:

```bash
python scripts/framework.py pull okpack-cti ../okcti
cd ../okcti
```

If the public catalog is not reachable yet, point at a local catalog (recipe members resolve from it,
or as siblings of the bundle in the same monorepo):

```bash
python scripts/framework.py pull okpack-cti ../okcti \
  --catalog /path/to/okpacks-library/catalog.json
```

You end up with **one** composed vault (reader `http://localhost:9400`, mcp on the per-pack bridge —
the historic sec ports, from the bundle's `port_offset: 200`). Its schema carries all 14 owned types
plus the STIX alias map, so a page authored `type: threat-actor` resolves to `actor`,
`attack-pattern` to `technique`, `vulnerability` to `cve`, and so on (the runtime backfill is
`schema_type_drain`).

## Configure

```bash
cp .env.example .env
```

Set your model provider key and delivery settings (the composed pack README + `.env.example` are the
source of truth). Per-lane model routing lives in `.okengine/model-profiles.yaml` +
`.okengine/cron-models.json` if you want cheaper models on the bulk backfill lanes.

To enable feed ingest, copy selected entries from each pack's `feeds/feeds.opml.example` into
`feeds/feeds.opml`, then probe before going live.

## Validate

The bundle validates its **recipe** (a bundle ships no schema of its own); the composed vault
validates as a normal pack:

```bash
# the bundle recipe (from the bundle source dir, before pull):
python ../okengine/scripts/framework.py validate /path/to/okpacks-library/packs/okpack-cti
# the composed vault (after pull):
python ../okengine/scripts/framework.py validate .
```

`PASS-with-warnings` is expected for a fresh pull — empty active feeds and absent runtime state are
normal before you enable ingest and deploy.

## Deploy

```bash
export ENGINE_DIR="$(cd ../okengine && pwd)"
# HERMES_UID/HERMES_GID default to your uid (you own the clone). Only for a portable/shared vault:
# export a fixed uid AND `sudo chown -R <uid> .`.
bash "$ENGINE_DIR/scripts/deploy.sh"
```

Open the reader at `http://localhost:9400`. The composed vault runs the six packs' `no_agent`
importers on schedule; the STIX/OCSF projectors live under `projectors/` on the host pack.

## Add or drop a pack

Because it's a bundle, growing or shrinking the security vault is an edit to the `compose:` list in
`okpack-cti/pack.yaml` (or a manual `framework install-domain <vault> <pack> --apply` onto a running
vault — exactly what `pull` automates). The library enforces globally-disjoint type ownership across
the composed set.

## Update later

Re-pull the bundle to re-resolve the recipe, or `--update` an individual composed pack in place so
local config and generated content aren't overwritten:

```bash
python ../okengine/scripts/framework.py validate .
```
