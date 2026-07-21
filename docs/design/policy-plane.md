# OKEngine policy plane

**Status:** implemented by #283

**Source:** `config/policy/catalog.yaml`

**Runtime artifacts:** `.okengine/effective-policy.json`, `.okengine/policy-coverage.json`,
`.okengine/policy-findings.json`, `.okengine/policy-events.jsonl`

OKEngine uses one canonical policy catalog with several specialized enforcement points. The
catalog owns rule identity, intent, severity, applicability, remediation, and coverage. Python
evaluators remain next to the authoritative state they need. This is deliberately not one giant
validator and not a claim that every rule can be expressed safely in YAML.

```text
engine catalog -> pack policy -> enabled-extension policy
                         |
                         +-- write reference monitor
                         +-- importer adapter
                         +-- scheduled corpus audit
                         +-- framework/deploy validation
                         +-- CI coverage gate
                         +-- Cockpit Ops policy health
```

## Rule and finding contracts

Every rule has a stable `id`, owner, description, severity, applicability, evaluator, remediation,
enforcement list, matching `verified_by` coverage, and override mode. Catalog validation fails on
duplicate IDs, unknown evaluators or enforcement targets, missing metadata, and declared
enforcement without a verified adapter. The generated coverage artifact makes these claims
inspectable and prevents a surface from disappearing silently.

The shared finding envelope is:

```text
rule_id, outcome, severity, subject, operation, actor,
message, remediation, evidence, enforcement_point, evaluated_at
```

Human MCP responses include `policy[rule-id]`, the reason, offending fields, and remediation. The
same object enters the event ledger for audit and Cockpit.

## Composition and waivers

Composition order is engine, pack, then enabled extensions. Documents are discovered at the engine
catalog, `<pack>/policy.yaml`, `<pack>/.okengine/policy.yaml`, and policy files under the extension
trees. Rules are additive. A forbidden engine rule cannot be replaced. A tighten-only replacement
must preserve its evaluator and cannot lower severity. Duplicate actor capabilities fail rather
than selecting by load order.

A waiver is allowed only for a `waivable` rule and requires rule ID, owner, reason, scope, creation
time, and expiry. Forbidden and tighten-only rules cannot be waived. Active and expired waivers are
both visible in Policy health; expiration never erases history.

```yaml
- rule_id: engine-page-quality-review
  owner: security-operations
  reason: Historical archive awaiting migration
  scope: wiki/archive/**
  created_at: 2026-07-18T12:00:00Z
  expires_at: 2026-08-01T12:00:00Z
```

## Authenticated write capabilities

Path scope alone cannot protect fields inside an otherwise authorized page. A capability binds:

- operations: create, update, patch, append, tombstone, converge, flag, review, or import;
- paths and page types;
- create/update-field allowlists, optional all-or-nothing `required_fields`, and protected-field denylists;
- body behavior: allow, append-only, or deny.

The reference monitor evaluates capability before mutation. A violation rejects the whole write and
leaves the page byte-for-byte unchanged. Server stamps do not broaden authority; authorization is
evaluated against the caller patch before stamps are applied.

Administrative stdio and explicitly authenticated administrative HTTP callers retain full access.
Extension tokens remain compatible with existing path scopes and may opt into
`capabilities.write_policy`; the richer grant is stored with the token hash and resolved
server-side. New privileged extension lanes should always declare it.

### Scheduled job identity and the first vertical slice

An agent-authored job ID is never trusted. A constrained job uses a dedicated MCP entry whose
process environment binds `OKENGINE_WRITE_ACTOR`. Its toolset exposes that server, not the general
administrative writer.

`cron:source-quality-backfill` is bound to:

```yaml
operations: [update]
paths: [sources/**]
types: [source]
update_fields: [reliability, credibility]
body: deny
```

Candidate lanes that are only meaningful as a complete evidence bundle can add
`required_fields`. The reference monitor then rejects the entire mutation when any required field
is absent, before schema validation or file replacement. Required fields must also appear in the
capability's allowlist; otherwise policy composition fails closed. This supports contracts such as
“mapping + claim-specific evidence + confidence + alternatives” without relying on prompt
obedience.

The server is named `okengine-write-source-quality`. `ensure-runtime.sh` adds it to older configs
without rewriting operator values. The job receives that toolset and no general writer. Attempts
to alter type, ID, publisher/provenance, publication time, URL/raw capture, lifecycle, TLP,
confidence, ownership, server stamps, or body are rejected as `source-quality-fields-only`.

Prompts explain policy but do not enforce it. Framework validation and CI check that the prompt
names both permitted fields and states `NO body`, preventing instruction drift.

## Enforcement inventory and migration matrix

| Rule class | Previous locations | Canonical rule | Adapters |
|---|---|---|---|
| Source-quality ownership | prompt and toolset | `source-quality-fields-only` | write, audit, CI, Cockpit |
| Type/namespace integrity | validator, writer, drains | `engine-strict-type-namespace` | write, importer, audit, CI |
| Source completeness | schema, audits, badges | `engine-source-metadata-complete` | write, audit, CI, Cockpit |
| Import normalization | connector checks | `engine-importer-envelope` | importer, CI |
| Page quality | write flags, audits, badges | `engine-page-quality-review` | audit, Cockpit |
| Runtime drift | deploy/manual checks | `engine-policy-digest` | CI, deploy, Cockpit |

Existing specialized checks stay until parity is proven. The catalog gives them one identity and
coverage record; it does not replace mature schema, body, importer, or review logic with a weaker
generic interpreter.

## Importer, audit, Cockpit, and metrics

The source connector validates every normalized record before output. `engine-importer-envelope`
requires stable connector/native identity, revision, observation time, authority/permission/
sensitivity provenance, and payload. Invalid output stops before checkpoint advancement.

The deterministic `policy-audit` job runs daily at 05:15. It composes policy, writes the effective
digest and coverage, scans auditable corpus rules, includes recent write events, and generates
`wiki/operational/policy-health.md`. Cockpit lists it under Ops and exposes `/api/policy` for
structured clients; Cockpit does not re-derive decisions.

The event ledger is content-light: rule, actor, operation, subject, outcome, field names, and time.
It must never contain credentials or page bodies. This supports metrics by rule, actor/job, outcome,
and enforcement point without leaking protected content.

## CI and deployment

CI validates grammar, composition, evaluator identities, prompt alignment, and declared-versus-
verified coverage before the suite. Deploy materializes effective policy before containers start.
Post-deploy verification recomputes the merged digest and runs a non-mutating probe proving that the
source-quality identity rejects a type change. A mismatch is a failure because runtime authorization
would differ from reviewed source.

## Pack and extension author workflow

1. Add pack policy only for a domain invariant or a permitted tightening. Universal invariants
   belong to the engine.
2. Use stable namespaced IDs and a supported evaluator; never embed arbitrary code or secrets.
3. Add every declared enforcement adapter and test in the same merge request.
4. Give new writing extensions least-privilege `write_policy` declarations.
5. Run `framework validate`, integration tests, normal deployment, and post-deploy verification.
6. Inspect Cockpit → Ops → Policy health after the first scheduled audit.

When a new requirement does not fit, choose its authoritative owner and evaluator first. Add an
evaluator kind only when a deterministic reusable contract exists; otherwise keep logic in the pack
and register a narrow adapter. Expansion is intentional contract evolution, not an ad hoc bypass.

## Diagnosing findings and waivers

Start with `rule_id`, then inspect actor, operation, subject, evidence, and remediation. Compare the
source and runtime digests, effective caller capability, evaluator/test named in coverage, recent
events, and waiver state. Do not broaden authority simply to clear an error. If the job genuinely
has a new responsibility, update the correct component, catalog, prompt, atomic negative tests, and
runtime digest. If the work belongs to another lane, use that lane.

## Security boundaries and non-goals

- Catalog YAML contains metadata and bounded grants, not executable code or secrets.
- Token stores retain hashes; plaintext follows the existing secret-store contract.
- Client identity, reviewer identity, connector revisions, and validation facts are not trusted
  merely because a request contains them.
- Rejections happen before mutation; out-of-band writes remain auditable.
- This does not solve hostile third-party OS sandboxing (#124).
- It does not remove the schema validator, specialized write guards, importer checks, or review
  state machine, and prompts never become a security boundary.

## Implementation anchors

- Catalog/composition/findings/audit: `tools/policy_plane.py`
- Rules/capabilities: `config/policy/catalog.yaml`
- Reference monitor: `okengine-mcp/write_server.py`
- Token propagation: `scripts/extension_tokens.py`
- Importer adapter: `scripts/cron/source_connector.py`
- Scheduled adapter: `scripts/cron/policy_audit.py`
- Framework/deploy: `scripts/framework_validate.py`, `scripts/deploy.sh`
- Runtime verification: `scripts/post_deploy_verify.sh`
- Cockpit: `okengine-cockpit/app.py:/api/policy`
- Tests: `tests/test_policy_plane.py`
