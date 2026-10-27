"""Cross-survey question statistics against real publications and MongoDB.

All data is synthetic and belongs to conftest's isolated database. Explicit
fault hooks below inject driver failures; they do not simulate network loss.
"""
import asyncio
from copy import deepcopy
import csv
from datetime import timedelta
import io
from uuid import uuid4

import pytest
from pymongo.errors import PyMongoError, WriteError

from app.database import init_database
from app.repositories import Repository
from conftest import account, published, start, submit
from test_question_sharing import parties, share
from test_question_sources import (
    definition, draft, keys_in, publish, read_survey, save, snapshot, source, unchanged,
)
from test_question_versions import accepted, create_question, derive, number_content, rejected


async def source_survey(context, owner, version, *, value=None, mappings=None):
    value = definition(version) if value is None else value
    mappings = [source(version)] if mappings is None else mappings
    survey = await draft(context, owner, value, mappings, multiple=True)
    result = await publish(context, owner, survey)
    assert result.status_code == 201, result.text
    return await read_survey(context, owner, survey), result.json()


async def answer(context, owner, survey, values, *, anonymous=True):
    result = await submit(context, owner, survey, await start(context, owner, survey), values, anonymous=anonymous)
    assert result.status_code == 201, result.text
    return result.json()


async def scope_response(context, owner, question, *, key=None, version=None, respondent='all'):
    return await context.client.post(f'/api/questions/{question["questionId"]}/statistics/scopes',
        headers={**owner, 'Idempotency-Key': key or str(uuid4())},
        json={'questionVersionId': version, 'respondentType': respondent})


async def new_scope(context, owner, question, **kwargs):
    result = await scope_response(context, owner, question, **kwargs)
    assert result.status_code == 201, result.text
    assert result.headers.get('Idempotent-Replay') == 'false'
    return result.json()


async def get(context, owner, question, scope, suffix='', **params):
    return await context.client.get(f'/api/questions/{question["questionId"]}/statistics{suffix}',
        headers=owner, params={'scopeId': scope['id'], **params})


async def statistics(context, owner, question, scope, **params):
    result = await get(context, owner, question, scope, **params)
    assert result.status_code == 200, result.text
    return result.json()


async def stat_grant(context, owner, survey, question, *, active=True, revision=0):
    return await context.client.put(f'/api/surveys/{survey["id"]}/question-statistics-grants/{question["questionId"]}',
        headers=owner, json={'active': active, 'expectedRevision': revision})


async def golden(context):
    owner, user = await account(context)
    question = accepted(await create_question(context, owner, number_content('共享评分', min=0, max=1000)), 1)
    adef = definition(question, 'a1', '问卷 A')
    adef['questions'][0].update(defaultTarget='a2', rules=[{'id': 'end_high', 'priority': 1,
        'condition': {'operator': 'gte', 'value': 30}, 'target': 'END'}])
    adef['questions'].append({'id': 'a2', **deepcopy(question['content']), 'required': False,
        'defaultTarget': 'END', 'rules': []})
    a, _ = await source_survey(context, owner, question, value=adef,
        mappings=[source(question, 'a1'), source(question, 'a2')])
    await answer(context, owner, a, {'a1': 0, 'a2': 10})
    await answer(context, owner, a, {'a1': 20}, anonymous=False)
    await answer(context, owner, a, {'a1': 30})
    bdef = definition(question, 'bscore', '问卷 B')
    bdef['entryQuestionId'] = 'gate'
    bdef['questions'].insert(0, {'id': 'gate', 'kind': 'single', 'title': '参与评分？',
        'config': {'options': [{'id': 'yes', 'label': '是'}, {'id': 'no', 'label': '否'}]},
        'required': True, 'defaultTarget': 'bscore', 'rules': [{'id': 'skip', 'priority': 1,
            'condition': {'operator': 'eqOption', 'value': 'no'}, 'target': 'END'}]})
    b, _ = await source_survey(context, owner, question, value=bdef, mappings=[source(question, 'bscore')])
    await answer(context, owner, b, {'gate': 'yes', 'bscore': 40}, anonymous=False)
    await answer(context, owner, b, {'gate': 'no'})
    return owner, user, question, a, b


async def test_multi_instance_routing_numeric_golden_and_independent_survey_boundaries(context):
    owner, _, question, a, b = await golden(context)
    boundary = await new_scope(context, owner, question)
    before = await snapshot(context)
    assert (boundary['surveyCount'], boundary['publicationCount'], boundary['instanceCount']) == (2, 2, 3)
    result = await statistics(context, owner, question, boundary)
    row = result['versions'][0]
    assert {key: row[key] for key in ('R', 'V', 'A', 'U', 'S', 'sum', 'avg', 'min', 'max')} == {
        'R': 8, 'V': 6, 'A': 5, 'U': 1, 'S': 2, 'sum': 100, 'avg': 20, 'min': 0, 'max': 40}
    assert row['responseCount'] == 5 and row['answeredResponseCount'] == 4
    assert row['anonymousResponses'] == 3 and row['namedResponses'] == 2
    assert sum(item['count'] for item in row['histogram']) == row['A']
    assert len(row['histogram']) == 10
    assert row['histogram'][0]['from'] == 0 and row['histogram'][-1]['to'] == 40
    assert row['histogram'][-1]['includeTo'] is True
    assert result['overview']['responseCount'] == 5
    sources = (await get(context, owner, question, boundary, '/sources')).json()['items']
    assert {item['surveyId']: item['asOfSeq'] for item in sources} == {a['id']: 3, b['id']: 2}
    await unchanged(context, before)
    # B's next seq=3 would sneak into an incorrectly global max(Aseq,Bseq)=3.
    await answer(context, owner, b, {'gate': 'yes', 'bscore': 999})
    assert await statistics(context, owner, question, boundary) == result
    refreshed = await new_scope(context, owner, question)
    fresh = (await statistics(context, owner, question, refreshed))['versions'][0]
    assert (fresh['R'], fresh['V'], fresh['A'], fresh['U'], fresh['S'], fresh['sum']) == (9, 7, 6, 1, 2, 1099)
    raw = (await get(context, owner, question, boundary, '/answers')).json()['items']
    assert sorted(row['value'] for row in raw) == [0, 10, 20, 30, 40]


async def test_versions_separate_and_overview_does_not_double_count_one_response(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content('旧评分', min=0, max=100)), 1)
    second = accepted(await derive(context, owner, first, number_content('新评分', min=1, max=5)), 2)
    value = definition(first, 'old')
    value['questions'][0]['defaultTarget'] = 'new'
    value['questions'].append({'id': 'new', **deepcopy(second['content']), 'required': True,
        'defaultTarget': 'END', 'rules': []})
    survey, _ = await source_survey(context, owner, first, value=value,
        mappings=[source(first, 'old'), source(second, 'new')])
    await answer(context, owner, survey, {'old': 90, 'new': 5})
    boundary = await new_scope(context, owner, first)
    result = await statistics(context, owner, first, boundary, limit=1)
    assert result['overview']['responseCount'] == result['overview']['answeredResponseCount'] == 1
    assert result['versions'][0]['questionVersionId'] == first['id']
    assert result['versions'][0]['sum'] == 90 and result['nextAfterVersionNo'] == 1
    next_page = await statistics(context, owner, first, boundary, limit=1, afterVersionNo=1)
    assert next_page['versions'][0]['questionVersionId'] == second['id']
    assert next_page['versions'][0]['sum'] == 5 and next_page['nextAfterVersionNo'] is None
    pinned = await new_scope(context, owner, first, version=second['id'])
    assert (await statistics(context, owner, first, pinned))['versions'][0]['sum'] == 5
    rejected(await get(context, owner, first, pinned, '/answers', questionVersionId=first['id']),
        'INVALID_STATISTICS_CURSOR', 422)
    another = accepted(await create_question(context, owner), 1)
    for suffix in ('/answers', '/export'):
        for wrong in (another['id'], str(uuid4())):
            rejected(await get(context, owner, first, boundary, suffix, questionVersionId=wrong),
                'QUESTION_VERSION_NOT_FOUND', 404)
        rejected(await get(context, owner, first, pinned, suffix, questionVersionId=first['id']),
            'INVALID_STATISTICS_CURSOR', 422)


async def test_republication_keeps_old_sources_and_requires_explicit_new_scope(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content('旧版评分', min=0, max=10)), 1)
    survey, _ = await source_survey(context, owner, first)
    await answer(context, owner, survey, {'score': 1})
    boundary = await new_scope(context, owner, first)
    original = await statistics(context, owner, first, boundary)
    second = accepted(await derive(context, owner, first, number_content('新版评分', min=1, max=5)), 2)
    changed = await save(context, owner, survey, definition(second), [source(second)])
    assert changed.status_code == 200, changed.text
    publication = await publish(context, owner, changed.json())
    assert publication.status_code == 201, publication.text
    survey = await read_survey(context, owner, survey)
    await answer(context, owner, survey, {'score': 4})
    assert await statistics(context, owner, first, boundary) == original
    refreshed = await new_scope(context, owner, first)
    result = await statistics(context, owner, first, refreshed)
    assert (refreshed['surveyCount'], refreshed['publicationCount'], refreshed['instanceCount']) == (1, 2, 2)
    assert result['overview']['responseCount'] == 2
    assert [(row['questionVersionId'], row['A'], row['sum']) for row in result['versions']] == [
        (first['id'], 1, 1), (second['id'], 1, 4)]


@pytest.mark.parametrize('respondent,expected', [('all', (8, 6, 5, 1, 2, 100)),
    ('anonymous', (5, 3, 3, 0, 2, 40)), ('named', (3, 3, 2, 1, 0, 60))])
async def test_respondent_filter_is_bound_in_every_read(context, respondent, expected):
    owner, _, question, _, _ = await golden(context)
    boundary = await new_scope(context, owner, question, respondent=respondent)
    result = await statistics(context, owner, question, boundary)
    assert tuple(result['versions'][0][key] for key in ('R', 'V', 'A', 'U', 'S', 'sum')) == expected
    raw = (await get(context, owner, question, boundary, '/answers')).json()['items']
    assert len(raw) == expected[2]
    if respondent != 'all':
        assert all(row['anonymous'] == (respondent == 'anonymous') for row in raw)


@pytest.mark.parametrize('values', [[], [0], [5, 5], [1e12, 1e12 + .0002]],
    ids=['empty', 'zero', 'constant', 'narrow-large'])
async def test_numeric_empty_constant_zero_and_narrow_histograms(context, values):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner, number_content()), 1)
    survey, _ = await source_survey(context, owner, question)
    for value in values:
        await answer(context, owner, survey, {'score': value})
    boundary = await new_scope(context, owner, question)
    row = (await statistics(context, owner, question, boundary))['versions'][0]
    assert row['A'] == len(values) and row['sum'] == sum(values)
    assert sum(bin['count'] for bin in row['histogram']) == len(values)
    assert len(row['histogram']) <= 10
    assert row['avg'] == (sum(values)/len(values) if values else None)
    assert (row['min'], row['max']) == ((min(values), max(values)) if values else (None, None))
    if values:
        assert row['histogram'][0]['from'] == min(values) and row['histogram'][-1]['to'] == max(values)
        assert row['histogram'][-1]['includeTo'] is True
    else:
        assert row['histogram'] == []


@pytest.mark.parametrize('kind', ['single', 'multi', 'text'])
async def test_other_kinds_distribution_and_text_privacy(context, kind):
    owner, _ = await account(context)
    options = [{'id': 'a', 'label': '甲'}, {'id': 'b', 'label': '乙'}, {'id': 'c', 'label': '丙'}]
    content = {'kind': kind, 'title': '复用题', 'config': {} if kind == 'text' else {'options': options}}
    question = accepted(await create_question(context, owner, content), 1)
    survey, _ = await source_survey(context, owner, question)
    values = {'single': ['a', 'a', 'b'], 'multi': [['a', 'b'], ['a'], ['b']],
              'text': ['正文内容', '=1+1', '  @formula']}[kind]
    for value in values:
        await answer(context, owner, survey, {'score': value})
    boundary = await new_scope(context, owner, question)
    row = (await statistics(context, owner, question, boundary))['versions'][0]
    assert (row['R'], row['V'], row['A'], row['U'], row['S']) == (3, 3, 3, 0, 0)
    if kind in ('single', 'multi'):
        counts = [2, 1, 0] if kind == 'single' else [2, 2, 0]
        assert [item['count'] for item in row['options']] == counts
        assert [item['percentage'] for item in row['options']] == pytest.approx([count*100/3 for count in counts])
        assert row['totalSelections'] == sum(counts)
    else:
        assert not {'texts', 'values', 'answers', 'responseId', 'participantKey'}.intersection(keys_in(row))
        assert '正文内容' not in str(row)
        export = await get(context, owner, question, boundary, '/export')
        assert export.status_code == 200 and export.content.startswith(b'\xef\xbb\xbf')
        rows = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
        # Submission's existing text normalization trims surrounding whitespace.
        assert [item['答案'] for item in rows] == ['正文内容', "'=1+1", "'@formula"]
        raw = (await get(context, owner, question, boundary, '/answers')).json()['items']
        assert [item['value'] for item in raw] == [value.strip() for value in values]


async def test_only_published_explicit_identity_sources_not_titles_or_current_drafts(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content('同一标题')), 1)
    other = accepted(await create_question(context, owner, number_content('同一标题')), 1)
    tracked, _ = await source_survey(context, owner, first)
    unrelated, _ = await source_survey(context, owner, other)
    legacy, _ = await published(context, owner, definition(first), multiple=True)
    await draft(context, owner, definition(first), [source(first)], multiple=True)
    await answer(context, owner, tracked, {'score': 3})
    await answer(context, owner, unrelated, {'score': 100})
    await answer(context, owner, legacy, {'score': 1000})
    changed = await save(context, owner, tracked, definition(first), [])
    assert changed.status_code == 200, changed.text
    boundary = await new_scope(context, owner, first)
    result = await statistics(context, owner, first, boundary)
    assert boundary['publicationCount'] == boundary['surveyCount'] == boundary['instanceCount'] == 1
    assert result['versions'][0]['A'] == 1 and result['versions'][0]['sum'] == 3


async def test_source_and_answer_keyset_pages_have_no_duplicates_and_cursors_are_bound(context):
    owner, _, question, _, _ = await golden(context)
    boundary = await new_scope(context, owner, question)
    sources, after = [], None
    for _ in range(4):
        response = await get(context, owner, question, boundary, '/sources', limit=1,
            **({'afterId': after} if after else {}))
        assert response.status_code == 200, response.text
        page = response.json(); sources.extend(page['items']); after = page['nextAfterId']
        if not after: break
    assert len(sources) == len({item['id'] for item in sources}) == 2
    rows, cursor, first_cursor = [], None, None
    for _ in range(10):
        result = await get(context, owner, question, boundary, '/answers', limit=1,
            **({'cursor': cursor} if cursor else {}))
        assert result.status_code == 200, result.text
        page = result.json(); rows.extend(page['items']); cursor = page['nextCursor']
        first_cursor = first_cursor or cursor
        if not cursor: break
    assert len(rows) == len({(item['responseId'], item['instanceId']) for item in rows}) == 5
    assert sorted(item['value'] for item in rows) == [0, 10, 20, 30, 40]
    assert not {'ownerId', 'participantKey', 'respondent', 'username', 'passwordHash', '_sourceId'}.intersection(keys_in(rows))
    second_scope = await new_scope(context, owner, question)
    rejected(await get(context, owner, question, second_scope, '/answers', cursor=first_cursor),
        'INVALID_STATISTICS_CURSOR', 422)
    rejected(await get(context, owner, question, boundary, '/answers', cursor=first_cursor,
        questionVersionId=question['id']), 'INVALID_STATISTICS_CURSOR', 422)
    corrupted = first_cursor[:-10] + ('A' if first_cursor[-10] != 'A' else 'B') + first_cursor[-9:]
    rejected(await get(context, owner, question, boundary, '/answers', cursor=corrupted),
        'INVALID_STATISTICS_CURSOR', 422)


async def test_scope_replay_conflict_ownership_expiry_and_metadata_are_explicit(context):
    owner, _, question, _, _ = await golden(context)
    other, _ = await account(context, 'stats_stranger')
    key = str(uuid4())
    boundary = await new_scope(context, owner, question, key=key)
    replay = await scope_response(context, owner, question, key=key)
    assert replay.status_code == 200 and replay.headers.get('Idempotent-Replay') == 'true'
    assert replay.json() == boundary
    rejected(await scope_response(context, owner, question, key=key, respondent='named'), 'IDEMPOTENCY_CONFLICT')
    rejected(await get(context, other, question, boundary), 'NOT_FOUND', 404)
    rejected(await scope_response(context, other, question), 'NOT_FOUND', 404)
    foreign_question = accepted(await create_question(context, owner), 1)
    rejected(await get(context, owner, foreign_question, boundary), 'NOT_FOUND', 404)
    root = await context.db.question_stat_scopes.find_one({'_id': boundary['id']})
    children = await context.db.question_stat_sources.find({'scopeId': boundary['id']}).to_list(length=None)
    assert not {'answers', 'value', 'participantKey', 'respondent', 'definition', 'questionSources'}.intersection(keys_in([root, children]))
    assert root['purgeAt'] - root['createdAt'] == timedelta(days=7)
    assert root['expiresAt'] - root['createdAt'] == timedelta(minutes=30)
    assert all(item['expiresAt'] == root['expiresAt'] for item in children)
    context.app.state.clock = lambda: root['expiresAt'] + timedelta(seconds=1)
    for suffix in ('', '/sources', '/answers', '/export'):
        rejected(await get(context, owner, question, boundary, suffix), 'STATISTICS_SCOPE_EXPIRED', 410)
    rejected(await scope_response(context, owner, question, key=key), 'STATISTICS_SCOPE_EXPIRED', 410)


async def test_scope_transaction_fault_rolls_back_and_original_request_can_retry(context):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner), 1)
    await source_survey(context, owner, question)
    key = str(uuid4())
    calls = []
    async def fail_after_insert(**state):
        calls.append(state['scope']['_id'])
        raise PyMongoError('explicit test injection after real range insert')
    context.app.state.stat_hooks['after_question_stats_scope_write'] = fail_after_insert
    rejected(await scope_response(context, owner, question, key=key), 'STATISTICS_SCOPE_WRITE_OUTCOME_UNKNOWN', 503)
    assert len(calls) == 1
    assert await context.db.question_stat_scopes.count_documents({}) == 0
    assert await context.db.question_stat_sources.count_documents({}) == 0
    context.app.state.stat_hooks.clear()
    boundary = await new_scope(context, owner, question, key=key)
    assert await context.db.question_stat_scopes.count_documents({}) == 1
    assert await context.db.question_stat_sources.count_documents({'scopeId': boundary['id']}) == 1


async def test_scope_snapshot_remains_before_submission_committed_during_capture(context):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner), 1)
    survey, _ = await source_survey(context, owner, question)
    await answer(context, owner, survey, {'score': 1})
    seen = []
    async def submit_after_capture(**state):
        seen.append(state['scope']['_id'])
        await answer(context, owner, survey, {'score': 100})
    context.app.state.stat_hooks['after_question_stats_capture'] = submit_after_capture
    boundary = await new_scope(context, owner, question)
    context.app.state.stat_hooks.clear()
    assert len(seen) == 1
    row = (await statistics(context, owner, question, boundary))['versions'][0]
    assert row['A'] == 1 and row['sum'] == 1
    children = await context.db.question_stat_sources.find({'scopeId': boundary['id']}).to_list(length=None)
    assert [item['asOfSeq'] for item in children] == [1]
    assert (await context.db.surveys.find_one({'_id': survey['id']}))['submitSequence'] == 2


async def test_scope_recovery_after_actual_commit_reuses_persisted_receipt(context, monkeypatch):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner), 1)
    await source_survey(context, owner, question)
    original = Repository.transaction
    calls = []
    async def committed_then_driver_failure(self, operation):
        result = await original(self, operation)
        calls.append(True)
        raise PyMongoError('explicit boundary injection after an actual committed transaction')
    monkeypatch.setattr(Repository, 'transaction', committed_then_driver_failure)
    key = str(uuid4())
    result = await scope_response(context, owner, question, key=key)
    assert result.status_code == 200, result.text
    assert result.headers.get('Idempotent-Replay') == 'true'
    assert len(calls) == 1 and await context.db.question_stat_scopes.count_documents({}) == 1
    replay = await scope_response(context, owner, question, key=key)
    assert replay.status_code == 200 and replay.json() == result.json()
    assert len(calls) == 1


async def test_use_sharing_does_not_grant_answers_and_survey_owner_can_authorize_aggregate(context):
    author, _, receiver, _, question, _ = await parties(context)
    own, _ = await source_survey(context, author, question)
    foreign, _ = await source_survey(context, receiver, question)
    await answer(context, author, own, {'score': 2})
    await answer(context, receiver, foreign, {'score': 7})
    before_scope = await new_scope(context, author, question)
    assert (await statistics(context, author, question, before_scope))['versions'][0]['sum'] == 2
    receiver_scope = await new_scope(context, receiver, question)
    assert (await statistics(context, receiver, question, receiver_scope))['versions'][0]['sum'] == 7
    # Existing ordinary per-survey answers/stats stay private to their owner.
    response = await context.client.get(f'/api/surveys/{foreign["id"]}/stats', headers=author,
        params={'versionId': foreign['currentVersionId']})
    assert response.status_code == 404, response.text
    candidates = await context.client.get(f'/api/surveys/{foreign["id"]}/question-statistics-grants', headers=receiver)
    assert candidates.status_code == 200, candidates.text
    assert [(item['questionId'], item['grant']) for item in candidates.json()['items']] == [(question['questionId'], None)]
    rejected(await stat_grant(context, author, foreign, question), 'NOT_FOUND', 404)
    grant = await stat_grant(context, receiver, foreign, question)
    assert grant.status_code == 200 and grant.json()['permission'] == 'aggregate'
    assert grant.json()['recipientUsername'] == 'share_author' and grant.json()['revision'] == 1
    replay = await stat_grant(context, receiver, foreign, question)
    assert replay.status_code == 200 and replay.headers.get('Idempotent-Replay') == 'true'
    assert replay.json() == grant.json()
    boundary = await new_scope(context, author, question)
    result = await statistics(context, author, question, boundary)
    assert result['versions'][0]['sum'] == 9 and result['versions'][0]['A'] == 2
    assert boundary['hasForeignSources'] is True
    # Scope captured before authorization does not silently grow after it.
    assert (await statistics(context, author, question, before_scope))['versions'][0]['sum'] == 2
    sources = (await get(context, author, question, boundary, '/sources')).json()['items']
    aggregate = next(item for item in sources if item['accessMode'] == 'aggregate')
    assert aggregate['canReadAnswers'] is False and aggregate['surveyTitle'] == '已获授权的其他问卷'
    assert not {'surveyId', 'surveyVersionId'}.intersection(aggregate)
    raw = (await get(context, author, question, boundary, '/answers')).json()
    assert [item['value'] for item in raw['items']] == [2]
    assert raw['coverage'] == {'ownOnly': True, 'hasForeignAggregateSources': True}
    exported = await get(context, author, question, boundary, '/export')
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))
    assert [row['答案'] for row in rows] == ['2']
    revoke = await stat_grant(context, receiver, foreign, question, active=False, revision=1)
    assert revoke.status_code == 200 and revoke.json()['revision'] == 2 and revoke.json()['active'] is False
    for suffix in ('', '/sources', '/answers', '/export'):
        rejected(await get(context, author, question, boundary, suffix), 'STATISTICS_SCOPE_INVALIDATED')
    rejected(await stat_grant(context, receiver, foreign, question), 'STATISTICS_GRANT_CONFLICT')
    regrant = await stat_grant(context, receiver, foreign, question, revision=2)
    assert regrant.status_code == 200 and regrant.json()['revision'] == 3
    rejected(await get(context, author, question, boundary), 'STATISTICS_SCOPE_INVALIDATED')
    refreshed = await new_scope(context, author, question)
    assert (await statistics(context, author, question, refreshed))['versions'][0]['sum'] == 9


async def test_aggregate_only_scope_has_no_raw_rows_and_use_revocation_preserves_owner_history(context):
    author, _, receiver, _, question, _ = await parties(context)
    foreign, _ = await source_survey(context, receiver, question)
    await answer(context, receiver, foreign, {'score': 4})
    assert (await stat_grant(context, receiver, foreign, question)).status_code == 200
    boundary = await new_scope(context, author, question)
    assert (await statistics(context, author, question, boundary))['versions'][0]['sum'] == 4
    raw = await get(context, author, question, boundary, '/answers')
    assert raw.status_code == 200 and raw.json()['items'] == []
    exported = await get(context, author, question, boundary, '/export')
    assert list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff')))) == []
    assert (await share(context, author, question, active=False, revision=1)).status_code == 200
    own_history = await new_scope(context, receiver, question)
    assert (await statistics(context, receiver, question, own_history))['versions'][0]['sum'] == 4


async def test_grant_fault_rollback_and_owner_only_candidate_derivation(context):
    author, _, receiver, _, question, _ = await parties(context)
    survey, _ = await source_survey(context, receiver, question)
    stranger, _ = await account(context, 'grant_stranger')
    denied = await context.client.get(f'/api/surveys/{survey["id"]}/question-statistics-grants', headers=stranger)
    assert denied.status_code == 404
    empty = await draft(context, receiver, definition(question), [source(question)])
    rejected(await stat_grant(context, receiver, empty, question), 'NOT_FOUND', 404)
    async def fail(**state):
        raise PyMongoError('explicit test injection after actual grant write')
    context.app.state.stat_hooks['after_question_stats_grant_write'] = fail
    rejected(await stat_grant(context, receiver, survey, question), 'STATISTICS_GRANT_WRITE_OUTCOME_UNKNOWN', 503)
    assert await context.db.question_stat_grants.count_documents({}) == 0
    context.app.state.stat_hooks.clear()
    assert (await stat_grant(context, receiver, survey, question)).status_code == 200
    own, _ = await source_survey(context, author, question)
    rejected(await stat_grant(context, author, own, question), 'INVALID_STATISTICS_GRANT', 422)


async def test_concurrent_grant_decisions_have_one_revision_winner(context):
    _, _, receiver, _, question, _ = await parties(context)
    survey, _ = await source_survey(context, receiver, question)
    assert (await stat_grant(context, receiver, survey, question)).status_code == 200
    decisions = await asyncio.gather(
        stat_grant(context, receiver, survey, question, active=False, revision=1),
        stat_grant(context, receiver, survey, question, active=True, revision=1))
    assert sorted(result.status_code for result in decisions) == [200, 409], [result.text for result in decisions]
    winner = next(result.json() for result in decisions if result.status_code == 200)
    loser = next(result for result in decisions if result.status_code == 409)
    assert loser.json()['code'] == 'STATISTICS_GRANT_CONFLICT'
    row = await context.db.question_stat_grants.find_one({'surveyId': survey['id']})
    assert row['revision'] == winner['revision'] == 2 and row['active'] == winner['active']
    rejected(await stat_grant(context, receiver, survey, question), 'STATISTICS_GRANT_CONFLICT')


async def test_scope_schemas_indexes_and_init_preserve_existing_business_documents(context):
    owner, _, question, _, _ = await golden(context)
    boundary = await new_scope(context, owner, question)
    before = await snapshot(context)
    await init_database(context.db)
    await unchanged(context, before)
    root = await context.db.question_stat_scopes.find_one({'_id': boundary['id']})
    child = await context.db.question_stat_sources.find_one({'scopeId': boundary['id']})
    for name, row in [('question_stat_scopes', root), ('question_stat_sources', child)]:
        invalid = deepcopy(row); invalid['_id'] = str(uuid4()); invalid['answers'] = [{'value': 'private'}]
        if name == 'question_stat_scopes': invalid['creationKey'] = str(uuid4())
        else: invalid['scopeId'] = str(uuid4())
        with pytest.raises(WriteError) as failure:
            await context.db[name].insert_one(invalid)
        assert failure.value.code == 121
    indexes = {name: list((await context.db[name].index_information()).values())
        for name in ('question_stat_scopes', 'question_stat_sources', 'question_stat_grants')}
    assert any(item['key'] == [('viewerId', 1), ('creationKey', 1)] and item.get('unique')
               for item in indexes['question_stat_scopes'])
    assert any(item['key'] == [('purgeAt', 1)] and item.get('expireAfterSeconds') == 0
               for item in indexes['question_stat_scopes'])
    assert any(item['key'] == [('expiresAt', 1)] and item.get('expireAfterSeconds') == 0
               for item in indexes['question_stat_sources'])
    assert any(item['key'] == [('surveyId', 1), ('questionId', 1)] and item.get('unique')
               for item in indexes['question_stat_grants'])


@pytest.mark.parametrize('body', [
    {'respondentType': 'everyone'}, {'respondentType': 1}, {'questionVersionId': ''},
    {'questionVersionId': 1}, {'extra': True},
])
async def test_scope_request_validation_does_not_create_receipts(context, body):
    owner, _ = await account(context)
    question = accepted(await create_question(context, owner), 1)
    result = await context.client.post(f'/api/questions/{question["questionId"]}/statistics/scopes',
        headers={**owner, 'Idempotency-Key': str(uuid4())}, json=body)
    assert result.status_code == 422, result.text
    assert await context.db.question_stat_scopes.count_documents({}) == 0


async def test_all_statistics_routes_require_authentication(context):
    base = '/api/questions/unknown/statistics'
    for suffix in ('', '/sources', '/answers', '/export'):
        result = await context.client.get(base + suffix, params={'scopeId': str(uuid4())})
        assert result.status_code == 401, result.text
    result = await context.client.post(base + '/scopes', headers={'Idempotency-Key': str(uuid4())}, json={})
    assert result.status_code == 401
    result = await context.client.get('/api/surveys/unknown/question-statistics-grants')
    assert result.status_code == 401
    result = await context.client.put('/api/surveys/unknown/question-statistics-grants/unknown',
        json={'active': True, 'expectedRevision': 0})
    assert result.status_code == 401
