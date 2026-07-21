# Multi-source entity resolution — the canonical overlay (design / RFC)

**Status:** draft · **Scope:** engine (data model, write path, reader) + okf-cti instantiation
· **Tracking:** okengine#38

## Problem

An OKF vault ingests from **many sources**, and those sources describe the **same
entities with different — sometimes conflicting — data**. The motivating case is threat
actors (MITRE ATT&CK, ThaiCERT/ETDA, Microsoft, vendor feeds each have an "APT29" page),
but this is **general**: it happens for every entity type two or more sources can both
describe.

- **Vulnerabilities** are the worst case: one CVE is described by CISA KEV, NVD, *and*
  vendor advisories — three sources, same entity, divergent fields.
- **Malware / tooling**: multiple vendors, different names and categories.
- **Identities, campaigns, infrastructure**: same pattern.

### Why today's model is wrong

The current importers **merge in place**: the first source creates `entities/a/apt29.md`,
later sources merge *into* it with "fill-don't-clobber, union the lists." That is lossy:

1. **Conflicts vanish.** When two sources disagree on a scalar (origin, malware category),
   whoever wrote first wins and the disagreement is silently dropped.
2. **Per-field provenance is lost.** You cannot tell which source claimed what.
3. **It degrades as sources are added** — each new feed makes the merge muddier.
4. **Merge-in-place races.** Two importers mutating the same page concurrently is the
   reshelve/clobber hazard we have repeatedly hit.

It also produces the symptom users actually see: a thin page that **links *out* to the
source authority** (mitre.org, ThaiCERT) instead of showing what *we* know, internally
linked. (See the linking-debt note at the end.)

## The established model (this is a solved problem — use its vocabulary)

What we're building has well-studied names; we should build against the prior art rather
than reinvent it.

| Concept | Field / name | Our instantiation |
|---|---|---|
| Keep each source's record intact + a thin canonical that links them | **Registry-style Master Data Management (MDM)** | per-source pages + canonical hub |
| The unified "first place you look" node | **golden record** / master record / identity node | canonical `entities/<slug>.md` |
| The overlay graph linking equivalent records across sources | **identity graph** | canonical → source-page links |
| "these two records are the same entity" links | **`owl:sameAs` / co-reference layer** | canonical `rels` / `same_as` |
| Deciding two records are the same | **entity resolution** (record linkage, reconciliation) | alias-match (crude today) |
| Reconciling conflicting *facts* | **knowledge fusion** / data fusion | Admiralty-weighted survivorship |
| Matching entities across two graphs | **entity alignment** | cross-source alias join |
| The unified view over heterogeneous sources | **mediated schema** (mediator/wrapper) | the canonical page |

**Domain instance:** the security industry already lives this. The Microsoft + CrowdStrike
**"Rosetta Stone"** (June 2025, 80+ deconflicted actors) and **MISP galaxies** (where our
ThaiCERT data comes from) and **Malpedia** are all cross-vendor identity overlays. The
okf-cti spec's **Admiralty reliability (A–F) / credibility (1–6)** is the natural fusion
weight when sources conflict.

## Proposed architecture: canonical hub + per-source observation pages

Two layers.

### 1. Source-observation pages (per source, per entity)

- **One importer owns each; never merged by another.** This eliminates the merge-in-place
  races and makes every import idempotent (overwrite one file).
- Holds **that source's view**: its prose body (you *cannot* deterministically union two
  prose descriptions) + its structured fields + its provenance (source reliability).
- Lightweight; not the primary thing a user reads.

### 2. Canonical entity page (the golden record / identity node)

- The **first place a user looks**, and what every other page **wikilinks to**.
- Carries the **fused structured frontmatter**:
  - **union** the union-able fields (aliases, sectors, observed tools),
  - **Admiralty-weighted consensus** for scalar fields (origin, category): the
    higher-reliability source supplies the headline value,
  - **conflicts preserved with attribution** — never silently dropped; shown as
    "origin: Russia [MITRE A2, Microsoft B2 agree]" or "category: X [src1] vs Y [src2]".
- Hosts the **relationship `[[wikilinks]]`** (uses-malware, uses-technique, attributed
  campaigns) and the **agent's synthesis**.
- Links down to its source-observation pages (the `sameAs` / co-reference edges).

### Assembly: deterministic first, agent only for hard conflicts

- A deterministic **canonical assembler** computes ~95% of the fused view (unions +
  Admiralty rules are mechanical, token-free).
- The **agent arbitrates only genuine conflicts** the rules can't settle, and writes the
  narrative synthesis. This keeps cost down and provenance auditable.

### Lazy split (avoid proliferation)

A single-source entity stays **one page** (implicitly its own canonical). The two-layer
split is created **only when a second source arrives** for that entity. This avoids 3–4×
page proliferation across the long tail of single-source entities.

## Scope — this touches the whole stack

- **Wiki data model / schema** — page kinds (canonical vs source-observation), the
  provenance + conflict frontmatter shape, stable IDs.
- **Write path (`okengine-write`)** — enforce the layering; source pages owned by one
  importer; canonical assembled, not hand-merged.
- **Importers (pack)** — stop merge-in-place; each writes its own source-observation
  page; emit internal `[[wikilinks]]`, not external authority URLs.
- **Canonical assembler (engine cron, token-free)** — union + Admiralty fusion + conflict
  surfacing; wake the agent only for hard conflicts.
- **Reader UI** — render the canonical with its fused view, a **conflict/provenance**
  affordance ("what each source says"), the **related/known-connections** panel (internal
  wikilinks), and source-page drill-down.
- **Migration** — fold the existing ~7,900 entities (many already merged-in-place) into
  the two-layer model without losing curated content.
- **Security profile/spec** — the security-specific instantiation (which types,
  Admiralty arbitration rules) extends the general model.

## Reference data to pull in

- **Microsoft threat-actor mapping** — `github.com/microsoft/mstic`
  `PublicFeeds/ThreatActorNaming/MicrosoftMapping.json` — flat array, 214 records,
  `{Threat actor name, Origin/Threat, Other names}`. Doubles as a **third reference
  source** and a **cross-vendor alias backbone** for entity resolution. (The
  Microsoft+CrowdStrike "Rosetta Stone" is the curated 80-actor subset of this effort.)

## Resolved decisions

Locked in 2026-06-20 (see okengine#38):

1. **Layout — dedicated `observations/` namespace.** Canonical entity pages stay in
   `entities/`; each source's view lives at `observations/<source>/<slug>.md`, one
   importer per source, never merged by another. `observations/` is **`exclude:`-ed from
   the browse rail** (reuses existing machinery) so `entities/` stays canonical-only and
   by-kind / tiering / backlinks aren't polluted. Observations partition by source.
2. **Assembler — hybrid.** A **deterministic, token-free cron** computes the fused
   frontmatter (union additive fields, Admiralty-weighted scalars, conflict detection) —
   ~95%, idempotent. The **agent** does only the synthesis prose (with internal
   `[[wikilinks]]`) and arbitrates flagged hard conflicts, on a conservative wake-gate.
   The agent body is preserved across deterministic re-assembly (as importers preserve
   agent `## ` sections today).
3. **Conflict policy — Admiralty headline + preserve + flag.** The highest-reliability
   source supplies the headline scalar value (recency as tiebreak); **all** values are
   kept with per-source attribution; a material conflict **flags the canonical for review**
   via the existing G3 flag-not-gate queue (`needs_review` + `wiki/_review-queue.md`).
   Requires a **per-field merge policy** in schema (`union | consensus | latest`): aliases
   / sectors / observed-tools always union; origin / category are scalar-consensus.
4. **Split — canonical-always, observations-lazy.** The canonical always lives in
   `entities/` and never moves (no risky promotion; always the wikilink target). A
   single-source entity is **just its canonical page**, recording its one source in
   `refs` / `sources` — no separate observation page. Observation pages spawn **only when
   a 2nd source arrives** for that entity, at which point both sources' distinct views are
   split out and the canonical becomes the fused view. Cheapest migration.
5. **Stable IDs — the canonical slug.** All wikilinks, by-kind browse, tiering, and STIX
   projection key off the canonical slug. Observation pages are addressable for drill-down
   but excluded from the graph/rail. Every source's authority ID (`mitre:G0007`,
   `microsoft:APT29`, the ThaiCERT card) is recorded in the canonical's existing
   `refs: [{std, id, url}]` list — which **is** the `sameAs` / co-reference layer.

### Migration of the existing ~7,900 entities (okengine#41 — resolved)

**Split-forward, no bulk migration, no field loss.** Under decision 4 the existing
`entities/<slug>.md` pages already ARE the canonicals — nothing is moved or re-shelved. A
pre-migration page stays single-layer until a source next imports for it in observation mode;
that observation spawns under `observations/<source>/`, the canonical is re-assembled, and the
page becomes the fused two-layer view. There is no migration script.

**Preserve-on-assembly** makes this safe with zero data loss: `write_canonical` computes its
"owned" set from the *current* fused fields, so any field the arriving observation does NOT
cover (plus curated frontmatter and the agent body) is preserved as-is — never dropped. Only
fields the observation provides update to the source-of-truth value. So the first observation
**augments** a merged page; it never regresses it. (Regression-tested in
`tests/cron/test_canonical_assemble.py::test_write_canonical_migration_preserves_unobserved_fields`.)

**Known limitation:** the pre-migration merged page's per-field provenance is not
reconstructable (the original merge can't be un-done to attribute which source said what), so
a value a *different* source contributed before migration is silently superseded by the first
observation that covers that field, without a surfaced conflict — until every source re-imports
in observation mode. We accept this (it self-corrects as sources re-import); no synthetic
"legacy" observations are minted.

### Alias-fragment prevention and reviewed convergence (#246)

`canonical_assemble` now resolves every source-native observation slug against active canonical
names and aliases before writing. It converges an exact primary-name match or two independent
identity-key matches; a lone alias remains separate and is reported as ambiguous, preserving the
Iridium/Sandworm over-merge guard.

Legacy fragments are handled by `scripts/cron/entity_converge.py`. Its default dry-run emits
candidate source→canonical mappings. Heuristic candidates never authorize mutation: `--apply`
requires an explicit analyst-reviewed YAML `--approve` mapping that still matches the current
candidate set. On approval the lane unions additive frontmatter only, retains the selected
canonical prose, tombstones duplicates, and rewrites address-bearing internal references. This
split is intentional: automatic prevention is conservative; historical cleanup is review-gated.

Cockpit applies the complementary presentation boundary. An entity with no linked source page,
an unresolved review flag, missing required fields, unsupported grounding, or conflicts is shown
as a quarantined unverified draft with its profile collapsed. A tombstone is shown as a retired
duplicate linked to its canonical rather than as a current actor profile.

### Source reliability — declared once, stamped on every observation, human-visible

The fusion weight (decision 3) is **Admiralty reliability**, and it must be both
machine-usable *and* visible to a human judging a conflict. Three layers:

1. **Declared once — a source-reliability registry.** Each ingest source carries a
   standing Admiralty **`reliability` (A–F)** rating, declared in the pack (e.g. a
   `sources:` block in `schema.yaml` or a `feeds`/`sources.yaml`): MITRE ATT&CK ≈ A
   (authoritative, curated), Microsoft ≈ A (first-party vendor on own telemetry),
   ThaiCERT/ETDA ≈ B (community encyclopedia), an unvetted feed ≈ D. This is the
   producer's general reliability; it's set deliberately, not guessed per page.
2. **Stamped on every observation.** Each `observations/<source>/<slug>` page records
   `source`, `reliability` (from the registry), and — where the source expresses
   per-claim confidence — `credibility` (1–6). These reuse the existing okf-cti
   `reliability` / `credibility` enums (the `source` type already requires them), so no
   new vocabulary. The score travels *with* the data.
3. **Visible to humans, two ways:**
   - On the **observation page** itself, the reader's frontmatter info-panel already
     renders `reliability` / `credibility` (this is what the just-built panel is for).
   - On the **canonical page**, the provenance / conflict affordance lists every value
     with its source and score, e.g. *origin: **Russia** — MITRE (A1), Microsoft (B2);
     ⚠ conflicting: Iran — VendorX (D4)*. A human sees exactly who said what, how
     reliable each is, and why the headline value won — and the page is flagged to the
     review queue when the conflict is material.

So reliability/credibility is declared per source, stamped per observation, drives the
deterministic fusion, and is surfaced for human judgment — one scale (Admiralty) end to
end. A reader filter ("show only ≥ B-reliability claims") falls out of this for free.

**Two sourcing paths into the same scale.** Reference-dataset sources (MITRE, ThaiCERT,
Microsoft, KEV, NVD) get a *standing* reliability from the registry, stamped by their
importer. Feed/report sources (news, advisories the agent ingests) are scored *per page*
by the existing source-quality Admiralty pass. Both land as `reliability` (A–F); fusion
treats them identically.

**Registry shape** — lives in the pack's `schema.yaml` (domain ratings are pack data; the
engine reads `source_registry` generically by `key → reliability`):

```yaml
# Source-reliability registry (entity-resolution fusion weight; okengine#38).
# KEY = the observations/<key>/ subdir AND the `source:` stamp on each observation page.
# Reference datasets declare a STANDING reliability here; agent-ingested feeds are scored
# per-page instead and carry their own. credibility is per-claim (from the source, else
# the default). class extends source_kind for non-report producers.
source_registry:
  mitre-attack:
    name: "MITRE ATT&CK"
    class: framework            # framework | vendor | community | government | feed
    url: https://attack.mitre.org/
    reliability: A              # authoritative, peer-reviewed taxonomy
    credibility_default: "2"
    importer: okpack_sec_attack_import.py
  microsoft:
    name: "Microsoft Threat Intelligence"
    class: vendor
    url: https://www.microsoft.com/security/blog/
    reliability: A              # first-party telemetry, broad visibility
    credibility_default: "2"
    importer: okpack_sec_msft_import.py     # planned — the Rosetta Stone / MicrosoftMapping feed
  thaicert:
    name: "ThaiCERT / ETDA Threat Group Cards"
    class: community
    url: https://apt.etda.or.th/
    reliability: B              # well-curated community encyclopedia
    credibility_default: "3"
    importer: okpack_sec_tgc_import.py
  cisa-kev:
    name: "CISA Known Exploited Vulnerabilities"
    class: government
    url: https://www.cisa.gov/kev
    reliability: A              # govt confirmation of active exploitation
    credibility_default: "1"
    importer: okpack_sec_kev_import.py
  nvd:
    name: "NVD (NIST)"
    class: government
    url: https://nvd.nist.gov/
    reliability: A
    credibility_default: "2"
    importer: okpack_sec_nvd_import.py
```

`reliability` / `credibility_default` reuse the existing okf-cti enums (`A–F`, `"1"–"6"`).
The importer reads its own registry entry and stamps `source`, `reliability`,
`credibility` onto each `observations/<key>/<slug>.md` it writes; the assembler reads
`source_registry` to weight conflicts; `validate.py` checks each `reliability` against the
enum and that every observation's `source` resolves to a registry key.

### We're mostly formalizing existing primitives

This design leans on machinery the schema already has, which lowers cost/risk:

- `refs: [{std, id, url}]` → the cross-source identity / `sameAs` layer.
- `id_authority` / `id_field` → already mints `<authority>:<id>` IDs per source.
- `aliases` → name variants (entity-resolution match keys).
- `sources` / Admiralty `reliability` + `credibility` → the fusion weight.
- G3 review (flag-not-gate) → conflict surfacing.
- `partitioning` + `exclude:` → add + hide the `observations/` namespace.

New build: the `observations/` namespace + page kind, the per-field merge policy in
schema, the deterministic fusion assembler (cron), the conservative synthesis/arbitration
agent gate, reader affordances (conflict/provenance view + related-connections panel +
observation drill-down), and the migration.

## Recommendation (one line)

Build a **registry-style MDM / identity-graph overlay**: per-source observation pages
(importer-owned, race-free) + a canonical golden-record hub, **canonical-always /
observations-lazy on the 2nd source**, conflicts arbitrated by **Admiralty-weighted
knowledge fusion** (deterministic, agent only for hard conflicts) — seeded with the
Microsoft cross-vendor alias mapping.

---

### Appendix: linking debt this supersedes

A prior, narrower fix (reader frontmatter panel + importer body summaries) surfaced
structured fields but left two problems this design subsumes: importer bodies link **out**
to mitre.org / ThaiCERT instead of to internal pages (only ~2% of entities carry any
internal `[[wikilink]]`), and the ATT&CK STIX **relationship edges** (group→uses→malware/
technique) are dropped on import. The canonical page is where internal relationship
wikilinks belong; the ATT&CK relationship import and internal-link resolution are part of
this work, and the external "Full profile →" CTA added to ~2,474 pages must be reverted to
plain provenance.
