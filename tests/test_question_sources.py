"""Question sources stay explicit, private and frozen with survey versions.

These tests use the real ASGI application and isolated MongoDB databases. The
only race barrier wraps the existing repository CAS; the failure uses the
existing publication hook. Neither substitutes successful storage operations.
"""

import asyncio
from copy import deepcopy
import csv
import io
from uuid import uuid4

import pytest

from app.database import init_database
from app.repositories import Repository
from app.services import ServiceError
from conftest import account, published, start, submit
from scripts.fixtures import example_definition
from test_question_versions import accepted, create_question, derive, number_content, rejected


OMITTED = object()
COLLECTIONS = ("users", "surveys", "survey_versions", "responses", "questions", "question_versions")


def source(version, instance="score"):
    return {"instanceId": instance, "questionId": version["questionId"], "questionVersionId": version["id"]}


def definition(version, instance="score", title="来源测试问卷"):
    return {"title": title, "description": "测试数据", "allowAnonymous": True,
            "entryQuestionId": instance, "questions": [
                {"id": instance, **deepcopy(version["content"]), "required": True,
                 "defaultTarget": "END", "rules": []}]}


async def save(context, owner, survey, value, sources=OMITTED):
    body = {"definition": value, "expectedDraftRevision": survey["draftRevision"]}
    if sources is not OMITTED:
        body["questionSources"] = sources
    return await context.client.put(f'/api/surveys/{survey["id"]}/draft', headers=owner, json=body)


async def draft(context, owner, value, sources=OMITTED, *, multiple=False):
    result = await context.client.post("/api/surveys", headers=owner, json={"title": value["title"]})
    assert result.status_code == 201, result.text
    survey = result.json()
    if multiple:
        settings = await context.client.patch(f'/api/surveys/{survey["id"]}/settings', headers=owner,
            json={"expectedSettingsRevision": 0, "allowMultiple": True})
        assert settings.status_code == 200, settings.text
        survey = settings.json()
    result = await save(context, owner, survey, value, sources)
    assert result.status_code == 200, result.text
    return result.json()


async def publish(context, owner, survey, *, key=None, expected=OMITTED):
    body = {"expectedDraftRevision": survey["draftRevision"],
            "expectedCurrentVersionId": survey["currentVersionId"] if expected is OMITTED else expected,
            "publishKey": key or str(uuid4())}
    return await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=owner, json=body)


async def read_survey(context, owner, survey):
    response = await context.client.get(f'/api/surveys/{survey["id"]}', headers=owner)
    assert response.status_code == 200, response.text
    return response.json()


async def snapshot(context, collections=COLLECTIONS):
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in collections}


async def unchanged(context, before, collections=COLLECTIONS):
    # Avoid exposing password hashes or internal participant fields on failure.
    equal = await snapshot(context, collections) == before
    assert equal, "Persisted business documents changed unexpectedly"


def keys_in(value):
    if isinstance(value, dict):
        return set(value).union(*(keys_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(item) for item in value))
    return set()


async def test_two_surveys_pin_v1_and_only_explicitly_updated_survey_uses_v2(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("原版评分", min=0, max=10)), 1)
    a = await draft(context, owner, definition(first, "a_score", "问卷 A"), [source(first, "a_score")], multiple=True)
    b = await draft(context, owner, definition(first, "b_score", "问卷 B"), [source(first, "b_score")], multiple=True)
    a_result, b_result = await publish(context, owner, a), await publish(context, owner, b)
    assert a_result.status_code == b_result.status_code == 201
    a_v1, b_v1 = a_result.json(), b_result.json()
    assert a_v1["questionSources"] == [source(first, "a_score")]
    assert b_v1["questionSources"] == [source(first, "b_score")]
    a, b = await read_survey(context, owner, a), await read_survey(context, owner, b)

    pending_a_v1 = await start(context, owner, a)
    for survey, instance in ((a, "a_score"), (b, "b_score")):
        attempt = await start(context, owner, survey)
        result = await submit(context, owner, survey, attempt, {instance: 0})
        assert result.status_code == 201, result.text
    old_a_version = await context.db.survey_versions.find_one({"_id": a_v1["id"]})
    old_a_answers = await context.db.responses.find({"surveyId": a["id"]}).to_list(length=None)
    old_b = {"survey": await context.db.surveys.find_one({"_id": b["id"]}),
             "version": await context.db.survey_versions.find_one({"_id": b_v1["id"]}),
             "responses": await context.db.responses.find({"surveyId": b["id"]}).to_list(length=None)}
    old_business = await snapshot(context, COLLECTIONS[:4])

    second = accepted(await derive(context, owner, first, number_content("新版评分", min=1, max=5)), 2)
    await unchanged(context, old_business, COLLECTIONS[:4])
    changed = await save(context, owner, a, definition(second, "a_score", "问卷 A"), [source(second, "a_score")])
    assert changed.status_code == 200, changed.text
    a = changed.json()
    a_result = await publish(context, owner, a)
    assert a_result.status_code == 201, a_result.text
    a_v2 = a_result.json()
    assert a_v2["questionSources"] == [source(second, "a_score")]
    assert a_v2["definition"]["questions"][0]["config"] == second["content"]["config"]
    assert await context.db.survey_versions.find_one({"_id": a_v1["id"]}) == old_a_version
    old_answers_equal = await context.db.responses.find({"surveyId": a["id"]}).to_list(length=None) == old_a_answers
    assert old_answers_equal, "Updating A changed its already accepted V1 responses"
    b_equal = {"survey": await context.db.surveys.find_one({"_id": b["id"]}),
               "version": await context.db.survey_versions.find_one({"_id": b_v1["id"]}),
               "responses": await context.db.responses.find({"surveyId": b["id"]}).to_list(length=None)} == old_b
    assert b_equal, "Updating A changed B's full stored documents"

    # A credential obtained before publication remains bound to the old version.
    assert pending_a_v1["versionId"] == a_v1["id"]
    delayed = await submit(context, owner, a, pending_a_v1, {"a_score": 0})
    assert delayed.status_code == 201 and delayed.json()["versionId"] == a_v1["id"]
    new_attempt = await start(context, owner, a)
    assert new_attempt["versionId"] == a_v2["id"]
    public = await context.client.get(f'/api/survey/{a["publicId"]}', headers=owner)
    assert public.status_code == 200
    for payload in (pending_a_v1, new_attempt, public.json()):
        assert not keys_in(payload).intersection({"questionSources", "draftQuestionSources", "questionVersionId", "ownerId"})
        assert first["questionId"] not in str(payload) and second["id"] not in str(payload)
    before_invalid = await snapshot(context)
    rejected(await submit(context, owner, a, new_attempt, {"a_score": 0}), "ANSWER_VALIDATION_FAILED", 422)
    await unchanged(context, before_invalid)
    result = await submit(context, owner, a, new_attempt, {"a_score": 3})
    assert result.status_code == 201, result.text

    for survey, version, count, total, instance, label in (
        (a, a_v1, 2, 0, "a_score", "原版评分"),
        (a, a_v2, 1, 3, "a_score", "新版评分"),
        (b, b_v1, 1, 0, "b_score", "原版评分"),
    ):
        response = await context.client.get(f'/api/surveys/{survey["id"]}/stats', headers=owner,
            params={"versionId": version["id"]})
        assert response.status_code == 200, response.text
        stats = response.json()
        question = stats["questions"][0]
        assert stats["totalResponses"] == count
        assert (question["questionId"], question["title"], question["A"], question["sum"]) == (instance, label, count, total)
        exported = await context.client.get(f'/api/surveys/{survey["id"]}/export', headers=owner,
            params={"versionId": version["id"]})
        assert exported.status_code == 200, exported.text
        rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
        assert len(rows) == count and label in rows[0]
        assert sum(float(row[label]) for row in rows) == total
    assert await context.db.survey_versions.find_one({"_id": a_v1["id"]}) == old_a_version
    assert "questionSources" not in keys_in(await context.db.responses.find({}).to_list(length=None))


async def test_same_bank_question_can_have_two_local_instances_and_local_routing(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("复用数值", min=0, max=10)), 1)
    value = definition(first, "entry")
    local = value["questions"][0]
    local["required"] = False
    local["defaultTarget"] = "followup"
    local["rules"] = [{"id": "skip_small", "priority": 1,
                       "condition": {"operator": "lt", "value": 5}, "target": "END"}]
    value["questions"].append({"id": "followup", **deepcopy(first["content"]), "required": True,
                               "defaultTarget": "END", "rules": []})
    sources = [source(first, "entry"), source(first, "followup")]
    survey = await draft(context, owner, value, sources, multiple=True)
    result = await publish(context, owner, survey)
    assert result.status_code == 201, result.text
    assert result.json()["definition"] == value and result.json()["questionSources"] == sources
    result = await submit(context, owner, survey, await start(context, owner, survey), {"entry": 0})
    assert result.status_code == 201, result.text
    saved = await context.db.responses.find_one({"_id": result.json()["receiptId"]})
    assert saved["path"] == ["entry"] and saved["answers"] == [{"questionId": "entry", "kind": "number", "value": 0}]
    result = await submit(context, owner, survey, await start(context, owner, survey), {"entry": 6, "followup": 0})
    assert result.status_code == 201, result.text
    assert (await context.db.responses.find_one({"_id": result.json()["receiptId"]}))["path"] == ["entry", "followup"]


@pytest.mark.parametrize("content,equivalent", [
    (number_content(min=1, max=10), number_content(min=1.0, max=10.0, integerOnly=False)),
    ({"kind": "text", "title": "文字", "config": {}},
     {"kind": "text", "title": "文字", "config": {"minLength": None, "maxLength": None}}),
    ({"kind": "multi", "title": "多选", "config": {"options": [{"id": "a", "label": "甲"}]}},
     {"kind": "multi", "title": "多选", "config": {"options": [{"id": "a", "label": "甲"}], "minSelected": None, "maxSelected": None}}),
], ids=["integral-number-and-default", "text-optional-bounds", "multi-optional-bounds"])
async def test_equivalent_valid_content_can_be_pinned_without_silently_replacing_local_content(context, content, equivalent):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, equivalent), 1)
    value = definition(first)
    value["questions"][0].update(deepcopy(content))
    survey = await draft(context, owner, value, [source(first)])
    assert survey["draft"] == value
    result = await publish(context, owner, survey)
    assert result.status_code == 201, result.text
    assert result.json()["definition"] == value


async def test_content_mismatch_and_invalid_content_never_rewrite_definition_or_reserve_revision(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content(" 保留题干空格 ", min=0, max=10)), 1)
    survey = await draft(context, owner, definition(first))
    before = await snapshot(context)
    changed_title = definition(first)
    changed_title["questions"][0]["title"] = "保留题干空格"
    changed_bound = definition(first)
    changed_bound["questions"][0]["config"]["min"] = 1
    invalid_default = definition(first)
    invalid_default["questions"][0]["config"]["integerOnly"] = 0
    for value in (changed_title, changed_bound, invalid_default):
        response = await save(context, owner, survey, value, [source(first)])
        rejected(response, "INVALID_QUESTION_SOURCE", 422)
        assert any(error.get("field") for error in response.json()["fieldErrors"])
        await unchanged(context, before)
    result = await save(context, owner, survey, definition(first), [source(first)])
    assert result.status_code == 200 and result.json()["draftRevision"] == survey["draftRevision"] + 1


async def test_option_order_and_labels_are_part_of_pinned_content(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, {"kind": "single", "title": "有序选项",
        "config": {"options": [{"id": "a", "label": "甲"}, {"id": "b", "label": "乙"}]}}), 1)
    survey = await draft(context, owner, definition(first))
    before = await snapshot(context)
    reversed_options = definition(first)
    reversed_options["questions"][0]["config"]["options"].reverse()
    changed_label = definition(first)
    changed_label["questions"][0]["config"]["options"][0]["label"] = "甲 "
    for value in (reversed_options, changed_label):
        rejected(await save(context, owner, survey, value, [source(first)]), "INVALID_QUESTION_SOURCE", 422)
        await unchanged(context, before)


async def test_question_ownership_and_version_membership_are_checked_before_binding(context):
    owner, _ = await account(context)
    other, _ = await account(context, "foreign_bank_owner")
    first = accepted(await create_question(context, owner), 1)
    another = accepted(await create_question(context, owner), 1)
    foreign = accepted(await create_question(context, other), 1)
    survey = await draft(context, owner, definition(first))
    before = await snapshot(context)
    for mapping, code in (
        (source(foreign), "NOT_FOUND"),
        ({**source(first), "questionId": str(uuid4())}, "NOT_FOUND"),
        ({**source(first), "questionVersionId": another["id"]}, "QUESTION_VERSION_NOT_FOUND"),
        ({**source(first), "questionVersionId": str(uuid4())}, "QUESTION_VERSION_NOT_FOUND"),
    ):
        rejected(await save(context, owner, survey, definition(first), [mapping]), code, 404)
        await unchanged(context, before)


async def test_duplicate_dangling_and_illegal_instance_mappings_are_rejected_with_locations(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    survey = await draft(context, owner, definition(first))
    before = await snapshot(context)
    for mappings in (
        [source(first), source(first)],
        [source(first, "missing")],
        [source(first, "END")],
        [source(first, "contains space")],
    ):
        response = await save(context, owner, survey, definition(first), mappings)
        rejected(response, "INVALID_QUESTION_SOURCE", 422)
        assert any("questionSources" in str(error.get("field", "")) for error in response.json()["fieldErrors"])
        await unchanged(context, before)


async def test_sources_request_shape_is_strict_and_bounded(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    survey = await draft(context, owner, definition(first))
    before = await snapshot(context)
    for mappings in (
        None,
        {},
        [source(first)] * 101,
        [{**source(first), "extra": "ignored?"}],
        [{**source(first), "questionId": 123}],
        [{**source(first), "questionVersionId": ""}],
        [{"instanceId": "score", "questionId": first["questionId"]}],
    ):
        rejected(await save(context, owner, survey, definition(first), mappings), "INVALID_REQUEST", 422)
        await unchanged(context, before)


async def test_bound_draft_requires_explicit_sources_and_explicit_detach_preserves_submitted_content(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("题库原文", min=0)), 1)
    survey = await draft(context, owner, definition(first), [source(first)])
    original = await publish(context, owner, survey)
    assert original.status_code == 201, original.text
    frozen = original.json()
    survey = await read_survey(context, owner, survey)
    before = await snapshot(context)
    changed = definition(first)
    changed["questions"][0]["title"] = "已明确解除来源的本地题"
    rejected(await save(context, owner, survey, changed), "QUESTION_SOURCES_REQUIRED")
    await unchanged(context, before)
    result = await save(context, owner, survey, changed, [])
    assert result.status_code == 200, result.text
    detached = result.json()
    assert detached["draft"] == changed and "draftQuestionSources" not in detached
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert "draftQuestionSources" not in stored
    # Once deliberately detached, old clients regain the unchanged old save contract.
    result = await save(context, owner, detached, changed)
    assert result.status_code == 200 and "draftQuestionSources" not in result.json()
    version = await publish(context, owner, result.json())
    assert version.status_code == 201 and "questionSources" not in version.json()
    old = await context.client.get(f'/api/surveys/{survey["id"]}/versions/{frozen["id"]}', headers=owner)
    assert old.status_code == 200 and old.json() == frozen


async def test_removing_a_bound_instance_requires_updating_the_mapping_in_same_save(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    value = definition(first, "bound")
    value["questions"][0]["defaultTarget"] = "local"
    value["questions"].append({"id": "local", **number_content("本地题"), "required": False,
                              "defaultTarget": "END", "rules": []})
    survey = await draft(context, owner, value, [source(first, "bound")])
    removed = deepcopy(value)
    removed["questions"] = removed["questions"][1:]
    removed["entryQuestionId"] = "local"
    before = await snapshot(context)
    rejected(await save(context, owner, survey, removed, [source(first, "bound")]), "INVALID_QUESTION_SOURCE", 422)
    await unchanged(context, before)
    result = await save(context, owner, survey, removed, [])
    assert result.status_code == 200 and result.json()["draft"] == removed
    assert "draftQuestionSources" not in result.json()


async def test_competing_draft_saves_keep_winning_definition_and_sources_together(context, monkeypatch):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("候选一", min=0)), 1)
    second = accepted(await derive(context, owner, first, number_content("候选二", min=1)), 2)
    survey = await draft(context, owner, definition(first))
    arrived, release = asyncio.Event(), asyncio.Event()
    entrants = 0
    original_save = Repository.save_draft

    async def wait_for_both(self, *args, **kwargs):
        nonlocal entrants
        if args[0] == survey["id"]:
            entrants += 1
            if entrants == 2:
                arrived.set()
            await release.wait()
        return await original_save(self, *args, **kwargs)

    candidates = [(definition(first), [source(first)]), (definition(second), [source(second)])]
    with monkeypatch.context() as patch:
        patch.setattr(Repository, "save_draft", wait_for_both)
        tasks = [asyncio.create_task(save(context, owner, survey, value, mappings)) for value, mappings in candidates]
        try:
            await asyncio.wait_for(arrived.wait(), 10)
            before_release = await context.db.surveys.find_one({"_id": survey["id"]})
            assert before_release["draftRevision"] == survey["draftRevision"]
            assert "draftQuestionSources" not in before_release
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(index for index, result in enumerate(results) if result.status_code == 200)
    loser = 1 - winner
    rejected(results[loser], "DRAFT_CONFLICT")
    current = await context.db.surveys.find_one({"_id": survey["id"]})
    assert current["draftRevision"] == survey["draftRevision"] + 1
    assert current["draft"] == candidates[winner][0]
    assert current["draftQuestionSources"] == candidates[winner][1]


async def test_publish_revalidates_bypassed_source_metadata_and_leaves_no_orphan(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("第一版", min=0)), 1)
    second = accepted(await derive(context, owner, first, number_content("第二版", min=1)), 2)
    survey = await draft(context, owner, definition(first), [source(first)])
    for mapping, code, status in (
        (source(second), "INVALID_QUESTION_SOURCE", 422),
        ({**source(first), "questionVersionId": str(uuid4())}, "QUESTION_VERSION_NOT_FOUND", 404),
    ):
        await context.db.surveys.update_one({"_id": survey["id"]}, {"$set": {"draftQuestionSources": [mapping]}})
        before = await snapshot(context)
        rejected(await publish(context, owner, survey), code, status)
        await unchanged(context, before)
        assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 0
    await context.db.surveys.update_one({"_id": survey["id"]}, {"$set": {"draftQuestionSources": [source(first)]}})
    valid = await publish(context, owner, survey)
    assert valid.status_code == 201 and valid.json()["questionSources"] == [source(first)]


async def test_publish_failure_rolls_back_sources_and_successful_key_replays_original_snapshot(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("事务原版", min=0)), 1)
    survey = await draft(context, owner, definition(first), [source(first)])
    key = str(uuid4())
    before = await snapshot(context)
    observations = []

    async def fail_after_insert(document):
        observations.append(deepcopy(document["questionSources"]))
        raise ServiceError("INJECTED_FAILURE", "来源发布事务测试故障", 503)

    context.app.state.hooks["after_version_insert"] = fail_after_insert
    try:
        rejected(await publish(context, owner, survey, key=key), "INJECTED_FAILURE", 503)
    finally:
        context.app.state.hooks.pop("after_version_insert", None)
    assert observations == [[source(first)]]
    await unchanged(context, before)
    successful = await publish(context, owner, survey, key=key)
    assert successful.status_code == 201, successful.text
    assert successful.headers["Idempotent-Replay"] == "false"
    original = successful.json()
    assert original["questionSources"] == [source(first)]
    second = accepted(await derive(context, owner, first, number_content("事务新版", min=1)), 2)
    current = await read_survey(context, owner, survey)
    changed = await save(context, owner, current, definition(second), [source(second)])
    assert changed.status_code == 200, changed.text
    newer = await publish(context, owner, changed.json())
    assert newer.status_code == 201 and newer.json()["questionSources"] == [source(second)]
    before_replay = await snapshot(context)
    replay = await publish(context, owner, survey, key=key)
    assert replay.status_code == 200 and replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == original
    await unchanged(context, before_replay)


async def test_old_format_records_and_old_api_shape_survive_repeated_database_initialization(context):
    owner, _ = await account(context)
    survey, version = await published(context, owner, example_definition(), multiple=True)
    attempt = await start(context, owner, survey)
    result = await submit(context, owner, survey, attempt, {"q_age": 17, "q_hobby": ["read"], "q_score": 0})
    assert result.status_code == 201, result.text
    old = await snapshot(context, COLLECTIONS[:4])
    assert not keys_in(old).intersection({"draftQuestionSources", "questionSources", "questionVersionId"})
    first = accepted(await create_question(context, owner), 1)
    accepted(await derive(context, owner, first), 2)
    await unchanged(context, old, COLLECTIONS[:4])
    all_before = await snapshot(context)
    await init_database(context.db)
    await init_database(context.db)
    await unchanged(context, all_before)
    detail = await read_survey(context, owner, survey)
    assert set(detail) == set(survey) and "draftQuestionSources" not in detail
    old_version = await context.client.get(f'/api/surveys/{survey["id"]}/versions/{version["id"]}', headers=owner)
    assert old_version.status_code == 200 and old_version.json() == version
    changed = deepcopy(survey["draft"])
    changed["title"] = "旧客户端仍可保存"
    result = await save(context, owner, survey, changed)
    assert result.status_code == 200 and "draftQuestionSources" not in result.json()
    new_version = await publish(context, owner, result.json())
    assert new_version.status_code == 201 and "questionSources" not in new_version.json()
