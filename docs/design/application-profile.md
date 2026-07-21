# Application profiles

**Status:** Shipped in #247  
**Reference implementation:** `applications/continuous-hypothesis/application.yaml`

An OKEngine application profile is a versioned, engine-owned composition contract. It is not a
pack, extension, runtime service, or deployment. It defines the smallest combination of reusable
capabilities and pack bindings that OKEngine supports as one recognizable operating product.

## Files and ownership

| File | Owner | Purpose |
|---|---|---|
| `applications/<id>/application.yaml` | engine | Versioned supported profile and conformance contract |
| `<pack>/.okengine/application.yaml` | pack/deployment | Profile selection plus domain bindings |
| `<pack>/schema.yaml` | pack | Proposition types, lifecycle fields, vocabulary, and namespaces |
| `<pack>/.okengine/extensions.yaml` | deployment | Enabled reusable capabilities and their configuration |

The catalog profile declares required extension floors, the operating-loop dependency order,
binding grammar, safety policy, primary surface and work-queue roles, and success-measure roles. The pack
declaration binds those roles to concrete types, fields, operations, and dashboard paths.

## Profile inheritance

A profile may extend one catalog parent. Resolution is deterministic: the complete parent contract is
loaded first, then the child adds to it. The child retains its own identity and version.

```yaml
schema_version: 1
id: specialized-application
version: 0.1.0
name: Specialized application
extends:
  profile: continuous-hypothesis
  version: ">=1.0.0"
```

The v1 rules are deliberately narrow:

- only one direct parent is accepted, although that parent may itself have one parent;
- `extends.version` is a semantic-version floor and the installed parent must satisfy it;
- missing parents, self-reference, and cycles fail before pack validation;
- extension requirements and binding requirements are additive, with the stricter minimum retained;
- surfaces, queues, and measures are stable ordered unions (parent entries first);
- inherited loop stages precede child stages; an identical duplicate is harmless, while a different
  stage with the same ID is a conflict;
- child stages may depend on inherited stages;
- a child may add or tighten policy, but cannot change an inherited `true` invariant to another value;
- validation errors name the child profile and inherited field or stage responsible.

`load_profile()` returns the effective composed contract. Callers do not need separate inheritance
logic, and standalone profiles remain unchanged.

## Generic artifact-role bindings

Profiles may require pack-owned artifact roles in addition to proposition classes. Roles describe
structural responsibilities without teaching the engine domain nouns:

```yaml
binding_contract:
  required_roles:
    evidence_item:
      minimum: 1
      required_fields: [type, namespace, behavior_field, evidence_field, as_of_field]
      required_operations: [refresh]
      allow_multiple: false
```

A pack binds the role to its effective schema and available operations:

```yaml
bindings:
  roles:
    evidence_item:
      - type: threat-procedure
        namespace: procedures
        behavior_field: behavior
        evidence_field: sources
        as_of_field: as_of
        operations:
          refresh: import-threat-procedures
```

The role contract accepts only `minimum`, `required_fields`, `required_operations`, and
`allow_multiple`. A role supplied by a required upstream pack may add
`provided_by: <pack-id>`; that ID must appear explicitly in the declaring pack's `pack.yaml`
`requires` list. This permits a composed application to bind a foreign-owned role without copying
its schema into the consumer. The composed-vault gate remains responsible for verifying the
producer's actual type, namespace, and field contract. Pack declarations fail closed on unknown
roles, binding keys, or operation keys.
Every bound type and namespace must exist in the effective base-plus-pack schema; every field-role
value must name a field in that schema; and every operation must resolve to an enabled extension
operation or pack job. Duplicate bindings and cardinality violations are rejected. Inheritance keeps
parent roles and permits a child to add fields, operations, or a stricter minimum; it cannot broaden
an inherited single-binding role to multiple bindings.

## Validation

`framework validate <pack>` discovers an optional `.okengine/application.yaml` and rejects:

- an unknown profile or incompatible profile version;
- absent or too-old required extensions;
- proposition types, namespaces, or required lifecycle fields absent from the pack schema;
- missing or overlapping open/resolved states;
- operation references that do not resolve to enabled extension operations or pack jobs;
- bound proposition types omitted from dependency indexing;
- missing primary surfaces or success measures;
- duplicate or incomplete proposition bindings.
- missing, duplicate, over-cardinality, or schema-invalid generic role bindings;
- external role providers not explicitly declared as pack dependencies;
- inheritance cycles, incompatible parents, conflicting stages, and weakened safety invariants.

An absent application declaration remains valid. Packs may be reusable domain components without
constituting a supported application.

## CHE profile

The first profile is `continuous-hypothesis` v1. It requires the reusable assessment and
dependency-aware reevaluation extensions. A conforming pack binds one or more proposition classes
to reassessment, resolution, and measurement operations, and names concrete surfaces for dependency
explanation, analyst review, and portfolio learning.

Its synthetic fixture binds both a forecast and a diagnostic proposition. The integration proof
checks that a changed evidence page selects only the proposition that cites it and validates a
portable lifecycle receipt preserving:

- prior and new assessment state;
- causal and considered evidence;
- evaluator and method;
- explicit human approval;
- resolution status and outcome;
- the portfolio measure receiving the result.

## Conformance is not deployment acceptance

Profile conformance answers: “Are these components connected in a supported way?” It does not
answer: “Are the conclusions correct or is this deployment improving?” A live deployment must
separately prove job health, exact caused reassessment, queue drainage, review timeliness,
resolution yield, calibration or outcome quality, and action effectiveness where applicable.

This boundary prevents a synthetic test from self-certifying analytic quality while still making
composition errors fail before runtime.
