---
title: How assessments work
---

# Assessments

Assessments are explicit, dated judgments over evidence. They are not silently promoted into
canonical facts. Wherever Cockpit shows an assessed value, the `◇` marker identifies it as an
analytical judgment and links back to the complete assessment record.

## Reading assessment states

- **Reported** — an identified authority or source makes the displayed claim; this records what
  the source says, not a stronger inference.
- **Assessed** — the evidence has been evaluated and supports the displayed judgment at the stated
  confidence.
- **Confirmed** — an authoritative basis establishes the exact displayed proposition. This label
  cannot be inferred merely from high confidence.
- **Disputed** — credible evidence supports competing conclusions.
- **Inconclusive** — the question was assessed, but the evidence does not justify a value.
- **Not assessed** — no current ledger record covers this subject and question.
- **Unknown** — a current assessment concludes that the underlying value is unavailable.

Confidence expresses strength of belief, not review completion. A high-confidence record may still
show `⚠` when a human disposition is required. Selecting an assessed value shows its sources,
evidence qualifications, alternatives, confidence, review state, and what would change the view.

If a ledger record lacks the metadata needed to render its value, Cockpit displays **assessment
metadata unavailable** rather than falling back to an unqualified canonical field.

Aggregate views follow the same rule. They group only current assessment values, keep disputed,
inconclusive, unknown, and not-assessed subjects separate, and show mean confidence plus pending
human-review counts. Selecting an aggregate opens its contributing subjects and assessment records.
