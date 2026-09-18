# Auditing and guarded repair

Run the deterministic audit family under one parent receipt:

```bash
bin/framework audit <deployment> --all
bin/framework audit <deployment> --checks schema-drift,grounding,policy --json
```

The command writes one immutable receipt beneath
`.okengine/operations/runs/`. A failed child check makes the parent audit fail; partial success is
never reported as complete.

Repair is an explicit three-phase workflow. It is never an audit side effect:

```bash
bin/framework repair plan <deployment> --from-audit <audit-run-id> \
  --repairs body-integrity,malformed-slugs
bin/framework repair apply <deployment> --plan <plan-id>
bin/framework repair verify <deployment> --plan <plan-id>
```

Plans are created once beneath `.okengine/operations/plans/` and bind the selected repairs to a
digest of the canonical wiki. Apply fails if canonical content changed after planning. A successful
plan cannot be applied twice, and verification requires a successful apply receipt. The currently
registered repairs are deterministic, dry-run-first scripts; adding a repair requires an equally
bounded planner/apply contract rather than a general script escape hatch.
