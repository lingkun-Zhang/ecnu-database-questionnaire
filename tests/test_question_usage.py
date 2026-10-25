"""Private question usage queries against real, isolated MongoDB documents.

The API-created cases retain the publication and ownership rules. Explicit
database seeds below are labelled: they test filtering before pagination and
same-array-element matching even if an invalid cross-owner reference exists.
No successful storage or query operation is mocked.
"""

from copy import deepcopy
from uuid import uuid4

import pytest

from conftest import account, start, submit
from test_question_sources import (
    definition, draft, keys_in, publish, read_survey, save, snapshot, source, unchanged,
)
from test_question_versions import accepted, create_question, derive, number_content, rejected


DRAFT_FIELDS = {"surveyId", "surveyTitle", "surveyStatus", "draftRevision", "instances"}
PUBLISHED_FIELDS = {"surveyId", "surveyTitle", "surveyStatus", "surveyVersionId", "versionNo",
                    "publishedAt", "isCurrent", "instances"}


async def usage(context, owner, version, **params):
    return await context.client.get(f'/api/questions/{version["questionId"]}/usages',
                                    headers=owner, params=params)


def result(response, version, scope="draft", question_version=None, limit=30):
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"questionId", "questionVersionId", "scope", "limit", "items", "nextAfterId"}
    assert (body["questionId"], body["questionVersionId"], body["scope"], body["limit"]) == (
        version["questionId"], question_version, scope, limit)
    fields = DRAFT_FIELDS if scope == "draft" else PUBLISHED_FIELDS
    for item in body["items"]:
        assert set(item) == fields
        assert item["instances"]
        assert all(set(instance) == {"instanceId", "questionVersionId"} for instance in item["instances"])
    return body


async def frozen(context, owner, survey):
    response = await publish(context, owner, survey)
    assert response.status_code == 201, response.text
    return response.json()


async def test_question_usages_distinguish_drafts_current_and_historical_snapshots(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner, number_content("同一道评分题", min=0)), 1)
    a = await draft(context, owner, definition(first, "a_score", "A原发布标题"), [source(first, "a_score")], multiple=True)
    b = await draft(context, owner, definition(first, "b_score", "B发布标题"), [source(first, "b_score")])
    a1, b1 = await frozen(context, owner, a), await frozen(context, owner, b)
    a = await read_survey(context, owner, a)
    attempt = await start(context, owner, a)
    response = await submit(context, owner, a, attempt, {"a_score": 0})
    assert response.status_code == 201, response.text

    second = accepted(await derive(context, owner, first, number_content("新版评分题", min=1)), 2)
    changed = await save(context, owner, a, definition(second, "a_score", "A第二版标题"), [source(second, "a_score")])
    assert changed.status_code == 200, changed.text
    a2 = await frozen(context, owner, changed.json())
    a = await read_survey(context, owner, a)
    renamed = deepcopy(a["draft"])
    renamed["title"] = "A后来修改的草稿标题"
    changed = await save(context, owner, a, renamed, [source(second, "a_score")])
    assert changed.status_code == 200, changed.text
    a = changed.json()
    closed = await context.client.post(f'/api/surveys/{a["id"]}/close', headers=owner,
        json={"expectedSettingsRevision": a["settingsRevision"]})
    assert closed.status_code == 200, closed.text

    before = await snapshot(context)
    drafts = result(await usage(context, owner, first), first)
    assert [row["surveyId"] for row in drafts["items"]] == sorted([a["id"], b["id"]])
    by_survey = {row["surveyId"]: row for row in drafts["items"]}
    assert by_survey[a["id"]] == {
        "surveyId": a["id"], "surveyTitle": renamed["title"], "surveyStatus": "closed",
        "draftRevision": a["draftRevision"],
        "instances": [{"instanceId": "a_score", "questionVersionId": second["id"]}],
    }
    assert by_survey[b["id"]]["instances"] == [{"instanceId": "b_score", "questionVersionId": first["id"]}]
    assert drafts["nextAfterId"] is None

    publications = result(await usage(context, owner, first, scope="published"), first, "published")
    assert [row["surveyVersionId"] for row in publications["items"]] == sorted([a1["id"], a2["id"], b1["id"]])
    rows = {row["surveyVersionId"]: row for row in publications["items"]}
    for version, survey_id, title, status, current, instance, question_version in (
        (a1, a["id"], "A原发布标题", "closed", False, "a_score", first["id"]),
        (a2, a["id"], "A第二版标题", "closed", True, "a_score", second["id"]),
        (b1, b["id"], "B发布标题", "open", True, "b_score", first["id"]),
    ):
        assert rows[version["id"]] == {
            "surveyId": survey_id, "surveyTitle": title, "surveyStatus": status,
            "surveyVersionId": version["id"], "versionNo": version["versionNo"],
            "publishedAt": version["publishedAt"], "isCurrent": current,
            "instances": [{"instanceId": instance, "questionVersionId": question_version}],
        }
    assert not keys_in(publications).intersection({"ownerId", "publicId", "definition", "answers", "identity", "participantKey", "publishKey"})
    await unchanged(context, before)


async def test_version_filter_groups_local_instances_without_counting_each_as_a_survey(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    value = definition(first, "old_one", "同卷多实例")
    value["questions"][0]["defaultTarget"] = "new_one"
    value["questions"].append({"id": "new_one", **deepcopy(second["content"]), "required": False,
                               "defaultTarget": "old_two", "rules": []})
    value["questions"].append({"id": "old_two", **deepcopy(first["content"]), "required": False,
                               "defaultTarget": "END", "rules": []})
    survey = await draft(context, owner, value, [source(first, "old_one"), source(second, "new_one"), source(first, "old_two")])
    version = await frozen(context, owner, survey)
    for scope in ("draft", "published"):
        combined = result(await usage(context, owner, first, scope=scope), first, scope)
        assert len(combined["items"]) == 1
        assert combined["items"][0]["instances"] == [
            {"instanceId": "old_one", "questionVersionId": first["id"]},
            {"instanceId": "new_one", "questionVersionId": second["id"]},
            {"instanceId": "old_two", "questionVersionId": first["id"]},
        ]
        old = result(await usage(context, owner, first, scope=scope, questionVersionId=first["id"]),
                     first, scope, first["id"])
        assert len(old["items"]) == 1
        assert old["items"][0]["instances"] == [
            {"instanceId": "old_one", "questionVersionId": first["id"]},
            {"instanceId": "old_two", "questionVersionId": first["id"]},
        ]
        new = result(await usage(context, owner, first, scope=scope, questionVersionId=second["id"]),
                     first, scope, second["id"])
        assert len(new["items"]) == 1
        assert new["items"][0]["instances"] == [{"instanceId": "new_one", "questionVersionId": second["id"]}]
        if scope == "published":
            assert new["items"][0]["surveyVersionId"] == version["id"]


async def test_detaching_draft_removes_live_usage_but_keeps_published_usage(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    survey = await draft(context, owner, definition(first), [source(first)])
    version = await frozen(context, owner, survey)
    survey = await read_survey(context, owner, survey)
    detached = await save(context, owner, survey, survey["draft"], [])
    assert detached.status_code == 200, detached.text
    draft_rows = result(await usage(context, owner, first), first)
    assert draft_rows["items"] == [] and draft_rows["nextAfterId"] is None
    published_rows = result(await usage(context, owner, first, scope="published"), first, "published")
    assert [row["surveyVersionId"] for row in published_rows["items"]] == [version["id"]]
    assert published_rows["items"][0]["isCurrent"] is True
    detached_version = await frozen(context, owner, detached.json())
    assert detached_version["id"] != version["id"]
    published_rows = result(await usage(context, owner, first, scope="published"), first, "published")
    assert [row["surveyVersionId"] for row in published_rows["items"]] == [version["id"]]
    assert published_rows["items"][0]["isCurrent"] is False


async def test_equal_wording_and_unbound_legacy_questions_do_not_create_usage(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    same_wording = accepted(await create_question(context, owner, first["content"]), 1)
    ordinary = await draft(context, owner, definition(first, "score", "无来源旧格式"))
    await frozen(context, owner, ordinary)
    different = await draft(context, owner, definition(same_wording, "score", "同字不同题"), [source(same_wording)])
    await frozen(context, owner, different)
    before = await snapshot(context)
    for scope in ("draft", "published"):
        body = result(await usage(context, owner, first, scope=scope), first, scope)
        assert body["items"] == [] and body["nextAfterId"] is None
    await unchanged(context, before)


async def test_creating_another_question_version_does_not_change_existing_usage(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    survey = await draft(context, owner, definition(first), [source(first)])
    await frozen(context, owner, survey)
    old = {scope: result(await usage(context, owner, first, scope=scope), first, scope)
           for scope in ("draft", "published")}
    second = accepted(await derive(context, owner, first), 2)
    for scope in ("draft", "published"):
        assert result(await usage(context, owner, first, scope=scope), first, scope) == old[scope]
        body = result(await usage(context, owner, first, scope=scope, questionVersionId=second["id"]), first, scope, second["id"])
        assert body["items"] == [] and body["nextAfterId"] is None


async def test_usage_requires_question_ownership_and_valid_version_membership(context):
    owner, _ = await account(context)
    other, _ = await account(context, "other_owner")
    first = accepted(await create_question(context, owner), 1)
    another = accepted(await create_question(context, owner), 1)
    foreign = accepted(await create_question(context, other), 1)
    before = await snapshot(context)
    unauthenticated = await context.client.get(f'/api/questions/{first["questionId"]}/usages')
    assert unauthenticated.status_code == 401
    invalid_auth = await usage(context, {"Authorization": "Bearer invalid"}, first)
    assert invalid_auth.status_code == 401
    rejected(await usage(context, other, first), "NOT_FOUND", 404)
    rejected(await usage(context, owner, foreign), "NOT_FOUND", 404)
    rejected(await usage(context, owner, {"questionId": str(uuid4())}), "NOT_FOUND", 404)
    for version_id in (another["id"], foreign["id"], str(uuid4())):
        rejected(await usage(context, owner, first, questionVersionId=version_id), "QUESTION_VERSION_NOT_FOUND", 404)
    await unchanged(context, before)


@pytest.mark.parametrize("params", [
    {"scope": "all"}, {"limit": 0}, {"limit": 101}, {"limit": "not-an-integer"},
    {"questionVersionId": ""}, {"questionVersionId": "x" * 161},
    {"afterId": ""}, {"afterId": "x" * 161},
], ids=["scope", "zero-limit", "over-limit", "non-integer-limit", "empty-version", "long-version", "empty-after", "long-after"])
async def test_usage_request_bounds_are_rejected_without_writes(context, params):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    before = await snapshot(context)
    rejected(await usage(context, owner, first, **params), "INVALID_REQUEST", 422)
    await unchanged(context, before)


@pytest.mark.parametrize("scope", ["draft", "published"])
async def test_keyset_pages_have_unique_containers_and_a_real_end(context, scope):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    expected = []
    for index in range(5):
        survey = await draft(context, owner, definition(first, "score", f"分页问卷{index}"), [source(first)])
        version = await frozen(context, owner, survey)
        expected.append(survey["id"] if scope == "draft" else version["id"])
    expected.sort()
    before = await snapshot(context)
    seen, after = [], None
    for page_index in range(3):
        params = {"scope": scope, "limit": 2}
        if after is not None:
            params["afterId"] = after
        body = result(await usage(context, owner, first, **params), first, scope, limit=2)
        ids = [row["surveyId" if scope == "draft" else "surveyVersionId"] for row in body["items"]]
        assert ids == expected[page_index * 2:(page_index + 1) * 2]
        seen.extend(ids)
        after = body["nextAfterId"]
        assert after == (ids[-1] if page_index < 2 else None)
    assert seen == expected and len(set(seen)) == 5
    beyond = result(await usage(context, owner, first, scope=scope, limit=2, afterId=expected[-1]), first, scope, limit=2)
    assert beyond["items"] == [] and beyond["nextAfterId"] is None
    await unchanged(context, before)


async def test_published_filter_excludes_foreign_and_missing_surveys_before_limit(context):
    owner, _ = await account(context)
    other, _ = await account(context, "hidden_owner")
    first = accepted(await create_question(context, owner), 1)
    foreign_question = accepted(await create_question(context, other), 1)
    local = await draft(context, owner, definition(first, "local"), [source(first, "local")])
    local_version = await frozen(context, owner, local)
    hidden = await draft(context, other, definition(foreign_question, "private", "不可泄露问卷标题"), [source(foreign_question, "private")])
    hidden_version = await frozen(context, other, hidden)
    # Deliberate DB-only seeds bypass source authenticity to test the read-side
    # permission check. Sorting puts both inaccessible versions before the local
    # version, so applying limit before ownership would create a false empty page.
    hidden_seed = await context.db.survey_versions.find_one({"_id": hidden_version["id"]})
    hidden_seed.update({"_id": "000-hidden", "versionNo": 2, "publishKey": str(uuid4()),
                        "questionSources": [source(first, "private")]})
    orphan = deepcopy(hidden_seed)
    orphan.update({"_id": "000-orphan", "surveyId": "missing-survey", "versionNo": 1, "publishKey": str(uuid4())})
    await context.db.survey_versions.insert_many([hidden_seed, orphan])
    await context.db.surveys.update_one({"_id": hidden["id"]}, {"$set": {"draftQuestionSources": [source(first, "private")]}})
    before = await snapshot(context)
    for scope in ("draft", "published"):
        body = result(await usage(context, owner, first, scope=scope, limit=1), first, scope, limit=1)
        assert len(body["items"]) == 1 and body["items"][0]["surveyId"] == local["id"]
        assert body["nextAfterId"] is None
        assert "不可泄露问卷标题" not in str(body) and hidden["id"] not in str(body)
        if scope == "published":
            assert body["items"][0]["surveyVersionId"] == local_version["id"]
    await unchanged(context, before)


async def test_question_and_version_filter_must_match_the_same_source_element(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    other = accepted(await create_question(context, owner), 1)
    value = definition(first, "target")
    value["questions"][0]["defaultTarget"] = "unrelated"
    value["questions"].append({"id": "unrelated", **deepcopy(other["content"]), "required": False,
                               "defaultTarget": "END", "rules": []})
    survey = await draft(context, owner, value, [source(first, "target"), source(other, "unrelated")])
    version = await frozen(context, owner, survey)
    # Structural validators do not establish cross-collection membership. This
    # deliberately invalid second pair must not satisfy a different source's
    # questionId together with the requested questionVersionId.
    sources = [source(first, "target"), {**source(other, "unrelated"), "questionVersionId": second["id"]}]
    await context.db.surveys.update_one({"_id": survey["id"]}, {"$set": {"draftQuestionSources": sources}})
    await context.db.survey_versions.update_one({"_id": version["id"]}, {"$set": {"questionSources": sources}})
    before = await snapshot(context)
    for scope in ("draft", "published"):
        body = result(await usage(context, owner, first, scope=scope, questionVersionId=second["id"]),
                      first, scope, second["id"])
        assert body["items"] == [] and body["nextAfterId"] is None
        whole = result(await usage(context, owner, first, scope=scope), first, scope)
        assert len(whole["items"]) == 1
        assert whole["items"][0]["instances"] == [{"instanceId": "target", "questionVersionId": first["id"]}]
    await unchanged(context, before)
