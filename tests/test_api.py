import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pymongo.errors import WriteError

from app.main import create_app
from app.security import utcnow, token
from app.services import Services
from scripts.fixtures import example_definition, ROWS
from conftest import account, published, start, submit


def valid_answers():
    return {"q_age": 17, "q_hobby": ["read", "sport"], "q_score": 0}


async def test_registration_normalization_login_and_owner_isolation(context):
    headers, user = await account(context, "Owner_ABC")
    duplicate = await context.client.post("/api/auth/register", json={"username": "Ｏｗｎｅｒ_ＡＢＣ", "password": "TestPassword2026!"})
    assert duplicate.status_code == 409
    login = await context.client.post("/api/auth/login", json={"username": "owner_abc", "password": "TestPassword2026!"})
    assert login.status_code == 200
    assert "passwordHash" not in str(login.json())
    survey, version = await published(context, headers, example_definition())
    other, _ = await account(context, "other_user")
    for suffix in ["", "/versions", f'/versions/{version["id"]}', f'/stats?versionId={version["id"]}',
                   f'/responses?versionId={version["id"]}', f'/export?versionId={version["id"]}']:
        assert (await context.client.get(f'/api/surveys/{survey["id"]}{suffix}', headers=other)).status_code == 404
    assert (await context.client.get("/api/surveys")).status_code == 401
    assert (await context.client.get("/api/surveys", headers=other)).json()["items"] == []


async def test_participant_cannot_mutate_owner_survey_or_see_private_public_fields(context):
    owner, owner_user = await account(context, "secured_owner")
    participant, _ = await account(context, "public_participant")
    survey, version = await published(context, owner, example_definition(), multiple=True)
    # Both private identity and anonymous internal fields exist in the database.
    owner_attempt = await start(context, owner, survey)
    assert (await submit(context, owner, survey, owner_attempt, valid_answers(), anonymous=False)).status_code == 201
    participant_attempt = await start(context, participant, survey)
    assert (await submit(context, participant, survey, participant_attempt, valid_answers())).status_code == 201

    public = await context.client.get(f'/api/survey/{survey["publicId"]}', headers=participant)
    assert public.status_code == 200, public.text
    public_payloads = (public.json(), participant_attempt)

    def response_keys(value):
        if isinstance(value, dict):
            return set(value).union(*(response_keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(response_keys(item) for item in value))
        return set()

    private_fields = {"ownerId", "identity", "userId", "usernameAtSubmit", "respondentName",
                      "participantKey", "oneTimeParticipantKey", "payloadDigest", "passwordHash",
                      "auth_secret", "fill_secret", "participant_secret", "mongo_uri"}
    secrets = {name: getattr(context.settings, name)
               for name in ("auth_secret", "fill_secret", "participant_secret", "mongo_uri")}
    for payload in public_payloads:
        assert not response_keys(payload).intersection(private_fields)
        # Report labels rather than secret values if a regression leaks configuration.
        leaked_settings = {name for name, value in secrets.items() if value and value in str(payload)}
        assert not leaked_settings
        assert owner_user["id"] not in str(payload)
        assert owner_user["username"] not in str(payload)

    async def database_snapshot():
        return {
            "survey": await context.db.surveys.find_one({"_id": survey["id"]}),
            "versions": await context.db.survey_versions.find({"surveyId": survey["id"]})
                .sort("versionNo", 1).to_list(length=100),
            "responses": await context.db.responses.find({"surveyId": survey["id"]})
                .sort("acceptedSeq", 1).to_list(length=100),
        }

    before = await database_snapshot()
    assert before["survey"]["status"] == "open" and before["survey"]["submitSequence"] == 2
    assert len(before["versions"]) == 1 and len(before["responses"]) == 2
    current = before["survey"]
    attempted_definition = deepcopy(current["draft"])
    attempted_definition["title"] = "不应保存的跨账号修改"
    mutations = [
        ("PUT", "/draft", {"expectedDraftRevision": current["draftRevision"], "definition": attempted_definition}),
        ("POST", "/preview", {"definition": attempted_definition, "answers": valid_answers(), "mode": "final"}),
        ("POST", "/publish", {"expectedDraftRevision": current["draftRevision"],
                               "expectedCurrentVersionId": version["id"], "publishKey": str(uuid4())}),
        ("PATCH", "/settings", {"expectedSettingsRevision": current["settingsRevision"],
                                 "deadlineAt": (utcnow() + timedelta(days=1)).isoformat()}),
        ("POST", "/close", {"expectedSettingsRevision": current["settingsRevision"]}),
    ]
    endpoint = f'/api/surveys/{survey["id"]}'
    for method, suffix, body in mutations:
        rejected = await context.client.request(method, endpoint + suffix, headers=participant, json=body)
        assert rejected.status_code == 404, (method, suffix, rejected.text)
        assert rejected.json()["code"] == "NOT_FOUND"
        # Full document equality includes draft/settings revisions, current version,
        # lifecycle, policy lock, coordination sequence, dates and both answer kinds.
        assert await database_snapshot() == before, f"{method} {suffix} changed owner data"
    detail = await context.client.get(endpoint, headers=owner)
    assert detail.status_code == 200 and detail.json()["draft"] == current["draft"]


async def test_draft_cas_and_publish_cas_idempotency(context):
    headers, _ = await account(context)
    survey, version = await published(context, headers, example_definition())
    definition = example_definition()
    definition["title"] = "修改后的问卷"
    endpoint = f'/api/surveys/{survey["id"]}'
    data = {"definition": definition, "expectedDraftRevision": survey["draftRevision"]}
    assert (await context.client.put(endpoint + "/draft", headers=headers, json=data)).status_code == 200
    assert (await context.client.put(endpoint + "/draft", headers=headers, json=data)).status_code == 409
    stale = {"expectedDraftRevision": survey["draftRevision"], "expectedCurrentVersionId": version["id"], "publishKey": str(uuid4())}
    assert (await context.client.post(endpoint + "/publish", headers=headers, json=stale)).status_code == 409
    stale["expectedDraftRevision"] += 1
    first = await context.client.post(endpoint + "/publish", headers=headers, json=stale)
    replay = await context.client.post(endpoint + "/publish", headers=headers, json=stale)
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json()
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 2


async def test_publish_transaction_rolls_back_snapshot_and_pointer(context):
    headers, _ = await account(context)
    definition = example_definition()
    response = await context.client.post("/api/surveys", headers=headers, json={"title": definition["title"]})
    survey = response.json()
    await context.client.put(f'/api/surveys/{survey["id"]}/draft', headers=headers,
                             json={"definition": definition, "expectedDraftRevision": 0})
    async def fail(**kwargs):
        from app.services import ServiceError
        raise ServiceError("INJECTED_FAILURE", "测试故障", 503)
    context.app.state.hooks["after_version_insert"] = fail
    response = await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
        json={"expectedDraftRevision": 1, "expectedCurrentVersionId": None, "publishKey": str(uuid4())})
    assert response.status_code == 503
    assert await context.db.survey_versions.count_documents({"surveyId": survey["id"]}) == 0
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert stored["currentVersionId"] is None and stored["lastVersionNo"] == 0


async def test_six_response_fixture_statistics_details_and_privacy(context):
    owner, _ = await account(context)
    survey, version = await published(context, owner, example_definition(), multiple=True)
    accounts = {}
    for username, anonymous, answers in ROWS:
        if username not in accounts:
            accounts[username], _ = await account(context, username)
        headers = accounts[username]
        attempt = await start(context, headers, survey)
        response = await submit(context, headers, survey, attempt, answers, anonymous=anonymous)
        assert response.status_code == 201, response.text
    response = await context.client.get(f'/api/surveys/{survey["id"]}/stats', headers=owner, params={"versionId": version["id"]})
    assert response.status_code == 200, response.text
    stats = response.json()
    assert (stats["totalResponses"], stats["uniqueParticipants"], stats["anonymousResponses"]) == (6, 5, 3)
    questions = {q["questionId"]: q for q in stats["questions"]}
    assert questions["q_score"]["avg"] == 5.6
    assert questions["q_score"]["A"] == 5
    assert questions["q_age"]["sum"] == 101
    assert questions["q_age"]["avg"] == pytest.approx(101 / 6)
    assert (questions["q_minor"]["V"], questions["q_minor"]["U"], questions["q_minor"]["S"]) == (3, 1, 3)
    assert {o["id"]: o["count"] for o in questions["q_hobby"]["options"]} == {"read": 3, "sport": 3, "music": 3}
    assert questions["q_hobby"]["options"][0]["uniqueParticipants"] == 2
    for question in questions.values():
        assert 0 <= question["A"] <= question["V"] <= question["R"]
        assert question["R"] == question["A"] + question["U"] + question["S"]
    detail = (await context.client.get(f'/api/surveys/{survey["id"]}/responses', headers=owner,
              params={"versionId": version["id"], "questionId": "q_score"})).json()
    assert len(detail["items"]) == 5
    assert any(a["value"] == 0 for item in detail["items"] for a in item["answers"] if a["questionId"] == "q_score")
    for item in detail["items"]:
        assert not {"participantKey", "identity", "oneTimeParticipantKey", "payloadDigest"}.intersection(item)
        assert ("respondentName" not in item) if item["anonymous"] else bool(item["respondentName"])
    exported = await context.client.get(f'/api/surveys/{survey["id"]}/export', headers=owner, params={"versionId": version["id"]})
    assert exported.status_code == 200 and "participantKey" not in exported.text
    assert "课程很好" in exported.text


async def test_preview_evaluation_submit_and_inactive_old_values_agree(context):
    headers, _ = await account(context)
    survey, version = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    answers = {**valid_answers(), "q_adult": ["invalid-but-inactive"]}
    preview = await context.client.post(f'/api/surveys/{survey["id"]}/preview', headers=headers,
               json={"definition": example_definition(), "answers": answers, "mode": "final"})
    live = await context.client.post(f'/api/survey/{survey["publicId"]}/evaluate',
        headers={**headers, "X-Fill-Token": attempt["fillToken"]}, json={"versionId": version["id"], "answers": answers})
    response = await submit(context, headers, survey, attempt, answers)
    assert preview.status_code == live.status_code == 200 and response.status_code == 201
    assert preview.json()["path"] == live.json()["path"]
    stored = await context.db.responses.find_one({"surveyId": survey["id"]})
    assert "q_adult" not in stored["path"] and all(a["questionId"] != "q_adult" for a in stored["answers"])


async def test_pending_required_answer_is_not_default_branch(context):
    headers, _ = await account(context)
    survey, version = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    response = await context.client.post(f'/api/survey/{survey["publicId"]}/evaluate',
        headers={**headers, "X-Fill-Token": attempt["fillToken"]}, json={"versionId": version["id"], "answers": {}})
    assert response.json()["path"] == ["q_age"] and response.json()["blockedAt"] == "q_age"


@pytest.mark.parametrize("bad", [{"q_age": True}, {"q_age": "18"}, {"q_age": 18.5},
    {"q_age": 17, "q_hobby": ["read", "read"]}, {**valid_answers(), "q_score": 11}])
async def test_invalid_answers_never_persist(context, bad):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    response = await submit(context, headers, survey, attempt, bad)
    assert response.status_code == 422, response.text
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert stored["submitSequence"] == 0 and stored["policyLocked"] is False
    assert await context.db.responses.count_documents({}) == 0


async def test_twenty_identical_requests_have_one_receipt(context):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    key = str(uuid4())
    responses = await asyncio.gather(*(submit(context, headers, survey, attempt, valid_answers(), key) for _ in range(20)))
    assert all(r.status_code in {200, 201} for r in responses), [(r.status_code, r.text) for r in responses]
    assert len({r.json()["receiptId"] for r in responses}) == 1
    assert await context.db.responses.count_documents({}) == 1
    assert (await context.db.surveys.find_one({"_id": survey["id"]}))["submitSequence"] == 1


@pytest.mark.parametrize("multiple,expected", [(False, 1), (True, 20)])
async def test_twenty_distinct_keys_respect_multiple_policy(context, multiple, expected):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition(), multiple)
    attempt = await start(context, headers, survey)
    responses = await asyncio.gather(*(submit(context, headers, survey, attempt, valid_answers()) for _ in range(20)))
    assert sum(r.status_code == 201 for r in responses) == expected, [(r.status_code, r.text) for r in responses]
    assert all(r.status_code in {201, 409} for r in responses)
    assert await context.db.responses.count_documents({}) == expected


async def test_idempotency_includes_anonymity_and_can_replay_expired_token_after_close(context):
    headers, user = await account(context)
    survey, version = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    key = str(uuid4())
    first = await submit(context, headers, survey, attempt, valid_answers(), key, anonymous=False)
    assert first.status_code == 201
    changed = await submit(context, headers, survey, attempt, valid_answers(), key, anonymous=True)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    closed = await context.client.post(f'/api/surveys/{survey["id"]}/close', headers=headers,
                                       json={"expectedSettingsRevision": survey["settingsRevision"]})
    assert closed.status_code == 200
    expired, _ = token(context.settings.fill_secret, "fill", user["id"], -10, surveyId=survey["id"], versionId=version["id"])
    retry = await submit(context, headers, survey, {**attempt, "fillToken": expired}, valid_answers(), key, anonymous=False)
    assert retry.status_code == 200 and retry.json() == first.json()
    absent = await submit(context, headers, survey, {"versionId": version["id"]}, valid_answers(), key, anonymous=False)
    assert absent.status_code == 200 and absent.json() == first.json()
    assert (await submit(context, headers, survey, attempt, valid_answers())).status_code == 409


async def test_lost_committed_submit_response_recovers_after_new_app_login_and_close(context):
    """Inject a lost HTTP acknowledgement after the real ASGI app returns 201.

    This uses real MongoDB and a synthetic HTTPX transport read failure, not a
    socket/network outage or OS process restart. The replacement application
    runs a new lifespan and Mongo client with the same persistent configuration;
    the fixture's original app remains available for owner close and DB cleanup.
    The intercepted receipt is a test oracle only, never an input to recovery.
    """
    headers, user = await account(context)
    survey, version = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    endpoint = f'/api/survey/{survey["publicId"]}/submit'
    retained_key = str(uuid4())
    retained_payload = {"versionId": attempt["versionId"], "anonymous": True,
                        "answers": deepcopy(valid_answers())}

    class DropCommittedAcknowledgement(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=context.app)
            self.dropped = 0
            self.server_receipt = None

        async def handle_async_request(self, request):
            # First await the whole real ASGI call; an exception before this
            # boundary would not prove loss of an already committed success.
            response = await self.inner.handle_async_request(request)
            if request.method == "POST" and request.url.path == endpoint and not self.dropped:
                await response.aread()
                assert response.status_code == 201, response.text
                assert response.headers["Idempotent-Replay"] == "false"
                self.server_receipt = response.json()
                stored = await context.db.responses.find_one({"_id": self.server_receipt["receiptId"]})
                assert stored is not None and stored["submissionKey"] == retained_key
                gate = await context.db.surveys.find_one({"_id": survey["id"]})
                assert gate["submitSequence"] == 1
                self.dropped += 1
                await response.aclose()
                raise httpx.ReadError("Injected loss of completed ASGI 201 acknowledgement", request=request)
            return response

        async def aclose(self):
            await self.inner.aclose()

    transport = DropCommittedAcknowledgement()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as first_client:
        with pytest.raises(httpx.ReadError, match="completed ASGI 201"):
            await first_client.post(endpoint,
                headers={**headers, "Idempotency-Key": retained_key, "X-Fill-Token": attempt["fillToken"]},
                json=deepcopy(retained_payload))
    assert transport.dropped == 1 and transport.server_receipt is not None
    committed_before_recovery = await context.db.responses.find_one({"surveyId": survey["id"]})
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 1

    closed = await context.client.post(f'/api/surveys/{survey["id"]}/close', headers=headers,
                                       json={"expectedSettingsRevision": survey["settingsRevision"]})
    assert closed.status_code == 200 and closed.json()["status"] == "closed"

    replacement = create_app(context.settings)
    assert replacement is not context.app
    async with LifespanManager(replacement):
        assert replacement.state.db.client is not context.db.client
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=replacement), base_url="http://test") as recovered_client:
            login = await recovered_client.post("/api/auth/login",
                json={"username": user["username"], "password": "TestPassword2026!"})
            assert login.status_code == 200, login.text
            assert login.json()["user"]["id"] == user["id"]
            # No fill credential or new start request: closed collection must
            # not prevent recovery of the committed original request.
            recovered = await recovered_client.post(endpoint,
                headers={"Authorization": "Bearer " + login.json()["accessToken"],
                         "Idempotency-Key": retained_key}, json=deepcopy(retained_payload))
            assert recovered.status_code == 200, recovered.text
            assert recovered.headers["Idempotent-Replay"] == "true"
            assert recovered.json() == transport.server_receipt
            assert recovered.json()["versionId"] == version["id"]
            assert recovered.json()["anonymous"] is True

        stored_survey = await replacement.state.db.surveys.find_one({"_id": survey["id"]})
        assert stored_survey["status"] == "closed" and stored_survey["submitSequence"] == 1
        assert await replacement.state.db.responses.count_documents({"surveyId": survey["id"]}) == 1
        stored = await replacement.state.db.responses.find_one({"surveyId": survey["id"]})
        assert stored == committed_before_recovery
        assert stored["_id"] == recovered.json()["receiptId"]
        assert stored["versionId"] == retained_payload["versionId"] == version["id"]
        assert stored["submissionKey"] == retained_key and stored["acceptedSeq"] == 1
        assert stored["anonymous"] is True and "identity" not in stored


async def test_old_version_fill_survives_v2_and_repeat_policy_spans_versions(context):
    headers, _ = await account(context)
    survey, version = await published(context, headers, example_definition())
    attempt_v1 = await start(context, headers, survey)
    definition_v2 = example_definition()
    definition_v2["questions"][0]["config"]["max"] = 100
    await context.client.put(f'/api/surveys/{survey["id"]}/draft', headers=headers,
        json={"definition": definition_v2, "expectedDraftRevision": survey["draftRevision"]})
    newer = await context.client.post(f'/api/surveys/{survey["id"]}/publish', headers=headers,
        json={"expectedDraftRevision": survey["draftRevision"] + 1, "expectedCurrentVersionId": version["id"], "publishKey": str(uuid4())})
    assert newer.status_code == 201
    first = await submit(context, headers, survey, attempt_v1, valid_answers())
    assert first.status_code == 201 and first.json()["versionId"] == version["id"]
    attempt_v2 = await start(context, headers, survey)
    assert attempt_v2["versionId"] == newer.json()["id"]
    assert (await submit(context, headers, survey, attempt_v2, valid_answers())).status_code == 409
    old = await context.db.survey_versions.find_one({"_id": version["id"]})
    assert old["definition"]["questions"][0]["config"]["max"] == 120


async def test_setting_policy_before_first_commit_uses_latest_inside_transaction(context):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause(**kwargs):
        entered.set()
        await release.wait()
    context.app.state.hooks["before_submission_transaction"] = pause
    pending = asyncio.create_task(submit(context, headers, survey, attempt, valid_answers()))
    await asyncio.wait_for(entered.wait(), 5)
    changed = await context.client.patch(f'/api/surveys/{survey["id"]}/settings', headers=headers,
        json={"expectedSettingsRevision": survey["settingsRevision"], "allowMultiple": False})
    assert changed.status_code == 200
    release.set()
    assert (await pending).status_code == 201
    context.app.state.hooks.clear()
    stored = await context.db.responses.find_one({})
    assert stored["oneTimeParticipantKey"] == stored["participantKey"]
    assert (await submit(context, headers, survey, attempt, valid_answers())).status_code == 409
    locked = await context.client.patch(f'/api/surveys/{survey["id"]}/settings', headers=headers,
        json={"expectedSettingsRevision": changed.json()["settingsRevision"], "allowMultiple": True})
    assert locked.status_code == 409 and locked.json()["code"] == "POLICY_LOCKED"


@pytest.mark.parametrize("submit_first", [True, False])
async def test_close_and_submit_controlled_order(context, submit_first):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    endpoint = f'/api/surveys/{survey["id"]}/close'
    if submit_first:
        entered, release = asyncio.Event(), asyncio.Event()
        async def pause(**kwargs):
            entered.set()
            await release.wait()
        context.app.state.hooks["after_submission_gate"] = pause
        pending = asyncio.create_task(submit(context, headers, survey, attempt, valid_answers()))
        await asyncio.wait_for(entered.wait(), 5)
        close_task = asyncio.create_task(context.client.post(endpoint, headers=headers,
                                     json={"expectedSettingsRevision": survey["settingsRevision"]}))
        release.set()
        assert (await pending).status_code == 201
        assert (await close_task).status_code == 200
    else:
        assert (await context.client.post(endpoint, headers=headers,
                json={"expectedSettingsRevision": survey["settingsRevision"]})).status_code == 200
        assert (await submit(context, headers, survey, attempt, valid_answers())).status_code == 409


@pytest.mark.parametrize("delta,accepted", [(-1, True), (0, False), (1, False)])
async def test_deadline_boundary_uses_server_clock(context, delta, accepted):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    deadline = utcnow() + timedelta(minutes=5)
    response = await context.client.patch(f'/api/surveys/{survey["id"]}/settings', headers=headers,
            json={"expectedSettingsRevision": survey["settingsRevision"], "deadlineAt": deadline.isoformat()})
    assert response.status_code == 200
    # BSON dates have millisecond resolution. Use the stored date for exact equality.
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    context.app.state.clock = lambda: stored["deadlineAt"] + timedelta(seconds=delta)
    submitted = await submit(context, headers, survey, attempt, valid_answers())
    assert submitted.status_code == (201 if accepted else 409), submitted.text


async def test_gate_wait_crossing_deadline_aborts_all_writes(context):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    deadline = utcnow() + timedelta(minutes=5)
    await context.client.patch(f'/api/surveys/{survey["id"]}/settings', headers=headers,
        json={"expectedSettingsRevision": survey["settingsRevision"], "deadlineAt": deadline.isoformat()})
    context.app.state.clock = lambda: deadline - timedelta(seconds=1)
    async def cross(**kwargs):
        context.app.state.clock = lambda: deadline + timedelta(seconds=1)
        # The service receives a clock callable at request entry, so advance its closure instead.
        clock_value[0] = deadline + timedelta(seconds=1)
    clock_value = [deadline - timedelta(seconds=1)]
    context.app.state.clock = lambda: clock_value[0]
    context.app.state.hooks["after_submission_gate"] = cross
    response = await submit(context, headers, survey, attempt, valid_answers())
    assert response.status_code == 409
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert stored["submitSequence"] == 0 and not stored["policyLocked"]


async def test_snapshot_pagination_is_stable_and_csv_formula_is_neutralized(context):
    headers, _ = await account(context)
    survey, version = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    await submit(context, headers, survey, attempt, {**valid_answers(), "q_note": "=1+1"})
    stats = (await context.client.get(f'/api/surveys/{survey["id"]}/stats', headers=headers,
              params={"versionId": version["id"]})).json()
    await submit(context, headers, survey, attempt, valid_answers())
    frozen = await context.client.get(f'/api/surveys/{survey["id"]}/responses', headers=headers,
        params={"versionId": version["id"], "asOfSeq": stats["asOfSeq"], "limit": 1})
    assert len(frozen.json()["items"]) == 1 and frozen.json()["nextCursor"] is None
    refreshed = (await context.client.get(f'/api/surveys/{survey["id"]}/stats', headers=headers,
                 params={"versionId": version["id"]})).json()
    assert refreshed["totalResponses"] == 2
    exported = await context.client.get(f'/api/surveys/{survey["id"]}/export', headers=headers,
        params={"versionId": version["id"], "asOfSeq": stats["asOfSeq"]})
    assert "'=1+1" in exported.text
    raw = await context.db.responses.find_one({"answers.questionId": "q_note"})
    assert next(a["value"] for a in raw["answers"] if a["questionId"] == "q_note") == "=1+1"


async def test_expired_or_other_user_fill_token_cannot_create_response(context):
    headers, user = await account(context)
    survey, version = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    other, _ = await account(context, "different_user")
    assert (await submit(context, other, survey, attempt, valid_answers())).status_code == 403
    expired, _ = token(context.settings.fill_secret, "fill", user["id"], -10, surveyId=survey["id"], versionId=version["id"])
    assert (await submit(context, headers, survey, {**attempt, "fillToken": expired}, valid_answers())).status_code == 409
    assert await context.db.responses.count_documents({}) == 0
