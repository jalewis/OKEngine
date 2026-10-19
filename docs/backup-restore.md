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
  `.okengine/backups`, heavy `.hermes-data/logs`, and **secrets** (`.env`, `.hermes-data/.env`,
  `.hermes-data/auth.json`).

Secrets are excluded by default. `.env` is not archived as a file; instead its keys are classified
in `scripts/framework_backup.py`:

- **Portable** (non-secret) settings such as `TZ`, `CRON_TZ`, `OKENGINE_BRIEF_HOUR`,
  `CRON_DEFER_UTC_HOURS`, `OKENGINE_BIND` and `OKENGINE_DEPLOYMENT` are recorded in the manifest and
  written into the restored `.env`, so a restored deployment keeps its local-time schedules instead
  of silently reverting to UTC. A key the target `.env` already sets is left alone.
- **Host-local** values are rebound on the target: `HERMES_UID`/`HERMES_GID` come from the owner of
  the restored runtime tree, and `deploy.sh` rewrites `ENGINE_DIR`/`OKENGINE_SOURCE_SHA`.
- **Secrets**, and any key the classification does not list (for example a pack-specific key), are
  never carried. Restore prints the names (never the values) of every `.env` key it did not carry;
  add those to the restored `.env` on the new host.

A carried value must be a single printable line; anything else is not carried.

`.hermes-data/.env` is never carried by a secrets-excluded backup: Hermes loads it over the container
environment and `hermes setup` writes credentials there. It also holds non-secret Hermes settings
(`BROWSER_*`, `TERMINAL_*`, `*_DEBUG`, pack switches), so the manifest records the names of its keys
and restore lists them (names only, never values) for you to re-provision. If the file could not be
read at backup time, restore says its keys are unknown rather than listing none.

Pass `--include-secrets` to capture the whole `.env` too; the archive is then sensitive, so store it
accordingly.

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

## Consistency and concurrent writes

`create` does not stop the deployment's writers for the length of the backup. It takes the corpus
fence (the lock every write tool uses) only long enough to list the vault and hardlink its files
into a staging directory under `.okengine/backups/.staging`, on the pack's own filesystem. It then
releases the fence and does the slow work (compression, hashing, the SQLite online backups and the
`.hermes-data` runtime files) while writes continue. The canonical files in the archive are the
state at one corpus epoch, recorded as `corpus_epoch` in the manifest. The `.hermes-data` runtime
files are captured file by file, and the SQLite databases each get an online backup. If the online
backup of a real SQLite database fails (for example the temporary directory is full), `create` fails
with an error and publishes no archive: a raw copy of the main file would miss the committed rows
still in its `-wal`. Only a file with a SQLite name that is not a SQLite database is archived as
plain bytes.

The enforced write path publishes by rename, so a hardlink keeps the fence-time content. Some
files are written *in place* instead, so `create` copies them under the fence rather than linking
them:

- files written within the two seconds before the fence, because a rewrite inside the same
  timestamp tick could otherwise go unnoticed;
- files the deployment is known to append or rewrite in place: `log.md`, `*.jsonl` (including the
  corpus journal) and the JSON state files under `.okengine/`;
- files modified in the last 30 minutes, up to 64 MB in total.

After the fence, `create` checks every linked file again:

- A changed modification time or size means the file was written.
- A change time that moved on its own is either a metadata operation or a same-size rewrite whose
  modification time was set back. If the mode, owner or group changed (`chmod -R`, `chown -R`,
  `ensure-runtime.sh --fix-perms`), it is metadata and is ignored. If the link count changed (a
  publish by rename, or another backup linking the file), the snapshot's bytes are untouched and it
  is ignored too. Otherwise it counts as a rewrite.

If files were rewritten, `create` prints a note and rebuilds the archive with those files copied
under the fence. The forced copies under one fence are capped at 2,000 files and 128 MB. The cap goes
first to the files a writer may be changing right now (written within the two seconds before the
fence, and the known in-place writers above), then to the files carried over from earlier attempts,
so a mass metadata event cannot starve `log.md` or the journal. Anything past the cap is linked
again at the new fence, so the whole corpus is never copied under the fence, whatever triggered the
rebuild. After three attempts `create` keeps the archive and lists any file
that was still changing in `consistency.rewritten_after_fence` in the manifest. Those files carry
bytes newer than `corpus_epoch`, and `restore` lists them. `consistency.copied_under_fence` counts
the copies.

If hardlinks are unavailable (a filesystem without them, or `protected_hardlinks` with files
owned by another user), `create` prints a warning and reads the unchanged files from
the vault after the fence. It checks each of them again, including its inode, so a file published
by rename during the archive is also rebuilt, and a file deleted or moved after the fence is
treated as changed. The manifest then records `corpus_snapshot: read-after-fence`.

### Known limitation: a same-size rewrite with its mtime set back

A rewrite that keeps the file's size and restores its modification time (`os.utime(ns=...)`) is
caught by its change time in the plain case, hardlinked or not. It is **not** caught when, on the
same file and inside the archive window, it coincides with any of:

- a mode, owner or group change;
- a link-count change, if the file is hardlinked;
- a change time that does not advance. The kernel stamps the change time from a coarse clock
  (a tick lasts milliseconds), so this includes a rewrite in the same tick as the backup's link of
  the file.

Each of these makes the change-time signal read as metadata. The archive stays internally valid
(every digest matches its bytes); only the cross-file consistency of that one file with the corpus
epoch is lost. No production writer sets a rewritten corpus file's modification time back to its
earlier value. Every hit of a `git grep` for `os.utime|touch -r|copystat|copy2` over the production
code writes a new file or a backup copy, or gives the target the time of a different source file
(pack pull, upgrade rollback).

A `create` that is killed leaves its staging directory behind. The next `create` removes any
staging directory that no running `create` holds locked.

Restoring an older archive moves the corpus epoch backwards. The Postgres projection, whose volume
is outside the backup, sees that and runs a full reconcile on its next run instead of applying an
empty incremental delta.

## Restore

```bash
framework backup restore my-vault-20260101T000000Z.tar.gz ../my-vault-restored
```

Refuses a non-empty target unless `--force`. Run it as the deployment user, not root: a root
restore would leave a root-owned tree and pin the gateway identity to uid 0, so it is refused
before anything is extracted. A post-restore validation that crashes counts as a failure; pass
`--no-validate` to skip validation deliberately. Restore sets every cron-plus job's `next_run_at`
to null so the scheduler computes fresh fire times. The archived `jobs.json` is never extracted in
place: it is staged under another name and published already re-armed, in one atomic replace under
the job store's own lock, with its archived mode (normally 0600). A scheduler that is still running during
a `--force` restore therefore never reads a half-written store or the archived fire times. Stop the
gateway before a `--force` restore anyway: its lanes would otherwise run against a vault that is
only partly restored.

The archive holds files only, so restore also recreates the directories recorded in the manifest
(`dir_modes`), including empty ones, and re-applies their modes: a private `.hermes-data` (0700)
comes back 0700, not with the restoring user's umask. Directories under the excluded trees (for
example `.hermes-data/logs`) are recreated by the runtime. File modes come back as archived too:
tar's `data` extraction filter strips group and other write from every file, and restore re-adds
exactly those bits, so a `--fix-perms` deployment's 0666 `config.yaml` and `jobs.json` stay
writable by the container uid. Setuid, setgid and sticky bits are never restored.

Restore prints a `⚠` line for every mode it could not reproduce, and still finishes (the `.env`
identity is rebound either way):

- an archive written before v0.15.13 records no directory modes, so directories come back with
  the restoring user's umask and empty ones are not recreated. Set the modes the deployment
  needs (for example `chmod 700 .hermes-data`) before deploying;
- a directory or file it could not `chmod` (a foreign-owned path, such as one a root
  `docker exec` left behind) or a directory it could not recreate is named with the reason.

`framework backup restore` runs through `scripts/framework.py`, which puts the engine's `src/` on
the import path; `python scripts/framework_backup.py restore …` run directly does the same. After it reports `integrity ✓` and the post-restore
validation passes, deploy the target like any pack (set `.env`, `bash deploy.sh`).

## Retention

`framework backup prune <pack> --keep N` keeps the newest `N` archives in the dest and removes the
rest. It (and `list`) match only the exact `<pack-name>-<YYYYMMDDTHHMMSSZ>.tar.gz` names `create`
writes, so in a shared dest the archives of a pack whose name extends this one (`okcti-test` beside
`okcti`) are never counted or removed. Run it after `create` (or on a schedule) to bound disk use.

## Scheduling

`create` is a plain command — wrap it in a host cron / systemd timer / a Hermes cron for periodic
backups, e.g. nightly `framework backup create <pack> --dest /backups && framework backup prune
<pack> --dest /backups --keep 14`.
