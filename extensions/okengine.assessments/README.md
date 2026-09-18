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

Assessment authors should set `subject` to the canonical vault path. When the subject also has a
stable external authority identifier (for example an ATT&CK `G` identifier), set `subject_ref` to
that identifier. The reader and cockpit use this declared fallback to preserve the assessment link
across a later entity creation, rename, or reshard.

## The remedy convention (okengine#746)

An assessment normally answers **"is this claim true?"**. With `assessment_kind: remedy` it
answers the prescriptive question instead: **"would this intervention work, and is it
tractable?"**

Nothing else changes. A proposed fix has evidence for and against it exactly like a factual
claim, so `alternatives`, `adversarial_evidence`, `would_increase_confidence` /
`would_decrease_confidence` and `consequence` all apply unchanged — which is why this is a
convention on the existing type rather than a second type carrying a duplicate copy of the
hardest part of this schema.

Three optional fields exist only for remedies:

| field | holds |
|---|---|
| `remedy_cost` | rough cost or effort |
| `remedy_prerequisites` | what must already be true for it to be available (list) |
| `remedy_decision` | `proposed` / `adopted` / `rejected` / `deferred` |

`remedy_decision` is **not** a confidence scale. `adopted` says someone decided; it says nothing
about whether the evidence got stronger. A remedy can be well-evidenced and rejected, or adopted
on thin evidence, and a reader must be able to see which — so the two vocabularies are kept
disjoint and a test pins that.

The optional `inquiry` field names the `okengine.inquiry` page a remedy answers. It is a soft
edge with no hard `requires`: inert when that extension is not enabled, and read by its dossier
when it is.
