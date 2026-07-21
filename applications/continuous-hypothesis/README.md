# Continuous Hypothesis Engine application profile

This is a supported composition contract, not an extension and not a domain pack. It says when a
pack plus reusable OKEngine capabilities constitute a Continuous Hypothesis Engine.

A participating pack declares `.okengine/application.yaml`, selects this profile, and binds each
pack-owned proposition class to its lifecycle fields and operating lanes. `framework validate`
then checks the profile, extension requirements, schema bindings, operation references, primary
surfaces, and success measures before deployment.

Passing profile conformance proves that the application is connected coherently. It does not prove
that an analytic conclusion is correct, that a live queue is draining, or that calibration is
improving. Those are deployment acceptance and continuing operational invariants.

Minimal declaration:

```yaml
profile: continuous-hypothesis
profile_version: 1.0.0
bindings:
  propositions:
    - type: prediction
      namespace: predictions
      status_field: status
      open_values: [open]
      resolved_values: [confirmed, refuted, ambiguous]
      confidence_field: confidence
      evidence_field: evidence
      resolution_field: outcome
      review_field: needs_review
      operations:
        reassess: okengine.predictions:regrade
        resolve: okengine.predictions:grade
        measure: okengine.predictions:calibration-refresh
surfaces:
  dependency_explanation: dashboards/reevaluation-impact
  assessment_review: dashboards/assessment-review
  portfolio_learning: dashboards/calibration
queues:
  affected_reassessment: okengine.predictions:regrade
  assessment_review: dashboards/assessment-review
success_measures:
  caused_reassessment_precision: dashboards/reevaluation-impact
  review_queue_age: dashboards/assessment-review
  resolution_yield: dashboards/prediction-portfolio-watch
  calibration_or_outcome_quality: dashboards/calibration
```

The profile deliberately requires `okengine.reevaluation` and `okengine.assessments`. Predictions,
completeness, events, and domain-specific proposition classes remain optional integrations. This
keeps CHE horizontal while preserving a strong assessment and caused-reassessment floor.
