"""Restore immutable question content through the real ASGI/MongoDB boundary.

Fault cases are labelled service hooks or driver-boundary faults around real
transactions. They do not simulate an actual network outage. All documents are
synthetic and the existing context fixture owns its random database cleanup.
"""

import asyncio
from copy import deepcopy
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from pymongo.errors import AutoReconnect, OperationFailure, PyMongoError
import pytest

from app.database import init_database
from app.repositories import Repository
from app.services import ServiceError
from conftest import account, start, submit
from test_question_sources import definition, draft, publish, snapshot, source, unchanged
from test_question_versions import VALID_CONTENTS, accepted, create_question, derive, number_content, rejected


async def restore(context, owner, target, *, expected=None, key=None):
    return await context.client.post(f'/api/questions/{target["questionId"]}/restore',
        headers={**owner, "Idempotency-Key": str(uuid4()) if key is None else key},
        json={"targetVersionId": target["id"],
              "expectedCurrentVersionId": target["id"] if expected is None else expected})


def restored(response, number, target, previous, replay=False):
    assert response.status_code == (200 if replay else 201), response.text
    assert response.headers.get("Idempotent-Replay") == str(replay).lower()
    body = response.json()
    assert set(body) == {"id", "questionId", "versionNo", "parentVersionId", "content", "createdAt",
                         "operation", "previousCurrentVersionId"}
    assert body["questionId"] == target["questionId"]
    assert body["versionNo"] == number and body["content"] == target["content"]
    assert body["parentVersionId"] == target["id"]
    assert body["operation"] == "restore" and body["previousCurrentVersionId"] == previous
    assert body["id"] not in {target["id"], previous}
    return body


async def test_restoring_old_content_creates_v4_without_rewriting_history_or_survey_references(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("第一版", min=0, max=10)), 1)
    second = accepted(await derive(context, owner, first, number_content("第二版", min=1, max=10)), 2)
    third = accepted(await derive(context, owner, second, number_content("第三版", min=2, max=10)), 3)
    a = await draft(context, owner, definition(first, "a_score", "保留第一版的问卷"),
                    [source(first, "a_score")], multiple=True)
    b = await draft(context, owner, definition(third, "b_score", "保留第三版的问卷"),
                    [source(third, "b_score")])
    for survey in (a, b):
        result = await publish(context, owner, survey)
        assert result.status_code == 201, result.text
    pending = await start(context, owner, a)
    first_answer = await submit(context, owner, a, pending, {"a_score": 0})
    assert first_answer.status_code == 201, first_answer.text
    old_business = await snapshot(context, ("users", "surveys", "survey_versions", "responses"))
    old_versions = await context.db.question_versions.find({}).sort("_id", 1).to_list(length=None)
    usages = {}
    for scope in ("draft", "published"):
        result = await context.client.get(f'/api/questions/{first["questionId"]}/usages',
                                          headers=owner, params={"scope": scope})
        assert result.status_code == 200, result.text
        usages[scope] = result.json()

    fourth = restored(await restore(context, owner, first, expected=third["id"]), 4, first, third["id"])
    await unchanged(context, old_business, ("users", "surveys", "survey_versions", "responses"))
    history_equal = await context.db.question_versions.find({"_id": {"$in": [item["_id"] for item in old_versions]}}).sort("_id", 1).to_list(length=None) == old_versions
    assert history_equal, "Restoration rewrote a historical question version"
    root = await context.db.questions.find_one({"_id": first["questionId"]})
    assert root["currentVersionId"] == fourth["id"] and root["lastVersionNo"] == 4
    for scope, expected in usages.items():
        result = await context.client.get(f'/api/questions/{first["questionId"]}/usages',
                                          headers=owner, params={"scope": scope})
        assert result.status_code == 200 and result.json() == expected
        empty = await context.client.get(f'/api/questions/{first["questionId"]}/usages', headers=owner,
                                         params={"scope": scope, "questionVersionId": fourth["id"]})
        assert empty.status_code == 200 and empty.json()["items"] == []
    history = await context.client.get(f'/api/questions/{first["questionId"]}/versions', headers=owner)
    assert history.status_code == 200 and history.json()["items"] == [fourth, third, second, first]
    read = await context.client.get(f'/api/questions/{first["questionId"]}/versions/{fourth["id"]}', headers=owner)
    assert read.status_code == 200 and read.json() == fourth
    # A fill credential obtained before restoring the bank question still uses
    # the original questionnaire snapshot and accepts its original zero value.
    late_answer = await submit(context, owner, a, pending, {"a_score": 0})
    assert late_answer.status_code == 201 and late_answer.json()["versionId"] == pending["versionId"]
    explicit = await draft(context, owner, definition(fourth, "restored_score"), [source(fourth, "restored_score")])
    publication = await publish(context, owner, explicit)
    assert publication.status_code == 201, publication.text
    attempt = await start(context, owner, explicit)
    valid = await submit(context, owner, explicit, attempt, {"restored_score": 0})
    assert valid.status_code == 201, valid.text


@pytest.mark.parametrize("content", VALID_CONTENTS, ids=["single", "multi", "text", "number"])
async def test_restore_copies_server_owned_content_for_all_four_kinds(context, content):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, content), 1)
    changed = {**deepcopy(content), "title": "后来编辑的题干"}
    second = accepted(await derive(context, owner, first, changed), 2)
    third = restored(await restore(context, owner, first, expected=second["id"]), 3, first, second["id"])
    stored = await context.db.question_versions.find_one({"_id": third["id"]})
    assert stored["content"] == content and stored["operation"] == "restore"
    assert stored["previousCurrentVersionId"] == second["id"]


async def test_restoring_current_or_identical_content_still_records_a_new_immutable_operation(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = restored(await restore(context, owner, first), 2, first, first["id"])
    second_document = await context.db.question_versions.find_one({"_id": second["id"]})
    third = restored(await restore(context, owner, first, expected=second["id"]), 3, first, second["id"])
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert await context.db.question_versions.find_one({"_id": second["id"]}) == second_document


async def test_restore_enforces_owner_and_target_membership_and_failed_key_can_retry(context):
    owner, _ = await account(context)
    other, _ = await account(context, "restore_other")
    first = accepted(await create_question(context, owner), 1)
    same_owner_other = accepted(await create_question(context, owner), 1)
    foreign = accepted(await create_question(context, other), 1)
    before = await snapshot(context)
    rejected(await restore(context, {}, first), "UNAUTHENTICATED", 401)
    rejected(await restore(context, {"Authorization": "Bearer invalid"}, first), "UNAUTHENTICATED", 401)
    rejected(await restore(context, other, first), "NOT_FOUND", 404)
    rejected(await restore(context, owner, foreign), "NOT_FOUND", 404)
    rejected(await restore(context, owner, {**first, "questionId": str(uuid4())}), "NOT_FOUND", 404)
    key = str(uuid4())
    for wrong in (str(uuid4()), same_owner_other["id"], foreign["id"]):
        rejected(await restore(context, owner, {**first, "id": wrong}, expected=first["id"], key=key),
                 "QUESTION_VERSION_NOT_FOUND", 404)
        await unchanged(context, before)
    rejected(await restore(context, owner, first, expected=str(uuid4()), key=key), "QUESTION_VERSION_CONFLICT")
    await unchanged(context, before)
    restored(await restore(context, owner, first, key=key), 2, first, first["id"])


async def test_restore_request_cannot_supply_content_or_forge_restore_metadata(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    base = {"targetVersionId": first["id"], "expectedCurrentVersionId": first["id"]}
    invalid = [
        {**base, "content": number_content("客户端伪造")},
        {**base, "operation": "restore"},
        {**base, "previousCurrentVersionId": first["id"]},
        {"expectedCurrentVersionId": first["id"]},
        {**base, "targetVersionId": None}, {**base, "targetVersionId": 123},
        {**base, "targetVersionId": ""}, {**base, "targetVersionId": "x" * 161},
        {**base, "expectedCurrentVersionId": None}, {**base, "expectedCurrentVersionId": "x" * 161},
    ]
    before = await snapshot(context)
    path = f'/api/questions/{first["questionId"]}/restore'
    for body in invalid:
        response = await context.client.post(path, headers={**owner, "Idempotency-Key": str(uuid4())}, json=body)
        rejected(response, "INVALID_REQUEST", 422)
        await unchanged(context, before)
    for headers in (owner, {**owner, "Idempotency-Key": ""}, {**owner, "Idempotency-Key": "x" * 129}):
        rejected(await context.client.post(path, headers=headers, json=base), "INVALID_REQUEST", 422)
        await unchanged(context, before)


async def test_restore_idempotency_binds_target_expected_head_and_operation_kind(context):
    owner, _ = await account(context)
    creation_key = str(uuid4())
    first = accepted(await create_question(context, owner, key=creation_key), 1)
    derive_key = str(uuid4())
    second = accepted(await derive(context, owner, first, key=derive_key), 2)
    before = await snapshot(context)
    for used in (creation_key, derive_key):
        rejected(await restore(context, owner, first, expected=second["id"], key=used), "IDEMPOTENCY_CONFLICT")
        await unchanged(context, before)
    key = str(uuid4())
    third = restored(await restore(context, owner, first, expected=second["id"], key=key), 3, first, second["id"])
    after = await snapshot(context)
    rejected(await restore(context, owner, second, expected=second["id"], key=key), "IDEMPOTENCY_CONFLICT")
    rejected(await restore(context, owner, first, expected=third["id"], key=key), "IDEMPOTENCY_CONFLICT")
    rejected(await derive(context, owner, first, first["content"], expected=second["id"], key=key), "IDEMPOTENCY_CONFLICT")
    await unchanged(context, after)


async def test_restore_replay_after_head_advances_returns_original_operation_without_moving_head(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    key = str(uuid4())
    third = restored(await restore(context, owner, first, expected=second["id"], key=key), 3, first, second["id"])
    fourth = accepted(await derive(context, owner, third, number_content("恢复后继续编辑")), 4)
    before = await snapshot(context)
    replay = await restore(context, owner, first, expected=second["id"], key=key)
    assert restored(replay, 3, first, second["id"], replay=True) == third
    await unchanged(context, before)
    root = await context.db.questions.find_one({"_id": first["questionId"]})
    assert root["currentVersionId"] == fourth["id"] and root["lastVersionNo"] == 4


@pytest.mark.parametrize("mode", ["same-key", "different-key", "restore-vs-derive"])
async def test_concurrent_restore_operations_have_one_committed_version_and_no_orphan(context, mode):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    key = str(uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    arrivals, tasks = [], []

    async def rendezvous(**state):
        assert state["question"]["currentVersionId"] == second["id"]
        arrivals.append(state["question"]["_id"])
        if len(arrivals) == 2:
            entered.set()
        await release.wait()

    context.app.state.hooks["before_question_version_transaction"] = rendezvous
    before = await snapshot(context)
    try:
        tasks.append(asyncio.create_task(restore(context, owner, first, expected=second["id"], key=key)))
        other = (derive(context, owner, first, number_content("并发派生"), expected=second["id"])
                 if mode == "restore-vs-derive" else
                 restore(context, owner, first, expected=second["id"], key=key if mode == "same-key" else str(uuid4())))
        tasks.append(asyncio.create_task(other))
        await asyncio.wait_for(entered.wait(), 5)
        assert len(arrivals) == 2 and all(not task.done() for task in tasks)
        await unchanged(context, before)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.hooks.pop("before_question_version_transaction", None)
    assert sorted(response.status_code for response in outcomes) == ([200, 201] if mode == "same-key" else [201, 409])
    winner = next(response for response in outcomes if response.status_code == 201)
    if winner.json().get("operation") == "restore":
        saved = restored(winner, 3, first, second["id"])
    else:
        assert mode == "restore-vs-derive"
        saved = accepted(winner, 3)
    if mode == "same-key":
        replay = next(response for response in outcomes if response.status_code == 200)
        assert restored(replay, 3, first, second["id"], replay=True) == saved
    else:
        rejected(next(response for response in outcomes if response.status_code == 409), "QUESTION_VERSION_CONFLICT")
    assert await context.db.question_versions.count_documents({"questionId": first["questionId"]}) == 3
    root = await context.db.questions.find_one({"_id": first["questionId"]})
    assert root["lastVersionNo"] == 3 and root["currentVersionId"] == saved["id"]


async def test_restore_insert_failure_rolls_back_metadata_and_original_key_can_retry(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    key = str(uuid4())
    before = await snapshot(context)
    inserted = []

    async def fail_after_insert(**state):
        inserted.append(deepcopy(state["document"]))
        raise ServiceError("INJECTED_FAILURE", "合成恢复事务失败", 503)

    context.app.state.hooks["after_question_version_insert"] = fail_after_insert
    try:
        rejected(await restore(context, owner, first, key=key), "INJECTED_FAILURE", 503)
    finally:
        context.app.state.hooks.pop("after_question_version_insert", None)
    assert len(inserted) == 1 and inserted[0]["operation"] == "restore"
    assert inserted[0]["previousCurrentVersionId"] == first["id"]
    await unchanged(context, before)
    second = restored(await restore(context, owner, first, key=key), 2, first, first["id"])
    assert restored(await restore(context, owner, first, key=key), 2, first, first["id"], replay=True) == second


async def test_restore_transaction_retry_leaves_only_one_new_version_with_complete_metadata(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    key, inserts = str(uuid4()), []

    async def transient_once(**state):
        inserts.append(deepcopy(state["document"]))
        if len(inserts) == 1:
            raise OperationFailure("synthetic restore transaction retry", code=112,
                                   details={"errorLabels": ["TransientTransactionError"]})

    context.app.state.hooks["after_question_version_insert"] = transient_once
    try:
        second = restored(await restore(context, owner, first, key=key), 2, first, first["id"])
    finally:
        context.app.state.hooks.pop("after_question_version_insert", None)
    assert len(inserts) == 2 and inserts[0]["_id"] != inserts[1]["_id"] == second["id"]
    assert all(item["content"] == first["content"] and item["previousCurrentVersionId"] == first["id"] for item in inserts)
    assert await context.db.question_versions.find_one({"_id": inserts[0]["_id"]}) is None
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 1
    assert await context.db.question_versions.count_documents({"questionId": first["questionId"]}) == 2


async def test_restore_recovers_original_metadata_after_real_commit_then_lost_service_return(context, monkeypatch):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    key, committed = str(uuid4()), []
    original = Repository.transaction

    async def lose_after_commit(repo, callback):
        result = await original(repo, callback)
        if repo.db.name == context.db.name:
            committed.append((jsonable_encoder(result), await snapshot(context)))
            raise AutoReconnect("synthetic lost service return after acknowledged restore commit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "transaction", lose_after_commit)
        response = await restore(context, owner, first, key=key)
    second = restored(response, 2, first, first["id"], replay=True)
    assert len(committed) == 1 and second == committed[0][0]
    await unchanged(context, committed[0][1])
    assert restored(await restore(context, owner, first, key=key), 2, first, first["id"], replay=True) == second
    await unchanged(context, committed[0][1])


async def test_restore_unknown_write_and_failed_recovery_preserve_retry_semantics(context, monkeypatch):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    key, phases = str(uuid4()), []
    before = await snapshot(context)
    original_transaction = Repository.transaction
    collection_type = type(context.db.question_versions)
    original_find = collection_type.find_one

    async def fail_before_write(repo, callback):
        if repo.db.name == context.db.name:
            phases.append("transaction-error")
            raise AutoReconnect("synthetic restore failure before any transaction write")
        return await original_transaction(repo, callback)

    async def fail_post_error_lookup(collection, criteria=None, *args, **kwargs):
        if (collection.database.name == context.db.name and collection.name == "question_versions"
                and criteria == {"questionId": first["questionId"], "requestKey": key}
                and phases == ["transaction-error"]):
            phases.append("recovery-error")
            raise AutoReconnect("synthetic restore recovery lookup failure")
        return await original_find(collection, criteria, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "transaction", fail_before_write)
        patch.setattr(collection_type, "find_one", fail_post_error_lookup)
        rejected(await restore(context, owner, first, key=key), "QUESTION_WRITE_OUTCOME_UNKNOWN", 503)
    assert phases == ["transaction-error", "recovery-error"]
    await unchanged(context, before)
    restored(await restore(context, owner, first, key=key), 2, first, first["id"])


async def test_mongo_enforces_restore_metadata_pair_types_and_disallows_restore_v1(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = restored(await restore(context, owner, first), 2, first, first["id"])
    before = await snapshot(context)
    invalid_updates = [
        {"$unset": {"operation": ""}}, {"$unset": {"previousCurrentVersionId": ""}},
        {"$set": {"operation": "derive"}}, {"$set": {"operation": None}},
        {"$set": {"previousCurrentVersionId": None}}, {"$set": {"previousCurrentVersionId": 123}},
        {"$set": {"previousCurrentVersionId": ""}}, {"$set": {"previousCurrentVersionId": "x" * 161}},
    ]
    for update in invalid_updates:
        code = None
        try:
            await context.db.question_versions.update_one({"_id": second["id"]}, update)
        except PyMongoError as error:
            code = getattr(error, "code", None)
        assert code == 121, f"Expected restore metadata validation error, received {code}"
        await unchanged(context, before)
    # A new synthetic series ID prevents a unique-index collision from hiding
    # the intended schema rejection. This is not an API creation workflow.
    original = await context.db.question_versions.find_one({"_id": first["id"]})
    illegal_first = {**deepcopy(original), "_id": str(uuid4()), "questionId": str(uuid4()),
                     "operation": "restore", "previousCurrentVersionId": first["id"]}
    code = None
    try:
        await context.db.question_versions.insert_one(illegal_first)
    except PyMongoError as error:
        code = getattr(error, "code", None)
    assert code == 121, f"Expected v1 restore rejection, received {code}"
    await unchanged(context, before)


async def test_legacy_and_restore_versions_survive_repeated_initialization_without_backfill(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    third = restored(await restore(context, owner, first, expected=second["id"]), 3, first, second["id"])
    before = await snapshot(context)
    for _ in range(2):
        await init_database(context.db)
        await unchanged(context, before)
    for old in (first, second):
        document = await context.db.question_versions.find_one({"_id": old["id"]})
        assert "operation" not in document and "previousCurrentVersionId" not in document
        response = await context.client.get(f'/api/questions/{first["questionId"]}/versions/{old["id"]}', headers=owner)
        assert response.status_code == 200 and response.json() == old
    read = await context.client.get(f'/api/questions/{first["questionId"]}/versions/{third["id"]}', headers=owner)
    assert read.status_code == 200 and read.json() == third
    await unchanged(context, before)
