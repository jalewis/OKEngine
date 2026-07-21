# Human review

Human review is an engine governance capability. It is not a CHE decision rule and it is not a
pack-owned mutation path. Packs identify the material that needs review; OKEngine owns reviewer
identity, transitions, optimistic locking, audit records, and the public/private boundary.

## Records and states

The source of truth is a durable YAML ledger under `wiki/operational/reviews/`. A request binds its
subject path, version, SHA-256 content hash, structured reasons, and complete evidence reference set.
It moves through `open`, `in-review`, `changes-requested`, `approved`, `rejected`, or `dismissed`.
Page fields such as `needs_review`, `review_state`, `review_id`, `reviewed_by`, `reviewed_at`, and
`reviewed_version` are projections for fast reads and compatibility; ordinary entity updates cannot
forge them or clear an existing flag.

Decisions are version locked. If the page differs from the version/hash opened by the reviewer, the
operation returns `409` and writes neither the page nor the decision. Approval and dismissal clear
quarantine. Request changes, rejection, and defer keep it. Every non-approval disposition requires a
note. A later content edit removes the stale approval projection and opens a
`changed-after-approval` request.

Machine checks are separate records with `supported`, `unsupported`, or `unresolved` outcomes. They
help a reviewer, but never set `reviewed_by` and never satisfy a human-required policy. The
`review-drain` selector passes every cited local source to `record_machine_review`; it does not
silently truncate evidence.

## Operator workflow

The Ops tab's Human review card opens the complete paginated worklist. Selecting a page opens its
quarantined content; **Review this page** shows the exact reasons, all evidence values and link
resolution, machine checks, assignment, and history. Reviewers may assign the request to themselves,
then approve, request changes, reject, dismiss, or defer. A confirmation records that the scoped
content and evidence were examined.

The generated Markdown dashboard remains a portable summary, not the interactive system of record.
The APIs are `GET /api/reviews` (pagination and reason/type/state filters) and
`GET /api/review?path=…` (workspace detail).

CLI decisions use the same state-machine functions:

```sh
framework review /path/to/pack
framework review /path/to/pack --approve entities/a/acme --by "Jane"
framework review /path/to/pack --page entities/a/acme \
  --decision request-changes --by "Jane" --note "Origin claim needs a direct source."
```

## Secure deployment

Cockpit's vault mount remains read-only. Review controls fail closed unless the review API, token,
and server-side reviewer identity are set, plus one explicit browser trust mode:

- `OKENGINE_REVIEWER_NAME` — server-side human identity;
- `OKENGINE_REVIEW_TOKEN` — shared secret for the bridge-only writer;
- `OKENGINE_REVIEW_API=http://okengine-review-write:8731`.
- `OKENGINE_READER_PASSWORD` — browser Basic authentication; **or**
- `OKENGINE_REVIEW_TRUSTED_NETWORK=1` — no login prompt; every browser that can reach the Cockpit
  is trusted to act as the configured reviewer.

Start the narrowly scoped writer explicitly:

```sh
docker compose --profile review up -d --build
```

`okengine-review-write` publishes no host port, mounts the vault read/write, requires bearer auth,
and exposes only health, assignment, human decision, and machine-check operations. Cockpit never
accepts reviewer identity from browser JSON and applies a same-origin action header check in addition
to Basic auth when that mode is selected. Without Basic auth, trusted-network mode must be explicit;
otherwise the Cockpit has no usable review mutation capability. Trusted-network mode is appropriate
only when network placement already defines the operator boundary.

Review-required pages expose the same **Review this page** action whether the page is a normal
record or quarantined by a trust gate. Packs can also place a scoped launcher in any declarative
Cockpit tab while retaining the complete Ops worklist:

```yaml
- title: Awaiting application review
  view: review-queue
  review_types: [assessment, prediction, analytic-hypothesis]
```

The launcher constrains the worklist to the declared record types; filters, pagination, evidence
detail, authorization, decision history, and the write path remain engine-owned.

## Pack policy

High-stakes types enter review when declared by the composed schema:

```yaml
review_required_types: [briefing, prediction, lacuna]
```

Grounding failures and `needs_review: true` are universal. Write policy may add reasons for
categorical confidence or selected field changes. New reason vocabularies belong in pack policy;
authorization and lifecycle transitions remain engine-owned. Machine disposition is not enabled by
default and must never clear a human-required class.

## Backlog migration and recovery

Migration is a dry run unless `--apply` is explicit:

```sh
framework review /path/to/pack --migrate
framework review /path/to/pack --migrate --apply
```

It creates idempotent open records for flagged pages, reports reason/type/age counts, and imports
legacy `reviewed_*` metadata as historical decisions labelled with unknown evidence scope. It never
auto-approves the flagged backlog. Rerun the dry run and compare the Cockpit total after applying.

For recovery, inspect the subject page, its matching ledger YAML, and `wiki/log.md`. A stale conflict
requires refresh, not manual metadata editing. If an interrupted storage operation is suspected,
restore the page and ledger together from version control, then resubmit; the operation holds the
review lock and rolls back both projections when its atomic replacements fail.
