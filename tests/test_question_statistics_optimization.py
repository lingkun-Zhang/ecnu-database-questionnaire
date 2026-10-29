"""Optimization contracts exercised through real MongoDB and API writes.

Counters delegate every read to PyMongo. The explicitly labelled transient
fault makes the real transaction retry; no successful storage is fabricated.
"""
from collections import Counter
from copy import deepcopy
import csv
import io

import pytest
from pymongo.errors import OperationFailure

from app.repositories import Repository
from app.question_statistics import QuestionStatisticsServices
from conftest import account
from test_question_sharing import parties
from test_question_sources import definition, publish, read_survey, save, source
from test_question_statistics import answer, get, new_scope, source_survey, stat_grant, statistics
from test_question_versions import accepted, create_question, derive, number_content, rejected


def choices(number, kind):
    return {'kind': kind, 'title': f'版本{number}的选择题', 'config': {'options': [
        {'id': 'same', 'label': f'版本{number}的甲'}, {'id': 'other', 'label': f'版本{number}的乙'}]}}


async def republish(context, owner, survey):
    response = await publish(context, owner, survey)
    assert response.status_code == 201, response.text
    return await read_survey(context, owner, survey)


@pytest.mark.parametrize('kind', ['single', 'multi'])
async def test_choice_details_use_fixed_labels_even_beyond_first_ten_summary_versions(context, kind):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, choices(1, kind)), 1)
    survey, _ = await source_survey(context, owner, first)
    raw = 'same' if kind == 'single' else ['same', 'other']
    await answer(context, owner, survey, {'score': raw})
    current = first
    for number in range(2, 12):
        current = accepted(await derive(context, owner, current, choices(number, kind)), number)
        saved = await save(context, owner, survey, definition(current), [source(current)])
        assert saved.status_code == 200, saved.text
        survey = await republish(context, owner, saved.json())
    await answer(context, owner, survey, {'score': raw})
    boundary = await new_scope(context, owner, first)
    summary = await statistics(context, owner, first, boundary)
    assert [row['versionNo'] for row in summary['versions']] == list(range(1, 11))
    assert summary['nextAfterVersionNo'] == 10
    details = await get(context, owner, first, boundary, '/answers')
    assert details.status_code == 200, details.text
    rows = {row['questionVersionNo']: row for row in details.json()['items']}
    assert set(rows) == {1, 11}
    for number, row in rows.items():
        expected = f'版本{number}的甲' if kind == 'single' else [f'版本{number}的乙', f'版本{number}的甲']
        assert row['value'] == (raw if kind == 'single' else sorted(raw))
        assert row['displayValue'] == expected
    # This requires no summary-page navigation and does not change CSV IDs.
    exported = await get(context, owner, first, boundary, '/export')
    assert exported.status_code == 200, exported.text
    csv_rows = list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))
    assert [row['答案'] for row in csv_rows] == ['same' if kind == 'single' else 'other；same'] * 2


async def repeated_sources(context):
    author, _, receiver, _, question, _ = await parties(context)
    value = definition(question, 'a')
    value['questions'][0]['defaultTarget'] = 'b'
    value['questions'].append({'id': 'b', **deepcopy(question['content']), 'required': False,
        'defaultTarget': 'END', 'rules': []})
    mappings = [source(question, 'a'), source(question, 'b')]
    own, _ = await source_survey(context, author, question, value=value, mappings=mappings)
    foreign, _ = await source_survey(context, receiver, question, value=value, mappings=mappings)
    for _ in range(2):
        own = await republish(context, author, own)
        foreign = await republish(context, receiver, foreign)
    assert (await stat_grant(context, receiver, foreign, question)).status_code == 200
    boundary = await new_scope(context, author, question)
    assert (boundary['surveyCount'], boundary['publicationCount'], boundary['instanceCount']) == (2, 6, 12)
    return author, receiver, question, own, foreign, boundary


def count_metadata_reads(context, monkeypatch):
    collection_type = type(context.db.surveys)
    original_find, original_find_one = collection_type.find, collection_type.find_one
    names = {'surveys', 'survey_versions', 'question_versions', 'question_stat_grants'}
    finds, singles = Counter(), Counter()

    def find(collection, query=None, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name in names:
            finds[collection.name] += 1
        return original_find(collection, query, *args, **kwargs)

    async def find_one(collection, query=None, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name in names:
            singles[collection.name] += 1
        return await original_find_one(collection, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'find', find)
    monkeypatch.setattr(collection_type, 'find_one', find_one)
    return finds, singles


async def test_metadata_reads_batch_duplicate_surveys_grants_and_version_instances(context, monkeypatch):
    author, _, question, _, _, boundary = await repeated_sources(context)
    finds, singles = count_metadata_reads(context, monkeypatch)
    result = await statistics(context, author, question, boundary)
    assert result['overview']['publicationCount'] == 6
    assert result['versions'][0]['instanceCount'] == 12
    assert result['versions'][0]['A'] == 0
    assert singles == {}
    assert finds == {'surveys': 1, 'question_stat_grants': 1, 'survey_versions': 1, 'question_versions': 1}


async def test_metadata_maps_are_rebuilt_on_actual_transaction_callback_retry(context, monkeypatch):
    author, _, question, _, _, boundary = await repeated_sources(context)
    finds, singles = count_metadata_reads(context, monkeypatch)
    original_transaction = Repository.transaction
    attempts = []

    async def transaction(repository, operation):
        async def transient_after_real_reads(session):
            result = await operation(session)
            attempts.append(True)
            if len(attempts) == 1:
                raise OperationFailure('explicit test transient after real snapshot reads', 112,
                    {'errorLabels': ['TransientTransactionError'], 'code': 112})
            return result
        return await original_transaction(repository, transient_after_real_reads)

    monkeypatch.setattr(Repository, 'transaction', transaction)
    result = await statistics(context, author, question, boundary)
    assert result['versions'][0]['instanceCount'] == 12 and result['versions'][0]['A'] == 0
    assert len(attempts) == 2 and singles == {}
    assert finds == {'surveys': 2, 'question_stat_grants': 2, 'survey_versions': 2, 'question_versions': 2}


async def test_choice_display_never_includes_foreign_answers_and_rechecks_revoked_grants(context):
    author, _, receiver, _, first, _ = await parties(context)
    question = accepted(await derive(context, author, first, choices(2, 'single')), 2)
    own, _ = await source_survey(context, author, question)
    foreign, _ = await source_survey(context, receiver, question)
    await answer(context, author, own, {'score': 'same'})
    await answer(context, receiver, foreign, {'score': 'other'})
    assert (await stat_grant(context, receiver, foreign, question)).status_code == 200
    boundary = await new_scope(context, author, question)
    assert (await statistics(context, author, question, boundary))['versions'][0]['A'] == 2
    result = await get(context, author, question, boundary, '/answers')
    assert result.status_code == 200, result.text
    assert [(row['value'], row['displayValue']) for row in result.json()['items']] == [('same', '版本2的甲')]
    assert result.json()['coverage'] == {'ownOnly': True, 'hasForeignAggregateSources': True}
    assert (await stat_grant(context, receiver, foreign, question, active=False, revision=1)).status_code == 200
    for suffix in ('', '/sources', '/answers', '/export'):
        rejected(await get(context, author, question, boundary, suffix), 'STATISTICS_SCOPE_INVALIDATED')
    assert (await stat_grant(context, receiver, foreign, question, revision=2)).status_code == 200
    rejected(await get(context, author, question, boundary), 'STATISTICS_SCOPE_INVALIDATED')
    refreshed = await new_scope(context, author, question)
    assert (await statistics(context, author, question, refreshed))['versions'][0]['A'] == 2


async def test_cursor_prefilter_preserves_equal_sequence_instances_and_multi_batch_csv(context, monkeypatch):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner, number_content(min=0, max=1000)), 1)
    value = definition(question, 'a')
    value['questions'][0]['defaultTarget'] = 'b'
    value['questions'].append({'id': 'b', **deepcopy(question['content']), 'required': True,
        'defaultTarget': 'END', 'rules': []})
    survey, _ = await source_survey(context, owner, question, value=value,
        mappings=[source(question, 'a'), source(question, 'b')])
    for number in range(1, 61):
        await answer(context, owner, survey, {'a': number * 2, 'b': number * 2 + 1})
    boundary = await new_scope(context, owner, question)
    collection_type = type(context.db.responses)
    original_aggregate = collection_type.aggregate
    pipelines = []

    async def aggregate(collection, pipeline, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name == 'responses':
            pipelines.append(deepcopy(pipeline))
        return await original_aggregate(collection, pipeline, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'aggregate', aggregate)
    first = await get(context, owner, question, boundary, '/answers', limit=1)
    assert first.status_code == 200, first.text
    second = await get(context, owner, question, boundary, '/answers', limit=1, cursor=first.json()['nextCursor'])
    assert second.status_code == 200, second.text
    assert [(item['acceptedSeq'], item['instanceId'], item['value']) for item in first.json()['items'] + second.json()['items']] == [
        (1, 'a', 2), (1, 'b', 3)]
    assert pipelines[0][0]['$match']['acceptedSeq'] == {'$lte': 60}
    assert pipelines[1][0]['$match']['acceptedSeq'] == {'$lte': 60, '$gte': 1}
    pipelines.clear()
    exported = await get(context, owner, question, boundary, '/export')
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))
    assert len(rows) == 120 and [int(row['答案']) for row in rows] == list(range(2, 122))
    assert len({(row['回执'], row['卷内题目标识']) for row in rows}) == 120
    assert len(pipelines) == 2
    assert pipelines[1][0]['$match']['acceptedSeq'] == {'$lte': 60, '$gte': 50}


async def test_complete_publication_cache_is_bounded_across_many_sources_and_csv_batches(context, monkeypatch):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner, number_content(min=0, max=1000)), 1)
    value = definition(question, 'a')
    value['questions'][0]['defaultTarget'] = 'b'
    value['questions'].append({'id': 'b', **deepcopy(question['content']), 'required': True,
        'defaultTarget': 'END', 'rules': []})
    survey, _ = await source_survey(context, owner, question, value=value,
        mappings=[source(question, 'a'), source(question, 'b')])
    current = question
    for number in range(2, 38):
        current = accepted(await derive(context, owner, current,
            number_content(f'范围版本{number}', min=0, max=1000)), number)
        value = definition(current, 'a')
        value['questions'][0]['defaultTarget'] = 'b'
        value['questions'].append({'id': 'b', **deepcopy(current['content']), 'required': True,
            'defaultTarget': 'END', 'rules': []})
        saved = await save(context, owner, survey, value, [source(current, 'a'), source(current, 'b')])
        assert saved.status_code == 200, saved.text
        survey = await republish(context, owner, saved.json())
    for number in range(1, 61):
        await answer(context, owner, survey, {'a': number * 2, 'b': number * 2 + 1})
    boundary = await new_scope(context, owner, question)
    assert (boundary['publicationCount'], boundary['instanceCount']) == (37, 74)
    original = QuestionStatisticsServices.publication_context
    seen, phase = [], {'name': 'summary'}

    async def traced(service, scope, sources, session, cache):
        result = await original(service, scope, sources, session, cache)
        seen.append((phase['name'], len(sources), len(cache['publications']), len(cache['versions'])))
        return result

    monkeypatch.setattr(QuestionStatisticsServices, 'publication_context', traced)
    result = await statistics(context, owner, question, boundary)
    assert result['overview']['responseCount'] == 60
    assert [row['versionNo'] for row in result['versions']] == list(range(1, 11))
    assert [row[1:] for row in seen] == [(32, 32, 32), (5, 5, 5)]
    phase['name'] = 'summary-last'
    result = await statistics(context, owner, question, boundary, afterVersionNo=30)
    assert result['versions'][-1]['versionNo'] == 37 and result['versions'][-1]['A'] == 120
    phase['name'] = 'sources'
    result = await get(context, owner, question, boundary, '/sources', limit=100)
    assert result.status_code == 200 and len(result.json()['items']) == 37
    assert [row[1:] for row in seen if row[0] == 'sources'] == [(32, 32, 32), (5, 5, 5)]
    phase['name'] = 'export'
    result = await get(context, owner, question, boundary, '/export')
    assert result.status_code == 200, result.text
    rows = list(csv.DictReader(io.StringIO(result.text.lstrip('\ufeff'))))
    assert len(rows) == 120 and [int(row['答案']) for row in rows] == list(range(2, 122))
    # The complete snapshots from earlier batches must not accumulate; only
    # question-version map is restricted to the same current batch as well.
    assert all(batch <= 32 and cached <= 32 and versions <= 32 for _, batch, cached, versions in seen)
    assert max(cached for _, _, cached, _ in seen) == 32
    assert any(name == 'export' for name, _, _, _ in seen)
