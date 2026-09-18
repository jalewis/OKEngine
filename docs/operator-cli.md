# Supported operator CLI

`bin/framework` is the supported operator interface. `python scripts/framework.py` is retained as
an invocation-compatible entry point. Both dispatch the same command implementations and return
the same exit codes.

## Command families

| Outcome | Supported command |
|---|---|
| Create, fetch, validate, and upgrade packs | `init`, `pull`, `reconcile`, `list`, `validate`, `upgrade` |
| Inspect a live deployment | `status`, `doctor` |
| Discover and run contributed operations | `operations list|inspect|plan|run|status|logs|history|resume|cancel` |
| Manage scheduled jobs by stable name | `jobs list|inspect|run|logs|pause|resume` |
| Run checks and guarded fixes | `audit`, `repair plan|apply|verify` |
| Acquire and reconcile source material | `ingest`, `sources` |
| Regenerate disposable artifacts | `rebuild` |
| Explain and package analytical evidence | `explain`, `snapshot evidence`, `export` |
| Human review and deployment administration | `review`, `backup`, `budget`, `extensions`, `install-domain`, `compose-preview` |

Use each command's `--help` for its exact arguments. New operational commands support stable JSON
results where automation needs them. Durable operations expose the same run IDs, events, locks,
receipts, and terminal state through the CLI and Cockpit/write API.

## Internal tooling boundary

Files beneath `scripts/cron/`, `scripts/audit/`, `cron-plus.sh`, and deployment helper scripts are
implementation and debugging surfaces. They remain callable for engine development and incident
diagnosis, but operator automation should not depend on their filenames, runtime hashes, output
text, or private arguments. The framework command resolves those details and applies deployment
selection, validation, locking, planning, receipt, and output-boundary policy.

In particular:

- Never automate against cron-plus job hashes; use `framework jobs` and the stable composed name.
- Never invoke a repair script as an audit side effect; use an immutable `repair plan`, then
  explicit `apply` and `verify` phases.
- Never use rebuild commands for canonical sources, entities, assessments, or reviews.
- Never treat an analytical export as a disaster-recovery backup.
