# Conformance rules — keeping old pages aligned with current rules

OKEngine enforces page rules at write time. The problem this guards against: a rule that lives in a
**prompt or convention** (not the schema) historically shipped with write-time enforcement *only* —
so **existing pages silently drifted** when the rule changed, and nothing noticed. (Concretely:
entity `sources:` were written as prose like `"Cisco Talos disclosure"` instead of a source-page
path, which links nothing in the graph and starved prediction candidate discovery — okengine#158.)

The fix is a discipline: **every rule gets enforce → detect → remediate**, the same triad schema
rules already have.

## The three layers

1. **Enforce** (write time) — the MCP write path / `schema_validator` rejects or normalizes a bad
   write. Schema-field rules live in `schema.yaml types`/`okf`; **content rules** (beyond field
   presence/type) live in the `conformance.rules` registry (below).
2. **Detect** (existing pages) — a whole-vault audit checks every page against the *current* rules
   and publishes a drift dashboard:
   - `schema-drift-lint` → `dashboards/schema-drift.md` (fields/types/namespaces).
   - `conformance-audit` → `dashboards/conformance.md` (content rules from the registry).
3. **Remediate** — each rule names a remediation, following **propose (deterministic) / dispose
   (LLM)**: a deterministic drain fixes the clean cases; ambiguous ones are *left flagged* for an
   LLM lane or human (never fabricated — a wrong fix is worse than an honest violation).

## The rule registry (`conformance.rules`)

One source of truth, consumed by **both** the audit and (going forward) the write-guard — so a rule
can't be enforced-but-unaudited. Engine floor in `config/base-schema.yaml`; a pack EXTENDS it in its
`schema.yaml` (additive, deduped by `id`; a same-`id` pack rule overrides the floor copy).

```yaml
conformance:
  rules:
    - id: source-refs-are-pages      # stable id (dedupe key)
      kind: ref_fields               # the checkable predicate (see kinds below)
      fields: [sources]              # which frontmatter list-fields it applies to
      severity: fix                  # fix (retroactively remediable) | forward-only (informational)
      remediation: "relink-prose-sources (deterministic) + entity-backfill (LLM); ambiguous left flagged"
```

**Rule kinds** (extend `conformance_audit.py` + `schema_lib` to add more):
- `ref_fields` — entries of the named list-fields must be **page-paths** (contain `/` or end `.md`),
  not prose. Remediated by `relink-prose-sources`.

**`severity: forward-only`** marks a rule whose past can't be reconstructed (e.g. ISO-8601
timestamps — you can't fabricate the time an old page was written). The audit reports it as
informational; don't expect the count to reach zero retroactively.

## The discipline — a new/changed rule is NOT done until:

1. **Registry** — it has a `conformance.rules` entry (or is a schema-field rule in `types`/`okf`).
2. **Enforced** — the write path applies it going forward (or it's documented why not).
3. **Detected** — it's covered by an audit. `ref_fields` and schema rules are automatic via the
   registry; a *new kind* needs a check added to `conformance_audit.py`.
4. **Remediated** — it names a deterministic drain OR a flag-for-review path. If neither is possible
   (forward-only), say so in `severity` so the drift is understood, not mistaken for neglect.

Skipping 3–4 is what created okengine#158. The dashboard is the proof the loop is closed: drift on a
`fix`-severity rule should trend down as the drain runs and new writes comply.
