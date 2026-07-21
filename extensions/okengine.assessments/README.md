# okengine.assessments

Opt-in estimative-assessment ledger and adversarial-evidence guardrail. It preserves three
questions that adversarial analysis must not collapse: whether something was authentically
observed, how diagnostic it is, and how easily an interested party could have staged it.
It also distinguishes ordinary observations from **expected absence**: something that should have
been visible under a hypothesis but was not found.

The extension owns the generic `assessment` type and `assessments/` namespace. Its strict
`adversarial_evidence` item contract is composed into the enforced write boundary. A deterministic,
zero-model operation evaluates proposed confidence moves and writes
`dashboards/adversarial-evidence-review.md`; it never changes assessment confidence itself.

`local_evidence.py` is the shared local-only resolver for assessment producers. It normalizes
heterogeneous vault references, preserves publisher separately from ingestion provenance, holds
alias-only matches for identity-scope evaluation, and exposes missing evidence for an explicit
collection operation. It never performs network research.

Policy outcomes are:

- `unrestricted`: the requested move has independent, diagnostic, manipulation-resistant support;
- `capped-held`: a positive move is capped (default `+0.05`) because repetition is one lineage,
  evidence is highly manipulable, or actor statements are being used as factual support;
- `human-review`: a high-consequence increase lacks resistant corroboration, evidence is absent,
  or “possible deception” was asserted without a testable hypothesis and alternatives.

Expected absence uses a stricter gate:

- `not-observed` records a pattern worth investigating but is not confidence-bearing by itself;
- `collection-gap` creates an explicit collection requirement and cannot increase confidence;
- `searched-not-found` can become negative evidence only when the expected observation, competing
  expectations, search scope, opportunity population, collection bias, coverage, and detection
  probability are all declared, coverage is at least substantial, and detectability is at least
  medium.

This prevents “not reported” from silently becoming “did not happen.” Existing evidence records
without `evidence_kind` retain the original observed-evidence behavior.

The engine owns schema enforcement and the reusable evaluator. A consuming domain may choose stricter
thresholds and authority limits. A domain pack should own its own question taxonomies, evidence
ladders, and rubrics; the foundation packs remain responsible for canonical identities and
observations—not estimative conclusions.

Enable with `framework extensions enable <pack> okengine.assessments`, compose the schema, and
write assessment pages through the MCP write path.
