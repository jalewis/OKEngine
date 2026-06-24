# Template placeholders

Every file under `skeleton/` uses `{{TOKEN}}` markers. `new-pack.sh` substitutes
them; or replace by hand with `sed`. Tokens are safe in YAML/JSON/shell (a raw
template is **not** expected to parse until rendered).

| Token | Meaning | Example | Derivation |
|-------|---------|---------|------------|
| `{{PACK}}` | Pack name — the repo/dir name. Convention: `okpack-<domain>`. | `okpack-sec` | you choose |
| `{{DOMAIN}}` | Short domain tag — used in `raw/<DOMAIN>`, the feed source-tag, env prefix. | `sec` | `{{PACK}}` minus `okpack-` |
| `{{TITLE}}` | Human title of the vault. | `security knowledge vault` | you choose |
| `{{BLURB}}` | One-line description (README intro + GitHub "About"). | `Agent-curated security knowledge vault…` | you choose |
| `{{ENGINE_VERSION}}` | Engine tag this pack is pinned to. | `v0.2.0` | the engine release |
| `{{HERMES_PIN}}` | Hermes runtime pin written to `engine.version`. | `v2026.6.19` | `--hermes-pin` (engine default) |
| `{{PORT_OFFSET}}` | Host-port offset so packs don't collide on one host. | `200` | you choose (unique per host) |
| `{{READER_PORT}}` | reader host port = `9200 + offset`. | `9400` | computed |
| `{{MCP_PORT}}` | mcp host port = `8730 + offset`. | `8930` | computed |
| `{{ENV_PREFIX}}` | Uppercase env prefix for the feeds var. | `OKPACK_SEC` | `{{PACK}}` upper, `-`→`_` |
| `{{BRIEF_HOUR}}` | UTC hour (0–23) for the daily-brief cron. | `13` | you choose |
| `{{LICENSE_YEAR}}` | Copyright year in the skeleton `LICENSE`. | `2026` | current year |
| `{{CRON_ID_1}}` / `{{CRON_ID_2}}` | Arbitrary unique hex ids for the two crons. | `7c0ff9a23fcf` | generator mints |

**Internal ports are fixed** (reader `9200`, mcp `8730`); only the host-side port
changes via `{{PORT_OFFSET}}`. Keep the offset unique per host.
