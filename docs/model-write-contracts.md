# Model-write output contracts

Prompts guide a model; they do not enforce what may land in a vault. Every model-driven lane that
mutates canonical pages should declare a versioned `output_contract` beside its cron definition.
The engine validates and composes this domain-neutral shape before deployment; packs supply their
own namespace, type, field, and relationship names.

```json
{
  "output_contract": {
    "api": 1,
    "allowed_namespaces": ["sources"],
    "allowed_types": ["source"],
    "operations": ["create", "update"],
    "required_fields": ["type", "title", "raw"],
    "required_relationships": [],
    "body": {"required": true, "min_non_whitespace": 80},
    "unknown_fields": "reject",
    "unresolved_links": "reject",
    "placeholder_links": "reject",
    "completion": "per-selected-item"
  }
}
```

Policy values are `allow`, `review`, or `reject`. Completion is either `run` or
`per-selected-item`. Engine-template prompts retain the legacy string form, or may use an object
with `prompt` and `output_contract`. A pack may narrow allowed values, add requirements, raise body
minimums, or strengthen `allow → review → reject`; it cannot weaken an engine floor.

`output_contract_exempt` is a temporary, explicit migration marker for a legacy model-writing lane.
It must contain a reason and must not be used for new lanes. Runtime enforcement and verified
per-item receipts are separate layers that consume this contract.

Generated jobs receive a stable `id` derived from the composed lane name and an
`output_contract_digest` (`sha256:<hex>`) over canonical JSON. Writers and receipts must carry both
values so a write cannot be credited to a different lane or contract revision.

Typical lane profiles are: source compilation (`sources`/`source`, body and `raw` required), entity
synthesis (`entities`/`entity`, body plus pack-defined relationships required), briefing and
prediction (their pack-defined namespace/type with per-item completion), and deterministic jobs
(`no_agent: true`, which do not need a model-write contract). These are profiles, not engine-owned
domain constants; each pack declares the exact vocabulary it supports.

Source ingestion is staged: raw compilation may write only accepted source pages; entity synthesis
runs later over accepted sources and requires a resolving source relationship. A raw dedupe key is
consumed only by a source page that satisfies its acceptance fields and meaningful-body minimum.
Large inputs must declare partial extraction instead of treating a context-limited read as complete.
Packs may declare bidirectional relationship field pairs; deterministic propagation fills the
missing inverse after either side is accepted.
