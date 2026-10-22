"""T14: committed submission identity, semantic replay and competing payloads.

Requests use the real ASGI application and isolated real MongoDB databases.
No driver or response is mocked. The race uses an explicit pre-transaction
barrier, not sleeps or an assumed task scheduling order.
"""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from app.security import token
from conftest import account, published, start, submit
from scripts.fixtures import example_definition


def original_answers():
    return {"q_age": 17, "q_minor": "  学生  ", "q_hobby": ["read", "sport"],
            "q_score": 0, "q_note": "  原建议  "}


async def snapshot(context):
    """Full business documents, including identity, digest, gates and dates."""
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


async def unchanged(context, before, label):
    # Do not dump account/password or internal participant fields if this fails.
    equal = await snapshot(context) == before
    assert equal, f"{label}: persisted business documents changed"


def conflict(response, label):
    assert response.status_code == 409, (label, response.status_code, response.text)
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT", label
    assert response.headers.get("Idempotent-Replay") != "true", label


def replay(response, first, label):
    assert response.status_code == 200, (label, response.status_code, response.text)
    assert response.headers["Idempotent-Replay"] == "true", label
    equal = response.json() == first.json()
    assert equal, f"{label}: the original receipt changed"


def proof(name, **details):
    # Only synthetic inputs and observed outcomes; never output auth/fill tokens,
    # password hashes, participant keys or configuration values.
    print("SUBMISSION_BOUNDARY_RESULT " + json.dumps({"scenario": name, **details}, ensure_ascii=False))


async def test_changed_active_answers_on_committed_key_conflict_without_writes(context):
    headers, _ = await account(context, "boundary_answer_owner")
    survey, _ = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    key = str(uuid4())
    original = original_answers()
    first = await submit(context, headers, survey, attempt, original, key, anonymous=False)
    assert first.status_code == 201 and first.headers["Idempotent-Replay"] == "false"
    before = await snapshot(context)
    stored = before["responses"][0]
    assert stored["identity"]["usernameAtSubmit"] == "boundary_answer_owner"
    assert before["surveys"][0]["submitSequence"] == 1

    cases = [
        ("different-number", {**original, "q_score": 1}),
        ("different-age-same-branch", {**original, "q_age": 16}),
        ("different-route", {**original, "q_age": 18, "q_adult": "student"}),
        ("different-choice-set", {**original, "q_hobby": ["read", "music"]}),
        ("different-active-text", {**original, "q_minor": "不同活动"}),
        ("remove-answered-optional", {k: v for k, v in original.items() if k != "q_note"}),
        ("missing-required", {k: v for k, v in original.items() if k != "q_age"}),
        ("invalid-active-number", {**original, "q_score": 11}),
        ("invalid-duplicate-choice", {**original, "q_hobby": ["read", "read"]}),
    ]
    observed = []
    for label, changed in cases:
        response = await submit(context, headers, survey, attempt, changed, key, anonymous=False)
        conflict(response, label)
        await unchanged(context, before, label)
        observed.append({"label": label, "status": response.status_code, "code": response.json()["code"]})

    # A new key follows normal answer validation; a committed key must never
    # silently replace the stored answer or turn into another submission.
    invalid_new = await submit(context, headers, survey, attempt,
                               {**original, "q_score": 11}, str(uuid4()), anonymous=False)
    assert invalid_new.status_code == 422 and invalid_new.json()["code"] == "ANSWER_VALIDATION_FAILED"
    await unchanged(context, before, "invalid-new-key")
    replay(await submit(context, headers, survey, attempt, original, key, anonymous=False), first, "original")
    await unchanged(context, before, "original-replay")
    proof("changed-active-answers", rejected=observed, invalidNewKeyStatus=invalid_new.status_code,
          wholeDatabaseUnchanged=True, responseCount=len(before["responses"]),
          submitSequence=before["surveys"][0]["submitSequence"])


async def test_same_key_cannot_switch_to_an_identical_new_version(context):
    headers, _ = await account(context, "boundary_version_owner")
    definition = example_definition()
    survey, first_version = await published(context, headers, definition, multiple=True)
    first_attempt = await start(context, headers, survey)
    answers, key = original_answers(), str(uuid4())
    first = await submit(context, headers, survey, first_attempt, answers, key)
    assert first.status_code == 201
    original_document = await context.db.responses.find_one({"_id": first.json()["receiptId"]})

    # No definition edit: version identity itself is part of submission meaning.
    published_v2 = await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
        json={"expectedDraftRevision": survey["draftRevision"],
              "expectedCurrentVersionId": first_version["id"], "publishKey": str(uuid4())})
    assert published_v2.status_code == 201, published_v2.text
    second_version = published_v2.json()
    assert second_version["id"] != first_version["id"] and second_version["versionNo"] == 2
    assert second_version["definition"] == first_version["definition"] == definition
    second_attempt = await start(context, headers, survey)
    assert second_attempt["versionId"] == second_version["id"]
    before = await snapshot(context)

    conflict(await submit(context, headers, survey, second_attempt, answers, key), "existing-v2")
    await unchanged(context, before, "existing-v2")
    unknown = {"versionId": str(uuid4())}
    conflict(await submit(context, headers, survey, unknown, answers, key), "different-unknown-version")
    await unchanged(context, before, "unknown-version")

    accepted_v2 = await submit(context, headers, survey, second_attempt, answers, str(uuid4()))
    assert accepted_v2.status_code == 201, accepted_v2.text
    assert accepted_v2.json()["versionId"] == second_version["id"]
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 2
    state = await context.db.surveys.find_one({"_id": survey["id"]})
    assert state["submitSequence"] == 2 and state["lastVersionNo"] == 2
    same_original = await context.db.responses.find_one({"_id": original_document["_id"]}) == original_document
    assert same_original, "Accepting V2 changed the V1 response"
    after_v2 = await snapshot(context)
    replay(await submit(context, headers, survey, {"versionId": first_version["id"]}, answers, key), first, "old-v1")
    await unchanged(context, after_v2, "old-v1-replay-after-v2")
    proof("version-bound-key", definitionsIdentical=True, versionNumbers=[1, second_version["versionNo"]],
          changedVersionStatus=409, unknownVersionStatus=409, validNewKeyStatus=accepted_v2.status_code,
          originalResponseUnchanged=same_original, oldV1ReplayStatus=200,
          responseCount=len(after_v2["responses"]), submitSequence=state["submitSequence"])


async def test_equivalent_payloads_replay_after_close_without_a_live_fill_credential(context):
    headers, user = await account(context, "boundary_equivalent_owner")
    survey, version = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    original = {**original_answers(), "q_note": " \t ", "q_adult": {"inactive": "old-value"}}
    key = str(uuid4())
    first = await submit(context, headers, survey, attempt, original, key)
    assert first.status_code == 201
    closed = await context.client.post(f'/api/surveys/{survey["id"]}/close', headers=headers,
                                      json={"expectedSettingsRevision": survey["settingsRevision"]})
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    expired, _ = token(context.settings.fill_secret, "fill", user["id"], -10,
                       surveyId=survey["id"], versionId=version["id"])
    before = await snapshot(context)
    absent = {"versionId": version["id"]}
    cases = [
        ("choice-order", {**original, "q_hobby": ["sport", "read"]}, absent),
        ("object-order", dict(reversed(list(original.items()))), absent),
        ("equal-integer-float", {**original, "q_age": 17.0}, absent),
        ("negative-zero", {**original, "q_score": -0.0}, absent),
        ("trimmed-text", {**original, "q_minor": "学生"}, absent),
        ("empty-optional", {**original, "q_note": ""}, absent),
        ("null-optional", {**original, "q_note": None}, absent),
        ("omitted-optional", {k: v for k, v in original.items() if k != "q_note"}, absent),
        ("inactive-invalid-cache", {**original, "q_adult": ["invalid", "old"]}, absent),
        ("expired-fill-token", deepcopy(original), {**attempt, "fillToken": expired}),
    ]
    observed = []
    for label, equivalent, credential in cases:
        response = await submit(context, headers, survey, credential, equivalent, key)
        replay(response, first, label)
        await unchanged(context, before, label)
        observed.append({"label": label, "status": response.status_code,
                         "replayHeader": response.headers["Idempotent-Replay"]})
    conflict(await submit(context, headers, survey, absent, {**original, "q_score": 1}, key), "changed-after-close")
    await unchanged(context, before, "changed-after-close")
    new_after_close = await submit(context, headers, survey, attempt, original, str(uuid4()))
    assert new_after_close.status_code == 409 and new_after_close.json()["code"] == "SURVEY_CLOSED"
    await unchanged(context, before, "new-key-after-close")
    proof("equivalent-replay-after-close", replays=observed, changedExistingKeyStatus=409,
          newKeyStatus=new_after_close.status_code, newKeyCode=new_after_close.json()["code"],
          wholeDatabaseUnchanged=True, responseCount=len(before["responses"]),
          submitSequence=before["surveys"][0]["submitSequence"])


async def test_concurrent_different_payloads_on_one_key_commit_only_the_winner(context):
    headers, _ = await account(context, "boundary_race_owner")
    survey, _ = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    answers = [{**original_answers(), "q_score": 1, "q_minor": "A"},
               {**original_answers(), "q_score": 9, "q_minor": "B"}]
    key, arrived = str(uuid4()), 0
    both_ready, release = asyncio.Event(), asyncio.Event()

    async def barrier(**state):
        nonlocal arrived
        assert state["survey"]["_id"] == survey["id"]
        arrived += 1
        if arrived == 2:
            both_ready.set()
        await asyncio.wait_for(release.wait(), timeout=10)

    context.app.state.hooks["before_submission_transaction"] = barrier
    tasks = [asyncio.create_task(submit(context, headers, survey, attempt, payload, key)) for payload in answers]
    try:
        await asyncio.wait_for(both_ready.wait(), timeout=10)
        assert arrived == 2, "Both requests must pass the initial replay lookup before competing."
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.hooks.pop("before_submission_transaction", None)

    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 201)
    loser = 1 - winner
    conflict(responses[loser], "concurrent-loser")
    assert responses[winner].headers["Idempotent-Replay"] == "false"
    stored = await context.db.responses.find_one({"surveyId": survey["id"]})
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 1
    assert stored["_id"] == responses[winner].json()["receiptId"] and stored["acceptedSeq"] == 1
    values = {item["questionId"]: item["value"] for item in stored["answers"]}
    assert values == {"q_age": 17, "q_minor": answers[winner]["q_minor"],
                      "q_hobby": ["read", "sport"], "q_score": answers[winner]["q_score"], "q_note": "原建议"}
    state = await context.db.surveys.find_one({"_id": survey["id"]})
    assert state["submitSequence"] == 1 and state["policyLocked"] is True
    before = await snapshot(context)
    conflict(await submit(context, headers, survey, attempt, answers[loser], key), "loser-retry")
    replay(await submit(context, headers, survey, attempt, answers[winner], key), responses[winner], "winner-retry")
    await unchanged(context, before, "post-race-retries")
    proof("competing-payloads", arrivalsBeforeTransaction=arrived,
          requestStatuses=[response.status_code for response in responses], winnerIndex=winner,
          storedScore=values["q_score"], responseCount=len(before["responses"]),
          acceptedSeq=stored["acceptedSeq"], submitSequence=state["submitSequence"],
          postRaceRetriesUnchanged=True)
