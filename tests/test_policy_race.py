"""T17: a real settings write overlaps a submission holding the Mongo gate.

Only the existing testing hook pauses execution. HTTP, storage and transactions
remain real. A narrowly matched currentOp proves server arrival before release;
the short polling interval is not used as evidence of request ordering.
The test server must permit inspecting currentOp; the application does not need it.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from app.security import utcnow
from conftest import account, published, start, submit
from scripts.fixtures import example_definition


ANSWERS = {"q_age": 17, "q_hobby": ["read", "sport"], "q_score": 0}


async def snapshot(context):
    return {name: await context.db[name].find({}).sort("_id", 1).to_list(length=None)
            for name in ("users", "surveys", "survey_versions", "responses")}


async def same_documents(context, expected, label):
    # A failing assertion must not print password hashes or participant keys.
    equal = await snapshot(context) == expected
    assert equal, f"{label}: persisted business documents differ"


def proof(name, **details):
    print("POLICY_RACE_RESULT " + json.dumps({"scenario": name, **details}, ensure_ascii=False))


async def observe_settings_command(context, survey_id, target, pending):
    """Observe only this random database/survey/policy command, without logging ops."""
    async with asyncio.timeout(5):
        while True:
            assert not pending.done(), "Settings completed before the held transaction was released"
            result = await context.db.client.admin.command(
                {"currentOp": 1, "active": True, "command.$db": context.db.name})
            for operation in result.get("inprog", []):
                command = operation.get("command", {})
                if (command.get("findAndModify") == "surveys"
                        and command.get("query", {}).get("_id") == survey_id
                        and command.get("update", {}).get("$set", {}).get("allowMultiple") is target):
                    assert not pending.done()
                    return {"command": "findAndModify", "active": operation["active"],
                            "waitingForLock": operation.get("waitingForLock", False)}
            # Poll for an observed condition; elapsed time does not establish order.
            await asyncio.sleep(0.01)


@asynccontextmanager
async def held_submission(context, headers, survey, attempt):
    entered, release = asyncio.Event(), asyncio.Event()
    gate_states = []

    async def pause(**state):
        gate = state["survey"]
        gate_states.append({"allowMultiple": gate["allowMultiple"],
                            "policyLocked": gate["policyLocked"],
                            "submitSequence": gate["submitSequence"]})
        entered.set()
        await release.wait()

    context.app.state.hooks["after_submission_gate"] = pause
    tasks = []
    try:
        submission = asyncio.create_task(submit(context, headers, survey, attempt, ANSWERS))
        tasks.append(submission)
        await asyncio.wait_for(entered.wait(), 5)
        yield submission, release, gate_states, tasks
    finally:
        # Assertions/timeouts must also release/cancel owned requests before the
        # fixture drops its isolated database. Never leave a live transaction.
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        context.app.state.hooks.pop("after_submission_gate", None)


@pytest.mark.parametrize("multiple", [False, True], ids=["single-to-multiple", "multiple-to-single"])
async def test_committing_first_submission_rejects_overlapping_policy_change(context, multiple):
    headers, _ = await account(context, "policy_commit_owner")
    survey, _ = await published(context, headers, example_definition(), multiple=multiple)
    attempt = await start(context, headers, survey)
    before = await snapshot(context)
    target = not multiple
    endpoint = f'/api/surveys/{survey["id"]}/settings'

    async with held_submission(context, headers, survey, attempt) as (submission, release, gates, tasks):
        assert gates == [{"allowMultiple": multiple, "policyLocked": True, "submitSequence": 1}]
        # Uncommitted gate/sequence writes cannot leak to outside readers.
        await same_documents(context, before, "held-uncommitted-gate")
        settings = asyncio.create_task(context.client.patch(endpoint, headers=headers,
            json={"expectedSettingsRevision": survey["settingsRevision"], "allowMultiple": target}))
        tasks.append(settings)
        observed = await observe_settings_command(context, survey["id"], target, settings)
        await same_documents(context, before, "overlapping-write-before-release")
        assert not settings.done() and not submission.done()
        release.set()
        first, changed = await asyncio.wait_for(asyncio.gather(submission, settings), 10)

    assert first.status_code == 201, first.text
    assert changed.status_code == 409 and changed.json()["code"] == "POLICY_LOCKED", changed.text
    after = await snapshot(context)
    unrelated_equal = (after["users"] == before["users"]
                       and after["survey_versions"] == before["survey_versions"])
    assert unrelated_equal, "Submission/settings race changed users or published versions"
    expected_survey = deepcopy(before["surveys"][0])
    expected_survey.update(policyLocked=True, submitSequence=1, updatedAt=after["surveys"][0]["updatedAt"])
    survey_equal = after["surveys"] == [expected_survey]
    assert survey_equal, "Rejected policy change modified survey settings"
    assert len(after["responses"]) == 1
    saved = after["responses"][0]
    assert saved["_id"] == first.json()["receiptId"] and saved["acceptedSeq"] == 1
    assert ("oneTimeParticipantKey" in saved) is (not multiple)
    if not multiple:
        assert saved["oneTimeParticipantKey"] == saved["participantKey"]

    # A rejected mixed patch is atomic: its permitted deadline field must not
    # sneak through when the same request tries to change the locked policy.
    deadline = utcnow() + timedelta(days=1)
    mixed = await context.client.patch(endpoint, headers=headers,
        json={"expectedSettingsRevision": survey["settingsRevision"],
              "allowMultiple": target, "deadlineAt": deadline.isoformat()})
    assert mixed.status_code == 409 and mixed.json()["code"] == "POLICY_LOCKED"
    await same_documents(context, after, "rejected-mixed-patch")

    # Locking the multiple policy does not prohibit an otherwise valid deadline edit.
    deadline_only = await context.client.patch(endpoint, headers=headers,
        json={"expectedSettingsRevision": survey["settingsRevision"], "deadlineAt": deadline.isoformat()})
    assert deadline_only.status_code == 200, deadline_only.text
    actual_survey = await context.db.surveys.find_one({"_id": survey["id"]})
    assert actual_survey["settingsRevision"] == survey["settingsRevision"] + 1
    assert actual_survey["policyLocked"] and actual_survey["allowMultiple"] is multiple
    assert actual_survey["deadlineAt"].isoformat() == deadline_only.json()["deadlineAt"]
    after_deadline = await snapshot(context)
    expected_deadline = deepcopy(after)
    expected_deadline["surveys"][0].update(
        deadlineAt=actual_survey["deadlineAt"], updatedAt=actual_survey["updatedAt"],
        settingsRevision=survey["settingsRevision"] + 1)
    await same_documents(context, expected_deadline, "deadline-only-patch")

    second = await submit(context, headers, survey, attempt, ANSWERS)
    assert second.status_code == (201 if multiple else 409), second.text
    if not multiple:
        assert second.json()["code"] == "ALREADY_SUBMITTED"
        await same_documents(context, after_deadline, "single-policy-second-submission")
    else:
        second_state = await snapshot(context)
        assert len(second_state["responses"]) == 2
        assert second_state["surveys"][0]["submitSequence"] == 2
        second_saved = next(item for item in second_state["responses"] if item["_id"] == second.json()["receiptId"])
        assert second_saved["acceptedSeq"] == 2 and "oneTimeParticipantKey" not in second_saved
        original_equal = await context.db.responses.find_one({"_id": saved["_id"]}) == saved
        assert original_equal, "Second accepted submission changed the first response"

    proof("commit-then-policy-change", initialAllowMultiple=multiple, attemptedAllowMultiple=target,
          heldGate=gates, settingsCommandObserved=observed, outsideSnapshotUnchangedBeforeRelease=True,
          submitStatus=first.status_code, settingsStatus=changed.status_code,
          settingsCode=changed.json()["code"], settingsRevisionUnchangedAfterConflict=True,
          firstResponseCount=1, firstSequence=1, firstHasOneTimeKey="oneTimeParticipantKey" in saved,
          mixedPatchUnchanged=True, deadlineOnlyStatus=deadline_only.status_code,
          secondSubmitStatus=second.status_code, finalResponseCount=2 if multiple else 1,
          finalSequence=2 if multiple else 1)


async def test_rolled_back_first_submission_allows_overlapping_policy_change(context):
    headers, _ = await account(context, "policy_rollback_owner")
    survey, _ = await published(context, headers, example_definition(), multiple=True)
    attempt = await start(context, headers, survey)
    endpoint = f'/api/surveys/{survey["id"]}/settings'
    configured = await context.client.patch(endpoint, headers=headers,
        json={"expectedSettingsRevision": survey["settingsRevision"],
              "deadlineAt": (utcnow() + timedelta(minutes=5)).isoformat()})
    assert configured.status_code == 200, configured.text
    survey = configured.json()
    before = await snapshot(context)
    deadline = before["surveys"][0]["deadlineAt"]
    original_clock = context.app.state.clock
    clock = [deadline - timedelta(seconds=1)]
    context.app.state.clock = lambda: clock[0]
    try:
        async with held_submission(context, headers, survey, attempt) as (submission, release, gates, tasks):
            assert gates == [{"allowMultiple": True, "policyLocked": True, "submitSequence": 1}]
            await same_documents(context, before, "held-rollback-gate")
            settings = asyncio.create_task(context.client.patch(endpoint, headers=headers,
                json={"expectedSettingsRevision": survey["settingsRevision"], "allowMultiple": False}))
            tasks.append(settings)
            observed = await observe_settings_command(context, survey["id"], False, settings)
            await same_documents(context, before, "overlapping-write-before-rollback")
            assert not settings.done() and not submission.done()
            # Advance the existing server-clock closure, not Mongo or wall time.
            # After the gate, real check_open rejects and the real transaction aborts.
            clock[0] = deadline + timedelta(seconds=1)
            release.set()
            rejected, changed = await asyncio.wait_for(asyncio.gather(submission, settings), 10)
    finally:
        context.app.state.clock = original_clock

    assert rejected.status_code == 409 and rejected.json()["code"] == "SURVEY_EXPIRED", rejected.text
    assert changed.status_code == 200, changed.text
    expected = deepcopy(before)
    expected["surveys"][0].update(allowMultiple=False,
        settingsRevision=survey["settingsRevision"] + 1, updatedAt=deadline - timedelta(seconds=1))
    await same_documents(context, expected, "rollback-and-policy-change-final-state")
    assert changed.json()["policyLocked"] is False and changed.json()["submitSequence"] == 0
    assert changed.json()["settingsRevision"] == survey["settingsRevision"] + 1

    # After clearing the expired deadline, the first actual success must use
    # the newly committed single-response policy and lock it normally.
    reopened = await context.client.patch(endpoint, headers=headers,
        json={"expectedSettingsRevision": changed.json()["settingsRevision"], "deadlineAt": None})
    assert reopened.status_code == 200, reopened.text
    first_actual = await submit(context, headers, survey, attempt, ANSWERS)
    assert first_actual.status_code == 201, first_actual.text
    final = await context.db.surveys.find_one({"_id": survey["id"]})
    saved = await context.db.responses.find_one({})
    assert await context.db.responses.count_documents({}) == 1
    assert final["submitSequence"] == saved["acceptedSeq"] == 1
    assert final["policyLocked"] is True and final["allowMultiple"] is False
    assert saved["oneTimeParticipantKey"] == saved["participantKey"]
    after_success = await snapshot(context)
    second = await submit(context, headers, survey, attempt, ANSWERS)
    assert second.status_code == 409 and second.json()["code"] == "ALREADY_SUBMITTED"
    await same_documents(context, after_success, "new-single-policy-second-submission")
    proof("rollback-then-policy-change", initialAllowMultiple=True, attemptedAllowMultiple=False,
          heldGate=gates, settingsCommandObserved=observed, outsideSnapshotUnchangedBeforeRelease=True,
          submitStatus=rejected.status_code, submitCode=rejected.json()["code"],
          settingsStatus=changed.status_code, afterRollbackResponseCount=0, afterRollbackSequence=0,
          afterRollbackPolicyLocked=False, onlyExpectedSettingsFieldsChanged=True,
          settingsRevisionIncrement=1, clearDeadlineStatus=reopened.status_code,
          nextSubmitStatus=first_actual.status_code, nextHasOneTimeKey=True,
          nextAcceptedSequence=1, secondSubmitStatus=second.status_code, finalResponseCount=1)
