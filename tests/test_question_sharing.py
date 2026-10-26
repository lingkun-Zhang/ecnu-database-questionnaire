"""Series use grants through the real API and owned, isolated MongoDB fixture.

The race cases pause explicit test hooks around real transactional writes.
They do not fake a grant, a successful save, or a committed transaction.
"""

import asyncio
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from pymongo.errors import PyMongoError

from app.database import init_database
from conftest import account, published, start, submit
from scripts.fixtures import example_definition
from test_question_restore import restore
from test_question_sources import (
    COLLECTIONS, definition, draft, keys_in, publish, read_survey, save, snapshot, source, unchanged,
)
from test_question_versions import accepted, create_question, derive, number_content, rejected


ALL_COLLECTIONS = COLLECTIONS + ("question_shares",)
QUESTION_FIELDS = {"id", "currentVersionId", "lastVersionNo", "createdAt", "updatedAt"}
SHARE_FIELDS = {"id", "questionId", "recipientId", "active", "revision", "createdAt", "updatedAt",
                "permission", "versionScope"}
PRIVATE_FIELDS = {"ownerId", "passwordHash", "usernameKey", "requestDigest", "useSequence", "schemaVersion"}


async def share(context, owner, question, username="share_receiver", *, active=True, revision=0):
    return await context.client.put(f'/api/questions/{question["questionId"]}/shares', headers=owner,
        json={"recipientUsername": username, "active": active, "expectedRevision": revision})


def granted(response, question, recipient, *, active=True, revision=1, created=False, replay=False):
    assert response.status_code == (201 if created else 200), response.text
    assert (response.headers.get("Idempotent-Replay") == "true") is replay
    body = response.json()
    assert SHARE_FIELDS <= set(body) <= SHARE_FIELDS | {"recipientUsername"}
    assert body["questionId"] == question["questionId"] and body["recipientId"] == recipient["id"]
    assert body["active"] is active and body["revision"] == revision
    assert body["permission"] == "use" and body["versionScope"] == "series"
    assert not PRIVATE_FIELDS.intersection(keys_in(body))
    return body


async def parties(context, *, grant=True):
    owner, author = await account(context, "share_author")
    recipient, reader = await account(context, "share_receiver")
    first = accepted(await create_question(context, owner, number_content("共享评分 V1", min=0, max=10)), 1)
    authorization = granted(await share(context, owner, first), first, reader, created=True) if grant else None
    return owner, author, recipient, reader, first, authorization


async def empty_survey(context, owner, title="尚未引用的问卷"):
    response = await context.client.post("/api/surveys", headers=owner, json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


async def stored_share(context, question, recipient):
    return await context.db.question_shares.find_one({"questionId": question["questionId"], "recipientId": recipient["id"]})


async def all_unchanged(context, before):
    await unchanged(context, before, ALL_COLLECTIONS)


async def test_new_use_grant_has_explicit_series_scope_and_no_internal_fields(context):
    owner, author, recipient, reader, first, authorization = await parties(context)
    stored = await stored_share(context, first, reader)
    assert set(stored) == {"_id", "questionId", "ownerId", "recipientId", "active", "revision", "useSequence",
                           "requestDigest", "createdAt", "updatedAt", "schemaVersion", "permission", "versionScope"}
    assert stored["_id"] == authorization["id"] and stored["ownerId"] == author["id"]
    assert stored["useSequence"] == 0 and stored["revision"] == stored["schemaVersion"] == 1
    assert stored["permission"] == "use" and stored["versionScope"] == "series"
    assert len(stored["requestDigest"]) == 64
    assert stored["createdAt"] == stored["updatedAt"] == datetime.fromisoformat(authorization["createdAt"])
    root = await context.db.questions.find_one({"_id": first["questionId"]})
    assert root["ownerId"] == author["id"]
    assert (await context.client.get("/api/questions", headers=recipient)).json()["items"] == []


async def test_exact_current_request_replay_keeps_the_grant_document_unchanged(context):
    owner, _, _, reader, first, authorization = await parties(context)
    before = await snapshot(context, ALL_COLLECTIONS)
    replay = granted(await share(context, owner, first), first, reader, created=False, replay=True)
    assert replay == authorization
    await all_unchanged(context, before)


async def test_retrying_an_older_activation_cannot_resurrect_a_later_revocation(context):
    owner, _, _, reader, first, _ = await parties(context)
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await share(context, owner, first), "QUESTION_SHARE_CONFLICT")
    await all_unchanged(context, before)
    assert (await stored_share(context, first, reader))["active"] is False


async def test_regrant_updates_the_same_grant_and_replays_only_its_latest_request(context):
    owner, _, _, reader, first, first_grant = await parties(context)
    revoked = granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    renewed = granted(await share(context, owner, first, revision=2), first, reader, revision=3)
    assert first_grant["id"] == revoked["id"] == renewed["id"]
    assert first_grant["createdAt"] == renewed["createdAt"]
    assert await context.db.question_shares.count_documents({}) == 1
    before = await snapshot(context, ALL_COLLECTIONS)
    assert granted(await share(context, owner, first, revision=2), first, reader, revision=3, replay=True) == renewed
    rejected(await share(context, owner, first, active=False, revision=1), "QUESTION_SHARE_CONFLICT")
    await all_unchanged(context, before)


async def test_share_routes_require_login_and_do_not_reveal_foreign_questions_or_grants(context):
    owner, _, recipient, _, first, _ = await parties(context)
    outsider, _ = await account(context, "share_outsider")
    before = await snapshot(context, ALL_COLLECTIONS)
    base = f'/api/questions/{first["questionId"]}/shares'
    for method, path, body in (("GET", base, None), ("PUT", base, {"recipientUsername": "share_outsider", "active": True, "expectedRevision": 0}),
                               ("GET", "/api/shared-questions", None)):
        response = await context.client.request(method, path, json=body)
        rejected(response, "UNAUTHENTICATED", 401)
    for caller in (recipient, outsider):
        rejected(await context.client.get(base, headers=caller), "NOT_FOUND", 404)
        rejected(await share(context, caller, first, "share_outsider"), "NOT_FOUND", 404)
    rejected(await share(context, owner, {"questionId": str(uuid4())}), "NOT_FOUND", 404)
    await all_unchanged(context, before)


async def test_recipient_validation_and_revoking_a_missing_grant_never_create_records(context):
    owner, _, _, _, first, _ = await parties(context, grant=False)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await share(context, owner, first, "share_author"), "INVALID_SHARE_RECIPIENT", 422)
    rejected(await share(context, owner, first, "not_registered"), "RECIPIENT_NOT_FOUND", 404)
    rejected(await share(context, owner, first, "invalid recipient!"), "INVALID_USERNAME", 422)
    rejected(await share(context, owner, first, active=False), "SHARE_NOT_FOUND", 404)
    await all_unchanged(context, before)


async def test_share_request_shape_and_page_bounds_are_strict_without_writes(context):
    owner, _, recipient, _, first, _ = await parties(context, grant=False)
    before = await snapshot(context, ALL_COLLECTIONS)
    valid = {"recipientUsername": "share_receiver", "active": True, "expectedRevision": 0}
    invalid = [
        {**valid, "expectedRevision": -1}, {**valid, "expectedRevision": True},
        {**valid, "active": "true"}, {**valid, "ownerId": "forged"},
        {key: value for key, value in valid.items() if key != "expectedRevision"},
    ]
    for body in invalid:
        rejected(await context.client.put(f'/api/questions/{first["questionId"]}/shares', headers=owner, json=body),
                 "INVALID_REQUEST", 422)
    for path, caller in ((f'/api/questions/{first["questionId"]}/shares', owner), ("/api/shared-questions", recipient)):
        for params in ({"limit": 0}, {"limit": 101}, {"afterId": ""}, {"afterId": "x" * 161}):
            rejected(await context.client.get(path, headers=caller, params=params), "INVALID_REQUEST", 422)
    await all_unchanged(context, before)


async def test_owner_share_pages_include_revoked_grants_once_without_private_metadata(context):
    owner, _, _, reader, first, first_grant = await parties(context)
    recipients = [reader]
    for name in ("share_reader_two", "share_reader_three"):
        _, user = await account(context, name)
        recipients.append(user)
        granted(await share(context, owner, first, name), first, user, created=True)
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    seen, after = [], None
    for _ in range(3):
        params = {"limit": 1, **({"afterId": after} if after is not None else {})}
        result = await context.client.get(f'/api/questions/{first["questionId"]}/shares', headers=owner, params=params)
        assert result.status_code == 200, result.text
        body = result.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert SHARE_FIELDS <= set(item) <= SHARE_FIELDS | {"recipientUsername"}
        assert not PRIVATE_FIELDS.intersection(keys_in(item))
        seen.append(item)
        after = body["nextAfterId"]
    assert after is None
    assert [item["id"] for item in seen] == sorted(item["id"] for item in seen)
    assert {item["recipientId"] for item in seen} == {user["id"] for user in recipients}
    assert next(item for item in seen if item["id"] == first_grant["id"])["active"] is False


async def test_received_question_pages_exclude_revoked_and_other_recipients(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    other_headers, other = await account(context, "share_other_receiver")
    visible = [first["questionId"]]
    for index in range(2):
        version = accepted(await create_question(context, owner, number_content(f"分页共享{index}")), 1)
        granted(await share(context, owner, version), version, reader, created=True)
        visible.append(version["questionId"])
    revoked = accepted(await create_question(context, owner, number_content("已经撤销")), 1)
    granted(await share(context, owner, revoked), revoked, reader, created=True)
    granted(await share(context, owner, revoked, active=False, revision=1), revoked, reader, active=False, revision=2)
    hidden = accepted(await create_question(context, owner, number_content("只给另一人")), 1)
    granted(await share(context, owner, hidden, other["username"]), hidden, other, created=True)
    before = await snapshot(context, ALL_COLLECTIONS)
    seen, after = [], None
    for _ in range(3):
        response = await context.client.get("/api/shared-questions", headers=recipient,
            params={"limit": 1, **({"afterId": after} if after is not None else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 1 and set(body["items"][0]) == QUESTION_FIELDS
        seen.append(body["items"][0]["id"])
        after = body["nextAfterId"]
    assert after is None and len(set(seen)) == 3 and set(seen) == set(visible)
    assert (await context.client.get("/api/questions", headers=recipient)).json()["items"] == []
    assert (await context.client.get("/api/shared-questions", headers=owner)).json()["items"] == []
    assert [item["id"] for item in (await context.client.get("/api/shared-questions", headers=other_headers)).json()["items"]] == [hidden["questionId"]]
    await all_unchanged(context, before)


async def test_recipient_can_read_the_whole_series_including_versions_created_after_the_grant(context):
    owner, _, recipient, _, first, _ = await parties(context)
    second = accepted(await derive(context, owner, first, number_content("后来创建 V2", min=1, max=10)), 2)
    base = f'/api/questions/{first["questionId"]}'
    before = await snapshot(context, ALL_COLLECTIONS)
    root = await context.client.get(base, headers=recipient)
    assert root.status_code == 200 and set(root.json()) == QUESTION_FIELDS
    assert root.json()["currentVersionId"] == second["id"]
    versions = await context.client.get(base + "/versions", headers=recipient)
    assert versions.status_code == 200 and versions.json()["items"] == [second, first]
    for version in (first, second):
        response = await context.client.get(base + f'/versions/{version["id"]}', headers=recipient)
        assert response.status_code == 200 and response.json() == version
        assert not PRIVATE_FIELDS.intersection(keys_in(response.json()))
    await all_unchanged(context, before)


async def test_use_permission_cannot_derive_restore_or_grant_to_someone_else(context):
    owner, _, recipient, _, first, _ = await parties(context)
    await account(context, "share_third_person")
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await derive(context, recipient, first), "NOT_FOUND", 404)
    rejected(await restore(context, recipient, first), "NOT_FOUND", 404)
    rejected(await share(context, recipient, first, "share_third_person"), "NOT_FOUND", 404)
    rejected(await context.client.get(f'/api/questions/{first["questionId"]}/shares', headers=recipient), "NOT_FOUND", 404)
    await all_unchanged(context, before)


async def test_cross_user_reuse_pins_the_selected_version_and_keeps_the_survey_owned_by_its_creator(context):
    owner, author, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first, "b_score", "接收者自己的调查"), [source(first, "b_score")])
    frozen = await publish(context, recipient, survey)
    assert frozen.status_code == 201, frozen.text
    version = frozen.json()
    assert version["questionSources"] == [source(first, "b_score")]
    old = await snapshot(context, ("surveys", "survey_versions", "responses"))
    second = accepted(await derive(context, owner, first, number_content("作者新版本", min=3, max=10)), 2)
    await unchanged(context, old, ("surveys", "survey_versions", "responses"))
    assert (await context.db.surveys.find_one({"_id": survey["id"]}))["ownerId"] == reader["id"]
    assert (await context.db.questions.find_one({"_id": first["questionId"]}))["ownerId"] == author["id"]
    attempt = await start(context, recipient, survey)
    assert attempt["versionId"] == version["id"]
    answer = await submit(context, recipient, survey, attempt, {"b_score": 0})
    assert answer.status_code == 201, answer.text
    assert version["definition"]["questions"][0]["config"]["min"] == 0
    assert second["content"]["config"]["min"] == 3


async def test_usage_queries_show_only_the_callers_own_surveys_for_shared_questions(context):
    owner, _, recipient, _, first, _ = await parties(context)
    a = await draft(context, owner, definition(first, "a_score", "作者私人问卷"), [source(first, "a_score")])
    b = await draft(context, recipient, definition(first, "b_score", "接收者私人问卷"), [source(first, "b_score")])
    for caller, survey in ((owner, a), (recipient, b)):
        response = await publish(context, caller, survey)
        assert response.status_code == 201, response.text
    before = await snapshot(context, ALL_COLLECTIONS)
    for caller, mine, hidden in ((owner, a, b), (recipient, b, a)):
        for scope in ("draft", "published"):
            response = await context.client.get(f'/api/questions/{first["questionId"]}/usages', headers=caller,
                params={"scope": scope, "limit": 1, "questionVersionId": first["id"]})
            assert response.status_code == 200, response.text
            body = response.json()
            assert [item["surveyId"] for item in body["items"]] == [mine["id"]]
            assert body["nextAfterId"] is None
            assert hidden["id"] not in str(body) and hidden["draft"]["title"] not in str(body)
            assert not {"publicId", "answers", "identity", "ownerId"}.intersection(keys_in(body))
    await all_unchanged(context, before)


async def test_question_sharing_does_not_grant_survey_versions_statistics_responses_or_csv(context):
    owner, _, recipient, _, first, _ = await parties(context)
    surveys = []
    for caller, title in ((owner, "作者保密调查"), (recipient, "接收者保密调查")):
        survey = await draft(context, caller, definition(first, title=title), [source(first)])
        publication = await publish(context, caller, survey)
        assert publication.status_code == 201, publication.text
        surveys.append((caller, survey, publication.json()))
        attempt = await start(context, caller, survey)
        answer = await submit(context, caller, survey, attempt, {"score": 3}, anonymous=False)
        assert answer.status_code == 201, answer.text
    before = await snapshot(context, ALL_COLLECTIONS)
    for index, (survey_owner, survey, publication) in enumerate(surveys):
        foreign = surveys[1 - index][0]
        base = f'/api/surveys/{survey["id"]}'
        for suffix in ("", "/versions", f'/versions/{publication["id"]}', "/stats", "/responses", "/export"):
            own_response = await context.client.get(base + suffix, headers=survey_owner,
                params={"versionId": publication["id"]})
            assert own_response.status_code == 200, own_response.text
            response = await context.client.get(base + suffix, headers=foreign,
                params={"versionId": publication["id"]})
            rejected(response, "NOT_FOUND", 404)
            assert survey["draft"]["title"] not in response.text
    await all_unchanged(context, before)


async def test_revocation_hides_question_reads_and_usage_without_hiding_the_recipients_survey(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    before = await snapshot(context, ALL_COLLECTIONS)
    base = f'/api/questions/{first["questionId"]}'
    for suffix in ("", "/versions", f'/versions/{first["id"]}', "/usages"):
        rejected(await context.client.get(base + suffix, headers=recipient), "NOT_FOUND", 404)
    assert (await context.client.get("/api/shared-questions", headers=recipient)).json()["items"] == []
    assert (await read_survey(context, recipient, survey))["draftQuestionSources"] == [source(first)]
    await all_unchanged(context, before)


async def test_revoked_but_server_retained_binding_can_be_saved_and_published(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    retained = deepcopy(survey["draft"])
    retained["title"] = "撤销后继续整理已有问卷"
    retained["questions"][0]["required"] = False
    saved = await save(context, recipient, survey, retained, [source(first)])
    assert saved.status_code == 200, saved.text
    publication = await publish(context, recipient, saved.json())
    assert publication.status_code == 201, publication.text
    assert publication.json()["questionSources"] == [source(first)]
    assert publication.json()["definition"] == retained
    assert (await stored_share(context, first, reader))["active"] is False


async def test_revocation_blocks_the_same_version_in_a_new_survey_without_partial_save(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    fresh = await empty_survey(context, recipient)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await save(context, recipient, fresh, definition(first), [source(first)]), "NOT_FOUND", 404)
    await all_unchanged(context, before)


async def test_revocation_blocks_a_new_instance_even_when_its_version_is_already_retained(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    expanded = deepcopy(survey["draft"])
    expanded["questions"][0]["defaultTarget"] = "another_score"
    expanded["questions"].append({"id": "another_score", **deepcopy(first["content"]), "required": False,
                                  "defaultTarget": "END", "rules": []})
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await save(context, recipient, survey, expanded, [source(first), source(first, "another_score")]), "NOT_FOUND", 404)
    await all_unchanged(context, before)


async def test_revocation_blocks_switching_a_retained_instance_to_another_version(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    second = accepted(await derive(context, owner, first, number_content("共享评分 V2", min=1, max=10)), 2)
    survey = await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await save(context, recipient, survey, definition(second), [source(second)]), "NOT_FOUND", 404)
    await all_unchanged(context, before)


async def test_detaching_a_revoked_source_cannot_be_undone_without_a_new_grant(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    detached = await save(context, recipient, survey, survey["draft"], [])
    assert detached.status_code == 200 and not detached.json().get("draftQuestionSources")
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await save(context, recipient, detached.json(), detached.json()["draft"], [source(first)]), "NOT_FOUND", 404)
    await all_unchanged(context, before)
    granted(await share(context, owner, first, revision=2), first, reader, revision=3)
    attached = await save(context, recipient, detached.json(), detached.json()["draft"], [source(first)])
    assert attached.status_code == 200, attached.text
    assert attached.json()["draftQuestionSources"] == [source(first)]


async def test_retained_source_after_revocation_still_requires_exact_version_content(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first), [source(first)])
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    changed = deepcopy(survey["draft"])
    changed["questions"][0]["config"]["min"] = 3
    before = await snapshot(context, ALL_COLLECTIONS)
    rejected(await save(context, recipient, survey, changed, [source(first)]), "INVALID_QUESTION_SOURCE", 422)
    await all_unchanged(context, before)
    detached = await save(context, recipient, survey, changed, [])
    assert detached.status_code == 200, detached.text
    assert detached.json()["draft"]["questions"][0]["config"]["min"] == 3


async def test_revocation_preserves_published_snapshots_old_answers_and_outstanding_fill_tokens(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await draft(context, recipient, definition(first), [source(first)], multiple=True)
    publication = await publish(context, recipient, survey)
    assert publication.status_code == 201, publication.text
    pending = await start(context, recipient, survey)
    answer = await submit(context, recipient, survey, pending, {"score": 0})
    assert answer.status_code == 201, answer.text
    old = await snapshot(context, ("survey_versions", "responses"))
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)
    accepted(await derive(context, owner, first, number_content("限制更严的新题", min=3, max=10)), 2)
    await unchanged(context, old, ("survey_versions", "responses"))
    delayed = await submit(context, recipient, survey, pending, {"score": 0})
    assert delayed.status_code == 201 and delayed.json()["versionId"] == publication.json()["id"]
    newer_fill = await start(context, recipient, survey)
    assert newer_fill["versionId"] == publication.json()["id"]
    assert newer_fill["definition"]["questions"][0]["config"]["min"] == 0
    accepted_again = await submit(context, recipient, survey, newer_fill, {"score": 0})
    assert accepted_again.status_code == 201, accepted_again.text
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 3


async def test_use_gate_advances_its_sequence_without_invalidating_share_revision_cas(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    initial = await stored_share(context, first, reader)
    survey = await draft(context, recipient, definition(first), [source(first)])
    used = await stored_share(context, first, reader)
    assert used["useSequence"] > initial["useSequence"]
    expected = deepcopy(initial)
    expected["useSequence"] = used["useSequence"]
    assert used == expected
    publication = await publish(context, recipient, survey)
    assert publication.status_code == 201, publication.text
    granted(await share(context, owner, first, active=False, revision=1), first, reader, active=False, revision=2)


async def test_a_failure_after_the_use_gate_rolls_back_its_sequence_and_the_draft(context):
    _, _, recipient, reader, first, _ = await parties(context)
    survey = await empty_survey(context, recipient)
    invalid = definition(first)
    invalid["questions"][0]["config"]["min"] = 3
    entered = []

    async def observe_gate(**state):
        entered.append(state["question_id"])

    before = await snapshot(context, ALL_COLLECTIONS)
    context.app.state.hooks["after_question_share_gate"] = observe_gate
    try:
        rejected(await save(context, recipient, survey, invalid, [source(first)]), "INVALID_QUESTION_SOURCE", 422)
    finally:
        context.app.state.hooks.pop("after_question_share_gate", None)
    assert entered == [first["questionId"]]
    await all_unchanged(context, before)
    assert (await stored_share(context, first, reader))["useSequence"] == 0


async def test_concurrent_different_updates_with_the_same_revision_have_one_winner(context):
    owner, _, _, reader, first, _ = await parties(context)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def after_write(**state):
        document = state["document"]
        if document["questionId"] == first["questionId"] and document["revision"] == 2:
            entered.set()
            await release.wait()

    context.app.state.hooks["after_question_share_write"] = after_write
    try:
        tasks = [asyncio.create_task(share(context, owner, first, active=active, revision=1)) for active in (False, True)]
        await asyncio.wait_for(entered.wait(), 5)
        assert all(not task.done() for task in tasks)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.hooks.pop("after_question_share_write", None)
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json() for result in results if result.status_code == 200)
    rejected(next(result for result in results if result.status_code == 409), "QUESTION_SHARE_CONFLICT")
    stored = await stored_share(context, first, reader)
    assert stored["revision"] == 2 and stored["active"] is winner["active"]
    assert await context.db.question_shares.count_documents({}) == 1


async def test_revocation_committed_before_the_use_gate_rejects_the_entire_draft_save(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await empty_survey(context, recipient)
    original_survey = await context.db.surveys.find_one({"_id": survey["id"]})
    entered, release = asyncio.Event(), asyncio.Event()
    task = None

    async def before_gate(**state):
        assert state["question_id"] == first["questionId"]
        entered.set()
        await release.wait()

    context.app.state.hooks["before_question_share_gate"] = before_gate
    try:
        task = asyncio.create_task(save(context, recipient, survey, definition(first), [source(first)]))
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
        context.app.state.hooks.pop("before_question_share_gate", None)
    rejected(response, "NOT_FOUND", 404)
    assert await context.db.surveys.find_one({"_id": survey["id"]}) == original_survey
    assert (await stored_share(context, first, reader))["useSequence"] == 0


async def test_use_gate_committed_before_revocation_keeps_the_new_saved_binding(context):
    owner, _, recipient, reader, first, _ = await parties(context)
    survey = await empty_survey(context, recipient)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def after_gate(**state):
        assert state["question_id"] == first["questionId"]
        entered.set()
        await release.wait()

    context.app.state.hooks["after_question_share_gate"] = after_gate
    try:
        tasks.append(asyncio.create_task(save(context, recipient, survey, definition(first), [source(first)])))
        await asyncio.wait_for(entered.wait(), 5)
        tasks.append(asyncio.create_task(share(context, owner, first, active=False, revision=1)))
        await asyncio.sleep(0)
        assert all(not task.done() for task in tasks)
        release.set()
        saved, revoked = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.hooks.pop("after_question_share_gate", None)
    assert saved.status_code == 200, saved.text
    granted(revoked, first, reader, active=False, revision=2)
    assert saved.json()["draftQuestionSources"] == [source(first)]
    stored = await stored_share(context, first, reader)
    assert stored["useSequence"] == 1 and stored["active"] is False
    publication = await publish(context, recipient, saved.json())
    assert publication.status_code == 201, publication.text
    assert publication.json()["questionSources"] == [source(first)]


async def test_share_unique_pair_and_database_validator_reject_invalid_permission_or_counters(context):
    _, _, _, reader, first, _ = await parties(context)
    stored = await stored_share(context, first, reader)
    before = await snapshot(context, ALL_COLLECTIONS)
    code = None
    try:
        await context.db.question_shares.insert_one({**deepcopy(stored), "_id": str(uuid4())})
    except PyMongoError as error:
        code = getattr(error, "code", None)
    assert code == 11000
    for update in ({"$set": {"revision": True}}, {"$set": {"useSequence": -1}},
                   {"$set": {"active": "true"}}, {"$set": {"permission": "edit"}},
                   {"$set": {"versionScope": "single"}}, {"$unset": {"permission": ""}}):
        code = None
        try:
            await context.db.question_shares.update_one({"_id": stored["_id"]}, update)
        except PyMongoError as error:
            code = getattr(error, "code", None)
        assert code == 121, f"Expected validation rejection, received {code}"
    await all_unchanged(context, before)


async def test_adding_share_schema_and_repeated_init_preserve_all_existing_documents_and_validators(context):
    owner, _, _, reader, first, _ = await parties(context, grant=False)
    survey, _ = await published(context, owner, example_definition(), multiple=True)
    attempt = await start(context, owner, survey)
    response = await submit(context, owner, survey, attempt, {"q_age": 17, "q_hobby": ["read"], "q_score": 0})
    assert response.status_code == 201, response.text
    accepted(await derive(context, owner, first), 2)
    before = await snapshot(context)
    validators = {name: (await context.db[name].options())["validator"] for name in COLLECTIONS}
    # This owned database has no grants. Removing only its empty new collection
    # reproduces the six-collection pre-sharing layout without touching data.
    assert await context.db.question_shares.count_documents({}) == 0
    await context.db.question_shares.drop()
    await init_database(context.db)
    assert "question_shares" in await context.db.list_collection_names()
    await unchanged(context, before)
    assert {name: (await context.db[name].options())["validator"] for name in COLLECTIONS} == validators
    granted(await share(context, owner, first), first, reader, created=True)
    await unchanged(context, before)
    with_grant = await snapshot(context, ALL_COLLECTIONS)
    for _ in range(2):
        await init_database(context.db)
        await all_unchanged(context, with_grant)
        assert {name: (await context.db[name].options())["validator"] for name in COLLECTIONS} == validators
