"""Private banks pin versions without granting question or response access.

The real ASGI/MongoDB fixture owns each random test database. Race tests pause
explicit hooks around actual transaction writes; driver faults are controlled
service-boundary injections, not claims of a real network outage.
"""

import asyncio
from copy import deepcopy
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from pymongo.errors import AutoReconnect, PyMongoError

from app.database import init_database
from app.repositories import Repository
from app.services import ServiceError
from conftest import account, published, start, submit
from scripts.fixtures import example_definition
from test_question_sharing import granted, parties, share, stored_share
from test_question_sources import definition, draft, keys_in, publish, save, snapshot, source, unchanged
from test_question_versions import accepted, create_question, derive, number_content, rejected


OLD_COLLECTIONS = ('users', 'surveys', 'survey_versions', 'responses',
                   'questions', 'question_versions', 'question_shares')
ALL_COLLECTIONS = OLD_COLLECTIONS + ('question_banks', 'question_bank_items')
BANK_FIELDS = {'id', 'name', 'description', 'archived', 'revision', 'createdAt', 'updatedAt'}
ITEM_FIELDS = {'id', 'questionId', 'questionVersionId', 'createdAt', 'updatedAt', 'available'}
PRIVATE_FIELDS = {'ownerId', 'creationKey', 'creationDigest', 'lastRequestDigest',
                  'schemaVersion', 'requestDigest', 'useSequence', 'recipientId',
                  'passwordHash', 'usernameKey', 'identity', 'answers'}


async def create_bank(context, owner, name='常用评分题', *, description='', key=None):
    return await context.client.post('/api/question-banks',
        headers={**owner, 'Idempotency-Key': key or str(uuid4())},
        json={'name': name, 'description': description})


def bank_ok(response, revision=0, *, created=False, replay=False):
    assert response.status_code == (201 if created else 200), response.text
    assert response.headers.get('Idempotent-Replay') == str(replay).lower()
    body = response.json()
    assert set(body) == BANK_FIELDS
    assert body['revision'] == revision
    assert not PRIVATE_FIELDS.intersection(keys_in(body))
    return body


async def metadata(context, owner, bank, *, revision=None, name=None, description=None, archived=None):
    return await context.client.put(f'/api/question-banks/{bank["id"]}', headers=owner,
        json={'name': bank['name'] if name is None else name,
              'description': bank['description'] if description is None else description,
              'archived': bank['archived'] if archived is None else archived,
              'expectedRevision': bank['revision'] if revision is None else revision})


async def put_item(context, owner, bank, version, *, revision=None):
    return await context.client.put(f'/api/question-banks/{bank["id"]}/questions/{version["questionId"]}',
        headers=owner, json={'questionVersionId': version['id'],
                            'expectedRevision': bank['revision'] if revision is None else revision})


async def remove_item(context, owner, bank, question, *, revision=None):
    return await context.client.delete(f'/api/question-banks/{bank["id"]}/questions/{question["questionId"]}',
        headers=owner, params={'expectedRevision': bank['revision'] if revision is None else revision})


async def items(context, owner, bank, **params):
    response = await context.client.get(f'/api/question-banks/{bank["id"]}/questions', headers=owner, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {'bank', 'items', 'limit', 'nextAfterId'}
    assert set(body['bank']) == BANK_FIELDS
    assert not PRIVATE_FIELDS.intersection(keys_in(body))
    for item in body['items']:
        assert set(item) == ITEM_FIELDS | ({'version', 'currentVersionId'} if item['available'] else set())
    return body


async def seeded(context):
    owner, user = await account(context)
    first = accepted(await create_question(context, owner, number_content('旧版评分', min=0, max=7)), 1)
    bank = bank_ok(await create_bank(context, owner), created=True)
    return owner, user, first, bank


async def all_unchanged(context, before):
    await unchanged(context, before, ALL_COLLECTIONS)


async def test_create_private_bank_has_explicit_empty_revision_and_white_list(context):
    owner, user = await account(context)
    bank = bank_ok(await create_bank(context, owner, '  常用题  ', description='个人整理'), created=True)
    assert bank['name'] == '常用题' and bank['description'] == '个人整理'
    assert bank['archived'] is False
    stored = await context.db.question_banks.find_one({'_id': bank['id']})
    assert stored['ownerId'] == user['id'] and stored['revision'] == 0
    assert isinstance(stored['creationDigest'], str) and len(stored['creationDigest']) == 64
    assert 'lastRequestDigest' not in stored
    assert stored['schemaVersion'] == 1
    page = await items(context, owner, bank)
    assert page['items'] == [] and page['nextAfterId'] is None and page['bank'] == bank


async def test_creation_key_keeps_one_identity_and_returns_current_bank_after_rename(context):
    owner, _ = await account(context)
    key = str(uuid4())
    first = bank_ok(await create_bank(context, owner, key=key), created=True)
    current = bank_ok(await metadata(context, owner, first, name='已重命名'), 1)
    before = await snapshot(context, ALL_COLLECTIONS)
    assert bank_ok(await create_bank(context, owner, key=key), 1, replay=True) == current
    rejected(await create_bank(context, owner, '不同初始内容', key=key), 'IDEMPOTENCY_CONFLICT')
    assert await context.db.question_banks.count_documents({}) == 1
    await all_unchanged(context, before)


async def test_bank_keyset_pages_filter_owner_and_archive_without_duplicates(context):
    owner, _ = await account(context)
    outsider, _ = await account(context, 'bank_outsider')
    banks = [bank_ok(await create_bank(context, owner, f'题库{index}'), created=True) for index in range(3)]
    hidden = bank_ok(await create_bank(context, outsider, '他人的题库'), created=True)
    archived = bank_ok(await metadata(context, owner, banks[1], archived=True), 1)
    seen, after = [], None
    for _ in range(2):
        response = await context.client.get('/api/question-banks', headers=owner,
            params={'limit': 1, **({'afterId': after} if after else {})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page['limit'] == 1 and len(page['items']) == 1
        assert set(page['items'][0]) == BANK_FIELDS
        seen.append(page['items'][0]['id'])
        after = page['nextAfterId']
    assert after is None and seen == sorted(seen)
    assert set(seen) == {banks[0]['id'], banks[2]['id']}
    response = await context.client.get('/api/question-banks', headers=owner, params={'archived': True})
    assert response.status_code == 200 and response.json()['items'] == [archived]
    assert hidden['id'] not in str(seen)
    detail = await context.client.get(f'/api/question-banks/{banks[0]["id"]}', headers=owner)
    assert detail.status_code == 200 and detail.json() == banks[0]


async def test_bank_routes_require_login_and_hide_foreign_bank_before_question_checks(context):
    owner, _, first, bank = await seeded(context)
    foreign, _ = await account(context, 'bank_foreign')
    paths = [('/api/question-banks', 'GET', None),
             ('/api/question-banks', 'POST', {'name': '题库'}),
             (f'/api/question-banks/{bank["id"]}', 'GET', None),
             (f'/api/question-banks/{bank["id"]}', 'PUT', {'name': '新名', 'description': '', 'archived': False, 'expectedRevision': 0}),
             (f'/api/question-banks/{bank["id"]}/questions', 'GET', None),
             (f'/api/question-banks/{bank["id"]}/questions/{first["questionId"]}', 'PUT', {'questionVersionId': first['id'], 'expectedRevision': 0}),
             (f'/api/question-banks/{bank["id"]}/questions/{first["questionId"]}?expectedRevision=0', 'DELETE', None)]
    before = await snapshot(context, ALL_COLLECTIONS)
    for path, method, body in paths:
        response = await context.client.request(method, path, json=body, headers={'Idempotency-Key': str(uuid4())})
        rejected(response, 'UNAUTHENTICATED', 401)
        if '/question-banks/' in path:
            rejected(await context.client.request(method, path, json=body, headers=foreign), 'NOT_FOUND', 404)
    rejected(await context.client.get('/api/question-banks/' + str(uuid4()), headers=owner), 'NOT_FOUND', 404)
    await all_unchanged(context, before)


async def test_bank_inputs_are_strict_and_invalid_requests_never_advance_revision(context):
    owner, _, first, bank = await seeded(context)
    before = await snapshot(context, ALL_COLLECTIONS)
    for body in ({'name': ''}, {'name': '   '}, {'name': 'x' * 121}, {'name': 3},
                 {'name': '题库', 'description': 'x' * 2001}, {'name': '题库', 'ownerId': 'forged'}):
        rejected(await context.client.post('/api/question-banks', headers={**owner, 'Idempotency-Key': str(uuid4())}, json=body), 'INVALID_REQUEST', 422)
    rejected(await context.client.post('/api/question-banks', headers=owner, json={'name': '题库'}), 'INVALID_REQUEST', 422)
    valid = {'name': '题库', 'description': '', 'archived': False, 'expectedRevision': 0}
    for body in ({**valid, 'expectedRevision': True}, {**valid, 'expectedRevision': -1},
                 {**valid, 'archived': 'false'}, {**valid, 'name': ' '},
                 {key: value for key, value in valid.items() if key != 'archived'}):
        rejected(await context.client.put(f'/api/question-banks/{bank["id"]}', headers=owner, json=body), 'INVALID_REQUEST', 422)
    for params in ({'limit': 0}, {'limit': 101}, {'afterId': ''}, {'afterId': 'x' * 161}):
        for path in ('/api/question-banks', f'/api/question-banks/{bank["id"]}/questions'):
            rejected(await context.client.get(path, headers=owner, params=params), 'INVALID_REQUEST', 422)
    for body in ({'questionVersionId': first['id'], 'expectedRevision': True},
                 {'questionVersionId': '', 'expectedRevision': 0},
                 {'questionVersionId': first['id'], 'expectedRevision': 0, 'content': {}}):
        rejected(await context.client.put(f'/api/question-banks/{bank["id"]}/questions/{first["questionId"]}', headers=owner, json=body), 'INVALID_REQUEST', 422)
    await all_unchanged(context, before)


async def test_member_pages_return_each_pinned_item_once_even_when_unavailable(context):
    owner, _, receiver, reader, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, receiver), created=True)
    versions = [first]
    for index in range(2):
        versions.append(accepted(await create_question(context, receiver, number_content(f'本人的题{index}')), 1))
    for index, version in enumerate(versions):
        bank = bank_ok(await put_item(context, receiver, bank, version), index + 1)
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    seen, after = [], None
    for _ in versions:
        page = await items(context, receiver, bank, limit=1, **({'afterId': after} if after else {}))
        assert len(page['items']) == 1
        seen += page['items']
        after = page['nextAfterId']
    assert after is None and [row['id'] for row in seen] == sorted(row['id'] for row in seen)
    assert {row['questionId'] for row in seen} == {row['questionId'] for row in versions}
    hidden = next(row for row in seen if row['questionId'] == first['questionId'])
    assert hidden['available'] is False and set(hidden) == ITEM_FIELDS


async def test_one_series_per_bank_and_the_same_series_in_two_banks_pin_independently(context):
    owner, _, first, a = await seeded(context)
    b = bank_ok(await create_bank(context, owner, '另一题库'), created=True)
    a = bank_ok(await put_item(context, owner, a, first), 1)
    b = bank_ok(await put_item(context, owner, b, first), 1)
    second = accepted(await derive(context, owner, first, number_content('新题', min=1, max=5)), 2)
    for bank in (a, b):
        row = (await items(context, owner, bank))['items'][0]
        assert row['questionVersionId'] == first['id'] and row['version'] == first
        assert row['currentVersionId'] == second['id']
    a = bank_ok(await put_item(context, owner, a, second), 2)
    assert (await items(context, owner, a))['items'][0]['version'] == second
    assert (await items(context, owner, b))['items'][0]['version'] == first
    assert await context.db.question_bank_items.count_documents({'bankId': a['id']}) == 1
    assert await context.db.question_bank_items.count_documents({}) == 2


async def test_bank_explicit_version_update_and_removal_do_not_change_surveys_or_old_answers(context):
    owner, _, first, bank = await seeded(context)
    bank = bank_ok(await put_item(context, owner, bank, first), 1)
    selected = (await items(context, owner, bank))['items'][0]
    selected_source = {'instanceId': 'score', 'questionId': selected['questionId'],
                       'questionVersionId': selected['questionVersionId']}
    survey = await draft(context, owner, definition(selected['version']), [selected_source], multiple=True)
    assert survey['draftQuestionSources'] == [source(first)]
    assert (await publish(context, owner, survey)).status_code == 201
    pending = await start(context, owner, survey)
    assert (await submit(context, owner, survey, pending, {'score': 0})).status_code == 201
    second = accepted(await derive(context, owner, first, number_content('新约束', min=1, max=5)), 2)
    before = await snapshot(context, OLD_COLLECTIONS)
    bank = bank_ok(await put_item(context, owner, bank, second), 2)
    bank = bank_ok(await remove_item(context, owner, bank, first), 3)
    await unchanged(context, before, OLD_COLLECTIONS)
    assert (await items(context, owner, bank))['items'] == []
    # Existing credentials and snapshots still interpret zero against V1.
    answer = await submit(context, owner, survey, pending, {'score': 0})
    assert answer.status_code == 201, answer.text
    assert answer.json()['versionId'] == pending['versionId']


async def test_invalid_question_version_and_private_question_leave_bank_unchanged(context):
    owner, _, first, bank = await seeded(context)
    foreign, _ = await account(context, 'bank_question_author')
    private = accepted(await create_question(context, foreign), 1)
    other = accepted(await create_question(context, owner, number_content('另一个题')), 1)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await put_item(context, owner, bank, private), 'NOT_FOUND', 404)
    rejected(await put_item(context, owner, bank, {**first, 'id': other['id']}), 'QUESTION_VERSION_NOT_FOUND', 404)
    rejected(await put_item(context, owner, bank, {**first, 'id': str(uuid4())}), 'QUESTION_VERSION_NOT_FOUND', 404)
    rejected(await remove_item(context, owner, bank, first), 'BANK_ITEM_NOT_FOUND', 404)
    await all_unchanged(context, before)


async def test_all_bank_mutations_use_one_revision_and_same_pin_with_new_revision_is_new_decision(context):
    owner, _, first, initial = await seeded(context)
    current = bank_ok(await put_item(context, owner, initial, first), 1)
    current = bank_ok(await put_item(context, owner, current, first), 2)
    current = bank_ok(await metadata(context, owner, current, name='改名'), 3)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await put_item(context, owner, initial, first), 'BANK_CONFLICT')
    rejected(await metadata(context, owner, initial, name='过期改名'), 'BANK_CONFLICT')
    rejected(await remove_item(context, owner, initial, first), 'BANK_CONFLICT')
    await all_unchanged(context, before)
    assert await context.db.question_bank_items.count_documents({}) == 1


async def test_latest_member_request_replays_without_rewriting_bank_or_member(context):
    owner, _, first, original = await seeded(context)
    current = bank_ok(await put_item(context, owner, original, first), 1)
    before = await snapshot(context, ALL_COLLECTIONS)
    assert bank_ok(await put_item(context, owner, original, first), 1, replay=True) == current
    await all_unchanged(context, before)


async def test_old_version_request_cannot_overwrite_a_later_bank_pin(context):
    owner, _, first, original = await seeded(context)
    current = bank_ok(await put_item(context, owner, original, first), 1)
    second = accepted(await derive(context, owner, first), 2)
    current = bank_ok(await put_item(context, owner, current, second), 2)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await put_item(context, owner, original, first), 'BANK_CONFLICT')
    await all_unchanged(context, before)
    assert (await items(context, owner, current))['items'][0]['questionVersionId'] == second['id']


async def test_remove_replay_is_limited_and_old_remove_cannot_erase_readded_member(context):
    owner, _, first, initial = await seeded(context)
    with_item = bank_ok(await put_item(context, owner, initial, first), 1)
    removed = bank_ok(await remove_item(context, owner, with_item, first), 2)
    before = await snapshot(context, ALL_COLLECTIONS)
    assert bank_ok(await remove_item(context, owner, with_item, first), 2, replay=True) == removed
    await all_unchanged(context, before)
    readded = bank_ok(await put_item(context, owner, removed, first), 3)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await remove_item(context, owner, with_item, first), 'BANK_CONFLICT')
    await all_unchanged(context, before)
    assert len((await items(context, owner, readded))['items']) == 1


async def test_archival_preserves_members_and_replay_but_blocks_new_member_changes(context):
    owner, _, first, bank = await seeded(context)
    bank = bank_ok(await put_item(context, owner, bank, first), 1)
    archived = bank_ok(await metadata(context, owner, bank, archived=True), 2)
    before = await snapshot(context, ALL_COLLECTIONS)
    assert bank_ok(await metadata(context, owner, bank, archived=True), 2, replay=True) == archived
    rejected(await put_item(context, owner, archived, first), 'BANK_ARCHIVED')
    rejected(await remove_item(context, owner, archived, first), 'BANK_ARCHIVED')
    assert (await items(context, owner, archived))['items'][0]['version'] == first
    await all_unchanged(context, before)
    restored = bank_ok(await metadata(context, owner, archived, archived=False, name='重新启用'), 3)
    restored = bank_ok(await remove_item(context, owner, restored, first), 4)
    assert (await items(context, owner, restored))['items'] == []


async def test_active_shared_question_can_be_pinned_without_changing_ownership_or_grant_revision(context):
    _, author, recipient, reader, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, recipient), created=True)
    before = await stored_share(context, first, reader)
    bank = bank_ok(await put_item(context, recipient, bank, first), 1)
    row = (await items(context, recipient, bank))['items'][0]
    assert row['available'] is True and row['version'] == first
    grant = await stored_share(context, first, reader)
    assert grant['useSequence'] == before['useSequence'] + 1 and grant['revision'] == before['revision']
    expected = deepcopy(before)
    expected['useSequence'] += 1
    assert grant == expected
    assert (await context.db.questions.find_one({'_id': first['questionId']}))['ownerId'] == author['id']


async def test_revoked_member_becomes_unavailable_and_cannot_authorize_new_pin_or_survey(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    original = bank_ok(await create_bank(context, recipient), created=True)
    bank = bank_ok(await put_item(context, recipient, original, first), 1)
    second = accepted(await derive(context, owner, first), 2)
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    empty = await context.client.post('/api/surveys', headers=recipient, json={'title': '撤销后的新问卷'})
    assert empty.status_code == 201
    before = await snapshot(context, ALL_COLLECTIONS)
    row = (await items(context, recipient, bank))['items'][0]
    assert row['available'] is False and set(row) == ITEM_FIELDS
    assert first['content']['title'] not in str(row)
    rejected(await put_item(context, recipient, bank, first), 'NOT_FOUND', 404)
    rejected(await put_item(context, recipient, bank, second), 'NOT_FOUND', 404)
    rejected(await save(context, recipient, empty.json(), definition(first), [source(first)]), 'NOT_FOUND', 404)
    # Acknowledging the latest committed original decision returns metadata,
    # not content or renewed permission; it must not touch the revoked grant.
    assert bank_ok(await put_item(context, recipient, original, first), 1, replay=True) == bank
    await all_unchanged(context, before)
    bank_ok(await remove_item(context, recipient, bank, first), 2)


async def test_regrant_restores_member_readability_without_updating_its_fixed_version(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, recipient), created=True)
    bank = bank_ok(await put_item(context, recipient, bank, first), 1)
    second = accepted(await derive(context, owner, first), 2)
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    assert (await items(context, recipient, bank))['items'][0]['available'] is False
    granted(await share(context, owner, first, revision=2), first, reader, revision=3)
    before = await snapshot(context, ALL_COLLECTIONS)
    row = (await items(context, recipient, bank))['items'][0]
    assert row['available'] is True and row['version'] == first and row['currentVersionId'] == second['id']
    await all_unchanged(context, before)


async def test_bank_ownership_does_not_leak_readable_member_content_to_another_user(context):
    owner, _, recipient, _, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, recipient), created=True)
    bank = bank_ok(await put_item(context, recipient, bank, first), 1)
    before = await snapshot(context, ALL_COLLECTIONS)
    # The question author can read the question, but not somebody else's bank.
    response = await context.client.get(f'/api/question-banks/{bank["id"]}/questions', headers=owner)
    rejected(response, 'NOT_FOUND', 404)
    assert first['content']['title'] not in response.text and bank['name'] not in response.text
    await all_unchanged(context, before)


async def test_failure_after_member_write_rolls_back_member_and_bank_revision(context):
    owner, _, first, bank = await seeded(context)
    before = await snapshot(context, ALL_COLLECTIONS)
    entered = []

    async def fail(**state):
        entered.append(True)
        raise ServiceError('BANK_TEST_FAILURE', 'synthetic failure after member write', 503)

    context.app.state.bank_hooks['after_bank_item_write'] = fail
    try:
        rejected(await put_item(context, owner, bank, first), 'BANK_TEST_FAILURE', 503)
    finally:
        context.app.state.bank_hooks.pop('after_bank_item_write', None)
    assert entered == [True]
    await all_unchanged(context, before)
    bank_ok(await put_item(context, owner, bank, first), 1)


async def test_failure_after_metadata_write_rolls_back_the_entire_owner_decision(context):
    owner, _, _, bank = await seeded(context)
    before = await snapshot(context, ALL_COLLECTIONS)

    async def fail(**state):
        raise ServiceError('BANK_TEST_FAILURE', 'synthetic failure after metadata write', 503)

    context.app.state.bank_hooks['after_bank_metadata_write'] = fail
    try:
        rejected(await metadata(context, owner, bank, name='不能留下的新名', archived=True), 'BANK_TEST_FAILURE', 503)
    finally:
        context.app.state.bank_hooks.pop('after_bank_metadata_write', None)
    await all_unchanged(context, before)


async def test_failure_after_shared_use_touch_rolls_back_sequence_and_bank_item(context):
    _, _, recipient, reader, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, recipient), created=True)
    before = await snapshot(context, ALL_COLLECTIONS)
    entered = []

    async def fail(**state):
        entered.append(True)
        raise ServiceError('BANK_TEST_FAILURE', 'synthetic failure after authorization write', 503)

    context.app.state.bank_hooks['after_bank_grant_touch'] = fail
    try:
        rejected(await put_item(context, recipient, bank, first), 'BANK_TEST_FAILURE', 503)
    finally:
        context.app.state.bank_hooks.pop('after_bank_grant_touch', None)
    assert entered == [True]
    await all_unchanged(context, before)
    assert (await stored_share(context, first, reader))['useSequence'] == 0


async def test_competing_member_and_metadata_decisions_with_one_revision_have_one_winner(context):
    owner, _, first, bank = await seeded(context)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def pause(**state):
        entered.set()
        await release.wait()

    context.app.state.bank_hooks['after_bank_item_write'] = pause
    context.app.state.bank_hooks['after_bank_metadata_write'] = pause
    try:
        tasks = [asyncio.create_task(put_item(context, owner, bank, first)),
                 asyncio.create_task(metadata(context, owner, bank, name='并发改名'))]
        await asyncio.wait_for(entered.wait(), 5)
        assert all(not task.done() for task in tasks)
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.bank_hooks.pop('after_bank_item_write', None)
        context.app.state.bank_hooks.pop('after_bank_metadata_write', None)
    assert sorted(row.status_code for row in responses) == [200, 409]
    rejected(next(row for row in responses if row.status_code == 409), 'BANK_CONFLICT')
    current = next(row.json() for row in responses if row.status_code == 200)
    assert current['revision'] == 1
    member_won = responses[0].status_code == 200
    assert await context.db.question_bank_items.count_documents({}) == int(member_won)
    assert current['name'] == (bank['name'] if member_won else '并发改名')


async def test_concurrent_identical_creation_keeps_one_bank_and_both_callers_acknowledge_it(context):
    owner, _ = await account(context)
    key = str(uuid4())
    responses = await asyncio.gather(create_bank(context, owner, key=key), create_bank(context, owner, key=key))
    assert sorted(row.status_code for row in responses) == [200, 201]
    assert responses[0].json() == responses[1].json()
    assert await context.db.question_banks.count_documents({}) == 1
    assert sorted(row.headers.get('Idempotent-Replay') for row in responses) == ['false', 'true']


async def test_revocation_before_authorization_write_forces_snapshot_retry_and_rejects_member(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, recipient), created=True)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None

    async def pause(**state):
        entered.set()
        await release.wait()

    context.app.state.bank_hooks['before_bank_grant_touch'] = pause
    try:
        task = asyncio.create_task(put_item(context, recipient, bank, first))
        await asyncio.wait_for(entered.wait(), 5)
        granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
        release.set()
        response = await asyncio.wait_for(task, 15)
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        context.app.state.bank_hooks.pop('before_bank_grant_touch', None)
    rejected(response, 'NOT_FOUND', 404)
    assert await context.db.question_bank_items.count_documents({}) == 0
    assert (await context.db.question_banks.find_one({'_id': bank['id']}))['revision'] == 0
    assert (await stored_share(context, first, reader))['useSequence'] == 0


async def test_member_commit_before_revocation_keeps_only_an_unavailable_pointer(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    bank = bank_ok(await create_bank(context, recipient), created=True)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def pause(**state):
        entered.set()
        await release.wait()

    context.app.state.bank_hooks['after_bank_grant_touch'] = pause
    try:
        tasks.append(asyncio.create_task(put_item(context, recipient, bank, first)))
        await asyncio.wait_for(entered.wait(), 5)
        tasks.append(asyncio.create_task(share(context, owner, first, active=False, revision=1)))
        await asyncio.sleep(0)
        assert all(not task.done() for task in tasks)
        release.set()
        added, revoked = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.bank_hooks.pop('after_bank_grant_touch', None)
    bank = bank_ok(added, 1)
    granted(revoked, first, reader, active=False, revision=2)
    row = (await items(context, recipient, bank))['items'][0]
    assert row['available'] is False and set(row) == ITEM_FIELDS
    assert (await stored_share(context, first, reader))['useSequence'] == 1
    rejected(await put_item(context, recipient, bank, first), 'NOT_FOUND', 404)


async def test_lost_service_return_after_real_commit_recovers_original_member_decision(context, monkeypatch):
    owner, _, first, bank = await seeded(context)
    original = Repository.transaction
    committed = []

    async def lose_after_commit(repo, callback):
        result = await original(repo, callback)
        if repo.db.name == context.db.name:
            committed.append((jsonable_encoder(result), await snapshot(context, ALL_COLLECTIONS)))
            raise AutoReconnect('synthetic lost service return after acknowledged bank commit')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Repository, 'transaction', lose_after_commit)
        response = await put_item(context, owner, bank, first)
    result = bank_ok(response, 1, replay=True)
    assert len(committed) == 1
    # The internal transaction may also return a replay marker; the persisted
    # state is the independent truth and must not be written a second time.
    await all_unchanged(context, committed[0][1])
    assert bank_ok(await put_item(context, owner, bank, first), 1, replay=True) == result
    await all_unchanged(context, committed[0][1])


async def test_unknown_write_and_failed_recovery_return_retryable_503_without_mutation(context, monkeypatch):
    owner, _, first, bank = await seeded(context)
    before = await snapshot(context, ALL_COLLECTIONS)
    original_transaction = Repository.transaction
    collection_type = type(context.db.question_banks)
    original_find = collection_type.find_one
    phases = []

    async def fail_transaction(repo, callback):
        if repo.db.name == context.db.name:
            phases.append('transaction-error')
            raise AutoReconnect('synthetic failure before transaction write')
        return await original_transaction(repo, callback)

    async def fail_recovery(collection, criteria=None, *args, **kwargs):
        if (collection.database.name == context.db.name and collection.name == 'question_banks'
                and phases == ['transaction-error']):
            phases.append('recovery-error')
            raise AutoReconnect('synthetic recovery lookup failure')
        return await original_find(collection, criteria, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, 'transaction', fail_transaction)
        patch.setattr(collection_type, 'find_one', fail_recovery)
        rejected(await put_item(context, owner, bank, first), 'BANK_WRITE_OUTCOME_UNKNOWN', 503)
    assert phases == ['transaction-error', 'recovery-error']
    await all_unchanged(context, before)
    bank_ok(await put_item(context, owner, bank, first), 1)


async def test_bank_schema_and_unique_membership_reject_invalid_shapes_without_writes(context):
    owner, user, first, bank = await seeded(context)
    bank = bank_ok(await put_item(context, owner, bank, first), 1)
    stored_bank = await context.db.question_banks.find_one({'_id': bank['id']})
    member = await context.db.question_bank_items.find_one({'bankId': bank['id']})
    before = await snapshot(context, ALL_COLLECTIONS)
    for collection, document in [('question_banks', {**stored_bank, '_id': str(uuid4())}),
                                  ('question_bank_items', {**member, '_id': str(uuid4())})]:
        code = None
        try:
            await context.db[collection].insert_one(document)
        except PyMongoError as error:
            code = getattr(error, 'code', None)
        assert code == 11000
    for collection, document, changes in [
        ('question_banks', stored_bank, {'revision': True}),
        ('question_banks', stored_bank, {'revision': -1}),
        ('question_banks', stored_bank, {'archived': 'false'}),
        ('question_banks', stored_bank, {'name': ''}),
        ('question_banks', stored_bank, {'name': 'x' * 121}),
        ('question_banks', stored_bank, {'description': 'x' * 2001}),
        ('question_banks', stored_bank, {'lastRequestDigest': 'invalid'}),
        ('question_bank_items', member, {'questionVersionId': ''}),
        ('question_bank_items', member, {'unexpected': 'field'}),
    ]:
        code = None
        try:
            await context.db[collection].update_one({'_id': document['_id']}, {'$set': changes})
        except PyMongoError as error:
            code = getattr(error, 'code', None)
        assert code == 121, f'Expected schema rejection, received {code}'
    assert stored_bank['ownerId'] == user['id']
    await all_unchanged(context, before)


async def test_new_collections_and_repeated_init_preserve_all_old_seven_collections_and_validators(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey, _ = await published(context, owner, example_definition(), multiple=True)
    attempt = await start(context, owner, survey)
    assert (await submit(context, owner, survey, attempt, {'q_age': 17, 'q_hobby': ['read'], 'q_score': 0})).status_code == 201
    accepted(await derive(context, owner, first), 2)
    before = await snapshot(context, OLD_COLLECTIONS)
    validators = {name: (await context.db[name].options())['validator'] for name in OLD_COLLECTIONS}
    assert await context.db.question_banks.count_documents({}) == 0
    assert await context.db.question_bank_items.count_documents({}) == 0
    # Drop only the empty newly added collections in this fixture-owned DB to
    # exercise upgrade from the immediately preceding seven-collection layout.
    await context.db.question_bank_items.drop()
    await context.db.question_banks.drop()
    await init_database(context.db)
    assert {'question_banks', 'question_bank_items'} <= set(await context.db.list_collection_names())
    await unchanged(context, before, OLD_COLLECTIONS)
    assert {name: (await context.db[name].options())['validator'] for name in OLD_COLLECTIONS} == validators
    bank = bank_ok(await create_bank(context, owner), created=True)
    bank = bank_ok(await put_item(context, owner, bank, first), 1)
    await unchanged(context, before, OLD_COLLECTIONS)
    with_bank = await snapshot(context, ALL_COLLECTIONS)
    for _ in range(2):
        await init_database(context.db)
        await all_unchanged(context, with_bank)
        assert {name: (await context.db[name].options())['validator'] for name in OLD_COLLECTIONS} == validators
