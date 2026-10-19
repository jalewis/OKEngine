# Cockpit assessed values

Declarative table columns can render a value from the assessment ledger instead of presenting an
analytical judgment as a canonical page fact:

```yaml
- label: Assessed origin
  assessment:
    kind: actor-country-linkage
    value_field: assessed_value
    labels: {IR: Iran, RU: Russia}
```

Cockpit joins current `active` or `disputed` records by their `subject` page, selects the newest
matching assessment, and renders a linked value such as `Iran ◇ 85%`. The marker is accompanied by
an accessible epistemic-state, confidence, and review label. Tables using an assessed column receive
an inline legend and a link to `assessments/_about`, which is installed from the
`okengine.assessments` extension methodology.

The renderer never falls back to a similarly named field on the subject page. No matching record is
`Not assessed`; an explicit inconclusive record is `Inconclusive`; an assessment whose value is
unavailable is `Unknown`; and a malformed record is visibly `assessment metadata unavailable`.

Aggregate bars and chips use the same contract at box level:

```yaml
- title: Top assessed origins
  view: bars
  dataset: {dir: entities, type: actor}
  assessment:
    kind: actor-country-linkage
    value_field: assessed_value
    labels: {IR: Iran, RU: Russia, CN: China}
```

The rollup counts only the newest current assessment for each subject. Reported, assessed, and
confirmed values are grouped by `assessed_value`; disputed, inconclusive, unknown, malformed, and
not-assessed subjects remain explicit separate buckets. The label shows `◇`, mean contributing
confidence, and the number awaiting human review. Selecting a value lists both its subject pages
and assessment records. Selecting `Not assessed` lists the uncovered subjects. Canonical subject
fields never fill a missing assessment.

Country coverage retains its terminal state labels and aggregate buckets. New coverage payloads may
provide `reason_details`, an array of reference diagnostics with category, reference, origin, reason,
resolution reason, and blocking/contextual disposition. The existing terminal link exposes these
escaped details through its accessible label and title, and opens the pack's actor-review-status
dashboard. Existing payloads containing only `reason` retain their original fallback. Current ledger
judgments still take precedence, preserving their country, confidence, and human review rendering.
