"""Private question versions against the real isolated MongoDB test database.

The context fixture owns database cleanup. Faults below are explicit service
hooks; they do not replace database transactions or mock successful writes.
"""

import asyncio
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from pymongo.errors import AutoReconnect, OperationFailure, PyMongoError
import pytest

from app.database import init_database
from app.repositories import Repository
from app.services import ServiceError
from conftest import account, published, start, submit
from scripts.fixtures import example_definition


OLD_COLLECTIONS = ("users", "surveys", "survey_versions", "responses")
NEW_COLLECTIONS = ("questions", "question_versions")
# SHA-256 of canonical JSON from database.py in commit 99b66d1e637fa3b4ecb13719c0230ecb7e958701.
# Keeping these independent of current schema constants detects accidental changes.
BASELINE_VALIDATOR_HASHES = {
    "users": "c6a946b641b728f1fe8dc2d9f7b86bdcc9d994286c55427ce2ec86e6f0decbb8",
    "surveys": "b96c06ff8072d489c57713503067a4d23616ee1141357d969b5062e6ae58d096",
    "survey_versions": "878ea6b5eb039efcacd842f59d41b2f3c94ac5bc1077727701893f3f0c66bfac",
    "responses": "ac20dfa6a0dc78487316b840818337c11635856d2fa673a28df2f04b519a717b",
}
OPTIONS = [{"id": "yes", "label": "是"}, {"id": "no", "label": "否"}]
VALID_CONTENTS = [
    {"kind": "single", "title": " 是否参加 ", "config": {"options": OPTIONS}},
    {"kind": "multi", "title": "选择项目", "config": {"options": OPTIONS, "minSelected": 0, "maxSelected": 2}},
    {"kind": "text", "title": "补充说明", "config": {"minLength": 0, "maxLength": 10000}},
    {"kind": "number", "title": "分数", "config": {"min": 0, "max": 10, "integerOnly": False}},
]


def number_content(title="数值题", **config):
    return {"kind": "number", "title": title, "config": config}


async def create_question(context, headers, content=None, key=None):
    return await context.client.post("/api/questions",
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json={"content": number_content() if content is None else content})


async def derive(context, headers, first, content=None, *, parent=None, expected=None, key=None):
    return await context.client.post(f'/api/questions/{first["questionId"]}/versions',
        headers={**headers, "Idempotency-Key": key or str(uuid4())}, json={
            "content": number_content("新版题干") if content is None else content,
            "parentVersionId": parent or first["id"],
            "expectedCurrentVersionId": expected or first["id"],
        })


def accepted(response, number, replay=False):
    assert response.status_code == (200 if replay else 201), response.text
    assert response.headers.get("Idempotent-Replay") == str(replay).lower()
    body = response.json()
    assert set(body) == {"id", "questionId", "versionNo", "parentVersionId", "content", "createdAt"}
    assert body["versionNo"] == number
    assert set(body["content"]) == {"kind", "title", "config"}
    return body


def rejected(response, code, status=409):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.headers.get("Idempotent-Replay") != "true"


async def snapshot(context, collections=NEW_COLLECTIONS):
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in collections}


async def unchanged(context, before, collections=NEW_COLLECTIONS):
    # Old snapshots contain private credentials/participant fields. Report only
    # equality so assertion introspection never expands those documents.
    equal = await snapshot(context, collections) == before
    assert equal, "Persisted documents changed unexpectedly"


@pytest.mark.parametrize("content", VALID_CONTENTS, ids=["single", "multi", "text", "number"])
async def test_four_kinds_create_persist_and_read_private_series(context, content):
    owner, user = await account(context)
    first = accepted(await create_question(context, owner, content), 1)
    assert first["parentVersionId"] is None
    assert first["content"] == content
    question = await context.db.questions.find_one({"_id": first["questionId"]})
    version = await context.db.question_versions.find_one({"_id": first["id"]})
    assert question["ownerId"] == user["id"]
    assert question["currentVersionId"] == version["_id"] == first["id"]
    assert question["lastVersionNo"] == version["versionNo"] == 1
    assert version["questionId"] == question["_id"]
    assert version["content"] == first["content"]
    assert version["parentVersionId"] is None
    assert len(version["requestDigest"]) == 64
    assert version["createdAt"] == datetime.fromisoformat(first["createdAt"])
    endpoint = f'/api/questions/{first["questionId"]}'
    detail = await context.client.get(endpoint, headers=owner)
    assert detail.status_code == 200
    assert set(detail.json()) == {"id", "currentVersionId", "lastVersionNo", "createdAt", "updatedAt"}
    assert detail.json()["currentVersionId"] == first["id"]
    listed = await context.client.get("/api/questions", headers=owner)
    assert listed.status_code == 200 and listed.json()["items"] == [detail.json()]
    read = await context.client.get(endpoint + f'/versions/{first["id"]}', headers=owner)
    assert read.status_code == 200 and read.json() == first


async def test_all_question_routes_require_login_and_hide_foreign_owners(context):
    owner, _ = await account(context)
    other, _ = await account(context, "question_other")
    first = accepted(await create_question(context, owner), 1)
    base = f'/api/questions/{first["questionId"]}'
    body = {"content": number_content(), "parentVersionId": first["id"],
            "expectedCurrentVersionId": first["id"]}
    before = await snapshot(context)
    for method, path, payload in [
        ("GET", "/api/questions", None), ("POST", "/api/questions", {"content": number_content()}),
        ("GET", base, None), ("GET", base + "/versions", None),
        ("GET", base + f'/versions/{first["id"]}', None), ("POST", base + "/versions", body),
    ]:
        response = await context.client.request(method, path,
            headers={"Idempotency-Key": str(uuid4())}, json=payload)
        rejected(response, "UNAUTHENTICATED", 401)
    for question_id in (first["questionId"], str(uuid4())):
        endpoint = f"/api/questions/{question_id}"
        for suffix in ("", "/versions", f'/versions/{first["id"]}'):
            rejected(await context.client.get(endpoint + suffix, headers=other), "NOT_FOUND", 404)
        rejected(await context.client.post(endpoint + "/versions", json=body,
            headers={**other, "Idempotency-Key": str(uuid4())}), "NOT_FOUND", 404)
    assert (await context.client.get("/api/questions", headers=other)).json()["items"] == []
    await unchanged(context, before)


async def test_immutable_versions_can_branch_from_an_old_version_with_current_head_cas(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("第一版", min=0, max=10)), 1)
    first_stored = await context.db.question_versions.find_one({"_id": first["id"]})
    second = accepted(await derive(context, owner, first, number_content("第二版", min=0, max=20)), 2)
    second_stored = await context.db.question_versions.find_one({"_id": second["id"]})
    third = accepted(await derive(context, owner, first, first["content"], expected=second["id"]), 3)
    assert third["parentVersionId"] == first["id"]
    assert third["content"] == first["content"]
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert await context.db.question_versions.find_one({"_id": first["id"]}) == first_stored
    assert await context.db.question_versions.find_one({"_id": second["id"]}) == second_stored
    endpoint = f'/api/questions/{first["questionId"]}'
    detail = (await context.client.get(endpoint, headers=owner)).json()
    assert detail["currentVersionId"] == third["id"] and detail["lastVersionNo"] == 3
    all_versions = await context.client.get(endpoint + "/versions", headers=owner)
    assert all_versions.status_code == 200 and all_versions.json()["items"] == [third, second, first]
    page = await context.client.get(endpoint + "/versions", headers=owner, params={"limit": 1, "offset": 1})
    assert page.status_code == 200 and page.json()["items"] == [second]
    assert (await context.client.get(endpoint + "/versions", headers=owner,
        params={"limit": 1, "offset": 3})).json()["items"] == []


@pytest.mark.parametrize("foreign_parent", [False, True], ids=["missing-parent", "other-question-parent"])
async def test_parent_must_belong_to_target_question_and_failed_key_is_reusable(context, foreign_parent):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    wrong = accepted(await create_question(context, owner), 1)["id"] if foreign_parent else str(uuid4())
    key = str(uuid4())
    before = await snapshot(context)
    rejected(await derive(context, owner, first, parent=wrong, key=key), "QUESTION_VERSION_NOT_FOUND", 404)
    await unchanged(context, before)
    second = accepted(await derive(context, owner, first, key=key), 2)
    assert second["parentVersionId"] == first["id"]


async def test_reading_a_missing_or_other_questions_version_has_no_cross_series_leak(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    foreign = accepted(await create_question(context, owner), 1)
    before = await snapshot(context)
    for version_id in (str(uuid4()), foreign["id"]):
        response = await context.client.get(
            f'/api/questions/{first["questionId"]}/versions/{version_id}', headers=owner)
        rejected(response, "QUESTION_VERSION_NOT_FOUND", 404)
    await unchanged(context, before)


async def test_stale_expected_head_rejects_without_reserving_the_request_key(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    key = str(uuid4())
    before = await snapshot(context)
    rejected(await derive(context, owner, first, key=key), "QUESTION_VERSION_CONFLICT")
    await unchanged(context, before)
    accepted(await derive(context, owner, first, expected=second["id"], key=key), 3)


async def test_create_replay_recovers_v1_after_head_moves_and_scopes_key_to_owner(context):
    owner, _ = await account(context)
    other, _ = await account(context, "independent_owner")
    key = str(uuid4())
    original = number_content(" 保留空格 ", min=1, max=10)
    first = accepted(await create_question(context, owner, original, key), 1)
    second = accepted(await derive(context, owner, first), 2)
    before = await snapshot(context)
    equivalent = number_content(" 保留空格 ", min=1.0, max=10.0, integerOnly=False)
    assert accepted(await create_question(context, owner, equivalent, key), 1, replay=True) == first
    rejected(await create_question(context, owner, number_content("不同题干"), key), "IDEMPOTENCY_CONFLICT")
    await unchanged(context, before)
    stored = await context.db.questions.find_one({"_id": first["questionId"]})
    assert stored["currentVersionId"] == second["id"]
    other_first = accepted(await create_question(context, other, original, key), 1)
    assert other_first["questionId"] != first["questionId"]


async def test_derive_replay_binds_parent_expected_head_content_and_operation(context):
    owner, _ = await account(context)
    creation_key = str(uuid4())
    first = accepted(await create_question(context, owner, key=creation_key), 1)
    # Reusing a creation key for a derivation cannot return v1 as a successful derive.
    rejected(await derive(context, owner, first, key=creation_key), "IDEMPOTENCY_CONFLICT")
    key = str(uuid4())
    second = accepted(await derive(context, owner, first, number_content(min=1, max=2), key=key), 2)
    third = accepted(await derive(context, owner, second, number_content("第三版")), 3)
    before = await snapshot(context)
    equivalent = number_content(min=1.0, max=2.0, integerOnly=False)
    assert accepted(await derive(context, owner, first, equivalent, key=key), 2, replay=True) == second
    for parent, expected, content in [
        (second["id"], first["id"], equivalent),
        (first["id"], third["id"], equivalent),
        (first["id"], first["id"], number_content(min=1, max=3)),
    ]:
        rejected(await derive(context, owner, first, content, parent=parent, expected=expected, key=key),
                 "IDEMPOTENCY_CONFLICT")
    await unchanged(context, before)


@pytest.mark.parametrize("kind,config,explicit", [
    ("multi", {"options": OPTIONS}, {"options": OPTIONS, "minSelected": None, "maxSelected": None}),
    ("text", {}, {"minLength": None, "maxLength": None}),
    ("number", {}, {"min": None, "max": None, "integerOnly": False}),
])
async def test_omitted_optional_bounds_and_explicit_defaults_replay_at_http_boundary(context, kind, config, explicit):
    owner, _ = await account(context)
    key = str(uuid4())
    implicit = {"kind": kind, "title": "复用题目", "config": config}
    explicit_content = {**implicit, "config": explicit}
    first = accepted(await create_question(context, owner, implicit, key), 1)
    assert first["content"] == explicit_content
    assert accepted(await create_question(context, owner, explicit_content, key), 1, replay=True) == first
    version_key = str(uuid4())
    second = accepted(await derive(context, owner, first, implicit, key=version_key), 2)
    before = await snapshot(context)
    assert accepted(await derive(context, owner, first, explicit_content, key=version_key), 2, replay=True) == second
    await unchanged(context, before)


async def test_option_order_and_exact_title_are_part_of_idempotency_meaning(context):
    owner, _ = await account(context)
    key = str(uuid4())
    content = {"kind": "single", "title": " 保留空格 ", "config": {"options": OPTIONS}}
    first = accepted(await create_question(context, owner, content, key), 1)
    before = await snapshot(context)
    changed_contents = [
        {**content, "title": "保留空格"},
        {**content, "config": {"options": list(reversed(OPTIONS))}},
    ]
    for changed in changed_contents:
        rejected(await create_question(context, owner, changed, key), "IDEMPOTENCY_CONFLICT")
        await unchanged(context, before)
    assert first["content"] == content


@pytest.mark.parametrize("content", [
    {"kind": "single", "title": "单选", "config": {"options": [OPTIONS[0], OPTIONS[0]]}},
    {"kind": "multi", "title": "多选", "config": {"options": OPTIONS, "maxSelected": 3}},
    {"kind": "text", "title": "文本", "config": {"minLength": 4, "maxLength": 3}},
    number_content(min=0.1, max=0.2, integerOnly=True),
    number_content(integerOnly=None),
    {**number_content(), "title": " "},
    {**number_content(), "id": "questionnaire_local_id"},
], ids=["duplicate-option", "choice-bound", "text-range", "no-integer", "null-integer-flag",
        "blank-title", "usage-field"])
async def test_invalid_domain_content_is_not_saved_or_assigned_an_idempotency_key(context, content):
    owner, _ = await account(context)
    key = str(uuid4())
    response = await create_question(context, owner, content, key)
    rejected(response, "INVALID_QUESTION_CONTENT", 422)
    assert response.json()["fieldErrors"]
    assert all(error["field"].startswith("content") for error in response.json()["fieldErrors"])
    assert await snapshot(context) == {"questions": [], "question_versions": []}
    first = accepted(await create_question(context, owner, key=key), 1)
    before = await snapshot(context)
    response = await derive(context, owner, first, content, key="invalid-derive")
    rejected(response, "INVALID_QUESTION_CONTENT", 422)
    await unchanged(context, before)
    accepted(await derive(context, owner, first, key="invalid-derive"), 2)


async def test_http_request_boundaries_pagination_and_series_paging(context):
    owner, _ = await account(context)
    before = await snapshot(context)
    for request_headers, body in [
        (owner, {"content": number_content()}),
        ({**owner, "Idempotency-Key": ""}, {"content": number_content()}),
        ({**owner, "Idempotency-Key": "x" * 129}, {"content": number_content()}),
        ({**owner, "Idempotency-Key": "valid"}, {"content": number_content(), "ownerId": "forged"}),
    ]:
        rejected(await context.client.post("/api/questions", headers=request_headers, json=body),
                 "INVALID_REQUEST", 422)
    await unchanged(context, before)
    for index in range(3):
        accepted(await create_question(context, owner, number_content(f"题目{index}")), 1)
    listed = await context.client.get("/api/questions", headers=owner)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 3
    items = listed.json()["items"]
    for offset in range(3):
        page = await context.client.get("/api/questions", headers=owner, params={"limit": 1, "offset": offset})
        assert page.status_code == 200 and page.json()["items"] == [items[offset]]
    endpoints = ["/api/questions", f'/api/questions/{items[0]["id"]}/versions']
    for endpoint in endpoints:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 100001}):
            rejected(await context.client.get(endpoint, headers=owner, params=params), "INVALID_REQUEST", 422)


@pytest.mark.parametrize("same_key", [True, False], ids=["same-key-replay", "competing-head-cas"])
async def test_concurrent_derivations_have_one_committed_version_and_no_orphan(context, same_key):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    keys = [str(uuid4()), str(uuid4())]
    if same_key:
        keys[1] = keys[0]
    entered, release = asyncio.Event(), asyncio.Event()
    arrivals, tasks = [], []

    async def wait_before_transaction(**state):
        assert state["question"]["currentVersionId"] == first["id"]
        arrivals.append(state["question"]["_id"])
        if len(arrivals) == 2:
            entered.set()
        await release.wait()

    context.app.state.hooks["before_question_version_transaction"] = wait_before_transaction
    before = await snapshot(context)
    try:
        tasks = [asyncio.create_task(derive(context, owner, first, key=key)) for key in keys]
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
    expected_statuses = [200, 201] if same_key else [201, 409]
    assert sorted(response.status_code for response in outcomes) == expected_statuses
    winner_index = next(index for index, response in enumerate(outcomes) if response.status_code == 201)
    second = accepted(outcomes[winner_index], 2)
    loser_index = 1 - winner_index
    if same_key:
        assert accepted(outcomes[loser_index], 2, replay=True) == second
    else:
        rejected(outcomes[loser_index], "QUESTION_VERSION_CONFLICT")
        assert await context.db.question_versions.count_documents({
            "questionId": first["questionId"], "requestKey": keys[loser_index]}) == 0
    versions = await context.db.question_versions.find({"questionId": first["questionId"]}).sort(
        "versionNo", 1).to_list(length=None)
    assert [version["versionNo"] for version in versions] == [1, 2]
    question = await context.db.questions.find_one({"_id": first["questionId"]})
    assert question["currentVersionId"] == second["id"] and question["lastVersionNo"] == 2
    after = await snapshot(context)
    assert accepted(await derive(context, owner, first, key=keys[winner_index]), 2, replay=True) == second
    await unchanged(context, after)
    if not same_key:
        # Failed competition never reserves the losing key.
        accepted(await derive(context, owner, first, expected=second["id"], key=keys[loser_index]), 3)


async def test_concurrent_creation_with_one_owner_key_returns_one_initial_version(context, monkeypatch):
    owner, _ = await account(context)
    key = str(uuid4())
    original = Repository.transaction
    entered, release = asyncio.Event(), asyncio.Event()
    arrivals, tasks = [], []

    async def pause_before_real_transaction(repo, callback):
        if repo.db.name == context.db.name:
            arrivals.append(len(arrivals) + 1)
            if len(arrivals) == 2:
                entered.set()
            await release.wait()
        return await original(repo, callback)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "transaction", pause_before_real_transaction)
        try:
            tasks = [asyncio.create_task(create_question(context, owner, key=key)) for _ in range(2)]
            await asyncio.wait_for(entered.wait(), 5)
            assert arrivals == [1, 2] and all(not task.done() for task in tasks)
            assert await snapshot(context) == {"questions": [], "question_versions": []}
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert Repository.transaction is original
    assert sorted(response.status_code for response in outcomes) == [200, 201]
    first = accepted(next(response for response in outcomes if response.status_code == 201), 1)
    assert accepted(next(response for response in outcomes if response.status_code == 200), 1, replay=True) == first
    assert await context.db.questions.count_documents({}) == 1
    assert await context.db.question_versions.count_documents({}) == 1


async def test_concurrent_same_key_with_different_valid_content_conflicts_without_extra_version(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    key = str(uuid4())
    contents = [number_content("并发版本甲", min=0, max=10), number_content("并发版本乙", min=0, max=20)]
    entered, release = asyncio.Event(), asyncio.Event()
    arrivals, tasks = [], []

    async def wait_before_transaction(**state):
        assert state["question"]["currentVersionId"] == first["id"]
        arrivals.append(state["question"]["_id"])
        if len(arrivals) == 2:
            entered.set()
        await release.wait()

    context.app.state.hooks["before_question_version_transaction"] = wait_before_transaction
    before = await snapshot(context)
    try:
        tasks = [asyncio.create_task(derive(context, owner, first, content, key=key)) for content in contents]
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
    assert sorted(response.status_code for response in outcomes) == [201, 409]
    winner_index = next(index for index, response in enumerate(outcomes) if response.status_code == 201)
    loser_index = 1 - winner_index
    second = accepted(outcomes[winner_index], 2)
    rejected(outcomes[loser_index], "IDEMPOTENCY_CONFLICT")
    assert second["content"] == {**contents[winner_index], "config": {
        **contents[winner_index]["config"], "integerOnly": False}}
    assert await context.db.question_versions.count_documents({"questionId": first["questionId"]}) == 2
    assert await context.db.question_versions.count_documents({"questionId": first["questionId"], "requestKey": key}) == 1
    question = await context.db.questions.find_one({"_id": first["questionId"]})
    assert question["currentVersionId"] == second["id"] and question["lastVersionNo"] == 2
    after = await snapshot(context)
    assert accepted(await derive(context, owner, first, contents[winner_index], key=key), 2, replay=True) == second
    rejected(await derive(context, owner, first, contents[loser_index], key=key), "IDEMPOTENCY_CONFLICT")
    await unchanged(context, after)


@pytest.mark.parametrize("operation", ["create", "derive"])
async def test_insert_failure_rolls_back_entire_transaction_and_same_key_can_retry(context, operation):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1) if operation == "derive" else None
    key = str(uuid4())
    before = await snapshot(context)
    inserted = []

    async def fail_after_insert(**state):
        inserted.append(state["document"]["versionNo"])
        raise ServiceError("INJECTED_FAILURE", "测试插入后事务失败", 503)

    context.app.state.hooks["after_question_version_insert"] = fail_after_insert
    try:
        response = await (create_question(context, owner, key=key) if operation == "create"
                          else derive(context, owner, first, key=key))
        rejected(response, "INJECTED_FAILURE", 503)
    finally:
        context.app.state.hooks.pop("after_question_version_insert", None)
    expected_number = 1 if operation == "create" else 2
    assert inserted == [expected_number]
    await unchanged(context, before)
    response = await (create_question(context, owner, key=key) if operation == "create"
                      else derive(context, owner, first, key=key))
    saved = accepted(response, expected_number)
    replay = await (create_question(context, owner, key=key) if operation == "create"
                    else derive(context, owner, first, key=key))
    assert accepted(replay, expected_number, replay=True) == saved


@pytest.mark.parametrize("operation", ["create", "derive"])
async def test_transient_transaction_retry_leaves_no_partial_question_or_version(context, operation):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1) if operation == "derive" else None
    key = str(uuid4())
    inserts = []

    async def transient_error_after_first_insert(**state):
        inserts.append(deepcopy(state["document"]))
        if len(inserts) == 1:
            raise OperationFailure("synthetic transient failure after actual question version insert", code=112,
                                   details={"errorLabels": ["TransientTransactionError"]})

    context.app.state.hooks["after_question_version_insert"] = transient_error_after_first_insert
    try:
        response = await (create_question(context, owner, key=key) if operation == "create"
                          else derive(context, owner, first, key=key))
    finally:
        context.app.state.hooks.pop("after_question_version_insert", None)
    expected_number = 1 if operation == "create" else 2
    saved = accepted(response, expected_number)
    assert [document["versionNo"] for document in inserts] == [expected_number, expected_number]
    assert inserts[0]["_id"] != inserts[1]["_id"] == saved["id"]
    assert await context.db.question_versions.find_one({"_id": inserts[0]["_id"]}) is None
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 1
    assert await context.db.questions.count_documents({}) == 1
    question = await context.db.questions.find_one({"_id": saved["questionId"]})
    assert question["currentVersionId"] == saved["id"] and question["lastVersionNo"] == expected_number
    assert await context.db.question_versions.count_documents({"questionId": saved["questionId"]}) == expected_number


@pytest.mark.parametrize("operation", ["create", "derive"])
async def test_service_recovers_original_version_after_real_commit_then_lost_return(context, monkeypatch, operation):
    """Inject at the service/driver boundary after a real acknowledged commit.

    This simulates a lost transaction return; it is not an actual network outage
    or an UnknownTransactionCommitResult injection inside the driver's retry loop.
    """
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1) if operation == "derive" else None
    key = str(uuid4())
    original = Repository.transaction
    committed = []

    async def commit_then_lose_return(repo, callback):
        result = await original(repo, callback)
        if repo.db.name == context.db.name:
            saved = await context.db.question_versions.find_one({"_id": result["id"]})
            question = await context.db.questions.find_one({"_id": result["questionId"]})
            assert saved is not None and saved["requestKey"] == key
            assert question["currentVersionId"] == result["id"]
            committed.append((jsonable_encoder(result), await snapshot(context)))
            raise AutoReconnect("synthetic failure after real transaction commit and before service return")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "transaction", commit_then_lose_return)
        response = await (create_question(context, owner, key=key) if operation == "create"
                          else derive(context, owner, first, key=key))
    assert Repository.transaction is original
    expected_number = 1 if operation == "create" else 2
    saved = accepted(response, expected_number, replay=True)
    assert len(committed) == 1 and saved == committed[0][0]
    await unchanged(context, committed[0][1])
    assert await context.db.questions.count_documents({}) == 1
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 1
    assert await context.db.question_versions.count_documents({"questionId": saved["questionId"]}) == expected_number
    replay = await (create_question(context, owner, key=key) if operation == "create"
                    else derive(context, owner, first, key=key))
    assert accepted(replay, expected_number, replay=True) == saved
    await unchanged(context, committed[0][1])


@pytest.mark.parametrize("operation", ["create", "derive"])
async def test_service_reports_unknown_write_without_record_then_original_key_succeeds(context, monkeypatch, operation):
    """A synthetic non-transient driver-boundary error occurs before any write.

    No network is disconnected. Recovery finds no stored key, so the service
    must report an unknown result and preserve the exact request for retry.
    """
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1) if operation == "derive" else None
    key = str(uuid4())
    original = Repository.transaction
    before = await snapshot(context)
    failures = []

    async def fail_before_real_transaction(repo, callback):
        if repo.db.name == context.db.name:
            failures.append("before-write")
            # No TransientTransactionError label: this is handled by the service,
            # rather than automatically retried by with_transaction.
            raise AutoReconnect("synthetic failure before transaction starts")
        return await original(repo, callback)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "transaction", fail_before_real_transaction)
        response = await (create_question(context, owner, key=key) if operation == "create"
                          else derive(context, owner, first, key=key))
    assert Repository.transaction is original
    rejected(response, "QUESTION_WRITE_OUTCOME_UNKNOWN", 503)
    assert failures == ["before-write"]
    await unchanged(context, before)
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 0
    expected_number = 1 if operation == "create" else 2
    response = await (create_question(context, owner, key=key) if operation == "create"
                      else derive(context, owner, first, key=key))
    saved = accepted(response, expected_number)
    after = await snapshot(context)
    replay = await (create_question(context, owner, key=key) if operation == "create"
                    else derive(context, owner, first, key=key))
    assert accepted(replay, expected_number, replay=True) == saved
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 1
    assert await context.db.questions.count_documents({}) == 1
    await unchanged(context, after)


async def test_service_preserves_unknown_outcome_semantics_when_post_failure_recovery_query_fails(context, monkeypatch):
    """Inject two service/driver-boundary failures; no real network outage occurs."""
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    key = str(uuid4())
    before = await snapshot(context)
    original_transaction = Repository.transaction
    collection_type = type(context.db.question_versions)
    original_find_one = collection_type.find_one
    phases = []

    async def fail_before_transaction(repo, callback):
        if repo.db.name == context.db.name:
            phases.append("transaction-error")
            raise AutoReconnect("synthetic transaction boundary failure before writes")
        return await original_transaction(repo, callback)

    async def fail_only_post_error_recovery(collection, criteria=None, *args, **kwargs):
        target_query = {"questionId": first["questionId"], "requestKey": key}
        if (collection.database.name == context.db.name and collection.name == "question_versions"
                and criteria == target_query and phases == ["transaction-error"]):
            phases.append("recovery-query-error")
            raise AutoReconnect("synthetic recovery query boundary failure after transaction error")
        return await original_find_one(collection, criteria, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "transaction", fail_before_transaction)
        patch.setattr(collection_type, "find_one", fail_only_post_error_recovery)
        response = await derive(context, owner, first, key=key)
    assert Repository.transaction is original_transaction and collection_type.find_one is original_find_one
    assert phases == ["transaction-error", "recovery-query-error"]
    rejected(response, "QUESTION_WRITE_OUTCOME_UNKNOWN", 503)
    await unchanged(context, before)
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 0
    second = accepted(await derive(context, owner, first, key=key), 2)
    assert accepted(await derive(context, owner, first, key=key), 2, replay=True) == second
    assert await context.db.question_versions.count_documents({"requestKey": key}) == 1


async def test_new_collection_unique_indexes_and_representative_bson_constraints(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    before = await snapshot(context)
    question, version = before["questions"][0], before["question_versions"][0]
    duplicates = [
        ("questions", {**deepcopy(question), "_id": str(uuid4())}),
        ("question_versions", {**deepcopy(version), "_id": str(uuid4()), "requestKey": "different-key"}),
        ("question_versions", {**deepcopy(version), "_id": str(uuid4()), "versionNo": 2,
                               "parentVersionId": first["id"]}),
    ]
    for collection, candidate in duplicates:
        code = None
        try:
            await context.db[collection].insert_one(candidate)
        except PyMongoError as error:
            code = getattr(error, "code", None)
        assert code == 11000, f"{collection}: expected unique index rejection, received {code}"
        await unchanged(context, before)
    invalid_updates = [
        ("questions", question["_id"], {"$unset": {"currentVersionId": ""}}),
        ("questions", question["_id"], {"$set": {"lastVersionNo": True}}),
        ("question_versions", version["_id"], {"$unset": {"content.kind": ""}}),
        ("question_versions", version["_id"], {"$set": {"content.required": True}}),
        ("question_versions", version["_id"], {"$set": {"content.config.integerOnly": 1}}),
        ("question_versions", version["_id"], {"$set": {"parentVersionId": "invalid-v1-parent"}}),
    ]
    for collection, document_id, update in invalid_updates:
        code = None
        try:
            await context.db[collection].update_one({"_id": document_id}, update)
        except PyMongoError as error:
            code = getattr(error, "code", None)
        assert code == 121, f"{collection}: expected validation rejection, received {code}"
        await unchanged(context, before)


async def test_question_operations_and_repeated_init_preserve_all_phase1_data_and_validators(context):
    owner, _ = await account(context)
    survey, _ = await published(context, owner, example_definition(), multiple=True)
    attempt = await start(context, owner, survey)
    for anonymous in (True, False):
        response = await submit(context, owner, survey, attempt,
            {"q_age": 17, "q_hobby": ["read", "sport"], "q_score": 0}, anonymous=anonymous)
        assert response.status_code == 201
    before = await snapshot(context, OLD_COLLECTIONS)
    assert [len(before[name]) for name in OLD_COLLECTIONS] == [1, 1, 1, 2]

    async def assert_old_contracts():
        for collection in OLD_COLLECTIONS:
            options = await context.db[collection].options()
            assert options["validationLevel"] == "strict" and options["validationAction"] == "error"
            validator = deepcopy(options["validator"])
            # Step 2 intentionally adds optional provenance, without weakening
            # any phase-1 field or constraint. Keep the original fingerprints.
            extra = {"surveys": "draftQuestionSources", "survey_versions": "questionSources"}.get(collection)
            if extra:
                schema = validator["$jsonSchema"]
                assert extra not in schema["required"]
                source_schema = schema["properties"].pop(extra)
                assert source_schema["bsonType"] == "array" and source_schema["maxItems"] == 100
                assert set(source_schema["items"]["required"]) == {"instanceId", "questionId", "questionVersionId"}
            canonical = json.dumps(validator, sort_keys=True, separators=(",", ":"))
            assert hashlib.sha256(canonical.encode()).hexdigest() == BASELINE_VALIDATOR_HASHES[collection]

    await assert_old_contracts()
    for _ in range(2):
        await init_database(context.db)
        await assert_old_contracts()
        await unchanged(context, before, OLD_COLLECTIONS)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    await unchanged(context, before, OLD_COLLECTIONS)
    rejected(await derive(context, owner, first), "QUESTION_VERSION_CONFLICT")
    await unchanged(context, before, OLD_COLLECTIONS)
    new_before = await snapshot(context)
    for _ in range(2):
        await init_database(context.db)
        await assert_old_contracts()
        await unchanged(context, before, OLD_COLLECTIONS)
        await unchanged(context, new_before)
    collections = set(await context.db.list_collection_names())
    assert collections == set(OLD_COLLECTIONS + NEW_COLLECTIONS)
    assert (await context.client.get(f'/api/questions/{first["questionId"]}', headers=owner)).json()[
        "currentVersionId"] == second["id"]
