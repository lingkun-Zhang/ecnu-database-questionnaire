"""Real snapshot reads, opt-in list summaries and bounded impact candidates."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.database import VALIDATORS, init_database
from app.question_usage import question_impact_pipeline, question_impact_view
from test_question_impact import fixture, impact, result, totals
from test_question_sharing import parties, share
from test_question_sources import definition, draft, keys_in, snapshot, source, unchanged
from test_question_versions import accepted, create_question, derive, number_content


ALL_COLLECTIONS = tuple(VALIDATORS)
ROOT_FIELDS = {"id", "currentVersionId", "lastVersionNo", "createdAt", "updatedAt"}


async def listing(context, headers, *, shared=False, summary=True, **params):
    return await context.client.get('/api/shared-questions' if shared else '/api/questions',
        headers=headers, params={"includeCurrentVersion": str(summary).lower(), **params})


def checked_summary(item):
    assert set(item) == ROOT_FIELDS | {"currentVersion"}
    version = item['currentVersion']
    assert set(version) == {'id', 'questionId', 'versionNo', 'content'}
    assert set(version['content']) == {'kind', 'title'}
    assert version['id'] == item['currentVersionId'] and version['questionId'] == item['id']
    assert not keys_in(item).intersection({'ownerId', 'recipientId', 'config', 'passwordHash', 'requestDigest', 'creationKey'})
    return version


@pytest.mark.parametrize('shared', [False, True], ids=['owner', 'shared'])
async def test_twenty_list_summaries_use_one_real_version_query_in_the_permission_snapshot(context, monkeypatch, shared):
    owner, _, recipient, _, first, _ = await parties(context)
    versions = [first]
    for i in range(19):
        version = accepted(await create_question(context, owner, number_content(f'轻列表{i}')), 1)
        versions.append(version)
        if shared:
            assert (await share(context, owner, version)).status_code == 201
    caller = recipient if shared else owner
    before = await snapshot(context, ALL_COLLECTIONS)
    legacy = await listing(context, caller, shared=shared, summary=False, limit=20)
    assert legacy.status_code == 200 and len(legacy.json()['items']) == 20
    assert all(set(item) == ROOT_FIELDS for item in legacy.json()['items'])
    collection_type = type(context.db.question_versions)
    original = collection_type.find
    queries = []

    def find(collection, query=None, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name == 'question_versions':
            queries.append((deepcopy(query), kwargs.get('session') is not None and kwargs['session'].in_transaction))
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'find', find)
    response = await listing(context, caller, shared=shared, limit=20)
    assert response.status_code == 200, response.text
    items = response.json()['items']
    assert [item['id'] for item in items] == [item['id'] for item in legacy.json()['items']]
    expected = {version['questionId']: version for version in versions}
    for item in items:
        version = checked_summary(item)
        assert version['content'] == {k: expected[item['id']]['content'][k] for k in ('kind', 'title')}
    assert len(queries) == 1 and queries[0][1] is True
    assert set(queries[0][0]) == {'_id'} and set(queries[0][0]['_id']) == {'$in'}
    assert set(queries[0][0]['_id']['$in']) == {version['id'] for version in versions}
    await unchanged(context, before, ALL_COLLECTIONS)


async def test_impact_index_upgrade_is_additive_idempotent_and_leaves_all_business_documents_unchanged(context):
    owner, _, _, _, first, _ = await parties(context)
    await draft(context, owner, definition(first), [source(first)])
    names = ('ix_surveys_question_impact', 'ix_surveys_question_version_impact')
    for name in names:
        await context.db.surveys.drop_index(name)
    previous = await context.db.surveys.index_information()
    before = await snapshot(context, ALL_COLLECTIONS)
    await init_database(context.db)
    upgraded = await context.db.surveys.index_information()
    assert all(upgraded[name] == item for name, item in previous.items())
    assert upgraded[names[0]]['key'] == [('draftQuestionSources.questionId', 1), ('_id', 1)]
    assert upgraded[names[1]]['key'] == [('draftQuestionSources.questionId', 1), ('draftQuestionSources.questionVersionId', 1), ('_id', 1)]
    await init_database(context.db)
    assert await context.db.surveys.index_information() == upgraded
    await unchanged(context, before, ALL_COLLECTIONS)


async def test_list_head_and_summary_keep_one_snapshot_while_a_real_derive_commits(context, monkeypatch):
    owner, _, _, _, first, _ = await parties(context)
    collection_type = type(context.db.question_versions)
    original = collection_type.find
    written = []

    async def commit_new_version():
        written.append(accepted(await derive(context, owner, first, number_content('稍后写入V2')), 2))

    # A real cursor wrapper delays only its first read. The roots have already
    # established the transaction snapshot before the concurrent real write.
    class DelayedCursor:
        def __init__(self, cursor): self.cursor = cursor
        async def to_list(self, *args, **kwargs):
            if not written:
                await commit_new_version()
            return await self.cursor.to_list(*args, **kwargs)

    def find(collection, *args, **kwargs):
        cursor = original(collection, *args, **kwargs)
        if (collection.database.name == context.settings.database and collection.name == 'question_versions'
                and len(args) > 1 and args[1].get('content.title') == 1
                and kwargs.get('session') is not None and not written):
            return DelayedCursor(cursor)
        return cursor

    monkeypatch.setattr(collection_type, 'find', find)
    response = await listing(context, owner)
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert checked_summary(item)['id'] == first['id'] and item['lastVersionNo'] == 1
    newer = await listing(context, owner)
    assert checked_summary(newer.json()['items'][0])['id'] == written[0]['id']


async def test_received_summary_inflight_revoke_keeps_snapshot_but_next_read_is_empty(context, monkeypatch):
    owner, _, recipient, _, first, _ = await parties(context)
    collection_type = type(context.db.question_versions)
    original = collection_type.find
    revoked = []

    class DelayedCursor:
        def __init__(self, cursor): self.cursor = cursor
        async def to_list(self, *args, **kwargs):
            if not revoked:
                response = await share(context, owner, first, active=False, revision=1)
                assert response.status_code == 200
                revoked.append(True)
            return await self.cursor.to_list(*args, **kwargs)

    def find(collection, *args, **kwargs):
        cursor = original(collection, *args, **kwargs)
        if (collection.database.name == context.settings.database and collection.name == 'question_versions'
                and len(args) > 1 and args[1].get('content.title') == 1
                and kwargs.get('session') is not None and not revoked):
            return DelayedCursor(cursor)
        return cursor

    monkeypatch.setattr(collection_type, 'find', find)
    response = await listing(context, recipient, shared=True)
    assert response.status_code == 200, response.text
    assert checked_summary(response.json()['items'][0])['id'] == first['id']
    later = await listing(context, recipient, shared=True)
    assert later.status_code == 200 and later.json()['items'] == []


async def test_summary_opt_in_keeps_paging_and_excludes_unshared_or_other_owned_titles(context):
    owner, _, recipient, _, first, _ = await parties(context)
    second = accepted(await create_question(context, owner, number_content('只属于作者的隐藏题')), 1)
    mine = accepted(await create_question(context, recipient, number_content('接收者自己的题')), 1)
    own = await listing(context, recipient)
    shared = await listing(context, recipient, shared=True, limit=1)
    assert [item['id'] for item in own.json()['items']] == [mine['questionId']]
    assert [item['id'] for item in shared.json()['items']] == [first['questionId']]
    assert second['content']['title'] not in str(shared.json()) and mine['content']['title'] not in str(shared.json())
    denied = await context.client.get('/api/shared-questions', params={'includeCurrentVersion': 'true'})
    assert denied.status_code == 401
    empty = await listing(context, owner, offset=20)
    assert empty.status_code == 200 and empty.json()['items'] == []


async def test_sparse_impact_matches_original_aggregate_without_visiting_unrelated_surveys(context, monkeypatch):
    owner, user, _, _, first, _, a, b, c, d, pubs = await fixture(context)
    template = await context.db.surveys.find_one({'_id': a['id']})
    noise = []
    for i in range(220):
        row = deepcopy(template)
        row.update(_id=str(uuid4()), publicId=uuid4().hex, currentVersionId=None, lastVersionNo=0,
                   draftQuestionSources=[], draftRevision=0)
        noise.append(row)
    await context.db.surveys.insert_many(noise)
    orphan = await context.db.survey_versions.find_one({'_id': pubs[0]['id']})
    orphan.update(_id=str(uuid4()), surveyId='missing-optimized-survey', versionNo=1, publishKey=str(uuid4()))
    await context.db.survey_versions.insert_one(orphan)
    before = await snapshot(context, ALL_COLLECTIONS)
    old_data = await (await context.db.surveys.aggregate(question_impact_pipeline(user['id'], first['questionId']))).to_list(length=1)
    expected = question_impact_view(old_data[0], first['questionId'], None, 'unused',
                                   context.settings.participant_secret, 0, 20)
    collection_type = type(context.db.surveys)
    original = collection_type.aggregate
    batches = []

    async def aggregate(collection, pipeline, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name == 'surveys':
            ids = pipeline[0]['$match']['_id']['$in']
            batches.append(list(ids))
            assert kwargs['session'].in_transaction
        return await original(collection, pipeline, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'aggregate', aggregate)
    actual = result(await impact(context, owner, first), first)
    assert actual['summary'] == expected['summary'] and actual['items'] == expected['items']
    assert len(batches) == 1
    assert set(batches[0]) == {a['id'], b['id'], c['id'], d['id'], 'missing-optimized-survey'}
    for offset in range(5):
        page = result(await impact(context, owner, first, offset=offset, limit=1), first, offset=offset, limit=1)
        assert page['summary'] == expected['summary']
        assert page['items'] == expected['items'][offset:offset+1]
        assert page['nextOffset'] == (offset+1 if offset+1 < len(expected['items']) else None)
    await unchanged(context, before, ALL_COLLECTIONS)


async def test_impact_candidate_batches_preserve_offset_order_and_totals_across_one_hundred(context, monkeypatch):
    owner, _, _, _, first, _ = await parties(context)
    initial = await draft(context, owner, definition(first), [source(first)])
    template = await context.db.surveys.find_one({'_id': initial['id']})
    copies = []
    for i in range(205):
        row = deepcopy(template)
        row.update(_id=str(uuid4()), publicId=uuid4().hex)
        copies.append(row)
    await context.db.surveys.insert_many(copies)
    expected = sorted([initial['id'], *[row['_id'] for row in copies]])
    # Synthetic orphan sorts immediately after the 100th real candidate,
    # testing that a missing survey does not consume an output page position.
    await context.db.survey_versions.insert_one({'_id': str(uuid4()),
        'surveyId': expected[99]+'~orphan', 'versionNo': 1,
        'sourceDraftRevision': template['draftRevision'], 'definition': template['draft'],
        'publishKey': str(uuid4()), 'publishRequestDigest': 'synthetic-orphan-query-test',
        'publishedAt': template['updatedAt'], 'schemaVersion': 1,
        'questionSources': template['draftQuestionSources']})
    before = await snapshot(context, ALL_COLLECTIONS)
    collection_type = type(context.db.surveys)
    original = collection_type.aggregate
    batch_sizes = []

    async def aggregate(collection, pipeline, *args, **kwargs):
        if collection.database.name == context.settings.database and collection.name == 'surveys':
            batch_sizes.append(len(pipeline[0]['$match']['_id']['$in']))
        return await original(collection, pipeline, *args, **kwargs)

    monkeypatch.setattr(collection_type, 'aggregate', aggregate)
    for offset in (0, 98, 99, 100, 199, 200, 205, 206, 210):
        batch_sizes.clear()
        page = result(await impact(context, owner, first, offset=offset, limit=5), first, offset=offset, limit=5)
        assert page['summary'] == {'own': totals(206, 206, 206), 'others': totals()}
        assert [item['surveyId'] for item in page['items']] == expected[offset:offset+5]
        assert page['nextOffset'] == (offset+5 if offset+5 < len(expected) else None)
        assert batch_sizes == [100, 100, 7]
    await unchanged(context, before, ALL_COLLECTIONS)
