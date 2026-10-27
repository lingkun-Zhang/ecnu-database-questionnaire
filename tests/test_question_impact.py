"""Author-only saved-reference impact against real isolated MongoDB/API data.

The labelled orphan/corrupt-source seeds only test defensive read filtering.
The snapshot test delays the real aggregate while a real save commits; neither
storage nor the aggregate's successful result is mocked.
"""
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest

from app.database import VALIDATORS
from conftest import account, start, submit
from test_question_sharing import parties, share
from test_question_sources import definition, draft, keys_in, publish, read_survey, save, snapshot, source, unchanged
from test_question_versions import accepted, create_question, derive, number_content, rejected


COUNTS = {"surveyCount", "draftSurveyCount", "draftInstanceCount", "publishedSurveyCount",
          "currentPublishedSurveyCount", "currentPublishedInstanceCount", "historicalSnapshotCount",
          "historicalInstanceCount"}
ITEM_COUNTS = {"draftInstanceCount", "currentPublishedInstanceCount", "historicalSnapshotCount", "historicalInstanceCount"}
COMMON_ITEM = {"reference", "ownership"} | ITEM_COUNTS
OLD_COLLECTIONS = tuple(VALIDATORS)


def totals(surveys=0, drafts=0, draft_instances=0, publications=0, current=0, current_instances=0, historical=0, historical_instances=0):
    return dict(zip(("surveyCount", "draftSurveyCount", "draftInstanceCount", "publishedSurveyCount",
                     "currentPublishedSurveyCount", "currentPublishedInstanceCount", "historicalSnapshotCount", "historicalInstanceCount"),
                    (surveys, drafts, draft_instances, publications, current, current_instances, historical, historical_instances)))


async def impact(context, headers, question, **params):
    return await context.client.get(f'/api/questions/{question["questionId"]}/impact', headers=headers, params=params)


def result(response, question, version=None, offset=0, limit=20):
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"questionId", "questionVersionId", "checkedAt", "scope", "summary", "items", "offset", "limit", "nextOffset"}
    assert (body["questionId"], body["questionVersionId"], body["scope"], body["offset"], body["limit"]) == (
        question["questionId"], version, "saved_references", offset, limit)
    assert datetime.fromisoformat(body["checkedAt"].replace("Z", "+00:00")).utcoffset() is not None
    assert set(body["summary"]) == {"own", "others"}
    assert all(set(row) == COUNTS and all(isinstance(v, int) and v >= 0 for v in row.values()) for row in body["summary"].values())
    for item in body["items"]:
        assert item["reference"].startswith("问卷引用 ") and len(item["reference"].removeprefix("问卷引用 ")) == 20
        assert item["ownership"] in ("own", "other")
        assert set(item) == COMMON_ITEM | ({"surveyId", "surveyTitle", "surveyStatus"} if item["ownership"] == "own" else set())
    return body


def doubled(version, title="同卷两个实例"):
    value = definition(version, "one", title)
    value["questions"][0]["defaultTarget"] = "two"
    value["questions"].append({"id": "two", **deepcopy(version["content"]), "required": False, "defaultTarget": "END", "rules": []})
    return value, [source(version, "one"), source(version, "two")]


async def frozen(context, headers, survey):
    response = await publish(context, headers, survey)
    assert response.status_code == 201, response.text
    return response.json()


async def fixture(context):
    owner, author, receiver, reader, first, _ = await parties(context)
    value, sources = doubled(first, "作者A")
    a = await draft(context, owner, value, sources)
    a1 = await frozen(context, owner, a)
    a = await read_survey(context, owner, a)
    second = accepted(await derive(context, owner, first, number_content("第二版", min=1, max=10)), 2)
    saved = await save(context, owner, a, definition(second, "score", "作者A的新草稿标题"), [source(second)])
    assert saved.status_code == 200, saved.text
    a2 = await frozen(context, owner, saved.json())
    a = await read_survey(context, owner, a)
    detached = await save(context, owner, a, a["draft"], [])
    assert detached.status_code == 200, detached.text
    a = detached.json()
    # B genuinely receives and publishes this author's question before sharing
    # is revoked. Its closed current publication remains current, not history.
    b = await draft(context, receiver, definition(first, "score", "接收者私有标题"), [source(first)])
    b1 = await frozen(context, receiver, b)
    b = await read_survey(context, receiver, b)
    value, sources = doubled(first, "接收者不能泄露的草稿标题")
    saved = await save(context, receiver, b, value, sources)
    assert saved.status_code == 200, saved.text
    b2 = await frozen(context, receiver, saved.json())
    b = await read_survey(context, receiver, b)
    closed = await context.client.post(f'/api/surveys/{b["id"]}/close', headers=receiver,
                                      json={"expectedSettingsRevision": b["settingsRevision"]})
    assert closed.status_code == 200, closed.text
    b = await read_survey(context, receiver, b)
    c = await draft(context, owner, definition(first, "score", "仅有草稿C"), [source(first)])
    d = await draft(context, owner, definition(first, "score", "历史引用D"), [source(first)])
    d1 = await frozen(context, owner, d)
    d = await read_survey(context, owner, d)
    detached = await save(context, owner, d, d["draft"], [])
    assert detached.status_code == 200, detached.text
    d2 = await frozen(context, owner, detached.json())
    return owner, author, receiver, reader, first, second, a, b, c, d, (a1, a2, b1, b2, d1, d2)


async def test_impact_counts_saved_surveys_instances_current_and_historical_snapshots(context):
    owner, _, _, _, first, _, a, b, c, d, _ = await fixture(context)
    before = await snapshot(context, OLD_COLLECTIONS)
    body = result(await impact(context, owner, first), first)
    assert body["summary"] == {"own": totals(3, 1, 1, 2, 1, 1, 2, 3), "others": totals(1, 1, 2, 1, 1, 2, 1, 1)}
    own = {item["surveyId"]: item for item in body["items"] if item["ownership"] == "own"}
    assert set(own) == {a["id"], c["id"], d["id"]}
    assert own[a["id"]]["surveyTitle"] == a["draft"]["title"]
    assert {field: own[a["id"]][field] for field in ITEM_COUNTS} == dict(draftInstanceCount=0, currentPublishedInstanceCount=1, historicalSnapshotCount=1, historicalInstanceCount=2)
    other = [row for row in body["items"] if row["ownership"] == "other"]
    assert len(other) == 1 and other[0]["currentPublishedInstanceCount"] == 2 and other[0]["historicalSnapshotCount"] == 1
    assert b["status"] == "closed"
    await unchanged(context, before, OLD_COLLECTIONS)


async def test_version_filter_keeps_identity_and_aliases_without_mixing_current_and_history(context):
    owner, _, _, _, first, second, a, _, _, _, _ = await fixture(context)
    all_rows = result(await impact(context, owner, first), first)
    old = result(await impact(context, owner, first, questionVersionId=first["id"]), first, first["id"])
    new = result(await impact(context, owner, first, questionVersionId=second["id"]), first, second["id"])
    assert old["summary"] == {"own": totals(3, 1, 1, 2, 0, 0, 2, 3), "others": totals(1, 1, 2, 1, 1, 2, 1, 1)}
    assert new["summary"] == {"own": totals(1, 0, 0, 1, 1, 1, 0, 0), "others": totals()}
    assert [row["surveyId"] for row in new["items"]] == [a["id"]]
    aliases = {row.get("surveyId", "other"): row["reference"] for row in all_rows["items"]}
    for row in old["items"] + new["items"]:
        assert row["reference"] == aliases[row.get("surveyId", "other")]


async def test_foreign_metadata_never_contains_titles_ids_answers_or_authorization_fields(context):
    owner, author, receiver, reader, first, _ = await parties(context)
    foreign = await draft(context, receiver, definition(first, "private_instance", "机密的接收者问卷标题"), [source(first, "private_instance")])
    version = await frozen(context, receiver, foreign)
    foreign = await read_survey(context, receiver, foreign)
    attempt = await start(context, receiver, foreign)
    submitted = await submit(context, receiver, foreign, attempt, {"private_instance": 6}, anonymous=False)
    assert submitted.status_code == 201, submitted.text
    body = result(await impact(context, owner, first), first)
    assert len(body["items"]) == 1 and body["items"][0]["ownership"] == "other"
    assert not keys_in(body).intersection({"surveyId", "surveyTitle", "surveyStatus", "ownerId", "recipientId", "publicId", "instanceId", "answers", "value", "participantKey", "respondentName", "grantId", "permission", "passwordHash", "username"})
    for private in (foreign["id"], foreign["publicId"], foreign["draft"]["title"], version["id"], author["id"], reader["id"], reader["username"], "private_instance", submitted.json()["receiptId"]):
        assert private not in str(body)
    assert body["summary"]["others"]["surveyCount"] == 1


async def test_revoking_use_and_statistics_grants_does_not_erase_saved_reference_impact(context):
    owner, _, receiver, _, first, _ = await parties(context)
    foreign = await draft(context, receiver, definition(first), [source(first)])
    await frozen(context, receiver, foreign)
    stat_path = f'/api/surveys/{foreign["id"]}/question-statistics-grants/{first["questionId"]}'
    stat = await context.client.put(stat_path, headers=receiver, json={"active": True, "expectedRevision": 0})
    assert stat.status_code == 200, stat.text
    before = result(await impact(context, owner, first), first)
    revoked = await share(context, owner, first, active=False, revision=1)
    assert revoked.status_code == 200, revoked.text
    stat = await context.client.put(stat_path, headers=receiver, json={"active": False, "expectedRevision": 1})
    assert stat.status_code == 200, stat.text
    after = result(await impact(context, owner, first), first)
    assert after["summary"] == before["summary"] and after["items"] == before["items"]
    ordinary = await context.client.get(f'/api/questions/{first["questionId"]}/usages', headers=receiver)
    assert ordinary.status_code == 404
    current = await read_survey(context, receiver, foreign)
    detached = await save(context, receiver, current, current["draft"], [])
    assert detached.status_code == 200, detached.text
    body = result(await impact(context, owner, first), first)
    assert body["summary"]["others"] == totals(1, 0, 0, 1, 1, 1, 0, 0)


async def test_only_author_can_read_impact_and_wrong_versions_do_not_probe_other_questions(context):
    owner, _, receiver, _, first, _ = await parties(context)
    stranger, _ = await account(context, "impact_stranger")
    another = accepted(await create_question(context, owner), 1)
    before = await snapshot(context, OLD_COLLECTIONS)
    unauthenticated = await impact(context, {}, first)
    assert unauthenticated.status_code == 401
    rejected(await impact(context, {"Authorization": "Bearer invalid"}, first), "UNAUTHENTICATED", 401)
    for caller in (receiver, stranger):
        rejected(await impact(context, caller, first), "NOT_FOUND", 404)
        rejected(await impact(context, caller, first, questionVersionId=another["id"]), "NOT_FOUND", 404)
    for version_id in (another["id"], str(uuid4())):
        rejected(await impact(context, owner, first, questionVersionId=version_id), "QUESTION_VERSION_NOT_FOUND", 404)
    rejected(await impact(context, owner, {"questionId": str(uuid4())}), "NOT_FOUND", 404)
    await unchanged(context, before, OLD_COLLECTIONS)


@pytest.mark.parametrize("params", [{"offset": -1}, {"offset": 9007199254740992}, {"offset": "1.5"},
    {"limit": 0}, {"limit": 101}, {"limit": "bad"}, {"questionVersionId": ""}, {"questionVersionId": "x" * 161}])
async def test_impact_rejects_invalid_request_bounds_without_business_writes(context, params):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    before = await snapshot(context, OLD_COLLECTIONS)
    rejected(await impact(context, owner, first, **params), "INVALID_REQUEST", 422)
    await unchanged(context, before, OLD_COLLECTIONS)


async def test_empty_reference_scope_and_offset_pages_have_whole_scope_totals(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    empty = result(await impact(context, owner, first), first)
    assert empty["summary"] == {"own": totals(), "others": totals()} and empty["items"] == [] and empty["nextOffset"] is None
    surveys = []
    for i in range(5):
        survey = await draft(context, owner, definition(first, title=f"影响分页{i}"), [source(first)])
        surveys.append(survey["id"])
    surveys.sort()
    seen = []
    for offset in (0, 2, 4):
        body = result(await impact(context, owner, first, offset=offset, limit=2), first, offset=offset, limit=2)
        assert body["summary"] == {"own": totals(5, 5, 5), "others": totals()}
        assert [row["surveyId"] for row in body["items"]] == surveys[offset:offset+2]
        assert body["nextOffset"] == (offset+2 if offset < 4 else None)
        seen.extend(row["reference"] for row in body["items"])
    assert len(seen) == len(set(seen)) == 5
    far = result(await impact(context, owner, first, offset=9007199254740991, limit=100), first, offset=9007199254740991, limit=100)
    assert far["items"] == [] and far["nextOffset"] is None and far["summary"]["own"]["surveyCount"] == 5


async def test_legacy_equal_wording_orphan_publications_and_detached_drafts_are_excluded(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    same = accepted(await create_question(context, owner, first["content"]), 1)
    legacy = await draft(context, owner, definition(first))
    await frozen(context, owner, legacy)
    other = await draft(context, owner, definition(same), [source(same)])
    await frozen(context, owner, other)
    bound = await draft(context, owner, definition(first), [source(first)])
    version = await frozen(context, owner, bound)
    row = await context.db.survey_versions.find_one({"_id": version["id"]})
    # Deliberate orphan seed: the structural validator does not enforce an FK.
    row.update(_id=str(uuid4()), surveyId="missing-impact-survey", versionNo=1, publishKey=str(uuid4()))
    await context.db.survey_versions.insert_one(row)
    bound = await read_survey(context, owner, bound)
    changed = await save(context, owner, bound, bound["draft"], [])
    assert changed.status_code == 200, changed.text
    await frozen(context, owner, changed.json())
    body = result(await impact(context, owner, first), first)
    assert body["summary"] == {"own": totals(1, 0, 0, 1, 0, 0, 1, 1), "others": totals()}
    assert [row["surveyId"] for row in body["items"]] == [bound["id"]]


async def test_question_and_version_must_match_the_same_source_element(context):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    second = accepted(await derive(context, owner, first), 2)
    other = accepted(await create_question(context, owner), 1)
    value = definition(first, "target")
    value["questions"][0]["defaultTarget"] = "unrelated"
    value["questions"].append({"id": "unrelated", **deepcopy(other["content"]), "required": False, "defaultTarget": "END", "rules": []})
    survey = await draft(context, owner, value, [source(first, "target"), source(other, "unrelated")])
    version = await frozen(context, owner, survey)
    # Invalid DB-only pair deliberately bypasses API provenance checks.
    sources = [source(first, "target"), {**source(other, "unrelated"), "questionVersionId": second["id"]}]
    await context.db.surveys.update_one({"_id": survey["id"]}, {"$set": {"draftQuestionSources": sources}})
    await context.db.survey_versions.update_one({"_id": version["id"]}, {"$set": {"questionSources": sources}})
    body = result(await impact(context, owner, first, questionVersionId=second["id"]), first, second["id"])
    assert body["summary"] == {"own": totals(), "others": totals()} and body["items"] == []


async def test_aliases_are_stable_per_question_and_do_not_reuse_participant_identifiers(context):
    owner, user = await account(context)
    first = accepted(await create_question(context, owner), 1)
    other = accepted(await create_question(context, owner), 1)
    value = definition(first, "first")
    value["questions"][0]["defaultTarget"] = "other"
    value["questions"].append({"id": "other", **deepcopy(other["content"]), "required": False, "defaultTarget": "END", "rules": []})
    survey = await draft(context, owner, value, [source(first, "first"), source(other, "other")])
    await frozen(context, owner, survey)
    one = result(await impact(context, owner, first), first)["items"][0]["reference"]
    again = result(await impact(context, owner, first), first)["items"][0]["reference"]
    two = result(await impact(context, owner, other), other)["items"][0]["reference"]
    assert one == again and one != two and survey["id"] not in one
    from app.security import participant_key
    assert one.removeprefix("问卷引用 ") != participant_key(context.settings.participant_secret, survey["id"], user["id"])[:20]


async def test_summary_and_page_share_snapshot_while_another_saved_reference_commits(context, monkeypatch):
    owner, _ = await account(context)
    first = accepted(await create_question(context, owner), 1)
    existing = await draft(context, owner, definition(first, title="已保存引用"), [source(first)])
    pending = await draft(context, owner, definition(first, title="稍后保存引用"))
    collection_class = type(context.db.surveys)
    original = collection_class.aggregate
    states = []
    changed = False
    async def aggregate(collection, *args, **kwargs):
        nonlocal changed
        if collection.name == "surveys":
            states.append(kwargs.get("session") is not None and kwargs["session"].in_transaction)
            if not changed:
                changed = True
                saved = await save(context, owner, pending, pending["draft"], [source(first)])
                assert saved.status_code == 200, saved.text
        return await original(collection, *args, **kwargs)
    monkeypatch.setattr(collection_class, "aggregate", aggregate)
    before = result(await impact(context, owner, first), first)
    assert before["summary"]["own"] == totals(1, 1, 1)
    assert [row["surveyId"] for row in before["items"]] == [existing["id"]]
    after = result(await impact(context, owner, first), first)
    assert after["summary"]["own"] == totals(2, 2, 2) and len(after["items"]) == 2
    assert states == [True, True]
