"""Driver-boundary fault injection against real MongoDB transactions.

These tests deliberately inject labeled driver exceptions. They do not simulate
packet loss, disconnect a socket, or claim that an actual network fault occurred.
"""

import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import ConnectionFailure, OperationFailure

from app.security import utcnow
from conftest import account, published, start, submit
from scripts.fixtures import ROWS, example_definition


def transient_failure():
    # OperationFailure receives labels through details, unlike ConnectionFailure.
    failure = OperationFailure(
        "test injection after a real transactional gate write",
        code=112,
        details={"code": 112, "codeName": "WriteConflict", "errorLabels": ["TransientTransactionError"]},
    )
    assert failure.has_error_label("TransientTransactionError")
    return failure


async def test_transient_error_retries_body_without_consuming_sequence(context):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    key = str(uuid4())
    gate_sequences = []
    inserted = []

    async def after_gate(**state):
        gate_sequences.append(state["survey"]["submitSequence"])
        if len(gate_sequences) == 1:
            raise transient_failure()

    async def after_insert(**state):
        inserted.append(state["document"]["acceptedSeq"])

    context.app.state.hooks["after_submission_gate"] = after_gate
    context.app.state.hooks["after_response_insert"] = after_insert
    response = await submit(context, headers, survey, attempt, deepcopy(ROWS[0][2]), key)
    assert response.status_code == 201, response.text
    assert gate_sequences == [1, 1]  # First gate increment was actually rolled back.
    assert inserted == [1]
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert stored["submitSequence"] == 1 and stored["policyLocked"] is True
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 1
    saved = await context.db.responses.find_one({"surveyId": survey["id"], "submissionKey": key})
    assert saved["acceptedSeq"] == 1 and saved["_id"] == response.json()["receiptId"]
    replay = await submit(context, headers, survey, attempt, deepcopy(ROWS[0][2]), key)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert gate_sequences == [1, 1]  # Successful replay must not enter a transaction.
    print(json.dumps({"case": "transient_body_retry", "injectedTransientErrors": 1,
                      "gateSequences": gate_sequences, "responseInserts": inserted,
                      "finalSequence": stored["submitSequence"], "documents": 1,
                      "replayedReceiptMatches": True}))


async def test_transient_retry_rechecks_clock_and_rolls_back_expired_submission(context):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    requested_deadline = utcnow() + timedelta(minutes=5)
    setting = await context.client.patch(
        f'/api/surveys/{survey["id"]}/settings', headers=headers,
        json={"expectedSettingsRevision": survey["settingsRevision"],
              "deadlineAt": requested_deadline.isoformat()},
    )
    assert setting.status_code == 200, setting.text
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    deadline = stored["deadlineAt"]  # Use BSON's actual millisecond precision.
    clock = [deadline - timedelta(seconds=1)]
    context.app.state.clock = lambda: clock[0]
    gate_sequences = []
    gate_times = []
    inserted = []

    async def advance_clock_and_inject(**state):
        gate_sequences.append(state["survey"]["submitSequence"])
        gate_times.append(state["survey"]["updatedAt"])
        if len(gate_sequences) == 1:
            clock[0] = deadline + timedelta(seconds=1)
            raise transient_failure()

    async def after_insert(**state):
        inserted.append(state["document"]["acceptedSeq"])

    context.app.state.hooks["after_submission_gate"] = advance_clock_and_inject
    context.app.state.hooks["after_response_insert"] = after_insert
    response = await submit(context, headers, survey, attempt, deepcopy(ROWS[0][2]), str(uuid4()))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SURVEY_EXPIRED"
    assert gate_sequences == [1, 1]
    assert gate_times[0] < deadline < gate_times[1]
    assert inserted == []
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert stored["submitSequence"] == 0 and stored["policyLocked"] is False
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 0
    print(json.dumps({"case": "retry_crosses_deadline", "injectedTransientErrors": 1,
                      "gateSequences": gate_sequences,
                      "gateTimeRelativeToDeadline": ["before", "after"],
                      "httpStatus": response.status_code, "errorCode": response.json()["code"],
                      "finalSequence": 0, "policyLocked": False, "documents": 0}))


async def test_unknown_commit_result_retries_commit_without_rerunning_body(context, monkeypatch):
    headers, _ = await account(context)
    survey, _ = await published(context, headers, example_definition())
    attempt = await start(context, headers, survey)
    key = str(uuid4())
    gate_sequences = []
    inserted = []
    commit_calls = []
    persisted_before_error = []
    original_commit = AsyncClientSession.commit_transaction

    async def after_gate(**state):
        gate_sequences.append(state["survey"]["submitSequence"])

    async def after_insert(**state):
        inserted.append(state["document"]["acceptedSeq"])

    async def commit_then_lose_ack(session, *args, **kwargs):
        commit_calls.append(len(commit_calls) + 1)
        await original_commit(session, *args, **kwargs)  # Actual server commit succeeds.
        if len(commit_calls) == 1:
            # Prove persistence before injecting the synthetic driver-side error.
            count = await context.db.responses.count_documents({"surveyId": survey["id"]})
            persisted_before_error.append(count)
            assert count == 1
            failure = ConnectionFailure(
                "test injection after actual commit; acknowledgement treated as unknown",
                error_labels=["UnknownTransactionCommitResult"],
            )
            assert failure.has_error_label("UnknownTransactionCommitResult")
            raise failure

    context.app.state.hooks["after_submission_gate"] = after_gate
    context.app.state.hooks["after_response_insert"] = after_insert
    # Patch only after setup publication; it must affect this submission's commit.
    monkeypatch.setattr(AsyncClientSession, "commit_transaction", commit_then_lose_ack)
    response = await submit(context, headers, survey, attempt, deepcopy(ROWS[0][2]), key)
    assert response.status_code == 201, response.text
    assert commit_calls == [1, 2]
    assert persisted_before_error == [1]
    assert gate_sequences == inserted == [1]  # Only the commit was retried.
    stored = await context.db.surveys.find_one({"_id": survey["id"]})
    assert stored["submitSequence"] == 1 and stored["policyLocked"] is True
    assert await context.db.responses.count_documents({"surveyId": survey["id"]}) == 1
    replay = await submit(context, headers, survey, attempt, deepcopy(ROWS[0][2]), key)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert commit_calls == [1, 2] and gate_sequences == inserted == [1]
    print(json.dumps({"case": "unknown_commit_result", "commitCalls": len(commit_calls),
                      "bodyGateCalls": len(gate_sequences), "bodyInsertCalls": len(inserted),
                      "documentsVisibleBeforeInjection": persisted_before_error[0],
                      "finalSequence": stored["submitSequence"], "documents": 1,
                      "replayedReceiptMatches": True,
                      "faultKind": "synthetic_driver_boundary_error_after_successful_commit"}))
