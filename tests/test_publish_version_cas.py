"""T10: unchanged draft revision cannot bypass the current-version publish CAS.

All HTTP requests, version inserts, transactions and conflicts are real. The
competition pauses Repository.insert_version entry, delegates to the original
method, and restores it after all owned tasks have finished or been cancelled.
"""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from uuid import uuid4

import pytest

from app.repositories import Repository
from conftest import account, published, start, submit
from scripts.fixtures import example_definition


async def snapshot(context):
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


async def same_documents(context, expected, label):
    # Do not let assertion introspection expand password/participant fields.
    equal = await snapshot(context) == expected
    assert equal, f"{label}: persisted business documents changed"


def proof(name, **details):
    print("PUBLISH_CAS_RESULT " + json.dumps({"scenario": name, **details}, ensure_ascii=False))


def conflict(response, code="PUBLISH_CONFLICT"):
    assert response.status_code == 409
    assert response.json()["code"] == code
    assert response.headers.get("Idempotent-Replay") != "true"


def accepted(response, number, revision, replay=False):
    assert response.status_code == (200 if replay else 201)
    assert response.headers["Idempotent-Replay"] == ("true" if replay else "false")
    body = response.json()
    assert set(body) == {"id", "surveyId", "versionNo", "sourceDraftRevision", "definition", "publishedAt"}
    assert body["versionNo"] == number and body["sourceDraftRevision"] == revision
    return body


async def publish(context, headers, survey, expected_current, key, revision=None):
    return await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
        json={"expectedDraftRevision": survey["draftRevision"] if revision is None else revision,
              "expectedCurrentVersionId": expected_current, "publishKey": key})


async def prepared_survey(context, advance):
    headers, _ = await account(context, "publish_cas_owner")
    survey, first = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    response = await submit(context, headers, survey, attempt,
        {"q_age": 17, "q_hobby": ["read"], "q_score": 0}, anonymous=False)
    assert response.status_code == 201
    versions = [first]
    if advance:
        second = accepted(await publish(context, headers, survey, first["id"], str(uuid4())),
                          2, survey["draftRevision"])
        assert second["definition"] == first["definition"]
        versions.append(second)
    fresh = await context.client.get(f'/api/surveys/{survey["id"]}', headers=headers)
    assert fresh.status_code == 200
    survey = fresh.json()
    assert survey["currentVersionId"] == versions[-1]["id"]
    assert survey["draftRevision"] == first["sourceDraftRevision"]
    assert survey["policyLocked"] is True and survey["submitSequence"] == 1
    return headers, survey, versions


async def only_new_version_and_pointer(context, before, survey, body, key):
    after = await snapshot(context)
    stored = await context.db.survey_versions.find_one({"_id": body["id"]})
    assert stored is not None
    assert set(stored) == {"_id", "surveyId", "versionNo", "sourceDraftRevision", "definition",
                           "publishKey", "publishRequestDigest", "publishedAt", "schemaVersion"}
    assert stored["surveyId"] == body["surveyId"] == survey["id"]
    assert stored["versionNo"] == body["versionNo"]
    assert stored["sourceDraftRevision"] == survey["draftRevision"] == body["sourceDraftRevision"]
    assert stored["definition"] == survey["draft"] == body["definition"]
    assert stored["publishKey"] == key and stored["schemaVersion"] == 1
    assert len(stored["publishRequestDigest"]) == 64
    assert stored["publishedAt"] == datetime.fromisoformat(body["publishedAt"])
    current = next(item for item in after["surveys"] if item["_id"] == survey["id"])
    expected = deepcopy(before)
    expected_survey = next(item for item in expected["surveys"] if item["_id"] == survey["id"])
    expected_survey.update(currentVersionId=body["id"], lastVersionNo=body["versionNo"],
                           updatedAt=current["updatedAt"])
    expected["survey_versions"].append(stored)
    expected["survey_versions"].sort(key=lambda item: item["_id"])
    # Whole-document equality also preserves users, the V1 named response,
    # all old/foreign versions, draft/settings revisions, gate and policy state.
    await same_documents(context, expected, "only-new-version-and-current-pointer")
    return after


@pytest.mark.parametrize("wrong_current", ["old-v1", "stale-null", "missing", "foreign-version"])
async def test_current_version_mismatch_alone_rejects_publish_without_writes(context, wrong_current):
    headers, survey, versions = await prepared_survey(context, advance=True)
    if wrong_current == "old-v1":
        expected_current = versions[0]["id"]
    elif wrong_current == "stale-null":
        expected_current = None
    elif wrong_current == "missing":
        expected_current = str(uuid4())
    else:
        definition = example_definition()
        definition["title"] = "独立问卷的版本"
        foreign, foreign_version = await published(context, headers, definition)
        assert foreign["id"] != survey["id"]
        expected_current = foreign_version["id"]
    before = await snapshot(context)
    stored = next(item for item in before["surveys"] if item["_id"] == survey["id"])
    assert stored["draftRevision"] == survey["draftRevision"] == versions[0]["sourceDraftRevision"]
    assert stored["currentVersionId"] == versions[1]["id"] != expected_current
    key = str(uuid4())  # No prior success may turn this into an idempotent replay.
    rejected = await publish(context, headers, survey, expected_current, key)
    conflict(rejected)
    await same_documents(context, before, "current-version-mismatch")
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"], "publishKey": key}) == 0

    # A failed attempt does not reserve its publishKey. Keep the draft/revision,
    # correct only the expected pointer, and the same key can create V3 normally.
    fresh = await publish(context, headers, survey, versions[1]["id"], key)
    third = accepted(fresh, 3, survey["draftRevision"])
    after = await only_new_version_and_pointer(context, before, survey, third, key)
    replay = await publish(context, headers, survey, versions[1]["id"], key)
    assert accepted(replay, 3, survey["draftRevision"], replay=True) == third
    await same_documents(context, after, "original-successful-publish-replay")

    # Once successful, changing the bound expectation is an idempotency
    # conflict, including changing it to the now-current pointer.
    changed = await publish(context, headers, survey, third["id"], key)
    conflict(changed, "IDEMPOTENCY_CONFLICT")
    await same_documents(context, after, "successful-key-changed-expectation")
    proof("current-pointer-only-mismatch", wrongExpectedCurrent=wrong_current,
          draftRevision=survey["draftRevision"], actualVersionBefore=2,
          requestDraftRevisionMatches=True, rejectStatus=rejected.status_code,
          rejectCode=rejected.json()["code"], fourCollectionsUnchangedAfterReject=True,
          failedKeyReserved=False, correctedSameKeyStatus=fresh.status_code, newVersionNo=third["versionNo"],
          onlyNewVersionAndPointerChanged=True, replayStatus=replay.status_code, replayHeader="true",
          changedSuccessfulKeyStatus=changed.status_code, changedSuccessfulKeyCode=changed.json()["code"],
          postSuccessConflictAndReplayUnchanged=True, originalResponseCount=len(before["responses"]))


async def test_competing_publish_keys_from_same_version_preserve_revision_and_no_orphans(context, monkeypatch):
    headers, survey, versions = await prepared_survey(context, advance=False)
    first = versions[0]
    before = await snapshot(context)
    keys = [str(uuid4()), str(uuid4())]
    original_insert = Repository.insert_version
    entered, release = asyncio.Event(), asyncio.Event()
    prepared, insert_attempts, tasks = {}, {key: 0 for key in keys}, []

    async def pause_then_insert(repo, document, session):
        if repo.db.name == context.db.name and document["surveyId"] == survey["id"]:
            key = document["publishKey"]
            assert key in keys
            insert_attempts[key] += 1
            prepared.setdefault(key, {"id": document["_id"], "versionNo": document["versionNo"],
                                      "sourceDraftRevision": document["sourceDraftRevision"]})
            if len(prepared) == 2:
                entered.set()
            await release.wait()
        return await original_insert(repo, document, session)

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "insert_version", pause_then_insert)
        try:
            tasks = [asyncio.create_task(publish(context, headers, survey, first["id"], key)) for key in keys]
            await asyncio.wait_for(entered.wait(), 5)
            assert len(prepared) == 2 and len({item["id"] for item in prepared.values()}) == 2
            assert all(item["versionNo"] == 2 and item["sourceDraftRevision"] == survey["draftRevision"]
                       for item in prepared.values())
            assert all(not task.done() for task in tasks)
            await same_documents(context, before, "both-publish-transactions-ready-before-insert")
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert Repository.insert_version is original_insert
    assert sorted(response.status_code for response in outcomes) == [201, 409]
    winner = next(index for index, response in enumerate(outcomes) if response.status_code == 201)
    loser = 1 - winner
    conflict(outcomes[loser])
    second = accepted(outcomes[winner], 2, survey["draftRevision"])
    after = await only_new_version_and_pointer(context, before, survey, second, keys[winner])
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 2
    assert await context.db.survey_versions.count_documents({"publishKey": keys[loser]}) == 0

    retried_loser = await publish(context, headers, survey, first["id"], keys[loser])
    conflict(retried_loser)
    replay = await publish(context, headers, survey, first["id"], keys[winner])
    assert accepted(replay, 2, survey["draftRevision"], replay=True) == second
    await same_documents(context, after, "loser-retry-and-winner-replay")

    corrected = await publish(context, headers, survey, second["id"], keys[loser])
    third = accepted(corrected, 3, survey["draftRevision"])
    final = await only_new_version_and_pointer(context, after, survey, third, keys[loser])
    current_versions = await context.db.survey_versions.find({"surveyId": survey["id"]}).sort("versionNo", 1).to_list(None)
    assert [version["versionNo"] for version in current_versions] == [1, 2, 3]
    old_replay = await publish(context, headers, survey, first["id"], keys[winner])
    assert accepted(old_replay, 2, survey["draftRevision"], replay=True) == second
    await same_documents(context, final, "old-winner-replay-after-v3")
    proof("competing-current-version-publish", arrivalsBeforeInsert=len(prepared),
          preparedVersionNumbers=[prepared[key]["versionNo"] for key in keys],
          insertEntries=[insert_attempts[key] for key in keys], draftRevision=survey["draftRevision"],
          requestDraftRevisionsMatch=True, fourCollectionsUnchangedBeforeRelease=True,
          requestStatuses=[response.status_code for response in outcomes], winnerIndex=winner,
          loserCode=outcomes[loser].json()["code"], versionCountAfterRace=2, currentVersionNoAfterRace=2,
          draftRevisionUnchanged=True, loserKeyHasNoVersion=True, onlyNewVersionAndPointerChanged=True,
          loserRetryStatus=retried_loser.status_code, winnerReplayStatus=replay.status_code,
          retriesUnchanged=True, correctedLoserKeyStatus=corrected.status_code, finalVersionNumbers=[1, 2, 3],
          oldWinnerReplayAfterV3Status=old_replay.status_code, oldReplayUnchanged=True,
          originalResponseCount=len(before["responses"]), insertWrapperRestored=True)
