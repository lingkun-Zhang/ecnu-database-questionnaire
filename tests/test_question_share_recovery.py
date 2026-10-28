"""Real use-share writes with explicit driver-boundary recovery failures.

No successful storage, commit or recovery query is fabricated. Faults are
injected after the real repository transaction or the existing post-write hook;
this is not a claim of TCP loss or an operating-system/network outage.
"""
import pytest
from pymongo.errors import PyMongoError

from app.database import VALIDATORS
from app.repositories import Repository
from test_question_sharing import granted, parties, share, stored_share
from test_question_sources import snapshot, unchanged
from test_question_versions import rejected


ALL_COLLECTIONS = tuple(VALIDATORS)
UNCHANGED_COLLECTIONS = tuple(name for name in ALL_COLLECTIONS if name != 'question_shares')


async def test_committed_share_is_recovered_without_a_second_transaction(context, monkeypatch):
    owner, author, _, reader, question, _ = await parties(context, grant=False)
    before = await snapshot(context, UNCHANGED_COLLECTIONS)
    original_transaction = Repository.transaction
    transaction_calls = []
    persisted_before_fault = []

    async def commit_then_fail_ack(repository, operation):
        assert repository.db.name == context.settings.database
        await original_transaction(repository, operation)  # Actually committed.
        transaction_calls.append(True)
        row = await stored_share(context, question, reader)
        persisted_before_fault.append(row is not None and row['active'] is True and row['revision'] == 1)
        assert persisted_before_fault[-1]
        raise PyMongoError('test boundary injection after actual use-share commit')

    with monkeypatch.context() as patch:
        patch.setattr(Repository, 'transaction', commit_then_fail_ack)
        recovered = granted(await share(context, owner, question), question, reader, replay=True)
        assert recovered['revision'] == 1 and recovered['active'] is True
        assert transaction_calls == [True] and persisted_before_fault == [True]
        assert await context.db.question_shares.count_documents({}) == 1
        all_after_commit = await snapshot(context, ALL_COLLECTIONS)
        repeated = granted(await share(context, owner, question), question, reader, replay=True)
        assert repeated == recovered
        assert transaction_calls == [True], 'exact replay must recover before opening another transaction'
        await unchanged(context, all_after_commit, ALL_COLLECTIONS)

    await unchanged(context, before, UNCHANGED_COLLECTIONS)
    row = await stored_share(context, question, reader)
    assert row['ownerId'] == author['id'] and row['useSequence'] == 0


@pytest.mark.parametrize('existing_grant', [False, True], ids=['create', 'revoke'])
async def test_post_write_fault_rolls_back_share_and_original_request_can_retry(context, existing_grant):
    owner, _, _, reader, question, _ = await parties(context, grant=existing_grant)
    before = await snapshot(context, ALL_COLLECTIONS)
    protected = await snapshot(context, UNCHANGED_COLLECTIONS)
    active, revision = (False, 1) if existing_grant else (True, 0)
    observed = []

    async def fail_after_real_write(**state):
        document = state['document']
        observed.append((document['active'], document['revision']))
        # The actual insert/update already ran inside the real transaction.
        raise PyMongoError('test post-write injection; transaction must abort')

    context.app.state.hooks['after_question_share_write'] = fail_after_real_write
    try:
        result = await share(context, owner, question, active=active, revision=revision)
        rejected(result, 'QUESTION_SHARE_WRITE_OUTCOME_UNKNOWN', 503)
        assert observed == [(active, revision + 1)]
        await unchanged(context, before, ALL_COLLECTIONS)
        assert await context.db.question_shares.count_documents({}) == int(existing_grant)
    finally:
        context.app.state.hooks.pop('after_question_share_write', None)

    success = granted(await share(context, owner, question, active=active, revision=revision),
                      question, reader, active=active, revision=revision + 1, created=not existing_grant)
    assert success['active'] is active
    after_success = await snapshot(context, ALL_COLLECTIONS)
    replay = granted(await share(context, owner, question, active=active, revision=revision),
                     question, reader, active=active, revision=revision + 1, replay=True)
    assert replay == success
    await unchanged(context, after_success, ALL_COLLECTIONS)
    await unchanged(context, protected, UNCHANGED_COLLECTIONS)


async def test_commit_and_recovery_read_failure_preserve_grant_then_retry_cannot_resurrect_revoke(context, monkeypatch):
    owner, author, _, reader, question, _ = await parties(context, grant=False)
    protected = await snapshot(context, UNCHANGED_COLLECTIONS)
    original_transaction = Repository.transaction
    collection_type = type(context.db.question_shares)
    original_find_one = collection_type.find_one
    recovery_query = {'questionId': question['questionId'], 'recipientId': reader['id'], 'ownerId': author['id']}
    committed = [False]
    transaction_calls, recovery_reads = [], []

    async def commit_then_fail_ack(repository, operation):
        assert repository.db.name == context.settings.database
        await original_transaction(repository, operation)  # Real commit, not a mocked result.
        transaction_calls.append(True)
        row = await original_find_one(context.db.question_shares, recovery_query)
        assert row is not None and row['active'] is True and row['revision'] == 1
        committed[0] = True
        raise PyMongoError('test committed use-share acknowledgement failure')

    async def fail_only_post_commit_recovery(collection, query=None, *args, **kwargs):
        is_recovery = (collection.database.name == context.settings.database
                       and collection.name == 'question_shares'
                       and query == recovery_query and kwargs.get('session') is None)
        if is_recovery:
            recovery_reads.append('after_commit' if committed[0] else 'before_write')
            if committed[0]:
                raise PyMongoError('test only the post-commit use-share recovery read fails')
        return await original_find_one(collection, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, 'transaction', commit_then_fail_ack)
        patch.setattr(collection_type, 'find_one', fail_only_post_commit_recovery)
        result = await share(context, owner, question)
        rejected(result, 'QUESTION_SHARE_WRITE_OUTCOME_UNKNOWN', 503)
        assert transaction_calls == [True]
        assert recovery_reads == ['before_write', 'after_commit']

    persisted = await stored_share(context, question, reader)
    assert persisted['active'] is True and persisted['revision'] == 1
    assert await context.db.question_shares.count_documents({}) == 1
    await unchanged(context, protected, UNCHANGED_COLLECTIONS)

    # A later request with the original body recovers the already committed grant.
    after_unknown = await snapshot(context, ALL_COLLECTIONS)
    granted(await share(context, owner, question), question, reader, replay=True)
    await unchanged(context, after_unknown, ALL_COLLECTIONS)
    revoked = granted(await share(context, owner, question, active=False, revision=1),
                      question, reader, active=False, revision=2)
    assert revoked['id'] == persisted['_id']
    after_revoke = await snapshot(context, ALL_COLLECTIONS)
    rejected(await share(context, owner, question), 'QUESTION_SHARE_CONFLICT')
    await unchanged(context, after_revoke, ALL_COLLECTIONS)
    assert (await stored_share(context, question, reader))['active'] is False
    await unchanged(context, protected, UNCHANGED_COLLECTIONS)
