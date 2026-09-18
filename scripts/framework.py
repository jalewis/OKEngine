#!/usr/bin/env python3
"""framework — the OKF engine CLI.

A single branded entrypoint over the engine's operator commands, so the docs say
`framework init` / `framework validate` instead of `python scripts/framework_init.py`.

Commands:
  framework init <dest> [--domain ...] [--interactive] [--port-offset N]
      Scaffold a NEW domain pack from the skeleton (delegates to framework_init).
  framework pull <source> [dest] [--into DIR] [--ref REF] [--force] [--update] [--port-offset N]
      Fetch an EXISTING pack definition from a repo or the catalog (framework_pull).
      --update refreshes a deployed pack in place, keeping .env/runtime/content and
      surfacing changed definition files as <file>.upstream for reconciliation; it
      previews the pack-version migrations pending for the update span, and
      --apply-migrations runs them (snapshot + validation gate + auto-rollback;
      okengine#312).
  framework reconcile <pack> [--interactive | --show/--accept/--keep/--merge FILE]
      Review and resolve files from `pull --update`; validates the pack after the final
      pending file is settled (framework_reconcile; okengine#61).
  framework list [--catalog URL|PATH] [--json]
      Browse the pack catalog (delegates to framework_list).
  framework validate <pack> [--probe-feeds] [--quiet]
      Pre-deploy sanity check on a pack (delegates to framework_validate). OFFLINE
      conformance on pack sources; the LIVE counterparts are status/doctor below.
  framework status <deployment> [--json]
      Compact live summary of a deployment: engine/Hermes version, scheduler state, and
      a per-area health roll-up from the shared deployment checks (framework_status).
  framework doctor <deployment> [--checks LIST] [--json] [--list-checks]
      Bounded live diagnostics over a deployment — the full finding list from the same
      shared checks the daily in-gateway validator runs (framework_doctor; okengine#405).
  framework install-domain <deployment> <pack> [--under wiki/<slug>] [--shape ...] [--refresh] [--apply]
      Install a pack ALONGSIDE the host in a live deployment (okengine#173): both
      co-install shapes (walk-up subtree / taxonomy-augmenting), collision-preflighted,
      key-based merges (idempotent), dry-run by default.
  framework upgrade <pack> [--apply]
      Reconcile the pack's engine.version pin to the running engine — dry-run by
      default; --apply bumps the pin, runs registered migrations, records state
      (okengine#66).
  framework backup (create|verify|restore|list|prune) …
      Disaster-recovery snapshots of a vault + runtime: create a verifiable .tar.gz,
      verify its integrity, restore into a target, list/prune (okengine#65).
  framework compose-preview <pack-dir> <pack-dir> [...] [--json]
      Multi-pack composition SAFETY GATE (read-only): merge >= 2 packs' schemas + crons
      and report blocking conflicts (schema ownership, cron name clashes, ID-authority
      overlap, trust mismatch) + the secrets union before any deploy (okengine#90 P1).
  framework budget (--status | --resume)
      Inspect/control the spend-cap guard. --resume is the manual recovery path
      after a budget trip (re-enables paused crons + clears state); see okengine#35.
  framework extensions (list | inspect | validate | enable | disable
                        | stage-plan | sidecar-generate | purge) <pack> [...]
      Discover/inspect/validate a pack's extensions across the engine/pack/operator
      tiers, enable/disable them, and emit deploy artifacts (delegates to
      framework_extensions; okengine#134, #113, #128, #135). enable/disable manage
      vault-level state + scoped MCP tokens; redeploy regenerates the fleet.
  framework operations (list | inspect | plan | run | status | logs | history | resume | cancel) …
      Discover and execute durable operations contributed by the engine, packs, and extensions.
      Operations share manifests, receipts, locking, and status across CLI and Cockpit.
  framework jobs (list | inspect | run | logs | pause | resume) …
      Operate cron-plus jobs by stable composed name; runtime hashes remain internal.
  framework audit DEPLOYMENT (--all | --checks LIST) [--json]
      Run composed deterministic checks under one immutable parent receipt.
  framework repair (plan | apply | verify) …
      Require an immutable, digest-bound plan before guarded canonical repair.
  framework ingest (status | run | retry) … / sources (hydrate | reconcile) …
      Run declarative connectors and source ingestion workflows.
  framework rebuild DEPLOYMENT (--indexes | --dashboards | --backlinks | --projection | --all-derived)
      Regenerate declared disposable artifacts without canonical-page authority.
  framework explain (page | assessment | field | config) …
      Explain ownership, evidence lineage, policy/review state, and producing runs.
  framework snapshot evidence … / export …
      Produce bounded analytical evidence artifacts distinct from disaster-recovery backup.
  framework deployment (render | diff | migrate) <pack>
      Deterministically render engine Compose v1 plus a constrained pack override.
  framework review-context [--strict] [--json] [--artifact PATH]
      Print the canonical branch/SHA/release/workspace provenance header used to
      attribute engineering evidence.

Each subcommand's own --help lists its flags. Exit code is the subcommand's.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ENGINE_ROOT = _HERE.parent
_SOURCE_ROOT = _HERE.parent / "src"
# The operator CLI must also work directly from a clean engine checkout.  CI and
# installed console scripts already provide the package, but deploy.sh deliberately
# invokes this source entrypoint with the host Python before any wheel is installed.
for _IMPORT_ROOT in (_ENGINE_ROOT, _SOURCE_ROOT):
    if str(_IMPORT_ROOT) not in sys.path:
        sys.path.insert(0, str(_IMPORT_ROOT))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, _HERE / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_COMMANDS = {
    "init": ("framework_init", "framework_init.py"),
    "pull": ("framework_pull", "framework_pull.py"),
    "reconcile": ("framework_reconcile", "framework_reconcile.py"),
    "list": ("framework_list", "framework_list.py"),
    "validate": ("framework_validate", "framework_validate.py"),
    "status": ("framework_status", "framework_status.py"),
    "doctor": ("framework_doctor", "framework_doctor.py"),
    "import": ("framework_import", "framework_import.py"),
    "install-domain": ("framework_install_domain", "framework_install_domain.py"),
    "review": ("framework_review", "framework_review.py"),
    "upgrade": ("framework_upgrade", "framework_upgrade.py"),
    "backup": ("framework_backup", "framework_backup.py"),
    "compose-preview": ("framework_compose_preview", "framework_compose_preview.py"),
    "budget": ("framework_budget", "framework_budget.py"),
    "extensions": ("framework_extensions", "framework_extensions.py"),
    "operations": ("framework_operations", "framework_operations.py"),
    "jobs": ("framework_jobs", "framework_jobs.py"),
    "lanes": ("framework_lanes", "framework_lanes.py"),
    "audit": ("framework_audit_repair", "framework_audit_repair.py"),
    "repair": ("framework_audit_repair", "framework_audit_repair.py"),
    "ingest": ("framework_ingest_rebuild", "framework_ingest_rebuild.py"),
    "sources": ("framework_ingest_rebuild", "framework_ingest_rebuild.py"),
    "rebuild": ("framework_ingest_rebuild", "framework_ingest_rebuild.py"),
    "explain": ("framework_explain_export", "framework_explain_export.py"),
    "snapshot": ("framework_explain_export", "framework_explain_export.py"),
    "export": ("framework_explain_export", "framework_explain_export.py"),
    "deployment": ("framework_deployment", "framework_deployment.py"),
    "review-context": ("review_context", "review_context.py"),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print("\nAvailable commands:", ", ".join(_COMMANDS))
        return 0 if (argv and argv[0] in ("-h", "--help", "help")) else 2
    cmd, rest = argv[0], argv[1:]
    if cmd not in _COMMANDS:
        print(f"ERROR: unknown command '{cmd}'. Try: {', '.join(_COMMANDS)}", file=sys.stderr)
        return 2
    modname, filename = _COMMANDS[cmd]
    module = _load(modname, filename)
    if cmd in ("audit", "repair", "ingest", "sources", "rebuild", "explain", "snapshot", "export"):
        return getattr(module, cmd)(rest)
    return module.main(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
