# Core types & extensions

How the OKF **core** works and how a pack **extends** it. (okengine#90 P2)

## What the core is

The **core** is the universal OKF structure every knowledge vault has. It is **engine-owned** —
defined once in `config/base-schema.yaml`, not re-declared per pack:

- **Core types:** `source`, `concept`, `prediction`, `finding`, `dashboard`, `briefing`, `trend`
- **Core namespaces:** `entities`, `sources`, `concepts`, `predictions`, `findings`, `briefings`,
  `trends` (+ default partitioning & tiering)

Engine-owning the core is what lets **multiple packs compose into one shared graph** (okengine#90):
two packs both putting domain entities under the *same* `entities/` namespace, both citing the
*same* `source` type — without ownership collisions. (Before this, each pack declared and *owned*
the core, so any two packs collided on `source`/`entities`/… — `framework compose-preview` flags
exactly that.)

## What a pack declares now

A pack declares **only its domain**:

```yaml
# pack.yaml
owns:
  types:      [model, lab, researcher, …]   # DOMAIN types only — NOT source/concept/prediction/…
  namespaces: [dashboards, operational]     # DOMAIN namespaces only — the core ones are inherited
```

```yaml
# schema.yaml
types:
  model: {required: [type]}                 # domain entity types (live under the core entities/)
  lab:   {required: [type]}
# source / concept / prediction / dashboard / briefing / trend are INHERITED from the core —
# do NOT re-declare them (re-declaring breaks composition).
```

The core types/namespaces appear in the pack's *merged* schema automatically.

## Cross-cutting optional fields (shipped by the core)

The core also ships a set of universally-useful **optional** fields — every pack gets them free,
validated by extensible base enums. Optional everywhere → composition-safe; a pack that wants one
*present* enforces it in its **ingest workflow**, not the gate.

| Field | Base enum | Notes |
|---|---|---|
| `tlp` | `CLEAR, GREEN, AMBER, AMBER+STRICT, RED` | FIRST.org Traffic Light Protocol — a cross-domain data-sharing standard, baked in |
| `source_kind` | `paper, post, release, news, report` (extensible) | extend with your domain's kinds |
| `severity` | `info, low, medium, high, critical` (extensible) | findings / issues |
| `publisher`, `reliability`, `credibility`, `sensitivity` | pack-supplied | provenance / trust |

**Extend an enum** — your values UNION into the base vocabulary (no re-declaration):

```yaml
enums:
  source_kind: [lab-post, commentary]   # added to base [paper, post, release, news, report]
```

**Where the line is:** a generic *concept* (trust, sensitivity, severity, provenance) or a
recognized *standard* (TLP) belongs in the core as an optional field. A domain *identifier*
(`cve_id`, `mitre_id`, `cusip`) is an `id_authority` in a pack; a domain *integration* (a Splunk
parser) is pack code. The core grows by a handful of concepts **once** and then stops — it does
NOT accumulate a section per industry.

## Extending a core type

To add a **domain field** to a core type, use `extends` — **additive and OPTIONAL only**:

```yaml
# schema.yaml — okpack-ai-research tags each source with a source_kind
extends:
  source:
    fields:
      source_kind: {optional: true}     # add a constrained value via field_enums below
field_enums:
  source_kind: {enum: source_kind}      # the value vocabulary (validated if present)
```

### The one hard rule: you cannot *tighten* a core type

A pack may **add optional fields** to a core type. It may **not** add **required** fields or
otherwise make a core type stricter. Why: under composition, a stricter `source`/`finding` would
**reject another pack's pages** (okpack-cti's findings have `severity`; okpack-fintech's don't —
a shared `finding` cannot require `severity`). The core's required set is fixed; everything a pack
adds is optional. If your domain genuinely needs a field present, enforce it in the pack's **ingest
workflow** (`CLAUDE.md`) + validate the value with a `field_enums` entry — not by tightening the
gate.

(`framework compose-preview` reports ownership/cron/trust collisions today; tightening-detection is
a planned addition.)

## Namespaces

`entities` is a **core, shared** namespace — every pack writes its domain entities there (that
shared graph is the whole point of composition). The entity *types* (`model`, `vulnerability`, …)
are domain-owned; the *namespace* is core. A pack's own generated dirs (`dashboards/`,
`operational/`) stay pack-owned.

## Checklist for a new/migrated pack

1. `pack.yaml` `owns`: domain types + domain namespaces only.
2. `schema.yaml` `types`: domain types only; **don't** re-declare core types.
3. Need a field on a core type? `extends` it (optional) + a `field_enums` entry for its values.
4. `framework compose-preview <this-pack> <other-pack>` → expect **SAFE** with packs you intend to compose.
