# okengine.completeness — declared-expectation gaps (the completeness tier)

The engine's conformance audit answers *"does this page satisfy its schema?"*. This
extension answers the next question up: **"does the corpus satisfy its declared
expectations?"** — object X exists, but the relationship, companion page, field, or
freshness the pack expects *around* it does not. The result is an explainable,
deterministic **gap queue**: the high-volume, high-trust completeness tier, distinct from
`okengine.lacuna`'s generative structural tier (low volume, model-priced, low-trust). The
object type is `gap`, deliberately never "lacuna", so the two queues cannot be confused.

## The boundary split (why this is engine, not pack)

The mechanism is domain-agnostic; **all domain judgment lives in the pack's rules file**
(`config/completeness-rules.yaml` by default) — the relevance-gate pattern. No rules file →
the lane no-ops loudly. The engine ships: the rule grammar, the evaluation lane
(no_agent, zero model cost), gap lifecycle + dispositions, and the queue dashboard with
per-rule precision.

## Rule grammar

```yaml
rules:
  - id: vendor-needs-exposure-page     # stable — keys gap identity; renaming re-opens
    title: "Vendor without an exposure page"
    when: {type: vendor}               # selector; optionally has_field: <fm-field>
    expect: companion                  # field | link | companion | freshness
    companion: "exposure/{slug}"       # {slug} substituted from the subject page
    severity: high                     # high | medium | low
    resolution_hint: "Create the exposure decision page."

  - id: ttp-needs-detection
    when: {type: ttp}
    expect: link
    link: {prefix: "detections/"}      # or  link: {type: detection}
    severity: high

  - id: risk-owner
    when: {type: risk}
    expect: field
    field: owner
    severity: medium

  - id: assumption-freshness
    when: {type: assumption}
    expect: freshness
    field: last_reviewed
    max_age_days: 90
    severity: medium
```

## Gap lifecycle

Gap pages live in the owned `gaps/` namespace, keyed `<rule>--<subject-key>`:

- **open** — the expectation is unmet; `last_seen` bumps daily while it persists.
- **resolved** — automatic: the expectation is now satisfied (or the subject vanished).
  The page is kept as audit trail.
- **dismissed** — the operator sets `status: dismissed` + `dismiss_reason:`. **Never
  reopened by the lane.** Dismissals feed the dashboard's per-rule precision table: a rule
  whose gaps are mostly dismissed is a rule to fix or retire — precision, not discovery
  volume, is the metric that keeps a gap queue trusted.

A rule that exceeds `max_gaps_per_rule` open gaps (default 200) is marked **saturated** on
the dashboard rather than flooding the queue — capped, never silently truncated.

## Config

| Key | Default | Meaning |
|---|---|---|
| `rules_file` / `COMPLETENESS_RULES` | `config/completeness-rules.yaml` | Vault-relative rules path |
| `max_gaps_per_rule` / `COMPLETENESS_MAX_PER_RULE` | `200` | Open-gap cap per rule |

## Enable

```bash
framework extensions enable <pack> okengine.completeness
# then supply config/completeness-rules.yaml declaring YOUR expectations
```

Pairs naturally with `okengine.lacuna` (structural tier) but neither requires the other.
