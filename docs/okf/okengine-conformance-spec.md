# OKEngine OKF Conformance Profile — v0.1

**Status:** normative for the engine v0.11.x series (profile v0.1, stable across the range). This is a *profile*, not a fork of
the format.

## 0. Scope & relationship to OKF

The **[Open Knowledge Format (OKF)](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)**
is Google's vendor-neutral, intentionally minimal markdown-+-YAML format for agent
knowledge: a directory of markdown files with YAML frontmatter, one required field
(`type`), everything else optional, linked into a graph. OKEngine **consumes** OKF;
it does not define it.

This document specifies the **additional, normative requirements** that an
*OKEngine-maintained* vault, writer, reader, and engine meet **on top of** base OKF
— schema discovery, immutable identifiers, tombstones, review flags, tiers,
reserved files, and the enforced write path. A base-OKF document is *not*
necessarily OKEngine-conformant; an OKEngine-conformant vault *is* valid base OKF
(modulo the link-dialect note in §1).

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are
to be interpreted as in RFC 2119. "The engine" = the reference implementation in
this repository.

## 1. Page format

1.1 A page **MUST** be a UTF-8 markdown file with optional leading YAML
frontmatter delimited by a `---` line, a YAML block, and a closing `---` line.

1.2 Frontmatter, when present, **MUST** parse as a YAML mapping. A page whose
frontmatter delimiters are present but unparseable is **non-conformant** (the
engine quarantines it for repair; it is never written half-fixed).

1.3 The body **MAY** contain `[[wikilinks]]` and `![[embeds]]`. NOTE: base OKF uses
portable markdown links; `[[wikilinks]]` are an OKEngine dialect. A vault that uses
them is OKEngine-conformant but **SHOULD** be projected to portable links for
external (non-OKEngine) consumption.

## 2. Frontmatter fields

2.1 Every page **MUST** carry `type` (a non-empty string). This is the sole field
base OKF requires, and OKEngine inherits it as the universal floor.

2.2 Every page **SHOULD** carry `id` (see §5). `id` is in the WARN tier: its
absence is reported but does not reject a write.

2.3 A writer **MUST NOT** silently drop frontmatter fields it does not recognize.
Undeclared/extra fields **MUST** be preserved verbatim (the format is
open/extensible — unknown data passes through). Fields a pack marks in
`protected_fields` are guarded against loss explicitly.

2.4 The universal interoperability fields a page **MAY** carry are: `id`, `title`,
`description`, `status`, `created`, `updated`, `version`, `created_by`,
`last_modified_by`, `confidence`, `tags`, `aliases`, `maintained_by`,
`discovered_by`, plus the cross-cutting **optional** fields `tlp`, `sensitivity`,
`source_kind`, `publisher`, `reliability`, `credibility`, `severity` (okengine#90 —
validated by the base's extensible enums: `tlp` ships the FIRST.org standard set; a
pack extends the rest with domain values). These are defined once in the engine base
schema; packs add domain fields on top.

## 3. Schema discovery & the base floor

3.1 A vault **MAY** declare a `schema.yaml`. The schema governing a page is found by
**walking up** from the page's directory to the nearest `schema.yaml`, else the
vault root's. A sub-tree **MAY** carry its own `schema.yaml` (a nested sub-domain).

3.2 The engine merges an **engine-owned base schema UNDER** every pack schema. The
base owns the universal requirements **and the universal OKF core**. A pack declares
its **domain** `types`/namespaces (+ any
domain `partitioning`/`tier`/`permissions` and extra keys):
- `okf.required` — the merged requirement is the **union** of base and pack; `type`
  is always present and the floor never loosens.
- `okf.should` — base-owned advisory tier (currently `[id]`).
- `strict_types` — open (`false`) by default. A pack **MAY** opt into `true`; the
  setting is monotonic, so a pack cannot loosen a stricter base/composed host.
- the **OKF core** (okengine#90) — engine-owned DEFAULTS merged under the pack: the
  core `types` (`source`/`concept`/`prediction`/`finding`/`dashboard`/`briefing`/
  `trend`), the core `partitioning.namespaces` + `tier`, and the cross-cutting
  optional fields/enums (§2.4). A pack **inherits** them and adds domain types on top;
  it **MUST NOT** *own* a core id, and **MUST NOT** *tighten* a core type (add required
  fields) — both are composition conflicts (`framework compose-preview` flags them).

3.3 If the base schema is absent/unreadable, validation **MUST** fail safe (fall
back to the pack's own `okf`, never crash a write).

## 4. Type conformance

4.1 A page is **type-conformant** when its `type` satisfies the governing schema:
the type's declared `required` fields are all present.

4.2 `strict_types` (default `false`): when `false`, a page whose
`type` is **not** in the schema's `types` is allowed if it satisfies `okf.required`
(open/extensible). When `true`, an unknown `type` is **rejected** against the full
composed type map. A declared `type_aliases` value is accepted as its target and
write boundaries store the canonical target, preventing new taxonomy fragments.

## 5. Identifiers (`id`)

5.1 `id` is the page's **immutable, type-independent** identity. Once assigned it
**MUST NOT** change; re-identifying a page is a versioned migration
(`NORM_VERSION`), never an in-place edit.

5.2 An `id` has the form `<scope>:<key>` where `key` is normalized (NFKD
ascii-fold → lowercase → hyphenated; bounded length). Two kinds:
- **authority id** — `<authority>:<local-id>` (e.g. `mitre:t1059`) when the owning
  type declares `id_authority` + `id_field`. Authority ids are **deterministic**:
  the same external entity yields the same id across packs.
- **minted slug** — `<namespace>:<slug>` minted once at creation from the page's
  natural key (`title` > `name` > path stem) when no authority applies.

5.3 `aliases` **MAY** list alternate/legacy ids; the id resolver consults them.

5.4 **Convergence:** when a writer creates a page whose authority id already exists
in the vault, the writer **MUST** merge into the existing canonical page rather than
create a duplicate. Minted-slug collisions **MUST NOT** auto-merge (they are flagged
for review). See §10 for ownership during merge.

## 6. Tombstones

6.1 A knowledge page **MUST NOT** be hard-deleted. Removal is a **tombstone**: the
file is retained with `status: tombstoned`.

6.2 A tombstone **SHOULD** carry `superseded_by` (an `id`) when the page was merged
into or replaced by another.

6.3 A tombstoned `id` **MUST NOT** be resurrected — a later write to that id is
refused; write to its successor instead.

## 7. Reserved files

7.1 The following filenames are **engine-managed structural files**, not
agent-writable knowledge pages, and **MUST NOT** be created/edited through the
write path: `index.md`, `log.md`, `AGENTS.md` (the agent contract; the engine's
runtime alias is the vault `CLAUDE.md`), `HOT.md`, `BUNDLE.md`, `HEALTH.md`,
`README.md`, sharded index files (`index-p*`), and any `_`-prefixed file.

7.2 A vault **MUST** maintain an append-only `log.md`; every accepted write through
the enforced path appends one line. `index.md` is a generated catalog. A vault
**SHOULD** ship an `AGENTS.md`-style agent contract. A schema **MAY** override the
reserved-filename set via `reserved_files`.

## 8. Review flags (soft, never gate)

8.1 Review is a **flag, not a gate**. A schema **MAY** declare `review`
(`confidence_field`, `confidence_review_values`, `review_on_change_fields`).

8.2 When a write asserts a categorical review value (or changes a watched field),
the write **MUST** still land, and the page **MUST** be flagged (`needs_review:
true` + an entry in the review queue). Numeric / `low|medium|high` confidence
**MUST NOT** flag. A reviewer (human) clears the flag.

## 9. Tiers (derived, never stored)

9.1 Hot/warm/cold tiers are **DERIVED at query time** from a schema-declared date
field; a tier **MUST NOT** be written into a page's frontmatter. A schema **MAY**
declare `tier` (per-namespace `date_field`, `hot_days`, `warm_days`, open-status
floor). Absent a declaration, the engine applies defaults.

## 10. Write governance (the enforced contract)

10.1 In an OKEngine deployment, agent writes **MUST** go through the enforced MCP
write path, which validates the composed page against the governing schema
**before** touching the filesystem and refuses non-conformant writes. The
file-tool write-guard is the backstop.

10.2 **Permissions (structural, hard):** a schema **MAY** mark a namespace
`create:false`/`update:false` (human-authored); writes to it through the agent path
**MUST** be refused. `delete` is `false` everywhere (see §6).

10.3 **Ownership (composition):** a type **MAY** declare an `owner` pack and
per-field `field_owners`. On convergent merge, a non-owner **MUST NOT** clobber an
owned field; conflicting writes are kept-and-flagged, not applied.

10.4 **Validation profiles.** The reference validator
(`tools/schema_validator.py`) exposes two profiles over the same checks; an
implementation **MUST** choose per the table below:

| Consumer | Profile | Behaviour on a missing/broken schema or validator error |
|---|---|---|
| agent runtime / write path / file write-guard | **runtime** (`schema_reject_reason`) | **fail-OPEN** — pass (never brick a write on infra); only a real violation rejects |
| CI / release gate / public conformance tests | **strict** (`conformance_reject_reason`, CLI `--strict`) | **fail-CLOSED** — treat it as a failure (a release **MUST NOT** pass on a silently-disabled check) |

Both profiles treat genuinely out-of-scope files (not `.md`, outside
`apply_under`/the schema root, excluded, reserved) as not-applicable (pass). The
strict profile **MUST** distinguish that "out of scope" from "schema
invalid/unavailable" (the latter is a failure).

## 11. Conformance profiles

An implementation declares which profile(s) it meets:

- **Document** — a single page satisfies §1–§2 (parseable frontmatter, `type`,
  field pass-through). The minimum unit; equivalent to base OKF plus field
  preservation.
- **Vault** — a tree satisfies §3 (schema discovery), §6 (tombstones, by retaining
  them), §7 (reserved files + `log.md`), and is internally consistent (every page
  Document-conformant).
- **Writer** — a producer of pages satisfies §2.3 (no field loss), §4
  (type-conformance before write), §5 (assigns/preserves `id`, converges authority
  ids), §6 (tombstone, don't delete/resurrect), §8 (review flags), §10
  (validation + permissions + ownership).
- **Reader** — a consumer satisfies path-confinement (every read resolved inside
  the vault root; `..`/absolute refused), output sanitization for rendered HTML,
  and operates **read-only** without requiring the engine or write path.
- **Engine** — the full maintenance layer: everything above plus schema/lint/repair
  drains, index/health/tier derivation, and N-pack composition.

## 12. Conformance clause → reference tests

The reference implementation's tests pin these clauses (where practical):

| Clause | Test(s) |
|---|---|
| §2.1/§3.2 base-floor `okf.required` union | `tests/test_schema_validator_base.py` |
| §3.2/§4.2 pack-opt-in `strict_types` | `tests/test_schema_validator_base.py`, `tests/cron/test_schema_lib_base.py` |
| §2.2 `okf.should` WARN tier (`id`) | `tests/test_schema_validator_base.py` |
| §5.2 id form / normalization | `tests/cron/test_id_lib.py` |
| §5.1/§5.4 id index, convergence, collisions | `tests/cron/test_id_index.py`, `tests/test_converge.py` |
| §5 / §10.3 converge + ownership + provenance | `tests/test_write_server_converge.py` |
| §2.3 field-loss preservation | `tests/test_write_server.py` |
| §6 tombstone / no-resurrect | `tests/test_write_server_converge.py`, `tests/test_write_server.py` |
| §8 review flags (flag-not-gate) | `tests/test_write_server.py`, `tests/test_write_server_converge.py` |
| §10.2 namespace permissions | `tests/test_write_server.py`, `tests/test_write_server_converge.py` |
| §9 derived tiers | `tests/cron/test_tier_lib.py` |
| §10.4 runtime vs strict validation profiles | `tests/test_schema_validator_base.py` |
| §11 pack composition / ownership disjointness | `tests/test_pack_meta.py`, `tests/cron/test_merge_packs.py` |

## 13. Versioning

This profile is versioned independently of the engine; profile `v0.1` is stable across the engine
v0.11.x series (and back to v0.2.0 — the normative clauses have not broken). Breaking changes to a
normative clause bump the profile's minor version.
The base OKF version this profile targets is OKF **v0.1**.
