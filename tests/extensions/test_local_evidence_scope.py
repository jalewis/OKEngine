"""Country coverage reference-scope regression fixtures from observed failure shapes."""
import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('scoped_evidence', ROOT / 'extensions/okengine.assessments/local_evidence.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
SUBJECT = 'entities/a/actor'
QUESTION = 'actor-country-linkage'
MARKER = 'collection-requirement:actor-relationship-contradictions'
CONTEXT = {'evidence_kind': 'expected-absence', 'evidence_lineage': 'collection-gap:not-an-evidentiary-origin', 'absence_status': 'collection-gap'}
POLICY = {'contract': 'question-scoped/v1', 'assessment_kinds': [QUESTION, 'actor-identity-scope', 'actor-motivation'], 'current_statuses': ['active', 'disputed'], 'archived_statuses': ['superseded', 'withdrawn'], 'collection_tasks': {MARKER: {'assessment_kinds': ['actor-identity-scope', 'actor-motivation'], 'fields': ['adversarial_evidence'], 'question_kinds': ['actor-identity-scope', 'actor-motivation'], 'required_context': CONTEXT}}}


def page(vault, rel, metadata, body='Actor reporting'):
    path = vault / 'wiki' / (rel + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\n' + yaml.safe_dump(metadata) + '---\n' + body)


def resolve(vault, **kwargs):
    return r.resolve(vault, SUBJECT, QUESTION, selection_policy=POLICY, **kwargs)


def actor(vault, **fields):
    page(vault, SUBJECT, {'type': 'actor', 'title': 'Actor', **fields})


def assessment(vault, kind='actor-motivation', status='active', refs=None, **fields):
    page(vault, 'assessments/record', {'type': 'assessment', 'subject': SUBJECT, 'assessment_kind': kind, 'status': status, 'sources': refs or [], **fields})


def test_observed_task_and_unrelated_missing_reports_do_not_become_country_documents(tmp_path):
    actor(tmp_path, sources=['MISP galaxy', 'MITRE ATT&CK'])
    assessment(tmp_path, refs=['https://example.test/missing-report'], adversarial_evidence=[{'source': MARKER, **CONTEXT}])
    result = resolve(tmp_path)
    assert {x['reference'] for x in result['unresolved']} == {'MISP galaxy', 'MITRE ATT&CK'}
    task = next(x for x in result['reference_diagnostics'] if x['reference'] == MARKER)
    assert task['category'] == 'collection-task' and task['blocking'] is False
    missing = next(x for x in result['reference_diagnostics'] if x['reference'].startswith('https:'))
    assert missing['resolution_reason'] == 'url-not-local' and missing['disposition'] == 'contextual'
    old = r.resolve(tmp_path, SUBJECT, QUESTION)
    assert len(old['unresolved']) == 4 and 'reference_diagnostics' not in old


@pytest.mark.parametrize('kind,status,blocking', [(QUESTION, 'active', True), (QUESTION, 'disputed', True), ('actor-motivation', 'active', False), (QUESTION, 'superseded', False), (QUESTION, 'withdrawn', False), ('unknown-kind', 'active', True), (None, 'active', True), ('actor-motivation', None, True), ('actor-motivation', 'draft', True), ('actor-motivation', 'resolved', True)])
def test_reference_selection_retains_unknowns_and_current_question(kind, status, blocking, tmp_path):
    actor(tmp_path)
    assessment(tmp_path, kind, status, ['sources/missing'])
    result = resolve(tmp_path)
    assert bool(result['unresolved']) is blocking
    assert result['reference_diagnostics'][0]['blocking'] is blocking


@pytest.mark.parametrize('status', ['active', 'superseded', 'unknown'])
def test_explicit_country_reference_is_load_bearing_even_in_another_family(tmp_path, status):
    actor(tmp_path)
    assessment(tmp_path, status=status, adversarial_evidence=[{'source': 'sources/missing', 'question_kind': QUESTION}])
    assert resolve(tmp_path)['unresolved'][0]['reference'] == 'sources/missing'


@pytest.mark.parametrize('change', ['malformed-marker', 'wrong-context', 'wrong-field', 'unknown-status', 'country-relevant'])
def test_marker_recognition_requires_exact_form_context_and_records_real_gaps(tmp_path, change):
    actor(tmp_path)
    ref = {'source': MARKER, **CONTEXT}
    if change == 'malformed-marker':
        ref['source'] += ':arbitrary'
    if change == 'wrong-context':
        ref.pop('absence_status')
    if change in {'country-relevant', 'wrong-context', 'wrong-field'}:
        ref['question_kind'] = QUESTION
    assessment(tmp_path, kind=QUESTION if change == 'malformed-marker' else 'actor-motivation', status='unknown' if change == 'unknown-status' else 'active', refs=[{'path': ref['source'], **CONTEXT, 'question_kind': QUESTION}] if change == 'wrong-field' else [], adversarial_evidence=[] if change == 'wrong-field' else [ref])
    result = resolve(tmp_path)
    if change in {'unknown-status', 'country-relevant'}:
        task = result['reference_diagnostics'][0]
        assert task['category'] == 'collection-task' and task['blocking'] is True
        assert result['unresolved'] == []
    else:
        assert result['unresolved'] and result['reference_diagnostics'][0]['category'] != 'collection-task'


def test_archived_readable_sources_survive_without_importing_assessment_conclusions(tmp_path):
    actor(tmp_path, sources=['assessments/record'])
    assessment(tmp_path, status='superseded', refs=['sources/old'])
    page(tmp_path, 'sources/old', {'type': 'source'}, 'Actor historical country reporting')
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['sources/old']
    assert result['unresolved'][0]['reason'] == 'not-independent-evidence'


@pytest.mark.parametrize('ref', ['https://example.test/report', 'source:duplicate'])
def test_duplicate_exact_url_or_id_cannot_choose_document(tmp_path, ref):
    actor(tmp_path, sources=[ref], repository_recent_articles=[{'url': 'https://example.test/report'}])
    for path in ['sources/one', 'sources/two']:
        page(tmp_path, path, {'type': 'source', 'id': 'source:duplicate', 'url': 'https://example.test/report'})
    result = resolve(tmp_path)
    assert result['resolved'] == []
    assert all(x['reason'] == 'ambiguous-local-reference' for x in result['unresolved'])


@pytest.mark.parametrize('kind,status,blocking', [(QUESTION, 'active', True), ('actor-motivation', 'active', False), (QUESTION, 'superseded', False), ('unknown', 'active', True)])
def test_malformed_assessment_reference_keeps_scope_and_cannot_prove_absence(tmp_path, kind, status, blocking):
    actor(tmp_path)
    assessment(tmp_path, kind, status, refs=[{'unsupported': 'value'}, 17])
    result = resolve(tmp_path)
    assert bool(result['unresolved']) is blocking
    assert all(x['blocking'] is blocking for x in result['reference_diagnostics'])


def test_direct_malformed_and_embedded_article_failures_block(tmp_path):
    actor(tmp_path, sources=[{'bad': 'shape'}, 42], repository_recent_articles=['bad'])
    result = resolve(tmp_path)
    assert len(result['unresolved']) == 3
    assert all(x['blocking'] for x in result['reference_diagnostics'])


def test_canonical_subject_handle_with_authority_ref_preserves_country_evidence(tmp_path):
    actor(tmp_path)
    assessment(tmp_path, QUESTION, 'active', ['sources/right'], subject_ref='G1234')
    page(tmp_path, 'sources/right', {'type': 'source'})
    page(tmp_path, 'assessments/wrong', {'type': 'assessment', 'subject': 'entities/other', 'assessment_kind': QUESTION, 'status': 'active', 'sources': ['sources/missing']})
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['sources/right']
    assert not result['unresolved']


def test_snapshot_changes_with_policy_status_context_and_alias_semantics(tmp_path):
    actor(tmp_path, sources=['sources/report'])
    page(tmp_path, 'sources/report', {'type': 'source'}, 'Alias country reporting')
    assessment(tmp_path, refs=['sources/missing'])
    one = resolve(tmp_path)
    assert one['snapshot_digest'] == resolve(tmp_path)['snapshot_digest']
    assert one['snapshot_digest'] != resolve(tmp_path, aliases=['Alias'])['snapshot_digest']
    assessment(tmp_path, status='superseded', refs=['sources/missing'])
    assert one['snapshot_digest'] != resolve(tmp_path)['snapshot_digest']
    p = copy.deepcopy(POLICY); p['current_statuses'].append('draft')
    assert resolve(tmp_path)['snapshot_digest'] != r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=p)['snapshot_digest']
    missing = r.resolve(tmp_path, 'entities/missing', QUESTION, selection_policy=POLICY)
    assert missing['snapshot_digest'] != r.resolve(tmp_path, 'entities/missing', 'other', selection_policy=POLICY)['snapshot_digest']


@pytest.mark.parametrize('policy', [False, {}, {'contract':'wrong'}, {**POLICY,'current_statuses':[]}, {**POLICY,'archived_statuses':['active']}, {**POLICY,'assessment_kinds':['']}, {**POLICY,'collection_tasks':[]}, {**POLICY,'collection_tasks':{'bad':{}}}, {**POLICY,'collection_tasks':{MARKER:False}}, {**POLICY,'collection_tasks':{MARKER:{}}}, {**POLICY,'collection_tasks':{MARKER:{**POLICY['collection_tasks'][MARKER], 'required_context':{}}}}])
def test_invalid_selection_policy_fails_closed_before_read(tmp_path, policy):
    with pytest.raises(ValueError):
        r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)


def test_malformed_unrelated_corpus_page_is_not_an_undocumented_subject_failure(tmp_path):
    actor(tmp_path)
    (tmp_path / 'wiki' / 'bad.md').write_text('malformed page')
    result = resolve(tmp_path)
    assert result['unresolved'] == []
    assert result['malformed'][0]['reason'] == 'malformed-local-page'


def test_contextual_assessment_reference_is_retained_but_not_independent(tmp_path):
    actor(tmp_path)
    assessment(tmp_path, refs=['assessments/historical'])
    page(tmp_path, 'assessments/historical', {'type':'assessment', 'subject':SUBJECT, 'assessment_kind':QUESTION, 'status':'superseded'})
    result = resolve(tmp_path)
    assert not result['unresolved'] and not result['resolved']
    assert any(x['category'] == 'assessment-conclusion' and not x['blocking'] for x in result['reference_diagnostics'])


def test_single_readable_embedded_url_is_not_ambiguous(tmp_path):
    url = 'https://example.test/single'
    actor(tmp_path, repository_recent_articles=[{'url': url}])
    page(tmp_path, 'sources/single', {'type': 'source', 'url': url})
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['sources/single']
    assert result['unresolved'] == []


def test_embedded_url_without_local_document_remains_article_metadata(tmp_path):
    url = 'https://example.test/not-local'
    actor(tmp_path, repository_recent_articles=[{'url': url, 'title': 'Actor reporting'}])
    result = resolve(tmp_path)
    assert result['unresolved'] == []
    assert not any(x.get('resolution_reason') == 'ambiguous-local-reference'
                   for x in result['reference_diagnostics'])
    assert len(result['resolved']) == 1
    assert result['resolved'][0]['source_url'] == url
    assert result['resolved'][0]['artifact_type'] == 'article-reference'
    assert result['resolved'][0]['evidence_access'] == 'local-metadata-only'
    assert result['resolved'][0]['evidence_lineage'] == 'unresolved'


def test_direct_documentary_and_provenance_diagnostics_keep_distinct_categories(tmp_path):
    actor(tmp_path, sources=['sources/readable', 'MISP galaxy'])
    page(tmp_path, 'sources/readable', {'type': 'source'})
    result = resolve(tmp_path)
    diagnostics = {x['reference']: x for x in result['reference_diagnostics']}
    assert diagnostics['sources/readable']['category'] == 'documentary'
    assert diagnostics['MISP galaxy']['category'] == 'contextual-provenance'
    assert diagnostics['MISP galaxy']['blocking'] is True


def test_matching_policy_contract_is_compared_by_value(tmp_path):
    actor(tmp_path)
    policy = copy.deepcopy(POLICY)
    policy['contract'] = bytearray(POLICY['contract'], 'utf-8').decode()
    result = r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)
    assert result['selection_policy'] == policy


def test_fully_formed_wrong_policy_contract_is_rejected(tmp_path):
    actor(tmp_path)
    policy = copy.deepcopy(POLICY)
    policy['contract'] = 'unsupported-contract'
    with pytest.raises(ValueError, match='unsupported evidence selection policy'):
        r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)


def test_exported_supported_contract_is_accepted(tmp_path):
    actor(tmp_path)
    policy = copy.deepcopy(POLICY)
    policy['contract'] = r.SELECTION_CONTRACT
    assert r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)['selection_policy'] == policy


def test_current_country_kind_does_not_include_alphabetically_earlier_family(tmp_path):
    actor(tmp_path)
    policy = copy.deepcopy(POLICY)
    policy['assessment_kinds'].append('actor-behavior-change')
    assessment(tmp_path, kind='actor-behavior-change', refs=['sources/context'])
    result = r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)
    diagnostic = result['reference_diagnostics'][0]
    assert diagnostic['disposition'] == 'contextual'
    assert diagnostic['reason'] == 'unrelated-assessment-reference'
    assert result['unresolved'] == []


@pytest.mark.parametrize('value', [{'unexpected': 'value'}, 42])
def test_scoped_direct_malformed_reference_keeps_its_actual_field_origin(tmp_path, value):
    actor(tmp_path, sources=[value])
    result = resolve(tmp_path)
    assert result['malformed'][0]['origin'] == SUBJECT + ':sources'
    assert result['unresolved'][0]['origin'] == SUBJECT + ':sources'


def test_known_current_family_explains_its_scope_instead_of_unknown_scope(tmp_path):
    actor(tmp_path)
    assessment(tmp_path, refs=['sources/missing'])
    result = resolve(tmp_path)
    assert result['reference_diagnostics'][0]['reason'] == 'unrelated-assessment-reference'


def test_nonassessment_page_cannot_supply_subject_assessment_sources(tmp_path):
    actor(tmp_path)
    page(tmp_path, 'entities/b/related', {'type': 'actor', 'subject': SUBJECT,
                                        'sources': ['sources/unrelated']})
    result = resolve(tmp_path)
    assert result['unresolved'] == [] and result['reference_diagnostics'] == []


def test_referenced_actor_page_is_readable_and_not_an_assessment_conclusion(tmp_path):
    actor(tmp_path, sources=['entities/b/related'])
    page(tmp_path, 'entities/b/related', {'type': 'actor'})
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['entities/b/related']
    assert result['unresolved'] == []
    assert result['reference_diagnostics'][0]['category'] == 'documentary'


@pytest.mark.parametrize('field,bad_value', [
    ('fields', None), ('fields', []), ('fields', [42]), ('fields', ['']),
    ('question_kinds', None), ('question_kinds', []), ('question_kinds', [42]),
    ('required_context', {'absence_status': 42}),
    ('required_context', {42: 'collection-gap'}),
    ('required_context', {'absence_status': ''})])
def test_task_policy_rejects_invalid_field_and_context_contracts(tmp_path, field, bad_value):
    policy = copy.deepcopy(POLICY)
    policy['collection_tasks'][MARKER][field] = bad_value
    with pytest.raises(ValueError):
        r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)


def test_task_policy_rejects_nonstring_marker_even_with_complete_task(tmp_path):
    policy = copy.deepcopy(POLICY)
    policy['collection_tasks'] = {42: policy['collection_tasks'][MARKER]}
    with pytest.raises(ValueError):
        r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)


def test_malformed_assessment_context_and_later_valid_reference_are_both_retained(tmp_path):
    actor(tmp_path)
    malformed = {'unexpected': 'retained-context', 'question_kind': QUESTION}
    assessment(tmp_path, refs=[malformed, {'path': 'sources/later', 'question_kind': QUESTION}])
    page(tmp_path, 'sources/later', {'type': 'source'})
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['sources/later']
    diagnostic = next(x for x in result['reference_diagnostics']
                      if x.get('resolution_reason') == 'malformed-reference')
    assert diagnostic['reference_context'] == malformed
    assert diagnostic['origin'] == 'assessments/record:sources'
    assert diagnostic['blocking'] is True


def test_unrelated_malformed_page_does_not_hide_actual_malformed_reference(tmp_path):
    actor(tmp_path, sources=[{'unexpected': 'reference'}])
    (tmp_path / 'wiki' / 'bad.md').write_text('malformed page')
    result = resolve(tmp_path)
    assert result['unresolved'][0]['reason'] == 'malformed-reference'
    assert result['unresolved'][0]['origin'] == SUBJECT + ':sources'


def test_legacy_assessment_collection_continues_across_multiple_records(tmp_path):
    actor(tmp_path)
    for name in ['one', 'two']:
        page(tmp_path, 'assessments/' + name, {'type': 'assessment', 'subject': SUBJECT,
             'sources': ['sources/' + name]})
        page(tmp_path, 'sources/' + name, {'type': 'source'})
    result = r.resolve(tmp_path, SUBJECT, QUESTION)
    assert [x['artifact'] for x in result['resolved']] == ['sources/one', 'sources/two']
    assert 'reference_diagnostics' not in result


def test_ambiguous_embedded_article_does_not_hide_later_readable_article(tmp_path):
    duplicate = 'https://example.test/duplicate'
    single = 'https://example.test/single-later'
    actor(tmp_path, repository_recent_articles=[{'url': duplicate}, {'url': single}])
    for name in ['one', 'two']:
        page(tmp_path, 'sources/' + name, {'type': 'source', 'url': duplicate})
    page(tmp_path, 'sources/later', {'type': 'source', 'url': single})
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['sources/later']
    assert result['unresolved'][0]['reason'] == 'ambiguous-local-reference'


def test_direct_reference_mapping_retains_explicit_question_metadata(tmp_path):
    actor(tmp_path, sources=[{'path': 'sources/missing', 'question_kind': QUESTION}])
    diagnostic = resolve(tmp_path)['reference_diagnostics'][0]
    assert diagnostic['question_kind'] == QUESTION
    assert diagnostic['reason'] == 'explicit-question-reference'
    assert diagnostic['blocking'] is True


def test_standing_assessment_duplicate_preserves_exact_operator_attestation(tmp_path):
    source = 'sources/private/catalog/actor/record'
    actor(tmp_path, operator_evidence_refs=[source])
    page(tmp_path, source, {'type': 'source', 'publisher': 'Malpedia',
         'local_only': True, 'export_policy': 'deny', 'retrieved_via': 'local-repository',
         'dataset': 'curated_actor_data', 'record_checksum': 'sha256:record',
         'bounded_auto_accept': True, 'evidence_role': 'curated-authority'})
    assessment(tmp_path, kind=QUESTION, refs=[source], subject_ref='G1234',
               adversarial_evidence=[{'source': source}])
    result = resolve(tmp_path)
    assert len(result['resolved']) == 1
    item = result['resolved'][0]
    assert item['authority_record_attested'] is True
    assert item['bounded_auto_accept'] is True
    assert item['reference_origin'] == SUBJECT + ':operator_evidence_refs'
    assert {x['origin'] for x in result['reference_diagnostics']} == {
        SUBJECT + ':operator_evidence_refs', 'assessments/record:sources',
        'assessments/record:adversarial_evidence'}
    legacy = r.resolve(tmp_path, SUBJECT, QUESTION)
    assert legacy['resolved'][0]['authority_record_attested'] is True


def test_current_documentary_reference_wins_over_archived_duplicate(tmp_path):
    source = 'sources/shared'
    actor(tmp_path)
    page(tmp_path, source, {'type': 'source'})
    for name, status in [('a-archived', 'superseded'), ('z-current', 'active')]:
        page(tmp_path, 'assessments/' + name, {'type': 'assessment', 'subject': SUBJECT,
             'assessment_kind': QUESTION, 'status': status, 'sources': [source]})
    result = resolve(tmp_path)
    assert len(result['resolved']) == 1
    assert result['resolved'][0]['reference_origin'] == 'assessments/z-current:sources'
    assert {x['disposition'] for x in result['reference_diagnostics']} == {'blocking', 'contextual'}


def test_context_only_readable_document_remains_discoverable(tmp_path):
    actor(tmp_path)
    page(tmp_path, 'sources/historical', {'type': 'source'})
    assessment(tmp_path, status='superseded', refs=['sources/historical'])
    result = resolve(tmp_path)
    assert [x['artifact'] for x in result['resolved']] == ['sources/historical']
    assert result['reference_diagnostics'][0]['disposition'] == 'contextual'
    assert result['unresolved'] == []


def test_operator_id_duplicate_cannot_attest_documentary_id_resolution(tmp_path):
    actor(tmp_path, sources=['source:record'], operator_evidence_refs=['source:record'])
    page(tmp_path, 'sources/private/record', {'type': 'source', 'id': 'source:record',
         'local_only': True, 'export_policy': 'deny', 'retrieved_via': 'local-repository',
         'dataset': 'curated_actor_data', 'record_checksum': 'sha256:record'})
    result = resolve(tmp_path)
    assert len(result['resolved']) == 1
    assert result['resolved'][0]['authority_record_attested'] is False
    assert result['resolved'][0]['reference_origin'] == SUBJECT + ':sources'
    assert result['unresolved'][0]['reason'] == 'operator-ref-not-a-page-path'


def test_missing_operator_page_does_not_gain_verified_operator_priority(tmp_path):
    actor(tmp_path, sources=['sources/a-readable'], operator_evidence_refs=['sources/z-missing'])
    page(tmp_path, 'sources/a-readable', {'type': 'source'})
    result = resolve(tmp_path)
    assert [x['reference'] for x in result['reference_diagnostics']] == [
        'sources/a-readable', 'sources/z-missing']
    assert result['unresolved'][0]['reason'] == 'operator-ref-not-a-page-path'


def test_default_duplicate_resolution_preserves_reference_then_origin_order(tmp_path):
    url = 'https://example.test/record'
    source = 'sources/record'
    actor(tmp_path, sources=[source, url])
    page(tmp_path, source, {'type': 'source', 'url': url})
    assessment(tmp_path, refs=[source, url])
    result = r.resolve(tmp_path, SUBJECT, QUESTION)
    assert len(result['resolved']) == 1
    assert result['resolved'][0]['reference'] == url
    assert result['resolved'][0]['reference_origin'] == 'assessments/record:sources'


def test_scoped_source_field_cannot_receive_exact_operator_priority(tmp_path):
    actor(tmp_path, sources=['sources/a-document'], operator_evidence_refs=['sources/z-operator'])
    for source in ['sources/a-document', 'sources/z-operator']:
        page(tmp_path, source, {'type': 'source'})
    result = resolve(tmp_path)
    assert [x['reference'] for x in result['reference_diagnostics']] == [
        'sources/z-operator', 'sources/a-document']
    operator = next(x for x in result['resolved'] if x['artifact'] == 'sources/z-operator')
    assert operator['reference_origin'] == SUBJECT + ':operator_evidence_refs'


# ── okengine#994 [21]: an unknown question_kind fails closed (stays blocking), never demotes ─────
DRIFTED = 'actor-country-linkge'


@pytest.mark.parametrize('status', ['active', 'superseded', 'withdrawn'])
def test_a_drifted_question_kind_keeps_the_reference_blocking(tmp_path, status):
    """A known unrelated (or archived) family is contextual by default, and an explicit question_kind
    is the producer's override. A drifted value was read as absent and demoted the reference."""
    actor(tmp_path)
    assessment(tmp_path, status=status,
               adversarial_evidence=[{'source': 'sources/missing', 'question_kind': DRIFTED}])
    result = resolve(tmp_path)
    diagnostic = result['reference_diagnostics'][0]
    assert diagnostic['blocking'] is True and diagnostic['disposition'] == 'blocking'
    assert diagnostic['reason'] == 'unknown-question-kind'
    assert diagnostic['question_kind'] == DRIFTED
    assert [x['reference'] for x in result['unresolved']] == ['sources/missing']


@pytest.mark.parametrize('value', [42, ['actor-country-linkage'], {'kind': QUESTION}, True, '  '])
def test_a_nonstring_or_blank_question_kind_is_unknown_and_blocks(tmp_path, value):
    actor(tmp_path)
    assessment(tmp_path, adversarial_evidence=[{'source': 'sources/missing', 'question_kind': value}])
    diagnostic = resolve(tmp_path)['reference_diagnostics'][0]
    assert diagnostic['blocking'] is True and diagnostic['reason'] == 'unknown-question-kind'


def test_a_drifted_question_kind_on_a_collection_task_blocks(tmp_path):
    """A recognized task for an unrelated question is contextual; a drifted tag must not inherit that."""
    actor(tmp_path)
    assessment(tmp_path, adversarial_evidence=[{'source': MARKER, **CONTEXT, 'question_kind': DRIFTED}])
    task = resolve(tmp_path)['reference_diagnostics'][0]
    assert task['category'] == 'collection-task'
    assert task['blocking'] is True and task['reason'] == 'unknown-question-kind'


@pytest.mark.parametrize('known', ['actor-motivation', 'actor-identity-scope'])
def test_a_known_other_question_kind_is_still_contextual(tmp_path, known):
    """Negative fixture: the fail-closed rule must not over-block an evidence item explicitly tagged
    for a different, KNOWN question."""
    actor(tmp_path)
    assessment(tmp_path, adversarial_evidence=[{'source': 'sources/missing', 'question_kind': known}])
    diagnostic = resolve(tmp_path)['reference_diagnostics'][0]
    assert diagnostic['blocking'] is False
    assert diagnostic['reason'] == 'unrelated-assessment-reference'


def test_a_task_only_question_kind_is_known(tmp_path):
    """A question kind declared only in a collection task's question_kinds is known vocabulary."""
    policy = copy.deepcopy(POLICY)
    policy['collection_tasks'][MARKER]['question_kinds'] = ['actor-tooling-overlap']
    page(tmp_path, SUBJECT, {'type': 'actor', 'title': 'Actor'})
    assessment(tmp_path, adversarial_evidence=[
        {'source': 'sources/missing', 'question_kind': 'actor-tooling-overlap'}])
    result = r.resolve(tmp_path, SUBJECT, QUESTION, selection_policy=policy)
    assert result['reference_diagnostics'][0]['blocking'] is False


@pytest.mark.parametrize('empty', [None, ''])
def test_an_absent_question_kind_keeps_the_family_rule(tmp_path, empty):
    actor(tmp_path)
    assessment(tmp_path, adversarial_evidence=[{'source': 'sources/missing', 'question_kind': empty}])
    diagnostic = resolve(tmp_path)['reference_diagnostics'][0]
    assert diagnostic['blocking'] is False
    assert diagnostic['reason'] == 'unrelated-assessment-reference'
