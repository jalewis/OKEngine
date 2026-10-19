# Auditing and guarded repair

Run the deterministic audit family under one parent receipt:

```bash
bin/framework audit <deployment> --all
bin/framework audit <deployment> --checks schema-drift,grounding,policy --json
```

The command writes one immutable receipt beneath
`.okengine/operations/runs/`. A failed child check makes the parent audit fail; partial success is
never reported as complete.

Repair is an explicit three-phase workflow. It is never an audit side effect:

```bash
bin/framework repair plan <deployment> --from-audit <audit-run-id> \
  --repairs body-integrity,malformed-slugs
bin/framework repair apply <deployment> --plan <plan-id>
bin/framework repair verify <deployment> --plan <plan-id>
```

Plans are created once beneath `.okengine/operations/plans/` and bind the selected repairs to a
digest of the canonical wiki. Creating a plan runs each selected repair's dry run against that
input and records its output in the plan (`dry_runs`); a dry run that fails leaves no plan. Apply
fails if canonical content changed after planning. A successful
plan cannot be applied twice, and verification requires a successful apply receipt. The currently
registered repairs are deterministic, dry-run-first scripts; adding a repair requires an equally
bounded planner/apply contract rather than a general script escape hatch.

`malformed-slugs` renames two kinds of page and rewrites the references to them:

- entity pages whose basename contains whitespace or exceeds 80 characters (#240);
- content pages under a reserved basename (okengine#997), such as `entities/h/e/health.md` or
  `concepts/_/.net-x.md`. "Reserved" is `schema_validator.is_reserved_basename`, the one predicate
  the validator's conformance exemption, the write path's refusal, the minting guard in
  `okf_migrate.write_key` and the `reserved-basenames` deployment check all share. It includes
  every `index-*` name, because the reader, backlinks, lint and critic hide those as the generated
  index tree (`tests/test_reserved_name_consumers.py` binds them). A live page is
  renamed to the stand-in `okf_migrate.mintable_slug` gives (`health-publisher`, `dotnet-x`,
  `concept-index`),
  which is the name the minting guard assigns, so the importer that minted the page converges on
  the renamed copy. The page keeps its `id`.

A reserved-name tombstone is never renamed. Its references are rewritten to its `superseded_by`
target and the tombstone is dropped. If the pointer does not resolve to a live page, the repair
reports it instead. A page in a scaffold shape, such as a pack's `_template.md`, is listed for
review and never renamed, because a pack's crons may read it by that name.

Run the script without `--apply` to see exactly what a plan would do:

```bash
python3 scripts/cron/repair_malformed_slugs.py --vault <deployment>
```

The `reserved-basenames` deployment check reports these pages. A live page under an OKF-reserved
or dot-leading name is a FAIL. Tombstones and scaffold-shaped pages are a WARN. Deploy the engine
release that carries the minting guard before you repair. Otherwise the importer re-mints the old
name on its next run. `deploy-cron-scripts.sh` refuses to stage onto a gateway whose baked validator
predates the shared predicate, so rebuild and recreate the image first.

## Legacy frontmatter shapes (`legacy-scalars`, `legacy-wikilinks`)

Pages written before the write path canonicalized values (okengine#196) can still hold a
schema-declared list field as a scalar (`tags: a, b`, `discovered_by: pack`) or a `[[target]]`
frontmatter value. Until okengine#994 [19], an MCP write could not repair either: the write path
judged the canonical shape an echo of the stored value and kept the legacy shape.
`scripts/cron/repair_legacy_shapes.py` repairs them directly, as a deterministic maintainer:

- `legacy-scalars` rewrites a scalar list field to the list the write path makes of it. A `list`
  field splits on its commas, but never at a comma inside brackets, a date or a number; a field
  declared `prose-list` in `field_shapes` becomes the single item `[whole string]`
  (`okengine.list_scalars`, the rule the write path applies too, okengine#1005);
- `legacy-wikilinks` rewrites a `[[target]]` value, alone or inside a list, to the plain `target`.

They are separate repairs on purpose: the scalar class is small, and the wikilink class can touch
thousands of pages, so each is its own operator decision.

The canonical value is never computed by the repair. It is the enforced write path's own
`_coerce_fm`, loaded from `okengine-mcp/write_server.py`. The repair bypasses the MCP server, not
its invariants:

- a field changes only to the canonical shape of the **same** data: the stored items,
  wikilink-stripped, in order. A value whose canonical form would lose data (`[[x|Display]]`,
  `[[x#anchor]]`, a duplicate item) is skipped and reported as `data-change`;
- an attested page (okengine#980 trust assertions) is skipped whole, and an operator-owned field
  on any page is skipped. Both stay byte for byte;
- immutable fields (`id`, `created`, `created_by`, `discovered_by`) only receive the canonical
  shape of the same value;
- tombstoned and reserved pages are skipped, because the write path refuses them;
- only the repaired keys' bytes change. Every other line, including comments, is left as stored,
  and the rewritten frontmatter is re-parsed and compared with the stored one before it is written;
- the new value is a fixpoint of `_coerce_fm`, so a second dry run shows nothing to do.

Apply runs inside one corpus transaction and re-plans under that fence. It refuses unless the
deployment's runtime stamp (`.hermes-data/engine-runtime.yaml`) is v0.15.7 or later, so the write
path that serves the repaired pages is the one whose canonicalization the repair applied. Roll
the image first.

```bash
# read-only: what each class would change, per field, and what is skipped and why
python3 scripts/cron/repair_legacy_shapes.py --vault <deployment> --classes scalars --list
python3 scripts/cron/repair_legacy_shapes.py --vault <deployment> --classes wikilinks --json

bin/framework audit <deployment> --checks schema-drift
bin/framework repair plan <deployment> --from-audit <audit-run-id> --repairs legacy-scalars
bin/framework repair apply <deployment> --plan <plan-id>
bin/framework repair verify <deployment> --plan <plan-id>   # fails while a repair remains
```
