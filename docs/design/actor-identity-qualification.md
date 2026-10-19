# Source-backed identity qualification, contract v1

Status: Active design under okengine#972 (parent #971). **No interface, store,
fixture runner, migration, or enforcement in this document is implemented.**
The existing code remains authoritative until its implementation issues pass.
This milestone freezes the contract and acceptance inputs; it does not qualify
the corpus or authorize a live repair.

Companion artifacts: [inventory](actor-identity-inventory.md) and
[acceptance manifest](actor-identity-acceptance.md). Fixture IDs below name
future executable acceptance contracts, not currently passing tests.

## Boundary and ownership

The engine owns authenticated principals, protected evidence receipts,
qualification lifecycle, transactional mutation, eligibility reads, and repair
recovery. Composed packs own role semantics, authority policy, assertion
interpretation, contextual mention resolution, and analysis. The mechanism must
be optional by composed schema declaration: a type named `actor` alone does not
activate a threat-intelligence policy. No publisher, product, country, adversary,
or authority-ID vocabulary belongs in the kernel.

An adopted pack supplies `identity_qualification/v1`: owned types, canonical
identity fields, alias fields, protected derived fields, policy IDs/digests,
bounded evaluators and contextual resolvers. Composition rejects duplicate type
owners, missing adapters, conflicting capabilities, unknown evaluators and
changed policy digests without an explicit activation plan. A never-adopted
corpus retains existing type behavior. Once activated, an engine-owned durable
activation record binds the governed type owner and policy revision. Removing
the declaration or type owner does not opt out: it is an operational fault,
refusing governed mutations and active eligibility. Only an authenticated,
approved versioned deactivation/migration plan with exact census, consumer
impact and CAS can change that record. Invalid adopted configuration fails
closed; it is not equivalent to opting out (Q22).

Owning maintainer roles are in the inventory. The release owner must record the
actual responsible person and review capacity in the activation plan. Policy
composition and write grants reuse [policy-plane](policy-plane.md); domain
logic must not grow the writer facade.

## Decisions and independent dependencies

Qualification means: a captured source supports the exact assertion that this
discrete named subject has the pack-declared role, under the recorded policy.
It certifies evidence linkage and bounded interpretation, not universal truth,
country attribution, operational responsibility or source independence.

A source assertion, a verified authority record, or an authenticated human
evidence-backed decision can support admission. Model-authored profile prose,
source grade, news count, existing roster membership, approval of another claim,
an ID's syntactic shape, and caller flags cannot. An unknown subtype and absence
of a catalog ID are compatible with qualification (Q01–Q05).

Each canonical assertion has one or more independent basis records. Aliases
have their own basis records asserting `alias refers to subject`, not a shared
all-or-nothing identity decision. Revoking an alias disables only that match
term. Withdrawing a basis invalidates its exact dependents; surviving canonical
support preserves identity eligibility. Withdrawing the last basis makes the
identity stale. A new contrary assertion creates a reviewed conflict instead
of silently selecting whichever source was read last (Q07, Q08).

Use `identity_digest = sha256(canonical JSON of owning type, normalized
canonical name, stable subject ID, owning policy digest)`. Per-alias digests bind
the normalized alias and subject ID separately. Nonidentity metadata edits
change page hash/version but reuse unchanged qualification. Renaming, type
promotion, merge/supersession, identity ID changes and relevant policy/evidence
changes require decision re-evaluation. IDs remain immutable: re-identification
requires an explicit versioned migration/successor, never an in-place ID edit.
Historical basis records remain immutable. A merge must explicitly rebind each
preserved basis to the resolved
subject; it cannot union qualification flags.

## Protected storage and provenance

Keep Markdown under `wiki/` canonical. Use the existing durable operation
namespace, not a new database. New v1 artifacts are engine-owned files under:

| Path relative to vault | Record and access |
|---|---|
| `.okengine/operations/identity/candidates/<uuid>/revisions/<sha256>.json` | Immutable normalized proposed page, notes and proposed evidence; no canonical actor seat |
| `.okengine/operations/identity/candidate-index.json` | Engine discovery-key → candidate mapping, overlap disposition and generation |
| `.okengine/operations/identity/decisions/<uuid>.json` | Immutable canonical or alias basis/disposition and dependencies |
| `.okengine/operations/identity/acquisitions/<uuid>.json` | Immutable server-recorded authenticated acquisition attestation |
| `.okengine/operations/identity/texts/<sha256>.txt` | Exact UTF-8 extracted evidence bytes, content addressed |
| `.okengine/operations/identity/subjects/<sha256(subject-id)>.json` | Replaceable current projection with generation, identity digest, basis IDs, alias IDs, state and last transaction |
| `.okengine/operations/identity/activation.json` | Protected current adopted owner/type/policy revision, activation plan/receipt IDs and generation; only transactional activation or approved deactivation changes it |
| `.okengine/operations/identity/transactions/<uuid>/` | Durable manifest, preimages, staged postimages, commit/abort marker and recovery disposition |
| `.okengine/operations/plans/<plan-id>.json` | Existing immutable plan registry; new identity repair/activation plan kinds |
| `.okengine/operations/runs/identity-write/<run-id>.json` | Engine authoritative operation receipt, using run lifecycle |

Ordinary page, model, deterministic-import and review-note grants cannot write
these paths. Pending content is reachable through authenticated engine worklist
and candidate-detail APIs; it is not hidden in an unowned queue. Canonical page
fields `identity_state`, `identity_decision_ids`, `identity_generation` and legacy
`actor_identity_validated` are server projections only. Ordinary administrative
page editing also cannot manufacture decisions. Authenticated human identity
review is a distinct narrow operation. Existing generic page review approval
and dismissal do not qualify identity.

Candidate deduplication is server-owned under the same transaction: normalize
owning type/name using the pack's versioned normalization policy and form a
discovery key from owner/policy digest/type/name plus authenticated stable
capture/import principal, connector/native source identity, native revision and
text digest. Acquisition receipt UUID is a dependency, not discovery identity:
a fresh receipt for the same authenticated source revision/text resolves to
the same candidate, preserving both receipt associations. Different request IDs rediscovering
the same key return the same candidate ID with no duplicate queue row. Check
canonical IDs and qualified aliases before claiming the key; a supported exact
identity resolves to the existing subject, while shared/ambiguous alias overlap
creates one visible pending conflict, never auto-merges or a duplicate actor.
Different acquisitions may add immutable evidence revisions to a candidate,
but cannot merge two discrete identities solely by shared normalized name.
Index and candidate revision commit together (Q23).

This subtree is persistent admission evidence, not disposable operation state.
`scripts/framework_backup.py` must explicitly preserve and verify the complete
`identity/` subtree (activation marker, candidate index, decisions, extracted
texts, subject projections, idempotency/transaction manifests and pre/postimages)
under the stable read fence, plus its transitive referenced acquisition objects,
source compilations, operation plans and authoritative run receipts. Capture
the corpus epoch/journal and canonical wiki from the same recovered checkpoint.
Do not archive live locks/PIDs or credentials. Restore verifies hashes and
authority/dependency closure before opening eligibility; it rebuilds disposable
indexes and performs manifest recovery before reads. Missing protected records
cause an explicit blocked eligibility/readiness result, not acceptance of page
flags. Ordinary unrelated run-state retention can remain unchanged. The existing
operation-run doc calls operations excluded from backup while the current
backup exclusion list does not exclude that entire namespace; implementation
must reconcile that documentation with explicit required persistent inclusion.
No deletion/pruning may remove a basis, receipt, text or transaction needed by
current decisions, unresolved work or retained historical dispositions. Any
retention release uses a dependency-aware approved plan. Preserve inherited
sensitivity, trust-boundary storage/access and backup confidentiality. Engine
#973 owns persistence, #974 restore/recovery acceptance and #975 deployment
backup evidence (Q24).

Acquisition records require: `api=1`, server `acquisition_id`, connector/capture
principal, operation run ID, source native identity, requested/final URL or
authenticated local import identity, observed time, object reference/hash,
extracted text reference/hash, extractor build/version/config digest and
source revision. The engine resolves these from an authenticated registered
capture/import operation; the proposed request supplies only a receipt ID.
It verifies a completed authoritative run, allowed principal, declared source
ownership, exact receipt bytes, object hash and text hash. Content-addressed
filenames alone provide integrity, not authenticity. Existing capture objects,
revision metadata and `content_state: captured` do not supply this attestation
today; implementation must add it without re-fetching unchanged valid evidence.

An evidence location is a half-open UTF-8 byte span `[start,end)` in the exact
protected extracted text. Require `0 <= start < end <= len(bytes)`, valid UTF-8
boundaries, `span_sha256 == sha256(bytes[start:end])`, and assertion text exactly
equal to decoded span. Keep sufficient neighboring sentence/context spans to
interpret the subject, negation, quotation and hypothetical scope. If a source
page is cited, record its stable ID and hash plus compilation receipt mapping
its content to this acquisition. Do not evaluate generated source summaries
when only the underlying capture establishes provenance. No caller replacement
text may stand in for inaccessible bytes. Path escape, symlink escape, hash
mismatch, fabricated receipt or unrelated source ID refuses the request (Q03).

The protected decision schema is fixed as a record with: `api=1`, `decision_id`,
`subject_id`, `identity_digest`, `kind=canonical|alias`, nullable `alias`,
`assertion`, `policy_id`, `policy_digest`, `method=source-assertion|authority-record|human`,
`outcome=qualified|pending|rejected|revoked`, nonempty structured `reasons`,
`basis_ids`, `evidence[]` (acquisition ID, source ID/revision/hash, text hash,
span/context locations), `dependencies[]` (kind, ID and hash), authenticated
`principal` (ID and role), timestamp, transaction ID and superseded decision IDs.
The engine creates IDs, principal, timestamp and digests; caller attempts to
provide protected facts are rejected, not silently ignored. Empty supporting
evidence cannot produce a qualified outcome. Rejection means the assertion is
unsupported under policy, not that the opposite claim is universally true.

Versioned pack evaluators accept only resolved evidence and produce bounded
proposed outcomes/reasons. A regex may recognize clear assertion shapes; it
cannot certify arbitrary prose. Negation, wrong-subject clauses, hypothetical
or quoted examples, conflicting assertions, truncated context and unavailable
evidence produce pending/rejected outcomes with reasons. A verified authority
adapter must read the actual typed record from authenticated acquisition and
its exact revision; URL plus ID fields are not sufficient. Human qualification
uses existing authenticated review bridge identity and hash locking, with an
identity-specific proposition, explicit evidence references and nonempty
rationale. A claimed reviewer name in request JSON is never accepted (Q04).

## Frozen transactional writer API

Implement a shared installed provider `okengine.identity.write_v1`, called by
MCP write services and deterministic adapters. The public request is:

```text
IdentityWriteRequest {
  api: 1, idempotency_key: nonempty string,
  operation: propose|maintain|evaluate|human-decide|alias-propose|alias-evaluate|
             promote|create-successor|revoke|retire|activity-replace|repair,
  subject_ref: candidate UUID or stable canonical ID|null,
  predecessor_ref: retired canonical ID|null (create-successor only),
  requested_path: optional wiki-relative path,
  patch: structured field/body operation (no principal or decision fields),
  evidence_receipt_ids: list of protected receipt IDs,
  expected: {page_sha256: hash|absent, candidate_sha256: hash|absent,
             identity_generation: integer, policy_digest: hash,
             evidence_digests: mapping, corpus_epoch: integer,
             predecessor_sha256: hash|null,
             predecessor_generation: integer|null},
  plan_id: optional immutable approved repair/activation plan
}
write_v1(context: server-resolved CallerContext, request) -> IdentityWriteResult
IdentityWriteResult {
  api: 1, outcome: accepted|pending|rejected|conflict|operational-failure,
  code: stable string, canonical_effect: none|created|updated|retired,
  candidate_id: UUID|null, subject_id: ID|null, decision_ids: list,
  generation: integer|null, transaction_id: UUID|null,
  receipt_id: engine run ID, reasons: nonempty list
}
```

For first `propose`, `subject_ref=null`, `predecessor_ref=null`, page/candidate
preconditions are `absent` and identity generation is `0`; the server allocates
the candidate and resolves discovery dedup before returning its reference.
For `create-successor`, `predecessor_ref` names the retained tombstone and
`subject_ref` names the already evaluated new candidate or is null when an
authenticated review operation supplies its evidence-backed new proposal.
The latter atomically creates/evaluates the candidate before promotion. New
page/seat preconditions are `absent`, new identity generation `0`, and the
predecessor hash/generation are additional required `expected` entries. The
engine allocates the new immutable ID and records explicit successor relation;
the caller cannot allocate a decision or replace the retired ID. All other
operations require an existing subject reference.

`accepted` requires committed durable effect and verified postimages (or a
durable idempotent/no-op receipt when the requested state already holds); `pending`
means a persisted candidate/evidence proposal or review disposition, never
canonical admission. `rejected` is a policy/input refusal with canonical bytes
unchanged. `conflict` means failed preconditions and no proposed changes; refresh
and replan. `operational-failure` means missing provider, unsupported version,
lock/storage failure, unavailable required service or indeterminate commit;
receipt names recovery requirements, never claims rejection or acceptance.
HTTP adapters map rejected auth to 403, invalid input to 422, conflict to 409,
and operational failure to 503/500. Pending is a structured 202 disposition.
Existing textual MCP results must be generated from this result. Model run
receipts cannot classify pending/rejected/error as accepted canonical output.
Failure before a durable receipt is possible must be surfaced as missing
evidence and failure by the calling engine run, never synthesized as success.

Capability checks evaluate the caller delta **and** final resolved,
normalized, ownership-merged candidate inside the fence. Resolve canonical ID,
partition seat, aliases and collisions before mutation. Every final adopted
type route uses the primitive, including generic creates, converge into an
existing ID, dedup merges, type promotion, append/body edits and deterministic
imports. Narrow activity grants cannot send `type: actor` or replace body.
Pack `_okf_write` is a compatibility adapter, not a second writer: require the
installed provider/version; no fallback to `write_text`, even in pack-only
tests. Nonadopted types retain their supported existing behavior. Audit repair,
sheet import and `os.replace` paths must migrate too (Q05, Q15, Q16).

The engine resolves principals from token grants, a process-bound scheduled
identity, authenticated connector registration or operator review bridge.
`no_agent`, environment passed by an agent, and an arbitrary operation ID do
not grant privilege. Deterministic jobs run through engine-owned operation
context, bound to the discovered manifest and installed revision. A Python
helper argument cannot claim an authority-import principal.

Operation routing is exact: `propose`/`alias-propose` require
`identity.propose`; `maintain` requires `identity.maintain`; `evaluate` and
`alias-evaluate` require `identity.evaluate` or `identity.authority-import`
within that principal's policy scope; `human-decide` requires
`identity.human-review`; `promote` runs only as `identity.promote` after a
protected decision; `create-successor` requires `identity.human-review` plus current
verified basis and invokes the engine promotion step for a new ID; `revoke`/`retire` require
`identity.revoke` or bounded `identity.repair`; `activity-replace` requires
`identity.activity`; `repair` requires `identity.repair`. Evaluation persists
basis, promotion applies it; no `qualify` operation conflates the two. A
canonical assertion's decision and promotion may share one transaction, but
remain distinct authorized steps. A tombstoned ID is never restored, consistent
with conformance §6.3. Human-authorized correction creates or resolves a verified
successor, explicitly rebinds supported dependencies, and preserves the original
tombstone and history. No automatic alias/ID transfer implies qualification.

| Capability | Permitted caller responsibility | Protected limit |
|---|---|---|
| `identity.propose` | Scoped model extraction and import discovery | candidate/evidence proposal only |
| `identity.maintain` | Existing pack ownership grants | nonidentity metadata; append candidate notes/proposed evidence; no promotion |
| `identity.evaluate` | Registered source evaluator | resolve captures, persist machine basis; no human identity |
| `identity.authority-import` | Registered typed authority adapter | exact owned policy/record scope; cannot approve arbitrary prose |
| `identity.human-review` | Authenticated named operator | exact version/evidence proposition; distinct from generic review |
| `identity.promote` | Engine decision application only | verified current basis, canonical collision/CAS checks |
| `identity.activity` | Bound news/report lane | owned fields and mention IDs only, generation locked |
| `identity.revoke` | Policy dependency invalidator / authenticated review | dependency-specific revoke; cannot create qualified support |
| `identity.repair` | Authorized engine plan executor | named plan deltas/preconditions only; no general mutation escape |

## State and operation matrix

State derives from protected records, never page Boolean or counts. `absent`
has no subject; `pending` has a candidate or legacy unresolved canonical;
`qualified` has current canonical support; `stale` has lost final applicable
basis; `rejected` has a reviewed unsupported assertion; `retired` is a retained
tombstone. Alias states are separate. Lifecycle retirement overrides support.

Every allowed cell still requires the named caller capability and exact
preconditions; `evaluate` can return pending or rejected instead of qualified.

| Operation | absent | pending | qualified | stale | rejected | retired |
|---|---|---|---|---|---|---|
| Propose identity | pending candidate | new candidate revision | proposed change, current basis unaffected | proposed requalification | new evidence proposal | new evidence proposal; retain tombstone |
| Notes/proposed evidence | candidate only | allowed | allowed | allowed | allowed | historical note allowed |
| Owned nonidentity canonical metadata | no page | existing legacy page only | allowed | allowed | existing legacy page only | history-only metadata |
| Change type/name/ID or admitted aliases | deny | deny | protected evaluate then promote | protected evaluate then promote | deny until new evaluation | deny original; human-authorized verified successor only |
| Evaluate/human identity decision | candidate only | qualify/pending/reject | requalify or revoke exact basis | qualify/pending/reject | new basis/pending/reject | record basis only; no automatic restore |
| Promote candidate | deny | verified basis → qualified | atomic identity merge if verified | verified basis → qualified | only verified new basis → qualified | deny original ID; create-successor operation required |
| Add activity/mention/count | deny | deny | eligible current aliases/mentions only | deny | deny | deny |
| Clear owned activity fields | no-op receipt | allowed existing page | allowed | allowed | allowed | allowed |
| Revoke/retire | no-op receipt | allowed | allowed | allowed | allowed | idempotent |
| Approved bounded repair | candidate cleanup | allowed | allowed | allowed | allowed | allowed; never restores eligibility implicitly |

Clear means replace owned counts with zero, lists with `[]`, dates with null;
do not drop the patch as an empty value. Page maintenance cannot add active
relations or evidence-backed claims to an ineligible actor; store proposals
outside canonical active fields instead. The matrix permits genuine upkeep,
notes and evidence collection while protecting type, aliases, decision
projections, review projections, counts and activity-bearing body sections.
Source links proposed during maintenance do not become qualifying evidence.
Existing pending legacy pages remain visible in historical/search/review views
with disposition; new pending identities have no canonical actor seat (Q06).

## Atomicity and crash recovery

The [corpus fence](corpus-transactions.md) serializes mutations and records
interruption. It does **not** roll back multiple file replacements. The new
primitive must add recoverable commit semantics; merely entering `mutation()`
is insufficient.

Within the exclusive corpus fence and identity resource lock, check for and
recover unfinished identity transactions before any new read/write. Resolve all
inputs, evaluate capabilities, verify page/candidate/generation/evidence/policy
CAS and collisions, then durably stage a manifest with exact target hashes,
preimage bytes (or absent marker), staged postimage bytes, ID/alias registry,
review projections, identity projection and canonical page changes. Restrict
preimages to the vault's trust boundary and sensitivity/backup policy.

Fsync files and directories, then write a durable commit-intent marker before
replacement. Under the held fence, replace staged targets and verify each
postimage. Append correlated corpus journal/epoch exactly once, persist a
terminal committed receipt, and only then expose the accepted result. Readers
using this contract never see a partially applied transaction. Canonical
promotion, seat claim, ID registry and current qualification projection are
one manifest; two candidates for one seat cannot both succeed (Q13).

Before commit intent, failures abort with unchanged targets. After intent,
recovery rolls forward only targets matching their recorded preimage or
postimage. A third value is a conflict: stop that transaction and fail closed
for affected eligibility until an authorized recovery plan accounts for it;
never overwrite newer content. Interrupted retirement must complete retirement,
not restore a false identity from a preimage. An explicit rollback is a new
CAS-protected repair, never blind restoration. Resume uses the same manifest
and idempotency key; repeats return the original durable receipt. Same key with
different request digest is conflict (Q13, Q14).

Indexes, search and PostgreSQL remain derived. Queue attributable rebuilds on
the committed epoch; do not claim cross-store SQL/file atomicity. Active
consumers must verify the identity projection generation/epoch and consult the
protected eligibility view, so stale cached indexes cannot restore a retired
actor. During a missing/invalid projection, active eligibility fails closed with
a visible operational error while historical lookup remains available. This
applies only after the adopted policy's activation barrier passes; preactivation
never-activated legacy reads preserve existing behavior and report qualification
as not active. Missing adoption configuration on a previously activated corpus
is an operational failure, not preactivation compatibility.
Extend
stable-read recovery to the protected identity targets, not only Markdown.

## Mention and analysis contract

Canonical qualification is reused across articles. A mention decision records
subject ID/generation, alias basis ID, source acquisition/revision, exact span,
resolver policy/digest, contextual resolution and lineage. Lexical matching
only generates candidates. Common words, short names and overlapping aliases
require contextual resolution; neither blanket exclusion nor substring alone
is enough. Unknown source kinds and incomplete acquisition are explicit
pending outcomes; do not silently exclude them or manufacture full text.

An article disputing an actor's involvement can be a supported mention without
supporting attribution. Operational claims require separate claim-specific
evidence. Source links, mentions, and attribution edges have different records
and invalidation rules. Several reports derived from one primary claim share
lineage, counted distinctly as publications but once as independent evidence.
Counts cannot use publisher names as a substitute for lineage (Q09–Q11).

Activity lanes recompute **all** owned fields for all affected subjects,
including actors with no hits and complete empty selections. Snapshot includes
qualified roster, aliases, source selection and revisions; stamp requires exact
generation/CAS. A valid empty selection clears old owned activity; a selection
failure reports operational-failure and cannot invent a zero conclusion.
Ineligible actors allow owned clears only. Report/news lanes preserve each
other's fields. Models cannot submit derived counters (Q12, Q13).

Every active consumer in the inventory must call the shared eligibility view
and carry generation/dependency evidence. Historical search/export may retain
pending, stale and retired records with explicit disposition, but cannot route
them into new assessments, priorities, forecasts, collection, auto-review,
risk ranking or active UI facets. Apply active eligibility before totals,
pagination and facet aggregation; filtering a sliced page is insufficient.
Invalidation retires unsupported priority
work and marks dependent conclusions; mixed-subject assessments preserve valid
claims/evidence and revoke only unsupported parts (Q17).

## Legacy activation and remediation barrier

Precompute every legacy actor outcome before enabling strict eligibility. The
immutable activation plan includes engine/pack/install/staging revisions,
composed policy digest, corpus/source/acquisition/identity generations, before
roster count and exact IDs, after eligible IDs/count, pending/stale/rejected/
retired IDs/count, every known legitimate actor that would be lost, review
owner/person, backlog sizes and staffed processing capacity. Counts reconcile
to the exact census; nonactors are reported separately.

For **each known legitimate legacy actor**, retain eligibility through current
verified qualification or an authenticated evidence-backed migration decision.
Otherwise require the responsible owner's explicit acceptance of that exact
named exclusion, reason, impact, disposition and restoration criteria. Generic
review explanations, stamps, inferred consent, old membership and count-based
continuity grants do not meet the barrier. Unaccepted exclusions, unexplained
losses, missing dispositions, unowned backlog or changed frozen inputs block
activation (Q18). Missing historical evidence is pending/unavailable, not proof
of a nonactor; try current authenticated acquisition or human evidence review.

Activation CAS checks the complete plan input set under the fence. Concurrent
changes require replanning; the plan digest is not an exemption from per-target
checks. Apply verified decisions and named exclusions before changing active
consumer behavior. Fleet rollout uses one compatible frozen revision set,
verified installed hashes and actual stored scheduler identities; no extra
intermediate rollout is presumed necessary (Q19, Q20).

False identity repair uses the existing plan → apply → verify operation
registry, extended with protected dependency inventories, exact named target
preimages/hashes, expected postimages, allowed deltas and recovery protocol.
Retain original source reports and historical receipts; tombstone the false
actor, revoke wrong actor edges/mentions, retire dependent priority work, and
recompute owned counts/projections including zeros. Do not rename the false
identity into a vulnerability or fabricate a replacement. Preserve valid parts
of mixed-subject assessments. Incident-specific evidence belongs in private
release/repair issues; the shared acceptance manifest uses fictional inputs.

## Evidence and delivery

This milestone is pure design/documentation. JSON syntax and manifest
consistency are review aids, not behavioral proof. No fixture runner exists and
all Qxx contracts are future work. The inventory records current tests as
navigation anchors without claiming those tests prove the new contract.

Implementation must first turn these fixtures into deterministic real provider
and consumer contracts and prove every negative gate fails on the original
defect. Then run disposable writer/importer integration, crash recovery,
authenticated review, scheduler and UI/API tests with attributable CI
evidence. Keep adopted coverage and mutation floors; no CI imitation on an
application host. Required environments fail visibly when unavailable.

The final release must separately report merged source, required CI,
fleet artifact deployment, per-vault migration/repair and late client-visible
verification. Scheduled evidence includes distinct disposable positive and
negative inputs through the actual stored job, plus durable receipts and an
immediate stable repeat. Telemetry covers admission failures, pending/stale
backlog, evidence access, ambiguity, invalidation and consumer discrepancies;
verify emission/collection/query before calling it operational. Runtime
readiness and cleanup remain open until these downstream issues are complete.
