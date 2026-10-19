# Identity writer and consumer inventory

Snapshot: 2026-10-02; engine `974f305c`, library
`5795bc3c010759aece9d91180080211a30878eb0`, analysis
`2c9c8292a63ab711961d4feece5d52da495a8c46`. The two pack checkouts were
clean during read-only inspection. Exact runtime installed/staged versions and
stored jobs have **not** been verified by this design milestone.

This is an implementation inventory for
[qualification v1](actor-identity-qualification.md), not a claim of new
enforcement or passing coverage. Existing tests below are verified file paths
for follow-up inspection. Future Qxx contracts are defined in the acceptance
manifest; every row needs adapter-level execution of its listed contracts.

Owner roles: E = okengine-maintainers; L = threat-actor-pack-maintainers
(okpacks-library); A = actor-analysis-maintainers; O = release owner/operators.
These roles require an actual named responsible person at activation.

## Engine writers and consumers

Paths are relative to the engine repository. API names denote existing
functions/tools unless explicitly marked future.

| ID / owner | Exact source/API/job | Current eligibility or mutation behavior | Future adapter contracts / implementation issue |
|---|---|---|---|
| E01 / E | `okengine-mcp/write_server.py`; `src/okengine/write_services/transactions.py` `_create`, `_update`, `_tombstone`; tools `create_entity`, `update_entity`, `tombstone_entity` | Actor class heuristics; existing schema/ownership/review behavior; no protected source-backed decision store | Q01–Q06, Q08, Q14–Q16 / engine #973 |
| E02 / E | `src/okengine/write_services/patching.py` `_patch`, `_append_section`; tools `patch_entity`, `append_to_section` | Field/body mutations; final-candidate identity guard must cover changes to existing profiles and promotion | Q05, Q06, Q13 / #973 |
| E03 / E | `src/okengine/write_services/convergence.py` `_converge`, `_entity_backfill_frontmatter`; `scripts/cron/select_entity_candidates.py`; job `entity-backfill` | Backfill requires explicit subtype and positive identity in caller profile body then stamps Boolean; converge merges into resolved IDs | Q01, Q02, Q04, Q05, Q07, Q13 / #973 |
| E04 / E | `src/okengine/write_services/deduplication.py` `_dedup_on_create`, `_alias_hits`; `okengine-mcp/converge.py`; `scripts/cron/entity_converge.py` | Identity/name/alias registry, merge/tombstone paths; deterministic merge path also changes canonical corpus | Q05, Q07, Q13, Q14 / #973 |
| E05 / E | `src/okengine/write_services/state.py`; `authorization.py`; `tools/policy_plane.py`; `config/policy/catalog.yaml`; `scripts/extension_tokens.py` | Caller context and field/body grants; administrative writer currently broader than constrained jobs | Q03–Q06, Q15, Q16, Q22 / #973 |
| E06 / E | `src/okengine/write_services/review.py`; `scripts/framework_review.py`; `scripts/cron/review_autoverify.py`; jobs `review-autoverify`, `review-drain`, `review-reconcile` | Generic review has protected human projections; auto-review uses its own pack-declared policy. Generic approval is not identity evidence | Q04, Q06, Q08, Q21 / #973 |
| E07 / E | `src/okengine/actor_identity.py` `actor_identity_error`, `has_positive_actor_identity`; `scripts/cron/corpus_audit.py`; `corpus_audit_domain.py` | Regex class contradiction/positive assertion and audit findings, not authenticated source truth | Q01–Q04, Q21, Q22 / #973 |
| E08 / E | `src/okengine/corpus_transaction.py`; `src/okengine/write_services/transport.py`; `src/okengine/operations/run.py` | Fence, epoch, hashes and interruption journal; operation receipts. Fence alone does not undo partial writes | Q13–Q16, Q19 / #973 |
| E09 / E | `scripts/framework_audit_repair.py`; `AUDITS`, `REPAIRS`; `framework audit`, `framework repair plan/apply/verify`; `scripts/framework_backup.py` | Immutable digest-bound plans; currently only body-integrity and malformed-slugs repair registered, no actor dependency repair; backup must retain future protected evidence closure | Q14, Q17–Q19, Q24 / #974 |
| E10 / E | `scripts/cron/web_capture.py` `capture`; `source_connector.py`; `source_hydrate.py`; `raw_compile.py`; `source_content.py` `source_state`; `src/okengine/write_services/raw_sources.py` `_converge_raw_source` | Captured object/revision and raw/source linkage exist; current capture-shaped fields and contained text do not authenticate acquisition principal | Q03, Q04, Q14, Q21 / #973 |
| E11 / E | `scripts/cron/schema_type_drain.py`, `normalize_entity_schema.py`, `authority_enrich.py`, `apply_curated_entity_fields.py`, `repair_carried_provenance.py`, `source_normalize.py`, `normalize_bare_name_links.py`, `relink_prose_sources.py`, `source_backlink_drain.py`, `entity_link.py` | Generic deterministic edits/type/links/provenance can touch actor pages. They need final adopted-type routing; no exemption for maintenance | Q05, Q06, Q13, Q15 / #973 |
| E12 / E | `scripts/cron/canonical_assemble.py`, `okf_migrate.py`, `reshelve.py`, `reshard_oversized.py`; `scripts/dedup_entity_slugs.py`, `okf_dedup_entity_shards.py`, `backfill_ids.py`, `backfill_typeless_type.py` | Canonical/identity/type/seat migration and recovery utilities; arbitrary privileged filesystem edits remain outside application boundary | Q05, Q13, Q14, Q18 / #973, #974 |
| E13 / E | `src/okengine/cockpit_services/datasets.py` `_scan_dir_meta`, `_ds_rows`; `dataset_views.py`, `streams.py`, `assessments.py` | Dataset skips tombstones, body contradiction and explicit Boolean false; missing/true Boolean is not proof. Cached rows and all active facets need shared eligibility | Q02, Q06, Q08, Q17, Q20 / #973 |
| E14 / E | `src/okengine/cockpit_services/page_metadata.py` `_page_trust_state`, `_evidence_sources`; `documents.py`, `exports.py`; `okengine-reader/app.py`; `okengine-mcp/server.py` search/retrieve | Historical detail and evidence displays; generic trust/review state differs from identity admission. Preserve direct lookup and disclose disposition | Q06, Q08, Q17, Q20 / #973 |
| E15 / E | `src/okengine/projection/projector.py`; `scripts/cron/postgres_projection.py`, `rebuild_index.py`, `id_index.py`, `kb_search.py`, `build_hot_set.py`, `corpus_indexer.py` | Derived search/metadata/ID/hot-set planes; no new protected generation contract yet | Q08, Q13, Q14, Q20 / #973 |
| E16 / E | `extensions/okengine.actor-risk-ranking/actor_risk_rank.py`, `extension.yaml`; `tests/extensions/test_actor_risk_ranking.py` | Optional analytical extension consumes actor corpus; shared adoption/eligibility required rather than independent class heuristic | Q02, Q08, Q17, Q22 / #973 |
| E17 / E,O | `scripts/build_engine_wheel.py`, `deploy-cron-scripts.sh`, `deploy-cron-plus-jobs.sh`, `regen-cron-plus-jobs.sh`, `cron_plus_store.py`, `staged_drift.py`, `post_deploy_verify.sh`; `config/engine-crons.json` | Package installation, staged engine/pack scripts, generated and stored scheduling surface. Source config is not running-job evidence | Q15, Q16, Q20 / #975 |

Existing engine test anchors: `tests/test_actor_identity.py`,
`tests/test_write_server_actor_name.py`, `tests/test_write_server.py`,
`tests/test_write_server_converge.py`, `tests/test_write_server_rejected_aliases.py`,
`tests/test_write_server_patching_mutation.py`, `tests/test_policy_plane.py`,
`tests/test_review_workflow.py`, `tests/cron/test_review_autoverify.py`,
`tests/test_corpus_transaction.py`, `tests/test_corpus_transaction_contracts.py`,
`tests/test_operation_run.py`, `tests/test_framework_operations.py`,
`tests/cron/test_importer_guard.py`, `tests/cron/test_web_capture.py`,
`tests/cron/test_source_content.py`, `tests/cron/test_corpus_audit.py`,
`tests/cron/test_generated_page_writers.py`, `tests/cron/test_entity_converge.py`,
`tests/cron/test_write_lanes_deploy_their_contract.py`.

These tests exercise existing contracts. They do not constitute evidence for the
future admission boundary. Existing conformance schema fail-open/review-as-flag
semantics remain distinct; adopted identity admission is a new protected gate.
Ordinary source quality flags must not suddenly become actor rejection rules.

## Threat-actor pack writers and consumers

All script paths in this section are relative to
`okpacks-library/packs/okpack-threat-actors/crons/scripts/`. Job names below
come from `../domain-crons.json`; the `okpack-threat-actors-` prefix is omitted
in the job column only. Domain vocabulary belongs here, not in engine policy.

| ID / owner | Exact file / job | Current behavior | Future contracts / issue |
|---|---|---|---|
| L01 / L,E | `_okf_write.py` `write_page` / all importer callers | Merge/union fields, partition helper fallback; optional guard reports violations and lands flagged; direct `path.write_text`; no shared transactional qualification | Q03–Q07, Q13, Q15, Q16 / library #115, engine #973 |
| L02 / L | `attack_import.py` / `attack-import` | Typed authority records, owned fields/relationships and authority disposition through helper; must authenticate captured typed record and revision | Q04, Q05, Q07 / #115 |
| L03 / L | `misp_galaxy_import.py`, `msft_import.py`, `thaicert_import.py` / `misp-import`, `msft-import`; thaicert has no listed scheduled job | Creates/enriches actor records/aliases using helper; source and alias assertions need independent qualification | Q04, Q05, Q07, Q16 / #115 |
| L04 / L | `aptnotes_import.py`, `annual_reports_import.py` / `aptnotes-seed`, `annual-reports` | Report sources and `mentions_actors` matched against roster; structured mentions still require identity/context provenance | Q02, Q09–Q12 / #115 |
| L05 / L | `actor_news_activity.py` `_terms_for`, `main` / `actor-news-activity` | Type roster plus title/aliases; common/short-name filters, shared-term drop, first 1500 body chars; no-terms/no-hits exits preserve previous activity; stamps `type: actor` through helper | Q02, Q07–Q13, Q21 / #115 |
| L06 / L | `actor_activity.py` `_actor_index`, `main` / `actor-activity` | Report mentions, `recent_reports`, `total_mentions`, top-N dashboard; independent report ownership | Q08–Q13 / #115 |
| L07 / L | `actor_identity_audit.py` `_authority_backed`, `classify`, `_apply` / `actor-identity-audit` | Trusts G-ID shape/truthy authority IDs/consensus; page-prose class decisions; direct temp `os.replace`; kept count includes nonactors | Q04, Q05, Q08, Q13, Q14, Q18, Q21 / #115, #974 |
| L08 / L | `authority_review_reconcile.py` `qualifying_source`, `reconcile` / `authority-review-reconcile` | Named source/URL/kind plus lexical actor mention and canonical confirmed attribution can clear review; neither establishes role admission | Q03, Q04, Q06, Q08 / #115 |
| L09 / L | `actor_correlation.py` `load_actors`, `_merge_related`; `signature_ttps.py`; `shared_tooling.py` / `correlation`, `signature-ttps`, `shared-tooling` | Actor tradecraft/relations and body sections via helper, no protected qualification; weak/empty results need owned clears | Q06, Q08, Q12, Q13, Q17 / #115 |
| L10 / L | `cve_actor_edge.py` `_stamp`; `origin_country_normalize.py`; `attribution_confidence_drain.py`; `source_url_reconcile.py` / `cve-actor-edge`, `origin-country-normalize`, `url-reconcile`; attribution drain no listed scheduled job | Separate direct `write_text`/`os.replace` paths, bypass helper; role-sensitive edges and metadata must preserve ownership/CAS | Q05, Q06, Q13, Q14, Q17 / #115 |
| L11 / L | `cti_dashboards.py` / `cti-dashboards`; `actor_evidence_export.py` `build_records`, `export_snapshot`; `../../projectors/stix.py`, `ocsf.py` | Dashboards, exported actor snapshots and formats consume canonical role pages; export must distinguish active qualification vs historical disposition | Q08, Q17, Q20 / #115 |
| L12 / L | `../engine-template-prompts.json`; `../domain-crons.json` / `weekly-brief`, pack overrides of engine templates | Model output uses MCP paths; prompts do not enforce admission or derived field ownership | Q01, Q05, Q06, Q17, Q20 / #115 |

Existing test anchors relative to pack: `tests/test_actor_identity_admission.py`,
`tests/test_actor_news_activity_terms.py`, `tests/test_actor_news_activity_selection.py`,
`conformance/test_actor_identity_audit.py`, `conformance/test_actor_news_activity.py`,
`conformance/test_actor_activity.py`, `conformance/test_authority_review.py`,
`conformance/test_misp_identity_boundary.py`, `conformance/test_cve_actor_edge.py`,
`conformance/test_write_page_partitioning.py`, `conformance/test_actor_evidence_export.py`.

## Actor-analysis writers and consumers

Paths relative to the threat-actor analysis pack's `crons/scripts/`. Job column
omits the pack's job-name prefix unless the actual name differs.

| ID / owner | Exact file/API / job | Current eligibility and output | Future contracts / issue |
|---|---|---|---|
| A01 / A | `actor_relationship_pipeline.py` `actor_index`, `discover`, `run` / `relationship-pipeline` | Shared roster accepts actor/threat-actor type, excludes redirects/tombstones; relationship assessment output | Q08, Q13, Q17 / analysis #51 |
| A02 / A | `actor_assessment_priority_pipeline.py` `discover`, `_sources`, `run` / `priority-pipeline` | Own type/nonactor prose checks; recent news or dispute routing; source list includes recent refs/canonical/operator evidence; queues collection and dimensions | Q02, Q08, Q11, Q12, Q17 / #51 |
| A03 / A | `actor_identity_pipeline.py`, `actor_behavior_pipeline.py`, `actor_motivation_pipeline.py` `discover`, `run` / `identity-pipeline`, `behavior-pipeline`, `motivation-pipeline` | Shared roster plus linked source/name and dimension-specific evidence; dimension qualification differs from actor admission | Q08–Q11, Q17 / #51 |
| A04 / A | `actor_assessment_pipeline.py` `_actor_records`, `run`; `country_linkage_assessment_export.py`; `country_linkage_candidates.py` / `assessment-pipeline` | Origin/linkage source and imported snapshot workflows; independent assessment lineage/authority | Q08, Q11, Q17, Q21 / #51 |
| A05 / A | `actor_forecast_pipeline.py` `discover`; `opinion_dissent_pipeline.py`; `diagnostic_candidate_watch.py`; `diagnostic_reevaluation.py`; `select_diagnostic_grading.py`; `diagnostic_metrics.py`; `portfolio_maturity.py` | Forecast currently reads active behavior assessments without roster check; diagnostics/portfolio consume derived conclusions | Q08, Q17, Q20 / #51 |
| A06 / A | `actor_review_operation.py` `actor_inventory`, `run`; `../../operations/actor-review/operation.yaml` / `full-actor-review` | Shared type roster drives coordinated dimensions, operations/receipts; needs shared eligibility/generation without changing human review scope | Q06, Q08, Q17, Q20 / #51 |
| A07 / A | `actor_alias_sheet_import.py` `index_actors`, `main`; `actor_origin_sheet_reviews.py` / script installers, not listed domain cron | Direct canonical alias and optional actor creation `write_text`; imported spreadsheet label/ID insufficient admission | Q03–Q07, Q13, Q15 / #51 |
| A08 / A | `actor_evidence_snapshot.py` `validate_records`, `activate`; `assessment_review_gate.py`; `actor_candidate_dashboard.py` | Manifest/content digest and source provenance checks; review/promotion and active snapshot projection | Q03, Q04, Q08, Q14, Q17, Q20 / #51 |
| A09 / A | `campaign_attribution_operation.py` / `campaign-attribution`; `actor_supersession_questions.py` / `supersession-questions` | Campaign attribution direct write and supersession questions use actor relations/identity; preserve mixed-subject valid results | Q08, Q13, Q17 / #51 |
| A10 / A,E,O | `engine_helpers.py`; `../../scripts/install-okcti-pipeline.py`, `install-sheet-import-cron.sh`, `sheet-import.sh`; `../domain-crons.json`, `../engine-template-prompts.json` | Helper/import path and staged job installation; exact installed provider and stored-job provenance required | Q15, Q16, Q20 / #51, engine #975 |

A06's finite review dimensions are `country-association`,
`ecosystem-relationship`, `behavior-capability`, `motivation-resourcing`,
`identity-scope`, `opinion-dissent`, `priority-reconciliation`, and `forecast`
(`actor_review_operation.py: DIMENSIONS`). Q20 must cover all eight, including
indirect derived inputs, before claiming consumer completeness.

Existing analysis test anchors: `tests/test_actor_alias_sheet_import.py`,
`tests/test_actor_review_operation.py`, `tests/test_actor_assessment_priority_pipeline.py`,
`tests/test_actor_assessment_pipeline.py`, `tests/test_actor_identity_pipeline.py`,
`tests/test_actor_relationship_pipeline.py`, `tests/test_actor_behavior_pipeline.py`,
`tests/test_actor_motivation_pipeline.py`, `tests/test_actor_forecast_pipeline.py`,
`tests/test_evidence_snapshot.py`, `tests/test_country_linkage_assessment_export.py`,
`tests/test_campaign_attribution.py`, `tests/test_actor_supersession_questions.py`,
`tests/test_install_okcti_pipeline.py`.

## Incident attribution and completeness limits

The August creation receipt described extraction of a vulnerability as an actor.
The source-only inventory establishes that the news lane matches title/aliases
and updates its dedicated activity fields. It does **not** explain the later
canonical `sources` list addition. A bounded read-only October receipt/journal
lookup found no attributable write for that addition. The October source-list
updater is **unknown**. Preserve this distinction in the issue evidence; do not
label the matcher as that updater. Historical receipt recovery is follow-up,
not a prerequisite to enforcing the current write contract.

This inventory covers checked-in source surfaces and scheduled manifests at
the revisions above, including nonscheduled repair/import utilities. It does
not certify live native file writes, deployment-local modifications, arbitrary
privileged edits, or stale installed jobs. Delivery #975 must compare the fleet
runtime inventory to this source inventory and reject missing/incompatible
adapters. No inventory row is declared implemented by this milestone.

## Delivery graph

Q23 discovery-index contracts apply to E01, E03, E04 and all actor-creating
L01–L03/A07 adapters. Q24 persistent-backup/recovery contracts apply to E08,
E09, E10 and E17. These remain future required adapter coverage.

Engine #973 implements qualification/provider/storage/security and consumers;
library #115 migrates the pack writers, mention/activity and exporters;
analysis #51 migrates roster/priorities/analysis/sheets; engine #974 implements
legacy census/barrier and bounded dependency repair; engine #975 owns
integrated exact-revision CI qualification and fleet delivery evidence.
Engine #972 freezes these artifacts; parent #971 stays open through delivery.
