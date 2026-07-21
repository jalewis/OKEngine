# Threat-Informed Detection Engineering application profile

This profile composes the Continuous Hypothesis Engine with a deployment-owned detection-engineering
pack. It maintains an inspectable path from sourced adversary procedure to local defensive judgment:

```text
procedure → relevance → observable requirement → telemetry → strategy → analytic
          → deployed revision → validation → faceted coverage → gap → action → outcome
```

It inherits CHE's proposition lifecycle, dependency-aware reassessment, review boundary, resolution,
and learning contract. The child profile adds detection artifact roles and operating stages; it does
not copy or weaken CHE policy.

The profile is intentionally platform-neutral. A conforming pack supplies the concrete types,
fields, namespaces, and read-only/import operations for its environment. Initial content belongs in
that pack until another application proves a stable reusable extension boundary.

Conformance establishes that the components are present and connected. It does not establish that a
mapping is correct, telemetry is complete, a repository rule is deployed, a deployed rule works, or
an action improved defense. Those claims require scoped, dated evidence and—where consequential—the
inherited human-review workflow.

Production write-back is outside the initial profile. Models may propose mappings, strategies,
analytics, tests, and priorities, but deterministic systems must establish inventory, compilation,
deployment, and validation facts.
