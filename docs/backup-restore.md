# Backup, restore & integrity (disaster recovery)

`framework backup` snapshots a vault + runtime into a single verifiable archive, restores it into
a new deployment, and verifies integrity — the supported DR path for the compounding knowledge a
vault holds (okengine#65).

```bash
framework backup create  <pack> [--dest DIR] [--include-secrets]
framework backup verify  <archive>
framework backup restore <archive> <target> [--force] [--no-validate]
framework backup list    <pack> [--dest DIR]
framework backup prune   <pack> [--dest DIR] --keep N
```

## What's in a backup

A `.tar.gz` of the pack **source + runtime state**, with a `MANIFEST.json` of per-file `sha256`
digests + a roll-up digest:

- **Included:** the vault (`wiki/`, `schema.yaml`, `pack.yaml`, `crons/`, `feeds/`, `engine.version`,
  extensions config) and `.hermes-data` (the deployed `config.yaml`, `cron-plus/` job state, the
  qmd index) — a complete point-in-time restore, no re-index needed.
- **Excluded:** `.git`, `__pycache__`/`node_modules`/`.venv`, transient `.okengine/snapshots` and
  `.okengine/backups`, heavy `.hermes-data/logs`, and **secrets** (`.env`, `.hermes-data/auth.json`).

Secrets are excluded by default — **restore re-provisions keys** (set `.env` on the new host). Pass
`--include-secrets` to capture them; the archive is then sensitive, so store it accordingly.

## Where backups go

Default `--dest` is **`<pack>.parent/<pack-name>-backups/`** — *beside* the pack, so losing the
pack dir doesn't lose its backups. For real DR, point `--dest` at an **offsite/remote** location
(another disk, an object store mount, a different host).

## Integrity model

`create` records a `sha256` of every file. `verify` re-hashes every archived file against the
manifest and reports any mismatch or missing entry. `restore` **runs that verification first and
refuses to extract a corrupt archive**, extracts with the path-traversal-safe `data` filter, then
(unless `--no-validate`) re-runs `framework validate` on the restored pack — so a restore that
silently produced a non-conformant vault is flagged, not deployed.

## Restore

```bash
framework backup restore my-vault-20260101T000000Z.tar.gz ../my-vault-restored
```

Refuses a non-empty target unless `--force`. After it reports `integrity ✓` and the post-restore
validation passes, deploy the target like any pack (set `.env`, `bash deploy.sh`).

## Retention

`framework backup prune <pack> --keep N` keeps the newest `N` archives in the dest and removes the
rest. Run it after `create` (or on a schedule) to bound disk use.

## Scheduling

`create` is a plain command — wrap it in a host cron / systemd timer / a Hermes cron for periodic
backups, e.g. nightly `framework backup create <pack> --dest /backups && framework backup prune
<pack> --dest /backups --keep 14`.
